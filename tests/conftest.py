from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "claude" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from emet_claude import pause, storage
from helpers import Clock, FakeEmet, Transcript

API_KEY = "Abcd1234.ZyxwVuts98765432QwertyUiop123456"


@pytest.fixture
def emet() -> Iterator[FakeEmet]:
    server = FakeEmet()
    yield server
    server.close()


@pytest.fixture(autouse=True)
def clock(monkeypatch: pytest.MonkeyPatch) -> Clock:
    """The pause reads this clock in every test. Advance it past a pause to let the next run send."""
    fake = Clock()
    monkeypatch.setattr(pause, "clock", lambda: fake.now)
    monkeypatch.setattr(pause, "spread", lambda: fake.spread)
    return fake


@pytest.fixture
def transcript(tmp_path: Path) -> Transcript:
    folder = tmp_path / "projects"
    folder.mkdir()
    return Transcript(folder)


@pytest.fixture
def environ(tmp_path: Path, emet: FakeEmet) -> Dict[str, str]:
    return {
        "CLAUDE_PLUGIN_OPTION_EMET_KEY": API_KEY,
        "CLAUDE_PLUGIN_OPTION_TENANT_REF": "engineering",
        "CLAUDE_PLUGIN_OPTION_ENDPOINT": emet.url,
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
        # Never the developer's own Claude account
        "CLAUDE_CONFIG_DIR": str(tmp_path / "claude-config"),
    }


@pytest.fixture(params=["as the system is", "as on Windows"])
def link_flag(request, monkeypatch):
    """Runs a test as the system is, and again without the flag that refuses a link at the open, which Windows does not have."""
    if request.param == "as on Windows":
        monkeypatch.setattr(storage, "READ_FLAGS", storage.READ_FLAGS & ~storage.NOFOLLOW)
        monkeypatch.setattr(storage, "NOFOLLOW", 0)
    return storage.NOFOLLOW
