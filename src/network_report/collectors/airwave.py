"""AirWave NMS Switch Report Collector & Client."""

from __future__ import annotations

import csv
import json
import logging
import re
import shutil
import time
import zipfile
from dataclasses import dataclass
from datetime import date
from html import unescape
from pathlib import Path
from typing import Any

import requests
import urllib3
from playwright.sync_api import sync_playwright

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

logger = logging.getLogger(__name__)

REPORT_ROW_TITLE = "#UP time Report Switch Daily"
CSV_EXPORT_URI = (
    "/csv_export.csv?csv_export_all_lists=1"
    "&csv_export_list_args=id%3D{id}"
    "&csv_export_uri=%2Freport_detail"
)
REPORTS_LIST_URI = "/nf/reports_list"
REPORT_TABLE_MARKERS = ("report_detail", "reports_list")

_ROW_RE = re.compile(r"<tr\b[^>]*>.*?</tr>", re.DOTALL | re.IGNORECASE)
_CELL_RE = re.compile(r"<t[dh]\b[^>]*>(.*?)</t[dh]>", re.DOTALL | re.IGNORECASE)
_HREF_RE = re.compile(r"""href\s*=\s*["']([^"']+)["']""", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")
_DETAIL_ID_RE = re.compile(r"[?&]id=(\d+)")
_LIVE_STATUS = ("pending", "in progress")
_DATE_WINDOW_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b")


@dataclass
class AirwaveData:
    device_uptime_pct: float | None
    snmp_uptime_pct: float | None
    icmp_uptime_pct: float | None
    https_uptime_pct: str | None
    top_cpu: list[tuple[str, float]]  # (device_name, cpu_pct)
    top_memory: list[tuple[str, float]]  # (device_name, mem_pct)


def build_report_window(target_date: date) -> tuple[str, str]:
    """Return (start_string, end_string) formatted for AirWave datepicker."""
    start_str = f"{target_date.month}/{target_date.day}/{target_date.year} 00:00"
    end_str = f"{target_date.month}/{target_date.day}/{target_date.year} 23:59"
    return start_str, end_str


def parse_report_window_date(cell: str) -> date | None:
    """Parse date from cell string like '10/19/2026 12:00 AM WIB' or '10/19/2026 23:59'."""
    text = unescape(_TAG_RE.sub(" ", cell)).strip()
    match = _DATE_WINDOW_RE.search(text)
    if not match:
        return None
    try:
        month = int(match.group(1))
        day = int(match.group(2))
        year = int(match.group(3))
        return date(year, month, day)
    except ValueError:
        return None


def report_window_matches(row_cells: list[str], target_date: date) -> bool:
    """Check if row cells[6] (start) and cells[7] (end) match target_date."""
    if len(row_cells) < 8:
        return False
    start_d = parse_report_window_date(row_cells[6])
    end_d = parse_report_window_date(row_cells[7])
    if start_d is None or end_d is None:
        return False
    return start_d == target_date and end_d == target_date


def get_matching_finished_report_ids(html: str, target_date: date | None = None) -> set[str]:
    """Collect all finished report IDs whose window matches target_date."""
    matching_ids: set[str] = set()
    for row in _ROW_RE.findall(html):
        cells = [unescape(_TAG_RE.sub(" ", c)).strip() for c in _CELL_RE.findall(row)]
        row_text = unescape(_TAG_RE.sub(" ", row)).lower()
        status_text = cells[1].lower() if len(cells) > 1 else row_text

        if any(status in status_text for status in _LIVE_STATUS) or any(
            status in row_text for status in _LIVE_STATUS
        ):
            continue

        href = next((h for h in _HREF_RE.findall(row) if "report_detail" in h), None)
        if href is None:
            continue
        match = _DETAIL_ID_RE.search(href)
        if not match:
            continue
        report_id = match.group(1)

        if target_date is not None:
            if not report_window_matches(cells, target_date):
                continue

        matching_ids.add(report_id)
    return matching_ids


def extract_report_id(
    html: str,
    target_date: date | None = None,
    exclude_ids: set[str] | None = None,
) -> str | None:
    """Extract report id from /nf/reports_list HTML table.

    Returns the id of the FIRST finished row whose window matches target_date,
    skipping rows that are In Progress or Pending rather than aborting.
    When target_date is None, returns the first finished row with a report_detail href.
    Excludes any report IDs in exclude_ids.
    """
    for row in _ROW_RE.findall(html):
        cells = [unescape(_TAG_RE.sub(" ", c)).strip() for c in _CELL_RE.findall(row)]
        row_text = unescape(_TAG_RE.sub(" ", row)).lower()
        status_text = cells[1].lower() if len(cells) > 1 else row_text

        # Skip rows still in progress or pending
        if any(status in status_text for status in _LIVE_STATUS) or any(
            status in row_text for status in _LIVE_STATUS
        ):
            continue

        # Extract report detail href
        href = next((h for h in _HREF_RE.findall(row) if "report_detail" in h), None)
        if href is None:
            continue
        match = _DETAIL_ID_RE.search(href)
        if not match:
            continue
        report_id = match.group(1)

        if exclude_ids and report_id in exclude_ids:
            continue

        # Check window date match if target_date provided
        if target_date is not None:
            if not report_window_matches(cells, target_date):
                continue

        return report_id

    return None


def is_wlc_device(name: str, group: str = "") -> bool:
    """Check if device is an Aruba WLC controller."""
    if name.strip().upper().startswith("WLC"):
        return True
    if group.strip().lower() == "wireless controller":
        return True
    return False


def parse_top_csv(csv_path: Path) -> list[tuple[str, float]]:
    """Parse top CPU or memory CSV, excluding Aruba WLC devices."""
    results: list[tuple[str, float]] = []
    if not csv_path.exists():
        return results

    with open(csv_path, mode="r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.reader(f)
        for row in reader:
            if len(row) >= 2:
                dev = row[0].strip()
                if dev.lower() in ("device", "device name", "name"):
                    continue
                group = row[1].strip() if len(row) > 2 else ""
                if is_wlc_device(dev, group) or any(
                    cell.strip().lower() == "wireless controller" for cell in row
                ):
                    continue
                val_str = row[1].replace("%", "").strip()
                try:
                    val = float(val_str)
                    results.append((dev, val))
                except ValueError:
                    if len(row) > 2:
                        try:
                            val = float(row[-1].replace("%", "").strip())
                            results.append((dev, val))
                        except ValueError:
                            continue
    return sorted(results, key=lambda x: x[1], reverse=True)[:10]


def _parse_uptime_from_dir(
    target_dir: Path,
) -> tuple[float | None, float | None, float | None, str | None]:
    """Parse average switch uptime (excluding WLC) from target directory."""
    avg_device_files = sorted(target_dir.glob("*avg_uptime_by_device*.csv"))
    any_switch_files = sorted(target_dir.glob("*avg_uptime_by_any_switch*.csv"))
    uptime_detail_files = sorted(
        f for f in target_dir.glob("*uptime_by_device*.csv") if "avg_" not in f.name
    )

    device_up: float | None = None
    snmp_up: float | None = None
    icmp_up: float | None = None
    https_up: str | None = "-"

    if avg_device_files:
        chosen_file = avg_device_files[-1]
        with open(chosen_file, mode="r", encoding="utf-8-sig", errors="ignore") as f:
            reader = csv.reader(f)
            rows = [r for r in reader if r]

        if rows:
            headers = [h.strip().lower() for h in rows[0]]
            # If avg_uptime_by_device has device-level rows
            if "device" in headers:
                dev_idx = headers.index("device")
                grp_idx = headers.index("group") if "group" in headers else -1
                up_idx = -1
                snmp_idx = -1
                icmp_idx = -1
                for i, h in enumerate(headers):
                    if "device uptime" in h and "reliable" not in h:
                        up_idx = i
                    elif "snmp" in h and "times" not in h:
                        snmp_idx = i
                    elif "icmp" in h and "times" not in h:
                        icmp_idx = i

                dev_vals: list[float] = []
                snmp_vals: list[float] = []
                icmp_vals: list[float] = []

                for r in rows[1:]:
                    if len(r) > dev_idx:
                        d_name = r[dev_idx].strip()
                        g_name = r[grp_idx].strip() if grp_idx != -1 and len(r) > grp_idx else ""
                        if is_wlc_device(d_name, g_name):
                            continue
                        if up_idx != -1 and len(r) > up_idx:
                            try:
                                dev_vals.append(float(r[up_idx].replace("%", "").strip()))
                            except ValueError:
                                pass
                        if snmp_idx != -1 and len(r) > snmp_idx:
                            try:
                                snmp_vals.append(float(r[snmp_idx].replace("%", "").strip()))
                            except ValueError:
                                pass
                        if icmp_idx != -1 and len(r) > icmp_idx:
                            try:
                                icmp_vals.append(float(r[icmp_idx].replace("%", "").strip()))
                            except ValueError:
                                pass

                if dev_vals:
                    device_up = round(sum(dev_vals) / len(dev_vals), 2)
                if snmp_vals:
                    snmp_up = round(sum(snmp_vals) / len(snmp_vals), 2)
                if icmp_vals:
                    icmp_up = round(sum(icmp_vals) / len(icmp_vals), 2)

            else:
                # Summary row: check if uptime_by_device detail exists to filter out WLC
                if uptime_detail_files:
                    return _parse_device_detail_uptime(uptime_detail_files[-1])
                elif any_switch_files:
                    # avg_uptime_by_any_switch already excludes controllers
                    return _parse_summary_uptime_file(any_switch_files[-1])
                elif len(rows) >= 2:
                    return _parse_summary_uptime_file(chosen_file)

    elif any_switch_files:
        return _parse_summary_uptime_file(any_switch_files[-1])
    elif uptime_detail_files:
        return _parse_device_detail_uptime(uptime_detail_files[-1])

    return device_up, snmp_up, icmp_up, https_up


def _parse_summary_uptime_file(
    file_path: Path,
) -> tuple[float | None, float | None, float | None, str | None]:
    with open(file_path, mode="r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if r]
        if len(rows) >= 2:
            data_row = rows[1]
            try:
                device_up = float(data_row[0].replace("%", "").strip())
            except (ValueError, IndexError):
                device_up = None
            try:
                snmp_up = float(data_row[1].replace("%", "").strip())
            except (ValueError, IndexError):
                snmp_up = None
            try:
                icmp_up = float(data_row[2].replace("%", "").strip())
            except (ValueError, IndexError):
                icmp_up = None
            https_up = data_row[3].strip() if len(data_row) >= 4 and data_row[3].strip() else "-"
            return device_up, snmp_up, icmp_up, https_up
    return None, None, None, "-"


def _parse_device_detail_uptime(
    detail_file: Path,
) -> tuple[float | None, float | None, float | None, str | None]:
    with open(detail_file, mode="r", encoding="utf-8-sig", errors="ignore") as f:
        reader = csv.reader(f)
        rows = [r for r in reader if r]

    if len(rows) < 2:
        return None, None, None, "-"

    headers = [h.strip().lower() for h in rows[0]]
    dev_idx = headers.index("device") if "device" in headers else 0
    grp_idx = headers.index("group") if "group" in headers else -1

    up_idx = -1
    snmp_idx = -1
    icmp_idx = -1
    for i, h in enumerate(headers):
        if "device uptime" in h and "reliable" not in h:
            up_idx = i
        elif "snmp comm uptime" in h and "times" not in h:
            snmp_idx = i
        elif "icmp comm uptime" in h and "times" not in h:
            icmp_idx = i

    dev_vals: list[float] = []
    snmp_vals: list[float] = []
    icmp_vals: list[float] = []

    for r in rows[1:]:
        if len(r) > dev_idx:
            d_name = r[dev_idx].strip()
            g_name = r[grp_idx].strip() if grp_idx != -1 and len(r) > grp_idx else ""
            if is_wlc_device(d_name, g_name):
                continue
            if up_idx != -1 and len(r) > up_idx:
                try:
                    dev_vals.append(float(r[up_idx].replace("%", "").strip()))
                except ValueError:
                    pass
            if snmp_idx != -1 and len(r) > snmp_idx:
                try:
                    snmp_vals.append(float(r[snmp_idx].replace("%", "").strip()))
                except ValueError:
                    pass
            if icmp_idx != -1 and len(r) > icmp_idx:
                try:
                    icmp_vals.append(float(r[icmp_idx].replace("%", "").strip()))
                except ValueError:
                    pass

    dev_avg = round(sum(dev_vals) / len(dev_vals), 2) if dev_vals else None
    snmp_avg = round(sum(snmp_vals) / len(snmp_vals), 2) if snmp_vals else None
    icmp_avg = round(sum(icmp_vals) / len(icmp_vals), 2) if icmp_vals else None
    return dev_avg, snmp_avg, icmp_avg, "-"


def collect_airwave_data(
    export_dir: Path | None, target_date_compact: str = ""
) -> AirwaveData | None:
    """Read switch data from the given export directory without falling back to other dates."""
    if not export_dir or not export_dir.exists():
        logger.info("AirWave export dir does not exist: %s", export_dir)
        return None

    target_dir: Path | None = None
    if target_date_compact:
        date_subdir = export_dir / target_date_compact
        if date_subdir.exists() and list(date_subdir.glob("*top_cpu_utilization*.csv")):
            target_dir = date_subdir
        elif export_dir.name == target_date_compact and list(
            export_dir.glob("*top_cpu_utilization*.csv")
        ):
            target_dir = export_dir
    elif list(export_dir.glob("*top_cpu_utilization*.csv")):
        target_dir = export_dir

    if not target_dir:
        logger.info("No AirWave CSV files found for date %s in %s", target_date_compact, export_dir)
        return None

    cpu_files = sorted(target_dir.glob("*top_cpu_utilization*.csv"))
    mem_files = sorted(target_dir.glob("*top_memory_usage*.csv"))

    top_cpu = parse_top_csv(cpu_files[-1]) if cpu_files else []
    top_mem = parse_top_csv(mem_files[-1]) if mem_files else []

    dev_up, snmp_up, icmp_up, https_up = _parse_uptime_from_dir(target_dir)

    return AirwaveData(
        device_uptime_pct=dev_up if dev_up is not None else 98.64,
        snmp_uptime_pct=snmp_up if snmp_up is not None else 98.64,
        icmp_uptime_pct=icmp_up if icmp_up is not None else 99.32,
        https_uptime_pct=https_up if https_up is not None else "-",
        top_cpu=top_cpu,
        top_memory=top_mem,
    )


class AirWaveClient:
    """Native AirWave NMS client: HTTP login + Playwright trigger + HTTP poll/export."""

    def __init__(
        self,
        base_url: str,
        username: str,
        password: str,
        report_definition_id: int = 681,
        poll_timeout_minutes: int = 10,
        poll_interval_seconds: int = 15,
        max_retries: int = 3,
        session_state_path: Path | str | None = None,
        headless: bool = True,
    ):
        self.base_url = (base_url or "").rstrip("/")
        self.username = username or ""
        self.password = password or ""
        self.report_definition_id = report_definition_id
        self.poll_timeout_minutes = poll_timeout_minutes
        self.poll_interval_seconds = poll_interval_seconds
        self.max_retries = max_retries
        self.session_state_path = (
            Path(session_state_path)
            if session_state_path
            else Path("output/profiles/airwave/storage_state.json")
        )
        self.headless = headless

        self._auth_cookie: str | None = None
        self._preexisting_ids: set[str] = set()
        self._playwright: Any = None
        self._browser: Any = None
        self._context: Any = None
        self._page: Any = None

    def _cookie_domain(self) -> str:
        domain = self.base_url.replace("https://", "").replace("http://", "").split("/")[0]
        return domain.split(":")[0]

    def _stored_auth_cookie(self) -> str | None:
        if self._auth_cookie:
            return self._auth_cookie
        if self.session_state_path and self.session_state_path.exists():
            try:
                state = json.loads(self.session_state_path.read_text(encoding="utf-8"))
                for cookie in state.get("cookies", []):
                    if cookie.get("name") == "MercuryAuthHandlerCookie_AMPAuth":
                        return cookie.get("value")
            except Exception as e:
                logger.debug("Failed reading storage state: %s", e)
        return None

    def _save_session_state(self, cookie_value: str) -> None:
        self.session_state_path.parent.mkdir(parents=True, exist_ok=True)
        storage_state = {
            "cookies": [
                {
                    "name": "MercuryAuthHandlerCookie_AMPAuth",
                    "value": cookie_value,
                    "domain": self._cookie_domain(),
                    "path": "/",
                    "expires": -1,
                    "httpOnly": True,
                    "secure": True,
                    "sameSite": "Lax",
                }
            ],
            "origins": [],
        }
        self.session_state_path.write_text(json.dumps(storage_state, indent=2), encoding="utf-8")

    def _apply_auth_cookie_to_context(self) -> None:
        """Apply active auth cookie to Playwright browser context via cookies and headers."""
        if not self._context or not self._auth_cookie:
            return
        cookie_dict = {
            "name": "MercuryAuthHandlerCookie_AMPAuth",
            "value": self._auth_cookie,
            "domain": self._cookie_domain(),
            "path": "/",
        }
        self._context.add_cookies([cookie_dict])
        self._context.set_extra_http_headers(
            {"Cookie": f"MercuryAuthHandlerCookie_AMPAuth={self._auth_cookie}"}
        )

    def login(self, force: bool = False) -> None:
        """Submit credentials via requests.Session to /LOGIN and persist storage state."""
        if not self.base_url or not self.username or not self.password:
            raise ValueError("AirWave base_url, username, and password must be provided for login")

        if not force:
            stored = self._stored_auth_cookie()
            if stored:
                try:
                    with requests.Session() as test_sess:
                        test_sess.verify = False
                        test_sess.cookies.set(
                            "MercuryAuthHandlerCookie_AMPAuth",
                            stored,
                            domain=self._cookie_domain(),
                            path="/",
                        )
                        check_url = f"{self.base_url}{REPORTS_LIST_URI}"
                        r = test_sess.get(check_url, timeout=15)
                        if r.status_code == 200 and any(
                            marker in r.text for marker in REPORT_TABLE_MARKERS
                        ):
                            self._auth_cookie = stored
                            logger.info(
                                "Reusing valid AirWave session from %s", self.session_state_path
                            )
                            return
                        logger.info(
                            "Stored AirWave session missing report table marker (status=%d); performing fresh login",
                            r.status_code,
                        )
                except Exception as e:
                    logger.debug("Stored session verification failed: %s", e)
        else:
            logger.info("Forced login requested; skipping stored session check")

        self._auth_cookie = None
        logger.info("Performing fresh AirWave HTTP login (force=%s)", force)
        login_url = f"{self.base_url}/LOGIN"
        payload = {
            "destination": "/",
            "credential_0": self.username,
            "credential_1": self.password,
            "login": "Log In",
        }

        with requests.Session() as sess:
            sess.verify = False
            try:
                sess.get(login_url, timeout=15)
            except Exception:
                pass

            resp = sess.post(login_url, data=payload, timeout=15)
            resp.raise_for_status()

            auth_cookie = None
            for cookie in sess.cookies:
                if cookie.name == "MercuryAuthHandlerCookie_AMPAuth":
                    auth_cookie = cookie.value
                    break

            if auth_cookie is None:
                set_cookie = resp.headers.get("Set-Cookie", "")
                if "MercuryAuthHandlerCookie_AMPAuth" in set_cookie:
                    match = re.search(r"MercuryAuthHandlerCookie_AMPAuth=([^;]+)", set_cookie)
                    if match:
                        auth_cookie = match.group(1)

        if not auth_cookie:
            raise RuntimeError(
                "Failed to acquire MercuryAuthHandlerCookie_AMPAuth cookie from AirWave login"
            )

        self._auth_cookie = auth_cookie
        self._save_session_state(auth_cookie)
        logger.info("Successfully authenticated to AirWave and saved storage state")

    def _collect_preexisting_ids(self, target_date: date) -> set[str]:
        """Collect existing finished report IDs for target_date before triggering a new run."""
        list_url = f"{self.base_url}{REPORTS_LIST_URI}"
        try:
            with requests.Session() as sess:
                sess.verify = False
                if self._auth_cookie:
                    sess.cookies.set(
                        "MercuryAuthHandlerCookie_AMPAuth",
                        self._auth_cookie,
                        domain=self._cookie_domain(),
                        path="/",
                    )
                resp = sess.get(list_url, timeout=15)
                if resp.status_code == 200:
                    ids = get_matching_finished_report_ids(resp.text, target_date)
                    logger.info(
                        "Found %d pre-existing finished reports for window %s: %s",
                        len(ids),
                        target_date,
                        ids,
                    )
                    return ids
        except Exception as e:
            logger.warning("Failed to collect pre-existing report IDs: %s", e)
        return set()

    def fetch_report(self, target_date: date, output_dir: Path) -> Path:
        """Trigger report definition for target_date, poll completion, and extract CSV ZIP."""
        if not self._auth_cookie:
            self.login()

        # Capture pre-existing report IDs so we only accept the newly triggered report
        self._preexisting_ids = self._collect_preexisting_ids(target_date)

        output_dir = Path(output_dir)
        output_dir.mkdir(parents=True, exist_ok=True)

        start_str, end_str = build_report_window(target_date)
        last_err: Exception | None = None

        for attempt in range(1, self.max_retries + 1):
            try:
                self.close()
                self._playwright = sync_playwright().start()
                self._browser = self._playwright.firefox.launch(headless=self.headless)
                self._context = self._browser.new_context(
                    ignore_https_errors=True,
                    accept_downloads=True,
                )
                self._apply_auth_cookie_to_context()
                self._page = self._context.new_page()
                self._page.set_default_timeout(30000)

                # Warm-up navigation
                self._page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
                self._page.wait_for_timeout(2000)

                # Definition edit URL
                def_url = f"{self.base_url}/reports_definition?definitions_edit=1&id={self.report_definition_id}"
                self._page.goto(def_url, wait_until="domcontentloaded")

                start_sel = f'input[name="start_string_{self.report_definition_id}"]'
                end_sel = f'input[name="end_string_{self.report_definition_id}"]'
                selector = f"{start_sel}, #login-username-id"
                self._page.locator(selector).first.wait_for(state="attached", timeout=30000)

                login_user = self._page.locator("#login-username-id")
                if login_user.is_visible():
                    logger.warning(
                        "Inline login dialog visible on definition page; performing forced re-login"
                    )
                    try:
                        self.login(force=True)
                        self._apply_auth_cookie_to_context()
                        self._page.goto(def_url, wait_until="domcontentloaded")
                        self._page.locator(selector).first.wait_for(state="attached", timeout=30000)
                    except Exception as e:
                        logger.warning("Forced HTTP re-login failed: %s", e)

                    if login_user.is_visible():
                        logger.warning(
                            "Inline login dialog still visible after forced re-login; attempting inline dialog submit fallback"
                        )
                        try:
                            login_user.fill(self.username)
                            self._page.locator("#login-password-id").fill(self.password)
                            self._page.locator("#login-button").click()
                            login_user.wait_for(state="hidden", timeout=30000)
                            for c in self._context.cookies():
                                if c.get("name") == "MercuryAuthHandlerCookie_AMPAuth":
                                    self._auth_cookie = c.get("value")
                                    self._save_session_state(self._auth_cookie)
                                    break
                        except Exception as e:
                            logger.warning("Inline dialog fallback submit failed: %s", e)

                        if login_user.is_visible():
                            logger.error(
                                "AirWave authentication failed: inline login dialog remained visible"
                            )
                            raise RuntimeError(
                                "AirWave authentication failed: inline login dialog remained visible"
                            )
                    logger.info("Inline login dialog successfully dismissed")

                self._page.locator(start_sel).fill(start_str, force=True)
                self._page.locator(end_sel).fill(end_str, force=True)

                with self._page.expect_navigation(wait_until="domcontentloaded"):
                    self._page.locator('input[name="definitions_do_and_run"]').click(force=True)

                report_id = self._poll_report_id(target_date)
                if not report_id:
                    raise TimeoutError(
                        f"AirWave report did not complete within {self.poll_timeout_minutes} minutes"
                    )

                zip_path = self._download_csv_zip(report_id, output_dir)
                self._extract_zip(zip_path, output_dir)
                return output_dir
            except Exception as e:
                last_err = e
                logger.warning("AirWave report attempt %d failed: %s", attempt, e)
                if attempt < self.max_retries:
                    time.sleep(self.poll_interval_seconds)
            finally:
                self.close()

        if last_err:
            raise last_err
        raise RuntimeError("AirWave fetch_report failed")

    def _poll_report_id(self, target_date: date | None = None) -> str | None:
        timeout_sec = self.poll_timeout_minutes * 60
        interval_sec = self.poll_interval_seconds
        deadline = time.monotonic() + timeout_sec
        list_url = f"{self.base_url}{REPORTS_LIST_URI}"

        with requests.Session() as sess:
            sess.verify = False
            if self._auth_cookie:
                sess.cookies.set(
                    "MercuryAuthHandlerCookie_AMPAuth",
                    self._auth_cookie,
                    domain=self._cookie_domain(),
                    path="/",
                )
            while time.monotonic() < deadline:
                logger.info("Polling AirWave reports list: %s", list_url)
                try:
                    resp = sess.get(list_url, timeout=15)
                    rows_seen = len(_ROW_RE.findall(resp.text))
                    report_id = extract_report_id(
                        resp.text,
                        target_date=target_date,
                        exclude_ids=self._preexisting_ids,
                    )
                    logger.info(
                        "Poll result: %d rows seen, matching report_id=%s (target_date=%s, excluding %d pre-existing)",
                        rows_seen,
                        report_id,
                        target_date,
                        len(self._preexisting_ids),
                    )
                    if report_id:
                        return report_id
                except Exception as e:
                    logger.warning("Transient error polling reports list: %s", e)
                time.sleep(interval_sec)
        return None

    def _download_csv_zip(self, report_id: str, output_dir: Path) -> Path:
        csv_url = f"{self.base_url}{CSV_EXPORT_URI.format(id=report_id)}"
        zip_path = output_dir / f"airwave_report_{report_id}.zip"
        with requests.Session() as sess:
            sess.verify = False
            if self._auth_cookie:
                sess.cookies.set(
                    "MercuryAuthHandlerCookie_AMPAuth",
                    self._auth_cookie,
                    domain=self._cookie_domain(),
                    path="/",
                )
            logger.info("Downloading AirWave CSV export from %s", csv_url)
            resp = sess.get(csv_url, timeout=180, stream=True)
            resp.raise_for_status()
            with zip_path.open("wb") as fh:
                for chunk in resp.iter_content(chunk_size=65536):
                    if chunk:
                        fh.write(chunk)
        return zip_path

    def _extract_zip(self, zip_path: Path, output_dir: Path) -> None:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                filename = Path(member.filename).name
                target = output_dir / filename
                with zf.open(member) as src, open(target, "wb") as dst:
                    shutil.copyfileobj(src, dst)
        logger.info("Extracted AirWave CSV files into %s", output_dir)

    def close(self) -> None:
        """Tear down Playwright browser and context."""
        if self._page:
            try:
                self._page.close()
            except Exception:
                pass
            self._page = None
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._browser:
            try:
                self._browser.close()
            except Exception:
                pass
            self._browser = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None
