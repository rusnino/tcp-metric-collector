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

# Or install as CLI tool
uvx --from . tcp-metric-collector -a <destination_ip>
```

### Plain Python

```bash
python3 tcp_metrics_collector.py -a <destination_ip>
```

Press `Ctrl+C` or send `SIGTERM` to stop collection and print results.

## Options

| Option | Description |
|--------|-------------|
| `-a IP` | Destination IPv4 address to monitor (required) |
| `--duration N` | Stop after N seconds |
| `--max-samples N` | Stop after N snapshots |
| `--output FILE` | Write results to file instead of stdout |
| `--stream` | (text format) Print each metric line as collected |
| `--format text\|ndjson\|csv` | Output format (default: `text`) |

## Examples

```bash
# Basic — Ctrl+C to stop, results printed on exit
uv run tcp_metrics_collector.py -a 192.168.1.100

# Collect for 30 seconds then exit
uv run tcp_metrics_collector.py -a 192.168.1.100 --duration 30

# Cap samples and save as NDJSON
uv run tcp_metrics_collector.py -a 192.168.1.100 --max-samples 1000 --format ndjson --output metrics.ndjson

# CSV to file
uv run tcp_metrics_collector.py -a 192.168.1.100 --duration 60 --format csv --output metrics.csv

# Stream NDJSON to stdout (pipe-friendly)
uv run tcp_metrics_collector.py -a 192.168.1.100 --format ndjson | jq .
```

## Output Formats

### `text` (default) — human-readable session blocks on exit

```
======== START TCP SESSION (192.168.1.50:45231 <--> 192.168.1.100:80) ========
1746518400.100 - "cwnd":10, "rtt":"1.234", "mss":1460, ...
1746518400.200 - "cwnd":12, "rtt":"1.198", ...
======== END TCP SESSION (...) ========
```

With `--stream`: one line per sample as collected, no session blocks.

### `ndjson` — one valid JSON object per line (always streams)

```json
{"ts": 1746518400.1, "src": "192.168.1.50:45231", "dst": "192.168.1.100:80", "cwnd": "10", "rtt": "1.234/1.198", "mss": "1460", "ssthresh": "2147483647", "send": "1.23Mbps", "unacked": "0", "retrans": "0/0"}
```

### `csv` — header + one row per sample (always streams)

```
ts,src,dst,cwnd,rtt,mss,ssthresh,send,unacked,retrans
1746518400.100,192.168.1.50:45231,192.168.1.100:80,10,1.234/1.198,1460,...
```

Timestamps are real wall-clock `time.time()` values (Unix epoch, seconds).

## Collected Metrics

| Metric | Description |
|--------|-------------|
| `cwnd` | Congestion window (segments) |
| `rtt` | Round-trip time (ms) |
| `mss` | Maximum segment size (bytes) |
| `ssthresh` | Slow-start threshold |
| `send` | Estimated send rate |
| `unacked` | Unacknowledged segments |
| `retrans` | Retransmission count (current/total) |

## Known Limitations

- **IPv4 only** — `is_valid_ipv4()` rejects IPv6 at input; `RE_TCP_SESSION_LOOKUP` only matches dotted-decimal addresses
- `CLOSING` state sessions skipped
