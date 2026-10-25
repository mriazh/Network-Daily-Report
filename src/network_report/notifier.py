"""GOWA WhatsApp Notification Adapter."""

from __future__ import annotations

import logging

import requests

from .config import GowaSettings

logger = logging.getLogger(__name__)


def format_duration(seconds: float | int) -> str:
    """Format duration in seconds to human readable string.

    Args:
        seconds: Duration in seconds

    Returns:
        Formatted duration string: <60s -> '{s}s', <3600s -> '{m}m {s}s', >=3600s -> '{h}h {m}m {s}s'
    """
    total_s = max(0, int(round(seconds)))
    if total_s < 60:
        return f"{total_s}s"
    elif total_s < 3600:
        m = total_s // 60
        s = total_s % 60
        return f"{m}m {s}s"
    else:
        h = total_s // 3600
        m = (total_s % 3600) // 60
        s = total_s % 60
        if m == 0 and s == 0:
            return f"{h}h"
        elif s == 0:
            return f"{h}h {m}m"
        else:
            return f"{h}h {m}m {s}s"


class GowaNotifier:
    def __init__(self, settings: GowaSettings) -> None:
        self.settings = settings

    def send_message(self, message: str) -> bool:
        if not self.settings.enabled:
            logger.info("GOWA notifications disabled; message was not sent.")
            return False
        if not self.settings.base_url or not self.settings.group_jid:
            logger.warning("GOWA enabled but base_url or group_jid is missing.")
            return False

        url = f"{self.settings.base_url.rstrip('/')}/send/message"
        payload = {
            "phone": self.settings.group_jid,
            "message": message,
        }
        headers = {"Content-Type": "application/json"}
        if self.settings.device_id:
            headers["X-Device-Id"] = self.settings.device_id

        try:
            resp = requests.post(
                url,
                json=payload,
                headers=headers,
                timeout=self.settings.timeout_seconds,
                verify=self.settings.tls_verify,
            )
            resp.raise_for_status()
            logger.info("WhatsApp notice sent successfully to %s", self.settings.group_jid)
            return True
        except Exception as exc:
            logger.error("Failed to send WhatsApp notice via GOWA: %s", exc)
            return False

    def send_report(self, message: str, test_mode: bool = False) -> bool:
        """Alias for send_message for semantic clarity."""
        if test_mode:
            message = (
                "*⚠️ [TESTING / VERIFIKASI SYSTEM DAILY REPORT] ⚠️*\n"
                "*Pesan ini adalah uji coba otomatisasi (Harap Abaikan)*\n\n" + message
            )
        return self.send_message(message)

    def send_start(self, target_date_str: str, test_mode: bool = False) -> bool:
        """Send START notification."""
        prefix = "[TEST] " if test_mode else ""
        message = f"{prefix}[Network Daily Report] START | date={target_date_str}"
        return self.send_message(message)

    def send_success(
        self, target_date_str: str, duration_sec: float, test_mode: bool = False
    ) -> bool:
        """Send SUCCESS notification with elapsed duration."""
        prefix = "[TEST] " if test_mode else ""
        message = f"{prefix}[Network Daily Report] SUCCESS | date={target_date_str} | elapsed={format_duration(duration_sec)}"
        return self.send_message(message)

    def send_failed(self, target_date_str: str, error: str, test_mode: bool = False) -> bool:
        """Send FAILED notification with error details."""
        prefix = "[TEST] " if test_mode else ""
        message = f"{prefix}[Network Daily Report] FAILED | date={target_date_str} | error={error}"
        return self.send_message(message)
