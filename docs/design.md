# Design Document — TCP Metric Collector

## Purpose

Passive TCP performance monitoring tool. Captures kernel-level TCP metrics for all active sessions to a target IP by periodically querying `ss` (socket statistics). Intended for diagnosing congestion, retransmission, and throughput issues on sender-side Linux hosts.

## Requirements

- Python 3.10+
- Linux with `iproute2` installed
- `click>=8.1.8`
- [uv](https://docs.astral.sh/uv/) (optional, recommended for running)

## Architecture

Single-file Python 3 script. One external dependency: `click` (CLI). Packaged with `uv` (`pyproject.toml`) for reproducible execution and CLI install.

```
┌──────────────────────────────────────────────────────────┐
│                         run()                            │
│                                                          │
│  args parse → IP validate → SIGINT/SIGTERM register      │
│                      │                                   │
│               ┌──────▼──────┐                            │
│               │ while !stop │  ← 100ms poll loop         │
│               │ _collect()  │  subprocess.run ss         │
│               │ _parse()    │  parse into sessions{}     │
│               │ [--stream]  │  optional: emit each line  │
│               └──────┬──────┘                            │
│                      │ Ctrl+C / SIGTERM / --duration     │
│                      │ / --max-samples                   │
│                      ▼                                   │
│          _print_sessions() or --stream already done      │
└──────────────────────────────────────────────────────────┘
```

## Data Flow

### Collection + Parse (merged, runs each poll iteration)

```
_collect_snapshot(ip)
  → subprocess.run(["ss", "-i", "dst", ip])
  → filter: keep only (session_line, adjacent wscale_line) pairs
  → return list[str]

_parse_snapshot(lines, snapshot_time, sessions, stream, out)
  → for i, line in enumerate(lines):
      session_key = _parse_session_line(line)     # None → skip
      metrics     = _parse_metrics_line(lines[i+1]) # None → skip
      sessions[session_key].append((ts, metrics))
      if stream: write line to out immediately
```

Metrics are parsed and stored into `sessions` on every poll cycle. Raw `ss` output is never retained — only `(timestamp, dict)` tuples per session. Memory grows proportionally to **unique sessions × samples per session**, not to total raw output volume.

### Session Key Format

`{src_ip}:{src_port}|{dst_ip}:{dst_port}`

`|` used as separator (constant `SESSION_SEP`) — cannot appear in an IPv4 address or port number, making the key unambiguous.

### Output Modes

| Format | Trigger | Behaviour |
|--------|---------|-----------|
| `text` (default) | exit | `_print_sessions()` called once after loop ends; session blocks |
| `text --stream` | per-sample | One line per sample emitted immediately; no session blocks |
| `ndjson` | per-sample | One valid JSON object per line, always streamed |
| `csv` | per-sample | Header + one row per sample, always streamed |

`ndjson` and `csv` always stream per-record regardless of `--stream` flag.
Both include `ts`, `src`, `dst` fields alongside the 7 metrics.
All modes write to stdout by default; `--output FILE` redirects to a file.

### Termination Conditions

Loop exits when any of these is true:
- `shutdown` flag set (SIGINT / SIGTERM)
- `--max-samples N` reached
- `--duration N` elapsed

## Key Design Decisions

### 1. Streaming parse — no raw buffer

Previous design buffered all raw `ss` output as `(float, str)` tuples in `tcp_metrics[]` and parsed only on exit. This caused unbounded memory growth even during idle runs (empty snapshot strings still appended every 100ms).

Current design parses each snapshot immediately in the collection loop. Only `(timestamp, dict)` tuples are kept per session. Raw `ss` output is never stored beyond the current poll cycle.

### 2. `subprocess.run` instead of `os.popen`

`os.popen` is deprecated in Python 3. `subprocess.run` with `capture_output=True, text=True` is the idiomatic replacement. Args passed as list — no shell injection risk.

### 3. `ss` instead of `/proc/net/tcp`

`ss` surfaces extended TCP info (`-i` flag: internal kernel socket stats — cwnd, rtt, etc.) not available in `/proc/net/tcp`. Requires `iproute2`.

### 4. Real wall-clock timestamps

Each sample stores `time()` at collection time. Accurate for correlation with external events.

### 5. IPv4-only validation via `is_valid_ipv4()`

`ipaddress.ip_address()` accepts both IPv4 and IPv6. Previous `is_valid_ip()` accepted IPv6 silently — the CLI would start collecting but `RE_TCP_SESSION_LOOKUP` only matches `\d+\.\d+\.\d+\.\d+` so no sessions would ever be found. User would see no output with no error.

Fixed by checking `isinstance(..., ipaddress.IPv4Address)`. IPv6 input fails immediately: `'::1' is not a valid IPv4 address.`

### 6. SIGTERM handling

Both `SIGINT` (Ctrl+C) and `SIGTERM` (`kill`) set a `shutdown` flag. The loop exits cleanly after the current iteration, then calls `_print_sessions()` (or skips it in `--stream` mode where output was already emitted).

### 7. `--format text|ndjson|csv` — proper structured output

Previous output did `metric[1:-1]` — stripped `{` and `}` from `json.dumps()`. Looked like JSON fields but was not valid JSON. Could not be piped to `jq`, parsed by CSV readers, or consumed by any standard tooling.

`_parse_session_line()` now returns `(src, dst)` tuple. `_parse_metrics_line()` returns a raw `dict`. Internal storage is `dict[key, list[tuple[float, dict]]]` — formatters receive typed data, not pre-serialised strings.

`--format ndjson` emits `{"ts":..., "src":..., "dst":..., <metrics>}` — one valid JSON object per line. `--format csv` emits RFC 4180 CSV with a header row. Both always stream per-record. `--format text` (default) preserves human-readable session block output.

### 8. `click` instead of `argparse`

`@click.command()` + `@click.option()` decorators make `run()` the entrypoint directly. IP validation failure raises `click.BadParameter` for consistent error formatting. `click.echo()` used throughout; `err=True` routes errors to stderr.

### 9. uv / uvx packaging

`pyproject.toml` declares `[project.scripts]` entrypoint for `uvx --from . tcp-metric-collector`. PEP 723 inline script metadata enables `uv run tcp_metrics_collector.py` without project setup. `hatchling` build backend with explicit `packages` config for flat single-file module.

### 10. `send` metric normalization

`ss` outputs send rate as `send Xbps` (space-separated). Normalised to `send:Xbps` before regex matching.

### 11. Monotonic tick scheduler

`sleep(DEFAULT_SLEEP)` after each `ss` call caused effective interval of `runtime(ss) + 100ms`. On a busy host ss can take 20–80ms, making the interval non-uniform and cumulative drift observable in cwnd/RTT series.

Fix: `next_tick` advances by exactly `DEFAULT_SLEEP` each iteration. `sleep(max(0, next_tick - monotonic()))` sleeps only the remaining budget. If ss overruns, sleep is skipped. `monotonic()` used for scheduling; `time()` for output timestamps (wall-clock, needed for external correlation).

### 12. Pair-based parsing — no cross-line state

Each session line is paired atomically with `lines[i+1]`. `_parse_session_line()` and `_parse_metrics_line()` each return `None` on skip conditions. No `curr_session` state persists — a skipped session line cannot cause metrics to leak into a previous session.

## Regex Patterns

| Pattern | Purpose |
|---------|---------|
| `RE_TCP_SESSION_LOOKUP` | Extracts `(src_ip:port, dst_ip:port)` from `ss` session line |
| `RE_TCP_METRIC_PARAM_LOOKUP` | Extracts known metric `key:value` pairs (allowlist of 7 keys) |

## Known Limitations

- **IPv4 only**: enforced at input by `is_valid_ipv4()`.
- **In-memory accumulation**: parsed tuples still grow with session count × duration. For very long runs, use `--max-samples` or `--duration` to bound memory.
- **Output format**: `--format ndjson` or `--format csv` for machine-readable output. `text` format is human-readable only.
