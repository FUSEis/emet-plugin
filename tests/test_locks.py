"""Runs the Windows branches of the two file locks on every system, against a stand-in for msvcrt.

Then what a lock does with a link, a pipe or a folder in its file's place.
"""

from __future__ import annotations

import os
import stat
import sys
import threading
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable, ContextManager, List, Set, Tuple

import pytest

from emet_claude import storage
from helpers import links, pipes

# The numbers the real msvcrt gives these names
LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = 0, 1, 2, 3, 4
# What the lock file already holds, so a handle opened to add to it does not start at byte 0
FILLER = b"0123456789"
# One call of locking: the mode, the count of bytes, and where the handle stood in the file
Call = Tuple[int, int, int]


class FakeMsvcrt:
    """Stands in for the msvcrt module. Lists each call of locking, and refuses the modes named in `refused` as Windows does."""

    LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK = LK_UNLCK, LK_LOCK, LK_NBLCK, LK_RLCK, LK_NBRLCK

    def __init__(self) -> None:
        self.calls: List[Call] = []
        self.refused: Set[int] = set()
        self.files: List[str] = []

    def locking(self, fd: int, mode: int, nbytes: int) -> None:
        # Fails on a closed handle, as the real call does
        self.calls.append((mode, nbytes, os.lseek(fd, 0, os.SEEK_CUR)))
        if mode in self.refused:
            raise PermissionError(13, "Permission denied")


@pytest.fixture
def msvcrt(monkeypatch: pytest.MonkeyPatch) -> FakeMsvcrt:
    """Makes storage take its Windows branch on any system, with the stand-in as the module it imports."""
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    # Only the name storage reads: the real sys.platform stays as it is for pytest and the standard library
    monkeypatch.setattr(storage, "sys", SimpleNamespace(platform="win32"))
    return fake


@pytest.fixture
def lock(tmp_path: Path) -> Path:
    path = tmp_path / "some.lock"
    path.write_bytes(FILLER)
    return path


TAKEN = "Windows locks a range of bytes, so every process must lock the same one: byte 0, one byte long"
FREED = "the byte is freed at byte 0 again, or the lock outlives its holder until the handle closes"
WAYS: List[Any] = [
    pytest.param(storage.locked, LK_LOCK, None, id="locked_waits_with_LK_LOCK"),
    pytest.param(storage.try_locked, LK_NBLCK, True, id="try_locked_never_waits_with_LK_NBLCK"),
]


@pytest.mark.parametrize("take, mode, held", WAYS)
def test_on_windows_a_lock_takes_the_first_byte_and_frees_it_after(
    msvcrt: FakeMsvcrt, lock: Path, take: Callable[[Path], ContextManager[Any]], mode: int, held: Any
) -> None:
    with take(lock) as given:
        assert given is held, "try_locked says True only once the lock is taken, and locked yields nothing"
        assert msvcrt.calls == [(mode, 1, 0)], TAKEN
    assert msvcrt.calls == [(mode, 1, 0), (LK_UNLCK, 1, 0)], FREED
    assert lock.read_bytes() == FILLER, "taking a lock never changes the lock file"


@pytest.mark.parametrize("take, mode, held", WAYS)
def test_on_windows_a_lock_is_freed_when_the_work_under_it_fails(
    msvcrt: FakeMsvcrt, lock: Path, take: Callable[[Path], ContextManager[Any]], mode: int, held: Any
) -> None:
    with pytest.raises(RuntimeError, match="the work failed"), take(lock):
        raise RuntimeError("the work failed")
    assert msvcrt.calls == [(mode, 1, 0), (LK_UNLCK, 1, 0)], (
        "a failure under the lock must still free it, or the session's next hook waits for nothing"
    )


def test_on_windows_a_lock_someone_holds_is_refused_without_waiting(msvcrt: FakeMsvcrt, lock: Path) -> None:
    msvcrt.refused = {LK_NBLCK}
    with storage.try_locked(lock) as held:
        assert held is False, "a lock that another process holds is reported as not taken, never raised: the poller must not fail over it"
    assert msvcrt.calls == [(LK_NBLCK, 1, 0)], (
        "only the mode that never waits is tried (LK_LOCK would retry for ten seconds), and a lock that was not taken is never freed"
    )


def test_on_windows_the_work_does_not_run_when_the_waiting_lock_gives_up(msvcrt: FakeMsvcrt, lock: Path) -> None:
    # The real LK_LOCK tries ten times, a second apart, then raises
    msvcrt.refused = {LK_LOCK}
    ran = []
    with pytest.raises(OSError), storage.locked(lock):
        ran.append("work")
    assert ran == [], "work under a lock that was never taken could run twice at once for one session"
    assert msvcrt.calls == [(LK_LOCK, 1, 0)], "a lock that was not taken is never freed"


def test_off_windows_the_stand_in_is_never_called(monkeypatch: pytest.MonkeyPatch, lock: Path) -> None:
    if sys.platform == "win32":
        pytest.skip("the branch for other systems needs fcntl")
    fake = FakeMsvcrt()
    monkeypatch.setitem(sys.modules, "msvcrt", fake)
    with storage.locked(lock), storage.try_locked(lock) as second:
        assert second is False, "the two locks exclude each other through the real system call"
    assert fake.calls == [], "the platform check, and nothing else, picks the Windows branch"


BOTH: List[Any] = [pytest.param(storage.locked, id="locked"), pytest.param(storage.try_locked, id="try_locked")]
Take = Callable[[Path], ContextManager[Any]]


@links
@pytest.mark.parametrize("take", BOTH)
@pytest.mark.parametrize("exists", [True, False])
def test_a_link_at_a_lock_file_is_removed_and_the_lock_is_taken_on_a_new_file(
    tmp_path: Path, link_flag: int, take: Take, exists: bool
) -> None:
    outside = tmp_path / "outside.lock"
    if exists:
        outside.write_bytes(FILLER)
    lock = tmp_path / "some.lock"
    lock.symlink_to(outside)
    with take(lock) as given:
        assert given is not False, "the lock is taken all the same, or a planted link would stop every hook of the session"
        assert not lock.is_symlink() and lock.is_file()
        with storage.try_locked(lock) as second:
            assert second is False, "the new file is the lock from here on"
    assert outside.read_bytes() == FILLER if exists else not outside.exists(), "the plugin makes and opens no file outside its data folder"


@links
@pytest.mark.parametrize("take", BOTH)
def test_a_link_put_at_a_lock_file_after_the_look_is_refused_by_the_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, take: Take
) -> None:
    outside = tmp_path / "outside.lock"
    lock = tmp_path / "some.lock"
    lock.symlink_to(outside)
    # The look sees no link, as when one is planted between the look and the open
    monkeypatch.setattr(type(lock), "is_symlink", lambda self: False)
    ran = []
    with pytest.raises(OSError), take(lock):
        ran.append("work")
    assert ran == [] and not outside.exists()


@pytest.mark.parametrize("take", BOTH)
def test_a_link_that_another_process_removed_first_does_not_stop_the_lock(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, take: Take
) -> None:
    lock = tmp_path / "some.lock"
    # The look still saw the link, and it is gone by the time it is removed
    monkeypatch.setattr(type(lock), "is_symlink", lambda self: True)
    with take(lock) as given:
        assert given is not False
    monkeypatch.undo()
    assert lock.is_file()


@pytest.mark.parametrize("take", BOTH)
def test_a_new_lock_file_gets_the_mode_any_new_file_gets(tmp_path: Path, take: Take) -> None:
    with open(tmp_path / "plain", "a+"), take(tmp_path / "some.lock"):
        pass
    assert stat.S_IMODE((tmp_path / "some.lock").stat().st_mode) == stat.S_IMODE((tmp_path / "plain").stat().st_mode)


@pipes
@pytest.mark.parametrize("take", BOTH)
def test_a_pipe_at_a_lock_file_fails_the_lock_and_nothing_waits(tmp_path: Path, link_flag: int, take: Take) -> None:
    lock = tmp_path / "some.lock"
    os.mkfifo(lock)
    seen: List[str] = []

    def work() -> None:
        try:
            with take(lock):
                seen.append("work")
        except OSError:
            seen.append("OSError")

    # On another thread, so an open that waits on the pipe fails the test and does not hang it
    waiter = threading.Thread(target=work, daemon=True)
    waiter.start()
    waiter.join(5)
    assert seen == ["OSError"], "no work runs under a lock that is a pipe, and the caller gets the error its own handler logs by type"
    assert stat.S_ISFIFO(lock.stat().st_mode)


@pytest.mark.parametrize("take", BOTH)
def test_a_folder_at_a_lock_file_fails_the_lock_and_leaves_no_descriptor_open(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, take: Take
) -> None:
    lock = tmp_path / "some.lock"
    lock.mkdir()
    opened = []
    real = os.open

    def recorded(*args: Any, **kwargs: Any) -> int:
        opened.append(real(*args, **kwargs))
        return opened[-1]

    monkeypatch.setattr(os, "open", recorded)
    ran = []
    with pytest.raises(OSError), take(lock):
        ran.append("work")
    monkeypatch.undo()
    assert ran == []
    for fd in opened:
        with pytest.raises(OSError):
            os.fstat(fd)
