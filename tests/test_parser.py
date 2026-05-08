"""Unit tests for tcp_metrics_collector netlink-based metric extraction."""

import io
from collections import defaultdict
from unittest.mock import MagicMock, patch

import click
import pytest

from tcp_metrics_collector import (
    SESSION_SEP,
    _TCP_ESTABLISHED,
    _collect_snapshot,
    _extract_metrics,
    _format_addr,
    _format_rate,
    _parse_snapshot,
    is_valid_ip,
)


# ---------------------------------------------------------------------------
# is_valid_ip — now accepts both IPv4 and IPv6
# ---------------------------------------------------------------------------

class TestIsValidIp:
    def test_ipv4_valid(self):
        assert is_valid_ip("192.168.1.100") is True

    def test_ipv4_loopback(self):
        assert is_valid_ip("127.0.0.1") is True

    def test_ipv6_loopback(self):
        assert is_valid_ip("::1") is True

    def test_ipv6_full(self):
        assert is_valid_ip("2001:db8::1") is True

    def test_garbage_rejected(self):
        assert is_valid_ip("not-an-ip") is False

    def test_out_of_range_rejected(self):
        assert is_valid_ip("999.0.0.1") is False


# ---------------------------------------------------------------------------
# _format_rate
# ---------------------------------------------------------------------------

class TestFormatRate:
    def test_gbps(self):
        assert _format_rate(2e9) == "2Gbps"

    def test_mbps(self):
        assert _format_rate(84.7e6) == "84.7Mbps"

    def test_kbps(self):
        assert _format_rate(500e3) == "500Kbps"

    def test_bps(self):
        assert _format_rate(100) == "100bps"


# ---------------------------------------------------------------------------
# _format_addr — RFC 2732 bracket notation for IPv6
# ---------------------------------------------------------------------------

class TestFormatAddr:
    def test_ipv4_no_brackets(self):
        assert _format_addr("192.168.1.1", 80) == "192.168.1.1:80"

    def test_ipv4_loopback(self):
        assert _format_addr("127.0.0.1", 1234) == "127.0.0.1:1234"

    def test_ipv6_loopback_bracketed(self):
        assert _format_addr("::1", 80) == "[::1]:80"

    def test_ipv6_full_bracketed(self):
        assert _format_addr("2001:db8::1", 443) == "[2001:db8::1]:443"

    def test_ipv6_port_unambiguous(self):
        # Ensure split on last ":" gives correct port in all cases
        addr = _format_addr("2001:db8::1", 8080)
        assert addr.endswith(":8080")
        assert addr.startswith("[")


# ---------------------------------------------------------------------------
# _extract_metrics — tcp_info dict → output schema
# ---------------------------------------------------------------------------

class TestExtractMetrics:
    def _full_tcp_info(self, **overrides) -> dict:
        base = {
            "tcpi_snd_cwnd": 10,
            "tcpi_snd_mss": 1460,
            "tcpi_snd_ssthresh": 2147483647,
            "tcpi_unacked": 0,
            "tcpi_rtt": 1234,       # µs → 1.234ms
            "tcpi_rttvar": 617,     # µs → 0.617ms
            "tcpi_retransmits": 0,
            "tcpi_total_retrans": 2,
        }
        base.update(overrides)
        return base

    def test_integer_fields_typed(self):
        m = _extract_metrics(self._full_tcp_info())
        assert m["cwnd"] == 10
        assert isinstance(m["cwnd"], int)
        assert m["mss"] == 1460
        assert isinstance(m["mss"], int)
        assert m["ssthresh"] == 2147483647
        assert m["unacked"] == 0

    def test_rtt_converted_from_microseconds(self):
        m = _extract_metrics(self._full_tcp_info(tcpi_rtt=1234, tcpi_rttvar=617))
        assert m["rtt_ms"] == pytest.approx(1.234)
        assert m["rttvar_ms"] == pytest.approx(0.617)

    def test_rtt_none_when_zero(self):
        m = _extract_metrics(self._full_tcp_info(tcpi_rtt=0, tcpi_rttvar=0))
        assert m["rtt_ms"] is None
        assert m["rttvar_ms"] is None

    def test_retrans_fields(self):
        m = _extract_metrics(self._full_tcp_info(tcpi_retransmits=3, tcpi_total_retrans=12))
        assert m["retrans_cur"] == 3
        assert m["retrans_total"] == 12

    def test_send_derived_from_cwnd_mss_rtt(self):
        # cwnd=10, mss=1460, rtt=1000µs → 10*1460*8*1e6/1000 = 116.8e6 bps ≈ 117Mbps
        m = _extract_metrics(self._full_tcp_info(
            tcpi_snd_cwnd=10, tcpi_snd_mss=1460, tcpi_rtt=1000
        ))
        assert m["send"] is not None
        assert "Mbps" in m["send"]
        # Verify order of magnitude — should be ~117Mbps not ~14Mbps
        rate_mbps = float(m["send"].replace("Mbps", ""))
        assert rate_mbps > 100

    def test_send_none_when_rtt_zero(self):
        m = _extract_metrics(self._full_tcp_info(tcpi_rtt=0))
        assert m["send"] is None

    def test_absent_field_is_none(self):
        m = _extract_metrics({})
        assert m["cwnd"] is None
        assert m["mss"] is None
        assert m["rtt_ms"] is None
        assert m["retrans_cur"] is None
        assert m["send"] is None


# ---------------------------------------------------------------------------
# _collect_snapshot — mock DiagSocket
# ---------------------------------------------------------------------------

def _make_sock(src_ip, src_port, dst_ip, dst_port, state=_TCP_ESTABLISHED, **tcp_info_overrides):
    """Build a mock inet_diag_msg object matching pyroute2's get_sock_stats() return."""
    tcp_info = {
        "tcpi_snd_cwnd": 10,
        "tcpi_snd_mss": 1460,
        "tcpi_snd_ssthresh": 2147483647,
        "tcpi_unacked": 0,
        "tcpi_rtt": 1000,
        "tcpi_rttvar": 500,
        "tcpi_retransmits": 0,
        "tcpi_total_retrans": 0,
    }
    tcp_info.update(tcp_info_overrides)
    # pyroute2 returns nlmsg objects that behave like dicts for scalar fields
    # but use get_attr() for NLA attributes (attrs is a list of tuples)
    sock = MagicMock()
    sock.get = lambda key, default=None: {
        "idiag_state": state,
        "idiag_src": src_ip,
        "idiag_dst": dst_ip,
        "idiag_sport": src_port,
        "idiag_dport": dst_port,
    }.get(key, default)
    sock.get_attr = lambda key: tcp_info if key == "INET_DIAG_INFO" else None
    return sock


class TestCollectSnapshot:
    def _run(self, ip: str, sockets: list) -> list | None:
        mock_ds = MagicMock()
        mock_ds.__enter__ = MagicMock(return_value=mock_ds)
        mock_ds.__exit__ = MagicMock(return_value=False)
        mock_ds.get_sock_stats.return_value = sockets

        with patch("tcp_metrics_collector.DiagSocket", return_value=mock_ds):
            return _collect_snapshot(ip, [False])

    def test_single_session_returned(self):
        socks = [_make_sock("192.168.1.50", 45231, "192.168.1.100", 80)]
        records = self._run("192.168.1.100", socks)
        assert records is not None
        assert len(records) == 1
        src, dst, metrics = records[0]
        assert src == "192.168.1.50:45231"
        assert dst == "192.168.1.100:80"
        assert metrics["cwnd"] == 10

    def test_non_established_sessions_excluded(self):
        socks = [
            _make_sock("192.168.1.50", 1234, "192.168.1.100", 80, state=2),  # SYN_SENT
            _make_sock("192.168.1.50", 5678, "192.168.1.100", 80, state=1),  # ESTABLISHED
        ]
        records = self._run("192.168.1.100", socks)
        assert records is not None
        assert len(records) == 1
        assert records[0][0] == "192.168.1.50:5678"

    def test_wrong_dst_excluded(self):
        socks = [
            _make_sock("192.168.1.50", 1234, "10.0.0.1", 80),     # different dst
            _make_sock("192.168.1.50", 5678, "192.168.1.100", 80), # correct dst
        ]
        records = self._run("192.168.1.100", socks)
        assert records is not None
        assert len(records) == 1
        assert records[0][1] == "192.168.1.100:80"

    def test_empty_result_on_no_sessions(self):
        records = self._run("192.168.1.100", [])
        assert records == []

    def test_shutdown_returns_none(self):
        def _side_effect(*_, **__):
            raise Exception("should not be called")

        with patch("tcp_metrics_collector.DiagSocket") as MockDS:
            # Simulate shutdown set before call
            result = _collect_snapshot("192.168.1.100", [True])
        assert result is None

    def test_shutdown_after_query_returns_none(self):
        socks = [_make_sock("192.168.1.50", 45231, "192.168.1.100", 80)]
        shutdown = [False]

        mock_ds = MagicMock()
        mock_ds.__enter__ = MagicMock(return_value=mock_ds)
        mock_ds.__exit__ = MagicMock(return_value=False)

        def _get_stats(*_, **__):
            shutdown[0] = True  # signal arrives during query
            return socks

        mock_ds.get_sock_stats.side_effect = _get_stats

        with patch("tcp_metrics_collector.DiagSocket", return_value=mock_ds):
            result = _collect_snapshot("192.168.1.100", shutdown)
        assert result is None

    def test_timeout_raises_click_exception(self):
        import time
        import threading

        mock_ds = MagicMock()
        mock_ds.__enter__ = MagicMock(return_value=mock_ds)
        mock_ds.__exit__ = MagicMock(return_value=False)

        def _slow_query(*_, **__):
            time.sleep(10)  # hangs — join(timeout) fires before this

        mock_ds.get_sock_stats.side_effect = _slow_query

        non_daemon_before = sum(1 for t in threading.enumerate() if not t.daemon)

        with patch("tcp_metrics_collector.DiagSocket", return_value=mock_ds):
            with pytest.raises(click.ClickException, match="0.05"):
                _collect_snapshot("192.168.1.100", [False], timeout=0.05)

        # Worker thread must be daemon — no new non-daemon threads should remain
        non_daemon_after = sum(1 for t in threading.enumerate() if not t.daemon)
        assert non_daemon_after == non_daemon_before

    def test_timeout_with_shutdown_returns_none(self):
        import time
        import threading

        mock_ds = MagicMock()
        mock_ds.__enter__ = MagicMock(return_value=mock_ds)
        mock_ds.__exit__ = MagicMock(return_value=False)

        shutdown = [False]

        def _slow_query(*_, **__):
            shutdown[0] = True
            time.sleep(10)

        mock_ds.get_sock_stats.side_effect = _slow_query

        non_daemon_before = sum(1 for t in threading.enumerate() if not t.daemon)

        with patch("tcp_metrics_collector.DiagSocket", return_value=mock_ds):
            result = _collect_snapshot("192.168.1.100", shutdown, timeout=0.05)
        assert result is None

        non_daemon_after = sum(1 for t in threading.enumerate() if not t.daemon)
        assert non_daemon_after == non_daemon_before

    def test_oserror_raises_click_exception(self):
        mock_ds = MagicMock()
        mock_ds.__enter__ = MagicMock(return_value=mock_ds)
        mock_ds.__exit__ = MagicMock(return_value=False)
        mock_ds.get_sock_stats.side_effect = OSError("permission denied")

        with patch("tcp_metrics_collector.DiagSocket", return_value=mock_ds):
            with pytest.raises(click.ClickException, match="permission denied"):
                _collect_snapshot("192.168.1.100", [False])

    def test_multiple_sessions_all_returned(self):
        socks = [
            _make_sock("192.168.1.50", 45231, "192.168.1.100", 80, tcpi_snd_cwnd=10),
            _make_sock("192.168.1.50", 45232, "192.168.1.100", 443, tcpi_snd_cwnd=20),
        ]
        records = self._run("192.168.1.100", socks)
        assert records is not None
        assert len(records) == 2
        cwnds = {r[2]["cwnd"] for r in records}
        assert cwnds == {10, 20}

    def test_ipv6_dst_filter(self):
        socks = [
            _make_sock("::1", 45231, "::1", 80),
            _make_sock("::1", 45232, "2001:db8::1", 80),
        ]
        records = self._run("::1", socks)
        assert records is not None
        assert len(records) == 1
        assert records[0][1] == "[::1]:80"

    def test_ipv6_non_canonical_input_matches(self):
        # Kernel may return compressed "2001:db8::1"; user may pass expanded form.
        # Both must match after canonical normalisation.
        socks = [_make_sock("::1", 45231, "2001:db8::1", 80)]
        # Pass expanded form — should still match compressed kernel output
        records = self._run("2001:0db8:0:0:0:0:0:1", socks)
        assert records is not None
        assert len(records) == 1

    def test_ipv6_src_address_normalised_in_output(self):
        # Source address from kernel is already compressed by pyroute2,
        # but verify it passes through correctly.
        socks = [_make_sock("2001:db8::2", 45231, "2001:db8::1", 80)]
        records = self._run("2001:db8::1", socks)
        assert records is not None
        assert records[0][0] == "[2001:db8::2]:45231"  # [addr]:port RFC 2732


# ---------------------------------------------------------------------------
# _parse_snapshot — structured records in, buffered or streamed out
# ---------------------------------------------------------------------------

class TestParseSnapshot:
    def _record(self, src="192.168.1.50:1234", dst="192.168.1.100:80", cwnd=10):
        return (src, dst, {
            "cwnd": cwnd, "mss": 1460, "ssthresh": 2147483647, "unacked": 0,
            "rtt_ms": 1.234, "rttvar_ms": 0.617,
            "retrans_cur": 0, "retrans_total": 0, "send": "14.6Mbps",
        })

    def test_text_mode_buffers_to_sessions(self):
        sessions: dict = defaultdict(list)
        _parse_snapshot(
            [self._record()], 1000.0, sessions,
            "text", False, io.StringIO(), None
        )
        key = f"192.168.1.50:1234{SESSION_SEP}192.168.1.100:80"
        assert key in sessions
        assert sessions[key][0][0] == 1000.0
        assert sessions[key][0][1]["cwnd"] == 10

    def test_ndjson_mode_emits_immediately(self):
        out = io.StringIO()
        sessions: dict = defaultdict(list)
        _parse_snapshot(
            [self._record()], 1000.0, sessions, "ndjson", False, out, None
        )
        assert len(sessions) == 0  # not buffered
        line = out.getvalue().strip()
        import json
        obj = json.loads(line)
        assert obj["cwnd"] == 10
        assert obj["src"] == "192.168.1.50:1234"

    def test_stream_mode_emits_immediately(self):
        out = io.StringIO()
        sessions: dict = defaultdict(list)
        _parse_snapshot(
            [self._record()], 1000.0, sessions, "text", True, out, None
        )
        assert len(sessions) == 0
        assert "192.168.1.50:1234" in out.getvalue()

    def test_two_records_both_buffered(self):
        records = [
            self._record("192.168.1.50:1", "192.168.1.100:80", cwnd=10),
            self._record("192.168.1.50:2", "192.168.1.100:443", cwnd=20),
        ]
        sessions: dict = defaultdict(list)
        _parse_snapshot(records, 1000.0, sessions, "text", False, io.StringIO(), None)
        assert len(sessions) == 2
