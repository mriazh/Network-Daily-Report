"""Unit tests for GOWA lifecycle notifications."""

from __future__ import annotations

import pytest

from network_report.config import GowaSettings
from network_report.notifier import GowaNotifier, format_duration


class _Recorder(GowaNotifier):
    """Captures the rendered message instead of calling the gateway."""

    def __init__(self) -> None:
        super().__init__(GowaSettings(enabled=True, base_url="http://x", group_jid="g@g.us"))
        self.sent: list[str] = []

    def send_message(self, message: str) -> bool:
        self.sent.append(message)
        return True


@pytest.mark.parametrize(
    ("seconds", "expected"),
    [(0, "0s"), (45, "45s"), (83, "1m 23s"), (60, "1m 0s"), (3600, "1h"), (3661, "1h 1m 1s")],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


def test_lifecycle_message_formats():
    n = _Recorder()
    n.send_start("12 Oktober 2026")
    n.send_success("12 Oktober 2026", 83.4)
    n.send_failed("12 Oktober 2026", "boom")
    assert n.sent == [
        "[Network Daily Report] START | date=12 Oktober 2026",
        "[Network Daily Report] SUCCESS | date=12 Oktober 2026 | elapsed=1m 23s",
        "[Network Daily Report] FAILED | date=12 Oktober 2026 | error=boom",
    ]


def test_test_mode_message_formats():
    n = _Recorder()
    n.send_start("12 Oktober 2026", test_mode=True)
    n.send_success("12 Oktober 2026", 83.4, test_mode=True)
    n.send_failed("12 Oktober 2026", "boom", test_mode=True)
    n.send_report("core report", test_mode=True)
    assert n.sent == [
        "[TEST] [Network Daily Report] START | date=12 Oktober 2026",
        "[TEST] [Network Daily Report] SUCCESS | date=12 Oktober 2026 | elapsed=1m 23s",
        "[TEST] [Network Daily Report] FAILED | date=12 Oktober 2026 | error=boom",
        "*⚠️ [TESTING / VERIFIKASI SYSTEM DAILY REPORT] ⚠️*\n*Pesan ini adalah uji coba otomatisasi (Harap Abaikan)*\n\ncore report",
    ]


def test_ap_sampler_lifecycle_messages():
    n = _Recorder()
    n.send_ap_sample_started("12 Oktober 2026")
    n.send_ap_sample_success("12 Oktober 2026", 454, 1, 2500, 45.3)
    n.send_ap_sample_failed("12 Oktober 2026", "login failed")
    assert n.sent == [
        "[HUAWEI AP SAMPLER] 🚀 Memulai sampling data AP Peak Hour (14:00 WIB)...",
        "[HUAWEI AP SAMPLER] ✅ Selesai sampling data AP Peak Hour:\n📅 Tanggal: 12 Oktober 2026 (14:00 WIB)\n📶 Status AP: 🟢 454 Normal, 🔴 1 Fault\n👥 Connected Users: 2500 Users\n⏱️ Durasi: 45s",
        "[HUAWEI AP SAMPLER] ❌ Gagal sampling data AP: login failed",
    ]


def test_ap_sampler_test_mode_messages():
    n = _Recorder()
    n.send_ap_sample_started("12 Oktober 2026", test_mode=True)
    n.send_ap_sample_success("12 Oktober 2026", 454, 1, 2500, 45.3, test_mode=True)
    n.send_ap_sample_failed("12 Oktober 2026", "login failed", test_mode=True)
    assert n.sent == [
        "[TEST] [HUAWEI AP SAMPLER] 🚀 Memulai sampling data AP Peak Hour (14:00 WIB)...",
        "[TEST] [HUAWEI AP SAMPLER] ✅ Selesai sampling data AP Peak Hour:\n📅 Tanggal: 12 Oktober 2026 (14:00 WIB)\n📶 Status AP: 🟢 454 Normal, 🔴 1 Fault\n👥 Connected Users: 2500 Users\n⏱️ Durasi: 45s",
        "[TEST] [HUAWEI AP SAMPLER] ❌ Gagal sampling data AP: login failed",
    ]


def test_send_report_alias_delivers_message(monkeypatch):
    n = _Recorder()
    monkeypatch.setattr(n, "send_message", lambda m: True)
    assert n.send_report("hello") is True


def test_pipeline_reports_failure_before_reraising(monkeypatch, tmp_path):
    """A crash mid-pipeline sends FAILED and still propagates."""
    from network_report import pipeline

    recorder = _Recorder()
    monkeypatch.setattr(pipeline, "load_config", lambda: _cfg(tmp_path))
    monkeypatch.setattr(pipeline, "GowaNotifier", lambda settings: recorder)
    monkeypatch.setattr(pipeline, "_archive_raw_artifacts", lambda **kw: None)
    monkeypatch.setattr(
        pipeline,
        "collect_nexthop_ping",
        lambda: (_ for _ in ()).throw(RuntimeError("ping collector exploded")),
    )

    with pytest.raises(RuntimeError, match="ping collector exploded"):
        pipeline.run_daily_pipeline(date_input="2026-10-12", send_wa=True)

    assert recorder.sent[0] == "[Network Daily Report] START | date=12 October 2026"
    assert recorder.sent[-1] == (
        "[Network Daily Report] FAILED | date=12 October 2026 | error=ping collector exploded"
    )


def _cfg(tmp_path):
    from network_report.config import AppConfig

    cfg = AppConfig()
    cfg.gowa = GowaSettings(enabled=True, base_url="http://x", group_jid="g@g.us")
    cfg.output_root = tmp_path
    cfg.output_raw_root = tmp_path / "raw"
    return cfg
