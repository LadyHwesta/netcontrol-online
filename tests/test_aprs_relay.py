"""
Tests for aprs_relay.py's pure logic: the APRS-IS passcode algorithm and
the TNC2 uncompressed-position packet parser.

These import the standalone relay script directly (repo root is on
sys.path the same way it is for `main`/`database` — see conftest.py).
Network/socket behavior (connect_aprs_is, main's poll loop) is not
covered here, matching the DMR precedent of not requiring real hardware
to verify relay logic.

`requests` is an OPTIONAL runtime dependency — it's intentionally not in
requirements.txt because relay scripts run on a separate machine (the
hotspot/igate host), not the server itself (see README's DMR relay
section). aprs_relay.py exits at import time if it's missing, mirroring
dmr_relay.py, so this whole module is skipped rather than failing
collection when `requests` isn't installed in the dev/test environment.
"""

import pytest

try:
    import requests  # noqa: F401
    import aprs_relay
except (ImportError, SystemExit):
    aprs_relay = None

pytestmark = pytest.mark.skipif(
    aprs_relay is None, reason="requests package not installed — optional dependency, see README's DMR relay section"
)


class TestAprsPasscode:
    def test_matches_published_reference(self):
        # N0CALL -> 13023 is the canonical published reference value for
        # the APRS-IS passcode algorithm.
        assert aprs_relay.aprs_passcode("N0CALL") == 13023

    def test_ignores_ssid(self):
        assert aprs_relay.aprs_passcode("N0CALL-9") == aprs_relay.aprs_passcode("N0CALL")

    def test_case_insensitive(self):
        assert aprs_relay.aprs_passcode("w1aw") == aprs_relay.aprs_passcode("W1AW")

    def test_result_is_15_bit(self):
        assert 0 <= aprs_relay.aprs_passcode("K1ABC") <= 0x7FFF


class TestParseTnc2Line:
    def test_position_no_timestamp_no_messaging(self):
        line = "N0CALL>APRS,TCPIP*:!4903.50N/07201.75W-Test comment"
        entry = aprs_relay.parse_tnc2_line(line)
        assert entry is not None
        assert entry["callsign"] == "N0CALL"
        assert entry["lat"] == pytest.approx(49 + 3.50 / 60)
        assert entry["lon"] == pytest.approx(-(72 + 1.75 / 60))
        assert entry["comment"] == "Test comment"
        assert entry["symbol"] == "/-"

    def test_position_with_messaging_south_east(self):
        line = "K1ABC>APRS,TCPIP*:=4903.50S/07201.75E-Field team"
        entry = aprs_relay.parse_tnc2_line(line)
        assert entry is not None
        assert entry["lat"] < 0
        assert entry["lon"] > 0

    def test_position_with_timestamp(self):
        line = "W1AW>APRS,TCPIP*:/092345z4903.50N/07201.75W>Net control"
        entry = aprs_relay.parse_tnc2_line(line)
        assert entry is not None
        assert entry["callsign"] == "W1AW"
        assert entry["comment"] == "Net control"
        assert "heard_at" in entry
        assert entry["heard_at"].endswith("UTC")

    def test_course_and_speed_parsed_and_stripped_from_comment(self):
        line = "N0CALL>APRS,TCPIP*:!4903.50N/07201.75W-088/036Comment here"
        entry = aprs_relay.parse_tnc2_line(line)
        assert entry["course"] == 88
        assert entry["speed"] == 36.0
        assert entry["comment"] == "Comment here"

    def test_altitude_parsed_and_stripped_from_comment(self):
        line = "N0CALL>APRS,TCPIP*:!4903.50N/07201.75W-/A=001234Balloon"
        entry = aprs_relay.parse_tnc2_line(line)
        assert entry["altitude"] == 1234.0
        assert "A=001234" not in entry["comment"]

    def test_no_comment_omits_field(self):
        line = "N0CALL>APRS,TCPIP*:!4903.50N/07201.75W-"
        entry = aprs_relay.parse_tnc2_line(line)
        assert "comment" not in entry

    def test_ignores_non_position_packet_types(self):
        # A status packet ('>') — not a position report, should be skipped.
        line = "N0CALL>APRS,TCPIP*:>Off duty"
        assert aprs_relay.parse_tnc2_line(line) is None

    def test_ignores_server_comment_lines(self):
        assert aprs_relay.parse_tnc2_line("# aprsc 2.1.19-g730c5c2") is None

    def test_ignores_blank_lines(self):
        assert aprs_relay.parse_tnc2_line("") is None
        assert aprs_relay.parse_tnc2_line("   ") is None

    def test_ignores_malformed_line_no_colon(self):
        assert aprs_relay.parse_tnc2_line("N0CALL>APRS,TCPIP*") is None

    def test_ignores_malformed_line_no_header(self):
        assert aprs_relay.parse_tnc2_line(":!4903.50N/07201.75W-x") is None

    def test_ignores_compressed_position_packet(self):
        # Compressed format's first body char is a non-digit symbol-table
        # char — v1 explicitly doesn't decode these.
        line = "N0CALL>APRS,TCPIP*:!/5L!!<*e7>7P[Compressed]"
        assert aprs_relay.parse_tnc2_line(line) is None

    def test_ignores_truncated_position(self):
        line = "N0CALL>APRS,TCPIP*:!4903.50N/072"
        assert aprs_relay.parse_tnc2_line(line) is None

    def test_rejects_out_of_range_coordinates(self):
        # Malformed digits could decode to nonsense degrees; guard against
        # ever emitting an obviously-invalid lat/lon.
        line = "N0CALL>APRS,TCPIP*:!9903.50N/07201.75W-bad lat"
        assert aprs_relay.parse_tnc2_line(line) is None
