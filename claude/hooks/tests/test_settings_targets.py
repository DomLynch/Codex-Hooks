"""Every hook command the shipped settings register must point at a script that exists in this tree.

2026-09-21: a PreToolUse entry was registered one step before its script was copied to ~/.claude/hooks, and because
the copy was itself a Bash tool call, every session's Bash failed for ~9 minutes. This pins the order: script first.
"""
import json
import re
import unittest
from pathlib import Path

HOOKS = Path(__file__).resolve().parents[1]
SETTINGS = HOOKS.parent / "config" / "settings.example.json"


class SettingsTargetsTests(unittest.TestCase):
    def test_every_registered_hook_script_exists_in_the_tree(self):
        data = json.loads(SETTINGS.read_text())
        commands = [h["command"] for groups in data["hooks"].values() for g in groups for h in g["hooks"]]
        self.assertTrue(commands)
        missing = []
        for command in commands:
            m = re.search(r"python3 \S*/\.claude/hooks/(\S+)", command)
            self.assertIsNotNone(m, command)
            if not (HOOKS / m.group(1)).is_file():
                missing.append(m.group(1))
        self.assertEqual(missing, [], f"settings.example.json registers scripts that are not in claude/hooks: {missing}")

    def test_deploy_guard_is_registered_on_bash(self):
        data = json.loads(SETTINGS.read_text())
        bash = [g for g in data["hooks"]["PreToolUse"] if g.get("matcher") == "Bash"]
        self.assertTrue(any("deploy_guard.py" in h["command"] for g in bash for h in g["hooks"]))
