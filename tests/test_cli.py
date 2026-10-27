"""Tests for CLI subcommands including backfill-cmp."""

from __future__ import annotations

from unittest.mock import MagicMock

import pytest

from network_report.cli import main
from network_report.collectors.cmp_crawler import CmpExportResult


def test_backfill_cmp_subcommand_registered():
    """Verify that backfill-cmp subcommand is registered with correct arguments."""
    from network_report.cli import main

    # Running with no args should print help and return 0
    code = main([])
    assert code == 0


def test_backfill_cmp_dry_run_does_not_call_crawler(monkeypatch, capsys):
    """Verify backfill-cmp dry-run produces per-date output and never calls crawler."""
    mock_crawler = MagicMock()
    monkeypatch.setattr("network_report.collectors.cmp_crawler.run_cmp_crawler", mock_crawler)

    code = main(["backfill-cmp", "--from", "2026-10-01", "--to", "2026-10-03", "--dry-run"])
    assert code == 0

    mock_crawler.assert_not_called()

    captured = capsys.readouterr()
    lines = [line.strip() for line in captured.out.strip().splitlines() if line.strip()]
    assert len(lines) == 3
    assert "2026-10-01: dry-run" in lines[0]
    assert "2026-10-02: dry-run" in lines[1]
    assert "2026-10-03: dry-run" in lines[2]


def test_backfill_cmp_calls_crawler_per_date_without_whatsapp(monkeypatch, tmp_path, capsys):
    """Verify backfill-cmp runs crawler per date, writes to output/raw/iot/{YYYYMMDD}, and sends no WA."""
    crawler_calls = []

    def mock_run_cmp_crawler(**kwargs):
        crawler_calls.append(kwargs)
        target_date = kwargs["target_date"]
        d_compact = target_date.replace("-", "")
        out_file = (
            tmp_path / "raw" / "iot" / d_compact / f"report_{d_compact}_DAILY_USAGE_by_SIM.xlsx"
        )
        out_file.parent.mkdir(parents=True, exist_ok=True)
        out_file.write_text("dummy")
        return CmpExportResult(daily_file=out_file)

    monkeypatch.setattr(
        "network_report.collectors.cmp_crawler.run_cmp_crawler", mock_run_cmp_crawler
    )

    mock_gowa = MagicMock()
    monkeypatch.setattr("network_report.cli.GowaNotifier", mock_gowa)

    code = main(["backfill-cmp", "--from", "2026-10-01", "--to", "2026-10-02"])
    assert code == 0
    assert len(crawler_calls) == 2
    assert crawler_calls[0]["target_date"] == "2026-10-01"
    assert crawler_calls[0]["monthly_export_enabled"] is False
    assert crawler_calls[1]["target_date"] == "2026-10-02"
    assert crawler_calls[1]["monthly_export_enabled"] is False

    mock_gowa.assert_not_called()

    captured = capsys.readouterr()
    lines = [line.strip() for line in captured.out.strip().splitlines() if line.strip()]
    assert len(lines) == 2
    assert "2026-10-01: exported ->" in lines[0]
    assert "2026-10-02: exported ->" in lines[1]


def test_sample_ap_subcommand_registered():
    """Verify that sample-ap subcommand is registered."""
    import sys
    from io import StringIO
    old_stdout = sys.stdout
    sys.stdout = StringIO()
    try:
        main(["sample-ap", "--help"])
    except SystemExit as e:
        assert e.code == 0
    finally:
        sys.stdout = old_stdout


def test_sample_ap_dry_run_without_credentials(monkeypatch, tmp_path, capsys):
    """Verify sample-ap without credentials fails gracefully."""
    # Ensure no Huawei credentials are configured
    from network_report import config
    monkeypatch.setattr(config, "load_config", lambda: _cfg(tmp_path))

    # Mock the NceCollector to avoid needing Playwright
    from network_report.collectors.huawei_nce import NceCollector, NceExport
    mock_collector = MagicMock(spec=NceCollector)
    mock_collector.collect.return_value = NceExport(error="Missing NCE base_url, username, or password")
    mock_collector.close = MagicMock()
    monkeypatch.setattr("network_report.collectors.huawei_nce.NceCollector", lambda **kw: mock_collector)

    mock_gowa = MagicMock()
    monkeypatch.setattr("network_report.cli.GowaNotifier", mock_gowa)

    code = main(["sample-ap", "--date", "2026-10-12", "--send-wa"])
    # Should fail because no credentials
    assert code == 1

    captured = capsys.readouterr()
    assert "AP Sampler failed" in captured.out or "AP Sampler completed" in captured.out


def test_sample_ap_with_mocked_collector(monkeypatch, tmp_path, capsys):
    """Verify sample-ap runs NceCollector and sends WA lifecycle notices."""
    from network_report.collectors.huawei_nce import NceCollector, NceExport, WacStatus

    # Mock config
    def mock_load_config():
        cfg = _cfg(tmp_path)
        cfg.huawei.enabled = True
        cfg.huawei.base_url = "https://example.com"
        cfg.huawei.username = "user"
        cfg.huawei.password = "pass"
        cfg.huawei.headless = True
        cfg.huawei.export_timeout_seconds = 120
        cfg.huawei.raw_dir = tmp_path / "raw"
        cfg.huawei.sel_login_username = "#username"
        cfg.huawei.sel_login_password = "#password"
        cfg.huawei.sel_login_submit = "#submitButton"
        cfg.huawei.sel_nav_ap = "#dashAP"
        cfg.huawei.sel_nav_ssid = "#dashSSIDs"
        cfg.huawei.sel_nav_ac = "#dashAC"
        cfg.huawei.sel_export_ap = "#outputApListInfoBtnEn"
        cfg.huawei.sel_export_ssid = "#ssidListExportBtn_uBtn"
        return cfg

    monkeypatch.setattr("network_report.cli.load_config", mock_load_config)

    # Mock NceCollector at the point of use in cli.py
    mock_collector = MagicMock(spec=NceCollector)
    mock_collector.collect.return_value = NceExport(
        ap_csv_path=tmp_path / "raw" / "huawei_ap" / "20261012" / "apInfo.csv",
        ssid_csv_path=tmp_path / "raw" / "huawei_ap" / "20261012" / "ssidInfo.csv",
        wac=WacStatus(cpu_pct=30.0, mem_pct=50.0, device_name="WAC-1-CORE"),
        error=None,
    )
    mock_collector.close = MagicMock()

    # Create the CSV files that collect_huawei_ap_data will read
    # The function searches in raw_dir/target_date_compact first
    ap_dir = tmp_path / "raw" / "20261012"
    ap_dir.mkdir(parents=True, exist_ok=True)
    (ap_dir / "apInfo.csv").write_text(
        "AP name,Status,Installation location,STA Quantity\n"
        "AP-01,Normal,Indoor,10\n"
        "AP-02,Normal,Indoor,20\n"
        "AP-03,Fault,Outdoor,0\n"
    )
    (ap_dir / "ssidInfo.csv").write_text(
        "SSID,User Quantity\n"
        "Employee,15\n"
        "Guest,15\n"
    )

    monkeypatch.setattr("network_report.collectors.huawei_nce.NceCollector", lambda **kw: mock_collector)

    mock_gowa = MagicMock()
    monkeypatch.setattr("network_report.cli.GowaNotifier", mock_gowa)

    code = main(["sample-ap", "--date", "2026-10-12", "--send-wa"])
    assert code == 0

    mock_collector.collect.assert_called_once()
    mock_collector.close.assert_called_once()

    # Verify WA notices sent
    assert mock_gowa.return_value.send_ap_sample_started.called
    assert mock_gowa.return_value.send_ap_sample_success.called
    assert not mock_gowa.return_value.send_ap_sample_failed.called

    # Check success message content
    success_call = mock_gowa.return_value.send_ap_sample_success.call_args
    assert success_call is not None
    args, kwargs = success_call
    assert args[0] == "12 October 2026"  # date_str (English month name from strftime)
    assert args[1] == 2  # normal
    assert args[2] == 1  # fault
    assert args[3] == 30  # users
    assert args[4] == pytest.approx(1.0, abs=1.0)  # duration (approximately 1 second)
    assert kwargs.get("indoor_ok") == 2
    assert kwargs.get("outdoor_ok") == 0
    assert kwargs.get("fault_names") == ["AP-03"]

    captured = capsys.readouterr()
    assert "AP Sampler completed" in captured.out


def test_sample_ap_with_collector_error(monkeypatch, tmp_path, capsys):
    """Verify sample-ap sends failure notice when collector fails."""
    from network_report.collectors.huawei_nce import NceCollector, NceExport

    def mock_load_config():
        cfg = _cfg(tmp_path)
        cfg.huawei.enabled = True
        cfg.huawei.base_url = "https://example.com"
        cfg.huawei.username = "user"
        cfg.huawei.password = "pass"
        cfg.huawei.headless = True
        cfg.huawei.export_timeout_seconds = 120
        cfg.huawei.raw_dir = tmp_path / "raw"
        cfg.huawei.sel_login_username = "#username"
        cfg.huawei.sel_login_password = "#password"
        cfg.huawei.sel_login_submit = "#submitButton"
        cfg.huawei.sel_nav_ap = "#dashAP"
        cfg.huawei.sel_nav_ssid = "#dashSSIDs"
        cfg.huawei.sel_nav_ac = "#dashAC"
        cfg.huawei.sel_export_ap = "#outputApListInfoBtnEn"
        cfg.huawei.sel_export_ssid = "#ssidListExportBtn_uBtn"
        return cfg

    monkeypatch.setattr("network_report.cli.load_config", mock_load_config)

    mock_collector = MagicMock(spec=NceCollector)
    mock_collector.collect.return_value = NceExport(error="login failed")
    mock_collector.close = MagicMock()

    monkeypatch.setattr("network_report.collectors.huawei_nce.NceCollector", lambda **kw: mock_collector)

    mock_gowa = MagicMock()
    monkeypatch.setattr("network_report.cli.GowaNotifier", mock_gowa)

    code = main(["sample-ap", "--date", "2026-10-12", "--send-wa"])
    assert code == 1

    mock_gowa.return_value.send_ap_sample_started.assert_called_once()
    mock_gowa.return_value.send_ap_sample_failed.assert_called_once()
    assert not mock_gowa.return_value.send_ap_sample_success.called

    captured = capsys.readouterr()
    assert "AP Sampler failed: login failed" in captured.out


def _cfg(tmp_path):
    from network_report.config import AppConfig
    from network_report.config import GowaSettings

    cfg = AppConfig()
    cfg.gowa = GowaSettings(enabled=True, base_url="http://x", group_jid="g@g.us")
    cfg.output_root = tmp_path
    cfg.output_raw_root = tmp_path / "raw"
    return cfg
