# /// script
# requires-python = ">=3.10"
# dependencies = ["click>=8.1.8", "pyroute2>=0.7"]
# ///
#
# TCP Metrics Collector
#

import csv
import ipaddress
import json
import re
import signal
import sys
from collections import defaultdict
from importlib.metadata import PackageNotFoundError, version as _pkg_version
from socket import AF_INET, AF_INET6
from time import monotonic, sleep, time
from typing import TextIO

import click
from pyroute2 import DiagSocket
from pyroute2.netlink.diag import SS_CONN

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
POLL_TIMEOUT: float = 5.0   # netlink socket timeout per query
SESSION_SEP = "|"

# inet_diag extension bitmask: request INET_DIAG_INFO (tcp_info struct)
_INET_DIAG_INFO_EXT: int = 1 << 1  # INET_DIAG_INFO = 1

# TCP state value for ESTABLISHED (from linux/tcp.h TCP_ESTABLISHED = 1)
_TCP_ESTABLISHED: int = 1

# Typed metric fields in output. rtt split into rtt_ms/rttvar_ms; retrans split
# into retrans_cur/retrans_total; send is a derived rate string.
CSV_FIELDS = (
    "ts", "src", "dst",
    "cwnd", "mss", "ssthresh", "unacked",
    "rtt_ms", "rttvar_ms",
    "retrans_cur", "retrans_total",
    "send",
)

_verbose = False
_debug = False

# Metric record type alias
MetricDict = dict[str, int | float | str | None]
SnapshotRecord = tuple[str, str, MetricDict]  # (src, dst, metrics)


def _log(msg: str) -> None:
    if _verbose:
        click.echo(f"[INFO] {msg}", file=sys.stderr)


def _dbg(msg: str) -> None:
    if _debug:
        click.echo(f"[DEBUG] {msg}", file=sys.stderr)


def is_valid_ip(ip: str) -> bool:
    """Accept both IPv4 and IPv6 addresses."""
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False


def _format_rate(bps: float) -> str:
    """Format bytes-per-second as human-readable rate string matching ss output style."""
    if bps >= 1e9:
        return f"{bps / 1e9:.3g}Gbps"
    if bps >= 1e6:
        return f"{bps / 1e6:.3g}Mbps"
    if bps >= 1e3:
        return f"{bps / 1e3:.3g}Kbps"
    return f"{bps:.3g}bps"


def _extract_metrics(tcp_info: dict) -> MetricDict:
    """Map tcp_info fields to the output metric schema.

    All integer/float fields come directly from the kernel tcp_info struct.
    send is derived: cwnd * mss * 1e6 / rtt_us (approximates ss send rate display).
    """
    rtt_us: int = tcp_info.get("tcpi_rtt", 0) or 0
    rttvar_us: int = tcp_info.get("tcpi_rttvar", 0) or 0
    cwnd: int | None = tcp_info.get("tcpi_snd_cwnd")
    mss: int | None = tcp_info.get("tcpi_snd_mss")
    ssthresh: int | None = tcp_info.get("tcpi_snd_ssthresh")
    unacked: int | None = tcp_info.get("tcpi_unacked")

    rtt_ms: float | None = rtt_us / 1000.0 if rtt_us else None
    rttvar_ms: float | None = rttvar_us / 1000.0 if rttvar_us else None

    send: str | None = None
    if cwnd and mss and rtt_us:
        send = _format_rate(cwnd * mss * 1_000_000 / rtt_us)

    return {
        "cwnd":          cwnd,
        "mss":           mss,
        "ssthresh":      ssthresh,
        "unacked":       unacked,
        "rtt_ms":        rtt_ms,
        "rttvar_ms":     rttvar_ms,
        "retrans_cur":   tcp_info.get("tcpi_retransmits"),
        "retrans_total": tcp_info.get("tcpi_total_retrans"),
        "send":          send,
    }


def _collect_snapshot(
    ip: str, shutdown_ref: list[bool], timeout: float = POLL_TIMEOUT
) -> list[SnapshotRecord] | None:
    """Query kernel via inet_diag netlink. Returns list of (src, dst, metrics)
    for all ESTABLISHED TCP sessions to dst ip, or None if interrupted."""
    if shutdown_ref[0]:
        return None

    family = AF_INET6 if ":" in ip else AF_INET

    try:
        with DiagSocket() as ds:
            ds.bind()
            sockets = ds.get_sock_stats(
                family=family,
                states=SS_CONN,
                extensions=_INET_DIAG_INFO_EXT,
            )
    except OSError as exc:
        raise click.ClickException(f"inet_diag query failed: {exc}")
    except Exception as exc:
        raise click.ClickException(f"inet_diag error: {exc}")

    if shutdown_ref[0]:
        _dbg("snapshot skipped — shutdown signalled during netlink query")
        return None

    _dbg(f"inet_diag returned {len(sockets)} socket(s)")

    results: list[SnapshotRecord] = []
    for sock in sockets:
        # Only ESTABLISHED (state=1); SS_CONN also includes SYN_SENT/SYN_RECV
        if sock.get("idiag_state") != _TCP_ESTABLISHED:
            continue

        dst_ip: str = sock.get("idiag_dst", "")
        if dst_ip != ip:
            continue

        src_ip: str = sock.get("idiag_src", "")
        sport: int = sock.get("idiag_sport", 0)
        dport: int = sock.get("idiag_dport", 0)

        src = f"{src_ip}:{sport}"
        dst = f"{dst_ip}:{dport}"

        tcp_info = dict(sock.get("attrs", {}).get("INET_DIAG_INFO") or {})
        metrics = _extract_metrics(tcp_info)

        _dbg(f"session {src} -> {dst}: {metrics}")
        results.append((src, dst, metrics))

    _dbg(f"collected {len(results)} session(s) to {ip}")
    return results


def _emit_record(
    ts: float,
    src: str,
    dst: str,
    metrics: MetricDict,
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
        fields = ", ".join(f'"{k}":{json.dumps(v)}' for k, v in metrics.items())
        out.write(f"{ts:.3f} [{label}] {fields}\n")
        out.flush()  # text --stream must flush per-record for pipe/file consumers


def _parse_snapshot(
    records: list[SnapshotRecord],
    snapshot_time: float,
    sessions: dict[str, list[tuple[float, MetricDict]]],
    fmt: str,
    stream: bool,
    out: TextIO,
    csv_writer: csv.DictWriter | None,
) -> None:
    for src, dst, metrics in records:
        if stream or fmt in ("ndjson", "csv"):
            _emit_record(snapshot_time, src, dst, metrics, fmt, out, csv_writer)
        else:
            key = f"{src}{SESSION_SEP}{dst}"
            sessions[key].append((snapshot_time, metrics))

    _dbg(f"snapshot processed: {len(records)} session(s)")


def _print_sessions(
    sessions: dict[str, list[tuple[float, MetricDict]]],
    out: TextIO,
) -> None:
    for key, recs in sessions.items():
        if not recs:
            continue
        src, dst = key.split(SESSION_SEP, 1)
        label = f"{src} <--> {dst}"
        out.write("\n")
        out.write(f"======== START TCP SESSION ({label}) ========\n")
        for ts, metrics in recs:
            fields = ", ".join(f'"{k}":{json.dumps(v)}' for k, v in metrics.items())
            out.write(f"{ts:.3f} - {fields}\n")
        out.write(f"======== END TCP SESSION ({label}) ========\n")
        out.write("\n")
    out.flush()


@click.command()
@click.version_option(version=_VERSION, prog_name="tcp-metric-collector")
@click.option("-a", "ip", required=True, help="Destination IP address to monitor (IPv4 or IPv6)")
@click.option("--duration", type=click.FloatRange(min=0.001), default=None,
              help="Stop collecting N seconds after the first TCP session is seen (must be > 0)."
                   " The tool waits indefinitely until traffic appears.")
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
@click.option("--poll-timeout", type=click.FloatRange(min=0.1), default=POLL_TIMEOUT,
              show_default=True,
              help="Max seconds to wait for kernel netlink response per poll cycle.")
def run(ip: str, duration: float | None, max_samples: int | None,
        output: str | None, stream: bool, fmt: str,
        verbose: bool, debug: bool, poll_timeout: float) -> None:
    """Collect TCP metrics for all sessions to a destination IP address."""
    global _verbose, _debug  # reset each invocation — safe for tests calling run() multiple times
    _verbose = verbose or debug
    _debug = debug

    if not is_valid_ip(ip):
        raise click.BadParameter(f"'{ip}' is not a valid IP address.", param_hint="'-a'")

    sessions: dict[str, list[tuple[float, MetricDict]]] = defaultdict(list)
    # Use a mutable container so signal_handler and _collect_snapshot share state
    # without needing a nonlocal closure per call.
    shutdown_ref: list[bool] = [False]
    sample_count = 0
    # duration countdown starts from first sample with ≥1 session, not from
    # program start — so the tool waits indefinitely until traffic appears.
    duration_start_mono: float | None = None

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
             f" output={output!r} poll_timeout={poll_timeout}")

        next_tick = monotonic()
        while not shutdown_ref[0]:
            # Duration check before collection — never over-collect past deadline.
            # Only active after first session found (duration_start_mono set below).
            if duration is not None and duration_start_mono is not None:
                if monotonic() - duration_start_mono >= duration:
                    _log(f"--duration {duration}s elapsed since first session, stopping")
                    break

            next_tick += DEFAULT_SLEEP

            snapshot_time = time()  # capture before query — timestamp reflects sample start
            records = _collect_snapshot(ip, shutdown_ref, poll_timeout)
            if records is None:
                break

            _parse_snapshot(records, snapshot_time, sessions, fmt, stream, out, csv_writer)
            sample_count += 1

            # Start duration countdown on first sample that contains a session.
            if duration is not None and duration_start_mono is None and records:
                duration_start_mono = monotonic()
                _log("first session found — duration countdown started")

            if _verbose:
                if fmt == "text" and not stream:
                    total_records = sum(len(v) for v in sessions.values())
                    _log(f"sample {sample_count}: {len(records)} session(s), "
                         f"{total_records} buffered records")
                else:
                    _log(f"sample {sample_count}: {len(records)} session(s) emitted")

            if max_samples is not None and sample_count >= max_samples:
                _log(f"--max-samples {max_samples} reached, stopping")
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
