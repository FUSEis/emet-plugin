import json
from pathlib import Path

import pytest

from emet_claude import commands, transport
from helpers import Transcript


def run(transcript, environ):
    commands.process(transcript.hook_input(), environ, wait=0)


def finished_turn(transcript, text="What is 2 + 2?", answer="4"):
    prompt = transcript.prompt(text)
    transcript.text(f"msg_{prompt['uuid'][:8]}", answer)
    return prompt


def pending(environ, session_id="session-1"):
    state = json.loads((Path(environ["CLAUDE_PLUGIN_DATA"]) / "state" / f"{session_id}.json").read_text(encoding="utf-8"))
    return [item["trace_uuid"] for item in state["pending"]]


def test_keeps_emets_id_of_each_finished_turn_in_order(transcript, environ, emet):
    first = finished_turn(transcript)
    run(transcript, environ)
    second = finished_turn(transcript, "And 3 + 3?", "6")
    run(transcript, environ)
    run(transcript, environ)
    assert pending(environ) == [emet.trace_uuids[first["uuid"]], emet.trace_uuids[second["uuid"]]]


def test_a_trace_that_gets_more_spans_later_is_kept_once(transcript, environ, emet):
    prompt = finished_turn(transcript)
    run(transcript, environ)
    transcript.text("msg_late", "one more thing")
    run(transcript, environ)
    assert len(emet.envelopes) == 2
    assert pending(environ) == [emet.trace_uuids[prompt["uuid"]]]


def test_a_reply_that_names_no_trace_keeps_nothing(transcript, environ, emet):
    emet.names_trace = False
    finished_turn(transcript)
    run(transcript, environ)
    assert len(emet.envelopes) == 1
    assert pending(environ) == []


def test_a_turn_emet_did_not_accept_is_not_kept(transcript, environ, emet):
    emet.statuses = [400]
    finished_turn(transcript)
    run(transcript, environ)
    assert len(emet.envelopes) == 1
    assert not emet.conversations
    assert pending(environ) == []


def test_a_queued_turn_is_kept_when_its_session_retries_it(transcript, environ, emet):
    emet.statuses = [503]
    prompt = finished_turn(transcript)
    run(transcript, environ)
    assert pending(environ) == []
    run(transcript, environ)
    assert pending(environ) == [emet.trace_uuids[prompt["uuid"]]]


def test_another_sessions_retried_turn_is_not_kept_here(tmp_path, environ, emet):
    queued, current = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    emet.statuses = [503]
    finished_turn(queued)
    run(queued, environ)
    # This session's worker flushes the shared queue first, then sends its own turn
    prompt = finished_turn(current)
    run(current, environ)
    assert len(emet.envelopes) == 3
    assert pending(environ, "session-1") == [emet.trace_uuids[prompt["uuid"]]]
    assert pending(environ, "session-0") == []


def accepted(session_id="session-1", trace_uuid="0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11", ended=True):
    return transport.Accepted(session_id, "", transport.Outcome(202, True, True, "", "", trace_uuid), ended)


@pytest.mark.parametrize(("end", "ended"), [(True, True), (False, False), (None, False), ("true", False)])
def test_only_the_last_batch_of_a_turn_counts_as_ended(tmp_path, end, ended):
    sender = transport.Sender(tmp_path, None)
    envelope = {"conversation_id": "session-1", "end": end}
    sender.record(envelope, transport.Outcome(202, True, True, "", "", "0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11"))
    assert sender.accepted[-1].ended is ended


def test_a_batch_that_does_not_end_its_turn_is_not_kept():
    state = {}
    commands.note_pending(state, "session-1", [accepted(ended=False)])
    assert state["pending"] == []


def test_the_list_keeps_the_newest_turns():
    state = {}
    many = [accepted(trace_uuid=f"00000000-0000-4000-8000-{index:012d}") for index in range(commands.MAX_PENDING + 3)]
    commands.note_pending(state, "session-1", many)
    kept = [item["trace_uuid"] for item in state["pending"]]
    assert len(kept) == commands.MAX_PENDING
    assert kept[-1] == many[-1].outcome.trace_uuid
    assert many[0].outcome.trace_uuid not in kept


@pytest.mark.parametrize("damaged", [None, "x", {"a": 1}, [1, "x", {"at": "now"}], [{"trace_uuid": ["x"]}], [{"trace_uuid": ""}]])
def test_a_damaged_list_is_replaced(damaged):
    state = {"pending": damaged}
    commands.note_pending(state, "session-1", [accepted()])
    assert [item["trace_uuid"] for item in state["pending"]] == ["0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11"]


@pytest.mark.parametrize("body", [b"", b"[]", b"null", b'"x"', b'{"trace": null}', b'{"trace": {}}', b"\xff\xfe", b'{"trace": "nope"}'])
def test_a_reply_without_a_valid_trace_gives_no_id(body):
    assert transport.accepted_trace(body) == ""


def test_the_trace_id_is_normalized():
    raw = "0F0E6C6E4A8E4D479B4C3C9F2F1D8A11"
    assert transport.accepted_trace(json.dumps({"trace": raw}).encode()) == "0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11"


def test_a_deeply_nested_reply_gives_no_trace():
    assert transport.accepted_trace(b"[" * 100_000) == ""
