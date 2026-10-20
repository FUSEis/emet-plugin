from __future__ import annotations

import sys
from pathlib import Path
from typing import Dict, Iterator

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "claude" / "scripts"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import FakeEmet, Transcript

API_KEY = "Abcd1234.ZyxwVuts98765432QwertyUiop123456"


@pytest.fixture
def emet() -> Iterator[FakeEmet]:
    server = FakeEmet()
    yield server
    server.close()


@pytest.fixture
def transcript(tmp_path: Path) -> Transcript:
    folder = tmp_path / "projects"
    folder.mkdir()
    return Transcript(folder)


@pytest.fixture
def environ(tmp_path: Path, emet: FakeEmet) -> Dict[str, str]:
    return {
        "CLAUDE_PLUGIN_OPTION_API_KEY": API_KEY,
        "CLAUDE_PLUGIN_OPTION_TENANT_REF": "engineering",
        "CLAUDE_PLUGIN_OPTION_ENDPOINT": emet.url,
        "CLAUDE_PLUGIN_DATA": str(tmp_path / "data"),
    }
