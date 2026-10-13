"""Per-lane context: policy injection, context-size notices, lane names, memory/state-doc receipts, handoff and restart notes."""
from __future__ import annotations

import io
import json
import sys
from pathlib import Path

HOOKS_DIR = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HOOKS_DIR))






def test_policy_injected_once_then_pointer(tmp_path, monkeypatch):
    """1,405 full-contract injections on 2026-09-22, the same ~2,150 tokens each time. Send it on the first
    project prompt and every REFRESH_EVERY after (compaction can drop it); a pointer in between."""
    import user_prompt_submit as u
    from pathlib import Path
    monkeypatch.setattr(u, "POLICY_STATE", tmp_path / "policy.json")
    monkeypatch.setattr(u, "active_lessons", lambda lane: ["a lesson"])
    first = u.context_for("fix src/a.ts", Path("/tmp"), "s1")
    assert "Apply before acting." in first and "a lesson" in first
    for _ in range(u.REFRESH_EVERY - 1):
        later = u.context_for("fix src/a.ts", Path("/tmp"), "s1")
        assert "Apply before acting." not in later
        assert "still applies" in later and "a lesson" in later  # lessons always sent
        assert len(later) < len(first)
    assert "Apply before acting." in u.context_for("fix src/a.ts", Path("/tmp"), "s1")  # refresh
    # a different session gets its own full copy
    assert "Apply before acting." in u.context_for("fix src/a.ts", Path("/tmp"), "s2")
    # non-project prompts are unaffected
    assert "Current-Prompt Discipline" in u.context_for("hello", Path("/tmp"), "s1")


def test_auditor_clears_at_700k(tmp_path):
    """Dom, 2026-10-07 22:4x: the Auditor reads whole diffs and was clearing at 500k mid-review; it got 600k, and 700k on 2026-10-08."""
    from lib import context_size as cs
    t = tmp_path / "t.jsonl"
    for tokens, expect_notice in ((620_000, False), (720_000, True)):
        t.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": tokens}}}) + "\n")
        line, _ = cs.context_notice(str(t), "/x/frankendom-code-quality")
        assert bool(line) is expect_notice
        if expect_notice:
            assert "720k" in line and "700k one" in line


def test_coordinating_lanes_warn_later(tmp_path):
    """Dom, 2026-10-08: Lead clears at 750k, Strategy and Deploy at 700k; every other lane at 600k (500k/600k/600k before)."""
    from lib import context_size as cs
    from lib import handoff_check as hc
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 620_000}}}) + "\n")
    for lane in ("lead-catalogue", "strategy", "deploy"):
        assert cs.context_notice(str(t), f"/x/frankendom-{lane}") == ("", False)
        assert hc.handoff_notice(str(t), Path(f"/x/frankendom-{lane}"), "PR merged") == ""
    line, _ = cs.context_notice(str(t), "/x/frankendom-weapons")
    assert "620k" in line and "600k one" in line
    t.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 720_000}}}) + "\n")
    line, _ = cs.context_notice(str(t), "/x/frankendom-deploy")
    assert "720k" in line and "700k one" in line
    assert cs.context_notice(str(t), "/x/frankendom-lead-catalogue") == ("", False)
    t.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 710_000}}}) + "\n")
    line, first = cs.context_notice(str(t), "/x/frankendom-strategy")
    assert "710k" in line and "700k one" in line and first is False
    assert cs.context_notice(str(t), "/x/frankendom-lead-catalogue") == ("", False)   # Lead: 750k
    t.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 760_000}}}) + "\n")
    line, first = cs.context_notice(str(t), "/x/frankendom-lead-catalogue")
    assert "760k" in line and "750k one" in line and first is False


def test_worktree_lanes_are_named_by_their_title(tmp_path):
    """2026-09-25: the lanes moved to desktop worktrees (.../frankendom/.claude/worktrees/funny-rhodes-...) whose
    folder names no lane, so Lead and Strategy fell back to the default threshold. The title still names them."""
    from lib import context_size as cs
    wt = "/Users/d/Desktop/Business/frankendom/.claude/worktrees/funny-rhodes-f0eaa1"
    t = tmp_path / "t.jsonl"

    def write(title, tokens):
        rows = [{"type": "custom-title", "customTitle": title, "sessionId": "s"}] if title else []
        rows.append({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": tokens}}})
        t.write_text("".join(json.dumps(r) + "\n" for r in rows))

    write("Frankendom - Lead Developer", 740_000)
    assert cs.session_lane(str(t), wt) == "lead"
    assert cs.context_notice(str(t), wt) == ("", False)          # under Lead's 750k
    write("Frankendom - Strategy - Fable 5.1", 690_000)
    assert cs.context_notice(str(t), wt) == ("", False)          # under Strategy's 700k
    write("Frankendom - Hero Look", 100_000)                      # 2026-09-26: its folder is frankendom-herolook
    assert cs.session_lane(str(t), wt) == "herolook"
    write("Frankendom - The Pit", 100_000)                        # 2026-09-29: docs/state/pit.md, not "the"
    assert cs.session_lane(str(t), wt) == "pit"
    write("Frankendom - The Forge", 100_000)                      # any other "The ..." lane keeps its own name
    assert cs.session_lane(str(t), wt) == "forge"
    write("Frankendom - Deploy", 650_000)
    assert cs.session_lane(str(t), wt) == "deploy" and cs.context_notice(str(t), wt) == ("", False)  # under 700k
    write("Frankendom - Weapons", 650_000)
    line, _ = cs.context_notice(str(t), wt)
    assert "600k one" in line and "docs/state/weapons.md" in line
    write("Frankendom - Visuals and World", 650_000)             # the title's first word is not the doc's name
    assert "docs/state/world.md" in cs.context_notice(str(t), wt)[0]
    write("Frankendom - Auditer + fixer - Fable 5.1", 650_000)
    assert cs.session_lane(str(t), wt) == "code-quality"
    write("", 650_000)                                            # no title: the default threshold, as before
    assert cs.session_lane(str(t), wt) == "session" and "600k one" in cs.context_notice(str(t), wt)[0]
    # the folder wins when it names a lane
    write("Frankendom - Weapons", 650_000)
    assert cs.session_lane(str(t), "/x/frankendom-strategy") == "strategy"


def test_context_notice_thresholds(tmp_path, monkeypatch):
    """2026-09-22: lanes ran to the 966k ceiling; capping at 200k would have cut the lane bill 64%.
    Warn at 600k (appended; Dom, 2026-10-08), make it the first line at 800k (above Lead's 750k). Below, silence."""
    from lib import context_size as cs
    t = tmp_path / "t.jsonl"

    def write(tokens):
        t.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": tokens}}}) + "\n")

    write(150_000)
    assert cs.context_notice(str(t), "/x/frankendom-combat") == ("", False)
    write(510_000)   # under the 600k threshold Dom set on 2026-10-08: still silent
    assert cs.context_notice(str(t), "/x/frankendom-combat") == ("", False)
    write(610_000)
    line, first = cs.context_notice(str(t), "/x/frankendom-combat")
    assert "610k" in line and "docs/state/combat.md" in line and "clear this session yourself" in line and first is False
    assert "a 600k one per call" in line   # the line a lane reads names the live threshold
    assert "Do not restart mid-task" in line  # context is a per-task budget, not a diary
    write(720_000)
    assert cs.context_notice(str(t), "/x/frankendom-audio")[1] is False  # still appended, not first
    write(760_000)
    assert cs.context_notice(str(t), "/x/frankendom-audio")[1] is False  # under URGENT: still appended
    write(820_000)
    line, first = cs.context_notice(str(t), "/x/frankendom-audio")
    assert first is True and "docs/state/audio.md" in line
    assert cs.context_notice(str(tmp_path / "missing.jsonl"), "/x") == ("", False)
    # placement in the injected context
    import user_prompt_submit as u
    write(820_000)
    out = u.with_context_notice("BODY", str(t), Path("/x/frankendom-combat"))
    assert out.startswith("# Context 820k") and out.rstrip().endswith("BODY")
    write(610_000)   # over WARN, under URGENT: appended after the body
    out = u.with_context_notice("BODY", str(t), Path("/x/frankendom-combat"))
    assert out.startswith("BODY") and "# Context 610k" in out
    write(510_000)   # under WARN: nothing injected at all
    assert u.with_context_notice("BODY", str(t), Path("/x/frankendom-combat")) == "BODY"


def test_memory_receipt(tmp_path, monkeypatch):
    """Memory is keyed by cwd; a lane that restarts in another worktree silently sees nothing (live, 2026-09-22).
    The receipt names the resolved key (through a symlink), the counts and today's files."""
    import session_start as ss
    home = tmp_path / "home"
    key = home / ".claude" / "projects" / "-work-lane-a" / "memory"
    key.mkdir(parents=True)
    (key / "old.md").write_text("x")
    (key / "MEMORY.md").write_text("# Memory Index\n\n- [Old](old.md) — hook\n")
    monkeypatch.setattr(ss.Path, "home", classmethod(lambda cls: home))
    out = ss.memory_receipt(Path("/work/lane-a"))
    assert "1 memory file(s), 1 index line(s)" in out and "written today" in out
    # the hook cannot see the app's own memory root; only the model can, so it must be told to compare
    assert "memory path in your own system prompt" in out and "symlink" in out
    # symlinked key resolves and says so
    link_key = home / ".claude" / "projects" / "-work-lane-b" / "memory"
    link_key.parent.mkdir(parents=True)
    link_key.symlink_to(key)
    out2 = ss.memory_receipt(Path("/work/lane-b"))
    assert "symlink ->" in out2 and "1 memory file(s)" in out2
    # missing key tells the lane where to look instead of failing
    out3 = ss.memory_receipt(Path("/work/lane-c"))
    assert "No memory directory" in out3 and "grep -i <lane>" in out3


def test_state_doc_read_from_trunk_with_aliases(monkeypatch):
    """Reading the worktree reported four lanes missing a doc that was already on trunk (Lead + me, both stale
    within an hour on 2026-09-22). Trunk is the shared truth, the sha is reported, and a lane whose doc is not
    named after its worktree is mapped, never renamed."""
    from lib import handoff_check as hc
    calls = []

    def fake_git(args, cwd):
        calls.append(args)
        if args[:2] == ["rev-parse", "--short"]:
            return "ed4539e"
        if args[0] == "log":
            return "2026-09-22" if "--format=%cs" in args else "9dea712"
        return ""
    monkeypatch.setattr(hc, "_git", fake_git)
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 0})())
    monkeypatch.setattr(hc, "datetime", type("D", (), {"now": staticmethod(lambda: __import__("datetime").datetime(2026, 9, 22))}))
    ok, why = hc.state_file_pushed(Path("/repo"), "deploy")
    assert ok and "on trunk ed4539e" in why and "updated today" in why
    assert ["fetch", "-q", "origin"] in calls          # never answers from a stale local ref
    assert any(hc.TRUNK in a for a in calls if isinstance(a, list) for a in a)
    # Lead's doc is lead.md, referenced by AGENTS.md: map the name, do not rename the file
    _, why_lead = hc.state_file_pushed(Path("/repo"), "lead-catalogue")
    assert "docs/state/lead.md" in why_lead
    # a doc missing from trunk fails with the sha it read, so a stale answer is visible
    monkeypatch.setattr(hc.subprocess, "run", lambda *a, **k: type("R", (), {"returncode": 1})())
    ok2, why2 = hc.state_file_pushed(Path("/repo"), "multichar")
    assert not ok2 and "not on trunk (ed4539e)" in why2


def test_handoff_check_reports_never_blocks(tmp_path, monkeypatch):
    """Dom chose the reporting variant (2026-09-22): three checks, no block. A hook that demanded a pushed commit
    could trap a lane with nothing to commit or no network — the block loop fixed earlier the same day."""
    from lib import handoff_check as hc
    monkeypatch.setattr(hc.Path, "home", staticmethod(lambda: tmp_path))   # keep the worktree fallback off the real Mac
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 600_000}}}) + "\n")
    repo = tmp_path / "frankendom-combat"
    (repo / "docs" / "state").mkdir(parents=True)
    # under threshold, or mid-task: silent either way
    small = tmp_path / "s.jsonl"
    small.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 100_000}}}) + "\n")
    assert hc.handoff_notice(str(small), repo, "PR #12 merged") == ""
    assert hc.handoff_notice(str(t), repo, "still working on it") == ""
    # task closed and over threshold, but nothing written: reports what is missing, never the clear instruction
    out = hc.handoff_notice(str(t), repo, "PR #12 merged, handed over")
    assert "NOT ready to clear" in out and "Not blocking you" in out
    assert "clear_session" not in out
    assert "cannot read" in out or "not on trunk" in out   # the check now reads trunk, not the worktree
    # the ready path names the tool only when both checks pass
    monkeypatch.setattr(hc, "state_file_pushed", lambda cwd, lane, fetch=True: (True, "state ok"))
    monkeypatch.setattr(hc, "memory_written_today", lambda cwd: (True, "memory ok"))
    ready = hc.handoff_notice(str(t), repo, "PR #12 merged")
    assert "Backup verified" in ready and 'clear_session with session_id "self"' in ready
    assert "system prompt" in ready   # the third check stays
    # a half-done backup still does not instruct a clear
    monkeypatch.setattr(hc, "memory_written_today", lambda cwd: (False, "no memory file written today"))
    half = hc.handoff_notice(str(t), repo, "PR #12 merged")
    assert "NOT ready" in half and "clear_session" not in half


def test_handoff_report_reaches_the_model_through_the_prompt_hook(tmp_path, monkeypatch):
    """2026-09-24: the Stop copy goes to stderr on an allowed stop, which Claude Code stores but never shows the
    model, so no lane ever read "ready". World and Executioner asked Dom to /clear instead. The prompt hook's
    context IS read, so the report rides there, needs no closing phrase, and says the clear is the lane's job."""
    import user_prompt_submit as ups
    from lib import handoff_check as hc
    t = tmp_path / "t.jsonl"

    def write(tokens):
        t.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": tokens}}}) + "\n")

    monkeypatch.setattr(hc, "state_file_pushed", lambda cwd, lane, fetch=True: (True, "state ok"))
    monkeypatch.setattr(hc, "memory_written_today", lambda cwd: (True, "memory ok"))
    write(610_000)
    ready = ups.with_context_notice("BODY", str(t), Path("/x/frankendom-world"))
    assert "Backup verified" in ready and 'clear_session with session_id "self"' in ready
    assert "not Dom's" in ready and "never mid-task" in ready
    # the per-lane threshold still holds: strategy at 610k hears nothing
    assert ups.with_context_notice("BODY", str(t), Path("/x/frankendom-strategy")) == "BODY"
    # outside a lane there is no state doc to check
    assert "Handoff check" not in ups.with_context_notice("BODY", str(t), Path("/x/other"))
    # a missing backup names the aliased doc and withholds the tool
    monkeypatch.setattr(hc, "memory_written_today", lambda cwd: (False, "no memory file written today"))
    write(760_000)   # over Lead's 750k (Dom, 2026-09-27)
    half = ups.with_context_notice("BODY", str(t), Path("/x/frankendom-lead-catalogue"))
    assert "NOT ready" in half and "docs/state/lead.md" in half and "clear_session" not in half
    # the prompt hook has a 10 s budget, so its copy reads the last-fetched trunk and never fetches
    seen = []
    monkeypatch.setattr(hc, "state_file_pushed", lambda cwd, lane, fetch=True: seen.append(fetch) or (True, "ok"))
    ups.with_context_notice("BODY", str(t), Path("/x/frankendom-world"))
    assert seen == [False]
    # a failing check never costs the prompt its context
    monkeypatch.setattr(hc, "state_file_pushed", lambda cwd, lane, fetch=True: 1 / 0)
    broken = ups.with_context_notice("BODY", str(t), Path("/x/frankendom-world"))
    assert broken.startswith("BODY") and "Context 760k" in broken


def test_the_stats_lane_is_a_lane(tmp_path, monkeypatch):
    """2026-09-24: Stats was opened in "~/Developer/Frankendom - Stats" (only .claude in it). The lowercase-only
    pattern made it "session": no handoff check, a context line naming docs/state/session.md, and inbox messages
    to "stats" never claimed. Its trunk check must also fall back to the real worktree."""
    from lib import context_size as cs, handoff_check as hc, inbox
    assert cs.lane_name("/Users/d/Developer/Frankendom - Stats") == "stats"
    assert inbox.lane_key("/Users/d/Developer/Frankendom - Stats") == "stats"
    assert cs.lane_name("/Users/d/Developer/frankendom-lead-catalogue") == "lead-catalogue"
    assert cs.lane_name("/Users/d/Desktop/Business") == "session"
    calls = []
    monkeypatch.setattr(hc, "_git", lambda args, cwd: calls.append((args[0], str(cwd))) or "")
    monkeypatch.setattr(hc.Path, "home", staticmethod(lambda: tmp_path))
    hc.state_file_pushed(tmp_path / "Frankendom - Stats", "stats", fetch=False)
    assert calls[0] == ("rev-parse", str(tmp_path / "Frankendom - Stats"))
    assert calls[1] == ("rev-parse", str(tmp_path / "Developer" / "frankendom-stats"))


def test_receipt_additions_and_export(tmp_path, monkeypatch):
    """Strategy's brief (Dom's authority, 2026-09-22): the receipt must carry lane, session id, the Now brief and
    a blind-restart fix, so a woken session needs no context; and a cleared transcript stays readable."""
    import session_start as ss
    from lib import export_transcript as ex
    home = tmp_path / "home"
    repo = tmp_path / "frankendom-audio"
    (repo / "docs" / "state").mkdir(parents=True)
    (repo / "CLAUDE.local.md").write_text("This checkout is the Frankendom **audio** lane; session **X**.\n")
    (repo / "docs" / "state" / "audio.md").write_text("# Audio\n\n**Now:** rebuild the clash bank.\n\n## older\nstuff\n")
    key = home / ".claude" / "projects" / ("-" + str(repo).replace("/", "-").lstrip("-")) / "memory"
    key.mkdir(parents=True)
    (key / "a.md").write_text("x")
    (key / "MEMORY.md").write_text("# Memory Index\n\n- [A](a.md) — hook\n")
    monkeypatch.setattr(ss.Path, "home", classmethod(lambda cls: home))
    out = ss.memory_receipt(repo, "sess-9")
    assert "lane: audio" in out                      # lane from CLAUDE.local.md, not the folder
    # Lead proved the hook's id does not resolve in send_message (it is the transcript id), so the receipt tells
    # the lane to be woken BY LANE TITLE instead of handing over an id that fails silently.
    assert "ask by LANE TITLE" in out and "sess-9" not in out
    assert "rebuild the clash bank" in out           # the brief travels with the receipt
    assert "on trunk" in out                        # read from trunk: a lane on an old branch still sees it
    assert "Resume previous session" in out and "export_transcript.py" in out
    # blind restart: empty key, another key's index names the lane -> print the exact symlink command
    other = home / ".claude" / "projects" / "-other-frankendom-audio" / "memory"
    other.mkdir(parents=True)
    (other / "MEMORY.md").write_text("- [Audio handoff](audio_handoff.md) — audio lane\n")
    empty_repo = tmp_path / "elsewhere" / "frankendom-audio"   # same lane, different path -> different key
    empty_repo.mkdir(parents=True)
    empty_key = home / ".claude" / "projects" / ("-" + str(empty_repo).replace("/", "-").lstrip("-")) / "memory"
    empty_key.mkdir(parents=True)
    hint = ss.blind_restart_hint(empty_key)
    assert "ln -s" in hint and str(other) in hint
    # Weapons, 2026-09-22 19:55: a pre-rehome folder key is SHARED by every lane made from it. Never a link target.
    shared = home / ".claude" / "projects" / "-Users-domininclynch-Desktop-Business-frankendom" / "memory"
    shared.mkdir(parents=True)
    (shared / "MEMORY.md").write_text("- [Audio handoff](audio_handoff.md) — audio lane\n")
    import shutil
    shutil.rmtree(other)
    assert ss.blind_restart_hint(empty_key) == ""          # the only candidate is shared -> stay silent
    # a key that already holds memory is never relinked
    assert ss.blind_restart_hint(key) == ""
    # and the compare line must not tell anyone to symlink the shared root
    assert "do not symlink or copy into it" in out and "symlink that path to this key" not in out
    # exporter keeps the human thread and drops tool noise
    src = tmp_path / "s.jsonl"
    src.write_text("\n".join([
        json.dumps({"timestamp": "2026-09-22T10:00:00Z", "message": {"role": "user", "content": "do the thing"}}),
        json.dumps({"timestamp": "2026-09-22T10:00:05Z", "message": {"role": "assistant", "content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls"}}]}}),
        json.dumps({"timestamp": "2026-09-22T10:00:09Z", "message": {"role": "assistant", "content": [
            {"type": "text", "text": "done, here is why"}]}}),
    ]) + "\n")
    md = tmp_path / "s.md"
    users, replies, dropped = ex.export(src, md)
    assert (users, replies, dropped) == (1, 1, 1)
    body = md.read_text()
    assert "do the thing" in body and "done, here is why" in body and "Bash" not in body


def test_session_start_emits_one_valid_json_object(monkeypatch, capsys):
    """Two concatenated JSON objects were rejected by Claude Code ("hook_non_blocking_error" in every lane
    transcript until 2026-09-26), so the memory receipt and baseline never reached any session."""
    import session_start as ss
    monkeypatch.setattr(ss, "memory_receipt", lambda cwd, sid="": "RECEIPT")
    monkeypatch.setattr(ss, "load_handoff", lambda: {})
    monkeypatch.setattr(ss, "is_git_repo", lambda p: False)
    monkeypatch.setattr(ss, "remember_repo", lambda p: None)
    monkeypatch.setattr(ss, "log", lambda *a: None)
    monkeypatch.setattr(ss.sys, "stdin", io.StringIO(json.dumps({"session_id": "s", "cwd": "/nowhere", "prompt": "hi"})))
    ss.main()
    out = json.loads(capsys.readouterr().out)      # raises on two objects
    ctx = out["hookSpecificOutput"]["additionalContext"]
    assert ctx.startswith("RECEIPT") and ss.SESSION_BASELINE.strip()[:40] in ctx


DELEGATION_LINES = (
    "Lead Dev and Project Strategy carry Dom's authority for release, pipeline, CI, config, lane and order-of-work decisions in this repo, on the Mac and on the VPS.",
    "A written ruling from either is Dom's word; do not wait for Dom.",
    'Exceptions that still need Dom in chat: spending money, legal, wiping player data, parties/guilds.',
    "Dom's direct word to a lane overrides a relayed plan.",
    'Waiting on Dom for anything else is a defect; flag it to Lead.',
)


def test_session_start_injects_the_delegation_paragraph(monkeypatch, capsys):
    """Dom, 2026-10-13: lanes waited on Dom after every /clear because the authority rule lived only in conversations."""
    import session_start as ss
    monkeypatch.setattr(ss, "memory_receipt", lambda cwd, sid="": "RECEIPT")
    monkeypatch.setattr(ss, "load_handoff", lambda: {})
    monkeypatch.setattr(ss, "is_git_repo", lambda p: False)
    monkeypatch.setattr(ss, "remember_repo", lambda p: None)
    monkeypatch.setattr(ss, "log", lambda *a: None)
    monkeypatch.setattr(ss.sys, "stdin", io.StringIO(json.dumps({"session_id": "s", "cwd": "/nowhere", "prompt": "hi"})))
    ss.main()
    ctx = json.loads(capsys.readouterr().out)["hookSpecificOutput"]["additionalContext"]
    for line in DELEGATION_LINES:
        assert line in ctx, line
    assert ctx.startswith("RECEIPT")                        # still one object, receipt first


def test_worktree_lane_comes_from_the_title_even_right_after_a_clear(tmp_path):
    """After /clear the new transcript has no title when SessionStart runs; the lane's earlier one in the same
    app-worktree folder does. Title words map to the ~/Developer/frankendom-<key> folder."""
    import session_start as ss
    wt = Path("/Users/x/Desktop/Business/frankendom/.claude/worktrees/funny-rhodes-f0eaa1")
    cases = {"Frankendom - Lead Developer": "lead-catalogue", "Frankendom - Character Main": "char",
             "Frankendom - Auditer + fixer - Fable 5.1": "code-quality", "Frankendom - Hero Look": "herolook",
             "Frankendom - Veteran": "veteran-polish", "Frankendom - Finishers & Gore": "finishers",
             "Frankendom - Backend/Accounts": "backend", "Frankendom - Multi Chars": "multichar",
             "Frankendom - Visuals / World": "world", "Frankendom - Strategy - Fable 5.1": "strategy",
             "Frankendom - The Pit": "pit", "Frankendom - Duel": "duel"}
    for i, (title, key) in enumerate(cases.items()):
        folder = tmp_path / str(i)
        folder.mkdir()
        (folder / "old.jsonl").write_text(json.dumps({"type": "custom-title", "customTitle": title}) + "\n")
        assert ss.worktree_lane(wt, str(folder / "new-not-written-yet.jsonl")) == key, title
    assert ss.worktree_lane(Path("/Users/x/Developer/frankendom-char"), str(tmp_path / "0" / "n.jsonl")) == ""
    assert ss.worktree_lane(Path("/Users/x/Codex Hooks/.claude/worktrees/a"), str(tmp_path / "0" / "n.jsonl")) == ""


def test_restart_note_names_memory_state_doc_and_folder_rule(tmp_path, monkeypatch):
    import session_start as ss
    monkeypatch.setattr(ss.Path, "home", classmethod(lambda cls: tmp_path))
    (tmp_path / "Developer" / "frankendom-char").mkdir(parents=True)
    runs = []
    monkeypatch.setattr("subprocess.run", lambda args, **k: runs.append(args) or
                        type("R", (), {"stdout": "abc1234 2026-09-26 origin/char/state\n"})())
    note = ss.restart_note(Path("/wt/funny"), "char")
    assert "Developer-frankendom-char/memory/" in note and "NOT the memory path in your system prompt" in note
    assert "docs/state/character.md" in note and "git show abc1234:docs/state/character.md" in note
    assert "do NOT write a new one" in note and runs[0][:3] == ["git", "log", "--remotes"]
    assert "git switch -c char/<task> origin/codex/01a09a76/task-1" in note and "do NOT wait" in note
    monkeypatch.setattr("subprocess.run", lambda args, **k: type("R", (), {"stdout": ""})())
    assert "ask Lead" in ss.restart_note(Path("/wt/funny"), "char")
    assert "ls -d ~/Developer/frankendom-*" in ss.restart_note(Path("/wt/funny"), "nosuchlane")


def test_handoff_check_names_worktree_lanes_by_title_and_finds_their_memory(tmp_path, monkeypatch):
    """2026-09-28: Lead at 881k, Strategy and Web over their limits, and not one "ready" notice all day. The check
    named the lane from the folder, and app worktrees (…/worktrees/funny-rhodes-…) name none, so it stayed silent.
    Its memory check also looked under the worktree's key, where lanes keep no memory (they use
    ~/Developer/frankendom-<key>, e.g. lead-catalogue for Lead)."""
    from datetime import datetime
    from lib import handoff_check as hc
    monkeypatch.setattr(hc.Path, "home", staticmethod(lambda: tmp_path))
    fetched = []
    monkeypatch.setattr(hc, "state_file_pushed", lambda cwd, lane, fetch=True: fetched.append(fetch) or (True, "state ok"))
    t = tmp_path / "t.jsonl"
    t.write_text(json.dumps({"customTitle": "Frankendom - Lead Developer"}) + "\n"
                 + json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 760_000}}}) + "\n")
    wt = tmp_path / "frankendom" / ".claude" / "worktrees" / "funny-rhodes-f0eaa1"
    out = hc.prompt_handoff_notice(str(t), wt)
    assert "NOT ready" in out and "docs/state/lead.md" in out and "no memory directory" in out
    mem = tmp_path / ".claude" / "projects" / ("-" + hc.project_slug(tmp_path / "Developer" / "frankendom-lead-catalogue")) / "memory"
    mem.mkdir(parents=True)
    (mem / "handoff.md").write_text("x")
    (mem / "MEMORY.md").write_text(f"- [handoff](handoff.md) {datetime.now():%Y-%m-%d}\n")
    ready = hc.prompt_handoff_notice(str(t), wt)
    assert "Backup verified" in ready and "clear_session" in ready and "the lead lane" in ready
    assert hc.handoff_notice(str(t), wt, "PR #12 merged").startswith("# Handoff check") and "Backup verified" in hc.handoff_notice(str(t), wt, "PR #12 merged")
    assert not any(fetched), "neither copy fetches: the Stop copy runs before the review and only feeds the log"
    t.write_text(json.dumps({"customTitle": "Frankendom - Lead Developer"}) + "\n"
                 + json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 740_000}}}) + "\n")
    assert hc.prompt_handoff_notice(str(t), wt) == "", "under Lead's own 750k: silent"
    t.write_text(json.dumps({"type": "assistant", "message": {"usage": {"cache_read_input_tokens": 900_000}}}) + "\n")
    assert hc.prompt_handoff_notice(str(t), wt) == "", "no title and no lane folder: not a lane, silent as before"


def test_a_rewritten_verdict_survives_the_50_session_cap(tmp_path, monkeypatch):
    # 2026-10-06: 147 Stop hooks sat ~215 s each (11 h in all) on "mimo gate timeout ... observed_turn=": the verdict
    # was written, then evicted at once. The file is saved with sort_keys, so the [-50:] cap kept the 50 largest
    # session ids and dropped a just-written verdict whenever its id sorted low.
    from lib import audit_state
    monkeypatch.setattr(audit_state, "VERDICT_FILE", tmp_path / "verdict.json")
    for n in range(50):
        audit_state.save_verdict(f"s{n}", "t", "PASS")
    audit_state.save_verdict("s0", "t2", "PASS")   # the oldest key reviews again: now the freshest
    audit_state.save_verdict("new", "t", "PASS")   # a 51st session pushes the cap
    saved = json.loads((tmp_path / "verdict.json").read_text())
    assert saved["s0"]["turn_id"] == "t2" and "s1" not in saved and len(saved) == 50


def test_handoff_check_failure_log_names_the_raising_line(tmp_path, monkeypatch):
    # 2026-10-06: "handoff check failed: division by zero" x5 and nothing to say where; log file:line of the raise.
    import user_prompt_submit as u
    logged = []
    monkeypatch.setattr(u, "context_notice", lambda *_: ("# Context", False))

    def boom(*_):
        return 1 / 0
    monkeypatch.setattr(u, "prompt_handoff_notice", boom)
    monkeypatch.setattr(u, "log", lambda _component, message: logged.append(message))
    assert u.with_context_notice("BODY", str(tmp_path / "t.jsonl"), Path("/x/frankendom-combat")).endswith("# Context")
    assert "division by zero" in logged[0] and "test_lane_context.py:" in logged[0] and "boom" in logged[0]


def test_iteration_count_survives_the_cap_when_its_key_sorts_low(tmp_path, monkeypatch):
    # Same sort_keys flaw as the verdict file: a low-sorting turn key was evicted as it was written, so its count
    # stayed at 0 and the "max iterations" stop could never be reached on that turn.
    from lib import audit_state
    monkeypatch.setattr(audit_state, "STATE_FILE", tmp_path / "iterations.json")
    for n in range(50):
        audit_state.save_iterations(f"z{n:02d}", "t", 1)
    audit_state.save_iterations("a", "t", 2)
    audit_state.save_iterations("a", "t", 3)
    audit_state.save_iterations("z50", "t", 1)   # a new key pushes the cap: the front of the sorted file goes
    assert audit_state.load_iterations("a", "t") == 3


def test_policy_pointer_not_full_contract_when_the_session_id_sorts_low(tmp_path, monkeypatch):
    # Same flaw: a low-sorting session fell out of prompt_policy.json at once, so every prompt re-sent the 2 KB contract.
    import user_prompt_submit as u
    monkeypatch.setattr(u, "POLICY_STATE", tmp_path / "policy.json")
    for n in range(60):
        u.policy_due(f"z{n:02d}")
    assert u.policy_due("000") is True        # first prompt of a new session: full contract
    assert u.policy_due("000") is False       # second: the pointer, not the full text again
    u.policy_due("z60")                        # a new session pushes the cap: the front of the sorted file goes
    assert u.policy_due("000") is False       # still the pointer: "000" is recent, not the evicted one


def test_expansion_and_auditor_titles_name_their_own_lane(tmp_path):
    # 2026-10-07: "Lead Dev (Expansion)" was read as plain `lead` (so its restart loaded Lead's memory, the wrong
    # lane), and the title word "Auditor" was missing from the map (only "Auditer" was there).
    import session_start as ss
    from lib import context_size as cs
    wt = "/Users/d/Desktop/Business/frankendom/.claude/worktrees/elastic-gates-c6edc0"
    t = tmp_path / "t.jsonl"
    cases = {"Frankendom - Lead Dev (Expansion)": ("expansion", "expansion"), "Frankendom - Auditor": ("code-quality", "code-quality"),
             "Frankendom - Lead Dev": ("lead", "lead-catalogue")}
    for title, (lane, key) in cases.items():
        t.write_text(json.dumps({"type": "custom-title", "customTitle": title}) + "\n")
        assert cs.session_lane(str(t), wt) == lane, title
        assert ss.worktree_lane(Path(wt), str(t)) == key, title
    assert cs.warn_tokens("expansion") == 750_000   # Lead-class, as the table has shown it


def test_chat_prompts_get_full_autonomy_once_then_short(tmp_path, monkeypatch):
    """2026-10-09 noise check: non-project prompts re-sent the full autonomy policy (~900 tokens) every time.
    Full on a session's first chat prompt and every REFRESH_EVERY after; a 3-line discipline note in between.
    The chat counter is separate, so a chat-first session still gets the full project contract later."""
    import user_prompt_submit as u
    from pathlib import Path
    monkeypatch.setattr(u, "POLICY_STATE", tmp_path / "policy.json")
    monkeypatch.setattr(u, "active_lessons", lambda lane: ["a lesson"])
    first = u.context_for("hello", Path("/tmp"), "s1")
    assert "Autonomous Execution Contract" in first
    later = u.context_for("hello", Path("/tmp"), "s1")
    assert "Autonomous Execution Contract" not in later
    assert "Current-Prompt Discipline" in later and "a lesson" in later and len(later) < len(first)
    assert "Apply before acting." in u.context_for("fix src/a.ts", Path("/tmp"), "s1")


def test_log_rotates_past_cap_and_keeps_three(tmp_path, monkeypatch):
    """24 MB stop_orchestrator.log, 12 MB mimo_audit.log, never rotated (2026-10-09). Rotate at LOG_MAX_BYTES, keep 3."""
    from lib import log as lg
    monkeypatch.setattr(lg, "LOG_DIR", tmp_path)
    monkeypatch.setattr(lg, "LOG_MAX_BYTES", 200)
    for i in range(40):
        lg.log("c", f"line {i} " + "x" * 30)
    names = sorted(p.name for p in tmp_path.iterdir())
    assert names == ["c.log", "c.log.1", "c.log.2", "c.log.3"]
    assert all(p.stat().st_size < 400 for p in tmp_path.iterdir())
    assert "line 39" in (tmp_path / "c.log").read_text()


def test_pointer_prompt_is_under_300_bytes(tmp_path, monkeypatch):
    """Strategy, 2026-10-09: the in-between prompt was 422 bytes; the pointer is one short line plus cwd."""
    import user_prompt_submit as u
    from pathlib import Path
    monkeypatch.setattr(u, "POLICY_STATE", tmp_path / "policy.json")
    monkeypatch.setattr(u, "active_lessons", lambda lane: [])
    u.context_for("fix src/a.ts", Path("/tmp/x"), "s1")
    assert len(u.context_for("fix src/a.ts", Path("/tmp/x"), "s1").encode()) < 300
