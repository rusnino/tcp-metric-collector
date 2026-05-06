# /// script
# requires-python = ">=3.7"
# dependencies = ["click>=8.1.8"]
# ///
#
# TCP Metrics Collector
#

import ipaddress
import json
import re
import signal
import subprocess
import sys
from time import sleep, time
from typing import Dict, List, Tuple

import click

DEFAULT_SLEEP: float = 0.1
RE_TCP_SESSION_LOOKUP = r"tcp\s+\S+\s+\d+\s+\d+\s+(\d+\.\d+\.\d+\.\d+\:\S+)\s+(\d+\.\d+\.\d+\.\d+\:\S+)$"
RE_TCP_METRIC_PARAM_LOOKUP = r"(\S+)\:(\S+)"


def is_valid_ip(ip: str) -> bool:
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def print_tcp_metrics(tcp_metrics: List[Tuple[float, str]]) -> None:
    sessions: Dict[str, List[Tuple[float, str]]] = {}
    curr_session: str = ""

    def _parse_tcp_metrics(metrics: str) -> str:
        parsed_metrics = {
            "cwnd": 0,
            "rtt": 0,
            "mss": 0,
            "ssthresh": 0,
            "send": 0,
            "unacked": 0,
            "retrans": 0,
        }

        for param in metrics.split(" "):
            lookup_param = re.findall(RE_TCP_METRIC_PARAM_LOOKUP, param)
            if not lookup_param or lookup_param[0][0].lower() not in parsed_metrics:
                continue
            parsed_metrics[lookup_param[0][0]] = lookup_param[0][1]

        return json.dumps(parsed_metrics)

    for snapshot_time, tcp_session in tcp_metrics:
        for line in tcp_session.splitlines():
            if "tcp " in line:
                if "CLOSING" in line:
                    continue

                lookup_tcp_session = re.findall(RE_TCP_SESSION_LOOKUP, line.strip())
                if not lookup_tcp_session:
                    continue

                curr_session = f"{lookup_tcp_session[0][0]}_{lookup_tcp_session[0][1]}"
                if curr_session not in sessions:
                    sessions[curr_session] = []

            if "wscale" in line and curr_session:
                line = line.replace("send ", "send:") if "send " in line else line
                sessions[curr_session].append((snapshot_time, _parse_tcp_metrics(line)))

    for session_key, metrics in sessions.items():
        if not metrics:
            continue

        label = session_key.replace("_", " <--> ")
        click.echo()
        click.echo(f"======== START TCP SESSION ({label}) ========")
        for ts, metric in metrics:
            inner = metric[1:-1]
            click.echo(f"{ts:.3f} - {inner}")
        click.echo(f"======== END TCP SESSION ({label}) ========")
        click.echo()


@click.command()
@click.version_option(version="0.1.0", prog_name="tcp-metric-collector")
@click.option("-a", "ip", required=True, help="Destination IP address to monitor")
def run(ip: str) -> None:
    """Collect TCP metrics for all sessions to a destination IP address."""
    if not is_valid_ip(ip):
        raise click.BadParameter(f"'{ip}' is not a valid IP address.", param_hint="'-a'")

    tcp_metrics: List[Tuple[float, str]] = []

    def signal_handler(*_) -> None:
        print_tcp_metrics(tcp_metrics)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    click.echo(f"INFO: Collecting TCP metrics every {DEFAULT_SLEEP}s. Press Ctrl+C to stop.")

    while True:
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
        kept: List[str] = []
        for i, line in enumerate(raw_lines):
            if ip in line:
                kept.append(line)
            elif "wscale" in line and i > 0 and ip in raw_lines[i - 1]:
                kept.append(line)

        tcp_metrics.append((time(), "\n".join(kept)))
        sleep(DEFAULT_SLEEP)


if __name__ == "__main__":
    run()
