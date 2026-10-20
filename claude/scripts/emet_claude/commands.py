"""Hook entry points. A hook returns at once and hands the work to a detached worker, so a session never waits on Emet."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from . import VERSION, envelope, storage, transcript, transport
from .config import Config, load
from .spans import Builder, Capture

# Claude Code writes the transcript asynchronously; give it a moment to flush the turn that just ended
FLUSH_WAIT_S = 1.0
# Subagent transcripts followed per session, newest kept
MAX_TRACKED_AGENTS = 200
HOOK_MODES = ("session-start", "stop", "subagent-stop", "session-end")


def entry_script() -> str:
    return str(Path(__file__).resolve().parent.parent / "emet_hook.py")


def spawn_worker(mode: str, raw: bytes) -> None:
    options: Dict[str, Any] = {}
    if os.name == "nt":
        # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
        options["creationflags"] = 0x00000008 | 0x00000200
    else:
        options["start_new_session"] = True
    worker = subprocess.Popen(
        [sys.executable, entry_script(), "worker", mode],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        close_fds=True,
        **options,
    )
    if worker.stdin is not None:
        worker.stdin.write(raw)
        worker.stdin.close()


def status_path(root: Path) -> Path:
    return root / "status.json"


def update_status(root: Path, config: Config, **fields: Any) -> None:
    """Records what the setup and status skills report. Never holds the key or any session content."""
    status = storage.read_json(status_path(root), {})
    if not isinstance(status, dict):
        status = {}
    status.update(
        plugin_version=VERSION,
        endpoint=config.endpoint,
        tenant_ref=config.tenant_ref,
        capture_mode=config.capture_mode,
        key_hint=config.key_hint,
        problem=config.problem,
        queued=len(list((root / "queue").glob("*.json"))),
        updated_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    )
    status.update(fields)
    storage.write_json(status_path(root), status)


def check_key(root: Path, config: Config) -> None:
    """Asks Emet whether the key is accepted, without sending a trace.

    Emet checks the key before the body, so an empty envelope gets 400 with a good key and 401 or 403 with a bad one.
    """
    outcome = transport.post(config, {})
    if outcome.status in (401, 403):
        result = "rejected"
    elif outcome.status == 400:
        result = "accepted"
    elif outcome.status == 0:
        result = "unreachable"
    else:
        result = f"unexpected {outcome.status}"
    storage.log(root, f"key check -> {result}")
    update_status(root, config, key_check=result, key_checked_at=time.strftime("%Y-%m-%dT%H:%M:%S%z"))


def deliver(sender: transport.Sender, envelopes: List[Dict[str, Any]], state: Dict[str, Any]) -> None:
    outcomes = sender.send(envelopes)
    for outcome in outcomes:
        if outcome.delivered:
            state["last_delivery"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "status": outcome.status}
        else:
            state["last_error"] = {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "detail": transport.describe(outcome)}


def track(state: Dict[str, Any], trace_id: str, built: Any) -> None:
    agents = state.setdefault("agents", {})
    for thread in built.agents:
        agents[thread.agent_id] = {
            "path": thread.path,
            "trace_id": trace_id,
            "parent_span_id": thread.parent_span_id,
            "offset": thread.offset,
        }
    while len(agents) > MAX_TRACKED_AGENTS:
        agents.pop(next(iter(agents)))


def process(hook_input: Mapping[str, Any], environ: Mapping[str, str] = os.environ, wait: float = FLUSH_WAIT_S) -> None:
    """Sends every finished turn and subagent update since the last run of this session."""
    config = load(environ)
    root = storage.data_dir(environ)
    session_id = str(hook_input.get("session_id") or "")
    path = Path(str(hook_input.get("transcript_path") or ""))
    cwd = str(hook_input.get("cwd") or "")
    if config.problem or not session_id or not path.is_file():
        return
    if wait:
        time.sleep(wait)
    with storage.locked(root / "state" / f"{session_id}.lock"):
        state_file = root / "state" / f"{session_id}.json"
        state = storage.read_json(state_file, {})
        if not isinstance(state, dict):
            state = {}
        sender = transport.Sender(root, config)
        sender.flush()
        builder = Builder(path, Capture(config.capture_mode, secrets=(config.api_key,)))

        entries, end = transcript.read_entries(path, int(state.get("offset") or 0))
        head, turns = transcript.split_turns(entries)
        offset = end
        # The last prompt may still be waiting for its answer; read it again next time
        if turns and not transcript.answered(turns[-1]):
            offset = turns[-1][0][0]
            turns = turns[:-1]

        known = set((state.get("agents") or {}).keys())
        if head and state.get("last_trace"):
            envelopes, built = envelope.for_tail(config, builder, session_id, state["last_trace"], head, "turn", cwd)
            deliver(sender, envelopes, state)
            track(state, state["last_trace"], built)
        for turn in turns:
            # Slash commands and local shell escapes look like prompts but no model call answers them
            if not transcript.answered(turn):
                continue
            trace_id, envelopes, built = envelope.for_turn(config, builder, session_id, turn, cwd)
            deliver(sender, envelopes, state)
            state["last_trace"] = trace_id
            track(state, trace_id, built)

        for agent_id in known:
            agent = state["agents"].get(agent_id)
            agent_path = Path(agent["path"]) if agent else None
            if agent_path is None or not agent_path.is_file():
                continue
            items, agent_end = transcript.read_entries(agent_path, int(agent.get("offset") or 0), sidechain=True)
            if items:
                envelopes, built = envelope.for_tail(config, builder, session_id, agent["trace_id"], items, agent["parent_span_id"], cwd)
                deliver(sender, envelopes, state)
                track(state, agent["trace_id"], built)
            agent["offset"] = agent_end

        state["offset"] = offset
        storage.write_json(state_file, state)
        update_status(root, config, last_delivery=state.get("last_delivery"), last_error=state.get("last_error"))


def run_worker(mode: str, raw: bytes, environ: Mapping[str, str] = os.environ) -> None:
    root = storage.data_dir(environ)
    try:
        hook_input = json.loads(raw or b"{}")
        if mode == "session-start":
            check_key(root, load(environ))
        process(hook_input if isinstance(hook_input, dict) else {}, environ)
    except Exception as error:  # noqa: BLE001 - an exporter failure must never reach the session
        storage.log(root, f"worker error {type(error).__name__}")


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "stop"
    if mode == "worker":
        run_worker(argv[1] if len(argv) > 1 else "stop", sys.stdin.buffer.read())
        return 0
    if mode not in HOOK_MODES:
        return 0
    raw = sys.stdin.buffer.read()
    config = load()
    if config.problem:
        with contextlib.suppress(OSError):
            update_status(storage.data_dir(), config)
        return 0
    spawn_worker(mode, raw)
    return 0
