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
from collections import defaultdict
from time import sleep, time
from typing import TextIO

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


def _collect_snapshot(ip: str) -> list[str]:
    """Run ss and return filtered lines preserving session+metrics adjacency."""
    result = subprocess.run(
        ["ss", "-i", "dst", ip],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        click.echo(f"Error: ss failed: {result.stderr.strip()}", err=True)
        sys.exit(1)

    raw = result.stdout.splitlines()
    kept: list[str] = []
    for i, line in enumerate(raw):
        if ip in line:
            kept.append(line)
        elif "wscale" in line and i > 0 and ip in raw[i - 1]:
            kept.append(line)
    return kept


def _parse_snapshot(
    lines: list[str],
    snapshot_time: float,
    sessions: dict[str, list[tuple[float, str]]],
    stream: bool,
    out: TextIO,
) -> None:
    """Parse one snapshot into sessions in-place. Streams to out if stream=True."""
    for i, line in enumerate(lines):
        session_key = _parse_session_line(line)
        if session_key is None:
            continue

        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        metrics = _parse_metrics_line(next_line)
        if metrics is None:
            continue

        sessions[session_key].append((snapshot_time, metrics))

        if stream:
            label = session_key.replace(SESSION_SEP, " <--> ")
            out.write(f"{snapshot_time:.3f} [{label}] {metrics[1:-1]}\n")
            out.flush()


def _print_sessions(
    sessions: dict[str, list[tuple[float, str]]],
    out: TextIO,
) -> None:
    for session_key, metrics in sessions.items():
        if not metrics:
            continue
        label = session_key.replace(SESSION_SEP, " <--> ")
        out.write("\n")
        out.write(f"======== START TCP SESSION ({label}) ========\n")
        for ts, metric in metrics:
            out.write(f"{ts:.3f} - {metric[1:-1]}\n")
        out.write(f"======== END TCP SESSION ({label}) ========\n")
        out.write("\n")
    out.flush()


@click.command()
@click.version_option(version="0.1.0", prog_name="tcp-metric-collector")
@click.option("-a", "ip", required=True, help="Destination IPv4 address to monitor")
@click.option("--duration", type=float, default=None,
              help="Stop collecting after N seconds.")
@click.option("--max-samples", type=int, default=None,
              help="Stop collecting after N snapshots.")
@click.option("--output", type=click.Path(), default=None,
              help="Write results to file instead of stdout.")
@click.option("--stream", is_flag=True, default=False,
              help="Print each metric line as collected instead of buffering.")
def run(ip: str, duration: float | None, max_samples: int | None,
        output: str | None, stream: bool) -> None:
    """Collect TCP metrics for all sessions to a destination IPv4 address."""
    if not is_valid_ipv4(ip):
        raise click.BadParameter(f"'{ip}' is not a valid IPv4 address.", param_hint="'-a'")

    sessions: dict[str, list[tuple[float, str]]] = defaultdict(list)
    shutdown = False
    sample_count = 0
    start_time = time()

    def signal_handler(*_) -> None:
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    out: TextIO = open(output, "w") if output else sys.stdout  # noqa: SIM115

    try:
        click.echo(
            f"INFO: Collecting TCP metrics every {DEFAULT_SLEEP}s. Press Ctrl+C to stop.",
            file=sys.stderr,
        )

        while not shutdown:
            lines = _collect_snapshot(ip)
            _parse_snapshot(lines, time(), sessions, stream, out)
            sample_count += 1

            if max_samples is not None and sample_count >= max_samples:
                break
            if duration is not None and time() - start_time >= duration:
                break

            sleep(DEFAULT_SLEEP)

        if not stream:
            _print_sessions(sessions, out)

    finally:
        if output and not out.closed:
            out.close()

    sys.exit(0)


if __name__ == "__main__":
    run()
