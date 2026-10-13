# Adit's skills

Skills I build and use every day with Claude Code and Codex. Install the pack once and you get each new skill as I add it.

## Install

**Claude Code**

```
/plugin marketplace add AKarode/skills
/plugin install akarode@akarode
```

Then run a skill, for example `/akarode:agent-scan`.

**Codex, Cursor, Gemini CLI and other agents**

```
npx skills add AKarode/skills
```

It asks which skills you want. In Codex, run one with `$agent-scan`.

## Skills

| Skill | What it does |
| --- | --- |
| `agent-scan` | Reads your Claude Code and Codex history on this machine and shows what your agents would cost at API prices, how often their shell commands fail, and how many failures repeat a command that already failed. Then your agent finds the cause of the top repeat and proposes one fix. |

## agent-scan and your data

The scan runs `scripts/scan.py`, one file of standard-library Python you can read first. It reads your local session files without changing them and makes no network calls.

The card it prints holds counts, dollars and percentages only. Under it, your agent sees the names of your most repeated failing commands so it can find the cause; those stay on your screen.

At the end your agent offers a link to share your card's numbers with me. Nothing is sent unless you open that link and submit the form.

## License

MIT
