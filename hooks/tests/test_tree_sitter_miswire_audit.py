from __future__ import annotations

import importlib.util
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


def test_build_cmd_uses_uvx_miswire_audit() -> None:
    with mock.patch("shutil.which", return_value="/bin/uvx"):
        cmd = tree_sitter_miswire_audit.build_cmd(ROOT)

    assert cmd[:4] == ["/bin/uvx", "--from", "tree-sitter-analyzer", "miswire-audit"]
    assert cmd[-1] == str(ROOT)
