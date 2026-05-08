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
_collect_snapshot(ip, shutdown_ref, timeout)
  → ThreadPoolExecutor wraps DiagSocket.get_sock_stats(SS_CONN, INET_DIAG_INFO_EXT)
  → fut.result(timeout=timeout)  — raises ClickException on hang
  → for each socket: filter idiag_state==ESTABLISHED, dst_canonical==ip_canonical
  → _extract_metrics(tcp_info) → MetricDict
  → _format_addr(src_canonical, sport) / _format_addr(dst_canonical, dport)
  → return list[(src, dst, MetricDict)]  or  None on shutdown

_parse_snapshot(records, snapshot_time, sessions, fmt, stream, out, csv_writer)
  → for src, dst, metrics in records:
      if stream or fmt in (ndjson, csv):
          emit immediately → discard (O(1) memory)
      else:
          sessions[key].append((ts, metrics))      # text mode only
```

Metrics structured directly from `tcp_info` kernel struct — no text parsing.
Only `(timestamp, MetricDict)` tuples retained per session. Raw netlink data not stored.

### Session Key Format

`{src_addr}|{dst_addr}` where addresses use RFC 2732 notation:
- IPv4: `192.168.1.1:80`
- IPv6: `[2001:db8::1]:80`

`|` used as separator (`SESSION_SEP`) — cannot appear in either format.
IPv6 bracket notation prevents port from being ambiguous with address colons.

### Output Modes

| Format | Trigger | Behaviour |
|--------|---------|-----------|
| `text` (default) | exit | `_print_sessions()` called once after loop ends; session blocks |
| `text --stream` | per-sample | One line per sample emitted immediately; no session blocks |
| `ndjson` | per-sample | One valid JSON object per line, always streamed |
| `csv` | per-sample | Header + one row per sample, always streamed |

`ndjson` and `csv` always stream per-record regardless of `--stream` flag.
Both include `ts`, `src`, `dst` fields alongside the 9 metric fields
(`cwnd`, `mss`, `ssthresh`, `unacked`, `rtt_ms`, `rttvar_ms`, `retrans_cur`, `retrans_total`, `send`).
All modes write to stdout by default; `--output FILE` redirects to a file.

### Termination Conditions

Loop exits when any of these is true:
- `shutdown` flag set (SIGINT / SIGTERM)
- `--max-samples N` reached (counted from first sample regardless of sessions)
- `--duration N` elapsed since first sample with ≥1 session

**`--duration` countdown semantics:** `duration_start_mono` is `None` until the first `_collect_snapshot()` call that returns a non-empty list (at least one session pair). Until then the tool polls indefinitely. This lets users start the collector before initiating traffic — the window starts when the first TCP session is actually observed, not when the command is invoked.

## Key Design Decisions

### 1. Streaming parse — no raw buffer

Previous design buffered all raw `ss` output as `(float, str)` tuples in `tcp_metrics[]` and parsed only on exit. This caused unbounded memory growth even during idle runs (empty snapshot strings still appended every 100ms).

Current design queries structured `tcp_info` from the kernel directly. Only `(timestamp, MetricDict)` tuples retained per session. No text parsing, no subprocess, no raw buffers.

### 2. `inet_diag` netlink instead of `ss` subprocess

Previous design ran `subprocess.run(["ss", "-H", "-n", "-i", "dst", ip])` every 100ms and regex-parsed the stdout text. Problems: subprocess fork/exec overhead, fragile text format, IPv4-only regex, short-lived connection blind spots between samples.

Current design uses `pyroute2.DiagSocket.get_sock_stats(family, SS_CONN, INET_DIAG_INFO_EXT)` — the same kernel interface `ss` itself queries via `NETLINK_SOCK_DIAG`. Returns structured `inet_diag_msg` objects with `tcp_info` NLA attributes. No text parsing. Supported on Linux kernel ≥2.6.14.

Requires `CAP_NET_ADMIN` or root.

### 3. `tcp_info` struct — kernel source of truth

`INET_DIAG_INFO` attribute (extension bitmask `1<<1`) requests the `tcp_info` struct attached to each socket. This is the canonical source for all metrics — the same data that `ss -i` displays. Fields are binary, typed integers (µs, segments, bytes) requiring no normalization beyond unit conversion.

Key fields and conversions:

| Output field | tcp_info field | Conversion |
|---|---|---|
| `cwnd` | `tcpi_snd_cwnd` | direct (segments) |
| `mss` | `tcpi_snd_mss` | direct (bytes) |
| `ssthresh` | `tcpi_snd_ssthresh` | direct |
| `unacked` | `tcpi_unacked` | direct (segments) |
| `rtt_ms` | `tcpi_rtt` | ÷1000 (kernel stores µs) |
| `rttvar_ms` | `tcpi_rttvar` | ÷1000 (kernel stores µs) |
| `retrans_cur` | `tcpi_retransmits` | direct |
| `retrans_total` | `tcpi_total_retrans` | direct |
| `send` | derived | `cwnd*mss*8*1e6/rtt_us` → bits/s → formatted |

### 4. Real wall-clock timestamps — captured before netlink query

`snapshot_time = time()` captured immediately before `_collect_snapshot()`. Netlink queries are local kernel calls (<1ms normally) but timestamping before ensures the anchor reflects sample intent, not query completion time. Aligned with monotonic tick scheduler.

### 5. IPv4 and IPv6 support via `is_valid_ip()`

`is_valid_ip()` accepts both families. `AF_INET6` selected when `":" in ip`. IPv6 addresses normalised to compressed form (`ipaddress.ip_address(addr).compressed`) before comparison with `idiag_dst`/`idiag_src` — prevents mismatch between user-supplied expanded form and kernel-returned compressed form. Output uses RFC 2732 bracket notation: `[2001:db8::1]:80`.

### 6. SIGTERM handling

Both `SIGINT` (Ctrl+C) and `SIGTERM` (`kill`) set a `shutdown` flag. The loop exits cleanly after the current iteration, then calls `_print_sessions()` (or skips it in `--stream` mode where output was already emitted).

### 7. `--format text|ndjson|csv` — proper structured output

Previous output did `metric[1:-1]` — stripped `{` and `}` from `json.dumps()`. Looked like JSON fields but was not valid JSON. Could not be piped to `jq`, parsed by CSV readers, or consumed by any standard tooling.

`_parse_session_line()` now returns `(src, dst)` tuple. `_parse_metrics_line()` returns a typed `dict` — not pre-serialised strings.

`--format ndjson` emits one valid JSON object per line. `--format csv` emits RFC 4180 CSV with a header row. Both always stream per-record. `--format text` (default) preserves human-readable session block output.

### 7a. Typed metric schema — no int/str ambiguity

Previous `_parse_metrics_line()` initialised all fields as `int 0` then overwrote found values with `str` from regex. Same field could be `"10"` (str) when present or `0` (int) when absent — type was unpredictable.

Current schema:

| Field | Type | Notes |
|-------|------|-------|
| `cwnd`, `mss`, `ssthresh`, `unacked` | `int \| None` | Direct integer parse |
| `rtt_ms`, `rttvar_ms` | `float \| None` | Split from `rtt:X/Y` |
| `retrans_cur`, `retrans_total` | `int \| None` | Split from `retrans:X/Y` |
| `send` | `str \| None` | Kept as string; unit varies (Mbps/Kbps/Gbps) |

`None` means absent in ss output — distinguishable from `0` (present but zero). `json.dumps()` serialises `None` as JSON `null` natively.

### 8. `click` instead of `argparse`

`@click.command()` + `@click.option()` decorators make `run()` the entrypoint directly. IP validation failure raises `click.BadParameter` for consistent error formatting. `click.echo()` used throughout; `err=True` routes errors to stderr.

### 9. uv / uvx packaging

`pyproject.toml` declares `[project.scripts]` entrypoint for `uvx --from . tcp-metric-collector`. PEP 723 inline script metadata enables `uv run tcp_metrics_collector.py` without project setup. `hatchling` build backend with explicit `packages` config for flat single-file module.

### 10. `send` metric normalization

`ss` outputs send rate as `send Xbps` (space-separated). Normalised to `send:Xbps` before regex matching.

### 11. Monotonic tick scheduler

`sleep(DEFAULT_SLEEP)` after each `ss` call caused effective interval of `runtime(ss) + 100ms`. On a busy host ss can take 20–80ms, making the interval non-uniform and cumulative drift observable in cwnd/RTT series.

Fix: `next_tick` advances by exactly `DEFAULT_SLEEP` each iteration. `sleep(max(0, next_tick - monotonic()))` sleeps only the remaining budget. If ss overruns, sleep is skipped. `monotonic()` used for scheduling; `time()` for output timestamps (wall-clock, needed for external correlation).

### 12. Pair-based pipeline — filter and parser both step by 2

`_collect_snapshot()` produces `lines` as guaranteed pairs: `[session₀, metrics₀, session₁, metrics₁, …]`. Only complete pairs are kept — session lines without an adjacent metrics line are discarded at filter time.

`_parse_snapshot()` steps over `lines` in strides of 2 (`range(0, len(lines)-1, 2)`), accessing `lines[i]` (session) and `lines[i+1]` (metrics) without bounds checks. No `curr_session` state persists. A session line that fails `_parse_session_line()` skips the pair; no metric can leak to a different session.

### 13. Metric line detection by content, not by wscale token

Previously `_parse_metrics_line()` returned `None` if `"wscale"` was not in the line, and `_collect_snapshot()` filtered adjacency by `"wscale" in line`. This created a hard dependency on `wscale` appearing in ss output — a configuration detail that can vary (e.g. `ss` output without window scaling negotiated, or future ss versions).

The contract is: a line is a metrics line if it matches `_RE_HAS_METRIC`:

```python
_RE_HAS_METRIC = re.compile(RE_TCP_METRIC_PARAM_LOOKUP + r"|\bsend\s+\S")
```

Two forms are accepted:
- **`key:value`** — any of the 7 allowlisted tokens (`cwnd`, `rtt`, `mss`, `ssthresh`, `send`, `unacked`, `retrans`) in colon-separated form.
- **`send VALUE`** — send rate in space-separated form (e.g. `send 84.7Mbps`), which `ss` emits without a colon. `_parse_metrics_line()` normalises this to `send:VALUE` before regex matching; `_RE_HAS_METRIC` must detect it in pre-normalised form to avoid filtering it out in `_collect_snapshot()`.

**Usage split:**
- `_collect_snapshot()` uses `_RE_HAS_METRIC` as a pre-filter to decide which lines to keep (adjacency check, line 169). It runs against the raw, un-normalised line.
- `_parse_metrics_line()` does **not** use `_RE_HAS_METRIC`. It normalises `send VALUE` → `send:VALUE` first, then matches with `RE_TCP_METRIC_PARAM_LOOKUP` (the base pattern). This is correct: the parser operates on a single line in isolation and can normalise before matching.

The two components are kept semantically consistent: `_RE_HAS_METRIC` detects both the colon-form and the raw space-form so the pre-filter accepts every line the parser can handle. If a new metric form is added to `_parse_metrics_line`, `_RE_HAS_METRIC` must be updated accordingly. `wscale` is no longer special-cased anywhere.

### 14. `SS_TIMEOUT` / `--ss-timeout` — configurable ss execution limit

`subprocess.run(["ss", ...])` previously had no timeout. If ss hung (kernel/netlink issue, overloaded host), the collector blocked indefinitely.

`SS_TIMEOUT = 5.0` is the default. Exposed as `--ss-timeout N` (min 0.1s) so users on very loaded or slow hosts can increase it without false timeout errors. The value is passed through `run()` → `_collect_snapshot(timeout=ss_timeout)`.

On `TimeoutExpired`: if `shutdown_ref` is set (Ctrl+C while ss hung), returns `None` (clean exit, buffered results printed). Otherwise raises `click.ClickException`.

### 15. No `sys.exit()` in `run()` — idiomatic Click

`sys.exit(0)` was called at the end of `run()` and `sys.exit(1)` on ss failure. Click manages exit codes itself; calling `sys.exit()` inside a Click command bypasses that and makes `CliRunner`-based testing awkward (the runner catches `SystemExit`, so tests worked, but it's non-idiomatic).

`run()` now returns normally on success. The ss failure path raises `click.ClickException(msg)` — Click catches it, prints `Error: <msg>` to stderr, and exits with code 1.

## Regex Patterns

| Pattern | Purpose |
|---------|---------|
| `RE_TCP_SESSION_LOOKUP` | Raw pattern string for session line — compiled as `_RE_TCP_SESSION` |
| `_RE_TCP_SESSION` | Compiled `RE_TCP_SESSION_LOOKUP`; extracts `(src_ip:port, dst_ip:port)` |
| `_RE_TCP_PREFIX` | `\btcp\s` — fast pre-check in `_parse_session_line()` before full regex |
| `RE_TCP_METRIC_PARAM_LOOKUP` | Raw pattern for metric `key:value` — used to build `_RE_HAS_METRIC` and in `_parse_metrics_line()` |
| `_RE_HAS_METRIC` | Compiled pre-filter in `_collect_snapshot()`: `RE_TCP_METRIC_PARAM_LOOKUP \| \bsend\s+\S` |

## Known Limitations

- **IPv4 only**: enforced at input by `is_valid_ipv4()`.
- **In-memory accumulation (text mode only)**: `sessions` dict grows with session count × duration, but only in `--format text` without `--stream`. In all other modes (`ndjson`, `csv`, `text --stream`) records are emitted and discarded — memory is O(1) per cycle.
- **Output format**: `--format ndjson` or `--format csv` for machine-readable output. `text` format is human-readable only.
- **CSV null representation**: absent metric fields are empty cells (Python `csv.DictWriter` default), not the string `"null"`. NDJSON uses JSON `null`. Consumers that rely on a single absent-value sentinel across formats must normalise at read time.
