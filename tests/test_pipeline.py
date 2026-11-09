"""Unit tests for pipeline raw-artifact archiving."""

from pathlib import Path
from unittest.mock import MagicMock

from network_report.config import AppConfig, CmpSettings
from network_report.collectors.netcare_mrtg import MrtgSample
from network_report.pipeline import _archive_raw_artifacts


def test_archive_iot_dashboard_screenshot(tmp_path: Path):
    """Dashboard screenshot from output/images/ lands in output/raw/iot/{date}/."""
    output_root = tmp_path / "output"
    images_dir = output_root / "images"
    images_dir.mkdir(parents=True)
    cmp_raw = tmp_path / "cmp_raw"
    cmp_raw.mkdir()

    (images_dir / "dashboard_20261012.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (images_dir / "dashboard_20261011.png").write_bytes(b"\x89PNG\r\n\x1a\n")
    (cmp_raw / "report_20261012_DAILY_USAGE_by_SIM.csv").write_text("a,b\n", encoding="utf-8")

    cfg = AppConfig(
        output_root=output_root,
        output_raw_root=output_root / "raw",
        cmp=CmpSettings(raw_dir=cmp_raw),
    )

    _archive_raw_artifacts(
        cfg=cfg,
        date_compact="20261012",
        ping_results=[],
        prtg_status=None,
        orbit_modems=[],
        mrtg_payload={},
    )

    iot_dest = output_root / "raw" / "iot" / "20261012"
    assert (iot_dest / "dashboard_20261012.png").exists()
    assert (iot_dest / "report_20261012_DAILY_USAGE_by_SIM.csv").exists()
    # Stale-day screenshots are not pulled in
    assert not (iot_dest / "dashboard_20261011.png").exists()


def _base_cfg(tmp_path):
    from network_report.config import AppConfig, GowaSettings, NetcareSettings

    cfg = AppConfig()
    cfg.output_root = tmp_path
    cfg.output_raw_root = tmp_path / "raw"
    cfg.mrtg.telkomcare_dir = tmp_path / "raw" / "mrtg"
    cfg.gowa = GowaSettings(enabled=True, base_url="http://x", group_jid="g@g.us")
    cfg.netcare = NetcareSettings(
        enabled=True,
        base_url="https://telkomcare.example.internal",
        username="u",
        password="p",
        captcha_enabled=False,
        captcha_api_keys="",
        captcha_models="gemini-3.5-flash-lite",
        headless=True,
        workers=1,
    )
    cfg.cmp.enabled = False
    cfg.prtg.enabled = False
    cfg.airwave.enabled = False
    cfg.huawei.enabled = False
    cfg.orbit.enabled = False
    return cfg


def _stub_collectors(monkeypatch, pipeline_mod):
    monkeypatch.setattr(pipeline_mod, "collect_nexthop_ping", lambda: [])
    monkeypatch.setattr(pipeline_mod, "collect_iot_summary", lambda **kw: None)
    monkeypatch.setattr(pipeline_mod, "fetch_prtg_status", lambda **kw: None)
    monkeypatch.setattr(pipeline_mod, "collect_airwave_data", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_mod, "collect_huawei_ap_data", lambda *a, **k: None)
    monkeypatch.setattr(pipeline_mod, "collect_orbit_modems", lambda **kw: [])
    monkeypatch.setattr(
        pipeline_mod, "generate_technical_analysis", lambda cpu, mem: ("normal", "stable")
    )
    monkeypatch.setattr(pipeline_mod, "write_daily_excel", lambda *a, **k: True)
    monkeypatch.setattr(
        pipeline_mod, "format_whatsapp_report", lambda payload: "report text"
    )


def test_deferred_retry_sends_delayed_notice_and_retries(monkeypatch, tmp_path):
    """Deferred retry: portal down -> delayed notice -> retry recovers -> final report sent."""
    import network_report.pipeline as pipeline

    cfg = _base_cfg(tmp_path)
    recorder = MagicMock()
    recorder.send_pipeline_delayed.return_value = True
    recorder.send_report.return_value = True
    recorder.send_success.return_value = True

    portal_down_samples = [
        MrtgSample(target_id="1000001-0000000001", error="portal_down_500"),
        MrtgSample(target_id="1000001-0000000002", error="portal_down_500"),
    ]
    recovered_samples = [
        MrtgSample(
            target_id="1000001-0000000001",
            inbound_maximum="10.0 M",
            outbound_maximum="5.0 M",
        ),
        MrtgSample(
            target_id="1000001-0000000002",
            inbound_maximum="2.0 M",
            outbound_maximum="1.0 M",
        ),
    ]
    capture_calls = {"count": 0}

    def fake_run_netcare_capture(cfg, circuits, target_date, output_dir):
        capture_calls["count"] += 1
        return portal_down_samples if capture_calls["count"] == 1 else recovered_samples

    monkeypatch.setattr(pipeline, "load_config", lambda: cfg)
    monkeypatch.setattr(pipeline, "GowaNotifier", lambda settings: recorder)
    monkeypatch.setattr(pipeline, "_archive_raw_artifacts", lambda **kw: None)
    monkeypatch.setattr(pipeline, "_run_netcare_capture", fake_run_netcare_capture)
    monkeypatch.setattr(pipeline, "_wait_until_slot", lambda slot: None)
    monkeypatch.setattr(
        pipeline,
        "_build_mrtg_payload",
        lambda circuits, cfg, date_iso, date_compact: {
            "ASTINET": [
                {
                    "name": "Circuit A",
                    "sid": "1000001-0000000001",
                    "capacity_str": "10 Mbps",
                    "status": "Normal",
                    "utilization_pct": 50.0,
                    "over_pct": None,
                    "capacity_mbps": 10.0,
                    "max_mbps": 5.0,
                    "remarks": "-",
                }
            ]
        },
    )
    _stub_collectors(monkeypatch, pipeline)

    result = pipeline.run_daily_pipeline(
        date_input="today", send_wa=True, test_mode=False, o_deferred_retry=True
    )

    recorder.send_pipeline_delayed.assert_called_once()
    delay_kwargs = recorder.send_pipeline_delayed.call_args.kwargs
    assert delay_kwargs["issue_title"] == "TelkomCare Portal Down (Error 500)"
    assert delay_kwargs["retry_slots"] == ["22:30", "23:00", "23:30", "00:00"]
    assert delay_kwargs["next_retry_str"] == "22:30"
    assert delay_kwargs["test_mode"] is False

    assert capture_calls["count"] == 2
    assert result["wa_sent"] is True


def test_deferred_retry_final_deadline_portal_still_down(monkeypatch, tmp_path):
    """Deferred retry: portal stays down through all slots -> downtime notation in payload."""
    import network_report.pipeline as pipeline

    cfg = _base_cfg(tmp_path)
    recorder = MagicMock()
    recorder.send_pipeline_delayed.return_value = True
    recorder.send_report.return_value = True
    recorder.send_success.return_value = True

    portal_down_samples = [
        MrtgSample(target_id="1000001-0000000001", error="portal_down_500"),
    ]
    capture_calls = {"count": 0}

    def fake_run_netcare_capture(cfg, circuits, target_date, output_dir):
        capture_calls["count"] += 1
        return list(portal_down_samples)

    monkeypatch.setattr(pipeline, "load_config", lambda: cfg)
    monkeypatch.setattr(pipeline, "GowaNotifier", lambda settings: recorder)
    monkeypatch.setattr(pipeline, "_archive_raw_artifacts", lambda **kw: None)
    monkeypatch.setattr(pipeline, "_run_netcare_capture", fake_run_netcare_capture)
    monkeypatch.setattr(pipeline, "_wait_until_slot", lambda slot: None)
    monkeypatch.setattr(
        pipeline,
        "_build_mrtg_payload",
        lambda circuits, cfg, date_iso, date_compact: {
            "ASTINET": [
                {
                    "name": "Circuit A",
                    "sid": "1000001-0000000001",
                    "capacity_str": "10 Mbps",
                    "status": "⚠️ Portal Down (Error 500)",
                    "utilization_pct": None,
                    "over_pct": None,
                    "capacity_mbps": 10.0,
                    "max_mbps": None,
                    "remarks": "Portal Down (Error 500)",
                }
            ]
        },
    )
    _stub_collectors(monkeypatch, pipeline)

    result = pipeline.run_daily_pipeline(
        date_input="today", send_wa=True, test_mode=False, o_deferred_retry=True
    )

    recorder.send_pipeline_delayed.assert_called_once()
    # Initial pass + 4 retry slots = 5 captures
    assert capture_calls["count"] == 5
    payload = result["payload"]
    assert payload.get("mrtg_error") is not None
    assert "500" in payload["mrtg_error"] or "portal down" in payload["mrtg_error"].lower()


def test_deferred_retry_skipped_when_flag_off(monkeypatch, tmp_path):
    """--no-deferred-retry skips the retry loop even when portal is down."""
    import network_report.pipeline as pipeline

    cfg = _base_cfg(tmp_path)
    recorder = MagicMock()
    recorder.send_pipeline_delayed.return_value = True
    recorder.send_report.return_value = True
    recorder.send_success.return_value = True

    portal_down_samples = [
        MrtgSample(target_id="1000001-0000000001", error="portal_down_500"),
    ]
    call_count = 0

    def counting_capture(cfg, circuits, target_date, output_dir):
        nonlocal call_count
        call_count += 1
        return list(portal_down_samples)

    monkeypatch.setattr(pipeline, "load_config", lambda: cfg)
    monkeypatch.setattr(pipeline, "GowaNotifier", lambda settings: recorder)
    monkeypatch.setattr(pipeline, "_archive_raw_artifacts", lambda **kw: None)
    monkeypatch.setattr(pipeline, "_run_netcare_capture", counting_capture)
    monkeypatch.setattr(pipeline, "_wait_until_slot", lambda slot: None)
    monkeypatch.setattr(
        pipeline,
        "_build_mrtg_payload",
        lambda circuits, cfg, date_iso, date_compact: {
            "ASTINET": [
                {
                    "name": "Circuit A",
                    "sid": "1000001-0000000001",
                    "capacity_str": "10 Mbps",
                    "status": "No Data",
                    "utilization_pct": None,
                    "over_pct": None,
                    "capacity_mbps": 10.0,
                    "max_mbps": None,
                    "remarks": "",
                }
            ]
        },
    )
    _stub_collectors(monkeypatch, pipeline)

    pipeline.run_daily_pipeline(
        date_input="today", send_wa=True, test_mode=False, o_deferred_retry=False
    )

    recorder.send_pipeline_delayed.assert_not_called()
    assert call_count == 1
