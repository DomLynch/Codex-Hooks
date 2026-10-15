"""Lead (Frankendom), 2026-10-09: the Mac heavy-job guard refused Deploy's WebKit/real-clock release rows, which Dom's rule says
MUST run on the Mac; Deploy had to use MAC_HEAVY_OK=1 (documented for "VPS down"). Allow the eight named rows by EXACT
command (rows 2,4,22,44,45,48,49,52 of .quality-gate.json release_commands), nothing by pattern."""
from __future__ import annotations

import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))

import deploy_guard as g  # noqa: E402

ROWS = [
    "node scripts/roster-browser-check.mjs",
    "node scripts/audio-preview.mjs --label audio-gate --check",
    "node scripts/arena-audio-check.mjs",
    "node scripts/sparring-browser-check.mjs",
    "node scripts/double-tap-browser-check.mjs",
    "node scripts/browser-replay-check.mjs",
    "node scripts/browser-replay-check.mjs --engine webkit",
    "node scripts/next-fight-black-check.mjs",
    "node scripts/pit-landscape-webkit.mjs --engine webkit",   # row 54
    # rows 9-12 (Lead + Strategy, 2026-10-14/15): the served-rig equality check cannot pass on the VPS, which packs rigs differently
    "node scripts/polearm-browser-check.mjs --opponents executioner --screens desktop",
    "node scripts/polearm-browser-check.mjs --opponents executioner --screens phone",
    "node scripts/polearm-browser-check.mjs --opponents veteran --screens desktop",
    "node scripts/polearm-browser-check.mjs --opponents veteran --screens phone",
]


def test_the_named_mac_rows_are_allowed():
    for cmd in ROWS:
        assert g.MAC_ROW_RE.match(cmd), cmd                         # by exact row, not only by the *webkit* name pattern
        assert g.decide(cmd, None) == "", cmd
        assert g.decide(cmd + " 2>&1", None) == "", cmd           # a redirect does not change what runs
        assert g.decide("DEPLOY_X=1 " + cmd, None) == "", cmd     # a leading env does not either


def test_a_row_not_on_the_list_is_still_refused():
    for cmd in ("node scripts/polearm-pose-check.mjs", "node scripts/other-browser-check.mjs", "node scripts/stance-panel-check.mjs",
                "node scripts/browser-check.mjs", "npm run test:all"):
        assert g.decide(cmd, None) == g.VPS_REASON, cmd


def test_listed_rows_are_matched_exactly_not_by_pattern():
    for cmd in ("node scripts/roster-browser-check.mjs --extra", "node scripts/next-fight-black-check.mjs.bak",
                "node scripts/browser-replay-check.mjs --engine chromium", "node scripts/audio-preview.mjs --label other",
                "node scripts/arena-audio-check.mjs && node scripts/browser-check.mjs",
                "node scripts/polearm-browser-check.mjs", "node scripts/polearm-browser-check.mjs --opponents other --screens phone",
                "node scripts/polearm-browser-check.mjs --opponents veteran --screens phone --extra",
                "node scripts/polearm-browser-check.mjs --opponents veteran --screens phone && npm test"):
        assert g.decide(cmd, None) == g.VPS_REASON, cmd
