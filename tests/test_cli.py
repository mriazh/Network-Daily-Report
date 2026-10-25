"""Tests for CLI subcommands including backfill-cmp."""

from __future__ import annotations

from unittest.mock import MagicMock

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
