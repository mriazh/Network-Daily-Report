"""Native CMP (Cisco Jasper / Control Center) Crawler for Telkomsel IoT M2M using Playwright."""

from __future__ import annotations

import calendar
import email as email_lib
from email.utils import parsedate_to_datetime
import imaplib
import logging
import os
from pathlib import Path
import re
import sys
import time
from dataclasses import dataclass

from playwright.sync_api import Page, sync_playwright


logger = logging.getLogger(__name__)


_SERVICE_LOGIN_URL = (
    "https://ep.iotcc.telkomsel.com/cas/login?service=https%3A%2F%2Fep.iotcc.telkomsel.com"
    "%2Fcas%2Foauth2.0%2FcallbackAuthorize%3Fclient_id%3DenterprisePortal%26redirect_uri%3D"
    "https%253A%252F%252Fep.iotcc.telkomsel.com%26response_type%3Dcode%26client_name%3DCasOAuthClient"
)


@dataclass
class CmpExportResult:
    daily_file: Path | None = None
    monthly_file: Path | None = None
    error: str | None = None


def _resolve_proxy_server(cfg_cmp_proxy: str = "", cfg_warp_proxy: str = "") -> str | None:
    """Resolve proxy server from explicit config, env var, or WARP default.

    Priority:
    1. cfg_cmp_proxy  (explicit CMP proxy, e.g. CMP_PROXY_SERVER)
    2. cfg_warp_proxy (WARP proxy_url from config)
    3. CMP_PROXY_SERVER environment variable
    4. WARP local socks5 default on Linux
    """
    if cfg_cmp_proxy:
        return cfg_cmp_proxy
    if cfg_warp_proxy:
        return cfg_warp_proxy

    env_proxy = os.getenv("CMP_PROXY_SERVER")
    if env_proxy:
        return env_proxy

    # ponytail: assumes local WARP socks5 listener is reachable on port 40000.
    # Add WARP_PROXY_SOCKS_PORT when a custom port is in use.
    if sys.platform.startswith("linux"):
        return "socks5://127.0.0.1:40000"
    return None


def _get_firefox_user_agent() -> str:
    """Return a standard desktop Firefox user-agent."""
    return "Mozilla/5.0 (X11; Linux x86_64; rv:128.0) Gecko/20100101 Firefox/128.0"


def _fetch_otp_from_imap(
    imap_host: str,
    imap_port: int,
    email: str,
    email_password: str,
    timeout_seconds: int = 120,
    poll_interval_seconds: int = 5,
    start_time: float | None = None,
) -> str | None:
    """
    Connect to IMAP and retrieve the 6-digit OTP from email subject 'CMP - YOUR TOKEN'.

    Args:
        imap_host: IMAP server host
        imap_port: IMAP server port (typically 993 for SSL)
        email: Email address
        email_password: Email password
        timeout_seconds: Total time to wait for OTP
        poll_interval_seconds: Time between IMAP polls
        start_time: Epoch timestamp marking login start

    Returns:
        6-digit OTP string or None if not found within timeout
    """
    if not all([imap_host, email, email_password]):
        logger.warning("IMAP credentials incomplete; skipping OTP retrieval")
        return None

    if start_time is None:
        start_time = time.time()
    cutoff = start_time - 30

    deadline = time.time() + timeout_seconds
    subject_pattern = re.compile(r"CMP\s*-\s*YOUR\s*TOKEN", re.IGNORECASE)
    otp_pattern = re.compile(r"\b(\d{6})\b")

    while time.time() < deadline:
        try:
            with imaplib.IMAP4_SSL(imap_host, imap_port) as mail:
                mail.login(email, email_password)
                mail.select("INBOX", readonly=True)

                # Search for recent emails with matching subject
                typ, data = mail.search(None, "SUBJECT", "CMP")
                if typ != "OK" or not data[0]:
                    logger.debug("No emails with 'CMP' in subject found")
                else:
                    # Check most recent emails first
                    email_ids = data[0].split()[-10:]  # Last 10 emails
                    for eid in reversed(email_ids):
                        typ, msg_data = mail.fetch(eid, "(RFC822)")
                        if typ != "OK":
                            continue
                        msg = email_lib.message_from_bytes(msg_data[0][1])
                        date_str = msg.get("Date", "")
                        try:
                            msg_date = parsedate_to_datetime(date_str)
                            if msg_date.tzinfo is not None:
                                msg_date_epoch = msg_date.timestamp()
                            else:
                                msg_date_epoch = msg_date.astimezone().timestamp()
                        except Exception:
                            continue

                        if msg_date_epoch < cutoff:
                            continue

                        subject = msg.get("Subject", "")
                        if subject_pattern.search(subject):
                            # Search body for 6-digit OTP
                            body = ""
                            if msg.is_multipart():
                                for part in msg.walk():
                                    if part.get_content_type() == "text/plain":
                                        body += part.get_payload(decode=True).decode(
                                            errors="ignore"
                                        )
                            else:
                                body = msg.get_payload(decode=True).decode(errors="ignore")

                            match = otp_pattern.search(body)
                            if match:
                                otp = match.group(1)
                                logger.info("Retrieved OTP from email: %s", otp)
                                return otp
        except (imaplib.IMAP4.error, OSError, UnicodeDecodeError, KeyError) as e:
            logger.debug("IMAP fetch attempt failed: %s", e)

        time.sleep(poll_interval_seconds)

    logger.warning("OTP not found within %d seconds", timeout_seconds)
    return None


def _wait_for_download(page: Page, timeout: int = 60000) -> Path | None:
    """Wait for a download to complete and return the file path."""
    try:
        with page.expect_download(timeout=timeout) as download_info:
            # The download should be triggered by the export button click
            pass
        download = download_info.value
        # Save to a temporary location first
        suggested_name = download.suggested_filename
        temp_path = Path.cwd() / "tmp" / suggested_name
        temp_path.parent.mkdir(parents=True, exist_ok=True)
        download.save_as(temp_path)
        logger.info("Downloaded file: %s", temp_path)
        return temp_path
    except Exception:  # noqa: BLE001
        logger.warning("Download wait failed")
        return None


def _login_cmp(
    page: Page,
    base_url: str,
    username: str,
    password: str,
    timeout: int,
    imap_host: str = "",
    imap_port: int = 993,
    email: str = "",
    email_password: str = "",
    otp_timeout_seconds: int = 120,
    otp_poll_interval_seconds: int = 5,
) -> bool:
    """Login to CMP portal with CAS and OTP handling."""
    try:
        if not base_url or "service=" not in base_url:
            base_url = _SERVICE_LOGIN_URL
        logger.info("Navigating to CMP login: %s", base_url)
        page.goto(base_url, wait_until="domcontentloaded", timeout=timeout)

        # Wait for login form - CAS login page
        page.wait_for_selector(
            'input[name="username"], input[id="username"], input[type="text"]', timeout=timeout
        )

        # Fill credentials
        page.fill('input[name="username"], input[id="username"], input[type="text"]', username)
        page.fill('input[name="password"], input[id="password"], input[type="password"]', password)

        # Record login start time before clicking login
        login_start = time.time()

        # Submit login form with bounded retry sequence
        submitted = False
        try:
            page.locator('#fm1 input[name="submit"]').click(timeout=15000)
            submitted = True
        except Exception as e:
            logger.debug("Attempt 1 click #fm1 input[name='submit'] failed: %s", e)

        if not submitted:
            try:
                page.locator('input[name="submit"]').click(force=True, timeout=15000)
                submitted = True
            except Exception as e:
                logger.debug("Attempt 2 click input[name='submit'] with force=True failed: %s", e)

        if not submitted:
            try:
                page.locator("#password").press("Enter", timeout=15000)
                submitted = True
            except Exception as e:
                logger.debug("Attempt 3 press Enter on #password failed: %s", e)

        if not submitted:
            logger.error("All login submission attempts failed")
            return False

        # Wait for navigation after login - could be OTP page
        page.wait_for_timeout(4000)

        # Check for OTP/2FA page
        token_input_selectors = [
            "input[id='token']",
            "#token",
            "input[name='token']",
            "input[name='otp']",
            "input[placeholder*='Token' i]",
            "input[placeholder*='OTP' i]",
        ]

        otp_page_detected = False
        for sel in token_input_selectors:
            if page.locator(sel).count() > 0:
                otp_page_detected = True
                logger.info("OTP page detected, retrieving OTP via IMAP")
                break

        if otp_page_detected:
            # Retrieve OTP from email
            otp = _fetch_otp_from_imap(
                imap_host=imap_host,
                imap_port=imap_port,
                email=email,
                email_password=email_password,
                timeout_seconds=otp_timeout_seconds,
                poll_interval_seconds=otp_poll_interval_seconds,
                start_time=login_start,
            )

            if not otp:
                logger.error("Failed to retrieve OTP from email")
                return False

            # Fill OTP and submit
            for sel in token_input_selectors:
                if page.locator(sel).count() > 0:
                    page.fill(sel, otp)
                    break

            # Click submit/verify button with Enter-key fallback
            try:
                page.click("input[name='_eventId_submit']")
            except Exception:
                page.locator("input[id='token']").press("Enter")
            page.wait_for_timeout(3000)
            logger.info("OTP submitted successfully")

        logger.info("CMP login successful")
        return True
    except Exception:
        logger.exception("CMP login failed")
        return False


def _handle_2fa_if_needed(page: Page, timeout: int) -> bool:
    """Handle 2FA if presented (e.g., OTP via email)."""
    try:
        # Check for 2FA/OTP page
        otp_selectors = [
            'input[name="otp"]',
            'input[name="code"]',
            'input[placeholder*="OTP" i]',
            'input[placeholder*="code" i]',
            "#otp",
            "#verificationCode",
        ]
        for sel in otp_selectors:
            if page.locator(sel).count() > 0:
                logger.info("2FA/OTP page detected, but automated OTP retrieval not implemented")
                logger.info("Please implement OTP retrieval via IMAP or manual entry")
                return False
        return True
    except Exception:  # noqa: BLE001
        logger.debug("2FA check error")
        return True


def _navigate_to_dashboard(page: Page, timeout: int) -> bool:
    """Navigate to CMP Dashboard and wait for dashboard container."""
    dashboard_url = "https://ep.iotcc.telkomsel.com/#!dashboard"
    try:
        if page.url.endswith("#!dashboard"):
            logger.info("Already on CMP Dashboard via URL: %s", page.url)
        else:
            logger.info("Attempting to navigate to CMP Dashboard via SPA menu item")
            clicked = False
            menu_selectors = [
                'div.main-menu-item[role="button"]:has(span.main-menu-item-caption:has-text("Dashboard"))',
                'span.main-menu-item-caption:has-text("Dashboard")',
            ]
            for sel in menu_selectors:
                try:
                    loc = page.locator(sel).filter(visible=True).first
                    loc.wait_for(state="visible", timeout=30000)
                    loc.click()
                    clicked = True
                    logger.info("Clicked dashboard menu item using selector: %s", sel)
                    break
                except Exception as e:
                    logger.debug("Failed clicking dashboard menu selector %s: %s", sel, e)
                    continue

            if clicked:
                # Wait for URL fragment up to ~20s
                start_time = time.time()
                success_fragment = False
                while time.time() - start_time < 20:
                    if "#!dashboard" in page.url:
                        success_fragment = True
                        break
                    page.wait_for_timeout(500)
                if not success_fragment:
                    logger.warning(
                        "Menu click did not lead to #!dashboard in URL within 20s, falling back to hash goto"
                    )
                    page.goto(dashboard_url, wait_until="commit", timeout=timeout)
            else:
                logger.warning("All dashboard menu selectors failed, falling back to direct goto")
                page.goto(dashboard_url, wait_until="commit", timeout=timeout)

        # Wait for dashboard container
        dashboard_selectors = [
            ".dashboard-container",
            ".v-dashboard",
            "[class*='dashboard']",
        ]
        for sel in dashboard_selectors:
            try:
                page.wait_for_selector(sel, timeout=10000)
                logger.info("Dashboard container found: %s", sel)
                return True
            except Exception as e:
                logger.debug("Dashboard selector %s not found: %s", sel, e)
                continue

        logger.warning("Dashboard container not found with known selectors")
        return False
    except Exception:
        logger.exception("Navigation to Dashboard failed")
        return False


def _extract_live_usage_from_dashboard(page: Page, timeout: int) -> tuple[str | None, float | None]:
    """
    Extract live total usage value from dashboard DOM.

    Returns:
        Tuple of (raw_text, usage_gb) where usage_gb is the parsed float value in GB
    """
    value_selectors = [
        "div.spark-value.huge",
        "div.v-label-spark-value.huge",
        "div.huge",
        "[class*='spark-value'][class*='huge']",
        "[class*='huge']:has-text('GB')",
    ]

    for sel in value_selectors:
        try:
            elements = page.locator(sel)
            count = elements.count()
            for i in range(count):
                text = elements.nth(i).inner_text(timeout=5000).strip()
                if text and ("GB" in text.upper() or "TB" in text.upper() or "MB" in text.upper()):
                    logger.info("Found live usage value: %s (selector: %s)", text, sel)
                    # Parse the value
                    usage_gb = _parse_usage_to_gb(text)
                    return text, usage_gb
        except Exception as e:
            logger.debug("Error extracting usage with selector %s: %s", sel, e)
            continue

    logger.warning("Live usage value not found on dashboard")
    return None, None


def _parse_usage_to_gb(text: str) -> float | None:
    """Parse usage text like '238.78 GB' or '1.5 TB' to float GB."""
    import re

    text = text.strip().upper().replace(",", "")
    # Match patterns like "238.78 GB", "1.5 TB", "500 MB"
    match = re.search(r"([\d.]+)\s*(TB|GB|MB)", text)
    if not match:
        return None

    value = float(match.group(1))
    unit = match.group(2)

    if unit == "TB":
        return value * 1024
    elif unit == "GB":
        return value
    elif unit == "MB":
        return value / 1024

    return None


def _resolve_iot_raw_dir(output_dir: Path, date_compact: str) -> Path:
    """Resolve destination directory for IoT raw artifacts (screenshots and XLSX exports)."""
    if output_dir.name == date_compact:
        target = output_dir
    elif output_dir.name == "iot":
        target = output_dir / date_compact
    elif "raw" in output_dir.parts and "iot" in output_dir.parts:
        target = output_dir / date_compact
    else:
        target = output_dir / "raw" / "iot" / date_compact
    target.mkdir(parents=True, exist_ok=True)
    return target


def _wait_for_loading(page: Page, timeout_ms: int = 15000) -> None:
    """Wait for active Vaadin loading indicator to disappear."""
    timeout = max(timeout_ms, 1000)
    try:
        indicator = page.locator(".v-loading-indicator").first
        if indicator.count() > 0 and indicator.is_visible():
            indicator.wait_for(state="hidden", timeout=timeout)
    except Exception:
        pass
    try:
        page.wait_for_load_state("domcontentloaded", timeout=timeout)
    except Exception:
        pass


def _save_dashboard_screenshot_and_sidecar(
    page: Page,
    output_dir: Path,
    date_compact: str,
) -> Path | None:
    """
    Take screenshot of dashboard element.

    Returns:
        Path to screenshot or None if failed
    """
    try:
        # Create output directory: output/raw/iot/{date_compact}/
        iot_raw_dir = _resolve_iot_raw_dir(output_dir, date_compact)

        # Screenshot filename
        screenshot_path = iot_raw_dir / f"dashboard_{date_compact}.png"

        # Try to screenshot the dashboard container specifically
        dashboard_selectors = [
            "div.v-csslayout.v-layout.v-widget.sparks.v-csslayout-sparks.v-has-width",
            "div.sparks.v-csslayout-sparks.v-has-width",
            '[class*="sparks"][class*="v-csslayout"]',
            ".dashboard-container",
            ".v-dashboard",
            "[class*='dashboard']",
        ]

        element = None
        for sel in dashboard_selectors:
            loc = page.locator(sel)
            if loc.count() > 0:
                for i in range(loc.count()):
                    candidate = loc.nth(i)
                    try:
                        if candidate.is_visible():
                            box = candidate.bounding_box()
                            if box and box["width"] > 0 and box["height"] > 0:
                                element = candidate
                                logger.info(
                                    "Found valid dashboard screenshot element via selector: %s", sel
                                )
                                break
                    except Exception:
                        continue
                if element:
                    break

        if element:
            element.screenshot(path=str(screenshot_path))
        else:
            # Fallback: full page screenshot
            page.screenshot(path=str(screenshot_path))

        logger.info("Dashboard screenshot saved to: %s", screenshot_path)

        return screenshot_path
    except Exception:
        logger.exception("Failed to save dashboard screenshot")
        return None


def _navigate_to_usage_query(page: Page, timeout: int) -> bool:
    """Navigate to Reports -> Usage Query page."""
    try:
        # Try various navigation paths
        nav_paths = [
            ("Reports", "Usage Query"),
            ("Laporan", "Query Penggunaan"),
            ("Usage", "Query"),
        ]

        for menu, submenu in nav_paths:
            try:
                # Click main menu
                page.click(f'text="{menu}"', timeout=5000)
                page.wait_for_timeout(1000)
                # Click submenu
                page.click(f'text="{submenu}"', timeout=5000)
                page.wait_for_load_state("domcontentloaded", timeout=timeout)
                logger.info("Navigated to Usage Query via %s -> %s", menu, submenu)
                return True
            except Exception:  # noqa: BLE001, S112
                continue

        # Fallback: CMP portal uses SPA hash routing, so the fragment
        # (!reports) is never sent to the server and the page stays on the
        # same document. Navigate with wait_until="commit".
        page.goto("https://ep.iotcc.telkomsel.com/#!reports", wait_until="commit", timeout=timeout)
        return True
    except Exception:
        logger.exception("Navigation to Usage Query failed")
        return False


def compute_monthly_date_range(target_month: str) -> tuple[str, str]:
    """Compute (start_date, end_date) for target_month (YYYY-MM or YYYY-MM-DD) as YYYY-MM-DD strings.

    Args:
        target_month: Target month string in 'YYYY-MM' or 'YYYY-MM-DD' format.

    Returns:
        Tuple of (first_day_iso, last_day_iso) strings, e.g. ('2026-10-01', '2026-10-31').
    """
    parts = target_month.strip().split("-")
    if len(parts) < 2:
        raise ValueError(f"Invalid target_month format: {target_month!r}, expected YYYY-MM")
    year = int(parts[0])
    month = int(parts[1])
    last_day = calendar.monthrange(year, month)[1]
    return f"{year:04d}-{month:02d}-01", f"{year:04d}-{month:02d}-{last_day:02d}"


def select_report_type(page: Page, report_type: str, timeout_ms: int = 30000) -> bool:
    """Select Report Type (Daily or Monthly) using Vaadin v-filterselect widget.

    Args:
        page: Playwright Page instance.
        report_type: Target report type string ('Daily' or 'Monthly').
        timeout_ms: Timeout in milliseconds.

    Returns:
        True if report type was selected or already active, False on failure. Never raises.
    """
    try:
        logger.info("Selecting Report Type: %s", report_type)
        _wait_for_loading(page, timeout_ms=min(timeout_ms, 15000))

        filter_inputs = page.locator("input.v-filterselect-input")
        try:
            input_count = filter_inputs.count()
        except Exception:
            input_count = 0

        if input_count == 0:
            logger.warning("No input.v-filterselect-input elements found")
            return False

        target_lower = report_type.strip().lower()

        # Step 1: Check if already set to requested type
        for index in range(input_count):
            candidate = filter_inputs.nth(index)
            try:
                val = candidate.input_value()
            except Exception:
                continue
            if isinstance(val, str) and val.strip().lower() == target_lower:
                logger.info("Report Type is already %s", report_type)
                return True

        # Step 2: Locate the button for the Report Type filterselect
        btn = None
        for index in range(input_count):
            candidate = filter_inputs.nth(index)
            try:
                val = candidate.input_value()
            except Exception:
                val = ""

            val_lower = val.strip().lower() if isinstance(val, str) else ""

            ancestor = candidate.locator(
                "xpath=ancestor::div[contains(concat(' ', normalize-space(@class), ' '), ' v-filterselect ')][1]"
            )
            scoped_button = ancestor.locator("div.v-filterselect-button[role='button']").first

            # Check if value matches alternate report type
            if val_lower in ("daily", "monthly"):
                try:
                    if scoped_button.count() > 0:
                        btn = scoped_button
                        break
                except Exception:
                    pass

            is_report_type = False
            try:
                row_container = candidate.locator(
                    "xpath=ancestor::*[contains(@class, 'v-formlayout-row') or contains(@class, 'v-layout') or contains(@class, 'v-widget') or self::tr][1]"
                )
                if row_container.count() > 0:
                    text = row_container.inner_text()
                    if "report type" in text.lower():
                        is_report_type = True
            except Exception:
                pass

            if not is_report_type:
                try:
                    preceding = candidate.locator(
                        "xpath=preceding::*[self::label or contains(@class, 'v-caption') or contains(@class, 'v-captiontext')][1]"
                    )
                    if preceding.count() > 0:
                        text = preceding.inner_text()
                        if "report type" in text.lower():
                            is_report_type = True
                except Exception:
                    pass

            if is_report_type:
                try:
                    if scoped_button.count() > 0:
                        btn = scoped_button
                        break
                except Exception:
                    pass

        # Fallback button locators
        if btn is None:
            try:
                label_container = page.locator(
                    "xpath=//*[self::label or contains(@class, 'v-caption') or contains(@class, 'v-captiontext')]"
                    "[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'report type')]"
                    "/ancestor::*[contains(@class, 'v-formlayout-row') or contains(@class, 'v-layout') or contains(@class, 'v-widget') or self::tr][1]"
                ).first
                if label_container.count() > 0:
                    candidate_btn = label_container.locator(
                        "div.v-filterselect-button[role='button']"
                    ).first
                    if candidate_btn.count() > 0:
                        btn = candidate_btn
            except Exception:
                pass

        if btn is None:
            try:
                following_btn = page.locator(
                    "xpath=//*[self::label or contains(@class, 'v-caption') or contains(@class, 'v-captiontext')]"
                    "[contains(translate(., 'ABCDEFGHIJKLMNOPQRSTUVWXYZ', 'abcdefghijklmnopqrstuvwxyz'), 'report type')]"
                    "/following::div[contains(@class, 'v-filterselect-button')][1]"
                ).first
                if following_btn.count() > 0:
                    btn = following_btn
            except Exception:
                pass

        if btn is None:
            try:
                buttons = page.locator("div.v-filterselect-button[role='button']")
                count = buttons.count()
                if count > 0:
                    btn = buttons.nth(1 if count > 1 else 0)
            except Exception:
                pass

        if btn is None:
            logger.warning("Report Type filterselect button was not found")
            return False

        # Step 3: Open the filterselect overlay menu
        try:
            btn.wait_for(state="visible", timeout=min(timeout_ms, 15000))
            btn.click()
        except Exception as exc:
            logger.warning("Report Type filterselect button click failed: %s", exc)
            return False

        # Step 4: Pick the overlay item by visible text case-insensitively.
        # The CMP portal renders the option as a bare <span> inside the overlay
        # container, with no v-select-item class, so the text match must also
        # consider plain elements inside the overlay.
        overlay_selector = (
            "td.gwt-MenuItem[role='listitem'], .gwt-MenuItem, .v-filterselect-suggestmenu span, "
            "div[role='option'], div.v-select-item, span.v-select-item-label, .v-filterselect-suggestpopup span"
        )
        overlay_options = page.locator(overlay_selector)
        pattern = re.compile(rf"^\s*{re.escape(report_type)}\s*$", re.IGNORECASE)

        target_option = None
        try:
            filtered = overlay_options.filter(has_text=pattern)
            if filtered.count() > 0:
                target_option = filtered.first
        except Exception:
            pass

        if target_option is None:
            try:
                total_opts = overlay_options.count()
                for i in range(total_opts):
                    opt = overlay_options.nth(i)
                    try:
                        if opt.inner_text().strip().lower() == target_lower:
                            target_option = opt
                            break
                    except Exception:
                        continue
            except Exception:
                pass

        if target_option is None:
            # Fallback: the option may be a plain element with no known class.
            # Scope the search to the overlay container so unrelated page text
            # (for example a "Daily" heading) cannot be clicked by mistake.
            for scope_selector in (
                ".v-overlay-container",
                ".v-filterselect-suggestpopup",
                "[class*='overlay']",
            ):
                scope = page.locator(scope_selector)
                if scope.count() == 0:
                    continue
                try:
                    exact = scope.get_by_text(re.compile(rf"^\s*{re.escape(report_type)}\s*$"))
                    if exact.count() > 0:
                        target_option = exact.first
                        break
                    any_text = scope.get_by_text(report_type, exact=False)
                    if any_text.count() > 0:
                        target_option = any_text.first
                        break
                except Exception:
                    continue

        if target_option is None:
            # Last resort: a page-wide exact text match.
            try:
                exact = page.get_by_text(re.compile(rf"^\s*{re.escape(report_type)}\s*$"))
                if exact.count() > 0:
                    target_option = exact.first
            except Exception:
                pass

        if target_option is None:
            logger.warning("Report Type overlay menu item '%s' not found", report_type)
            return False

        try:
            target_option.wait_for(state="visible", timeout=min(timeout_ms, 15000))
            target_option.click()
        except Exception as exc:
            logger.warning("Failed clicking Report Type overlay item '%s': %s", report_type, exc)
            return False

        _wait_for_loading(page, timeout_ms=min(timeout_ms, 15000))
        logger.info("Report Type successfully selected: %s", report_type)
        return True
    except Exception as exc:
        logger.warning("select_report_type encountered unexpected error: %s", exc)
        return False


def _fill_query_dates(page: Page, start_date: str, end_date: str, timeout_ms: int = 15000) -> bool:
    """Fill date range inputs with start_date and end_date."""
    logger.info("Setting query dates: start=%s, end=%s", start_date, end_date)
    _wait_for_loading(page, timeout_ms=min(timeout_ms, 5000))

    # Allow Vaadin DOM to render date inputs
    date_inputs = page.locator("input.v-textfield.v-datefield-textfield")
    for _ in range(10):
        try:
            if date_inputs.count() >= 2:
                break
        except Exception:
            pass
        if hasattr(page, "wait_for_timeout"):
            page.wait_for_timeout(200)

    try:
        count = date_inputs.count()
    except Exception:
        count = 0

    if count < 2:
        date_inputs = page.locator(".v-datefield input, .v-datefield-textfield")
        try:
            count = date_inputs.count()
        except Exception:
            count = 0

    if count < 2:
        date_inputs = page.locator(
            "xpath=//input[not(contains(@class, 'v-filterselect-input')) and not(@type='hidden') and not(@type='submit') and not(@type='button')]"
        )
        try:
            count = date_inputs.count()
        except Exception:
            count = 0

    if count < 2:
        logger.warning("Expected at least 2 date inputs, found %d", count)
        return False

    def _press_escape() -> None:
        if hasattr(page, "keyboard") and hasattr(page.keyboard, "press"):
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass

    try:
        _press_escape()
        first_input = date_inputs.nth(0)
        first_input.wait_for(state="visible", timeout=timeout_ms)
        try:
            first_input.click(timeout=5000)
        except Exception:
            if hasattr(first_input, "focus"):
                first_input.focus()
        first_input.fill(start_date)
        _press_escape()
        _wait_for_loading(page, timeout_ms=3000)

        date_inputs = page.locator("input.v-textfield.v-datefield-textfield")
        if date_inputs.count() < 2:
            date_inputs = page.locator(".v-datefield input, .v-datefield-textfield")

        second_input = date_inputs.nth(1)
        second_input.wait_for(state="visible", timeout=timeout_ms)
        try:
            second_input.click(timeout=5000)
        except Exception:
            if hasattr(second_input, "focus"):
                second_input.focus()
        second_input.fill(end_date)
        _press_escape()
        _wait_for_loading(page, timeout_ms=3000)
        return True
    except Exception as e:
        logger.warning("Standard date fill failed (%s); attempting evaluate fallback", e)
        if hasattr(page, "evaluate"):
            try:
                filled = page.evaluate(
                    """([s, e]) => {
                        const inps = Array.from(document.querySelectorAll('.v-datefield input, input.v-datefield-textfield, input[type="text"]'))
                            .filter(el => !el.classList.contains('v-filterselect-input') && el.offsetParent !== null);
                        if (inps.length >= 2) {
                            inps[0].value = s;
                            inps[0].dispatchEvent(new Event('input', {bubbles: true}));
                            inps[0].dispatchEvent(new Event('change', {bubbles: true}));
                            inps[1].value = e;
                            inps[1].dispatchEvent(new Event('input', {bubbles: true}));
                            inps[1].dispatchEvent(new Event('change', {bubbles: true}));
                            document.querySelectorAll('.v-datefield-popup, .v-popupview-popup').forEach(p => p.style.display = 'none');
                            return true;
                        }
                        return false;
                    }""",
                    [start_date, end_date],
                )
                _press_escape()
                if filled:
                    return True
            except Exception as eval_e:
                logger.warning("Evaluate fallback failed: %s", eval_e)
        return False


def _set_report_type_and_date(page: Page, report_type: str, target_date: str, timeout: int) -> bool:
    """Set report type (Daily/Monthly) using select_report_type and fill date range."""
    try:
        # Step 1: Select report type via Vaadin filterselect
        if not select_report_type(page, report_type, timeout_ms=timeout):
            logger.warning("Failed to select Report Type '%s'", report_type)
            return False

        # Step 2: Compute date range
        if report_type.lower() == "daily":
            start_date = target_date
            end_date = target_date
        else:
            start_date, end_date = compute_monthly_date_range(target_date)

        # Step 3: Fill dates
        if not _fill_query_dates(page, start_date, end_date, timeout_ms=min(timeout, 15000)):
            logger.warning(
                "Failed to fill query dates %s to %s for Report Type '%s'",
                start_date,
                end_date,
                report_type,
            )
            return False

        logger.info(
            "Configured Report Type '%s' with date range %s to %s",
            report_type,
            start_date,
            end_date,
        )
        return True
    except Exception:
        logger.exception("Failed to set report type and date")
        return False


def _sort_and_export(page: Page, timeout: int, export_timeout_ms: int | None = None) -> Path | None:
    """Submit search, sort by Total Data Usage descending, and export to XLSX."""
    if export_timeout_ms is None:
        try:
            from network_report.config import load_config_from_env

            export_timeout_ms = load_config_from_env().cmp.export_timeout_seconds * 1000
        except Exception:
            export_timeout_ms = int(os.getenv("CMP_EXPORT_TIMEOUT_SECONDS", "300")) * 1000
    try:
        # 1. Click Search button
        search_selectors = [
            ".v-button.icon-only.primary",
            "div[role='button'].v-button.icon-only.primary",
            "div[role='button'].v-button.primary",
            ".v-button.primary",
            'button:has-text("Search")',
            'button:has-text("Cari")',
            'button[type="submit"]',
            'input[type="submit"]',
        ]
        search_btn = None
        for sel in search_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0:
                    search_btn = loc
                    break
            except Exception:
                continue

        if search_btn:
            try:
                search_btn.click(timeout=10000)
            except Exception:
                search_btn.click(force=True, timeout=10000)
            _wait_for_loading(page, timeout_ms=min(timeout, 30000))
        else:
            logger.warning("Could not find Search button")

        # 2. Wait for results table
        try:
            table_loc = page.locator(
                ".v-table-table, .v-grid-tablewrapper, .v-table, table, [role='grid'], .data-table"
            ).first
            table_loc.wait_for(state="visible", timeout=min(timeout, 20000))
        except Exception:
            pass

        # 3. Sort by Total Data Usage descending
        sort_header_sel = (
            ".v-table-header-cell, th, .v-table-caption-container, [role='columnheader'], td"
        )
        try:
            header = page.locator(sort_header_sel).filter(has_text="Total Data Usage").first
            if header.count() > 0:
                header.click(force=True, timeout=5000)
                page.wait_for_timeout(500)
                header.click(force=True, timeout=5000)
                page.wait_for_timeout(500)
                logger.info("Sorted by Total Data Usage descending")
        except Exception as e:
            logger.debug("Sort click failed: %s", e)

        # 4. Export & Download
        # 1. Find the export icon: Try selectors in order:
        #    - span.v-icon.IcoMoon-Ultimate (icon font element)
        #    - Parent of span.v-icon.IcoMoon-Ultimate (clickable container)
        #    - Fallback: .v-menubar-menuitem:has-text('Export')
        # Click it. Wait 3 seconds for dropdown.
        export_btn = None
        for candidate in [
            page.locator("span.v-icon.IcoMoon-Ultimate").first,
            page.locator("span.v-icon.IcoMoon-Ultimate").locator("..").first,
            page.locator(".v-menubar-menuitem:has-text('Export')").first,
        ]:
            try:
                if candidate.count() > 0 and candidate.is_visible():
                    export_btn = candidate
                    break
            except Exception:
                pass

        if export_btn is None:
            for candidate in [
                page.locator("span.v-icon.IcoMoon-Ultimate").first,
                page.locator("span.v-icon.IcoMoon-Ultimate").locator("..").first,
                page.locator(".v-menubar-menuitem:has-text('Export')").first,
            ]:
                try:
                    if candidate.count() > 0:
                        export_btn = candidate
                        break
                except Exception:
                    pass

        if export_btn is not None:
            try:
                export_btn.click(timeout=5000)
            except Exception:
                export_btn.click(force=True, timeout=5000)
            page.wait_for_timeout(3000)

        # 2. Click "Export to xlsx": Look for dropdown item
        export_option = (
            page.locator("span.v-menubar-menuitem-caption")
            .filter(has_text=re.compile(r"export to (xlsx|excel)", re.IGNORECASE))
            .first
        )
        has_option = False
        try:
            has_option = export_option.count() > 0 and export_option.is_visible()
        except Exception:
            pass

        if not has_option:
            for fallback_sel in [
                'text="Export to xlsx"',
                ':text("Export to xlsx")',
                '.v-menubar-menuitem:has-text("Export to xlsx")',
                'span:has-text("Export to xlsx")',
                "[role='menuitem']:has-text('Export to xlsx')",
            ]:
                loc = page.locator(fallback_sel).first
                try:
                    if loc.count() > 0 and loc.is_visible():
                        export_option = loc
                        has_option = True
                        break
                except Exception:
                    pass

        # If dropdown not found, try clicking parent of the icon font if icon was clicked
        if not has_option:
            try:
                parent_btn = page.locator("span.v-icon.IcoMoon-Ultimate").locator("..").first
                if parent_btn.count() > 0:
                    try:
                        parent_btn.click(timeout=5000)
                    except Exception:
                        parent_btn.click(force=True, timeout=5000)
                    page.wait_for_timeout(3000)
                    opt = (
                        page.locator("span.v-menubar-menuitem-caption")
                        .filter(has_text=re.compile(r"export to (xlsx|excel)", re.IGNORECASE))
                        .first
                    )
                    if opt.count() > 0 and opt.is_visible():
                        export_option = opt
                        has_option = True
            except Exception:
                pass

        if not has_option:
            try:
                if export_option.count() > 0:
                    has_option = True
            except Exception:
                pass

        if has_option:
            try:
                export_option.click(timeout=5000)
            except Exception:
                export_option.click(force=True, timeout=5000)

            # 3. Wait for "Download" button: wait up to export timeout
            download_btn = page.locator(
                "div[role='button']:has-text('Download'), .v-button:has-text('Download')"
            ).first
            try:
                download_btn.wait_for(state="visible", timeout=export_timeout_ms)

                # 4. Download the file: Use page.expect_download(timeout=export_timeout_ms)
                with page.expect_download(timeout=export_timeout_ms) as download_info:
                    try:
                        download_btn.click(timeout=10000)
                    except Exception:
                        download_btn.click(force=True, timeout=10000)
                download = download_info.value
                suggested_name = download.suggested_filename
                temp_path = Path.cwd() / "tmp" / suggested_name
                temp_path.parent.mkdir(parents=True, exist_ok=True)
                download.save_as(temp_path)
                logger.info("Exported file via dialog: %s", temp_path)

                # 5. Close the popup: Click Close button if visible
                close_btn = page.locator(
                    "div[role='button']:has-text('Close'), .v-button:has-text('Close'), div.v-window-closebox"
                ).first
                try:
                    if close_btn.count() > 0 and close_btn.is_visible():
                        close_btn.click(timeout=5000)
                except Exception:
                    pass

                return temp_path
            except Exception as e:
                logger.warning("Popup download wait failed: %s", e)

        # Fallback: direct button/link download
        direct_export_selectors = [
            'button:has-text("Export")',
            'button:has-text("Download")',
            'button:has-text("Ekspor")',
            'a:has-text("Export")',
            'button[id*="export" i]',
            'button[class*="export" i]',
        ]
        for sel in direct_export_selectors:
            loc = page.locator(sel).first
            try:
                if loc.count() > 0:
                    with page.expect_download(timeout=export_timeout_ms) as download_info:
                        loc.click()
                    download = download_info.value
                    suggested_name = download.suggested_filename
                    temp_path = Path.cwd() / "tmp" / suggested_name
                    temp_path.parent.mkdir(parents=True, exist_ok=True)
                    download.save_as(temp_path)
                    logger.info("Exported file via direct click: %s", temp_path)
                    return temp_path
            except Exception as e:
                logger.debug("Direct export click %s failed: %s", sel, e)

        logger.warning("Could not complete export or find Download button")
        return None
    except Exception:
        logger.exception("Sort and export failed")
        return None


def run_cmp_crawler(
    base_url: str,
    username: str,
    password: str,
    target_date: str,  # YYYY-MM-DD
    target_month: str,  # YYYY-MM
    output_dir: Path,
    headless: bool = True,
    timeout_seconds: int = 60,
    imap_host: str = "",
    imap_port: int = 993,
    email: str = "",
    email_password: str = "",
    otp_timeout_seconds: int = 120,
    otp_poll_interval_seconds: int = 5,
    proxy_server: str = "",
    warp_proxy_server: str = "",
    export_timeout_seconds: int | None = None,
    monthly_export_enabled: bool | None = None,
    max_attempts: int = 3,
) -> CmpExportResult:
    """
    Run the CMP crawler to export both Daily and Monthly usage reports,
    and extract live dashboard usage.

    Args:
        base_url: CMP portal login URL
        username: CMP username
        password: CMP password
        target_date: Target date for daily report (YYYY-MM-DD)
        target_month: Target month for monthly report (YYYY-MM)
        output_dir: Directory to save exported files
        headless: Run browser in headless mode
        timeout_seconds: Timeout for page operations
        imap_host: IMAP host for OTP email retrieval
        imap_port: IMAP port
        email: Email address for OTP
        email_password: Email password
        otp_timeout_seconds: OTP retrieval timeout
        otp_poll_interval_seconds: OTP polling interval
        proxy_server: Explicit proxy server for CMP (e.g., "socks5://127.0.0.1:40001")
        warp_proxy_server: WARP proxy server from config
        max_attempts: Maximum number of attempts (default 3)

    Returns:
        CmpExportResult with paths to downloaded files and live_total_usage_gb
    """
    output_dir.mkdir(parents=True, exist_ok=True)
    timeout_ms = timeout_seconds * 1000
    if export_timeout_seconds is None:
        try:
            from network_report.config import load_config_from_env

            export_timeout_seconds = load_config_from_env().cmp.export_timeout_seconds
        except Exception:
            export_timeout_seconds = int(os.getenv("CMP_EXPORT_TIMEOUT_SECONDS", "300"))
    export_timeout_ms = export_timeout_seconds * 1000

    if monthly_export_enabled is None:
        try:
            from network_report.config import load_config_from_env

            monthly_export_enabled = load_config_from_env().cmp.monthly_export_enabled
        except Exception:
            monthly_export_enabled = os.getenv("CMP_MONTHLY_EXPORT_ENABLED", "false").lower() in (
                "true",
                "1",
                "yes",
            )
    date_compact = target_date.replace("-", "")
    iot_raw_dir = _resolve_iot_raw_dir(output_dir, date_compact)

    if not base_url or "service=" not in base_url:
        base_url = _SERVICE_LOGIN_URL

    # Resolve proxy server
    resolved_proxy = _resolve_proxy_server(proxy_server, warp_proxy_server)
    if resolved_proxy:
        logger.info("Using proxy server: %s", resolved_proxy)

    last_error: str | None = None

    for attempt in range(1, max_attempts + 1):
        logger.info("Starting CMP crawler attempt %d/%d for %s", attempt, max_attempts, target_date)
        try:
            with sync_playwright() as p:
                # Use Firefox with standard desktop user-agent to bypass portal restrictions
                launch_args = {"headless": headless}
                if resolved_proxy:
                    launch_args["proxy"] = {"server": resolved_proxy}
                browser = p.firefox.launch(**launch_args)
                context = browser.new_context(accept_downloads=True, user_agent=_get_firefox_user_agent())
                page = context.new_page()
                page.set_default_timeout(timeout_ms)

                result = CmpExportResult()

                try:
                    # Step 1: Login with CAS and OTP handling
                    if not _login_cmp(
                        page,
                        base_url,
                        username,
                        password,
                        timeout_ms,
                        imap_host=imap_host,
                        imap_port=imap_port,
                        email=email,
                        email_password=email_password,
                        otp_timeout_seconds=otp_timeout_seconds,
                        otp_poll_interval_seconds=otp_poll_interval_seconds,
                    ):
                        raise RuntimeError("Login failed")

                    # Step 2: Navigate to Dashboard and extract live usage
                    if _navigate_to_dashboard(page, timeout_ms):
                        page.wait_for_timeout(15000)
                        # Save screenshot
                        _save_dashboard_screenshot_and_sidecar(page, output_dir, date_compact)
                    else:
                        logger.warning("Failed to navigate to dashboard; skipping screenshot")

                    # Step 3: Handle 2FA if needed (fallback check)
                    if not _handle_2fa_if_needed(page, timeout_ms):
                        raise RuntimeError("2FA required but not automated")

                    # Step 4: Navigate to Usage Query
                    if not _navigate_to_usage_query(page, timeout_ms):
                        raise RuntimeError("Navigation to Usage Query failed")

                    # --- Daily Export ---
                    logger.info("Starting Daily export attempt for %s", target_date)
                    daily_ok = _set_report_type_and_date(page, "Daily", target_date, timeout_ms)
                    if daily_ok:
                        logger.info("Daily filter and date configured; initiating export and download")
                        try:
                            daily_temp = _sort_and_export(
                                page, timeout_ms, export_timeout_ms=export_timeout_ms
                            )
                        except TypeError:
                            daily_temp = _sort_and_export(page, timeout_ms)
                        if daily_temp and daily_temp.exists():
                            final_name = f"report_{date_compact}_DAILY_USAGE_by_SIM.xlsx"
                            final_path = iot_raw_dir / final_name
                            daily_temp.replace(final_path)
                            result.daily_file = final_path
                            logger.info("Daily export succeeded, saved to: %s", final_path)
                        else:
                            raise RuntimeError("Daily export failed: download did not produce a file")
                    else:
                        raise RuntimeError(f"Daily export failed: could not set report type 'Daily' or target date {target_date}")

                    # --- Monthly Export ---
                    if not monthly_export_enabled:
                        logger.info(
                            "CMP monthly export is disabled; month-to-date figure will be summed from the daily exports"
                        )
                    else:
                        logger.info("Starting Monthly export attempt for %s", target_month)
                        monthly_ok = _set_report_type_and_date(page, "Monthly", target_month, timeout_ms)
                        if monthly_ok:
                            logger.info(
                                "Monthly filter and date configured; initiating export and download"
                            )
                            try:
                                monthly_temp = _sort_and_export(
                                    page, timeout_ms, export_timeout_ms=export_timeout_ms
                                )
                            except TypeError:
                                monthly_temp = _sort_and_export(page, timeout_ms)
                            if monthly_temp and monthly_temp.exists():
                                month_compact = target_month.replace("-", "")
                                final_name = f"report_{month_compact}_MONTHLY_USAGE_by_SIM.xlsx"
                                final_path = iot_raw_dir / final_name
                                monthly_temp.replace(final_path)
                                result.monthly_file = final_path
                                logger.info("Monthly export succeeded, saved to: %s", final_path)
                            else:
                                raise RuntimeError("Monthly export failed: download did not produce a file")
                        else:
                            raise RuntimeError(f"Monthly export failed: could not set report type 'Monthly' or target month {target_month}")

                    logger.info("CMP crawler succeeded on attempt %d/%d", attempt, max_attempts)
                    return result

                except Exception as e:
                    last_error = str(e)
                    logger.warning("CMP crawler attempt %d/%d failed: %s", attempt, max_attempts, last_error)
                finally:
                    context.close()
                    browser.close()

        except Exception as e:
            last_error = str(e)
            logger.warning("CMP crawler attempt %d/%d failed (outer): %s", attempt, max_attempts, last_error)

        if attempt < max_attempts:
            logger.info("Retrying CMP crawler in 5 seconds (attempt %d/%d)...", attempt + 1, max_attempts)
            time.sleep(5)

    logger.error("All %d CMP crawler attempts failed", max_attempts)
    return CmpExportResult(error=last_error or "CMP crawler failed after all retries")
