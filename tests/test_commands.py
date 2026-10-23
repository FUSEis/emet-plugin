import http.client
import io
import json
import os
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from conftest import API_KEY
from emet_claude import commands, config, storage, transport
from helpers import Transcript


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
    environ.pop("CLAUDE_PLUGIN_OPTION_EMET_KEY")
    run(transcript, environ)
    assert emet.requests == []


@pytest.mark.parametrize("endpoint", ["http://emet.example.com", "https://collector.example.com"])
def test_sends_nothing_to_a_host_other_than_emet(transcript, environ, emet, endpoint):
    finished_turn(transcript)
    environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"] = endpoint
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


def test_hook_mode_without_configuration_only_records_the_problem(monkeypatch, environ):
    environ.pop("CLAUDE_PLUGIN_OPTION_EMET_KEY")
    monkeypatch.delenv("CLAUDE_PLUGIN_OPTION_EMET_KEY", raising=False)
    spawned = []
    monkeypatch.setattr(commands, "spawn_worker", lambda mode, raw: spawned.append(mode))
    hook_stdin(monkeypatch, environ, b"{}")
    assert commands.main(["stop"]) == 0
    assert spawned == []
    assert status(environ)["problem"] == "No Emet key is set."


def test_session_start_checks_the_key_and_sends_only_finished_turns(transcript, environ, emet):
    emet.statuses = [400]
    earlier = finished_turn(transcript)
    finished_turn(transcript, "latest", "maybe still running")
    commands.run_worker("session-start", json.dumps(transcript.hook_input()).encode(), environ)
    assert emet.envelopes[0] == {}
    assert status(environ)["key_check"] == "accepted"
    assert [env["trace_id"] for env in emet.envelopes[1:]] == [earlier["uuid"]]


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


def test_a_queued_turn_gets_its_link_when_it_is_retried(transcript, environ, emet):
    emet.statuses = [503]
    finished_turn(transcript)
    run(transcript, environ)
    assert status(environ)["recent"] == []
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


def test_a_failed_retry_is_the_last_error(transcript, environ, emet):
    emet.statuses = [503, 429]
    finished_turn(transcript)
    run(transcript, environ)
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


def test_another_sessions_retry_gets_that_sessions_link(tmp_path, environ, emet):
    queued, current = Transcript(tmp_path, "session-0"), Transcript(tmp_path, "session-1")
    emet.statuses = [503]
    finished_turn(queued)
    run(queued, environ)
    # The other session's worker flushes the shared queue first, then sends its own turn
    finished_turn(current)
    run(current, environ)
    recent = {item["session_id"]: item for item in status(environ)["recent"]}
    assert recent["session-0"]["report_url"] == report_link(emet, 0, "session-0")
    assert recent["session-1"]["report_url"] == report_link(emet, 1, "session-1")
    assert status(environ)["recent"][0]["session_id"] == "session-1"


def test_a_run_that_delivers_and_fails_reports_both(transcript, environ, emet):
    emet.statuses = [503]
    finished_turn(transcript)
    run(transcript, environ)
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
    transcript.tool_result("toolu_env", f"CLAUDE_PLUGIN_OPTION_EMET_KEY={API_KEY}\nAWS_SECRET_ACCESS_KEY=abc")
    transcript.text("msg_2", "done")
    run(transcript, environ)
    sent = json.dumps(emet.envelopes)
    assert API_KEY not in sent
    assert "[REDACTED:emet_key]" in sent
    # Everything else is sent as is in full mode
    assert "AWS_SECRET_ACCESS_KEY=abc" in sent
