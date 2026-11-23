"""Calls models through the Claude Code CLI (`claude -p`) on the logged-in account, and replays written replies offline.

Requests and responses are the Messages-shaped dicts the harness already uses, so graders and cases do not change.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from typing import Any, Dict, List, Protocol

# --safe-mode turns off the user's hooks, plugins, skills, MCP servers and CLAUDE.md but keeps the login (--bare drops it).
# dontAsk denies every call that would prompt, and an ask rule per enabled tool makes each one prompt: seen, never run
FLAGS = [
    *("--safe-mode", "--setting-sources", "project,local", "--strict-mcp-config", "--disable-slash-commands"),
    *("--no-session-persistence", "--include-hook-events", "--permission-mode", "dontAsk"),
    *("--output-format", "stream-json", "--verbose", "--max-turns", "1"),
]
# The case's stand-in tools, as Claude Code names them
TOOLS = {"read_file": "Read", "run_command": "Bash", "fetch_url": "WebFetch"}
# What the child needs to find the login and run; provider and routing overrides stay out
ENV_NAMES = ("PATH", "HOME", "USER", "LOGNAME", "LANG", "LC_ALL", "TMPDIR", "TERM")
# A case scripts this many assistant turns before its notice, and this many more for each earlier wake in the session
SCRIPTED_ASSISTANT_TURNS = 2
EARLIER_WAKE_TURNS = 3


class Client(Protocol):
    def create(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Runs one Messages-shaped request and returns a Messages-shaped response."""


class ApiError(Exception):
    """One failed call: the exit code and a short kind. Never the prompt or the output."""

    def __init__(self, status: int, kind: str) -> None:
        super().__init__(f"{status} {kind}")


def count(tokens: Dict[str, int], response: Dict[str, Any]) -> None:
    usage = response.get("usage") or {}
    for key in ("input", "output"):
        tokens[key] += usage.get(f"{key}_tokens", 0)


def blocks(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = response.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


def text_of(response: Dict[str, Any]) -> str:
    return "\n".join(block.get("text", "") for block in blocks(response) if block.get("type") == "text").strip()


def render(message: Dict[str, Any]) -> str:
    """Writes a turn as text: the CLI takes no earlier assistant turn or tool result as structured input."""
    content = message["content"]
    return content if isinstance(content, str) else "\n".join(block.get("text") or f"[{json.dumps(block)}]" for block in content)


class ClaudeCodeClient:
    """Runs each request as one `claude -p` call, from an empty temporary directory and without any billing key."""

    def __init__(self, timeout: float = 600.0) -> None:
        if shutil.which("claude") is None:
            raise SystemExit("The claude command is not on PATH. Install Claude Code and log in, or pass --offline.")
        self.timeout = timeout

    def create(self, request: Dict[str, Any]) -> Dict[str, Any]:
        *earlier, last = request["messages"]
        prompt = render(last)
        if earlier:
            transcript = "\n\n".join(f"[{item['role']}]\n{render(item)}" for item in earlier)
            prompt = f"The conversation so far. You are the assistant.\n\n<transcript>\n{transcript}\n</transcript>\n\n{prompt}"
        graded = "format" in request.get("output_config", {})
        system = request["system"] + ("\n\nReply with the JSON object only, with no code fence." if graded else "")
        command = ["claude", "-p", "--model", request["model"], "--system-prompt", system, *FLAGS]
        tools = [TOOLS[tool["name"]] for tool in request.get("tools", [])]
        command += ["--tools", ",".join(tools), "--settings", json.dumps({"permissions": {"ask": tools}})]
        if "effort" in request.get("output_config", {}):
            command += ["--effort", request["output_config"]["effort"]]
        env = {key: os.environ[key] for key in ENV_NAMES if key in os.environ}
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(request["max_tokens"])
        with tempfile.TemporaryDirectory(prefix="emet-eval-") as empty:
            try:
                done = subprocess.run(command, input=prompt, capture_output=True, text=True, timeout=self.timeout, cwd=empty, env=env)  # noqa: S603
            except subprocess.TimeoutExpired:
                raise ApiError(0, "timeout") from None
        response = parse(done.stdout, done.returncode)
        if graded:
            response["content"] = [{"type": "text", "text": judged(text_of(response))}]
        return response


def parse(output: str, code: int) -> Dict[str, Any]:
    """Turns the stream-json lines into a response. A hook event, a failed run or an empty reply is an error."""
    try:
        events = [json.loads(line) for line in output.splitlines() if line.strip()]
    except ValueError:
        raise ApiError(code, "bad_stream") from None
    if not all(isinstance(event, dict) for event in events):
        raise ApiError(code, "bad_stream")
    if any(event.get("type") == "system" and str(event.get("subtype")).startswith("hook") for event in events):
        raise ApiError(code, "hook_ran")
    done: Dict[str, Any] = next((event for event in events if event.get("type") == "result"), {})
    messages = [event["message"] for event in events if event.get("type") == "assistant" and not event.get("parent_tool_use_id")]
    content = [block for message in messages for block in message["content"]]
    for block in content:
        block["name"] = {claude: ours for ours, claude in TOOLS.items()}.get(block.get("name"), block.get("name"))
    cut = done.get("subtype") == "error_max_turns"  # A tool attempt ends the run this way, with exit code 1
    if not done or not content or ((code or done.get("is_error")) and not cut):
        raise ApiError(code, str(done.get("subtype") or "claude_exit"))
    stop = messages[-1].get("stop_reason") or "end_turn"
    return {"content": content, "stop_reason": stop, "usage": done.get("usage")}


def judged(text: str) -> str:
    """Drops a markdown fence a model put around its JSON verdict."""
    return text.strip().removeprefix("```json").removeprefix("```").removesuffix("```").strip()


class ReplayClient:
    """Answers each case with the reply written for it in fixtures/replies.json, so the whole run works with no login.

    A case is found by its opening request, and the step by how many assistant turns the request already holds.
    A grading request is answered with the hand label of the reply when it has one, else with a pass.
    """

    def __init__(self, cases: List[Dict[str, Any]], replies: Dict[str, List[Any]], labels: List[Dict[str, Any]]) -> None:
        self.by_request = {opening(case): (scripted_turns(case), replies[case["id"]]) for case in cases}
        self.labels = {item["reply"]: item["label"] for item in labels}
        self.requests: List[Dict[str, Any]] = []

    def create(self, request: Dict[str, Any]) -> Dict[str, Any]:
        # A copy: the caller goes on adding to its messages
        self.requests.append(json.loads(json.dumps(request)))
        if "output_config" in request and "format" in request["output_config"]:
            graded = json.loads(request["messages"][0]["content"])["reply_to_grade"]
            verdict = {"critique": "Replayed from the hand label.", "verdict": self.labels.get(graded, "pass")}
            return message([{"type": "text", "text": json.dumps(verdict)}])
        scripted, steps = self.by_request[request["messages"][0]["content"]]
        taken = sum(1 for item in request["messages"] if item["role"] == "assistant")
        step = steps[taken - scripted]
        return message([{"type": "text", "text": step}] if isinstance(step, str) else step)


def opening(case: Dict[str, Any]) -> str:
    """Returns the first user message of the case's conversation."""
    first: str = (case.get("earlier") or [case])[0]["request"]
    return first


def scripted_turns(case: Dict[str, Any]) -> int:
    return SCRIPTED_ASSISTANT_TURNS + EARLIER_WAKE_TURNS * len(case.get("earlier", []))


def message(content: List[Dict[str, Any]], stop_reason: str = "") -> Dict[str, Any]:
    """Builds a response body in the API's shape."""
    calls = any(block.get("type") == "tool_use" for block in content)
    return {
        "id": "msg_replay",
        "type": "message",
        "role": "assistant",
        "model": "replay",
        "content": content,
        "stop_reason": stop_reason or ("tool_use" if calls else "end_turn"),
        "usage": {"input_tokens": 0, "output_tokens": 0},
    }
