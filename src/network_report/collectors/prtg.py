"""PRTG Sensor Status Scraper."""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass

import requests

logger = logging.getLogger(__name__)


@dataclass
class PrtgStatus:
    up: int
    paused: int
    down: int = 0
    down_ack: int = 0
    warning: int = 0
    unusual: int = 0
    unknown: int = 0
    total: int = 0


def fetch_prtg_status(
    base_url: str = "http://127.0.0.1:8080",
    username: str = "",
    password: str = "",
    timeout: int = 15,
) -> PrtgStatus | None:
    """Logs into PRTG (if credentials given) and parses sensor counters from welcome.htm."""
    base = base_url.rstrip("/")
    session = requests.Session()

    if username and password:
        login_url = f"{base}/public/checklogin.htm"
        login_payload = {
            "loginurl": "/welcome.htm",
            "username": username,
            "password": password,
        }
        try:
            resp = session.post(login_url, data=login_payload, timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:
            logger.debug("PRTG login form post failed: %s; attempting direct welcome page...", exc)

    welcome_url = f"{base}/welcome.htm"
    try:
        resp = session.get(welcome_url, timeout=timeout)
        resp.raise_for_status()
        html = resp.text
    except Exception as exc:
        logger.warning("Could not fetch PRTG welcome page at %s: %s", welcome_url, exc)
        return None

    # Primary: PRTG embedded lastStats object
    p_m = re.search(r"PausedSens.*?(\d+)", html)
    t_m = re.search(r"TotalSens.*?(\d+)", html)
    if p_m and t_m:
        paused_val = int(p_m.group(1))
        total_val = int(t_m.group(1))
        up_val = max(0, total_val - paused_val)
        return PrtgStatus(
            up=up_val,
            paused=paused_val,
            total=total_val,
        )

    def _extract_count(label: str) -> int:
        pattern = rf">\s*(\d+)\s*<[^>]*>(?:[^<]*\b{re.escape(label)}\b)"
        alt_pattern = rf"\b{re.escape(label)}\b[^>]*>\s*(\d+)\s*<"
        m = re.search(pattern, html, re.IGNORECASE) or re.search(alt_pattern, html, re.IGNORECASE)
        return int(m.group(1)) if m else 0

    up_val = _extract_count("Up")
    paused_val = _extract_count("Paused")
    down_val = _extract_count("Down")
    down_ack_val = _extract_count("Down (Acknowledged)")
    warn_val = _extract_count("Warning")
    unusual_val = _extract_count("Unusual")
    unknown_val = _extract_count("Unknown")

    total_val = up_val + paused_val + down_val + down_ack_val + warn_val + unusual_val + unknown_val

    return PrtgStatus(
        up=up_val,
        paused=paused_val,
        down=down_val,
        down_ack=down_ack_val,
        warning=warn_val,
        unusual=unusual_val,
        unknown=unknown_val,
        total=total_val if total_val > 0 else (up_val + paused_val),
    )
