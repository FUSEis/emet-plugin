"""Reads Claude Code session transcripts (JSONL). The format is not a public contract, so every read is defensive."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

Entry = Dict[str, Any]

# Prompts a person wrote or accepted, as opposed to ones Claude Code generated
PERSON_SOURCES = ("typed", "queued", "suggestion_accepted")
# Wrappers Claude Code puts around slash commands and shell escapes; no model call answers them
LOCAL_COMMAND = re.compile(
    r"^\s*<(?:command-name|command-message|local-command-stdout|local-command-stderr|bash-input|bash-stdout|bash-stderr)>"
)
PERSISTED_PATH = re.compile(r"Full output saved to: (\S+)")
# Never read more than this from a saved tool output
PERSISTED_MAX_BYTES = 8 * 1024 * 1024
BINARY_PARTS = ("image", "document")


def read_entries(path: Path, offset: int, sidechain: bool = False) -> Tuple[List[Tuple[int, Entry]], int]:
    """Returns (byte offset, entry) for each user and assistant entry after offset, and the offset after the last complete line.

    A session transcript skips sidechain entries; a subagent transcript (sidechain=True) is all sidechain.
    """
    entries: List[Tuple[int, Entry]] = []
    with open(path, "rb") as handle:
        handle.seek(0, 2)
        size = handle.tell()
        if offset > size:
            # The file was replaced or truncated; start over
            offset = 0
        handle.seek(offset)
        position = offset
        for raw in handle:
            if not raw.endswith(b"\n"):
                # Still being written; read it next time
                break
            start, position = position, position + len(raw)
            try:
                entry = json.loads(raw)
            except ValueError:
                continue
            if isinstance(entry, dict) and entry.get("type") in ("user", "assistant") and (sidechain or not entry.get("isSidechain")):
                entries.append((start, entry))
    return entries, position


def content_parts(entry: Entry) -> List[Dict[str, Any]]:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return [part for part in content if isinstance(part, dict)]
    return []


def text_of(entry: Entry) -> str:
    """Returns the text of a message, with a placeholder where it held an image or a document."""
    texts = []
    for part in content_parts(entry):
        if part.get("type") == "text":
            texts.append(part.get("text") or "")
        elif part.get("type") in BINARY_PARTS:
            texts.append(f"[{part['type']} content not shown]")
    return "\n".join(texts)


def has_binary(entry: Entry) -> bool:
    return any(part.get("type") in BINARY_PARTS for part in content_parts(entry))


def starts_turn(entry: Entry) -> bool:
    """True for a user entry that a model call answers: a prompt, a background-task notice or a compaction summary."""
    if entry.get("type") != "user" or entry.get("isMeta"):
        return False
    parts = content_parts(entry)
    if any(part.get("type") == "tool_result" for part in parts):
        return False
    return bool(parts)


def by_person(entry: Entry) -> bool:
    source = entry.get("promptSource")
    if entry.get("isCompactSummary"):
        return False
    if source is None:
        return not LOCAL_COMMAND.match(text_of(entry))
    return source in PERSON_SOURCES


def prompt_text(entry: Entry) -> str:
    text = text_of(entry)
    # Slash commands arrive wrapped in tags; show them as typed
    name = re.search(r"<command-name>(.*?)</command-name>", text, re.S)
    if name:
        args = re.search(r"<command-args>(.*?)</command-args>", text, re.S)
        return f"{name.group(1).strip()} {args.group(1).strip() if args else ''}".strip()
    return text


def split_turns(entries: List[Tuple[int, Entry]]) -> Tuple[List[Tuple[int, Entry]], List[List[Tuple[int, Entry]]]]:
    """Returns the entries before the first turn start (the tail of a turn already sent) and the turns."""
    head: List[Tuple[int, Entry]] = []
    turns: List[List[Tuple[int, Entry]]] = []
    for item in entries:
        if starts_turn(item[1]):
            turns.append([item])
        elif turns:
            turns[-1].append(item)
        else:
            head.append(item)
    return head, turns


def answered(turn: List[Tuple[int, Entry]]) -> bool:
    return any(entry.get("type") == "assistant" for _, entry in turn)


def session_folder(transcript: Path) -> Path:
    """Claude Code keeps saved tool outputs and subagent transcripts beside the session file, in a folder named after it."""
    return transcript.with_suffix("")


def persisted_output(transcript: Path, part: Dict[str, Any], result: Any) -> Optional[Tuple[str, bool]]:
    """Returns the text of a tool output Claude Code saved to disk instead of the transcript, and whether it was cut short.

    Reads only from this session's own tool-results folder, whatever path the transcript names.
    """
    candidates = []
    if isinstance(result, dict) and isinstance(result.get("persistedOutputPath"), str):
        candidates.append(result["persistedOutputPath"])
    content = part.get("content")
    if isinstance(content, str) and content.startswith("<persisted-output>"):
        match = PERSISTED_PATH.search(content)
        if match:
            candidates.append(match.group(1))
    folder = (session_folder(transcript) / "tool-results").resolve()
    for candidate in candidates:
        path = Path(candidate).resolve()
        if path.parent != folder or not path.is_file():
            continue
        with open(path, "rb") as handle:
            data = handle.read(PERSISTED_MAX_BYTES + 1)
        cut = len(data) > PERSISTED_MAX_BYTES
        return data[:PERSISTED_MAX_BYTES].decode("utf-8", "replace"), cut
    return None


def subagents(transcript: Path) -> Dict[str, Tuple[Path, Dict[str, Any]]]:
    """Maps the tool_use id that launched each subagent to its transcript and metadata."""
    found: Dict[str, Tuple[Path, Dict[str, Any]]] = {}
    folder = session_folder(transcript) / "subagents"
    if not folder.is_dir():
        return found
    real_folder = folder.resolve()
    for meta_path in folder.glob("agent-*.meta.json"):
        try:
            meta = json.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tool_use_id = meta.get("toolUseId") if isinstance(meta, dict) else None
        agent_path = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".jsonl")
        # A link pointing out of the folder could make the plugin send some other file
        if agent_path.is_symlink() or agent_path.resolve().parent != real_folder:
            continue
        if isinstance(tool_use_id, str) and agent_path.is_file():
            found[tool_use_id] = (agent_path, meta)
    return found
