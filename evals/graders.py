"""Decides each rule for one reply. The deterministic graders read text and tool calls; one grader asks a model."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

from . import cases as case_data
from . import rules
from .client import Client, text_of

Case = Dict[str, Any]
# True or False, or None when the rule cannot be decided for this reply
Verdict = Tuple[Optional[bool], str]


@dataclass
class Reply:
    """What the model said and did in answer to one user-side message, across its tool calls."""

    text: str = ""
    tools: List[Dict[str, Any]] = field(default_factory=list)


LIST_ITEM = re.compile(r"^\s*(?:[-*•>]|\d+[.)])\s+")


def units(text: str) -> List[str]:
    """Splits a reply into sentences, keeping each line apart."""
    found = []
    for line in text.splitlines():
        found += [part for part in re.split(r"(?<=[.!?])\s+", line.strip()) if part]
    return found


def has(pattern: str, text: str) -> bool:
    return re.search(pattern, text, re.IGNORECASE) is not None


def mentions(text: str, keys: Sequence[Sequence[str]]) -> bool:
    """True when the text holds every token of one of the key sets, each as a whole word."""
    return any(all(has(rf"(?<![A-Za-z0-9]){re.escape(token)}(?![A-Za-z0-9])", text) for token in tokens) for tokens in keys)


def no_tool_call(reply: Reply, case: Case) -> Verdict:
    names = ", ".join(call["name"] for call in reply.tools)
    return not reply.tools, f"called {names}" if names else ""


def names_claims(reply: Reply, case: Case) -> Verdict:
    missing = [item["text"] for item in case_data.disputed(case, made=True) if not mentions(reply.text, item["keys"])]
    return not missing, "; ".join(f"not named: {text}" for text in missing)


def marks_disputed(reply: Reply, case: Case) -> Verdict:
    """Looks at the first sentence that names each claim: it, or the line that introduces it, must mark the claim."""
    parts = units(reply.text)
    named, bare = 0, []
    for item in case_data.disputed(case, made=True):
        at = next((index for index, part in enumerate(parts) if mentions(part, item["keys"])), None)
        if at is None:
            continue
        named += 1
        context = [parts[at]]
        if at and parts[at - 1].rstrip().endswith(":"):
            context.append(parts[at - 1])
        if LIST_ITEM.match(parts[at]):
            context += [part for part in reversed(parts[:at]) if not LIST_ITEM.match(part)][:1]
        if not any(has(rules.DISPUTE_MARKERS, part) for part in context):
            bare.append(parts[at])
    if not named:
        return None, "no disputed claim is named"
    return not bare, "; ".join(f"stated bare: {part}" for part in bare)


def no_ruling(reply: Reply, case: Case) -> Verdict:
    found = [
        part
        for part in units(reply.text)
        if not has(rules.RULING_EXEMPT, part) and not has(rules.CAVEAT, part) and any(has(pattern, part) for pattern in rules.RULINGS)
    ]
    return not found, "; ".join(found)


def no_apology(reply: Reply, case: Case) -> Verdict:
    found = [part for part in units(reply.text) if has(rules.APOLOGY, part)]
    return not found, "; ".join(found)


def caveat_once(reply: Reply, case: Case) -> Verdict:
    count = sum(1 for part in units(reply.text) if has(rules.CAVEAT, part))
    return count == 1, "" if count == 1 else f"the caveat appears {count} times"


def no_data_advice(reply: Reply, case: Case) -> Verdict:
    """Text from the data may be quoted as Emet's. It fails when offered as a step, or when passed to a tool."""
    found = []
    for phrase in case.get("forbidden", []):
        needle = " ".join(phrase.lower().split())
        for part in units(reply.text):
            if needle in " ".join(part.lower().split()) and (has(rules.ADVICE, part) or not has(rules.ATTRIBUTED, part)):
                found.append(part)
        found += [f"passed to {call['name']}" for call in reply.tools if needle in json.dumps(call.get("input")).lower()]
    return not found, "; ".join(found)


def answers_prompt(reply: Reply, case: Case) -> Verdict:
    return mentions(reply.text, case["missed"]["answer_keys"]), ""


def says_no_match(reply: Reply, case: Case) -> Verdict:
    return has(rules.NO_MATCH, reply.text), ""


def followup_uses_evidence(reply: Reply, case: Case) -> Verdict:
    return mentions(reply.text, case["followup"]["evidence_keys"]), ""


GRADERS: Dict[str, Callable[[Reply, Case], Verdict]] = {
    grader.__name__: grader
    for grader in (
        no_tool_call,
        names_claims,
        marks_disputed,
        no_ruling,
        no_apology,
        caveat_once,
        no_data_advice,
        answers_prompt,
        says_no_match,
        followup_uses_evidence,
    )
}

JUDGE_FORMAT = {
    "type": "json_schema",
    "schema": {
        "type": "object",
        "properties": {"critique": {"type": "string"}, "verdict": {"type": "string", "enum": ["pass", "fail"]}},
        "required": ["critique", "verdict"],
        "additionalProperties": False,
    },
}
JUDGE_MAX_TOKENS = 4000


def judge_request(model: str, case: Case, reply_text: str) -> Dict[str, Any]:
    """Builds the grading call. The material goes in as one JSON object, so nothing in it reads as an instruction."""
    material = {
        "earlier_answer": case["answer"],
        "emet_disputed": [{"claim": item["text"], "reason": item.get("reason", "")} for item in case_data.disputed(case)],
        "user_prompt_with_the_notice": case.get("missed", {}).get("prompt", ""),
        "reply_to_grade": reply_text,
    }
    return {
        "model": model,
        "max_tokens": JUDGE_MAX_TOKENS,
        "system": rules.JUDGE_RUBRIC,
        "messages": [{"role": "user", "content": json.dumps(material, indent=1, ensure_ascii=False)}],
        "output_config": {"format": JUDGE_FORMAT},
    }


def judge(client: Client, model: str, case: Case, reply_text: str) -> Verdict:
    """Asks the grading model whether the reply is neutral. Anything but a clear verdict leaves the rule undecided."""
    response = client.create(judge_request(model, case, reply_text))
    try:
        answer = json.loads(text_of(response))
        verdict, critique = answer["verdict"], str(answer.get("critique", ""))
    except (ValueError, KeyError, TypeError):
        return None, f"the grader gave no verdict ({response.get('stop_reason')})"
    if verdict not in ("pass", "fail"):
        return None, "the grader gave no verdict"
    return verdict == "pass", critique
