# Contributing

## Development

The plugin runs on the Python standard library alone, so it works on any machine with `python3` 3.9 or later. The tests and linters use [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run mypy
claude plugin validate --strict ./claude && claude plugin validate --strict .
```

To try a change in a real session, load the plugin from your checkout and give it options through the environment that Claude Code would otherwise set. `CLAUDE_PLUGIN_OPTION_ENDPOINT` is not a plugin option; it points the hook at an Emet on this machine for development, and the hook refuses any other host:

```bash
CLAUDE_PLUGIN_OPTION_EMET_KEY=... CLAUDE_PLUGIN_OPTION_TENANT_REF=... \
CLAUDE_PLUGIN_OPTION_ENDPOINT=http://localhost:8000 \
claude --plugin-dir ./claude
```

The hook log is in the plugin's data folder (`~/.claude/plugins/data/emet-inline/emet.log` with `--plugin-dir`).

To keep that session apart from the Claude Code you use every day, with its own home, its own key and a pinned Claude Code version, run it in the container described in [dev/README.md](dev/README.md).

## Rules for changes

- **No runtime dependencies.** Anything outside the standard library would have to be installed on every developer machine.
- **Python 3.9 compatible.** It is what macOS ships. CI runs the tests on 3.9 and on the latest release.
- **Never block the session.** The four hooks that send return at once and leave their network work to a detached worker. The report check is a second `Stop` hook that Claude Code runs in the background; it makes its own requests and ends itself. Every failure in either is logged, not raised. The hook on the prompt is the one hook the session waits for, and it has [rules of its own](#prompt-hook-rules).
- **Never log content or the key.** The log holds ids, counts and HTTP statuses only; the status file also holds report links and the email sent. A test checks this.
- **Say when content is cut.** Any tool result that is not sent complete is marked `is_truncated`, which Emet needs to avoid verifying a claim against partial evidence.
- **Tests use synthetic transcripts** built in `tests/helpers.py`. Never commit a real session transcript; they hold private content.
- A change to what is sent, to the transcript parsing, or to what a user sees needs a test and a changelog entry, written as a new file in `changelog.d/` (see [Changelog entries](#changelog-entries)). CI holds part of this: a pull request that changes anything under `claude/scripts/`, `claude/hooks/`, `claude/skills/` or `claude/.claude-plugin/` and adds no such file fails the `changelog` job. A release passes too, because it adds a version heading. The job does not read what the entry says, and it does not look at `claude/README.md`, `docs/` or the tests. When the change alters nothing a user sees and nothing Emet receives, such as a refactor, a comment or a type, add the `no-changelog` label in place of an entry. The job runs again when a label is added or removed.
- A change to the words Claude reads about a report (`report.py`) posts its before and after eval table in the pull request. Both are made from the branch, the before one with `--plugin-scripts` naming a checkout of `main`. See [evals/README.md](evals/README.md).

## Changelog entries

Do not edit `CHANGELOG.md` in a pull request. Add one new file in `changelog.d/` in its place. Every pull request used to add a line under **Unreleased**, so open pull requests conflicted with each other after each merge. Two new files never conflict.

Name the file `<slug>.<kind>.md`. The slug is a few words for the change, in lower case with hyphens. The kind is one of `added`, `changed`, `deprecated`, `removed`, `fixed`, `security`, and it names the heading the entry goes under. The file holds the entry's text. For example, `changelog.d/skip-unreadable-turn.fixed.md`:

```markdown
A turn that cannot be read no longer stops the session from being sent. It is skipped and written to the log as `turn skipped`.
```

A plain paragraph becomes one list item; start lines with `- ` to write several. The `changelog` job fails on a file in `changelog.d/` with another kind, another ending, a heading or no text. A pull request opened before this rule that adds its line under **Unreleased** still passes. The release folds the files into `CHANGELOG.md` and deletes them. [changelog.d/README.md](changelog.d/README.md) has the details.

## Wake-up rules

The report check (`watch.py` and `report.py`) is the part of the plugin that can wake Claude. `hooks/hooks.json` starts it at every `Stop` as a background hook (`async` and `asyncRewake`), and when it exits with code 2 Claude Code hands what it wrote to stderr to Claude. A change to it keeps the six rules below. Each rule names the tests that fail when it is broken; they are in `tests/test_watch.py` unless another file is named.

1. **The hook never blocks the session.** The process ends itself after 320 seconds, below the 330 its hook is given, and starts no request after 300. It never waits for the session's watcher lock, and never takes the sender's session lock or the queue lock. It does not run at all where it could not run in the background or no person would see the wake. The status and pause locks are still taken with a wait, each for one small write. Claude Code enforces no timeout on a background hook, so the process's own end is the limit that counts. The hook entry keeps both flags and the 330 seconds, and no other event starts the check.
   - In `tests/test_manifests.py`: `test_the_report_check_runs_only_at_stop_in_the_background_with_the_time_the_poller_needs`
   - In `tests/test_process.py`, which runs the command as `hooks.json` spells it: `test_the_command_under_test_is_the_one_the_shipped_hook_names`, `test_the_process_ends_itself_while_a_request_hangs`
   - `test_the_process_ends_itself_below_the_hook_limit`
   - `test_the_poller_ends_itself_inside_the_time_its_hook_is_given`
   - `test_no_request_starts_after_five_minutes_however_slow_the_ones_before_it`
   - `test_the_poller_never_takes_the_session_lock_or_the_queue_lock`
   - `test_a_stop_that_finds_a_watcher_waits_without_the_lock_and_takes_over_when_it_leaves`
   - `test_a_waiting_stop_leaves_within_its_own_five_minutes_when_the_watcher_never_does`
   - `test_nothing_happens_on_a_claude_code_older_than_2_1_287`
   - `test_a_session_with_no_person_at_it_is_not_polled`
   - The `no_wait_for_the_watcher_lock` fixture fails any test in which the watcher lock is taken with a wait.
2. **Exit code 2 comes from one place, after the record.** Only `watch.run` returns it, through `watch.wake`, and only once the flagged outcome is in the checks file. A crash after that cannot wake Claude twice. The verdict is saved before the record, and a verdict that cannot be saved does not stop the wake. Every other path, errors included, exits 0 and prints nothing.
   - `test_the_report_is_recorded_and_saved_before_claude_is_woken`
   - `test_a_report_that_cannot_be_recorded_wakes_no_one`
   - `test_a_verdict_says_it_was_handed_over_only_after_that_is_recorded`
   - `test_a_report_that_is_not_flagged_never_wakes_claude`
   - `test_the_hook_entry_point_runs_the_poller_and_passes_its_exit_code`
   - `test_an_error_in_the_poller_is_logged_by_type_and_never_raised`
   - `test_input_that_is_no_hook_input_exits_0_in_silence`
3. **No wake chains.** The turn Claude takes after a wake is sent to Emet, and its report is never read. The same holds for a turn one of the plugin's own commands started (`/emet:status`, `/emet:setup`): its answer lists the plugin's files, and a report on it would wake Claude about the plugin itself. Such a turn is told by its whole prompt, which must be Claude Code's wrapper of a command the plugin ships under the name in its `plugin.json`. Unlike the turn after a wake, it holds back the report of the turn before it, since a person started it. A Stop that a hook started is not polled. One revision of a report wakes Claude once, and a session gets three wakes at most. A report that is ready more than 60 seconds after its turn ended, or after a newer turn began, is kept and wakes no one. The report of a turn whose prompt carried a saved verdict is read and recorded, and Claude is never told of it (rule 4 of the [prompt hook](#prompt-hook-rules)).
   - `test_a_wake_starts_a_turn_that_is_sent_and_never_wakes_claude_again`
   - `test_a_turn_a_hook_started_is_not_polled`
   - `test_the_turn_after_a_wake_is_never_polled_and_is_recorded_as_its_own_outcome`
   - `test_the_stop_that_takes_over_after_a_wake_never_wakes_claude_for_the_woken_turn`
   - `test_a_turn_a_command_of_the_plugin_started_is_never_polled_and_is_recorded_as_its_own_outcome`
   - `test_a_command_of_the_plugin_starts_a_turn_that_is_sent_unchanged_and_never_wakes_claude`
   - `test_a_command_turn_holds_back_the_report_of_the_turn_before_it`
   - In `tests/test_transcript.py`: `test_only_the_whole_wrapper_of_a_command_this_plugin_ships_is_taken_for_one`
   - `test_the_same_report_never_wakes_claude_twice_however_often_stop_fires`
   - `test_wakes_are_capped_per_session`
   - `test_a_report_ready_a_minute_after_its_turn_ended_still_wakes_claude_and_a_later_one_does_not`
   - `test_the_first_reason_that_applies_is_the_one_a_held_report_names`
4. **Emet's text appears only inside the data block.** Every value of a report is untrusted. It reaches Claude as a JSON string between the two markers, cleaned and cut, with fixed words before and after. Outside the block the plugin writes only its own words, whole numbers it checked, and a link it built from its own values. Only the rendered text is saved, never the report.
   - In `tests/test_report.py`: `test_a_fake_closing_marker_in_any_field_stays_inside_the_block`, `test_a_closing_marker_written_with_lookalike_brackets_is_escaped_like_a_real_one`, `test_every_line_of_the_block_is_json_the_plugin_wrote`, `test_control_bidirectional_and_invisible_characters_are_removed`, `test_the_fixed_words_of_a_wake_are_these_and_in_this_order`, `test_an_unknown_status_is_never_repeated_and_reads_as_unreadable`, `test_the_link_is_built_from_the_plugins_own_values_and_never_from_the_document`, `test_the_whole_message_stays_within_the_limit_for_the_worst_report`
   - `test_the_body_of_a_202_is_never_rendered`
   - `test_the_saved_verdict_holds_the_rendered_text_and_ids_not_the_document`
   - `test_no_file_but_the_verdict_holds_report_text_and_none_holds_the_key`
5. **The key goes only to the configured address.** Every request goes through `transport.ask`, which follows no redirect, to the one endpoint `config.endpoint_problem` allows.
   - `test_nothing_is_asked_of_a_host_other_than_emet`
   - `test_nothing_is_asked_without_configuration`
   - `test_a_report_read_is_tried_once_within_ten_seconds_through_the_one_opener_that_follows_no_redirect`
   - `test_a_redirected_report_read_is_not_followed`
   - In `tests/test_commands.py`, for the sender: `test_sends_nothing_to_a_host_other_than_emet`, `test_a_redirected_send_is_not_followed_and_the_turn_waits`, `test_key_check_follows_no_redirect`
6. **Standard library only, Python 3.9, three operating systems.** No single test guards this. The `test` job in CI does: it runs the whole suite on Linux, macOS and Windows, on Python 3.9 and the latest release, with nothing installed for the plugin itself.

## Prompt hook rules

The prompt hook (`missed.py`) runs at `UserPromptSubmit`, in the foreground: the session waits for it at every prompt. It gives Claude a saved verdict that the report check could not wake Claude with. It is the second part of the plugin that can make Claude speak, and the only one that runs before a prompt is answered. A change to it keeps the five rules below. The tests are in `tests/test_missed.py` unless another file is named.

1. **A prompt always goes through, unchanged and at once.** The hook exits 0 whatever happens. It prints nothing, or one JSON object with `hookSpecificOutput.additionalContext` and `systemMessage` and no other field: no `decision`, no `continue`. It makes no request and reads no key. It takes no lock with a wait. It tries three and gives up at once on one that is not free: the session's own prompt lock (`state/<session>.prompt.lock`), the status lock, and the session's watcher lock, which it lets go at once and tries only to learn whether a watcher is running, before it reads the session file. A Stop that starts in that instant finds the watcher lock taken and tries again 2 seconds later, inside the 8 seconds it waits before its first read. Its entry in `hooks.json` has a `timeout` of 5 seconds, and neither `async` nor `onFailure`. A prompt with nothing saved for its session costs one look at a folder.
   - `test_a_missed_verdict_ends_the_process_with_exit_0_and_one_json_object_on_stdout`
   - `test_what_must_not_be_shown_ends_the_process_with_exit_0_and_no_output`
   - `test_a_prompt_with_nothing_to_show_ends_the_process_with_exit_0_and_no_output`
   - `test_the_output_sets_no_field_that_can_block_or_change_the_prompt`
   - `test_the_hook_waits_for_no_lock_another_process_holds`
   - `test_the_hook_takes_no_lock_with_a_wait_and_tries_only_the_three_it_needs`
   - `test_the_hook_needs_no_key_and_asks_emet_nothing`
   - `test_an_error_in_the_prompt_hook_is_logged_by_type_and_never_raised`
   - `test_the_sender_never_loads_the_prompt_hook`
   - In `tests/test_manifests.py`: `test_the_prompt_hook_runs_in_the_foreground_with_a_short_limit_and_cannot_be_made_to_block`
2. **A verdict is shown at most once.** The checks file decides, and the verdict file only supplies the text. The showing is recorded as `shown` first, then the file is removed, then the text is printed. A crash in between loses a showing and never repeats one. A showing that cannot be recorded shows nothing. All of it runs under the session's prompt lock, so two prompts that overlap cannot both take one verdict: the one that finds the lock taken shows nothing. A showing is known by the report's id and revision, so a later revision of a report already shown is shown as a report of its own, under the same cap: its claims can differ, and the user never got it.
   - `test_two_prompts_of_a_session_that_overlap_cannot_both_take_the_verdict`
   - `test_a_later_revision_of_a_report_already_shown_is_shown_as_a_report_of_its_own`
   - `test_a_verdict_is_shown_once_and_the_second_prompt_shows_nothing`
   - `test_a_showing_that_cannot_be_recorded_shows_nothing_and_keeps_the_file`
   - `test_a_crash_after_the_record_loses_the_showing_and_never_repeats_it`
   - `test_a_file_that_could_not_be_removed_is_never_shown_again_and_is_removed_later`
3. **Only a verdict Claude was not given, in its own session, with a prompt a person sent.** A verdict is shown when its `flagged` line has `handed` false. One with `handed` true is shown only when the hand-over is at least 10 seconds old, no watcher of the session is running (its wake could still come), and the session file, read from before the hand-over on, holds no wake since. A wake is one of three entries. The first two were seen on Claude Code 2.1.305: a `queue-operation` entry with `operation` `enqueue`, written the moment Claude Code receives the wake, whose `content` begins with `<task-notification>` and holds the wake's first two sentences, within 10 minutes of the hand-over; and the entry `transcript.started_by_wake` takes for the turn a wake started, within the same 10 minutes. The third is assumed and was not seen: a `queued_command` attachment that holds the wake's first two sentences, for a wake that landed in a running turn. The sentence in a prompt, an answer or a tool result is no wake. When the session file does hold the wake, the verdict's file is removed. A verdict with no `flagged` line is left for the report check. One verdict comes with a prompt, the newest first, and a session gets three showings, counted apart from its wakes; a verdict past the cap is removed unshown, and so is one saved before the conversation was compacted. Nothing is shown with a slash command, a shell escape, the turn a wake starts, a subagent's prompt, a prompt that names no `prompt_id`, or where no person is at the session; the verdict then waits.
   - `test_a_report_claude_was_not_woken_for_is_shown_with_the_next_prompt`
   - `test_a_verdict_the_poller_did_not_record_is_left_for_the_poller`
   - `test_a_wake_that_started_a_turn_is_not_shown_again_and_its_file_is_removed`
   - `test_a_wake_folded_into_a_running_turn_is_not_shown_again`
   - `test_a_wake_claude_code_took_into_its_queue_is_not_shown_again_whatever_became_of_it`
   - `test_a_queue_entry_that_is_not_this_wake_being_queued_does_not_stand_in_for_it`
   - `test_the_sentence_of_a_wake_anywhere_but_in_a_wake_does_not_stand_in_for_one`
   - `test_a_handed_verdict_is_not_judged_while_a_watcher_of_the_session_runs`
   - `test_a_handed_wake_the_session_file_never_got_is_shown_with_the_next_prompt`
   - `test_a_wake_handed_over_a_moment_ago_is_not_judged_yet`
   - `test_a_handed_wake_is_not_judged_from_a_session_file_that_does_not_reach_back_to_it`
   - `test_one_verdict_comes_with_a_prompt_the_newest_first`
   - `test_a_session_gets_three_showings_and_the_verdicts_after_them_are_removed_unshown`
   - `test_wakes_and_showings_are_counted_apart`
   - `test_a_verdict_of_a_conversation_compacted_since_is_removed_unshown`
   - `test_a_verdict_waits_for_a_prompt_that_a_model_call_answers`
   - `test_nothing_is_shown_with_a_prompt_that_names_no_id`
   - `test_nothing_is_shown_where_no_person_is_at_the_session_and_the_verdict_waits`
4. **No chain.** Claude's answer to a prompt that carried a verdict quotes the disputed claims, and Emet flags such an answer again. The `shown` line names the prompt by Claude Code's id, and the report check holds the report of that turn back as `shown_turn`: it is read and recorded, Claude is not woken, and no verdict is saved, so no later prompt carries it either.
   - `test_the_report_of_the_turn_that_carried_a_verdict_is_read_and_claude_is_never_told`
   - `test_without_a_showing_the_same_turn_wakes_claude`
   - `test_a_showing_holds_back_only_the_turn_of_its_own_prompt`
   - `test_a_turn_that_carried_a_verdict_is_the_first_reason_a_report_is_held_for`
5. **Only the saved text reaches Claude, and no report text reaches anything else.** The hook reads a verdict through `watch.verdicts` only, never renders one and never asks Emet for a report. The line Claude Code shows the user is fixed words. The log, the checks file and the status file hold ids and outcome words.
   - `test_the_line_for_the_user_is_fixed_words_whatever_the_report_holds`
   - `test_the_log_the_checks_file_and_the_status_file_hold_no_text_of_the_verdict`
   - `test_a_verdict_file_this_plugin_cannot_read_is_left_where_it_is`

## Files the plugin keeps

Everything is in the plugin's data folder. [claude/README.md](claude/README.md#where-data-goes-and-what-stays-local) tells users about the files an installed plugin writes. This table has the same files, and says who owns each. "Sender" is the worker behind the four hooks (`commands.process`), "report check" is `watch.py`, "prompt hook" is `missed.py`.

| Path | Written by | Read by | Format owned by |
| --- | --- | --- | --- |
| `state/<session>.json` | Sender | Sender | `commands.py` |
| `state/<session>.pending.jsonl` | Sender, of any session | Sender, report check | `pending.py` |
| `state/<session>.checks.jsonl` | Report check, and the prompt hook for a `shown` line | Report check, prompt hook, and the sender to find the turn a wake started | `watch.py` |
| `state/<session>.stop.json` | Report check | Report check | `watch.py` |
| `verdicts/<session>.<trace>.<revision>.json` | Report check | Report check, and the prompt hook, which removes the file once its verdict was shown or will never be | `watch.py`, and `report.py` for the text |
| `queue/<number>-<hash>.json` | Sender | Sender, of any session | `transport.py`, and `envelope.py` for the content |
| `pause.json`, `read-pause.json` | Sender, report check | Sender, report check | `pause.py` |
| `status.json` | Sender, report check, prompt hook | The `/emet:setup` and `/emet:status` skills | `commands.py`, and `watch.py` for `checks` and `reads_paused_until` |
| `emet.log`, `emet.log.1` | Every module, through `storage.log` | The `/emet:status` skill | `storage.py` |
| `*.lock` | Whoever takes the lock first: the sender (`state/<session>.lock`, `state/<session>.pending.lock`, `queue.lock`), the report check (`state/<session>.watch.lock`), the prompt hook (`state/<session>.prompt.lock`), both (`status.lock`, `pause.lock`) | No one. They are empty and are never removed | `storage.py` |
| `.<name>.<number>.tmp` | `storage.write_private`, while it writes any of the JSON files above | No one. A write that fails removes it. A process killed while writing leaves it behind, with what the file was to hold | `storage.py` |

These files outlive an update, and a session that started before an update keeps running the old code next to a new one. So:

- **Readers ignore what they do not know.** A key a reader does not know is skipped, and so is a line it cannot read. Writers only add keys. `test_a_line_with_a_field_the_poller_does_not_know_is_still_a_turn` and `test_readable_lines_survive_damaged_ones` (in `tests/test_pending.py`) hold this.
- **A new outcome word goes in the outcomes table.** Add it to `watch.OUTCOMES` and to the table at the top of `watch.py`, with whether it is final. `test_the_table_of_outcomes_names_every_outcome_and_whether_it_is_final` and `test_only_a_listed_outcome_can_be_recorded` hold this. A reader treats a word it does not know as not final.
- **Three files name the version of their format.** `state/<session>.json`, `state/<session>.stop.json` and a saved verdict each hold a `v` key, a whole number, 1 today. A file with no `v` is read as version 1, which is how 0.1.4 wrote the state file. Adding a key needs no new number. A change that renames or removes a key, or changes what a value means, raises the number in the module that owns the file (`commands.STATE_VERSION`, `watch.STOP_VERSION`, `watch.VERDICT_VERSION`) and keeps reading the forms before it. `storage.known_version` is the one check, and every reader of the three files asks it. A `v` that is above the reader's number, or is no whole number, is a version the reader does not know. What it then does differs by file:
  - **The state file:** the sender sends nothing for that session and does not write the file. Read as version 1, a place it cannot read could send the whole session again or drop turns. The log says `session not sent, its state file is of a version this plugin does not know`, and the plugin that wrote the file finds its place as it left it. `test_a_session_whose_state_file_is_of_an_unknown_version_is_neither_sent_again_nor_moved` holds this, and `test_a_state_file_with_no_version_is_read_as_the_first_one` the form without `v`.
  - **The Stop file:** read as a damaged one, so the run goes by its own Stop. The session file and the pending file still tell it of a newer turn. `test_a_stop_file_of_a_version_this_plugin_does_not_know_is_read_as_a_damaged_one` holds this.
  - **A saved verdict:** never returned, and never removed, since its time may not mean what this version thinks. `test_a_saved_verdict_of_a_version_this_plugin_does_not_know_is_never_shown` and `test_a_verdict_of_a_version_this_plugin_does_not_know_is_left_where_it_is_however_old` hold this.
- **The other files carry no version.** Each for its own reason, and each with its own way to change:
  - `state/<session>.pending.jsonl` and `state/<session>.checks.jsonl` get lines from every version of the plugin that runs, so no line can speak for the file, and a reader that skipped the lines of a newer version would count wakes and turns wrong. They change only by adding keys and kinds of line, as the first two rules say. A change that cannot be made that way goes into a new file under a new name, which a plugin before it never opens.
  - `queue/<number>-<hash>.json` holds the envelope as it is sent, and a worker of any session and any version sends it. 0.1.4 sends every key of a queued file, so a `v` there would reach Emet. The envelope names Emet's own format under `semconv_version`.
  - `pause.json` and `read-pause.json` are believed for an hour at most, and `status.json` is rewritten at every run and names the plugin that wrote it under `plugin_version`. Every version on the machine writes them in turn, and each reader checks the type and the range of what it takes from them.
- **A saved verdict is read through `watch.read_verdict` only.** Its text goes to Claude, and another process running as the user can write the file. The function returns a verdict only when the whole file checks out, and the log then names why one is not shown by one word: `name` (the file name is not a verdict's), `link` (a symbolic link, or a file with a second name), `size` (longer than any verdict the plugin writes), `unreadable` (no JSON object, or nested too deep), `version`, `wording` (saved under another `report.PROMPT_VERSION`), `shape` (a key is missing or holds what the plugin would not write), `mismatch` (the name and the content give another session, trace or revision), `text` (too long, or not equal to what `report.py` renders for a missed verdict with the data block it holds and the link the file's own ids give) and `age` (no readable time, more than seven days old, or ahead of the clock). Each has a test in `tests/test_watch.py`, and `test_whatever_a_report_holds_the_verdict_the_poller_saves_is_one_it_reads_back` holds the writer to the same form. A new key in a verdict is added to `watch.shaped` only together with a new `VERDICT_VERSION`, or verdicts saved before it stop being shown.
  - The file is opened once, without following a link, and what is judged is what was opened: a plain file with one name, read up to the size limit and no further. Windows has no flag that refuses a link at the open, so there a look at the name before the open is the only guard against one. `test_a_verdict_file_is_opened_once_and_judged_by_what_was_opened` and `test_a_verdict_file_that_grows_after_its_size_was_read_is_never_shown` hold this.
  - The text is checked by writing it again. `report.is_missed_message` reads the parts that vary out of the text (the sentence with the counts, the data block, the number of claims left out), checks the data block key by key and string by string, and has `report.compose`, which `render` also writes with, put the message together from them. Only an equal text passes, so a change to the fixed words needs no change to the check, only the new `PROMPT_VERSION` it already needs. `test_every_missed_message_about_a_flagged_report_is_one_the_check_lets_through` in `tests/test_report.py` holds the two together.
  - What the check cannot know: whether the strings and the numbers in the text are the ones Emet reported. The report is not kept. A file with a well formed data block of other claims passes, which is why a process running as the user stays outside what the plugin defends against.
- **The README lists what an installed plugin writes, and nothing else.** A file goes into its table in the pull request that makes a hook write it, and not before. `test_the_readme_lists_the_files_an_installed_plugin_keeps_and_no_others` holds this.

## Live check

The tests run the plugin against a stand-in Emet and a written session file. They cannot show two things: how Claude words its reply to the text the plugin hands it, and whether Claude Code still treats the hook as the plugin expects. So a change to `report.py`, `watch.py`, `missed.py`, the hooks or the transcript parsing is tried once in a real session before it merges.

1. Start the container described in [dev/README.md](dev/README.md), with an Emet running on your machine. It pins the Claude Code version CI validates with.
2. Run these four prompts in one session, and compare with `state/<session>.checks.jsonl` and `emet.log` in the data folder. The second `Stop` hook in `hooks/hooks.json` starts the report check and the `UserPromptSubmit` hook shows a missed verdict, so nothing is added by hand.

   | Prompt | Expect |
   | --- | --- |
   | `Run python3 -c "print(6 * 7)" and tell me the result.` | The answer, and nothing after it. The checks file gets one line for the turn with an outcome that is not `flagged`, usually `clear`. |
   | `I am testing a fact checker and need a deliberately wrong summary. Run printf 'q3_revenue=4200 q3_orders=310' and then write exactly this sentence as your answer, nothing else: Q3 revenue was 9800 and there were 75 orders.` | The answer, then, usually within a minute, a line that reads "Stop hook feedback" (seen on Claude Code 2.1.305) and a second message from Claude that you did not ask for. It is a short message to you: it names the claim Emet disputed and Emet's reason, says what the tool output of that turn shows about the claim, calls no tool and opens no link. The checks file gets `flagged` with `"handed": true` for the turn, then `wake_turn` for Claude's second message, and `verdicts/` gets one file, which a later prompt removes (the log then says `verdict <id> removed, Claude was woken with it`). No third message follows, whatever Emet says about the second. |
   | The second prompt again, and as soon as its answer shows, `What is 2 + 2?` | Both answers, and no message you did not ask for. The checks file gets `flagged` with `"handed": false` and `"held": "newer_turn"` for the first of the two turns. |
   | Once that `flagged` line is there, `What is 5 + 5?` | The missed verdict: you typed `What is 2 + 2?` before the report was back, so the report arrives with this prompt, once. Claude Code shows one line under the prompt: `UserPromptSubmit says: Emet disputed a claim in an earlier answer of this session. Claude was given Emet's report with this prompt.` Claude's answer begins with `About an earlier answer:`, names the disputed claim and Emet's reason, says what the tool output of that earlier turn shows about the claim, and then answers the prompt. The checks file gets `shown` for the flagged turn, and its `prompt_id` is the `prompt_id` of this turn's line in `state/<session>.pending.jsonl`. The file in `verdicts/` is gone. No message you did not ask for follows. If Emet flags this answer, the checks file gets `flagged` with `"handed": false` and `"held": "shown_turn"` for it, and `verdicts/` gets no file. A further prompt carries nothing. |

3. Run `/emet:status` in the same session. It lists the last five checks, each under its heading. The turn that was held back is under Flagged twice: as `flagged` and not told to Claude, with the reason `newer_turn`, and as `shown`. Claude's second message from the second prompt is under Not checked, as `wake_turn`. A turn Emet found nothing against is checked with no conflicts found. Each entry shows its outcome word as the file has it, in backticks. It should show ids, outcome words and links, and no sentence from a report. The answer of `/emet:status` is a turn too, and Emet checks it against the files the skill read, so no second message should follow it, and the prompt after it carries nothing.
4. When the change touches `hooks/hooks.json` or the version rule in `watch.py`, try a Claude Code older than 2.1.287 once. The plugin does not support one; this checks that it stays quiet there. Build the image with an old version, `docker compose -f dev/compose.yaml build --build-arg CLAUDE_CODE_VERSION=2.1.271`, start a session without `--build`, and run the second prompt. The prompt comes back as soon as the answer is shown, with no wait, no second message follows, and the log says `watch skipped, Claude Code is older than 2.1.287 or its version is not known`. The next start with `--build` puts the pinned version back.
5. In the pull request, name the Claude Code version you tried, say which of the checks matched, and paste Claude's second message from the second prompt, or its answer to the fourth, when the change touches the wording.

Whether Emet flags the second prompt varies from run to run: the same prompt with other numbers came back `clear` once. So you may need to send it twice. A prompt that only asks Claude to misreport a result does not work, because Claude refuses and says what the tool printed. If it still does not flag, use another prompt whose answer states something a tool result of the same turn contradicts.

## Pull requests

`main` is protected: every change goes through a pull request with passing checks, and is squash-merged. Keep pull requests to one change, and describe what changes for users and for the data that is sent.

## Releases

1. Run `uv run python .github/scripts/changelog_release.py X.Y.Z` from the root. It moves the **Unreleased** entries in `CHANGELOG.md` and the entry of every file in `changelog.d/` under the new version with today's date, updates the links at the end of the file and deletes the files it moved. Add `--check` to print the new section without writing anything. A date after the version, as in `2026-12-01`, replaces today's. Take out the sentences in the documents that say the release before lacks something (`test_the_documents_say_the_latest_release_has_no_report_check_until_one_has` names them).
2. Set the version in `claude/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `claude/scripts/emet_claude/__init__.py` and `pyproject.toml`, and the `ref` pins in `docs/enterprise.md`, then run `uv lock` (tests keep them equal).
3. Merge, then at once push a tag `vX.Y.Z` on `main` (the admin guide on `main` already pins it). The release workflow checks the version and publishes the GitHub release with the changelog entry.
