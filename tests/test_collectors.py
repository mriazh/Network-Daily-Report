"""Unit tests for collectors and writers."""

import base64
import builtins
import subprocess
from pathlib import Path

import pytest
from openpyxl import Workbook

from network_report.collectors.airwave import collect_airwave_data
from network_report.collectors.cmp_crawler import (
    _get_firefox_user_agent,
    _navigate_to_usage_query,
    _resolve_proxy_server,
    _SERVICE_LOGIN_URL,
    run_cmp_crawler,
)
from network_report.collectors.iot_m2m import collect_iot_summary, load_iccid_mapping
from network_report.collectors.modem_orbit import (
    _parse_cached_data,
    _parse_native_results,
)
from network_report.collectors.mrtg import (
    evaluate_circuit,
    load_indibiz_daily_max,
    parse_capacity_to_mbps,
)
from network_report.collectors.nexthop_ping import ping_host
from network_report.collectors.orbit_scraper import _parse_quota_text
from network_report.collectors.prtg import PrtgStatus
from network_report.config import load_config
from network_report.processing.analyzer import generate_technical_analysis
from network_report.writers.wag_formatter import format_whatsapp_report


def test_config_defaults():
    cfg = load_config()
    assert cfg.schedule_time == "23:00"
    assert cfg.gowa.timeout_seconds == 20
    assert cfg.checkpoint.trac_path is not None
    assert cfg.warp.mode == "proxy"


def test_cmp_proxy_resolution(monkeypatch):
    """Explicit CMP proxy wins over WARP, WARP wins over env."""
    monkeypatch.delenv("CMP_PROXY_SERVER", raising=False)
    assert _resolve_proxy_server("http://cmp:8080", "socks5://warp:40000") == "http://cmp:8080"
    assert _resolve_proxy_server("", "socks5://warp:40000") == "socks5://warp:40000"

    monkeypatch.setenv("CMP_PROXY_SERVER", "http://env:3128")
    assert _resolve_proxy_server("", "") == "http://env:3128"


def test_firefox_user_agent_is_desktop():
    ua = _get_firefox_user_agent()
    assert "Firefox" in ua
    assert "Headless" not in ua
    assert "Windows" not in ua


class _FakePage:
    """Minimal page stub: every menu click fails, so the URL fallback runs."""

    def __init__(self):
        self.goto_calls = []

    def click(self, *args, **kwargs):
        raise RuntimeError("menu not found")

    def wait_for_timeout(self, *args, **kwargs):
        pass

    def wait_for_load_state(self, *args, **kwargs):
        raise RuntimeError("menu not found")

    def goto(self, url, **kwargs):
        self.goto_calls.append((url, kwargs))


def test_cmp_usage_query_falls_back_to_spa_hash_url():
    """CMP is a hash-routed SPA: fallback must use the #!reports fragment."""
    page = _FakePage()
    assert _navigate_to_usage_query(page, 30000) is True

    url, kwargs = page.goto_calls[-1]
    assert url == "https://ep.iotcc.telkomsel.com/#!reports"
    # Hash-only navigation never fires a load event on a new document.
    assert kwargs["wait_until"] == "commit"
    assert kwargs["timeout"] == 30000


def test_cmp_crawler_url_normalization(monkeypatch, tmp_path):
    called_urls = []

    class FakeBrowser:
        def new_context(self, *a, **kw):
            return self

        def new_page(self):
            return self

        def set_default_timeout(self, *a):
            pass

        def close(self):
            pass

    class FakePlaywright:
        class firefox:
            @staticmethod
            def launch(**kwargs):
                return FakeBrowser()

        def __enter__(self):
            return self

        def __exit__(self, *args):
            pass

    monkeypatch.setattr("network_report.collectors.cmp_crawler.sync_playwright", FakePlaywright)

    def mock_login_cmp(page, base_url, username, password, timeout, *args, **kwargs):
        called_urls.append(base_url)
        return False

    monkeypatch.setattr("network_report.collectors.cmp_crawler._login_cmp", mock_login_cmp)

    # Test plain URL normalized to SERVICE_LOGIN_URL
    run_cmp_crawler(
        base_url="https://ep.iotcc.telkomsel.com",
        username="u",
        password="p",
        target_date="2026-10-12",
        target_month="2026-10",
        output_dir=tmp_path,
        headless=True,
    )
    assert called_urls[-1] == _SERVICE_LOGIN_URL

    # Test URL already containing service= is preserved
    custom_url = "https://ep.iotcc.telkomsel.com/cas/login?service=custom"
    run_cmp_crawler(
        base_url=custom_url,
        username="u",
        password="p",
        target_date="2026-10-12",
        target_month="2026-10",
        output_dir=tmp_path,
        headless=True,
    )
    assert called_urls[-1] == custom_url


def test_mrtg_evaluation():
    assert parse_capacity_to_mbps("1 GB") == 1024.0
    assert parse_capacity_to_mbps("10 MB") == 10.0

    c1 = evaluate_circuit("Link A", "SID-1", "10 MB", max_in_mbps=11.5, max_out_mbps=2.0)
    assert c1.status == "High Traffic"
    assert c1.over_pct == 15.0

    c2 = evaluate_circuit("Link B", "SID-2", "10 MB", max_in_mbps=1.5, max_out_mbps=1.0)
    assert c2.status == "Normal"
    assert c2.utilization_pct == 15.0


def test_indibiz_query_nonexistent(tmp_path: Path):
    non_existent = tmp_path / "traffic.db"
    rx, tx = load_indibiz_daily_max(non_existent, "2026-10-12")
    assert rx is None and tx is None


def test_prtg_dataclass():
    st = PrtgStatus(up=100, paused=20, total=120)
    assert st.up == 100
    assert st.total == 120


def test_ping_host_mock():
    # Ping localhost
    res = ping_host("Localhost", "127.0.0.1", count=1, timeout_sec=1)
    assert res.name == "Localhost"
    assert res.is_up is True or res.status in ("Normal", "Warning", "Down")


def test_analyzer():
    cpu = [("SW-CORE-01", 18.5)]
    mem = [("WLC-01", 69.5)]
    c_txt, m_txt = generate_technical_analysis(cpu, mem)
    assert "SW-CORE-01" in c_txt
    assert "WLC-01" in m_txt


def test_wag_formatter():
    payload = {
        "date_str": "12 Oktober 2026",
        "iot": {
            "status": "Normal",
            "total_usage_gb": 150.0,
            "active_sessions": 30,
            "top_daily": [("SITE.1", 2.5)],
            "top_periodic": [],
        },
        "mrtg": {
            "ASTINET": [
                {
                    "name": "Link 1",
                    "sid": "SID-1",
                    "capacity_str": "10 MB",
                    "status": "Normal",
                    "utilization_pct": 10.0,
                }
            ]
        },
        "ping": [{"name": "Gateway", "ip": "10.10.1.1", "status": "Normal"}],
        "prtg": {"up": 98, "paused": 29, "total": 127},
        "cpu": [("SW-01", 15.0)],
        "memory": [("WLC-01", 68.0)],
        "uptime": {"device": 99.0, "snmp": 99.0, "icmp": 99.0, "https": "-"},
        "ap": {
            "normal": 454,
            "fault": 0,
            "users": 2500,
            "indoor_ok": 328,
            "outdoor_ok": 115,
            "outdoor_fault": 0,
            "fault_names": [],
            "ssid_distribution": {"Staff": 1000},
            "is_snapshot_1400": True,
        },
        "orbit": [],
        "highlights": {"problem_open": "N/A"},
    }
    msg = format_whatsapp_report(payload)
    assert "*MONITORING NETWORK INFRASTRUCTURE*" in msg
    assert "12 Oktober 2026" in msg
    assert "SITE.1 → 2.50 GB" in msg
    # Section D header must reflect 14:00 WIB peak hour data source
    assert "*D. 📶 AP Huawei (Data: 14.00 WIB)*" in msg


def test_wag_top_usage_headers_are_distinct():
    """A.2 (top_daily) and A.3 (top_periodic) must not share one header."""
    msg = format_whatsapp_report(
        {
            "date_str": "12 Oktober 2026",
            "iot": {
                "top_daily": [("SITE.1", 2.5)],
                "top_periodic": [("SITE.2", 40.0)],
                "periodic_range_str": "7 Days",
            },
        }
    )
    assert "2. 🔝 Top Daily Usage" in msg
    assert "3. 🔝 Top Periodic Usage" in msg
    assert "Top Utility" not in msg


def test_wag_nexthop_ping_rows_show_loss_reply_and_avg():
    msg = format_whatsapp_report(
        {
            "ping": [
                {
                    "name": "GW-A",
                    "ip": "10.10.0.1",
                    "status": "Normal",
                    "avg_latency_ms": 12.34,
                    "packet_loss_pct": 0.0,
                    "total_sent": 4,
                    "packets_received": 4,
                },
                {
                    "name": "GW-B",
                    "ip": "10.10.0.2",
                    "status": "Monitoring",
                    "avg_latency_ms": 45.5,
                    "packet_loss_pct": 25.0,
                    "total_sent": 4,
                    "packets_received": 3,
                },
                {
                    "name": "GW-C",
                    "ip": "10.10.0.3",
                    "status": "Down",
                    "avg_latency_ms": None,
                    "packet_loss_pct": 100.0,
                    "total_sent": 4,
                    "packets_received": 0,
                },
            ],
        }
    )
    assert "GW-A → ✅ Normal (IP 10.10.0.1) (Loss: 0% (4/4 Reply), Avg: 12.3 ms)" in msg
    assert "GW-B → ⚠️ Monitoring (IP 10.10.0.2) (Loss: 25% (3/4 Reply), Avg: 45.5 ms)" in msg
    assert "GW-C → ⚠️ Down (IP 10.10.0.3) (Loss: 100% (0/4 Reply), Request Timeout)" in msg


def test_wag_nexthop_ping_omits_avg_when_missing():
    msg = format_whatsapp_report(
        {
            "ping": [
                {
                    "name": "GW-A",
                    "ip": "10.10.0.1",
                    "status": "Normal",
                    "avg_latency_ms": None,
                    "packet_loss_pct": 0.0,
                    "total_sent": 4,
                    "packets_received": 4,
                }
            ],
        }
    )
    assert "GW-A → ✅ Normal (IP 10.10.0.1) (Loss: 0% (4/4 Reply))" in msg


def test_wag_orbit_lists_all_twelve_modems_with_no_package_marker():
    orbit = [
        {"location": f"Modem {i}", "packages": [{"remaining_gb": 10.5, "total_gb": 30.0, "expiry_str": "30 Oct 2026"}]}
        for i in range(1, 11)
    ]
    orbit.append({"location": "Modem 11", "packages": []})
    orbit.append({"location": "Modem 12", "packages": []})

    msg = format_whatsapp_report({"orbit": orbit})
    assert "*E. 📱 Modem Orbit*" in msg
    assert msg.count("Kuota: 10.5 GB / 30.0 GB (Exp: 30 Oct 2026)") == 10
    assert msg.count("Kuota: - (No Active Package)") == 2
    assert "11. Modem 11: Kuota: - (No Active Package)" in msg
    assert "12. Modem 12: Kuota: - (No Active Package)" in msg


def test_iot_summary_reads_raw_files_from_date_subdirs(tmp_path: Path):
    """CMP exports archived under raw/{YYYYMMDD}/ must still be picked up. Total usage is 0.0 without dashboard image."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(
        mapping_file,
        [
            ("8988234000001234567", "SITE-A"),
            ("8988234000001234568", "SITE-B"),
        ],
    )

    dated = raw_dir / "20261012"
    dated.mkdir()
    _create_daily_cmp_file(
        dated / "report_20261012_DAILY_USAGE_by_SIM.xlsx",
        "2026-10-12",
        [
            ("8988234000001234567", 1_073_741_824),  # 1 GB
            ("8988234000001234568", 536_870_912),  # 0.5 GB
        ],
    )
    _create_monthly_cmp_file(
        dated / "report_202610_MONTHLY_USAGE_by_SIM.xlsx",
        [
            ("8988234000001234567", 4_294_967_296),  # 4 GB cumulative
            ("8988234000001234568", 2_147_483_648),  # 2 GB cumulative
        ],
    )

    summary = collect_iot_summary(raw_dir, "2026-10-12", mapping_file)
    assert summary is not None
    assert summary.active_sessions == 2
    assert summary.total_usage_gb == 0.0
    assert summary.top_daily[0] == ("SITE-A", 1.0)


def test_iot_summary_falls_back_to_latest_daily_when_target_missing(tmp_path: Path, monkeypatch):
    """Today's CMP export only lands after accumulation: fall back to the previous day. Total usage is 0.0 without dashboard image."""
    from network_report.collectors import iot_m2m

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(
        mapping_file,
        [
            ("8988234000001234567", "SITE-A"),
            ("8988234000001234568", "SITE-B"),
        ],
    )

    _create_daily_cmp_file(
        raw_dir / "report_20261012_DAILY_USAGE_by_SIM.xlsx",
        "2026-10-12",
        [
            ("8988234000001234567", 1_073_741_824),  # 1 GB
            ("8988234000001234568", 536_870_912),  # 0.5 GB
        ],
    )

    # No dashboard at all: sessions must come from the latest available daily file.
    monkeypatch.setattr(iot_m2m, "extract_dashboard_metrics", lambda *_a, **_k: (None, None))

    summary = collect_iot_summary(raw_dir, "2026-10-14", mapping_file)
    assert summary is not None
    assert summary.active_sessions == 2
    assert summary.top_daily[0] == ("SITE-A", 1.0)
    assert summary.top_daily[1] == ("SITE-B", 0.5)
    # Total usage is 0.0 without dashboard image
    assert summary.total_usage_gb == 0.0


def _fake_ping(monkeypatch, loss_pct: float) -> None:
    received = 4 - round(4 * loss_pct / 100)
    output = (
        f"4 packets transmitted, {received} received, {loss_pct:g}% packet loss, time 3062ms\n"
        "rtt min/avg/max/mdev = 20.0/23.5/25.0/2.0 ms\n"
    )
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(a[0] if a else [], 1, output, ""),
    )


def test_ping_transient_loss_is_monitoring(monkeypatch):
    _fake_ping(monkeypatch, 25.0)
    res = ping_host("Hop", "10.10.0.1")
    assert res.packet_loss_pct == 25.0
    assert res.is_up is True
    assert res.status == "Monitoring"
    assert res.total_sent == 4
    assert res.packets_received == 3


def test_ping_zero_loss_is_normal(monkeypatch):
    _fake_ping(monkeypatch, 0.0)
    res = ping_host("Hop", "10.10.0.1")
    assert res.status == "Normal"
    assert res.total_sent == 4
    assert res.packets_received == 4
    assert res.avg_latency_ms == 23.5


def test_ping_defaults_are_count_4_timeout_3():
    import inspect

    sig = inspect.signature(ping_host)
    assert sig.parameters["count"].default == 4
    assert sig.parameters["timeout_sec"].default == 3


def test_ping_total_loss_is_down(monkeypatch):
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda *a, **k: subprocess.CompletedProcess(
            a[0] if a else [], 1, "4 packets transmitted, 0 received, 100% packet loss\n", ""
        ),
    )
    res = ping_host("Hop", "10.10.0.1")
    assert res.status == "Down"
    assert res.is_up is False
    assert res.total_sent == 4
    assert res.packets_received == 0


def test_ping_exception_reports_down_with_counts(monkeypatch):
    def boom(*_a, **_k):
        raise OSError("no ping binary")

    monkeypatch.setattr(subprocess, "run", boom)
    res = ping_host("Hop", "10.10.0.1")
    assert res.status == "Down"
    assert res.total_sent == 4
    assert res.packets_received == 0


def test_airwave_no_silent_fallback_to_other_day(tmp_path: Path):
    """Target day 14 has no CSVs; day 13 exists but must NOT be used."""
    day13 = tmp_path / "20261013"
    day13.mkdir(parents=True)
    (day13 / "x_top_cpu_utilization.csv").write_text("SW-01,42.5\nSW-02,10.0\n")
    (day13 / "x_top_memory_usage.csv").write_text("SW-01,60.0\n")

    data = collect_airwave_data(tmp_path, "20261014")
    assert data is None


def test_airwave_returns_none_when_month_empty(tmp_path: Path):
    """No CSVs anywhere -> None, not a crash or an empty report."""
    (tmp_path / "20261014").mkdir(parents=True)
    assert collect_airwave_data(tmp_path, "20261014") is None


def test_orbit_quota_parsing():
    # Test "Sisa Kuota X GB dari Y GB"
    rem, tot, exp, _days = _parse_quota_text("Sisa Kuota 200 GB dari 200 GB")
    assert rem == 200.0
    assert tot == 200.0

    # Test "Sisa X GB dari Y GB"
    rem, tot, exp, _days = _parse_quota_text("Sisa 28.56 GB dari 30 GB")
    assert rem == 28.56
    assert tot == 30.0

    # Test "Kuota: X GB / Y GB"
    rem, tot, exp, _days = _parse_quota_text("Kuota: 200.0 GB / 200.0 GB")
    assert rem == 200.0
    assert tot == 200.0

    # Test "X GB / Y GB"
    rem, tot, exp, _days = _parse_quota_text("25.0 GB / 25.0 GB")
    assert rem == 25.0
    assert tot == 25.0

    # Test with expiry
    rem, tot, exp, _days = _parse_quota_text(
        "Sisa Kuota 200 GB dari 200 GB\nBerlaku s.d. 30 Oct 2026"
    )
    assert rem == 200.0
    assert tot == 200.0

    # Test with Indonesian format "Berlaku s.d 30 Oct 2026" (no dot after d)
    rem, tot, exp, _days = _parse_quota_text("Sisa 10 GB dari 20 GB\nBerlaku s.d 01 Nov 2026")
    assert rem == 10.0
    assert tot == 20.0
    assert "01 Nov 2026" in exp


def _create_daily_cmp_file(path: Path, date_iso: str, data: list[tuple[str, float]]) -> None:
    """Create a CMP daily usage Excel file for testing."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Date", "ICCID", "UsageBytes"])
    for iccid, usage_bytes in data:
        ws.append([date_iso, iccid, int(usage_bytes)])
    wb.save(path)


def _create_monthly_cmp_file(path: Path, data: list[tuple[str, float]]) -> None:
    """Create a CMP monthly usage Excel file for testing."""
    wb = Workbook()
    ws = wb.active
    ws.append(["Date", "ICCID", "UsageBytes"])
    # Monthly file has cumulative data, date can be the last day of month
    for iccid, usage_bytes in data:
        ws.append(["2026-10-12", iccid, int(usage_bytes)])
    wb.save(path)


def _create_iccid_mapping_file(path: Path, data: list[tuple[str, str]]) -> None:
    """Create ICCID to location mapping Excel file."""
    wb = Workbook()
    ws = wb.active
    ws.append(["ICCID", "Location"])
    for iccid, loc in data:
        ws.append([iccid, loc])
    wb.save(path)


def test_iot_summary_daily_only(tmp_path: Path):
    """Test IoT summary with only daily file (no monthly, no dashboard image). Total usage must be 0.0 without Excel fallback."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    # ICCID mapping
    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(
        mapping_file,
        [
            ("8988234000001234567", "SITE-A"),
            ("8988234000001234568", "SITE-B"),
            ("8988234000001234569", "SITE-C"),
        ],
    )

    # Daily file for 2026-10-12
    daily_file = raw_dir / "report_20261012_DAILY_USAGE_by_SIM.xlsx"
    _create_daily_cmp_file(
        daily_file,
        "2026-10-12",
        [
            ("8988234000001234567", 1_073_741_824),  # 1 GB
            ("8988234000001234568", 536_870_912),  # 0.5 GB
            ("8988234000001234569", 2_147_483_648),  # 2 GB
        ],
    )

    summary = collect_iot_summary(raw_dir, "2026-10-12", mapping_file)
    assert summary is not None
    assert summary.active_sessions == 3
    # Total usage must be 0.0 since excel fallback is removed and no dashboard image
    assert summary.total_usage_gb == 0.0
    assert len(summary.top_daily) == 3
    assert summary.top_daily[0] == ("SITE-C", 2.0)
    assert summary.top_daily[1] == ("SITE-A", 1.0)
    assert summary.top_daily[2] == ("SITE-B", 0.5)
    assert summary.top_periodic[0] == ("SITE-C", 2.0)


def test_iot_summary_with_monthly(tmp_path: Path):
    """Test IoT summary with both daily and monthly files. Total usage is 0.0 without dashboard image."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(
        mapping_file,
        [
            ("8988234000001234567", "SITE-A"),
            ("8988234000001234568", "SITE-B"),
            ("8988234000001234569", "SITE-C"),
        ],
    )

    # Daily file for 2026-10-12
    daily_file = raw_dir / "report_20261012_DAILY_USAGE_by_SIM.xlsx"
    _create_daily_cmp_file(
        daily_file,
        "2026-10-12",
        [
            ("8988234000001234567", 1_073_741_824),  # 1 GB
            ("8988234000001234568", 536_870_912),  # 0.5 GB
        ],
    )

    # Monthly file (cumulative MTD) - higher values than daily
    monthly_file = raw_dir / "report_202610_MONTHLY_USAGE_by_SIM.xlsx"
    _create_monthly_cmp_file(
        monthly_file,
        [
            ("8988234000001234567", 5_368_709_120),  # 5 GB cumulative
            ("8988234000001234568", 3_221_225_472),  # 3 GB cumulative
            ("8988234000001234569", 10_737_418_240),  # 10 GB cumulative
        ],
    )

    summary = collect_iot_summary(raw_dir, "2026-10-12", mapping_file)
    assert summary is not None
    # Active sessions from daily file
    assert summary.active_sessions == 2
    # Top daily from daily file
    assert summary.top_daily[0] == ("SITE-A", 1.0)
    assert summary.top_daily[1] == ("SITE-B", 0.5)
    # Total usage is 0.0 without dashboard image
    assert summary.total_usage_gb == 0.0
    assert summary.top_periodic[0] == ("SITE-C", 10.0)
    assert summary.top_periodic[1] == ("SITE-A", 5.0)
    assert summary.top_periodic[2] == ("SITE-B", 3.0)


def test_iot_summary_multiple_daily_files_accumulation(tmp_path: Path):
    """Test accumulation when monthly file missing; total_usage_gb remains 0.0 without dashboard image."""
    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()

    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(
        mapping_file,
        [
            ("8988234000001234567", "SITE-A"),
            ("8988234000001234568", "SITE-B"),
        ],
    )

    # Multiple daily files in the month
    daily_file1 = raw_dir / "report_20261001_DAILY_USAGE_by_SIM.xlsx"
    _create_daily_cmp_file(
        daily_file1,
        "2026-10-01",
        [
            ("8988234000001234567", 1_073_741_824),  # 1 GB
            ("8988234000001234568", 536_870_912),  # 0.5 GB
        ],
    )

    daily_file2 = raw_dir / "report_20261002_DAILY_USAGE_by_SIM.xlsx"
    _create_daily_cmp_file(
        daily_file2,
        "2026-10-02",
        [
            ("8988234000001234567", 2_147_483_648),  # 2 GB
            ("8988234000001234568", 1_073_741_824),  # 1 GB
        ],
    )

    daily_file3 = raw_dir / "report_20261012_DAILY_USAGE_by_SIM.xlsx"
    _create_daily_cmp_file(
        daily_file3,
        "2026-10-12",
        [
            ("8988234000001234567", 536_870_912),  # 0.5 GB
            ("8988234000001234568", 2_147_483_648),  # 2 GB
        ],
    )

    summary = collect_iot_summary(raw_dir, "2026-10-12", mapping_file)
    assert summary is not None
    # Active sessions from target date daily file
    assert summary.active_sessions == 2
    # Top daily from target date (2026-10-12)
    assert summary.top_daily[0] == ("SITE-B", 2.0)
    assert summary.top_daily[1] == ("SITE-A", 0.5)
    # Total usage must be 0.0 since excel fallback is removed
    assert summary.total_usage_gb == 0.0
    assert summary.top_periodic[0][0] in ("SITE-A", "SITE-B")
    assert summary.top_periodic[0][1] == 3.5


def test_load_iccid_mapping(tmp_path: Path):
    """Test ICCID mapping loading."""
    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(
        mapping_file,
        [
            ("8988234000001234567", "SITE-A"),
            ("8988234000001234568", "SITE-B"),
        ],
    )

    mapping = load_iccid_mapping(mapping_file)
    assert mapping["8988234000001234567"] == "SITE-A"
    assert mapping["8988234000001234568"] == "SITE-B"
    assert len(mapping) == 2


# --- Dashboard screenshot OCR (Total Usage / User Session) ---

_DASHBOARD_OCR_TEXT = """IoT M2M Dashboard
Report Period: 01-12 (Month to Date)
Total Usage
221.88 GB
Active SIM
1.204
User Session
37
"""


def _write_dashboard_image(directory: Path, name: str = "dashboard_20261012.png") -> Path:
    """Write a placeholder dashboard screenshot file (contents never OCR'd in tests)."""
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    path.write_bytes(b"\x89PNG\r\n\x1a\n")
    return path


def _write_real_dashboard_png(directory: Path, name: str = "dashboard_20261012.png") -> Path:
    """Write a real (blank) PNG so the PaddleOCR crop path can actually open it."""
    pil_image = pytest.importorskip("PIL.Image")
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / name
    pil_image.new("RGB", (800, 400), "white").save(path)
    return path


def _stub_paddle_subprocess(monkeypatch, texts: list[str]) -> None:
    """PaddleOCR runs out-of-process; stub the venv call and return `texts` as its stdout."""
    from network_report.collectors import iot_m2m

    monkeypatch.setattr(
        iot_m2m, "_paddle_interpreters", lambda: [Path("C:/fake/.venv312/Scripts/python.exe")]
    )

    def _fake_run(cmd, **_kwargs):
        Path(cmd[-1]).write_bytes(b"crop")
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(texts) + "\n", stderr="")

    monkeypatch.setattr(iot_m2m.subprocess, "run", _fake_run)


def _stub_gemini_disabled(monkeypatch) -> None:
    """Silence the Gemini fallback so Paddle-only tests never touch the network."""
    from network_report.collectors import iot_m2m

    monkeypatch.setattr(iot_m2m, "_read_image_text_gemini", lambda _p: "")


def test_read_image_text_uses_paddleocr(tmp_path: Path, monkeypatch):
    """PaddleOCR reads the Total Usage card and it overrides the total."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    monkeypatch.setattr(
        iot_m2m, "_read_image_text_gemini", lambda _p: pytest.fail("Gemini must stay unused")
    )
    _stub_paddle_subprocess(monkeypatch, ["Total Usage", "221.88 GB"])

    assert iot_m2m._read_image_text(img) == "Total Usage\n221.88 GB"
    assert iot_m2m.extract_dashboard_metrics(img) == (221.88, None)


def test_read_image_text_skips_gemini_when_paddle_succeeds(tmp_path: Path, monkeypatch):
    """Gemini is the fallback only; a PaddleOCR hit means no API call at all."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    monkeypatch.setattr(
        iot_m2m, "_read_image_text_gemini", lambda _p: pytest.fail("Gemini must stay unused")
    )
    _stub_paddle_subprocess(monkeypatch, ["Total Usage", "9 GB"])

    assert iot_m2m._read_image_text(img) == "Total Usage\n9 GB"


def test_read_image_text_paddle_subprocess_fallback(tmp_path: Path, monkeypatch):
    """PaddleOCR runs from a venv interpreter via subprocess with a cropped tile."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_gemini_disabled(monkeypatch)
    seen: dict[str, object] = {}

    def _fake_run(cmd, **kwargs):
        seen["cmd"] = list(cmd)
        Path(cmd[-1]).write_bytes(b"crop")
        return subprocess.CompletedProcess(cmd, 0, stdout="Total Usage\n221.88 GB\n", stderr="")

    monkeypatch.setattr(iot_m2m.subprocess, "run", _fake_run)
    monkeypatch.setattr(
        iot_m2m, "_paddle_interpreters", lambda: [Path("C:/fake/.venv312/Scripts/python.exe")]
    )

    assert iot_m2m._read_image_text(img) == "Total Usage\n221.88 GB"
    assert seen["cmd"][1] == "-c" and Path(seen["cmd"][-2]) == img
    assert "paddleocr" in seen["cmd"][2]


def test_read_image_text_empty_when_no_ocr_backend(tmp_path: Path, monkeypatch):
    """OCR stays best-effort: no PaddleOCR and no Gemini key means empty text, not a raise."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    monkeypatch.setattr(iot_m2m, "_paddle_interpreters", list)
    _stub_gemini_disabled(monkeypatch)

    assert iot_m2m._read_image_text(img) == ""
    assert iot_m2m.extract_dashboard_metrics(img) == (None, None)


def test_paddle_text_ignores_other_tiles_in_the_crop(tmp_path: Path, monkeypatch):
    """Only the Total Usage tile may override metrics; Active SIM must not fake sessions."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_paddle_subprocess(monkeypatch, ["Total Usage", "221.88 GB", "Active SIM", "1.204"])

    assert iot_m2m._read_image_text(img) == "Total Usage\n221.88 GB"
    assert iot_m2m.extract_dashboard_metrics(img) == (221.88, None)


def test_paddle_text_keeps_value_on_the_label_line(tmp_path: Path, monkeypatch):
    """OCR that glues the label and value on one line still yields the value."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_paddle_subprocess(monkeypatch, ["Total Usage 218,75 GB"])

    assert iot_m2m.extract_dashboard_metrics(img) == (218.75, None)


def test_paddle_text_accepts_billing_cycle_tile_without_usage_label(tmp_path: Path, monkeypatch):
    """The CMP tile is headed 'Billing Cycle'; that wording alone keeps the TB/GB/MB value."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_paddle_subprocess(monkeypatch, ["Billing Cycle", "221.88 GB", "Active SIM", "1.204"])

    assert iot_m2m._read_image_text(img) == "Total Usage\n221.88 GB"
    assert iot_m2m.extract_dashboard_metrics(img) == (221.88, None)


def test_paddle_text_accepts_sim_cards_tile_header(tmp_path: Path, monkeypatch):
    """The CMP tile header reads 'SIM Cards Data Usage', so that label must be recognised."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_paddle_subprocess(
        monkeypatch, ["SIM Cards Data Usage", "221.88 GB", "Active SIM", "1.204"]
    )

    assert iot_m2m._read_image_text(img) == "Total Usage\n221.88 GB"
    assert iot_m2m.extract_dashboard_metrics(img) == (221.88, None)


def test_paddle_text_never_imports_paddleocr_in_process(tmp_path: Path, monkeypatch, caplog):
    """paddlepaddle is absent here, so OCR must stay subprocess-only and log no traceback."""
    import logging

    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)

    def _forbidden_import(name, *_a, **_k):
        if name.split(".")[0] in {"paddle", "paddleocr"}:
            pytest.fail("PaddleOCR must not be imported in-process")
        return original_import(name, *_a, **_k)

    original_import = builtins.__import__
    monkeypatch.setattr(iot_m2m, "_paddle_interpreters", list)  # no venv interpreter available
    _stub_gemini_disabled(monkeypatch)
    monkeypatch.setattr(builtins, "__import__", _forbidden_import)

    with caplog.at_level(logging.DEBUG):
        assert iot_m2m._read_image_text(img) == ""

    assert [r.name for r in caplog.records if r.levelno >= logging.WARNING] == []
    assert not any(r.exc_info for r in caplog.records)


def test_paddle_text_survives_non_visual_tile_order(tmp_path: Path, monkeypatch):
    """PaddleOCR may return tiles bottom-up; the Total Usage value is still isolated."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_paddle_subprocess(monkeypatch, ["Active SIM 1.204", "221.88 GB", "Total Usage"])

    assert iot_m2m._read_image_text(img) == "Total Usage\n221.88 GB"
    assert iot_m2m.extract_dashboard_metrics(img) == (221.88, None)


def test_paddle_text_drops_unattributable_garbage(tmp_path: Path, monkeypatch):
    """OCR noise without any TB/GB/MB value is discarded, never parsed as a metric."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_paddle_subprocess(monkeypatch, ["age", "ctive SIM 1.204"])
    _stub_gemini_disabled(monkeypatch)

    assert iot_m2m._read_image_text(img) == ""
    assert iot_m2m.extract_dashboard_metrics(img) == (None, None)


# --- Gemini Vision OCR fallback ---


class _FakeResponse:
    """Minimal requests.Response stand-in."""

    def __init__(self, status_code: int, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload


def _gemini_payload(text: str) -> dict:
    return {"candidates": [{"content": {"parts": [{"text": text}]}}]}


def _stub_gemini(monkeypatch, models: list[str], responses: list) -> None:
    """Point the Gemini fallback at fake models and a scripted list of responses/errors."""
    from network_report.collectors import iot_m2m

    monkeypatch.setattr(iot_m2m, "_gemini_credentials", lambda: ("test-key", models))

    calls: list[dict] = []

    def _fake_post(url, **kwargs):
        calls.append({"url": url, **kwargs})
        outcome = responses[min(len(calls) - 1, len(responses) - 1)]
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    monkeypatch.setattr(iot_m2m.requests, "post", _fake_post)
    monkeypatch.setattr(iot_m2m, "_gemini_calls", calls, raising=False)
    monkeypatch.setattr(iot_m2m, "_paddle_interpreters", list)  # force the Gemini path


def test_gemini_fallback_used_when_paddle_yields_nothing(tmp_path: Path, monkeypatch):
    """PaddleOCR finds no value -> Gemini Vision supplies Total Usage and User Session."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_gemini(
        monkeypatch,
        ["gemini-2.0-flash"],
        [_FakeResponse(200, _gemini_payload("Total Usage: 221.88 GB, User Session: 37"))],
    )

    assert iot_m2m.extract_dashboard_metrics(img) == (221.88, 37)


def test_gemini_sends_image_base64_with_prompt(tmp_path: Path, monkeypatch):
    """The request carries the base64 screenshot plus the exact reading prompt."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_gemini(monkeypatch, ["gemini-2.0-flash"], [_FakeResponse(200, _gemini_payload(""))])
    iot_m2m._read_image_text_gemini(img)

    call = iot_m2m._gemini_calls[0]
    parts = call["json"]["contents"][0]["parts"]
    assert parts[0]["text"] == (
        "Read the SIM Cards Data Usage total gigabytes and user session count. "
        "Return in format: Total Usage: X GB, User Session: Y"
    )
    assert parts[1]["inline_data"]["mime_type"] == "image/png"
    assert base64.b64decode(parts[1]["inline_data"]["data"]) == img.read_bytes()
    assert call["headers"]["x-goog-api-key"] == "test-key"
    assert "gemini-2.0-flash:generateContent" in call["url"]


def test_gemini_tries_models_in_order_until_one_answers(tmp_path: Path, monkeypatch):
    """An HTTP error on the first model falls through to the next configured model."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_gemini(
        monkeypatch,
        ["gemini-bad", "gemini-good"],
        [
            _FakeResponse(429),
            _FakeResponse(200, _gemini_payload("Total Usage: 221.88 GB, User Session: 37")),
        ],
    )

    assert iot_m2m.extract_dashboard_metrics(img) == (221.88, 37)
    assert "gemini-bad:generateContent" in iot_m2m._gemini_calls[0]["url"]
    assert "gemini-good:generateContent" in iot_m2m._gemini_calls[1]["url"]


def test_gemini_network_error_is_best_effort(tmp_path: Path, monkeypatch):
    """A transport failure yields no metrics instead of breaking the pipeline."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_gemini(monkeypatch, ["gemini-2.0-flash"], [iot_m2m.requests.RequestException("boom")])

    assert iot_m2m._read_image_text_gemini(img) == ""
    assert iot_m2m.extract_dashboard_metrics(img) == (None, None)


def test_gemini_skipped_without_credentials(tmp_path: Path, monkeypatch):
    """No GEMINI_API_KEY means no request at all, not an unauthenticated call."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    monkeypatch.setattr(iot_m2m, "_gemini_credentials", lambda: ("", []))

    def _forbidden_post(*_a, **_k):
        pytest.fail("Gemini must not be called without a key")

    monkeypatch.setattr(iot_m2m.requests, "post", _forbidden_post)

    assert iot_m2m._read_image_text_gemini(img) == ""


def test_gemini_credentials_prefer_environment(monkeypatch):
    """Environment wins over config; the model list is split on commas."""
    from network_report.collectors import iot_m2m

    monkeypatch.setenv("GEMINI_API_KEY", " env-key ")
    monkeypatch.setenv("GEMINI_MODELS", "gemini-a, gemini-b ,")

    assert iot_m2m._gemini_credentials() == ("env-key", ["gemini-a", "gemini-b"])


def test_gemini_credentials_fall_back_to_config(monkeypatch):
    """With no env key, the MRTG settings supply the Gemini credentials."""
    import network_report.config as nr_config
    from network_report.collectors import iot_m2m
    from network_report.config import MrtgSettings

    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODELS", raising=False)

    cfg = nr_config.load_config()
    monkeypatch.setattr(
        cfg,
        "mrtg",
        MrtgSettings(gemini_api_key=" cfg-key ", gemini_models="gemini-cfg"),
        raising=False,
    )
    monkeypatch.setattr(nr_config, "load_config", lambda: cfg)

    assert iot_m2m._gemini_credentials() == ("cfg-key", ["gemini-cfg"])


def test_gemini_unparsable_reply_yields_no_metrics(tmp_path: Path, monkeypatch):
    """A prose reply without a Total Usage value is discarded, not guessed at."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_gemini(
        monkeypatch,
        ["gemini-2.0-flash"],
        [_FakeResponse(200, _gemini_payload("The dashboard looks mostly empty to me."))],
    )

    assert iot_m2m.extract_dashboard_metrics(img) == (None, None)


def test_gemini_falls_through_when_model_returns_unparsable_text(tmp_path: Path, monkeypatch):
    """When model 1 returns 200 with unparsable text, it tries model 2 and successfully extracts metrics."""
    from network_report.collectors import iot_m2m

    img = _write_real_dashboard_png(tmp_path)
    _stub_gemini(
        monkeypatch,
        ["gemini-bad", "gemini-good"],
        [
            _FakeResponse(200, _gemini_payload("Cannot read")),
            _FakeResponse(200, _gemini_payload("Total Usage: 221.88 GB, User Session: 37")),
        ],
    )

    assert iot_m2m.extract_dashboard_metrics(img) == (221.88, 37)
    assert "gemini-bad:generateContent" in iot_m2m._gemini_calls[0]["url"]
    assert "gemini-good:generateContent" in iot_m2m._gemini_calls[1]["url"]


def test_paddle_interpreter_discovery_prefers_env_override(tmp_path: Path, monkeypatch):
    """PADDLEOCR_PYTHON wins; otherwise sibling repo venvs are probed."""
    from network_report.collectors import iot_m2m

    override = tmp_path / "python.exe"
    override.write_bytes(b"")
    monkeypatch.setenv("PADDLEOCR_PYTHON", str(override))
    monkeypatch.setattr(iot_m2m, "_sibling_repo_dirs", lambda _root: [tmp_path / "no-venv"])

    assert iot_m2m._paddle_interpreters()[0] == override

    monkeypatch.delenv("PADDLEOCR_PYTHON")
    venv_py = tmp_path / "MRTG" / ".venv312" / "Scripts" / "python.exe"
    venv_py.parent.mkdir(parents=True)
    venv_py.write_bytes(b"")
    monkeypatch.setattr(iot_m2m, "_sibling_repo_dirs", lambda _root: [tmp_path / "MRTG"])

    assert iot_m2m._paddle_interpreters() == [venv_py]


def test_find_dashboard_image_prefers_dated_match(tmp_path: Path):
    """Dashboard is found in raw/{date_compact}/ and in output/images/."""
    from network_report.collectors.iot_m2m import find_dashboard_image

    raw_dir = tmp_path / "raw"
    images_dir = tmp_path / "images"
    stale = _write_dashboard_image(images_dir, "dashboard_20261011.png")
    dated_dir = raw_dir / "20261012"
    hit = _write_dashboard_image(dated_dir)
    _write_dashboard_image(images_dir)

    assert find_dashboard_image([dated_dir, images_dir], "20261012") == hit
    assert find_dashboard_image([dated_dir], "20261012") == hit
    assert find_dashboard_image([images_dir], "20261010") is None
    assert stale.exists()


def test_extract_dashboard_metrics_parses_total_and_sessions(tmp_path: Path, monkeypatch):
    """Total Usage GB and User Session count are parsed from dashboard OCR text."""
    from network_report.collectors import iot_m2m
    from network_report.collectors.iot_m2m import extract_dashboard_metrics

    img = _write_dashboard_image(tmp_path)
    assert extract_dashboard_metrics(None) == (None, None)
    assert extract_dashboard_metrics(tmp_path / "missing.png") == (None, None)

    monkeypatch.setattr(iot_m2m, "_read_image_text", lambda p: _DASHBOARD_OCR_TEXT)

    total_gb, sessions = extract_dashboard_metrics(img)
    assert total_gb == 221.88
    assert sessions == 37

    # Indonesian decimal comma and label variants
    monkeypatch.setattr(
        iot_m2m,
        "_read_image_text",
        lambda p: "Total Data Usage 218,75 GB\nUser Session Aktif : 12",
    )
    assert extract_dashboard_metrics(img) == (218.75, 12)

    # OCR artifacts: "3/7" must not truncate to 3, "2218868 GB" keeps its digits
    monkeypatch.setattr(
        iot_m2m, "_read_image_text", lambda p: "Total Usage\n221.88 GB\nUser Session\n3/7"
    )
    assert extract_dashboard_metrics(img) == (221.88, 37)

    # Unreadable dashboard yields no override
    monkeypatch.setattr(iot_m2m, "_read_image_text", lambda p: "")
    assert extract_dashboard_metrics(img) == (None, None)


def test_iot_summary_uses_dashboard_total_usage(tmp_path: Path, monkeypatch):
    """Dashboard screenshot overrides the calculated MTD total and session count."""
    from network_report.collectors import iot_m2m

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(mapping_file, [("8988234000001234567", "SITE-A")])

    _create_daily_cmp_file(
        raw_dir / "report_20261012_DAILY_USAGE_by_SIM.xlsx",
        "2026-10-12",
        [
            ("8988234000001234567", 1_073_741_824),  # 1 GB
        ],
    )
    _create_monthly_cmp_file(
        raw_dir / "report_202610_MONTHLY_USAGE_by_SIM.xlsx",
        [
            ("8988234000001234567", 18_000_000_000),
        ],
    )
    _write_dashboard_image(raw_dir / "20261012")

    monkeypatch.setattr(iot_m2m, "_read_image_text", lambda p: _DASHBOARD_OCR_TEXT)

    summary = collect_iot_summary(raw_dir, "2026-10-12", mapping_file)
    assert summary is not None
    assert summary.total_usage_gb == 221.88
    assert summary.active_sessions == 37
    # Per-site ranking still comes from the CMP export
    assert summary.top_periodic[0][0] == "SITE-A"


def test_iot_summary_falls_back_when_dashboard_unusable(tmp_path: Path, monkeypatch):
    """Missing/unreadable dashboard yields total_usage_gb = 0.0."""
    from network_report.collectors import iot_m2m

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(
        mapping_file,
        [
            ("8988234000001234567", "SITE-A"),
            ("8988234000001234568", "SITE-B"),
        ],
    )
    _create_daily_cmp_file(
        raw_dir / "report_20261012_DAILY_USAGE_by_SIM.xlsx",
        "2026-10-12",
        [
            ("8988234000001234567", 1_073_741_824),
            ("8988234000001234568", 536_870_912),
        ],
    )
    # Dashboard present but OCR yields nothing usable
    _write_dashboard_image(raw_dir / "20261012")
    monkeypatch.setattr(iot_m2m, "_read_image_text", lambda p: "\n\n")

    summary = collect_iot_summary(raw_dir, "2026-10-12", mapping_file)
    assert summary is not None
    assert summary.total_usage_gb == 0.0
    assert summary.active_sessions == 2


def test_dashboard_sidecar_txt_removed(tmp_path: Path, monkeypatch):
    """Sidecar .txt is no longer read; image text is read exclusively via OCR."""
    from network_report.collectors.iot_m2m import extract_dashboard_metrics

    img = _write_dashboard_image(tmp_path)
    img.with_suffix(".txt").write_text("Total Usage\n218.75 GB\n", encoding="utf-8")

    # Prove that sidecar is ignored and OCR (stubbed via paddle) is invoked instead.
    _stub_paddle_subprocess(monkeypatch, ["Total Usage", "221.88 GB"])

    assert extract_dashboard_metrics(img) == (221.88, None)


def test_iot_summary_dashboard_dir_argument(tmp_path: Path, monkeypatch):
    """Explicit dashboard_dirs (e.g. output/images/) is honoured."""
    from network_report.collectors import iot_m2m

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    images_dir = tmp_path / "images"
    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(mapping_file, [("8988234000001234567", "SITE-A")])
    _create_daily_cmp_file(
        raw_dir / "report_20261012_DAILY_USAGE_by_SIM.xlsx",
        "2026-10-12",
        [
            ("8988234000001234567", 1_073_741_824),
        ],
    )
    _write_dashboard_image(images_dir)

    monkeypatch.setattr(iot_m2m, "_read_image_text", lambda p: _DASHBOARD_OCR_TEXT)

    summary = collect_iot_summary(
        raw_dir, "2026-10-12", mapping_file, dashboard_dirs=[raw_dir / "20261012", images_dir]
    )
    assert summary is not None
    assert summary.total_usage_gb == 221.88


_REPORT_NAMES_12 = [f"Modem {i}" for i in range(1, 13)]


def _cached_item(no: int, name: str, remaining: float | None) -> dict:
    packages = []
    if remaining is not None:
        packages.append(
            {
                "name": "Internet Orbit",
                "remaining_gb": remaining,
                "total_gb": 30.0,
                "expiry_str": "-",
            }
        )
    return {"target": {"no": no, "name": name, "ssid": f"ssid-{no}"}, "packages": packages}


def test_parse_cached_data_returns_all_twelve_modems():
    data = [_cached_item(no, f"Modem {no}", float(no)) for no in range(1, 13)]
    modems = _parse_cached_data(data, _REPORT_NAMES_12)

    assert len(modems) == 12
    assert [m.location for m in modems] == _REPORT_NAMES_12
    # Each modem keeps its own quota instead of the four idle units collapsing
    # into one grouped slot.
    assert [m.packages[0].remaining_gb for m in modems] == [float(n) for n in range(1, 13)]
    assert [m.ssid for m in modems] == [f"ssid-{n}" for n in range(1, 13)]


def test_parse_cached_data_keeps_modems_without_packages():
    data = [
        _cached_item(no, f"Modem {no}", None if no in (7, 12) else float(no)) for no in range(1, 13)
    ]
    modems = _parse_cached_data(data, _REPORT_NAMES_12)

    assert len(modems) == 12
    assert modems[6].packages == []
    assert modems[11].packages == []
    assert modems[0].packages[0].remaining_gb == 1.0


def test_parse_native_results_returns_all_twelve_modems():
    data = [
        {
            "location": f"Modem {no}",
            "ssid": f"ssid-{no}",
            "packages": [
                {
                    "name": "Internet Orbit",
                    "remaining_gb": float(no),
                    "total_gb": 30.0,
                    "expiry_str": "-",
                },
            ],
        }
        for no in range(1, 13)
    ]
    modems = _parse_native_results(data, _REPORT_NAMES_12)

    assert len(modems) == 12
    assert [m.location for m in modems] == _REPORT_NAMES_12
    assert [m.packages[0].remaining_gb for m in modems] == [float(n) for n in range(1, 13)]
    assert [m.ssid for m in modems] == [f"ssid-{n}" for n in range(1, 13)]


def test_parse_native_results_duplicate_names_do_not_collapse():
    """Four idle modems share a name; index-keyed grouping keeps them separate."""
    names = ["Modem Idle"] * 4 + [f"Modem {i}" for i in range(5, 13)]
    data = [
        {
            "location": name,
            "ssid": f"ssid-{i}",
            "packages": [
                {
                    "name": "Internet Orbit",
                    "remaining_gb": float(i),
                    "total_gb": 30.0,
                    "expiry_str": "-",
                },
            ],
        }
        for i, name in enumerate(names)
    ]
    modems = _parse_native_results(data, names)

    assert len(modems) == 12
    assert [m.packages[0].remaining_gb for m in modems] == [float(i) for i in range(12)]


def test_parse_cached_data_legacy_nine_slot_grouping_preserved():
    """Below 12 report names the historical idle-grouping behaviour is kept."""
    names = [f"Slot {i}" for i in range(1, 10)]
    data = [
        _cached_item(1, "Modem Alpha", 1.0),
        _cached_item(2, "Modem Bravo", 2.0),
        _cached_item(6, "Modem Idle", 6.0),
        _cached_item(7, "Modem Idle", 7.0),
        _cached_item(12, "Portable Unit", 12.0),
    ]
    modems = _parse_cached_data(data, names)

    assert len(modems) == 9
    assert modems[0].packages[0].remaining_gb == 2.0  # target 2 -> slot 1
    assert modems[2].packages[0].remaining_gb == 1.0  # target 1 -> slot 3
    assert [p.remaining_gb for p in modems[5].packages] == [6.0, 7.0]  # idles grouped
    assert modems[8].packages[0].remaining_gb == 12.0


def test_paddle_script_confidence_filtering():
    """Verify _PADDLE_SCRIPT correctly filters text by confidence threshold 0.80."""
    import sys
    import subprocess
    import tempfile
    from network_report.collectors.iot_m2m import _PADDLE_SCRIPT

    with tempfile.TemporaryDirectory() as tmp:
        img_path = Path(tmp) / "test.png"
        crop_path = Path(tmp) / "crop.png"
        # Create a dummy image
        from PIL import Image

        Image.new("RGB", (100, 100), color="white").save(img_path)

        # Mock PaddleOCR script behaviour by passing a script that mocks PaddleOCR return value
        mock_script = (
            "import sys\n"
            "from PIL import Image\n"
            "# Mock PaddleOCR result\n"
            "class MockOCR:\n"
            "    def __init__(self, *a, **kw): pass\n"
            "    def ocr(self, img):\n"
            "        return [{\n"
            "            'rec_texts': ['HighConf', 'LowConf', 'NoScore'],\n"
            "            'rec_scores': [0.95, 0.50, 1.0]\n"
            "        }]\n"
            "import types\n"
            "m = types.ModuleType('paddleocr')\n"
            "m.PaddleOCR = MockOCR\n"
            "sys.modules['paddleocr'] = m\n"
        ) + _PADDLE_SCRIPT

        script_path = Path(tmp) / "run_paddle.py"
        script_path.write_text(mock_script, encoding="utf-8")

        res = subprocess.run(
            [sys.executable, str(script_path), str(img_path), str(crop_path)],
            capture_output=True,
            text=True,
            check=False,
            stdin=subprocess.DEVNULL,
        )
        output_lines = [
            line_txt.strip() for line_txt in res.stdout.splitlines() if line_txt.strip()
        ]
        assert "HighConf" in output_lines
        assert "NoScore" in output_lines
        assert "LowConf" not in output_lines


def test_iot_summary_mtd_accumulation_sums_daily_and_ignores_missing_day(
    tmp_path: Path, monkeypatch
):
    """Month-to-date total is the sum of daily exports; missing day contributes nothing."""
    from network_report.collectors import iot_m2m, iot_mrtg

    raw_dir = tmp_path / "raw"
    raw_dir.mkdir()
    mapping_file = tmp_path / "iccid_mapping.xlsx"
    _create_iccid_mapping_file(
        mapping_file,
        [
            ("8988234000001234567", "SITE-A"),
            ("8988234000001234568", "SITE-B"),
        ],
    )

    # Day 1: A=1.0 GB, B=2.0 GB
    _create_daily_cmp_file(
        raw_dir / "report_20261001_DAILY_USAGE_by_SIM.xlsx",
        "2026-10-01",
        [
            ("8988234000001234567", 1_073_741_824),
            ("8988234000001234568", 2_147_483_648),
        ],
    )
    # Day 2: A=1.5 GB, B=0.5 GB
    _create_daily_cmp_file(
        raw_dir / "report_20261002_DAILY_USAGE_by_SIM.xlsx",
        "2026-10-02",
        [
            ("8988234000001234567", 1_610_612_736),
            ("8988234000001234568", 536_870_912),
        ],
    )
    # Day 3: Missing intentionally (contributes nothing)
    # Day 4: A=2.0 GB, B=1.0 GB
    _create_daily_cmp_file(
        raw_dir / "report_20261004_DAILY_USAGE_by_SIM.xlsx",
        "2026-10-04",
        [
            ("8988234000001234567", 2_147_483_648),
            ("8988234000001234568", 1_073_741_824),
        ],
    )

    monkeypatch.setattr(iot_m2m, "find_dashboard_image", lambda *_a, **_k: None)

    # Test via iot_m2m
    summary = iot_m2m.collect_iot_summary(raw_dir, "2026-10-04", mapping_file)
    assert summary is not None
    assert summary.periodic_range_str == "01-04 (Month to Date)"
    assert dict(summary.top_periodic) == {"SITE-A": 4.5, "SITE-B": 3.5}
    assert dict(summary.top_daily) == {"SITE-A": 2.0, "SITE-B": 1.0}

    # Test via iot_mrtg alias module
    summary_mrtg = iot_mrtg.collect_iot_summary(raw_dir, "2026-10-04", mapping_file)
    assert summary_mrtg is not None
    assert dict(summary_mrtg.top_periodic) == {"SITE-A": 4.5, "SITE-B": 3.5}


def test_wag_formatter_live_header():
    payload = {
        "date_str": "12 Oktober 2026",
        "ap": {"is_snapshot_1400": False, "normal": 1, "fault": 0, "users": 2, "indoor_ok": 1, "outdoor_ok": 0, "outdoor_fault": 0, "fault_names": [], "ssid_distribution": {}},
    }
    from network_report.writers.wag_formatter import format_whatsapp_report
    msg = format_whatsapp_report(payload)
    assert "*D. 📶 AP Huawei (Live - Data 14.00 WIB Kosong)*" in msg
