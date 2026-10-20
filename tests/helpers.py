"""Builds synthetic Claude Code transcripts and runs a stand-in Emet endpoint for the tests."""

from __future__ import annotations

import json
import threading
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, List, Optional


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
        with open(path or self.path, "a") as handle:
            handle.write(json.dumps(entry) + "\n")
        return entry

    def prompt(self, text: str, source: Optional[str] = "typed", **extra: Any) -> Dict[str, Any]:
        entry: Dict[str, Any] = {"type": "user", "message": {"role": "user", "content": text}, **extra}
        if source:
            entry["promptSource"] = source
        return self.write(entry)

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
        path.write_text(text)
        return path

    def hook_input(self) -> Dict[str, Any]:
        return {"session_id": self.session_id, "transcript_path": str(self.path), "cwd": "/work/acme-app", "hook_event_name": "Stop"}


class FakeEmet:
    """Records every request; answers with the next status in `statuses`, then 202."""

    def __init__(self) -> None:
        self.requests: List[Dict[str, Any]] = []
        self.statuses: List[int] = []
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length) or b"{}")
                owner.requests.append({"path": self.path, "headers": dict(self.headers), "body": body})
                status = owner.statuses.pop(0) if owner.statuses else 202
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, *args: Any) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}"

    @property
    def envelopes(self) -> List[Dict[str, Any]]:
        return [request["body"] for request in self.requests]

    def spans(self) -> List[Dict[str, Any]]:
        return [span for envelope in self.envelopes for span in envelope.get("spans", [])]

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
