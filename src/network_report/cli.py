"""Command-line interface for Network Daily Report Automation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from .collectors.mrtg import evaluate_circuit, load_indibiz_daily_max
from .collectors.nexthop_ping import collect_nexthop_ping
from .config import load_config
from .notifier import GowaNotifier
from .pipeline import run_daily_pipeline

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
logger = logging.getLogger("network_report")


def _load_circuits(cfg) -> dict:
    """Load circuits from config/circuits.json (fallback to example)."""
    circuits_file = cfg.circuits_file
    if not circuits_file.exists():
        circuits_file = Path(__file__).resolve().parents[2] / "config" / "circuits.example.json"
    if circuits_file.exists():
        with open(circuits_file, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def _build_mrtg_payload(circuits: dict, cfg, date_iso: str) -> dict:
    """Evaluate all circuit groups and build MRTG payload."""
    mrtg_payload = {}

    # ASTINET
    astinet_list = []
    for name, sid, cap in circuits.get("astinet", []):
        status = evaluate_circuit(name, sid, cap)
        astinet_list.append(
            {
                "name": name,
                "sid": sid,
                "capacity_str": cap,
                "status": status.status,
                "utilization_pct": status.utilization_pct,
                "capacity_mbps": status.capacity_mbps,
                "max_mbps": status.max_mbps,
                "remarks": status.remarks,
            }
        )
    mrtg_payload["ASTINET"] = astinet_list

    # Metro-E
    metro_list = []
    for name, sid, cap in circuits.get("metro_e", []):
        status = evaluate_circuit(name, sid, cap)
        metro_list.append(
            {
                "name": name,
                "sid": sid,
                "capacity_str": cap,
                "status": status.status,
                "utilization_pct": status.utilization_pct,
                "capacity_mbps": status.capacity_mbps,
                "max_mbps": status.max_mbps,
                "remarks": status.remarks,
            }
        )
    mrtg_payload["Metro-E"] = metro_list

    # VPN IP
    vpn_list = []
    for name, sid, cap in circuits.get("vpn_ip_osa", []):
        status = evaluate_circuit(name, sid, cap)
        vpn_list.append(
            {
                "name": name,
                "sid": sid,
                "capacity_str": cap,
                "status": status.status,
                "utilization_pct": status.utilization_pct,
                "capacity_mbps": status.capacity_mbps,
                "max_mbps": status.max_mbps,
                "remarks": status.remarks,
            }
        )
    mrtg_payload["VPN IP"] = vpn_list

    # Indibiz - query local traffic store
    indibiz_list = []
    indibiz_db = cfg.routeros.db_path
    for name, sid, cap in circuits.get("indibiz", []):
        max_rx, max_tx = load_indibiz_daily_max(indibiz_db, date_iso)
        status = evaluate_circuit(name, sid, cap, max_rx, max_tx)
        indibiz_list.append(
            {
                "name": name,
                "sid": sid,
                "capacity_str": cap,
                "status": status.status,
                "utilization_pct": status.utilization_pct,
                "capacity_mbps": status.capacity_mbps,
                "max_mbps": status.max_mbps,
                "remarks": status.remarks,
            }
        )
    mrtg_payload["Telkom Indibiz"] = indibiz_list

    return mrtg_payload


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Network Daily Report Automation Runner")
    subparsers = parser.add_subparsers(dest="command", help="Command to run")

    # Command: run
    run_parser = subparsers.add_parser("run", help="Run daily automated reporting pipeline")
    run_parser.add_argument(
        "--date",
        default="yesterday",
        help="Target date to report (YYYY-MM-DD, 'today', or 'yesterday')",
    )
    run_parser.add_argument("--send-wa", action="store_true", help="Send WhatsApp notice via GOWA")
    run_parser.add_argument(
        "--test", action="store_true", help="Include test banner in WhatsApp notification"
    )

    # Command: test-ping
    subparsers.add_parser("test-ping", help="Test ICMP ping to all 7 nexthop targets")

    # Command: test-wa
    wa_parser = subparsers.add_parser(
        "test-wa", help="Send test WhatsApp message to verify GOWA bridge"
    )
    wa_parser.add_argument(
        "--message",
        default="[TEST] Network Daily Report Automation connection check.",
        help="Custom text",
    )

    # Command: sample-indibiz
    sampler_parser = subparsers.add_parser(
        "sample-indibiz",
        help="Run RouterOS Indibiz traffic sampler loop or query daily max",
    )
    sampler_parser.add_argument(
        "--once",
        action="store_true",
        help="Collect a single sample and exit",
    )
    sampler_parser.add_argument(
        "--interval",
        type=int,
        default=30,
        help="Sampling interval in seconds (default: 30)",
    )
    sampler_parser.add_argument(
        "--date",
        help="Optional date (YYYY-MM-DD) to print daily max traffic and exit",
    )

    # Command: backfill-cmp
    backfill_parser = subparsers.add_parser(
        "backfill-cmp",
        help="Export CMP Daily usage workbooks for a range of dates",
    )
    backfill_parser.add_argument(
        "--from",
        dest="from_date",
        required=True,
        help="Start date in YYYY-MM-DD format",
    )
    backfill_parser.add_argument(
        "--to",
        dest="to_date",
        required=True,
        help="End date in YYYY-MM-DD format (inclusive)",
    )
    backfill_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate backfill without invoking CMP crawler",
    )

    args = parser.parse_args(argv)
    cfg = load_config()

    if args.command == "sample-indibiz":
        from .collectors.routeros_indibiz import (
            IndibizStore,
            RouterOSClient,
            run_sampler,
            sample_once,
        )

        store = IndibizStore(cfg.routeros.db_path)

        if args.date:
            rx_mbps, tx_mbps = load_indibiz_daily_max(cfg.routeros.db_path, args.date)
            rx_str = f"{rx_mbps:.2f} Mbps" if rx_mbps is not None else "N/A"
            tx_str = f"{tx_mbps:.2f} Mbps" if tx_mbps is not None else "N/A"
            print(
                f"Daily max traffic for {args.date} ({cfg.routeros.interface}): RX={rx_str}, TX={tx_str}"
            )
            return 0

        client = RouterOSClient(
            host=cfg.routeros.host,
            username=cfg.routeros.username,
            password=cfg.routeros.password,
            port=cfg.routeros.port,
            timeout=cfg.routeros.timeout_seconds,
            plaintext_login=cfg.routeros.plaintext_login,
        )

        if args.once:
            print(
                f"Sampling RouterOS interface '{cfg.routeros.interface}' on "
                f"{cfg.routeros.host}:{cfg.routeros.port} once..."
            )
            sample = sample_once(client, store, cfg.routeros.interface)
            client.close()
            print(
                f"Sample recorded: {sample.timestamp} | RX={sample.rx_bps / 1_000_000.0:.2f} Mbps | "
                f"TX={sample.tx_bps / 1_000_000.0:.2f} Mbps | Status={sample.status} | Uptime={sample.uptime}"
            )
            return 0

        interval = (
            args.interval if args.interval is not None else cfg.routeros.sample_interval_seconds
        )
        print(
            f"Starting Indibiz sampler loop on {cfg.routeros.host}:{cfg.routeros.port} "
            f"interface '{cfg.routeros.interface}' (interval={interval}s)..."
        )
        run_sampler(client, store, cfg.routeros.interface, interval_seconds=interval)
        return 0

    if args.command == "test-ping":
        print("Running ping test to 7 nexthop targets...")
        results = collect_nexthop_ping()
        for r in results:
            print(
                f"[{r.status:7s}] {r.name:25s} ({r.ip:15s}) - Loss: {r.packet_loss_pct}% Avg: {r.avg_latency_ms} ms"
            )
        return 0

    elif args.command == "test-wa":
        notifier = GowaNotifier(cfg.gowa)
        ok = notifier.send_message(args.message)
        print(f"GOWA Send Result: {'SUCCESS' if ok else 'FAILED / DISABLED'}")
        return 0 if ok else 1

    elif args.command == "backfill-cmp":
        from datetime import datetime, timedelta
        from .collectors.cmp_crawler import run_cmp_crawler

        try:
            start_date = datetime.strptime(args.from_date, "%Y-%m-%d").date()
            end_date = datetime.strptime(args.to_date, "%Y-%m-%d").date()
        except ValueError as err:
            print(f"Invalid date format: {err}", file=sys.stderr)
            return 1

        if start_date > end_date:
            print(
                f"Invalid date range: --from ({args.from_date}) is after --to ({args.to_date})",
                file=sys.stderr,
            )
            return 1

        curr = start_date
        while curr <= end_date:
            d_str = curr.strftime("%Y-%m-%d")
            d_compact = curr.strftime("%Y%m%d")
            month_str = curr.strftime("%Y-%m")
            dest_dir = cfg.output_root / "raw" / "iot" / d_compact

            if args.dry_run:
                print(f"{d_str}: dry-run -> would export to {dest_dir}")
                curr += timedelta(days=1)
                continue

            try:
                res = run_cmp_crawler(
                    base_url=cfg.cmp.base_url or "https://ep.iotcc.telkomsel.com",
                    username=cfg.cmp.username,
                    password=cfg.cmp.password,
                    target_date=d_str,
                    target_month=month_str,
                    output_dir=cfg.output_root,
                    headless=cfg.cmp.headless,
                    timeout_seconds=cfg.cmp.timeout_seconds,
                    export_timeout_seconds=cfg.cmp.export_timeout_seconds,
                    imap_host=cfg.cmp.imap_host,
                    imap_port=cfg.cmp.imap_port,
                    email=cfg.cmp.email,
                    email_password=cfg.cmp.email_password,
                    otp_timeout_seconds=cfg.cmp.otp_timeout_seconds,
                    otp_poll_interval_seconds=cfg.cmp.otp_poll_interval_seconds,
                    proxy_server=cfg.cmp.proxy_server,
                    warp_proxy_server=cfg.warp.proxy_server,
                    monthly_export_enabled=False,
                )
                if res.daily_file and res.daily_file.exists():
                    print(f"{d_str}: exported -> {res.daily_file}")
                elif res.error:
                    print(f"{d_str}: error -> {res.error}")
                else:
                    print(f"{d_str}: error -> export did not produce a daily file")
            except Exception as exc:
                print(f"{d_str}: error -> {exc}")

            curr += timedelta(days=1)

        return 0

    elif args.command == "run":
        print(f"Executing daily report pipeline for: {args.date}...")
        result = run_daily_pipeline(args.date, args.send_wa, test_mode=args.test)
        print(f"Excel: {result['excel_path']}")
        print(f"WhatsApp report: {result['wag_path']}")
        print(f"WhatsApp sent: {result['wa_sent']}")
        return 0

    else:
        parser.print_help()
        return 0


if __name__ == "__main__":
    sys.exit(main())
