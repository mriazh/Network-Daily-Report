"""WhatsApp Broadcast Message Formatter."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _load_default_pic() -> list[str]:
    cfg = Path(__file__).resolve().parents[3] / "config" / "circuits.json"
    if cfg.exists():
        try:
            with open(cfg, "r", encoding="utf-8") as f:
                return json.load(f).get("pic_names", [])
        except Exception:
            pass
    return [
        "Staff Engineer 1",
        "Staff Engineer 2",
        "Staff Engineer 3",
        "Staff Engineer 4",
        "Staff Engineer 5",
    ]


def format_whatsapp_report(payload: dict[str, Any]) -> str:
    date_str = payload.get("date_str", "01 Januari 2026")
    pic_list = payload.get("pic_list") or _load_default_pic()
    iot = payload.get("iot", {})
    mrtg = payload.get("mrtg", {})
    ping = payload.get("ping", [])
    prtg = payload.get("prtg", {})
    cpu = payload.get("cpu", [])
    mem = payload.get("memory", [])
    uptime = payload.get("uptime", {})
    ap = payload.get("ap", {})
    orbit = payload.get("orbit", [])
    highlights = payload.get("highlights", {})

    lines: list[str] = [
        "*MONITORING NETWORK INFRASTRUCTURE*",
        f"📅 {date_str}",
        "",
        "*PIC Network Support:*",
    ]
    for idx, pic in enumerate(pic_list, 1):
        lines.append(f"{idx}. {pic}")

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "*A. 🌐 Telkomsel IoT M2M*",
            "",
            "1. Service Overview",
            f"  a. Status → ✅ {iot.get('status', 'Normal')}",
            f"  b. Total Usage → {iot.get('total_usage_gb', 0.0):.2f} GB",
            f"  c. Active Session → {iot.get('active_sessions', 0)} Session",
            "",
            "2. 🔝 Top Daily Usage",
        ]
    )
    for idx, (site, val) in enumerate(iot.get("top_daily", [])[:5], ord("a")):
        lines.append(f"  {chr(idx)}. {site} → {val:.2f} GB")

    lines.extend(
        [
            "",
            "3. 🔝 Top Periodic Usage",
        ]
    )
    for idx, (site, val) in enumerate(iot.get("top_periodic", [])[:5], ord("a")):
        lines.append(f"  {chr(idx)}. {site} → {val:.2f} GB")

    mrtg_err = payload.get("mrtg_error")
    mrtg_date = payload.get("mrtg_source_date")
    mrtg_date_label = f" (Data: {mrtg_date})" if mrtg_date else ""
    header_extra = " (⚠️ Gagal scrape)" if mrtg_err else mrtg_date_label

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"*B. 📊 Usage by MRTG*{header_extra}",
        ]
    )
    # Support both split keys (VPN IP (JTI), VPN IP (OSA)) and legacy single key (VPN IP)
    vpn_jti = mrtg.get("VPN IP (JTI)", [])
    vpn_osa = mrtg.get("VPN IP (OSA)", [])
    vpn_legacy = mrtg.get("VPN IP", [])
    # If split keys are empty but legacy exists, split legacy by name
    if not vpn_jti and not vpn_osa and vpn_legacy:
        for link_item in vpn_legacy:
            name = link_item.get("name", "")
            if "PARTNER-DC" in name.upper() or "STL" in name.upper() or "JTI" in name.upper():
                vpn_jti.append(link_item)
            else:
                vpn_osa.append(link_item)

    for group_num, group_name, links in [
        (1, "ASTINET", mrtg.get("ASTINET", [])),
        (2, "Metro-E", mrtg.get("Metro-E", [])),
        (3, "VPN IP (JTI)", vpn_jti),
        (4, "VPN IP (OSA)", vpn_osa),
        (5, "Telkom Indibiz", mrtg.get("Telkom Indibiz", [])),
    ]:
        lines.extend(["", f"{group_num}. {group_name}"])
        if not links and mrtg_err:
            lines.append("  ⚠️ Gagal scrape")
            continue
        for idx, link_item in enumerate(links, ord("a")):
            st = link_item.get("status")
            if st == "High Traffic":
                status_icon = "⚠️"
                extra = f" ({link_item.get('remarks', '')})"
            elif st == "No Data":
                status_icon = "⚠️"
                extra = ""
            else:
                status_icon = "✅"
                extra = ""
            lines.append(
                f"  {chr(idx)}. {link_item.get('name')} (SID: {link_item.get('sid')}) ({link_item.get('capacity_str')}) → {status_icon} {st}{extra}"
            )

    airwave_err = payload.get("airwave_error") or payload.get("uptime", {}).get("error")
    airwave_date = payload.get("airwave_source_date") or payload.get("uptime", {}).get(
        "source_date"
    )
    date_label = f" (Data: {airwave_date})" if airwave_date else ""

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            f"*C. 🔀 Switch & Core Infrastructure*{date_label}",
            "",
            "1. 📡 Nexthop (Ping)",
        ]
    )
    for idx, p in enumerate(ping, ord("a")):
        name = p.get("name")
        ip = p.get("ip")
        status = p.get("status")
        received = p.get("packets_received", 0)
        sent = p.get("total_sent", 4)
        loss_pct = p.get("loss_pct") or p.get("packet_loss_pct") or 0.0
        avg_ms = p.get("avg_latency_ms")
        if status == "Normal":
            avg_str = f", Avg: {avg_ms:.1f} ms" if avg_ms is not None else ""
            lines.append(
                f"  {chr(idx)}. {name} → ✅ Normal (IP {ip}) (Loss: {loss_pct:.0f}% ({received}/{sent} Reply){avg_str})"
            )
        elif status == "Monitoring" or status == "Warning":
            avg_str = f", Avg: {avg_ms:.1f} ms" if avg_ms is not None else ""
            lines.append(
                f"  {chr(idx)}. {name} → ⚠️ Monitoring (IP {ip}) (Loss: {loss_pct:.0f}% ({received}/{sent} Reply){avg_str})"
            )
        else:  # Down
            lines.append(
                f"  {chr(idx)}. {name} → ⚠️ Down (IP {ip}) (Loss: 100% (0/{sent} Reply), Request Timeout)"
            )

    lines.extend(
        [
            "",
            "2. 🖥️ PRTG Sensors",
            f"  a. 🟢 UP → {prtg.get('up', 0)}",
            f"  b. ⏸️ Paused → {prtg.get('paused', 0)}",
            f"  c. Total → {prtg.get('total', 0)} Sensors",
            "",
            f"3. ⚙️ Top 10 CPU Utilization{date_label}",
        ]
    )
    if airwave_err:
        lines.append("  a. Status → ⚠️ Gagal scrape")
    else:
        filtered_cpu = [item for item in cpu if not str(item[0]).upper().startswith("WLC")]
        for idx, (dev, val) in enumerate(filtered_cpu[:10], ord("a")):
            lines.append(f"  {chr(idx)}. {dev} → {val:.2f}%")

    lines.extend(
        [
            "",
            f"4. 💾 Top 10 Memory Usage{date_label}",
        ]
    )
    if airwave_err:
        lines.append("  a. Status → ⚠️ Gagal scrape")
    else:
        filtered_mem = [item for item in mem if not str(item[0]).upper().startswith("WLC")]
        for idx, (dev, val) in enumerate(filtered_mem[:10], ord("a")):
            lines.append(f"  {chr(idx)}. {dev} → {val:.2f}%")

    lines.extend(
        [
            "",
            "5. 🎛️ Huawei WAC / iMaster NCE (Live)",
        ]
    )
    # Unified WAC/NCE section: show WAC controller metrics only (AP/Users are in Section D)
    wac = payload.get("wac")
    if wac and not wac.get("error"):
        cpu_str = f"{wac.get('cpu_pct'):.2f}%" if wac.get("cpu_pct") is not None else "-"
        mem_str = f"{wac.get('mem_pct'):.2f}%" if wac.get("mem_pct") is not None else "-"
        lines.append(f"  a. Device Name → {wac.get('device_name', '-')}")
        lines.append(f"  b. CPU Utilization → {cpu_str}")
        lines.append(f"  c. Memory Usage → {mem_str}")
        lines.append(f"  d. Temperature → {wac.get('temperature', '-')}")
        lines.append(f"  e. Running Time → {wac.get('running_time', '-')}")
        lines.append(f"  f. Software Version → {wac.get('software_version', '-')}")
    else:
        err = wac.get("error") if wac else None
        err_msg = f"⚠️ Gagal scrape ({err})" if err else "⚠️ Data WAC tidak tersedia"
        lines.append(f"  a. Status → {err_msg}")

    lines.extend(
        [
            "",
            f"6. ⏱️ Avg Uptime by Device{date_label}",
        ]
    )
    if airwave_err:
        lines.append("  a. Status → ⚠️ Gagal scrape")
    else:
        lines.append(f"  a. Device Uptime → {uptime.get('device', 98.64)}%")
        lines.append(f"  b. SNMP Comm Uptime → {uptime.get('snmp', 98.64)}%")
        lines.append(f"  c. ICMP Comm Uptime → {uptime.get('icmp', 99.32)}%")
        lines.append(f"  d. HTTPS Comm Uptime → {uptime.get('https', '-')}")

    lines.extend(
        [
            "",
            "7. 📝 Analisa Utilisasi",
            f"  a. CPU: {payload.get('analysis_cpu', 'Beban normal.')}",
            f"  b. Memori: {payload.get('analysis_mem', 'Alokasi stabil.')}",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "*D. 📶 AP Huawei (WAC)*",
            "",
            "1. Status Connectivity",
            f"  a. Normal → 🟢 {ap.get('normal', 454)}",
            f"  b. Fault → 🔴 {ap.get('fault', 0)}",
            f"  c. Connected Users → 👥 {ap.get('users', 0)}",
            "",
            "2. Status Fisik AP",
            f"  a. Indoor → ✅ {ap.get('indoor_ok', 328)} OK",
            f"  b. Outdoor → ✅ {ap.get('outdoor_ok', 115)} OK"
            + (
                f", ⚠️ {ap.get('outdoor_fault', 0)} Fault ({', '.join(ap.get('fault_names', []))})"
                if ap.get("outdoor_fault", 0) > 0
                else ""
            ),
            "",
            "3. 📊 User Distribution by SSID",
        ]
    )
    for idx, (ssid_name, cnt) in enumerate(ap.get("ssid_distribution", {}).items(), ord("a")):
        lines.append(f"  {chr(idx)}. {ssid_name} → {cnt}")

    if orbit:
        lines.extend(
            [
                "",
                "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
                "*E. 📱 Modem Orbit*",
            ]
        )
        for idx, m in enumerate(orbit, 1):
            pkgs = m.get("packages", [])
            active = [p for p in pkgs if p] if pkgs else []
            if not active:
                p_text = "Kuota: - (No Active Package)"
            else:
                p = active[0]
                rem = p.get("remaining_gb")
                tot = p.get("total_gb")
                exp = p.get("expiry_str")
                if exp:
                    p_text = f"Kuota: {rem} GB / {tot} GB (Exp: {exp})"
                else:
                    p_text = f"Kuota: {rem} GB / {tot} GB"
            lines.append(f"{idx}. {m.get('location')}: {p_text}")

    lines.extend(
        [
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "*F. ⚠️ Highlights & Incident Follow-up*",
            "1. High Traffic:",
        ]
    )
    ht_list = highlights.get("high_traffic", [])
    if ht_list:
        for idx, h in enumerate(ht_list, ord("a")):
            lines.append(f"  {chr(idx)}. {h.get('name')} ({h.get('remarks', '')})")
    else:
        lines.append("  a. Seluruh sirkuit terpantau Normal.")

    lines.extend(
        [
            "2. Hardware Fault:",
            f"  a. {highlights.get('hardware_fault', 'Tidak ada hardware fault.')}",
            "3. Problem Open:",
            "  a. -",
            "",
            "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━",
            "🏁 Sekian laporan harian Network Support. Terima kasih.",
        ]
    )

    return "\n".join(lines)
