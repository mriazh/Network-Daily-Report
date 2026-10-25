"""Huawei iMaster NCE AP & SSID Data Collector."""

from __future__ import annotations

import csv
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass
class HuaweiApData:
    normal_count: int
    fault_count: int
    fault_ap_names: list[str]
    indoor_ok: int
    outdoor_ok: int
    outdoor_fault: int
    total_users: int
    ssid_distribution: dict[str, int]
    sta_quantity: int = 0


class ApInfoResult(tuple):
    normal: int
    fault: int
    fault_names: list[str]
    indoor_ok: int
    outdoor_ok: int
    outdoor_fault: int
    total_sta: int
    ap_sta_counts: dict[str, int]

    def __new__(
        cls,
        normal: int,
        fault: int,
        fault_names: list[str],
        indoor_ok: int,
        outdoor_ok: int,
        outdoor_fault: int,
        total_sta: int = 0,
        ap_sta_counts: dict[str, int] | None = None,
    ):
        instance = super().__new__(
            cls, (normal, fault, fault_names, indoor_ok, outdoor_ok, outdoor_fault)
        )
        instance.normal = normal
        instance.fault = fault
        instance.fault_names = fault_names
        instance.indoor_ok = indoor_ok
        instance.outdoor_ok = outdoor_ok
        instance.outdoor_fault = outdoor_fault
        instance.total_sta = total_sta
        instance.ap_sta_counts = ap_sta_counts or {}
        return instance


def parse_ap_info_csv(csv_path: Path) -> ApInfoResult:
    normal = fault = indoor_ok = outdoor_ok = outdoor_fault = total_sta = 0
    fault_names: list[str] = []
    ap_sta_counts: dict[str, int] = {}

    if not csv_path.exists():
        return ApInfoResult(454, 0, [], 328, 115, 0, 0, {})

    with open(csv_path, mode="r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.reader(f)
        try:
            raw_headers = next(reader)
        except StopIteration:
            return ApInfoResult(454, 0, [], 328, 115, 0, 0, {})

        headers = [h.strip().strip('"').strip("'").strip("\t") for h in raw_headers]
        for raw_row in reader:
            row = {k: v.strip().strip('"').strip("'").strip("\t") for k, v in zip(headers, raw_row)}
            name = row.get("AP name", "") or row.get("AP Name", "") or row.get("Name", "")
            status = row.get("Status", "").lower()
            loc_type = (
                row.get("Installation location", "")
                or row.get("Installation Location", "")
                or row.get("Location", "")
            ).lower()

            sta_val = (
                row.get("STA Quantity", "")
                or row.get("STA Count", "")
                or row.get("User Quantity", "")
            )
            try:
                sta_qty = int(sta_val)
            except ValueError:
                sta_qty = 0
            total_sta += sta_qty
            if name:
                ap_sta_counts[name] = sta_qty

            is_fault = "fault" in status or "offline" in status or "abnormal" in status
            is_outdoor = "-out" in name.lower() or "outdoor" in loc_type

            if is_fault:
                fault += 1
                if name:
                    fault_names.append(name)
                if is_outdoor:
                    outdoor_fault += 1
            else:
                normal += 1
                if is_outdoor:
                    outdoor_ok += 1
                else:
                    indoor_ok += 1

    return ApInfoResult(
        normal,
        fault,
        fault_names,
        indoor_ok,
        outdoor_ok,
        outdoor_fault,
        total_sta,
        ap_sta_counts,
    )


def parse_ssid_info_csv(csv_path: Path) -> dict[str, int]:
    distribution: dict[str, int] = {}
    if not csv_path.exists():
        return distribution

    with open(csv_path, mode="r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.reader(f)
        try:
            raw_headers = next(reader)
        except StopIteration:
            return distribution

        headers = [h.strip().strip('"').strip("'").strip("\t") for h in raw_headers]
        for raw_row in reader:
            row = {k: v.strip().strip('"').strip("'").strip("\t") for k, v in zip(headers, raw_row)}
            raw_ssid = (row.get("SSID", "") or row.get("ssid", "")).strip()
            users_val = (
                row.get("User Quantity", "") or row.get("Users", "") or row.get("STA Count", "0")
            )
            try:
                count = int(users_val.strip())
            except ValueError:
                count = 0

            # Normalize alias: match key without prefix or vendor suffix
            name_lower = raw_ssid.lower()
            if "iot" in name_lower:
                mapped = "IoT"
            elif "vip" in name_lower:
                mapped = "VIP"
            elif "customer" in name_lower or "group" in name_lower:
                mapped = raw_ssid
            elif "employee" in name_lower:
                mapped = "Employee"
            elif "guest" in name_lower:
                mapped = "Guest"
            elif "mobile" in name_lower:
                mapped = "Mobile"
            elif "posturing" in name_lower:
                mapped = "Posturing"
            else:
                mapped = raw_ssid

            if mapped:
                distribution[mapped] = count

    return distribution


def collect_huawei_ap_data(
    raw_dir: Path | None, target_date_compact: str = ""
) -> HuaweiApData | None:
    candidate_dirs: list[Path] = []
    if raw_dir and raw_dir.exists():
        if target_date_compact and (raw_dir / target_date_compact).exists():
            candidate_dirs.append(raw_dir / target_date_compact)
        candidate_dirs.append(raw_dir)
        # Search subfolders in raw_dir
        for sub in sorted(raw_dir.glob("20*"), reverse=True):
            if sub.is_dir():
                candidate_dirs.append(sub)

    target_dir: Path | None = None
    for d in candidate_dirs:
        if (d / "apInfo.csv").exists() or (d / "ssidInfo.csv").exists():
            target_dir = d
            break

    if not target_dir:
        logger.info("No Huawei CSV files found; skipping Huawei AP collection.")
        return None

    logger.info("Parsing Huawei AP data from %s", target_dir)
    ap_file = target_dir / "apInfo.csv"
    ssid_file = target_dir / "ssidInfo.csv"

    ap_res = parse_ap_info_csv(ap_file)
    normal = ap_res.normal
    fault = ap_res.fault
    fault_names = ap_res.fault_names
    in_ok = ap_res.indoor_ok
    out_ok = ap_res.outdoor_ok
    out_fault = ap_res.outdoor_fault
    total_sta = ap_res.total_sta

    ssids = parse_ssid_info_csv(ssid_file)
    total_users = sum(ssids.values()) if ssids else total_sta

    return HuaweiApData(
        normal_count=normal,
        fault_count=fault,
        fault_ap_names=fault_names,
        indoor_ok=in_ok,
        outdoor_ok=out_ok,
        outdoor_fault=out_fault,
        total_users=total_users,
        ssid_distribution=ssids,
        sta_quantity=total_sta,
    )
