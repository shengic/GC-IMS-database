"""`promote()` covers all 6 firmware generations without raising.

Pins the DESIGN §2f HEADER_KEY_ALIASES table: every promoted column must
be filled OR left None (never raise) for each firmware generation. Also
pins numeric extraction from '150 [kHz]', multi-format date parsing, and
the drift_gas fallback to `EPC gas settings` for fw 4.73.
"""
import datetime as dt
import pytest

from mea_parser import (
    promote,
    parse_program,
    program_hash,
    sample_type_from_name,
    parse_telemetry,
    TELEMETRY_ALIASES,
)


class TestPromoteNeverRaisesOnRealFiles:
    def test_returns_dict_with_all_expected_keys(self, parsed_sample):
        header, _, _ = parsed_sample
        p = promote(header)
        expected = {
            "machine_type", "machine_serial", "machine_name", "adio_serial",
            "firmware_version", "firmware_date",
            "drift_tube_um", "drift_voltage_v", "sensor_data",
            "program_raw", "gc_column", "drift_gas",
            "flow_ims_setpoint_ml_min", "flow_gc_setpoint_ml_min",
            "temp_setpoints_c",
            "measured_at", "sample_name", "sample_class", "status",
            "chunk_averages", "sample_rate_khz", "trig_repetition_ms",
            "ambient_pressure_kpa",
        }
        assert expected.issubset(set(p.keys()))

    def test_required_identity_fields_present(self, parsed_sample):
        """machine_serial and measured_at must resolve on every real file."""
        header, _, _ = parsed_sample
        p = promote(header)
        assert p["machine_serial"], "no machine serial — ingest would 'parse_error'"
        assert isinstance(p["measured_at"], dt.datetime)

    def test_sample_rate_and_trig_are_numeric_on_all_firmwares(self, parsed_sample, sample_firmware):
        header, _, _ = parsed_sample
        p = promote(header)
        assert p["sample_rate_khz"] is not None, (
            f"fw {sample_firmware}: sample rate not extracted — "
            "check HEADER_KEY_ALIASES map (Chunk sample rate vs Sample rate)"
        )
        assert p["trig_repetition_ms"] is not None, (
            f"fw {sample_firmware}: trig repetition not extracted — "
            "check alias (Chunk trigger repetition vs Trig. repetition time)"
        )


class TestPromoteFirmwareSpecificAliases:
    """Pin exact alias-to-column mapping per DESIGN §2f."""

    def test_fw216_lacks_drift_gas_and_gc_column(self, parsed_sample, sample_firmware):
        if sample_firmware != "fw216":
            pytest.skip("fw2.16-only")
        header, _, _ = parsed_sample
        p = promote(header)
        assert p["drift_gas"] is None
        assert p["gc_column"] is None
        assert p["drift_tube_um"] is None
        assert p["drift_voltage_v"] is None

    def test_fw473_drift_gas_resolves(self, parsed_sample, sample_firmware):
        """fw 4.73 always exposes `EPC gas settings` (e.g. 'IMS: N2, GC: N2')
        and may also carry `Drift Gas`. Promoter must resolve to N-something
        via whichever key is present (Drift Gas wins per alias order)."""
        if sample_firmware != "fw473":
            pytest.skip("fw4.73-only")
        header, _, _ = parsed_sample
        assert "EPC gas settings" in header
        p = promote(header)
        assert p["drift_gas"] is not None
        assert p["drift_gas"].upper().startswith("N")  # N2

    def test_fw482_drift_voltage_from_sensor_drift_voltage(self, parsed_sample, sample_firmware):
        """fw 4.x uses `Sensor drift voltage`; fw 2.5x uses `nom Drift
        Potential Difference`. Promoter tries the fw 4.x key first."""
        if sample_firmware != "fw482":
            pytest.skip("fw4.82-only")
        header, _, _ = parsed_sample
        p = promote(header)
        assert p["drift_voltage_v"] is not None


class TestPromoteFakeHeaders:
    def test_absent_promoted_key_returns_none(self):
        p = promote({"Machine serial": '"X1"'})  # very sparse header
        assert p["machine_serial"] == "X1"
        assert p["gc_column"] is None
        assert p["drift_gas"] is None
        assert p["measured_at"] is None
        assert p["temp_setpoints_c"] is None

    def test_number_extracted_from_units_annotation(self):
        p = promote({"Chunk sample rate": "150 [kHz]"})
        assert p["sample_rate_khz"] == 150.0

    def test_off_and_xxx_placeholders_become_none(self):
        p = promote({"nom Drift Tube Length": '"xxx"',
                     "Chunk averages": '"off"'})
        assert p["drift_tube_um"] is None
        assert p["chunk_averages"] is None

    def test_iso_and_legacy_dates_both_parse(self):
        p1 = promote({"Firmware date": '"2025-11-25"'})
        assert p1["firmware_date"] == dt.date(2025, 11, 25)
        p2 = promote({"Firmware date": '"Apr 25 2014"'})
        assert p2["firmware_date"] == dt.date(2014, 4, 25)

    def test_temp_setpoints_collected(self):
        h = {"Temp 1 setpoint": "45 [°C]",
             "Temp 2 setpoint": "60 [°C]",
             "Temp 3 setpoint": "80 [°C]"}
        p = promote(h)
        assert set(p["temp_setpoints_c"].keys()) == {"T1", "T2", "T3"}
        assert "45" in p["temp_setpoints_c"]["T1"]
        assert "60" in p["temp_setpoints_c"]["T2"]


class TestParseProgram:
    def test_extracts_name_from_program_string(self):
        prog = '"Name=`TEA-C`|Avges=6|Passes=1"'
        name, ok = parse_program(prog)
        assert name == "TEA-C"
        assert ok is True

    def test_absent_program_returns_none_ok(self):
        name, ok = parse_program(None)
        assert name is None
        assert ok is True  # absent is not a failure per §19

    def test_unparseable_returns_marker_and_warning(self):
        # deliberately without backticks so the Name=`X` regex cannot match
        name, ok = parse_program("some new firmware format without backticks")
        assert name == "(unparsed)"
        assert ok is False  # ingest should warn per §19


class TestProgramHash:
    def test_stable(self):
        h1 = program_hash("Name=`X`|A=1", "col1", "N2")
        h2 = program_hash("Name=`X`|A=1", "col1", "N2")
        assert h1 == h2
        assert len(h1) == 64  # sha256 hex

    def test_different_components_produce_different_hash(self):
        base = program_hash("N", "C", "G")
        assert program_hash("N2", "C", "G") != base
        assert program_hash("N", "C2", "G") != base
        assert program_hash("N", "C", "G2") != base

    def test_none_components_tolerated(self):
        # fw 2.16 lacks GC Column / Drift Gas — hash must still compute
        h = program_hash("Name=`X`", None, None)
        assert len(h) == 64


class TestSampleTypeClassification:
    @pytest.mark.parametrize("name,expected", [
        ("Blank-Air", "blank"),
        ("Blind_Luft_0µgL-1", "blank"),
        ("Calibration", "standard"),
        ("QC-batch-A", "qc"),
        ("Tea-1-1", "sample"),
        ("103BY01086-1", "sample"),
        (None, "unknown"),
        ("", "unknown"),
    ])
    def test_classification(self, name, expected):
        assert sample_type_from_name(name) == expected


class TestParseTelemetry:
    def test_at_least_two_series_on_real_files(self, parsed_sample):
        """Even fw 2.16 has Flow Epc 1/2. Every real file → ≥2 series."""
        header, _, _ = parsed_sample
        series = parse_telemetry(header)
        assert len(series) >= 2

    def test_series_names_are_canonical(self, parsed_sample):
        header, _, _ = parsed_sample
        series = parse_telemetry(header)
        canonical = set(TELEMETRY_ALIASES.values())
        for name, _ in series:
            assert name in canonical

    def test_fw473_has_pump1_series(self, parsed_sample, sample_firmware):
        if sample_firmware != "fw473":
            pytest.skip("fw4.73 GC-IMS-only")
        header, _, _ = parsed_sample
        series = dict(parse_telemetry(header))
        assert "pump1_flow" in series
        assert "pump1_pressure" in series

    def test_non_numeric_tokens_dropped(self):
        header = {"Flow Epc 1": '"1.0 2.0 xxx 3.0 off 4.0"'}
        series = dict(parse_telemetry(header))
        assert series["flow_ims"] == [1.0, 2.0, 3.0, 4.0]

    def test_absent_series_not_in_output(self):
        series = parse_telemetry({})
        assert series == []
