import json
import sys
import threading
from datetime import datetime, timezone

import pytest

from emet_claude import commands, envelope, pending, report, storage, timestamps, transport, watch
from helpers import WAKE_WRAPPER, Transcript
from test_report import document

TRACE = "0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11"


def run(transcript, environ, turn_ended=True):
    commands.process(transcript.hook_input(), environ, wait=0, turn_ended=turn_ended)


def finished_turn(transcript, text="What is 2 + 2?", answer="4"):
    prompt = transcript.prompt(text)
    transcript.text(f"msg_{prompt['uuid'][:8]}", answer)
    return prompt


def woken_turn(transcript, environ, clock, handed=True, answer="Emet disputes one claim."):
    """The turn Claude takes after this plugin woke it with a report. With handed, the poller recorded that wake."""
    if handed:
        assert watch.record(root(environ), transcript.session_id, TRACE, "flagged", {"handed": True})
    started = timestamps.text(datetime.fromtimestamp(clock.now + 14, timezone.utc))
    text = WAKE_WRAPPER + report.wake_message(document(), "https://app.emet.so", "session-1")
    prompt = transcript.notice(text, timestamp=started)
    transcript.text(f"msg_{prompt['uuid'][:8]}", answer)
    return prompt


def marks(environ, session_id="session-1"):
    return [(item["trace_uuid"], item.get("started_by")) for item in pending.lines(root(environ), session_id) if "trace_uuid" in item]


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
    assert [sorted(item) for item in lines] == [["at", "conversation_uuid", "ended", "endpoint", "trace_uuid"], ["finished", "queued"]]
    assert lines[0]["conversation_uuid"] == emet.conversations[0]
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


def test_a_refused_turn_is_marked_and_never_read_as_a_report_to_fetch(environ):
    pending.note(root(environ), "session-1", TRACE, "")
    # Even a refused id that has the form of Emet's own
    pending.refuse(root(environ), "session-1", "00000000-0000-4000-8000-000000000002", 400, "https://app.emet.so")
    pending.refuse(root(environ), "session-1", "turn-at-12", 413, "https://app.emet.so")
    assert kept(environ) == [TRACE]
    assert pending.refused(root(environ), "session-1") == {"00000000-0000-4000-8000-000000000002", "turn-at-12"}
    marks = [item for item in pending.lines(root(environ), "session-1") if "refused" in item]
    assert [(item["refused"], item["status"], "trace_uuid" in item, bool(item["at"])) for item in marks] == [
        ("00000000-0000-4000-8000-000000000002", 400, False, True),
        ("turn-at-12", 413, False, True),
    ]


@pytest.mark.parametrize("line", ['{"refused": ["x"]}', '{"refused": ""}', '{"refused": null}', '{"refused": 5}', '{"trace_uuid": "x"}'])
def test_a_damaged_refused_line_marks_nothing(environ, line):
    pending.path(root(environ), "session-1").write_text(line + "\n", encoding="utf-8")
    assert pending.refused(root(environ), "session-1") == set()
    assert pending.refused(root(environ), "../session-1") == set()


def test_a_refusal_that_cannot_be_written_still_holds_for_the_run(tmp_path):
    sender = transport.Sender(tmp_path / "missing", None)
    refused = {"conversation_id": "session-1", "trace_id": "turn-a", "spans": [], "end": True}
    sender.record(refused, transport.Outcome(400, False, True, ""))
    assert sender.refused("session-1") == {"turn-a"}
    assert sender.is_refused(refused)


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


def test_a_trace_line_names_the_conversation_only_when_emet_named_one(environ):
    other = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"
    pending.note(root(environ), "session-1", TRACE, "", "7d3c1c52-0d0a-4a52-8d0e-0c7e3a1b2c3d")
    pending.note(root(environ), "session-1", other, "")
    first, second = pending.lines(root(environ), "session-1")
    assert first["conversation_uuid"] == "7d3c1c52-0d0a-4a52-8d0e-0c7e3a1b2c3d"
    assert "conversation_uuid" not in second


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


def test_the_line_of_the_turn_after_a_wake_carries_a_mark(transcript, environ, emet, clock):
    first = finished_turn(transcript)
    wake = woken_turn(transcript, environ, clock)
    notice = transcript.notice("<task-notification>Background command finished</task-notification>")
    transcript.text("msg_notice", "The build passed.")
    run(transcript, environ)
    assert len(emet.envelopes) == 3
    assert marks(environ) == [
        (emet.trace_uuids[first["uuid"]], None),
        (emet.trace_uuids[wake["uuid"]], "report_wake"),
        (emet.trace_uuids[notice["uuid"]], None),
    ]
    (line,) = [item for item in pending.lines(root(environ), "session-1") if "started_by" in item]
    assert set(line) == {"trace_uuid", "at", "endpoint", "conversation_uuid", "started_by", "prompt_id", "ended"}
    assert line["endpoint"] == emet.url


def test_the_mark_is_kept_when_another_session_delivers_the_turn_from_the_queue(tmp_path, environ, emet, clock):
    queued, current = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    emet.statuses = [503]
    wake = woken_turn(queued, environ, clock)
    run(queued, environ)
    assert marks(environ, "session-0") == []
    clock.advance(60)
    prompt = finished_turn(current)
    run(current, environ)
    assert marks(environ, "session-0") == [(emet.trace_uuids[wake["uuid"]], "report_wake")]
    assert marks(environ, "session-1") == [(emet.trace_uuids[prompt["uuid"]], None)]


def test_a_marked_turn_sent_in_batches_is_marked_by_its_first_batch(transcript, environ, emet, monkeypatch, clock):
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 1)
    # The last batch holds no root span, and here it is never accepted
    emet.statuses = [202, 503]
    wake = woken_turn(transcript, environ, clock)
    run(transcript, environ)
    assert [item["end"] for item in emet.envelopes] == [False, True]
    assert marks(environ) == [(emet.trace_uuids[wake["uuid"]], "report_wake")]


def test_a_marked_turn_is_written_once_however_many_batches_it_has(transcript, environ, emet, monkeypatch, clock):
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 1)
    wake = woken_turn(transcript, environ, clock)
    run(transcript, environ)
    transcript.text("msg_late", "one more thing")
    run(transcript, environ)
    assert len(emet.envelopes) == 3
    assert marks(environ) == [(emet.trace_uuids[wake["uuid"]], "report_wake")]


def test_a_notice_with_the_wake_text_is_not_marked_unless_the_poller_recorded_a_wake_before_it(transcript, environ, emet, clock):
    notice = woken_turn(transcript, environ, clock, handed=False)
    run(transcript, environ)
    assert marks(environ) == [(emet.trace_uuids[notice["uuid"]], None)]
    assert report.WAKE_OPENING in json.dumps(emet.envelopes[0])


@pytest.mark.parametrize(
    ("line", "counts"),
    [
        ({"handed": True}, True),
        ({"handed": True, "held": "wake_cap"}, True),
        ({"handed": False, "held": "newer_turn"}, False),
        ({}, False),
        ({"handed": "yes"}, False),
    ],
)
def test_only_a_flagged_report_handed_to_claude_counts_as_a_wake_for_the_sender(environ, clock, line, counts):
    assert watch.record(root(environ), "session-1", TRACE, "flagged", line)
    assert watch.record(root(environ), "session-1", TRACE, "wake_turn", {"handed": True})
    assert watch.record(root(environ), "session-2", TRACE, "flagged", {"handed": True})
    assert watch.handed_times(root(environ), "session-1") == ([clock.now] if counts else [])
    assert watch.handed_times(root(environ), "session-3") == []


def trace_lines(environ, session_id="session-1"):
    return [item for item in pending.lines(root(environ), session_id) if "trace_uuid" in item]


def test_a_trace_line_names_the_turn_by_claude_codes_id_and_the_time_of_its_last_entry(transcript, environ, emet):
    transcript.prompt("What is 2 + 2?", promptId="prompt-1", timestamp="2026-10-20T10:00:01.250Z")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "ls"}, timestamp="2026-10-20T10:00:02.000Z")
    transcript.tool_result("toolu_1", "a.txt", timestamp="2026-10-20T10:00:09.875Z")
    transcript.text("msg_2", "4", timestamp="2026-10-20T10:00:04+00:00")
    run(transcript, environ)
    (line,) = trace_lines(environ)
    # The latest time of the turn, as the root span ends, in the one form the plugin writes
    assert (line["prompt_id"], line["ended"]) == ("prompt-1", "2026-10-20T10:00:09.875Z")
    assert line["ended"] == emet.spans()[0]["ended_at"]


def test_the_turns_identity_is_kept_on_this_machine_and_never_sent(transcript, environ, emet, monkeypatch, clock):
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 1)
    emet.statuses = [503]
    transcript.prompt("What is 2 + 2?", promptId="prompt-kept-here")
    transcript.text("msg_1", "4")
    run(transcript, environ)
    queued = [storage.read_json(path, {}) for path in transport.queued(root(environ))]
    assert len(queued) > 1 and all(item[envelope.LOCAL]["prompt_id"] == "prompt-kept-here" for item in queued)
    clock.advance(60)
    run(transcript, environ)
    assert trace_lines(environ)[0]["prompt_id"] == "prompt-kept-here"
    assert len(emet.requests) > 2
    for request in emet.requests:
        assert envelope.LOCAL not in request["body"], "what the plugin keeps for its own files was sent to Emet"
        assert "prompt-kept-here" not in json.dumps(request["body"])
        assert set(request["body"]) <= {
            "tenant_ref",
            "conversation_id",
            "trace_id",
            "session_ref",
            "enduser_ref",
            "ingestion_mode",
            "semconv_version",
            "resource",
            "end",
            "conversation_title",
            "spans",
        }


def test_the_turns_identity_survives_the_queue_and_delivery_by_another_sessions_worker(tmp_path, environ, emet, monkeypatch, clock):
    # The batch that ends the turn holds no root span
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 1)
    queued, current = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    emet.statuses = [503]
    queued.prompt("What is 2 + 2?", promptId="prompt-0")
    last = queued.text("msg_1", "4")
    run(queued, environ)
    assert trace_lines(environ, "session-0") == []
    clock.advance(60)
    current.prompt("And 3 + 3?", promptId="prompt-1")
    current.text("msg_2", "6")
    run(current, environ)
    (line,) = trace_lines(environ, "session-0")
    assert (line["prompt_id"], line["ended"]) == ("prompt-0", last["timestamp"])
    assert trace_lines(environ, "session-1")[0]["prompt_id"] == "prompt-1"


@pytest.mark.parametrize("prompt_id", [None, "", 5, ["prompt-1"], "prompt 1", "prompt/../1", "p" * 101, "prompt-1\n"])
def test_a_turn_id_that_is_not_a_plain_name_is_left_out_of_the_line(transcript, environ, emet, prompt_id):
    transcript.prompt("What is 2 + 2?", **({} if prompt_id is None else {"promptId": prompt_id}))
    transcript.text("msg_1", "4")
    run(transcript, environ)
    (line,) = trace_lines(environ)
    assert "prompt_id" not in line and "ended" in line


@pytest.mark.parametrize(
    ("local", "kept"),
    [
        ({"prompt_id": "prompt-1", "ended": "2026-10-20T10:00:02+00:00"}, {"prompt_id": "prompt-1", "ended": "2026-10-20T10:00:02.000Z"}),
        ({"prompt_id": "a b", "ended": "soon"}, {}),
        ({"prompt_id": 5, "ended": 1_800_000_000}, {}),
        ({"ended": "2026-10-20T10:00:02"}, {}),
        ("prompt-1", {}),
        (None, {}),
    ],
)
def test_what_a_queued_envelope_says_of_its_turn_is_checked_again_before_it_is_written(environ, local, kept):
    sender = transport.Sender(root(environ), type("Settings", (), {"endpoint": "https://app.emet.so"})())
    sent = {"conversation_id": "session-1", "end": True, envelope.LOCAL: local}
    sender.record(sent, transport.Outcome(202, True, True, "", "", TRACE))
    (line,) = trace_lines(environ)
    assert {key: value for key, value in line.items() if key in ("prompt_id", "ended")} == kept


def test_a_turn_id_that_is_not_a_plain_name_never_reaches_the_queue(transcript, environ, emet):
    emet.statuses = [503]
    transcript.prompt("What is 2 + 2?", promptId="prompt 1")
    transcript.text("msg_1", "4")
    run(transcript, environ)
    (queued,) = [storage.read_json(path, {}) for path in transport.queued(root(environ))]
    assert list(queued[envelope.LOCAL]) == ["ended"]
