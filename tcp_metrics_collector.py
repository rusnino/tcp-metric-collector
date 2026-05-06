# /// script
# requires-python = ">=3.10"
# dependencies = ["click>=8.1.8"]
# ///
#
# TCP Metrics Collector
#

import csv
import ipaddress
import json
import re
import signal
import subprocess
import sys
from collections import defaultdict
from time import monotonic, sleep, time
from typing import TextIO

import click

DEFAULT_SLEEP: float = 0.1
SESSION_SEP = "|"

# Typed metric fields in output. rtt split into rtt_ms/rttvar_ms; retrans split
# into retrans_cur/retrans_total; send kept as string (unit varies: Mbps/Kbps).
CSV_FIELDS = (
    "ts", "src", "dst",
    "cwnd", "mss", "ssthresh", "unacked",
    "rtt_ms", "rttvar_ms",
    "retrans_cur", "retrans_total",
    "send",
)

RE_TCP_SESSION_LOOKUP = r"tcp\s+\S+\s+\d+\s+\d+\s+(\d+\.\d+\.\d+\.\d+\:\S+)\s+(\d+\.\d+\.\d+\.\d+\:\S+)$"
RE_TCP_METRIC_PARAM_LOOKUP = r"\b(cwnd|rtt|mss|ssthresh|send|unacked|retrans)\:(\S+)"

_verbose = False
_debug = False


def _log(msg: str) -> None:
    if _verbose or _debug:
        click.echo(f"[INFO] {msg}", file=sys.stderr)


def _dbg(msg: str) -> None:
    if _debug:
        click.echo(f"[DEBUG] {msg}", file=sys.stderr)


def is_valid_ipv4(ip: str) -> bool:
    try:
        return isinstance(ipaddress.ip_address(ip), ipaddress.IPv4Address)
    except ValueError:
        return False


def _parse_session_line(line: str) -> tuple[str, str] | None:
    if "tcp " not in line or "CLOSING" in line:
        _dbg(f"session line skipped: {line.strip()!r}")
        return None
    lookup = re.findall(RE_TCP_SESSION_LOOKUP, line.strip())
    if not lookup:
        _dbg(f"session regex no match: {line.strip()!r}")
        return None
    return lookup[0][0], lookup[0][1]


def _parse_metrics_line(line: str) -> dict[str, int | float | str | None] | None:
    """Parse ss metrics line into typed fields. Returns None if not a metrics line.

    Integer fields: cwnd, mss, ssthresh, unacked, retrans_cur, retrans_total
    Float fields:   rtt_ms, rttvar_ms
    String fields:  send  (unit varies: e.g. "84.7Mbps")
    None:           field absent in ss output
    """
    if "wscale" not in line:
        return None

    raw: dict[str, str] = {}
    normalized = re.sub(r"\bsend ", "send:", line, count=1) if "send " in line else line
    for match in re.finditer(RE_TCP_METRIC_PARAM_LOOKUP, normalized):
        raw[match.group(1)] = match.group(2)

    def _int(key: str) -> int | None:
        v = raw.get(key)
        try:
            return int(v) if v is not None else None
        except ValueError:
            return None

    rtt_ms: float | None = None
    rttvar_ms: float | None = None
    if "rtt" in raw:
        parts = raw["rtt"].split("/")
        try:
            rtt_ms = float(parts[0])
            rttvar_ms = float(parts[1]) if len(parts) > 1 else None
        except ValueError:
            pass

    retrans_cur: int | None = None
    retrans_total: int | None = None
    if "retrans" in raw:
        parts = raw["retrans"].split("/")
        try:
            retrans_cur = int(parts[0])
            retrans_total = int(parts[1]) if len(parts) > 1 else None
        except ValueError:
            pass

    result: dict[str, int | float | str | None] = {
        "cwnd":          _int("cwnd"),
        "mss":           _int("mss"),
        "ssthresh":      _int("ssthresh"),
        "unacked":       _int("unacked"),
        "rtt_ms":        rtt_ms,
        "rttvar_ms":     rttvar_ms,
        "retrans_cur":   retrans_cur,
        "retrans_total": retrans_total,
        "send":          raw.get("send"),
    }
    _dbg(f"parsed metrics: {result}")
    return result


def _collect_snapshot(ip: str, shutdown_ref: list[bool]) -> list[str] | None:
    """Run ss and return filtered lines. Returns None if interrupted by signal."""
    result = subprocess.run(
        ["ss", "-i", "dst", ip],
        capture_output=True,
        text=True,
    )

    # Ctrl+C during subprocess sets shutdown flag AND causes ss to exit non-zero.
    # Check flag first so we don't emit a spurious error on clean shutdown.
    if shutdown_ref[0]:
        _dbg("snapshot skipped — shutdown signalled during ss run")
        return None

    if result.returncode != 0:
        click.echo(
            f"ERROR: ss exited with code {result.returncode}"
            + (f": {result.stderr.strip()}" if result.stderr.strip() else ""),
            err=True,
        )
        sys.exit(1)

    raw = result.stdout.splitlines()
    _dbg(f"ss returned {len(raw)} lines")

    kept: list[str] = []
    for i, line in enumerate(raw):
        if ip in line:
            kept.append(line)
        elif "wscale" in line and i > 0 and ip in raw[i - 1]:
            kept.append(line)

    _dbg(f"kept {len(kept)} lines after filter")
    return kept


def _emit_record(
    ts: float,
    src: str,
    dst: str,
    metrics: dict[str, int | float | str | None],
    fmt: str,
    out: TextIO,
    csv_writer: csv.DictWriter | None,
) -> None:
    if fmt == "ndjson":
        obj = {"ts": round(ts, 3), "src": src, "dst": dst, **metrics}
        out.write(json.dumps(obj) + "\n")
        out.flush()
    elif fmt == "csv":
        if csv_writer is None:
            raise RuntimeError("csv_writer required for csv format")
        csv_writer.writerow({"ts": f"{ts:.3f}", "src": src, "dst": dst, **metrics})
        out.flush()
    else:
        label = f"{src} <--> {dst}"
        fields = ", ".join(
            f'"{k}":{json.dumps(v)}'
            for k, v in metrics.items()
        )
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
    found = 0
    for i, line in enumerate(lines):
        session = _parse_session_line(line)
        if session is None:
            continue

        next_line = lines[i + 1] if i + 1 < len(lines) else ""
        metrics = _parse_metrics_line(next_line)
        if metrics is None:
            _dbg(f"no metrics line after session {session}")
            continue

        src, dst = session
        found += 1

        if stream or fmt in ("ndjson", "csv"):
            _emit_record(snapshot_time, src, dst, metrics, fmt, out, csv_writer)
        else:
            key = f"{src}{SESSION_SEP}{dst}"
            sessions[key].append((snapshot_time, metrics))

    _dbg(f"snapshot parsed: {found} session(s) with metrics")


def _print_sessions(
    sessions: dict[str, list[tuple[float, dict]]],
    out: TextIO,
) -> None:
    for key, records in sessions.items():
        if not records:
            continue
        src, dst = key.split(SESSION_SEP, 1)
        label = f"{src} <--> {dst}"
        out.write("\n")
        out.write(f"======== START TCP SESSION ({label}) ========\n")
        for ts, metrics in records:
            fields = ", ".join(f'"{k}":{json.dumps(v)}' for k, v in metrics.items())
            out.write(f"{ts:.3f} - {fields}\n")
        out.write(f"======== END TCP SESSION ({label}) ========\n")
        out.write("\n")
    out.flush()


@click.command()
@click.version_option(version="0.3.0", prog_name="tcp-metric-collector")
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
@click.option("--verbose", "-v", is_flag=True, default=False,
              help="Log collection progress to stderr (sample count, session count).")
@click.option("--debug", is_flag=True, default=False,
              help="Log detailed parse events to stderr (implies --verbose).")
def run(ip: str, duration: float | None, max_samples: int | None,
        output: str | None, stream: bool, fmt: str,
        verbose: bool, debug: bool) -> None:
    """Collect TCP metrics for all sessions to a destination IPv4 address."""
    global _verbose, _debug
    _verbose = verbose or debug
    _debug = debug

    if not is_valid_ipv4(ip):
        raise click.BadParameter(f"'{ip}' is not a valid IPv4 address.", param_hint="'-a'")

    sessions: dict[str, list[tuple[float, dict]]] = defaultdict(list)
    # Use a mutable container so signal_handler and _collect_snapshot share state
    # without needing a nonlocal closure per call.
    shutdown_ref: list[bool] = [False]
    sample_count = 0
    start_time = time()
    start_mono = monotonic()

    def signal_handler(*_) -> None:
        shutdown_ref[0] = True

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
            f"INFO: Collecting TCP metrics for {ip} every {DEFAULT_SLEEP}s."
            " Press Ctrl+C to stop.",
            file=sys.stderr,
        )
        _log(f"format={fmt} stream={stream} duration={duration} max_samples={max_samples}"
             f" output={output!r}")

        next_tick = monotonic()
        while not shutdown_ref[0]:
            next_tick += DEFAULT_SLEEP

            lines = _collect_snapshot(ip, shutdown_ref)
            if lines is None:
                break

            _parse_snapshot(lines, time(), sessions, fmt, stream, out, csv_writer)
            sample_count += 1

            total_records = sum(len(v) for v in sessions.values())
            _log(f"sample {sample_count}: {len(lines)} lines, "
                 f"{len(sessions)} session(s), {total_records} total records")

            if max_samples is not None and sample_count >= max_samples:
                _log(f"--max-samples {max_samples} reached, stopping")
                break
            if duration is not None and monotonic() - start_mono >= duration:
                _log(f"--duration {duration}s reached, stopping")
                break

            wait = next_tick - monotonic()
            if wait > 0:
                sleep(wait)

        _log(f"collection finished: {sample_count} samples, {len(sessions)} session(s)")

        if fmt == "text" and not stream:
            _print_sessions(sessions, out)

    finally:
        if output and not out.closed:
            out.close()

    sys.exit(0)


if __name__ == "__main__":
    run()
