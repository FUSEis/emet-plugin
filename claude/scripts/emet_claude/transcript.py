"""Reads Claude Code session transcripts (JSONL). The format is not a public contract, so every read is defensive."""

from __future__ import annotations

import functools
import os
import re
import stat
import sys
from pathlib import Path
from typing import Any, BinaryIO, Dict, List, Optional, Sequence, Tuple

from . import report, storage, timestamps

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
# How Claude Code names a turn a notice started, in turnOrigin and in origin.kind
NOTICE_ORIGINS = ("task_notification", "task-notification")
# How much of a prompt is searched for the plugin's own sentence; a wake is far shorter
WAKE_SCAN = 64 * 1024
# How long after the poller handed a report to Claude a turn can still be the one that wake started
WAKE_WINDOW_S = 600
# The plugin's own folder, two above this package
PLUGIN_ROOT = Path(__file__).resolve().parent.parent.parent
# A plugin or skill name as it can stand in a command
COMMAND_PART = re.compile(r"[a-z0-9][a-z0-9_-]{0,63}")
# The whole prompt of a turn a skill's slash command started, as Claude Code 2.1.305 writes it
COMMAND_PROMPT = re.compile(
    r"<command-message>([^<>\s]{1,200})</command-message>\s*<command-name>/([^<>\s]{1,200})</command-name>"
    r"(?:\s*<command-args>.*</command-args>)?",
    re.S,
)
# How Claude Code names a turn a person started, in turnOrigin and in origin.kind
PERSON_ORIGIN = "human"


def read_entries(path: Path, offset: int, sidechain: bool = False) -> Tuple[List[Tuple[int, Entry]], int]:
    """Returns (byte offset, entry) for each user and assistant entry after offset, and the offset after the last complete line.

    A session transcript skips sidechain entries; a subagent transcript (sidechain=True) is all sidechain.
    """
    with open(path, "rb") as handle:
        return entries_of(handle, offset, sidechain)


def entries_of(handle: BinaryIO, offset: int, sidechain: bool = False) -> Tuple[List[Tuple[int, Entry]], int]:
    """Does the work of read_entries on a file that is already open."""
    entries: List[Tuple[int, Entry]] = []
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
            entry = storage.loads(raw)
        # RecursionError: a line nested too deep to read, which would otherwise fail on every run
        except (ValueError, RecursionError):
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


def started_by_wake(entry: Entry, handed: Sequence[float] = ()) -> bool:
    """Says whether this plugin's own wake started the turn. handed holds the times the poller handed a report to Claude in this session.

    Three things are needed: Claude Code names the turn a notice and nothing names a person, the text holds the wake's
    opening sentence, and the poller recorded a wake shortly before. A finished background command starts a notice turn
    too, and anyone can print or paste the sentence.
    """
    origin = entry.get("origin")
    named = [name for name in (entry.get("turnOrigin"), origin.get("kind") if isinstance(origin, dict) else None) if name is not None]
    # Every field that names an origin must name a notice
    if not named or not all(isinstance(name, str) and name in NOTICE_ORIGINS for name in named):
        return False
    if entry.get("promptSource") in PERSON_SOURCES:
        return False
    # Claude Code can wrap the lines again, and a cut keeps the start
    if report.WAKE_OPENING not in " ".join(text_of(entry)[:WAKE_SCAN].split()):
        return False
    start = timestamps.parse(entry.get("timestamp"))
    return start is not None and any(0 <= start.timestamp() - at <= WAKE_WINDOW_S for at in handed)


@functools.lru_cache(maxsize=1)
def own_commands() -> Tuple[str, ...]:
    """Returns the commands this plugin ships, each as plugin:skill, from its manifest and its skills folder. Never raises.

    The plugin's name is the one in the installed plugin.json. A command Claude Code names under another plugin name is not one of these.
    """
    manifest = storage.read_json(PLUGIN_ROOT / ".claude-plugin" / "plugin.json", None)
    name = manifest.get("name") if isinstance(manifest, dict) else None
    if not isinstance(name, str) or not COMMAND_PART.fullmatch(name):
        return ()
    try:
        skills = sorted(path.parent.name for path in (PLUGIN_ROOT / "skills").glob("*/SKILL.md"))
    except OSError:
        return ()
    return tuple(f"{name}:{skill}" for skill in skills if COMMAND_PART.fullmatch(skill))


def own_command(entry: Entry) -> str:
    """Returns the command of this plugin that started the turn, as plugin:skill, or "" for any other turn.

    The whole prompt must be Claude Code's wrapper of the command, and nothing may name another origin than a person.
    A message that quotes the wrapper among other words is an ordinary turn.
    """
    if entry.get("type") != "user" or entry.get("isMeta") or entry.get("isSidechain") or entry.get("isCompactSummary"):
        return ""
    origin = entry.get("origin")
    if origin is not None and not isinstance(origin, dict):
        return ""
    named = [entry.get("turnOrigin"), origin.get("kind") if origin else None]
    if any(name is not None and name != PERSON_ORIGIN for name in named):
        return ""
    if entry.get("promptSource") is not None and entry.get("promptSource") not in PERSON_SOURCES:
        return ""
    parts = content_parts(entry)
    if len(parts) != 1 or parts[0].get("type") != "text" or not isinstance(parts[0].get("text"), str):
        return ""
    text = parts[0]["text"]
    match = COMMAND_PROMPT.fullmatch(text.strip()) if len(text) <= WAKE_SCAN else None
    if not match or match.group(1) != match.group(2):
        return ""
    return match.group(1) if match.group(1) in own_commands() else ""


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


def copied(items: List[Tuple[int, Entry]]) -> bool:
    """Says whether the entries are ones a fork copied from another session: each carries forkedFrom, as Claude Code 2.1.305 writes it.

    One entry without it makes them the session's own. A user entry marked isMeta, such as the caveat before a local
    command, is no continuation by the model and decides nothing, unless no other entry is there. The field is not documented.
    """
    deciding = [entry for _, entry in items if not (entry.get("type") == "user" and entry.get("isMeta"))]
    return bool(items) and all(isinstance(entry.get("forkedFrom"), dict) for entry in deciding or [entry for _, entry in items])


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
        try:
            with open(path, "rb") as handle:
                data = handle.read(PERSISTED_MAX_BYTES + 1)
        # Removed or locked since the check above: the result in the transcript is sent instead
        except OSError:
            continue
        cut = len(data) > PERSISTED_MAX_BYTES
        return data[:PERSISTED_MAX_BYTES].decode("utf-8", "replace"), cut
    return None


def own_subagent_file(transcript: Path, path: Path) -> bool:
    """Says whether path is a file of this session's own subagents folder, with neither the file nor that folder a symbolic link.

    Asked again before every read: a path kept in the state file is only as trustworthy as that file.
    """
    folder = session_folder(transcript) / "subagents"
    try:
        return not path.is_symlink() and not folder.is_symlink() and path.resolve().parent == folder.resolve()
    # RuntimeError: a loop of links, before Python 3.13. ValueError: a path no file can have
    except (OSError, RuntimeError, ValueError):
        return False


def open_subagent_file(transcript: Path, path: Path) -> Optional[BinaryIO]:
    """Opens a subagent file for reading, or returns None when it is not the session's own or cannot be opened.

    The file is opened first and judged afterwards, so the file that was checked is the one that is read. A link is
    refused by the open itself where the platform can, and a file with a second name (a hard link) is refused too.
    """
    try:
        fd = os.open(str(path), storage.READ_FLAGS)
    except (OSError, ValueError):
        return None
    try:
        opened = os.fstat(fd)
        own = stat.S_ISREG(opened.st_mode) and opened.st_nlink == 1 and own_subagent_file(transcript, path)
        # The name must still lead to the file that was opened. Not compared on Windows, where the check above refuses a link
        if own and (sys.platform == "win32" or os.path.samestat(opened, os.lstat(path))):
            return os.fdopen(fd, "rb")
    except (OSError, ValueError):
        pass
    os.close(fd)
    return None


def read_subagent(transcript: Path, path: Path, offset: int) -> Optional[Tuple[List[Tuple[int, Entry]], int]]:
    """Reads a subagent file as read_entries does, or returns None when open_subagent_file refuses it."""
    handle = open_subagent_file(transcript, path)
    if handle is None:
        return None
    with handle:
        return entries_of(handle, offset, sidechain=True)


def subagents(transcript: Path) -> Dict[str, Tuple[Path, Dict[str, Any]]]:
    """Maps the tool_use id that launched each subagent to its transcript and metadata."""
    found: Dict[str, Tuple[Path, Dict[str, Any]]] = {}
    folder = session_folder(transcript) / "subagents"
    if not folder.is_dir():
        return found
    for meta_path in folder.glob("agent-*.meta.json"):
        try:
            meta = storage.loads(meta_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        tool_use_id = meta.get("toolUseId") if isinstance(meta, dict) else None
        agent_path = meta_path.with_name(meta_path.name[: -len(".meta.json")] + ".jsonl")
        # A link pointing out of the folder could make the plugin send some other file
        if not own_subagent_file(transcript, agent_path):
            continue
        if isinstance(tool_use_id, str) and agent_path.is_file():
            found[tool_use_id] = (agent_path, meta)
    return found
