"""Unit tests for tcp_metrics_collector parsing functions."""

import io
from collections import defaultdict
from pathlib import Path

import pytest

from tcp_metrics_collector import (
    SESSION_SEP,
    _parse_metrics_line,
    _parse_session_line,
    _parse_snapshot,
    is_valid_ipv4,
)

FIXTURES = Path(__file__).parent / "fixtures"


def load_fixture(name: str) -> list[str]:
    return (FIXTURES / name).read_text().splitlines()


# ---------------------------------------------------------------------------
# is_valid_ipv4
# ---------------------------------------------------------------------------

class TestIsValidIPv4:
    def test_valid(self):
        assert is_valid_ipv4("192.168.1.100") is True

    def test_loopback(self):
        assert is_valid_ipv4("127.0.0.1") is True

    def test_ipv6_rejected(self):
        assert is_valid_ipv4("::1") is False

    def test_ipv6_full_rejected(self):
        assert is_valid_ipv4("2001:db8::1") is False

    def test_garbage_rejected(self):
        assert is_valid_ipv4("not-an-ip") is False

    def test_out_of_range_rejected(self):
        assert is_valid_ipv4("999.0.0.1") is False


# ---------------------------------------------------------------------------
# _parse_session_line
# ---------------------------------------------------------------------------

class TestParseSessionLine:
    def test_valid_estab(self):
        line = "tcp   ESTAB  0      0      192.168.1.50:45231   192.168.1.100:80"
        result = _parse_session_line(line)
        assert result == ("192.168.1.50:45231", "192.168.1.100:80")

    def test_closing_skipped(self):
        line = "tcp   CLOSING  0  0  192.168.1.50:45230   192.168.1.100:80"
        assert _parse_session_line(line) is None

    def test_non_tcp_skipped(self):
        line = "udp   ESTAB  0  0  192.168.1.50:1234  192.168.1.100:53"
        assert _parse_session_line(line) is None

    def test_metrics_line_skipped(self):
        line = "\t cubic wscale:7,8 rto:204 rtt:1.234/0.617 cwnd:10"
        assert _parse_session_line(line) is None

    def test_ipv6_session_not_matched(self):
        line = "tcp   ESTAB  0  0  [2001:db8::1]:45231  [2001:db8::2]:80"
        assert _parse_session_line(line) is None

    def test_header_line_skipped(self):
        line = "Netid State  Recv-Q Send-Q Local Address:Port    Peer Address:Port"
        assert _parse_session_line(line) is None


# ---------------------------------------------------------------------------
# _parse_metrics_line
# ---------------------------------------------------------------------------

class TestParseMetricsLine:
    def test_valid_metrics(self):
        line = "\t cubic wscale:7,8 rto:204 rtt:1.234/0.617 mss:1460 cwnd:10 ssthresh:2147483647 send 84.7Mbps unacked:0 retrans:0/0"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["cwnd"] == "10"
        assert result["rtt"] == "1.234/0.617"
        assert result["mss"] == "1460"
        assert result["ssthresh"] == "2147483647"
        assert result["send"] == "84.7Mbps"
        assert result["unacked"] == "0"
        assert result["retrans"] == "0/0"

    def test_no_wscale_returns_none(self):
        line = "\t cubic rto:204 rtt:1.234/0.617 mss:1460 cwnd:10"
        assert _parse_metrics_line(line) is None

    def test_missing_metric_defaults_to_zero(self):
        line = "\t cubic wscale:7,8 rto:204 cwnd:5"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["cwnd"] == "5"
        assert result["rtt"] == 0
        assert result["mss"] == 0
        assert result["retrans"] == 0

    def test_send_space_normalized(self):
        line = "\t cubic wscale:7,8 send 123Mbps cwnd:10"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["send"] == "123Mbps"

    def test_non_metric_tokens_ignored(self):
        line = "\t cubic wscale:7,8 timer:on rto:204 cwnd:10 unknown:value"
        result = _parse_metrics_line(line)
        assert result is not None
        assert "timer" not in result
        assert "unknown" not in result
        assert result["cwnd"] == "10"


# ---------------------------------------------------------------------------
# _parse_snapshot — integration of session+metrics pairing
# ---------------------------------------------------------------------------

class TestParseSnapshot:
    def _run(self, fixture: str, ip: str = "192.168.1.100") -> dict:
        lines = load_fixture(fixture)
        # Filter lines as _collect_snapshot would
        filtered = [
            line for i, line in enumerate(lines)
            if ip in line or (
                "wscale" in line and i > 0 and ip in lines[i - 1]
            )
        ]
        sessions: dict = defaultdict(list)
        _parse_snapshot(filtered, 1000.0, sessions, "text", False, io.StringIO(), None)
        return dict(sessions)

    def test_single_session_parsed(self):
        sessions = self._run("ss_estab_single.txt")
        assert len(sessions) == 1
        key = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        assert key in sessions
        ts, metrics = sessions[key][0]
        assert ts == 1000.0
        assert metrics["cwnd"] == "10"
        assert metrics["mss"] == "1460"

    def test_multiple_sessions_not_mixed(self):
        sessions = self._run("ss_multiple_sessions.txt")
        assert len(sessions) == 2
        key1 = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        key2 = f"192.168.1.50:45232{SESSION_SEP}192.168.1.100:443"
        assert key1 in sessions
        assert key2 in sessions
        assert sessions[key1][0][1]["cwnd"] == "10"
        assert sessions[key2][0][1]["cwnd"] == "20"
        assert sessions[key1][0][1]["mss"] == "1460"
        assert sessions[key2][0][1]["mss"] == "1448"

    def test_closing_session_not_parsed(self):
        sessions = self._run("ss_closing.txt")
        # Only ESTAB session should appear
        assert len(sessions) == 1
        key = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        assert key in sessions

    def test_closing_metrics_not_leaked_to_estab(self):
        sessions = self._run("ss_closing.txt")
        key = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        metrics = sessions[key][0][1]
        # ESTAB session cwnd=10, CLOSING had cwnd=5; must be 10
        assert metrics["cwnd"] == "10"

    def test_ipv6_session_produces_no_output(self):
        sessions = self._run("ss_ipv6.txt", ip="2001:db8::2")
        assert len(sessions) == 0

    def test_no_wscale_produces_no_output(self):
        sessions = self._run("ss_no_wscale.txt")
        assert len(sessions) == 0
