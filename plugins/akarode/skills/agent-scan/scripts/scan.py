#!/usr/bin/python3
"""Scan this machine's coding-agent sessions and print one card of numbers.

Reads Claude Code transcripts and Codex rollouts, read-only. Nothing leaves the
machine. The card holds counts, dollars and percentages only. The section
printed under it names your most repeated failing commands; it stays on your
screen and is left out of the card and the export.

Usage: python3 scan.py [--days 30] [--card-only] [--export card.json]
"""
import argparse
import ast
import datetime
import hashlib
import json
import os
from pathlib import Path
import re
import shlex
import sys
import time

VERSION = 'scan v1'
PRICES_AS_OF = '2026-10-13'
M = 1000000.0

# Copied from contracts/fixed-signatures-v2.json so the release stays one file.
# The lane's tests fail if the two drift apart.
SIGNATURES = (
    ('compound-cd', r'^\s*cd\s+[^;&|\n]*?\s*(&&|;)', None,
     'cd chained to another command'),
    ('nested-agent-cli',
     r'(?:^|[;&|(\n]\s*|&&\s*|\bthen\s+|\bdo\s+|\btimeout\s+\S+\s+|\bnohup\s+|\bexec\s+|=\$\(\s*)'
     r'(?:\S*/)?codex\s+(exec|review|consult)\b', 'subagent',
     'agent CLI launched from a subagent'),
)

# Published API list prices in USD per 1M tokens, as of PRICES_AS_OF.
# Claude: (input, output, cache read). Cache writes cost 1.25x input for the
# 5-minute duration and 2x input for the 1-hour duration.
# A model id matches the longest key equal to it or followed by '-'.
CLAUDE_PRICES = {
    'claude-fable-5-1': (10.0, 50.0, 0.25),
    'claude-mythos-5-1': (10.0, 50.0, 0.25),
    'claude-fable-5': (10.0, 50.0, 1.0),
    'claude-mythos-5': (10.0, 50.0, 1.0),
    'claude-opus-5-5': (4.0, 20.0, 0.20),
    'claude-opus-5': (5.0, 25.0, 0.50),
    'claude-opus-4-8': (5.0, 25.0, 0.50),
    'claude-opus-4-7': (5.0, 25.0, 0.50),
    'claude-opus-4-6': (5.0, 25.0, 0.50),
    'claude-opus-4-5': (5.0, 25.0, 0.50),
    'claude-opus-4-1': (15.0, 75.0, 1.50),
    'claude-opus-4': (15.0, 75.0, 1.50),
    'claude-sonnet-5-5': (2.0, 10.0, 0.20),
    'claude-sonnet-5': (2.0, 10.0, 0.20),
    'claude-sonnet-4-6': (3.0, 15.0, 0.30),
    'claude-sonnet-4-5': (3.0, 15.0, 0.30),
    'claude-sonnet-4': (3.0, 15.0, 0.30),
    'claude-3-7-sonnet': (3.0, 15.0, 0.30),
    'claude-haiku-5-5': (0.10, 0.50, 0.01),
    'claude-haiku-4-5': (1.0, 5.0, 0.10),
    'claude-3-5-haiku': (0.80, 4.0, 0.08),
}
# Claude Haiku 5.5 bills prompts over 100K tokens at a higher rate.
CLAUDE_LONG = {'claude-haiku-5-5': (100000, (0.50, 2.50, 0.05))}

# OpenAI: (input, cached input, output). Cached input is part of input.
OPENAI_PRICES = {
    'gpt-6-astra': (10.0, 1.0, 50.0),
    'gpt-6.1-sol': (2.0, 0.10, 10.0),
    'gpt-6-sol': (2.0, 0.20, 10.0),
    'gpt-6-luna': (0.10, 0.01, 0.50),
    'gpt-5.6-sol': (4.0, 0.40, 20.0),
    'gpt-5.6-terra': (2.0, 0.20, 12.0),
    'gpt-5.6-luna': (0.20, 0.02, 1.20),
    'gpt-5.5': (5.0, 0.50, 30.0),
    'gpt-5.4': (2.50, 0.25, 15.0),
    'gpt-5.4-mini': (0.75, 0.075, 4.50),
    'gpt-5.4-nano': (0.20, 0.02, 1.25),
    'gpt-5.3-codex': (1.75, 0.175, 14.0),
    'gpt-5.2': (1.75, 0.175, 14.0),
    'gpt-5.1': (1.25, 0.125, 10.0),
    'gpt-5': (1.25, 0.125, 10.0),
    'gpt-5-mini': (0.25, 0.025, 2.0),
    'gpt-5-nano': (0.05, 0.005, 0.40),
}
# Requests with more than 272K input tokens use these rates.
OPENAI_LONG = {
    'gpt-6-astra': (20.0, 2.0, 75.0),
    'gpt-6.1-sol': (4.0, 0.20, 15.0),
    'gpt-6-sol': (4.0, 0.40, 15.0),
    'gpt-6-luna': (0.20, 0.02, 0.75),
    'gpt-5.6-sol': (8.0, 0.80, 30.0),
    'gpt-5.6-terra': (4.0, 0.40, 18.0),
    'gpt-5.6-luna': (0.40, 0.04, 1.80),
    'gpt-5.5': (10.0, 1.0, 45.0),
    'gpt-5.4': (5.0, 0.50, 22.50),
}
OPENAI_LONG_AT = 272000

# Leading words that never decide whether a command failed.
SKIP_PROGRAMS = {'cd', 'pushd', 'popd', 'export', 'source', '.', 'set', 'unset', 'true', 'echo', 'printf', 'sleep'}
WRAPPERS = {'sudo', 'time', 'timeout', 'nohup', 'env', 'command', 'exec', 'nice'}
SHELLS = {'bash', 'sh', 'zsh'}
# Look-up commands whose non-zero exit is an answer, not a mistake (grep finds no match, test is false).
PROBES = {'ls', 'cat', 'nl', 'less', 'head', 'tail', 'grep', 'egrep', 'fgrep', 'zgrep', 'rg', 'find', 'fd', 'test', '[', '[[',
          'which', 'type', 'diff', 'cmp', 'stat', 'wc', 'file', 'readlink', 'realpath', 'pgrep', 'du', 'df', 'tree'}
# Tools whose action is the second word (gh pr create), and runners whose target is (npm run build).
TWO_WORD = {'gh', 'docker', 'kubectl', 'aws', 'gcloud', 'az'}
RUNNERS = {'npm', 'pnpm', 'yarn', 'bun', 'uv', 'poetry'}
# Interpreters whose script name is the useful part of the label (python3 build.py).
INTERPRETERS = {'node', 'ruby', 'perl', 'deno', 'bash', 'sh', 'zsh'}
SCRIPT = re.compile(r'^[\w.+-]+\.[A-Za-z0-9]{1,5}$')
SED_READ = re.compile(r'\bsed\s+(-[A-Za-z]*n\b|--quiet\b|--silent\b)')
SUBCOMMAND = re.compile(r'^[a-z][a-z0-9_:-]{0,23}$')
EXIT_CODE = re.compile(r'exit_code\\*"?\s*:\s*(-?\d+)|Process exited with code (-?\d+)|Exit code:? (-?\d+)')


def parse_ts(value):
    """Return epoch seconds for an ISO timestamp, or None."""
    if not isinstance(value, str):
        return None
    try:
        value = re.sub(r'(T\d{2}:\d{2}:\d{2})[.,](\d{1,9})(?=Z|[+-]|$)',
                       lambda m: m.group(1) + '.' + (m.group(2) + '000000')[:6], value)
        dt = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=datetime.timezone.utc)
    return dt.timestamp()


def lines(path):
    """Yield parsed JSON objects, skipping a half-written last line and bad lines."""
    try:
        with path.open('rb') as stream:
            for raw in stream:
                if not raw.endswith(b'\n'):
                    break
                yield raw
    except OSError:
        return


def load(raw):
    try:
        value = json.loads(raw)
    except (ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def files(root, patterns, since):
    """Regular files under root, not symlinks, modified at or after since."""
    root = Path(root).expanduser()
    if not root.is_dir():
        return
    seen = set()
    for pattern in patterns:
        for path in sorted(root.glob(pattern)):
            if path in seen or path.is_symlink() or not path.is_file():
                continue
            seen.add(path)
            try:
                if path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            yield path


def num(value):
    return value if isinstance(value, int) and not isinstance(value, bool) else 0


def price_key(table, model):
    if not isinstance(model, str):
        return None
    best = None
    for key in table:
        if (model == key or model.startswith(key + '-')) and (best is None or len(key) > len(best)):
            best = key
    return best


def claude_cost(model, u):
    key = price_key(CLAUDE_PRICES, model)
    if key is None:
        return None
    rates = CLAUDE_PRICES[key]
    prompt = u['input'] + u['cache_read'] + u['cache_5m'] + u['cache_1h']
    if key in CLAUDE_LONG and prompt > CLAUDE_LONG[key][0]:
        rates = CLAUDE_LONG[key][1]
    inp, out, read = rates
    return (u['input'] * inp + u['cache_read'] * read + u['cache_5m'] * inp * 1.25
            + u['cache_1h'] * inp * 2.0 + u['output'] * out) / M


def openai_cost(model, u):
    key = price_key(OPENAI_PRICES, model)
    if key is None:
        return None
    rates = OPENAI_PRICES[key]
    if key in OPENAI_LONG and u['input'] > OPENAI_LONG_AT:
        rates = OPENAI_LONG[key]
    inp, cached, out = rates
    cached_tokens = min(u['cached'], u['input'])
    return ((u['input'] - cached_tokens) * inp + cached_tokens * cached + u['output'] * out) / M


def claude_usage(usage):
    cache = usage.get('cache_creation')
    split = isinstance(cache, dict) and any(k in cache for k in ('ephemeral_5m_input_tokens', 'ephemeral_1h_input_tokens'))
    return dict(input=num(usage.get('input_tokens')),
                cache_read=num(usage.get('cache_read_input_tokens')),
                cache_5m=num(cache.get('ephemeral_5m_input_tokens')) if split else num(usage.get('cache_creation_input_tokens')),
                cache_1h=num(cache.get('ephemeral_1h_input_tokens')) if split else 0,
                output=num(usage.get('output_tokens')))


def result_text(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return '\n'.join(b.get('text', '') for b in content if isinstance(b, dict) and isinstance(b.get('text'), str))
    return ''


def scan_claude(roots, since, data):
    patterns = ('*/*.jsonl', '*/*/subagents/**/agent-*.jsonl')
    for root in roots:
        for path in files(root, patterns, since):
            data['files'] += 1
            subagent = path.name.startswith('agent-') and 'subagents' in path.parts
            if subagent:
                session = path.parts[path.parts.index('subagents') - 1]
                agent = path.stem[len('agent-'):]
            else:
                session, agent = path.stem, None
            for raw in lines(path):
                if b'"usage"' not in raw and b'"tool_use"' not in raw and b'"tool_result"' not in raw:
                    continue
                entry = load(raw)
                if entry is None:
                    continue
                message = entry.get('message')
                if not isinstance(message, dict):
                    continue
                ts = parse_ts(entry.get('timestamp'))
                sid = entry.get('sessionId') or entry.get('session_id') or session
                aid = agent or entry.get('agentId')
                usage = message.get('usage')
                if isinstance(usage, dict) and message.get('id') and ts and ts >= since:
                    base = (message.get('id'), entry.get('requestId'))
                    keep_request(data['claude'], base + (-1,), ts, sid, message.get('model'), claude_usage(usage))
                    for index, extra in enumerate(usage.get('iterations') or []):
                        if isinstance(extra, dict) and extra.get('type') == 'advisor_message':
                            extra_usage = extra.get('usage', extra)
                            if isinstance(extra_usage, dict):
                                keep_request(data['claude'], base + (index,), ts, sid,
                                             extra.get('model') or extra.get('model_id'), claude_usage(extra_usage))
                content = message.get('content')
                for block in content if isinstance(content, list) else []:
                    if not isinstance(block, dict):
                        continue
                    if block.get('type') == 'tool_use' and block.get('name') == 'Bash' and ts and ts >= since:
                        inp = block.get('input')
                        command = inp.get('command') if isinstance(inp, dict) else None
                        if isinstance(command, str) and block.get('id'):
                            data['calls'].setdefault(('claude', block['id']), dict(
                                harness='claude', session=sid, agent=aid, ts=ts, command=command))
                    elif block.get('type') == 'tool_result' and block.get('tool_use_id'):
                        key = ('claude', block['tool_use_id'])
                        if not block.get('is_error'):
                            data['outcomes'][key] = False
                        elif result_text(block.get('content')).startswith(('Exit code', 'Command timed out')):
                            data['outcomes'][key] = True
                        # Other errors (a denied or blocked call) say nothing about the command itself.


def keep_request(rows, key, ts, session, model, usage):
    old = rows.get(key)
    # Streaming writes the same request several times; the last, largest line is the real one.
    if old is None or (ts, usage['output']) >= (old['ts'], old['usage']['output']):
        rows[key] = dict(ts=ts, session=session, model=model, usage=usage)


def literal_exec_commands(source):
    """Pull literal cmd strings out of a Codex exec script without running it."""
    pattern = r'\b(?:tools\.)?exec_command\s*\(\s*\{[^{}]*?(?:"cmd"|\x27cmd\x27|\bcmd)\s*:\s*'
    for match in re.finditer(pattern, source):
        start = match.end()
        if start >= len(source) or source[start] not in ('"', "'", '`'):
            continue
        quote = source[start]
        end = start + 1
        while end < len(source):
            if source[end] == '\\':
                end += 2
            elif source[end] == quote:
                break
            else:
                end += 1
        if end >= len(source) or source[end + 1:].lstrip()[:1] not in (',', '}'):
            continue
        token = source[start:end + 1]
        try:
            if quote == '`':
                if '${' in token:
                    continue
                command = token[1:-1].replace('\\`', '`').replace('\\\\', '\\')
            elif quote == '"':
                command = json.loads(token)
            else:
                command = ast.literal_eval(token)
        except (ValueError, SyntaxError):
            continue
        if isinstance(command, str):
            yield command


def shell_commands(payload):
    name = str(payload.get('name') or '').split('.')[-1]
    kind = payload.get('type')
    if kind == 'local_shell_call':
        action = payload.get('action')
        if isinstance(action, dict) and action.get('type') == 'exec':
            return shell_commands(dict(type='function_call', name='shell', arguments=action))
        return []
    if kind == 'custom_tool_call' and name == 'exec':
        source = payload.get('input')
        return list(literal_exec_commands(source)) if isinstance(source, str) else []
    if kind != 'function_call' or name not in ('shell', 'shell_command', 'exec_command', 'exec'):
        return []
    args = payload.get('arguments')
    if isinstance(args, str):
        try:
            args = json.loads(args)
        except ValueError:
            return []
    if not isinstance(args, dict):
        return []
    command = args.get('cmd', args.get('command'))
    if isinstance(command, list) and all(isinstance(x, str) for x in command):
        if len(command) >= 3 and command[0].split('/')[-1] in ('bash', 'sh', 'zsh') and command[1] in ('-c', '-lc', '-ic'):
            command = command[2]
        else:
            command = ' '.join(shlex.quote(x) for x in command)
    return [command] if isinstance(command, str) else []


def output_failed(output):
    """True or False from the exit codes in a tool output, None when it shows none."""
    if isinstance(output, list):
        text = '\n'.join(b.get('text', '') for b in output if isinstance(b, dict) and isinstance(b.get('text'), str))
    elif isinstance(output, str):
        text = output
    else:
        return None
    codes = [int(next(g for g in m.groups() if g is not None)) for m in EXIT_CODE.finditer(text)]
    return any(c != 0 for c in codes) if codes else None


def codex_homes(extra):
    if extra:
        return [Path(p).expanduser() for p in extra]
    home = Path.home()
    homes = [Path(os.environ['CODEX_HOME']).expanduser()] if os.environ.get('CODEX_HOME') else []
    homes.append(home / '.codex')
    for accounts in (home / '.config/orca/codex-accounts', home / 'Library/Application Support/orca/codex-accounts'):
        homes.extend(sorted(accounts.glob('*/home')))
    unique = []
    for h in homes:
        if h not in unique:
            unique.append(h)
    return unique


def scan_codex(homes, since, data):
    for home in homes:
        for folder, pattern in (('sessions', '**/rollout-*.jsonl'), ('archived_sessions', '**/*.jsonl')):
            for path in files(home / folder, (pattern,), since):
                data['files'] += 1
                scan_codex_file(path, since, data)


def scan_codex_file(path, since, data):
    entries = [e for e in (load(raw) for raw in lines(path)) if e is not None]
    meta = {}
    modern = False
    for entry in entries:
        payload = entry.get('payload') if isinstance(entry.get('payload'), dict) else {}
        if entry.get('type') == 'session_meta':
            meta = payload
        if entry.get('type') == 'token_usage_record' or payload.get('type') == 'token_usage_record':
            modern = True
    session = meta.get('id') or meta.get('thread_id') or meta.get('session_id') or path.stem
    model = meta.get('model')
    previous = None
    pending = {}
    for entry in entries:
        payload = entry.get('payload')
        if not isinstance(payload, dict):
            continue
        kind = entry.get('type')
        ts = parse_ts(entry.get('timestamp'))
        if kind == 'turn_context':
            model = payload.get('model', model)
        usage, key = None, None
        if kind == 'token_usage_record' or payload.get('type') == 'token_usage_record':
            usage = payload.get('usage')
            if payload.get('response_id'):
                key = 'response:' + payload['response_id']
        elif not modern and payload.get('type') == 'token_count':
            info = payload.get('info') or {}
            usage = info.get('last_token_usage')
            # Older rollouts repeat the same event; skip an exact replay.
            fingerprint = json.dumps([usage, info.get('total_token_usage')], sort_keys=True)
            if fingerprint == previous:
                continue
            previous = fingerprint
            key = 'legacy:' + hashlib.sha256(json.dumps([session, entry.get('timestamp'), usage], sort_keys=True).encode()).hexdigest()
        if key and isinstance(usage, dict) and ts and ts >= since and key not in data['codex']:
            data['codex'][key] = dict(ts=ts, session=session, model=payload.get('model', model), usage=dict(
                input=num(usage.get('input_tokens')), cached=num(usage.get('cached_input_tokens')),
                output=num(usage.get('output_tokens'))))
        if kind != 'response_item':
            continue
        call_id = payload.get('call_id') or payload.get('id')
        if payload.get('type') in ('function_call_output', 'custom_tool_call_output'):
            commands = pending.pop(call_id, None)
            if commands and len(commands) == 1:
                failed = output_failed(payload.get('output'))
                if failed is not None:
                    data['outcomes'][('codex', call_id)] = failed
            continue
        commands = shell_commands(payload)
        if not commands or not (ts and ts >= since):
            continue
        pending[call_id] = commands
        for index, command in enumerate(commands):
            cid = call_id if len(commands) == 1 else '%s:%d' % (call_id, index)
            data['calls'].setdefault(('codex', cid), dict(harness='codex', session=session, agent=None, ts=ts, command=command))


def head(command):
    """A short label for a command: program plus subcommand, never paths or arguments."""
    for segment in re.split(r'&&|\|\||;|\||\n', command):
        try:
            words = shlex.split(segment, posix=True)
        except ValueError:
            words = segment.split()
        while words:
            if re.match(r'^[A-Za-z_][A-Za-z0-9_]*=', words[0]):
                words = words[1:]
            elif words[0] in WRAPPERS:
                words = words[1:]
                # Drop the wrapper's own flags and durations, as in timeout 30s or nice -n 5.
                while words and (words[0].startswith('-') or re.match(r'^\d+(\.\d+)?[smhd]?$', words[0])):
                    words = words[1:]
            else:
                break
        if not words:
            continue
        program = words[0].split('/')[-1]
        if program in SKIP_PROGRAMS or not program:
            continue
        rest = words[1:]
        if program in SHELLS and rest[:1] in (['-c'], ['-lc'], ['-ic']) and len(rest) > 1:
            inner = head(rest[1])
            if inner:
                return inner
        if program == 'git':
            # git -C <dir> and -c <key=value> come before the subcommand.
            while len(rest) > 1 and rest[0] in ('-C', '-c', '--git-dir', '--work-tree'):
                rest = rest[2:]
        label = [program]
        interpreter = program.startswith('python') or program in INTERPRETERS
        if program.startswith('python') and rest[:1] in (['-m'], ['-c']):
            label.append(rest[0])
            if rest[0] == '-m' and len(rest) > 1 and SUBCOMMAND.match(rest[1]):
                label.append(rest[1])
        elif interpreter and rest and rest[0] == '-':
            label.append('-')
        elif interpreter and rest and SCRIPT.match(rest[0].split('/')[-1]):
            # The script's file name only, never its folder.
            label.append(rest[0].split('/')[-1])
        elif rest and SUBCOMMAND.match(rest[0]):
            label.append(rest[0])
            second = len(rest) > 1 and SUBCOMMAND.match(rest[1])
            if second and (program in TWO_WORD or (program in RUNNERS and rest[0] in ('run', 'exec', 'x'))):
                label.append(rest[1])
        return ' '.join(label)
    return None


def week_start(ts):
    day = datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).date()
    return day - datetime.timedelta(days=day.weekday())


def summarize(data, days, now):
    since_day = datetime.datetime.fromtimestamp(now - days * 86400, datetime.timezone.utc).date()
    out = dict(days=days, start=since_day, end=datetime.datetime.fromtimestamp(now, datetime.timezone.utc).date(),
               files=data['files'])
    sessions = {'claude': set(), 'codex': set()}
    spend, by_week, by_model, unpriced = 0.0, {}, {}, {}
    for harness, rows, cost in (('claude', data['claude'], claude_cost), ('codex', data['codex'], openai_cost)):
        for row in rows.values():
            sessions[harness].add(row['session'])
            if row['model'] == '<synthetic>':
                continue
            dollars = cost(row['model'], row['usage'])
            if dollars is None:
                label = row['model'] or 'unknown model'
                unpriced[label] = unpriced.get(label, 0) + sum(row['usage'].values())
                continue
            spend += dollars
            week = week_start(row['ts'])
            by_week[week] = by_week.get(week, 0.0) + dollars
            by_model[row['model']] = by_model.get(row['model'], 0.0) + dollars
    calls = sorted(data['calls'].items(), key=lambda kv: kv[1]['ts'])
    for (harness, _), call in calls:
        sessions[harness].add(call['session'])
    known, failures, repeats, seen = 0, 0, 0, set()
    by_head = {}
    for key, call in calls:
        outcome = data['outcomes'].get(key)
        if outcome is None:
            continue
        known += 1
        label = head(call['command']) or 'unlabelled'
        program = label.split(' ')[0]
        if not outcome or program in PROBES or (program == 'sed' and SED_READ.search(call['command'])):
            continue
        failures += 1
        if label in seen:
            repeats += 1
        seen.add(label)
        entry = by_head.setdefault(label, dict(count=0, sessions=set()))
        entry['count'] += 1
        entry['sessions'].add(call['session'])
    top = sorted(((v['count'], len(v['sessions']), k) for k, v in by_head.items() if v['count'] > 1), reverse=True)[:3]
    signatures = []
    for sig_id, pattern, context, label in SIGNATURES:
        rx = re.compile(pattern)
        hits = [c for _, c in calls if (context != 'subagent' or c['agent']) and rx.search(c['command'])]
        signatures.append(dict(id=sig_id, label=label, count=len(hits), sessions=len({c['session'] for c in hits}),
                               share=len(hits) / len(calls) if calls else 0.0))
    out.update(sessions={h: len(s) for h, s in sessions.items()}, spend=spend if (by_model or not unpriced) else None,
               by_week=sorted(by_week.items()), by_model=sorted(by_model.items(), key=lambda kv: -kv[1]),
               unpriced=unpriced, calls=len(calls), known=known, failures=failures, repeats=repeats,
               top=top, signatures=signatures)
    return out


def money(value):
    return '$%s' % format(round(value, 2), ',.2f')


def pct(part, whole):
    return '%.1f%%' % (100.0 * part / whole) if whole else '0.0%'


def render_card(s):
    rows = []
    rows.append('Agent scan, last %d days (%s to %s)' % (s['days'], s['start'].isoformat(), s['end'].isoformat()))
    parts = []
    if s['sessions']['claude']:
        parts.append('Claude Code: %d sessions' % s['sessions']['claude'])
    if s['sessions']['codex']:
        parts.append('Codex: %d threads' % s['sessions']['codex'])
    rows.append(', '.join(parts) if parts else 'No agent sessions found in this window.')
    rows.append('')
    if s['spend'] is not None:
        rows.append('Estimated spend at API list prices: %s' % money(s['spend']))
        for week, dollars in s['by_week']:
            # Clip each Monday-to-Sunday week to the window, so a partial week reads as one.
            first = max(week, s['start'])
            last = min(week + datetime.timedelta(days=6), s['end'])
            span = first.strftime('%b %d') if first == last else '%s to %s' % (first.strftime('%b %d'), last.strftime('%b %d'))
            rows.append('  %-18s %s' % (span, money(dollars)))
        if s['by_model']:
            total = sum(d for _, d in s['by_model']) or 1.0
            shown = ['%s %s (%d%%)' % (m, money(d), round(100 * d / total)) for m, d in s['by_model'][:4]]
            rest = s['by_model'][4:]
            if rest:
                other = sum(d for _, d in rest)
                shown.append('%d others %s (%d%%)' % (len(rest), money(other), round(100 * other / total)))
            rows.append('  By model: ' + ', '.join(shown))
        rows.append('  On a subscription you pay a flat fee; this is what the same tokens cost on the API.')
    if s['unpriced']:
        rows.append('  Tokens with no known price: %s' % format(sum(s['unpriced'].values()), ','))
    rows.append('')
    rows.append('Shell commands your agents ran: %s' % format(s['calls'], ','))
    if s['known']:
        rows.append('  Failed: %s (%s; look-ups such as grep or ls that find nothing are not counted)'
                    % (format(s['failures'], ','), pct(s['failures'], s['known'])))
        if s['failures']:
            rows.append('  Failed again after the same command had already failed: %s (%s of failures)'
                        % (format(s['repeats'], ','), pct(s['repeats'], s['failures'])))
        if s['top']:
            rows.append('  Most repeated failures: ' + ', '.join(
                '%d times in %d sessions' % (count, sessions) for count, sessions, _ in s['top']))
    rows.append('')
    rows.append('Fixed habit patterns (list v2):')
    for sig in s['signatures']:
        rows.append('  %s: %s calls in %d sessions (%s of calls)'
                    % (sig['label'], format(sig['count'], ','), sig['sessions'], pct(sig['count'], s['calls'])))
    rows.append('')
    rows.append('%s, prices as of %s. Counts only: no paths, ids or command text.' % (VERSION, PRICES_AS_OF))
    return '\n'.join(rows)


def render_details(s):
    rows = ['Only on this screen, not part of the card or the export:']
    if s['top']:
        rows.append('  Your most repeated failing commands')
        width = max(len(label) for _, _, label in s['top'])
        for count, sessions, label in s['top']:
            rows.append('    %4d  %s  (%d sessions)' % (count, label.ljust(width), sessions))
        rows.append('  Fix the top one (a line in your agent instructions, a hook or a script), then scan again in a week.')
    else:
        rows.append('  No command failed more than once in this window.')
    if s['unpriced']:
        rows.append('  Models with no known price: ' + ', '.join(sorted(s['unpriced'])))
    return '\n'.join(rows)


def export(s):
    harnesses = [name for name, key in (('claude-code', 'claude'), ('codex', 'codex')) if s['sessions'][key]]
    return dict(version=1, generated_at=s['end'].isoformat(), harnesses=harnesses, window_days=s['days'],
                sessions=s['sessions']['claude'] + s['sessions']['codex'],
                spend_usd=round(s['spend'], 2) if s['spend'] is not None else None,
                signatures=[dict(id=g['id'], count=g['count'], sessions=g['sessions'], share_of_calls=round(g['share'], 6))
                            for g in s['signatures']])


def scan(claude_roots, homes, days, now):
    since = now - days * 86400
    data = dict(files=0, claude={}, codex={}, calls={}, outcomes={})
    scan_claude(claude_roots, since, data)
    scan_codex(homes, since, data)
    return summarize(data, days, now)


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only scan of local coding-agent sessions.')
    parser.add_argument('--days', type=int, default=30, help='how many days back to read (default 30)')
    parser.add_argument('--claude-root', action='append', help='Claude Code projects folder (repeatable)')
    parser.add_argument('--codex-home', action='append', help='Codex home folder (repeatable)')
    parser.add_argument('--card-only', action='store_true', help='print the card without the local details')
    parser.add_argument('--export', metavar='PATH', help='also write the card numbers as JSON (scan export v1)')
    parser.add_argument('--now', help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if args.days < 1:
        parser.error('--days must be at least 1')
    now = parse_ts(args.now) if args.now else time.time()
    if now is None:
        parser.error('--now must be an ISO timestamp')
    roots = args.claude_root
    if not roots:
        base = os.environ.get('CLAUDE_CONFIG_DIR')
        roots = [Path(base).expanduser() / 'projects'] if base else [Path.home() / '.claude' / 'projects']
    started = time.time()
    summary = scan(roots, codex_homes(args.codex_home), args.days, now)
    print(render_card(summary))
    if not args.card_only:
        print('')
        print(render_details(summary))
    if args.export:
        with open(args.export, 'w') as stream:
            json.dump(export(summary), stream, indent=2, sort_keys=True)
            stream.write('\n')
    sys.stderr.write('read %d files in %.1f s\n' % (summary['files'], time.time() - started))
    return 0


if __name__ == '__main__':
    sys.exit(main())
