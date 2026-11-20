"""Loads the cases and turns each into the conversation a model sees, with the notice rendered by this checkout's report.py."""

from __future__ import annotations

import hashlib
import importlib
import importlib.util
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Set

from . import rules

HERE = Path(__file__).resolve().parent
# The plugin of the checkout the eval runs in, never an installed copy
sys.path.insert(0, str(HERE.parent / "claude" / "scripts"))

from emet_claude import report  # noqa: E402


def use_plugin(scripts: Path) -> None:
    """Renders every notice with the report.py of another checkout, loaded under its own name beside this one's."""
    package = scripts / "emet_claude"
    name = "emet_claude_at_" + hashlib.sha256(str(package.resolve()).encode("utf-8")).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, package / "__init__.py", submodule_search_locations=[str(package)])
    if not (package / "report.py").is_file() or spec is None or spec.loader is None:
        raise SystemExit(f"No emet_claude/report.py under {scripts}. Name the plugin's scripts folder, such as ../other/claude/scripts.")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    # Nothing is written into the other checkout, not even bytecode
    writes, sys.dont_write_bytecode = sys.dont_write_bytecode, True
    try:
        spec.loader.exec_module(module)
        globals()["report"] = importlib.import_module(f"{name}.report")
    finally:
        sys.dont_write_bytecode = writes


Case = Dict[str, Any]
Message = Dict[str, Any]

ENDPOINT = "https://app.emet.so"
SESSION = "session-eval"
CONVERSATION = "7d3c1c52-0d0a-4a52-8d0e-0c7e3a1b2c3d"

# Stands in for Claude Code's own system prompt, which is far longer. It says nothing about Emet
SYSTEM = (
    "You are Claude Code, a coding assistant working in the user's terminal on the repository at /work/acme-app. "
    "Use the tools to read files and run commands when the user's request needs them. Keep replies short."
)
# Nothing runs: a call is recorded and answered with fixed text
TOOLS: List[Dict[str, Any]] = [
    {
        "name": "read_file",
        "description": "Reads a text file from the repository and returns its contents.",
        "input_schema": {
            "type": "object",
            "properties": {"path": {"type": "string", "description": "Path relative to the repository root."}},
            "required": ["path"],
        },
    },
    {
        "name": "run_command",
        "description": "Runs a shell command in the repository root and returns what it printed.",
        "input_schema": {
            "type": "object",
            "properties": {"command": {"type": "string", "description": "The command line to run."}},
            "required": ["command"],
        },
    },
    {
        "name": "fetch_url",
        "description": "Fetches a web page and returns its text.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string", "description": "The address to fetch."}},
            "required": ["url"],
        },
    },
]
TOOL_RESULTS = {"read_file": "File not found.", "run_command": "(no output)", "fetch_url": "Request failed: the network is unavailable."}


def load(path: Path = HERE / "cases.json") -> List[Case]:
    cases: List[Case] = json.loads(path.read_text(encoding="utf-8"))
    return cases


def traits(case: Case) -> Set[str]:
    """Names what a case is, which decides the rules it is graded on."""
    found = {"report", "missed" if "missed" in case else "wake"}
    if disputed(case, made=True):
        found.add("made")
    if case.get("unseen_claim"):
        found.add("unseen")
    if "followup" in case:
        found.add("followup")
    # What the tool output shows about each disputed claim decides how a ruling is graded
    for trait, words in (("contradicted", ("contradicts",)), ("supported", ("supports",)), ("unsettled", ("none",))):
        if shown(case, *words):
            found.add(trait)
    if shown(case, "contradicts", "supports"):
        found.add("settled")
    return found


def disputed(case: Case, made: bool = False) -> List[Dict[str, Any]]:
    """Returns the claims Emet disputed, or only those the assistant's answer holds."""
    return [item for item in case["claims"] if item["verdict"] == "disputed" and (not made or item.get("stated_in"))]


def shown(case: Case, *words: str) -> List[Dict[str, Any]]:
    """Returns the disputed claims of the answer whose "shows" is one of the words: contradicts, supports or none."""
    return [item for item in disputed(case, made=True) if item["shows"] in words]


def document(case: Case) -> Dict[str, Any]:
    """Builds the report document Emet would return for the case's answer."""
    answer = case["answer"]
    claims = []
    for number, item in enumerate(case["claims"], start=1):
        passage = item.get("stated_in", "")
        start = answer.index(passage) if passage else 0
        claims.append(
            {
                "id": f"00000000-0000-4000-8000-{number:012d}",
                "sequence": number,
                "text": item["text"],
                "verdict": item["verdict"],
                "reason": item.get("reason", ""),
                "clarification": item.get("clarification", ""),
                "correction": item.get("correction", ""),
                "ranges": [{"kind": "passage", "start": start, "end": start + len(passage)}] if passage else [],
            }
        )

    def tally(verdict: str) -> int:
        return sum(1 for item in claims if item["verdict"] == verdict)

    return {
        "version": report.DOCUMENT_VERSION,
        "report": {"id": "R-1", "status": report.FLAGGED, "status_text": "Flagged"},
        "trace_id": "turn-1",
        "conversation_id": SESSION,
        "answer": {"text": answer},
        "counts": {
            "claims": len(claims),
            "verified": tally("verified"),
            "clarified": tally("imprecise"),
            "disputed": tally("disputed"),
            "corrected": 0,
            "unchecked": 0,
        },
        "claims": claims,
    }


def notice(case: Case) -> str:
    """Returns the notice as Claude Code shows it to the model."""
    if "missed" in case:
        return rules.MISSED_PREFIX + report.missed_message(document(case), ENDPOINT, SESSION, CONVERSATION)
    return rules.WAKE_PREFIX + report.wake_message(document(case), ENDPOINT, SESSION, CONVERSATION)


def scripted(turn: Case, number: int) -> List[Message]:
    """Returns one scripted turn: the request, a tool call with its result, and the answer."""
    tool, tool_id = turn["tool"], f"toolu_eval_{number}"
    return [
        {"role": "user", "content": turn["request"]},
        {"role": "assistant", "content": [{"type": "tool_use", "id": tool_id, "name": tool["name"], "input": tool["input"]}]},
        {"role": "user", "content": [{"type": "tool_result", "tool_use_id": tool_id, "content": tool["result"]}]},
        {"role": "assistant", "content": turn["answer"]},
    ]


def messages(case: Case) -> List[Message]:
    """Returns the scripted turn, then the notice as the next user-side message. Earlier wakes of the session come first."""
    found: List[Message] = []
    earlier: List[Case] = case.get("earlier", [])
    for number, turn in enumerate(earlier, start=1):
        found += scripted(turn, number)
        found += [{"role": "user", "content": notice(turn)}, {"role": "assistant", "content": turn["reply"]}]
    last: Any = notice(case)
    if "missed" in case:
        last = [{"type": "text", "text": last}, {"type": "text", "text": case["missed"]["prompt"]}]
    return [*found, *scripted(case, len(earlier) + 1), {"role": "user", "content": last}]


def tool_result(case: Case, name: str) -> str:
    """Returns the fixed text that answers a tool call the model made."""
    planned: Dict[str, str] = case.get("missed", {}).get("tool_results", {})
    return planned.get(name, TOOL_RESULTS.get(name, "(no output)"))


def wording(cases: List[Case]) -> str:
    """Identifies the wording under test: it changes when any rendered notice changes."""
    digest = hashlib.sha256("\n".join(notice(case) for case in cases).encode("utf-8"))
    return digest.hexdigest()[:12]
