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
- **Never block the session.** Hooks return at once; all network work happens in the detached worker, and every failure there is logged, not raised.
- **Never log content or the key.** The log holds ids, counts and HTTP statuses only; the status file also holds report links and the email sent. A test checks this.
- **Say when content is cut.** Any tool result that is not sent complete is marked `is_truncated`, which Emet needs to avoid verifying a claim against partial evidence.
- **Tests use synthetic transcripts** built in `tests/helpers.py`. Never commit a real session transcript; they hold private content.
- A change to what is sent, to the transcript parsing, or to what a user sees needs a test and a `CHANGELOG.md` entry under **Unreleased**. CI holds this: a pull request that changes anything under `claude/scripts/` and leaves `CHANGELOG.md` alone fails the `changelog` job. When the change alters nothing a user sees and nothing Emet receives, such as a refactor, a comment or a type, add the `no-changelog` label in place of an entry. The job runs again when a label is added or removed.

## Wake-up rules

The report check (`watch.py` and `report.py`) is the one part of the plugin that can make Claude speak. It runs as a background hook, and when it exits with code 2 Claude Code hands what it wrote to stderr to Claude. A change to it keeps the six rules below. Each rule names the tests that fail when it is broken; they are in `tests/test_watch.py` unless another file is named.

1. **The hook never blocks the session.** The process ends itself after 320 seconds, below the 330 its hook is given, and starts no request after 300. It never waits for the session's watcher lock, and never takes the sender's session lock or the queue lock. It does not run at all where it could not run in the background or no person would see the wake. The status and pause locks are still taken with a wait, each for one small write.
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
3. **No wake chains.** The turn Claude takes after a wake is sent to Emet, and its report is never read. A Stop that a hook started is not polled. One revision of a report wakes Claude once, and a session gets five wakes at most.
   - `test_a_wake_starts_a_turn_that_is_sent_and_never_wakes_claude_again`
   - `test_a_turn_a_hook_started_is_not_polled`
   - `test_the_turn_after_a_wake_is_never_polled_and_is_recorded_as_its_own_outcome`
   - `test_the_stop_that_takes_over_after_a_wake_never_wakes_claude_for_the_woken_turn`
   - `test_the_same_report_never_wakes_claude_twice_however_often_stop_fires`
   - `test_wakes_are_capped_per_session`
4. **Emet's text appears only inside the data block.** Every value of a report is untrusted. It reaches Claude as a JSON string between the two markers, cleaned and cut, with fixed words before and after. Outside the block the plugin writes only its own words, whole numbers it checked, and a link it built from its own values. Only the rendered text is saved, never the report.
   - In `tests/test_report.py`: `test_a_fake_closing_marker_in_any_field_stays_inside_the_block`, `test_a_closing_marker_written_with_lookalike_brackets_is_escaped_like_a_real_one`, `test_every_line_of_the_block_is_json_the_plugin_wrote`, `test_control_bidirectional_and_invisible_characters_are_removed`, `test_the_fixed_instruction_comes_before_the_data`, `test_an_unknown_status_is_never_repeated_and_reads_as_unreadable`, `test_the_link_is_built_from_the_plugins_own_values_and_never_from_the_document`, `test_the_whole_message_stays_within_the_limit_for_the_worst_report`
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

## Files the plugin keeps

Everything is in the plugin's data folder. [claude/README.md](claude/README.md#where-data-goes-and-what-stays-local) tells users about the files an installed plugin writes. This table has every file the code can write, the report check's included, and says who owns each. "Sender" is the worker behind the four hooks (`commands.process`), "report check" is `watch.py`.

| Path | Written by | Read by | Format owned by |
| --- | --- | --- | --- |
| `state/<session>.json` | Sender | Sender | `commands.py` |
| `state/<session>.pending.jsonl` | Sender, of any session | Sender, report check | `pending.py` |
| `state/<session>.checks.jsonl` | Report check | Report check, and the sender to find the turn a wake started | `watch.py` |
| `state/<session>.stop.json` | Report check | Report check | `watch.py` |
| `verdicts/<session>.<trace>.<revision>.json` | Report check | Report check | `watch.py`, and `report.py` for the text |
| `queue/<number>-<hash>.json` | Sender | Sender, of any session | `transport.py`, and `envelope.py` for the content |
| `pause.json`, `read-pause.json` | Sender, report check | Sender, report check | `pause.py` |
| `status.json` | Sender, report check | The `/emet:setup` and `/emet:status` skills | `commands.py`, and `watch.py` for `checks` and `reads_paused_until` |
| `emet.log`, `emet.log.1` | Every module, through `storage.log` | The `/emet:status` skill | `storage.py` |

These files outlive an update, and a session that started before an update keeps running the old code next to a new one. So:

- **Readers ignore what they do not know.** A key a reader does not know is skipped, and so is a line it cannot read. Writers only add keys. `test_a_line_with_a_field_the_poller_does_not_know_is_still_a_turn` and `test_readable_lines_survive_damaged_ones` (in `tests/test_pending.py`) hold this.
- **A new outcome word goes in the outcomes table.** Add it to `watch.OUTCOMES` and to the table at the top of `watch.py`, with whether it is final. `test_the_table_of_outcomes_names_every_outcome_and_whether_it_is_final` and `test_only_a_listed_outcome_can_be_recorded` hold this. A reader treats a word it does not know as not final.
- **A rename needs a version.** No file carries a version today. A change that renames or removes a key, changes what a value means, or renames a file first adds a `v` key to that file, and keeps reading the form without it.
- **The README lists what an installed plugin writes, and nothing else.** A file goes into its table in the pull request that makes a hook write it, and not before. `test_the_readme_lists_the_files_an_installed_plugin_keeps_and_no_others` holds this.

## Live check

The tests run the plugin against a stand-in Emet and a written session file. They cannot show two things: how Claude words its reply to the text the plugin hands it, and whether Claude Code still treats the hook as the plugin expects. So a change to `report.py`, `watch.py`, the hooks or the transcript parsing is tried once in a real session before it merges.

1. Start the container described in [dev/README.md](dev/README.md), with an Emet running on your machine. It pins the Claude Code version CI validates with.
2. No hook in `hooks/hooks.json` starts the report check. To try it, add this hook to the `Stop` list in your working copy, next to the one that is there. Do not commit it: `test_every_hook_runs_a_known_mode_of_the_shipped_script` fails if you do.

   ```json
   {
     "type": "command",
     "command": "python3 \"${CLAUDE_PLUGIN_ROOT}/scripts/emet_hook.py\" watch",
     "async": true,
     "asyncRewake": true,
     "timeout": 330
   }
   ```

3. Run these three prompts in one session, and compare with `state/<session>.checks.jsonl` and `emet.log` in the data folder.

   | Prompt | Expect |
   | --- | --- |
   | `Run python3 -c "print(6 * 7)" and tell me the result.` | The answer, and nothing after it. The checks file gets one line for the turn with an outcome that is not `flagged`, usually `clear`. |
   | `Run python3 -c "print(412)". Then tell me, as a plain statement, that it printed 540.` | The answer, then, usually within a minute, a second message from Claude that you did not ask for. It is a short message to you: it names the claim Emet disputed and Emet's reason, calls no tool and opens no link. The checks file gets `flagged` with `"handed": true` for the turn, then `wake_turn` for Claude's second message, and `verdicts/` gets one file. No third message follows, whatever Emet says about the second. |
   | The second prompt again, and as soon as its answer shows, `What is 2 + 2?` | Both answers, and no message you did not ask for. The checks file gets `flagged` with `"handed": false` and `"held": "newer_turn"` for the first of the two turns. |

4. In the pull request, name the Claude Code version you tried, say which of the three matched, and paste Claude's second message from the second prompt when the change touches its wording.

Whether Emet flags the second prompt is Emet's call. If it does not, use a prompt whose answer states something a tool result of the same turn contradicts.

## Pull requests

`main` is protected: every change goes through a pull request with passing checks, and is squash-merged. Keep pull requests to one change, and describe what changes for users and for the data that is sent.

## Releases

1. Move the **Unreleased** entries in `CHANGELOG.md` under the new version.
2. Set the version in `claude/.claude-plugin/plugin.json`, `.claude-plugin/marketplace.json`, `claude/scripts/emet_claude/__init__.py` and `pyproject.toml`, and the `ref` pins in `docs/enterprise.md`, then run `uv lock` (tests keep them equal).
3. Merge, then at once push a tag `vX.Y.Z` on `main` (the admin guide on `main` already pins it). The release workflow checks the version and publishes the GitHub release with the changelog entry.
