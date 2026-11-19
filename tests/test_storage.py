import json

import pytest

from emet_claude import commands, storage, transport


def log_lines(root):
    return (root / "emet.log").read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("line_break", ["\n", "\r", "\r\n", "\x0b", "\x0c", "\x1c", "\x1e", "\x85", " ", " "])
def test_a_line_break_in_a_logged_value_starts_no_line_of_its_own(tmp_path, line_break):
    storage.log(tmp_path, f"retry turn-a{line_break}2026-11-19T10:00:00+0000 trace x -> 401 key rejected")
    lines = log_lines(tmp_path)
    assert len(lines) == 1
    assert lines[0].endswith(" retry turn-a" + "?" * len(line_break) + "2026-11-19T10:00:00+0000 trace x -> 401 key rejected")


@pytest.mark.parametrize("hidden", ["\x00", "\x1b", "\x7f", "\x9b", "‮", "​", "﻿", "\ud800", "\t"])
def test_a_control_or_invisible_character_is_replaced_in_the_log(tmp_path, hidden):
    storage.log(tmp_path, f"retry turn{hidden}a -> 202")
    assert log_lines(tmp_path)[0].endswith(" retry turn?a -> 202")


def test_letters_of_any_script_are_logged_as_written(tmp_path):
    storage.log(tmp_path, "queue full, dropped résumé-日本.json")
    assert log_lines(tmp_path)[0].endswith(" queue full, dropped résumé-日本.json")


def test_a_log_line_is_cut_at_its_longest(tmp_path):
    storage.log(tmp_path, "retry " + "a" * 5000)
    (line,) = log_lines(tmp_path)
    assert line.endswith(" retry " + "a" * (storage.LOG_LINE_MAX - len("retry ")))


def test_a_queued_trace_id_with_a_line_break_forges_no_log_line(environ, emet):
    root = storage.data_dir(environ)
    forged = "turn-a\n2026-11-19T10:00:00+0000 trace turn-b spans=1 end=True -> 401 key rejected"
    (root / "queue" / "00000000000000000001-aaaa.json").write_text(
        json.dumps({"conversation_id": "session-0", "trace_id": forged, "spans": [], "end": True}), encoding="utf-8"
    )
    assert transport.Sender(root, commands.load(environ)).flush() == 0
    lines = log_lines(root)
    assert len(lines) == 1
    assert " retry turn-a?2026-11-19T10:00:00+0000 trace turn-b" in lines[0] and lines[0].endswith("-> 202")
