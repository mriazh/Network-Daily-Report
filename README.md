# Network Daily Report Automation System

Automated 24/7 network infrastructure daily reporting and monitoring system designed for Debian production hosts. The system performs native crawling and scraping of network management portals, aggregates metrics, populates monthly Excel workbooks (`Daily-Report-YYYY-MM.xlsx`), and generates formal WhatsApp broadcast reports (`Template-Daily-WAG.md`).

---

## Table of Contents
1. [System Overview & Architecture](#1-system-overview--architecture)
2. [Key Modules & Collectors](#2-key-modules--collectors)
3. [Failsafe & Resilience Engine](#3-failsafe--resilience-engine)
4. [CLI Usage & Commands](#4-cli-usage--commands)
5. [Systemd Deployment & Timers](#5-systemd-deployment--timers)
6. [Testing & CI/CD](#6-testing--cicd)
7. [Zero-Leak Security & CSIRT Compliance](#7-zero-leak-security--csirt-compliance)

---

## 1. System Overview & Architecture

The system operates on a dual-schedule architecture to ensure data accuracy, operational resilience, and peak-hour fidelity:

- **14:00 WIB Peak Hour AP Sampler**: Captures Huawei iMaster NCE Access Point (AP) connection statistics during peak office hours (~2,500–2,600 active users), preventing the under-reporting caused by evening sampling.
- **22:00 WIB Daily Report Pipeline**: Executes comprehensive data extraction across all network pillars, validates connectivity, performs strict OCR parsing for cellular metrics, updates Excel reports, and broadcasts summaries via WhatsApp (GOWA gateway).

```
                              +-------------------------------------+
                              |       network_report.pipeline       |
                              +-------------------------------------+
                                                 |
      +--------------------+---------------------+--------------------+--------------------+
      |                    |                     |                    |                    |
      v                    v                     v                    v                    v
[CMP Crawler]       [Nexthop Ping & PRTG]   [AirWave & Huawei]  [MRTG & Indibiz]    [Orbit Scraper]
Dual Query:         • ICMP Ping (7 IPs)     • AirWave CSVs      • TelkomCare OCR    • MyOrbit Portal
1. Daily export     • PRTG HTTP scraper     • Huawei WAC CSVs   • Indibiz traffic   • Direct scrape
2. Monthly export                                                                     (Phone + IMEI)
      |                    |                     |                    |                    |
      +--------------------+---------------------+--------------------+--------------------+
                                                 |
                                                 v
                                     [Data Models & Analysis]
                                     • Analyzer (CPU & Memory)
                                     • Highlights & Incident Generator
                                                 |
                                                 v
                                     +-----------------------+
                                     |   Writers & Output    |
                                     +-----------------------+
                                     | 1. Excel Workbook     |
                                     | 2. WhatsApp WAG Text  |
                                     | 3. Raw Data Archiver  |
                                     +-----------------------+
```

---

## 2. Key Modules & Collectors

### A. Telkomsel IoT M2M (CMP)
- **Portal**: Cisco Jasper / Control Center CMP.
- **Workflow**: Automated Playwright browser automation executing dual export workflows (Daily usage query and Monthly Month-to-Date query) via Vaadin `v-filterselect` components.
- **Strict OCR Source of Truth**: Total Usage is extracted exclusively from live dashboard screenshots (`dashboard_{YYYYMMDD}*.png`) using PaddleOCR (confidence threshold $\ge 0.80$) with Gemini Vision API fallback. Excel and sidecar text parsing for total usage are strictly prohibited.

### B. Huawei iMaster NCE (AP/WAC)
- **Portal**: Huawei iMaster NCE-AC.
- **Data Extracted**: `apInfo.csv`, `ssidInfo.csv`, and `wac_status.json` (CPU, memory, temperature, running time, software version, serial number, and active AP count).
- **Dynamic Header Routing**: Automatically detects whether a genuine 14:00 WIB peak snapshot exists (`snapshot_1400.json`), annotating Section D with `(Data: 14.00 WIB)` or falling back to live collection.

### C. Switch & Core Infrastructure (AirWave, PRTG, & Ping)
- **AirWave NMS**: Exports historical device uptime, CPU utilization, and memory utilization CSV reports. Non-relevant legacy controllers (such as Aruba WLC) are automatically filtered out.
- **PRTG Sensors**: HTTP scrapers monitor UP and Paused sensor states.
- **Nexthop Ping**: ICMP ping checks across 7 critical core destinations.

### D. MRTG TelkomCare & Indibiz
- **Portal**: TelkomCare Netcare (`https://telkomcare.example.internal`).
- **Circuits Covered**: Astinet CORP, Metro-E, VPN IP (JTI / OSA), and Telkom Indibiz.
- **Threshold Evaluation**: Circuits are flagged as `⚠️ High Traffic` only when bandwidth utilization strictly exceeds 100.0% ($\text{Utilisation} > 100.0\%$).

### E. Modem Orbit
- **Source**: Cached MyOrbit portal telemetry (`modems.json`) covering 12 enterprise modems (MSISDN, IMEI, remaining quota, total quota, and package expiry).

---

## 3. Failsafe & Resilience Engine

- **Standardized 3x Attempt Retry**: All network crawlers and scrapers implement robust retry logic with fresh browser/session contexts upon encountering transient timeout or connection errors.
- **SOCKS5 WARP Proxy**: Automated SOCKS5 proxy routing (`127.0.0.1:40000`) for Gemini Vision CAPTCHA solvers, bypassing intranet firewall restrictions.
- **Serial PaddleOCR Lock & Caching**: Employs a threading lock (`_PADDLE_LOCK`) and interpreter caching to prevent CPU thrashing during concurrent OCR extraction passes.
- **On-Disk PNG Recovery**: Automatically recovers missing MRTG metrics by re-extracting legends directly from saved raw PNG snapshots (`MRTG_{sid}_{date}.png`), eliminating false-negative "No Data" statuses.
- **Live WAC & Snapshot Fallback**: Graceful fallback hierarchy from 14:00 peak snapshots to live NCE scraping and past historical cache files.

---

## 4. CLI Usage & Commands

The system is controlled via the `network_report.cli` module:

```bash
# Run the complete daily report pipeline for today (or specified date)
python -m network_report.cli run [--date YYYY-MM-DD] [--send-wa] [--test]

# Run the 14:00 WIB Peak Hour AP Sampler
python -m network_report.cli sample-ap [--send-wa] [--test]

# Run the Indibiz bandwidth sampler
python -m network_report.cli sample-indibiz [--send-wa] [--test]

# Test nexthop ICMP ping connectivity
python -m network_report.cli test-ping

# Test WhatsApp GOWA notification gateway connectivity
python -m network_report.cli test-wa
```

---

## 5. Systemd Deployment & Timers

For 24/7 automated execution on Debian hosts, place service and timer units under `/etc/systemd/system/`:

### Daily Report Pipeline Timer (22:00 WIB)
```ini
[Unit]
Description=Network Daily Report Pipeline Timer

[Timer]
OnCalendar=*-*-* 22:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

### AP Peak Hour Sampler Timer (14:00 WIB)
```ini
[Unit]
Description=Huawei AP Peak Hour Sampler Timer

[Timer]
OnCalendar=*-*-* 14:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

---

## 6. Testing & CI/CD

The codebase includes a comprehensive pytest test suite (180+ unit and integration tests):

```bash
# Run full test suite
pytest

# Run CSIRT Zero-Leak compliance check
pytest tests/test_no_leak.py
```

---

## 7. Zero-Leak Security & CSIRT Compliance

To comply strictly with Telkom CSIRT security policies and portfolio publication standards, the repository enforces zero leakage of sensitive identifiers:
- **No Private IPs**: Strict regex validation prevents private IPv4 leaks (`192.168.*`, `172.16-31.*`, restricted `10.*`).
- **Sanitized Circuits**: All circuit SIDs are replaced with synthetic dummy identifiers.
- **Neutral Identifiers**: Corporate names are generalized (`CORP`).
- **Secure Domain Templates**: Internal production portals reference `telkomcare.example.internal`.
- **Automated Verification**: `tests/test_no_leak.py` runs in CI to reject any accidental commits containing sensitive patterns.
