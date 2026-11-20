"""Runs the poller the way Claude Code does: the command of the shipped hook as a process, with real pipes and a real exit code."""

from __future__ import annotations

import json
import os
import shlex
import socket
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

import pytest

from emet_claude import commands, pending, report, storage, timestamps, watch
from helpers import FakeEmet, Transcript
from test_report import claim, document

SCRIPTS = Path(commands.__file__).resolve().parent.parent
SCRIPT = SCRIPTS / "emet_hook.py"
PLUGIN = SCRIPTS.parent
# What Claude Code puts the plugin's folder in for, in a hook's command
PLUGIN_ROOT = "${CLAUDE_PLUGIN_ROOT}"
SESSION = "session-1"
T1 = "0f0e6c6e-4a8e-4d47-9b4c-3c9f2f1d8a11"
CONVERSATION = "7d3c1c52-0d0a-4a52-8d0e-0c7e3a1b2c3d"
# Two, three and four byte characters: a pipe that is not UTF-8 would change or refuse them
BEYOND_ASCII = "São Paulo → \U00020bb7"
# Above the 8 seconds an unchanged run waits before its first read, with room for a slow machine
PROCESS_TIMEOUT_S = 60
# Names of the developer's own session that would change what the child does
INHERITED = ("CLAUDE_PLUGIN_OPTION_", "CLAUDE_PLUGIN_DATA", "CLAUDE_CONFIG_DIR", watch.ENTRYPOINT_NAME, "PYTHONPATH", "PYTHONIOENCODING")
Result = Tuple[int, bytes, bytes]


def flagged() -> Dict[str, Any]:
    """A flagged report whose disputed claim holds characters outside ASCII."""
    disputed = claim(1, "disputed", f"Spend in {BEYOND_ASCII} rose 12%.", reason=f"The export for {BEYOND_ASCII} shows 9%.")
    built: Dict[str, Any] = document([disputed])
    return built


def waits(folder: Path, **seconds: float) -> Path:
    """Writes a startup file that shortens the named waits of watch.py in the child, and returns its folder.

    The child still runs the entry script by its real command line. Only the named numbers differ.
    """
    for name in seconds:
        assert isinstance(getattr(watch, name), (int, float)), f"watch.{name} is not a wait this file can shorten"
    folder.mkdir()
    lines = [
        "import sys",
        f"sys.path.insert(0, {str(SCRIPTS)!r})",
        "from emet_claude import watch",
        *(f"watch.{name} = {value!r}" for name, value in seconds.items()),
    ]
    (folder / "sitecustomize.py").write_text("\n".join(lines) + "\n", encoding="utf-8")
    return folder


def shipped_hook() -> Dict[str, Any]:
    """Returns the one hook of the shipped hooks.json that can wake Claude."""
    hooks = storage.loads((PLUGIN / "hooks" / "hooks.json").read_bytes())["hooks"]
    waking = [(event, hook) for event, groups in hooks.items() for group in groups for hook in group["hooks"] if hook.get("asyncRewake")]
    ((event, found),) = waking
    assert event == "Stop", "the tests below hand the process the input of a Stop"
    assert isinstance(found, dict)
    return found


def shipped_command() -> List[str]:
    """Returns the command of the shipped hook, word for word, with this Python for `python3` and the plugin's folder filled in."""
    program, *arguments = shlex.split(shipped_hook()["command"])
    assert program == "python3", "the hook names the interpreter by the one name every supported system has"
    assert any(PLUGIN_ROOT in argument for argument in arguments), "the hook finds its script through the folder Claude Code names"
    return [sys.executable, *(argument.replace(PLUGIN_ROOT, str(PLUGIN)) for argument in arguments)]


def hook(environ: Dict[str, str], stdin: bytes, startup: Optional[Path] = None) -> Result:
    """Runs the command of the shipped hook to its end and returns (exit code, stdout, stderr)."""
    inherited = {name: value for name, value in os.environ.items() if not name.startswith(INHERITED)}
    env = {**inherited, **environ, **({"PYTHONPATH": str(startup)} if startup else {})}
    result = subprocess.run(shipped_command(), input=stdin, capture_output=True, env=env, timeout=PROCESS_TIMEOUT_S)
    return result.returncode, result.stdout, result.stderr


def stop_input(transcript: Transcript, **fields: Any) -> bytes:
    return json.dumps({**transcript.hook_input(), **fields}).encode("utf-8")


def root(environ: Dict[str, str]) -> Path:
    return storage.data_dir(environ)


def deliver(environ: Dict[str, str], ended_ago: float = 0.0) -> None:
    """Writes the line the sender writes once Emet accepted the turn, stamped by the real clock the child reads.

    The turn ended now, or ended_ago seconds before. Without an end the report would be held as a late one.
    """
    ended = timestamps.text(datetime.now(timezone.utc) - timedelta(seconds=ended_ago))
    pending.note(root(environ), SESSION, T1, environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"], CONVERSATION, ended=ended)


def finish(environ: Dict[str, str]) -> None:
    """Writes the line that ends the sender's run. Stamped ahead, so it counts as later than the child's start however slow that is."""
    pending.append(root(environ), SESSION, {"finished": storage.now(time.time() + PROCESS_TIMEOUT_S), "queued": 0})


def outcomes(environ: Dict[str, str]) -> List[Tuple[str, str]]:
    return [(item["trace_uuid"], item["outcome"]) for item in watch.checks(root(environ), SESSION)]


def log(environ: Dict[str, str]) -> str:
    path = root(environ) / "emet.log"
    return path.read_text(encoding="utf-8") if path.exists() else ""


@pytest.fixture
def session(transcript: Transcript) -> Transcript:
    """A session file of a person's turn, on a Claude Code that can be woken."""
    transcript.prompt("What is 2 + 2?")
    transcript.text("msg_1", "4")
    return transcript


@pytest.fixture
def ready(session: Transcript, environ: Dict[str, str], emet: FakeEmet) -> Dict[str, Any]:
    """One delivered turn whose flagged report Emet answers with at the first read. Returns the report."""
    deliver(environ)
    emet.report(T1, 200, flagged())
    return flagged()


@pytest.fixture
def at_once(tmp_path: Path) -> Path:
    """The first read is due when the process starts, and the loop looks again without the two second wait."""
    return waits(tmp_path / "startup", FIRST_S=0, TICK_S=0.05)


def wake_bytes(emet: FakeEmet, doc: Dict[str, Any]) -> bytes:
    return report.wake_message(doc, emet.url, SESSION, CONVERSATION).encode("utf-8")


# What Python would encode text written to the child's streams with. The wake is written as bytes, so none of them changes it
STREAM_ENCODINGS = [
    pytest.param("", id="streams_as_the_system_sets_them"),
    pytest.param("ascii", id="streams_that_refuse_all_but_ascii"),
    pytest.param("cp1252", id="streams_in_a_windows_code_page"),
]


@pytest.mark.parametrize("encoding", STREAM_ENCODINGS)
def test_a_flagged_report_ends_the_process_with_exit_2_and_the_wake_text_on_stderr(
    session: Transcript, environ: Dict[str, str], emet: FakeEmet, ready: Dict[str, Any], at_once: Path, encoding: str
) -> None:
    code, out, err = hook({**environ, **({"PYTHONIOENCODING": encoding} if encoding else {})}, stop_input(session), at_once)
    expected = wake_bytes(emet, ready)
    assert BEYOND_ASCII.encode("utf-8") in expected, "the report under test must hold characters outside ASCII, or the bytes prove nothing"
    assert code == watch.WAKE_EXIT == 2, (
        f"only exit code 2 makes Claude Code wake Claude for a flagged report, got {code} (log: {log(environ)!r})"
    )
    assert err == expected, (
        "stderr is the one stream Claude Code hands to Claude: it must hold the rendered report, byte for byte, as UTF-8"
    )
    assert err.decode("utf-8").count(BEYOND_ASCII) == 2, "letters outside ASCII reach Claude unchanged, also through a pipe on Windows"
    assert out == b"", "stdout of a background hook is not shown to Claude: nothing is written there"
    assert emet.polled(T1) == 1, "one read is enough for a report that is ready"
    assert outcomes(environ) == [(T1, "flagged")], "the wake is recorded before the process exits, so it is never handed over twice"


def test_the_command_under_test_is_the_one_the_shipped_hook_names() -> None:
    program, script, *modes = shipped_command()
    assert program == sys.executable
    assert Path(script).resolve() == SCRIPT.resolve(), "the hook runs the entry script of this checkout"
    assert modes == [commands.WATCH_MODE], "the hook runs the script in the one mode that can exit with code 2"


def test_the_unchanged_command_wakes_claude_after_its_real_first_wait(
    session: Transcript, environ: Dict[str, str], emet: FakeEmet, ready: Dict[str, Any]
) -> None:
    # No startup file: this run waits the real 8 seconds in the child, the test itself does not sleep
    code, out, err = hook(environ, stop_input(session))
    assert (code, out) == (2, b""), (
        f"the hook exactly as Claude Code runs it exits 2 with an empty stdout for a flagged report (log: {log(environ)!r})"
    )
    assert err == wake_bytes(emet, ready), "the hook exactly as Claude Code runs it writes the rendered report to stderr as UTF-8 bytes"


def test_a_flagged_report_of_a_turn_that_ended_too_long_ago_ends_the_process_with_exit_0_and_no_output(
    session: Transcript, environ: Dict[str, str], emet: FakeEmet, at_once: Path
) -> None:
    deliver(environ, ended_ago=watch.LATE_S + PROCESS_TIMEOUT_S)
    finish(environ)
    emet.report(T1, 200, flagged())
    result = hook(environ, stop_input(session), at_once)
    assert result == (0, b"", b""), (
        "a report that comes after the user has moved on must not wake Claude: exit 0 and nothing on either stream"
    )
    held = [(item["outcome"], item["handed"], item["held"]) for item in watch.checks(root(environ), SESSION)]
    assert held == [("flagged", False, watch.LATE)], "the report was read and kept for later, and the record says why Claude was not woken"


@pytest.mark.parametrize("word", sorted(set(report.STATUS_WORDS) - {report.FLAGGED}), ids=lambda word: f"report_is_{word}")
def test_a_report_that_is_not_flagged_ends_the_process_with_exit_0_and_no_output(
    session: Transcript, environ: Dict[str, str], emet: FakeEmet, at_once: Path, word: str
) -> None:
    deliver(environ)
    finish(environ)
    emet.report(T1, 200, document(status=word))
    result = hook(environ, stop_input(session), at_once)
    assert result == (0, b"", b""), f"a report with status {word} must never wake Claude: exit 0 and nothing on either stream"
    assert outcomes(environ) == [(T1, word)], (
        "the report was read and recorded, so the silence is the verdict and not a run that did nothing"
    )


def hook_started(session: Transcript, environ: Dict[str, str]) -> bytes:
    return stop_input(session, stop_hook_active=True)


def headless(session: Transcript, environ: Dict[str, str]) -> bytes:
    session.prompt("And 3 + 3?", source="sdk")
    session.text("msg_2", "6", entrypoint="sdk-cli")
    return stop_input(session)


def not_configured(session: Transcript, environ: Dict[str, str]) -> bytes:
    del environ["CLAUDE_PLUGIN_OPTION_EMET_KEY"]
    return stop_input(session)


NEVER_POLLED = [
    pytest.param(hook_started, [], "watch skipped, a hook started the turn", id="turn_started_by_a_hook"),
    pytest.param(headless, [(T1, "no_person")], "watch skipped, no person is at the session", id="headless_session"),
    pytest.param(not_configured, [(T1, "not_configured")], "watch skipped, the plugin is not set up", id="no_emet_key"),
]


@pytest.mark.parametrize("arrange, recorded, logged", NEVER_POLLED)
def test_a_stop_that_is_not_polled_ends_the_process_at_once_with_exit_0_and_no_output(
    session: Transcript, environ: Dict[str, str], emet: FakeEmet, ready: Dict[str, Any], arrange: Any, recorded: Any, logged: str
) -> None:
    # A flagged report is ready all along: only the rule under test keeps the process silent
    stdin = arrange(session, environ)
    result = hook(environ, stdin)
    assert result == (0, b"", b""), f"'{logged}' means no wake: exit 0 and nothing on either stream, even with a flagged report ready"
    assert emet.reads == [], f"'{logged}' means Emet is never asked for a report"
    assert logged in log(environ), "the log says why the Stop was not polled, so a silent run can be told from a broken one"
    assert outcomes(environ) == recorded, "the reason is recorded for the turn already delivered, and never for a turn a hook started"


GARBAGE = [
    pytest.param(b"", id="empty"),
    pytest.param(b"not json {", id="not_json"),
    pytest.param(b"\xff\xfe\x00", id="not_utf8"),
    pytest.param(b"[1, 2]", id="json_list"),
    pytest.param(b'{"session_id": "../../etc", "transcript_path": 5}', id="session_id_that_is_a_path"),
    pytest.param(b'{"session_id": {"a": 1}}', id="session_id_that_is_an_object"),
]


@pytest.mark.parametrize("stdin", GARBAGE)
def test_garbage_on_stdin_ends_the_process_with_exit_0_and_no_output(environ: Dict[str, str], emet: FakeEmet, stdin: bytes) -> None:
    result = hook(environ, stdin)
    assert result == (0, b"", b""), "a hook must never break the session: input it cannot read ends in exit 0 with no traceback on stderr"
    assert emet.reads == [], "nothing is asked of Emet for a Stop that names no session"
    assert list((root(environ) / "state").iterdir()) == [], "a session id that is not a plain name must never name a file"


def test_a_session_id_that_is_a_path_never_names_a_file(
    session: Transcript, environ: Dict[str, str], emet: FakeEmet, tmp_path: Path
) -> None:
    # Everything else about this Stop is in order, so only the check of the id keeps it from being followed
    result = hook(environ, stop_input(session, session_id="../escaped"))
    assert result == (0, b"", b""), "a Stop whose session id is not a plain name is left alone: exit 0 and nothing on either stream"
    assert list(tmp_path.rglob("*escaped*")) == [], "the session id names files, so one that is a path must never reach a file name"
    assert emet.reads == [], "nothing is asked of Emet for a session id that is not a plain name"


@pytest.fixture
def closed_port() -> str:
    """The address of a local port nothing listens on."""
    # Released, not held: macOS leaves a connect to a bound socket that never listens waiting 8 seconds, where a free port refuses at once
    with socket.socket() as taken:
        taken.bind(("127.0.0.1", 0))
        port = taken.getsockname()[1]
    return f"http://127.0.0.1:{port}"


def test_an_emet_that_cannot_be_reached_ends_the_process_with_exit_0_and_no_output(
    session: Transcript, environ: Dict[str, str], closed_port: str, tmp_path: Path
) -> None:
    environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"] = closed_port
    deliver(environ)
    # One second of budget stands in for the five minutes the run would keep asking
    result = hook(environ, stop_input(session), waits(tmp_path / "startup", FIRST_S=0, BUDGET_S=1))
    assert result == (0, b"", b""), "a network failure is logged and never raised: exit 0 and nothing on either stream"
    assert f"report {T1} -> network, asked again later" in log(environ), (
        "the read was tried and failed, so the silence is not a run that did nothing"
    )
    assert outcomes(environ) == [(T1, "timed_out")], "the turn stays to be polled after a later Stop"


class Silent:
    """A local server that takes one request and never answers it. Keeps what it was sent."""

    def __init__(self) -> None:
        self.received: List[bytes] = []
        self.done = threading.Event()
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(1)
        # A short wait per try, so close never hangs on a client that did not come
        self.server.settimeout(0.05)
        self.thread = threading.Thread(target=self.hold, daemon=True)
        self.thread.start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.getsockname()[1]}"

    def hold(self) -> None:
        while not self.done.is_set():
            try:
                client, _ = self.server.accept()
            except OSError:
                continue
            with client:
                client.settimeout(PROCESS_TIMEOUT_S)
                try:
                    self.received.append(client.recv(65536))
                except OSError:
                    self.received.append(b"")
                # Held open and unanswered until the test is over
                self.done.wait()

    def close(self) -> None:
        self.done.set()
        self.thread.join()
        self.server.close()


@pytest.fixture
def silent() -> Iterator[Silent]:
    server = Silent()
    yield server
    server.close()


def test_the_process_ends_itself_while_a_request_hangs(
    session: Transcript, environ: Dict[str, str], silent: Silent, tmp_path: Path
) -> None:
    environ["CLAUDE_PLUGIN_OPTION_ENDPOINT"] = silent.url
    deliver(environ)
    assert watch.REQUEST_TIMEOUT_S > 5, "the request must outlast the one second timer below, or the timer is not what ends the run"
    result = hook(environ, stop_input(session), waits(tmp_path / "startup", FIRST_S=0, HARD_STOP_S=1))
    asked = b"".join(silent.received)
    assert asked.startswith(f"GET /api/v1/traces/{T1}/report/ ".encode()), (
        "the report read must have reached the server that never answers, or the timer ended a process that was not stuck in a request"
    )
    assert result == (0, b"", b""), "the timer ends a stuck process with exit 0 and no output, below the hook's own limit"
    assert "asked again later" not in log(environ), "the request was still hanging when the process ended, so the timer ended it"
    assert outcomes(environ) == [], "a process ended by its timer records nothing: the turn is polled again after a later Stop"
