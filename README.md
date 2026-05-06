# TCP Metric Collector

Collects TCP metrics per destination IPv4 address on Linux. Samples `ss` socket statistics at 100ms intervals; parses each snapshot immediately in the collection loop.

## Requirements

- Linux with `iproute2` (`ss` command)
- Python 3.7+
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
| `--stream` | Print each metric line immediately as collected |

## Examples

```bash
# Basic — Ctrl+C to stop, results printed on exit
uv run tcp_metrics_collector.py -a 192.168.1.100

# Collect for 30 seconds then exit
uv run tcp_metrics_collector.py -a 192.168.1.100 --duration 30

# Cap memory: stop after 1000 snapshots (~100s)
uv run tcp_metrics_collector.py -a 192.168.1.100 --max-samples 1000

# Save to file
uv run tcp_metrics_collector.py -a 192.168.1.100 --duration 60 --output metrics.txt

# Stream mode: print each metric as it arrives (low latency, no buffering)
uv run tcp_metrics_collector.py -a 192.168.1.100 --stream
```

## Output

**Default (buffered)** — per-session blocks printed on exit:

```
======== START TCP SESSION (192.168.1.50:45231 <--> 192.168.1.100:80) ========
1746518400.100 - "cwnd":10,"rtt":1.234,"mss":1460,"ssthresh":2147483647,"send":"1.23Mbps","unacked":0,"retrans":0/0
1746518400.200 - "cwnd":12,"rtt":1.198,...
======== END TCP SESSION (...) ========
```

**Stream mode (`--stream`)** — one line per sample as collected:

```
1746518400.100 [192.168.1.50:45231 <--> 192.168.1.100:80] "cwnd":10,"rtt":1.234,...
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
