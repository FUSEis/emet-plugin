import json
import re
from datetime import datetime, timezone

import pytest

from emet_claude import commands, envelope, storage, timestamps

SENT = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.\d{3}Z")


def run(transcript, environ):
    commands.process(transcript.hook_input(), environ, wait=0)


def turn(transcript, text="What is 2 + 2?", answer="4", **prompt):
    entry = transcript.prompt(text, **prompt)
    transcript.text(f"msg_{transcript.clock}", answer)
    return entry


def status(environ):
    return json.loads((storage.data_dir(environ) / "status.json").read_text(encoding="utf-8"))


def log(environ):
    return (storage.data_dir(environ) / "emet.log").read_text(encoding="utf-8")


def times(span):
    return span["started_at"], span["ended_at"]


@pytest.mark.parametrize("digits", range(1, 10))
def test_reads_every_fraction_length(digits):
    fraction = "123456789"[:digits]
    at = timestamps.parse(f"2026-10-20T10:00:01.{fraction}Z")
    assert at == datetime(2026, 10, 20, 10, 0, 1, int(fraction.ljust(6, "0")[:6]), tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-20T10:00:01Z",
        "2026-10-20T10:00:01.000Z",
        "2026-10-20T10:00:01+00:00",
        "2026-10-20T12:00:01+02:00",
        "2026-10-20T07:00:01-03:00",
        "2026-10-20T15:30:01+0530",
        "2026-10-20T08:00:01-02",
        "2026-10-21T00:00:01.000+14:00",
    ],
)
def test_reads_z_and_numeric_offsets_as_one_instant(value):
    assert timestamps.parse(value) == datetime(2026, 10, 20, 10, 0, 1, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    "value",
    [
        "2026-10-20T10:00:01",
        "2026-10-20T10:00:01.000",
        "",
        None,
        1792490401,
        ["2026-10-20T10:00:01Z"],
        "yesterday",
        "2026-13-20T10:00:01Z",
        "2026-10-20T10:00:60Z",
        "2026-10-20T24:00:00Z",
        "2026-10-20 10:00:01Z",
        "2026-10-20T10:00:01z",
        "2026-10-20T10:00:01+24:00",
        "2026-10-20T10:00:01+02:60",
        "2026-10-20T10:00:01.Z",
        "2026-10-20T10:00:01.1234567890Z",
        "2026-10-20T10:00:01Z\n",
        " 2026-10-20T10:00:01Z",
        "２０２６-10-20T10:00:01Z",
        "0001-01-01T00:00:00+01:00",
    ],
)
def test_a_time_with_no_zone_or_a_bad_form_is_not_read(value):
    assert timestamps.parse(value) is None


def test_writes_what_claude_code_writes():
    assert timestamps.text(timestamps.parse("2026-10-20T10:00:01.000Z")) == "2026-10-20T10:00:01.000Z"
    assert timestamps.text(timestamps.parse("2026-10-20T12:00:01.9876+02:00")) == "2026-10-20T10:00:01.987Z"
    assert timestamps.text(datetime(987, 1, 2, 3, 4, 5, tzinfo=timezone.utc)) == "0987-01-02T03:04:05.000Z"


def test_reads_the_time_the_plugin_writes_in_its_own_files():
    assert timestamps.parse(storage.now()) is not None


def test_sends_times_unchanged_for_todays_transcripts(transcript, environ, emet):
    prompt = turn(transcript)
    run(transcript, environ)
    spans = emet.envelopes[0]["spans"]
    assert spans[0]["started_at"] == prompt["timestamp"]
    assert all(SENT.fullmatch(value) for span in spans for value in times(span))


def test_a_turn_with_mixed_offsets_is_ordered_by_instant(transcript, environ, emet):
    # As text the first time sorts after the second; as instants it is one second earlier
    transcript.prompt("What is 2 + 2?", timestamp="2026-10-20T12:00:01.000+02:00")
    transcript.text("msg_1", "4", timestamp="2026-10-20T10:00:02.000Z")
    run(transcript, environ)
    root, model = emet.envelopes[0]["spans"]
    assert times(root) == ("2026-10-20T10:00:01.000Z", "2026-10-20T10:00:02.000Z")
    assert times(model) == ("2026-10-20T10:00:01.000Z", "2026-10-20T10:00:02.000Z")


@pytest.mark.parametrize("bad", ["", None, "2026-10-20T10:00:02", "soon"])
def test_an_entry_with_no_readable_time_takes_the_time_before_it(transcript, environ, emet, bad):
    prompt = transcript.prompt("What is 2 + 2?")
    transcript.text("msg_1", "4", timestamp=bad)
    run(transcript, environ)
    root, model = emet.envelopes[0]["spans"]
    assert times(root) == times(model) == (prompt["timestamp"], prompt["timestamp"])


def test_a_prompt_with_no_readable_time_takes_the_turns_first_one(transcript, environ, emet):
    transcript.prompt("What is 2 + 2?", timestamp="")
    answer = transcript.text("msg_1", "4")
    run(transcript, environ)
    assert {times(span) for span in emet.envelopes[0]["spans"]} == {(answer["timestamp"], answer["timestamp"])}


def test_a_turn_with_no_times_is_skipped_and_the_session_goes_on(transcript, environ, emet):
    transcript.prompt("What is 2 + 2?", timestamp="")
    transcript.text("msg_1", "4", timestamp=None)
    after = turn(transcript, "And 3 + 3?", "6")
    run(transcript, environ)
    assert [item["trace_id"] for item in emet.envelopes] == [after["uuid"]]
    assert status(environ)["skipped_turns"] == 1
    assert status(environ)["last_skipped"]["reason"] == "Unreadable"
    assert "turn skipped Unreadable" in log(environ)
    # The skipped turn is behind the saved offset, so no later run meets it again
    run(transcript, environ)
    assert len(emet.envelopes) == 1
    assert status(environ)["skipped_turns"] == 1
    assert "worker error" not in log(environ)


def test_any_error_while_building_a_turn_skips_only_that_turn(transcript, environ, emet, monkeypatch):
    broken = turn(transcript)
    after = turn(transcript, "And 3 + 3?", "6")
    real = envelope.for_turn

    def for_turn(config, builder, session_id, entries, cwd):
        if entries[0][1]["uuid"] == broken["uuid"]:
            raise KeyError("a field Claude Code no longer writes")
        return real(config, builder, session_id, entries, cwd)

    monkeypatch.setattr(envelope, "for_turn", for_turn)
    run(transcript, environ)
    assert [item["trace_id"] for item in emet.envelopes] == [after["uuid"]]
    assert status(environ)["last_skipped"]["reason"] == "KeyError"
    assert "a field" not in log(environ)


def test_what_is_added_to_a_skipped_turn_does_not_join_the_turn_before_it(transcript, environ, emet):
    turn(transcript)
    transcript.prompt("And 3 + 3?", timestamp="")
    transcript.text("msg_a", "6", timestamp="")
    run(transcript, environ)
    assert len(emet.envelopes) == 1
    transcript.text("msg_b", "one more thing")
    run(transcript, environ)
    assert len(emet.envelopes) == 1


def test_an_addition_that_cannot_be_built_is_skipped_once(transcript, environ, emet, monkeypatch):
    turn(transcript)
    run(transcript, environ)
    transcript.text("msg_late", "one more thing")

    def for_tail(*args, **kwargs):
        raise KeyError("a field Claude Code no longer writes")

    monkeypatch.setattr(envelope, "for_tail", for_tail)
    run(transcript, environ)
    run(transcript, environ)
    assert len(emet.envelopes) == 1
    assert status(environ)["skipped_turns"] == 1


def test_a_run_that_skips_nothing_records_no_skip(transcript, environ, emet):
    turn(transcript)
    run(transcript, environ)
    assert "skipped_turns" not in status(environ)
    assert "last_skipped" not in status(environ)


@pytest.mark.parametrize("missing", [None, "", 7])
def test_a_turn_with_no_id_gets_one_from_its_place_in_the_file(transcript, environ, emet, missing):
    turn(transcript, uuid=missing)
    turn(transcript, "And 3 + 3?", "6", uuid=missing)
    run(transcript, environ)
    first, second = [item["trace_id"] for item in emet.envelopes]
    assert first == "turn-at-0"
    assert re.fullmatch(r"turn-at-[1-9][0-9]*", second)
    assert "None" not in (first, second)


def background_agent(transcript):
    transcript.prompt("research in the background")
    transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig", "run_in_background": True})
    path = transcript.subagent("bg1", "toolu_agent")
    transcript.write({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "dig"}}, path)
    transcript.tool_result("toolu_agent", "Async agent launched")
    transcript.text("msg_2", "Started a background agent.")
    return path


def test_subagent_progress_with_no_times_is_skipped_once_and_later_progress_is_sent(transcript, environ, emet):
    path = background_agent(transcript)
    run(transcript, environ)
    transcript.text("msg_s1", "halfway", path=path, isSidechain=True, timestamp="")
    run(transcript, environ)
    run(transcript, environ)
    assert len(emet.envelopes) == 1
    assert status(environ)["skipped_turns"] == 1
    transcript.text("msg_s2", "done", path=path, isSidechain=True)
    run(transcript, environ)
    assert [span["span_id"] for span in emet.envelopes[-1]["spans"]] == ["model.msg_s2"]


def test_a_subagent_with_no_times_takes_the_time_of_the_call_that_launched_it(transcript, environ, emet):
    transcript.prompt("research it")
    call = transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig"})
    path = transcript.subagent("s1", "toolu_agent")
    transcript.write({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "dig"}, "timestamp": ""}, path)
    transcript.text("msg_s1", "found it", path=path, isSidechain=True, timestamp="")
    transcript.tool_result("toolu_agent", "found it")
    transcript.text("msg_2", "Here is what the agent found.")
    run(transcript, environ)
    spans = {span["span_id"]: span for span in emet.envelopes[0]["spans"]}
    assert times(spans["agent.s1"]) == times(spans["model.msg_s1"]) == (call["timestamp"], call["timestamp"])
