"""MRTG Network Connectivity Collector."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class CircuitStatus:
    name: str
    sid: str
    capacity_str: str
    capacity_mbps: float
    max_mbps: float | None
    utilization_pct: float | None
    over_pct: float | None
    over_mbps: float | None
    direction: str | None
    status: str  # "Normal" / "High Traffic" / "Fault"
    remarks: str


def parse_capacity_to_mbps(cap_str: str) -> float:
    cap = cap_str.strip().upper()
    if "GB" in cap:
        num = float(cap.replace("GB", "").strip())
        return num * 1024.0
    if "MB" in cap:
        return float(cap.replace("MB", "").strip())
    return 10.0


def parse_mrtg_speed_mbps(text: str | None) -> float:
    """Parses MRTG speed string like '12.31 M', '745.11 k', or '623.25' into numeric Mbps."""
    if not text or text == "-" or "n/a" in str(text).lower():
        return 0.0
    val_clean = str(text).replace(",", ".").strip()
    match = re.search(r"([\d.]+)\s*([kKmMgG])?", val_clean)
    if not match:
        return 0.0
    num = float(match.group(1))
    unit = (match.group(2) or "").upper()
    if unit == "K":
        return num / 1000.0
    elif unit == "M":
        return num
    elif unit == "G":
        return num * 1000.0
    else:
        return num / 1_000_000.0  # Raw bps without unit


def evaluate_circuit(
    name: str,
    sid: str,
    capacity_str: str,
    max_in_mbps: float | None = None,
    max_out_mbps: float | None = None,
    threshold_pct: float = 100.0,
) -> CircuitStatus:
    cap_mbps = parse_capacity_to_mbps(capacity_str)
    in_val = max_in_mbps or 0.0
    out_val = max_out_mbps or 0.0
    highest = max(in_val, out_val)

    if highest <= 0.0:
        return CircuitStatus(
            name=name,
            sid=sid,
            capacity_str=capacity_str,
            capacity_mbps=cap_mbps,
            max_mbps=None,
            utilization_pct=None,
            over_pct=None,
            over_mbps=None,
            direction=None,
            status="No Data",
            remarks="",
        )

    util_pct = (highest / cap_mbps) * 100.0
    if util_pct > threshold_pct:
        over = round(util_pct - 100.0, 2)
        over_mbps = round(highest - cap_mbps, 2)
        direction = "inbound" if in_val >= out_val else "outbound"
        status = "High Traffic"
        remarks = f"High Traffic ({direction} >{over_mbps:.2f} MB / >{over:.2f}%)"
        return CircuitStatus(
            name=name,
            sid=sid,
            capacity_str=capacity_str,
            capacity_mbps=cap_mbps,
            max_mbps=highest,
            utilization_pct=round(util_pct, 2),
            over_pct=over,
            over_mbps=over_mbps,
            direction=direction,
            status=status,
            remarks=remarks,
        )
    else:
        status = "Normal"
        remarks = "-"
        return CircuitStatus(
            name=name,
            sid=sid,
            capacity_str=capacity_str,
            capacity_mbps=cap_mbps,
            max_mbps=highest,
            utilization_pct=round(util_pct, 2),
            over_pct=None,
            over_mbps=None,
            direction=None,
            status=status,
            remarks=remarks,
        )


def load_mrtg_ocr_data(
    report_root: Path | None,
    date_compact: str,
) -> dict[str, dict[str, float]]:
    """Loads parsed OCR data from output/raw/mrtg/{YYYYMMDD}/mrtg_values_{YYYYMMDD}.json."""
    results: dict[str, dict[str, float]] = {}
    if not report_root:
        return results

    candidates = [
        report_root / date_compact / f"mrtg_values_{date_compact}.json",
        report_root / f"mrtg_values_{date_compact}.json",
    ]
    target_file = next((f for f in candidates if f.is_file()), None)
    if not target_file:
        logger.info("No MRTG values JSON file found for %s", date_compact)
        return results

    logger.info("Loading MRTG values from %s for date %s", target_file, date_compact)
    try:
        payload = json.loads(target_file.read_text(encoding="utf-8"))
        if isinstance(payload, dict):
            for sid, vals in payload.items():
                if isinstance(vals, dict):
                    results[str(sid).strip()] = {
                        "in_mbps": parse_mrtg_speed_mbps(vals.get("Inbound_Maximum")),
                        "out_mbps": parse_mrtg_speed_mbps(vals.get("Outbound_Maximum")),
                    }
    except Exception as exc:
        logger.warning("Failed reading MRTG values from %s: %s", target_file, exc)

    return results


def load_indibiz_daily_max(
    db_path: Path | None, date_iso: str
) -> tuple[float | None, float | None]:
    """Loads daily max traffic from local Indibiz SQLite store and returns (max_rx_mbps, max_tx_mbps)."""
    if not db_path:
        return None, None
    try:
        from .routeros_indibiz import IndibizStore

        store = IndibizStore(db_path)
        rx_bps, tx_bps = store.daily_max(date_iso)
        if rx_bps is None and tx_bps is None:
            return None, None
        rx_mbps = (rx_bps / 1_000_000.0) if rx_bps is not None else None
        tx_mbps = (tx_bps / 1_000_000.0) if tx_bps is not None else None
        return rx_mbps, tx_mbps
    except Exception as exc:
        logger.warning("Failed loading Indibiz daily max from %s: %s", db_path, exc)
        return None, None
