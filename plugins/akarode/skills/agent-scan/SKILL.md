---
name: agent-scan
description: Scan this machine's Claude Code and Codex history to show what the agents would cost at API prices, how often their shell commands fail, and which failures keep repeating, then find the cause of the top repeat and propose one fix. Use when the user asks to scan their agents, check what their agents cost, or find the mistakes their agents repeat.
allowed-tools: Bash(python3 -I ${CLAUDE_SKILL_DIR}/scripts/scan.py *)
---

# Agent scan

`scripts/scan.py`, next to this file, reads the user's local Claude Code transcripts and Codex rollouts without changing them and prints one card. It uses only the Python standard library and makes no network calls.

1. Run `python3 -I "${CLAUDE_SKILL_DIR}/scripts/scan.py"`. If your agent has no `CLAUDE_SKILL_DIR`, use the path of this skill's folder. Show the output exactly as printed. If it fails, show the error and stop.
2. In two or three sentences, say which numbers matter most for this user and why. The spend line is an estimate at API list prices; on a subscription it is not a bill.
3. Take the top entry under "Your most repeated failing commands". Find a few of those failed attempts in the user's recent sessions (Claude Code: `~/.claude/projects/**/*.jsonl`; Codex: `sessions/` under `~/.codex`). Read each failed call and the attempt that came next, and work out the cause.
4. Propose one concrete fix: a line for the user's agent instructions file (`CLAUDE.md` or `AGENTS.md`) or a hook. Show the exact text and where it goes. Change nothing until the user says yes.
5. Offer once to share the card anonymously. Only if the user agrees, build this link from the card and give it to them to open. Fill in numbers only: no paths, ids, command text or labels.
   `https://tally.so/r/ODpeWA?v=1&via=skill&days=<days>&harnesses=<claude-code and/or codex, comma separated>&sessions=<sessions plus threads>&spend_usd=<number>&shell_calls=<number>&fail_pct=<number>&repeat_pct=<number>&cd_pct=<number>&top_repeat_count=<number>`
6. Never send the user's data anywhere yourself.
7. Suggest running the scan again in a week, after the fix, to see whether the repeat count drops.
