from __future__ import annotations

import importlib.util
from pathlib import Path
import pytest


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
    answers = {"responses": [{"case_id": "case", "text": "eval has injection risk, but eval is safe here"}]}

    result = reviewer_quality_benchmark.score_suite(suite, answers)

    assert result["caught"] == 1
    assert result["missed"] == 0
    assert result["forbidden_claims"] == 1
    assert result["false_flags"] == 1
    assert result["false_positive_rate"] == 1.0
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
    assert result["forbidden_claims"] == 0
    assert result["false_positive_rate"] == 0.0
    assert result["recall"] == 0.0


def test_reject_any_prevents_negated_match_credit() -> None:
    suite = {
        "cases": [
            {
                "id": "case",
                "expected_findings": [
                    {
                        "id": "bug",
                        "match_any": [["eval", "injection"]],
                        "reject_any": [["no", "injection", "risk"]],
                    }
                ],
                "forbidden_claims": [{"id": "bad_clearance", "match_any": [["eval", "safe"]]}],
            }
        ]
    }
    answers = {"responses": [{"case_id": "case", "text": "eval is safe here, no injection risk"}]}

    result = reviewer_quality_benchmark.score_suite(suite, answers)

    assert result["caught"] == 0
    assert result["missed"] == 1
    assert result["false_flags"] == 1


def test_forbidden_reject_any_prevents_not_safe_false_flag() -> None:
    suite = {
        "cases": [
            {
                "id": "case",
                "expected_findings": [
                    {
                        "id": "bug",
                        "match_any": [["eval", "injection"]],
                        "reject_any": [["eval is safe"], ["no injection risk"]],
                    }
                ],
                "forbidden_claims": [
                    {
                        "id": "bad_clearance",
                        "match_any": [["eval is safe"], ["no injection risk"]],
                        "reject_any": [["not safe"], ["command injection"]],
                    }
                ],
            }
        ]
    }
    answers = {"responses": [{"case_id": "case", "text": "eval has injection risk; it is not safe"}]}

    result = reviewer_quality_benchmark.score_suite(suite, answers)

    assert result["caught"] == 1
    assert result["false_flags"] == 0


def test_eval_is_safe_blocks_bug_credit_and_counts_false_flag() -> None:
    suite = {
        "cases": [
            {
                "id": "case",
                "expected_findings": [
                    {
                        "id": "bug",
                        "match_any": [["eval", "injection"]],
                        "reject_any": [["eval is safe"], ["no injection risk"]],
                    }
                ],
                "forbidden_claims": [
                    {
                        "id": "bad_clearance",
                        "match_any": [["eval is safe"], ["no injection risk"]],
                        "reject_any": [["not safe"], ["command injection"]],
                    }
                ],
            }
        ]
    }
    answers = {"responses": [{"case_id": "case", "text": "eval is safe here, no injection risk"}]}

    result = reviewer_quality_benchmark.score_suite(suite, answers)

    assert result["caught"] == 0
    assert result["missed"] == 1
    assert result["false_flags"] == 1


def test_term_matches_are_token_bounded() -> None:
    assert not reviewer_quality_benchmark.group_matches("yaml.load is unsafe", ["safe"])
    assert not reviewer_quality_benchmark.group_matches("use yaml.safe_load", ["safe"])
    assert reviewer_quality_benchmark.group_matches("use yaml.safe_load", ["safe_load"])


def test_malformed_match_group_has_clear_error() -> None:
    with pytest.raises(ValueError, match="match groups must be lists of strings"):
        reviewer_quality_benchmark.detected("text", [[["bad shape"]]])
