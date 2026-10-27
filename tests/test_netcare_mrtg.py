from __future__ import annotations

import json
from datetime import date
from pathlib import Path
from unittest.mock import MagicMock, patch

from network_report.collectors.mrtg import evaluate_circuit, load_mrtg_ocr_data
from network_report.collectors.netcare_mrtg import (
    GeminiCaptchaSolver,
    LegendExtractor,
    MrtgSample,
    MrtgTarget,
    NetcareScraper,
    build_targets_from_circuits,
    resolve_netcare_target,
    validate_graph_png,
)
from network_report.writers.wag_formatter import format_whatsapp_report


def _make_valid_png_bytes() -> bytes:
    import io
    import random
    from PIL import Image

    rnd = random.Random(42)
    raw = rnd.randbytes(150 * 150 * 3)
    img = Image.frombytes("RGB", (150, 150), raw)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


VALID_PNG_BYTES = _make_valid_png_bytes()


def test_mrtg_sample_and_loader(tmp_path: Path):
    target_date_compact = "20261022"
    mrtg_dir = tmp_path / target_date_compact
    mrtg_dir.mkdir(parents=True)
    json_path = mrtg_dir / f"mrtg_values_{target_date_compact}.json"

    data = {
        "1000001-0000000001": {
            "Inbound_Current": "12.31 M",
            "Inbound_Average": "8.15 M",
            "Inbound_Maximum": "25.40 M",
            "Outbound_Current": "4.12 M",
            "Outbound_Average": "3.10 M",
            "Outbound_Maximum": "10.05 M",
            "image": "MRTG_1000001-0000000001_20261022.png",
            "error": None,
        },
        "4000-1000000001": {
            "Inbound_Current": "100.00 k",
            "Inbound_Average": "50.00 k",
            "Inbound_Maximum": "750.00 k",
            "Outbound_Current": "20.00 k",
            "Outbound_Average": "10.00 k",
            "Outbound_Maximum": "150.00 k",
            "image": "MRTG_4000-1000000001_20261022.png",
            "error": None,
        },
    }
    json_path.write_text(json.dumps(data), encoding="utf-8")

    loaded = load_mrtg_ocr_data(tmp_path, target_date_compact)
    assert "1000001-0000000001" in loaded
    assert loaded["1000001-0000000001"]["in_mbps"] == 25.40
    assert loaded["1000001-0000000001"]["out_mbps"] == 10.05

    assert "4000-1000000001" in loaded
    assert round(loaded["4000-1000000001"]["in_mbps"], 3) == 0.750
    assert round(loaded["4000-1000000001"]["out_mbps"], 3) == 0.150


def test_loader_missing_json(tmp_path: Path):
    loaded = load_mrtg_ocr_data(tmp_path, "20261099")
    assert loaded == {}

    status = evaluate_circuit("CGK Backup", "1000001-0020265222", "1 GB", None, None)
    assert status.status == "No Data"
    assert status.remarks == ""
    assert status.max_mbps is None


def test_evaluate_circuit_statuses():
    no_data_1 = evaluate_circuit("Link A", "SID-1", "10 MB", None, None)
    assert no_data_1.status == "No Data"
    assert no_data_1.remarks == ""

    no_data_2 = evaluate_circuit("Link A", "SID-1", "10 MB", 0.0, 0.0)
    assert no_data_2.status == "No Data"
    assert no_data_2.remarks == ""

    normal = evaluate_circuit("Link B", "SID-2", "10 MB", 5.0, 4.0)
    assert normal.status == "Normal"
    assert normal.remarks == "-"
    assert normal.utilization_pct == 50.0

    high = evaluate_circuit("Link C", "SID-3", "10 MB", 12.5, 3.0)
    assert high.status == "High Traffic"
    assert high.over_pct == 25.0
    assert "High Traffic" in high.remarks


def test_wag_formatter_renders_no_data():
    payload = {
        "date_str": "22 Oktober 2026",
        "mrtg": {
            "ASTINET": [
                {
                    "name": "CGK Primary",
                    "sid": "1000001-0000000002",
                    "capacity_str": "1 GB",
                    "status": "No Data",
                    "remarks": "",
                },
                {
                    "name": "CGK Backup",
                    "sid": "1000001-0000000001",
                    "capacity_str": "1 GB",
                    "status": "Normal",
                    "remarks": "-",
                },
                {
                    "name": "DPS",
                    "sid": "1000001-0000000003",
                    "capacity_str": "12 MB",
                    "status": "High Traffic",
                    "remarks": "High Traffic (inbound >2.5 MB / >20.83%)",
                },
            ]
        },
        "mrtg_error": "Capture failure",
    }
    wag = format_whatsapp_report(payload)
    assert "*B. 📊 Usage by MRTG* (⚠️ Gagal scrape)" in wag
    assert "CGK Primary (SID: 1000001-0000000002) (1 GB) → ⚠️ No Data" in wag
    assert "CGK Backup (SID: 1000001-0000000001) (1 GB) → ✅ Normal" in wag
    assert "DPS (SID: 1000001-0000000003) (12 MB) → ⚠️ High Traffic" in wag
    assert "CGK Primary (SID: 1000001-0000000002) (1 GB) → ✅ Normal" not in wag


def test_resolve_netcare_target():
    expected_mappings = [
        ("4000-1000000001", ("title", "1000000001")),
        ("4001-2000000001", ("title", "2000000001")),
        ("1000001-0020265222", ("sid", "1000001-0020265222")),
        ("1708594346", ("sid", "1708594346")),
        ("1708594520", ("sid", "1708594520")),
        ("1001", ("sid", "1001")),
        ("1002", ("sid", "1002")),
        ("2000000001", ("sid", "2000000001")),
        ("2000000002", ("sid", "2000000002")),
    ]
    for identifier, expected in expected_mappings:
        assert resolve_netcare_target(identifier) == expected


def test_build_targets_from_circuits():
    circuits_fixture = {
        "astinet": [
            ["CGK Backup", "1000001-0000000001", "1 GB"],
            ["CGK Primary", "1000001-0000000002", "1 GB"],
            ["UPG Bandara", "4000-1000000001", "4 MB"],
            ["UPG Ruko", "1708594520", "4 MB"],
            ["DPS", "1000001-0000000003", "12 MB"],
            ["SUB", "1000001-0000000004", "8 MB"],
        ],
        "metro_e": [
            ["CGK T3 G.19", "2000001-0000000001", "10 MB"],
            ["CGK T3 G.53", "2000001-0000000002", "10 MB"],
            ["CGK T1C", "2000000001", "5 MB"],
            ["CGK T3 G.69", "2000000002", "15 MB"],
        ],
        "vpn_ip_osa": [
            ["CORP CGK - PARTNER-DC", "1001", "50 MB"],
            ["PARTNER-DC - CORP CGK", "1002", "50 MB"],
            ["CORP - SUB", "1000001-0000000005", "6 MB"],
            ["CORP - DPS (Denpasar)", "1000001-0000000006", "12 MB"],
            ["CORP - BPN (Balikpapan)", "1000001-0000000007", "6 MB"],
            ["CORP - UPG Ruko Makassar", "1708594346", "6 MB"],
            ["CORP - UPG Bandara Makassar", "3034-2000000001", "4 MB"],
        ],
        "indibiz": [
            ["CORP Pondok Cabe", "121212300001", "150 MB"],
        ],
    }

    targets = build_targets_from_circuits(circuits_fixture)
    assert len(targets) == 17
    target_ids = [t.target_id for t in targets]
    assert "121212300001" not in target_ids
    assert not any("Pondok Cabe" in t.name for t in targets)

    # 4-digit prefix circuits become title with trailing id
    assert MrtgTarget(target_id="1000000001", kind="title", name="UPG Bandara") in targets
    assert (
        MrtgTarget(target_id="2000000001", kind="sid", name="CGK T1C")
        in targets
    )

    # Standard SIDs
    assert MrtgTarget(target_id="1000001-0000000001", kind="sid", name="CGK Backup") in targets
    assert MrtgTarget(target_id="2000001-0000000001", kind="sid", name="CGK T3 G.19") in targets
    assert MrtgTarget(target_id="1708594520", kind="sid", name="UPG Ruko") in targets
    assert MrtgTarget(target_id="1708594346", kind="sid", name="CORP - UPG Ruko Makassar") in targets

    # Bare numeric IDs now resolve as SID (not in BARE_NUMERIC_TITLE_IDS)
    assert MrtgTarget(target_id="1001", kind="sid", name="CORP CGK - PARTNER-DC") in targets
    assert MrtgTarget(target_id="1002", kind="sid", name="PARTNER-DC - CORP CGK") in targets
    assert MrtgTarget(target_id="2000000001", kind="sid", name="CGK T1C") in targets
    assert MrtgTarget(target_id="2000000002", kind="sid", name="CGK T3 G.69") in targets

    # Overall kind counts: 15 SIDs, 2 titles (bare numerics now resolve as SID)
    assert sum(1 for t in targets if t.kind == "sid") == 15
    assert sum(1 for t in targets if t.kind == "title") == 2


def test_mocked_legend_extraction_paddle(tmp_path: Path):
    fake_img = tmp_path / "test.png"
    fake_img.write_bytes(b"\x89PNG\r\n\x1a\nfakeimg")

    extractor = LegendExtractor()
    fake_paddle_output = (
        "Inbound\n"
        "Max : 25.40 M\n"
        "Average : 8.15 M\n"
        "Current : 12.31 M\n"
        "Outbound\n"
        "Max : 10.05 M\n"
        "Average : 3.10 M\n"
        "Current : 4.12 M\n"
    )

    with (
        patch.object(extractor, "_paddle_interpreters", return_value=[Path("/usr/bin/python3")]),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=fake_paddle_output)
        res = extractor.extract(fake_img)
        assert res["Inbound_Current"] == "12.31 M"
        assert res["Inbound_Average"] == "8.15 M"
        assert res["Inbound_Maximum"] == "25.40 M"
        assert res["Outbound_Current"] == "4.12 M"
        assert res["Outbound_Average"] == "3.10 M"
        assert res["Outbound_Maximum"] == "10.05 M"

    multiline_paddle_output = (
        "Inbound\n"
        "Max\n"
        "25.40 M\n"
        "Average\n"
        "8.15 M\n"
        "Current\n"
        "12.31 M\n"
        "Outbound\n"
        "Max\n"
        "10.05 M\n"
        "Average\n"
        "3.10 M\n"
        "Current\n"
        "4.12 M\n"
    )
    with (
        patch.object(extractor, "_paddle_interpreters", return_value=[Path("/usr/bin/python3")]),
        patch("subprocess.run") as mock_run,
    ):
        mock_run.return_value = MagicMock(returncode=0, stdout=multiline_paddle_output)
        res_multi = extractor.extract(fake_img)
        assert res_multi["Inbound_Current"] == "12.31 M"
        assert res_multi["Inbound_Average"] == "8.15 M"
        assert res_multi["Inbound_Maximum"] == "25.40 M"
        assert res_multi["Outbound_Current"] == "4.12 M"
        assert res_multi["Outbound_Average"] == "3.10 M"
        assert res_multi["Outbound_Maximum"] == "10.05 M"


def test_mocked_legend_extraction_gemini_fallback(tmp_path: Path):
    fake_img = tmp_path / "test2.png"
    fake_img.write_bytes(b"\x89PNG\r\n\x1a\nfakeimg")

    extractor = LegendExtractor(api_keys=["test_key"], models=["gemini-3.5-flash-lite"])
    gemini_reply = (
        "Inbound_Current: 1.10 M\n"
        "Inbound_Average: 800.00 k\n"
        "Inbound_Maximum: 2.50 M\n"
        "Outbound_Current: 500.00 k\n"
        "Outbound_Average: 300.00 k\n"
        "Outbound_Maximum: 1.20 M\n"
    )
    parsed_direct = extractor._parse_gemini_text(gemini_reply)
    assert parsed_direct["Inbound_Maximum"] == "2.50 M"

    with (
        patch.object(extractor, "_extract_paddle", return_value={}),
        patch.object(extractor, "_extract_gemini") as mock_gem,
    ):
        mock_gem.return_value = parsed_direct
        res = extractor.extract(fake_img)
        assert res["Inbound_Maximum"] == "2.50 M"
        assert res["Outbound_Maximum"] == "1.20 M"


def test_legend_extractor_proxy_support(monkeypatch):
    """Test that LegendExtractor accepts proxy configuration and builds ProxyHandler."""
    # Test without proxy
    extractor = LegendExtractor()
    assert extractor._proxy is None
    assert extractor._proxy_handler is None

    # Test with explicit proxy
    extractor = LegendExtractor(proxy="socks5://127.0.0.1:40000")
    assert extractor._proxy == "socks5://127.0.0.1:40000"
    assert extractor._proxy_handler is not None

    # Test with HTTP proxy
    extractor = LegendExtractor(proxy="http://proxy.example.com:8080")
    assert extractor._proxy == "http://proxy.example.com:8080"
    assert extractor._proxy_handler is not None

    # Test fallback to env var
    monkeypatch.setenv("NETCARE_CAPTCHA_PROXY", "socks5://env:40000")
    extractor = LegendExtractor(proxy=None)
    assert extractor._proxy == "socks5://env:40000"
    monkeypatch.delenv("NETCARE_CAPTCHA_PROXY")

    monkeypatch.setenv("ALL_PROXY", "socks5://all:40000")
    extractor = LegendExtractor(proxy=None)
    assert extractor._proxy == "socks5://all:40000"
    monkeypatch.delenv("ALL_PROXY")


def test_gemini_captcha_solver():
    solver = GeminiCaptchaSolver(
        api_keys=["key1", "key2"],
        models=["model1", "model2"],
    )

    def mock_transport(url: str, payload: dict) -> tuple[int, dict]:
        if "key1" in url:
            return 429, {}
        return 200, {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": "7xk"}],
                    }
                }
            ]
        }

    solver._transport = mock_transport
    code = solver.solve(b"fake_image_bytes")
    assert code == "7xk"
    assert solver._next_key_index == 1


def test_gemini_captcha_solver_transport_default():
    """Test that _transport defaults to _default_transport when not provided."""
    solver = GeminiCaptchaSolver(
        api_keys=["key1"],
        models=["model1"],
    )
    # _transport should be set to the _default_transport method
    assert solver._transport is not None
    assert callable(solver._transport)
    # Verify it's the default transport (same underlying function)
    assert solver._transport.__func__ is solver._default_transport.__func__


def test_gemini_captcha_solver_with_proxy(monkeypatch):
    """Test that GeminiCaptchaSolver accepts proxy configuration and builds ProxyHandler."""
    # Test without proxy
    solver = GeminiCaptchaSolver(
        api_keys=["key1"],
        models=["model1"],
        proxy=None,
    )
    assert solver._proxy is None
    assert solver._proxy_handler is None

    # Test with explicit proxy
    solver = GeminiCaptchaSolver(
        api_keys=["key1"],
        models=["model1"],
        proxy="socks5://127.0.0.1:40000",
    )
    assert solver._proxy == "socks5://127.0.0.1:40000"
    assert solver._proxy_handler is not None

    # Test with HTTP proxy
    solver = GeminiCaptchaSolver(
        api_keys=["key1"],
        models=["model1"],
        proxy="http://proxy.example.com:8080",
    )
    assert solver._proxy == "http://proxy.example.com:8080"
    assert solver._proxy_handler is not None

    # Test fallback to env var
    monkeypatch.setenv("NETCARE_CAPTCHA_PROXY", "socks5://env:40000")
    solver = GeminiCaptchaSolver(
        api_keys=["key1"],
        models=["model1"],
        proxy=None,
    )
    assert solver._proxy == "socks5://env:40000"
    monkeypatch.delenv("NETCARE_CAPTCHA_PROXY")

    monkeypatch.setenv("ALL_PROXY", "socks5://all:40000")
    solver = GeminiCaptchaSolver(
        api_keys=["key1"],
        models=["model1"],
        proxy=None,
    )
    assert solver._proxy == "socks5://all:40000"
    monkeypatch.delenv("ALL_PROXY")


def test_netcare_scraper_capture_all(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        username="user",
        password="pwd",
        totp_secret="AAAAAAAAAAAAAAAA",
        captcha_enabled=True,
        captcha_api_keys=["k1"],
        workers=2,
    )

    fake_driver = MagicMock()
    fake_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    with (
        patch.object(scraper, "_create_driver", return_value=fake_driver),
        patch.object(scraper, "login", return_value=True),
        patch.object(scraper, "capture") as mock_cap,
    ):
        mock_cap.side_effect = lambda t, d, driver=None, output_dir=None: MrtgSample(
            target_id=t.target_id,
            inbound_maximum="10.0 M",
            outbound_maximum="5.0 M",
            image_path=output_dir / f"MRTG_{t.target_id}_{d.strftime('%Y%m%d')}.png",
        )

        targets = [
            MrtgTarget(target_id="SID-1", kind="sid", name="Link 1"),
            MrtgTarget(target_id="SID-2", kind="sid", name="Link 2"),
        ]
        target_date = date(2026, 10, 22)
        samples = scraper.capture_all(targets, target_date, output_dir=tmp_path / "20261022")

        assert len(samples) == 2
        assert samples[0].target_id == "SID-1"
        assert samples[0].inbound_maximum == "10.0 M"
        assert samples[1].target_id == "SID-2"
        assert samples[1].outbound_maximum == "5.0 M"

        json_file = tmp_path / "20261022" / "mrtg_values_20261022.json"
        assert json_file.exists()
        saved_data = json.loads(json_file.read_text(encoding="utf-8"))
        assert "SID-1" in saved_data
        assert saved_data["SID-1"]["Inbound_Maximum"] == "10.0 M"


def test_pipeline_mrtg_missing_data_evaluation(tmp_path: Path):
    from network_report.config import AppConfig, MrtgSettings, RouterOSSettings
    from network_report.pipeline import _build_mrtg_payload
    from network_report.writers.excel_writer import write_daily_excel

    circuits = {
        "astinet": [["Link A", "SID-1", "10 MB"]],
        "metro_e": [["Link B", "SID-2", "10 MB"]],
        "vpn_ip_osa": [["Link C", "SID-3", "10 MB"]],
        "indibiz": [["Link D", "SID-4", "10 MB"]],
    }
    cfg = AppConfig(
        output_root=tmp_path / "output",
        output_raw_root=tmp_path / "output" / "raw",
        mrtg=MrtgSettings(telkomcare_dir=tmp_path / "nonexistent"),
        # Point the Indibiz store at a path that does not exist so the test stays
        # hermetic even on a host where the live sampler is writing real samples.
        routeros=RouterOSSettings(db_path=tmp_path / "nonexistent" / "traffic.db"),
    )

    mrtg_payload = _build_mrtg_payload(circuits, cfg, "2026-10-22", "20261022")
    all_circuits = [c for grp in mrtg_payload.values() for c in grp]
    assert len(all_circuits) == 4
    assert all(c["status"] == "No Data" for c in all_circuits)
    assert all(c["remarks"] == "" for c in all_circuits)

    wag = format_whatsapp_report({"date_str": "22 Oktober 2026", "mrtg": mrtg_payload})
    assert "Link A (SID: SID-1) (10 MB) → ⚠️ No Data" in wag
    assert "Link A (SID: SID-1) (10 MB) → ✅ Normal" not in wag
    assert "Link B (SID: SID-2) (10 MB) → ⚠️ No Data" in wag
    assert "Link B (SID: SID-2) (10 MB) → ✅ Normal" not in wag

    dest_xlsx = tmp_path / "output" / "Daily-Report-2026-10.xlsx"
    dest_xlsx.parent.mkdir(parents=True, exist_ok=True)
    template_path = Path(__file__).resolve().parents[1] / "config" / "Template-Daily-Report.xlsx"
    if not template_path.exists():
        template_path = (
            Path(__file__).resolve().parents[1] / "config" / "Template-Daily-Report.example.xlsx"
        )
    write_daily_excel(
        template_path=template_path,
        output_path=dest_xlsx,
        day_number=22,
        payload={"mrtg": mrtg_payload, "mrtg_error": "No MRTG data available"},
    )
    assert dest_xlsx.exists()


def test_portal_root_normalization_strips_public_login():
    for base in [
        "https://telkomcare.example.internal/public/login",
        "https://telkomcare.example.internal/public/login/",
        "https://telkomcare.example.internal/public/login///",
    ]:
        scraper = NetcareScraper(base_url=base)
        assert scraper.portal_root == "https://telkomcare.example.internal"
        assert scraper.login_url == "https://telkomcare.example.internal/public/login"
        assert scraper.graph_root == "https://telkomcare.example.internal/mrtgnetcare2"
        assert scraper.login_url.count("/public/login") == 1
        assert "/public/login" not in scraper.graph_root

        sid_url = f"{scraper.graph_root}/graph/monitoring"
        title_url = f"{scraper.graph_root}/graph"
        assert sid_url == "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"
        assert title_url == "https://telkomcare.example.internal/mrtgnetcare2/graph"
        assert "/public/login" not in sid_url
        assert "/public/login" not in title_url


def test_portal_root_normalization_without_public_login():
    for base in [
        "https://telkomcare.example.internal",
        "https://telkomcare.example.internal/",
        "https://telkomcare.example.internal///",
    ]:
        scraper = NetcareScraper(base_url=base)
        assert scraper.portal_root == "https://telkomcare.example.internal"
        assert scraper.login_url == "https://telkomcare.example.internal/public/login"
        assert scraper.graph_root == "https://telkomcare.example.internal/mrtgnetcare2"
        assert scraper.login_url.count("/public/login") == 1
        assert "/public/login" not in scraper.graph_root


def test_capture_executes_isolation_before_screenshot_and_restore_after(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal/public/login",
        graph_timeout_seconds=2,
    )
    scraper.legend_extractor = MagicMock()
    scraper.legend_extractor.extract.return_value = {
        "Inbound_Current": "1 M",
        "Inbound_Average": "2 M",
        "Inbound_Maximum": "3 M",
        "Outbound_Current": "1 M",
        "Outbound_Average": "2 M",
        "Outbound_Maximum": "3 M",
    }

    recorded_scripts: list[str] = []
    execution_order: list[str] = []

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_alert = MagicMock()
    mock_driver.switch_to.alert = mock_alert

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    mock_btn_filter = MagicMock()
    mock_btn_filter.is_displayed.return_value = True
    mock_btn_filter.is_enabled.return_value = True

    mock_date_inp1 = MagicMock()
    mock_date_inp2 = MagicMock()

    mock_graph_img = MagicMock()

    def mock_screenshot():
        execution_order.append("screenshot")
        return VALID_PNG_BYTES

    type(mock_graph_img).screenshot_as_png = property(lambda self: mock_screenshot())

    def mock_find_element(by, value=None):
        if value == "sid":
            return mock_sid_input
        return mock_btn_filter

    def mock_find_elements(by, value=None):
        if "graph.php" in str(value):
            return [mock_graph_img]
        if "preceding::input" in str(value):
            return [mock_date_inp1, mock_date_inp2]
        return []

    def mock_execute_script(script, *args):
        recorded_scripts.append(script)
        if "MRTG restore script" in script:
            execution_order.append("restore")
            return None
        if "MRTG isolate script" in script:
            execution_order.append("isolate")
            return True
        if "naturalWidth" in script:
            return 500
        return None

    mock_driver.find_element.side_effect = mock_find_element
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1000001-0020265222", kind="sid")
    target_date = date(2026, 10, 22)

    sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)

    assert sample.error is None
    assert sample.image_path is not None
    assert sample.image_path.exists()
    assert sample.image_path.read_bytes() == VALID_PNG_BYTES

    assert execution_order == ["isolate", "screenshot", "restore"]

    isolate_scripts = [s for s in recorded_scripts if "MRTG isolate script" in s]
    assert len(isolate_scripts) == 1
    isolate_js = isolate_scripts[0]
    assert "img[src*='graph.php']" in isolate_js
    assert "naturalWidth > 400" in isolate_js
    assert "position" in isolate_js and "fixed" in isolate_js
    assert "display" in isolate_js and "none" in isolate_js
    assert "z-index" in isolate_js
    assert "overflow" in isolate_js

    restore_scripts = [s for s in recorded_scripts if "MRTG restore script" in s]
    assert len(restore_scripts) == 1
    restore_js = restore_scripts[0]
    assert "_mrtg_hidden_elements" in restore_js
    assert "_mrtg_target" in restore_js

    mock_driver.save_screenshot.assert_not_called()
    assert mock_alert.accept.call_count >= 1


def test_capture_requeries_graph_image_after_isolation_and_screenshots_new_element(
    tmp_path: Path,
):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    scraper.legend_extractor = MagicMock()
    scraper.legend_extractor.extract.return_value = {
        "Inbound_Current": "1 M",
        "Inbound_Average": "2 M",
        "Inbound_Maximum": "3 M",
        "Outbound_Current": "1 M",
        "Outbound_Average": "2 M",
        "Outbound_Maximum": "3 M",
    }

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    initial_img = MagicMock()
    fresh_img = MagicMock()

    initial_screenshotted = False
    fresh_screenshotted = False

    def mock_initial_screenshot():
        nonlocal initial_screenshotted
        initial_screenshotted = True
        raise ValueError("Stale element reference")

    def mock_fresh_screenshot():
        nonlocal fresh_screenshotted
        fresh_screenshotted = True
        return VALID_PNG_BYTES

    type(initial_img).screenshot_as_png = property(lambda self: mock_initial_screenshot())
    type(fresh_img).screenshot_as_png = property(lambda self: mock_fresh_screenshot())

    graph_query_count = 0

    def mock_find_elements(by, value=None):
        nonlocal graph_query_count
        if "graph.php" in str(value):
            graph_query_count += 1
            if graph_query_count == 1:
                return [initial_img]
            return [fresh_img]
        if "preceding::input" in str(value):
            return [MagicMock(), MagicMock()]
        return []

    def mock_execute_script(script, *args):
        if "MRTG isolate script" in script:
            return True
        if "MRTG restore script" in script:
            return None
        if "naturalWidth" in script:
            return 500
        return None

    mock_driver.find_element.side_effect = lambda by, value=None: mock_sid_input
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1000001-0020265222", kind="sid")
    target_date = date(2026, 10, 22)

    sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)

    assert sample.error is None
    assert graph_query_count >= 2
    assert fresh_screenshotted is True
    assert initial_screenshotted is False


def test_capture_fails_when_no_wide_element_found_after_isolation(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    initial_img = MagicMock()
    small_img = MagicMock()

    graph_query_count = 0
    restore_called = False
    isolate_called = False

    def mock_find_elements(by, value=None):
        nonlocal graph_query_count
        if "graph.php" in str(value):
            graph_query_count += 1
            if graph_query_count == 1:
                return [initial_img]
            return [small_img]
        if "preceding::input" in str(value):
            return [MagicMock(), MagicMock()]
        return []

    def mock_execute_script(script, *args):
        nonlocal restore_called, isolate_called
        if "MRTG isolate script" in script:
            isolate_called = True
            return True
        if "MRTG restore script" in script:
            restore_called = True
            return None
        if "naturalWidth" in script:
            if args and args[0] is initial_img:
                if isolate_called:
                    from selenium.common.exceptions import StaleElementReferenceException

                    raise StaleElementReferenceException("Stale pre-isolation reference")
                return 500
            return 200
        return None

    mock_driver.find_element.side_effect = lambda by, value=None: mock_sid_input
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1000001-0020265222", kind="sid")
    target_date = date(2026, 10, 22)

    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)

    assert sample.error is not None
    assert "No wide graph image found after isolation" in sample.error
    assert restore_called is True
    assert graph_query_count >= 2
    mock_driver.save_screenshot.assert_not_called()


def test_capture_poll_succeeds_on_second_attempt_after_initial_empty(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    scraper.legend_extractor = MagicMock()
    scraper.legend_extractor.extract.return_value = {
        "Inbound_Current": "1 M",
        "Inbound_Average": "2 M",
        "Inbound_Maximum": "3 M",
        "Outbound_Current": "1 M",
        "Outbound_Average": "2 M",
        "Outbound_Maximum": "3 M",
    }

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    initial_img = MagicMock()
    empty_img = MagicMock()
    fresh_img = MagicMock()
    fresh_img.screenshot_as_png = VALID_PNG_BYTES

    graph_query_count = 0

    def mock_find_elements(by, value=None):
        nonlocal graph_query_count
        if "graph.php" in str(value):
            graph_query_count += 1
            if graph_query_count == 1:
                return [initial_img]
            if graph_query_count == 2:
                return [empty_img]
            return [fresh_img]
        if "preceding::input" in str(value):
            return [MagicMock(), MagicMock()]
        return []

    def mock_execute_script(script, *args):
        if "MRTG isolate script" in script:
            return True
        if "MRTG restore script" in script:
            return None
        if "naturalWidth" in script:
            if args and args[0] is empty_img:
                return 0
            return 500
        return None

    mock_driver.find_element.side_effect = lambda by, value=None: mock_sid_input
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1000001-0020265222", kind="sid")
    target_date = date(2026, 10, 22)

    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)

    assert sample.error is None
    assert sample.image_path is not None
    assert sample.image_path.exists()
    assert sample.image_path.read_bytes() == VALID_PNG_BYTES
    assert graph_query_count == 3


def test_capture_falls_back_to_pre_isolation_reference_when_post_query_finds_nothing(
    tmp_path: Path,
):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    scraper.legend_extractor = MagicMock()
    scraper.legend_extractor.extract.return_value = {
        "Inbound_Current": "1 M",
        "Inbound_Average": "2 M",
        "Inbound_Maximum": "3 M",
        "Outbound_Current": "1 M",
        "Outbound_Average": "2 M",
        "Outbound_Maximum": "3 M",
    }

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    initial_img = MagicMock()
    initial_img.screenshot_as_png = VALID_PNG_BYTES

    graph_query_count = 0

    def mock_find_elements(by, value=None):
        nonlocal graph_query_count
        if "graph.php" in str(value):
            graph_query_count += 1
            if graph_query_count == 1:
                return [initial_img]
            return []
        if "preceding::input" in str(value):
            return [MagicMock(), MagicMock()]
        return []

    def mock_execute_script(script, *args):
        if "MRTG isolate script" in script:
            return True
        if "MRTG restore script" in script:
            return None
        if "naturalWidth" in script:
            if args and args[0] is initial_img:
                return 500
            return 0
        return None

    mock_driver.find_element.side_effect = lambda by, value=None: mock_sid_input
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1000001-0020265222", kind="sid")
    target_date = date(2026, 10, 22)

    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)

    assert sample.error is None
    assert sample.image_path is not None
    assert sample.image_path.exists()
    assert sample.image_path.read_bytes() == VALID_PNG_BYTES
    assert graph_query_count == 16


def test_capture_accepts_pinned_element_when_natural_width_not_ready(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    scraper.legend_extractor = MagicMock()
    scraper.legend_extractor.extract.return_value = {}

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    initial_img = MagicMock()
    pinned_img = MagicMock()
    pinned_img.screenshot_as_png = VALID_PNG_BYTES

    graph_query_count = 0

    def mock_find_elements(by, value=None):
        nonlocal graph_query_count
        if "graph.php" in str(value):
            graph_query_count += 1
            if graph_query_count == 1:
                return [initial_img]
            return [pinned_img]
        if "preceding::input" in str(value):
            return [MagicMock(), MagicMock()]
        return []

    def mock_execute_script(script, *args):
        if "MRTG isolate script" in script:
            return True
        if "MRTG restore script" in script:
            return None
        if "naturalWidth" in script:
            if args and args[0] is initial_img:
                return 500
            return 0
        if "position" in script and "fixed" in script:
            if args and args[0] is pinned_img:
                return True
        return None

    mock_driver.find_element.side_effect = lambda by, value=None: mock_sid_input
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1000001-0020265222", kind="sid")
    target_date = date(2026, 10, 22)

    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)

    assert sample.error is None
    assert sample.image_path is not None
    assert sample.image_path.exists()
    assert sample.image_path.read_bytes() == VALID_PNG_BYTES


def test_capture_tolerates_stale_element_during_post_isolation_poll(tmp_path: Path):
    from selenium.common.exceptions import StaleElementReferenceException

    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    scraper.legend_extractor = MagicMock()
    scraper.legend_extractor.extract.return_value = {}

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    initial_img = MagicMock()
    stale_img = MagicMock()
    fresh_img = MagicMock()
    fresh_img.screenshot_as_png = VALID_PNG_BYTES

    graph_query_count = 0

    def mock_find_elements(by, value=None):
        nonlocal graph_query_count
        if "graph.php" in str(value):
            graph_query_count += 1
            if graph_query_count == 1:
                return [initial_img]
            return [stale_img, fresh_img]
        if "preceding::input" in str(value):
            return [MagicMock(), MagicMock()]
        return []

    def mock_execute_script(script, *args):
        if "MRTG isolate script" in script:
            return True
        if "MRTG restore script" in script:
            return None
        if "naturalWidth" in script:
            if args and args[0] is initial_img:
                return 500
            if args and args[0] is stale_img:
                raise StaleElementReferenceException("Element detached")
            if args and args[0] is fresh_img:
                return 500
        return None

    mock_driver.find_element.side_effect = lambda by, value=None: mock_sid_input
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1000001-0020265222", kind="sid")
    target_date = date(2026, 10, 22)

    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)

    assert sample.error is None
    assert sample.image_path is not None
    assert sample.image_path.exists()
    assert sample.image_path.read_bytes() == VALID_PNG_BYTES


def test_capture_restore_script_runs_when_screenshot_raises(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    execution_order: list[str] = []

    mock_graph_img = MagicMock()

    def mock_screenshot_raise():
        execution_order.append("screenshot")
        raise ValueError("Screenshot device failed")

    type(mock_graph_img).screenshot_as_png = property(lambda self: mock_screenshot_raise())

    def mock_execute_script(script, *args):
        if "MRTG restore script" in script:
            execution_order.append("restore")
            return None
        if "MRTG isolate script" in script:
            execution_order.append("isolate")
            return True
        if "naturalWidth" in script:
            return 500
        return None

    mock_driver.find_element.side_effect = lambda by, value=None: mock_sid_input
    mock_driver.find_elements.side_effect = lambda by, value=None: (
        [mock_graph_img] if "graph.php" in str(value) else [MagicMock(), MagicMock()]
    )
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="SID-RAISE", kind="sid")
    target_date = date(2026, 10, 22)

    sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)
    assert sample.error is not None
    assert "Screenshot device failed" in sample.error
    assert execution_order == ["isolate", "screenshot", "restore"]
    mock_driver.save_screenshot.assert_not_called()


def test_capture_blank_or_small_png_reported_as_failure_and_triggers_retry(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    mock_graph_img = MagicMock()
    # Blank white rectangle of ~1.4 KB as verified on the Debian host
    small_white_bytes = b"\x89PNG\r\n\x1a\n" + b"\x00" * 1400
    mock_graph_img.screenshot_as_png = small_white_bytes

    def mock_execute_script(script, *args):
        if "MRTG isolate script" in script or "_mrtg_target" in script:
            return True
        if "naturalWidth" in script:
            return 500
        return None

    mock_driver.find_element.side_effect = lambda by, value=None: mock_sid_input
    mock_driver.find_elements.side_effect = lambda by, value=None: (
        [mock_graph_img] if "graph.php" in str(value) else [MagicMock(), MagicMock()]
    )
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1001", kind="title", name="CORP CGK - PARTNER-DC")
    target_date = date(2026, 10, 22)

    sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)
    assert sample.error is not None
    assert "validation failed" in sample.error.lower()


def test_capture_all_retries_on_validation_failure(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        cookie_cache_path=tmp_path / "cookies.json",
        workers=1,
    )

    capture_attempts = 0

    def mock_capture(t, d, driver=None, output_dir=None):
        nonlocal capture_attempts
        capture_attempts += 1
        if capture_attempts == 1:
            return MrtgSample(
                target_id=t.target_id, error="MRTG graph screenshot validation failed"
            )
        return MrtgSample(
            target_id=t.target_id,
            inbound_maximum="25.0 M",
            outbound_maximum="10.0 M",
            image_path=output_dir / f"MRTG_{t.target_id}_{d.strftime('%Y%m%d')}.png",
        )

    targets = [MrtgTarget(target_id="3784", kind="title")]
    target_date = date(2026, 10, 22)

    with (
        patch.object(scraper, "_create_driver", side_effect=lambda **kw: MagicMock()),
        patch.object(scraper, "login", return_value=True),
        patch.object(scraper, "_apply_cached_session", return_value=True),
        patch.object(scraper, "capture", side_effect=mock_capture),
    ):
        samples = scraper.capture_all(targets, target_date, output_dir=tmp_path / "out")
        assert len(samples) == 1
        assert samples[0].error is None
        assert samples[0].inbound_maximum == "25.0 M"
        assert capture_attempts == 2


def test_validate_graph_png_rules(tmp_path: Path):
    from PIL import Image, PngImagePlugin

    nonexistent = tmp_path / "missing.png"
    assert validate_graph_png(nonexistent) is False

    small_file = tmp_path / "small.png"
    small_file.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 1000)
    assert validate_graph_png(small_file) is False

    tiny_dim = tmp_path / "tiny_dim.png"
    img_tiny = Image.new("RGB", (50, 50), (255, 0, 0))
    info = PngImagePlugin.PngInfo()
    info.add_text("pad", "x" * 5000)
    img_tiny.save(tiny_dim, pnginfo=info)
    assert tiny_dim.stat().st_size >= 2000
    assert validate_graph_png(tiny_dim) is False

    blank_white = tmp_path / "blank_white.png"
    img_white = Image.new("RGB", (200, 200), (255, 255, 255))
    img_white.save(blank_white, pnginfo=info)
    assert blank_white.stat().st_size >= 2000
    assert validate_graph_png(blank_white) is False

    sparse_graph = tmp_path / "sparse.png"
    img_sparse = Image.new("RGB", (150, 150), (255, 255, 255))
    for x in range(150):
        img_sparse.putpixel((x, 75), (0, 255, 0))
    info_sparse = PngImagePlugin.PngInfo()
    info_sparse.add_text("pad", "x" * 2500)
    img_sparse.save(sparse_graph, pnginfo=info_sparse)
    assert 2000 <= sparse_graph.stat().st_size < 20000
    assert validate_graph_png(sparse_graph) is True

    valid_file = tmp_path / "valid.png"
    valid_file.write_bytes(VALID_PNG_BYTES)
    assert validate_graph_png(valid_file) is True


def test_capture_element_screenshot_fallback_to_viewport_when_isolation_cannot_run(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    scraper.legend_extractor = MagicMock()
    scraper.legend_extractor.extract.return_value = {}

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    mock_btn_filter = MagicMock()
    mock_btn_filter.is_displayed.return_value = True
    mock_btn_filter.is_enabled.return_value = True

    mock_graph_img = MagicMock()

    def mock_save_screenshot(path_str):
        Path(path_str).write_bytes(VALID_PNG_BYTES)
        return True

    mock_driver.save_screenshot.side_effect = mock_save_screenshot

    def mock_find_element(by, value=None):
        if value == "sid":
            return mock_sid_input
        return mock_btn_filter

    def mock_find_elements(by, value=None):
        if "graph.php" in str(value):
            return [mock_graph_img]
        if "preceding::input" in str(value):
            return [MagicMock(), MagicMock()]
        return []

    def mock_execute_script(script, *args):
        if "MRTG isolate script" in script or "_mrtg_target" in script:
            return False
        if "naturalWidth" in script:
            return 500
        return None

    mock_driver.find_element.side_effect = mock_find_element
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="SID-FALLBACK", kind="sid")
    target_date = date(2026, 10, 22)

    sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)
    assert sample.error is None
    assert sample.image_path is not None
    assert sample.image_path.exists()
    assert sample.image_path.read_bytes() == VALID_PNG_BYTES
    mock_driver.save_screenshot.assert_called_once()


def test_capture_session_expired_raises_runtime_error(tmp_path: Path):
    import pytest

    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"

    mock_sid_input = MagicMock()
    mock_sid_input.is_displayed.return_value = True
    mock_sid_input.is_enabled.return_value = True

    # Simulate redirect to login page after target submit
    def simulate_submit(*args):
        mock_driver.current_url = "https://telkomcare.example.internal/public/login"

    mock_sid_input.send_keys.side_effect = simulate_submit

    mock_driver.find_element.return_value = mock_sid_input
    mock_driver.find_elements.return_value = []

    target = MrtgTarget(target_id="SID-EXPIRED", kind="sid")
    target_date = date(2026, 10, 22)

    with pytest.raises(RuntimeError, match="Netcare session expired"):
        scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)


def test_shared_login_happens_once_for_round(tmp_path: Path):
    cookie_file = tmp_path / "cookies.json"
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        username="user",
        password="pwd",
        totp_secret="AAAAAAAAAAAAAAAA",
        cookie_cache_path=cookie_file,
        workers=2,
    )

    class StubDriver:
        def __init__(self):
            self.cookies: list[dict] = []
            self.current_url = "https://telkomcare.example.internal/public/login"

        def get(self, url: str):
            if "/mrtgnetcare2" in url and self.cookies:
                self.current_url = (
                    "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"
                )

        def delete_all_cookies(self):
            self.cookies.clear()

        def add_cookie(self, cookie: dict):
            self.cookies.append(cookie)

        def get_cookies(self):
            return list(self.cookies)

        def quit(self):
            pass

    auto_login_calls = 0

    def fake_auto_login(driver) -> bool:
        nonlocal auto_login_calls
        auto_login_calls += 1
        driver.add_cookie({"name": "session_id", "value": "valid_token"})
        driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"
        return True

    targets = [MrtgTarget(target_id=f"SID-{i}", kind="sid", name=f"Link {i}") for i in range(4)]
    target_date = date(2026, 10, 22)

    with (
        patch.object(scraper, "_create_driver", side_effect=lambda **kw: StubDriver()),
        patch.object(scraper, "_auto_login", side_effect=fake_auto_login),
        patch.object(
            scraper,
            "capture",
            side_effect=lambda t, d, driver=None, output_dir=None: MrtgSample(
                target_id=t.target_id, inbound_maximum="10 M"
            ),
        ),
    ):
        samples = scraper.capture_all(targets, target_date, output_dir=tmp_path / "out")
        assert len(samples) == 4
        assert all(s.error is None for s in samples)
        assert auto_login_calls == 1
        assert cookie_file.exists()


def test_worker_with_usable_cached_session_does_not_trigger_auto_login(tmp_path: Path):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(
        json.dumps([{"name": "session_id", "value": "pre_cached"}]), encoding="utf-8"
    )

    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        username="user",
        password="pwd",
        totp_secret="AAAAAAAAAAAAAAAA",
        cookie_cache_path=cookie_file,
        workers=2,
    )

    class CachedStubDriver:
        def __init__(self):
            self.cookies: list[dict] = []
            self.current_url = "https://telkomcare.example.internal/public/login"

        def get(self, url: str):
            if "/mrtgnetcare2" in url:
                self.current_url = (
                    "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"
                )

        def delete_all_cookies(self):
            self.cookies.clear()

        def add_cookie(self, cookie: dict):
            self.cookies.append(cookie)

        def get_cookies(self):
            return [{"name": "session_id", "value": "pre_cached"}]

        def quit(self):
            pass

    targets = [MrtgTarget(target_id="SID-CACHED", kind="sid")]
    target_date = date(2026, 10, 22)

    with (
        patch.object(scraper, "_create_driver", side_effect=lambda **kw: CachedStubDriver()),
        patch.object(scraper, "_auto_login") as mock_auto_login,
        patch.object(
            scraper,
            "capture",
            return_value=MrtgSample(target_id="SID-CACHED", inbound_maximum="5 M"),
        ),
    ):
        samples = scraper.capture_all(targets, target_date, output_dir=tmp_path / "out")
        assert len(samples) == 1
        assert samples[0].error is None
        mock_auto_login.assert_not_called()


def test_target_crash_retried_once_with_new_driver_and_succeeds(tmp_path: Path):
    from selenium.common.exceptions import WebDriverException

    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        cookie_cache_path=tmp_path / "cookies.json",
        workers=1,
    )

    drivers_created: list[MagicMock] = []

    def mock_create_driver(**kw):
        d = MagicMock()
        drivers_created.append(d)
        return d

    capture_attempts = 0

    def mock_capture(t, d, driver=None, output_dir=None):
        nonlocal capture_attempts
        capture_attempts += 1
        if capture_attempts == 1:
            raise WebDriverException("Message: renderer crash\nStacktrace: backtrace details")
        return MrtgSample(
            target_id=t.target_id,
            inbound_maximum="25.0 M",
            outbound_maximum="10.0 M",
        )

    targets = [MrtgTarget(target_id="SID-RETRY", kind="sid")]
    target_date = date(2026, 10, 22)

    with (
        patch.object(scraper, "_create_driver", side_effect=mock_create_driver),
        patch.object(scraper, "login", return_value=True),
        patch.object(scraper, "_apply_cached_session", return_value=True),
        patch.object(scraper, "capture", side_effect=mock_capture),
    ):
        samples = scraper.capture_all(targets, target_date, output_dir=tmp_path / "out")
        assert len(samples) == 1
        assert samples[0].error is None
        assert samples[0].inbound_maximum == "25.0 M"
        assert capture_attempts == 2
        # drivers: 1 for shared login, 1 for worker attempt 1, 1 for worker retry
        assert len(drivers_created) == 3
        drivers_created[1].quit.assert_called_once()
        drivers_created[2].quit.assert_called_once()


def test_target_fails_thrice_yields_sample_with_error_and_no_values(tmp_path: Path):
    from selenium.common.exceptions import WebDriverException

    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        cookie_cache_path=tmp_path / "cookies.json",
        workers=1,
    )

    capture_attempts = 0

    def mock_capture_always_crash(t, d, driver=None, output_dir=None):
        nonlocal capture_attempts
        capture_attempts += 1
        raise WebDriverException(
            f"Message: renderer crash attempt {capture_attempts}\nStacktrace: fatal"
        )

    targets = [MrtgTarget(target_id="SID-FAIL-THREE", kind="sid")]
    target_date = date(2026, 10, 22)

    with (
        patch.object(scraper, "_create_driver", side_effect=lambda **kw: MagicMock()),
        patch.object(scraper, "login", return_value=True),
        patch.object(scraper, "_apply_cached_session", return_value=True),
        patch.object(scraper, "capture", side_effect=mock_capture_always_crash),
    ):
        samples = scraper.capture_all(targets, target_date, output_dir=tmp_path / "out")
        assert len(samples) == 1
        sample = samples[0]
        assert capture_attempts == 3
        assert sample.error is not None
        assert "Stacktrace" in sample.error or "WebDriverException" in sample.error
        assert sample.inbound_current is None
        assert sample.inbound_average is None
        assert sample.inbound_maximum is None
        assert sample.outbound_current is None
        assert sample.outbound_average is None
        assert sample.outbound_maximum is None


def test_round_with_three_targets_creates_and_quits_three_separate_drivers(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        cookie_cache_path=tmp_path / "cookies.json",
        workers=2,
    )

    drivers_created: list[MagicMock] = []

    def stub_driver_factory(*args, **kwargs):
        d = MagicMock()
        drivers_created.append(d)
        return d

    targets = [
        MrtgTarget(target_id="SID-1", kind="sid"),
        MrtgTarget(target_id="SID-2", kind="sid"),
        MrtgTarget(target_id="SID-3", kind="sid"),
    ]
    target_date = date(2026, 10, 22)

    with (
        patch.object(scraper, "_create_driver", return_value=MagicMock()),
        patch.object(scraper, "login", return_value=True),
        patch.object(scraper, "_apply_cached_session", return_value=True),
        patch.object(
            scraper, "_new_session_driver", side_effect=stub_driver_factory
        ) as mock_factory,
        patch.object(
            scraper,
            "capture",
            side_effect=lambda t, d, driver=None, output_dir=None: MrtgSample(
                target_id=t.target_id, inbound_maximum="10 M"
            ),
        ),
    ):
        samples = scraper.capture_all(targets, target_date, output_dir=tmp_path / "out")
        assert len(samples) == 3
        assert mock_factory.call_count == 3
        assert len(drivers_created) == 3
        for d in drivers_created:
            d.quit.assert_called_once()


def test_shared_login_driver_quit_before_target_drivers_created(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        cookie_cache_path=tmp_path / "cookies.json",
        workers=1,
    )

    call_order: list[str] = []

    shared_driver = MagicMock()
    shared_driver.quit.side_effect = lambda: call_order.append("shared_login_quit")

    def mock_create_driver(profile_subpath=None):
        call_order.append("shared_driver_created")
        return shared_driver

    def stub_driver_factory(worker_idx):
        call_order.append(f"target_driver_created_{worker_idx}")
        d = MagicMock()
        d.quit.side_effect = lambda: call_order.append(f"target_driver_quit_{worker_idx}")
        return d

    targets = [
        MrtgTarget(target_id="SID-1", kind="sid"),
        MrtgTarget(target_id="SID-2", kind="sid"),
    ]
    target_date = date(2026, 10, 22)

    with (
        patch.object(scraper, "_create_driver", side_effect=mock_create_driver),
        patch.object(scraper, "login", return_value=True),
        patch.object(scraper, "_apply_cached_session", return_value=True),
        patch.object(scraper, "_new_session_driver", side_effect=stub_driver_factory),
        patch.object(
            scraper,
            "capture",
            side_effect=lambda t, d, driver=None, output_dir=None: MrtgSample(
                target_id=t.target_id, inbound_maximum="10 M"
            ),
        ),
    ):
        samples = scraper.capture_all(targets, target_date, output_dir=tmp_path / "out")
        assert len(samples) == 2
        assert "shared_login_quit" in call_order
        quit_idx = call_order.index("shared_login_quit")
        target_creation_indices = [
            i for i, event in enumerate(call_order) if event.startswith("target_driver_created")
        ]
        assert len(target_creation_indices) == 2
        assert all(quit_idx < idx for idx in target_creation_indices)


def test_new_session_driver_isolated_profile_dir(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        profile_dir=tmp_path / "profiles",
    )
    with patch.object(scraper, "_create_driver") as mock_create:
        scraper._new_session_driver(2)
        mock_create.assert_called_once_with(profile_subpath=tmp_path / "profiles" / "target_2")


def test_capture_graphtitle_present_not_clickable(tmp_path: Path):
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    scraper.legend_extractor = MagicMock()
    scraper.legend_extractor.extract.return_value = {
        "Inbound_Current": "1 M",
        "Inbound_Average": "2 M",
        "Inbound_Maximum": "3 M",
        "Outbound_Current": "1 M",
        "Outbound_Average": "2 M",
        "Outbound_Maximum": "3 M",
    }

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph"
    mock_alert = MagicMock()
    mock_driver.switch_to.alert = mock_alert

    mock_graphtitle_input = MagicMock()
    mock_graphtitle_input.is_displayed.return_value = False
    mock_graphtitle_input.is_enabled.return_value = True

    mock_startdate = MagicMock()
    mock_enddate = MagicMock()
    mock_filter_btn = MagicMock()

    mock_graph_img = MagicMock()
    mock_graph_img.screenshot_as_png = VALID_PNG_BYTES

    recorded_scripts: list[tuple[str, tuple]] = []

    def mock_find_element(by, value=None):
        if value == "graphtitle":
            return mock_graphtitle_input
        if value == "startdate":
            return mock_startdate
        if value == "enddate":
            return mock_enddate
        if value == "graphfilter":
            return mock_filter_btn
        return MagicMock()

    def mock_find_elements(by, value=None):
        if "graph.php" in str(value):
            return [mock_graph_img]
        return []

    def mock_execute_script(script, *args):
        recorded_scripts.append((script, args))
        if "naturalWidth" in script:
            return 500
        return None

    mock_driver.find_element.side_effect = mock_find_element
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1001", kind="title", name="CORP CGK - PARTNER-DC")
    target_date = date(2026, 10, 22)

    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)

    assert sample.error is None
    assert any("scrollIntoView" in call[0] for call in recorded_scripts)
    mock_graphtitle_input.clear.assert_called_once()
    mock_graphtitle_input.send_keys.assert_any_call("1001")


def test_capture_send_keys_raises_handled_by_javascript_fallback(tmp_path: Path):
    from selenium.common.exceptions import ElementNotInteractableException

    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        graph_timeout_seconds=2,
    )
    scraper.legend_extractor = MagicMock()
    scraper.legend_extractor.extract.return_value = {
        "Inbound_Current": "1 M",
        "Inbound_Average": "2 M",
        "Inbound_Maximum": "3 M",
        "Outbound_Current": "1 M",
        "Outbound_Average": "2 M",
        "Outbound_Maximum": "3 M",
    }

    mock_driver = MagicMock()
    mock_driver.current_url = "https://telkomcare.example.internal/mrtgnetcare2/graph"
    mock_alert = MagicMock()
    mock_driver.switch_to.alert = mock_alert

    mock_graphtitle_input = MagicMock()
    mock_graphtitle_input.send_keys.side_effect = ElementNotInteractableException(
        "Element not interactable"
    )

    mock_startdate = MagicMock()
    mock_enddate = MagicMock()
    mock_filter_btn = MagicMock()

    mock_graph_img = MagicMock()
    mock_graph_img.screenshot_as_png = VALID_PNG_BYTES

    recorded_scripts: list[tuple[str, tuple]] = []

    def mock_find_element(by, value=None):
        if value == "graphtitle":
            return mock_graphtitle_input
        if value == "startdate":
            return mock_startdate
        if value == "enddate":
            return mock_enddate
        if value == "graphfilter":
            return mock_filter_btn
        return MagicMock()

    def mock_find_elements(by, value=None):
        if "graph.php" in str(value):
            return [mock_graph_img]
        return []

    def mock_execute_script(script, *args):
        recorded_scripts.append((script, args))
        if "naturalWidth" in script:
            return 500
        return None

    mock_driver.find_element.side_effect = mock_find_element
    mock_driver.find_elements.side_effect = mock_find_elements
    mock_driver.execute_script.side_effect = mock_execute_script

    target = MrtgTarget(target_id="1001", kind="title", name="CORP CGK - PARTNER-DC")
    target_date = date(2026, 10, 22)

    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        sample = scraper.capture(target, target_date, driver=mock_driver, output_dir=tmp_path)

    assert sample.error is None
    fallback_calls = [
        call
        for call in recorded_scripts
        if "arguments[0].value" in call[0] and "Event('input'" in call[0] and "Enter" in call[0]
    ]
    assert len(fallback_calls) == 1
    script, args = fallback_calls[0]
    assert args[0] == mock_graphtitle_input
    assert args[1] == "1001"


def test_open_graph_page_title_visits_dashboard_before_graph():
    scraper = NetcareScraper(base_url="https://telkomcare.example.internal")
    requested_urls: list[str] = []

    class StubDriver:
        def __init__(self):
            self.current_url = ""

        def get(self, url: str):
            requested_urls.append(url)
            self.current_url = url

    driver = StubDriver()
    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        res_url = scraper._open_graph_page(driver, "title")

    dashboard_url = scraper.graph_root
    title_url = f"{scraper.graph_root}/graph"

    assert dashboard_url in requested_urls
    assert title_url in requested_urls
    dashboard_idx = requested_urls.index(dashboard_url)
    title_idx = requested_urls.index(title_url)
    assert dashboard_idx < title_idx
    assert res_url == title_url


def test_open_graph_page_title_retries_via_dashboard_if_redirected_to_monitoring():
    scraper = NetcareScraper(base_url="https://telkomcare.example.internal")
    requested_urls: list[str] = []

    class RedirectStubDriver:
        def __init__(self):
            self.current_url = ""
            self.graph_attempts = 0

        def get(self, url: str):
            requested_urls.append(url)
            if url == f"{scraper.graph_root}/graph":
                self.graph_attempts += 1
                if self.graph_attempts == 1:
                    self.current_url = f"{scraper.graph_root}/graph/monitoring"
                else:
                    self.current_url = url
            else:
                self.current_url = url

    driver = RedirectStubDriver()
    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        res_url = scraper._open_graph_page(driver, "title")

    dashboard_url = scraper.graph_root
    title_url = f"{scraper.graph_root}/graph"

    assert requested_urls == [dashboard_url, title_url, dashboard_url, title_url]
    assert res_url == title_url


def test_open_graph_page_sid_navigates_to_monitoring_page():
    scraper = NetcareScraper(base_url="https://telkomcare.example.internal")
    requested_urls: list[str] = []

    class FreshStubDriver:
        def __init__(self):
            self.current_url = ""

        def get(self, url: str):
            requested_urls.append(url)
            self.current_url = url

    driver = FreshStubDriver()
    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        res_url = scraper._open_graph_page(driver, "sid")

    dashboard_url = scraper.graph_root
    monitoring_url = f"{scraper.graph_root}/graph/monitoring"

    assert requested_urls == [dashboard_url, monitoring_url]
    assert res_url == monitoring_url

    requested_urls.clear()
    with patch("network_report.collectors.netcare_mrtg.time.sleep"):
        res_url2 = scraper._open_graph_page(driver, "sid")
    assert requested_urls == []
    assert res_url2 == monitoring_url


def test_login_cookie_reuse_branch_url_no_doubled_mrtgnetcare2(tmp_path: Path):
    cookie_file = tmp_path / "cookies.json"
    cookie_file.write_text(json.dumps([{"name": "session_id", "value": "dummy"}]), encoding="utf-8")
    scraper = NetcareScraper(
        base_url="https://telkomcare.example.internal",
        cookie_cache_path=cookie_file,
    )
    requested_urls: list[str] = []

    class MockDriver:
        def __init__(self):
            self.current_url = ""

        def get(self, url: str):
            requested_urls.append(url)
            self.current_url = url

        def delete_all_cookies(self):
            pass

        def add_cookie(self, c):
            pass

    driver = MockDriver()
    scraper.login(driver)

    monitoring_requests = [u for u in requested_urls if "/graph/monitoring" in u]
    assert len(monitoring_requests) >= 1
    for url in monitoring_requests:
        assert url.count("/mrtgnetcare2") == 1
        assert "/mrtgnetcare2/mrtgnetcare2" not in url
        assert url == "https://telkomcare.example.internal/mrtgnetcare2/graph/monitoring"
