"""Configuration management for Network Daily Report Automation."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from pydantic import BaseModel, Field

ROOT_DIR = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT_DIR / "config"
OUTPUT_DIR = ROOT_DIR / "output"
GITHUB_DIR = ROOT_DIR.parents[0]

# Load environment from config/.env
ENV_FILE = CONFIG_DIR / ".env"
if ENV_FILE.exists():
    load_dotenv(ENV_FILE)


def _resolve_repo_path(value: str | None) -> Path | None:
    """Resolve a configured path against the repository root.

    Relative values in ``config/.env`` are written relative to the project
    directory, so they must not be interpreted against the process working
    directory: the systemd unit may run from anywhere.
    """
    if not value or not value.strip():
        return None
    candidate = Path(value.strip()).expanduser()
    if candidate.is_absolute():
        return candidate
    return ROOT_DIR / candidate


def _resolve_with_fallback(primary: str | None, *fallbacks: Path) -> Path | None:
    """Resolve primary path from env, fallback to sibling directories if empty/missing data files."""
    candidates = []
    if primary:
        p = Path(primary)
        cand = ROOT_DIR / p if not p.is_absolute() else p
        candidates.append(cand)
    candidates.extend(fallbacks)

    # 1. Prefer candidate that is an existing file
    for c in candidates:
        if c.is_file() and c.exists():
            return c

    # 2. Prefer candidate directory that actually contains data files
    for c in candidates:
        if c.is_dir() and c.exists():
            if (
                any(c.glob("*.xlsx"))
                or any(c.glob("*.csv"))
                or any(c.glob("*.json"))
                or any(c.glob("*.png"))
            ):
                return c
            if any(c.glob("20*/*.csv")) or any(c.glob("Raw/*/*.csv")) or any(c.glob("20*/*.png")):
                return c

    # 3. Fallback to first existing directory
    for c in candidates:
        if c.exists():
            return c

    return candidates[0] if candidates else None


class GowaSettings(BaseModel):
    enabled: bool = Field(default=False)
    base_url: str = Field(default="http://localhost:3000")
    device_id: str = Field(default="default")
    group_jid: str = Field(default="")
    timeout_seconds: int = Field(default=20)
    tls_verify: bool = Field(default=True)


class CheckpointSettings(BaseModel):
    enabled: bool = Field(default=False)
    trac_path: str = Field(default="trac")
    site: str = Field(default="CORP-VPN")
    gateway: str = Field(default="CORP-GATEWAY01")
    username: str = Field(default="")
    password: str = Field(default="")
    connect_timeout_seconds: int = Field(default=60)


class WarpSettings(BaseModel):
    enabled: bool = Field(default=False)
    cli_path: str = Field(default="warp-cli")
    mode: str = Field(default="proxy")
    proxy_port: int = Field(default=40000)
    auto_connect: bool = Field(default=True)
    trace_url: str = Field(default="https://www.cloudflare.com/cdn-cgi/trace")
    proxy_server: str = Field(default="")


class CmpSettings(BaseModel):
    enabled: bool = Field(default=True)
    monthly_export_enabled: bool = Field(default=False)
    base_url: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    headless: bool = Field(default=True)
    timeout_seconds: int = Field(default=60)
    export_timeout_seconds: int = Field(default=300)
    imap_host: str = Field(default="")
    imap_port: int = Field(default=993)
    email: str = Field(default="")
    email_password: str = Field(default="")
    otp_timeout_seconds: int = Field(default=120)
    otp_poll_interval_seconds: int = Field(default=5)
    raw_dir: Path | None = None
    proxy_server: str = Field(default="")


class MrtgSettings(BaseModel):
    enabled: bool = Field(default=True)
    base_url: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    totp_secret: str = Field(default="")
    gemini_api_key: str = Field(default="")
    gemini_models: str = Field(default="gemini-1.5-flash,gemini-2.0-flash")
    telkomcare_dir: Path | None = None


class RouterOSSettings(BaseModel):
    host: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    port: int = Field(default=5336)
    interface: str = Field(default="WAN")
    timeout_seconds: int = Field(default=30)
    plaintext_login: bool = Field(default=True)
    db_path: Path | None = Field(
        default_factory=lambda: ROOT_DIR / "output" / "raw" / "indibiz" / "traffic.db"
    )
    sample_interval_seconds: int = Field(default=30)


class NetcareSettings(BaseModel):
    enabled: bool = Field(default=True)
    base_url: str = Field(default="https://telkomcare.telkom.co.id")
    username: str = Field(default="")
    password: str = Field(default="")
    totp_secret: str = Field(default="")
    captcha_enabled: bool = Field(default=True)
    captcha_api_keys: str = Field(default="")
    captcha_models: str = Field(
        default="gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash"
    )
    profile_dir: Path | None = None
    cookie_cache_path: Path | None = None
    headless: bool = Field(default=True)
    page_timeout_seconds: int = Field(default=30)
    graph_timeout_seconds: int = Field(default=30)
    workers: int = Field(default=3)
    binary_path: str = Field(default="/usr/bin/chromium")
    driver_path: str = Field(default="/usr/bin/chromedriver")


class PrtgSettings(BaseModel):
    enabled: bool = Field(default=True)
    base_url: str = Field(default="http://127.0.0.1:8080")
    username: str = Field(default="")
    password: str = Field(default="")
    timeout_seconds: int = Field(default=15)


class AirwaveSettings(BaseModel):
    enabled: bool = Field(default=True)
    base_url: str = Field(default="")
    username: str = Field(default="")
    password: str = Field(default="")
    headless: bool = Field(default=True)
    report_title: str = Field(default="#UP time Report Switch Daily")
    report_definition_id: int = Field(default=681)
    poll_interval_seconds: int = Field(default=15)
    poll_timeout_minutes: int = Field(default=10)
    max_retries: int = Field(default=3)
    session_state_path: Path | None = Field(
        default_factory=lambda: ROOT_DIR / "output" / "profiles" / "airwave" / "storage_state.json"
    )
    export_dir: Path | None = None


class HuaweiSettings(BaseModel):
    enabled: bool = Field(default=True)
    base_url: str = Field(default="")
    login_path: str = Field(default="/view/login.html")
    username: str = Field(default="")
    password: str = Field(default="")
    headless: bool = Field(default=True)
    login_timeout_seconds: int = Field(default=45)
    export_timeout_seconds: int = Field(default=120)
    nav_timeout_seconds: int = Field(default=60)
    sel_login_username: str = Field(default="#username")
    sel_login_password: str = Field(default="#password")
    sel_login_submit: str = Field(default="#submitButton")
    sel_nav_ap: str = Field(default="#dashAP")
    sel_nav_ssid: str = Field(default="#dashSSIDs")
    sel_nav_ac: str = Field(default="#dashAC")
    sel_export_ap: str = Field(default="#outputApListInfoBtnEn")
    sel_export_ssid: str = Field(default="#ssidListExportBtn_uBtn")
    raw_dir: Path | None = None


class OrbitSettings(BaseModel):
    enabled: bool = Field(default=True)
    cache_file: Path | None = None
    headless: bool = Field(default=True)
    timeout_seconds: int = Field(default=60)


class AppConfig(BaseModel):
    timezone: str = Field(default="Asia/Jakarta")
    log_level: str = Field(default="INFO")
    schedule_time: str = Field(default="23:00")

    # Paths
    template_excel: Path = Field(
        default_factory=lambda: (
            CONFIG_DIR / "Template-Daily-Report.xlsx"
            if (CONFIG_DIR / "Template-Daily-Report.xlsx").exists()
            else CONFIG_DIR / "Template-Daily-Report.example.xlsx"
        )
    )
    template_wag: Path = Field(default_factory=lambda: CONFIG_DIR / "Template-Daily-WAG.md")
    iccid_location_file: Path = Field(default_factory=lambda: CONFIG_DIR / "ICCID-Location.xlsx")
    circuits_file: Path = Field(default_factory=lambda: CONFIG_DIR / "circuits.json")
    output_root: Path = Field(default_factory=lambda: OUTPUT_DIR)
    output_raw_root: Path = Field(default_factory=lambda: OUTPUT_DIR / "raw")

    # Integrations
    gowa: GowaSettings = Field(default_factory=GowaSettings)
    checkpoint: CheckpointSettings = Field(default_factory=CheckpointSettings)
    warp: WarpSettings = Field(default_factory=WarpSettings)

    # Source Subsystems
    cmp: CmpSettings = Field(default_factory=CmpSettings)
    mrtg: MrtgSettings = Field(default_factory=MrtgSettings)
    routeros: RouterOSSettings = Field(default_factory=RouterOSSettings)
    netcare: NetcareSettings = Field(default_factory=NetcareSettings)
    prtg: PrtgSettings = Field(default_factory=PrtgSettings)
    airwave: AirwaveSettings = Field(default_factory=AirwaveSettings)
    huawei: HuaweiSettings = Field(default_factory=HuaweiSettings)
    orbit: OrbitSettings = Field(default_factory=OrbitSettings)

    # Nexthop
    ping_count: int = Field(default=5)
    ping_timeout_seconds: int = Field(default=2)


def load_config() -> AppConfig:
    def _opt_path(val: str | None) -> Path | None:
        return Path(val) if val and val.strip() else None

    gowa = GowaSettings(
        enabled=os.getenv("GOWA_ENABLED", "false").lower() in ("true", "1", "yes"),
        base_url=os.getenv("GOWA_BASE_URL", "http://localhost:3000"),
        device_id=os.getenv("GOWA_DEVICE_ID", "default"),
        group_jid=os.getenv("GOWA_GROUP_JID", ""),
        timeout_seconds=int(os.getenv("GOWA_TIMEOUT_SECONDS", "20")),
        tls_verify=os.getenv("GOWA_TLS_VERIFY", "true").lower() in ("true", "1", "yes"),
    )
    checkpoint = CheckpointSettings(
        enabled=os.getenv("CHECKPOINT_ENABLED", "false").lower() in ("true", "1", "yes"),
        trac_path=os.getenv("CHECKPOINT_TRAC_PATH", "trac"),
        site=os.getenv("CHECKPOINT_SITE", "CORP-VPN"),
        gateway=os.getenv("CHECKPOINT_GATEWAY", "CORP-GATEWAY01"),
        username=os.getenv("CHECKPOINT_USERNAME", ""),
        password=os.getenv("CHECKPOINT_PASSWORD", ""),
        connect_timeout_seconds=int(os.getenv("CHECKPOINT_CONNECT_TIMEOUT_SECONDS", "60")),
    )
    warp = WarpSettings(
        enabled=os.getenv("WARP_ENABLED", "false").lower() in ("true", "1", "yes"),
        cli_path=os.getenv("WARP_CLI_PATH", "warp-cli"),
        mode=os.getenv("WARP_MODE", "proxy"),
        proxy_port=int(os.getenv("WARP_PROXY_PORT", "40000")),
        auto_connect=os.getenv("WARP_AUTO_CONNECT", "true").lower() in ("true", "1", "yes"),
        trace_url=os.getenv("WARP_TRACE_URL", "https://www.cloudflare.com/cdn-cgi/trace"),
        proxy_server=os.getenv("WARP_PROXY_SERVER", ""),
    )
    cmp_fallbacks = [ROOT_DIR / "output" / "raw" / "iot"]
    if GITHUB_DIR.exists():
        cmp_fallbacks.extend([d / "output" / "raw" for d in GITHUB_DIR.glob("*CMP-Automation")])

    cmp_set = CmpSettings(
        enabled=os.getenv("CMP_ENABLED", "true").lower() in ("true", "1", "yes"),
        monthly_export_enabled=os.getenv("CMP_MONTHLY_EXPORT_ENABLED", "false").lower()
        in ("true", "1", "yes"),
        base_url=os.getenv("CMP_BASE_URL", ""),
        username=os.getenv("CMP_USERNAME", ""),
        password=os.getenv("CMP_PASSWORD", ""),
        headless=os.getenv("CMP_HEADLESS", "true").lower() in ("true", "1", "yes"),
        timeout_seconds=int(os.getenv("CMP_TIMEOUT_SECONDS", "60")),
        export_timeout_seconds=int(os.getenv("CMP_EXPORT_TIMEOUT_SECONDS", "300")),
        imap_host=os.getenv("CMP_IMAP_HOST") or os.getenv("MAIL_IMAP_HOST", ""),
        imap_port=int(os.getenv("CMP_IMAP_PORT") or os.getenv("MAIL_IMAP_PORT", "993")),
        email=os.getenv("CMP_EMAIL") or os.getenv("MAIL_EMAIL", ""),
        email_password=os.getenv("CMP_EMAIL_PASSWORD") or os.getenv("MAIL_PASSWORD", ""),
        otp_timeout_seconds=int(os.getenv("OTP_TIMEOUT_SECONDS", "120")),
        otp_poll_interval_seconds=int(os.getenv("OTP_POLL_INTERVAL_SECONDS", "5")),
        raw_dir=_resolve_with_fallback(os.getenv("CMP_RAW_DIR"), *cmp_fallbacks),
        proxy_server=os.getenv("CMP_PROXY_SERVER", ""),
    )
    mrtg_set = MrtgSettings(
        enabled=os.getenv("MRTG_ENABLED", "true").lower() in ("true", "1", "yes"),
        base_url=os.getenv("TELKOMCARE_BASE_URL", ""),
        username=os.getenv("TELKOMCARE_USERNAME", "") or os.getenv("NETCARE_USER_LEGACY", ""),
        password=os.getenv("TELKOMCARE_PASSWORD", "") or os.getenv("NETCARE_PASSWORD_LEGACY", ""),
        totp_secret=os.getenv("TOTP_SECRET", ""),
        gemini_api_key=os.getenv("GEMINI_API_KEY", ""),
        gemini_models=os.getenv("GEMINI_MODELS", "gemini-1.5-flash,gemini-2.0-flash"),
        telkomcare_dir=_resolve_repo_path(os.getenv("MRTG_TELKOMCARE_DIR"))
        or (ROOT_DIR / "output" / "raw" / "mrtg"),
    )
    routeros_set = RouterOSSettings(
        host=os.getenv("ROUTEROS_HOST", ""),
        username=os.getenv("ROUTEROS_USERNAME", ""),
        password=os.getenv("ROUTEROS_PASSWORD", ""),
        port=int(os.getenv("ROUTEROS_PORT", "5336")),
        interface=os.getenv("ROUTEROS_INTERFACE", "WAN"),
        timeout_seconds=int(os.getenv("ROUTEROS_TIMEOUT_SECONDS", "30")),
        plaintext_login=os.getenv("ROUTEROS_PLAINTEXT_LOGIN", "true").lower()
        in ("true", "1", "yes"),
        db_path=_resolve_repo_path(os.getenv("INDIBIZ_DB_PATH"))
        or (ROOT_DIR / "output" / "raw" / "indibiz" / "traffic.db"),
        sample_interval_seconds=int(os.getenv("ROUTEROS_SAMPLE_INTERVAL_SECONDS", "30")),
    )
    netcare_api_keys = (
        os.getenv("NETCARE_CAPTCHA_API_KEYS")
        or os.getenv("GEMINI_API_KEYS")
        or os.getenv("GEMINI_API_KEY", "")
    )
    netcare_models = os.getenv(
        "NETCARE_CAPTCHA_MODELS",
        os.getenv(
            "GEMINI_MODELS",
            "gemini-3.5-flash-lite,gemini-3.1-flash-lite,gemini-2.5-flash",
        ),
    )
    netcare_set = NetcareSettings(
        enabled=os.getenv("NETCARE_ENABLED", "true").lower() in ("true", "1", "yes"),
        base_url=os.getenv("TELKOMCARE_GRAPH_URL")
        or os.getenv("TELKOMCARE_BASE_URL", "https://telkomcare.telkom.co.id"),
        username=os.getenv("TELKOMCARE_USERNAME") or os.getenv("NETCARE_USER_LEGACY", ""),
        password=os.getenv("TELKOMCARE_PASSWORD") or os.getenv("NETCARE_PASSWORD_LEGACY", ""),
        totp_secret=os.getenv("TOTP_SECRET", ""),
        captcha_enabled=os.getenv("NETCARE_CAPTCHA_ENABLED", "true").lower()
        in ("true", "1", "yes"),
        captcha_api_keys=netcare_api_keys,
        captcha_models=netcare_models,
        profile_dir=_resolve_repo_path(os.getenv("NETCARE_PROFILE_DIR"))
        or (ROOT_DIR / "output" / "profiles" / "netcare"),
        cookie_cache_path=_resolve_repo_path(os.getenv("NETCARE_COOKIE_CACHE"))
        or (ROOT_DIR / "output" / "profiles" / "netcare" / "cookies.json"),
        headless=os.getenv("NETCARE_HEADLESS", "true").lower() in ("true", "1", "yes"),
        page_timeout_seconds=int(os.getenv("NETCARE_PAGE_TIMEOUT_SECONDS", "30")),
        graph_timeout_seconds=int(os.getenv("NETCARE_GRAPH_TIMEOUT_SECONDS", "30")),
        workers=int(os.getenv("NETCARE_WORKERS", "3")),
        binary_path=os.getenv("NETCARE_BROWSER_BINARY", "/usr/bin/chromium"),
        driver_path=os.getenv("NETCARE_CHROMEDRIVER", "/usr/bin/chromedriver"),
    )
    prtg_set = PrtgSettings(
        enabled=os.getenv("PRTG_ENABLED", "true").lower() in ("true", "1", "yes"),
        base_url=os.getenv("PRTG_BASE_URL", "http://127.0.0.1:8080"),
        username=os.getenv("PRTG_USERNAME", ""),
        password=os.getenv("PRTG_PASSWORD", ""),
        timeout_seconds=int(os.getenv("PRTG_TIMEOUT_SECONDS", "15")),
    )
    airwave_set = AirwaveSettings(
        enabled=os.getenv("AIRWAVE_ENABLED", "true").lower() in ("true", "1", "yes"),
        base_url=os.getenv("AIRWAVE_BASE_URL", "") or os.getenv("AIRWAVE_URL", ""),
        username=os.getenv("AIRWAVE_USERNAME", ""),
        password=os.getenv("AIRWAVE_PASSWORD", ""),
        headless=os.getenv("AIRWAVE_HEADLESS", "true").lower() in ("true", "1", "yes"),
        report_title=os.getenv(
            "AIRWAVE_REPORT_ROW_TITLE",
            os.getenv("AIRWAVE_REPORT_TITLE", "#UP time Report Switch Daily"),
        ),
        report_definition_id=int(os.getenv("AIRWAVE_REPORT_DEFINITION_ID", "681")),
        poll_interval_seconds=int(os.getenv("AIRWAVE_POLL_INTERVAL_SECONDS", "15")),
        poll_timeout_minutes=int(os.getenv("AIRWAVE_POLL_TIMEOUT_MINUTES", "10")),
        max_retries=int(os.getenv("AIRWAVE_MAX_RETRIES", "3")),
        session_state_path=_resolve_repo_path(os.getenv("AIRWAVE_SESSION_STATE_PATH"))
        or (ROOT_DIR / "output" / "profiles" / "airwave" / "storage_state.json"),
        export_dir=_resolve_repo_path(os.getenv("AIRWAVE_EXPORT_DIR"))
        or (ROOT_DIR / "output" / "raw" / "airwave"),
    )
    huawei_set = HuaweiSettings(
        enabled=os.getenv("HUAWEI_ENABLED", "true").lower() in ("true", "1", "yes"),
        base_url=os.getenv("HUAWEI_BASE_URL", ""),
        login_path=os.getenv("HUAWEI_LOGIN_PATH", "/view/login.html"),
        username=os.getenv("HUAWEI_USERNAME", ""),
        password=os.getenv("HUAWEI_PASSWORD", ""),
        headless=os.getenv("HUAWEI_HEADLESS", "true").lower() in ("true", "1", "yes"),
        login_timeout_seconds=int(os.getenv("HUAWEI_LOGIN_TIMEOUT_SECONDS", "45")),
        export_timeout_seconds=int(os.getenv("HUAWEI_EXPORT_TIMEOUT_SECONDS", "120")),
        nav_timeout_seconds=int(os.getenv("HUAWEI_NAV_TIMEOUT_SECONDS", "60")),
        sel_login_username=os.getenv("HUAWEI_SEL_LOGIN_USERNAME", "#username"),
        sel_login_password=os.getenv("HUAWEI_SEL_LOGIN_PASSWORD", "#password"),
        sel_login_submit=os.getenv("HUAWEI_SEL_LOGIN_SUBMIT", "#submitButton"),
        sel_nav_ap=os.getenv("HUAWEI_SEL_NAV_AP", "#dashAP"),
        sel_nav_ssid=os.getenv("HUAWEI_SEL_NAV_SSID", "#dashSSIDs"),
        sel_nav_ac=os.getenv("HUAWEI_SEL_NAV_AC", "#dashAC"),
        sel_export_ap=os.getenv("HUAWEI_SEL_EXPORT_AP", "#outputApListInfoBtnEn"),
        sel_export_ssid=os.getenv("HUAWEI_SEL_EXPORT_SSID", "#ssidListExportBtn_uBtn"),
        raw_dir=_resolve_repo_path(os.getenv("HUAWEI_RAW_DIR"))
        or (ROOT_DIR / "output" / "raw" / "huawei_ap"),
    )
    orbit_set = OrbitSettings(
        enabled=os.getenv("ORBIT_ENABLED", "true").lower() in ("true", "1", "yes"),
        cache_file=_resolve_with_fallback(
            os.getenv("ORBIT_CACHE_FILE"),
            GITHUB_DIR / "MRTG-CMP" / "data" / "orbit_cache" / "modems.json",
            ROOT_DIR / "output" / "raw" / "orbit" / "modems.json",
        ),
        headless=os.getenv("ORBIT_HEADLESS", "true").lower() in ("true", "1", "yes"),
        timeout_seconds=int(os.getenv("ORBIT_TIMEOUT_SECONDS", "60")),
    )

    return AppConfig(
        timezone=os.getenv("TIMEZONE", "Asia/Jakarta"),
        log_level=os.getenv("LOG_LEVEL", "INFO"),
        schedule_time=os.getenv("DAILY_SCHEDULE_TIME", "23:00"),
        gowa=gowa,
        checkpoint=checkpoint,
        warp=warp,
        cmp=cmp_set,
        mrtg=mrtg_set,
        routeros=routeros_set,
        netcare=netcare_set,
        prtg=prtg_set,
        airwave=airwave_set,
        huawei=huawei_set,
        orbit=orbit_set,
        ping_count=int(os.getenv("PING_COUNT", "5")),
        ping_timeout_seconds=int(os.getenv("PING_TIMEOUT_SECONDS", "2")),
    )
