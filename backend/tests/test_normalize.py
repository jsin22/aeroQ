"""The terminal normalizer's fixture matrix.

These strings are the shapes real providers emit for the same physical
terminal. Every row asserts that format drift collapses to one canonical value
— which is what makes a terminal filter work across providers.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.normalize import (
    NormalizedFlight,
    format_local_time,
    normalize_terminal,
    parse_flight_number,
    parse_local_time,
    split_flight_number,
)


@pytest.mark.parametrize(
    "raw,expected",
    [
        # Numeric terminals, every observed spelling
        ("1", "1"),
        ("T1", "1"),
        ("t1", "1"),
        ("T 1", "1"),
        ("T-1", "1"),
        ("Terminal 1", "1"),
        ("TERMINAL 1", "1"),
        ("terminal1", "1"),
        ("Term 1", "1"),
        ("Terminal-1", "1"),
        ("01", "1"),
        ("  T2  ", "2"),
        # Letter terminals / concourses
        ("A", "A"),
        ("a", "A"),
        ("Terminal A", "A"),
        ("Concourse A", "A"),
        ("CONCOURSE B", "B"),
        ("Pier C", "C"),
        ("Hall D", "D"),
        # Mixed alphanumerics stay intact — "2A" is not terminal 2
        ("2A", "2A"),
        ("T2A", "2A"),
        ("Terminal 2A", "2A"),
        # Null-ish values
        ("", None),
        ("   ", None),
        ("-", None),
        ("--", None),
        ("N/A", None),
        ("n/a", None),
        ("NA", None),
        ("TBD", None),
        ("TBA", None),
        ("null", None),
        ("None", None),
        ("?", None),
        ("Unknown", None),
        (None, None),
        # Named terminals keep their identity
        ("Main", "MAIN"),
        ("International", "INTERNATIONAL"),
    ],
)
def test_normalize_terminal(raw, expected):
    assert normalize_terminal(raw) == expected


def test_normalize_terminal_accepts_non_strings():
    """Providers sometimes deliver a bare integer in JSON."""
    assert normalize_terminal(1) == "1"
    assert normalize_terminal(2) == "2"


def test_cross_provider_agreement():
    """The property that actually matters: different vendors, same answer."""
    aerodatabox_style = "Terminal 2"
    airlabs_style = "T2"
    bare_style = "2"
    results = {
        normalize_terminal(aerodatabox_style),
        normalize_terminal(airlabs_style),
        normalize_terminal(bare_style),
    }
    assert results == {"2"}


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("UA123", "UA123"),
        ("UA 123", "UA123"),
        ("ua123", "UA123"),
        ("UA-123", "UA123"),
        ("  ua  123 ", "UA123"),
        ("BAW1476", "BAW1476"),
        ("UA0123", "UA123"),  # zero-padding collapsed
        ("AA1", "AA1"),
        ("DL9999", "DL9999"),
    ],
)
def test_parse_flight_number_valid(raw, expected):
    assert parse_flight_number(raw) == expected


@pytest.mark.parametrize(
    "raw,expected",
    [
        # IATA designators containing a digit. These are what is printed on the
        # ticket, and a letters-only pattern rejects every one of them.
        ("B62018", "B62018"),      # JetBlue
        ("b6 2018", "B62018"),
        ("B6-2018", "B62018"),
        ("9W123", "9W123"),        # Jet Airways
        ("6E5301", "6E5301"),      # IndiGo
        ("U21234", "U21234"),      # easyJet
        ("3U8888", "3U8888"),      # Sichuan
        ("W6501", "W6501"),        # Wizz
    ],
)
def test_alphanumeric_airline_designators(raw, expected):
    assert parse_flight_number(raw) == expected


def test_iata_and_icao_forms_of_one_flight_both_parse():
    """B6 2018 and JBU2018 are the same JetBlue flight, differently designated."""
    assert split_flight_number("B62018") == ("B6", 2018, "")
    assert split_flight_number("JBU2018") == ("JBU", 2018, "")


def test_designator_is_not_read_greedily():
    """A widened [A-Z0-9]{2,3} would read B62018 as airline 'B62', flight 018."""
    assert split_flight_number("B62018")[0] == "B6"
    assert split_flight_number("B62018")[1] == 2018


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "hello",
        "123",
        "U1",
        "UNITED 123",
        "11123",  # digit-digit designators are not issued
        "UA",
        "UA12345",
        "!!",
        "SELECT * FROM flights",
    ],
)
def test_parse_flight_number_rejects_garbage(raw):
    """Malformed input must be rejected locally — a provider call costs budget."""
    assert parse_flight_number(raw) is None


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("2026-08-10 14:30", datetime(2026, 8, 10, 14, 30)),
        ("2026-08-10T14:30", datetime(2026, 8, 10, 14, 30)),
        ("2026-08-10 14:30:00", datetime(2026, 8, 10, 14, 30)),
        ("2026-08-10T14:30:00", datetime(2026, 8, 10, 14, 30)),
        ("2026-08-10T14:30+02:00", datetime(2026, 8, 10, 14, 30)),
        ("2026-08-10T14:30:00Z", datetime(2026, 8, 10, 14, 30)),
        ("", None),
        (None, None),
        ("garbage", None),
    ],
)
def test_parse_local_time(raw, expected):
    assert parse_local_time(raw) == expected


def test_local_time_roundtrip():
    dt = datetime(2026, 8, 10, 14, 30)
    assert parse_local_time(format_local_time(dt)) == dt


def test_normalized_flight_derives_terminal():
    f = NormalizedFlight(
        dep_iata="sfo",
        dep_time_local=datetime(2026, 8, 10, 14, 30),
        dep_terminal="Terminal 2",
        source_provider="mock",
    )
    assert f.dep_iata == "SFO"
    assert f.dep_terminal == "Terminal 2"  # raw value preserved for debugging
    assert f.dep_terminal_norm == "2"
