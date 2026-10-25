"""Unit tests for pipeline raw-artifact archiving."""

from pathlib import Path

from network_report.config import AppConfig, CmpSettings
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
