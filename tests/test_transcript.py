import json
import os
from pathlib import Path

import pytest

from emet_claude import report, timestamps
from emet_claude import transcript as tr
from helpers import WAKE_WRAPPER, Transcript
from test_report import document

WAKE = WAKE_WRAPPER + report.wake_message(document(), "https://app.emet.so", "session-1")
HUMAN = {"turnOrigin": "human", "origin": {"kind": "human"}}


def woke(entry, ago=5.0):
    """Asks whether the entry starts a turn this plugin's wake started, with a report handed to Claude that long before it."""
    return tr.started_by_wake(entry, [timestamps.parse(entry["timestamp"]).timestamp() - ago])


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


@pytest.mark.parametrize("kind", ["scheduled", "peer"])
def test_a_meta_entry_of_a_scheduled_fire_or_of_a_message_from_another_session_starts_a_turn(transcript, kind):
    transcript.prompt("one")
    transcript.text("msg_1", "done")
    fired = transcript.meta_turn("check the deploy", kind)
    transcript.text("msg_2", "The deploy is up.")
    head, turns = tr.split_turns(tr.read_entries(transcript.path, 0)[0])
    assert head == [] and [len(turn) for turn in turns] == [2, 2]
    assert turns[1][0][1]["uuid"] == fired["uuid"] and tr.answered(turns[1])
    # No person typed it, no command of the plugin started it, and it is no wake
    assert not tr.by_person(fired) and tr.own_command(fired) == "" and not woke(fired)


def test_a_notice_kept_as_a_record_only_starts_no_turn_and_is_part_of_none(transcript):
    first = transcript.meta_turn("the first agent's result", "peer")
    answers = [transcript.text("msg_1", "Noted.")]
    record = transcript.record_notice()
    second = transcript.meta_turn("the second agent's result", "peer")
    answers.append(transcript.text("msg_2", "Noted too."))
    transcript.record_notice()
    assert not tr.starts_turn(record)
    head, turns = tr.split_turns(tr.read_entries(transcript.path, 0)[0])
    assert head == [] and [[entry["uuid"] for _, entry in turn] for turn in turns] == [
        [first["uuid"], answers[0]["uuid"]],
        [second["uuid"], answers[1]["uuid"]],
    ]
    assert tr.starts_turn(transcript.record_notice(queueTranscriptOnly=False)) and tr.starts_turn(
        transcript.record_notice(queueTranscriptOnly=1)
    )
    # Alone after a turn already sent, it is nothing to add to that turn
    assert tr.split_turns([(0, record)]) == ([], [])


@pytest.mark.parametrize("kind", ["scheduled", "peer"])
def test_a_meta_start_right_after_a_start_with_no_answer_yet_joins_that_turn(transcript, kind):
    notice = transcript.notice("<task-notification>agent finished</task-notification>")
    transcript.meta_turn("check the deploy", kind)
    transcript.meta_turn("and the queue", kind)
    transcript.text("msg_1", "Both are fine.")
    own = transcript.meta_turn("check again", kind)
    transcript.meta_turn("and once more", kind)
    head, turns = tr.split_turns(tr.read_entries(transcript.path, 0)[0])
    assert head == [] and [len(turn) for turn in turns] == [4, 2]
    assert [turn[0][1]["uuid"] for turn in turns] == [notice["uuid"], own["uuid"]]


@pytest.mark.parametrize(
    "fields",
    [
        {"turnOrigin": None},
        {"turnOrigin": "human"},
        {"turnOrigin": "task_notification"},
        {"turnOrigin": "sdk"},
        {"turnOrigin": "Scheduled"},
        {"turnOrigin": ["scheduled"]},
        {"turnOrigin": None, "origin": {"kind": "peer"}},
        {"promptSource": None},
        {"promptSource": "typed"},
        {"promptId": None},
        {"promptId": ""},
        {"promptId": 5},
        {"message": {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "x"}]}},
        {"message": {"role": "user", "content": []}},
    ],
)
@pytest.mark.parametrize("kind", ["scheduled", "peer"])
def test_a_meta_entry_that_is_not_wholly_one_of_the_two_known_shapes_starts_no_turn(transcript, kind, fields):
    assert tr.starts_turn(transcript.meta_turn("check the deploy", kind))
    assert not tr.starts_turn(transcript.meta_turn("check the deploy", kind, **fields))


def test_only_a_start_claude_code_names_scheduled_and_no_person_sent_is_a_turn_on_a_schedule(transcript):
    assert tr.on_schedule(transcript.meta_turn("check the deploy"))
    assert not tr.on_schedule(transcript.meta_turn("the agent's result", "peer"))
    assert not tr.on_schedule(transcript.meta_turn("check the deploy", promptSource="typed"))
    assert not tr.on_schedule(transcript.meta_turn("check the deploy", promptId=None))
    assert not tr.on_schedule(transcript.prompt("check the deploy"))


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
    assert tr.persisted_output(transcript.path, part, None) == ("full output", False)
    assert tr.persisted_output(transcript.path, {"content": "x"}, {"persistedOutputPath": str(saved)}) == ("full output", False)


def test_persisted_output_says_when_it_was_cut(transcript, monkeypatch):
    monkeypatch.setattr(tr, "PERSISTED_MAX_BYTES", 10)
    saved = transcript.saved_output("big.txt", "x" * 50)
    assert tr.persisted_output(transcript.path, {"content": ""}, {"persistedOutputPath": str(saved)}) == ("x" * 10, True)


def test_persisted_output_refuses_paths_outside_the_session_folder(transcript, tmp_path):
    outside = tmp_path / "secrets.txt"
    outside.write_text("do not read")
    part = {"content": f"<persisted-output>\nFull output saved to: {outside}\n"}
    assert tr.persisted_output(transcript.path, part, {"persistedOutputPath": str(outside)}) is None
    sneaky = transcript.folder / "tool-results" / ".." / ".." / "secrets.txt"
    assert tr.persisted_output(transcript.path, {"content": ""}, {"persistedOutputPath": str(sneaky)}) is None


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_subagent_links_out_of_the_folder_are_ignored(transcript, tmp_path):
    outside = tmp_path / "elsewhere.jsonl"
    outside.write_text("{}\n")
    path = transcript.subagent("a2", "toolu_link")
    path.unlink()
    path.symlink_to(outside)
    assert "toolu_link" not in tr.subagents(transcript.path)


def test_images_in_messages_leave_a_placeholder(transcript):
    entry = transcript.write(
        {
            "type": "user",
            "message": {
                "role": "user",
                "content": [
                    {"type": "text", "text": "what is this"},
                    {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}},
                ],
            },
        }
    )
    assert tr.text_of(entry) == "what is this\n[image content not shown]"
    assert tr.has_binary(entry)


def test_subagents_are_found_by_launching_tool_call(transcript):
    path = transcript.subagent("a1", "toolu_agent")
    found = tr.subagents(transcript.path)
    assert found["toolu_agent"][0] == path
    assert found["toolu_agent"][1]["agentType"] == "Explore"


def test_the_turn_this_plugins_wake_started_is_recognised(transcript):
    assert woke(transcript.notice(WAKE))


@pytest.mark.parametrize("fields", [{"origin": None}, {"turnOrigin": None}, {"origin": "task-notification"}])
def test_either_origin_field_names_a_notice_turn(transcript, fields):
    assert woke(transcript.notice(WAKE, **fields))


def test_another_task_notification_is_ordinary_work(transcript):
    done = "<task-notification><status>completed</status><summary>Background command finished</summary></task-notification>"
    assert not woke(transcript.notice(done))
    # The data block alone is in every saved verdict, which a background command can print
    assert not woke(transcript.notice(done + WAKE[WAKE.index(report.OPEN) :]))


@pytest.mark.parametrize("source", ["typed", "queued", "suggestion_accepted"])
def test_a_person_who_quotes_the_wake_stays_a_person(transcript, source):
    assert not woke(transcript.prompt(f"What does this mean?\n{WAKE}", source=source, **HUMAN))
    assert not woke(transcript.prompt(WAKE, source=source))
    # One field naming a person is enough, whatever the other says
    assert not woke(transcript.notice(WAKE, promptSource=source))
    assert not woke(transcript.notice(WAKE, turnOrigin="human"))
    assert not woke(transcript.notice(WAKE, origin={"kind": "human"}))


@pytest.mark.parametrize("fields", [{"origin": {"kind": "user"}}, {"turnOrigin": "sdk"}, {"origin": {"kind": 7}}, {"turnOrigin": ""}])
def test_an_origin_field_that_names_anything_but_a_notice_keeps_the_turn_ordinary(transcript, fields):
    assert not woke(transcript.notice(WAKE, **fields))


def test_a_wake_claude_code_cut_short_or_wrapped_again_is_still_recognised(transcript):
    text = report.wake_message(document(), "https://app.emet.so", "session-1")
    cut = WAKE[: len(WAKE_WRAPPER) + len(report.WAKE_OPENING) + 40] + "... [truncated]"
    assert woke(transcript.notice(cut))
    assert woke(transcript.notice(f"<task-notification>\n<stderr>{text}</stderr>\n</task-notification>"))
    assert woke(transcript.notice("A hook said:\n" + text.replace(" ", "\n")))
    assert woke(transcript.notice([{"type": "text", "text": WAKE_WRAPPER}, {"type": "text", "text": text}]))
    # Cut inside the opening sentence, nothing of the plugin's is left to know it by
    assert not woke(transcript.notice(WAKE[: len(WAKE_WRAPPER) + 30]))


@pytest.mark.parametrize("source", ["system", None])
def test_without_the_origin_fields_no_turn_is_taken_for_a_wake(transcript, source):
    # An older Claude Code writes neither field, and the text alone proves nothing
    assert not woke(transcript.prompt(WAKE, source=source))
    assert not woke(transcript.prompt(WAKE, source=source, origin={}, turnOrigin=""))


def test_a_persons_turn_that_carries_the_wake_as_a_queued_command_stays_a_normal_turn(transcript):
    prompt = transcript.prompt("And what about April?", **HUMAN)
    transcript.write({"type": "attachment", "attachment": {"type": "queued_command", "prompt": WAKE, "commandMode": "task-notification"}})
    transcript.text("msg_1", "April was flat.")
    (turn,) = tr.split_turns(tr.read_entries(transcript.path, 0)[0])[1]
    assert turn[0][1]["uuid"] == prompt["uuid"]
    assert not any(woke(entry) for _, entry in turn)


def test_only_the_start_of_a_long_prompt_is_searched(transcript, monkeypatch):
    monkeypatch.setattr(tr, "WAKE_SCAN", len(WAKE))
    assert woke(transcript.notice(WAKE))
    assert not woke(transcript.notice("x" + WAKE[: len(WAKE_WRAPPER)] + " " * len(WAKE) + WAKE[len(WAKE_WRAPPER) :]))


def test_a_notice_turn_with_the_sentence_is_ours_only_shortly_after_the_poller_handed_a_report_over(transcript):
    entry = transcript.notice(WAKE)
    start = timestamps.parse(entry["timestamp"]).timestamp()
    # A background command can print the sentence, and no id links a wake to its turn
    assert not tr.started_by_wake(entry)
    assert not tr.started_by_wake(entry, [])
    assert tr.started_by_wake(entry, [start])
    assert tr.started_by_wake(entry, [start - tr.WAKE_WINDOW_S])
    assert not tr.started_by_wake(entry, [start - tr.WAKE_WINDOW_S - 1])
    assert not tr.started_by_wake(entry, [start + 1])
    assert tr.started_by_wake(entry, [start + 1, start - 3 * tr.WAKE_WINDOW_S, start - 14])


@pytest.mark.parametrize("stamp", [None, "", "soon", "2026-10-20T10:00:00"])
def test_a_notice_turn_with_no_readable_time_is_never_taken_for_a_wake(transcript, stamp):
    entry = transcript.notice(WAKE, timestamp=stamp)
    assert not tr.started_by_wake(entry, [timestamps.parse("2026-10-20T10:00:00.000Z").timestamp()])


def test_only_a_file_of_the_sessions_own_subagents_folder_is_its_subagent_file(transcript, tmp_path):
    inside = transcript.subagent("a1", "toolu_agent")
    assert tr.own_subagent_file(transcript.path, inside)
    assert tr.own_subagent_file(transcript.path, inside.parent / ".." / "subagents" / inside.name)
    assert tr.own_subagent_file(transcript.path, inside.with_name("agent-not-written-yet.jsonl"))
    saved = transcript.saved_output("out.txt", "x")
    other = Transcript(transcript.path.parent, "session-2").subagent("a1", "toolu_agent")
    for path in (saved, other, transcript.path, tmp_path / "agent-a1.jsonl", inside.parent, Path(""), Path("a\x00b")):
        assert not tr.own_subagent_file(transcript.path, path), path


@pytest.mark.skipif(os.name == "nt", reason="symlinks need privileges on Windows")
def test_a_link_is_never_a_subagent_file_even_inside_the_folder(transcript):
    inside = transcript.subagent("a1", "toolu_agent")
    link = inside.with_name("agent-a2.jsonl")
    link.symlink_to(inside)
    loop = inside.with_name("agent-a3.jsonl")
    loop.symlink_to(loop)
    assert not tr.own_subagent_file(transcript.path, link)
    assert not tr.own_subagent_file(transcript.path, loop)
    # A loop in a folder above the file, which Python before 3.13 raises on
    assert not tr.own_subagent_file(transcript.path, loop / "agent-a4.jsonl")


PLUGIN = Path(tr.__file__).resolve().parent.parent.parent
PLUGIN_NAME = json.loads((PLUGIN / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))["name"]
SKILLS = sorted(path.name for path in (PLUGIN / "skills").iterdir() if (path / "SKILL.md").is_file())
OWN = [f"{PLUGIN_NAME}:{skill}" for skill in SKILLS]
STATUS = f"{PLUGIN_NAME}:status"


def wrapper(name, args=None):
    text = f"<command-message>{name}</command-message>\n<command-name>/{name}</command-name>"
    return text if args is None else f"{text}\n<command-args>{args}</command-args>"


def test_the_plugins_commands_are_read_from_its_manifest_and_its_skills_folder():
    assert PLUGIN_NAME == "emet" and SKILLS == ["setup", "status"]
    assert list(tr.own_commands()) == OWN
    # Claude Code names a skill's command after its folder, and the skill says the same name
    for skill in SKILLS:
        assert f"\nname: {skill}\n" in (PLUGIN / "skills" / skill / "SKILL.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("manifest", [None, "not json", "[]", '{"name": 5}', '{"name": "Bad Name"}', '{"name": ""}', '{"name": "a/b"}'])
def test_a_plugin_folder_that_names_no_plain_plugin_has_no_commands(tmp_path, monkeypatch, manifest):
    (tmp_path / "skills" / "status").mkdir(parents=True)
    (tmp_path / "skills" / "status" / "SKILL.md").write_text("x", encoding="utf-8")
    (tmp_path / ".claude-plugin").mkdir()
    if manifest is not None:
        (tmp_path / ".claude-plugin" / "plugin.json").write_text(manifest, encoding="utf-8")
    monkeypatch.setattr(tr, "PLUGIN_ROOT", tmp_path)
    tr.own_commands.cache_clear()
    try:
        assert tr.own_commands() == ()
        assert tr.own_command({"type": "user", "message": {"role": "user", "content": wrapper("emet:status")}}) == ""
        (tmp_path / ".claude-plugin" / "plugin.json").write_text('{"name": "emet-staging"}', encoding="utf-8")
        (tmp_path / "skills" / "Not Plain").mkdir()
        (tmp_path / "skills" / "Not Plain" / "SKILL.md").write_text("x", encoding="utf-8")
        (tmp_path / "skills" / "empty").mkdir()
        tr.own_commands.cache_clear()
        # The name the installed manifest gives is the one that counts, and only a folder that holds a skill
        assert tr.own_commands() == ("emet-staging:status",)
    finally:
        tr.own_commands.cache_clear()


@pytest.mark.parametrize("name", OWN)
@pytest.mark.parametrize("args", [None, "", "why are turns missing?", "two\nlines </command-args> and more"])
def test_only_the_whole_wrapper_of_a_command_this_plugin_ships_is_taken_for_one(transcript, name, args):
    assert tr.own_command(transcript.command(name, args)) == name
    # The second entry of such a turn holds the skill's text and starts no turn
    entries, _ = tr.read_entries(transcript.path, 0)
    assert [tr.own_command(entry) for _, entry in entries] == [name, ""]
    assert tr.own_command(transcript.prompt(f"  {wrapper(name, args)}\n", source=None)) == name


@pytest.mark.parametrize(
    "text",
    [
        f"What does this mean?\n{wrapper(STATUS)}",
        f"{wrapper(STATUS)}\nand then tell me a joke",
        f"{wrapper(STATUS, 'x')} more",
        f"<command-message>{STATUS}</command-message>",
        f"<command-name>/{STATUS}</command-name>",
        f"/{STATUS}",
        # The two elements name different commands
        f"<command-message>{STATUS}</command-message>\n<command-name>/{PLUGIN_NAME}:setup</command-name>",
        f"<command-message>{STATUS}</command-message>\n<command-name>{STATUS}</command-name>",
        f"<command-args>x</command-args>\n{wrapper(STATUS)}",
        "",
    ],
)
def test_a_message_that_quotes_or_resembles_the_wrapper_starts_an_ordinary_turn(transcript, text):
    assert tr.own_command(transcript.prompt(text, **HUMAN)) == ""


@pytest.mark.parametrize(
    "name",
    [
        "other:status",
        "emet-staging:status",
        "emetx:status",
        "status",
        ":status",
        "emet:",
        "emet:review",
        "emet:status:status",
        "EMET:status",
        "x:emet:status",
    ],
)
def test_a_command_of_another_plugin_or_under_another_plugin_name_starts_an_ordinary_turn(transcript, name):
    assert name not in OWN
    assert tr.own_command(transcript.command(name)) == ""
    assert tr.own_command(transcript.command(name, "args")) == ""


@pytest.mark.parametrize(
    "fields",
    [
        {"turnOrigin": "task_notification"},
        {"origin": {"kind": "task-notification"}},
        {"origin": "human"},
        {"origin": {"kind": 5}},
        {"turnOrigin": "sdk"},
        {"promptSource": "system"},
        {"promptSource": "sdk"},
        {"isMeta": True},
        {"isSidechain": True},
        {"isCompactSummary": True},
        {"type": "assistant"},
    ],
)
def test_a_wrapper_in_an_entry_that_names_another_origin_than_a_person_starts_an_ordinary_turn(transcript, fields):
    assert tr.own_command(transcript.command(STATUS, **fields)) == ""


@pytest.mark.parametrize(
    "fields", [{}, {"promptSource": "typed"}, {"promptSource": "queued"}, {"turnOrigin": None, "origin": None}, {"origin": {}}]
)
def test_a_wrapper_in_an_entry_of_a_person_or_one_that_names_no_origin_is_a_command(transcript, fields):
    assert tr.own_command(transcript.command(STATUS, **fields)) == STATUS


@pytest.mark.parametrize(
    "content",
    [
        [{"type": "text", "text": wrapper(STATUS)}, {"type": "text", "text": "and more"}],
        [{"type": "text", "text": wrapper(STATUS)}, {"type": "image", "source": {}}],
        [{"type": "tool_result", "tool_use_id": "t", "content": wrapper(STATUS)}],
        [{"type": "text", "text": None}],
        [],
        wrapper(STATUS, "x" * tr.WAKE_SCAN),
    ],
    # Named, since Windows cannot hold a test id this long in its environment
    ids=["text after it", "an image after it", "inside a tool result", "text that is none", "no parts", "arguments too long"],
)
def test_a_prompt_that_is_more_than_the_one_wrapper_text_starts_an_ordinary_turn(transcript, content):
    entry = transcript.write({"type": "user", "message": {"role": "user", "content": content}, **HUMAN})
    assert tr.own_command(entry) == ""
    assert tr.own_command(
        transcript.write({"type": "user", "message": {"role": "user", "content": [{"type": "text", "text": wrapper(STATUS)}]}})
    )
