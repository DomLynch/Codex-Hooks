#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SUITE = ROOT / "EVALS" / "reviewer_quality_cases.json"


def load_json(path: Path) -> Any:
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.casefold())


def group_matches(text: str, group: list[str]) -> bool:
    haystack = normalize(text)
    return all(term.casefold() in haystack for term in group)


def detected(text: str, match_any: list[list[str]]) -> bool:
    return any(group_matches(text, group) for group in match_any)


def responses_by_case(answer_payload: Any) -> dict[str, str]:
    if isinstance(answer_payload, dict):
        answer_payload = answer_payload.get("responses", [])
    out: dict[str, str] = {}
    for item in answer_payload:
        case_id = str(item.get("case_id", "")).strip()
        if case_id:
            out[case_id] = str(item.get("text", ""))
    return out


def score_suite(suite: dict[str, Any], answer_payload: Any) -> dict[str, Any]:
    answers = responses_by_case(answer_payload)
    cases = suite.get("cases", [])
    rows: list[dict[str, Any]] = []
    expected_total = 0
    detected_total = 0
    false_total = 0

    for case in cases:
        text = answers.get(case["id"], "")
        expected = case.get("expected_findings", [])
        forbidden = case.get("forbidden_claims", [])
        caught = [finding["id"] for finding in expected if detected(text, finding["match_any"])]
        false_flags = [claim["id"] for claim in forbidden if detected(text, claim["match_any"])]
        expected_total += len(expected)
        detected_total += len(caught)
        false_total += len(false_flags)
        rows.append(
            {
                "case_id": case["id"],
                "caught": caught,
                "missed": [finding["id"] for finding in expected if finding["id"] not in caught],
                "false_flags": false_flags,
                "expected": len(expected),
            }
        )

    recall = detected_total / expected_total if expected_total else 1.0
    return {
        "suite": suite.get("name", "reviewer-quality"),
        "cases": len(cases),
        "expected_findings": expected_total,
        "caught": detected_total,
        "missed": expected_total - detected_total,
        "false_flags": false_total,
        "recall": round(recall, 4),
        "rows": rows,
    }


def print_summary(result: dict[str, Any]) -> None:
    print(
        f"{result['suite']}: caught={result['caught']}/{result['expected_findings']} "
        f"recall={result['recall']:.2%} false_flags={result['false_flags']}"
    )
    for row in result["rows"]:
        print(
            f"- {row['case_id']}: caught={','.join(row['caught']) or '-'} "
            f"missed={','.join(row['missed']) or '-'} false={','.join(row['false_flags']) or '-'}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Score saved GPT/Claude/MiMo review output against known bug signatures.")
    parser.add_argument("--suite", type=Path, default=DEFAULT_SUITE)
    parser.add_argument("--answers", type=Path, help="JSON file with reviewer responses.")
    parser.add_argument("--list-cases", action="store_true")
    parser.add_argument("--json", action="store_true", help="Emit machine-readable score JSON.")
    parser.add_argument("--min-recall", type=float, default=0.0, help="Fail if recall is below this threshold.")
    args = parser.parse_args()

    suite = load_json(args.suite)
    if args.list_cases:
        for case in suite.get("cases", []):
            print(f"{case['id']}: {case.get('title', '')}")
        return 0
    if not args.answers:
        raise SystemExit("--answers is required unless --list-cases is used")

    result = score_suite(suite, load_json(args.answers))
    if args.json:
        print(json.dumps(result, indent=2, sort_keys=True))
    else:
        print_summary(result)
    return 0 if result["recall"] >= args.min_recall else 1


if __name__ == "__main__":
    raise SystemExit(main())
