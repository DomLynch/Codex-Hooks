#!/usr/bin/env python3
"""Send a message to a Claude Code session through the shared inbox (see lib/inbox.py).

    python3 ~/.codex/hooks/send_to_claude.py --to deploy --from "Hook scripts - codex" "message text"
    echo "long message" | python3 ~/.codex/hooks/send_to_claude.py --to deploy --from "Hook scripts - codex"

--to is a Frankendom lane key (deploy, strategy, lead-catalogue, ...) or the recipient session's absolute working
directory. The message is shown on that session's NEXT prompt, labelled as coming from another agent — it cannot
wake an idle session. Prints the file written; once it moves to <inbox>/delivered/ it has been shown.
Never type into the Claude app's composer instead: text there arrives as the user, with the user's authority.
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from lib.inbox import send  # noqa: E402


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--to", required=True, help="lane key (e.g. deploy) or the session's absolute cwd")
    ap.add_argument("--from", dest="sender", required=True, help="who is sending, e.g. the Codex thread name")
    ap.add_argument("text", nargs="?", help="the message; read from stdin when omitted")
    args = ap.parse_args()
    text = args.text if args.text is not None else sys.stdin.read()
    try:
        print(send(args.to, args.sender, text))
    except ValueError as exc:
        print(f"send_to_claude: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
