from __future__ import annotations

import base64
import contextlib
import json
import logging
import os
import re
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import pyotp

logger = logging.getLogger(__name__)

CAPTCHA_PROMPT = (
    "Solve this text CAPTCHA. It is a 3-character alphanumeric string and may be case-sensitive. "
    "Output only the 3-character string; no explanation, markdown, punctuation, or newline."
)

MRTG_LEGEND_PROMPT = """Analyze this MRTG network traffic graph image. Extract ONLY the legend values from the bottom of the image.

Return the values in this EXACT format (no additional text):
Inbound_Current: [value]
Inbound_Average: [value]
Inbound_Maximum: [value]
Outbound_Current: [value]
Outbound_Average: [value]
Outbound_Maximum: [value]

Values should include units (e.g., "6.65 k", "1.82 M", "N/A").
If a value is not visible or unclear, write "N/A" for that line."""

ROTATE_STATUS = frozenset({429, 500, 502, 503, 504})
MODEL_BLACKLIST_STATUS = frozenset({404, 503})
DEFAULT_TIMEOUT_SECONDS = 6

MRTG_ISOLATE_SCRIPT = """
// MRTG isolate script
const imgs = document.querySelectorAll("img[src*='graph.php']");
let target = null;
for (let i = 0; i < imgs.length; i++) {
    if (imgs[i].naturalWidth > 400) {
        target = imgs[i];
        break;
    }
}
if (!target && arguments[0] && arguments[0].tagName === "IMG") {
    target = arguments[0];
}
if (!target) return false;

window._mrtg_target = target;
window._mrtg_target_style = target.getAttribute("style");
window._mrtg_body_style = document.body ? document.body.getAttribute("style") : null;
window._mrtg_doc_style = document.documentElement ? document.documentElement.getAttribute("style") : null;

const ancestors = new Set();
let cur = target;
while (cur) {
    ancestors.add(cur);
    cur = cur.parentElement;
}

window._mrtg_hidden_elements = [];
const allElems = document.querySelectorAll("*");
for (let i = 0; i < allElems.length; i++) {
    const el = allElems[i];
    if (el !== target && !ancestors.has(el) && el.tagName !== "SCRIPT" && el.tagName !== "STYLE") {
        window._mrtg_hidden_elements.push({
            el: el,
            display: el.style.getPropertyValue("display"),
            priority: el.style.getPropertyPriority("display")
        });
        el.style.setProperty("display", "none", "important");
    }
}

target.style.setProperty("position", "fixed", "important");
target.style.setProperty("top", "0", "important");
target.style.setProperty("left", "0", "important");
target.style.setProperty("z-index", "2147483647", "important");
target.style.setProperty("margin", "0", "important");
target.style.setProperty("padding", "0", "important");
target.style.setProperty("transform", "none", "important");
target.style.setProperty("max-width", "none", "important");
target.style.setProperty("max-height", "none", "important");

const w = target.naturalWidth || target.width || target.clientWidth;
const h = target.naturalHeight || target.height || target.clientHeight;
if (w) target.style.setProperty("width", w + "px", "important");
if (h) target.style.setProperty("height", h + "px", "important");

if (document.body) {
    document.body.style.setProperty("background-color", "white", "important");
    document.body.style.setProperty("padding", "0", "important");
    document.body.style.setProperty("overflow", "hidden", "important");
}
if (document.documentElement) {
    document.documentElement.style.setProperty("background-color", "white", "important");
    document.documentElement.style.setProperty("padding", "0", "important");
    document.documentElement.style.setProperty("overflow", "hidden", "important");
}

return true;
"""

MRTG_RESTORE_SCRIPT = """
// MRTG restore script
if (window._mrtg_hidden_elements) {
    for (let i = 0; i < window._mrtg_hidden_elements.length; i++) {
        const item = window._mrtg_hidden_elements[i];
        if (item.display) {
            item.el.style.setProperty("display", item.display, item.priority || "");
        } else {
            item.el.style.removeProperty("display");
        }
    }
    window._mrtg_hidden_elements = null;
}
const target = window._mrtg_target || arguments[0];
if (target) {
    if (window._mrtg_target_style !== null && window._mrtg_target_style !== undefined) {
        target.setAttribute("style", window._mrtg_target_style);
    } else {
        target.removeAttribute("style");
    }
    window._mrtg_target = null;
    window._mrtg_target_style = null;
}
if (document.body) {
    if (window._mrtg_body_style !== null && window._mrtg_body_style !== undefined) {
        document.body.setAttribute("style", window._mrtg_body_style);
    } else {
        document.body.removeAttribute("style");
    }
    window._mrtg_body_style = null;
}
if (document.documentElement) {
    if (window._mrtg_doc_style !== null && window._mrtg_doc_style !== undefined) {
        document.documentElement.setAttribute("style", window._mrtg_doc_style);
    } else {
        document.documentElement.removeAttribute("style");
    }
    window._mrtg_doc_style = null;
}
"""

MRTG_CHECK_PINNED_SCRIPT = """
var el = arguments[0];
if (!el) return false;
var cs = window.getComputedStyle ? window.getComputedStyle(el) : null;
var pos = (cs && (cs.position || cs.getPropertyValue('position'))) || (el.style && (el.style.position || el.style.getPropertyValue('position')));
var width = parseFloat((cs && (cs.width || cs.getPropertyValue('width'))) || (el.style && (el.style.width || el.style.getPropertyValue('width')))) || 0;
return pos === 'fixed' && width > 400;
"""


def validate_graph_png(image_path: Path | str) -> bool:
    try:
        path = Path(image_path)
        if not path.is_file():
            return False
        # Size floor deliberately low because a sparse graph compresses well.
        if path.stat().st_size < 2000:
            return False
        from PIL import Image

        with Image.open(path) as img:
            width, height = img.size
            if width < 100 or height < 100:
                return False
            gray = img.convert("L")
            min_lum, max_lum = gray.getextrema()
            lum_range = max_lum - min_lum
            if lum_range < 10:
                hsv = img.convert("RGB").convert("HSV")
                _, s_channel, _ = hsv.split()
                _, max_sat = s_channel.getextrema()
                if max_sat < 10:
                    return False
                s_hist = s_channel.histogram()
                sat_ratio = sum(s_hist[11:]) / (width * height)
                if sat_ratio < 0.0005:
                    return False
        return True
    except Exception as exc:
        logger.warning("MRTG PNG validation failed for %s: %s", image_path, exc)
        return False


_PADDLE_SCRIPT = (
    "import sys\n"
    "from paddleocr import PaddleOCR\n"
    "res = PaddleOCR(lang='en').ocr(sys.argv[1])\n"
    "lines = []\n"
    "if res and res[0]:\n"
    "    first = res[0]\n"
    "    if isinstance(first, dict) and 'rec_texts' in first:\n"
    "        texts = first.get('rec_texts', [])\n"
    "        scores = first.get('rec_scores', [])\n"
    "        for i, t in enumerate(texts):\n"
    "            score = scores[i] if i < len(scores) else 1.0\n"
    "            if score >= 0.80:\n"
    "                lines.append(t)\n"
    "    elif isinstance(first, list):\n"
    "        for item in first:\n"
    "            if isinstance(item, (list, tuple)) and len(item) >= 2:\n"
    "                txt_score = item[1]\n"
    "                if isinstance(txt_score, (list, tuple)) and len(txt_score) >= 2:\n"
    "                    text_val, score = txt_score[0], txt_score[1]\n"
    "                    if score >= 0.80:\n"
    "                        lines.append(text_val)\n"
    "print('\\n'.join(lines))\n"
)

_PADDLE_VENV_GLOBS = (
    ".venv312/Scripts/python.exe",
    ".venv312/bin/python",
    ".venv/Scripts/python.exe",
    ".venv/bin/python",
)

_DROPPED_COOKIE_KEYS = ("sameSite", "storeId", "hostOnly", "session", "expirySource")


@dataclass
class MrtgTarget:
    target_id: str
    kind: str = "sid"
    name: str = ""


@dataclass
class MrtgSample:
    target_id: str
    inbound_current: str | None = None
    inbound_average: str | None = None
    inbound_maximum: str | None = None
    outbound_current: str | None = None
    outbound_average: str | None = None
    outbound_maximum: str | None = None
    image_path: Path | None = None
    error: str | None = None


# The TelkomCare portal uses either SID or graphtitle lookup per circuit.
# The lookup field is a portal-side property that cannot be inferred purely from
# the identifier shape for bare-numeric identifiers.
# No hardcoded title IDs; lookup is inferred from identifier pattern.
BARE_NUMERIC_TITLE_IDS: frozenset[str] = frozenset()
TITLE_CIRCUIT_IDS = BARE_NUMERIC_TITLE_IDS

_FOUR_TEN_PATTERN = re.compile(r"^\d{4}-\d{10}$")
_SID_PATTERN = re.compile(r"^(\d{4}-\d{6}-\d{6}|\d{7}-\d{10})$")


def resolve_netcare_target(identifier: str) -> tuple[str, str]:
    clean_id = identifier.strip()
    if _FOUR_TEN_PATTERN.match(clean_id):
        return ("title", clean_id.split("-", 1)[1])
    if _SID_PATTERN.match(clean_id):
        return ("sid", clean_id)
    if clean_id.isdigit() and len(clean_id) <= 6:
        return ("title", clean_id)
    return ("sid", clean_id)


def build_targets_from_circuits(circuits: dict) -> list[MrtgTarget]:
    title_ids = set(circuits.get("title_ids", []))
    targets: list[MrtgTarget] = []
    for group_key in ("astinet", "metro_e", "vpn_ip_osa"):
        for entry in circuits.get(group_key, []):
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                name = str(entry[0]).strip()
                raw_id = str(entry[1]).strip()
                if len(entry) >= 4 and str(entry[3]).strip() in ("title", "sid"):
                    kind = str(entry[3]).strip()
                    target_id = raw_id
                elif raw_id in title_ids:
                    kind = "title"
                    target_id = raw_id
                else:
                    kind, target_id = resolve_netcare_target(raw_id)
                targets.append(MrtgTarget(target_id=target_id, kind=kind, name=name))
    return targets


def sanitize_cookie(cookie: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(cookie)
    for key in _DROPPED_COOKIE_KEYS:
        cleaned.pop(key, None)
    if isinstance(cleaned.get("expiry"), float):
        cleaned["expiry"] = int(cleaned["expiry"])
    return cleaned


def _is_crash_or_webdriver_exception(exc: BaseException) -> bool:
    try:
        from selenium.common.exceptions import WebDriverException

        if isinstance(exc, WebDriverException):
            return True
    except ImportError:
        pass
    msg = str(exc)
    name = type(exc).__name__.lower()
    return "stacktrace" in msg or "webdriver" in name or "crash" in msg.lower()


def _build_proxy_handler(proxy: str | None) -> urllib.request.BaseHandler | None:
    """Build a ProxyHandler for urllib if proxy is configured.

    Supports http://, https://, socks5://, socks5h:// schemes.
    """
    if not proxy:
        return None
    parsed = urllib.parse.urlparse(proxy)
    if parsed.scheme in ("http", "https"):
        return urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    if parsed.scheme in ("socks5", "socks5h"):
        try:
            import socks  # type: ignore  # noqa: F401
        except Exception:
            logger.warning(
                "PySocks not installed; SOCKS5 proxy %s cannot be used", proxy
            )
            return None
        return urllib.request.ProxyHandler({"http": proxy, "https": proxy})
    logger.warning("Unsupported proxy scheme: %s", parsed.scheme)
    return None


class GeminiCaptchaSolver:
    def __init__(
        self,
        api_keys: list[str] | str,
        models: list[str],
        timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS,
        proxy: str | None = None,
        transport: callable | None = None,
    ) -> None:
        # Defensive parsing: accept api_keys as comma-separated string or list
        if isinstance(api_keys, str):
            self.api_keys = [k.strip() for k in api_keys.split(",") if k.strip()]
        else:
            self.api_keys = [k.strip() for k in api_keys if k.strip()]
        self.models = [m.strip() for m in models if m.strip()]
        self.timeout_seconds = timeout_seconds
        self._proxy = proxy or os.getenv("NETCARE_CAPTCHA_PROXY") or os.getenv("ALL_PROXY")
        self._proxy_handler = _build_proxy_handler(self._proxy)
        self._blacklisted_models: set[str] = set()
        self._next_key_index = 0
        self._transport = transport or self._default_transport

    def _default_transport(self, url: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        req = urllib.request.Request(
            url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        opener = urllib.request.build_opener()
        if self._proxy_handler:
            opener.add_handler(self._proxy_handler)
        try:
            with opener.open(req, timeout=self.timeout_seconds) as response:
                body = response.read().decode("utf-8")
                return 200, json.loads(body)
        except urllib.error.HTTPError as exc:
            raw = b""
            with contextlib.suppress(Exception):
                raw = exc.read()
            try:
                parsed = json.loads(raw.decode("utf-8")) if raw else {}
            except ValueError:
                parsed = {}
            return exc.code, parsed
        except Exception:
            return 599, {}

    def solve(self, image_bytes: bytes) -> str:
        if not image_bytes or not self.api_keys or not self.models:
            return ""

        encoded = base64.b64encode(image_bytes).decode("utf-8")
        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": CAPTCHA_PROMPT},
                        {"inline_data": {"mime_type": "image/png", "data": encoded}},
                    ]
                }
            ]
        }

        total_keys = len(self.api_keys)
        models = [m for m in self.models if m not in self._blacklisted_models]

        for model in models:
            for offset in range(total_keys):
                idx = (self._next_key_index + offset) % total_keys
                key = self.api_keys[idx]
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                status, body = self._transport(url, payload)
                if status == 200:
                    text = self._extract_text(body)
                    code = self._normalize_captcha(text)
                    if code:
                        return code
                elif status in MODEL_BLACKLIST_STATUS:
                    self._blacklisted_models.add(model)
                    break
                elif status in ROTATE_STATUS:
                    self._next_key_index = (idx + 1) % total_keys

        return ""

    @staticmethod
    def _extract_text(payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates") or []
        if candidates and isinstance(candidates, list):
            content = candidates[0].get("content") or {}
            parts = content.get("parts") or []
            if parts and isinstance(parts, list):
                return str(parts[0].get("text") or "").strip()
        return ""

    @staticmethod
    def _normalize_captcha(raw: str) -> str:
        if not raw:
            return ""
        segments = raw.split()
        if not segments:
            return ""
        candidate = segments[-1].strip("`*_'\"| \t\r\n.,:;!?")
        return candidate if re.match(r"^[A-Za-z0-9]{3}$", candidate) else ""


class LegendExtractor:
    def __init__(
        self,
        api_keys: list[str] | None = None,
        models: list[str] | None = None,
        proxy: str | None = None,
    ) -> None:
        self.api_keys = [k.strip() for k in (api_keys or []) if k.strip()]
        self.models = [
            m.strip()
            for m in (
                models or ["gemini-3.5-flash-lite", "gemini-3.1-flash-lite"]
            )
            if m.strip()
        ]
        self._proxy = proxy or os.getenv("NETCARE_CAPTCHA_PROXY") or os.getenv("ALL_PROXY")
        self._proxy_handler = _build_proxy_handler(self._proxy)

    def extract(self, image_path: Path) -> dict[str, str | None]:
        paddle_vals = self._extract_paddle(image_path)
        if (
            paddle_vals
            and paddle_vals.get("Inbound_Maximum")
            and paddle_vals.get("Outbound_Maximum")
        ):
            return paddle_vals
        gemini_vals = self._extract_gemini(image_path)
        if gemini_vals:
            merged = dict(paddle_vals)
            for k, v in gemini_vals.items():
                if v and not merged.get(k):
                    merged[k] = v
            return merged
        return paddle_vals

    def _extract_paddle(self, image_path: Path) -> dict[str, str | None]:
        result = {
            "Inbound_Current": None,
            "Inbound_Average": None,
            "Inbound_Maximum": None,
            "Outbound_Current": None,
            "Outbound_Average": None,
            "Outbound_Maximum": None,
        }
        for python_exe in self._paddle_interpreters():
            cmd = [str(python_exe), "-c", _PADDLE_SCRIPT, str(image_path)]
            try:
                proc = subprocess.run(
                    cmd,
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                    stdin=subprocess.DEVNULL,
                )
            except Exception:
                continue
            if proc.returncode == 0 and proc.stdout:
                lines = [line.strip() for line in proc.stdout.splitlines() if line.strip()]
                parsed = self._parse_legend_lines(lines)
                if any(v is not None for v in parsed.values()):
                    return parsed
        return result

    @staticmethod
    def _paddle_interpreters() -> list[Path]:
        found: list[Path] = []
        override = os.environ.get("PADDLEOCR_PYTHON", "")
        if override and Path(override).is_file():
            found.append(Path(override))
        repo_root = Path(__file__).resolve().parents[3]
        parent = repo_root.parent
        siblings = [p for p in parent.iterdir() if p.is_dir()] if parent.is_dir() else []
        for sibling in siblings:
            for pattern in _PADDLE_VENV_GLOBS:
                found.extend(sorted(p for p in sibling.glob(pattern) if p.is_file()))
        return found

    @staticmethod
    def _parse_legend_lines(lines: list[str]) -> dict[str, str | None]:
        res: dict[str, str | None] = {
            "Inbound_Current": None,
            "Inbound_Average": None,
            "Inbound_Maximum": None,
            "Outbound_Current": None,
            "Outbound_Average": None,
            "Outbound_Maximum": None,
        }

        def find_section_values(direction: str) -> dict[str, str | None]:
            collected: list[str] = []
            active = False
            other = "outbound" if direction == "inbound" else "inbound"
            for line in lines:
                lower = line.lower()
                if direction in lower or (direction == "inbound" and " in " in f" {lower} "):
                    active = True
                    continue
                if active:
                    if (
                        other in lower
                        or (other == "inbound" and " in " in f" {lower} ")
                        or (other == "outbound" and " out " in f" {lower} ")
                    ):
                        break
                    collected.append(line)

            def extract_val(label: str) -> str | None:
                for idx, text in enumerate(collected):
                    if label in text.lower():
                        m_same = re.search(
                            rf"{label}\s*[:=-]?\s*(\d+(?:[.,]\d+)?)\s*([MkGTmkgt]?(?:[Bb]?p?s?)?)",
                            text,
                            re.IGNORECASE,
                        )
                        if m_same and m_same.group(1):
                            num = m_same.group(1).replace(",", ".")
                            unit = m_same.group(2).strip()
                            return f"{num} {unit}".strip()
                        for probe in collected[idx + 1 : idx + 4]:
                            if any(
                                k in probe.lower()
                                for k in ("max", "average", "current", "inbound", "outbound")
                            ):
                                break
                            m = re.search(r"(\d+(?:[.,]\d+)?)\s*([MkGTmkgt]?(?:[Bb]?p?s?)?)", probe)
                            if m:
                                num = m.group(1).replace(",", ".")
                                unit = m.group(2).strip()
                                return f"{num} {unit}".strip()
                for text in collected:
                    m = re.search(
                        rf"{label}.*?(\d+(?:[.,]\d+)?)\s*([MkGTmkgt]?(?:[Bb]?p?s?)?)",
                        text,
                        re.IGNORECASE,
                    )
                    if m:
                        num = m.group(1).replace(",", ".")
                        unit = m.group(2).strip()
                        return f"{num} {unit}".strip()
                return None

            return {
                "Current": extract_val("current"),
                "Average": extract_val("average"),
                "Maximum": extract_val("max"),
            }

        in_vals = find_section_values("inbound")
        out_vals = find_section_values("outbound")
        res["Inbound_Current"] = in_vals["Current"]
        res["Inbound_Average"] = in_vals["Average"]
        res["Inbound_Maximum"] = in_vals["Maximum"]
        res["Outbound_Current"] = out_vals["Current"]
        res["Outbound_Average"] = out_vals["Average"]
        res["Outbound_Maximum"] = out_vals["Maximum"]
        return res

    def _extract_gemini(self, image_path: Path) -> dict[str, str | None]:
        res: dict[str, str | None] = {
            "Inbound_Current": None,
            "Inbound_Average": None,
            "Inbound_Maximum": None,
            "Outbound_Current": None,
            "Outbound_Average": None,
            "Outbound_Maximum": None,
        }
        if not self.api_keys or not self.models:
            return res

        try:
            image_b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
        except Exception:
            return res

        payload = {
            "contents": [
                {
                    "parts": [
                        {"text": MRTG_LEGEND_PROMPT},
                        {"inline_data": {"mime_type": "image/png", "data": image_b64}},
                    ]
                }
            ]
        }

        for model in self.models:
            for key in self.api_keys:
                url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={key}"
                req = urllib.request.Request(
                    url,
                    data=json.dumps(payload).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                )
                try:
                    opener = urllib.request.build_opener()
                    if self._proxy_handler:
                        opener.add_handler(self._proxy_handler)
                    with opener.open(req, timeout=30) as resp:
                        body = json.loads(resp.read().decode("utf-8"))
                        text = GeminiCaptchaSolver._extract_text(body)
                        if text:
                            parsed = self._parse_gemini_text(text)
                            if any(v is not None for v in parsed.values()):
                                return parsed
                except Exception:
                    continue
        return res

    @staticmethod
    def _parse_gemini_text(text: str) -> dict[str, str | None]:
        keys = [
            "Inbound_Current",
            "Inbound_Average",
            "Inbound_Maximum",
            "Outbound_Current",
            "Outbound_Average",
            "Outbound_Maximum",
        ]
        res: dict[str, str | None] = {k: None for k in keys}
        for k in keys:
            m = re.search(rf"{k}\s*:\s*([^\r\n]+)", text, re.IGNORECASE)
            if m:
                val = m.group(1).strip()
                if val and val.upper() != "N/A" and val != "-":
                    res[k] = val
        return res


class NetcareScraper:
    def __init__(
        self,
        base_url: str = "https://telkomcare.example.internal",
        username: str = "",
        password: str = "",
        totp_secret: str = "",
        captcha_enabled: bool = True,
        captcha_api_keys: list[str] | None = None,
        captcha_models: list[str] | None = None,
        captcha_proxy: str | None = None,
        profile_dir: Path | None = None,
        cookie_cache_path: Path | None = None,
        headless: bool = True,
        page_timeout_seconds: int = 30,
        graph_timeout_seconds: int = 30,
        workers: int = 3,
        binary_path: str = "/usr/bin/chromium",
        driver_path: str = "/usr/bin/chromedriver",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        clean_base = self.base_url
        while clean_base.endswith("/public/login"):
            clean_base = clean_base[: -len("/public/login")].rstrip("/")
        self.portal_root = clean_base.rstrip("/")
        self.login_url = f"{self.portal_root}/public/login"
        self.graph_root = f"{self.portal_root}/mrtgnetcare2"
        self.username = username
        self.password = password
        self.totp_secret = totp_secret
        self.captcha_enabled = captcha_enabled
        self.captcha_api_keys = captcha_api_keys or []
        self.captcha_models = captcha_models or [
            "gemini-3.5-flash-lite",
            "gemini-3.1-flash-lite",
        ]
        self.captcha_proxy = captcha_proxy or os.getenv("NETCARE_CAPTCHA_PROXY") or os.getenv("ALL_PROXY")
        self.profile_dir = Path(profile_dir) if profile_dir else None
        if cookie_cache_path:
            self.cookie_cache_path = Path(cookie_cache_path)
        elif self.profile_dir:
            self.cookie_cache_path = self.profile_dir / "cookies.json"
        else:
            self.cookie_cache_path = Path("output/profiles/netcare/cookies.json")
        self.headless = headless
        self.page_timeout_seconds = page_timeout_seconds
        self.graph_timeout_seconds = graph_timeout_seconds
        self.workers = max(1, workers)
        self.binary_path = binary_path
        self.driver_path = driver_path
        self._login_lock = threading.RLock()
        self.captcha_solver = GeminiCaptchaSolver(
            api_keys=self.captcha_api_keys,
            models=self.captcha_models,
            proxy=self.captcha_proxy,
        )
        self.legend_extractor = LegendExtractor(
            api_keys=self.captcha_api_keys,
            models=self.captcha_models,
            proxy=self.captcha_proxy,
        )

    def _create_driver(self, profile_subpath: Path | None = None) -> Any:
        from selenium import webdriver
        from selenium.webdriver.chrome.options import Options as ChromeOptions
        from selenium.webdriver.chrome.service import Service as ChromeService

        options = ChromeOptions()
        if self.headless:
            options.add_argument("--headless=new")
        if self.binary_path and Path(self.binary_path).exists():
            options.binary_location = self.binary_path
        pdir = profile_subpath or self.profile_dir
        if pdir:
            pdir.mkdir(parents=True, exist_ok=True)
            options.add_argument(f"--user-data-dir={pdir}")
        options.add_argument("--no-sandbox")
        options.add_argument("--disable-dev-shm-usage")
        options.add_argument("--disable-gpu")
        options.add_argument("--window-size=1920,1080")
        options.add_experimental_option("excludeSwitches", ["enable-logging"])

        service = None
        if self.driver_path and Path(self.driver_path).exists():
            service = ChromeService(executable_path=self.driver_path)

        return (
            webdriver.Chrome(service=service, options=options)
            if service
            else webdriver.Chrome(options=options)
        )

    def _new_session_driver(self, worker_index: int | str = 0) -> Any:
        sub_profile = self.profile_dir / f"target_{worker_index}" if self.profile_dir else None
        return self._create_driver(profile_subpath=sub_profile)

    def _save_cookies(self, driver: Any) -> None:
        if self.cookie_cache_path:
            try:
                cookies = driver.get_cookies()
                if isinstance(cookies, list) and cookies:
                    self.cookie_cache_path.parent.mkdir(parents=True, exist_ok=True)
                    self.cookie_cache_path.write_text(
                        json.dumps(cookies, indent=2), encoding="utf-8"
                    )
            except Exception as exc:
                logger.debug("Failed saving Netcare cookies: %s", exc)

    def _apply_cached_session(self, driver: Any) -> bool:
        if self.cookie_cache_path and self.cookie_cache_path.is_file():
            try:
                cookies = json.loads(self.cookie_cache_path.read_text(encoding="utf-8"))
                if isinstance(cookies, list) and cookies:
                    driver.get(self.login_url)
                    driver.delete_all_cookies()
                    for c in cookies:
                        if isinstance(c, dict):
                            with contextlib.suppress(Exception):
                                driver.add_cookie(sanitize_cookie(c))
                    driver.get(f"{self.graph_root}/graph/monitoring")
                    time.sleep(2)
                    if self._is_authenticated_url(driver.current_url):
                        return True
            except Exception as exc:
                logger.debug("Failed reusing Netcare cookies: %s", exc)
        return False

    def login(self, driver: Any) -> bool:
        if self._apply_cached_session(driver):
            self._save_cookies(driver)
            return True
        with self._login_lock:
            if self._apply_cached_session(driver):
                self._save_cookies(driver)
                return True
            ok = self._auto_login(driver)
            if ok:
                self._save_cookies(driver)
            return ok

    def _auto_login(self, driver: Any) -> bool:
        if not self.username or not self.password:
            return False

        from selenium.webdriver.common.by import By
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        wait = WebDriverWait(driver, self.page_timeout_seconds)
        login_url = self.login_url
        driver.get(login_url)

        try:
            uname_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#uname")))
            passw_input = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "#passw")))
            uname_input.clear()
            uname_input.send_keys(self.username)
            passw_input.clear()
            passw_input.send_keys(self.password)

            if self.captcha_enabled:
                captcha_elem = wait.until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, "#captcha-element img"))
                )
                captcha_png = captcha_elem.screenshot_as_png
                solved = self.captcha_solver.solve(captcha_png)
                if not solved:
                    return False
                captcha_input = driver.find_element(By.CSS_SELECTOR, "#captcha-input")
                captcha_input.clear()
                captcha_input.send_keys(solved)

            agrees = driver.find_elements(By.CSS_SELECTOR, "#agree")
            if agrees and not agrees[0].is_selected():
                driver.execute_script("arguments[0].click();", agrees[0])

            submits = driver.find_elements(By.CSS_SELECTOR, "#submit")
            if submits:
                submits[0].click()
            time.sleep(3)

            if "/public/login/msg/" in driver.current_url:
                return False

            otp_inputs = driver.find_elements(By.CSS_SELECTOR, "input[name='otp[]']")
            if len(otp_inputs) == 6 and self.totp_secret:
                code = pyotp.TOTP(self.totp_secret).now()
                for idx, ch in enumerate(code):
                    inp = otp_inputs[idx]
                    driver.execute_script("arguments[0].removeAttribute('disabled');", inp)
                    inp.clear()
                    inp.send_keys(ch)
                time.sleep(3)

            if self._is_authenticated_url(driver.current_url):
                self._save_cookies(driver)
                return True
        except Exception as exc:
            logger.warning("Netcare auto-login failed: %s", exc)

        return False

    @staticmethod
    def _is_authenticated_url(url: str) -> bool:
        if not url:
            return False
        clean = url.lower()
        if "/public/login" in clean or "/public/mfa" in clean:
            return False
        return "/mrtgnetcare2" in clean

    @staticmethod
    def _dismiss_alerts(driver: Any, max_attempts: int = 3) -> None:
        for _ in range(max_attempts):
            try:
                alert = driver.switch_to.alert
                alert.accept()
            except Exception:
                break

    @staticmethod
    def _check_session(driver: Any) -> None:
        current_url = getattr(driver, "current_url", "") or ""
        if "/public/login" in current_url.lower():
            raise RuntimeError("Netcare session expired")

    @staticmethod
    def _wait_for_overlay(driver: Any, timeout: float = 10.0) -> None:
        from selenium.webdriver.common.by import By

        time.sleep(0.5)
        end_time = time.time() + timeout
        while time.time() < end_time:
            try:
                overlays = driver.find_elements(
                    By.CSS_SELECTOR,
                    ".loading, .overlay, .blockUI, .blockOverlay, #loading, #overlay, .spinner, .loading-overlay",
                )
                if not isinstance(overlays, (list, tuple)):
                    return
            except Exception:
                break
            visible = False
            for el in overlays:
                with contextlib.suppress(Exception):
                    if el.is_displayed() is True:
                        visible = True
                        break
            if not visible:
                break
            time.sleep(0.5)

    @staticmethod
    def _has_lookup_input(driver: Any, name: str) -> bool:
        from selenium.webdriver.common.by import By

        find_elements = getattr(driver, "find_elements", None)
        find_element = getattr(driver, "find_element", None)
        if find_elements is None and find_element is None:
            return True
        if find_elements is not None:
            try:
                elems = find_elements(By.NAME, name)
                if elems:
                    return True
            except Exception:
                pass
        if find_element is not None:
            try:
                elem = find_element(By.NAME, name)
                if elem is not None:
                    return True
            except Exception:
                pass
        return False

    def _open_graph_page(self, driver: Any, kind: str) -> str:
        if kind == "title":
            dashboard_url = self.graph_root
            title_url = f"{self.graph_root}/graph"
            driver.get(dashboard_url)
            time.sleep(2)
            driver.get(title_url)
            time.sleep(2)

            current = getattr(driver, "current_url", "") or ""
            if "/monitoring" in current or not self._has_lookup_input(driver, "graphtitle"):
                driver.get(dashboard_url)
                time.sleep(2)
                driver.get(title_url)
                time.sleep(2)

            assert self._has_lookup_input(
                driver, "graphtitle"
            ), f"graphtitle input missing on {getattr(driver, 'current_url', '')}"
            page_url = getattr(driver, "current_url", "") or title_url
            logger.info("Opened MRTG graph page: %s (kind=%s)", page_url, kind)
            return page_url

        monitoring_url = f"{self.graph_root}/graph/monitoring"
        current = getattr(driver, "current_url", "") or ""
        if self.graph_root not in current:
            driver.get(self.graph_root)
            time.sleep(2)
            current = getattr(driver, "current_url", "") or ""
        if monitoring_url not in current:
            driver.get(monitoring_url)
            time.sleep(2)
        page_url = getattr(driver, "current_url", "") or monitoring_url
        logger.info("Opened MRTG graph page: %s (kind=%s)", page_url, kind)
        return page_url

    def capture(
        self,
        target: MrtgTarget,
        target_date: date,
        driver: Any | None = None,
        output_dir: Path | None = None,
    ) -> MrtgSample:
        close_driver = False
        if driver is None:
            driver = self._new_session_driver(0)
            close_driver = True
            if not self.login(driver):
                driver.quit()
                return MrtgSample(target_id=target.target_id, error="login_failed")

        from selenium.webdriver.common.by import By
        from selenium.webdriver.common.keys import Keys
        from selenium.webdriver.support import expected_conditions as EC
        from selenium.webdriver.support.ui import WebDriverWait

        try:
            self._dismiss_alerts(driver)
            page_url = self._open_graph_page(driver, target.kind)
            logger.info(
                "MRTG graph page URL reached for target %s: %s",
                target.target_id,
                page_url,
            )

            self._dismiss_alerts(driver)
            self._check_session(driver)

            wait = WebDriverWait(driver, self.page_timeout_seconds)
            input_name = "sid" if target.kind == "sid" else "graphtitle"
            sid_input = wait.until(EC.presence_of_element_located((By.NAME, input_name)))
            driver.execute_script("arguments[0].scrollIntoView(true);", sid_input)
            try:
                sid_input.clear()
                sid_input.send_keys(target.target_id)
                sid_input.send_keys(Keys.ENTER)
            except Exception:
                driver.execute_script(
                    "arguments[0].value = arguments[1]; "
                    "arguments[0].dispatchEvent(new Event('input', { bubbles: true })); "
                    "arguments[0].dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', code: 'Enter', keyCode: 13, which: 13, bubbles: true }));",
                    sid_input,
                    target.target_id,
                )
            time.sleep(2)

            if target.kind == "title":
                try:
                    btn_graph = wait.until(EC.element_to_be_clickable((By.CSS_SELECTOR, "a.btn-graph, .btn-graph")))
                    driver.execute_script("arguments[0].click();", btn_graph)
                except Exception:
                    pass
            else:
                btn_graphs = driver.find_elements(By.CSS_SELECTOR, "a.btn-graph")
                if btn_graphs:
                    driver.execute_script("arguments[0].click();", btn_graphs[0])
            time.sleep(2)

            self._check_session(driver)
            logger.info("MRTG target submitted: %s", target.target_id)

            start_str = target_date.strftime("%d/%m/%Y 00:00")
            end_str = target_date.strftime("%d/%m/%Y 23:55")

            if target.kind == "sid":
                wait.until(
                    EC.presence_of_element_located(
                        (By.XPATH, "//button[contains(normalize-space(), 'Filter')]")
                    )
                )
                inputs = driver.find_elements(
                    By.XPATH,
                    "//button[contains(normalize-space(), 'Filter')]/preceding::input[not(@type='hidden')]",
                )
                if len(inputs) >= 2:
                    driver.execute_script(
                        "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('change'));",
                        inputs[-2],
                        start_str,
                    )
                    driver.execute_script(
                        "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('change'));",
                        inputs[-1],
                        end_str,
                    )
                    fbtn = driver.find_element(
                        By.XPATH, "//button[contains(normalize-space(), 'Filter')]"
                    )
                    driver.execute_script("arguments[0].click();", fbtn)
            else:
                s_inp = wait.until(EC.presence_of_element_located((By.ID, "startdate")))
                e_inp = wait.until(EC.presence_of_element_located((By.ID, "enddate")))
                driver.execute_script(
                    "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('change'));",
                    s_inp,
                    start_str,
                )
                driver.execute_script(
                    "arguments[0].value = arguments[1]; arguments[0].dispatchEvent(new Event('change'));",
                    e_inp,
                    end_str,
                )
                gbtn = driver.find_element(By.ID, "graphfilter")
                driver.execute_script("arguments[0].click();", gbtn)

            self._check_session(driver)
            logger.info(
                "MRTG filter applied: %s (%s - %s)",
                target.target_id,
                start_str,
                end_str,
            )
            self._wait_for_overlay(driver)
            self._check_session(driver)

            graph_img = None
            for _ in range(self.graph_timeout_seconds):
                imgs = driver.find_elements(By.XPATH, "//img[contains(@src, 'graph.php')]")
                for img in imgs:
                    with contextlib.suppress(Exception):
                        w = driver.execute_script("return arguments[0].naturalWidth;", img)
                        if w and int(w) > 400:
                            graph_img = img
                            break
                if graph_img:
                    break
                time.sleep(1)

            if not graph_img:
                return MrtgSample(target_id=target.target_id, error="graph_not_rendered")

            logger.info("MRTG graph image found for target %s", target.target_id)

            date_compact = target_date.strftime("%Y%m%d")
            out_dir = output_dir or (Path("output/raw/mrtg") / date_compact)
            out_dir.mkdir(parents=True, exist_ok=True)
            image_path = out_dir / f"MRTG_{target.target_id}_{date_compact}.png"

            isolated = False
            try:
                isolated = bool(driver.execute_script(MRTG_ISOLATE_SCRIPT, graph_img))
            except Exception as exc:
                logger.warning(
                    "MRTG isolation script failed to run for %s: %s",
                    target.target_id,
                    exc,
                )
                isolated = False

            if isolated:
                target_img = None
                try:
                    for _ in range(15):
                        try:
                            fresh_imgs = driver.find_elements(
                                By.XPATH, "//img[contains(@src, 'graph.php')]"
                            )
                        except Exception:
                            fresh_imgs = []
                        for img in fresh_imgs:
                            try:
                                w = driver.execute_script("return arguments[0].naturalWidth;", img)
                                if w and float(w) > 400:
                                    target_img = img
                                    break
                                pinned = driver.execute_script(MRTG_CHECK_PINNED_SCRIPT, img)
                                if pinned:
                                    target_img = img
                                    break
                            except Exception:
                                continue
                        if target_img:
                            break
                        time.sleep(1)

                    if not target_img and graph_img is not None:
                        try:
                            driver.execute_script("return arguments[0].naturalWidth;", graph_img)
                            target_img = graph_img
                        except Exception:
                            target_img = None

                    if not target_img:
                        raise ValueError(
                            f"No wide graph image found after isolation for {target.target_id}"
                        )
                    png_bytes = target_img.screenshot_as_png
                    if not png_bytes or not isinstance(png_bytes, (bytes, bytearray)):
                        raise ValueError("Element screenshot empty or invalid")
                    image_path.write_bytes(png_bytes)
                finally:
                    with contextlib.suppress(Exception):
                        driver.execute_script(MRTG_RESTORE_SCRIPT, target_img or graph_img)
            else:
                logger.warning(
                    "MRTG isolation script could not run for %s, falling back to viewport",
                    target.target_id,
                )
                driver.save_screenshot(str(image_path))

            if not validate_graph_png(image_path):
                raise ValueError(
                    f"MRTG graph screenshot validation failed for {target.target_id}: {image_path}"
                )

            image_size = image_path.stat().st_size if image_path.exists() else 0
            logger.info("MRTG screenshot saved: %s (%d bytes)", image_path, image_size)

            legend_vals = self.legend_extractor.extract(image_path)
            return MrtgSample(
                target_id=target.target_id,
                inbound_current=legend_vals.get("Inbound_Current"),
                inbound_average=legend_vals.get("Inbound_Average"),
                inbound_maximum=legend_vals.get("Inbound_Maximum"),
                outbound_current=legend_vals.get("Outbound_Current"),
                outbound_average=legend_vals.get("Outbound_Average"),
                outbound_maximum=legend_vals.get("Outbound_Maximum"),
                image_path=image_path,
            )
        except RuntimeError:
            raise
        except Exception as exc:
            if _is_crash_or_webdriver_exception(exc):
                raise
            logger.warning("Failed capturing MRTG graph for %s: %s", target.target_id, exc)
            return MrtgSample(target_id=target.target_id, error=str(exc))
        finally:
            if close_driver:
                driver.quit()

    def capture_all(
        self,
        targets: list[MrtgTarget],
        target_date: date,
        output_dir: Path | None = None,
    ) -> list[MrtgSample]:
        if not targets:
            return []

        date_compact = target_date.strftime("%Y%m%d")
        dest_dir = output_dir or (Path("output/raw/mrtg") / date_compact)
        dest_dir.mkdir(parents=True, exist_ok=True)

        worker_count = min(self.workers, len(targets))
        results_map: dict[str, MrtgSample] = {}

        logger.info("Shared login performed by shared_worker")
        shared_profile = self.profile_dir / "shared" if self.profile_dir else None
        shared_driver = self._create_driver(profile_subpath=shared_profile)
        shared_login_ok = False
        try:
            shared_login_ok = self.login(shared_driver)
            if shared_login_ok:
                self._save_cookies(shared_driver)
                logger.info("Shared login: ok")
            else:
                logger.info("Shared login: error (login_failed)")
        except Exception as exc:
            logger.info("Shared login: error (%s)", exc)
            logger.warning("Shared login failed: %s", exc)
        finally:
            with contextlib.suppress(Exception):
                shared_driver.quit()

        def _worker_task(target_list: list[MrtgTarget], worker_idx: int) -> list[MrtgSample]:
            samples: list[MrtgSample] = []
            for t in target_list:
                max_attempts = 3  # 1 initial + 2 retries
                sample = None
                used_driver = None

                for attempt in range(1, max_attempts + 1):
                    driver = self._new_session_driver(worker_idx)
                    used_driver = driver
                    try:
                        logged_in = self._apply_cached_session(driver)
                        if not logged_in:
                            logger.warning(
                                "Target %s (worker %d, attempt %d/%d) found cached session unusable; performing login",
                                t.target_id,
                                worker_idx,
                                attempt,
                                max_attempts,
                            )
                            logged_in = self.login(driver)

                        if not logged_in:
                            sample = MrtgSample(target_id=t.target_id, error="login_failed")
                        else:
                            sample = self.capture(t, target_date, driver=driver, output_dir=dest_dir)
                    except Exception as exc:
                        msg = str(exc)
                        logger.warning(
                            "Capture attempt %d/%d failed for %s (worker %d): %s",
                            attempt,
                            max_attempts,
                            t.target_id,
                            worker_idx,
                            exc,
                        )
                        sample = MrtgSample(
                            target_id=t.target_id,
                            error=msg or type(exc).__name__,
                        )
                    finally:
                        with contextlib.suppress(Exception):
                            driver.quit()

                    if sample.error is None:
                        logger.info("Target %s succeeded on attempt %d/%d", t.target_id, attempt, max_attempts)
                        break

                    if attempt < max_attempts:
                        logger.info(
                            "Retrying target %s with fresh driver (attempt %d/%d, worker %d)",
                            t.target_id,
                            attempt + 1,
                            max_attempts,
                            worker_idx,
                        )

                assert sample is not None
                samples.append(sample)
                status_str = "ok" if sample.error is None else f"error ({sample.error})"
                logger.info("Target %s (driver %s): %s", t.target_id, used_driver, status_str)

            return samples

        buckets: list[list[MrtgTarget]] = [[] for _ in range(worker_count)]
        for idx, t in enumerate(targets):
            buckets[idx % worker_count].append(t)

        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = [
                executor.submit(_worker_task, buckets[i], i)
                for i in range(worker_count)
                if buckets[i]
            ]
            for f in futures:
                for sample in f.result():
                    results_map[sample.target_id] = sample

        logger.info("MRTG capture round completed: %d targets processed", len(targets))

        ordered_samples = [
            results_map.get(t.target_id, MrtgSample(target_id=t.target_id, error="not_captured"))
            for t in targets
        ]

        values_json: dict[str, Any] = {}
        for s in ordered_samples:
            values_json[s.target_id] = {
                "Inbound_Current": s.inbound_current,
                "Inbound_Average": s.inbound_average,
                "Inbound_Maximum": s.inbound_maximum,
                "Outbound_Current": s.outbound_current,
                "Outbound_Average": s.outbound_average,
                "Outbound_Maximum": s.outbound_maximum,
                "image": s.image_path.name if s.image_path else None,
                "error": s.error,
            }

        json_path = dest_dir / f"mrtg_values_{date_compact}.json"
        json_path.write_text(json.dumps(values_json, indent=2), encoding="utf-8")

        return ordered_samples


def _open_graph_page(driver: Any, kind: str, scraper: NetcareScraper | None = None) -> str:
    active = scraper or NetcareScraper()
    return active._open_graph_page(driver, kind)


__all__ = [
    "BARE_NUMERIC_TITLE_IDS",
    "GeminiCaptchaSolver",
    "LegendExtractor",
    "MRTG_CHECK_PINNED_SCRIPT",
    "MRTG_ISOLATE_SCRIPT",
    "MRTG_RESTORE_SCRIPT",
    "MrtgSample",
    "MrtgTarget",
    "NetcareScraper",
    "TITLE_CIRCUIT_IDS",
    "_open_graph_page",
    "build_targets_from_circuits",
    "resolve_netcare_target",
    "sanitize_cookie",
    "validate_graph_png",
]
