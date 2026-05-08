# TCP Metric Collector

Collects TCP metrics per destination IP address (IPv4 and IPv6) on Linux via **kernel `inet_diag` netlink** — the same source used by `ss` internally, but without subprocess overhead or text parsing fragility. Queries every 100ms and emits structured metrics immediately.

## Requirements

- Linux kernel with `inet_diag` support (standard since kernel 2.6.14)
- Python 3.10+
- `click>=8.1.8` and `pyroute2>=0.7` (managed automatically by uv)
- **`CAP_NET_ADMIN` capability or root** for netlink socket access
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

> **`--version` source:** The script resolves its version in order: (1) installed package metadata (`importlib.metadata`) — available when installed via `uvx --from .` or `pip install`; (2) `pyproject.toml` in the same directory — covers `uv run tcp_metrics_collector.py` (PEP 723 script mode) and plain `python3` invocation; (3) `"unknown"` if neither source is available.

## Options

| Option | Description |
|--------|-------------|
| `-a IP` | Destination IP address to monitor — IPv4 or IPv6 (required) |
| `--duration N` | Stop N seconds after the **first TCP session** is seen (N > 0). Waits indefinitely until traffic appears. |
| `--max-samples N` | Stop after N snapshots (N ≥ 1) |
| `--output FILE` | Write results to file instead of stdout |
| `--stream` | (text format) Print each metric line as collected |
| `--format text\|ndjson\|csv` | Output format (default: `text`) |
| `-v, --verbose` | Log sample count and session count to stderr each cycle |
| `--debug` | Log detailed parse events to stderr (implies `--verbose`) |
| `--poll-timeout N` | Max seconds to wait for kernel netlink response (default: `5.0`, min: `0.1`) |
| `--version` | Print version and exit (reads `pyproject.toml` in bare-script mode) |

## Examples

```bash
# Basic — Ctrl+C to stop, results printed on exit
uv run tcp_metrics_collector.py -a 192.168.1.100

# Collect for 30 seconds after first session appears (waits for traffic)
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

# Long-running collection — use ndjson/csv to avoid memory growth
uv run tcp_metrics_collector.py -a 192.168.1.100 --format ndjson | tee metrics.ndjson

# Increase ss timeout on slow/loaded hosts (default 5.0s)
uv run tcp_metrics_collector.py -a 192.168.1.100 --ss-timeout 15
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

Absent fields in CSV are **empty cells** (standard RFC 4180 behavior via Python `csv.DictWriter`), not the string `"null"`. A consumer expecting `null` should treat empty cell as absent.

IPv6 `src`/`dst` fields use RFC 2732 bracket notation (`[2001:db8::1]:80`) so port is always unambiguously the last component after the final `:`.

Timestamps are real wall-clock `time.time()` values (Unix epoch, seconds).

## Collected Metrics

| Field | Type | ndjson absent | csv absent | Description |
|-------|------|--------------|------------|-------------|
| `cwnd` | `int` | `null` | empty cell | Congestion window (segments) |
| `mss` | `int` | `null` | empty cell | Maximum segment size (bytes) |
| `ssthresh` | `int` | `null` | empty cell | Slow-start threshold |
| `unacked` | `int` | `null` | empty cell | Unacknowledged segments |
| `rtt_ms` | `float` | `null` | empty cell | Round-trip time (ms) |
| `rttvar_ms` | `float` | `null` | empty cell | RTT variance (ms) — second component of `rtt:X/Y` from ss |
| `retrans_cur` | `int` | `null` | empty cell | Current retransmissions |
| `retrans_total` | `int` | `null` | empty cell | Total retransmissions |
| `send` | `string` | `null` | empty cell | Estimated send rate (e.g. `"84.7Mbps"`) |

Absent = field not present in `ss` output for that sample. Distinct from `0` (present but zero).

## Diagnostics

**No output after Ctrl+C?** Use `--debug` to see which lines ss returns and why sessions are skipped:

```bash
uv run tcp_metrics_collector.py -a 192.168.1.100 --debug
```

Common causes: no active TCP connections to target IP; target unreachable; ss output format differs from expected (check `ss -H -n -i dst 192.168.1.100` manually).

**Sampling interval accuracy:** Uses monotonic tick scheduler — actual interval is 100ms minus ss execution time. If ss takes longer than 100ms, next sample fires immediately. Use `--verbose` to observe sample cadence.

## Known Limitations

- **`CAP_NET_ADMIN` required** — netlink inet_diag queries require elevated privileges; run with `sudo` or grant the capability
- `CLOSING` state sessions skipped
- **Memory growth in default text mode** — without `--stream`, every parsed record is buffered in memory until exit so session blocks can be printed. Memory grows as `sessions × samples`. For runs longer than a few minutes or with many concurrent TCP sessions, use `--format ndjson`, `--format csv`, or `--stream` instead — these emit and discard each record immediately (O(1) memory per cycle).
