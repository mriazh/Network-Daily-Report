*MONITORING NETWORK INFRASTRUCTURE*
📅 [DD MMMM YYYY]

*PIC Network Support:*
1. [Staff Engineer 1]
2. [Staff Engineer 2]
3. [Staff Engineer 3]
4. [Staff Engineer 4]
5. [Staff Engineer 5]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*A. 🌐 Telkomsel IoT M2M*

1. Service Overview
  a. Status → ✅ Normal
  b. Total Usage → [0.00] GB
  c. Active Session → [0] Session

2. 🔝 Top Daily Usage
  a. SITE.1 → [0.00] GB
  b. SITE.2 → [0.00] GB
  c. SITE.3 → [0.00] GB
  d. SITE.4 → [0.00] GB
  e. SITE.5 → [0.00] GB

3. 🔝 Top Periodic Usage
  a. SITE.1 → [0.00] GB
  b. SITE.2 → [0.00] GB
  c. SITE.3 → [0.00] GB
  d. SITE.4 → [0.00] GB
  e. SITE.5 → [0.00] GB

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*B. 📊 Usage by MRTG*

1. 🔗 ASTINET
  a. Internet Link Backup (SID: SID-AST-XXXX) ([XX Mbps]) → ✅ Normal
  b. Internet Link Primary (SID: SID-AST-XXXX) ([XX Mbps]) → ✅ Normal
  c. Branch Office Alpha (SID: SID-AST-XXXX) ([XX Mbps]) → ✅ Normal
  d. Branch Office Bravo (SID: SID-AST-XXXX) ([XX Mbps]) → ✅ Normal
  e. Branch Office Charlie (SID: SID-AST-XXXX) ([XX Mbps]) → ⚠️ High Traffic (>0.00%)
  f. Branch Office Delta (SID: SID-AST-XXXX) ([XX Mbps]) → ✅ Normal

2. 🌉 Metro-E
  a. Metro-E Node Alpha (SID: SID-MTR-XXXX) ([XX Mbps]) → ✅ Normal
  b. Metro-E Node Bravo (SID: SID-MTR-XXXX) ([XX Mbps]) → ✅ Normal
  c. Metro-E Node Charlie (SID: SID-MTR-XXXX) ([XX Mbps]) → ✅ Normal
  d. Metro-E Node Delta (SID: SID-MTR-XXXX) ([XX Mbps]) → ✅ Normal

3. 🔐 VPN IP
  a. Core Link HQ - DC A (SID: SID-VPN-XXXX) ([XX Mbps]) → ✅ Normal
  b. Core Link DC A - HQ (SID: SID-VPN-XXXX) ([XX Mbps]) → ✅ Normal
  c. VPN Branch Alpha (SID: SID-VPN-XXXX) ([XX Mbps]) → ✅ Normal
  d. VPN Branch Charlie (SID: SID-VPN-XXXX) ([XX Mbps]) → ⚠️ High Traffic (>0.00%)
  e. VPN Branch Delta (SID: SID-VPN-XXXX) ([XX Mbps]) → ✅ Normal
  f. VPN Branch Echo (SID: SID-VPN-XXXX) ([XX Mbps]) → ✅ Normal
  g. VPN Branch Foxtrot (SID: SID-VPN-XXXX) ([XX Mbps]) → ✅ Normal

4. 📡 Indibiz / Internet Backup
  a. Remote Office (SID: SID-IND-XXXX) ([XX Mbps]) → ✅ Normal

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*C. 🔀 Switch & Core Infrastructure*

1. 📡 Nexthop (Ping)
  a. Private Cloud Gateway → ✅ Normal (IP 10.10.1.1)
  b. VPN Hub Gateway → ✅ Normal (IP 10.10.1.2)
  c. Upstream Provider Router → ✅ Normal (IP 10.10.2.1)
  d. Cellular IoT Gateway → ✅ Normal (IP 10.10.2.2)
  e. Metro Backbone Core → ✅ Normal (IP 10.10.3.1)
  f. Partner B2B Gateway → ✅ Normal (IP 10.10.3.2)
  g. Cloud Transit Hub → ✅ Normal (IP 10.10.4.1)

2. 🖥️ PRTG Sensors
  a. 🟢 UP → [0]
  b. ⏸️ Paused → [0]
  c. Total → [0] Sensors

3. ⚙️ Top 10 CPU Utilization
  a. SW-CORE-01 → [0.00%]
  b. SW-DIST-01 → [0.00%]
  c. SW-ACCESS-01 → [0.00%]
  d. SW-ACCESS-02 → [0.00%]
  e. SW-ACCESS-03 → [0.00%]
  f. SW-ACCESS-04 → [0.00%]
  g. SW-ACCESS-05 → [0.00%]
  h. SW-ACCESS-06 → [0.00%]
  i. SW-ACCESS-07 → [0.00%]
  j. SW-ACCESS-08 → [0.00%]

4. 💾 Top 10 Memory Usage
  a. WLC-PRIMARY-01 → [0.00%] (⚠️ Monitoring)
  b. SW-DIST-01 → [0.00%]
  c. SW-DIST-02 → [0.00%]
  d. SW-ACCESS-01 → [0.00%]
  e. SW-ACCESS-02 → [0.00%]
  f. SW-ACCESS-03 → [0.00%]
  g. SW-ACCESS-04 → [0.00%]
  h. SW-ACCESS-05 → [0.00%]
  i. SW-ACCESS-06 → [0.00%]
  j. SW-ACCESS-07 → [0.00%]

5. ⏱️ Avg Uptime by Device
  a. Device Uptime → [0.00%]
  b. SNMP Comm Uptime → [0.00%]
  c. ICMP Comm Uptime → [0.00%]
  d. HTTPS Comm Uptime → -

6. 📝 Technical Utilization Analysis
  a. CPU Utilization:
    1.) All monitored switches are operating within safe baseline limits (<60%).
    2.) Highest utilization recorded on SW-CORE-01 at [0.00%], normal operation without spikes.
  b. Memory Usage:
    1.) Access and distribution switches maintain stable memory allocation (40%–44%).
    2.) WLC-PRIMARY-01 recorded highest memory usage at [0.00%], under routine monitoring.

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*D. 📶 Huawei AP (WAC)*

1. Status Connectivity
  a. Normal → 🟢 [0]
  b. Fault → 🔴 [0]
  c. Connected Users → 👥 [0]

2. AP Physical Status
  a. Indoor → ✅ [0] OK
  b. Outdoor → ✅ [0] OK, ⚠️ [0] Fault

3. 📊 User Distribution by SSID
  a. Corporate-Staff → [0]
  b. Corporate-Mobile → [0]
  c. Customer-Access → [0]
  d. Guest-Access → [0]
  e. Executive-VIP → [0]
  f. IoT-Sensors → [0]
  g. Partner-Corporate → [0]
  h. Staging-Quarantine → [0]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*E. 📱 Modem Orbit*

1. Modem Meeting Room Alpha
  a. Quota: [0.00] GB / [0] GB
  b. Valid Until: [DD MMMM YYYY]
2. Modem Operations Center
  a. Quota: [0.00] GB / [0] GB
  b. Valid Until: [DD MMMM YYYY]
3. Modem Command Post
  a. Quota: [0.00] GB / [0] GB
  b. Valid Until: [DD MMMM YYYY]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
*F. ⚠️ Highlights & Incident Follow-up*

1. High Traffic
  a. [Circuits with high traffic]

2. Hardware Fault
  a. [Hardware faults or AP downs]

3. Problem Open
  a. [Open incident description]
  b. Next Action: [Follow-up actions]

━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
🏁 Network Support Daily Report. Thank you.
