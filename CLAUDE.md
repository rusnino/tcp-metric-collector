# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Usage

```bash
python tcp_metrics_collector.py -a <destination_ip>
# Press Ctrl+C to stop and print collected metrics
```

Requires Linux with `ss` command available (iproute2) and Python 3.6+. Must run on sender side.

## Architecture

Single-file script (`tcp_metrics_collector.py`). Flow:

1. **Collection loop** — polls `ss -i dst <ip>` every 100ms, appends raw output to `tcp_metrics` list
2. **SIGINT handler** — Ctrl+C triggers `print_tcp_metrics()` on accumulated data then exits
3. **Parsing** (`print_tcp_metrics`) — walks raw `ss` output, matches TCP sessions via regex, extracts 7 metrics per sample: `cwnd`, `rtt`, `mss`, `ssthresh`, `send`, `unacked`, `retrans`
4. **Output** — per-session blocks with `timestamp - {metrics_json}` lines

Key detail: `send` metric needs special handling — `ss` outputs it as `send <value>` (space-separated) not `send:<value>`, so it's normalized via string replace before regex parsing (`tcp_metrics_collector.py:81`).

Timestamps are synthetic: each sample increments by `DEFAULT_SLEEP` (0.1s), not wall-clock time.

## Commit Policy

Each functionality change must be its own commit. Commit message format:

```
<type>: <short summary>

<detailed explanation of what changed, why, and any side effects or trade-offs>
```

Types: `feat` / `fix` / `refactor` / `docs` / `test`. Body required for all non-trivial changes — explain the why, not just the what.

After every commit, push immediately:

```bash
git push origin master
```
