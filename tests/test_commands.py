import email.message
import http.client
import io
import json
import os
import stat
import subprocess
import sys
import urllib.error
from pathlib import Path

import pytest

from conftest import API_KEY
from emet_claude import commands, config, envelope, pause, pending, storage, transport
from helpers import NOT_FOUND, FakeEmet, Transcript


def run(transcript, environ):
    commands.process(transcript.hook_input(), environ, wait=0)


def status(environ):
    return json.loads((Path(environ["CLAUDE_PLUGIN_DATA"]) / "status.json").read_text(encoding="utf-8"))


def finished_turn(transcript, text="What is 2 + 2?", answer="4"):
    prompt = transcript.prompt(text)
    transcript.text(f"msg_{prompt['uuid'][:8]}", answer)
    return prompt


def test_sends_each_finished_turn_once(transcript, environ, emet):
    first = finished_turn(transcript)
    run(transcript, environ)
    second = finished_turn(transcript, "And 3 + 3?", "6")
    run(transcript, environ)
    run(transcript, environ)
    assert [env["trace_id"] for env in emet.envelopes] == [first["uuid"], second["uuid"]]
    request = emet.requests[0]
    assert request["path"] == "/api/v1/traces/"
    assert request["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert request["headers"]["User-Agent"].startswith("emet-claude-code/")


def test_waits_for_an_unanswered_prompt(transcript, environ, emet):
    prompt = transcript.prompt("still thinking")
    run(transcript, environ)
    assert emet.envelopes == []
    transcript.text("msg_1", "done")
    run(transcript, environ)
    assert [env["trace_id"] for env in emet.envelopes] == [prompt["uuid"]]


def test_skips_slash_commands_that_get_no_answer(transcript, environ, emet):
    transcript.prompt("<command-name>/clear</command-name>", source=None)
    answered = finished_turn(transcript)
    run(transcript, environ)
    assert [env["trace_id"] for env in emet.envelopes] == [answered["uuid"]]


def test_late_entries_join_the_trace_already_sent(transcript, environ, emet):
    prompt = finished_turn(transcript)
    run(transcript, environ)
    # More of the same turn arrives after the hook ran, with no new prompt
    transcript.tool_use("msg_late", "toolu_late", "Bash", {"command": "echo"})
    transcript.tool_result("toolu_late", "echo")
    run(transcript, environ)
    assert [env["trace_id"] for env in emet.envelopes] == [prompt["uuid"], prompt["uuid"]]
    assert {span["span_id"] for span in emet.envelopes[1]["spans"]} == {"tool.toolu_late", "model.msg_late"}


def test_background_subagent_progress_is_sent_later(transcript, environ, emet):
    prompt = transcript.prompt("research in the background")
    transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig", "run_in_background": True})
    agent_path = transcript.subagent("bg1", "toolu_agent")
    transcript.write({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "dig"}}, agent_path)
    transcript.tool_result("toolu_agent", "Async agent launched")
    transcript.text("msg_2", "Started a background agent.")
    run(transcript, environ)
    transcript.tool_use("msg_s1", "toolu_s1", "Grep", {"pattern": "x"}, path=agent_path, isSidechain=True)
    transcript.tool_result("toolu_s1", "found x", path=agent_path, isSidechain=True)
    run(transcript, environ)
    late = emet.envelopes[-1]
    assert late["trace_id"] == prompt["uuid"]
    assert {span["span_id"] for span in late["spans"]} == {"tool.toolu_s1", "model.msg_s1"}
    assert all(span["parent_span_id"] == "agent.bg1" for span in late["spans"])


def test_unreachable_endpoint_queues_and_retries_in_order(transcript, environ, emet, clock):
    emet.statuses = [503]
    first = finished_turn(transcript)
    second = finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    # One failed attempt; the second turn went straight to the queue
    assert len(emet.requests) == 1
    assert status(environ)["queued"] == 2
    clock.advance(60)
    third = finished_turn(transcript, "third", "ok")
    run(transcript, environ)
    delivered = [env["trace_id"] for env in emet.envelopes[1:]]
    assert delivered == [first["uuid"], second["uuid"], third["uuid"]]
    assert status(environ)["queued"] == 0


@pytest.mark.parametrize("code", [400, 401, 403, 413])
def test_final_errors_are_not_retried(transcript, environ, emet, code):
    emet.statuses = [code]
    finished_turn(transcript)
    run(transcript, environ)
    run(transcript, environ)
    assert len(emet.requests) == 1
    assert status(environ)["queued"] == 0


def kept(environ):
    return pending.traces(storage.data_dir(environ), "session-1")


def state(environ):
    return json.loads((Path(environ["CLAUDE_PLUGIN_DATA"]) / "state" / "session-1.json").read_text(encoding="utf-8"))


@pytest.mark.parametrize("code", [400, 413])
def test_what_is_added_later_to_a_refused_turn_is_not_sent(transcript, environ, emet, code):
    emet.statuses = [code]
    finished_turn(transcript)
    run(transcript, environ)
    assert state(environ)["last_trace"] == ""
    transcript.text("msg_late", "one more thing")
    run(transcript, environ)
    assert len(emet.requests) == 1
    assert kept(environ) == []
    # The session goes on with the next turn
    following = finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    assert emet.envelopes[-1]["trace_id"] == following["uuid"]
    assert kept(environ) == [emet.trace_uuids[following["uuid"]]]


def test_a_turn_whose_last_batch_was_refused_is_not_followed(transcript, environ, emet, monkeypatch):
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 1)
    emet.statuses = [202, 400]
    finished_turn(transcript)
    run(transcript, environ)
    # A late batch would end the turn and keep the id of a turn Emet refused
    transcript.text("msg_late", "one more thing")
    run(transcript, environ)
    assert len(emet.requests) == 2
    assert kept(environ) == []


def test_the_batches_after_a_refused_one_are_not_sent(transcript, environ, emet, monkeypatch):
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 1)
    emet.statuses = [400]
    prompt = finished_turn(transcript)
    following = finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    assert [(env["trace_id"], env["end"]) for env in emet.envelopes] == [
        (prompt["uuid"], False),
        (following["uuid"], False),
        (following["uuid"], True),
    ]
    assert kept(environ) == [emet.trace_uuids[following["uuid"]]]
    assert f"trace {prompt['uuid']} was refused, batch not sent" in (Path(environ["CLAUDE_PLUGIN_DATA"]) / "emet.log").read_text()


def test_a_turn_queued_for_retry_is_still_followed(transcript, environ, emet, clock):
    emet.statuses = [503]
    prompt = finished_turn(transcript)
    run(transcript, environ)
    assert state(environ)["last_trace"] == prompt["uuid"]
    clock.advance(60)
    transcript.text("msg_late", "one more thing")
    run(transcript, environ)
    assert [env["trace_id"] for env in emet.envelopes] == [prompt["uuid"]] * 3
    assert kept(environ) == [emet.trace_uuids[prompt["uuid"]]]


def test_a_queued_turn_refused_on_retry_is_dropped_whole_and_no_longer_followed(transcript, environ, emet, clock, monkeypatch):
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 1)
    emet.statuses = [503]
    finished_turn(transcript)
    run(transcript, environ)
    assert status(environ)["queued"] == 2
    clock.advance(60)
    emet.statuses = [400]
    transcript.text("msg_late", "one more thing")
    run(transcript, environ)
    # The first batch was retried and refused; its second batch and the late addition were not sent
    assert len(emet.requests) == 2
    assert status(environ)["queued"] == 0
    assert state(environ)["last_trace"] == ""
    assert kept(environ) == []


def test_a_subagent_of_a_refused_turn_is_not_followed(transcript, environ, emet):
    emet.statuses = [400]
    transcript.prompt("research in the background")
    transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig", "run_in_background": True})
    agent_path = transcript.subagent("bg1", "toolu_agent")
    transcript.write({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "dig"}}, agent_path)
    transcript.tool_result("toolu_agent", "Async agent launched")
    transcript.text("msg_2", "Started a background agent.")
    run(transcript, environ)
    assert state(environ).get("agents") == {}
    transcript.tool_use("msg_s1", "toolu_s1", "Grep", {"pattern": "x"}, path=agent_path, isSidechain=True)
    transcript.tool_result("toolu_s1", "found x", path=agent_path, isSidechain=True)
    run(transcript, environ)
    assert len(emet.requests) == 1


def test_a_turn_is_no_longer_followed_once_an_addition_to_it_is_refused(transcript, environ, emet):
    prompt = finished_turn(transcript)
    run(transcript, environ)
    emet.statuses = [400]
    transcript.text("msg_late", "one more thing")
    run(transcript, environ)
    transcript.text("msg_later", "and another")
    run(transcript, environ)
    assert len(emet.requests) == 2
    # Emet has the turn itself, so its id stays
    assert kept(environ) == [emet.trace_uuids[prompt["uuid"]]]


def test_a_queued_turn_another_session_saw_refused_is_no_longer_followed_by_its_own(tmp_path, environ, emet, clock):
    owner, other = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    emet.statuses = [503]
    refused = finished_turn(owner)
    run(owner, environ)
    clock.advance(60)
    # The other session's worker retries the queued turn, and is the one Emet refuses it to
    emet.statuses = [400]
    finished_turn(other)
    run(other, environ)
    root = storage.data_dir(environ)
    assert pending.refused(root, "session-0") == {refused["uuid"]}
    assert pending.refused(root, "session-1") == set()
    owner.text("msg_late", "one more thing")
    run(owner, environ)
    assert [env["trace_id"] for env in emet.envelopes].count(refused["uuid"]) == 2
    assert status(environ)["queued"] == 0
    saved = json.loads((root / "state" / "session-0.json").read_text(encoding="utf-8"))
    assert saved["last_trace"] == ""
    assert pending.traces(root, "session-0") == []
    # The session goes on with the next turn
    following = finished_turn(owner, "next", "ok")
    run(owner, environ)
    assert emet.envelopes[-1]["trace_id"] == following["uuid"]


def test_a_subagent_of_a_turn_another_worker_saw_refused_is_not_followed(transcript, environ, emet):
    prompt = transcript.prompt("research in the background")
    transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig", "run_in_background": True})
    agent_path = transcript.subagent("bg1", "toolu_agent")
    transcript.write({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "dig"}}, agent_path)
    transcript.tool_result("toolu_agent", "Async agent launched")
    transcript.text("msg_2", "Started a background agent.")
    run(transcript, environ)
    assert "bg1" in state(environ)["agents"]
    pending.refuse(storage.data_dir(environ), "session-1", prompt["uuid"], 400, emet.url)
    transcript.tool_use("msg_s1", "toolu_s1", "Grep", {"pattern": "x"}, path=agent_path, isSidechain=True)
    transcript.tool_result("toolu_s1", "found x", path=agent_path, isSidechain=True)
    run(transcript, environ)
    assert len(emet.requests) == 1
    assert state(environ)["agents"] == {}


def queued_batch(environ, session_id, trace_id):
    transport.enqueue(storage.data_dir(environ), {"conversation_id": session_id, "trace_id": trace_id, "spans": [], "end": True})


def test_a_queued_batch_of_a_turn_refused_in_an_earlier_run_is_dropped_unsent(environ, emet):
    root = storage.data_dir(environ)
    pending.refuse(root, "session-0", "turn-a", 400, emet.url)
    queued_batch(environ, "session-0", "turn-a")
    queued_batch(environ, "session-0", "turn-b")
    assert transport.Sender(root, commands.load(environ)).flush() == 0
    assert [env["trace_id"] for env in emet.envelopes] == ["turn-b"]
    assert transport.queued(root) == []
    assert "trace turn-a was refused, queued batch dropped" in (root / "emet.log").read_text(encoding="utf-8")


def test_a_refusal_holds_for_the_session_it_was_given_to_only(environ, emet):
    """A forked session sends its copied turns under the ids the first session used."""
    root = storage.data_dir(environ)
    queued_batch(environ, "session-0", "turn-a")
    queued_batch(environ, "session-1", "turn-a")
    emet.statuses = [400]
    sender = transport.Sender(root, commands.load(environ))
    assert sender.flush() == 0
    assert [env["conversation_id"] for env in emet.envelopes] == ["session-0", "session-1"]
    assert pending.refused(root, "session-0") == {"turn-a"}
    assert pending.refused(root, "session-1") == set()
    assert sender.refused("session-1") == set()


def test_a_refusal_another_worker_writes_during_a_run_is_seen_at_once(environ, emet):
    root = storage.data_dir(environ)
    sender = transport.Sender(root, commands.load(environ))
    late = {"conversation_id": "session-0", "trace_id": "turn-a", "spans": [], "end": True}
    assert not sender.is_refused(late)
    queued_batch(environ, "session-0", "turn-a")
    # Another worker gets the refusal after this one first looked
    pending.refuse(root, "session-0", "turn-a", 400, emet.url)
    assert sender.refused("session-0") == {"turn-a"}
    sender.send([late])
    assert sender.flush() == 0
    assert emet.requests == []
    assert transport.queued(root) == []


@pytest.mark.parametrize("code", [400, 403])
def test_a_refused_turn_sent_in_batches_is_marked_once(transcript, environ, emet, monkeypatch, code):
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 1)
    emet.statuses = [code]
    finished_turn(transcript)
    run(transcript, environ)
    assert len(emet.requests) == 1
    assert len([item for item in pending.lines(storage.data_dir(environ), "session-1") if "refused" in item]) == 1


@pytest.mark.parametrize("code", [400, 401, 403, 413])
def test_a_refusal_is_never_counted_as_a_turn_that_could_not_be_read(transcript, environ, emet, code):
    emet.statuses = [code]
    prompt = finished_turn(transcript)
    run(transcript, environ)
    root = storage.data_dir(environ)
    assert "skipped_turns" not in status(environ)
    assert status(environ)["last_error"]["detail"].startswith(str(code))
    assert "turn skipped" not in (root / "emet.log").read_text(encoding="utf-8")
    marks = [item for item in pending.lines(root, "session-1") if "finished" not in item]
    assert [(item.get("refused"), item.get("status"), item.get("endpoint")) for item in marks] == [(prompt["uuid"], code, emet.url)]
    assert all(item["at"] for item in marks)


def outage(kind):
    def ask(request, timeout=None):
        if kind == "timeout":
            raise TimeoutError
        if kind == "refused":
            raise ConnectionRefusedError
        if kind == "not http":
            raise http.client.BadStatusLine("garbage")
        body = CutOff() if kind == "body cut off" else io.BytesIO(b"")
        raise urllib.error.HTTPError(request.full_url, 503, "Service Unavailable", email.message.Message(), body)

    return ask


class CutOff(io.BytesIO):
    """The body of an answer whose connection closed early."""

    def read(self, *args):
        raise http.client.IncompleteRead(b"")


@pytest.mark.parametrize("kind", ["timeout", "refused", "not http", "503", "body cut off"])
def test_an_outage_is_never_counted_as_a_turn_that_could_not_be_read(transcript, environ, clock, monkeypatch, kind):
    monkeypatch.setattr(transport, "ask", outage(kind))
    finished_turn(transcript)
    finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    root = storage.data_dir(environ)
    assert status(environ)["queued"] == 2
    assert "skipped_turns" not in status(environ)
    assert status(environ)["last_error"]
    assert pause.until(root)
    log = (root / "emet.log").read_text(encoding="utf-8")
    assert "turn skipped" not in log and "worker error" not in log
    assert [item for item in pending.lines(root, "session-1") if "finished" not in item] == []


@pytest.fixture
def elsewhere():
    """A second address, which must never be asked anything."""
    server = FakeEmet()
    yield server
    server.close()


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirected_send_is_not_followed_and_the_turn_waits(transcript, environ, emet, elsewhere, clock, code):
    emet.statuses = [code]
    emet.location = f"{elsewhere.url}/api/v1/traces/?token=secret-token"
    prompt = finished_turn(transcript)
    run(transcript, environ)
    # Nothing reached the address the answer pointed to, so the key was not sent there
    assert elsewhere.requests == [] and elsewhere.reads == []
    assert len(emet.requests) == 1
    root = storage.data_dir(environ)
    assert status(environ)["queued"] == 1
    assert pause.until(root)
    assert pending.refused(root, "session-1") == set()
    assert state(environ)["last_trace"] == prompt["uuid"]
    assert status(environ)["last_error"]["detail"] == f"{code} redirected"
    assert status(environ)["recent"] == []
    log = (root / "emet.log").read_text(encoding="utf-8")
    assert f"-> {code} redirected" in log
    for text in (log, (root / "status.json").read_text(encoding="utf-8")):
        assert "secret-token" not in text and elsewhere.url not in text and API_KEY not in text
    # Once Emet answers as itself again, the turn is delivered
    clock.advance(60)
    run(transcript, environ)
    assert [env["trace_id"] for env in emet.envelopes] == [prompt["uuid"]] * 2
    assert status(environ)["queued"] == 0
    assert elsewhere.requests == [] and elsewhere.reads == []


def test_a_redirect_that_repeats_its_address_in_the_body_keeps_it_out_of_the_log(environ, monkeypatch):
    def redirected(request, timeout=None):
        body = io.BytesIO(b'<a href="https://elsewhere.example/?token=secret-token">moved</a>')
        raise urllib.error.HTTPError(request.full_url, 302, "Found", email.message.Message(), body)

    monkeypatch.setattr(transport, "ask", redirected)
    outcome = transport.post(commands.load(environ), {})
    assert (outcome.status, outcome.delivered, outcome.final, outcome.detail) == (302, False, False, "")
    assert transport.describe(outcome) == "302 redirected"


def test_an_error_while_sending_is_not_taken_for_a_turn_that_could_not_be_built(transcript, environ, emet, monkeypatch):
    def broken(config, envelope):
        raise RuntimeError("the secret plan")

    monkeypatch.setattr(transport, "post", broken)
    prompt = finished_turn(transcript)
    commands.run_worker("stop", json.dumps(transcript.hook_input()).encode(), environ)
    root = storage.data_dir(environ)
    log = (root / "emet.log").read_text(encoding="utf-8")
    assert "worker error RuntimeError" in log and "turn skipped" not in log
    assert all("skipped" not in item for item in pending.lines(root, "session-1"))
    # The turn was not passed over: the next run sends it
    monkeypatch.undo()
    run(transcript, environ)
    assert [env["trace_id"] for env in emet.envelopes] == [prompt["uuid"]]


def test_the_queue_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "QUEUE_MAX_FILES", 3)
    root = storage.data_dir({"CLAUDE_PLUGIN_DATA": str(tmp_path)})
    for index in range(5):
        transport.enqueue(root, {"trace_id": f"t{index}", "spans": [{"span_id": "turn"}]})
    assert len(list((root / "queue").glob("*.json"))) == 3


def test_does_nothing_without_configuration(transcript, environ, emet):
    finished_turn(transcript)
    environ.pop("CLAUDE_PLUGIN_OPTION_EMET_KEY")
    run(transcript, environ)
    assert emet.requests == []


@pytest.mark.parametrize("endpoint", ["http://emet.example.com", "https://collector.example.com"])
def test_sends_nothing_to_a_host_other_than_emet(transcript, environ, emet, endpoint):
    finished_turn(transcript)
    environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"] = endpoint
    run(transcript, environ)
    assert emet.requests == []


def check(environ):
    commands.check_key(storage.data_dir(environ), commands.load(environ))
    return status(environ)["key_check"]


def test_key_check_reads_a_report_and_sends_no_trace(environ, emet):
    assert check(environ) == "accepted"
    assert emet.requests == []
    (read,) = emet.reads
    assert read["path"] == f"/api/v1/traces/{transport.PLACEHOLDER_TRACE}/report/"
    assert read["headers"]["Authorization"] == f"Bearer {API_KEY}"
    assert read["headers"]["User-Agent"].startswith("emet-claude-code/")
    assert status(environ)["key_checked_at"]


@pytest.mark.parametrize("code", [401, 403])
def test_key_check_reports_a_refused_key(environ, emet, code):
    emet.read_replies = [(code, "application/json", b'{"code": "not_found"}')]
    assert check(environ) == "rejected"


@pytest.mark.parametrize(
    ("content_type", "body"),
    [
        ("text/html", b"<html>Not Found</html>"),
        ("application/json", b""),
        ("application/json", b"[]"),
        ("application/json", b'"not_found"'),
        ("application/json", b'{"detail": "Not found."}'),
        ("application/json", b'{"code": "not_assessed"}'),
        ("application/json", b'{"code": ["not_found"]}'),
        ("application/json", b"\xff\xfe"),
        # Named, since Windows cannot hold a test id this long in its environment
        pytest.param("application/json", b"[" * 100_000, id="deeply nested json"),
    ],
)
def test_a_404_that_is_not_emets_own_is_never_read_as_accepted(environ, emet, content_type, body):
    emet.read_replies = [(404, content_type, body)]
    assert check(environ) == "unexpected 404"


@pytest.mark.parametrize("code", [200, 202, 400, 429, 500, 503])
def test_key_check_reports_any_other_answer_as_unexpected(environ, emet, code):
    emet.read_replies = [(code, "application/json", NOT_FOUND)]
    assert check(environ) == f"unexpected {code}"
    assert len(emet.reads) == 1


def test_key_check_follows_no_redirect(environ, emet):
    emet.read_replies = [(302, "text/html", b"")]
    assert check(environ) == "unexpected 302"
    assert len(emet.reads) == 1


def test_key_check_is_tried_once_within_five_seconds(environ, emet, monkeypatch):
    waits = []

    class Unreachable:
        def open(self, request, timeout=None):
            waits.append(timeout)
            raise TimeoutError

    monkeypatch.setattr(transport.urllib.request, "build_opener", lambda *handlers: Unreachable())
    assert check(environ) == "unreachable"
    assert waits == [5]
    assert not pause.until(storage.data_dir(environ))


def test_key_check_is_not_asked_during_a_pause(environ, emet):
    root = storage.data_dir(environ)
    pause.extend(root)
    commands.check_key(root, commands.load(environ))
    assert emet.reads == []
    assert not (root / "status.json").exists()
    assert "key check skipped, paused" in (root / "emet.log").read_text(encoding="utf-8")


def test_key_check_asks_no_host_other_than_emet(environ, emet, monkeypatch):
    asked = []
    monkeypatch.setattr(transport, "check_key", lambda config: asked.append(config.endpoint) or "accepted")
    environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"] = "https://emet.example.com"
    commands.run_worker("session-start", b"{}", environ)
    assert asked == []
    assert not (storage.data_dir(environ) / "status.json").exists()


def test_status_and_log_never_hold_the_key_or_content(transcript, environ, emet):
    finished_turn(transcript, "my secret plan", "the secret answer")
    emet.statuses = [500]
    run(transcript, environ)
    root = Path(environ["CLAUDE_PLUGIN_DATA"])
    for name in ("status.json", "emet.log"):
        text = (root / name).read_text(encoding="utf-8")
        assert API_KEY not in text and "ZyxwVuts" not in text
        assert "secret" not in text
    assert status(environ)["key_hint"] == "Abcd1234…"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_private_files_are_owner_only(transcript, environ, emet):
    emet.statuses = [500]
    finished_turn(transcript)
    run(transcript, environ)
    root = Path(environ["CLAUDE_PLUGIN_DATA"])
    for path in [root / "status.json", root / "emet.log", *(root / "queue").glob("*.json"), *(root / "state").glob("*.json")]:
        assert stat.S_IMODE(path.stat().st_mode) == 0o600, path
    assert stat.S_IMODE((root / "queue").stat().st_mode) == 0o700


def test_hook_returns_at_once_and_the_worker_delivers(transcript, environ, emet):
    finished_turn(transcript)
    script = Path(commands.__file__).resolve().parent.parent / "emet_hook.py"
    env = {**os.environ, **environ}
    result = subprocess.run([sys.executable, str(script), "stop"], input=json.dumps(transcript.hook_input()).encode(), env=env, timeout=10)
    assert result.returncode == 0
    # The detached worker waits briefly for the transcript to flush, then sends
    import time

    deadline = time.time() + 10
    while not emet.envelopes and time.time() < deadline:
        time.sleep(0.1)
    assert len(emet.envelopes) == 1


def test_unknown_mode_is_ignored():
    assert commands.main(["something-else"]) == 0


def test_worker_errors_are_logged_not_raised(environ):
    commands.run_worker("stop", b"not json", environ)
    log = (Path(environ["CLAUDE_PLUGIN_DATA"]) / "emet.log").read_text(encoding="utf-8")
    assert "worker error JSONDecodeError" in log


def hook_stdin(monkeypatch, environ, raw):
    for key, value in environ.items():
        monkeypatch.setenv(key, value)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(raw)))


def test_hook_mode_hands_off_to_the_worker(monkeypatch, environ):
    spawned = []
    monkeypatch.setattr(commands, "spawn_worker", lambda mode, raw: spawned.append((mode, raw)))
    hook_stdin(monkeypatch, environ, b'{"session_id": "s"}')
    assert commands.main(["stop"]) == 0
    assert spawned == [("stop", b'{"session_id": "s"}')]


class DeadPipe:
    """The stdin of a worker that died while starting."""

    def __init__(self, fails_on):
        self.fails_on = fails_on

    def write(self, raw):
        if self.fails_on == "write":
            raise BrokenPipeError

    def close(self):
        if self.fails_on == "close":
            raise BrokenPipeError


def worker_that_fails_on(step):
    def start(*args, **kwargs):
        if step == "start":
            raise FileNotFoundError("/secret/path/python3")
        return type("Worker", (), {"stdin": DeadPipe(step)})()

    return start


@pytest.mark.parametrize(("step", "error"), [("start", "FileNotFoundError"), ("write", "BrokenPipeError"), ("close", "BrokenPipeError")])
def test_a_worker_that_fails_to_start_never_breaks_the_hook(monkeypatch, environ, capfd, step, error):
    monkeypatch.setattr(subprocess, "Popen", worker_that_fails_on(step))
    hook_stdin(monkeypatch, environ, b'{"session_id": "s"}')
    assert commands.main(["stop"]) == 0
    assert capfd.readouterr() == ("", "")
    log = (Path(environ["CLAUDE_PLUGIN_DATA"]) / "emet.log").read_text(encoding="utf-8")
    assert f"worker start failed {error}" in log
    assert "secret" not in log


def test_a_failed_start_that_cannot_be_logged_still_exits_0(monkeypatch, environ, capfd):
    monkeypatch.setattr(subprocess, "Popen", worker_that_fails_on("start"))
    hook_stdin(monkeypatch, environ, b"{}")

    def no_folder(*args, **kwargs):
        raise PermissionError

    monkeypatch.setattr(storage, "data_dir", no_folder)
    assert commands.main(["stop"]) == 0
    assert capfd.readouterr() == ("", "")


def test_the_entry_point_exits_0_and_prints_nothing_whatever_fails(monkeypatch, capfd):
    import emet_hook

    def broken(*args, **kwargs):
        raise RuntimeError("the secret plan")

    monkeypatch.setattr(commands, "main", broken)
    assert emet_hook.run() == 0
    assert capfd.readouterr() == ("", "")


def test_the_entry_point_script_exits_0_when_the_plugin_cannot_be_loaded(tmp_path):
    script = Path(commands.__file__).resolve().parent.parent / "emet_hook.py"
    copy = tmp_path / "emet_hook.py"
    copy.write_bytes(script.read_bytes())
    # Next to the copy there is no emet_claude package to import
    result = subprocess.run([sys.executable, "-I", str(copy), "stop"], input=b"{}", capture_output=True, timeout=10)
    assert (result.returncode, result.stdout, result.stderr) == (0, b"", b"")


def test_hook_mode_without_configuration_only_records_the_problem(monkeypatch, environ):
    environ.pop("CLAUDE_PLUGIN_OPTION_EMET_KEY")
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_EMET_KEY", raising=False)
    spawned = []
    monkeypatch.setattr(commands, "spawn_worker", lambda mode, raw: spawned.append(mode))
    hook_stdin(monkeypatch, environ, b"{}")
    assert commands.main(["stop"]) == 0
    assert spawned == []
    assert status(environ)["problem"] == "No Emet key is set."


@pytest.fixture
def no_wait(monkeypatch):
    """The worker waits for the transcript to flush; these tests write it first."""
    monkeypatch.setattr(commands.time, "sleep", lambda seconds: None)


def session_start(transcript, environ, **extra):
    hook_input = dict(transcript.hook_input(), hook_event_name="SessionStart", **extra)
    commands.run_worker("session-start", json.dumps(hook_input).encode(), environ)


def test_session_start_checks_the_key_and_sends_only_finished_turns(transcript, environ, emet, no_wait):
    earlier = finished_turn(transcript)
    finished_turn(transcript, "latest", "maybe still running")
    session_start(transcript, environ, source="startup")
    assert len(emet.reads) == 1
    assert status(environ)["key_check"] == "accepted"
    assert [env["trace_id"] for env in emet.envelopes] == [earlier["uuid"]]


@pytest.mark.parametrize("source", ["startup", "resume", "fork", None, "something new"])
def test_the_key_is_checked_when_a_session_starts_resumes_or_forks(transcript, environ, emet, source, no_wait):
    session_start(transcript, environ, **({"source": source} if source else {}))
    assert len(emet.reads) == 1


@pytest.mark.parametrize("source", ["clear", "compact"])
def test_the_key_is_not_checked_after_clear_or_compaction(transcript, environ, emet, source, no_wait):
    earlier = finished_turn(transcript)
    finished_turn(transcript, "latest", "maybe still running")
    session_start(transcript, environ, source=source)
    assert emet.reads == []
    assert "key_check" not in status(environ)
    # The turns are still sent
    assert [env["trace_id"] for env in emet.envelopes] == [earlier["uuid"]]


def test_a_session_start_with_input_that_is_no_object_still_checks_the_key(environ, emet, no_wait):
    commands.run_worker("session-start", b"[]", environ)
    assert len(emet.reads) == 1


def test_subagent_stop_does_not_send_the_turn_still_running(transcript, environ, emet):
    prompt = transcript.prompt("research it")
    transcript.tool_use("msg_1", "toolu_agent", "Agent", {"prompt": "dig"})
    agent_path = transcript.subagent("s1", "toolu_agent")
    transcript.write({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "dig"}}, agent_path)
    transcript.text("msg_s1", "found it", path=agent_path, isSidechain=True)
    # The subagent finished; the parent turn has a tool call but no answer yet
    commands.run_worker("subagent-stop", json.dumps(transcript.hook_input()).encode(), environ)
    assert emet.envelopes == []
    transcript.tool_result("toolu_agent", "found it")
    transcript.text("msg_2", "Here is what the agent found.")
    commands.run_worker("stop", json.dumps(transcript.hook_input()).encode(), environ)
    assert [env["trace_id"] for env in emet.envelopes] == [prompt["uuid"]]
    finals = [span["span_id"] for span in emet.spans() if span.get("is_final_output")]
    assert finals == ["model.msg_2"]


def test_queue_keeps_the_order_turns_were_queued(tmp_path):
    root = storage.data_dir({"CLAUDE_PLUGIN_DATA": str(tmp_path)})
    for index in range(20):
        transport.enqueue(root, {"trace_id": f"t{index:02d}", "spans": [{"span_id": "turn"}]})
    order = [storage.read_json(path, {})["trace_id"] for path in transport.queued(root)]
    assert order == [f"t{index:02d}" for index in range(20)]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permissions")
def test_existing_data_folders_are_made_private(tmp_path):
    (tmp_path / "queue").mkdir(mode=0o755)
    tmp_path.chmod(0o755)
    storage.data_dir({"CLAUDE_PLUGIN_DATA": str(tmp_path)})
    assert stat.S_IMODE(tmp_path.stat().st_mode) == 0o700
    assert stat.S_IMODE((tmp_path / "queue").stat().st_mode) == 0o700


def sign_in(environ, account):
    """Writes the account Claude Code would keep in its config folder."""
    folder = Path(environ["CLAUDE_CONFIG_DIR"])
    folder.mkdir(exist_ok=True)
    (folder / ".claude.json").write_text(json.dumps({"oauthAccount": account}), encoding="utf-8")


def report_link(emet, index, session_id="session-1"):
    return f"{emet.url}/conversations?q={session_id}&selected={emet.conversations[index]}"


def test_sends_the_claude_account_email_as_the_person(transcript, environ, emet):
    sign_in(environ, {"emailAddress": " ada@example.com "})
    finished_turn(transcript)
    run(transcript, environ)
    assert emet.envelopes[0]["enduser_ref"] == "ada@example.com"
    assert status(environ)["enduser_source"] == config.FROM_CLAUDE_ACCOUNT


def test_the_email_setting_wins_over_the_claude_account(transcript, environ, emet):
    sign_in(environ, {"emailAddress": "personal@example.com"})
    environ["CLAUDE_PLUGIN_OPTION_EMET_EMAIL"] = "ada@work.example.com"
    finished_turn(transcript)
    run(transcript, environ)
    assert emet.envelopes[0]["enduser_ref"] == "ada@work.example.com"
    assert status(environ)["enduser_source"] == config.FROM_SETTING


@pytest.mark.parametrize("account", [None, {}, {"emailAddress": "not an email"}, "ada@example.com"])
def test_sends_no_person_without_a_signed_in_account(transcript, environ, emet, account):
    sign_in(environ, account)
    finished_turn(transcript)
    run(transcript, environ)
    assert emet.envelopes[0]["enduser_ref"] == ""
    assert status(environ)["enduser_note"] == "no signed-in Claude account"


def test_sends_no_person_without_an_account_file_and_logs_nothing(transcript, environ, emet):
    finished_turn(transcript)
    run(transcript, environ)
    assert emet.envelopes[0]["enduser_ref"] == ""
    assert status(environ)["enduser_note"] == "no Claude account file"
    assert "sign-in" not in (Path(environ["CLAUDE_PLUGIN_DATA"]) / "emet.log").read_text(encoding="utf-8")


@pytest.mark.parametrize("text", ["{not json", "[]"])
def test_an_unreadable_account_file_sends_no_person_and_says_why(transcript, environ, emet, text):
    folder = Path(environ["CLAUDE_CONFIG_DIR"])
    folder.mkdir()
    (folder / ".claude.json").write_text(text, encoding="utf-8")
    finished_turn(transcript)
    run(transcript, environ)
    assert emet.envelopes[0]["enduser_ref"] == ""
    assert status(environ)["enduser_note"] == "Claude account file unreadable"
    log = (Path(environ["CLAUDE_PLUGIN_DATA"]) / "emet.log").read_text(encoding="utf-8")
    assert "sign-in email unreadable" in log


def test_status_links_the_latest_report_of_each_session(transcript, environ, emet):
    finished_turn(transcript)
    run(transcript, environ)
    assert status(environ)["last_delivery"]["report_url"] == report_link(emet, 0)
    finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    (recent,) = status(environ)["recent"]
    assert recent["session_id"] == transcript.session_id
    assert recent["repository"] == "acme-app"
    assert recent["report_url"] == report_link(emet, 1)


def test_status_keeps_the_last_five_sessions(tmp_path, environ, emet):
    for index in range(7):
        session = Transcript(tmp_path, f"session-{index}")
        finished_turn(session)
        run(session, environ)
    sessions = [item["session_id"] for item in status(environ)["recent"]]
    assert sessions == ["session-6", "session-5", "session-4", "session-3", "session-2"]


def test_a_run_that_sends_nothing_keeps_the_newest_link_first(tmp_path, environ, emet):
    older, newer = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    for session in (older, newer):
        finished_turn(session)
        run(session, environ)
    # The older session runs again with nothing new to send
    run(older, environ)
    assert [item["session_id"] for item in status(environ)["recent"]] == ["session-1", "session-0"]
    assert status(environ)["last_delivery"]["report_url"] == report_link(emet, 1)


def test_a_queued_turn_gets_its_link_when_it_is_retried(transcript, environ, emet, clock):
    emet.statuses = [503]
    finished_turn(transcript)
    run(transcript, environ)
    assert status(environ)["recent"] == []
    clock.advance(60)
    # Nothing new to send: only the retry delivers
    run(transcript, environ)
    (recent,) = status(environ)["recent"]
    assert recent["session_id"] == transcript.session_id
    assert recent["report_url"] == report_link(emet, 0)


def test_a_long_reply_still_gives_the_link(transcript, environ, emet):
    # A metadata-mode batch of 500 tool calls, each listed twice
    emet.gaps = [f"tool.toolu_{index:024d}" for index in range(500)]
    finished_turn(transcript)
    run(transcript, environ)
    assert status(environ)["last_delivery"]["report_url"] == report_link(emet, 0)


def test_a_reply_without_a_conversation_keeps_the_sessions_earlier_link(transcript, environ, emet):
    finished_turn(transcript)
    run(transcript, environ)
    emet.reply = "not-a-uuid"
    finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    assert status(environ)["last_delivery"]["report_url"] == report_link(emet, 0)
    assert status(environ)["queued"] == 0


def test_a_reply_without_a_conversation_still_counts_as_delivered(transcript, environ, emet):
    emet.reply = "not-a-uuid"
    finished_turn(transcript)
    run(transcript, environ)
    assert status(environ)["last_delivery"]["report_url"] == ""
    assert status(environ)["queued"] == 0


def test_a_cut_off_reply_still_counts_as_delivered(transcript, environ, emet, monkeypatch):
    def cut_off(self, amount=None):
        raise http.client.IncompleteRead(b"{")

    monkeypatch.setattr(http.client.HTTPResponse, "read", cut_off)
    finished_turn(transcript)
    run(transcript, environ)
    run(transcript, environ)
    assert len(emet.requests) == 1
    assert status(environ)["queued"] == 0
    assert status(environ)["last_delivery"]["status"] == 202
    assert status(environ)["last_delivery"]["report_url"] == ""


def test_a_rejected_key_is_the_last_error(transcript, environ, emet):
    emet.statuses = [401]
    finished_turn(transcript)
    run(transcript, environ)
    assert status(environ)["last_error"]["detail"] == "401 key rejected"
    assert "last_delivery" not in status(environ)


def test_a_failed_retry_is_the_last_error(transcript, environ, emet, clock):
    emet.statuses = [503, 429]
    finished_turn(transcript)
    run(transcript, environ)
    clock.advance(60)
    # Only the retry can answer 429
    run(transcript, environ)
    assert len(emet.requests) == 2
    assert status(environ)["last_error"]["detail"].startswith("429")
    assert status(environ)["queued"] == 1


@pytest.mark.parametrize(
    "body",
    [b"", b"[]", b"null", b'"x"', b'{"conversation": null}', b'{"conversation": {}}', b"\xff\xfe", b'{"conversation": "nope"}'],
)
def test_a_reply_without_a_valid_conversation_gives_no_id(body):
    assert transport.accepted_conversation(body) == ""


def test_the_conversation_id_is_normalized():
    raw = "0F0E6C6E4A8E4D479B4C3C9F2F1D8A11"
    assert transport.accepted_conversation(json.dumps({"conversation": raw}).encode()) == "0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11"


def test_reads_the_account_file_in_the_home_folder_by_default(transcript, environ, emet, tmp_path, monkeypatch):
    environ.pop("CLAUDE_CONFIG_DIR")
    monkeypatch.setattr(Path, "home", lambda: tmp_path / "home")
    sign_in({"CLAUDE_CONFIG_DIR": str(tmp_path / "home")}, {"emailAddress": "ada@example.com"})
    finished_turn(transcript)
    run(transcript, environ)
    assert emet.envelopes[0]["enduser_ref"] == "ada@example.com"


def test_the_report_link_searches_for_the_session():
    from urllib.parse import parse_qs, urlsplit

    link = commands.report_url(config.load({}), "a session&id", "0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11")
    parts = urlsplit(link)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "app.emet.so", "/conversations")
    assert parse_qs(parts.query) == {"q": ["a session&id"], "selected": ["0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11"]}
    assert commands.report_url(config.load({}), "s", "") == ""


def test_a_session_that_delivers_again_moves_first_once(tmp_path, environ, emet):
    first, second = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    for session in (first, second):
        finished_turn(session)
        run(session, environ)
    finished_turn(first, "again", "ok")
    run(first, environ)
    recent = status(environ)["recent"]
    assert [item["session_id"] for item in recent] == ["session-0", "session-1"]
    assert recent[0]["report_url"] == report_link(emet, 2, "session-0")


def test_another_sessions_retry_gets_that_sessions_link(tmp_path, environ, emet, clock):
    queued, current = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    emet.statuses = [503]
    finished_turn(queued)
    run(queued, environ)
    clock.advance(60)
    # The other session's worker flushes the shared queue first, then sends its own turn
    finished_turn(current)
    run(current, environ)
    recent = {item["session_id"]: item for item in status(environ)["recent"]}
    assert recent["session-0"]["report_url"] == report_link(emet, 0, "session-0")
    assert recent["session-1"]["report_url"] == report_link(emet, 1, "session-1")
    assert status(environ)["recent"][0]["session_id"] == "session-1"


def test_a_run_that_delivers_and_fails_reports_both(transcript, environ, emet, clock):
    emet.statuses = [503]
    finished_turn(transcript)
    run(transcript, environ)
    clock.advance(60)
    # The retry is accepted, then the new turn is refused for good
    emet.statuses = [202, 400]
    finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    assert status(environ)["last_delivery"]["report_url"] == report_link(emet, 0)
    assert status(environ)["last_error"]["detail"].startswith("400")


def test_a_deeply_nested_reply_gives_no_id():
    assert transport.accepted_conversation(b"[" * 100_000) == ""


def test_full_mode_still_never_sends_the_emet_key(transcript, environ, emet):
    environ["CLAUDE_PLUGIN_OPTION_CAPTURE_MODE"] = "full"
    transcript.prompt("print the environment")
    transcript.tool_use("msg_1", "toolu_env", "Bash", {"command": "env"})
    # The administrator's key reaches commands even when the hook sends with one the developer saved
    other_key = "Zyxw9876.Abcdefghijklmnopqrstuvwxyz123456"
    transcript.tool_result("toolu_env", f"CLAUDE_PLUGIN_OPTION_EMET_KEY={API_KEY}\nOLD={other_key}\nAWS_SECRET_ACCESS_KEY=abc")
    transcript.text("msg_2", "done")
    run(transcript, environ)
    sent = json.dumps(emet.envelopes)
    assert API_KEY not in sent and other_key not in sent
    assert "[REDACTED:emet_key]" in sent
    # Everything else is sent as is in full mode
    assert "AWS_SECRET_ACCESS_KEY=abc" in sent
    tool = next(span for span in emet.spans() if span["span_id"] == "tool.toolu_env")
    assert tool["attributes"]["emet.redactions"] == 2


def test_a_long_folder_name_is_cut_in_status(transcript, environ, emet):
    finished_turn(transcript)
    commands.process(dict(transcript.hook_input(), cwd="/work/" + "a" * 300), environ, wait=0)
    assert len(status(environ)["recent"][0]["repository"]) == transport.REPOSITORY_MAX


@pytest.mark.parametrize("damaged", [["x", 3, None], 3, True, "x"])
def test_a_damaged_recent_list_is_replaced(transcript, environ, emet, damaged):
    root = Path(environ["CLAUDE_PLUGIN_DATA"])
    root.mkdir(parents=True, exist_ok=True)
    (root / "status.json").write_text(json.dumps({"recent": damaged}), encoding="utf-8")
    finished_turn(transcript)
    run(transcript, environ)
    (recent,) = status(environ)["recent"]
    assert recent["session_id"] == transcript.session_id
