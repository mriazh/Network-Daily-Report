"""Huawei iMaster NCE & WAC Native Collector (Playwright)."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from playwright.sync_api import sync_playwright

logger = logging.getLogger(__name__)

DEFAULT_SEL_LOGIN_USERNAME = "#username"
DEFAULT_SEL_LOGIN_PASSWORD = "#password"
DEFAULT_SEL_LOGIN_SUBMIT = "#submitButton"
DEFAULT_SEL_NAV_AP = "#dashAP"
DEFAULT_SEL_NAV_SSID = "#dashSSIDs"
DEFAULT_SEL_NAV_AC = "#dashAC"
DEFAULT_SEL_EXPORT_AP = "#outputApListInfoBtnEn"
DEFAULT_SEL_EXPORT_SSID = "#ssidListExportBtn_uBtn"


@dataclass
class WacStatus:
    cpu_pct: float | None = None
    mem_pct: float | None = None
    device_name: str = ""
    device_type: str = ""
    temperature: str = ""
    running_time: str = ""
    software_version: str = ""
    serial_number: str = ""
    max_ap_count: str = ""
    captured_at: str = ""
    error: str | None = None


@dataclass
class NceExport:
    ap_csv_path: Path | None = None
    ssid_csv_path: Path | None = None
    wac: WacStatus | None = None
    error: str | None = None


def parse_wac_pct(text: str) -> float | None:
    if not text:
        return None
    m = re.search(r"(\d+(?:\.\d+)?)\s*%", text)
    if not m:
        return None
    try:
        return float(m.group(1))
    except ValueError:
        return None


class NceCollector:
    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        selectors: dict[str, str] | None = None,
        headless: bool = True,
        timeout_seconds: int = 120,
        output_root: Path | None = None,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.headless = headless
        self.timeout_seconds = timeout_seconds
        self.output_root = output_root or Path("output")

        sel = {
            "login_username": DEFAULT_SEL_LOGIN_USERNAME,
            "login_password": DEFAULT_SEL_LOGIN_PASSWORD,
            "login_submit": DEFAULT_SEL_LOGIN_SUBMIT,
            "nav_ap": DEFAULT_SEL_NAV_AP,
            "nav_ssid": DEFAULT_SEL_NAV_SSID,
            "nav_ac": DEFAULT_SEL_NAV_AC,
            "export_ap": DEFAULT_SEL_EXPORT_AP,
            "export_ssid": DEFAULT_SEL_EXPORT_SSID,
        }
        if selectors:
            sel.update({k: v for k, v in selectors.items() if v})
        self.selectors = sel

        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    def collect(self, date_compact: str) -> NceExport:
        if not self.base_url or not self.username or not self.password:
            err = "Missing NCE base_url, username, or password"
            logger.warning(err)
            return NceExport(error=err)

        try:
            self._playwright = sync_playwright().start()
            self._browser = self._playwright.chromium.launch(headless=self.headless)
            self._context = self._browser.new_context(
                accept_downloads=True,
                ignore_https_errors=True,
            )
            self._page = self._context.new_page()
            self._page.set_default_timeout(self.timeout_seconds * 1000)

            # Step 1: Login
            login_url = f"{self.base_url}/view/login.html"
            logger.info("Opening NCE login: %s", login_url)
            self._page.goto(login_url, wait_until="domcontentloaded")
            self._page.fill(self.selectors["login_username"], self.username)
            self._page.fill(self.selectors["login_password"], self.password)
            self._page.click(self.selectors["login_submit"])
            self._page.wait_for_timeout(12000)

            if "/view/login.html" in self._page.url:
                try:
                    if self._page.is_visible(self.selectors["login_submit"]):
                        err = "NCE login failed: still on login page"
                        logger.error(err)
                        return NceExport(error=err)
                except Exception:
                    pass

            ap_dir = self.output_root / "raw" / "huawei_ap" / date_compact
            ap_dir.mkdir(parents=True, exist_ok=True)

            # Step 2: AP Export
            ap_path = ap_dir / "apInfo.csv"
            logger.info("Exporting Huawei AP CSV...")
            self._page.click(self.selectors["nav_ap"])
            self._page.wait_for_timeout(5000)
            with self._page.expect_download(timeout=self.timeout_seconds * 1000) as download_info:
                self._page.click(self.selectors["export_ap"])
            download = download_info.value
            download.save_as(str(ap_path))
            logger.info("Saved AP CSV to %s", ap_path)

            # Step 3: SSID Export
            ssid_path = ap_dir / "ssidInfo.csv"
            logger.info("Exporting Huawei SSID CSV...")
            self._page.click(self.selectors["nav_ssid"])
            self._page.wait_for_timeout(5000)
            with self._page.expect_download(timeout=self.timeout_seconds * 1000) as download_info:
                self._page.click(self.selectors["export_ssid"])
            download = download_info.value
            download.save_as(str(ssid_path))
            logger.info("Saved SSID CSV to %s", ssid_path)

            # Step 4: WAC AC Tab
            logger.info("Reading Huawei WAC status...")
            self._page.click(self.selectors["nav_ap"])
            self._page.wait_for_timeout(2000)
            self._page.click(self.selectors["nav_ac"])
            self._page.wait_for_timeout(12000)

            def _get_text(sel: str) -> str:
                try:
                    el = self._page.locator(sel)
                    if el.count() > 0:
                        return (el.first.inner_text() or "").strip()
                except Exception as ex:
                    logger.debug("Failed reading selector %s: %s", sel, ex)
                return ""

            raw_cpu = _get_text("#cpuUsageCurrentValue")
            raw_mem = _get_text("#memeryUsageCurrentValue")
            dev_name = _get_text("#deviceInfo_deviceName")
            dev_type = _get_text("#deviceInfo_deviceType")
            temperature = _get_text("#deviceInfo_temperature")
            running_time = _get_text("#deviceInfo_runningTime")
            software_version = _get_text("#deviceInfo_softwareVersion")
            serial_number = _get_text("#deviceInfo_serialNumber")
            max_ap_count = _get_text("#deviceInfo_maxApCount")

            wac_status = WacStatus(
                cpu_pct=parse_wac_pct(raw_cpu),
                mem_pct=parse_wac_pct(raw_mem),
                device_name=dev_name,
                device_type=dev_type,
                temperature=temperature,
                running_time=running_time,
                software_version=software_version,
                serial_number=serial_number,
                max_ap_count=max_ap_count,
                captured_at=datetime.now().isoformat(),
                error=None,
            )

            wac_dir = self.output_root / "raw" / "wac" / date_compact
            wac_dir.mkdir(parents=True, exist_ok=True)
            wac_json_path = wac_dir / "wac_status.json"
            wac_json_path.write_text(json.dumps(asdict(wac_status), indent=2), encoding="utf-8")
            logger.info("Saved WAC status to %s", wac_json_path)

            return NceExport(
                ap_csv_path=ap_path,
                ssid_csv_path=ssid_path,
                wac=wac_status,
                error=None,
            )

        except Exception as e:
            err_msg = str(e)
            logger.error("NCE collection failed: %s", err_msg, exc_info=True)
            return NceExport(error=err_msg)

    def close(self) -> None:
        try:
            if self._page:
                self._page.close()
        except Exception:
            pass
        try:
            if self._context:
                self._context.close()
        except Exception:
            pass
        try:
            if self._browser:
                self._browser.close()
        except Exception:
            pass
        try:
            if self._playwright:
                self._playwright.stop()
        except Exception:
            pass
        self._page = None
        self._context = None
        self._browser = None
        self._playwright = None

    def __enter__(self) -> NceCollector:
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.close()
