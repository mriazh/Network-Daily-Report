"""RouterOS Telkom Indibiz traffic collector and local SQLite store."""

from __future__ import annotations

import logging
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import routeros_api

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path(__file__).resolve().parents[3] / "output" / "raw" / "indibiz" / "traffic.db"


@dataclass
class IndibizSample:
    timestamp: str
    epoch: float
    rx_bytes: int
    tx_bytes: int
    rx_bps: float
    tx_bps: float
    uptime: str
    status: str


class RouterOSClient:
    """Wrapper around routeros_api.RouterOsApiPool for interface querying."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        port: int = 5336,
        timeout: int = 30,
        plaintext_login: bool = True,
    ) -> None:
        self.host = host
        self.username = username
        self.password = password
        self.port = port
        self.timeout = timeout
        self.plaintext_login = plaintext_login
        self._pool: routeros_api.RouterOsApiPool | None = None

    def _get_api(self):
        if self._pool is None:
            self._pool = routeros_api.RouterOsApiPool(
                host=self.host,
                username=self.username,
                password=self.password,
                port=self.port,
                plaintext_login=self.plaintext_login,
            )
            self._pool.set_timeout(self.timeout)
        return self._pool.get_api()

    def query_interface(self, interface_name: str) -> dict:
        api = self._get_api()
        iface_resource = api.get_resource("/interface")
        items = iface_resource.get(name=interface_name)
        if not items:
            raise ValueError(f"Interface '{interface_name}' not found on {self.host}")
        item = items[0]
        rx_bytes = int(item.get("rx-byte", 0))
        tx_bytes = int(item.get("tx-byte", 0))
        running_raw = str(item.get("running", "")).lower()
        status = "UP" if running_raw == "true" else "DOWN"

        uptime = ""
        try:
            sys_resource = api.get_resource("/system/resource")
            sys_items = sys_resource.get()
            if sys_items:
                uptime = str(sys_items[0].get("uptime", ""))
        except Exception as exc:
            logger.warning("Failed reading RouterOS /system/resource: %s", exc)

        return {
            "name": interface_name,
            "rx_bytes": rx_bytes,
            "tx_bytes": tx_bytes,
            "uptime": uptime,
            "status": status,
        }

    def close(self) -> None:
        if self._pool is not None:
            try:
                self._pool.disconnect()
            except Exception:
                pass
            self._pool = None


class IndibizStore:
    """Local SQLite store for Indibiz cumulative traffic samples."""

    def __init__(self, db_path: Path | str | None = None) -> None:
        self.db_path = Path(db_path) if db_path is not None else DEFAULT_DB_PATH

    def initialize(self) -> None:
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS traffic_samples (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    epoch REAL NOT NULL,
                    rx_bytes INTEGER NOT NULL,
                    tx_bytes INTEGER NOT NULL,
                    rx_bps REAL NOT NULL,
                    tx_bps REAL NOT NULL,
                    uptime TEXT,
                    status TEXT NOT NULL
                )
                """
            )
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_traffic_samples_epoch ON traffic_samples(epoch)"
            )
            conn.commit()

    def record(self, sample: IndibizSample) -> None:
        self.initialize()
        with sqlite3.connect(self.db_path) as conn:
            cur = conn.cursor()
            cur.execute(
                """
                INSERT INTO traffic_samples (
                    timestamp, epoch, rx_bytes, tx_bytes, rx_bps, tx_bps, uptime, status
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample.timestamp,
                    sample.epoch,
                    sample.rx_bytes,
                    sample.tx_bytes,
                    sample.rx_bps,
                    sample.tx_bps,
                    sample.uptime,
                    sample.status,
                ),
            )
            conn.commit()

    def daily_max(self, date_iso: str) -> tuple[float | None, float | None]:
        """Returns (max_rx_bps, max_tx_bps) for the given YYYY-MM-DD day."""
        if not self.db_path.exists():
            return None, None

        dt = datetime.strptime(date_iso, "%Y-%m-%d")
        local_tz = datetime.now().astimezone().tzinfo
        start_dt = datetime.combine(dt.date(), dtime.min).replace(tzinfo=local_tz)
        end_dt = start_dt + timedelta(days=1)
        start_epoch = start_dt.timestamp()
        end_epoch = end_dt.timestamp()

        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT MAX(rx_bps), MAX(tx_bps)
                    FROM traffic_samples
                    WHERE epoch >= ? AND epoch < ?
                    """,
                    (start_epoch, end_epoch),
                )
                row = cur.fetchone()
                if not row or (row[0] is None and row[1] is None):
                    return None, None
                max_rx = float(row[0]) if row[0] is not None else None
                max_tx = float(row[1]) if row[1] is not None else None
                return max_rx, max_tx
        except Exception as exc:
            logger.warning("Failed querying daily max from %s: %s", self.db_path, exc)
            return None, None

    def latest_sample(self) -> IndibizSample | None:
        if not self.db_path.exists():
            return None
        try:
            with sqlite3.connect(self.db_path) as conn:
                cur = conn.cursor()
                cur.execute(
                    """
                    SELECT timestamp, epoch, rx_bytes, tx_bytes, rx_bps, tx_bps, uptime, status
                    FROM traffic_samples
                    ORDER BY id DESC
                    LIMIT 1
                    """
                )
                row = cur.fetchone()
                if not row:
                    return None
                return IndibizSample(
                    timestamp=row[0],
                    epoch=float(row[1]),
                    rx_bytes=int(row[2]),
                    tx_bytes=int(row[3]),
                    rx_bps=float(row[4]),
                    tx_bps=float(row[5]),
                    uptime=row[6] or "",
                    status=row[7],
                )
        except Exception as exc:
            logger.warning("Failed fetching latest sample from %s: %s", self.db_path, exc)
            return None


def compute_rate(
    prev: IndibizSample | None, cur_rx: int, cur_tx: int, cur_epoch: float
) -> tuple[float, float]:
    """Calculates bits per second from successive byte counter samples.

    Returns (0.0, 0.0) when there is no previous sample, non-positive delta time,
    or counters went backwards (device reboot or wrap).
    """
    if prev is None:
        return 0.0, 0.0

    delta_time = cur_epoch - prev.epoch
    if delta_time <= 0:
        return 0.0, 0.0

    delta_rx = cur_rx - prev.rx_bytes
    delta_tx = cur_tx - prev.tx_bytes
    if delta_rx < 0 or delta_tx < 0:
        return 0.0, 0.0

    rx_bps = (delta_rx * 8.0) / delta_time
    tx_bps = (delta_tx * 8.0) / delta_time
    return rx_bps, tx_bps


def sample_once(client: RouterOSClient, store: IndibizStore, interface_name: str) -> IndibizSample:
    """Executes a single poll cycle: queries interface, calculates rate, records sample."""
    store.initialize()
    prev = store.latest_sample()
    data = client.query_interface(interface_name)

    now = datetime.now().astimezone()
    cur_epoch = now.timestamp()
    cur_iso = now.isoformat()
    cur_rx = data["rx_bytes"]
    cur_tx = data["tx_bytes"]

    rx_bps, tx_bps = compute_rate(prev, cur_rx, cur_tx, cur_epoch)
    sample = IndibizSample(
        timestamp=cur_iso,
        epoch=cur_epoch,
        rx_bytes=cur_rx,
        tx_bytes=cur_tx,
        rx_bps=rx_bps,
        tx_bps=tx_bps,
        uptime=data.get("uptime", ""),
        status=data.get("status", "UP"),
    )
    store.record(sample)
    return sample


def run_sampler(
    client: RouterOSClient,
    store: IndibizStore,
    interface_name: str,
    interval_seconds: int = 30,
    iterations: int | None = None,
) -> None:
    """Loops sampling until interrupted or iterations exhausted."""
    count = 0
    try:
        while iterations is None or count < iterations:
            sample = sample_once(client, store, interface_name)
            count += 1
            logger.info(
                "Sample recorded [%d]: %s (in=%.2f bps, out=%.2f bps, status=%s)",
                count,
                sample.timestamp,
                sample.rx_bps,
                sample.tx_bps,
                sample.status,
            )
            if iterations is not None and count >= iterations:
                break
            time.sleep(interval_seconds)
    except KeyboardInterrupt:
        logger.info("Sampler interrupted by user.")
    finally:
        client.close()


def daily_max_from_store(store_path: Path, date_iso: str) -> tuple[float | None, float | None]:
    """Module-level convenience function for querying daily max (in bps)."""
    store = IndibizStore(store_path)
    return store.daily_max(date_iso)
