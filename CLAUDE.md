# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Usage

```bash
# Recommended — uv manages the environment automatically
uv run tcp_metrics_collector.py -a <destination_ip>

# As installed CLI tool via uvx
uvx --from . tcp-metric-collector -a <destination_ip>

# Plain Python fallback
python3 tcp_metrics_collector.py -a <destination_ip>
```

Requires Linux with `inet_diag` netlink support (kernel ≥2.6.14, standard) and Python 3.10+. Needs `CAP_NET_ADMIN` or root. Must run on sender side.

Project uses `uv` for environment management (`pyproject.toml`). External dependencies: `click>=8.1.8`, `pyroute2>=0.7`.

## Architecture

Single-file script (`tcp_metrics_collector.py`). Key functions:

- `_collect_snapshot(ip, shutdown_ref, timeout)` → `list[(src, dst, metrics)] | None` — queries kernel via `DiagSocket.get_sock_stats` (inet_diag netlink), filters ESTABLISHED sessions to dst ip, maps tcp_info → metric dict; returns `None` on shutdown
- `_extract_metrics(tcp_info)` → `MetricDict` — maps tcp_info fields to output schema
- `_format_rate(bps)` → `str` — formats bytes/s as "X.XXMbps" etc.
- `_parse_snapshot(records, ts, sessions, fmt, stream, out, csv_writer)` — iterates `(src, dst, metrics)` tuples; emits immediately for ndjson/csv or `--stream`, otherwise appends to `sessions`
- `_emit_record(ts, src, dst, metrics, fmt, out, csv_writer)` — formats and writes one record in requested format
- `_print_sessions(sessions, out)` — formats buffered text output on exit (text format only)
- `run()` — click entrypoint; monotonic tick scheduler, `shutdown_ref` flag, `--duration` + `--max-samples` termination

Collection and parsing merged: each snapshot parsed immediately in the loop. `sessions` stores `(timestamp, dict)` tuples — not pre-serialised strings. Raw `ss` output never retained beyond current cycle.

CLI options: `-a IP` (IPv4 or IPv6), `--duration N` (after first session seen), `--max-samples N`, `--output FILE`, `--stream`, `--format text|ndjson|csv`, `--verbose`, `--debug`, `--poll-timeout N`, `--version`

Key constants:
- `DEFAULT_SLEEP = 0.1` — poll interval (seconds); monotonic tick scheduler keeps this exact
- `POLL_TIMEOUT = 5.0` — default max seconds for kernel netlink response; overridable via `--poll-timeout`
- `SESSION_SEP = "|"` — session key separator (safe: cannot appear in IPv4:port)

`snapshot_time = time()` captured before `_collect_snapshot()` — timestamp is sample start, not ss completion.

## Running Tests

```bash
uv run pytest               # all tests
uv run pytest -v            # verbose
uv run pytest tests/test_parser.py::TestParseSnapshot  # single class
uv run pytest tests/test_cli.py   # CLI integration tests only
```

Two test files:

- `tests/test_parser.py` — unit tests for `is_valid_ip`, `_format_rate`, `_extract_metrics`, `_collect_snapshot`, `_parse_snapshot`. No real netlink calls — `DiagSocket` mocked via `unittest.mock.MagicMock`. `_make_sock()` helper builds mock socket dicts.
- `tests/test_cli.py` — CLI integration tests via `click.testing.CliRunner`. `_collect_snapshot` mocked to return `list[(src, dst, metrics)]`. Covers input validation, IPv6 acceptance, text/ndjson/csv output, `--output` file errors, inet_diag failure.

## Commit Policy

Each functionality change must be its own commit. Commit message format:

```
<type>: <short summary>

<detailed explanation of what changed, why, and any side effects or trade-offs>
```

Types: `feat` / `fix` / `refactor` / `docs` / `test`. Body required for all non-trivial changes — explain the why, not just the what.

After every commit, push immediately:

```bash
git push origin main
```
