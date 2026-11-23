"""What storage.read_json gives back for a file it cannot read."""

from __future__ import annotations

import os
import threading
from pathlib import Path
from typing import Any, List

import pytest

from emet_claude import storage
from helpers import links, pipes

DEFAULT = {"read": "default"}
UNREADABLE = [
    pytest.param(None, id="no_file"),
    pytest.param(b"not json {", id="not_json"),
    pytest.param(b"\xff\xfe", id="not_utf8"),
    pytest.param(b"[" * 100_000, id="nested_too_deep"),
]


@pytest.mark.parametrize("content", UNREADABLE)
def test_a_file_that_cannot_be_read_gives_the_default(tmp_path: Path, content: object) -> None:
    path = tmp_path / "saved.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    found = storage.read_json(path, DEFAULT)
    assert found is DEFAULT, "a damaged file of the plugin's own is read as if it were absent: a hook must never fail over what is on disk"


@pipes
@pytest.mark.parametrize("writer", [False, True])
def test_a_pipe_in_the_files_place_gives_the_default_without_waiting(tmp_path: Path, writer: bool) -> None:
    path = tmp_path / "saved.json"
    os.mkfifo(path)
    # A pipe takes a writer only while it has a reader. With one that writes nothing, a read that does not wait has no answer
    ends = [os.open(path, os.O_RDONLY | os.O_NONBLOCK), os.open(path, os.O_WRONLY | os.O_NONBLOCK)] if writer else []
    found: List[Any] = []
    # On another thread, so an open that waits for a writer fails the test and does not hang it
    waiter = threading.Thread(target=lambda: found.append(storage.read_json(path, DEFAULT)), daemon=True)
    waiter.start()
    waiter.join(5)
    for end in ends:
        os.close(end)
    assert found == [DEFAULT], (
        "a pipe is read as an absent file: the read neither waits for a writer nor fails over one that writes nothing"
    )


def test_a_folder_in_the_files_place_gives_the_default_and_leaves_no_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    path = tmp_path / "saved.json"
    path.mkdir()
    opened = []
    real = os.open

    def recorded(*args: Any, **kwargs: Any) -> int:
        opened.append(real(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(os, "open", recorded)
    assert storage.read_json(path, DEFAULT) is DEFAULT
    monkeypatch.undo()
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)


@links
def test_a_link_in_the_files_place_is_left_where_it_is_by_a_read(tmp_path: Path, link_flag: int) -> None:
    outside = tmp_path / "outside.json"
    outside.write_text('{"from": "outside"}', encoding="utf-8")
    path = tmp_path / "saved.json"
    path.symlink_to(outside)
    found = storage.read_json(path, DEFAULT)
    # Windows has no flag that refuses a link at the open, so a read follows one there
    assert found is DEFAULT if link_flag else found == {"from": "outside"}
    assert path.is_symlink() and outside.read_text(encoding="utf-8") == '{"from": "outside"}', "a reader never removes a link"
