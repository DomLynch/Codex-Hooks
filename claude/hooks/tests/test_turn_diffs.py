"""What the reviewer sees of a turn: diffs, commits, earlier-session evidence, and which repo the turn worked in."""
from __future__ import annotations

import contextlib
import io
import json
import sys
import tempfile
import time
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import mimo_audit  # noqa: E402
from lib import audit_state  # noqa: E402





class TurnDiffTests(unittest.TestCase):
    def _repo(self, tmp):
        import subprocess
        subprocess.run(["git", "-C", tmp, "init", "-q"], check=True)
        subprocess.run(["git", "-C", tmp, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "--allow-empty", "-m", "init"], check=True)

    def test_diff_is_scoped_to_edited_paths_only(self) -> None:
        import subprocess
        from lib.transcript import turn_diff

        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            (Path(tmp) / "mine.py").write_text("a = 1\n")
            (Path(tmp) / "theirs.py").write_text("b = 1\n")
            subprocess.run(["git", "-C", tmp, "add", "."], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "base"], check=True)
            (Path(tmp) / "mine.py").write_text("a = 2\n")
            (Path(tmp) / "theirs.py").write_text("b = 2\n")   # another agent's change, same worktree
            out = turn_diff(tmp, [f"{tmp}/mine.py"])
        self.assertIn("-a = 1", out)
        self.assertIn("+a = 2", out)
        self.assertNotIn("theirs.py", out, "other agents' changes must not be attributed to this turn")

    def test_new_untracked_file_is_shown(self) -> None:
        from lib.transcript import turn_diff

        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            (Path(tmp) / "fresh.py").write_text("print('hi')\n")
            out = turn_diff(tmp, [f"{tmp}/fresh.py"])
        self.assertIn("NEW FILE fresh.py", out)
        self.assertIn("print('hi')", out)

    def test_empty_when_not_a_repo_or_no_paths(self) -> None:
        from lib.transcript import turn_diff

        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "x.py").write_text("x")
            self.assertEqual(turn_diff(tmp, [f"{tmp}/x.py"]), "", "not a git repo")
            self._repo(tmp)
            self.assertEqual(turn_diff(tmp, []), "", "nothing edited")

    def test_large_diff_is_capped(self) -> None:
        from lib.transcript import turn_diff, DIFF_CAP

        import subprocess

        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            big = Path(tmp) / "big.py"
            big.write_text("\n".join(f"line_{i} = {i}" for i in range(2000)))
            subprocess.run(["git", "-C", tmp, "add", "."], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "b"], check=True)
            big.write_text("\n".join(f"line_{i} = {i * 2}" for i in range(2000)))   # every line changes
            out = turn_diff(tmp, [f"{tmp}/big.py"])
        self.assertLessEqual(len(out), DIFF_CAP)
        self.assertIn("elided", out)

    def test_main_appends_diff_to_reviewer_input(self) -> None:
        import mimo_audit

        with tempfile.TemporaryDirectory() as tmp:
            self._repo(tmp)
            f = Path(tmp) / "app.py"
            f.write_text("def f():\n    return 1\n")
            import subprocess
            subprocess.run(["git", "-C", tmp, "add", "."], check=True)
            subprocess.run(["git", "-C", tmp, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "b"], check=True)
            f.write_text("def f():\n    return 2\n")
            entries = [("TOOL CALL", f'Edit: [t] {{"file_path": "{f}", "old_string": "1", "new_string": "2"}}')]
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "l.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as model,
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=entries),
            ):
                payload = {"session_id": "G", "turn_id": "t", "transcript_path": "/nope", "cwd": tmp, "last_assistant_message": "Changed f."}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                        mimo_audit.main()
            sent = model.call_args.args[0]
        self.assertIn("WORKING TREE DIFF", sent)
        self.assertIn("-    return 1", sent)
        self.assertIn("+    return 2", sent)


class EarlierSessionEvidenceTests(unittest.TestCase):
    """The answer cites a SHA / #PR / time / pass count from an earlier turn; the reviewer gets that line."""

    def _transcript(self, tmp: str) -> str:
        path = Path(tmp) / "t.jsonl"
        rows = [
            {"type": "user", "timestamp": "2026-09-26T10:00:00Z", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "a", "content": "noise\nMerged PR #21 as a6a4f4e\n412 passed in 9.1s"}]}},
            {"type": "assistant", "timestamp": "2026-09-26T10:01:00Z", "message": {"content": [{"type": "text", "text": "I claim 3774da8 is live"}]}},
            {"type": "user", "timestamp": "2026-09-26T10:02:00Z", "message": {"content": "Another Claude session sent a message:\n<cross-session-message>deployed 3774da8 at 10:02</cross-session-message>"}},
            {"type": "user", "timestamp": "2026-09-26T10:03:00Z", "message": {"content": "my own note 3774da8"}},
            {"type": "user", "timestamp": "2026-09-26T11:00:00Z", "message": {"content": [
                {"type": "tool_result", "tool_use_id": "b", "content": [{"type": "text", "text": "this turn: a6a4f4e #21"}]}]}},
        ]
        path.write_text("\n".join(json.dumps(r) for r in rows) + "\n")
        return str(path)

    def test_finds_cited_facts_the_slice_lacks_in_earlier_tool_output_and_peer_messages(self) -> None:
        from lib.claude_adapter import earlier_session_evidence

        with tempfile.TemporaryDirectory() as tmp:
            tp = self._transcript(tmp)
            answer = "#21 merged as a6a4f4e, 412 passed; 3774da8 went live."
            got = earlier_session_evidence(tp, answer, "slice mentions nothing", "2026-09-26T11:00:00Z")
        joined = "\n".join(got)
        self.assertIn("Merged PR #21 as a6a4f4e", joined)
        self.assertIn("412 passed in 9.1s", joined)
        self.assertIn("deployed 3774da8 at 10:02", joined)
        self.assertNotIn("noise", joined)
        self.assertNotIn("I claim", joined, "the agent's own words are not evidence")
        self.assertNotIn("my own note", joined, "only tool output and peer messages")
        self.assertNotIn("this turn", joined, "the current turn is already in the slice")
        self.assertTrue(joined.index("3774da8") < joined.index("a6a4f4e"), "newest first")

    def test_nothing_when_the_slice_already_has_the_facts_or_the_answer_cites_none(self) -> None:
        from lib.claude_adapter import earlier_session_evidence

        with tempfile.TemporaryDirectory() as tmp:
            tp = self._transcript(tmp)
            self.assertEqual(earlier_session_evidence(tp, "#21 is a6a4f4e", "#21 a6a4f4e", "2026-09-26T11:00:00Z"), [])
            self.assertEqual(earlier_session_evidence(tp, "Added the deadline field.", "", "2026-09-26T11:00:00Z"), [])
            self.assertEqual(earlier_session_evidence(tp, "#21", "", "2026-09-26T11:00:00Z", max_chars=10), [])

    def test_main_feeds_the_section_and_rule_to_the_reviewer(self) -> None:
        import mimo_audit

        self.assertIn("EARLIER IN THIS SESSION lines", mimo_audit.AUDIT_PROMPT)
        with tempfile.TemporaryDirectory() as tmp:
            tp = self._transcript(tmp)
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "l.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=[("USER", "status?")]),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as model,
            ):
                payload = {"session_id": "EE", "turn_id": "t", "transcript_path": tp, "cwd": tmp,
                           "last_assistant_message": "PR #21 merged as a6a4f4e and 412 passed. " * 5}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    with redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):
                        mimo_audit.main()
        sent = model.call_args[0][0]
        self.assertIn("EARLIER IN THIS SESSION (tool output", sent)
        self.assertIn("[10:00] Merged PR #21 as a6a4f4e", sent)


class TurnDiffSinceTests(unittest.TestCase):
    """Edits the agent committed during the turn still reach the reviewer."""

    def test_diff_covers_commits_made_after_the_turn_started(self) -> None:
        import subprocess
        from lib.transcript import turn_diff

        with tempfile.TemporaryDirectory() as tmp:
            repo = Path(tmp)

            def git(*a):
                subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=repo, check=True, capture_output=True)

            git("init", "-q", "-b", "main")
            (repo / "app.py").write_text("v1\n")
            git("add", ".")
            git("commit", "-qm", "base")
            time.sleep(1.1)
            started = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
            time.sleep(1.1)
            (repo / "app.py").write_text("v2\n")
            git("commit", "-qam", "turn edit")  # committed inside the turn
            self.assertEqual(turn_diff(str(repo), ["app.py"]), "", "plain HEAD diff sees nothing once committed")
            since = turn_diff(str(repo), ["app.py"], since=started)
            self.assertIn("-v1", since)
            self.assertIn("+v2", since)
            self.assertEqual(turn_diff(str(repo), ["app.py"], since="not-a-date"), "", "unresolvable timestamp falls back to the plain HEAD diff")


class TurnCommitsDiffTests(unittest.TestCase):
    """The reviewer gets the code the turn COMMITTED, however it was edited (2026-09-27: the hooks lane and the
    Auditer edit via Bash, so edited_paths() was empty and 7 code turns were judged from prose alone)."""

    def _repo(self, tmp: str):
        import subprocess
        repo = Path(tmp)

        def git(*a):
            return subprocess.run(["git", "-c", "user.email=t@t", "-c", "user.name=t", *a], cwd=repo, check=True,
                                  capture_output=True, text=True).stdout

        git("init", "-q", "-b", "main")
        (repo / "hook.py").write_text("OLD = 1\n")
        git("add", ".")
        git("commit", "-qm", "base")
        return repo, git

    def test_only_this_turns_commits_code_patch_and_test_names(self) -> None:
        from lib.transcript import turn_commits, turn_commits_diff

        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self._repo(tmp)
            time.sleep(1.1)
            started = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            time.sleep(1.1)
            git("switch", "-q", "-c", "lane/fix")                      # a checkout is not a commit
            (repo / "hook.py").write_text("OLD = 1\nMATCH = 'buggy line'\n")
            (repo / "tests").mkdir()
            (repo / "tests" / "test_hook.py").write_text("def test_x():\n    assert 'SECRET_TEST_BODY'\n")
            git("add", ".")
            git("commit", "-qm", "feat: new matcher")               # made by a Bash script, not the Edit tool
            shas = turn_commits(str(repo), started)
            self.assertEqual(len(shas), 1)
            diff = turn_commits_diff(str(repo), started)
            self.assertIn("=== commit", diff)
            self.assertIn("+MATCH = 'buggy line'", diff)
            self.assertIn("tests/test_hook.py", diff, "test files are named in the file list")
            self.assertNotIn("SECRET_TEST_BODY", diff, "but their bodies do not use the reviewer's budget")
            self.assertNotIn("OLD = 1\n+", diff.split("=== commit")[0])
            self.assertEqual(turn_commits(str(repo), "2099-01-01T00:00:00Z"), [], "nothing after a future start")
            self.assertEqual(turn_commits(str(repo), "not-a-date"), [])
            self.assertEqual(turn_commits_diff(str(Path(tmp) / "nope"), started), "")

    def test_main_sends_a_bash_only_turns_committed_code_to_the_reviewer(self) -> None:

        with tempfile.TemporaryDirectory() as tmp:
            repo, git = self._repo(tmp)
            time.sleep(1.1)
            started = time.strftime("%Y-%m-%dT%H:%M:%S.000Z", time.gmtime())
            tp = Path(tmp) / "t.jsonl"
            tp.write_text(json.dumps({"type": "user", "timestamp": started, "message": {"content": "add the guard"}}) + "\n")
            time.sleep(1.1)
            (repo / "hook.py").write_text("OLD = 1\nGUARD = 'committed via bash'\n")
            git("commit", "-qam", "feat: guard")
            bash = ("TOOL CALL", 'Bash: [toolu_1] {"command": "python3 - <<EOF ... write_text ... EOF && git commit -qam guard"}')
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(tmp) / "s.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(tmp) / "v.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(tmp) / "l.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=[("USER", "add the guard"), bash]),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as model,
            ):
                payload = {"session_id": "TC", "turn_id": "t", "transcript_path": str(tp), "cwd": str(repo),
                           "last_assistant_message": "Added the guard and committed it. " * 5}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    with redirect_stdout(io.StringIO()), contextlib.suppress(SystemExit):
                        mimo_audit.main()
        sent = model.call_args[0][0]
        self.assertIn("WORKING TREE DIFF", sent)
        self.assertIn("+GUARD = 'committed via bash'", sent)

    def test_prompt_asks_for_a_concrete_wrong_input_on_new_matchers(self) -> None:
        self.assertIn("try one input it should NOT match", mimo_audit.AUDIT_PROMPT)
        self.assertIn("Do not raise\n  it when you cannot name a concrete input.", mimo_audit.AUDIT_PROMPT)


class RepoCwdResolutionTests(unittest.TestCase):
    """Regression: session cwd is a non-git parent; the diff must still reach the reviewer."""

    @staticmethod
    def _repo(path: Path) -> Path:
        import subprocess
        path.mkdir()
        subprocess.run(["git", "-C", str(path), "init", "-q"], check=True)
        (path / "a.txt").write_text("a\n")
        subprocess.run(["git", "-C", str(path), "add", "."], check=True)
        subprocess.run(["git", "-C", str(path), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "b"], check=True)
        return path.resolve()

    def test_edited_files_in_another_repo_win_over_launch_repo(self) -> None:
        # Lanes launched from the desktop app's project folder (a git repo) edit
        # a worktree of a different repo all turn; side effects belong there.
        from lib.git import resolve_git_cwd

        with tempfile.TemporaryDirectory() as parent:
            launch = self._repo(Path(parent) / "launch")
            other = self._repo(Path(parent) / "other")
            edited = [other / "src" / "x.ts", other / "y.ts"]
            self.assertEqual(resolve_git_cwd(launch, edited), other)
            # Any edit inside the launch repo keeps it authoritative.
            self.assertEqual(resolve_git_cwd(launch, edited + [launch / "z.py"]), launch)
            # Two foreign repos: ambiguous, keep the launch repo.
            third = self._repo(Path(parent) / "third")
            self.assertEqual(resolve_git_cwd(launch, [other / "x.ts", third / "y.ts"]), launch)
            self.assertEqual(resolve_git_cwd(launch, []), launch)

    def test_diff_fed_when_session_cwd_is_parent_workspace(self) -> None:
        import subprocess
        import mimo_audit

        with tempfile.TemporaryDirectory() as parent:
            repo = Path(parent) / "proj"
            repo.mkdir()
            subprocess.run(["git", "-C", str(repo), "init", "-q"], check=True)
            f = repo / "app.py"
            f.write_text("x = 1\n")
            subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
            subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-q", "-m", "b"], check=True)
            f.write_text("x = 2\n")
            entries = [("TOOL CALL", f'Edit: [t] {{"file_path": "{f}", "old_string": "1", "new_string": "2"}}')]
            with (
                mock.patch.object(audit_state, "STATE_FILE", Path(parent) / "s.json"),
                mock.patch.object(audit_state, "VERDICT_FILE", Path(parent) / "v.json"),
                mock.patch.object(mimo_audit.ledger, "LEDGER_PATH", Path(parent) / "l.json"),
                mock.patch.object(mimo_audit, "log"),
                mock.patch.object(mimo_audit, "ask_mimo_with_retry", return_value="VERDICT: PASS") as model,
                mock.patch.object(mimo_audit, "latest_task_slice_claude", return_value="x" * 600 + " TOOL CALL edit implemented"),
                mock.patch.object(mimo_audit, "latest_task_entries_claude", return_value=entries),
            ):
                # cwd is the PARENT (not a git repo) — exactly what Claude Code passes in practice
                payload = {"session_id": "C", "turn_id": "t", "transcript_path": "/nope", "cwd": parent, "last_assistant_message": "Changed x."}
                with mock.patch.object(mimo_audit.sys, "stdin", io.StringIO(json.dumps(payload))):
                    with redirect_stdout(io.StringIO()), self.assertRaises(SystemExit):
                        mimo_audit.main()
            sent = model.call_args.args[0]
        self.assertIn("WORKING TREE DIFF", sent)
        self.assertIn("+x = 2", sent)
