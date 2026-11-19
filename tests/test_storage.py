import json
import os

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
    assert line.split(" ", 1)[1] == "retry " + "a" * (storage.LOG_LINE_MAX - len("retry "))


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


links = pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")


def temp_of(path):
    return path.with_name(f".{path.name}.{os.getpid()}.tmp")


@links
@pytest.mark.parametrize("exists", [True, False])
def test_a_link_planted_at_the_temporary_name_is_not_written_through(tmp_path, exists):
    outside = tmp_path / "outside.txt"
    if exists:
        outside.write_text("untouched", encoding="utf-8")
    target = tmp_path / "data" / "status.json"
    target.parent.mkdir()
    temp_of(target).symlink_to(outside)
    storage.write_json(target, {"queued": 0})
    assert json.loads(target.read_text(encoding="utf-8")) == {"queued": 0}
    assert not target.is_symlink() and not temp_of(target).is_symlink() and not temp_of(target).exists()
    assert outside.read_text(encoding="utf-8") == "untouched" if exists else not outside.exists()


def test_a_temporary_file_left_by_a_crashed_run_does_not_stop_the_write(tmp_path):
    target = tmp_path / "status.json"
    temp_of(target).write_text("left behind " * 100, encoding="utf-8")
    storage.write_private(target, "new")
    assert target.read_text(encoding="utf-8") == "new"
    assert not temp_of(target).exists()


@links
@pytest.mark.parametrize("exists", [True, False])
@pytest.mark.parametrize("has_flag", [True, False])
def test_a_link_planted_at_the_log_is_not_written_through(tmp_path, monkeypatch, exists, has_flag):
    if not has_flag:
        # As on Windows, where no flag refuses a link
        monkeypatch.setattr(storage, "NOFOLLOW", 0)
    outside = tmp_path / "outside.txt"
    if exists:
        outside.write_text("untouched", encoding="utf-8")
    root = tmp_path / "data"
    root.mkdir()
    (root / "emet.log").symlink_to(outside)
    storage.log(root, "key check -> accepted")
    assert not (root / "emet.log").is_symlink()
    assert log_lines(root)[0].endswith(" key check -> accepted")
    assert outside.read_text(encoding="utf-8") == "untouched" if exists else not outside.exists()


@links
def test_a_link_put_at_the_log_after_the_check_is_refused_by_the_open(tmp_path, monkeypatch):
    outside = tmp_path / "outside.txt"
    outside.write_text("untouched", encoding="utf-8")
    root = tmp_path / "data"
    root.mkdir()
    (root / "emet.log").symlink_to(outside)
    # The check sees no link, as when one is planted between the check and the open
    monkeypatch.setattr(type(root), "is_symlink", lambda self: False)
    storage.log(root, "key check -> accepted")
    assert outside.read_text(encoding="utf-8") == "untouched"


@links
@pytest.mark.parametrize("name", ["status.json", "pause.json", "state/session-1.json", "queue/00000000000000000001-aaaa.json"])
def test_a_link_planted_at_a_private_file_is_replaced_and_its_target_left_alone(environ, tmp_path, name):
    root = storage.data_dir(environ)
    outside = tmp_path / "outside.json"
    outside.write_text('{"mine": true}', encoding="utf-8")
    (root / name).symlink_to(outside)
    storage.write_json(root / name, {"offset": 7})
    assert not (root / name).is_symlink()
    assert json.loads((root / name).read_text(encoding="utf-8")) == {"offset": 7}
    assert json.loads(outside.read_text(encoding="utf-8")) == {"mine": True}


@links
def test_a_run_writes_through_no_link_planted_in_the_data_folder(transcript, environ, emet, tmp_path):
    root = storage.data_dir(environ)
    outside = {}
    for name in ("emet.log", "status.json", "state/session-1.json", f".status.json.{os.getpid()}.tmp"):
        outside[name] = tmp_path / f"outside-{len(outside)}"
        outside[name].write_text("{}", encoding="utf-8")
        (root / name).symlink_to(outside[name])
    transcript.prompt("What is 2 + 2?")
    transcript.text("msg_1", "4")
    commands.process(transcript.hook_input(), environ, wait=0)
    assert len(emet.requests) == 1
    for name, target in outside.items():
        assert target.read_text(encoding="utf-8") == "{}", name
        assert not (root / name).is_symlink(), name
    assert json.loads((root / "status.json").read_text(encoding="utf-8"))["last_delivery"]["status"] == 202


def test_private_files_and_the_log_are_written_where_no_flag_refuses_a_link(tmp_path, monkeypatch):
    # Windows has no O_NOFOLLOW
    monkeypatch.setattr(storage, "NOFOLLOW", 0)
    storage.write_json(tmp_path / "status.json", {"queued": 1})
    storage.write_json(tmp_path / "status.json", {"queued": 2})
    storage.log(tmp_path, "key check -> accepted")
    storage.log(tmp_path, "paused 30s")
    assert json.loads((tmp_path / "status.json").read_text(encoding="utf-8")) == {"queued": 2}
    assert len(log_lines(tmp_path)) == 2
    assert [path.name for path in tmp_path.iterdir() if path.name.endswith(".tmp")] == []


@pytest.mark.skipif(not hasattr(os, "O_NOFOLLOW"), reason="the platform has no O_NOFOLLOW")
def test_every_open_for_writing_refuses_a_link_and_the_temporary_file_is_always_new(tmp_path, monkeypatch):
    opened = []
    real_open = os.open

    def spy(path, flags, mode=0o777):
        opened.append((os.path.basename(str(path)), flags))
        return real_open(path, flags, mode)

    monkeypatch.setattr(storage.os, "open", spy)
    storage.write_json(tmp_path / "status.json", {})
    storage.log(tmp_path, "key check -> accepted")
    assert [name for name, _ in opened] == [f".status.json.{os.getpid()}.tmp", "emet.log"]
    assert all(flags & os.O_NOFOLLOW for _, flags in opened)
    assert opened[0][1] & os.O_EXCL and not opened[0][1] & os.O_TRUNC


@links
@pytest.mark.parametrize("name", ["state", "queue"])
def test_a_link_in_place_of_a_folder_the_plugin_makes_is_removed_and_nothing_is_written_through_it(environ, tmp_path, name):
    root = storage.data_dir(environ)
    (root / name).rmdir()
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "kept.json").write_text("{}", encoding="utf-8")
    (root / name).symlink_to(elsewhere, target_is_directory=True)
    assert storage.data_dir(environ) == root
    assert (root / name).is_dir() and not (root / name).is_symlink()
    storage.write_json(root / name / "session-1.json", {"offset": 7})
    assert [path.name for path in elsewhere.iterdir()] == ["kept.json"]


@links
def test_a_data_folder_that_is_a_link_by_choice_is_still_used(tmp_path):
    real = tmp_path / "real"
    real.mkdir()
    (tmp_path / "data").symlink_to(real, target_is_directory=True)
    root = storage.data_dir({"CLAUDE_PLUGIN_DATA": str(tmp_path / "data")})
    assert (tmp_path / "data").is_symlink() and (real / "state").is_dir() and root == tmp_path / "data"


@links
def test_a_private_file_that_is_a_link_is_not_read(tmp_path):
    outside = tmp_path / "outside.json"
    outside.write_text('{"agents": {"x": {"path": "/etc/hosts"}}}', encoding="utf-8")
    (tmp_path / "session-1.json").symlink_to(outside)
    assert storage.read_json(tmp_path / "session-1.json", {}) == {}
    assert storage.read_json(outside, {}) == {"agents": {"x": {"path": "/etc/hosts"}}}


def test_a_private_file_is_read_as_written_where_no_flag_refuses_a_link(tmp_path, monkeypatch):
    monkeypatch.setattr(storage, "READ_FLAGS", os.O_RDONLY | getattr(os, "O_BINARY", 0))
    storage.write_json(tmp_path / "status.json", {"note": "r\u00e9sum\u00e9 \r\n \x1a end"})
    assert storage.read_json(tmp_path / "status.json", None) == {"note": "r\u00e9sum\u00e9 \r\n \x1a end"}
    assert storage.read_json(tmp_path / "missing.json", "none") == "none"


@links
def test_a_link_in_the_queue_is_removed_and_what_it_points_to_is_never_sent(environ, emet, tmp_path):
    root = storage.data_dir(environ)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"conversation_id": "session-0", "trace_id": "turn-x", "spans": [], "end": True}), encoding="utf-8")
    link = root / "queue" / "00000000000000000001-aaaa.json"
    link.symlink_to(outside)
    transport.enqueue(root, {"conversation_id": "session-0", "trace_id": "turn-b", "spans": [], "end": True})
    assert transport.Sender(root, commands.load(environ)).flush() == 0
    assert [item["trace_id"] for item in emet.envelopes] == ["turn-b"]
    assert outside.exists() and not link.is_symlink() and transport.queued(root) == []
    assert log_lines(root)[0].endswith(" queued batch dropped, it is a link")


@links
def test_a_link_put_in_the_queue_after_the_check_is_still_not_sent(environ, emet, tmp_path, monkeypatch):
    root = storage.data_dir(environ)
    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps({"conversation_id": "session-0", "trace_id": "turn-x", "spans": [], "end": True}), encoding="utf-8")
    (root / "queue" / "00000000000000000001-aaaa.json").symlink_to(outside)
    # The check sees no link, as when one is planted between the check and the read
    monkeypatch.setattr(type(root), "is_symlink", lambda self: False)
    assert transport.Sender(root, commands.load(environ)).flush() == 0
    assert emet.requests == [] and outside.exists()
