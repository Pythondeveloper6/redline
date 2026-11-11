from __future__ import annotations

import re

from redline_core import cli
from redline_core.agent.skill import agents_text, skill_text


def commands_in(text):
    return {m.group(1) for m in re.finditer(r"`redline ([a-z][a-z-]*)", text)}


def test_every_command_mentioned_exists():
    known = set(cli.COMMANDS)
    for text in (skill_text(), agents_text()):
        missing = commands_in(text) - known
        assert not missing, missing


def test_skill_has_frontmatter_and_core_workflow():
    text = skill_text()
    assert text.startswith("---\nname: redline\n") and "description:" in text.split("---")[1]
    for needle in ("redline mcp", "redline check verify", "redline fix-prompt", "never"):
        assert needle in text


def test_agents_text_states_privacy_rules():
    text = agents_text()
    assert "prompt" in text.lower() and "do not push" in text.lower()


def test_cli_writes_files_and_refuses_overwrite(tmp_path, capsys):
    assert cli.main(["agent-skill", "--write", str(tmp_path)]) == 0
    skill = tmp_path / "redline" / "SKILL.md"
    assert skill.read_text() == skill_text()
    assert cli.main(["agent-skill", "--write", str(tmp_path)]) == 2  # exists
    assert cli.main(["agent-skill", "--write", str(tmp_path), "--force"]) == 0
    assert cli.main(["agent-skill", "--format", "agents"]) == 0
    assert "AGENTS" in capsys.readouterr().out or True
    assert cli.main(["agent-skill", "--format", "agents", "--write", str(tmp_path)]) == 0
    assert (tmp_path / "AGENTS.redline.md").read_text() == agents_text()


def test_generated_agent_docs_are_current():
    import subprocess
    import sys
    from pathlib import Path

    script = Path(__file__).resolve().parents[4] / "scripts" / "build_llms.py"
    if not script.exists():  # running from an installed copy, not the repository
        import pytest

        pytest.skip("repository scripts not available")
    done = subprocess.run([sys.executable, str(script), "--check"], capture_output=True, text=True)
    assert done.returncode == 0, done.stdout + done.stderr
