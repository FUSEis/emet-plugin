#!/usr/bin/env python3
"""Claude Code hook that sends each finished turn to Emet as one envelope.

Standard library only. The hook returns at once and a detached worker reads the
session transcript, builds the envelope and posts it. Failed sends are queued
and retried on the next hook.
"""

from __future__ import annotations

import fcntl
import hashlib
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

SEMCONV_VERSION = "emet-genai-1"
PLUGIN_VERSION = "0.0.1"
REQUEST_TIMEOUT_S = 10
# Give Claude Code a moment to flush the last transcript lines after Stop
FLUSH_WAIT_S = 0.5


def config() -> dict[str, str]:
    """Reads plugin options, falling back to EMET_* env vars."""

    def pick(option: str, env: str, default: str = "") -> str:
        return os.environ.get(f"CLAUDE_PLUGIN_OPTION_{option}") or os.environ.get(env) or default

    return {
        "api_key": pick("API_KEY", "EMET_API_KEY"),
        "tenant_ref": pick("TENANT_REF", "EMET_TENANT_REF"),
        "endpoint": pick("ENDPOINT", "EMET_ENDPOINT", "https://app.emet.so").rstrip("/"),
    }


def data_dir() -> Path:
    root = os.environ.get("CLAUDE_PLUGIN_DATA") or str(Path.home() / ".emet-claude-code")
    path = Path(root)
    (path / "state").mkdir(parents=True, exist_ok=True)
    (path / "queue").mkdir(parents=True, exist_ok=True)
    return path


def log(message: str) -> None:
    try:
        with open(data_dir() / "emet.log", "a") as handle:
            handle.write(f"{time.strftime('%Y-%m-%dT%H:%M:%S')} {message}\n")
    except OSError:
        pass


# Transcript parsing


def read_entries(transcript: Path, start_line: int) -> tuple[list[dict[str, Any]], int]:
    """Returns the user and assistant entries after start_line and the new line count."""
    entries: list[dict[str, Any]] = []
    count = 0
    with open(transcript) as handle:
        for count, line in enumerate(handle, start=1):
            if count <= start_line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("type") in ("user", "assistant") and not entry.get("isSidechain"):
                entries.append(entry)
    return entries, max(count, start_line)


def content_parts(entry: dict[str, Any]) -> list[dict[str, Any]]:
    content = entry.get("message", {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    return [part for part in content or [] if isinstance(part, dict)]


def is_prompt(entry: dict[str, Any]) -> bool:
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    return not any(part.get("type") == "tool_result" for part in content_parts(entry))


def prompt_text(entry: dict[str, Any]) -> str:
    return "\n".join(part.get("text", "") for part in content_parts(entry) if part.get("type") == "text")


def split_turns(entries: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    """Groups entries into turns, each starting at a typed prompt."""
    turns: list[list[dict[str, Any]]] = []
    for entry in entries:
        if is_prompt(entry):
            turns.append([entry])
        elif turns:
            turns[-1].append(entry)
    return turns


def as_object_or_string(value: Any) -> Any:
    if value is None or isinstance(value, dict | str):
        return value
    return json.dumps(value, ensure_ascii=False)


def tool_result_text(part: dict[str, Any]) -> Any:
    content = part.get("content")
    if isinstance(content, list):
        return "\n".join(item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text")
    return content


def strip_nul(value: Any) -> Any:
    """Emet refuses NUL characters anywhere in a payload."""
    if isinstance(value, str):
        return value.replace("\x00", "")
    if isinstance(value, dict):
        return {strip_nul(k): strip_nul(v) for k, v in value.items()}
    if isinstance(value, list):
        return [strip_nul(v) for v in value]
    return value


def build_spans(turn: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Builds the turn, model and tool spans for one turn."""
    prompt = turn[0]
    user_message = {"role": "user", "parts": [{"type": "text", "content": prompt_text(prompt)}]}
    spans: list[dict[str, Any]] = []

    # One API response is spread over several entries that share message.id
    calls: list[dict[str, Any]] = []
    by_id: dict[str, dict[str, Any]] = {}
    tool_calls: dict[str, dict[str, Any]] = {}
    pending_inputs: list[dict[str, Any]] = []
    previous_ts = prompt["timestamp"]

    for entry in turn[1:]:
        if entry["type"] == "assistant":
            message = entry.get("message", {})
            message_id = message.get("id") or entry.get("uuid")
            call = by_id.get(message_id)
            if call is None:
                call = {
                    "id": message_id,
                    "started_at": previous_ts,
                    "ended_at": entry["timestamp"],
                    "model": message.get("model", ""),
                    "usage": message.get("usage") or {},
                    "stop_reason": message.get("stop_reason") or "",
                    "inputs": pending_inputs,
                    "parts": [],
                }
                pending_inputs = []
                by_id[message_id] = call
                calls.append(call)
            call["ended_at"] = entry["timestamp"]
            call["usage"] = message.get("usage") or call["usage"]
            call["stop_reason"] = message.get("stop_reason") or call["stop_reason"]
            for part in content_parts(entry):
                if part.get("type") == "text" and part.get("text"):
                    call["parts"].append({"type": "text", "content": part["text"]})
                elif part.get("type") == "tool_use":
                    call["parts"].append(
                        {"type": "tool_call", "id": part.get("id"), "name": part.get("name"), "arguments": part.get("input")}
                    )
                    tool_calls[part["id"]] = {"name": part.get("name", ""), "input": part.get("input"), "started_at": entry["timestamp"]}
        else:
            for part in content_parts(entry):
                if part.get("type") != "tool_result":
                    continue
                call_id = part.get("tool_use_id", "")
                result = entry.get("toolUseResult")
                if result is None or isinstance(result, str):
                    result = tool_result_text(part)
                pending_inputs.append(
                    {"role": "tool", "parts": [{"type": "tool_call_response", "id": call_id, "response": as_object_or_string(tool_result_text(part))}]}
                )
                tool = tool_calls.get(call_id, {"name": "", "input": None, "started_at": previous_ts})
                failed = bool(part.get("is_error"))
                spans.append(
                    {
                        "span_id": f"tool.{call_id}"[:64],
                        "parent_span_id": "turn",
                        "name": f"execute_tool {tool['name']}",
                        "kind": "tool",
                        "status": "error" if failed else "ok",
                        "tool_name": tool["name"],
                        "tool_call_id": call_id,
                        "started_at": tool["started_at"],
                        "ended_at": max(entry["timestamp"], tool["started_at"]),
                        "error_message": str(tool_result_text(part))[:2000] if failed else "",
                        "payload": {"arguments": as_object_or_string(tool["input"]), "result": as_object_or_string(result)},
                    }
                )
        previous_ts = entry["timestamp"]

    final_index = max((i for i, call in enumerate(calls) if any(p["type"] == "text" for p in call["parts"])), default=None)
    for index, call in enumerate(calls):
        usage = call["usage"]
        cache = usage.get("cache_creation") or {}
        spans.append(
            {
                "span_id": f"model.{index}",
                "parent_span_id": "turn",
                "name": f"chat {call['model']}",
                "kind": "model",
                "status": "ok",
                "started_at": call["started_at"],
                "ended_at": max(call["ended_at"], call["started_at"]),
                "model_name": call["model"],
                "provider": "anthropic",
                "response_id": call["id"],
                "finish_reason": call["stop_reason"][:32],
                "input_tokens": usage.get("input_tokens"),
                "output_tokens": usage.get("output_tokens"),
                "cache_read_tokens": usage.get("cache_read_input_tokens"),
                "cache_write_tokens": usage.get("cache_creation_input_tokens"),
                "cache_write_1h_tokens": cache.get("ephemeral_1h_input_tokens"),
                "reasoning_tokens": (usage.get("output_tokens_details") or {}).get("thinking_tokens"),
                "is_final_output": index == final_index,
                "payload": {
                    "input_messages": [user_message, *call["inputs"]],
                    "output_messages": [{"role": "assistant", "parts": call["parts"], "finish_reason": call["stop_reason"]}],
                },
            }
        )

    ended_at = max([prompt["timestamp"], *(entry["timestamp"] for entry in turn)])
    spans.insert(0, {"span_id": "turn", "name": "claude_code.turn", "kind": "turn", "started_at": prompt["timestamp"], "ended_at": ended_at})
    return spans


def build_envelope(session_id: str, turn: list[dict[str, Any]], cfg: dict[str, str], hook_input: dict[str, Any]) -> dict[str, Any]:
    prompt = turn[0]
    title = prompt_text(prompt).strip().splitlines()[0][:255] if prompt_text(prompt).strip() else ""
    return strip_nul(
        {
            "tenant_ref": cfg["tenant_ref"],
            "conversation_id": session_id,
            "conversation_title": title,
            "trace_id": prompt["uuid"],
            "session_ref": session_id,
            "ingestion_mode": "sdk",
            "semconv_version": SEMCONV_VERSION,
            "resource": {
                "service.name": "claude-code",
                "service.version": str(prompt.get("version", "")),
                "emet.plugin.version": PLUGIN_VERSION,
                "vcs.repository.name": Path(hook_input.get("cwd") or prompt.get("cwd") or "").name,
                "vcs.ref.head.name": str(prompt.get("gitBranch", "")),
            },
            "spans": build_spans(turn),
            "end": True,
        }
    )


# Sending


def post(envelope: dict[str, Any], cfg: dict[str, str]) -> tuple[bool, str]:
    """Returns (done, detail). A 4xx other than 429 is done: resending will not help."""
    request = urllib.request.Request(
        f"{cfg['endpoint']}/api/v1/traces/",
        data=json.dumps(envelope, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {cfg['api_key']}", "Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_S) as response:
            return True, f"{response.status} {response.read(2000).decode('utf-8', 'replace')}"
    except urllib.error.HTTPError as error:
        body = error.read(2000).decode("utf-8", "replace")
        return error.code < 500 and error.code != 429, f"{error.code} {body}"
    except (urllib.error.URLError, TimeoutError, OSError) as error:
        return False, f"network {error}"


def send_or_queue(envelope: dict[str, Any], cfg: dict[str, str], queue: Path) -> None:
    done, detail = post(envelope, cfg)
    log(f"trace {envelope['trace_id']} spans={len(envelope['spans'])} -> {detail[:500]}")
    if not done:
        name = hashlib.sha256(envelope["trace_id"].encode()).hexdigest()[:16]
        (queue / f"{name}.json").write_text(json.dumps(envelope, ensure_ascii=False))


def flush_queue(cfg: dict[str, str], queue: Path) -> None:
    for path in sorted(queue.glob("*.json")):
        try:
            envelope = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError):
            path.unlink(missing_ok=True)
            continue
        done, detail = post(envelope, cfg)
        log(f"retry {envelope.get('trace_id')} -> {detail[:300]}")
        if not done:
            return
        path.unlink(missing_ok=True)


# Entry points


def work(hook_input: dict[str, Any]) -> None:
    cfg = config()
    root = data_dir()
    session_id = hook_input.get("session_id", "")
    transcript = Path(hook_input.get("transcript_path") or "")
    if not session_id or not transcript.is_file():
        return
    time.sleep(FLUSH_WAIT_S)
    state_path = root / "state" / f"{session_id}.json"
    with open(root / "state" / f"{session_id}.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        flush_queue(cfg, root / "queue")
        state = json.loads(state_path.read_text()) if state_path.exists() else {"line": 0}
        entries, line_count = read_entries(transcript, state["line"])
        for turn in split_turns(entries):
            # Slash commands like /clear look like prompts but get no answer
            if not any(entry["type"] == "assistant" for entry in turn):
                continue
            send_or_queue(build_envelope(session_id, turn, cfg, hook_input), cfg, root / "queue")
        state_path.write_text(json.dumps({"line": line_count}))


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "stop"
    if mode == "worker":
        try:
            work(json.loads(sys.stdin.read() or "{}"))
        except Exception as error:  # Never surface an exporter failure to the session
            log(f"worker error {type(error).__name__}: {error}")
        return
    raw = sys.stdin.read()
    cfg = config()
    if not cfg["api_key"] or not cfg["tenant_ref"]:
        return
    if os.environ.get("EMET_SYNC") == "1":
        os.environ["EMET_SYNC"] = "0"
        work(json.loads(raw or "{}"))
        return
    worker = subprocess.Popen(
        [sys.executable, __file__, "worker"],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
    )
    assert worker.stdin is not None
    worker.stdin.write(raw.encode())
    worker.stdin.close()


if __name__ == "__main__":
    main()
