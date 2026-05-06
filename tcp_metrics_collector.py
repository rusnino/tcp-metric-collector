# /// script
# requires-python = ">=3.7"
# dependencies = ["click>=8.1.8"]
# ///
#
# TCP Metrics Collector
#

from __future__ import annotations

import csv
import io
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
METRIC_KEYS = ("cwnd", "rtt", "mss", "ssthresh", "send", "unacked", "retrans")
CSV_FIELDS = ("ts", "src", "dst") + METRIC_KEYS

RE_TCP_SESSION_LOOKUP = r"tcp\s+\S+\s+\d+\s+\d+\s+(\d+\.\d+\.\d+\.\d+\:\S+)\s+(\d+\.\d+\.\d+\.\d+\:\S+)$"
RE_TCP_METRIC_PARAM_LOOKUP = r"\b(cwnd|rtt|mss|ssthresh|send|unacked|retrans)\:(\S+)"


def is_valid_ipv4(ip: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address)
    except ValueError:
        return False


def _parse_session_line(line: str) -> tuple[str, str] | None:
    """Return (src, dst) if line is a valid non-CLOSING TCP session, else None."""
    if "tcp " not in line or "CLOSING" in line:
        return None
    lookup = re.findall(RE_TCP_SESSION_LOOKUP, line.strip())
    if not lookup:
        return None
    return lookup[0][0], lookup[0][1]


def _parse_metrics_line(line: str) -> dict[str, int | str] | None:
    """Return parsed metrics dict if line is a wscale metrics line, else None."""
    if "wscale" not in line:
        return None
    parsed: dict[str, int | str] = {k: 0 for k in METRIC_KEYS}
    normalized = line.replace("send ", "send:") if "send " in line else line
    for match in re.finditer(RE_TCP_METRIC_PARAM_LOOKUP, normalized):
        parsed[match.group(1)] = match.group(2)
    return parsed


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


def _emit_record(
    ts: float,
    src: str,
    dst: str,
    metrics: dict[str, int | str],
    fmt: str,
    out: TextIO,
    csv_writer: csv.DictWriter | None,
) -> None:
    """Write one record to out in the requested format."""
    if fmt == "ndjson":
        obj = {"ts": round(ts, 3), "src": src, "dst": dst, **metrics}
        out.write(json.dumps(obj) + "\n")
        out.flush()
    elif fmt == "csv":
        assert csv_writer is not None
        csv_writer.writerow({"ts": f"{ts:.3f}", "src": src, "dst": dst, **metrics})
        out.flush()
    else:
        label = f"{src} <--> {dst}"
        fields = ", ".join(f'"{k}":{v!r}' if isinstance(v, str) else f'"{k}":{v}' for k, v in metrics.items())
        out.write(f"{ts:.3f} [{label}] {fields}\n")
        out.flush()


def _parse_snapshot(
    lines: list[str],
    snapshot_time: float,
    sessions: dict[str, list[tuple[float, dict]]],
    fmt: str,
    stream: bool,
    out: TextIO,
    csv_writer: csv.DictWriter | None,
) -> None:
    """Parse one snapshot into sessions in-place. Emits immediately for ndjson/csv or --stream."""
    for i, line in enumerate(lines):
        session = _parse_session_line(line)
        if session is None:
            continue

        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        metrics = _parse_metrics_line(next_line)
        if metrics is None:
            continue

        src, dst = session
        key = f"{src}{SESSION_SEP}{dst}"
        sessions[key].append((snapshot_time, metrics))

        if stream or fmt in ("ndjson", "csv"):
            _emit_record(snapshot_time, src, dst, metrics, fmt, out, csv_writer)


def _print_sessions(
    sessions: dict[str, list[tuple[float, dict]]],
    fmt: str,
    out: TextIO,
    csv_writer: csv.DictWriter | None,
) -> None:
    """Print buffered results. Only used for --format text without --stream."""
    for key, records in sessions.items():
        if not records:
            continue
        src, dst = key.split(SESSION_SEP, 1)
        label = f"{src} <--> {dst}"
        out.write("\n")
        out.write(f"======== START TCP SESSION ({label}) ========\n")
        for ts, metrics in records:
            fields = ", ".join(f'"{k}":{v!r}' if isinstance(v, str) else f'"{k}":{v}' for k, v in metrics.items())
            out.write(f"{ts:.3f} - {fields}\n")
        out.write(f"======== END TCP SESSION ({label}) ========\n")
        out.write("\n")
    out.flush()


@click.command()
@click.version_option(version="0.2.0", prog_name="tcp-metric-collector")
@click.option("-a", "ip", required=True, help="Destination IPv4 address to monitor")
@click.option("--duration", type=float, default=None,
              help="Stop collecting after N seconds.")
@click.option("--max-samples", type=int, default=None,
              help="Stop collecting after N snapshots.")
@click.option("--output", type=click.Path(), default=None,
              help="Write results to file instead of stdout.")
@click.option("--stream", is_flag=True, default=False,
              help="(text format) Print each metric line as collected instead of buffering.")
@click.option("--format", "fmt", default="text",
              type=click.Choice(["text", "ndjson", "csv"], case_sensitive=False),
              help="Output format. ndjson and csv always stream per-record.")
def run(ip: str, duration: float | None, max_samples: int | None,
        output: str | None, stream: bool, fmt: str) -> None:
    """Collect TCP metrics for all sessions to a destination IPv4 address."""
    if not is_valid_ipv4(ip):
        raise click.BadParameter(f"'{ip}' is not a valid IPv4 address.", param_hint="'-a'")

    sessions: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    shutdown = False
    sample_count = 0
    start_time = time()

    def signal_handler(*_) -> None:
        nonlocal shutdown
        shutdown = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    out: TextIO = open(output, "w") if output else sys.stdout  # noqa: SIM115

    csv_writer: csv.DictWriter | None = None
    if fmt == "csv":
        csv_writer = csv.DictWriter(out, fieldnames=list(CSV_FIELDS))
        csv_writer.writeheader()
        out.flush()

    try:
        click.echo(
            f"INFO: Collecting TCP metrics every {DEFAULT_SLEEP}s. Press Ctrl+C to stop.",
            file=sys.stderr,
        )

        while not shutdown:
            lines = _collect_snapshot(ip)
            _parse_snapshot(lines, time(), sessions, fmt, stream, out, csv_writer)
            sample_count += 1

            if max_samples is not None and sample_count >= max_samples:
                break
            if duration is not None and time() - start_time >= duration:
                break

            sleep(DEFAULT_SLEEP)

        if fmt == "text" and not stream:
            _print_sessions(sessions, fmt, out, csv_writer)

    finally:
        if output and not out.closed:
            out.close()

    sys.exit(0)


if __name__ == "__main__":
    run()
