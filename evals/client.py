"""Calls the Anthropic Messages API with the standard library, and a stand-in that replays written replies offline.

Requests and responses are the JSON the API documents (https://platform.claude.com/docs/en/api/messages), as plain dicts.
"""

from __future__ import annotations

import http.client
import json
import os
import socket
import time
import urllib.error
import urllib.request
from typing import Any, Callable, Dict, List, Optional, Protocol

# The only host the eval calls. The plugin itself never does
API_URL = "https://api.anthropic.com/v1/messages"
# The version whose request and response shapes this file was written against
API_VERSION = "2026-10-21"
KEY_VARIABLE = "ANTHROPIC_API_KEY"
# The API's errors page says to retry a rate limit, a server error and an overload. A 5xx sent after the model
# ran can be billed once more; a reply lost after the request was sent is never asked for again
RETRIED = (429,)
RETRIES = 4
# A timeout says nothing about whether the request arrived (socket.timeout is its own class before Python 3.10)
TIMEOUTS = (socket.timeout, TimeoutError)
# The only failures that prove no request left: nothing listens at the address, or its name does not resolve
NEVER_SENT = (ConnectionRefusedError, socket.gaierror)
MAX_WAIT_S = 60.0
# A case scripts this many assistant turns before its notice, and this many more for each earlier wake in the session
SCRIPTED_ASSISTANT_TURNS = 2
EARLIER_WAKE_TURNS = 3


class Client(Protocol):
    def create(self, request: Dict[str, Any]) -> Dict[str, Any]:
        """Sends one Messages request and returns the response body."""


class ApiError(Exception):
    """Carries the status and the API's error type. Never the request, its headers or the key."""

    def __init__(self, status: int, kind: str) -> None:
        super().__init__(f"{status} {kind}")
        self.status = status
        self.kind = kind


class NoRedirect(urllib.request.HTTPRedirectHandler):
    """Refuses every redirect, so the key is never sent on to another address."""

    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def ask(call: urllib.request.Request, timeout: float) -> Any:
    """Makes one request and follows no redirect: a 3xx answer is raised as an HTTPError."""
    return urllib.request.build_opener(NoRedirect).open(call, timeout=timeout)


def count(tokens: Dict[str, int], response: Dict[str, Any]) -> None:
    """Adds the tokens a response reports to a running total."""
    usage = response.get("usage")
    for key in ("input", "output"):
        spent = usage.get(f"{key}_tokens") if isinstance(usage, dict) else None
        tokens[key] += spent if isinstance(spent, int) and not isinstance(spent, bool) else 0


def text_of(response: Dict[str, Any]) -> str:
    """Joins the text blocks of a response. Thinking and tool blocks are skipped."""
    return "\n".join(block.get("text", "") for block in blocks(response) if block.get("type") == "text").strip()


def blocks(response: Dict[str, Any]) -> List[Dict[str, Any]]:
    content = response.get("content")
    return [block for block in content if isinstance(block, dict)] if isinstance(content, list) else []


class AnthropicClient:
    """Reads the key from the environment once, and keeps it out of every message, error and repr."""

    def __init__(self, url: str = API_URL, timeout: float = 600.0, sleep: Callable[[float], None] = time.sleep) -> None:
        key = os.environ.get(KEY_VARIABLE, "")
        if not key:
            raise SystemExit(f"{KEY_VARIABLE} is not set. Set it in your shell, or pass --offline to replay the written replies.")
        self._key = key
        self.url = url
        self.timeout = timeout
        self.sleep = sleep

    def create(self, request: Dict[str, Any]) -> Dict[str, Any]:
        body = json.dumps(request).encode("utf-8")
        headers = {"x-api-key": self._key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        for attempt in range(RETRIES + 1):
            wait: Optional[float] = None
            try:
                call = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
                with ask(call, self.timeout) as reply:
                    return parsed(reply.status, reply.read())
            except urllib.error.HTTPError as error:
                if 300 <= error.code < 400:
                    raise ApiError(error.code, "redirect") from None
                kind = error_kind(error)
                if attempt == RETRIES or not (error.code in RETRIED or error.code >= 500):
                    raise ApiError(error.code, kind) from None
                wait = retry_after(error)
            except urllib.error.URLError as error:
                # Asked again only when the request cannot have left. A timeout while connecting is not that proof
                if attempt == RETRIES or not isinstance(error.reason, NEVER_SENT):
                    raise ApiError(0, "connection_error") from None
            except (*TIMEOUTS, ConnectionError, http.client.HTTPException):
                # Sent, and the reply was lost: the API may have run it
                raise ApiError(0, "no_reply") from None
            self.sleep(min(wait if wait is not None else 2.0**attempt, MAX_WAIT_S))
        raise ApiError(0, "connection_error")  # pragma: no cover - the loop always returns or raises


def parsed(status: int, body: bytes) -> Dict[str, Any]:
    """Returns the response object. Anything else is an error of this one call, like any other."""
    try:
        answer = json.loads(body)
    except ValueError:
        raise ApiError(status, "bad_response") from None
    if not isinstance(answer, dict):
        raise ApiError(status, "bad_response")
    return answer


def error_kind(error: urllib.error.HTTPError) -> str:
    """Returns the API's error type, limited to plain characters."""
    try:
        kind = json.loads(error.read())["error"]["type"]
    except (ValueError, KeyError, TypeError, OSError):
        return "error"
    return kind if isinstance(kind, str) and kind.replace("_", "").isalnum() and len(kind) <= 40 else "error"


def retry_after(error: urllib.error.HTTPError) -> Optional[float]:
    try:
        return max(0.0, float(error.headers.get("retry-after", "")))
    except (ValueError, TypeError):
        return None


class ReplayClient:
    """Answers each case with the reply written for it in fixtures/replies.json, so the whole run works with no key.

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
