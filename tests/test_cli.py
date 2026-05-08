"""CLI integration tests using click.testing.CliRunner."""

import csv
import io
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from click.testing import CliRunner

from tcp_metrics_collector import run

FIXTURES = Path(__file__).parent / "fixtures"

# Filtered lines from ss_estab_single.txt as _collect_snapshot would return
# (header already stripped by -H flag; only session+metrics lines kept)
_SINGLE_SESSION_LINES = [
    "tcp   ESTAB  0      0      192.168.1.50:45231   192.168.1.100:80",
    "\t cubic wscale:7,8 rto:204 rtt:1.234/0.617 ato:40 mss:1460 cwnd:10 ssthresh:2147483647 send 84.7Mbps unacked:0 retrans:0/0",
]

_TWO_SESSION_LINES = [
    "tcp   ESTAB  0      0      192.168.1.50:45231   192.168.1.100:80",
    "\t cubic wscale:7,8 rtt:1.234/0.617 mss:1460 cwnd:10 ssthresh:2147483647 send 84.7Mbps unacked:0 retrans:0/0",
    "tcp   ESTAB  0      0      192.168.1.50:45232   192.168.1.100:443",
    "\t cubic wscale:7,8 rtt:2.500/1.250 mss:1448 cwnd:20 ssthresh:87380 send 46.2Mbps unacked:3 retrans:1/2",
]


def _mock_one_shot(lines: list[str]):
    """Return a _collect_snapshot mock that yields lines once then signals shutdown."""
    calls = []

    def _collect(ip, shutdown_ref):
        if calls:
            shutdown_ref[0] = True
            return None
        calls.append(1)
        return lines

    return _collect


@pytest.fixture
def runner():
    return CliRunner()


def _json_lines(output: str) -> list[str]:
    """Extract only JSON object lines from mixed output (INFO banner etc.)."""
    return [l for l in output.splitlines() if l.startswith("{")]


def _csv_output(output: str) -> str:
    """Return only CSV lines (header + data rows) from mixed output.

    CSV header starts with 'ts,'; data rows start with a Unix timestamp digit.
    INFO/DEBUG lines are excluded.
    """
    kept = [l for l in output.splitlines()
            if l.startswith("ts,") or (l and l[0].isdigit())]
    return "\n".join(kept)


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------

class TestInputValidation:
    def test_missing_ip_exits_2(self, runner):
        result = runner.invoke(run, [])
        assert result.exit_code == 2
        assert "-a" in result.output

    def test_invalid_ipv4_exits_2(self, runner):
        result = runner.invoke(run, ["-a", "::1"])
        assert result.exit_code == 2
        assert "not a valid IPv4" in result.output

    def test_garbage_ip_exits_2(self, runner):
        result = runner.invoke(run, ["-a", "not-an-ip"])
        assert result.exit_code == 2

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


# ---------------------------------------------------------------------------
# Text format (default)
# ---------------------------------------------------------------------------

class TestTextFormat:
    def test_session_block_in_output(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_LINES)):
            result = runner.invoke(run, ["-a", "192.168.1.100"])
        assert result.exit_code == 0
        assert "START TCP SESSION" in result.output
        assert "192.168.1.50:45231" in result.output
        assert "192.168.1.100:80" in result.output

    def test_two_sessions_both_in_output(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_TWO_SESSION_LINES)):
            result = runner.invoke(run, ["-a", "192.168.1.100"])
        assert result.exit_code == 0
        assert result.output.count("START TCP SESSION") == 2

    def test_empty_snapshot_no_crash(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot([])):
            result = runner.invoke(run, ["-a", "192.168.1.100"])
        assert result.exit_code == 0

    def test_max_samples_1_stops_after_one(self, runner):
        call_count = []

        def _collect(ip, shutdown_ref):
            call_count.append(1)
            return _SINGLE_SESSION_LINES

        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_collect):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--max-samples", "1"])
        assert result.exit_code == 0
        assert len(call_count) == 1


# ---------------------------------------------------------------------------
# NDJSON format
# ---------------------------------------------------------------------------

class TestNdjsonFormat:
    def test_output_is_valid_json(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_LINES)):
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
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_LINES)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "ndjson"])
        obj = json.loads(_json_lines(result.output)[0])
        assert isinstance(obj["cwnd"], int)
        assert isinstance(obj["mss"], int)

    def test_rtt_split_into_two_fields(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_LINES)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "ndjson"])
        obj = json.loads(_json_lines(result.output)[0])
        assert "rtt_ms" in obj
        assert "rttvar_ms" in obj
        assert isinstance(obj["rtt_ms"], float)

    def test_absent_field_is_null(self, runner):
        lines_no_mss = [
            "tcp   ESTAB  0  0  192.168.1.50:45231   192.168.1.100:80",
            "\t cubic wscale:7,8 cwnd:5 rtt:1.0/0.5",
        ]
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(lines_no_mss)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "ndjson"])
        obj = json.loads(_json_lines(result.output)[0])
        assert obj["mss"] is None

    def test_two_sessions_two_lines(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_TWO_SESSION_LINES)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "ndjson"])
        assert len(_json_lines(result.output)) == 2


# ---------------------------------------------------------------------------
# CSV format
# ---------------------------------------------------------------------------

class TestCsvFormat:
    def test_output_is_valid_csv(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_LINES)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "csv"])
        assert result.exit_code == 0
        rows = list(csv.DictReader(io.StringIO(_csv_output(result.output))))
        assert len(rows) >= 1

    def test_csv_has_expected_columns(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_LINES)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "csv"])
        reader = csv.DictReader(io.StringIO(_csv_output(result.output)))
        fields = reader.fieldnames or []
        for col in ("ts", "src", "dst", "cwnd", "rtt_ms", "rttvar_ms", "retrans_cur", "retrans_total"):
            assert col in fields, f"missing column: {col}"

    def test_two_sessions_two_data_rows(self, runner):
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_TWO_SESSION_LINES)):
            result = runner.invoke(run, ["-a", "192.168.1.100", "--format", "csv"])
        rows = list(csv.DictReader(io.StringIO(_csv_output(result.output))))
        assert len(rows) == 2


# ---------------------------------------------------------------------------
# --output file
# ---------------------------------------------------------------------------

class TestOutputFile:
    def test_output_written_to_file(self, runner, tmp_path):
        out_file = tmp_path / "metrics.txt"
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_LINES)):
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
        with patch("tcp_metrics_collector._collect_snapshot", side_effect=_mock_one_shot(_SINGLE_SESSION_LINES)):
            result = runner.invoke(run, [
                "-a", "192.168.1.100",
                "--format", "ndjson",
                "--output", str(out_file),
            ])
        assert result.exit_code == 0
        for line in out_file.read_text().splitlines():
            if line.strip():
                json.loads(line)  # must not raise


# ---------------------------------------------------------------------------
# ss failure
# ---------------------------------------------------------------------------

class TestSsFailure:
    def test_ss_nonzero_exits_1(self, runner):
        import subprocess
        mock_result = subprocess.CompletedProcess(
            args=["ss"], returncode=1, stdout="", stderr="permission denied"
        )
        with patch("subprocess.run", return_value=mock_result):
            result = runner.invoke(run, ["-a", "192.168.1.100"])
        assert result.exit_code == 1

    def test_ss_error_message_on_stderr(self, runner):
        import subprocess
        mock_result = subprocess.CompletedProcess(
            args=["ss"], returncode=1, stdout="", stderr="permission denied"
        )
        with patch("subprocess.run", return_value=mock_result):
            result = runner.invoke(run, ["-a", "192.168.1.100"], catch_exceptions=False)
        assert "permission denied" in result.output

    def test_ss_not_found_exits_1_with_message(self, runner):
        with patch("subprocess.run", side_effect=FileNotFoundError("ss")):
            result = runner.invoke(run, ["-a", "192.168.1.100"], catch_exceptions=False)
        assert result.exit_code == 1
        assert "iproute2" in result.output
