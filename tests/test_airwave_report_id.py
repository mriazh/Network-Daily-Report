"""Tests for AirWave report window date parsing and extract_report_id logic."""

from __future__ import annotations

from datetime import date

from network_report.collectors.airwave import (
    extract_report_id,
    get_matching_finished_report_ids,
    parse_report_window_date,
    report_window_matches,
)


def test_parse_report_window_date():
    # AM/PM with timezone suffix
    assert parse_report_window_date("10/19/2026 12:00 AM WIB") == date(2026, 10, 19)
    assert parse_report_window_date("10/19/2026 11:59 PM WIB") == date(2026, 10, 19)

    # 24-hour clock
    assert parse_report_window_date("10/19/2026 00:00") == date(2026, 10, 19)
    assert parse_report_window_date("10/19/2026 23:59") == date(2026, 10, 19)

    # Single-digit month/day
    assert parse_report_window_date("9/7/2026 12:00 PM WIB") == date(2026, 9, 7)

    # Garbage / invalid input
    assert parse_report_window_date("") is None
    assert parse_report_window_date("-") is None
    assert parse_report_window_date("invalid text") is None
    assert parse_report_window_date("99/99/9999") is None


def test_extract_report_id_regression_in_progress_first():
    """Verify that In Progress unrelated row first does not block our finished row second."""
    html = """
    <table>
      <tr>
        <td>icon</td>
        <td>In Progress</td>
        <td>weekly</td>
        <td>Client Inventory Report</td>
        <td>All Groups and Folders</td>
        <td>adm1n</td>
        <td>9/27/2026 12:00 PM WIB</td>
        <td>-</td>
      </tr>
      <tr>
        <td>icon</td>
        <td>10/22/2026 1:16 AM WIB</td>
        <td><a href="/report_detail?id=5450">#UP time Report Switch Daily</a></td>
        <td>Custom</td>
        <td>Groups SW Core Network</td>
        <td>bot</td>
        <td>10/19/2026 12:00 AM WIB</td>
        <td>10/19/2026 11:59 PM WIB</td>
      </tr>
    </table>
    """
    target = date(2026, 10, 19)
    # When target_date is given
    assert extract_report_id(html, target_date=target) == "5450"
    # When target_date is None (loose mode)
    assert extract_report_id(html) == "5450"


def test_extract_report_id_returns_none_when_only_matching_row_in_progress():
    html = """
    <table>
      <tr>
        <td>icon</td>
        <td>In Progress</td>
        <td><a href="/report_detail?id=5450">#UP time Report Switch Daily</a></td>
        <td>Custom</td>
        <td>SW</td>
        <td>bot</td>
        <td>10/19/2026 12:00 AM WIB</td>
        <td>10/19/2026 11:59 PM WIB</td>
      </tr>
    </table>
    """
    assert extract_report_id(html, target_date=date(2026, 10, 19)) is None
    assert extract_report_id(html) is None


def test_extract_report_id_skips_different_day_window():
    html = """
    <table>
      <tr>
        <td>icon</td>
        <td>10/21/2026 1:16 AM WIB</td>
        <td><a href="/report_detail?id=5440">#UP time Report Switch Daily</a></td>
        <td>Custom</td>
        <td>SW</td>
        <td>bot</td>
        <td>10/18/2026 12:00 AM WIB</td>
        <td>10/18/2026 11:59 PM WIB</td>
      </tr>
      <tr>
        <td>icon</td>
        <td>10/22/2026 1:16 AM WIB</td>
        <td><a href="/report_detail?id=5450">#UP time Report Switch Daily</a></td>
        <td>Custom</td>
        <td>SW</td>
        <td>bot</td>
        <td>10/19/2026 12:00 AM WIB</td>
        <td>10/19/2026 11:59 PM WIB</td>
      </tr>
    </table>
    """
    assert extract_report_id(html, target_date=date(2026, 10, 19)) == "5450"
    assert extract_report_id(html, target_date=date(2026, 10, 18)) == "5440"
    assert extract_report_id(html, target_date=date(2026, 10, 20)) is None


def test_extract_report_id_no_report_detail_href():
    html = """
    <table>
      <tr>
        <td>icon</td>
        <td>10/22/2026 1:16 AM WIB</td>
        <td>#UP time Report Switch Daily</td>
        <td>Custom</td>
        <td>SW</td>
        <td>bot</td>
        <td>10/19/2026 12:00 AM WIB</td>
        <td>10/19/2026 11:59 PM WIB</td>
      </tr>
    </table>
    """
    assert extract_report_id(html, target_date=date(2026, 10, 19)) is None
    assert extract_report_id(html) is None


def test_extract_report_id_with_exclude_ids():
    html = """
    <table>
      <tr>
        <td>icon</td>
        <td>10/22/2026 1:16 AM WIB</td>
        <td><a href="/report_detail?id=5450">#UP time Report Switch Daily</a></td>
        <td>Custom</td>
        <td>SW</td>
        <td>bot</td>
        <td>10/19/2026 12:00 AM WIB</td>
        <td>10/19/2026 11:59 PM WIB</td>
      </tr>
      <tr>
        <td>icon</td>
        <td>10/22/2026 1:00 AM WIB</td>
        <td><a href="/report_detail?id=5449">#UP time Report Switch Daily</a></td>
        <td>Custom</td>
        <td>SW</td>
        <td>bot</td>
        <td>10/19/2026 12:00 AM WIB</td>
        <td>10/19/2026 11:59 PM WIB</td>
      </tr>
    </table>
    """
    # Exclude the first one (5450), should return 5449
    assert (
        extract_report_id(
            html,
            target_date=date(2026, 10, 19),
            exclude_ids={"5450"},
        )
        == "5449"
    )
    # Exclude both, return None
    assert (
        extract_report_id(
            html,
            target_date=date(2026, 10, 19),
            exclude_ids={"5450", "5449"},
        )
        is None
    )


def test_get_matching_finished_report_ids_and_window_matches():
    html = """
    <table>
      <tr>
        <td>icon</td>
        <td>In Progress</td>
        <td><a href="/report_detail?id=9999">#UP time Report Switch Daily</a></td>
        <td>Custom</td>
        <td>SW</td>
        <td>bot</td>
        <td>10/19/2026 12:00 AM WIB</td>
        <td>10/19/2026 11:59 PM WIB</td>
      </tr>
      <tr>
        <td>icon</td>
        <td>10/22/2026 1:16 AM WIB</td>
        <td><a href="/report_detail?id=5450">#UP time Report Switch Daily</a></td>
        <td>Custom</td>
        <td>SW</td>
        <td>bot</td>
        <td>10/19/2026 12:00 AM WIB</td>
        <td>10/19/2026 11:59 PM WIB</td>
      </tr>
      <tr>
        <td>icon</td>
        <td>10/22/2026 1:00 AM WIB</td>
        <td><a href="/report_detail?id=5440">#UP time Report Switch Daily</a></td>
        <td>Custom</td>
        <td>SW</td>
        <td>bot</td>
        <td>10/18/2026 12:00 AM WIB</td>
        <td>10/18/2026 11:59 PM WIB</td>
      </tr>
    </table>
    """
    # Window matches helper directly
    cells_ok = ["", "", "", "", "", "", "10/19/2026 12:00 AM WIB", "10/19/2026 11:59 PM WIB"]
    assert report_window_matches(cells_ok, date(2026, 10, 19)) is True
    assert report_window_matches(cells_ok, date(2026, 10, 18)) is False
    assert report_window_matches(["short"], date(2026, 10, 19)) is False

    # Finished IDs for 10/19: only 5450 (9999 is in progress)
    ids_19 = get_matching_finished_report_ids(html, date(2026, 10, 19))
    assert ids_19 == {"5450"}

    # Finished IDs for 10/18: only 5440
    ids_18 = get_matching_finished_report_ids(html, date(2026, 10, 18))
    assert ids_18 == {"5440"}
