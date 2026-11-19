import json
import os
import stat
from email.utils import formatdate

import pytest

from emet_claude import commands, pause, pending, storage, timestamps, transport
from helpers import Transcript


def run(transcript, environ):
    commands.process(transcript.hook_input(), environ, wait=0)


def finished_turn(transcript, text="What is 2 + 2?", answer="4"):
    prompt = transcript.prompt(text)
    transcript.text(f"msg_{prompt['uuid'][:8]}", answer)
    return prompt


def root(environ):
    return storage.data_dir(environ)


def status(environ):
    return json.loads((root(environ) / "status.json").read_text(encoding="utf-8"))


def left(environ, clock):
    """Seconds until the pause ends, 0 when none is in force."""
    until = pause.until(root(environ))
    return timestamps.parse(until).timestamp() - clock.now if until else 0


def test_a_failed_send_starts_a_pause_of_about_thirty_seconds(transcript, environ, emet, clock):
    emet.statuses = [503]
    finished_turn(transcript)
    run(transcript, environ)
    assert left(environ, clock) == 30
    assert status(environ)["paused_until"] == pause.until(root(environ))
    assert "paused 30s" in (root(environ) / "emet.log").read_text(encoding="utf-8")


def test_while_paused_turns_are_queued_and_no_request_is_made(transcript, environ, emet, clock):
    emet.statuses = [503]
    first = finished_turn(transcript)
    run(transcript, environ)
    clock.advance(29)
    second = finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    assert len(emet.requests) == 1
    assert status(environ)["queued"] == 2
    assert status(environ)["paused_until"]
    # The run still ends with a stamp that counts what waits
    stamps = [item["queued"] for item in pending.lines(root(environ), "session-1") if "finished" in item]
    assert stamps == [1, 2]
    clock.advance(2)
    run(transcript, environ)
    assert [env["trace_id"] for env in emet.envelopes[1:]] == [first["uuid"], second["uuid"]]


def test_a_request_that_succeeds_after_the_pause_clears_it(transcript, environ, emet, clock):
    emet.statuses = [503]
    finished_turn(transcript)
    run(transcript, environ)
    clock.advance(31)
    run(transcript, environ)
    assert not pause.path(root(environ)).exists()
    assert status(environ)["paused_until"] == ""
    assert status(environ)["queued"] == 0
    # The next failure starts again from the shortest pause
    emet.statuses = [503]
    finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    assert left(environ, clock) == 30


def test_after_the_pause_one_request_is_tried_and_a_failure_doubles_it(transcript, environ, emet, clock):
    emet.statuses = [503, 503]
    finished_turn(transcript)
    finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    assert status(environ)["queued"] == 2
    clock.advance(31)
    finished_turn(transcript, "third", "ok")
    run(transcript, environ)
    assert len(emet.requests) == 2
    assert left(environ, clock) == 60
    assert status(environ)["queued"] == 3


def test_the_pause_doubles_up_to_ten_minutes(environ, clock):
    waits = []
    for _ in range(7):
        pause.extend(root(environ))
        waits.append(left(environ, clock))
        clock.advance(waits[-1] + 1)
    assert waits == [30, 60, 120, 240, 480, 600, 600]


@pytest.mark.parametrize(("spread", "first", "longest"), [(0.8, 24, 480), (1.2, 36, 600)])
def test_the_pause_is_spread_by_chance_and_never_passes_ten_minutes(environ, clock, spread, first, longest):
    clock.spread = spread
    pause.extend(root(environ))
    assert left(environ, clock) == first
    storage.write_json(pause.path(root(environ)), {"until": "", "delay": 600})
    pause.extend(root(environ))
    assert left(environ, clock) == longest


def test_the_chance_stays_within_a_fifth(monkeypatch):
    monkeypatch.undo()
    assert pause.SPREAD == (0.8, 1.2)
    assert all(0.8 <= pause.spread() <= 1.2 for _ in range(200))
    assert len({pause.spread() for _ in range(20)}) > 1


def test_the_wait_emet_names_is_used(transcript, environ, emet, clock):
    emet.statuses = [429]
    emet.retry_after = "120"
    finished_turn(transcript)
    run(transcript, environ)
    assert left(environ, clock) == 120


def test_a_wait_named_only_when_emet_refused_for_good_starts_no_pause(transcript, environ, emet, clock):
    emet.statuses = [400]
    emet.retry_after = "120"
    finished_turn(transcript)
    run(transcript, environ)
    assert left(environ, clock) == 0


def test_a_named_wait_does_not_stop_the_doubling(environ, clock):
    pause.extend(root(environ), 5.0)
    assert left(environ, clock) == 5
    clock.advance(6)
    pause.extend(root(environ))
    assert left(environ, clock) == 60


@pytest.mark.parametrize(
    ("header", "seconds"),
    [
        ("0", 0),
        ("45", 45),
        (" 45 ", 45),
        ("99999999999999999999", 3600),
        ("9" * 5000, 3600),
        ("10000", 3600),
        ("3601", 3600),
        ("1200", 1200),
        ("0000045", 45),
        ("000", 0),
        ("", None),
        (None, None),
        ("soon", None),
        ("-5", None),
        ("1.5", None),
        ("٣٠", None),
    ],
)
def test_reads_a_wait_given_in_seconds(header, seconds):
    assert pause.named(header) == seconds


def test_reads_a_wait_given_as_a_date(clock):
    assert pause.named(formatdate(clock.now + 90, usegmt=True)) == 90
    assert pause.named(formatdate(clock.now - 90, usegmt=True)) == 0
    assert pause.named(formatdate(clock.now + 90_000, usegmt=True)) == 3600
    # A date with no zone is read as UTC, as HTTP dates are
    assert pause.named("Fri, 15 Jan 2027 08:01:30 -0000") == 90
    # The first and last dates a header can name are read like any other
    assert pause.named("Fri, 31 Dec 9999 23:59:59 -2359") == 3600
    assert pause.named("Mon, 01 Jan 0001 00:00:00 +2359") == 0
    assert pause.named("Fri, 31 Dec 99999 23:59:59 GMT") is None


def test_a_wait_emet_names_is_capped_at_an_hour(environ, clock):
    pause.extend(root(environ), 90_000.0)
    assert left(environ, clock) == 3600


def test_an_endpoint_that_cannot_be_reached_starts_a_pause(transcript, environ, clock, monkeypatch):
    def unreachable(request, timeout=None):
        raise ConnectionRefusedError

    monkeypatch.setattr(transport, "ask", unreachable)
    finished_turn(transcript)
    run(transcript, environ)
    assert left(environ, clock) == 30
    assert status(environ)["queued"] == 1


@pytest.mark.parametrize("code", [400, 401, 403, 413])
def test_an_answer_that_refuses_for_good_starts_no_pause_and_ends_one(transcript, environ, emet, clock, code):
    emet.statuses = [code]
    finished_turn(transcript)
    run(transcript, environ)
    assert left(environ, clock) == 0
    emet.statuses = [503]
    finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    clock.advance(31)
    emet.statuses = [code]
    run(transcript, environ)
    assert not pause.path(root(environ)).exists()


def test_every_session_on_the_machine_shares_the_pause(tmp_path, environ, emet):
    failed, other = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    emet.statuses = [503]
    finished_turn(failed)
    run(failed, environ)
    finished_turn(other)
    run(other, environ)
    assert len(emet.requests) == 1
    assert status(environ)["queued"] == 2
    assert "paused, queued without sending" in (root(environ) / "emet.log").read_text(encoding="utf-8")


def test_a_worker_that_waited_for_the_queue_does_not_try_again(environ, emet, clock):
    """Of two workers that start once the pause is over, the second finds the pause the first one started."""
    for trace in ("a", "b"):
        transport.enqueue(root(environ), {"trace_id": trace, "spans": [], "end": True})
    first, second = (transport.Sender(root(environ), commands.load(environ)) for _ in range(2))
    emet.statuses = [503]
    assert first.flush() == 2
    assert second.flush() == 2
    assert len(emet.requests) == 1
    assert left(environ, clock) == 30


def test_a_pause_another_worker_started_holds_a_run_already_sending(environ, emet):
    sender = transport.Sender(root(environ), commands.load(environ))
    sender.send([{"trace_id": "a", "spans": [], "end": True}])
    pause.extend(root(environ))
    sender.send([{"trace_id": "b", "spans": [], "end": True}])
    assert [env["trace_id"] for env in emet.envelopes] == ["a"]
    assert len(transport.queued(root(environ))) == 1


def test_a_failure_during_a_pause_already_in_force_leaves_it(environ, clock):
    pause.extend(root(environ))
    clock.advance(10)
    pause.extend(root(environ))
    pause.extend(root(environ), 500.0)
    assert left(environ, clock) == 20
    assert pause.read(root(environ))["delay"] == 30


@pytest.mark.parametrize(
    "text",
    [
        "",
        "{not json",
        "[]",
        '"2099-01-01T00:00:00+0000"',
        "{}",
        '{"until": null}',
        '{"until": 5}',
        '{"until": "soon"}',
        '{"until": "2027-01-15T08:05:00"}',
        '{"until": "2099-01-01T00:00:00+0000"}',
        '{"until": "2001-01-01T00:00:00+0000", "delay": "x"}',
        '{"until": "", "delay": true}',
        '{"until": "", "delay": -5}',
        '{"until": "", "delay": [1]}',
    ],
)
def test_a_damaged_pause_file_is_no_pause_and_is_replaced(transcript, environ, emet, clock, text):
    pause.path(root(environ)).write_text(text, encoding="utf-8")
    assert pause.until(root(environ)) == ""
    finished_turn(transcript)
    run(transcript, environ)
    assert len(emet.envelopes) == 1
    pause.path(root(environ)).write_text(text, encoding="utf-8")
    pause.extend(root(environ))
    assert left(environ, clock) == 30


def test_a_missing_pause_file_is_no_pause(environ):
    assert pause.until(root(environ)) == ""
    pause.clear(root(environ))


def test_a_pause_file_that_cannot_be_written_never_stops_a_send(transcript, environ, emet):
    pause.path(root(environ)).mkdir()
    emet.statuses = [503]
    finished_turn(transcript)
    run(transcript, environ)
    assert status(environ)["queued"] == 1
    assert "pause not written" in (root(environ) / "emet.log").read_text(encoding="utf-8")
    run(transcript, environ)
    assert status(environ)["queued"] == 0


def test_the_pause_file_holds_one_time_in_the_form_of_the_other_files(environ, clock):
    pause.extend(root(environ))
    saved = json.loads(pause.path(root(environ)).read_text(encoding="utf-8"))
    assert saved == {"until": storage.now(clock.now + 30), "delay": 30}
    assert timestamps.parse(saved["until"]).timestamp() == clock.now + 30
    assert timestamps.parse(storage.now()) is not None


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_the_pause_file_is_private(environ):
    pause.extend(root(environ))
    assert stat.S_IMODE(pause.path(root(environ)).stat().st_mode) == 0o600


@pytest.mark.parametrize("name", [pause.SENDS, pause.READS])
def test_an_answer_does_not_end_a_pause_another_process_has_just_started(environ, clock, name):
    pause.extend(root(environ), 120.0, name)
    until = pause.until(root(environ), name)
    pause.clear(root(environ), name)
    assert pause.until(root(environ), name) == until
    clock.advance(121)
    pause.clear(root(environ), name)
    assert not pause.path(root(environ), name).exists()


def test_a_pause_is_forgotten_under_the_lock_that_starts_one(environ, clock, monkeypatch):
    taken = []
    real = storage.locked

    def named(path):
        taken.append(path.name)
        return real(path)

    pause.extend(root(environ))
    clock.advance(31)
    monkeypatch.setattr(storage, "locked", named)
    pause.clear(root(environ))
    assert taken == ["pause.lock"]
    pause.clear(root(environ))
    assert taken == ["pause.lock"]


def test_a_longer_wait_emet_names_for_the_reads_moves_the_end_of_their_pause(environ, clock):
    pause.extend(root(environ), None, pause.READS)
    until = pause.until(root(environ), pause.READS)
    assert until == storage.now(clock.now + 30)
    clock.advance(10)
    for shorter in (5.0, 20.0, None):
        pause.extend(root(environ), shorter, pause.READS)
        assert pause.until(root(environ), pause.READS) == until
    pause.extend(root(environ), 120.0, pause.READS)
    assert pause.until(root(environ), pause.READS) == storage.now(clock.now + 120)
    assert pause.read(root(environ), pause.READS)["delay"] == 30
    pause.extend(root(environ), 90_000.0, pause.READS)
    assert pause.until(root(environ), pause.READS) == storage.now(clock.now + 3600)
    assert pause.until(root(environ)) == ""
