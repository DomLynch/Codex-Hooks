import io
import json
import subprocess
import sys
import tempfile
import unittest
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import mimo_audit  # noqa: E402
import post_tool  # noqa: E402
import stop_orchestrator  # noqa: E402


class AuditSafetyTests(unittest.TestCase):
    def test_hooks_never_publish_or_start_another_task(self):
        for request in ('Audit only; do not commit or start another task.', 'Fix the bug.'):
            with self.subTest(request=request), tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
                root = Path(tmp)
                def git(*args):
                    return subprocess.check_output(['git', *args], cwd=root, text=True).strip()
                git('init', '-q', '-b', 'main')
                git('config', 'user.email', 'test@example.invalid')
                git('config', 'user.name', 'Hook Test')
                (root / 'existing.txt').write_text('baseline')
                git('add', '.')
                git('commit', '-qm', 'baseline')
                (root / 'current-task.md').write_text('my work')
                (root / 'unrelated-task.md').write_text('someone else\'s work')
                git('add', 'unrelated-task.md')
                before = (git('rev-parse', 'HEAD'), git('status', '--porcelain'), git('branch', '--show-current'))
                payload = {'session_id': 'safety', 'turn_id': 'turn', 'cwd': tmp,
                           'tool_name': 'Write', 'tool_input': {'file_path': str(root / 'current-task.md')},
                           'last_assistant_message': 'Done.'}
                publish = stack.enter_context(mock.patch.object(stop_orchestrator, 'commit_push_pr', create=True))
                next_task = stack.enter_context(mock.patch.object(stop_orchestrator, 'next_task', create=True))
                checkpoint = stack.enter_context(mock.patch.object(post_tool, 'maybe_checkpoint', create=True))
                for module in (post_tool, stop_orchestrator):
                    stack.enter_context(mock.patch.object(module, 'remember_repo'))
                    stack.enter_context(mock.patch.object(module, 'log'))
                    stack.enter_context(mock.patch.object(module, 'pending_quality', return_value=[]))
                for name in ('update_checkpoint_state', 'update_audit_state', 'maybe_mimo_checkpoint_audit'):
                    stack.enter_context(mock.patch.object(post_tool, name, create=True))
                stack.enter_context(mock.patch.object(stop_orchestrator, 'read_session_spec', return_value={}))
                stack.enter_context(mock.patch.object(stop_orchestrator, 'audit_with_claude', return_value=True))
                stack.enter_context(mock.patch.object(stop_orchestrator, 'wait_for_mimo_pass', return_value=True))
                stack.enter_context(mock.patch.object(stop_orchestrator, 'latest_task_slice', return_value='USER:\n' + request))
                stack.enter_context(mock.patch.object(post_tool, 'sync_codegraph'))
                for module in (post_tool, stop_orchestrator):
                    with mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()):
                        try:
                            module.main()
                        except SystemExit:
                            pass
                checkpoint.assert_not_called()
                publish.assert_not_called()
                next_task.assert_not_called()
                self.assertEqual(before, (git('rev-parse', 'HEAD'), git('status', '--porcelain'), git('branch', '--show-current')))

    def test_complete_request_survives_review_compaction(self):
        request = 'Background. ' * 150 + 'Create all 18 separate Codex tasks now.' + ' Details. ' * 200
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'turn.jsonl'
            rows = [('user', request), ('assistant', 'Investigation. ' * 1000),
                    ('assistant', 'I did not create the separate tasks.')]
            records = [{'type': 'message', 'role': role, 'content': body} for role, body in rows]
            records.insert(1, {'type': 'function_call_output', 'output': 'Inspection finished; no tasks created.'})
            path.write_text('\n'.join(json.dumps({'type': 'response_item', 'payload': row}) for row in records))
            text = mimo_audit.read_task_slice(str(path))
            self.assertIn(request, text)
            self.assertIn('I did not create the separate tasks.', text)
            self.assertLessEqual(len(text), len(request) + 4200)
            with ExitStack() as stack:
                for name, filename in [('STATE_FILE', 'iterations.json'), ('VERDICT_FILE', 'verdict.json'), ('LOG_FILE', 'audit.log')]:
                    stack.enter_context(mock.patch.object(mimo_audit, name, Path(tmp) / filename))
                stack.enter_context(mock.patch.object(mimo_audit.ledger, 'LEDGER_PATH', Path(tmp) / 'ledger.json'))
                review = stack.enter_context(mock.patch.object(mimo_audit, 'ask_mimo_with_retry', side_effect=['', 'VERDICT: PASS']))
                payload = {'session_id': 'request-test', 'turn_id': 'turn', 'cwd': tmp,
                           'transcript_path': str(path), 'last_assistant_message': rows[-1][1]}
                with mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit):
                        mimo_audit.main()
                self.assertEqual(review.call_count, 2)
                for call in review.call_args_list:
                    self.assertIn(request, call.args[0])

    def test_non_repository_is_never_indexed(self):
        with tempfile.TemporaryDirectory() as tmp, mock.patch.object(post_tool.subprocess, 'run') as run:
            post_tool.sync_codegraph(Path(tmp))
        run.assert_not_called()

    def test_incomplete_fix_is_not_reclassified_as_advice(self):
        finding = ('[quote: "its underlying performance defect still needs repair."] '
                   'The fix remains incomplete: claim cards were disabled to restore paper loading. '
                   'Repair the claim-card lookup, then verify paper loading and claim-card behavior together.')
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            root = Path(tmp)
            for name, filename in [('STATE_FILE', 'iterations.json'), ('VERDICT_FILE', 'verdict.json'), ('LOG_FILE', 'audit.log')]:
                stack.enter_context(mock.patch.object(mimo_audit, name, root / filename))
            stack.enter_context(mock.patch.object(mimo_audit.ledger, 'LEDGER_PATH', root / 'ledger.json'))
            stack.enter_context(mock.patch.object(mimo_audit, 'record_lesson'))
            review = stack.enter_context(mock.patch.object(mimo_audit, 'ask_mimo_with_retry', return_value='VERDICT: ISSUES\n1. ' + finding))
            payload = {'session_id': 'safety', 'turn_id': 'incomplete', 'cwd': tmp,
                       'last_assistant_message': 'I fixed paper loading; its underlying performance defect still needs repair.'}
            with mock.patch('sys.stdin', io.StringIO(json.dumps(payload))), redirect_stdout(io.StringIO()) as out:
                with self.assertRaises(SystemExit):
                    mimo_audit.main()
            self.assertEqual(json.loads(out.getvalue())['decision'], 'block')
            review.assert_called_once()


if __name__ == '__main__':
    unittest.main()
