"""Keeps the manifests, the code and the docs consistent with each other."""

import json
import re
from pathlib import Path

from emet_claude import VERSION

ROOT = Path(__file__).resolve().parent.parent
PLUGIN = ROOT / "claude"


def load(path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_versions_agree():
    plugin = load(PLUGIN / ".claude-plugin" / "plugin.json")
    marketplace = load(ROOT / ".claude-plugin" / "marketplace.json")
    (entry,) = marketplace["plugins"]
    assert plugin["version"] == VERSION == entry["version"]


def test_changelog_has_the_current_version():
    assert f"## [{VERSION}]" in (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")


def test_marketplace_points_at_the_plugin():
    marketplace = load(ROOT / ".claude-plugin" / "marketplace.json")
    (entry,) = marketplace["plugins"]
    assert (ROOT / entry["source"]).resolve() == PLUGIN
    assert entry["name"] == load(PLUGIN / ".claude-plugin" / "plugin.json")["name"]


def test_every_hook_runs_a_known_mode_of_the_shipped_script():
    from emet_claude.commands import HOOK_MODES

    hooks = load(PLUGIN / "hooks" / "hooks.json")["hooks"]
    assert set(hooks) == {"SessionStart", "Stop", "SubagentStop", "SessionEnd"}
    for groups in hooks.values():
        for group in groups:
            for hook in group["hooks"]:
                match = re.fullmatch(r'python3 "\$\{CLAUDE_PLUGIN_ROOT\}/(scripts/emet_hook\.py)" ([a-z-]+)', hook["command"])
                assert match, hook["command"]
                assert (PLUGIN / match.group(1)).is_file()
                assert match.group(2) in HOOK_MODES


def test_capture_mode_choices_match_the_code():
    from emet_claude.config import CAPTURE_MODES, DEFAULT_CAPTURE_MODE

    option = load(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]["capture_mode"]
    assert set(option["options"]) == set(CAPTURE_MODES)
    assert option["default"] == DEFAULT_CAPTURE_MODE


def test_the_key_is_sensitive():
    assert load(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]["api_key"]["sensitive"] is True


def test_plugin_folder_is_self_contained():
    for name in ("README.md", "LICENSE"):
        assert (PLUGIN / name).is_file(), name
    for path in PLUGIN.rglob("*"):
        assert not path.is_symlink(), path
        assert path.name != ".DS_Store"
