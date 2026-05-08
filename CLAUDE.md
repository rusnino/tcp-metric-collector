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

Requires Linux with `ss` command available (iproute2) and Python 3.10+. Must run on sender side.

Project uses `uv` for environment management (`pyproject.toml`). One external dependency: `click>=8.1.8`.

## Architecture

Single-file script (`tcp_metrics_collector.py`). Key functions:

- `_collect_snapshot(ip, shutdown_ref)` → `list[str] | None` — runs `ss -H -n -i dst <ip>`, filters to session+metrics pairs; returns `None` on shutdown
- `_parse_session_line(line)` → `(src, dst) | None`
- `_parse_metrics_line(line)` → `dict[str, int | float | str | None] | None`
- `_parse_snapshot(lines, ts, sessions, fmt, stream, out, csv_writer)` — merges one snapshot into `sessions` dict in-place; emits immediately for ndjson/csv or `--stream`
- `_emit_record(ts, src, dst, metrics, fmt, out, csv_writer)` — formats and writes one record in requested format
- `_print_sessions(sessions, out)` — formats buffered text output on exit (text format only)
- `run()` — click entrypoint; monotonic tick scheduler, `shutdown_ref` flag, `--duration` + `--max-samples` termination

Collection and parsing merged: each snapshot parsed immediately in the loop. `sessions` stores `(timestamp, dict)` tuples — not pre-serialised strings. Raw `ss` output never retained beyond current cycle.

CLI options: `-a IP`, `--duration N`, `--max-samples N`, `--output FILE`, `--stream`, `--format text|ndjson|csv`, `--verbose`, `--debug`, `--version`

`snapshot_time = time()` is captured before `_collect_snapshot()` to timestamp the sample start, not the end of ss execution.

## Running Tests

```bash
uv run pytest               # all tests
uv run pytest -v            # verbose
uv run pytest tests/test_parser.py::TestParseSnapshot  # single class
uv run pytest tests/test_cli.py   # CLI integration tests only
```

Two test files:

- `tests/test_parser.py` — unit tests for `is_valid_ipv4`, `_parse_session_line`, `_parse_metrics_line`, `_parse_snapshot`. No real `ss` invocation. Fixtures in `tests/fixtures/` simulate `ss -i` output.
- `tests/test_cli.py` — CLI integration tests via `click.testing.CliRunner`. `_collect_snapshot` mocked with `unittest.mock.patch`. Covers input validation (exit codes), text/ndjson/csv output correctness, `--output` file errors, `ss` failure modes.

Fixtures in `tests/fixtures/`: `ss_estab_single.txt`, `ss_multiple_sessions.txt`, `ss_closing.txt`, `ss_ipv6.txt`, `ss_no_wscale.txt`, `ss_no_metrics.txt`, `ss_send_only.txt`.

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
