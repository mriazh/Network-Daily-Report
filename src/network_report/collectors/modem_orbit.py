"""Modem Orbit Data Collector - supports native Playwright scraper and cache fallback."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class OrbitPackage:
    name: str
    remaining_gb: float
    total_gb: float
    expiry_str: str


@dataclass
class OrbitModem:
    location: str
    ssid: str
    packages: list[OrbitPackage]


def _load_report_modem_names(circuits_file: Path) -> list[str]:
    """Load modem display names from circuits.json."""
    if not circuits_file.exists():
        circuits_file = Path(__file__).resolve().parents[3] / "config" / "circuits.example.json"
    if circuits_file.exists():
        try:
            with open(circuits_file, mode="r", encoding="utf-8") as f:
                data = json.load(f)
                return [m[0] for m in data.get("modems", [])]
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Error reading circuits file: %s", exc)
    return [f"Modem Location {i}" for i in range(1, 10)]


def _load_orbit_targets(circuits_file: Path) -> list[dict[str, Any]]:
    """Load Orbit modem targets with phone/IMEI from circuits.json."""
    if not circuits_file.exists():
        return []
    try:
        with open(circuits_file, mode="r", encoding="utf-8") as f:
            data = json.load(f)
            return data.get("orbit_targets", [])
    except (json.JSONDecodeError, OSError) as exc:
        logger.warning("Error reading orbit_targets from circuits: %s", exc)
        return []


def _parse_cached_data(data: list[dict], report_names: list[str]) -> list[OrbitModem]:
    """Parse cached data format (from MRTG-CMP or previous native run).

    When there are 12 report names (12 orbit targets), each target maps
    directly to its own slot (target no 1 -> report_names[0], etc.) so all
    12 modems are returned individually. For fewer than 12 names, the legacy
    9-slot grouping is used.
    """
    if len(report_names) >= 12:
        # 1:1 mapping — target no 1 -> report_names[0], no 2 -> report_names[1], etc.
        slot_of_target_no = {n: n - 1 for n in range(1, 13)}
    else:
        # Legacy 9-slot grouping
        slot_of_target_no = {
            2: 0,  # Slot 1
            3: 1,  # Slot 2
            1: 2,  # Slot 3
            4: 3,  # Slot 4
            5: 4,  # Slot 5
            6: 5,
            7: 5,
            8: 5,
            9: 5,  # Slot 6 (Idle modems grouped)
            10: 6,  # Slot 7 (Customer)
            11: 7,  # Slot 8 (MiFi 1)
            12: 8,  # Slot 9 (MiFi 2)
        }

    grouped: dict[int, list[OrbitPackage]] = {slot: [] for slot in range(len(report_names))}
    grouped_ssid: dict[int, str] = {}

    for item in data:
        target = item.get("target", {})
        t_no = target.get("no", 0)
        ssid = target.get("ssid", "")
        slot = slot_of_target_no.get(t_no, 0)
        grouped_ssid[slot] = ssid

        for p in item.get("packages", []):
            rem = float(p.get("remaining_gb", 0.0))
            tot = float(p.get("total_gb", 0.0))
            exp = p.get("expiry_str", "-")
            name = p.get("name", "Internet Orbit")
            grouped.setdefault(slot, []).append(
                OrbitPackage(
                    name=name,
                    remaining_gb=rem,
                    total_gb=tot,
                    expiry_str=exp,
                )
            )

    modems: list[OrbitModem] = []
    for slot_idx, rep_name in enumerate(report_names):
        pkgs = grouped.get(slot_idx, [])
        modems.append(
            OrbitModem(
                location=rep_name,
                ssid=grouped_ssid.get(slot_idx, ""),
                packages=pkgs,
            )
        )

    return modems


def _parse_native_results(data: list[dict], report_names: list[str]) -> list[OrbitModem]:
    """Parse native scraper results format (direct from orbit_scraper or cache).

    With 12 report names the results map 1:1 by position so each of the 12
    modems is returned individually (duplicate names like the four idle
    modems are no longer collapsed). With fewer names, names are matched
    by their display name.
    """
    one_to_one = len(report_names) >= 12

    # Name-keyed lookup for the fallback path (duplicate names collapse, so
    # only used when we are not doing a 1:1 positional mapping).
    native_to_report: dict[str, str] = {}
    if not one_to_one:
        for i, target in enumerate(data):
            target_obj = target.get("target", {})
            native_name = target_obj.get("name") or target_obj.get("location", "")
            if i < len(report_names):
                native_to_report[native_name] = report_names[i]

    # Keyed by slot index, never by name: report_names may contain duplicate
    # display names (four idle modems) that would otherwise collapse together.
    grouped: dict[int, list[OrbitPackage]] = {slot: [] for slot in range(len(report_names))}
    grouped_ssid: dict[int, str] = {}

    for i, item in enumerate(data):
        target = item.get("target", {})
        # Native scraper conversion writes location/ssid at the top level,
        # cache snapshots nest them under "target".
        ssid = target.get("ssid", "") or item.get("ssid", "")
        if one_to_one:
            slot = i if i < len(report_names) else -1
        else:
            # Try native "name" first, then fallback to "location" for cache format
            native_name = target.get("name") or target.get("location", "")
            rep_name = native_to_report.get(native_name, native_name)
            slot = report_names.index(rep_name) if rep_name in report_names else -1
        if slot < 0:
            continue
        grouped_ssid[slot] = ssid

        for p in item.get("packages", []):
            rem = float(p.get("remaining_gb", 0.0))
            tot = float(p.get("total_gb", 0.0))
            exp = p.get("expiry_str", "-")
            name = p.get("name", "Internet Orbit")
            grouped[slot].append(
                OrbitPackage(
                    name=name,
                    remaining_gb=rem,
                    total_gb=tot,
                    expiry_str=exp,
                )
            )

    modems: list[OrbitModem] = []
    for slot, rep_name in enumerate(report_names):
        modems.append(
            OrbitModem(
                location=rep_name,
                ssid=grouped_ssid.get(slot, ""),
                packages=grouped.get(slot, []),
            )
        )

    return modems


def collect_orbit_modems(
    cache_json_path: Path | None,
    circuits_file: Path | None = None,
    output_raw_root: Path | None = None,
    date_compact: str | None = None,
    headless: bool = True,
    timeout_seconds: int = 60,
) -> list[OrbitModem]:
    """
    Collect Orbit modem data.

    Priority:
    1. Native Playwright scraper (if circuits_file has orbit_targets with phone/IMEI)
    2. Fallback to cache JSON file (MRTG-CMP or previous run)
    """
    report_names = _load_report_modem_names(
        circuits_file or Path(__file__).resolve().parents[3] / "config" / "circuits.json"
    )

    # Try native scraper first
    if circuits_file:
        orbit_targets = _load_orbit_targets(circuits_file)
        if orbit_targets:
            logger.info("Using native Orbit scraper for %d targets", len(orbit_targets))
            try:
                from .orbit_scraper import convert_to_orbit_modem_items, scrape_all_orbit_modems

                if output_raw_root and date_compact:
                    output_dir = output_raw_root / "orbit" / date_compact
                else:
                    output_dir = (
                        Path(__file__).resolve().parents[3]
                        / "output"
                        / "raw"
                        / "orbit"
                        / (date_compact or datetime.now(timezone.utc).strftime("%Y%m%d"))
                    )

                native_results = scrape_all_orbit_modems(
                    circuits_file=circuits_file,
                    output_dir=output_dir,
                    date_compact=date_compact or datetime.now(timezone.utc).strftime("%Y%m%d"),
                    headless=headless,
                    timeout_seconds=timeout_seconds,
                )

                # Check if native results have valid data (at least one modem with packages)
                valid_results = [r for r in native_results if r.packages and not r.error]
                if valid_results:
                    # Convert to legacy format for compatibility
                    converted = convert_to_orbit_modem_items(native_results)
                    return _parse_native_results(converted, report_names)
                else:
                    logger.warning(
                        "Native Orbit scraper returned no valid data, falling back to cache"
                    )
            except ImportError:
                logger.warning("Playwright not available, falling back to cache")
            except Exception as exc:  # noqa: BLE001
                # Broad except is intentional here: we want to fall back to cache
                # for ANY error (network timeout, page structure changes, etc.)
                logger.warning("Native Orbit scraper failed: %s, falling back to cache", exc)

    # Fallback to cache file
    if cache_json_path and cache_json_path.exists():
        logger.info("Using Orbit cache file: %s", cache_json_path)
        try:
            with open(cache_json_path, mode="r", encoding="utf-8") as f:
                data = json.load(f)

            # Detect format: native format has "target" with "name", old format has "target" with "no"
            if data and "target" in data[0] and "name" in data[0]["target"]:
                return _parse_native_results(data, report_names)
            else:
                return _parse_cached_data(data, report_names)
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("Error reading Orbit cache json: %s", exc)

    logger.info("No Orbit data source available; returning empty list.")
    return []
