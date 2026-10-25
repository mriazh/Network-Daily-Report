#!/usr/bin/env python3
"""Fill the daily Excel template from the WhatsApp broadcast text files.

Input   : C:/Users/adima/Downloads/Daily-WAG/{day}.txt   (day 1..31)
Template: config/Template-Daily-Report.xlsx (dibaca saja, kecuali --in-place)
Output  : config/Daily-Report-2026-10.xlsx

Run    : python scripts/fill_from_wag.py
         python scripts/fill_from_wag.py --days 1-5 --check
         python scripts/fill_from_wag.py --in-place

Tabel lokasi row dicari dari isi sheet (section / subsection / header), jadi filler
tetap aman bila generate_template.py menggeser baris.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

from openpyxl import load_workbook
from openpyxl.cell.cell import MergedCell

ROOT = Path(__file__).resolve().parents[1]
TEMPLATE = ROOT / "config" / "Template-Daily-Report.xlsx"
WAG_DIR = Path(r"C:\Users\adima\Downloads\Daily-WAG")
DEFAULT_OUTPUT = ROOT / "output" / "reports" / "2026-10" / "Daily-Report-2026-10.xlsx"

HEADER_KEYS = {"No", "Rank"}
ROW_RE = re.compile(r"^\s*(\d+)\.\s*(.*)$")
SECTION_RE = re.compile(r"^([A-F])\.\s")


class Table:
    __slots__ = ("header", "rows", "section", "subsection")

    def __init__(self, section, subsection, header, rows):
        self.section = section
        self.subsection = subsection
        self.header = header
        self.rows = rows

    def __repr__(self) -> str:
        return f"<Table {self.subsection or self.section} rows={len(self.rows)}>"


def _blank(ws, row: int) -> bool:
    return all(ws.cell(row=row, column=col).value is None for col in range(1, 9))


def _is_total(ws, row: int) -> bool:
    side = ws.cell(row=row, column=1).border.top
    return side is not None and side.style == "double"


def _is_note(ws, row: int) -> bool:
    """Baris keterangan: teks panjang di kolom B tanpa nomor (A kosong)."""
    label = ws.cell(row=row, column=2).value
    return ws.cell(row=row, column=1).value is None and isinstance(label, str) and len(label) > 25


def scan_tables(ws) -> dict[str, Table]:
    """subsection title -> Table. Tabel Modem Orbit (tanpa subsection) jadi 'E!'.

    Baris data = setelah header, selama kolom A berisi angka atau kosong (baris
    lanjutan modem) dan bukan baris total (border double).
    """
    tables: dict[str, Table] = {}
    section = subsection = None
    row = 1
    while row <= ws.max_row:
        value = ws.cell(row=row, column=1).value
        if isinstance(value, str):
            match = SECTION_RE.match(value)
            if match:
                section, subsection = match.group(1), None
            elif ROW_RE.match(value):
                subsection = value.strip()
            elif value.strip() in HEADER_KEYS:
                rows: list[int] = []
                cursor = row + 1
                while cursor <= ws.max_row:
                    head = ws.cell(row=cursor, column=1).value
                    if isinstance(head, str) or _blank(ws, cursor) or _is_note(ws, cursor):
                        break
                    if not _is_total(ws, cursor):
                        rows.append(cursor)
                    cursor += 1
                tables[subsection or f"{section}!"] = Table(section, subsection, row, rows)
                row = cursor
                continue
        row += 1
    return tables


def norm(text: object) -> str:
    return re.sub(r"[^A-Z0-9]", "", str(text or "").upper())


def number(text: str | None):
    if not text:
        return None
    match = re.search(r"-?\d+(?:[.,]\d+)?", str(text).replace(" ", " "))
    if not match:
        return None
    value = float(match.group(0).replace(",", "."))
    return int(value) if value.is_integer() else value


def lookup(index: dict, key: str, suffix: bool = False):
    """Cocokkan label template ke label WAG: exact, lalu prefiks terpanjang."""
    if key in index:
        return index[key]
    candidates = [value for name, value in index.items()
                  if name.startswith(key) or key.startswith(name)
                  or (suffix and (name.endswith(key) or key.endswith(name)))]
    return max(candidates, key=lambda item: len(norm(item.get("name", "")))) if candidates else None


def to_gb(text: str):
    """'2.80 GB' -> 2.8 ; '968.08 MB' -> 0.945 (MB biner, konsisten dengan Excel)."""
    value = number(text)
    if value is None:
        return None
    if re.search(r"MB", text, re.IGNORECASE):
        return round(value / 1024, 4)
    return value


def split_sections(text: str) -> dict[str, list[str]]:
    markers = [
        ("iot", r"A\.\s*Telkomsel IoT\s*M2M", False),
        ("astinet", r"B\.\s*ASTINET", False),
        ("metro", r"C\.\s*Metro", False),
        ("vpn", r"D\.\s*VPN\s*IP", False),
        ("indibiz", r"^\s*Telkom Indibiz", False),
        ("nexthop", r"E\.\s*Nexthop", False),
        ("prtg", r"F\.\s*PRTG", False),
        ("ap", r"G\.\s*AP Huawei", False),
        ("ssid", r"H\.\s*User SSID", False),
        ("cpu", r"CPU Utilization", False),
        ("memory", r"Memory Usage", False),
        ("uptime", r"Avg Uptime by Device", False),
        ("modem", r"(?:E\.\s*Modem|1\.\s*Modem\s+\w+)", True),
        ("highlights", r"Highlights\s*:", False),
    ]
    lines = text.splitlines()
    found: list[tuple[str, int, bool]] = []
    for name, pattern, keep in markers:
        for index, line in enumerate(lines):
            if re.search(pattern, line, re.IGNORECASE):
                found.append((name, index, keep))
                break
    found.sort(key=lambda item: item[1])
    sections: dict[str, list[str]] = {}
    for position, (name, start, keep) in enumerate(found):
        end = found[position + 1][1] if position + 1 < len(found) else len(lines)
        sections[name] = lines[start:end] if keep else lines[start + 1:end]
    return sections
def parse_ranking(lines) -> list[tuple[str, object]]:
    out = []
    for line in lines:
        if "→" not in line:
            break
        match = ROW_RE.match(line)
        if not match:
            continue
        site, usage = match.group(2).split("→", 1)
        out.append((site.strip(), to_gb(usage)))
    return out


def parse_iot(lines, date_line: str) -> dict:
    data: dict = {"status": None, "usage_gb": None, "sessions": None,
                  "top_daily": [], "top_periodic": [], "period": None}
    for index, line in enumerate(lines):
        if re.match(r"^\s*Status\s*:", line, re.IGNORECASE):
            tail = line.split(":", 1)[1].strip(" .")
            data["status"] = "Normal" if re.search(r"normal", line, re.IGNORECASE) else (tail or None)
        elif "Top Daily" in line:
            data["top_daily"] = parse_ranking(lines[index + 1:])
        elif "Top Periodic" in line or "Top Utility" in line:
            data["top_periodic"] = parse_ranking(lines[index + 1:])
            match = re.search(r"\((.*?)\)", line)
            if match:
                data["period"] = re.sub(r"\s*-\s*", "-", re.sub(r"\s+", " ", match.group(1)).strip())
        elif re.search(r"Usage\s*:", line, re.IGNORECASE):
            data["usage_gb"] = to_gb(line)
        elif re.search(r"User\s*:", line, re.IGNORECASE):
            data["sessions"] = number(line)
    date_match = re.search(r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b", date_line)
    data["date"] = date_match.group(0) if date_match else date_line.strip()
    return data


def parse_links(lines, group: str) -> list[dict]:
    out = []
    for line in lines:
        if "Normal" not in line and "High Traffic" not in line:
            continue
        name = re.split(r"\(|:|→", line.strip(" *•"))[0].strip()
        match = re.search(r"Persentase\s*([\d.]+)\s*%", line, re.IGNORECASE)
        out.append({
            "group": group,
            "name": name,
            "key": norm(f"{group} {name}"),
            "status": "High Traffic" if "High Traffic" in line else "Normal",
            "percent": float(match.group(1)) if match else None,
        })
    return out


def parse_mrtg(sections) -> list[dict]:
    links = parse_links(sections.get("astinet", []), "ASTINET")
    links += parse_links(sections.get("metro", []), "Metro-E")
    links += parse_links(sections.get("vpn", []), "VPN IP")
    links += parse_links(sections.get("indibiz", []), "Telkom Indibiz")
    return links


def parse_nexthop(lines) -> list[dict]:
    out = []
    for line in lines:
        if "Normal" not in line and "IP" not in line:
            continue
        name = re.split(r":|→", line.strip(" *•"))[0].strip()
        match = re.search(r"IP\s*([\d.]+)", line)
        out.append({"key": norm(name), "ip": match.group(1) if match else None,
                    "status": "Normal" if "Normal" in line else "Fault"})
    return out


def parse_counts(lines, labels) -> dict:
    found = {}
    for line in lines:
        match = re.match(r"^\s*[•*o]\s*([^:]+?)\s*:\s*(.+)$", line)
        if match:
            found[norm(match.group(1))] = number(match.group(2))
    return {key: found[key] for key in map(norm, labels) if key in found}


def parse_top_percent(lines) -> list[tuple[str, float]]:
    out = []
    for line in lines:
        text = re.sub(r"^\s*[•*o]\s*", "", line.strip())
        if "==>" in text:
            name, value = text.rsplit("==>", 1)
        elif ":" in text:
            name, value = text.rsplit(":", 1)
        else:
            continue
        match = re.search(r"([\d.]+)\s*%", value)
        if match:
            out.append((name.strip(), float(match.group(1))))
    return out


def parse_uptime(lines) -> dict:
    text = " ".join(line.strip() for line in lines
                     if not re.match(r"^\s*[=━\-]{3,}\s*$", line))
    text = re.sub(r"[\ue000-\uf8ff]", "", text)  # bullet private-use dari font lama
    out: dict = {"device": None, "snmp": None, "icmp": None, "https": None,
                 "error": None}
    if not re.search(r"Comm Uptime", text, re.IGNORECASE):
        note = re.search(r"Website Airwave.*", text)
        out["error"] = note.group(0).strip() if note else "Avg Uptime not reported"
        return out
    for label, key in (("Device Uptime", "device"), ("SNMP Comm Uptime", "snmp"),
                       ("ICMP Comm Uptime", "icmp")):
        match = re.search(label + r"\s*([\d.]+)\s*%", text, re.IGNORECASE)
        out[key] = float(match.group(1)) if match else None
    match = re.search(r"HTTPS Comm Uptime\s*([\d.]+\s*%|-)", text, re.IGNORECASE)
    out["https"] = match.group(1) if match else None
    return out


def parse_ap(lines) -> dict:
    out = {"normal": None, "fault": None, "users": None, "indoor_ok": None,
           "outdoor_ok": None, "outdoor_fault": None, "fault_ap": None}
    for line in lines:
        match = re.match(r"^\s*[•*]\s*(Normal|Fault|User)\s*:\s*(.+)$", line, re.IGNORECASE)
        if match:
            label = match.group(1).lower()
            out["users" if label == "user" else label] = number(match.group(2))
        for key, count in (("Indoor", "indoor_ok"), ("Outdoor", "outdoor_ok")):
            if key in line:
                match = re.search(key + r"[^\d]*(\d+)\s*OK", line)
                if match:
                    out[count] = int(match.group(1))
        if "Outdoor" in line:
            match = re.search(r"(\d+)\s*Fault", line)
            if match:
                out["outdoor_fault"] = int(match.group(1))
        match = re.match(r"^\s*\*?\s*(AP-[A-Za-z0-9-]+)", line)
        if match:
            out["fault_ap"] = match.group(1)
    return out


def parse_ssid(lines) -> list[tuple[str, int]]:
    out = []
    for line in lines:
        match = re.match(r"^\s*[•*]\s*([^:]+?)\s*:\s*([\d,]+)", line)
        if match:
            out.append((match.group(1).strip(), int(match.group(2).replace(",", ""))))
    return out
def parse_modems(lines) -> list[dict]:
    """Grup baris WAG menjadi modem -> daftar paket (satu dict per baris tabel)."""
    modems: list[dict] = []
    current: dict | None = None
    package: dict | None = None

    def new_package(label=None):
        return {"label": label, "sisa": None, "total": None, "masa": None, "note": None}

    def flush() -> None:
        nonlocal package
        if current is not None and package is not None and package["sisa"] is not None:
            current["packages"].append(package)
        package = None

    for raw in lines:
        line = raw.strip().lstrip("•*-–—\uf000-\uf8ff \t")
        if re.match(r"^o\s*\t", line):
            line = line[1:].lstrip()
        if not line:
            continue
        start = ROW_RE.match(line)
        if start and not re.match(r"^o\s", line, re.IGNORECASE):
            flush()
            current = {"name": start.group(2).strip(), "packages": []}
            modems.append(current)
            continue
        if current is None:
            continue
        if re.match(r"^Paket\s+\d+", line, re.IGNORECASE):
            flush()
            package = new_package(line.rstrip(":").strip())
            continue
        match = re.search(r"Jenis Paket\s*:\s*(.+)", line, re.IGNORECASE)
        if match:
            if package is None:
                package = new_package()
            package["label"] = package["label"] or match.group(1).strip()
            continue
        if "Sisa Kuota" in line:
            if package is None:
                package = new_package()
            body = re.sub(r"^.*Sisa\s+Kuota.*?:\s*", "", line, flags=re.IGNORECASE)
            values = re.findall(r"[\d.]+", body)
            package["sisa"] = number(values[0]) if values else None
            package["total"] = number(values[1]) if len(values) > 1 else None
            continue
        match = re.search(r"Kuota\s+(.+?)\s*:\s*(.+)", line, re.IGNORECASE)
        if match:
            flush()
            package = new_package(match.group(1).strip())
            left, _, right = match.group(2).partition("/")
            values = re.findall(r"[\d.]+", left)
            package["sisa"] = number(values[0]) if values else None
            package["total"] = number(right)
            masa = re.search(r"Berlaku\s*s\.?d\.?\s*(.+?)\s*\)", match.group(2))
            if masa:
                package["masa"] = masa.group(1).strip()
            continue
        match = re.match(r"\s*([A-Za-z][\w ]{2,})\s*:\s*([\d.]+\s*(?:GB|MB)\b.*)$", line)
        if match:
            flush()
            package = new_package(match.group(1).strip())
            values = re.findall(r"[\d.]+", match.group(2))
            package["sisa"] = number(values[0]) if values else None
            package["total"] = number(values[1]) if len(values) > 1 else None
            masa = re.search(r"Berlaku\s*s\.?d\.?\s*(.+?)\s*\)", match.group(2))
            if masa:
                package["masa"] = masa.group(1).strip()
            continue
        match = re.search(r"Masa Berlaku\s*:\s*(.+)", line, re.IGNORECASE)
        if match:
            if package is None:
                package = new_package()
            package["masa"] = re.sub(r"\s+", " ", match.group(1)).strip()
    flush()
    return modems


def modem_status(masa: str | None) -> str:
    text = (masa or "").lower()
    if "expired soon" in text:
        return "Expired Soon"
    if "expired" in text:
        return "Expired"
    return "Active"


def parse_highlights(lines) -> tuple[list[str], list[str]]:
    """(item high traffic, item lain). Baris indentasi menyambung item sebelumnya."""
    traffic: list[str] = []
    others: list[str] = []
    for raw in lines:
        text = raw.strip()
        if re.search(r"Sekian laporan|Salam\s*,|\bNetwork Support\s*$", text):
            break
        if not text or re.search(r"^(━|=)+$", text):
            continue
        bullet = re.sub(r"^[•*o]\s*", "", text).strip()
        if not bullet:
            continue
        if re.match(r"^High\s+Traffic\b", bullet, re.IGNORECASE):
            traffic += [item.strip() for item in bullet.split(":", 1)[1].split(",") if item.strip()]
            continue
        indented = raw[:1].isspace() and not re.match(r"^\s*[•*o]\s", raw)
        if indented and others:
            others[-1] = f"{others[-1]} {bullet}".strip()
        else:
            others.append(bullet)
    return traffic, others


def parse_analisa(lines) -> tuple[str, str]:
    """Pisahkan blok analisa menjadi bagian CPU dan Memory."""
    block: list[str] = []
    started = False
    for line in lines:
        text = line.strip()
        if re.search(r"^(={3,}|━{3,})", text):
            if started:
                break
            continue
        if not started and re.search(r"\bAnalis[ai]s\b|\bBerikut adalah", text, re.IGNORECASE):
            started = True
        if started and text:
            block.append(text)
    joined = " ".join(block)
    clean = re.sub(r"^.*?(?:di atas|berikut ini)\s*:\s*", "", joined, flags=re.IGNORECASE)
    clean = re.sub(r"^\d+\.\s*", "", clean)
    marks = list(re.finditer(r"Analisis\s+(CPU|Memo(?:ri|ry))(?:\s+\w+)?", clean, re.IGNORECASE))
    if not marks:
        return "", ""
    out = {}
    for position, mark in enumerate(marks):
        end = marks[position + 1].start() if position + 1 < len(marks) else len(clean)
        text = re.sub(r"\s+", " ", clean[mark.end():end]).strip(" :·*")
        key = "cpu" if mark.group(1).lower().startswith("cpu") else "memory"
        out.setdefault(key, text)
    return out.get("cpu", ""), out.get("memory", "")


def put(ws, row: int, col: int, value) -> None:
    """Tulis nilai; sel merge aside-leading tidak bisa diisi, jadi dilewati."""
    if value is None or value == "":
        return
    cell = ws.cell(row=row, column=col)
    if not isinstance(cell, MergedCell):
        cell.value = value
OVERVIEW = "1. Service Status Overview"
TOP_DAILY = "2. Top Daily Usage (Top 5)"
TOP_PERIODIC = "3. Top Usage by Site - Periodic Monitoring"
NEXTHOP = "1. Nexthop Monitoring (Ping)"
PRTG = "2. PRTG Sensor Monitoring"
CPU = "3. Top 10 CPU Utilization"
MEMORY = "4. Top 10 Memory Usage"
UPTIME = "5. Avg Uptime by Device"
ANALISA = "6. Technical Utilization Analysis"
AP_STATUS = "1. Access Point Status"
AP_PHYSICAL = "2. AP Physical Status"
SSID_TABLE = "3. User Distribution by SSID"
MODEMS = "E!"
HIGH_TRAFFIC = "1. High Traffic"
HARDWARE = "2. Hardware Fault"
PROBLEM = "3. Problem Open & Follow-up"
MRTG_TABLES = (("1. ASTINET", "ASTINET"), ("2. Metro-E", "Metro-E"),
               ("3. VPN IP (OSA)", "VPN IP"), ("4. Telkom Indibiz", "Telkom Indibiz"))


def fill_sheet(ws, day: int, text: str, warnings: list[str]) -> None:
    lines = text.splitlines()
    sections = split_sections(text)
    tables = scan_tables(ws)

    date_match = re.search(r"\b\d{1,2}\s+[A-Za-z]+\s+\d{4}\b", text)
    date_str = date_match.group(0) if date_match else (lines[1].strip() if len(lines) > 1 else "")
    iot = parse_iot(sections.get("iot", []), date_str)
    links = parse_mrtg(sections)
    nexthop = {hop["key"]: hop for hop in parse_nexthop(sections.get("nexthop", []))}
    prtg = parse_counts(sections.get("prtg", []), ("UP", "Paused"))
    cpu = parse_top_percent(sections.get("cpu", []))
    memory = parse_top_percent(sections.get("memory", []))
    uptime = parse_uptime(sections.get("uptime", []))
    ap = parse_ap(sections.get("ap", []))
    ssid_items = parse_ssid(sections.get("ssid", []))
    ssid = {norm(name): value for name, value in ssid_items}
    modems = parse_modems(sections.get("modem", []))
    traffic, others = parse_highlights(sections.get("highlights", []))
    analisa_cpu, analisa_memory = parse_analisa(
        sections.get("memory", []) + sections.get("cpu", []))

    ws["C4"] = iot["date"]

    # A. Telkomsel IoT M2M
    if OVERVIEW in tables:
        rows = tables[OVERVIEW].rows
        # Row 1 (Status Service) keeps Value empty; Status carries the state.
        put(ws, rows[0], 5, "Normal" if iot["status"] == "Normal" else iot["status"])
        put(ws, rows[1], 4, iot["usage_gb"])
        put(ws, rows[2], 4, iot["sessions"])
    for key, period, field in ((TOP_DAILY, iot["date"], "top_daily"),
                               (TOP_PERIODIC, iot["period"], "top_periodic")):
        if key not in tables:
            continue
        for row, (site, usage) in zip(tables[key].rows, iot[field]):
            put(ws, row, 2, site)
            put(ws, row, 4, usage)
            put(ws, row, 5, period)

    # B. Usage by MRTG
    by_key = {link["key"]: link for link in links}
    for key, group in MRTG_TABLES:
        if key not in tables:
            continue
        for row in tables[key].rows:
            link = lookup(by_key, norm(f"{group} {ws.cell(row=row, column=2).value}"))
            if not link:
                continue
            put(ws, row, 5, link["status"])
            if link["percent"] is not None:
                put(ws, row, 6, link["percent"])
                put(ws, row, 7, f"High Traffic {link['percent']}% bandwidth")

    # C. Switch & Core
    if NEXTHOP in tables:
        for row in tables[NEXTHOP].rows:
            hop = nexthop.get(norm(ws.cell(row=row, column=2).value))
            if hop:
                put(ws, row, 4, hop["ip"])
                put(ws, row, 7, hop["status"])
    if PRTG in tables:
        for row in tables[PRTG].rows:
            value = prtg.get(norm(ws.cell(row=row, column=2).value))
            put(ws, row, 4, value)
            if value:
                put(ws, row, 5, "Normal")
    if CPU in tables:
        for row, (device, percent) in zip(tables[CPU].rows, cpu):
            put(ws, row, 2, device)
            put(ws, row, 4, percent)
            put(ws, row, 6, "Normal" if percent < 60.0 else ("Warning" if percent <= 80.0 else "Critical"))
    if MEMORY in tables:
        for row, (device, percent) in zip(tables[MEMORY].rows, memory):
            put(ws, row, 2, device)
            put(ws, row, 4, percent)
            put(ws, row, 6, "Normal" if percent < 70.0 else ("Monitoring" if percent <= 85.0 else "Critical"))
    if UPTIME in tables:
        row = tables[UPTIME].rows[0]
        if uptime["snmp"] is None:
            put(ws, row, 7, uptime["error"] or "Avg Uptime not reported")
            warnings.append(f"hari {day}: {uptime['error']} - Avg Uptime kosong")
        else:
            put(ws, row, 2, "Device Uptime")
            put(ws, row, 4, uptime["snmp"])
            put(ws, row, 5, uptime["icmp"])
            put(ws, row, 6, uptime["https"])
            put(ws, row, 7, f"Device Uptime {uptime['device']}%")
    if ANALISA in tables:
        rows = tables[ANALISA].rows
        put(ws, rows[0], 4, analisa_cpu)
        put(ws, rows[1], 4, analisa_memory)

    # D. Huawei AP (WAC) & SSID
    if AP_STATUS in tables:
        for row, label, value in zip(tables[AP_STATUS].rows,
                                     ("Normal", "Fault", "Connected Users"),
                                     (ap["normal"], ap["fault"], ap["users"])):
            put(ws, row, 5, value)
            if label != "Connected Users":
                put(ws, row, 7, "Fault" if label == "Fault" and value else "Normal")
    if AP_PHYSICAL in tables:
        rows = tables[AP_PHYSICAL].rows
        put(ws, rows[0], 4, ap["indoor_ok"])
        put(ws, rows[0], 5, 0)
        put(ws, rows[1], 4, ap["outdoor_ok"])
        put(ws, rows[1], 5, ap["outdoor_fault"] or 0)
        put(ws, rows[0], 8, "Normal")
        put(ws, rows[1], 8, "Fault" if ap["outdoor_fault"] else "Normal")
        if ap["fault_ap"]:
            put(ws, rows[1], 6, f"AP Fault: {ap['fault_ap']}")
    if SSID_TABLE in tables:
        rows = tables[SSID_TABLE].rows
        known = {norm(ws.cell(row=row, column=2).value) for row in rows}
        for row in rows:
            put(ws, row, 5, ssid.get(norm(ws.cell(row=row, column=2).value)))
        extra = [name for name, _ in ssid_items if norm(name) not in known]
        if extra:
            warnings.append(f"hari {day}: SSID di WAG tidak ada di template: {extra}")

    # E. Modem Orbit
    if MODEMS in tables:
        groups: list[tuple[str, list[int]]] = []
        for row in tables[MODEMS].rows:
            label = ws.cell(row=row, column=2).value
            if label:
                groups.append((str(label), []))
            if groups:
                groups[-1][1].append(row)
        parsed = {norm(m["name"]): m for m in modems}
        for label, rows in groups:
            modem = parsed.get(norm(label))
            if not modem:
                continue
            for row, package in zip(rows, modem["packages"]):
                put(ws, row, 4, package["sisa"])
                put(ws, row, 5, package["total"])
                put(ws, row, 6, package["masa"])
                put(ws, row, 7, modem_status(package["masa"]))
                if package["note"]:
                    put(ws, row, 3, f"{package['label']} ({package['note']})")
            for extra in modem["packages"][len(rows):]:
                warnings.append(
                    f"hari {day}: paket {extra['label']} pada {label} melebihi "
                    f"{len(rows)} baris template, dilewati")
    if day >= 7 and not modems:
        warnings.append(f"hari {day}: blok Modem Orbit tidak ditemukan di WAG")

    # F. Highlights & Incident Follow-up
    high = [link for link in links if link["status"] == "High Traffic"]
    if HIGH_TRAFFIC in tables:
        rows = tables[HIGH_TRAFFIC].rows
        for row, link in zip(rows, high):
            put(ws, row, 2, f"{link['group']} {link['name']}")
            put(ws, row, 4, link["percent"])
            put(ws, row, 5, "Monitor daily bandwidth")
            put(ws, row, 7, "Monitoring")
        if len(high) > len(rows):
            for link in high[len(rows):]:
                warnings.append(
                    f"hari {day}: high traffic '{link['group']} {link['name']}' "
                    f"melebihi {len(rows)} baris template")
    fault_items = [item for item in others if re.search(r"fault", item, re.IGNORECASE)]
    open_items = [item for item in others if item not in fault_items]
    if HARDWARE in tables:
        rows = tables[HARDWARE].rows
        for row, item in zip(rows, fault_items):
            put(ws, row, 2, ap["fault_ap"] or "AP Outdoor")
            put(ws, row, 3, "Outdoor")
            put(ws, row, 4, item.rstrip(". "))
            put(ws, row, 6, "Device check on AP")
            put(ws, row, 8, "Fault")
        for item in fault_items[len(rows):]:
            warnings.append(f"hari {day}: hardware fault '{item}' melebihi {len(rows)} baris")
    if PROBLEM in tables:
        # Template has a single open-problem row; extra WAG bullets become Next Action.
        row = tables[PROBLEM].rows[0]
        if open_items:
            put(ws, row, 2, open_items[0].rstrip(",. "))
            follow_up = " ".join(item.rstrip(",. ") for item in open_items[1:])
            put(ws, row, 5, follow_up or "Monitoring oleh tim terkait")
            put(ws, row, 7, "Open")


def parse_day_range(text: str, limit: int) -> list[int]:
    text = (text or "all").strip()
    if text == "all":
        return list(range(1, min(limit, 31) + 1))
    days: list[int] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" in chunk:
            start, _, end = chunk.partition("-")
            days.extend(range(int(start), int(end) + 1))
        else:
            days.append(int(chunk))
    return [day for day in days if 1 <= day <= min(limit, 31)]


def check_output(path: Path, days: list[int]) -> None:
    workbook = load_workbook(path)
    for day in days:
        sheet = workbook[f"{day:02d}"]
        tables = scan_tables(sheet)
        for required in (OVERVIEW, TOP_DAILY, TOP_PERIODIC, NEXTHOP, PRTG, CPU, MEMORY,
                         UPTIME, ANALISA, AP_STATUS, AP_PHYSICAL, SSID_TABLE, MODEMS,
                         HIGH_TRAFFIC, HARDWARE, PROBLEM):
            if required not in tables or not tables[required].rows:
                raise AssertionError(f"sheet {day:02d}: tabel '{required}' tidak ditemukan")
        if not str(sheet["C4"].value).startswith(f"{day:02d}"):
            raise AssertionError(f"sheet {day:02d}: C4 = {sheet['C4'].value!r}")
        filled = sum(1 for row in sheet.iter_rows() for cell in row if cell.value is not None)
        cpu_value = sheet.cell(row=tables[CPU].rows[0], column=4).value
        usage = sheet.cell(row=tables[OVERVIEW].rows[1], column=4).value
        print(f"    cek {day:02d}    : {filled} sel, CPU1={cpu_value}, usage={usage}")
    workbook.close()


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8", errors="replace")
    parser = argparse.ArgumentParser(description="Isi template Excel harian dari teks broadcast WAG.")
    parser.add_argument("--template", type=Path, default=TEMPLATE)
    parser.add_argument("--wag-dir", type=Path, default=WAG_DIR)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--in-place", action="store_true",
                        help="tulis ke file template (hanya untuk data final)")
    parser.add_argument("--days", default="all", help="'all', '1-11', atau '1,2,5'")
    parser.add_argument("--check", action="store_true", help="verifikasi isi setelah ditulis")
    args = parser.parse_args(argv)

    if not args.template.is_file():
        print(f"ERROR: template tidak ditemukan: {args.template}", file=sys.stderr)
        return 2
    available = sorted(int(path.stem) for path in args.wag_dir.glob("*.txt") if path.stem.isdigit())
    if not available:
        print(f"ERROR: tidak ada file .txt di {args.wag_dir}", file=sys.stderr)
        return 2
    days = parse_day_range(args.days, available[-1])
    if not days:
        print(f"ERROR: rentang hari '{args.days}' di luar data 1-{available[-1]}", file=sys.stderr)
        return 2

    workbook = load_workbook(args.template)
    warnings: list[str] = []
    for day in days:
        source = args.wag_dir / f"{day}.txt"
        if not source.is_file():
            warnings.append(f"hari {day}: {source.name} tidak ada, dilewati")
            continue
        fill_sheet(workbook[f"{day:02d}"], day,
                   source.read_text(encoding="utf-8", errors="replace"), warnings)
    target = args.template if args.in_place else args.output
    target.parent.mkdir(parents=True, exist_ok=True)
    workbook.save(target)
    workbook.close()

    print(f"OK  {target}")
    print(f"    template     : {args.template.name}")
    print(f"    sumber WAG   : {args.wag_dir} (hari {days[0]}-{days[-1]})")
    if args.check:
        check_output(target, days)
    for message in warnings:
        print(f"    WARN  {message}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())