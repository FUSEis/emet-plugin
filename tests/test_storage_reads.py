"""What storage.read_json gives back for a file it cannot read."""

from __future__ import annotations

from pathlib import Path

import pytest

from emet_claude import storage

DEFAULT = {"read": "default"}
UNREADABLE = [
    pytest.param(None, id="no_file"),
    pytest.param(b"not json {", id="not_json"),
    pytest.param(b"\xff\xfe", id="not_utf8"),
    pytest.param(
        b"[" * 100_000,
        id="nested_too_deep",
        marks=pytest.mark.xfail(
            strict=True,
            raises=RecursionError,
            reason="storage.read_json catches OSError and ValueError only, so a file nested too deep raises RecursionError "
            "where every other reader of the plugin's files passes such a line over. One damaged status.json, state or "
            "stop file then ends every run that reads it. Fix: add RecursionError to the except in read_json",
        ),
    ),
]


@pytest.mark.parametrize("content", UNREADABLE)
def test_a_file_that_cannot_be_read_gives_the_default(tmp_path: Path, content: object) -> None:
    path = tmp_path / "saved.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    found = storage.read_json(path, DEFAULT)
    assert found is DEFAULT, "a damaged file of the plugin's own is read as if it were absent: a hook must never fail over what is on disk"
