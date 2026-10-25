# Network Daily Report

Automated 24/7 network infrastructure daily report system for Debian host.

## Components
- **Data Collectors**:
  - Telkomsel IoT M2M (CMP)
  - MRTG (TelkomCare & Netcare)
  - Switch & Core (ICMP Ping, PRTG, Aruba AirWave NMS)
  - Access Points & Wi-Fi (Huawei iMaster NCE)
  - Modem Orbit (MRTG-CMP cache)
- **Report Writers**:
  - Daily Excel workbook (`Daily-Report-YYYY-MM.xlsx`)
  - WhatsApp broadcast notification (GOWA gateway)
