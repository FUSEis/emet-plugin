import json
import sys
import threading

import pytest

from emet_claude import commands, envelope, pending, storage, transport
from helpers import Transcript

TRACE = "0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11"


def run(transcript, environ, turn_ended=True):
    commands.process(transcript.hook_input(), environ, wait=0, turn_ended=turn_ended)


def finished_turn(transcript, text="What is 2 + 2?", answer="4"):
    prompt = transcript.prompt(text)
    transcript.text(f"msg_{prompt['uuid'][:8]}", answer)
    return prompt


def root(environ):
    return storage.data_dir(environ)


def kept(environ, session_id="session-1"):
    return pending.traces(root(environ), session_id)


def stamps(environ, session_id="session-1"):
    return [item for item in pending.lines(root(environ), session_id) if "finished" in item]


def status(environ):
    return json.loads((root(environ) / "status.json").read_text(encoding="utf-8"))


def test_keeps_emets_id_of_each_finished_turn_in_order(transcript, environ, emet):
    first = finished_turn(transcript)
    run(transcript, environ)
    second = finished_turn(transcript, "And 3 + 3?", "6")
    run(transcript, environ)
    run(transcript, environ)
    assert kept(environ) == [emet.trace_uuids[first["uuid"]], emet.trace_uuids[second["uuid"]]]


def test_a_trace_that_gets_more_spans_later_is_written_once(transcript, environ, emet):
    prompt = finished_turn(transcript)
    run(transcript, environ)
    transcript.text("msg_late", "one more thing")
    run(transcript, environ)
    assert len(emet.envelopes) == 2
    written = [item["trace_uuid"] for item in pending.lines(root(environ), "session-1") if "trace_uuid" in item]
    assert written == [emet.trace_uuids[prompt["uuid"]]]


def test_a_reply_that_names_no_trace_keeps_nothing_and_still_counts_as_delivered(transcript, environ, emet):
    emet.names_trace = False
    finished_turn(transcript)
    run(transcript, environ)
    run(transcript, environ)
    assert len(emet.envelopes) == 1
    assert status(environ)["queued"] == 0
    assert kept(environ) == []


def test_a_turn_emet_did_not_accept_is_not_kept(transcript, environ, emet):
    emet.statuses = [400]
    finished_turn(transcript)
    run(transcript, environ)
    assert len(emet.envelopes) == 1
    assert not emet.conversations
    assert kept(environ) == []


def test_a_turn_sent_in_batches_is_kept_once_its_last_batch_is_accepted(transcript, environ, emet, monkeypatch):
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 1)
    emet.statuses = [202, 400]
    finished_turn(transcript)
    run(transcript, environ)
    assert [item["end"] for item in emet.envelopes] == [False, True]
    assert kept(environ) == []
    prompt = finished_turn(transcript, "And 3 + 3?", "6")
    run(transcript, environ)
    assert kept(environ) == [emet.trace_uuids[prompt["uuid"]]]


def test_a_queued_turn_is_kept_when_its_session_retries_it(transcript, environ, emet, clock):
    emet.statuses = [503]
    prompt = finished_turn(transcript)
    run(transcript, environ)
    assert kept(environ) == []
    clock.advance(60)
    run(transcript, environ)
    assert kept(environ) == [emet.trace_uuids[prompt["uuid"]]]


def test_a_queued_turn_another_session_delivers_is_kept_for_its_own_session(tmp_path, environ, emet, clock):
    queued, current = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    emet.statuses = [503]
    late = finished_turn(queued)
    run(queued, environ)
    clock.advance(60)
    # This session's worker flushes the shared queue first, then sends its own turn
    prompt = finished_turn(current)
    run(current, environ)
    assert len(emet.envelopes) == 3
    assert kept(environ, "session-1") == [emet.trace_uuids[prompt["uuid"]]]
    assert kept(environ, "session-0") == [emet.trace_uuids[late["uuid"]]]


def test_a_retried_turn_is_kept_even_when_the_rest_of_the_run_fails(transcript, environ, emet, monkeypatch, clock):
    emet.statuses = [503]
    prompt = finished_turn(transcript)
    run(transcript, environ)
    clock.advance(60)

    def broken(*args, **kwargs):
        raise RuntimeError("unreadable transcript")

    monkeypatch.setattr(commands.transcript, "read_entries", broken)
    with pytest.raises(RuntimeError):
        run(transcript, environ)
    assert kept(environ) == [emet.trace_uuids[prompt["uuid"]]]
    assert len(stamps(environ)) == 2


def test_a_run_after_a_finished_turn_stamps_its_end(transcript, environ, emet):
    finished_turn(transcript)
    run(transcript, environ, turn_ended=False)
    assert stamps(environ) == []
    run(transcript, environ)
    lines = pending.lines(root(environ), "session-1")
    assert [sorted(item) for item in lines] == [["at", "endpoint", "trace_uuid"], ["finished", "queued"]]
    assert lines[0]["endpoint"] == emet.url
    assert lines[1]["queued"] == 0
    assert all(value for value in (lines[0]["at"], lines[1]["finished"]))


def test_the_stamp_counts_what_is_still_queued(transcript, environ, emet, clock):
    emet.statuses = [503]
    prompt = finished_turn(transcript)
    run(transcript, environ)
    assert [item["queued"] for item in stamps(environ)] == [1]
    clock.advance(60)
    run(transcript, environ)
    assert [item["queued"] for item in stamps(environ)] == [1, 0]
    assert kept(environ) == [emet.trace_uuids[prompt["uuid"]]]


def test_a_run_that_cannot_start_stamps_nothing(transcript, environ, emet):
    finished_turn(transcript)
    hook_input = dict(transcript.hook_input(), transcript_path=str(transcript.path) + ".missing")
    commands.process(hook_input, environ, wait=0, turn_ended=True)
    assert pending.lines(root(environ), "session-1") == []


def test_the_list_is_not_in_the_session_state(transcript, environ, emet):
    finished_turn(transcript)
    run(transcript, environ)
    state = json.loads((root(environ) / "state" / "session-1.json").read_text(encoding="utf-8"))
    assert "pending" not in state


@pytest.mark.parametrize(("end", "ended"), [(True, True), (False, False), (None, False), ("true", False)])
def test_only_a_batch_that_ends_its_turn_is_kept(environ, end, ended):
    sender = transport.Sender(root(environ), None)
    sender.record({"conversation_id": "session-1", "end": end}, transport.Outcome(202, True, True, "", "", TRACE))
    assert kept(environ) == ([TRACE] if ended else [])


def test_readable_lines_survive_damaged_ones(environ):
    path = pending.path(root(environ), "session-1")
    good = json.dumps({"trace_uuid": TRACE, "at": "t"})
    damaged = [
        "1",
        '"x"',
        "[]",
        '{"at": "t"}',
        '{"trace_uuid": ["x"]}',
        '{"trace_uuid": ""}',
        '{"trace_uuid": "../../keys"}',
        "[" * 100_000,
    ]
    path.write_text(
        "\n".join(
            [good, *damaged, '{"trace_uuid": "0f0e6c6e'],
        )
        + "\n",
        encoding="utf-8",
    )
    newer = "00000000-0000-4000-8000-000000000001"
    pending.note(root(environ), "session-1", newer, "")
    assert kept(environ) == [TRACE, newer]
    assert all(isinstance(item, dict) for item in pending.lines(root(environ), "session-1"))


def test_a_line_cut_short_without_a_newline_does_not_swallow_the_next_one(environ):
    path = pending.path(root(environ), "session-1")
    path.write_bytes(b'{"trace_uuid": "0f0e6c6e')
    pending.note(root(environ), "session-1", TRACE, "")
    assert kept(environ) == [TRACE]
    assert path.read_bytes().count(b"\n") == 2


def test_a_session_id_that_is_not_a_plain_name_reads_nothing(environ):
    line = json.dumps({"trace_uuid": TRACE, "at": "t"}) + "\n"
    (root(environ) / "outside.pending.jsonl").write_text(line, encoding="utf-8")
    assert pending.traces(root(environ), "../outside") == []


def test_a_file_that_is_not_text_reads_as_empty(environ):
    pending.path(root(environ), "session-1").write_bytes(b"\xff\xfe\n")
    assert pending.lines(root(environ), "session-1") == []


def test_a_trace_written_twice_is_read_once(environ):
    line = json.dumps({"trace_uuid": TRACE, "at": "t"}) + "\n"
    pending.path(root(environ), "session-1").write_text(line * 2, encoding="utf-8")
    assert kept(environ) == [TRACE]


def test_two_writers_keep_a_trace_once(environ):
    workers = [threading.Thread(target=pending.note, args=(root(environ), "session-1", TRACE, "")) for _ in range(8)]
    for worker in workers:
        worker.start()
    for worker in workers:
        worker.join()
    assert len(pending.lines(root(environ), "session-1")) == 1


def test_a_write_that_fails_is_logged_without_the_id(tmp_path, environ):
    pending.note(root(environ), "../session-1", TRACE, "")
    (root(environ) / "state" / "session-1.pending.jsonl").mkdir()
    pending.note(root(environ), "session-1", TRACE, "")
    log = (root(environ) / "emet.log").read_text(encoding="utf-8")
    assert log.count("pending not written") == 2
    assert TRACE not in log


@pytest.mark.parametrize("session_id", ["", "..", "../session-1", "a/b", "a\\b", "x" * 101])
def test_a_session_id_that_is_not_a_plain_name_writes_nothing(environ, session_id):
    before = sorted(root(environ).parent.rglob("*"))
    pending.note(root(environ), session_id, TRACE, "")
    assert pending.lines(root(environ), session_id) == []
    # The skipped write is logged, and nothing else appears
    assert sorted(path for path in root(environ).parent.rglob("*") if path.name != "emet.log") == before


def test_a_folder_that_cannot_be_written_never_stops_a_send(tmp_path):
    sender = transport.Sender(tmp_path / "missing", None)
    sender.record({"conversation_id": "session-1", "end": True}, transport.Outcome(202, True, True, "", "", TRACE))
    assert len(sender.accepted) == 1


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_the_file_is_private(environ):
    pending.note(root(environ), "session-1", TRACE, "")
    assert pending.path(root(environ), "session-1").stat().st_mode & 0o077 == 0


@pytest.mark.parametrize("body", [b"", b"[]", b"null", b'"x"', b'{"trace": null}', b'{"trace": {}}', b"\xff\xfe", b'{"trace": "nope"}'])
def test_a_reply_without_a_valid_trace_gives_no_id(body):
    assert transport.accepted_trace(body) == ""


def test_the_trace_id_is_normalized():
    raw = "0F0E6C6E4A8E4D479B4C3C9F2F1D8A11"
    assert transport.accepted_trace(json.dumps({"trace": raw}).encode()) == TRACE


def test_a_deeply_nested_reply_gives_no_trace():
    assert transport.accepted_trace(b"[" * 100_000) == ""
