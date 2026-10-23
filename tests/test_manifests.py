"""Keeps the manifests, the code and the docs consistent with each other."""

import json
import re
import shutil
import subprocess
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
    project = re.search(r'^version = "(.+)"$', (ROOT / "pyproject.toml").read_text(encoding="utf-8"), re.M)
    assert project is not None
    assert plugin["version"] == VERSION == entry["version"] == project.group(1)


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


def test_users_set_only_the_key_team_email_and_capture_mode():
    options = load(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]
    assert set(options) == {"emet_key", "tenant_ref", "user_email", "capture_mode"}


# Every host a published file names; anything else, such as an internal Emet, must not ship
PUBLIC_HOSTS = {
    "app.emet.so",
    "emet.so",
    "github.com",
    "code.claude.com",
    "docs.astral.sh",
    "keepachangelog.com",
    "semver.org",
    "www.apache.org",
    "hooks.slack.com",
    "localhost",
    "127.0.0.1",
}


def test_published_files_name_only_public_hosts():
    tracked = subprocess.run([str(shutil.which("git")), "ls-files", "-z"], cwd=ROOT, capture_output=True, check=True).stdout.decode()
    for name in filter(None, tracked.split("\0")):
        if name.startswith("tests/") or name == "uv.lock":
            continue
        text = (ROOT / name).read_text(encoding="utf-8", errors="ignore").replace("\\.", ".")
        for host in re.findall(r"https?://([A-Za-z0-9.-]+)", text):
            assert host.lower() in PUBLIC_HOSTS, f"{name} names {host}"


def test_the_key_is_sensitive():
    assert load(PLUGIN / ".claude-plugin" / "plugin.json")["userConfig"]["emet_key"]["sensitive"] is True


def test_plugin_folder_is_self_contained():
    for name in ("README.md", "LICENSE"):
        assert (PLUGIN / name).is_file(), name
    for path in PLUGIN.rglob("*"):
        assert not path.is_symlink(), path
        assert path.name != ".DS_Store"
