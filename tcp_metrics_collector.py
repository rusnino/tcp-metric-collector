#
# TCP Metrics Collector
#

import argparse
import ipaddress
import json
import re
import signal
import subprocess
import sys
from time import sleep, time
from typing import Dict, List, Tuple

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
    print_data: Dict = {"curr_session": ""}

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

                tcp_session_key = f"{lookup_tcp_session[0][0]}_{lookup_tcp_session[0][1]}"
                print_data["curr_session"] = tcp_session_key
                if tcp_session_key not in print_data:
                    print_data[tcp_session_key] = {"metrics": []}

            if "wscale" in line and print_data["curr_session"]:
                line = line.replace("send ", "send:") if "send " in line else line
                tcp_session_metrics = _parse_tcp_metrics(line)
                if not tcp_session_metrics:
                    continue

                curr_session = print_data["curr_session"]
                print_data[curr_session]["metrics"].append((snapshot_time, tcp_session_metrics))

    for tcp, data in print_data.items():
        if tcp == "curr_session":
            continue
        if not data["metrics"]:
            continue

        print()
        print(f"======== START TCP SESSION ({tcp.replace('_', ' <--> ')}) ========")
        for ts, metric in data["metrics"]:
            print(f"{ts:.3f} - {metric[1:-1]}")
        print(f"======== END TCP SESSION ({tcp.replace('_', ' <--> ')}) ========")
        print()


def run() -> None:
    parser = argparse.ArgumentParser(description="TCP Metrics Collector")
    parser.add_argument("-a", action="store", dest="ip", required=True, type=str,
                        help="Destination IP address to monitor")
    args = parser.parse_args()

    if not is_valid_ip(args.ip):
        print(f"Error: '{args.ip}' is not a valid IP address.")
        sys.exit(1)

    tcp_metrics: List[Tuple[float, str]] = []

    def signal_handler(*_) -> None:
        print_tcp_metrics(tcp_metrics)
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print(f"INFO: Collecting TCP metrics every {DEFAULT_SLEEP}s. Press Ctrl+C to stop.")

    while True:
        result = subprocess.run(
            ["ss", "-i", "dst", args.ip],
            capture_output=True,
            text=True,
        )
        lines = [l for l in result.stdout.splitlines() if args.ip in l or "wscale" in l]
        tcp_metrics.append((time(), "\n".join(lines)))
        sleep(DEFAULT_SLEEP)


if __name__ == "__main__":
    run()
