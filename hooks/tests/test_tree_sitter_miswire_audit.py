from __future__ import annotations

import importlib.util
import subprocess
from pathlib import Path
from unittest import mock


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "tree_sitter_miswire_audit.py"

spec = importlib.util.spec_from_file_location("tree_sitter_miswire_audit", SCRIPT)
assert spec and spec.loader
tree_sitter_miswire_audit = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tree_sitter_miswire_audit)


def test_resolves_allowlisted_repo_alias() -> None:
    target = tree_sitter_miswire_audit.resolve_target("hooks")

    assert target == ROOT


def test_installed_copy_keeps_canonical_alias(tmp_path: Path) -> None:
    with mock.patch.object(tree_sitter_miswire_audit, "__file__", str(tmp_path / "scripts" / "audit.py")):
        assert tree_sitter_miswire_audit.resolve_target("hooks") == tree_sitter_miswire_audit.ALLOWLIST["hooks"].resolve()


def test_build_cmd_uses_uvx_miswire_audit() -> None:
    with mock.patch("shutil.which", return_value="/bin/uvx"):
        cmd = tree_sitter_miswire_audit.build_cmd(ROOT)

    assert cmd[:4] == ["/bin/uvx", "--from", "tree-sitter-analyzer", "miswire-audit"]
    assert cmd[-1] == str(ROOT)


def test_run_audit_removes_generated_ast_cache(tmp_path: Path) -> None:
    def create_cache(_cmd: list[str]) -> subprocess.CompletedProcess[str]:
        (tmp_path / ".ast-cache").mkdir()
        return subprocess.CompletedProcess(_cmd, 0)

    with mock.patch("subprocess.run", side_effect=create_cache):
        rc = tree_sitter_miswire_audit.run_audit(["uvx"], tmp_path)

    assert rc == 0
    assert not (tmp_path / ".ast-cache").exists()
