"""Unit tests for tcp_metrics_collector parsing functions."""

import io
import subprocess
from collections import defaultdict
from pathlib import Path
from unittest.mock import patch

import click
import pytest

from tcp_metrics_collector import (
    SESSION_SEP,
    _RE_HAS_METRIC,
    _collect_snapshot,
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
    def test_no_wscale_but_has_metrics_parsed(self):
        # wscale absence no longer gates parsing — regex matches decide
        line = "\t cubic rto:204 rtt:1.234/0.617 mss:1460 cwnd:10"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["cwnd"] == 10
        assert result["rtt_ms"] == 1.234

    def test_no_metric_tokens_returns_none(self):
        line = "\t cubic rto:204 ato:40 rcv_rtt:1 rcv_space:29200"
        assert _parse_metrics_line(line) is None

    def test_integer_fields_typed(self):
        line = "\t cubic wscale:7,8 rto:204 mss:1460 cwnd:10 ssthresh:2147483647 unacked:0"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["cwnd"] == 10
        assert isinstance(result["cwnd"], int)
        assert result["mss"] == 1460
        assert isinstance(result["mss"], int)
        assert result["ssthresh"] == 2147483647
        assert isinstance(result["ssthresh"], int)
        assert result["unacked"] == 0
        assert isinstance(result["unacked"], int)

    def test_rtt_split_into_two_floats(self):
        line = "\t cubic wscale:7,8 rtt:1.234/0.617"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["rtt_ms"] == 1.234
        assert isinstance(result["rtt_ms"], float)
        assert result["rttvar_ms"] == 0.617
        assert isinstance(result["rttvar_ms"], float)

    def test_retrans_split_into_two_ints(self):
        line = "\t cubic wscale:7,8 retrans:3/12"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["retrans_cur"] == 3
        assert isinstance(result["retrans_cur"], int)
        assert result["retrans_total"] == 12
        assert isinstance(result["retrans_total"], int)

    def test_retrans_zero_zero(self):
        line = "\t cubic wscale:7,8 retrans:0/0"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["retrans_cur"] == 0
        assert result["retrans_total"] == 0

    def test_send_kept_as_string(self):
        line = "\t cubic wscale:7,8 send 84.7Mbps"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["send"] == "84.7Mbps"
        assert isinstance(result["send"], str)

    def test_send_double_space_normalized(self):
        line = "\t cubic wscale:7,8 send  84.7Mbps"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["send"] == "84.7Mbps"

    def test_absent_fields_are_none(self):
        line = "\t cubic wscale:7,8 cwnd:5"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["rtt_ms"] is None
        assert result["rttvar_ms"] is None
        assert result["retrans_cur"] is None
        assert result["retrans_total"] is None
        assert result["send"] is None
        assert result["mss"] is None

    def test_no_type_ambiguity_absent_vs_zero(self):
        # absent field → None, not 0
        line = "\t cubic wscale:7,8 cwnd:5"
        result = _parse_metrics_line(line)
        assert result is not None
        assert result["unacked"] is None  # absent, not 0

    def test_non_metric_tokens_ignored(self):
        line = "\t cubic wscale:7,8 timer:on rto:204 cwnd:10 unknown:value"
        result = _parse_metrics_line(line)
        assert result is not None
        assert "timer" not in result
        assert "unknown" not in result
        assert result["cwnd"] == 10


# ---------------------------------------------------------------------------
# _parse_snapshot — integration of session+metrics pairing
# ---------------------------------------------------------------------------

class TestParseSnapshot:
    def _run(self, fixture: str, ip: str = "192.168.1.100") -> dict:
        lines = load_fixture(fixture)
        filtered = [
            line for i, line in enumerate(lines)
            if ip in line or (
                _RE_HAS_METRIC.search(line) and i > 0 and ip in lines[i - 1]
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
        assert metrics["cwnd"] == 10
        assert metrics["mss"] == 1460

    def test_multiple_sessions_not_mixed(self):
        sessions = self._run("ss_multiple_sessions.txt")
        assert len(sessions) == 2
        key1 = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        key2 = f"192.168.1.50:45232{SESSION_SEP}192.168.1.100:443"
        assert key1 in sessions
        assert key2 in sessions
        assert sessions[key1][0][1]["cwnd"] == 10
        assert sessions[key2][0][1]["cwnd"] == 20
        assert sessions[key1][0][1]["mss"] == 1460
        assert sessions[key2][0][1]["mss"] == 1448

    def test_closing_session_not_parsed(self):
        sessions = self._run("ss_closing.txt")
        assert len(sessions) == 1
        key = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        assert key in sessions

    def test_closing_metrics_not_leaked_to_estab(self):
        sessions = self._run("ss_closing.txt")
        key = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        metrics = sessions[key][0][1]
        # ESTAB cwnd=10, CLOSING had cwnd=5; must be 10
        assert metrics["cwnd"] == 10

    def test_ipv6_session_produces_no_output(self):
        sessions = self._run("ss_ipv6.txt", ip="2001:db8::2")
        assert len(sessions) == 0

    def test_no_wscale_still_parsed_if_metrics_present(self):
        # ss_no_wscale.txt has cwnd/rtt/etc but no wscale token — must still parse
        sessions = self._run("ss_no_wscale.txt")
        assert len(sessions) == 1
        key = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        assert key in sessions
        metrics = sessions[key][0][1]
        assert metrics["cwnd"] == 10
        assert metrics["rtt_ms"] == 1.234

    def test_send_only_metrics_line_not_filtered(self):
        # Line with only "send VALUE" (space-separated) must survive _collect_snapshot
        # filter and be parsed. Previously _RE_HAS_METRIC missed it (matched key:value only).
        sessions = self._run("ss_send_only.txt")
        assert len(sessions) == 1
        key = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        assert key in sessions
        assert sessions[key][0][1]["send"] == "84.7Mbps"

    def test_no_metric_tokens_produces_no_output(self):
        # ss_no_metrics.txt has a metrics-style line but zero allowlisted tokens
        sessions = self._run("ss_no_metrics.txt")
        assert len(sessions) == 0


# ---------------------------------------------------------------------------
# _collect_snapshot — adjacency filter tested with mocked subprocess
# ---------------------------------------------------------------------------

def _make_ss_result(fixture: str, returncode: int = 0) -> subprocess.CompletedProcess:
    text = (FIXTURES / fixture).read_text()
    return subprocess.CompletedProcess(
        args=["ss"], returncode=returncode, stdout=text, stderr=""
    )


class TestCollectSnapshot:
    def test_session_and_metric_line_kept(self):
        with patch("subprocess.run", return_value=_make_ss_result("ss_estab_single.txt")):
            kept = _collect_snapshot("192.168.1.100", [False])
        assert kept is not None
        assert len(kept) == 2
        assert "192.168.1.100" in kept[0]   # session line
        assert "cwnd" in kept[1]             # metrics line

    def test_header_line_not_kept(self):
        # ss_estab_single.txt has a Netid/State header; -H flag removes it in production
        # but our fixture includes it to test that the filter correctly ignores it
        with patch("subprocess.run", return_value=_make_ss_result("ss_estab_single.txt")):
            kept = _collect_snapshot("192.168.1.100", [False])
        assert kept is not None
        assert not any("Netid" in line for line in kept)

    def test_send_only_metric_line_kept(self):
        # Fixture has metrics line with only "send VALUE" (space-separated).
        # _RE_HAS_METRIC must match it so it survives the filter.
        with patch("subprocess.run", return_value=_make_ss_result("ss_send_only.txt")):
            kept = _collect_snapshot("192.168.1.100", [False])
        assert kept is not None
        assert len(kept) == 2
        assert "send 84.7Mbps" in kept[1]

    def test_send_double_space_filter_and_parse(self):
        # "send  84.7Mbps" (two spaces) — pre-filter and normalizer must handle \s+
        sessions = TestParseSnapshot()._run("ss_send_doublespace.txt")
        assert len(sessions) == 1
        key = f"192.168.1.50:45231{SESSION_SEP}192.168.1.100:80"
        assert sessions[key][0][1]["send"] == "84.7Mbps"

    def test_multiple_sessions_all_pairs_kept(self):
        with patch("subprocess.run", return_value=_make_ss_result("ss_multiple_sessions.txt")):
            kept = _collect_snapshot("192.168.1.100", [False])
        assert kept is not None
        # 2 sessions × 2 lines each
        assert len(kept) == 4

    def test_no_metric_line_session_not_kept(self):
        # ss_no_metrics.txt: metrics line has no allowlisted tokens → not kept
        with patch("subprocess.run", return_value=_make_ss_result("ss_no_metrics.txt")):
            kept = _collect_snapshot("192.168.1.100", [False])
        assert kept is not None
        # session line itself contains IP and is kept; metrics line is not
        assert len(kept) == 1

    def test_shutdown_during_ss_returns_none(self):
        shutdown = [False]

        def _side_effect(*args, **kwargs):
            shutdown[0] = True  # simulate SIGINT arriving while ss runs
            return subprocess.CompletedProcess(args=["ss"], returncode=0, stdout="", stderr="")

        with patch("subprocess.run", side_effect=_side_effect):
            result = _collect_snapshot("192.168.1.100", shutdown)
        assert result is None

    def test_nonzero_returncode_raises(self):
        bad = subprocess.CompletedProcess(args=["ss"], returncode=1, stdout="", stderr="oops")
        with patch("subprocess.run", return_value=bad):
            with pytest.raises(click.ClickException, match="oops"):
                _collect_snapshot("192.168.1.100", [False])

    def test_file_not_found_raises(self):
        with patch("subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(click.ClickException, match="iproute2"):
                _collect_snapshot("192.168.1.100", [False])

    def test_timeout_raises(self):
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ss", timeout=5.0)):
            with pytest.raises(click.ClickException, match="5.0"):
                _collect_snapshot("192.168.1.100", [False])

    def test_timeout_with_shutdown_returns_none(self):
        # Ctrl+C while ss is hung: TimeoutExpired fires but shutdown is already set.
        # Must return None (clean exit) so buffered text results are printed.
        shutdown = [True]
        with patch("subprocess.run", side_effect=subprocess.TimeoutExpired(cmd="ss", timeout=5.0)):
            result = _collect_snapshot("192.168.1.100", shutdown)
        assert result is None
