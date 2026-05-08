"""CLI integration tests using click.testing.CliRunner."""

import csv
import io
import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

if sys.version_info >= (3, 11):
    import tomllib
else:
    try:
        import tomllib  # type: ignore[no-redef]
    except ImportError:
        import tomli as tomllib  # type: ignore[no-redef]

from tcp_metrics_collector import _VERSION, run

FIXTURES = Path(__file__).parent / "fixtures"

# Structured records as returned by the new _collect_snapshot (netlink backend)
_METRICS_SINGLE = {
    "cwnd": 10, "mss": 1460, "ssthresh": 2147483647, "unacked": 0,
    "rtt_ms": 1.234, "rttvar_ms": 0.617,
    "retrans_cur": 0, "retrans_total": 0, "send": "14.6Mbps",
}
_METRICS_SECOND = {
    "cwnd": 20, "mss": 1448, "ssthresh": 87380, "unacked": 3,
    "rtt_ms": 2.5, "rttvar_ms": 1.25,
    "retrans_cur": 1, "retrans_total": 2, "send": "46.2Mbps",
}

_SINGLE_SESSION_RECORDS = [
    ("192.168.1.50:45231", "192.168.1.100:80", _METRICS_SINGLE),
]

_TWO_SESSION_RECORDS = [
    ("192.168.1.50:45231", "192.168.1.100:80", _METRICS_SINGLE),
    ("192.168.1.50:45232", "192.168.1.100:443", _METRICS_SECOND),
]


def _mock_one_shot(records: list):
    """Return a _collect_snapshot mock that yields records once then signals shutdown."""
    fired = False

    def _collect(ip, shutdown_ref, timeout=None):
        nonlocal fired
        if fired:
            shutdown_ref[0] = True
            return None
        fired = True
        return records

    return _collect


@pytest.fixture
def runner():
    return CliRunner()


def _json_lines(output: str) -> list[str]:
    """Extract only JSON object lines from mixed output (INFO banner etc.)."""
    return [line for line in output.splitlines() if line.startswith("{")]


def _csv_output(output: str) -> str:
    """Return only CSV lines (header + data rows) from mixed output.

    CSV header starts with 'ts,'; data rows start with a Unix timestamp digit.
    INFO/DEBUG lines are excluded.
    """
    kept = [line for line in output.splitlines()
            if line.startswith("ts,") or (line and line[0].isdigit())]
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_missing_ip_exits_2(self, runner):
        result = runner.invoke(run, [])
        assert result.exit_code == 2
        assert "-a" in result.output

    def test_invalid_ip_exits_2(self, runner):
        result = runner.invoke(run, ["-a", "not-an-ip"])
        assert result.exit_code == 2

    def test_garbage_ip_exits_2(self, runner):
        result = runner.invoke(run, ["-a", "999.0.0.1"])
        assert result.exit_code == 2

    def test_ipv4_accepted(self, runner):
        # Valid IPv4 passes validation — will fail at netlink (not mocked here)
        # but exit_code != 2 means validation passed
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot([])):
            result = runner.invoke(run, ["-a", "192.168.1.100"])
        assert result.exit_code == 0

    def test_ipv6_accepted(self, runner):
        # IPv6 now valid input
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot([])):
            result = runner.invoke(run, ["-a", "::1"])
        assert result.exit_code == 0

    def test_max_samples_zero_exits_2(self, runner):
        result = runner.invoke(run, ["-a", "1.2.3.4", "--max-samples", "0"])
        assert result.exit_code == 2

    def test_max_samples_negative_exits_2(self, runner):
        result = runner.invoke(run, ["-a", "1.2.3.4", "--max-samples", "-1"])
        assert result.exit_code == 2

    def test_duration_zero_exits_2(self, runner):
        result = runner.invoke(run, ["-a", "1.2.3.4", "--duration", "0"])
        assert result.exit_code == 2

    def test_duration_negative_exits_2(self, runner):
        result = runner.invoke(run, ["-a", "1.2.3.4", "--duration", "-5"])
        assert result.exit_code == 2

    def test_invalid_format_exits_2(self, runner):
        result = runner.invoke(run, ["-a", "1.2.3.4", "--format", "xml"])
        assert result.exit_code == 2

    def test_version_matches_pyproject(self, runner):
        pyproject = tomllib.loads(
            (Path(__file__).parent.parent / "pyproject.toml").read_text()
        )
        expected = pyproject["project"]["version"]
        assert _VERSION == expected
        result = runner.invoke(run, ["--version"])
        assert expected in result.output


# ---------------------------------------------------------------------------
# Text format (default)
# ---------------------------------------------------------------------------

class TestTextFormat:
    def test_session_block_in_output(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100"])
        assert result.exit_code == 0
        assert "START TCP SESSION" in result.output
        assert "192.168.1.50:45231" in result.output
        assert "192.168.1.100:80" in result.output

    def test_two_sessions_both_in_output(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_TWO_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100"])
        assert result.exit_code == 0
        assert result.output.count("START TCP SESSION") == 2

    def test_empty_snapshot_no_crash(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot([])):
            result = runner.invoke(run, ["-a", "192.168.1.100"])
        assert result.exit_code == 0

    def test_stream_emits_per_sample(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--stream"])
        assert result.exit_code == 0
        data_lines = [l for l in result.output.splitlines() if "192.168.1" in l]
        assert len(data_lines) >= 1

    def test_stream_to_file_written_per_sample(self, runner, tmp_path):
        out_file = tmp_path / "stream.txt"
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, [
                "-a", "192.168.1.100", "--stream", "--output", str(out_file),
            ])
        assert result.exit_code == 0
        content = out_file.read_text()
        assert "192.168.1.50:45231" in content

    def test_max_samples_1_stops_after_one(self, runner):
        call_count = []

        def _collect(ip, shutdown_ref, timeout=None):
            call_count.append(1)
            return _SINGLE_SESSION_RECORDS

        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_collect):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--max-samples", "1"])
        assert result.exit_code == 0
        assert len(call_count) == 1

    def test_duration_stops_collection(self, runner):
        call_count = []

        def _collect(ip, shutdown_ref, timeout=None):
            call_count.append(1)
            return _SINGLE_SESSION_RECORDS

        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_collect):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--duration", "0.001"])
        assert result.exit_code == 0
        assert len(call_count) == 1

    def test_duration_waits_for_first_session(self, runner):
        calls = []

        def _collect(ip, shutdown_ref, timeout=None):
            calls.append(1)
            if len(calls) < 3:
                return []
            shutdown_ref[0] = True
            return _SINGLE_SESSION_RECORDS

        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_collect):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--duration", "60"])
        assert result.exit_code == 0
        assert len(calls) == 3
        assert "START TCP SESSION" in result.output


# ---------------------------------------------------------------------------
# NDJSON format
# ---------------------------------------------------------------------------

class TestNdjsonFormat:
    def test_output_is_valid_json(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "ndjson"])
        assert result.exit_code == 0
        lines = _json_lines(result.output)
        assert len(lines) >= 1
        for line in lines:
            obj = json.loads(line)
            assert "ts" in obj
            assert "src" in obj
            assert "dst" in obj

    def test_integer_fields_are_int(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "ndjson"])
        obj = json.loads(_json_lines(result.output)[0])
        assert isinstance(obj["cwnd"], int)
        assert isinstance(obj["mss"], int)

    def test_rtt_split_into_two_fields(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "ndjson"])
        obj = json.loads(_json_lines(result.output)[0])
        assert "rtt_ms" in obj
        assert "rttvar_ms" in obj
        assert isinstance(obj["rtt_ms"], float)

    def test_absent_field_is_null(self, runner):
        records_no_rtt = [("192.168.1.50:1234", "192.168.1.100:80", {
            **_METRICS_SINGLE, "rtt_ms": None, "rttvar_ms": None,
        })]
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(records_no_rtt)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "ndjson"])
        obj = json.loads(_json_lines(result.output)[0])
        assert obj["rtt_ms"] is None

    def test_two_sessions_two_lines(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_TWO_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "ndjson"])
        assert len(_json_lines(result.output)) == 2


# ---------------------------------------------------------------------------
# CSV format
# ---------------------------------------------------------------------------

class TestCsvFormat:
    def test_output_is_valid_csv(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "csv"])
        assert result.exit_code == 0
        rows = list(csv.DictReader(io.StringIO(_csv_output(result.output))))
        assert len(rows) >= 1

    def test_csv_has_expected_columns(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "csv"])
        reader = csv.DictReader(io.StringIO(_csv_output(result.output)))
        fields = reader.fieldnames or []
        for col in ("ts", "src", "dst", "cwnd", "rtt_ms", "rttvar_ms", "retrans_cur", "retrans_total"):
            assert col in fields, f"missing column: {col}"

    def test_two_sessions_two_data_rows(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_TWO_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "csv"])
        rows = list(csv.DictReader(io.StringIO(_csv_output(result.output))))
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# --output file
# ---------------------------------------------------------------------------

class TestOutputFile:
    def test_output_written_to_file(self, runner, tmp_path):
        out_file = tmp_path / "metrics.txt"
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--output", str(out_file)])
        assert result.exit_code == 0
        assert out_file.exists()
        content = out_file.read_text()
        assert "START TCP SESSION" in content

    def test_output_bad_path_exits_1(self, runner):
        result = runner.invoke(run, ["-a", "192.168.1.100", "--output", "/nonexistent/dir/out.txt"])
        assert result.exit_code == 1
        assert "Cannot open output file" in result.output

    def test_output_directory_as_file_exits_1(self, runner, tmp_path):
        result = runner.invoke(run, ["-a", "192.168.1.100", "--output", str(tmp_path)])
        assert result.exit_code == 1
        assert "Cannot open output file" in result.output

    def test_ndjson_to_file_is_valid(self, runner, tmp_path):
        out_file = tmp_path / "metrics.ndjson"
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_RECORDS)):
            result = runner.invoke(run, [
                "-a", "192.168.1.100",
                "--format", "ndjson",
                "--output", str(out_file),
            ])
        assert result.exit_code == 0
        for line in out_file.read_text().splitlines():
            if line.strip():
                json.loads(line)


# ---------------------------------------------------------------------------
# inet_diag failure
# ---------------------------------------------------------------------------

class TestDiagFailure:
    def test_oserror_exits_1(self, runner):
        from unittest.mock import MagicMock
        mock_ds = MagicMock()
        mock_ds.__enter__ = MagicMock(return_value=mock_ds)
        mock_ds.__exit__ = MagicMock(return_value=False)
        mock_ds.get_sock_stats.side_effect = OSError("permission denied")

        with patch("tcp_metrics_collector.DiagSocket", return_value=mock_ds):
            result = runner.invoke(run, ["-a", "192.168.1.100"])
        assert result.exit_code == 1
        assert "permission denied" in result.output
