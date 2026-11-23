"""Builds synthetic Claude Code transcripts and runs a stand-in Emet endpoint for the tests."""

from __future__ import annotations

import json
import os
import re
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

import pytest

# The body Emet answers a report read with when no trace has the id
# Stands in for the words Claude Code puts before a Stop hook's stderr when it wakes Claude
WAKE_WRAPPER = 'Stop hook blocking error from command: "python3 emet_hook.py watch": '
NOT_FOUND = json.dumps({"error": "No trace was found with this id.", "code": "not_found", "status_code": 404}).encode()
# For a test that makes a symbolic link, and for one that makes a named pipe
links = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
pipes = pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="the platform has no named pipes")


class Transcript:
    """Writes transcript entries in the shape Claude Code uses, one JSON object per line."""

    def __init__(self, folder: Path, session_id: str = "session-1") -> None:
        self.session_id = session_id
        self.path = folder / f"{session_id}.jsonl"
        self.path.write_text("")
        self.clock = 0
        self.last_uuid: Optional[str] = None

    def tick(self) -> str:
        self.clock += 1
        minutes, seconds = divmod(self.clock, 60)
        return f"2026-10-20T10:{minutes:02d}:{seconds:02d}.000Z"

    def write(self, entry: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
        entry.setdefault("uuid", str(uuid.uuid4()))
        entry.setdefault("parentUuid", self.last_uuid)
        entry.setdefault("sessionId", self.session_id)
        entry.setdefault("timestamp", self.tick())
        entry.setdefault("cwd", "/work/acme-app")
        entry.setdefault("gitBranch", "main")
        entry.setdefault("version", "2.1.294")
        self.last_uuid = entry["uuid"]
        with open(path or self.path, "a", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(entry) + "\n")
        return entry

    def prompt(self, text: str, source: Optional[str] = "typed", **extra: Any) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"type": "user", "message": {"role": "user", "content": text}, **extra}
        if source:
            entry["promptSource"] = source
        return self.write(entry)

    def notice(self, text: str, **extra: Any) -> Dict[str, Any]:
        """Starts a turn no person started (a wake, a finished background command), with the fields Claude Code 2.1.305 writes."""
        fields = {"turnOrigin": "task_notification", "origin": {"kind": "task-notification"}, "promptId": str(uuid.uuid4())}
        return self.prompt(text, source=None, **{"promptSource": "system", **fields, **extra})

    def command(self, name: str, args: Optional[str] = None, **extra: Any) -> Dict[str, Any]:
        """Starts a turn a skill's slash command started, with the two entries Claude Code 2.1.305 writes. Returns the first."""
        text = f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>"
        if args is not None:
            text += f"\n<command-args>{args}</command-args>"
        fields = {"turnOrigin": "human", "origin": {"kind": "human"}, "promptId": str(uuid.uuid4())}
        entry = self.prompt(text, source=None, **{**fields, **extra})
        skill = f"Base directory for this skill: /plugin/skills/{name.rpartition(':')[2]}\n\n# The skill's text"
        self.write(
            {
                "type": "user",
                "isMeta": True,
                "message": {"role": "user", "content": [{"type": "text", "text": skill}]},
                "promptId": entry["promptId"],
            }
        )
        return entry

    def assistant(
        self,
        message_id: str,
        parts: List[Dict[str, Any]],
        usage: Optional[Dict[str, Any]] = None,
        stop_reason: Optional[str] = None,
        path: Optional[Path] = None,
        **extra: Any,
    ) -> Dict[str, Any]:
        return self.write(
            {
                "type": "assistant",
                "message": {
                    "id": message_id,
                    "role": "assistant",
                    "model": "claude-opus-5-5",
                    "content": parts,
                    "stop_reason": stop_reason,
                    "usage": usage
                    or {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 100, "cache_creation_input_tokens": 20},
                },
                **extra,
            },
            path,
        )

    def text(self, message_id: str, text: str, path: Optional[Path] = None, **extra: Any) -> Dict[str, Any]:
        return self.assistant(message_id, [{"type": "text", "text": text}], stop_reason="end_turn", path=path, **extra)

    def tool_use(
        self, message_id: str, tool_id: str, name: str, arguments: Dict[str, Any], path: Optional[Path] = None, **extra: Any
    ) -> Dict[str, Any]:
        return self.assistant(
            message_id, [{"type": "tool_use", "id": tool_id, "name": name, "input": arguments}], stop_reason="tool_use", path=path, **extra
        )

    def tool_result(
        self, tool_id: str, content: Any, is_error: bool = False, tool_use_result: Any = None, path: Optional[Path] = None, **extra: Any
    ) -> Dict[str, Any]:
        part: Dict[str, Any] = {"type": "tool_result", "tool_use_id": tool_id, "content": content}
        if is_error:
            part["is_error"] = True
        entry: Dict[str, Any] = {"type": "user", "message": {"role": "user", "content": [part]}, **extra}
        if tool_use_result is not None:
            entry["toolUseResult"] = tool_use_result
        return self.write(entry, path)

    @property
    def folder(self) -> Path:
        folder = self.path.with_suffix("")
        folder.mkdir(exist_ok=True)
        return folder

    def subagent(self, agent_id: str, tool_use_id: str, agent_type: str = "Explore") -> Path:
        folder = self.folder / "subagents"
        folder.mkdir(exist_ok=True)
        (folder / f"agent-{agent_id}.meta.json").write_text(
            json.dumps({"agentType": agent_type, "description": "look around", "toolUseId": tool_use_id})
        )
        path = folder / f"agent-{agent_id}.jsonl"
        path.write_text("")
        return path

    def saved_output(self, name: str, text: str) -> Path:
        folder = self.folder / "tool-results"
        folder.mkdir(exist_ok=True)
        path = folder / name
        path.write_bytes(text.encode("utf-8"))
        return path

    def hook_input(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "transcript_path": str(self.path), "cwd": "/work/acme-app", "hook_event_name": "Stop"}


# The one form the plugin writes a time in: UTC, milliseconds, Z
UTC_FORM = re.compile(r"[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}\.[0-9]{3}Z")


def old_form(at: float) -> str:
    """A time as 0.1.4 wrote it in its files, which stay on disk: local time, whole seconds, a numeric offset.

    Always five hours behind UTC, so the offset is read on a machine in UTC too.
    """
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(int(at) - 5 * 3600)) + "-0500"


class Clock:
    """Stands in for the time and the randomness of the pause, so no test waits or depends on chance."""

    def __init__(self) -> None:
        self.now = 1_800_000_000.0
        self.spread = 1.0

    def advance(self, seconds: float) -> None:
        self.now += seconds


class FakeEmet:
    """Records every request; answers a send with the next status in `statuses`, then 202."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.statuses: List[int] = []
        # The conversation uuid each accepted request was answered with; a fresh one per request unless reply is set
        self.conversations: List[str] = []
        self.reply: Optional[str] = None
        # Emet's own uuid for each trace id it accepted, the same on every batch of that trace
        self.trace_uuids: Dict[str, str] = {}
        # False answers as an Emet that names no trace in its 202
        self.names_trace = True
        # Span ids listed as missing content in each 202, as Emet does for a metadata-mode batch
        self.gaps: List[str] = []
        # Sent as Retry-After with every send that is not accepted
        self.retry_after: Optional[str] = None
        # The body of every send that is not accepted
        self.error_body = b"{}"
        # Where a send answered with a redirect status points
        self.location = "/elsewhere/"
        # Report reads, kept apart from the sends; each is answered with the next (status, content type, body), then Emet's not found
        self.reads: List[Dict[str, Any]] = []
        self.read_replies: List[Tuple[int, str, bytes]] = []
        # Answers to the report reads of one trace uuid, used before read_replies. The last one repeats
        self.reports: Dict[str, List[Tuple[int, bytes, Optional[str]]]] = {}
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status = owner.statuses.pop(0) if owner.statuses else 202
                reply = owner.error_body
                if status == 202:
                    owner.conversations.append(owner.reply or str(uuid.uuid4()))
                    gaps = {"missing_tool_arguments": owner.gaps, "missing_tool_results": owner.gaps}
                    accepted = {"conversation": owner.conversations[-1], "gaps": gaps}
                    if owner.names_trace:
                        accepted["trace"] = owner.trace_uuids.setdefault(str(body.get("trace_id")), str(uuid.uuid4()))
                    reply = json.dumps(accepted).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(reply)))
                if status != 202 and owner.retry_after is not None:
                    self.send_header("Retry-After", owner.retry_after)
                if 300 <= status < 400:
                    self.send_header("Location", owner.location)
                self.end_headers()
                self.wfile.write(reply)

            def do_GET(self) -> None:
                owner.reads.append({"path": self.path, "headers": dict(self.headers)})
                parts = self.path.strip("/").split("/")
                planned = owner.reports.get(parts[-2]) if len(parts) > 1 else None
                retry_after = None
                if planned:
                    status, reply, retry_after = planned.pop(0) if len(planned) > 1 else planned[0]
                    content_type = "application/json"
                elif owner.read_replies:
                    status, content_type, reply = owner.read_replies.pop(0)
                else:
                    status, content_type, reply = 404, "application/json", NOT_FOUND
                self.send_response(status)
                if retry_after is not None:
                    self.send_header("Retry-After", retry_after)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(reply)))
                if status in (301, 302, 307, 308):
                    self.send_header("Location", "/elsewhere/")
                self.end_headers()
                self.wfile.write(reply)

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        # The default interval makes every close wait half a second
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def envelopes(self) -> List[Dict[str, Any]]:
        return [request["body"] for request in self.requests]

    def spans(self) -> List[Dict[str, Any]]:
        return [span for envelope in self.envelopes for span in envelope.get("spans", [])]

    def report(self, trace_uuid: str, status: int, body: Any = None, retry_after: Optional[str] = None) -> None:
        """Plans the next answer to a report read of the trace: a document for 200, or Emet's error code for 404."""
        if status == 404:
            body = {"error": "No report.", "code": body, "status_code": 404}
        reply = body if isinstance(body, bytes) else json.dumps({} if body is None else body).encode()
        self.reports.setdefault(trace_uuid, []).append((status, reply, retry_after))

    def polled(self, trace_uuid: str) -> int:
        """Counts the report reads of the trace."""
        return sum(1 for read in self.reads if read["path"] == f"/api/v1/traces/{trace_uuid}/report/")

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
