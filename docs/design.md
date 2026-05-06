# Design Document — TCP Metric Collector

## Purpose

Passive TCP performance monitoring tool. Captures kernel-level TCP metrics for all active sessions to a target IP by periodically querying `ss` (socket statistics). Intended for diagnosing congestion, retransmission, and throughput issues on sender-side Linux hosts.

## Requirements

- Python 3.7+
- Linux with `iproute2` installed
- `click>=8.1.8`
- [uv](https://docs.astral.sh/uv/) (optional, recommended for running)

## Architecture

Single-file Python 3 script. One external dependency: `click` (CLI). Packaged with `uv` (`pyproject.toml`) for reproducible execution and CLI install.

```
┌─────────────────────────────────────────────────────┐
│                      run()                          │
│                                                     │
│  args parse → IP validate → SIGINT/SIGTERM register │
│                     │                               │
│              ┌──────▼──────┐                        │
│              │  while True │  ← 100ms poll loop     │
│              │  ss -i dst  │  (subprocess.run)      │
│              │  append raw │  + wall-clock time()   │
│              └──────┬──────┘                        │
│                     │ Ctrl+C / SIGTERM               │
│                     ▼                               │
│           print_tcp_metrics(list)                   │
│                     │                               │
│         parse sessions → parse metrics              │
│                     │                               │
│              stdout output                          │
└─────────────────────────────────────────────────────┘
```

## Data Flow

### Collection Phase

```
subprocess.run(["ss", "-i", "dst", <ip>])
  → filter lines containing <ip> or "wscale"
  → (time(), filtered_output) appended to tcp_metrics[]
  → repeated every DEFAULT_SLEEP (0.1s) until SIGINT/SIGTERM
```

All raw `ss` output held in memory as list of `(timestamp, str)` tuples. No streaming parse during collection.

### Parse Phase (triggered on SIGINT or SIGTERM)

```
tcp_metrics[]  (list of (float, str) tuples)
  → iterate snapshots
    → scan lines for "tcp " → extract session key (src:port_dst:port)
    → scan next line for "wscale" → extract metrics
      → special-case: "send <val>" → "send:<val>" before regex
      → RE_TCP_METRIC_PARAM_LOOKUP extracts key:value pairs
      → real wall-clock timestamp stored per sample
  → print per-session blocks
```

### Session Key Format

`{src_ip}:{src_port}|{dst_ip}:{dst_port}`

`|` used as separator (constant `SESSION_SEP`) — cannot appear in an IPv4 address or port number, making the key unambiguous. Used as dict key in `sessions`. `curr_session` string tracks current session while iterating lines.

## Key Design Decisions

### 1. Buffer-all, parse-on-exit

Simplifies collection loop. Avoids parse overhead during sampling window. Trade-off: unbounded memory growth for long captures. Acceptable for short diagnostic sessions.

### 2. `subprocess.run` instead of `os.popen`

`os.popen` is deprecated in Python 3. `subprocess.run` with `capture_output=True, text=True` is the idiomatic replacement. Args passed as list — no shell injection risk.

### 3. `ss` instead of `/proc/net/tcp`

`ss` surfaces extended TCP info (`-i` flag: internal kernel socket stats — cwnd, rtt, etc.) not available in `/proc/net/tcp`. Requires `iproute2`.

### 4. Real wall-clock timestamps

Each sample stores `time()` at collection time. Accurate for correlation with external events. Previous versions used synthetic `sample_count * DEFAULT_SLEEP` which drifted if `ss` call exceeded 100ms.

### 5. IPv4-only validation via `is_valid_ipv4()`

`ipaddress.ip_address()` accepts both IPv4 and IPv6. Previous `is_valid_ip()` accepted IPv6 addresses silently — the CLI would start collecting but `RE_TCP_SESSION_LOOKUP` only matches `\d+\.\d+\.\d+\.\d+` so no sessions would ever be found. User would see no output with no error.

Fixed by checking `isinstance(..., ipaddress.IPv4Address)`. IPv6 input now fails immediately with a clear error: `'::1' is not a valid IPv4 address.` The function is renamed `is_valid_ipv4()` to make the constraint explicit. The `--help` text and docstring also say "IPv4".

### 6. SIGTERM handling

Both `SIGINT` (Ctrl+C) and `SIGTERM` (`kill`) now trigger graceful shutdown and metric printout. Previous version silently exited on SIGTERM without printing results.

### 7. `click` instead of `argparse`

`click` replaces `argparse` for CLI parsing. `@click.command()` + `@click.option()` decorators on `run()` make the function the entrypoint directly — no separate `args = parser.parse_args()` call. IP validation failure raises `click.BadParameter` which formats the error consistently with click's own error output. `click.echo()` replaces `print()` throughout for correct stdout/stderr routing (`err=True` for errors). The `run()` function docstring becomes the `--help` description automatically.

### 8. uv / uvx packaging

`pyproject.toml` declares the project with `[project.scripts]` entrypoint so `uvx --from . tcp-metric-collector` works as a zero-install CLI. PEP 723 inline script metadata (`# /// script`) in the source file allows `uv run tcp_metrics_collector.py` without any project setup. `hatchling` used as build backend with explicit `packages` config since the module is a single flat `.py` file (not a package directory).

### 9. `send` metric normalization

`ss` outputs send rate as `send Xbps` (space-separated), unlike all other metrics which use `key:value`. Line with `"send "` is special-cased before regex parsing.

## Regex Patterns

| Pattern | Purpose |
|---------|---------|
| `RE_TCP_SESSION_LOOKUP` | Extracts `(src_ip:port, dst_ip:port)` from `ss` session line |
| `RE_TCP_METRIC_PARAM_LOOKUP` | Extracts `key:value` pairs from metrics line |

## Known Limitations

- **IPv4 only**: enforced at input by `is_valid_ipv4()`. IPv6 addresses are rejected with an explicit error at startup rather than silently producing no output.
- **Memory**: `tcp_metrics[]` grows unbounded. Could rotate or stream-parse for long captures.
- **Output format**: JSON per line is human-readable but not ideal for machine ingestion. Could output NDJSON or CSV.
