import io
import json
import os
import socket
import stat
import sys
import uuid

import pytest

from conftest import API_KEY
from emet_claude import commands, config, pause, pending, report, storage, transport, watch
from helpers import Transcript
from test_report import claim, document

SESSION = "session-1"
T1 = "0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11"
T2 = "1b4e28ba-2fa1-41d2-883f-0016d3cca427"
CONVERSATION = "7d3c1c52-0d0a-4a52-8d0e-0c7e3a1b2c3d"
REPORT = "11111111-1111-4111-8111-111111111111"
EVERY_REQUEST = [8, 13, 23, 38, 58, 88, 118, 148, 178, 208, 238, 268, 298]
REAL_EXTRA = watch.extra


class Sleeper:
    """Stands in for sleeping: moves the clock, then runs what a test planned for that moment of the run."""

    def __init__(self, clock):
        self.clock = clock
        self.start = clock.now
        self.planned = []

    def at(self, offset, action):
        self.planned.append((offset, action))

    def __call__(self, seconds):
        assert seconds > 0
        self.clock.advance(seconds)
        for item in list(self.planned):
            if self.clock.now - self.start >= item[0]:
                self.planned.remove(item)
                item[1]()


@pytest.fixture(autouse=True)
def sleeper(monkeypatch, clock):
    """No test sleeps or depends on chance."""
    fake = Sleeper(clock)
    monkeypatch.setattr(watch, "sleep", fake)
    monkeypatch.setattr(watch, "extra", lambda: 0.0)
    return fake


@pytest.fixture
def asked(monkeypatch, clock):
    """Lists each report read as (seconds since the test began, trace)."""
    found = []
    start = clock.now
    real = watch.read_report

    def recording(settings, trace_uuid):
        found.append((round(clock.now - start, 3), trace_uuid))
        return real(settings, trace_uuid)

    monkeypatch.setattr(watch, "read_report", recording)
    return found


@pytest.fixture
def session(transcript):
    """A session file whose last entry names a Claude Code that can be woken."""
    transcript.prompt("What is 2 + 2?")
    transcript.text("msg_1", "4")
    return transcript


def root(environ):
    return storage.data_dir(environ)


def deliver(environ, clock, trace_uuid=T1, ago=0.0, **fields):
    """Writes the line the sender writes once Emet accepted a turn."""
    line = {"trace_uuid": trace_uuid, "at": storage.now(clock.now - ago), "endpoint": environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"]}
    line.update(fields)
    pending.append(root(environ), SESSION, line)


def finish(environ, clock, queued=0, ago=0.0):
    pending.append(root(environ), SESSION, {"finished": storage.now(clock.now - ago), "queued": queued})


def stop(transcript, environ, **fields):
    return watch.run(json.dumps({**transcript.hook_input(), **fields}).encode(), environ)


def outcomes(environ, session_id=SESSION):
    return [(item["trace_uuid"], item["outcome"], item["final"]) for item in watch.checks(root(environ), session_id)]


def status(environ):
    return json.loads((root(environ) / "status.json").read_text(encoding="utf-8"))


def log(environ):
    return (root(environ) / "emet.log").read_text(encoding="utf-8")


def test_a_flagged_report_wakes_claude_with_the_rendered_text(session, environ, emet, clock, capfdbinary):
    deliver(environ, clock, conversation_uuid=CONVERSATION)
    emet.report(T1, 200, document())
    assert stop(session, environ) == 2
    out, err = capfdbinary.readouterr()
    assert out == b""
    assert err == report.wake_message(document(), emet.url, SESSION, CONVERSATION).encode("utf-8")
    assert f"selected={CONVERSATION}" in err.decode("utf-8")
    assert emet.reads[0]["path"] == f"/api/v1/traces/{T1}/report/"
    assert emet.reads[0]["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert emet.requests == []


def test_a_flagged_report_is_recorded_under_emets_ids(session, environ, emet, clock):
    deliver(environ, clock)
    emet.report(T1, 200, document())
    stop(session, environ)
    (check,) = watch.checks(root(environ), SESSION)
    assert check == {
        "trace_uuid": T1,
        "outcome": "flagged",
        "final": True,
        "at": storage.now(clock.now),
        "handed": True,
        "report_uuid": REPORT,
        "revision": 1,
    }


@pytest.mark.parametrize("word", ["clear", "clarified", "unchecked", "nothing"])
def test_a_report_that_is_not_flagged_never_wakes_claude(session, environ, emet, clock, capfdbinary, word):
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 200, document(status=word))
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert outcomes(environ) == [(T1, word, True)]
    assert watch.verdicts(root(environ), SESSION) == []


def test_the_first_request_comes_eight_seconds_after_the_turn_ends(session, environ, emet, clock, asked):
    deliver(environ, clock)
    emet.report(T1, 200, document())
    stop(session, environ)
    assert asked == [(8, T1)]


def test_a_report_that_is_never_ready_is_asked_for_thirteen_times_in_five_minutes(session, environ, emet, clock, asked):
    deliver(environ, clock)
    emet.report(T1, 202)
    start = clock.now
    assert stop(session, environ) == 0
    assert [at for at, _ in asked] == EVERY_REQUEST
    assert clock.now - start == 300
    assert outcomes(environ) == [(T1, "timed_out", False)]


def test_a_turn_still_waiting_when_the_time_is_up_is_polled_after_the_next_stop(session, environ, emet, clock, capfdbinary):
    deliver(environ, clock)
    emet.report(T1, 202)
    emet.report(T1, 202)
    emet.report(T1, 200, document())
    emet.reports[T1][0:1] = [emet.reports[T1][0]] * 13
    assert stop(session, environ) == 0
    assert stop(session, environ) == 2
    assert outcomes(environ) == [(T1, "timed_out", False), (T1, "flagged", True)]


def test_the_body_of_a_202_is_never_rendered(session, environ, emet, clock, capfdbinary):
    deliver(environ, clock)
    emet.report(T1, 202, document())
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert watch.verdicts(root(environ), SESSION) == []
    assert outcomes(environ) == [(T1, "timed_out", False)]


@pytest.mark.parametrize(("named", "second"), [("12", 20), ("2", 13), (None, 13)])
def test_each_wait_is_the_longer_of_the_step_and_the_wait_emet_names(session, environ, emet, clock, asked, named, second):
    deliver(environ, clock)
    emet.report(T1, 202, retry_after=named)
    emet.report(T1, 202)
    stop(session, environ)
    assert [at for at, _ in asked][:3] == [8, second, second + 10]


def test_a_named_wait_past_the_time_left_ends_the_run_on_time(session, environ, emet, clock, asked):
    deliver(environ, clock)
    emet.report(T1, 202, retry_after="900")
    start = clock.now
    assert stop(session, environ) == 0
    assert len(asked) == 1 and clock.now - start == 300


def test_chance_only_makes_a_wait_longer(session, environ, emet, clock, asked, monkeypatch):
    monkeypatch.setattr(watch, "extra", lambda: 0.1)
    deliver(environ, clock)
    emet.report(T1, 202)
    stop(session, environ)
    assert [at for at, _ in asked][:3] == [8, 13.5, 24.5]


def test_the_chance_is_never_negative_and_at_most_a_tenth():
    drawn = [REAL_EXTRA() for _ in range(200)]
    assert all(0 <= value <= 0.1 for value in drawn)
    assert len(set(drawn)) > 1


def test_the_poller_waits_for_the_sender_to_name_the_trace(session, environ, emet, clock, asked, sleeper):
    sleeper.at(3, lambda: deliver(environ, clock))
    emet.report(T1, 200, document())
    assert stop(session, environ) == 2
    assert asked == [(8, T1)]


def test_a_trace_the_sender_names_late_is_asked_for_at_once(session, environ, emet, clock, asked, sleeper):
    sleeper.at(30, lambda: deliver(environ, clock))
    emet.report(T1, 200, document())
    assert stop(session, environ) == 2
    assert asked == [(30, T1)]


def test_the_poller_gives_up_after_a_minute_with_nothing_to_poll(session, environ, emet, clock):
    start = clock.now
    assert stop(session, environ) == 0
    assert clock.now - start == 60
    assert emet.reads == []
    assert outcomes(environ) == []


def test_the_poller_leaves_at_once_when_the_sender_finished_with_an_empty_queue(session, environ, emet, clock):
    finish(environ, clock)
    finish(environ, clock)
    start = clock.now
    assert stop(session, environ) == 0
    assert clock.now == start


@pytest.mark.parametrize(("queued", "ago"), [(1, 0), (0, 5), (True, 0), (False, 0), ("0", 0), (None, 0)])
def test_the_poller_keeps_waiting_while_the_queue_holds_envelopes_or_the_stamp_is_old(session, environ, emet, clock, queued, ago):
    finish(environ, clock, queued=queued, ago=ago)
    start = clock.now
    assert stop(session, environ) == 0
    assert clock.now - start == 60


def test_a_turn_delivered_from_the_queue_during_the_wait_is_polled(session, environ, emet, clock, asked, sleeper):
    finish(environ, clock, queued=1)
    sleeper.at(40, lambda: deliver(environ, clock))
    emet.report(T1, 200, document())
    assert stop(session, environ) == 2
    assert asked == [(40, T1)]


def test_the_poller_leaves_once_every_turn_has_an_outcome_and_the_sender_is_done(session, environ, emet, clock):
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 200, document(status="clear"))
    start = clock.now
    assert stop(session, environ) == 0
    assert clock.now - start < 12


def test_the_minute_counts_from_the_last_turn_that_was_waiting(session, environ, emet, clock):
    deliver(environ, clock)
    emet.report(T1, 200, document(status="clear"))
    start = clock.now
    assert stop(session, environ) == 0
    assert 68 <= clock.now - start <= 70


@pytest.mark.parametrize("code", ["not_assessed", "failed", "not_found"])
def test_emets_own_404_ends_the_wait_for_good(session, environ, emet, clock, capfdbinary, code):
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 404, code)
    assert stop(session, environ) == 0
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert emet.polled(T1) == 1
    assert outcomes(environ) == [(T1, code, True)]


def test_a_404_that_is_not_emets_own_is_asked_again(session, environ, emet, clock):
    deliver(environ, clock)
    emet.report(T1, 404, "gone_fishing")
    assert stop(session, environ) == 0
    assert emet.polled(T1) == 13
    assert outcomes(environ) == [(T1, "timed_out", False)]


@pytest.mark.parametrize("code", [401, 403])
def test_a_refused_key_stops_the_run(session, environ, emet, clock, capfdbinary, code):
    deliver(environ, clock)
    deliver(environ, clock, T2)
    emet.report(T1, code)
    start = clock.now
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert len(emet.reads) == 1 and clock.now - start == 8
    assert outcomes(environ) == [(T1, "key_refused", False), (T2, "key_refused", False)]
    assert pause.until(root(environ)) == ""
    emet.reports[T1] = []
    emet.report(T1, 200, document(status="clear"))
    assert stop(session, environ) == 0
    assert (T1, "clear", True) in outcomes(environ)


def test_a_429_stops_the_run_and_starts_the_pause_every_session_shares(session, environ, emet, clock, capfdbinary):
    deliver(environ, clock)
    deliver(environ, clock, T2)
    emet.report(T1, 429, retry_after="120")
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert len(emet.reads) == 1
    assert outcomes(environ) == [(T1, "rate_limited", False), (T2, "rate_limited", False)]
    until = pause.until(root(environ))
    assert until == storage.now(clock.now + 120)
    assert transport.Sender(root(environ), config.load(environ)).held()


def test_no_report_is_asked_for_during_a_pause(session, environ, emet, clock):
    deliver(environ, clock)
    emet.report(T1, 200, document())
    pause.extend(root(environ))
    start = clock.now
    assert stop(session, environ) == 0
    assert emet.reads == [] and clock.now == start
    assert outcomes(environ) == [(T1, "paused", False)]
    clock.advance(31)
    assert stop(session, environ) == 2


def test_a_pause_another_session_starts_ends_a_run_already_polling(session, environ, emet, clock, sleeper):
    deliver(environ, clock)
    emet.report(T1, 202)
    sleeper.at(20, lambda: pause.extend(root(environ)))
    start = clock.now
    assert stop(session, environ) == 0
    assert clock.now - start < 24
    assert outcomes(environ) == [(T1, "paused", False)]


@pytest.mark.parametrize("code", [500, 503, 302, 418])
def test_any_other_answer_is_asked_again_on_the_same_schedule(session, environ, emet, clock, asked, code):
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, code)
    emet.report(T1, 200, document(status="clear"))
    assert stop(session, environ) == 0
    assert [at for at, _ in asked] == [8, 13]
    assert all(read["path"] == f"/api/v1/traces/{T1}/report/" for read in emet.reads)
    assert outcomes(environ) == [(T1, "clear", True)]
    assert f"report {T1} -> {code}, asked again later" in log(environ)


def test_an_emet_that_cannot_be_reached_never_wakes_or_raises(session, environ, clock, capfdbinary):
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"] = f"http://127.0.0.1:{taken.getsockname()[1]}"
    deliver(environ, clock)
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert outcomes(environ) == [(T1, "timed_out", False)]
    assert f"report {T1} -> network, asked again later" in log(environ)


@pytest.mark.parametrize(
    "body",
    [
        b"not json",
        b"[]",
        b"\xff\xfe",
        document(version=2),
        {"version": 1, "report": {"status": "a-new-word"}},
        {"version": 1},
    ],
)
def test_a_document_that_cannot_be_read_is_its_own_outcome(session, environ, emet, clock, capfdbinary, body):
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 200, body)
    assert stop(session, environ) == 0
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert emet.polled(T1) == 1
    assert outcomes(environ) == [(T1, "unreadable", True)]


@pytest.mark.parametrize(("over", "outcome"), [(1, "unreadable"), (0, "clear")])
def test_a_document_longer_than_the_limit_is_unreadable(session, environ, emet, clock, monkeypatch, over, outcome):
    body = json.dumps(document(status="clear")).encode()
    monkeypatch.setattr(watch, "REPORT_MAX_BYTES", len(body) - over)
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 200, body)
    assert stop(session, environ) == 0
    assert outcomes(environ) == [(T1, outcome, True)]


@pytest.mark.parametrize(
    "line",
    [
        {"endpoint": "https://app.emet.so"},
        {"endpoint": None},
        {"refused": "2027-01-15T08:00:00+0000"},
        {"finished": "2020-01-01T00:00:00+0000"},
        {"skipped": "2027-01-15T08:00:00+0000"},
        {"at": "t"},
        {"at": "2027-01-15T08:00:00"},
        {"at": None},
        {"trace_uuid": "not-a-uuid"},
        {"trace_uuid": 7},
        {"trace_uuid": None},
    ],
)
def test_a_line_that_is_not_a_turn_delivered_to_this_emet_is_never_polled(session, environ, emet, clock, line):
    deliver(environ, clock, **line)
    assert stop(session, environ) == 0
    assert emet.reads == []
    assert outcomes(environ) == []


@pytest.mark.parametrize("refused_id", ["c0ffee00c0ffee00c0ffee00c0ffee00", T1])
def test_a_turn_emet_refused_is_never_polled(session, environ, emet, clock, refused_id):
    pending.refuse(root(environ), SESSION, refused_id, 422, emet.url)
    (line,) = pending.lines(root(environ), SESSION)
    assert {**line, "at": ""} == {"refused": refused_id, "at": "", "status": 422, "endpoint": emet.url}
    finish(environ, clock)
    emet.report(refused_id, 200, document())
    assert stop(session, environ) == 0
    assert emet.reads == []
    assert outcomes(environ) == []
    assert watch.delivered(pending.lines(root(environ), SESSION), emet.url) == []


def test_a_line_with_a_field_the_poller_does_not_know_is_still_a_turn(session, environ, emet, clock):
    deliver(environ, clock, something_new=1)
    emet.report(T1, 200, document())
    assert stop(session, environ) == 2


def test_a_trace_written_twice_is_asked_for_once(session, environ, emet, clock):
    deliver(environ, clock)
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 200, document(status="clear"))
    stop(session, environ)
    assert emet.polled(T1) == 1


def test_a_turn_delivered_more_than_a_day_ago_is_recorded_and_not_polled(session, environ, emet, clock):
    deliver(environ, clock, ago=24 * 3600 + 60)
    deliver(environ, clock, T2, ago=24 * 3600 - 60)
    finish(environ, clock)
    emet.report(T2, 200, document(status="clear"))
    assert stop(session, environ) == 0
    assert emet.polled(T1) == 0 and emet.polled(T2) == 1
    assert outcomes(environ) == [(T1, "timed_out", True), (T2, "clear", True)]
    assert stop(session, environ) == 0
    assert len(outcomes(environ)) == 2


def test_only_the_five_newest_turns_are_polled_at_once(session, environ, emet, clock):
    traces = [str(uuid.UUID(int=number)) for number in range(1, 8)]
    for trace in traces:
        deliver(environ, clock, trace)
        emet.report(trace, 202)
    assert stop(session, environ) == 0
    assert [emet.polled(trace) for trace in traces] == [0, 0, 13, 13, 13, 13, 13]
    assert outcomes(environ) == [(trace, "timed_out", False) for trace in traces]


def test_older_turns_are_polled_as_newer_ones_get_their_outcome(session, environ, emet, clock):
    traces = [str(uuid.UUID(int=number)) for number in range(1, 8)]
    for trace in traces:
        deliver(environ, clock, trace)
        emet.report(trace, 200, document(status="clear"))
    finish(environ, clock)
    assert stop(session, environ) == 0
    assert sorted(outcomes(environ)) == [(trace, "clear", True) for trace in traces]


@pytest.mark.parametrize(
    ("version", "polled"),
    [
        ("2.1.286", False),
        ("2.0.999", False),
        ("1.9.999", False),
        ("2.1", False),
        ("", False),
        ("latest", False),
        (None, False),
        (2, False),
        ("2.1.287", True),
        ("2.1.287-beta.1", True),
        ("2.1.1000", True),
        ("2.2.0", True),
        ("3.0.0", True),
    ],
)
def test_nothing_happens_on_a_claude_code_older_than_2_1_287(transcript, environ, emet, clock, version, polled):
    transcript.prompt("What is 2 + 2?", version=version)
    deliver(environ, clock)
    emet.report(T1, 200, document())
    start = clock.now
    assert stop(transcript, environ) == (2 if polled else 0)
    assert bool(emet.reads) is polled
    if not polled:
        assert clock.now == start
        assert outcomes(environ) == []


def test_the_version_is_the_last_one_the_session_file_names(transcript, environ, emet, clock):
    transcript.prompt("An old question", version="2.1.100")
    transcript.text("msg_1", "4", version="2.1.294")
    with open(transcript.path, "a", encoding="utf-8") as handle:
        handle.write('{"type": "summary"}\n[1, 2]\n{"cut short')
    deliver(environ, clock)
    emet.report(T1, 200, document())
    assert stop(transcript, environ) == 2


def test_only_the_end_of_a_long_session_file_is_read_for_the_version(transcript, environ, emet, clock, monkeypatch):
    monkeypatch.setattr(watch, "TAIL_BYTES", 600)
    transcript.prompt("x" * 2000, version="2.1.100")
    transcript.text("msg_1", "4", version="2.1.294")
    deliver(environ, clock)
    emet.report(T1, 200, document())
    assert stop(transcript, environ) == 2


@pytest.mark.parametrize("path", ["", "missing.jsonl", "."])
def test_nothing_happens_without_a_session_file_to_read_the_version_from(session, environ, emet, clock, path):
    deliver(environ, clock)
    emet.report(T1, 200, document())
    start = clock.now
    assert stop(session, environ, transcript_path=path) == 0
    assert emet.reads == [] and clock.now == start


def test_a_turn_a_hook_started_is_not_polled(session, environ, emet, clock, capfdbinary):
    deliver(environ, clock)
    emet.report(T1, 200, document())
    start = clock.now
    assert stop(session, environ, stop_hook_active=True) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert emet.reads == [] and clock.now == start
    assert stop(session, environ, stop_hook_active=False) == 2


def test_a_second_watcher_of_a_session_leaves_at_once(session, environ, emet, clock, tmp_path):
    deliver(environ, clock)
    emet.report(T1, 200, document())
    start = clock.now
    with storage.try_locked(root(environ) / "state" / f"{SESSION}.watch.lock") as held:
        assert held
        assert stop(session, environ) == 0
        assert emet.reads == [] and clock.now == start
        other = Transcript(tmp_path / "projects", "session-2")
        other.prompt("Another session")
        assert stop(other, environ) == 0
        assert clock.now - start == 60
    assert stop(session, environ) == 2


def test_the_lock_is_taken_without_waiting_and_freed_after(tmp_path):
    path = tmp_path / "some.lock"
    with storage.try_locked(path) as first:
        with storage.try_locked(path) as second:
            assert (first, second) == (True, False)
        with storage.try_locked(path) as third:
            assert third is False
    with storage.try_locked(path) as fourth:
        assert fourth is True
    with storage.locked(path), storage.try_locked(path) as fifth:
        assert fifth is False


def test_the_running_watcher_polls_a_turn_another_stop_delivers(session, environ, emet, clock, asked, sleeper):
    deliver(environ, clock)
    emet.report(T1, 202)
    emet.report(T2, 200, document(status="clear"))
    sleeper.at(20, lambda: deliver(environ, clock, T2))
    assert stop(session, environ) == 0
    assert [at for at, trace in asked if trace == T2] == [21]
    assert (T2, "clear", True) in outcomes(environ)


def test_only_the_newest_turn_wakes_claude_and_an_older_flagged_one_is_kept_for_the_next_prompt(session, environ, emet, clock):
    for trace in (T1, T2):
        deliver(environ, clock, trace)
        emet.report(trace, 200, document())
    assert stop(session, environ) == 2
    first, second = watch.checks(root(environ), SESSION)
    assert (first["trace_uuid"], first["outcome"], first["handed"], first["held"]) == (T1, "flagged", False, "newer_turn")
    assert (second["trace_uuid"], second["handed"], "held" in second) == (T2, True, False)
    assert [item["handed"] for item in watch.verdicts(root(environ), SESSION)] == [False, True]
    assert f"report {T1} not handed to Claude, newer_turn" in log(environ)


def test_a_turn_delivered_while_the_report_was_awaited_keeps_claude_asleep(session, environ, emet, clock, sleeper, capfdbinary):
    deliver(environ, clock)
    emet.report(T1, 202)
    emet.report(T1, 200, document())
    emet.report(T2, 202)
    sleeper.at(10, lambda: deliver(environ, clock, T2))
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert watch.checks(root(environ), SESSION)[0]["held"] == "newer_turn"
    assert len(watch.verdicts(root(environ), SESSION)) == 1


@pytest.mark.parametrize(
    ("entry", "woken"),
    [
        ({"type": "user", "promptSource": "typed", "message": {"role": "user", "content": "And 3 + 3?"}}, False),
        ({"type": "user", "promptSource": "queued", "message": {"role": "user", "content": "And 3 + 3?"}}, False),
        ({"type": "user", "promptSource": "system", "message": {"role": "user", "content": "<task-notification>"}}, False),
        ({"type": "user", "message": {"role": "user", "content": "<local-command-stdout>ok</local-command-stdout>"}}, True),
        ({"type": "user", "isMeta": True, "message": {"role": "user", "content": "Stop hook feedback"}}, True),
        ({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "Look around"}}, True),
        ({"type": "user", "message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}}, True),
        ({"type": "assistant", "message": {"role": "assistant", "content": [{"type": "text", "text": "late"}]}}, True),
    ],
)
def test_a_turn_that_started_while_the_report_was_awaited_keeps_claude_asleep(
    session, environ, emet, clock, sleeper, capfdbinary, entry, woken
):
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 200, document())
    sleeper.at(4, lambda: session.write(dict(entry)))
    assert stop(session, environ) == (2 if woken else 0)
    (check,) = watch.checks(root(environ), SESSION)
    assert (check["outcome"], check["final"], check["handed"], check.get("held")) == (
        "flagged",
        True,
        woken,
        None if woken else "newer_turn",
    )
    assert len(watch.verdicts(root(environ), SESSION)) == 1
    assert bool(capfdbinary.readouterr()[1]) is woken


def test_a_prompt_queued_before_the_poller_started_is_told_apart_by_the_turns_id(transcript, environ, emet, clock, capfdbinary):
    transcript.prompt("What is 2 + 2?", promptId="prompt-1")
    transcript.text("msg_1", "4")
    transcript.prompt("And 3 + 3?", source="queued", promptId="prompt-2")
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 200, document())
    assert stop(transcript, environ, prompt_id="prompt-1") == 0
    assert watch.checks(root(environ), SESSION)[0]["held"] == "newer_turn"


@pytest.mark.parametrize("prompt_id", ["prompt-2", "prompt-9", "", None, 5])
def test_the_turn_that_just_ended_is_not_taken_for_a_newer_one(transcript, environ, emet, clock, prompt_id):
    transcript.prompt("What is 2 + 2?", promptId="prompt-1")
    transcript.text("msg_1", "4")
    transcript.prompt("And 3 + 3?", promptId="prompt-2")
    transcript.text("msg_2", "6")
    deliver(environ, clock)
    emet.report(T1, 200, document())
    assert stop(transcript, environ, prompt_id=prompt_id) == 2


@pytest.mark.parametrize(
    "fields",
    [{"source": "sdk"}, {"turnOrigin": "sdk"}, {"entrypoint": "sdk-cli"}, {"entrypoint": "sdk-ts"}],
)
def test_a_session_with_no_person_at_it_is_not_polled(transcript, environ, emet, clock, fields):
    transcript.prompt("What is 2 + 2?", **fields)
    transcript.text("msg_1", "4", **({"entrypoint": fields["entrypoint"]} if "entrypoint" in fields else {}))
    deliver(environ, clock)
    emet.report(T1, 200, document())
    start = clock.now
    assert stop(transcript, environ) == 0
    assert emet.reads == [] and clock.now == start
    assert "watch skipped, no person is at the session" in log(environ)


@pytest.mark.parametrize("fields", [{"turnOrigin": "human"}, {"entrypoint": "cli"}, {"entrypoint": 5}, {"source": "queued"}])
def test_a_session_a_person_is_at_is_polled(transcript, environ, emet, clock, fields):
    transcript.prompt("An earlier headless question", source="sdk", entrypoint="sdk-cli")
    transcript.text("msg_0", "4", entrypoint="sdk-cli")
    transcript.prompt("What is 2 + 2?", **{"entrypoint": "cli", **fields})
    transcript.text("msg_1", "4", entrypoint="cli")
    deliver(environ, clock)
    emet.report(T1, 200, document())
    assert stop(transcript, environ) == 2


def test_the_same_report_never_wakes_claude_twice_however_often_stop_fires(session, environ, emet, clock, capfdbinary):
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 200, document())
    codes = [stop(session, environ)]
    codes.append(stop(session, environ, stop_hook_active=True))
    codes += [stop(session, environ) for _ in range(8)]
    assert codes == [2] + [0] * 9
    assert emet.polled(T1) == 1
    assert capfdbinary.readouterr()[1].count(report.WAKE_OPENING.encode()) == 1


def test_wakes_are_capped_per_session(session, environ, emet, clock, tmp_path):
    codes = []
    for number in range(1, watch.WAKE_CAP + 2):
        trace = str(uuid.UUID(int=number))
        deliver(environ, clock, trace)
        emet.report(trace, 200, document())
        codes.append(stop(session, environ))
    assert watch.WAKE_CAP == 5
    assert codes == [2, 2, 2, 2, 2, 0]
    last = watch.checks(root(environ), SESSION)[-1]
    assert (last["outcome"], last["final"], last["handed"], last["held"]) == ("flagged", True, False, "wake_cap")
    saved = watch.verdicts(root(environ), SESSION)
    assert [item["handed"] for item in saved] == [True] * 5 + [False]
    assert "not handed to Claude, wake_cap" in log(environ)
    other = Transcript(tmp_path / "projects", "session-2")
    other.prompt("Another session")
    pending.append(root(environ), "session-2", {"trace_uuid": T1, "at": storage.now(clock.now), "endpoint": emet.url})
    emet.report(T1, 200, document())
    assert stop(other, environ) == 2


def test_the_report_is_recorded_and_saved_before_claude_is_woken(session, environ, emet, clock, monkeypatch, capfdbinary):
    seen = {}
    real = watch.wake

    def crash(text):
        seen["checks"] = outcomes(environ)
        seen["verdicts"] = len(watch.verdicts(root(environ), SESSION))
        raise OSError("the pipe broke")

    monkeypatch.setattr(watch, "wake", crash)
    deliver(environ, clock)
    emet.report(T1, 200, document())
    assert stop(session, environ) == 0
    assert seen == {"checks": [(T1, "flagged", True)], "verdicts": 1}
    assert "watch error OSError" in log(environ) and "the pipe broke" not in log(environ)
    monkeypatch.setattr(watch, "wake", real)
    assert stop(session, environ) == 0
    assert emet.polled(T1) == 1
    assert capfdbinary.readouterr() == (b"", b"")


def test_a_report_that_cannot_be_recorded_wakes_no_one(session, environ, emet, clock, capfdbinary):
    watch.checks_path(root(environ), SESSION).mkdir()
    deliver(environ, clock)
    emet.report(T1, 200, document())
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert emet.polled(T1) == 1
    assert len(watch.verdicts(root(environ), SESSION)) == 1
    assert f"report {T1} -> flagged, not recorded" in log(environ)


def test_a_verdict_that_cannot_be_saved_still_wakes_claude(session, environ, emet, clock):
    (root(environ) / "verdicts").write_text("in the way")
    deliver(environ, clock)
    emet.report(T1, 200, document())
    assert stop(session, environ) == 2
    assert "verdict not saved" in log(environ)


def test_the_same_revision_of_a_report_is_shown_once_and_a_new_one_again(environ, emet, clock):
    settings = config.load(environ)
    trace = watch.Trace(T1, clock.now, "")
    ended = watch.Stop(SESSION, "", "", 0)
    first = watch.settle(root(environ), settings, ended, trace, document())
    again = watch.settle(root(environ), settings, ended, trace, document())
    rechecked = document()
    rechecked["report"]["revision"] = 2
    third = watch.settle(root(environ), settings, ended, trace, rechecked)
    assert first and third and again == ""
    assert [item["revision"] for item in watch.checks(root(environ), SESSION)] == [1, 2]
    assert len(watch.verdicts(root(environ), SESSION)) == 2


@pytest.mark.parametrize(
    ("named", "ids"), [({}, ("", 0)), ({"uuid": "nope", "revision": True}, ("", 0)), ({"uuid": 5, "revision": -1}, ("", 0))]
)
def test_a_report_that_names_no_ids_is_keyed_by_its_trace(session, environ, emet, clock, named, ids):
    deliver(environ, clock)
    emet.report(T1, 200, document(report={"status": "flagged", **named}))
    assert stop(session, environ) == 2
    (check,) = watch.checks(root(environ), SESSION)
    assert (check["report_uuid"], check["revision"]) == ids


def test_the_saved_verdict_holds_the_rendered_text_and_ids_not_the_document(session, environ, emet, clock):
    long = [claim(n, "disputed", "A claim. " * 400, reason="A reason. " * 4000) for n in range(1, 40)]
    deliver(environ, clock, conversation_uuid=CONVERSATION)
    emet.report(T1, 200, document(long))
    assert stop(session, environ) == 2
    path = watch.verdict_path(root(environ), SESSION, T1, 1)
    verdict = json.loads(path.read_text(encoding="utf-8"))
    assert verdict == {
        "session_id": SESSION,
        "trace_uuid": T1,
        "report_uuid": REPORT,
        "revision": 1,
        "conversation_uuid": CONVERSATION,
        "endpoint": emet.url,
        "at": storage.now(clock.now),
        "handed": True,
        "text": report.missed_message(document(long), emet.url, SESSION, CONVERSATION),
    }
    assert len(verdict["text"]) <= 4000
    assert path.stat().st_size < 20_000
    assert watch.verdicts(root(environ), SESSION) == [verdict]


def test_a_turn_with_no_conversation_id_gets_a_link_that_searches_for_the_session(session, environ, emet, clock, capfdbinary):
    deliver(environ, clock, conversation_uuid=7)
    emet.report(T1, 200, document())
    assert stop(session, environ) == 2
    err = capfdbinary.readouterr()[1].decode("utf-8")
    assert f"Report: {emet.url}/conversations?q={SESSION}\n" in err + "\n"
    assert "selected=" not in err


def test_a_verdict_stays_eligible_for_seven_days_and_is_then_removed(session, environ, emet, clock):
    deliver(environ, clock)
    emet.report(T1, 200, document())
    stop(session, environ)
    path = watch.verdict_path(root(environ), SESSION, T1, 1)
    clock.advance(7 * 24 * 3600 - 60)
    assert len(watch.verdicts(root(environ), SESSION)) == 1
    finish(environ, clock)
    stop(session, environ)
    assert path.exists()
    clock.advance(120)
    assert watch.verdicts(root(environ), SESSION) == []
    assert watch.verdicts(root(environ), "../x") == []
    damaged = path.with_name(f"{SESSION}.{T2}.1.json")
    damaged.write_text("{")
    finish(environ, clock)
    stop(session, environ)
    assert not path.exists() and not damaged.exists()


def test_every_outcome_is_listed_in_the_status_file_newest_first_and_once_per_trace(session, environ, emet, clock):
    storage.write_json(root(environ) / "status.json", {"queued": 3, "checks": "damaged"})
    traces = [str(uuid.UUID(int=number)) for number in range(1, 8)]
    for trace in traces:
        deliver(environ, clock, trace)
        emet.report(trace, 202)
    stop(session, environ)
    for trace in traces:
        emet.report(trace, 404, "not_assessed")
        emet.reports[trace].pop(0)
    finish(environ, clock)
    stop(session, environ)
    listed = status(environ)["checks"]
    assert status(environ)["queued"] == 3
    assert len(listed) == 5 and len({item["trace_uuid"] for item in listed}) == 5
    assert all(item["outcome"] == "not_assessed" and item["session_id"] == SESSION for item in listed)
    assert listed[0] == {"session_id": SESSION, **watch.checks(root(environ), SESSION)[-1]}
    commands.update_status(root(environ), config.load(environ))
    assert status(environ)["checks"] == listed


def test_a_later_outcome_of_a_trace_replaces_its_earlier_one_in_the_status_file(session, environ, emet, clock):
    deliver(environ, clock)
    emet.report(T1, 202)
    stop(session, environ)
    emet.report(T1, 200, document(status="clear"))
    emet.reports[T1].pop(0)
    finish(environ, clock)
    stop(session, environ)
    assert [(item["trace_uuid"], item["outcome"]) for item in status(environ)["checks"]] == [(T1, "clear")]


def test_no_file_but_the_verdict_holds_report_text_and_none_holds_the_key(session, environ, emet, clock):
    deliver(environ, clock)
    emet.report(T1, 200, document())
    assert stop(session, environ) == 2
    verdict = watch.verdict_path(root(environ), SESSION, T1, 1)
    files = [path for path in root(environ).rglob("*") if path.is_file()]
    assert verdict in files
    for path in files:
        text = path.read_text(encoding="utf-8")
        assert API_KEY not in text and "ZyxwVuts" not in text
        assert ("March" in text) is (path == verdict)


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX permissions")
def test_the_poller_files_are_private(session, environ, emet, clock):
    deliver(environ, clock)
    emet.report(T1, 200, document())
    stop(session, environ)
    verdict = watch.verdict_path(root(environ), SESSION, T1, 1)
    assert stat.S_IMODE(verdict.stat().st_mode) == 0o600
    assert stat.S_IMODE(verdict.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(watch.checks_path(root(environ), SESSION).stat().st_mode) == 0o600


def test_an_outcome_written_after_a_line_cut_short_is_still_read(session, environ, emet, clock):
    watch.checks_path(root(environ), SESSION).write_text('{"trace_uuid": "cut sh', encoding="utf-8")
    deliver(environ, clock)
    finish(environ, clock)
    emet.report(T1, 200, document(status="clear"))
    stop(session, environ)
    assert outcomes(environ) == [(T1, "clear", True)]


def test_an_outcome_file_that_is_not_text_reads_as_empty(environ):
    watch.checks_path(root(environ), SESSION).write_bytes(b"\xff\xfe\n[1]\n")
    assert watch.checks(root(environ), SESSION) == []


def test_the_wake_text_is_written_as_utf8_bytes(monkeypatch):
    class Piped:
        """A stream that refuses text, as a piped one on Windows can."""

        def __init__(self):
            self.buffer = io.BytesIO()

        def write(self, text):
            raise UnicodeEncodeError("charmap", text, 0, 1, "no such character")

    piped = Piped()
    monkeypatch.setattr(sys, "stderr", piped)
    assert watch.wake("Emet disputed “12 %” … café") == 2
    assert piped.buffer.getvalue() == "Emet disputed “12 %” … café".encode()


def test_the_hook_entry_point_runs_the_poller_and_passes_its_exit_code(session, environ, emet, clock, monkeypatch, capfdbinary):
    import emet_hook

    for name, value in environ.items():
        monkeypatch.setenv(name, value)
    deliver(environ, clock)
    emet.report(T1, 200, document())
    monkeypatch.setattr(sys, "argv", ["emet_hook.py", "watch"])
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(session.hook_input()).encode())))
    assert emet_hook.run() == 2
    assert capfdbinary.readouterr()[1].startswith(report.WAKE_OPENING.encode())
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(json.dumps(session.hook_input()).encode())))
    assert emet_hook.run() == 0


@pytest.mark.parametrize("raw", [b"", b"{", b"[]", b"null", b'"x"', b"\xff", b'{"session_id": 5}'])
def test_input_that_is_no_hook_input_exits_0_in_silence(environ, emet, clock, capfdbinary, raw):
    start = clock.now
    assert watch.run(raw, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert emet.reads == [] and clock.now == start


def test_an_error_in_the_poller_is_logged_by_type_and_never_raised(session, environ, emet, clock, monkeypatch, capfdbinary):
    def broken(*args, **kwargs):
        raise RuntimeError("the secret plan")

    monkeypatch.setattr(watch, "follow", broken)
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")
    assert "watch error RuntimeError" in log(environ) and "secret" not in log(environ)


def test_an_error_that_cannot_be_logged_still_exits_0(session, environ, monkeypatch, capfdbinary):
    def no_folder(*args, **kwargs):
        raise PermissionError

    monkeypatch.setattr(storage, "data_dir", no_folder)
    assert stop(session, environ) == 0
    assert capfdbinary.readouterr() == (b"", b"")


@pytest.mark.parametrize("name", ["CLAUDE_PLUGIN_OPTION_EMET_KEY", "CLAUDE_PLUGIN_OPTION_TENANT_REF"])
def test_nothing_is_asked_without_configuration(session, environ, emet, clock, name):
    deliver(environ, clock)
    del environ[name]
    assert stop(session, environ) == 0
    assert emet.reads == []


def test_nothing_is_asked_of_a_host_other_than_emet(session, environ, emet, clock, monkeypatch):
    calls = []
    monkeypatch.setattr(watch, "read_report", lambda *args: calls.append(args))
    environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"] = "https://collector.example.com"
    deliver(environ, clock)
    start = clock.now
    assert stop(session, environ) == 0
    assert calls == [] and clock.now == start


@pytest.mark.parametrize("session_id", ["", "..", "../session-1", "a/b", "a\\b", "x" * 101])
def test_a_session_id_that_is_not_a_plain_name_is_not_polled(session, environ, emet, clock, session_id):
    start = clock.now
    assert stop(session, environ, session_id=session_id) == 0
    assert clock.now == start
    assert [path.name for path in (root(environ) / "state").iterdir()] == []


def test_the_process_ends_itself_below_the_hook_limit(monkeypatch):
    started = []

    class Timer:
        def __init__(self, interval, function, args=()):
            self.interval, self.function, self.args, self.daemon = interval, function, args, False

        def start(self):
            started.append(self)

        def cancel(self):
            started.append("cancelled")

    monkeypatch.setattr(watch.threading, "Timer", Timer)
    assert watch.run(b"{}", {}) == 0
    timer, cancelled = started
    assert (timer.interval, timer.function, timer.args, timer.daemon) == (320, os._exit, (0,), True)
    assert cancelled == "cancelled"
    assert watch.BUDGET_S == 300 < watch.HARD_STOP_S < 330 - watch.REQUEST_TIMEOUT_S + 1


def test_a_report_read_is_tried_once_within_ten_seconds_through_the_one_opener_that_follows_no_redirect(environ, emet, monkeypatch):
    seen = []

    def ask(request, timeout):
        seen.append((request.full_url, request.get_method(), timeout))
        raise OSError

    def no_other_opener(*args, **kwargs):
        raise AssertionError("the poller opened a request of its own")

    monkeypatch.setattr(transport, "ask", ask)
    monkeypatch.setattr(watch.urllib.request, "build_opener", no_other_opener)
    monkeypatch.setattr(watch.urllib.request, "urlopen", no_other_opener)
    assert watch.read_report(config.load(environ), T1) == watch.Answer(0)
    assert seen == [(f"{emet.url}/api/v1/traces/{T1}/report/", "GET", 10)]


def test_a_redirected_report_read_is_not_followed(environ, emet):
    emet.report(T1, 302)
    assert watch.read_report(config.load(environ), T1) == watch.Answer(302)
    assert [read["path"] for read in emet.reads] == [f"/api/v1/traces/{T1}/report/"]
