from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "reviewer_quality_benchmark.py"

spec = importlib.util.spec_from_file_location("reviewer_quality_benchmark", SCRIPT)
assert spec and spec.loader
reviewer_quality_benchmark = importlib.util.module_from_spec(spec)
spec.loader.exec_module(reviewer_quality_benchmark)


def test_scores_expected_findings_and_false_flags() -> None:
    suite = {
        "name": "unit",
        "cases": [
            {
                "id": "case",
                "expected_findings": [{"id": "bug", "match_any": [["eval", "injection"]]}],
                "forbidden_claims": [{"id": "bad_clearance", "match_any": [["eval", "safe"]]}],
            }
        ],
    }
    answers = {"responses": [{"case_id": "case", "text": "eval has injection risk; it is not safe"}]}

    result = reviewer_quality_benchmark.score_suite(suite, answers)

    assert result["caught"] == 1
    assert result["missed"] == 0
    assert result["false_flags"] == 1
    assert result["recall"] == 1.0


def test_missing_response_counts_as_miss() -> None:
    suite = {
        "cases": [
            {"id": "case", "expected_findings": [{"id": "bug", "match_any": [["yaml.load", "unsafe"]]}]}
        ]
    }

    result = reviewer_quality_benchmark.score_suite(suite, {"responses": []})

    assert result["caught"] == 0
    assert result["missed"] == 1
    assert result["recall"] == 0.0
