import base64
import json

import pytest

from emet_claude import envelope
from emet_claude import transcript as tr
from emet_claude.config import Config
from emet_claude.spans import Builder, Capture

CONFIG = Config("Abcd1234.ZyxwVuts98765432QwertyUiop123456", "engineering", "https://app.emet.so", "sanitized")


def build(transcript, mode="sanitized"):
    config = Config(CONFIG.emet_key, CONFIG.tenant_ref, CONFIG.endpoint, mode)
    builder = Builder(transcript.path, Capture(mode, secrets=(config.emet_key,)))
    _, turns = tr.split_turns(tr.read_entries(transcript.path, 0)[0])
    return [envelope.for_turn(config, builder, transcript.session_id, turn, "/work/acme-app") for turn in turns]


def spans_of(result):
    return [span for env in result[1] for span in env["spans"]]


def by_kind(spans, kind):
    return [span for span in spans if span["kind"] == kind]


def simple_turn(transcript):
    prompt = transcript.prompt("How many files are there?")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "ls | wc -l"})
    transcript.tool_result("toolu_1", "forty-two files counted")
    transcript.text("msg_2", "There are 42 files.")
    return prompt


def test_one_turn_becomes_one_trace(transcript):
    prompt = simple_turn(transcript)
    (result,) = build(transcript)
    trace_id, envelopes, _ = result
    assert trace_id == prompt["uuid"]
    assert len(envelopes) == 1
    env = envelopes[0]
    assert env["tenant_ref"] == "engineering"
    assert env["conversation_id"] == transcript.session_id
    assert env["conversation_title"] == "How many files are there?"
    assert env["ingestion_mode"] == "sdk" and env["end"] is True
    assert env["resource"]["vcs.repository.name"] == "acme-app"
    assert env["resource"]["emet.capture_mode"] == "sanitized"
    kinds = [span["kind"] for span in env["spans"]]
    assert kinds.count("turn") == 1 and kinds.count("tool") == 1 and kinds.count("model") == 2


def test_tool_span_carries_arguments_and_the_result_the_model_saw(transcript):
    transcript.prompt("edit it")
    transcript.tool_use("msg_1", "toolu_1", "Edit", {"file_path": "a.py", "old_string": "x", "new_string": "y"})
    # toolUseResult holds Claude Code's own record, including the whole original file
    transcript.tool_result(
        "toolu_1", "The file a.py has been updated.", tool_use_result={"originalFile": "SECRET ORIGINAL FILE", "filePath": "a.py"}
    )
    transcript.text("msg_2", "Done.")
    (tool,) = by_kind(spans_of(build(transcript)[0]), "tool")
    assert tool["span_id"] == "tool.toolu_1"
    assert tool["tool_name"] == "Edit" and tool["tool_call_id"] == "toolu_1"
    assert tool["payload"]["arguments"]["new_string"] == "y"
    assert tool["payload"]["result"] == "The file a.py has been updated."
    assert "SECRET ORIGINAL FILE" not in json.dumps(tool)


def test_failed_tool_call_is_marked_error(transcript):
    transcript.prompt("run it")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "make"})
    transcript.tool_result("toolu_1", "make: *** No rule", is_error=True)
    transcript.text("msg_2", "The build failed.")
    (tool,) = by_kind(spans_of(build(transcript)[0]), "tool")
    assert tool["status"] == "error"
    assert tool["error_message"] == "make: *** No rule"


def test_usage_is_counted_once_per_api_response(transcript):
    transcript.prompt("hi")
    usage = {
        "input_tokens": 7,
        "output_tokens": 3,
        "cache_read_input_tokens": 50,
        "cache_creation_input_tokens": 9,
        "cache_creation": {"ephemeral_1h_input_tokens": 4},
        "output_tokens_details": {"thinking_tokens": 2},
    }
    # One response streamed as two entries sharing message.id, each repeating the usage
    transcript.assistant("msg_1", [{"type": "thinking", "thinking": "..."}], usage=usage)
    transcript.assistant("msg_1", [{"type": "text", "text": "Hello"}], usage=usage, stop_reason="end_turn")
    (model,) = by_kind(spans_of(build(transcript)[0]), "model")
    assert model["span_id"] == "model.msg_1" and model["response_id"] == "msg_1"
    assert (model["input_tokens"], model["output_tokens"]) == (7, 3)
    assert (model["cache_read_tokens"], model["cache_write_tokens"], model["cache_write_1h_tokens"], model["reasoning_tokens"]) == (
        50,
        9,
        4,
        2,
    )
    assert model["provider"] == "anthropic" and model["finish_reason"] == "end_turn"


def test_final_output_is_the_last_call_with_text(transcript):
    simple_turn(transcript)
    models = by_kind(spans_of(build(transcript)[0]), "model")
    finals = [model["span_id"] for model in models if model["is_final_output"]]
    assert finals == ["model.msg_2"]


def test_model_inputs_reference_tool_results_without_repeating_them(transcript):
    simple_turn(transcript)
    second = next(m for m in by_kind(spans_of(build(transcript)[0]), "model") if m["span_id"] == "model.msg_2")
    inputs = second["payload"]["input_messages"]
    assert inputs[0]["parts"][0]["content"] == "How many files are there?"
    assert inputs[1]["parts"] == [{"type": "tool_call_response", "id": "toolu_1"}]


def test_sanitized_mode_redacts_secrets_in_tool_results(transcript):
    transcript.prompt("show env")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "cat .env"})
    transcript.tool_result("toolu_1", "GITHUB_TOKEN=ghp_" + "a" * 36 + "\nEMET=Abcd1234.ZyxwVuts98765432QwertyUiop123456")
    transcript.text("msg_2", "Your token is ghp_" + "a" * 36)
    spans = spans_of(build(transcript)[0])
    (tool,) = by_kind(spans, "tool")
    assert "ghp_" not in tool["payload"]["result"] and "Abcd1234." not in tool["payload"]["result"]
    assert tool["attributes"]["emet.redactions"] >= 2
    assert "ghp_" not in json.dumps(spans)


def test_first_model_call_starts_when_the_prompt_was_sent(transcript):
    prompt = simple_turn(transcript)
    spans = spans_of(build(transcript)[0])
    first = next(span for span in spans if span["span_id"] == "model.msg_1")
    assert first["started_at"] == prompt["timestamp"] < first["ended_at"]


def test_redactions_are_counted_where_they_happen(transcript):
    token = "ghp_" + "b" * 36
    transcript.prompt(f"is {token} still valid?")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": f"curl -H 'Authorization: token {token}' https://api.github.com/user"})
    transcript.tool_result("toolu_1", "401")
    transcript.text("msg_2", f"No, {token} was rejected.")
    spans = spans_of(build(transcript)[0])
    count = {span["span_id"]: span.get("attributes", {}).get("emet.redactions", 0) for span in spans}
    # Each secret is counted once: the prompt on the turn, the command on its tool span, the answer on its model call
    assert count == {"turn": 1, "model.msg_1": 0, "tool.toolu_1": 1, "model.msg_2": 1}
    assert token not in json.dumps(spans)


@pytest.mark.parametrize("mode", ["full", "metadata"])
def test_only_sanitized_mode_reports_redactions(transcript, mode):
    token = "ghp_" + "b" * 36
    transcript.prompt(f"is {token} still valid?")
    transcript.text("msg_1", f"No, {token} was rejected.")
    spans = spans_of(build(transcript, mode)[0])
    assert all("emet.redactions" not in span.get("attributes", {}) for span in spans)


def test_full_mode_sends_content_as_is(transcript):
    transcript.prompt("show env")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "cat .env"})
    transcript.tool_result("toolu_1", "GITHUB_TOKEN=ghp_" + "a" * 36)
    transcript.text("msg_2", "ok")
    (tool,) = by_kind(spans_of(build(transcript, "full")[0]), "tool")
    assert "ghp_" in tool["payload"]["result"]


def test_metadata_mode_sends_no_content(transcript):
    simple_turn(transcript)
    _, envelopes, _ = build(transcript, "metadata")[0]
    env = envelopes[0]
    assert "conversation_title" not in env
    for span in env["spans"]:
        assert not span.get("payload")
    model = by_kind(env["spans"], "model")[0]
    assert model["input_tokens"] == 10 and model["model_name"] == "claude-opus-5-5"
    assert "files counted" not in json.dumps(env) and "How many" not in json.dumps(env)


def test_images_become_placeholders_and_mark_the_span_truncated(transcript):
    data = base64.b64encode(b"z" * 5000).decode()
    transcript.prompt("look")
    transcript.tool_use("msg_1", "toolu_1", "screenshot", {})
    transcript.tool_result(
        "toolu_1",
        [{"type": "text", "text": "Captured"}, {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": data}}],
    )
    transcript.text("msg_2", "I see a login page.")
    (tool,) = by_kind(spans_of(build(transcript)[0]), "tool")
    assert tool["is_truncated"] is True
    assert data not in json.dumps(tool)
    assert "image content not shown" in json.dumps(tool["payload"]["result"])


def test_an_image_the_user_attached_marks_the_model_span_truncated(transcript):
    transcript.write(
        {
            "type": "user",
            "promptSource": "typed",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                ],
            },
        }
    )
    transcript.text("msg_1", "A login page.")
    (model,) = by_kind(spans_of(build(transcript)[0]), "model")
    assert model["is_truncated"] is True
    assert model["payload"]["input_messages"][0]["parts"][0]["content"] == "what is this\n[image content not shown]"


def test_a_cut_saved_output_marks_the_tool_span_truncated(transcript, monkeypatch):
    monkeypatch.setattr(tr, "PERSISTED_MAX_BYTES", 100)
    saved = transcript.saved_output("cut.txt", "y" * 1000)
    transcript.prompt("big")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "cat big"})
    transcript.tool_result("toolu_1", "preview", tool_use_result={"persistedOutputPath": str(saved)})
    transcript.text("msg_2", "done")
    (tool,) = by_kind(spans_of(build(transcript)[0]), "tool")
    assert tool["is_truncated"] is True and len(tool["payload"]["result"]) == 100


def test_lone_surrogates_do_not_abort_a_turn(transcript):
    transcript.prompt("odd \ud800 text")
    transcript.text("msg_1", "ok")
    _, envelopes, _ = build(transcript)[0]
    json.dumps(envelopes, ensure_ascii=False).encode("utf-8")


def test_saved_outputs_are_sent_in_full(transcript):
    saved = transcript.saved_output("b1.txt", "line\n" * 5000)
    transcript.prompt("big")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "cat big"})
    transcript.tool_result(
        "toolu_1",
        f"<persisted-output>\nOutput too large. Full output saved to: {saved}\n\nPreview (first 2KB):\nline",
        tool_use_result={"stdout": "line\n" * 400, "persistedOutputPath": str(saved)},
    )
    transcript.text("msg_2", "5000 lines")
    (tool,) = by_kind(spans_of(build(transcript)[0]), "tool")
    assert tool["payload"]["result"] == "line\n" * 5000
    assert tool["attributes"]["claude_code.saved_output"] is True


def test_subagent_spans_nest_under_the_launching_call(transcript):
    transcript.prompt("research it")
    transcript.tool_use("msg_1", "toolu_agent", "Agent", {"description": "look", "prompt": "Find the config"})
    agent_path = transcript.subagent("a1", "toolu_agent")
    transcript.write({"type": "user", "isSidechain": True, "message": {"role": "user", "content": "Find the config"}}, agent_path)
    transcript.tool_use("msg_s1", "toolu_s1", "Grep", {"pattern": "config"}, path=agent_path, isSidechain=True)
    transcript.tool_result("toolu_s1", "settings.py", path=agent_path, isSidechain=True)
    transcript.text("msg_s2", "It is in settings.py", path=agent_path, isSidechain=True)
    transcript.tool_result("toolu_agent", "It is in settings.py")
    transcript.text("msg_2", "The config is in settings.py.")
    _, envelopes, built = build(transcript)[0]
    spans = [span for env in envelopes for span in env["spans"]]
    (agent,) = by_kind(spans, "agent")
    assert agent["span_id"] == "agent.a1" and agent["parent_span_id"] == "tool.toolu_agent"
    assert agent["name"] == "invoke_agent Explore"
    inner = [span for span in spans if span.get("parent_span_id") == "agent.a1"]
    assert {span["span_id"] for span in inner} == {"tool.toolu_s1", "model.msg_s1", "model.msg_s2"}
    assert not any(span["is_final_output"] for span in inner if span["kind"] == "model")
    first_inner = next(span for span in inner if span["span_id"] == "model.msg_s1")
    assert first_inner["payload"]["input_messages"][0]["parts"][0]["content"] == "Find the config"
    # The subagent's first call starts when its prompt was written, as does the agent span
    assert first_inner["started_at"] == agent["started_at"] < first_inner["ended_at"]
    assert built.agents[0].agent_id == "a1" and built.agents[0].offset == agent_path.stat().st_size


def test_large_turns_split_into_batches_and_only_the_last_ends(transcript, monkeypatch):
    monkeypatch.setattr(envelope, "MAX_SPANS_PER_BATCH", 3)
    transcript.prompt("many")
    for index in range(4):
        transcript.tool_use(f"msg_{index}", f"toolu_{index}", "Read", {"file_path": f"{index}.py"})
        transcript.tool_result(f"toolu_{index}", "x")
    transcript.text("msg_end", "read 4 files")
    _, envelopes, _ = build(transcript)[0]
    assert len(envelopes) > 1
    assert [env["end"] for env in envelopes] == [False] * (len(envelopes) - 1) + [True]
    assert all(len(env["spans"]) <= 3 for env in envelopes)
    assert "conversation_title" in envelopes[0] and all("conversation_title" not in env for env in envelopes[1:])
    assert len({env["trace_id"] for env in envelopes}) == 1


def test_oversized_tool_results_are_clipped_and_marked(transcript, monkeypatch):
    monkeypatch.setattr(envelope, "MAX_RESULT_CHARS", 100)
    transcript.prompt("dump")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "dump"})
    transcript.tool_result("toolu_1", "q" * 1000)
    transcript.text("msg_2", "done")
    (tool,) = by_kind(spans_of(build(transcript)[0]), "tool")
    assert tool["is_truncated"] is True
    assert len(tool["payload"]["result"]) < 200 and "characters not shown" in tool["payload"]["result"]


def test_span_ids_fit_emet_limits(transcript):
    transcript.prompt("hi")
    transcript.text("msg_" + "x" * 200, "hello")
    for span in spans_of(build(transcript)[0]):
        assert len(span["span_id"]) <= 64


def test_title_comes_only_from_a_person(transcript):
    transcript.prompt("<task-notification>agent finished</task-notification>", source="system")
    transcript.text("msg_1", "The agent finished.")
    _, envelopes, _ = build(transcript)[0]
    assert "conversation_title" not in envelopes[0]
    assert envelopes[0]["spans"][0]["attributes"]["claude_code.prompt_source"] == "system"


@pytest.mark.parametrize("mode", ["sanitized", "full", "metadata"])
def test_no_nul_characters_leave(transcript, mode):
    transcript.prompt("nul \x00 here")
    transcript.text("msg_1", "ok \x00")
    _, envelopes, _ = build(transcript, mode)[0]
    assert "\\u0000" not in json.dumps(envelopes)
