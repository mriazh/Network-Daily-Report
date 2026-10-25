"""Native Telkomsel Orbit Scraper using Playwright."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from playwright.sync_api import Page, sync_playwright

logger = logging.getLogger(__name__)


@dataclass
class OrbitTarget:
    no: int
    name: str
    phone: str
    imei: str
    location: str
    ssid: str


@dataclass
class OrbitPackage:
    name: str
    remaining_gb: float
    total_gb: float
    expiry_str: str
    days_left: int | None = None


@dataclass
class OrbitModemResult:
    target: OrbitTarget
    total_remaining_gb: float
    total_quota_gb: float
    packages: list[OrbitPackage]
    error: str | None = None
    last_scraped_at: str | None = None


def _load_orbit_targets(circuits_file: Path) -> list[OrbitTarget]:
    """Load Orbit modem targets from circuits.json."""
    if not circuits_file.exists():
        logger.warning("Circuits file not found at %s", circuits_file)
        return []

    with open(circuits_file, mode="r", encoding="utf-8") as f:
        data = json.load(f)

    targets = []
    for item in data.get("orbit_targets", []):
        targets.append(
            OrbitTarget(
                no=item.get("no", 0),
                name=item.get("name", ""),
                phone=item.get("phone", ""),
                imei=item.get("imei", ""),
                location=item.get("location", ""),
                ssid=item.get("ssid", ""),
            )
        )
    return targets


def _parse_quota_text(text: str) -> tuple[float, float, str, int | None]:
    """
    Parse quota text from Orbit result page.
    Expected formats:
    - "Sisa Kuota 200 GB dari 200 GB"
    - "Sisa 28.56 GB dari 30 GB"
    - "Kuota: 200.0 GB / 200.0 GB"
    - "Berlaku s.d. 30 Oct 2026"
    """
    remaining_gb = 0.0
    total_gb = 0.0
    expiry_str = "-"
    days_left = None

    # Pattern 1: "Sisa Kuota X GB dari Y GB" or "Sisa X GB dari Y GB"
    match = re.search(
        r"Sisa\s+(?:Kuota\s+)?([\d.]+)\s*GB\s+dari\s+([\d.]+)\s*GB", text, re.IGNORECASE
    )
    if match:
        remaining_gb = float(match.group(1))
        total_gb = float(match.group(2))
    else:
        # Pattern 2: "Kuota: X GB / Y GB"
        match = re.search(r"Kuota[:\s]+([\d.]+)\s*GB\s*/\s*([\d.]+)\s*GB", text, re.IGNORECASE)
        if match:
            remaining_gb = float(match.group(1))
            total_gb = float(match.group(2))
        else:
            # Pattern 3: Just "X GB / Y GB"
            match = re.search(r"([\d.]+)\s*GB\s*/\s*([\d.]+)\s*GB", text)
            if match:
                remaining_gb = float(match.group(1))
                total_gb = float(match.group(2))

    # Pattern for expiry: "Berlaku s.d. DD MMM YYYY" or "Berlaku s.d DD MMM YYYY"
    match = re.search(r"Berlaku\s+s\.?d\.?\s+(\d{1,2}\s+\w{3}\s+\d{4})", text, re.IGNORECASE)
    if match:
        expiry_str = match.group(1)
        try:
            expiry_date = datetime.strptime(expiry_str, "%d %b %Y").replace(tzinfo=timezone.utc)
            days_left = (expiry_date - datetime.now(timezone.utc)).days
        except ValueError:
            days_left = None

    return remaining_gb, total_gb, expiry_str, days_left


def _extract_package_info(page: Page) -> list[OrbitPackage]:
    """Extract package information from the loaded page."""
    packages = []

    # Try multiple selectors for quota cards
    selectors_to_try = [
        ".quota-card",
        ".package-card",
        "[class*='quota']",
        "[class*='package']",
        ".card",
    ]

    content = page.content()

    # Try to find package blocks in the page content
    # The page may contain multiple packages
    package_blocks = []

    for selector in selectors_to_try:
        elements = page.query_selector_all(selector)
        if elements:
            for el in elements:
                text = el.inner_text()
                if text and ("GB" in text or "kuota" in text.lower() or "sisa" in text.lower()):
                    package_blocks.append(text)
            if package_blocks:
                break

    # If no cards found, use full page content
    if not package_blocks:
        package_blocks = [content]

    seen_packages = set()
    for block in package_blocks:
        rem, tot, exp, days = _parse_quota_text(block)
        if tot > 0:
            # Try to extract package name
            pkg_name = "Internet Orbit"
            name_match = re.search(
                r"(Kuota\s+\w+|Internet\s+Orbit|Paket\s+\w+|FantaSIX)", block, re.IGNORECASE
            )
            if name_match:
                pkg_name = name_match.group(1).strip()

            pkg_key = (pkg_name, rem, tot, exp)
            if pkg_key not in seen_packages:
                seen_packages.add(pkg_key)
                packages.append(
                    OrbitPackage(
                        name=pkg_name,
                        remaining_gb=rem,
                        total_gb=tot,
                        expiry_str=exp,
                        days_left=days,
                    )
                )

    return packages


def scrape_orbit_modem(
    target: OrbitTarget, headless: bool = True, timeout_seconds: int = 60
) -> OrbitModemResult:
    """Scrape a single modem from MyOrbit portal."""
    url = "https://www.myorbit.id/informasi-modem-input"
    last_scraped = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
        )
        page = context.new_page()
        page.set_default_timeout(timeout_seconds * 1000)

        try:
            logger.info("Navigating to %s for modem %s", url, target.name)
            page.goto(url, wait_until="networkidle")

            # Fill phone number
            phone_selector = 'input[name="phone"], input[placeholder*="nomor" i], input[type="tel"], input:first-of-type'
            phone_input = page.query_selector(phone_selector)
            if not phone_input:
                # Try alternative selectors
                phone_input = page.query_selector('input[id*="phone" i], input[id*="nomor" i]')
            if not phone_input:
                # Last resort: first input on page
                inputs = page.query_selector_all("input")
                if inputs:
                    phone_input = inputs[0]

            if phone_input:
                phone_input.fill(target.phone)
                logger.debug("Filled phone: %s", target.phone)
            else:
                raise RuntimeError("Could not find phone input field")

            # Fill IMEI
            imei_selector = 'input[name="imei"], input[placeholder*="imei" i], input:not([type="tel"]):nth-of-type(2)'
            imei_input = page.query_selector(imei_selector)
            if not imei_input:
                inputs = page.query_selector_all("input")
                if len(inputs) >= 2:
                    imei_input = inputs[1]

            if imei_input:
                imei_input.fill(target.imei)
                logger.debug("Filled IMEI: %s", target.imei)
            else:
                raise RuntimeError("Could not find IMEI input field")

            # Click submit/lanjutkan button
            submit_selectors = [
                'button:has-text("Lanjutkan")',
                'button:has-text("Cek")',
                'button:has-text("Submit")',
                'button[type="submit"]',
                'input[type="submit"]',
                "button.btn-primary",
                'button:has-text("Check")',
            ]
            clicked = False
            for sel in submit_selectors:
                btn = page.query_selector(sel)
                if btn:
                    btn.click()
                    clicked = True
                    logger.debug("Clicked submit button: %s", sel)
                    break

            if not clicked:
                # Try pressing Enter on IMEI field
                imei_input.press("Enter")
                logger.debug("Pressed Enter on IMEI field")

            # Wait for results to load
            page.wait_for_load_state("networkidle", timeout=timeout_seconds * 1000)

            # Check for error messages
            error_text = ""
            error_selectors = [
                ".alert-danger",
                ".error-message",
                "[class*='error']",
                ".text-danger",
            ]
            for sel in error_selectors:
                el = page.query_selector(sel)
                if el and el.is_visible():
                    error_text = el.inner_text().strip()
                    break

            if error_text:
                logger.warning("Orbit portal returned error for %s: %s", target.name, error_text)
                return OrbitModemResult(
                    target=target,
                    total_remaining_gb=0.0,
                    total_quota_gb=0.0,
                    packages=[],
                    error=error_text,
                    last_scraped_at=last_scraped,
                )

            # Extract package information
            packages = _extract_package_info(page)

            total_remaining = sum(p.remaining_gb for p in packages)
            total_quota = sum(p.total_gb for p in packages)

            logger.info(
                "Scraped %s: %d packages, total remaining=%.2f GB",
                target.name,
                len(packages),
                total_remaining,
            )

            return OrbitModemResult(
                target=target,
                total_remaining_gb=total_remaining,
                total_quota_gb=total_quota,
                packages=packages,
                error=None,
                last_scraped_at=last_scraped,
            )

        except Exception as exc:
            logger.exception("Failed to scrape modem %s", target.name)
            return OrbitModemResult(
                target=target,
                total_remaining_gb=0.0,
                total_quota_gb=0.0,
                packages=[],
                error=str(exc),
                last_scraped_at=last_scraped,
            )
        finally:
            context.close()
            browser.close()


def scrape_all_orbit_modems(
    circuits_file: Path,
    output_dir: Path,
    date_compact: str,
    headless: bool = True,
    timeout_seconds: int = 60,
) -> list[OrbitModemResult]:
    """Scrape all Orbit modems and save raw snapshot."""
    targets = _load_orbit_targets(circuits_file)
    if not targets:
        logger.warning("No Orbit targets found in circuits file")
        return []

    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for target in targets:
        result = scrape_orbit_modem(target, headless=headless, timeout_seconds=timeout_seconds)
        results.append(result)

    # Save raw snapshot
    snapshot = []
    for r in results:
        snapshot.append(
            {
                "target": {
                    "no": r.target.no,
                    "imei": r.target.imei,
                    "phone": r.target.phone,
                    "location": r.target.location,
                    "ssid": r.target.ssid,
                    "status": "ACTIVE" if not r.error else "ERROR",
                    "imei_valid": r.error is None,
                },
                "total_remaining_gb": r.total_remaining_gb,
                "total_quota_gb": r.total_quota_gb,
                "multimedia_active": len(r.packages) > 0,
                "packages": [
                    {
                        "name": p.name,
                        "remaining_gb": p.remaining_gb,
                        "total_gb": p.total_gb,
                        "expiry_str": p.expiry_str,
                        "days_left": p.days_left,
                    }
                    for p in r.packages
                ],
                "earliest_expiry_str": min(
                    (p.expiry_str for p in r.packages if p.expiry_str != "-"),
                    default=None,
                ),
                "earliest_days_left": min(
                    (p.days_left for p in r.packages if p.days_left is not None),
                    default=None,
                ),
                "last_scraped_at": r.last_scraped_at,
                "error": r.error,
            }
        )

    snapshot_path = output_dir / "modems.json"
    snapshot_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False), encoding="utf-8")
    logger.info("Saved Orbit snapshot to %s", snapshot_path)

    return results


def convert_to_orbit_modem_items(results: list[OrbitModemResult]) -> list[dict[str, Any]]:
    """Convert scraper results to the format expected by pipeline/writers."""
    items = []
    for r in results:
        items.append(
            {
                "location": r.target.name,
                "ssid": r.target.ssid,
                "packages": [
                    {
                        "name": p.name,
                        "remaining_gb": p.remaining_gb,
                        "total_gb": p.total_gb,
                        "expiry_str": p.expiry_str,
                    }
                    for p in r.packages
                ],
            }
        )
    return items
