# /// script
# requires-python = ">=3.7"
# dependencies = ["click>=8.1.8"]
# ///
#
# TCP Metrics Collector
#

from __future__ import annotations

import ipaddress
import json
import re
import signal
import subprocess
import sys
from time import sleep, time

import click

DEFAULT_SLEEP: float = 0.1
SESSION_SEP = "|"

RE_TCP_SESSION_LOOKUP = r"tcp\s+\S+\s+\d+\s+\d+\s+(\d+\.\d+\.\d+\.\d+\:\S+)\s+(\d+\.\d+\.\d+\.\d+\:\S+)$"
RE_TCP_METRIC_PARAM_LOOKUP = r"\b(cwnd|rtt|mss|ssthresh|send|unacked|retrans)\:(\S+)"


def is_valid_ipv4(ip: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address)
    except ValueError:
        return False


def _parse_session_line(line: str) -> str | None:
    """Return session key if line is a valid non-CLOSING TCP session, else None."""
    if "tcp " not in line or "CLOSING" in line:
        return None
    lookup = re.findall(RE_TCP_SESSION_LOOKUP, line.strip())
    if not lookup:
        return None
    return f"{lookup[0][0]}{SESSION_SEP}{lookup[0][1]}"


def _parse_metrics_line(line: str) -> str | None:
    """Return JSON metrics string if line contains wscale (ss metrics line), else None."""
    if "wscale" not in line:
        return None
    parsed: dict[str, int | str] = {
        "cwnd": 0, "rtt": 0, "mss": 0, "ssthresh": 0,
        "send": 0, "unacked": 0, "retrans": 0,
    }
    normalized = line.replace("send ", "send:") if "send " in line else line
    for match in re.finditer(RE_TCP_METRIC_PARAM_LOOKUP, normalized):
        parsed[match.group(1)] = match.group(2)
    return json.dumps(parsed)


def print_tcp_metrics(tcp_metrics: list[tuple[float, str]]) -> None:
    sessions: dict[str, list[tuple[float, str]]] = {}

    for snapshot_time, snapshot in tcp_metrics:
        lines = snapshot.splitlines()
        for i, line in enumerate(lines):
            session_key = _parse_session_line(line)
            if session_key is None:
                continue

            next_line = lines[i + 1] if i + 1 < len(lines) else ""
            metrics = _parse_metrics_line(next_line)
            if metrics is None:
                continue

            if session_key not in sessions:
                sessions[session_key] = []
            sessions[session_key].append((snapshot_time, metrics))

    for session_key, metrics in sessions.items():
        if not metrics:
            continue

        label = session_key.replace(SESSION_SEP, " <--> ")
        click.echo()
        click.echo(f"======== START TCP SESSION ({label}) ========")
        for ts, metric in metrics:
            click.echo(f"{ts:.3f} - {metric[1:-1]}")
        click.echo(f"======== END TCP SESSION ({label}) ========")
        click.echo()


@click.command()
@click.version_option(version="0.1.0", prog_name="tcp-metric-collector")
@click.option("-a", "ip", required=True, help="Destination IPv4 address to monitor")
def run(ip: str) -> None:
    """Collect TCP metrics for all sessions to a destination IPv4 address."""
    if not is_valid_ipv4(ip):
        raise click.BadParameter(f"'{ip}' is not a valid IPv4 address.", param_hint="'-a'")

    tcp_metrics: list[tuple[float, str]] = []
    shutdown = False

    def signal_handler(*_) -> None:
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    click.echo(f"INFO: Collecting TCP metrics every {DEFAULT_SLEEP}s. Press Ctrl+C to stop.")

    while not shutdown:
        result = subprocess.run(
            ["ss", "-i", "dst", ip],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            click.echo(f"Error: ss failed: {result.stderr.strip()}", err=True)
            sys.exit(1)

        # Preserve adjacency: keep session line + immediately following metrics line
        raw_lines = result.stdout.splitlines()
        kept: list[str] = []
        for i, line in enumerate(raw_lines):
            if ip in line:
                kept.append(line)
            elif "wscale" in line and i > 0 and ip in raw_lines[i - 1]:
                kept.append(line)

        tcp_metrics.append((time(), "\n".join(kept)))
        sleep(DEFAULT_SLEEP)

    print_tcp_metrics(tcp_metrics)
    sys.exit(0)


if __name__ == "__main__":
    run()
