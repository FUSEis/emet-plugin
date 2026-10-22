"""Turns transcript entries into Emet spans: one model span per API call, one tool span per tool call, one agent span per subagent."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import content, transcript
from .transcript import Entry

SPAN_ID_MAX = 64
# A message as sent, whether content was left out of it, and how many secrets were redacted
Opening = Tuple[Dict[str, Any], bool, int]
ERROR_MESSAGE_MAX = 2000


@dataclass
class Capture:
    """Applies the capture mode to everything that leaves the machine."""

    mode: str
    secrets: Tuple[str, ...] = ()

    @property
    def keeps_content(self) -> bool:
        return self.mode != "metadata"

    def value(self, value: Any) -> Tuple[Any, bool, int]:
        """Returns the value as sent, whether binary content was removed, and how many secrets were redacted."""
        value, removed = content.strip_binary(value)
        redactions = 0
        if self.mode == "sanitized":

            def scrub(text: str) -> str:
                nonlocal redactions
                text, count = content.redact(text, self.secrets)
                redactions += count
                return text

            value = content.walk_strings(value, scrub)
        return value, removed, redactions

    def text(self, text: str) -> Tuple[str, bool, int]:
        """Returns the text as sent, whether binary content was removed from it, and how many secrets were redacted."""
        if not self.keeps_content:
            return "", False, 0
        value, removed, redactions = self.value(text)
        return str(value), removed, redactions


@dataclass
class AgentThread:
    """A subagent transcript the turn launched, read up to offset."""

    agent_id: str
    path: str
    parent_span_id: str
    offset: int


@dataclass
class Built:
    spans: List[Dict[str, Any]] = field(default_factory=list)
    agents: List[AgentThread] = field(default_factory=list)
    final_span_id: Optional[str] = None


def span_id(*parts: str) -> str:
    return ".".join(parts)[:SPAN_ID_MAX]


def object_or_string(value: Any) -> Any:
    if value is None or isinstance(value, (dict, str)):
        return value
    return json.dumps(value, ensure_ascii=False)


def tool_result_text(part: Dict[str, Any]) -> Any:
    value = part.get("content")
    if isinstance(value, list):
        texts = [item.get("text") or "" for item in value if isinstance(item, dict) and item.get("type") == "text"]
        if len(texts) == len(value):
            return "\n".join(texts)
    return value


class Builder:
    def __init__(self, transcript_path: Path, capture: Capture) -> None:
        self.transcript_path = transcript_path
        self.capture = capture
        self._subagents: Optional[Dict[str, Tuple[Path, Dict[str, Any]]]] = None

    @property
    def subagents(self) -> Dict[str, Tuple[Path, Dict[str, Any]]]:
        if self._subagents is None:
            self._subagents = transcript.subagents(self.transcript_path)
        return self._subagents

    def message(self, role: str, text: str, binary: bool = False) -> Opening:
        """Returns a message, whether content was left out of it (such as an image the user attached) and its redaction count."""
        sent, removed, redactions = self.capture.text(text)
        return {"role": role, "parts": [{"type": "text", "content": sent}]}, (removed or binary) and self.capture.keeps_content, redactions

    def thread(
        self,
        items: List[Tuple[int, Entry]],
        parent_span_id: str,
        opening: Optional[Opening],
        built: Built,
        main: bool,
        since: str = "",
    ) -> None:
        """Adds the model and tool spans of one thread of entries under parent_span_id. since is when the
        thread's opening message was sent, which is when its first model call started."""
        calls: List[Dict[str, Any]] = []
        by_id: Dict[str, Dict[str, Any]] = {}
        tool_calls: Dict[str, Dict[str, Any]] = {}
        pending: List[Dict[str, Any]] = []
        previous_ts = since or (str(items[0][1].get("timestamp") or "") if items else "")

        for _, entry in items:
            timestamp = str(entry.get("timestamp") or previous_ts)
            if entry.get("type") == "assistant":
                message = entry.get("message") or {}
                # One API response is split across entries that share message.id, each repeating the usage
                message_id = str(message.get("id") or entry.get("uuid") or len(calls))
                call = by_id.get(message_id)
                if call is None:
                    call = {
                        "id": message_id,
                        "started_at": previous_ts or timestamp,
                        "ended_at": timestamp,
                        "model": message.get("model") or "",
                        "usage": message.get("usage") or {},
                        "stop_reason": message.get("stop_reason") or "",
                        "inputs": pending,
                        "parts": [],
                        "truncated": False,
                        "redactions": 0,
                    }
                    pending = []
                    by_id[message_id] = call
                    calls.append(call)
                call["ended_at"] = timestamp
                call["usage"] = message.get("usage") or call["usage"]
                call["stop_reason"] = message.get("stop_reason") or call["stop_reason"]
                for part in transcript.content_parts(entry):
                    if part.get("type") == "text" and part.get("text"):
                        sent, removed, redactions = self.capture.text(part["text"])
                        call["parts"].append({"type": "text", "content": sent})
                        call["truncated"] = call["truncated"] or removed
                        call["redactions"] += redactions
                    elif part.get("type") == "tool_use" and part.get("id"):
                        arguments = None
                        if self.capture.keeps_content:
                            # Counted on the tool span, which carries these arguments too
                            arguments, removed, _ = self.capture.value(part.get("input"))
                            call["truncated"] = call["truncated"] or removed
                        call["parts"].append(
                            {"type": "tool_call", "id": part["id"], "name": part.get("name"), "arguments": object_or_string(arguments)}
                        )
                        tool_calls[part["id"]] = {"name": part.get("name") or "", "input": part.get("input"), "started_at": timestamp}
            else:
                for part in transcript.content_parts(entry):
                    if part.get("type") != "tool_result":
                        continue
                    call_id = str(part.get("tool_use_id") or "")
                    # The model span names the result; its content lives once, in the tool span
                    pending.append({"role": "tool", "parts": [{"type": "tool_call_response", "id": call_id}]})
                    tool = tool_calls.get(call_id, {"name": "", "input": None, "started_at": previous_ts or timestamp})
                    built.spans.append(self.tool_span(entry, part, call_id, tool, timestamp, parent_span_id))
                    self.agent(call_id, tool, built)
            previous_ts = timestamp

        final_index = None
        if main:
            texts = [index for index, call in enumerate(calls) if any(p["type"] == "text" for p in call["parts"])]
            final_index = texts[-1] if texts else None
        for index, call in enumerate(calls):
            span = self.model_span(call, parent_span_id, opening, index == final_index)
            if index == final_index:
                built.final_span_id = span["span_id"]
            built.spans.append(span)

    def tool_span(
        self,
        entry: Entry,
        part: Dict[str, Any],
        call_id: str,
        tool: Dict[str, Any],
        timestamp: str,
        parent_span_id: str,
    ) -> Dict[str, Any]:
        failed = bool(part.get("is_error"))
        # The evidence is what the model received. Claude Code's own toolUseResult can hold more,
        # such as the whole original file of an edit, which the model never saw
        saved = transcript.persisted_output(self.transcript_path, part, entry.get("toolUseResult"))
        result: Any = saved[0] if saved is not None else tool_result_text(part)
        span: Dict[str, Any] = {
            "span_id": span_id("tool", call_id),
            "parent_span_id": parent_span_id,
            "name": f"execute_tool {tool['name']}".strip(),
            "kind": "tool",
            "status": "error" if failed else "ok",
            "tool_name": tool["name"][:255],
            "tool_call_id": call_id[:255],
            "started_at": tool["started_at"],
            "ended_at": max(timestamp, tool["started_at"]),
            "attributes": {},
            "payload": {},
        }
        if saved is not None:
            span["attributes"]["claude_code.saved_output"] = True
        if not self.capture.keeps_content:
            return span
        arguments, removed_in, count_in = self.capture.value(tool["input"])
        result, removed_out, count_out = self.capture.value(result)
        span["payload"] = {"arguments": object_or_string(arguments), "result": object_or_string(result)}
        span["is_truncated"] = removed_in or removed_out or (saved is not None and saved[1])
        if count_in + count_out:
            span["attributes"]["emet.redactions"] = count_in + count_out
        if failed:
            span["error_message"] = str(self.capture.value(tool_result_text(part))[0])[:ERROR_MESSAGE_MAX]
        return span

    def model_span(self, call: Dict[str, Any], parent_span_id: str, opening: Optional[Opening], final: bool) -> Dict[str, Any]:
        usage = call["usage"] if isinstance(call["usage"], dict) else {}
        cache = usage.get("cache_creation") or {}
        details = usage.get("output_tokens_details") or {}
        span: Dict[str, Any] = {
            "span_id": span_id("model", call["id"]),
            "parent_span_id": parent_span_id,
            "name": f"chat {call['model']}".strip(),
            "kind": "model",
            "status": "ok",
            "started_at": call["started_at"],
            "ended_at": max(call["ended_at"], call["started_at"]),
            "model_name": call["model"][:255],
            "provider": "anthropic",
            "response_id": call["id"][:255],
            "finish_reason": call["stop_reason"][:32],
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "cache_read_tokens": usage.get("cache_read_input_tokens"),
            "cache_write_tokens": usage.get("cache_creation_input_tokens"),
            "cache_write_1h_tokens": cache.get("ephemeral_1h_input_tokens") if isinstance(cache, dict) else None,
            "reasoning_tokens": details.get("thinking_tokens") if isinstance(details, dict) else None,
            "is_final_output": final,
            "payload": {},
        }
        if self.capture.keeps_content:
            inputs = ([opening[0]] if opening else []) + call["inputs"]
            span["is_truncated"] = call["truncated"] or bool(opening and opening[1])
            span["payload"] = {
                "input_messages": inputs,
                "output_messages": [{"role": "assistant", "parts": call["parts"], "finish_reason": call["stop_reason"]}],
            }
            # Counts the text written in this call; the prompt is counted on the turn, tool arguments on the tool span
            if call["redactions"]:
                span["attributes"] = {"emet.redactions": call["redactions"]}
        return span

    def agent(self, call_id: str, tool: Dict[str, Any], built: Built) -> None:
        """Adds the subagent a tool call launched, with its own model and tool spans."""
        found = self.subagents.get(call_id)
        if found is None:
            return
        path, meta = found
        items, offset = transcript.read_entries(path, 0, sidechain=True)
        agent_id = str(meta.get("agentId") or path.stem.replace("agent-", ""))
        agent_span_id = span_id("agent", agent_id)
        timestamps = [str(entry["timestamp"]) for _, entry in items if entry.get("timestamp")]
        started = timestamps[0] if timestamps else tool["started_at"]
        span: Dict[str, Any] = {
            "span_id": agent_span_id,
            "parent_span_id": span_id("tool", call_id),
            "name": f"invoke_agent {meta.get('agentType') or 'subagent'}".strip(),
            "kind": "agent",
            "started_at": started,
            "ended_at": max(timestamps[-1], started) if timestamps else started,
            "attributes": {"claude_code.agent_type": str(meta.get("agentType") or "")},
        }
        built.spans.append(span)
        opening = None
        if items and items[0][1].get("type") == "user":
            # Its redactions are counted on the Agent tool span, whose arguments hold the same prompt
            opening = self.message("user", transcript.text_of(items[0][1]), transcript.has_binary(items[0][1]))
            items = items[1:]
        self.thread(items, agent_span_id, opening, built, main=False, since=started)
        built.agents.append(AgentThread(agent_id=agent_id, path=str(path), parent_span_id=agent_span_id, offset=offset))
