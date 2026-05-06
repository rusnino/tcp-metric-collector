# TCP Metric Collector

Collects TCP metrics per destination IP on Linux. Samples `ss` socket statistics at 100ms intervals, prints per-session metrics on exit.

## Requirements

- Linux with `iproute2` (`ss` command)
- Python 3.6+
- Run on **sender side**

## Installation

Copy `tcp_metrics_collector.py` to any directory on the Linux host.

## Usage

```bash
python3 tcp_metrics_collector.py -a <destination_ip>
```

Press `Ctrl+C` or send `SIGTERM` to stop collection and print results.

**Example:**
```bash
python3 tcp_metrics_collector.py -a 192.168.1.100
```

## Output

Per TCP session block printed on exit:

```
======== START TCP SESSION (192.168.1.50:45231 <--> 192.168.1.100:80) ========
1746518400.100 - "cwnd":10,"rtt":1.234,"mss":1460,"ssthresh":2147483647,"send":"1.23Mbps","unacked":0,"retrans":0/0
1746518400.200 - "cwnd":12,"rtt":1.198,...
======== END TCP SESSION (...) ========
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

- IPv4 only (session regex does not match IPv6)
- All collected samples held in memory; long runs on busy hosts may consume significant RAM
- `CLOSING` state sessions skipped
