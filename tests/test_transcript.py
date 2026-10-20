import json

from emet_claude import transcript as tr


def test_reads_from_offset_and_skips_partial_last_line(transcript):
    first = transcript.prompt("hello")
    transcript.text("msg_1", "hi")
    with open(transcript.path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write('{"type": "user", "message"')
    entries, offset = tr.read_entries(transcript.path, 0)
    assert entries[0][1]["uuid"] == first["uuid"]
    assert len(entries) == 2
    assert offset < transcript.path.stat().st_size
    again, _ = tr.read_entries(transcript.path, offset)
    assert again == []


def test_restarts_when_the_file_shrank(transcript):
    transcript.prompt("hello")
    entries, _ = tr.read_entries(transcript.path, 10_000)
    assert len(entries) == 1


def test_ignores_non_message_entries_and_bad_json(transcript):
    with open(transcript.path, "a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps({"type": "summary", "summary": "x"}) + "\n")
        handle.write("not json\n")
    transcript.prompt("hello")
    entries, _ = tr.read_entries(transcript.path, 0)
    assert len(entries) == 1


def test_sidechain_entries_are_skipped_in_the_session_file(transcript):
    transcript.prompt("hello", isSidechain=True)
    assert tr.read_entries(transcript.path, 0)[0] == []
    assert len(tr.read_entries(transcript.path, 0, sidechain=True)[0]) == 1


def test_turns_start_at_prompts_not_tool_results(transcript):
    transcript.prompt("one")
    transcript.tool_use("msg_1", "toolu_1", "Bash", {"command": "ls"})
    transcript.tool_result("toolu_1", "file.txt")
    transcript.text("msg_2", "done")
    transcript.prompt("two")
    head, turns = tr.split_turns(tr.read_entries(transcript.path, 0)[0])
    assert head == []
    assert [len(turn) for turn in turns] == [4, 1]
    assert tr.answered(turns[0]) and not tr.answered(turns[1])


def test_meta_entries_do_not_start_turns(transcript):
    transcript.prompt("context", isMeta=True)
    head, turns = tr.split_turns(tr.read_entries(transcript.path, 0)[0])
    assert turns == [] and len(head) == 1


def test_prompt_text_shows_slash_commands_as_typed(transcript):
    entry = transcript.prompt(
        "<command-message>review</command-message>\n<command-name>/review</command-name>\n<command-args>123</command-args>", source=None
    )
    assert tr.prompt_text(entry) == "/review 123"


def test_by_person(transcript):
    assert tr.by_person(transcript.prompt("hi"))
    assert tr.by_person(transcript.prompt("hi", source="queued"))
    assert not tr.by_person(transcript.prompt("<task-notification>done</task-notification>", source="system"))
    assert not tr.by_person(transcript.prompt("summary", source=None, isCompactSummary=True))
    assert not tr.by_person(transcript.prompt("<local-command-stdout>Bye!</local-command-stdout>", source=None))
    assert tr.by_person(transcript.prompt("older client prompt", source=None))


def test_persisted_output_reads_the_saved_file(transcript):
    saved = transcript.saved_output("toolu_9.txt", "full output")
    part = {"type": "tool_result", "content": f"<persisted-output>\nOutput too large (50KB). Full output saved to: {saved}\n\nPreview..."}
    assert tr.persisted_output(transcript.path, part, None) == "full output"
    assert tr.persisted_output(transcript.path, {"content": "x"}, {"persistedOutputPath": str(saved)}) == "full output"


def test_persisted_output_refuses_paths_outside_the_session_folder(transcript, tmp_path):
    outside = tmp_path / "secrets.txt"
    outside.write_text("do not read")
    part = {"content": f"<persisted-output>\nFull output saved to: {outside}\n"}
    assert tr.persisted_output(transcript.path, part, {"persistedOutputPath": str(outside)}) is None
    sneaky = transcript.folder / "tool-results" / ".." / ".." / "secrets.txt"
    assert tr.persisted_output(transcript.path, {"content": ""}, {"persistedOutputPath": str(sneaky)}) is None


def test_subagents_are_found_by_launching_tool_call(transcript):
    path = transcript.subagent("a1", "toolu_agent")
    found = tr.subagents(transcript.path)
    assert found["toolu_agent"][0] == path
    assert found["toolu_agent"][1]["agentType"] == "Explore"
