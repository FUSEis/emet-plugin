import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import API_KEY
from emet_claude import commands, storage, transport


def run(transcript, environ):
    commands.process(transcript.hook_input(), environ, wait=0)


def status(environ):
    return json.loads((Path(environ["CLAUDE_PLUGIN_DATA"]) / "status.json").read_text())


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


def test_unreachable_endpoint_queues_and_retries_in_order(transcript, environ, emet):
    emet.statuses = [503]
    first = finished_turn(transcript)
    second = finished_turn(transcript, "next", "ok")
    run(transcript, environ)
    # One failed attempt; the second turn went straight to the queue
    assert len(emet.requests) == 1
    assert status(environ)["queued"] == 2
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


def test_the_queue_is_bounded(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "QUEUE_MAX_FILES", 3)
    root = storage.data_dir({"CLAUDE_PLUGIN_DATA": str(tmp_path)})
    for index in range(5):
        transport.enqueue(root, {"trace_id": f"t{index}", "spans": [{"span_id": "turn"}]})
    assert len(list((root / "queue").glob("*.json"))) == 3


def test_does_nothing_without_configuration(transcript, environ, emet):
    finished_turn(transcript)
    environ.pop("CLAUDE_PLUGIN_OPTION_API_KEY")
    run(transcript, environ)
    assert emet.requests == []


def test_refuses_a_plain_http_endpoint(transcript, environ, emet):
    finished_turn(transcript)
    environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"] = "http://emet.example.com"
    run(transcript, environ)
    assert emet.requests == []


def test_key_check_sends_no_trace(environ, emet):
    emet.statuses = [400]
    commands.check_key(storage.data_dir(environ), commands.load(environ))
    assert emet.envelopes == [{}]
    assert status(environ)["key_check"] == "accepted"
    emet.statuses = [403]
    commands.check_key(storage.data_dir(environ), commands.load(environ))
    assert status(environ)["key_check"] == "rejected"


def test_status_and_log_never_hold_the_key_or_content(transcript, environ, emet):
    finished_turn(transcript, "my secret plan", "the secret answer")
    emet.statuses = [500]
    run(transcript, environ)
    root = Path(environ["CLAUDE_PLUGIN_DATA"])
    for name in ("status.json", "emet.log"):
        text = (root / name).read_text()
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
    log = (Path(environ["CLAUDE_PLUGIN_DATA"]) / "emet.log").read_text()
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


def test_hook_mode_without_configuration_only_records_the_problem(monkeypatch, environ):
    environ.pop("CLAUDE_PLUGIN_OPTION_API_KEY")
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_API_KEY", raising=False)
    spawned = []
    monkeypatch.setattr(commands, "spawn_worker", lambda mode, raw: spawned.append(mode))
    hook_stdin(monkeypatch, environ, b"{}")
    assert commands.main(["stop"]) == 0
    assert spawned == []
    assert status(environ)["problem"] == "No Emet key is set."


def test_session_start_checks_the_key_then_sends(transcript, environ, emet):
    emet.statuses = [400]
    finished_turn(transcript)
    commands.run_worker("session-start", json.dumps(transcript.hook_input()).encode(), environ)
    assert emet.envelopes[0] == {}
    assert status(environ)["key_check"] == "accepted"
    assert len(emet.envelopes) == 2
