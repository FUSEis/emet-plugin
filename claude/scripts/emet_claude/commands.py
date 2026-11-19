"""Hook entry points. A hook returns at once and hands the work to a detached worker, so a session never waits on Emet."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import time
from dataclasses import replace
from functools import partial
from pathlib import Path
from typing import Any, Callable, Collection, Dict, List, Mapping, Optional, Sequence, Tuple, TypeVar
from urllib.parse import urlencode

from . import VERSION, envelope, pause, pending, storage, transcript, transport, watch
from .config import FROM_CLAUDE_ACCOUNT, Config, load
from .spans import Builder, Capture

# Claude Code writes the transcript asynchronously; give it a moment to flush the turn that just ended
FLUSH_WAIT_S = 1.0
# Subagent transcripts followed per session, newest kept
MAX_TRACKED_AGENTS = 200
HOOK_MODES = ("session-start", "stop", "subagent-stop", "session-end")
# Hooks that fire only once the current turn is over
TURN_END_MODES = ("stop", "session-end")
# Deliveries listed by /emet:status
RECENT_DELIVERIES = 5
# SessionStart sources where the key cannot have changed since the last check
SAME_KEY_SOURCES = ("clear", "compact")
Made = TypeVar("Made")


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


def signed_in_email(environ: Mapping[str, str], root: Optional[Path] = None) -> Tuple[str, str]:
    """Returns (email, "") for the signed-in Claude account, or ("", why there is none).

    Reads Claude Code's account file instead of running claude, since settings env can override CLAUDE_CODE_EXECPATH.
    """
    path = Path(environ.get("CLAUDE_CONFIG_DIR") or Path.home()) / ".claude.json"
    try:
        account = json.loads(path.read_text(encoding="utf-8")).get("oauthAccount")
    except FileNotFoundError:
        return "", "no Claude account file"
    # AttributeError: the file holds a JSON list or string, which has no .get
    except (OSError, ValueError, AttributeError) as error:
        if root is not None:
            storage.log(root, f"sign-in email unreadable {type(error).__name__}")
        return "", "Claude account file unreadable"
    email = account.get("emailAddress") if isinstance(account, dict) else None
    if isinstance(email, str) and "@" in email:
        return email.strip(), ""
    return "", "no signed-in Claude account"


def load_with_sign_in(environ: Mapping[str, str]) -> Config:
    """Returns the options, with the signed-in email as the person when none is set."""
    config = load(environ)
    if config.problem or config.enduser_ref:
        return config
    email, note = signed_in_email(environ, storage.data_dir(environ))
    if not email:
        return replace(config, enduser_note=note)
    return replace(config, enduser_ref=email, enduser_source=FROM_CLAUDE_ACCOUNT)


def report_url(config: Config, session_id: str, conversation_uuid: str) -> str:
    """Links the conversation in Emet. The search narrows the list to it, so Emet never selects another one instead."""
    if not conversation_uuid:
        return ""
    return f"{config.endpoint}/conversations?{urlencode({'q': session_id, 'selected': conversation_uuid})}"


def remember(recent: List[Dict[str, Any]], delivery: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Puts a session's newest delivery first, keeping its earlier link when Emet's reply had none."""
    session_id = delivery["session_id"]
    earlier = next((item for item in recent if item.get("session_id") == session_id), None)
    if earlier and not delivery["report_url"]:
        delivery = dict(delivery, report_url=earlier.get("report_url") or "")
    others = [item for item in recent if item.get("session_id") != session_id]
    return [delivery, *others][:RECENT_DELIVERIES]


def last_error(sender: transport.Sender) -> Dict[str, Any]:
    """Returns the status fields for this run's latest failure, or none when nothing failed."""
    if not sender.failures:
        return {}
    return {"last_error": {"at": storage.now(), "detail": transport.describe(sender.failures[-1])}}


def status_path(root: Path) -> Path:
    return root / "status.json"


def update_status(
    root: Path, config: Config, accepted: Sequence[transport.Accepted] = (), skipped: Sequence[str] = (), **fields: Any
) -> None:
    """Records what the setup and status skills report. Never holds the key or any session content.

    Only this run's deliveries change the links, so a run that sent nothing never hides a newer one.
    """
    # Workers for different sessions share this file
    with storage.locked(root / "status.lock"):
        status = storage.read_json(status_path(root), {})
        if not isinstance(status, dict):
            status = {}
        status.update(
            plugin_version=VERSION,
            endpoint=config.endpoint,
            tenant_ref=config.tenant_ref,
            capture_mode=config.capture_mode,
            enduser_ref=config.enduser_ref,
            enduser_source=config.enduser_source,
            enduser_note=config.enduser_note,
            key_hint=config.key_hint,
            problem=config.problem,
            queued=len(transport.queued(root)),
            paused_until=pause.until(root),
            reads_paused_until=pause.until(root, pause.READS),
            updated_at=storage.now(),
        )
        status.update(fields)
        saved = status.get("recent")
        recent = [item for item in saved if isinstance(item, dict)] if isinstance(saved, list) else []
        for item in accepted:
            delivery = {
                "session_id": item.session_id,
                "repository": item.repository,
                "at": storage.now(),
                "status": item.outcome.status,
                "report_url": report_url(config, item.session_id, item.outcome.conversation_uuid),
            }
            recent = remember(recent, delivery)
            status["last_delivery"] = recent[0]
        status["recent"] = recent
        if skipped:
            earlier = status.get("skipped_turns")
            status["skipped_turns"] = (earlier if isinstance(earlier, int) else 0) + len(skipped)
            status["last_skipped"] = {"at": storage.now(), "reason": skipped[-1]}
        storage.write_json(status_path(root), status)


def check_key(root: Path, config: Config) -> None:
    """Asks Emet whether the key is accepted, with one read that sends no trace. Not asked during a pause."""
    if config.problem:
        return
    if pause.until(root):
        storage.log(root, "key check skipped, paused")
        return
    result = transport.check_key(config)
    storage.log(root, f"key check -> {result}")
    update_status(root, config, key_check=result, key_checked_at=storage.now())


def forget(state: Dict[str, Any], refused: Collection[str]) -> None:
    """Stops following the traces Emet refused, so nothing is added later to a trace that does not exist."""
    if str(state.get("last_trace") or "") in refused:
        state["last_trace"] = ""
    agents = state.get("agents")
    if isinstance(agents, dict):
        for agent_id in [key for key, agent in agents.items() if isinstance(agent, dict) and str(agent.get("trace_id")) in refused]:
            del agents[agent_id]


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


def process(
    hook_input: Mapping[str, Any],
    environ: Mapping[str, str] = os.environ,
    wait: float = FLUSH_WAIT_S,
    turn_ended: bool = True,
) -> None:
    """Sends every finished turn and subagent update since the last run of this session.

    turn_ended says the hook fired because the current turn ended (Stop, SessionEnd). Otherwise
    (SubagentStop, SessionStart) the last turn may still be running and is left for a later run.
    """
    config = load_with_sign_in(environ)
    root = storage.data_dir(environ)
    session_id = str(hook_input.get("session_id") or "")
    path = Path(str(hook_input.get("transcript_path") or ""))
    cwd = str(hook_input.get("cwd") or "")
    if config.problem or not session_id or not path.is_file():
        return
    if wait:
        time.sleep(wait)
    with storage.locked(root / "state" / f"{session_id}.lock"), pending.finishing(root, session_id, turn_ended):
        state_file = root / "state" / f"{session_id}.json"
        state = storage.read_json(state_file, {})
        if not isinstance(state, dict):
            state = {}
        sender = transport.Sender(root, config)
        sender.flush()
        builder = Builder(path, Capture(config.capture_mode, secrets=(config.emet_key,)))
        skipped: List[str] = []

        def attempt(build: Callable[[], Made]) -> Optional[Made]:
            """Builds one turn or addition. One that cannot be built is recorded and left out, so the rest is still sent."""
            try:
                return build()
            except Exception as error:  # noqa: BLE001 - the transcript format is not a contract
                skipped.append(type(error).__name__)
                storage.log(root, f"turn skipped {type(error).__name__}")
                # A reader of the session's own file must not take a turn that was never sent for a clean one
                pending.append(root, session_id, {"skipped": storage.now(), "reason": type(error).__name__})
                return None

        entries, end = transcript.read_entries(path, int(state.get("offset") or 0))
        head, turns = transcript.split_turns(entries)
        offset = end
        # The last turn may still be running, or its prompt still waiting for an answer; read it again next time
        if turns and (not turn_ended or not transcript.answered(turns[-1])):
            offset = turns[-1][0][0]
            turns = turns[:-1]

        known = set((state.get("agents") or {}).keys())
        if head and state.get("last_trace"):
            tail = attempt(partial(envelope.for_tail, config, builder, session_id, state["last_trace"], head, "turn", cwd))
            if tail is not None:
                sender.send(tail[0])
                track(state, state["last_trace"], tail[1])
        for turn in turns:
            # Slash commands and local shell escapes look like prompts but no model call answers them
            if not transcript.answered(turn):
                continue
            made = attempt(partial(envelope.for_turn, config, builder, session_id, turn, cwd))
            if made is None:
                # Whatever is added to this turn later must not join the turn before it
                state["last_trace"] = ""
                continue
            trace_id, envelopes, built = made
            sender.send(envelopes)
            state["last_trace"] = trace_id
            track(state, trace_id, built)

        for agent_id in known:
            agent = state["agents"].get(agent_id)
            agent_path = Path(agent["path"]) if agent else None
            if agent_path is None or not agent_path.is_file():
                continue
            items, agent_end = transcript.read_entries(agent_path, int(agent.get("offset") or 0), sidechain=True)
            if items:
                late = attempt(
                    partial(envelope.for_tail, config, builder, session_id, agent["trace_id"], items, agent["parent_span_id"], cwd)
                )
                if late is not None:
                    sender.send(late[0])
                    track(state, agent["trace_id"], late[1])
            agent["offset"] = agent_end

        state["offset"] = offset
        forget(state, sender.refused(session_id))
        storage.write_json(state_file, state)
        update_status(root, config, sender.accepted, skipped, **last_error(sender))


def run_worker(mode: str, raw: bytes, environ: Mapping[str, str] = os.environ) -> None:
    root = storage.data_dir(environ)
    try:
        hook_input = json.loads(raw or b"{}")
        if not isinstance(hook_input, dict):
            hook_input = {}
        if mode == "session-start" and hook_input.get("source") not in SAME_KEY_SOURCES:
            check_key(root, load_with_sign_in(environ))
        process(hook_input, environ, turn_ended=mode in TURN_END_MODES)
    except Exception as error:  # noqa: BLE001 - an exporter failure must never reach the session
        storage.log(root, f"worker error {type(error).__name__}")


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    mode = argv[0] if argv else "stop"
    if mode == "worker":
        run_worker(argv[1] if len(argv) > 1 else "stop", sys.stdin.buffer.read())
        return 0
    if mode == "watch":
        # The one mode that can exit with another code than 0, to wake Claude for a flagged report
        return watch.run(sys.stdin.buffer.read())
    if mode not in HOOK_MODES:
        return 0
    raw = sys.stdin.buffer.read()
    # Only checks that the plugin can send; the worker reads the sign-in
    config = load()
    if config.problem:
        with contextlib.suppress(OSError):
            update_status(storage.data_dir(), config)
        return 0
    try:
        spawn_worker(mode, raw)
    except Exception as error:  # noqa: BLE001 - a worker that cannot start must never break the hook
        with contextlib.suppress(Exception):
            storage.log(storage.data_dir(), f"worker start failed {type(error).__name__}")
    return 0
