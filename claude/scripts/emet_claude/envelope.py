"""Builds the Emet envelopes for a turn, split into batches that fit Emet's limits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import VERSION, content, pending, timestamps, transcript
from .config import Config
from .spans import Builder, Built

SEMCONV_VERSION = "emet-genai-1"
# Emet accepts up to 1000 spans and 32 MiB per request; stay well inside both
MAX_SPANS_PER_BATCH = 500
MAX_BATCH_BYTES = 24 * 1024 * 1024
# A single tool result is clipped only when it alone would not fit a batch
MAX_RESULT_CHARS = 16 * 1024 * 1024
TITLE_MAX = 255
# The resource attribute naming the repository, which /emet:status also shows
REPOSITORY_ATTRIBUTE = "vcs.repository.name"
# The root span attribute naming the hook of this plugin that started a turn no person started
STARTED_BY_ATTRIBUTE = "emet.plugin.turn_started_by"
REPORT_WAKE = "report_wake"
# Sent in place of the prompt of such a turn, which is Emet's own report text in Claude Code's wrapper
# What an envelope holds for the plugin's own files. Taken off before the envelope is sent
LOCAL = "plugin_local"
WAKE_PROMPT = "[Emet's report of an earlier answer, handed to Claude by the Emet plugin. No person wrote this prompt.]"


def size_of(value: Any) -> int:
    return len(json.dumps(value, ensure_ascii=False).encode("utf-8"))


def clip_span(span: Dict[str, Any]) -> Dict[str, Any]:
    """Clips an oversized tool result and marks the span truncated, as Emet requires."""
    result = span.get("payload", {}).get("result")
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    if len(text) <= MAX_RESULT_CHARS:
        return span
    span = dict(span, payload=dict(span["payload"], result=content.clip(text, MAX_RESULT_CHARS)), is_truncated=True)
    return span


def batches(spans: List[Dict[str, Any]]) -> List[List[Dict[str, Any]]]:
    """Groups spans in order into batches under the span and byte limits."""
    groups: List[List[Dict[str, Any]]] = []
    current: List[Dict[str, Any]] = []
    current_bytes = 0
    for span in spans:
        span = clip_span(span) if span.get("kind") == "tool" else span
        span_bytes = size_of(span)
        if current and (len(current) >= MAX_SPANS_PER_BATCH or current_bytes + span_bytes > MAX_BATCH_BYTES):
            groups.append(current)
            current, current_bytes = [], 0
        current.append(span)
        current_bytes += span_bytes
    if current:
        groups.append(current)
    return groups


def resource(config: Config, first: transcript.Entry, cwd: str) -> Dict[str, Any]:
    return {
        "service.name": "claude-code",
        "service.version": str(first.get("version") or ""),
        "emet.plugin.name": "emet-claude-code",
        "emet.plugin.version": VERSION,
        "emet.capture_mode": config.capture_mode,
        REPOSITORY_ATTRIBUTE: Path(cwd or first.get("cwd") or "").name,
        "vcs.ref.head.name": str(first.get("gitBranch") or ""),
    }


def started_by(envelope: Dict[str, Any]) -> str:
    """Returns what of this plugin started the envelope's turn, or "" for any other turn.

    A wake is read from the root span, which Emet is sent. A command of the plugin is read from what is kept for the plugin's files.
    """
    spans = envelope.get("spans")
    for span in spans if isinstance(spans, list) else []:
        attributes = span.get("attributes") if isinstance(span, dict) and span.get("kind") == "turn" else None
        if isinstance(attributes, dict) and attributes.get(STARTED_BY_ATTRIBUTE) == REPORT_WAKE:
            return REPORT_WAKE
    local = envelope.get(LOCAL)
    if isinstance(local, dict) and local.get(pending.STARTED_BY) == pending.OWN_COMMAND:
        return pending.OWN_COMMAND
    return ""


def turn_identity(envelope: Dict[str, Any]) -> Tuple[str, str, str]:
    """Returns Claude Code's id of the envelope's turn, the time the turn ended and "scheduled" for a turn started on a schedule.

    Each is "" when the envelope names none. Read back from a queued file, so all are checked again: a plain name,
    a time written the one way, and the one word.
    """
    local = envelope.get(LOCAL)
    named = local if isinstance(local, dict) else {}
    ended = timestamps.parse(named.get("ended"))
    origin = pending.SCHEDULED if named.get(pending.TURN_ORIGIN) == pending.SCHEDULED else ""
    return pending.plain(named.get("prompt_id")), timestamps.text(ended) if ended else "", origin


def sendable(envelope: Dict[str, Any]) -> Dict[str, Any]:
    """Returns the envelope as Emet is sent it, without what the plugin keeps for its own files."""
    return {key: value for key, value in envelope.items() if key != LOCAL}


def wrap(
    config: Config,
    session_id: str,
    trace_id: str,
    spans: List[Dict[str, Any]],
    resource_attrs: Dict[str, Any],
    title: str = "",
    local: Optional[Dict[str, str]] = None,
) -> List[Dict[str, Any]]:
    """Returns the envelopes for one trace. Only the last carries end, so Emet seals the trace once.

    local goes on every envelope: the batch that ends the turn may be delivered alone, from the queue, by another session.
    """
    groups = batches(content.clean(spans)) or [[]]
    envelopes = []
    for index, group in enumerate(groups):
        envelope: Dict[str, Any] = content.clean(
            {
                "tenant_ref": config.tenant_ref,
                "conversation_id": session_id,
                "trace_id": trace_id,
                "session_ref": session_id,
                "enduser_ref": config.enduser_ref,
                "ingestion_mode": "sdk",
                "semconv_version": SEMCONV_VERSION,
                "resource": resource_attrs,
                "end": index == len(groups) - 1,
            }
        )
        if title and index == 0:
            envelope["conversation_title"] = content.clean_text(title)
        # Spans were cleaned once, before batching
        envelope["spans"] = group
        if local:
            envelope[LOCAL] = dict(local)
        envelopes.append(envelope)
    return envelopes


def for_turn(
    config: Config,
    builder: Builder,
    session_id: str,
    turn: List[Tuple[int, transcript.Entry]],
    cwd: str,
    handed: Sequence[float] = (),
) -> Tuple[str, List[Dict[str, Any]], Built]:
    """Returns the trace id, envelopes and built spans (for the subagents to track) of one turn.

    handed holds the times the poller handed a report to Claude in this session, which a turn a wake started must follow.
    Raises timestamps.Unreadable for a turn with no time that can be read."""
    first = turn[0][1]
    times = timestamps.of(entry for _, entry in turn)
    if not times:
        raise timestamps.Unreadable("no readable time")
    uuid = first.get("uuid")
    # Without an id every such turn of a session would land on one trace; the place in the file never changes
    trace_id = uuid if isinstance(uuid, str) and uuid else f"turn-at-{turn[0][0]}"
    capture = builder.capture
    text = transcript.prompt_text(first)
    woken = transcript.started_by_wake(first, handed)
    built = Built()
    # The report text is not sent back to Emet as a person's message; marked as left out, like an image
    opening = builder.message("user", WAKE_PROMPT, True) if woken else builder.message("user", text, transcript.has_binary(first))
    builder.thread(turn[1:], "turn", opening, built, main=True, since=timestamps.parse(first.get("timestamp")) or times[0])
    root: Dict[str, Any] = {
        "span_id": "turn",
        "name": "claude_code.turn",
        "kind": "turn",
        "started_at": timestamps.text(min(times)),
        "ended_at": timestamps.text(max(times)),
        "attributes": {
            "claude_code.prompt_source": str(first.get("promptSource") or ("compaction" if first.get("isCompactSummary") else ""))
        },
    }
    if opening[2]:
        root["attributes"]["emet.redactions"] = opening[2]
    if woken:
        root["attributes"][STARTED_BY_ATTRIBUTE] = REPORT_WAKE
    title = ""
    if transcript.by_person(first) and not woken and capture.keeps_content:
        title = capture.text(text)[0].strip().splitlines()[0][:TITLE_MAX] if text.strip() else ""
    # The turn's own identity for the poller, which cannot tell it from the order turns are delivered in
    local = {"ended": root["ended_at"]}
    if pending.plain(first.get("promptId")):
        local["prompt_id"] = first["promptId"]
    # Kept on this machine only: the turn is sent as any other, and its report wakes no one
    if transcript.on_schedule(first):
        local[pending.TURN_ORIGIN] = pending.SCHEDULED
    # Kept on this machine only: the turn is sent as any other, and its report is never read
    if not woken and transcript.own_command(first):
        local[pending.STARTED_BY] = pending.OWN_COMMAND
    return trace_id, wrap(config, session_id, trace_id, [root, *built.spans], resource(config, first, cwd), title, local), built


def for_tail(
    config: Config,
    builder: Builder,
    session_id: str,
    trace_id: str,
    items: List[Tuple[int, transcript.Entry]],
    parent_span_id: str,
    cwd: str,
) -> Tuple[List[Dict[str, Any]], Built]:
    """Returns late envelopes for entries that belong to a trace already sent: the rest of a turn or of a subagent."""
    built = Built()
    builder.thread(items, parent_span_id, None, built, main=False)
    if not built.spans:
        return [], built
    return wrap(config, session_id, trace_id, built.spans, resource(config, items[0][1], cwd)), built
