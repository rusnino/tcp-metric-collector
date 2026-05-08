# TCP Metric Collector

Collects TCP metrics per destination IPv4 address on Linux. Samples `ss` socket statistics at 100ms intervals; parses each snapshot immediately in the collection loop.

## Requirements

- Linux with `iproute2` (`ss` command)
- Python 3.10+
- `click>=8.1.8` (managed automatically by uv)
- [uv](https://docs.astral.sh/uv/) (recommended) or plain Python 3
- Run on **sender side**

## Installation

### With uv (recommended)

```bash
# Run directly without install
uv run tcp_metrics_collector.py -a <destination_ip>

# Install as CLI tool
uvx --from . tcp-metric-collector -a <destination_ip>
```

### Plain Python

```bash
python3 tcp_metrics_collector.py -a <destination_ip>
```

Press `Ctrl+C` or send `SIGTERM` to stop collection and print results.

> **`--version` in bare-script mode:** `uv run` and `uvx` always install the package, so `--version` reports the correct version from package metadata. When run as `python3 tcp_metrics_collector.py`, the script falls back to reading `pyproject.toml` from the same directory. If `pyproject.toml` is absent, `--version` shows `unknown`.

## Options

| Option | Description |
|--------|-------------|
| `-a IP` | Destination IPv4 address to monitor (required) |
| `--duration N` | Stop after N seconds (N > 0) |
| `--max-samples N` | Stop after N snapshots (N ≥ 1) |
| `--output FILE` | Write results to file instead of stdout |
| `--stream` | (text format) Print each metric line as collected |
| `--format text\|ndjson\|csv` | Output format (default: `text`) |
| `-v, --verbose` | Log sample count and session count to stderr each cycle |
| `--debug` | Log detailed parse events to stderr (implies `--verbose`) |
| `--version` | Print version and exit (reads `pyproject.toml` in bare-script mode) |

## Examples

```bash
# Basic — Ctrl+C to stop, results printed on exit
uv run tcp_metrics_collector.py -a 192.168.1.100

# Collect for 30 seconds then exit
uv run tcp_metrics_collector.py -a 192.168.1.100 --duration 30

# Cap samples and save as NDJSON
uv run tcp_metrics_collector.py -a 192.168.1.100 \
  --max-samples 1000 --format ndjson --output metrics.ndjson

# CSV to file
uv run tcp_metrics_collector.py -a 192.168.1.100 \
  --duration 60 --format csv --output metrics.csv

# Stream NDJSON to stdout (pipe-friendly)
uv run tcp_metrics_collector.py -a 192.168.1.100 --format ndjson | jq .

# Verbose — shows sample/session counts on stderr each cycle
uv run tcp_metrics_collector.py -a 192.168.1.100 --verbose

# Debug — shows every line parsed/skipped (useful when no output appears)
uv run tcp_metrics_collector.py -a 192.168.1.100 --debug
```

## Output Formats

### `text` (default) — human-readable session blocks on exit

```
======== START TCP SESSION (192.168.1.50:45231 <--> 192.168.1.100:80) ========
1746518400.100 - "cwnd":10, "mss":1460, "ssthresh":2147483647, "unacked":0, "rtt_ms":1.234, "rttvar_ms":0.617, "retrans_cur":0, "retrans_total":0, "send":"84.7Mbps"
1746518400.200 - "cwnd":12, "mss":1460, "ssthresh":2147483647, "unacked":0, "rtt_ms":1.198, "rttvar_ms":0.601, "retrans_cur":0, "retrans_total":0, "send":"84.7Mbps"
======== END TCP SESSION (...) ========
```

With `--stream`: one line per sample as collected, no session blocks.

### `ndjson` — one valid JSON object per line (always streams)

```json
{"ts": 1746518400.1, "src": "192.168.1.50:45231", "dst": "192.168.1.100:80", "cwnd": 10, "mss": 1460, "ssthresh": 2147483647, "unacked": 0, "rtt_ms": 1.234, "rttvar_ms": 0.617, "retrans_cur": 0, "retrans_total": 0, "send": "84.7Mbps"}
```

All integer/float fields are native JSON numbers. Absent fields are `null`, not `0`.

### `csv` — header + one row per sample (always streams)

```
ts,src,dst,cwnd,mss,ssthresh,unacked,rtt_ms,rttvar_ms,retrans_cur,retrans_total,send
1746518400.100,192.168.1.50:45231,192.168.1.100:80,10,1460,2147483647,0,1.234,0.617,0,0,84.7Mbps
```

Timestamps are real wall-clock `time.time()` values (Unix epoch, seconds).

## Collected Metrics

| Field | Type | Description |
|-------|------|-------------|
| `cwnd` | `int \| null` | Congestion window (segments) |
| `mss` | `int \| null` | Maximum segment size (bytes) |
| `ssthresh` | `int \| null` | Slow-start threshold |
| `unacked` | `int \| null` | Unacknowledged segments |
| `rtt_ms` | `float \| null` | Round-trip time (ms) |
| `rttvar_ms` | `float \| null` | RTT variance (ms) — second component of `rtt:X/Y` from ss |
| `retrans_cur` | `int \| null` | Current retransmissions |
| `retrans_total` | `int \| null` | Total retransmissions |
| `send` | `string \| null` | Estimated send rate (e.g. `"84.7Mbps"`) |

`null` means the field was absent in the `ss` output for that sample (not zero).

## Diagnostics

**No output after Ctrl+C?** Use `--debug` to see which lines ss returns and why sessions are skipped:

```bash
uv run tcp_metrics_collector.py -a 192.168.1.100 --debug
```

Common causes: no active TCP connections to target IP; target unreachable; ss output format differs from expected (check `ss -H -n -i dst 192.168.1.100` manually).

**Sampling interval accuracy:** Uses monotonic tick scheduler — actual interval is 100ms minus ss execution time. If ss takes longer than 100ms, next sample fires immediately. Use `--verbose` to observe sample cadence.

## Known Limitations

- **IPv4 only** — `is_valid_ipv4()` rejects IPv6 at input; `RE_TCP_SESSION_LOOKUP` only matches dotted-decimal addresses
- `CLOSING` state sessions skipped
