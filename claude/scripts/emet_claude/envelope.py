"""Builds the Emet envelopes for a turn, split into batches that fit Emet's limits."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Tuple

from . import VERSION, content, transcript
from .config import Config
from .spans import Builder, Built

SEMCONV_VERSION = "emet-genai-1"
# Emet accepts up to 1000 spans and 32 MiB per request; stay well inside both
MAX_SPANS_PER_BATCH = 500
MAX_BATCH_BYTES = 24 * 1024 * 1024
# A single tool result is clipped only when it alone would not fit a batch
MAX_RESULT_CHARS = 16 * 1024 * 1024
TITLE_MAX = 255


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
        "vcs.repository.name": Path(cwd or first.get("cwd") or "").name,
        "vcs.ref.head.name": str(first.get("gitBranch") or ""),
    }


def wrap(
    config: Config,
    session_id: str,
    trace_id: str,
    spans: List[Dict[str, Any]],
    resource_attrs: Dict[str, Any],
    title: str = "",
) -> List[Dict[str, Any]]:
    """Returns the envelopes for one trace. Only the last carries end, so Emet seals the trace once."""
    groups = batches(content.clean(spans)) or [[]]
    envelopes = []
    for index, group in enumerate(groups):
        envelope: Dict[str, Any] = content.clean(
            {
                "tenant_ref": config.tenant_ref,
                "conversation_id": session_id,
                "trace_id": trace_id,
                "session_ref": session_id,
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
        envelopes.append(envelope)
    return envelopes


def for_turn(
    config: Config,
    builder: Builder,
    session_id: str,
    turn: List[Tuple[int, transcript.Entry]],
    cwd: str,
) -> Tuple[str, List[Dict[str, Any]], Built]:
    """Returns the trace id, envelopes and built spans (for the subagents to track) of one turn."""
    first = turn[0][1]
    trace_id = str(first.get("uuid"))
    capture = builder.capture
    text = transcript.prompt_text(first)
    built = Built()
    opening = builder.message("user", text, transcript.has_binary(first))
    timestamps = [str(entry["timestamp"]) for _, entry in turn if entry.get("timestamp")]
    builder.thread(turn[1:], "turn", opening, built, main=True, since=str(first.get("timestamp") or ""))
    root: Dict[str, Any] = {
        "span_id": "turn",
        "name": "claude_code.turn",
        "kind": "turn",
        "started_at": timestamps[0],
        "ended_at": max(timestamps),
        "attributes": {
            "claude_code.prompt_source": str(first.get("promptSource") or ("compaction" if first.get("isCompactSummary") else ""))
        },
    }
    if opening[2]:
        root["attributes"]["emet.redactions"] = opening[2]
    title = ""
    if transcript.by_person(first) and capture.keeps_content:
        title = capture.text(text)[0].strip().splitlines()[0][:TITLE_MAX] if text.strip() else ""
    return trace_id, wrap(config, session_id, trace_id, [root, *built.spans], resource(config, first, cwd), title), built


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
