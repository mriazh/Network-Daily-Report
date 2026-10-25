"""Nexthop Ping Collector."""

from __future__ import annotations

import json
import logging
import platform
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


def _get_default_targets() -> list[tuple[str, str]]:
    cfg_file = Path(__file__).resolve().parents[3] / "config" / "circuits.json"
    if not cfg_file.exists():
        cfg_file = Path(__file__).resolve().parents[3] / "config" / "circuits.example.json"
    if cfg_file.exists():
        try:
            with open(cfg_file, "r", encoding="utf-8") as f:
                data = json.load(f)
                return [tuple(x) for x in data.get("nexthop", [])]
        except Exception:
            pass
    return [
        ("Private Cloud Gateway", "10.10.1.1"),
        ("VPN Hub Gateway", "10.10.1.2"),
        ("Upstream Provider Router", "10.10.2.1"),
        ("Cellular IoT Gateway", "10.10.2.2"),
        ("Metro Backbone Core", "10.10.3.1"),
        ("Partner B2B Gateway", "10.10.3.2"),
        ("Cloud Transit Hub", "10.10.4.1"),
    ]


DEFAULT_TARGETS = _get_default_targets()


@dataclass
class PingResult:
    name: str
    ip: str
    is_up: bool
    avg_latency_ms: float | None
    packet_loss_pct: float
    status: str  # "Normal" / "Monitoring" / "Down"
    total_sent: int = 0
    packets_received: int = 0


def ping_host(name: str, ip: str, count: int = 4, timeout_sec: int = 3) -> PingResult:
    is_win = platform.system().lower() == "windows"
    cmd = [
        "ping",
        "-n" if is_win else "-c",
        str(count),
        "-w" if is_win else "-W",
        str(timeout_sec * (1000 if is_win else 1)),
        ip,
    ]

    try:
        proc = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=count * timeout_sec + 5,
        )
        output = proc.stdout
    except Exception as exc:
        logger.warning("Ping failed for %s (%s): %s", name, ip, exc)
        return PingResult(
            name=name,
            ip=ip,
            is_up=False,
            avg_latency_ms=None,
            packet_loss_pct=100.0,
            status="Down",
            total_sent=count,
            packets_received=0,
        )

    # Parse loss
    loss_match = re.search(r"(\d+(?:\.\d+)?)%\s*(?:packet\s*)?loss", output, re.IGNORECASE)
    loss = float(loss_match.group(1)) if loss_match else (0.0 if proc.returncode == 0 else 100.0)

    packets_received = count - round(count * loss / 100.0)

    # Parse avg latency. The group is non-greedy so "rtt min/avg/max/mdev = 20.0/23.5/..."
    # yields the avg (23.5) rather than the trailing mdev.
    avg_match = re.search(
        r"(?:Average|rtt\s*min/avg/max/mdev.*=.*?)/\s*(\d+(?:\.\d+)?)", output, re.IGNORECASE
    )
    avg_ms = float(avg_match.group(1)) if avg_match else None

    if loss >= 100.0:
        status = "Down"
    elif loss > 0.0:
        status = "Monitoring"
    else:
        status = "Normal"

    return PingResult(
        name=name,
        ip=ip,
        is_up=(loss < 100.0),
        avg_latency_ms=avg_ms,
        packet_loss_pct=loss,
        status=status,
        total_sent=count,
        packets_received=packets_received,
    )


def collect_nexthop_ping(targets: list[tuple[str, str]] | None = None) -> list[PingResult]:
    target_list = targets or DEFAULT_TARGETS
    results = []
    for name, ip in target_list:
        results.append(ping_host(name, ip))
    return results
