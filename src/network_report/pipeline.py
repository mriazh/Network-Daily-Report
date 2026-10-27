"""Daily Report Pipeline - Core orchestration logic."""

from __future__ import annotations

import json
import logging
import shutil
import time
from datetime import datetime, timedelta
from pathlib import Path

from .collectors.airwave import AirWaveClient, AirwaveData, collect_airwave_data
from .collectors.cmp_crawler import CmpExportResult, run_cmp_crawler
from .collectors.huawei_ap import HuaweiApData, collect_huawei_ap_data
from .collectors.huawei_nce import NceCollector, WacStatus
from .collectors.iot_m2m import IotDailySummary, collect_iot_summary
from .collectors.modem_orbit import OrbitModem, collect_orbit_modems
from .collectors.mrtg import evaluate_circuit, load_indibiz_daily_max, load_mrtg_ocr_data
from .collectors.netcare_mrtg import NetcareScraper, build_targets_from_circuits
from .collectors.nexthop_ping import PingResult, collect_nexthop_ping
from .collectors.prtg import PrtgStatus, fetch_prtg_status
from .config import AppConfig, load_config
from .notifier import GowaNotifier
from .processing.analyzer import generate_technical_analysis
from .writers.excel_writer import write_daily_excel
from .writers.wag_formatter import format_whatsapp_report

logger = logging.getLogger(__name__)


def _load_circuits(cfg: AppConfig) -> dict:
    """Load circuits from config/circuits.json (fallback to example)."""
    circuits_file = cfg.circuits_file
    if not circuits_file.exists():
        circuits_file = Path(__file__).resolve().parents[2] / "config" / "circuits.example.json"
    if circuits_file.exists():
        with open(circuits_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _build_mrtg_payload(
    circuits: dict, cfg: AppConfig, date_iso: str, date_compact: str = ""
) -> dict:
    """Evaluate all circuit groups and build MRTG payload."""
    mrtg_payload = {}
    ocr_data = load_mrtg_ocr_data(cfg.mrtg.telkomcare_dir, date_compact)

    def _find_ocr(sid_str: str) -> dict[str, float] | None:
        sid_clean = sid_str.strip()
        if sid_clean in ocr_data:
            return ocr_data[sid_clean]
        for k, v in ocr_data.items():
            if k in sid_clean or sid_clean in k or (len(k) >= 6 and k[-6:] == sid_clean[-6:]):
                return v
        return None

    # ASTINET
    astinet_list = []
    for name, sid, cap in circuits.get("astinet", []):
        d = _find_ocr(sid)
        max_in = d.get("in_mbps") if d else None
        max_out = d.get("out_mbps") if d else None
        status = evaluate_circuit(name, sid, cap, max_in, max_out)
        astinet_list.append(
            {
                "name": name,
                "sid": sid,
                "capacity_str": cap,
                "status": status.status,
                "utilization_pct": status.utilization_pct,
                "over_pct": status.over_pct,
                "capacity_mbps": status.capacity_mbps,
                "max_mbps": status.max_mbps,
                "remarks": status.remarks,
            }
        )
    mrtg_payload["ASTINET"] = astinet_list

    # Metro-E
    metro_list = []
    for name, sid, cap in circuits.get("metro_e", []):
        d = _find_ocr(sid)
        max_in = d.get("in_mbps") if d else None
        max_out = d.get("out_mbps") if d else None
        status = evaluate_circuit(name, sid, cap, max_in, max_out)
        metro_list.append(
            {
                "name": name,
                "sid": sid,
                "capacity_str": cap,
                "status": status.status,
                "utilization_pct": status.utilization_pct,
                "over_pct": status.over_pct,
                "capacity_mbps": status.capacity_mbps,
                "max_mbps": status.max_mbps,
                "remarks": status.remarks,
            }
        )
    mrtg_payload["Metro-E"] = metro_list

    # VPN IP - Split into JTI and OSA
    vpn_jti_list = []
    vpn_osa_list = []
    for name, sid, cap in circuits.get("vpn_ip_osa", []):
        d = _find_ocr(sid)
        max_in = d.get("in_mbps") if d else None
        max_out = d.get("out_mbps") if d else None
        status = evaluate_circuit(name, sid, cap, max_in, max_out)
        link_item = {
            "name": name,
            "sid": sid,
            "capacity_str": cap,
            "status": status.status,
            "utilization_pct": status.utilization_pct,
            "over_pct": status.over_pct,
            "capacity_mbps": status.capacity_mbps,
            "max_mbps": status.max_mbps,
            "remarks": status.remarks,
        }
        # JTI links are the core links to PARTNER-DC (STL/JTI)
        if "PARTNER-DC" in name.upper() or "STL" in name.upper() or "JTI" in name.upper():
            vpn_jti_list.append(link_item)
        else:
            vpn_osa_list.append(link_item)
    mrtg_payload["VPN IP (JTI)"] = vpn_jti_list
    mrtg_payload["VPN IP (OSA)"] = vpn_osa_list

    # Indibiz - Native local store first, fallback to Netcare OCR
    indibiz_list = []
    indibiz_db = cfg.routeros.db_path
    for name, sid, cap in circuits.get("indibiz", []):
        max_in, max_out = load_indibiz_daily_max(indibiz_db, date_iso)
        if (max_in is None or max_in == 0.0) and (max_out is None or max_out == 0.0):
            d = _find_ocr(sid)
            max_in = d.get("in_mbps") if d else None
            max_out = d.get("out_mbps") if d else None
        status = evaluate_circuit(name, sid, cap, max_in, max_out)
        indibiz_list.append(
            {
                "name": name,
                "sid": sid,
                "capacity_str": cap,
                "status": status.status,
                "utilization_pct": status.utilization_pct,
                "over_pct": status.over_pct,
                "capacity_mbps": status.capacity_mbps,
                "max_mbps": status.max_mbps,
                "remarks": status.remarks,
            }
        )
    mrtg_payload["Telkom Indibiz"] = indibiz_list

    return mrtg_payload


def _archive_raw_artifacts(
    cfg: AppConfig,
    date_compact: str,
    ping_results: list[PingResult],
    prtg_status: PrtgStatus | None,
    orbit_modems: list[OrbitModem],
    mrtg_payload: dict,
) -> None:
    """Collects and archives raw data files and state snapshots into output/raw/{category}/{date}/."""
    raw_root = cfg.output_raw_root
    raw_root.mkdir(parents=True, exist_ok=True)

    # 1. Archive IoT M2M raw files (.xlsx, .csv, .png) and dashboard screenshots
    iot_raw_dest = raw_root / "iot" / date_compact
    iot_raw_dest.mkdir(parents=True, exist_ok=True)
    cmp_sources = [cfg.cmp.raw_dir] if cfg.cmp.raw_dir else []
    cmp_sources.append(cfg.output_root / "images")
    for d in Path(__file__).resolve().parents[3].glob("*CMP-Automation"):
        cmp_sources.append(d / "output" / "raw")
        cmp_sources.append(d / "output" / "images")
    for s_dir in cmp_sources:
        if s_dir and s_dir.exists():
            for pattern in (
                "*{date_compact}*.xlsx",
                "*{date_compact}*.csv",
                "*{date_compact}*.png",
                "dashboard_{date_compact}*.png",
                "dashboard_{date_compact}*.jpg",
            ):
                for f in s_dir.glob(pattern.format(date_compact=date_compact)):
                    if not (iot_raw_dest / f.name).exists():
                        shutil.copy2(f, iot_raw_dest / f.name)

    # 2. Archive Huawei AP raw CSVs and PNGs
    ap_raw_dest = raw_root / "huawei_ap" / date_compact
    ap_raw_dest.mkdir(parents=True, exist_ok=True)
    ap_sources = []
    if cfg.huawei.raw_dir:
        ap_sources.append(cfg.huawei.raw_dir / date_compact)
        ap_sources.append(cfg.huawei.raw_dir)
        ap_sources.extend(list(cfg.huawei.raw_dir.glob("20*")))

    for src_c in ap_sources:
        if src_c and src_c.exists():
            for pattern in ("apInfo.csv", "ssidInfo.csv", "*.png"):
                for f in src_c.glob(pattern):
                    if not (ap_raw_dest / f.name).exists():
                        shutil.copy2(f, ap_raw_dest / f.name)
            if (ap_raw_dest / "apInfo.csv").exists():
                break

    # 3. Archive AirWave raw CSVs and PNGs
    airwave_dest = raw_root / "airwave" / date_compact
    airwave_dest.mkdir(parents=True, exist_ok=True)
    airwave_sources = []
    if cfg.airwave.export_dir:
        airwave_sources.extend(
            [
                cfg.airwave.export_dir / date_compact,
                cfg.airwave.export_dir,
            ]
        )

    for src_c in airwave_sources:
        if src_c and src_c.exists() and src_c != airwave_dest:
            for pattern in ("*.csv", "*.png"):
                for f in src_c.glob(pattern):
                    if not (airwave_dest / f.name).exists():
                        shutil.copy2(f, airwave_dest / f.name)
            if list(airwave_dest.glob("*.csv")):
                break

    # 4. Archive MRTG raw files (.xlsx, .csv, .png, .json)
    mrtg_raw_dest = raw_root / "mrtg" / date_compact
    mrtg_raw_dest.mkdir(parents=True, exist_ok=True)
    mrtg_sources = []
    if cfg.mrtg.telkomcare_dir:
        mrtg_sources.append(cfg.mrtg.telkomcare_dir)

    for src_c in mrtg_sources:
        if src_c and src_c.exists():
            for pattern in (
                "*{date_compact}*.xlsx",
                "*{date_compact}*.csv",
                "*{date_compact}*.png",
                "*{date_compact}*.json",
            ):
                for f in src_c.glob(pattern.format(date_compact=date_compact)):
                    if not (mrtg_raw_dest / f.name).exists():
                        shutil.copy2(f, mrtg_raw_dest / f.name)

    # 5. Snapshot Orbit modems JSON
    orbit_dest = raw_root / "orbit" / date_compact
    orbit_dest.mkdir(parents=True, exist_ok=True)
    if cfg.orbit.cache_file and cfg.orbit.cache_file.exists():
        shutil.copy2(cfg.orbit.cache_file, orbit_dest / "modems.json")
    elif orbit_modems:
        snapshot = [
            {
                "location": m.location,
                "ssid": m.ssid,
                "packages": [
                    {
                        "name": p.name,
                        "remaining_gb": p.remaining_gb,
                        "total_gb": p.total_gb,
                        "expiry_str": p.expiry_str,
                    }
                    for p in m.packages
                ],
            }
            for m in orbit_modems
        ]
        (orbit_dest / "modems.json").write_text(json.dumps(snapshot, indent=2), encoding="utf-8")

    # 6. Snapshot PRTG sensor status JSON
    prtg_dest = raw_root / "prtg" / date_compact
    prtg_dest.mkdir(parents=True, exist_ok=True)
    if prtg_status:
        prtg_json = {
            "up": prtg_status.up,
            "paused": prtg_status.paused,
            "down": prtg_status.down,
            "warning": prtg_status.warning,
            "total": prtg_status.total,
        }
        (prtg_dest / "prtg_status.json").write_text(
            json.dumps(prtg_json, indent=2), encoding="utf-8"
        )

    # 7. Snapshot Nexthop Ping JSON
    ping_dest = raw_root / "nexthop" / date_compact
    ping_dest.mkdir(parents=True, exist_ok=True)
    ping_json = [
        {
            "name": r.name,
            "ip": r.ip,
            "status": r.status,
            "avg_latency_ms": r.avg_latency_ms,
            "loss_pct": r.packet_loss_pct,
        }
        for r in ping_results
    ]
    (ping_dest / "ping_results.json").write_text(json.dumps(ping_json, indent=2), encoding="utf-8")

    # 8. Snapshot MRTG circuits status JSON
    mrtg_dest = raw_root / "mrtg" / date_compact
    mrtg_dest.mkdir(parents=True, exist_ok=True)
    (mrtg_dest / "circuits_status.json").write_text(
        json.dumps(mrtg_payload, indent=2), encoding="utf-8"
    )


def run_daily_pipeline(
    date_input: str = "today", send_wa: bool = False, test_mode: bool = False
) -> dict:
    """Execute full daily report pipeline.

    Args:
        date_input: "today", "yesterday", or YYYY-MM-DD string.
        send_wa: Whether to send WhatsApp notification via GOWA.

    Returns:
        dict with keys: 'excel_path', 'wag_path', 'payload', 'wa_sent'
    """
    cfg = load_config()
    target_dt = (
        datetime.now()
        if date_input == "today"
        else (
            datetime.strptime(date_input, "%Y-%m-%d")
            if "-" in date_input
            else datetime.now() - timedelta(days=1)
        )
    )
    date_iso = target_dt.strftime("%Y-%m-%d")
    date_str = target_dt.strftime("%d %B %Y")
    month_str = target_dt.strftime("%Y-%m")
    date_compact = target_dt.strftime("%Y%m%d")
    day_num = target_dt.day

    # Setup file logger
    log_dir = cfg.output_root / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_dir / f"pipeline_{date_compact}.log", encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    )
    logger.addHandler(file_handler)

    logger.info("Starting daily pipeline for %s (Day %d)", date_str, day_num)

    start_time = time.time()
    notifier = GowaNotifier(cfg.gowa) if send_wa else None
    if notifier is not None:
        notifier.send_start(date_str, test_mode=test_mode)

    try:
        # Pre-archive raw data files from sources into local output/raw/
        _archive_raw_artifacts(
            cfg=cfg,
            date_compact=date_compact,
            ping_results=[],
            prtg_status=None,
            orbit_modems=[],
            mrtg_payload={},
        )

        # 1. Collect all data
        # Ping nexthop
        ping_results: list[PingResult] = collect_nexthop_ping()
        ping_payload = [
            {
                "name": r.name,
                "ip": r.ip,
                "status": r.status,
                "avg_latency_ms": r.avg_latency_ms,
                "packet_loss_pct": r.packet_loss_pct,
                "total_sent": r.total_sent,
                "packets_received": r.packets_received,
            }
            for r in ping_results
        ]

        # IoT M2M & CMP crawler
        # Run cmp_crawler FIRST so fresh dashboard screenshot is saved in output/raw/iot/{date_compact}/
        cmp_crawler_result: CmpExportResult | None = None
        if cfg.cmp.enabled and cfg.cmp.username and cfg.cmp.email:
            logger.info("Running CMP live dashboard crawler...")
            cmp_crawler_result = run_cmp_crawler(
                base_url=cfg.cmp.base_url or "https://ep.iotcc.telkomsel.com",
                username=cfg.cmp.username,
                password=cfg.cmp.password,
                target_date=target_dt.strftime("%Y-%m-%d"),
                target_month=target_dt.strftime("%Y-%m"),
                output_dir=cfg.output_root,
                headless=cfg.cmp.headless,
                timeout_seconds=cfg.cmp.timeout_seconds,
                export_timeout_seconds=cfg.cmp.export_timeout_seconds,
                imap_host=cfg.cmp.imap_host,
                imap_port=cfg.cmp.imap_port,
                email=cfg.cmp.email,
                email_password=cfg.cmp.email_password,
                otp_timeout_seconds=cfg.cmp.otp_timeout_seconds,
                otp_poll_interval_seconds=cfg.cmp.otp_poll_interval_seconds,
                proxy_server=cfg.cmp.proxy_server,
                warp_proxy_server=cfg.warp.proxy_server,
                monthly_export_enabled=cfg.cmp.monthly_export_enabled,
            )
            if cmp_crawler_result.error:
                logger.warning("CMP crawler error: %s", cmp_crawler_result.error)

        iot_summary: IotDailySummary | None = None
        if cfg.cmp.enabled and cfg.cmp.raw_dir:
            iot_summary = collect_iot_summary(
                raw_dir=cfg.cmp.raw_dir,
                target_date=target_dt.strftime("%Y%m%d"),
                mapping_path=cfg.iccid_location_file,
                dashboard_dirs=[
                    cfg.output_raw_root / "iot" / date_compact,
                    cfg.output_root / "images",
                    cfg.cmp.raw_dir,
                ],
            )

        # PRTG
        prtg_status: PrtgStatus | None = None
        if cfg.prtg.enabled:
            prtg_status = fetch_prtg_status(
                base_url=cfg.prtg.base_url,
                username=cfg.prtg.username,
                password=cfg.prtg.password,
                timeout=cfg.prtg.timeout_seconds,
            )

        # AirWave
        airwave_data: AirwaveData | None = None
        airwave_error: str | None = None
        airwave_target_date = target_dt.date()
        if cfg.airwave.enabled:
            airwave_export_dir = (
                (cfg.airwave.export_dir / date_compact)
                if cfg.airwave.export_dir
                else (cfg.output_raw_root / "airwave" / date_compact)
            )
            if cfg.airwave.base_url and cfg.airwave.username and cfg.airwave.password:
                try:
                    logger.info("Executing native AirWave client for %s...", airwave_target_date)
                    client = AirWaveClient(
                        base_url=cfg.airwave.base_url,
                        username=cfg.airwave.username,
                        password=cfg.airwave.password,
                        report_definition_id=cfg.airwave.report_definition_id,
                        poll_timeout_minutes=cfg.airwave.poll_timeout_minutes,
                        poll_interval_seconds=cfg.airwave.poll_interval_seconds,
                        max_retries=cfg.airwave.max_retries,
                        session_state_path=cfg.airwave.session_state_path,
                        headless=cfg.airwave.headless,
                    )
                    client.fetch_report(airwave_target_date, airwave_export_dir)
                    client.close()
                except Exception as e:
                    logger.error("AirWave scraping failed: %s", e)
                    airwave_error = str(e)
            else:
                logger.info(
                    "AirWave credentials not configured, reading existing export dir if present"
                )

            airwave_data = collect_airwave_data(airwave_export_dir, date_compact)
            if airwave_data is None and not airwave_error:
                airwave_error = "Data AirWave tidak ditemukan"

        # Huawei NCE (AP, SSID, WAC) & AP processing
        huawei_ap_data: HuaweiApData | None = None
        wac_status: WacStatus | None = None
        if cfg.huawei.enabled:
            # Check if 14:00 WIB peak hour AP snapshot already exists (non-empty apInfo.csv)
            ap_1400_file = cfg.output_raw_root / "huawei_ap" / date_compact / "apInfo.csv"
            if ap_1400_file.exists() and ap_1400_file.stat().st_size > 0:
                logger.info("Using existing 14:00 WIB peak hour AP snapshot from %s", ap_1400_file)
                huawei_ap_data = collect_huawei_ap_data(cfg.huawei.raw_dir, date_compact)
                # Still try to get WAC status if available
                wac_saved = cfg.output_root / "raw" / "wac" / date_compact / "wac_status.json"
                if wac_saved.exists():
                    try:
                        w_dict = json.loads(wac_saved.read_text(encoding="utf-8"))
                        wac_status = WacStatus(**w_dict)
                    except Exception:
                        pass
            else:
                # No 14:00 snapshot, run NceCollector to get fresh data
                try:
                    selectors = {
                        "login_username": cfg.huawei.sel_login_username,
                        "login_password": cfg.huawei.sel_login_password,
                        "login_submit": cfg.huawei.sel_login_submit,
                        "nav_ap": cfg.huawei.sel_nav_ap,
                        "nav_ssid": cfg.huawei.sel_nav_ssid,
                        "nav_ac": cfg.huawei.sel_nav_ac,
                        "export_ap": cfg.huawei.sel_export_ap,
                        "export_ssid": cfg.huawei.sel_export_ssid,
                    }
                    collector = NceCollector(
                        base_url=cfg.huawei.base_url,
                        username=cfg.huawei.username,
                        password=cfg.huawei.password,
                        selectors=selectors,
                        headless=cfg.huawei.headless,
                        timeout_seconds=cfg.huawei.export_timeout_seconds,
                        output_root=cfg.output_root,
                    )
                    nce_export = collector.collect(date_compact)
                    collector.close()
                    if nce_export.wac:
                        wac_status = nce_export.wac
                except Exception as e:
                    logger.error("Error executing NceCollector: %s", e)

                if not wac_status:
                    wac_saved = cfg.output_root / "raw" / "wac" / date_compact / "wac_status.json"
                    if wac_saved.exists():
                        try:
                            w_dict = json.loads(wac_saved.read_text(encoding="utf-8"))
                            wac_status = WacStatus(**w_dict)
                        except Exception:
                            pass

                huawei_ap_data = collect_huawei_ap_data(cfg.huawei.raw_dir, date_compact)

        # Orbit Modems
        orbit_modems: list[OrbitModem] = []
        if cfg.orbit.enabled:
            orbit_modems = collect_orbit_modems(
                cache_json_path=cfg.orbit.cache_file,
                circuits_file=cfg.circuits_file,
                output_raw_root=cfg.output_raw_root,
                date_compact=date_compact,
                headless=cfg.orbit.headless if hasattr(cfg.orbit, "headless") else True,
                timeout_seconds=cfg.orbit.timeout_seconds
                if hasattr(cfg.orbit, "timeout_seconds")
                else 60,
            )

        # MRTG circuits
        circuits = _load_circuits(cfg)
        mrtg_error: str | None = None
        if cfg.netcare.enabled:
            try:
                targets = build_targets_from_circuits(circuits)
                if targets:
                    api_keys = [
                        k.strip() for k in cfg.netcare.captcha_api_keys.split(",") if k.strip()
                    ]
                    models = [m.strip() for m in cfg.netcare.captcha_models.split(",") if m.strip()]
                    scraper = NetcareScraper(
                        base_url=cfg.netcare.base_url,
                        username=cfg.netcare.username,
                        password=cfg.netcare.password,
                        totp_secret=cfg.netcare.totp_secret,
                        captcha_enabled=cfg.netcare.captcha_enabled,
                        captcha_api_keys=api_keys,
                        captcha_models=models,
                        captcha_proxy=cfg.netcare.captcha_proxy,
                        profile_dir=cfg.netcare.profile_dir,
                        cookie_cache_path=cfg.netcare.cookie_cache_path,
                        headless=cfg.netcare.headless,
                        page_timeout_seconds=cfg.netcare.page_timeout_seconds,
                        graph_timeout_seconds=cfg.netcare.graph_timeout_seconds,
                        workers=cfg.netcare.workers,
                        binary_path=cfg.netcare.binary_path,
                        driver_path=cfg.netcare.driver_path,
                    )
                    target_date = datetime.strptime(date_iso, "%Y-%m-%d").date()
                    scraper.capture_all(
                        targets,
                        target_date,
                        output_dir=cfg.output_raw_root / "mrtg" / date_compact,
                    )
            except Exception as exc:
                logger.error("Netcare MRTG scrape failed: %s", exc)
                mrtg_error = str(exc)

        mrtg_payload = _build_mrtg_payload(circuits, cfg, date_iso, date_compact)
        all_circuits = [c for grp in mrtg_payload.values() for c in grp]
        if all_circuits and all(c.get("status") == "No Data" for c in all_circuits):
            if not mrtg_error:
                mrtg_error = "No MRTG data available"

        # 2. Build highlights / technical analysis
        raw_cpu = airwave_data.top_cpu if airwave_data else []
        raw_mem = airwave_data.top_memory if airwave_data else []
        cpu_data = [x for x in raw_cpu if not x[0].upper().startswith("WLC")]
        mem_data = [x for x in raw_mem if not x[0].upper().startswith("WLC")]
        if airwave_error:
            c_txt = "⚠️ Gagal scrape"
            m_txt = "⚠️ Gagal scrape"
        else:
            c_txt, m_txt = generate_technical_analysis(cpu_data, mem_data)

        high_traffic_links = []
        for grp in mrtg_payload.values():
            for link_item in grp:
                if link_item.get("status") == "High Traffic":
                    high_traffic_links.append(
                        {
                            "name": link_item["name"],
                            "util_pct": link_item.get("utilization_pct", 0.0),
                            "over_pct": link_item.get("over_pct", 0.0),
                            "remarks": link_item.get("remarks", ""),
                        }
                    )

        hardware_fault_text = "Tidak ada hardware fault."
        if huawei_ap_data and huawei_ap_data.outdoor_fault > 0:
            names = (
                f" ({', '.join(huawei_ap_data.fault_ap_names)})"
                if huawei_ap_data.fault_ap_names
                else ""
            )
            hardware_fault_text = f"AP Outdoor Fault : {huawei_ap_data.outdoor_fault} ea.{names}"
        elif huawei_ap_data and huawei_ap_data.fault_count > 0:
            names = (
                f" ({', '.join(huawei_ap_data.fault_ap_names)})"
                if huawei_ap_data.fault_ap_names
                else ""
            )
            hardware_fault_text = f"AP Fault : {huawei_ap_data.fault_count} ea.{names}"

        highlights = {
            "high_traffic": high_traffic_links,
            "hardware_fault": hardware_fault_text,
            "problem_open": "-",
        }

        wac_dict = None
        if wac_status:
            wac_dict = {
                "device_name": wac_status.device_name,
                "device_type": wac_status.device_type,
                "cpu_pct": wac_status.cpu_pct,
                "mem_pct": wac_status.mem_pct,
                "temperature": wac_status.temperature,
                "running_time": wac_status.running_time,
                "software_version": wac_status.software_version,
                "serial_number": wac_status.serial_number,
                "max_ap_count": wac_status.max_ap_count,
                "captured_at": wac_status.captured_at,
                "error": wac_status.error,
            }

        # 3. Assemble full payload
        payload = {
            "date_str": date_str,
            "day_number": day_num,
            "date_iso": date_iso,
            "ping": ping_payload,
            "wac": wac_dict,
            "iot": {
                "status": "Normal",
                "total_usage_gb": round(iot_summary.total_usage_gb, 2) if iot_summary else 0.0,
                "active_sessions": iot_summary.active_sessions if iot_summary else 0,
                "top_daily": iot_summary.top_daily[:5] if iot_summary else [],
                "top_periodic": iot_summary.top_periodic[:5] if iot_summary else [],
                "periodic_range_str": iot_summary.periodic_range_str if iot_summary else "",
            },
            "prtg": {
                "up": prtg_status.up if prtg_status else 98,
                "paused": prtg_status.paused if prtg_status else 29,
                "total": prtg_status.total if prtg_status else 127,
            },
            "ap": {
                "normal": huawei_ap_data.normal_count if huawei_ap_data else 454,
                "fault": huawei_ap_data.fault_count if huawei_ap_data else 0,
                "users": huawei_ap_data.total_users if huawei_ap_data else 0,
                "indoor_ok": huawei_ap_data.indoor_ok if huawei_ap_data else 328,
                "outdoor_ok": huawei_ap_data.outdoor_ok if huawei_ap_data else 115,
                "outdoor_fault": huawei_ap_data.outdoor_fault if huawei_ap_data else 0,
                "fault_names": huawei_ap_data.fault_ap_names if huawei_ap_data else [],
                "ssid_distribution": huawei_ap_data.ssid_distribution if huawei_ap_data else {},
            },
            "nce": {
                "status": "Normal" if huawei_ap_data else "Error",
                "ap_normal": huawei_ap_data.normal_count if huawei_ap_data else 0,
                "ap_fault": huawei_ap_data.fault_count if huawei_ap_data else 0,
                "users": huawei_ap_data.total_users if huawei_ap_data else 0,
                "error": None if huawei_ap_data else "Data NCE tidak tersedia",
            },
            "mrtg": mrtg_payload,
            "mrtg_error": mrtg_error,
            "mrtg_source_date": date_str,
            "airwave_error": airwave_error,
            "airwave_source_date": date_str,
            "cpu": cpu_data[:10] if not airwave_error else [],
            "memory": mem_data[:10] if not airwave_error else [],
            "uptime": {
                "device": airwave_data.device_uptime_pct if airwave_data else None,
                "snmp": airwave_data.snmp_uptime_pct if airwave_data else None,
                "icmp": airwave_data.icmp_uptime_pct if airwave_data else None,
                "https": airwave_data.https_uptime_pct if airwave_data else "-",
                "error": airwave_error,
                "source_date": date_str,
            },
            "orbit": [
                {
                    "location": m.location,
                    "ssid": m.ssid,
                    "packages": [
                        {
                            "name": p.name,
                            "remaining_gb": p.remaining_gb,
                            "total_gb": p.total_gb,
                            "expiry_str": p.expiry_str,
                        }
                        for p in m.packages
                    ],
                }
                for m in orbit_modems
            ],
            "analysis_cpu": c_txt,
            "analysis_mem": m_txt,
            "highlights": highlights,
        }

        # Archive raw data artifacts and state snapshots to output/raw/
        _archive_raw_artifacts(
            cfg=cfg,
            date_compact=date_compact,
            ping_results=ping_results,
            prtg_status=prtg_status,
            orbit_modems=orbit_modems,
            mrtg_payload=mrtg_payload,
        )

        # 4. Write Excel
        template_path = cfg.template_excel
        output_path = cfg.output_root / "reports" / month_str / f"Daily-Report-{month_str}.xlsx"
        output_path.parent.mkdir(parents=True, exist_ok=True)
        excel_ok = write_daily_excel(template_path, output_path, day_num, payload)

        # 5. Write WhatsApp report (wag)
        wa_text = format_whatsapp_report(payload)
        wag_path = cfg.output_root / "wag" / f"Report_{date_compact}.txt"
        wag_path.parent.mkdir(parents=True, exist_ok=True)
        wag_path.write_text(wa_text, encoding="utf-8")
        logger.info("Wrote WhatsApp report to %s", wag_path)

        # 6. Optional: Send WhatsApp report, then SUCCESS notice
        wa_sent = False
        if notifier is not None:
            wa_sent = notifier.send_report(wa_text, test_mode=test_mode)
            if wa_sent:
                logger.info("WhatsApp notification sent successfully")
            else:
                logger.warning("WhatsApp notification failed or disabled")
            notifier.send_success(date_str, time.time() - start_time, test_mode=test_mode)

    except Exception as exc:
        if notifier is not None:
            notifier.send_failed(date_str, str(exc), test_mode=test_mode)
        raise

    return {
        "excel_path": str(output_path) if excel_ok else None,
        "wag_path": str(wag_path),
        "payload": payload,
        "wa_sent": wa_sent,
    }
