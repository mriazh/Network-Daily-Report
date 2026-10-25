"""Tests for RouterOS Telkom Indibiz native traffic collector and store."""

from __future__ import annotations

from datetime import datetime, time as dtime
from pathlib import Path
from unittest.mock import MagicMock, patch

from network_report.collectors import mrtg as mrtg_module
from network_report.collectors.mrtg import load_indibiz_daily_max
from network_report.collectors.routeros_indibiz import (
    IndibizSample,
    IndibizStore,
    RouterOSClient,
    compute_rate,
    daily_max_from_store,
    sample_once,
)
from network_report.config import AppConfig, RouterOSSettings
from network_report.pipeline import _build_mrtg_payload


def test_compute_rate():
    prev = IndibizSample(
        timestamp="2026-10-22T08:00:00",
        epoch=100.0,
        rx_bytes=1000,
        tx_bytes=2000,
        rx_bps=0.0,
        tx_bps=0.0,
        uptime="1d",
        status="UP",
    )
    # 1. With previous sample -> correct bps
    rx_bps, tx_bps = compute_rate(prev, cur_rx=11000, cur_tx=22000, cur_epoch=110.0)
    # delta_rx = 10000 bytes * 8 / 10s = 8000 bps
    # delta_tx = 20000 bytes * 8 / 10s = 16000 bps
    assert rx_bps == 8000.0
    assert tx_bps == 16000.0

    # 2. With no previous sample -> 0.0
    assert compute_rate(None, cur_rx=11000, cur_tx=22000, cur_epoch=110.0) == (0.0, 0.0)

    # 3. With counters going backwards (reboot / rollover) -> 0.0
    assert compute_rate(prev, cur_rx=500, cur_tx=22000, cur_epoch=110.0) == (0.0, 0.0)
    assert compute_rate(prev, cur_rx=11000, cur_tx=1000, cur_epoch=110.0) == (0.0, 0.0)

    # 4. Non-positive delta time -> 0.0
    assert compute_rate(prev, cur_rx=11000, cur_tx=22000, cur_epoch=100.0) == (0.0, 0.0)
    assert compute_rate(prev, cur_rx=11000, cur_tx=22000, cur_epoch=90.0) == (0.0, 0.0)


def test_indibiz_store_round_trip(tmp_path: Path):
    db_path = tmp_path / "traffic.db"
    store = IndibizStore(db_path)
    store.initialize()

    # Determine epoch for target date in local timezone
    date_iso = "2026-10-22"
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    local_tz = datetime.now().astimezone().tzinfo
    base_dt = datetime.combine(dt.date(), dtime(hour=10, minute=0)).replace(tzinfo=local_tz)
    epoch_day1 = base_dt.timestamp()

    # Another day (2026-10-21)
    epoch_prev_day = (base_dt.replace(day=21)).timestamp()

    # Sample 1: day 2026-10-22, 10:00
    s1 = IndibizSample(
        timestamp="2026-10-22T10:00:00",
        epoch=epoch_day1,
        rx_bytes=100_000,
        tx_bytes=50_000,
        rx_bps=5_000_000.0,
        tx_bps=2_000_000.0,
        uptime="1d",
        status="UP",
    )
    # Sample 2: day 2026-10-22, 14:00 (higher rx, lower tx)
    s2 = IndibizSample(
        timestamp="2026-10-22T14:00:00",
        epoch=epoch_day1 + 14400,
        rx_bytes=500_000,
        tx_bytes=100_000,
        rx_bps=12_000_000.0,
        tx_bps=1_500_000.0,
        uptime="1d04h",
        status="UP",
    )
    # Sample 3: day 2026-10-21 (very high, should be excluded from 2026-10-22)
    s3 = IndibizSample(
        timestamp="2026-10-21T10:00:00",
        epoch=epoch_prev_day,
        rx_bytes=50_000,
        tx_bytes=20_000,
        rx_bps=99_000_000.0,
        tx_bps=99_000_000.0,
        uptime="20h",
        status="UP",
    )

    store.record(s1)
    store.record(s2)
    store.record(s3)

    # daily_max for 2026-10-22
    max_rx, max_tx = store.daily_max("2026-10-22")
    assert max_rx == 12_000_000.0
    assert max_tx == 2_000_000.0

    # check latest sample
    latest = store.latest_sample()
    assert latest is not None
    assert latest.timestamp == s3.timestamp

    # convenience function
    max_rx_conv, max_tx_conv = daily_max_from_store(db_path, "2026-10-22")
    assert max_rx_conv == 12_000_000.0
    assert max_tx_conv == 2_000_000.0


def test_daily_max_empty_store(tmp_path: Path):
    db_path = tmp_path / "empty_traffic.db"
    store = IndibizStore(db_path)
    # File does not exist yet
    assert store.daily_max("2026-10-22") == (None, None)

    # Initialized but empty table
    store.initialize()
    assert store.daily_max("2026-10-22") == (None, None)
    assert store.latest_sample() is None


def test_load_indibiz_daily_max_conversion(tmp_path: Path):
    db_path = tmp_path / "traffic.db"
    store = IndibizStore(db_path)
    store.initialize()

    dt = datetime.strptime("2026-10-22", "%Y-%m-%d")
    local_tz = datetime.now().astimezone().tzinfo
    epoch_sample = (
        datetime.combine(dt.date(), dtime(hour=12, minute=0)).replace(tzinfo=local_tz).timestamp()
    )

    store.record(
        IndibizSample(
            timestamp="2026-10-22T12:00:00",
            epoch=epoch_sample,
            rx_bytes=1000,
            tx_bytes=1000,
            rx_bps=15_500_000.0,  # 15.5 Mbps
            tx_bps=7_250_000.0,  # 7.25 Mbps
            uptime="1d",
            status="UP",
        )
    )

    rx_mbps, tx_mbps = load_indibiz_daily_max(db_path, "2026-10-22")
    assert rx_mbps == 15.5
    assert tx_mbps == 7.25

    # None path or missing date
    assert load_indibiz_daily_max(None, "2026-10-22") == (None, None)
    assert load_indibiz_daily_max(db_path, "2026-10-23") == (None, None)


def test_pipeline_prefers_native_store(tmp_path: Path):
    db_path = tmp_path / "traffic.db"
    store = IndibizStore(db_path)
    store.initialize()

    date_iso = "2026-10-22"
    date_compact = "20261022"
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    local_tz = datetime.now().astimezone().tzinfo
    epoch_sample = (
        datetime.combine(dt.date(), dtime(hour=12, minute=0)).replace(tzinfo=local_tz).timestamp()
    )

    # Record native sample: 20 Mbps in, 10 Mbps out
    store.record(
        IndibizSample(
            timestamp=f"{date_iso}T12:00:00",
            epoch=epoch_sample,
            rx_bytes=1000,
            tx_bytes=1000,
            rx_bps=20_000_000.0,
            tx_bps=10_000_000.0,
            uptime="2d",
            status="UP",
        )
    )

    cfg = AppConfig()
    cfg.routeros = RouterOSSettings(db_path=db_path)
    circuits = {"indibiz": [["Indibiz Main", "12345678", "100 MB"]]}

    # Mock OCR returning different values (5 Mbps in, 2 Mbps out)
    with patch(
        "network_report.pipeline.load_mrtg_ocr_data",
        return_value={"12345678": {"in_mbps": 5.0, "out_mbps": 2.0}},
    ):
        payload = _build_mrtg_payload(circuits, cfg, date_iso, date_compact)
        indibiz = payload["Telkom Indibiz"][0]
        # Native store (20.0 Mbps max) must be preferred over OCR (5.0 Mbps)
        assert indibiz["max_mbps"] == 20.0
        assert indibiz["status"] == "Normal"

    # When native store has no data for that date, fall back to OCR
    other_date = "2026-10-25"
    other_compact = "20261025"
    with patch(
        "network_report.pipeline.load_mrtg_ocr_data",
        return_value={"12345678": {"in_mbps": 8.0, "out_mbps": 4.0}},
    ):
        payload = _build_mrtg_payload(circuits, cfg, other_date, other_compact)
        indibiz = payload["Telkom Indibiz"][0]
        assert indibiz["max_mbps"] == 8.0

    # When neither has data, status is "No Data"
    with patch("network_report.pipeline.load_mrtg_ocr_data", return_value={}):
        payload = _build_mrtg_payload(circuits, cfg, other_date, other_compact)
        indibiz = payload["Telkom Indibiz"][0]
        assert indibiz["max_mbps"] is None
        assert indibiz["status"] == "No Data"


def test_sibling_traffic_db_reader_removed():
    assert not hasattr(mrtg_module, "query_indibiz_traffic_db")


def test_routeros_client_and_sample_once(tmp_path: Path):
    db_path = tmp_path / "test_sample_once.db"
    store = IndibizStore(db_path)

    client = RouterOSClient(
        host="fake.host",
        username="mrtg",
        password="secret",
        port=5336,
    )

    mock_pool = MagicMock()
    mock_api = MagicMock()
    mock_iface_res = MagicMock()
    mock_sys_res = MagicMock()

    mock_pool.get_api.return_value = mock_api
    mock_api.get_resource.side_effect = lambda path: (
        mock_iface_res if path == "/interface" else mock_sys_res
    )
    mock_iface_res.get.return_value = [
        {"name": "WAN", "rx-byte": "1000000", "tx-byte": "500000", "running": "true"}
    ]
    mock_sys_res.get.return_value = [{"uptime": "4w2d"}]

    client._pool = mock_pool

    sample = sample_once(client, store, "WAN")
    assert sample.rx_bytes == 1000000
    assert sample.tx_bytes == 500000
    assert sample.uptime == "4w2d"
    assert sample.status == "UP"
    # First sample baseline -> 0.0 bps
    assert sample.rx_bps == 0.0
    assert sample.tx_bps == 0.0

    client.close()
    mock_pool.disconnect.assert_called_once()
    assert client._pool is None


def test_cli_sample_indibiz(tmp_path: Path, capsys):
    from network_report.cli import main

    db_path = tmp_path / "traffic.db"
    store = IndibizStore(db_path)
    store.initialize()

    date_iso = "2026-10-22"
    dt = datetime.strptime(date_iso, "%Y-%m-%d")
    local_tz = datetime.now().astimezone().tzinfo
    epoch_sample = (
        datetime.combine(dt.date(), dtime(hour=12, minute=0)).replace(tzinfo=local_tz).timestamp()
    )
    store.record(
        IndibizSample(
            timestamp=f"{date_iso}T12:00:00",
            epoch=epoch_sample,
            rx_bytes=1000,
            tx_bytes=1000,
            rx_bps=20_000_000.0,
            tx_bps=10_000_000.0,
            uptime="1d",
            status="UP",
        )
    )

    with patch("network_report.cli.load_config") as mock_cfg:
        cfg = AppConfig()
        cfg.routeros = RouterOSSettings(db_path=db_path, host="fake.host")
        mock_cfg.return_value = cfg

        # 1. Test --date
        code = main(["sample-indibiz", "--date", date_iso])
        assert code == 0
        captured = capsys.readouterr()
        assert "20.00 Mbps" in captured.out
        assert "10.00 Mbps" in captured.out

        # 2. Test --once with mocked sample_once
        with patch("network_report.collectors.routeros_indibiz.sample_once") as mock_sample:
            mock_sample.return_value = IndibizSample(
                timestamp="2026-10-22T12:00:00",
                epoch=epoch_sample,
                rx_bytes=1000,
                tx_bytes=1000,
                rx_bps=20_000_000.0,
                tx_bps=10_000_000.0,
                uptime="1d",
                status="UP",
            )
            code = main(["sample-indibiz", "--once"])
            assert code == 0
            captured = capsys.readouterr()
            assert "Sample recorded" in captured.out
