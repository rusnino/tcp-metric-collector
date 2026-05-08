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
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from time import monotonic, sleep, time
from typing import TextIO

import click

try:
    _VERSION = _pkg_version("tcp-metric-collector")
except PackageNotFoundError:
    # Bare-script mode (python3 tcp_metrics_collector.py): package not installed.
    # Fall back to reading version from pyproject.toml in the same directory.
    # Uses regex to avoid requiring tomllib/tomli at runtime.
    try:
        import pathlib as _pathlib
        _pyproject = _pathlib.Path(__file__).with_name("pyproject.toml")
        _m = re.search(r'^\[project\].*?^version\s*=\s*"([^"]+)"',
                       _pyproject.read_text(), re.MULTILINE | re.DOTALL)
        _VERSION = _m.group(1) if _m else "unknown"
        del _pathlib, _pyproject, _m
    except Exception:
        _VERSION = "unknown"

DEFAULT_SLEEP: float = 0.1
SS_TIMEOUT: float = 5.0   # max seconds to wait for ss before raising an error
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

RE_TCP_SESSION_LOOKUP = r"tcp\s+\S+\s+\d+\s+\d+\s+(\d+\.\d+\.\d+\.\d+:\S+)\s+(\d+\.\d+\.\d+\.\d+:\S+)$"
RE_TCP_METRIC_PARAM_LOOKUP = r"\b(cwnd|rtt|mss|ssthresh|send|unacked|retrans):(\S+)"
# Compiled fast-check: is a line a ss metrics line at all?
# Matches standard key:value tokens OR "send VALUE" (space-separated in ss output).
# Used only by _collect_snapshot() as a pre-filter (not by _parse_metrics_line).
# If a new metric form is added to _parse_metrics_line, add a matching
# alternation here so the line reaches the parser.
_RE_HAS_METRIC = re.compile(RE_TCP_METRIC_PARAM_LOOKUP + r"|\bsend \S")

_verbose = False
_debug = False


def _log(msg: str) -> None:
    if _verbose:
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
        _dbg(f"session line not TCP/CLOSING: {line.strip()!r}")
        return None
    m = re.search(RE_TCP_SESSION_LOOKUP, line.strip())
    if not m:
        _dbg(f"session regex no match: {line.strip()!r}")
        return None
    return m.group(1), m.group(2)


def _parse_metrics_line(line: str) -> dict[str, int | float | str | None] | None:
    """Parse ss metrics line into typed fields. Returns None if not a metrics line.

    Integer fields: cwnd, mss, ssthresh, unacked, retrans_cur, retrans_total
    Float fields:   rtt_ms, rttvar_ms
    String fields:  send  (unit varies: e.g. "84.7Mbps")
    None:           field absent in ss output
    """
    normalized = re.sub(r"\bsend ", "send:", line, count=1) if "send " in line else line
    matches = list(re.finditer(RE_TCP_METRIC_PARAM_LOOKUP, normalized))
    if not matches:
        return None

    raw: dict[str, str] = {}
    for match in matches:
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
    try:
        result = subprocess.run(
            ["ss", "-H", "-n", "-i", "dst", ip],
            capture_output=True,
            text=True,
            timeout=SS_TIMEOUT,
        )
    except FileNotFoundError:
        raise click.ClickException("ss command not found; install iproute2")
    except subprocess.TimeoutExpired:
        if shutdown_ref[0]:
            # Ctrl+C arrived while ss was hung — treat as clean shutdown so
            # buffered text results are still printed before exit.
            return None
        raise click.ClickException(
            f"ss did not respond within {SS_TIMEOUT}s; "
            "possible kernel/netlink hang or overloaded host"
        )

    # Ctrl+C during subprocess sets shutdown flag AND causes ss to exit non-zero.
    # Check flag first so we don't emit a spurious error on clean shutdown.
    if shutdown_ref[0]:
        _dbg("snapshot skipped — shutdown signalled during ss run")
        return None

    if result.returncode != 0:
        msg = f"ss exited with code {result.returncode}"
        if result.stderr.strip():
            msg += f": {result.stderr.strip()}"
        raise click.ClickException(msg)

    raw = result.stdout.splitlines()
    _dbg(f"ss returned {len(raw)} lines")

    kept: list[str] = []
    for i, line in enumerate(raw):
        if ip in line:
            kept.append(line)
        elif _RE_HAS_METRIC.search(line) and i > 0 and ip in raw[i - 1]:
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
        out.flush()  # text --stream must flush per-record for pipe/file consumers


def _parse_snapshot(
    lines: list[str],
    snapshot_time: float,
    sessions: dict[str, list[tuple[float, dict[str, int | float | str | None]]]],
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
    sessions: dict[str, list[tuple[float, dict[str, int | float | str | None]]]],
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
@click.version_option(version=_VERSION, prog_name="tcp-metric-collector")
@click.option("-a", "ip", required=True, help="Destination IPv4 address to monitor")
@click.option("--duration", type=click.FloatRange(min=0.001), default=None,
              help="Stop collecting after N seconds (must be > 0).")
@click.option("--max-samples", type=click.IntRange(min=1), default=None,
              help="Stop collecting after N snapshots (must be >= 1).")
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
    global _verbose, _debug  # reset each invocation — safe for tests calling run() multiple times
    _verbose = verbose or debug
    _debug = debug

    if not is_valid_ipv4(ip):
        raise click.BadParameter(f"'{ip}' is not a valid IPv4 address.", param_hint="'-a'")

    sessions: dict[str, list[tuple[float, dict[str, int | float | str | None]]]] = defaultdict(list)
    # Use a mutable container so signal_handler and _collect_snapshot share state
    # without needing a nonlocal closure per call.
    shutdown_ref: list[bool] = [False]
    sample_count = 0
    start_mono = monotonic()

    def signal_handler(*_) -> None:
        shutdown_ref[0] = True

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        out: TextIO = open(output, "w") if output else sys.stdout  # noqa: SIM115
    except OSError as exc:
        raise click.ClickException(f"Cannot open output file '{output}': {exc.strerror}")

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

            snapshot_time = time()  # capture before ss runs — timestamp reflects sample start
            lines = _collect_snapshot(ip, shutdown_ref)
            if lines is None:
                break

            _parse_snapshot(lines, snapshot_time, sessions, fmt, stream, out, csv_writer)
            sample_count += 1

            if _verbose:
                if fmt == "text" and not stream:
                    total_records = sum(len(v) for v in sessions.values())
                    _log(f"sample {sample_count}: {len(lines)} lines, "
                         f"{len(sessions)} session(s), {total_records} buffered records")
                else:
                    _log(f"sample {sample_count}: {len(lines)} lines emitted")

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


if __name__ == "__main__":
    run()
