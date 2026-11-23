import json
import re
from datetime import datetime, timezone

import pytest

from emet_claude import commands, envelope, pending, storage, timestamps

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


@pytest.mark.parametrize(
    ("fraction", "microseconds"),
    [
        ("1", 100000),
        ("12", 120000),
        ("123", 123000),
        ("1234", 123400),
        ("12345", 123450),
        ("123456", 123456),
        ("1234567", 123456),
        ("12345678", 123456),
        ("123456789", 123456),
    ],
)
def test_reads_every_fraction_length(fraction, microseconds):
    at = timestamps.parse(f"2026-10-20T10:00:01.{fraction}Z")
    assert at == datetime(2026, 10, 20, 10, 0, 1, microseconds, tzinfo=timezone.utc)


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


def test_reads_the_time_the_plugin_writes_in_its_own_files(monkeypatch):
    # One reading of one clock. Two readings can fall in different seconds, and two clocks can disagree
    monkeypatch.setattr(storage.time, "time", lambda: 1_800_000_000.789)
    assert storage.now() == "2027-01-15T08:00:00.789Z"
    assert timestamps.parse(storage.now()) == datetime(2027, 1, 15, 8, 0, 0, 789000, tzinfo=timezone.utc)


@pytest.mark.parametrize("old", ["2027-01-15T03:00:00-0500", "2027-01-15T08:00:00+0000", "2027-01-15T13:30:00+0530"])
def test_still_reads_the_time_an_older_plugin_wrote_in_its_files(old):
    assert timestamps.parse(old) == datetime(2027, 1, 15, 8, tzinfo=timezone.utc)


def test_sends_times_unchanged_for_todays_transcripts(transcript, environ, emet):
    prompt = turn(transcript)
    run(transcript, environ)
    spans = emet.envelopes[0]["spans"]
    assert [span["kind"] for span in spans] == ["turn", "model"]
    answered = f"2026-10-20T10:00:{transcript.clock:02d}.000Z"
    assert [times(span) for span in spans] == [(prompt["timestamp"], answered)] * 2
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

    def for_turn(config, builder, session_id, entries, cwd, handed):
        if entries[0][1]["uuid"] == broken["uuid"]:
            raise KeyError("a field Claude Code no longer writes")
        return real(config, builder, session_id, entries, cwd, handed)

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
    second_starts_at = transcript.path.stat().st_size
    turn(transcript, "And 3 + 3?", "6", uuid=missing)
    run(transcript, environ)
    assert [item["trace_id"] for item in emet.envelopes] == ["turn-at-0", f"turn-at-{second_starts_at}"]


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


def test_a_subagent_whose_first_entry_has_no_time_starts_with_the_call_that_launched_it(transcript, environ, emet):
    transcript.prompt("research it")
    call = transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig"})
    path = transcript.subagent("s1", "toolu_agent")
    transcript.write({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "dig"}, "timestamp": ""}, path)
    found = transcript.text("msg_s1", "found it", path=path, isSidechain=True)
    transcript.tool_result("toolu_agent", "found it")
    transcript.text("msg_2", "Here is what the agent found.")
    run(transcript, environ)
    spans = {span["span_id"]: span for span in emet.envelopes[0]["spans"]}
    assert times(spans["agent.s1"]) == times(spans["model.msg_s1"]) == (call["timestamp"], found["timestamp"])


def test_an_entry_with_no_time_takes_the_time_before_it_not_the_first_of_the_turn(transcript, environ, emet):
    transcript.prompt("What is 2 + 2?", timestamp="2026-10-20T10:00:01.000Z")
    transcript.assistant("msg_1", [{"type": "text", "text": "a"}], timestamp="2026-10-20T10:00:05.000Z")
    transcript.assistant("msg_2", [{"type": "text", "text": "b"}], timestamp="2026-10-20T10:00:07.000Z")
    transcript.assistant("msg_3", [{"type": "text", "text": "c"}], timestamp="")
    transcript.text("msg_4", "d", timestamp="2026-10-20T10:00:09.000Z")
    run(transcript, environ)
    spans = {span["span_id"]: span for span in emet.envelopes[0]["spans"]}
    assert times(spans["model.msg_3"]) == ("2026-10-20T10:00:07.000Z", "2026-10-20T10:00:07.000Z")
    assert times(spans["model.msg_4"]) == ("2026-10-20T10:00:07.000Z", "2026-10-20T10:00:09.000Z")


def test_no_span_ends_before_it_starts_when_the_clock_steps_back(transcript, environ, emet):
    transcript.prompt("go", timestamp="2026-10-20T10:00:10.000Z")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "ls"}, timestamp="2026-10-20T10:00:20.000Z")
    transcript.tool_result("toolu_1", "ok", timestamp="2026-10-20T10:00:15.000Z")
    transcript.text("msg_2", "done", timestamp="2026-10-20T10:00:12.000Z")
    run(transcript, environ)
    spans = {span["span_id"]: span for span in emet.envelopes[0]["spans"]}
    assert times(spans["turn"]) == ("2026-10-20T10:00:10.000Z", "2026-10-20T10:00:20.000Z")
    assert times(spans["tool.toolu_1"]) == ("2026-10-20T10:00:20.000Z", "2026-10-20T10:00:20.000Z")
    assert times(spans["model.msg_2"]) == ("2026-10-20T10:00:15.000Z", "2026-10-20T10:00:15.000Z")


def test_a_subagent_does_not_end_before_it_starts_when_its_clock_steps_back(transcript, environ, emet):
    transcript.prompt("research it", timestamp="2026-10-20T10:00:01.000Z")
    transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig"}, timestamp="2026-10-20T10:00:02.000Z")
    path = transcript.subagent("s1", "toolu_agent")
    opening = {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "dig"}}
    transcript.write(dict(opening, timestamp="2026-10-20T10:00:09.000Z"), path)
    transcript.text("msg_s1", "found it", path=path, isSidechain=True, timestamp="2026-10-20T10:00:04.000Z")
    transcript.tool_result("toolu_agent", "found it", timestamp="2026-10-20T10:00:10.000Z")
    transcript.text("msg_2", "Here it is.", timestamp="2026-10-20T10:00:11.000Z")
    run(transcript, environ)
    spans = {span["span_id"]: span for span in emet.envelopes[0]["spans"]}
    assert times(spans["agent.s1"]) == ("2026-10-20T10:00:09.000Z", "2026-10-20T10:00:09.000Z")


def test_skips_add_up_within_a_run_and_across_runs(transcript, environ, emet, monkeypatch):
    broken = turn(transcript)
    transcript.prompt("And 3 + 3?", timestamp="")
    transcript.text("msg_a", "6", timestamp="")
    real = envelope.for_turn

    def for_turn(config, builder, session_id, entries, cwd, handed):
        if entries[0][1]["uuid"] == broken["uuid"]:
            raise KeyError("a field Claude Code no longer writes")
        return real(config, builder, session_id, entries, cwd, handed)

    monkeypatch.setattr(envelope, "for_turn", for_turn)
    run(transcript, environ)
    assert status(environ)["skipped_turns"] == 2
    assert status(environ)["last_skipped"]["reason"] == "Unreadable"
    turn(transcript, "And 4 + 4?", "8", uuid=broken["uuid"])
    run(transcript, environ)
    assert status(environ)["skipped_turns"] == 3
    assert status(environ)["last_skipped"]["reason"] == "KeyError"


def test_a_line_nested_too_deep_to_read_is_passed_over(transcript, environ, emet):
    first = turn(transcript)
    with open(transcript.path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write("[" * 100_000 + "\n")
    second = turn(transcript, "And 3 + 3?", "6")
    run(transcript, environ)
    assert [item["trace_id"] for item in emet.envelopes] == [first["uuid"], second["uuid"]]
    assert "worker error" not in log(environ)


def test_a_subagent_file_that_cannot_be_read_costs_the_subagent_not_the_turn(transcript, environ, emet, monkeypatch):
    prompt = transcript.prompt("research it")
    transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig"})
    path = transcript.subagent("s1", "toolu_agent")
    transcript.text("msg_s1", "found it", path=path, isSidechain=True)
    transcript.tool_result("toolu_agent", "found it")
    transcript.text("msg_2", "Here it is.")
    real = commands.transcript.entries_of

    def entries_of(handle, offset, sidechain=False):
        if sidechain:
            raise PermissionError("locked")
        return real(handle, offset, sidechain)

    monkeypatch.setattr(commands.transcript, "entries_of", entries_of)
    run(transcript, environ)
    assert [item["trace_id"] for item in emet.envelopes] == [prompt["uuid"]]
    assert {span["span_id"] for span in emet.envelopes[0]["spans"]} == {"turn", "model.msg_1", "tool.toolu_agent", "model.msg_2"}
    assert "skipped_turns" not in status(environ)


def test_a_saved_tool_output_that_cannot_be_read_falls_back_to_the_result_in_the_transcript(transcript, environ, emet, monkeypatch):
    saved = transcript.saved_output("out.txt", "the full output")
    transcript.prompt("go")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "ls"})
    transcript.tool_result("toolu_1", "short", tool_use_result={"persistedOutputPath": str(saved)})
    transcript.text("msg_2", "done")
    real = open

    def locked(target, *args, **kwargs):
        if str(target) == str(saved.resolve()):
            raise PermissionError("locked")
        return real(target, *args, **kwargs)

    monkeypatch.setattr("builtins.open", locked)
    run(transcript, environ)
    tool = next(span for span in emet.envelopes[0]["spans"] if span["kind"] == "tool")
    assert tool["payload"]["result"] == "short"
    assert "skipped_turns" not in status(environ)


def test_the_id_from_a_turns_place_counts_bytes_and_holds_across_runs(transcript, environ, emet):
    turn(transcript)
    run(transcript, environ)
    # A line Claude Code wrote with letters that take more than one byte each
    with open(transcript.path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"type": "summary", "summary": "Umsatz in Köln 売上"}\n')
    place = transcript.path.stat().st_size
    assert place > len(transcript.path.read_text(encoding="utf-8"))
    turn(transcript, "And 3 + 3?", "6", uuid=None)
    run(transcript, environ)
    assert emet.envelopes[-1]["trace_id"] == f"turn-at-{place}"


def test_a_turn_starts_at_its_earliest_time_so_no_span_starts_before_it(transcript, environ, emet):
    transcript.prompt("go", timestamp="2026-10-20T10:00:10.000Z")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "ls"}, timestamp="2026-10-20T10:00:05.000Z")
    transcript.tool_result("toolu_1", "ok", timestamp="2026-10-20T10:00:12.000Z")
    transcript.text("msg_2", "done", timestamp="2026-10-20T10:00:14.000Z")
    run(transcript, environ)
    spans = {span["span_id"]: span for span in emet.envelopes[0]["spans"]}
    assert times(spans["turn"]) == ("2026-10-20T10:00:05.000Z", "2026-10-20T10:00:14.000Z")
    assert times(spans["tool.toolu_1"]) == ("2026-10-20T10:00:05.000Z", "2026-10-20T10:00:12.000Z")


def test_a_subagent_ends_at_its_latest_time_not_its_last_line(transcript, environ, emet):
    transcript.prompt("research it", timestamp="2026-10-20T10:00:01.000Z")
    transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig"}, timestamp="2026-10-20T10:00:02.000Z")
    path = transcript.subagent("s1", "toolu_agent")
    opening = {"type": "user", "isSidechain": True, "message": {"role": "user", "content": "dig"}}
    transcript.write(dict(opening, timestamp="2026-10-20T10:00:03.000Z"), path)
    transcript.text("msg_s1", "halfway", path=path, isSidechain=True, timestamp="2026-10-20T10:00:08.000Z")
    transcript.text("msg_s2", "found it", path=path, isSidechain=True, timestamp="2026-10-20T10:00:06.000Z")
    transcript.tool_result("toolu_agent", "found it", timestamp="2026-10-20T10:00:10.000Z")
    transcript.text("msg_2", "Here it is.", timestamp="2026-10-20T10:00:11.000Z")
    run(transcript, environ)
    spans = {span["span_id"]: span for span in emet.envelopes[0]["spans"]}
    assert times(spans["agent.s1"]) == ("2026-10-20T10:00:03.000Z", "2026-10-20T10:00:08.000Z")


def test_a_skipped_turn_is_marked_in_the_sessions_own_file_before_the_run_is_stamped(transcript, environ, emet):
    transcript.prompt("What is 2 + 2?", timestamp="")
    transcript.text("msg_1", "4", timestamp="")
    after = turn(transcript, "And 3 + 3?", "6")
    run(transcript, environ)
    lines = pending.lines(storage.data_dir(environ), "session-1")
    assert [sorted(line) for line in lines] == [
        ["reason", "skipped"],
        ["at", "conversation_uuid", "ended", "endpoint", "trace_uuid"],
        ["finished", "queued"],
    ]
    assert lines[0]["reason"] == "Unreadable"
    assert timestamps.parse(lines[0]["skipped"]) is not None
    assert pending.traces(storage.data_dir(environ), "session-1") == [emet.trace_uuids[after["uuid"]]]
