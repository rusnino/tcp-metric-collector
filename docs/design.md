# Design Document — TCP Metric Collector

## Purpose

Passive TCP performance monitoring tool. Captures kernel-level TCP metrics for all ESTABLISHED sessions to a target IP by querying the kernel `inet_diag` netlink interface every 100ms. Intended for diagnosing congestion, retransmission, and throughput issues on sender-side Linux hosts.

## Requirements

- Python 3.10+
- Linux kernel ≥2.6.14 (inet_diag netlink)
- `CAP_NET_ADMIN` or root
- `click>=8.1.8`, `pyroute2>=0.7`
- [uv](https://docs.astral.sh/uv/) (optional, recommended for running)

## Architecture

Single-file Python 3 script. Two external dependencies: `click` (CLI) and `pyroute2` (netlink). Packaged with `uv` (`pyproject.toml`) for reproducible execution and CLI install.

```
┌──────────────────────────────────────────────────────────┐
│                         run()                            │
│                                                          │
│  args parse → IP validate → SIGINT/SIGTERM register      │
│                      │                                   │
│               ┌──────▼──────┐                            │
│               │ while !stop │  ← 100ms poll loop         │
│               │ _collect()  │  DiagSocket.get_sock_stats │
│               │ _extract()  │  tcp_info → MetricDict     │
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

Previous design buffered all raw `ss` stdout as `(float, str)` tuples and parsed only on exit. Caused unbounded memory growth even during idle runs (empty snapshots still appended every 100ms).

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

### 7. `--format text|ndjson|csv` and typed metric schema

`--format ndjson` emits one valid JSON object per line (`{"ts":..., "src":..., "dst":..., <metrics>}`). `--format csv` emits RFC 4180 CSV with a header row. Both always stream per-record. `--format text` (default) buffers and prints session blocks on exit.

All fields are typed directly from `tcp_info` kernel struct. No string-to-int coercion needed:

| Field | Type | Source |
|-------|------|--------|
| `cwnd`, `mss`, `ssthresh`, `unacked` | `int \| None` | Direct from tcp_info (None if field is 0/absent) |
| `rtt_ms`, `rttvar_ms` | `float \| None` | `tcpi_rtt` / `tcpi_rttvar` ÷ 1000 (µs → ms) |
| `retrans_cur`, `retrans_total` | `int \| None` | `tcpi_retransmits` / `tcpi_total_retrans` |
| `send` | `str \| None` | Derived rate string — `None` if rtt=0 |

`None` serialises as JSON `null` natively. CSV absent fields are empty cells (see Known Limitations).

### 8. `click` instead of `argparse`

`@click.command()` + `@click.option()` decorators make `run()` the entrypoint directly. IP validation failure raises `click.BadParameter` for consistent error formatting. `click.echo()` used throughout; `err=True` routes errors to stderr.

### 9. uv / uvx packaging

`pyproject.toml` declares `[project.scripts]` entrypoint for `uvx --from . tcp-metric-collector`. PEP 723 inline script metadata enables `uv run tcp_metrics_collector.py` without project setup. `hatchling` build backend with explicit `packages` config for flat single-file module.

### 10. `send` rate — derived from tcp_info

`send` is not a direct tcp_info field. Derived as:
```
send_bps = tcpi_snd_cwnd * tcpi_snd_mss * 8 * 1_000_000 / tcpi_rtt
```
Where `tcpi_rtt` is in microseconds. Matches the formula used by `ss` iproute2 internally (`cwnd * mss * 8000 / rtt_ms`). Result formatted via `_format_rate()` as e.g. `"84.7Mbps"`.

### 11. Monotonic tick scheduler

`sleep(DEFAULT_SLEEP)` after each poll caused effective interval of `runtime(query) + 100ms`. Netlink queries are <1ms locally so drift is minimal, but the tick scheduler ensures correctness regardless.

Fix: `next_tick` advances by exactly `DEFAULT_SLEEP` each iteration. `sleep(max(0, next_tick - monotonic()))` sleeps only the remaining budget. If the query overruns `DEFAULT_SLEEP`, sleep is skipped. `monotonic()` for scheduling; `time()` for output timestamps.

### 12. Daemon thread for netlink timeout

`DiagSocket.get_sock_stats()` is a blocking kernel call that cannot be interrupted from userspace. To enforce `--poll-timeout`, the call runs in a `threading.Thread(daemon=True)`. The main thread calls `thread.join(timeout=poll_timeout)` and checks `thread.is_alive()`.

If timed out, the thread is abandoned. As a daemon it does not prevent process exit and does not accumulate as a non-daemon thread across repeated timeouts. When the kernel eventually responds, the abandoned thread exits silently.

### 13. No `sys.exit()` in `run()` — idiomatic Click

`sys.exit(0)` was called at the end of `run()` and `sys.exit(1)` on collection failure. Click manages exit codes itself; calling `sys.exit()` inside a Click command bypasses that and makes `CliRunner`-based testing awkward (the runner catches `SystemExit`, so tests worked, but it's non-idiomatic).

`run()` now returns normally on success. The failure path raises `click.ClickException(msg)` — Click catches it, prints `Error: <msg>` to stderr, and exits with code 1.

## Known Limitations

- **`CAP_NET_ADMIN` required**: inet_diag queries need elevated privileges; run with `sudo` or grant the capability.
- **Only ESTABLISHED sessions**: `SS_CONN` bitmask includes SYN_SENT/SYN_RECV but the filter accepts only `idiag_state == 1` (ESTABLISHED). Short-lived connections that never reach ESTABLISHED are invisible.
- **In-memory accumulation (text mode only)**: `sessions` dict grows with session count × duration, but only in `--format text` without `--stream`. All other modes emit and discard per-record (O(1) memory per cycle).
- **`send` is derived**: computed as `cwnd * mss * 8e6 / rtt_us` — a bandwidth estimate, not a direct kernel measurement. May differ from kernel pacing rate in congestion.
- **CSV null representation**: absent metric fields are empty cells (`csv.DictWriter` default), not `"null"`. NDJSON uses JSON `null`. Consumers must normalise at read time.
- **Output format**: `--format ndjson` or `--format csv` for machine-readable output. `text` format is human-readable only.
