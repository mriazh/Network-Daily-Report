"""Telkomsel IoT M2M Data Collector and Aggregator - Dual Mode (Daily + Monthly)."""

from __future__ import annotations

import base64
import logging
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

import requests
from openpyxl import load_workbook

logger = logging.getLogger(__name__)

_DASHBOARD_EXTS = (".png", ".jpg", ".jpeg", ".webp")
_DASHBOARD_PATTERNS = ("dashboard_{d}*", "*{d}*dashboard*", "dashboard*{d}*")
# Labels as rendered by the CMP dashboard (English + Indonesian variants) + OCR noise.
_TOTAL_USAGE_LABEL = (
    r"(?:SIM\s*Card[s$]?\s*)?data\s*usag[e]?|total\s*(?:data\s*)?usage|total\s*penggunaan|usag[e]?"
)
# Ordered by specificity: the generic active-* labels also match unrelated tiles.
_SESSION_LABELS = (
    r"user\s*session|session\s*aktif",
    r"active\s*session",
    r"active\s*(?:user|sim|subscriber)",
)
_GB_FACTORS = {"TB": 1024.0, "GB": 1.0, "MB": 1.0 / 1024}
# PaddleOCR runs only out-of-process: paddlepaddle is not in this project's deps, so an
# in-process import would just log a traceback. _PADDLE_SCRIPT crops the leftmost tile
# (where Total Usage lives) and prints its recognised lines.
_PADDLE_TIMEOUT_S = 300
_PADDLE_SCRIPT = (
    "import sys\n"
    "from PIL import Image\n"
    "from paddleocr import PaddleOCR\n"
    "im = Image.open(sys.argv[1])\n"
    "im.crop((0, 0, min(500, im.width), min(150, im.height))).save(sys.argv[2])\n"
    "res = PaddleOCR(lang='en').ocr(sys.argv[2])\n"
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
# Gemini Vision OCR fallback
_GEMINI_PROMPT = (
    "Read the SIM Cards Data Usage total gigabytes and user session count. "
    "Return in format: Total Usage: X GB, User Session: Y"
)
_GEMINI_TIMEOUT_S = 30


@dataclass
class IotDailySummary:
    date_str: str
    total_usage_gb: float
    active_sessions: int
    top_daily: list[tuple[str, float]]  # (site, usage_gb)
    top_periodic: list[tuple[str, float]]  # (site, cumulative_gb)
    periodic_range_str: str


def load_iccid_mapping(mapping_path: Path) -> dict[str, str]:
    mapping: dict[str, str] = {}
    if not mapping_path.exists():
        logger.warning("ICCID mapping file not found at %s", mapping_path)
        return mapping

    wb = load_workbook(mapping_path, data_only=True)
    ws = wb.active
    for r in range(2, ws.max_row + 1):
        iccid = ws.cell(r, 1).value
        loc = ws.cell(r, 2).value
        if iccid and loc:
            mapping[str(iccid).strip()] = str(loc).strip()
    return mapping


def parse_cmp_raw_excel(
    raw_excel_path: Path, mapping: dict[str, str]
) -> tuple[str, dict[str, float]]:
    """Parses a single raw file from CMP (Daily or Monthly) and returns (record_date_iso, site -> usage_bytes)."""
    site_usage: dict[str, float] = {}
    wb = load_workbook(raw_excel_path, data_only=True)
    ws = wb.active

    record_date = ""
    for r in range(2, ws.max_row + 1):
        if not record_date:
            val_d = ws.cell(r, 1).value
            if val_d:
                record_date = str(val_d).strip()
        iccid = str(ws.cell(r, 2).value or "").strip()
        usage_bytes = ws.cell(r, 3).value or 0
        try:
            val = float(usage_bytes)
        except (ValueError, TypeError):
            val = 0.0

        loc = mapping.get(iccid, f"UNKNOWN_{iccid[-4:]}")
        site_usage[loc] = site_usage.get(loc, 0.0) + val

    wb.close()
    return record_date, site_usage


def _raw_files(raw_dir: Path, suffix_pattern: str) -> list[Path]:
    """CMP exports sit either flat in raw_dir or inside a per-date subdirectory (raw/{YYYYMMDD}/).

    '**/' already covers both, so a single recursive glob is enough.
    """
    return sorted(raw_dir.glob(f"**/{suffix_pattern}"), key=lambda f: (f.name, str(f)))


def _find_latest_file(
    raw_dir: Path, pattern: str, target_date_iso: str | None = None
) -> Path | None:
    """Find the latest matching file (flat or in a date subdirectory), optionally filtered by target date."""
    files = _raw_files(raw_dir, pattern)
    if not files:
        return None
    if target_date_iso:
        date_compact = target_date_iso.replace("-", "")
        # Prefer file with target date in name
        for f in files:
            if date_compact in f.name:
                return f
    # Return the latest file
    return files[-1]


def find_dashboard_image(dirs: list[Path | None], date_compact: str) -> Path | None:
    """Locate the dashboard screenshot for a date, searching dirs in order."""
    for d in dirs:
        if not d or not d.exists():
            continue
        all_hits = []
        for pattern in _DASHBOARD_PATTERNS:
            for ext in _DASHBOARD_EXTS:
                all_hits.extend(d.glob(pattern.format(d=date_compact) + ext))
        if all_hits:
            # Sort by mtime descending so the most recent screenshot is picked!
            newest = sorted(set(all_hits), key=lambda f: f.stat().st_mtime, reverse=True)[0]
            return newest
    return None


def _sibling_repo_dirs(repo_root: Path) -> list[Path]:
    """Repositories sitting next to this one (paddleocr venvs live in a sibling checkout)."""
    parent = repo_root.parent
    return (
        sorted(p for p in parent.iterdir() if p.is_dir() and p != repo_root)
        if parent.is_dir()
        else []
    )


def _paddle_interpreters() -> list[Path]:
    """Interpreters that may have paddleocr: PADDLEOCR_PYTHON first, then sibling venvs."""
    found: list[Path] = []
    override = os.environ.get("PADDLEOCR_PYTHON", "")
    if override and Path(override).is_file():
        found.append(Path(override))
    for sibling in _sibling_repo_dirs(Path(__file__).resolve().parents[3]):
        for pattern in _PADDLE_VENV_GLOBS:
            found.extend(sorted(p for p in sibling.glob(pattern) if p.is_file()))
    return found


def _paddle_text_via_subprocess(image_path: Path, crop_path: Path) -> str:
    """Run PaddleOCR from a venv that has it (paddleocr is absent from this project's deps)."""
    for python in _paddle_interpreters():
        cmd = [str(python), "-c", _PADDLE_SCRIPT, str(image_path), str(crop_path)]
        try:
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=_PADDLE_TIMEOUT_S,
                check=False,
                stdin=subprocess.DEVNULL,
            )
        except (OSError, subprocess.SubprocessError):
            logger.info("PaddleOCR interpreter %s unusable", python, exc_info=True)
            continue
        text = (proc.stdout or "").strip()
        if proc.returncode == 0 and text:
            return text
        logger.info("PaddleOCR via %s returned no text (rc=%s)", python, proc.returncode)
    return ""


def _paddle_total_usage_text(text: str) -> str:
    """Reduce PaddleOCR output to the Total Usage value, with its label re-attached.

    A TB/GB/MB value is kept when the crop also shows a usage label, the 'Billing Cycle' tile
    header, or any such value at all: OCR mangles those labels often enough that requiring one
    dropped real dashboards. "" when no TB/GB/MB value is present, since a garbled tile is
    worse than the calculated CMP value.

    Handles OCR noise: '230.09_GB' (underscore), 'Usag' (missing 'e'), 'SIM Card$' etc.
    """
    # Label, 'Billing Cycle' header, or a bare value are each enough to accept the crop.
    accepted = (
        re.search(_TOTAL_USAGE_LABEL, text, re.IGNORECASE)
        or re.search(r"billing\s*cycle", text, re.IGNORECASE)
        or re.search(r"\d+(?:[.,]\d+)?\s*[_]?\s*(?:TB|GB|MB)", text, re.IGNORECASE)
    )
    if not accepted:
        return ""  # garbled tile text is worse than the calculated value

    # Look for a value token matching the pattern: number with optional underscore, then TB/GB/MB
    # Pattern: (\d+(?:[.,]\d+)?)\s*[_]?\s*(?:TB|GB|MB)
    value_pattern = re.compile(r"(\d+(?:[.,]\d+)?)\s*[_]?\s*(TB|GB|MB)", re.IGNORECASE)
    match = value_pattern.search(text)
    if match:
        num = match.group(1).replace(",", ".")
        unit = match.group(2).upper()
        return f"Total Usage\n{num} {unit}"

    return ""


def _read_image_text_paddle(image_path: Path) -> str:
    """Fallback OCR: Total Usage tile text, with the label re-attached for the parser."""
    with tempfile.TemporaryDirectory(prefix="paddle_ocr_") as tmp:
        crop_path = Path(tmp) / f"{image_path.stem}_crop1.png"
        text = _paddle_text_via_subprocess(image_path, crop_path)
    return _paddle_total_usage_text(text) if text else ""


def _gemini_credentials() -> tuple[str, list[str]]:
    """Return (api_key, models) from the app config, falling back to the environment.

    The key is only ever used for OCR of this dashboard screenshot; nothing is persisted.
    """
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    models_raw = (
        os.environ.get("GEMINI_MODELS", "").strip()
        or "gemini-1.5-flash,gemini-2.0-flash,gemini-2.5-flash,gemini-1.5-pro"
    )

    if not api_key:
        try:
            from network_report.config import load_config

            mrtg = load_config().mrtg
            api_key = (mrtg.gemini_api_key or "").strip()
            models_raw = (mrtg.gemini_models or "").strip() or models_raw
        except Exception:  # config is optional; OCR stays best-effort
            logger.info("Gemini config unavailable, relying on environment only", exc_info=True)

    models = [m.strip() for m in models_raw.split(",") if m.strip()]
    return api_key, models


def _gemini_response_text(payload: dict) -> str:
    """Pull the model's plain text out of a Gemini generateContent response."""
    candidates = payload.get("candidates") or []
    for candidate in candidates:
        content = candidate.get("content") or {}
        parts = content.get("parts") or []
        chunks = [p.get("text", "") for p in parts if p.get("text")]
        if chunks:
            return "".join(chunks).strip()
    return ""


def _read_image_text_gemini(image_path: Path) -> str:
    """Fallback OCR via the Gemini Vision API; empty string when unavailable/failing."""
    api_key, models = _gemini_credentials()
    if not api_key or not models:
        logger.info(
            "No GEMINI_API_KEY/GEMINI_MODELS configured, skipping OCR for %s", image_path.name
        )
        return ""

    try:
        image_b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    except OSError:
        logger.warning("Cannot read dashboard image %s", image_path.name, exc_info=True)
        return ""

    mime = "image/png" if image_path.suffix.lower() == ".png" else "image/jpeg"
    body = {
        "contents": [
            {
                "parts": [
                    {"text": _GEMINI_PROMPT},
                    {"inline_data": {"mime_type": mime, "data": image_b64}},
                ]
            }
        ]
    }

    for model in models:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
        try:
            resp = requests.post(
                url,
                json=body,
                headers={"x-goog-api-key": api_key, "Content-Type": "application/json"},
                timeout=_GEMINI_TIMEOUT_S,
            )
        except requests.RequestException:
            logger.info("Gemini OCR request failed for model %s", model, exc_info=True)
            continue

        if resp.status_code != 200:
            logger.info("Gemini OCR model %s returned HTTP %s", model, resp.status_code)
            continue

        text = _gemini_response_text(resp.json())
        if text:
            metrics_text = _gemini_metrics_text(text)
            if metrics_text.strip():
                logger.info("Gemini OCR succeeded with model %s", model)
                return metrics_text
            logger.info("Gemini OCR model %s returned unparsable text: %s", model, text[:100])
        else:
            logger.info("Gemini OCR model %s returned no text", model)

    return ""


def _gemini_metrics_text(text: str) -> str:
    """Normalise the Gemini reply into the label/value shape the metric parser expects.

    Accepts the requested 'Total Usage: 221.88 GB, User Session: 37' form and tolerates
    a bare value or extra prose around it, since the model is not a strict formatter.
    """
    lines: list[str] = []

    usage = re.search(
        r"total\s*(?:data\s*)?usage[^0-9\-]{0,20}(\d[\d.,]*)\s*(TB|GB|MB)?",
        text,
        re.IGNORECASE,
    )
    if usage:
        num = usage.group(1).replace(",", ".")
        unit = (usage.group(2) or "GB").upper()
        lines.append(f"Total Usage\n{num} {unit}")

    session = re.search(
        r"(?:user\s*session|session\s*aktif)\s*[^0-9\-]{0,20}(\d[\d.,]*)",
        text,
        re.IGNORECASE,
    )
    if session:
        lines.append(f"User Session\n{session.group(1).replace(',', '.')}")

    return "\n".join(lines)


def _read_image_text(image_path: Path) -> str:
    """Read dashboard text via PaddleOCR, else Gemini Vision."""
    text = _read_image_text_paddle(image_path)
    return text if text.strip() else _read_image_text_gemini(image_path)


def _match_after_label(
    text: str, label_pattern: str, value_pattern: str, window: int = 120
) -> re.Match | None:
    """Find value_pattern in the text shortly following the first label hit."""
    label = re.search(label_pattern, text, re.IGNORECASE)
    if not label:
        return None
    return re.search(value_pattern, text[label.end() : label.end() + window], re.IGNORECASE)


def _parse_decimal(raw: str) -> float:
    """Parse a dashboard number, accepting '221.88', Indonesian '221,88' and OCR '3/7'."""
    cleaned = raw.strip().replace(" ", "").replace("O", "0").replace("/", "")
    if "," in cleaned and "." not in cleaned:
        cleaned = cleaned.replace(",", ".")
    else:
        cleaned = cleaned.replace(",", "")
    try:
        return float(cleaned)
    except ValueError:
        return 0.0


def extract_dashboard_metrics(image_path: Path | str | None) -> tuple[float | None, int | None]:
    """Return (total_usage_gb, active_sessions) parsed from a dashboard screenshot.

    Returns (None, None) when the image is missing or its text is unusable, so the
    caller keeps the calculated CMP values.
    """
    image = Path(image_path) if image_path else None
    if image is None or not image.exists():
        return None, None

    text = _read_image_text(image)
    if not text.strip():
        return None, None

    total_gb: float | None = None
    hit = _match_after_label(text, _TOTAL_USAGE_LABEL, r"(\d[\d.,/]*)\s*(TB|GB|MB)")
    if hit:
        total_gb = round(_parse_decimal(hit.group(1)) * _GB_FACTORS[hit.group(2).upper()], 2)

    sessions: int | None = None
    for label in _SESSION_LABELS:
        hit = _match_after_label(text, label, r"(\d[\d.,/]*)\b(?!\s*(?:TB|GB|MB|%))", window=60)
        if hit:
            sessions = int(_parse_decimal(hit.group(1)))
            break

    logger.info(
        "Dashboard %s parsed: total_usage_gb=%s sessions=%s",
        image.name,
        total_gb,
        sessions,
    )
    return total_gb, sessions


def collect_iot_summary(
    raw_dir: Path,
    target_date: str,  # format YYYYMMDD or YYYY-MM-DD
    mapping_path: Path,
    dashboard_dirs: list[Path | None] | None = None,
) -> IotDailySummary | None:
    """
    Collect IoT summary using dual-mode ingestion.

    The month-to-date total is the sum of the daily exports, and a missing day
    simply contributes nothing.

    - Daily file (DAILY_USAGE_by_SIM): active_sessions, top_daily
    - Monthly file (MONTHLY_USAGE_by_SIM): total_usage_gb (MTD grand total), top_periodic
    - Dashboard screenshot: overrides total_usage_gb / active_sessions when readable
    - Fallback: accumulate daily files if monthly not available
    """
    mapping = load_iccid_mapping(mapping_path)
    if not raw_dir.exists():
        logger.warning("CMP raw directory not found: %s", raw_dir)
        return None

    # Normalise target date to ISO format YYYY-MM-DD
    if "-" in target_date:
        target_date_iso = target_date
    elif len(target_date) == 8:
        target_date_iso = f"{target_date[:4]}-{target_date[4:6]}-{target_date[6:8]}"
    else:
        target_date_iso = target_date

    month_prefix = target_date_iso[:7]  # YYYY-MM

    # --- 1. Try to load Monthly file for MTD grand total and top_periodic ---
    monthly_file = _find_latest_file(raw_dir, "*MONTHLY_USAGE_by_SIM.xlsx", target_date_iso)
    monthly_site_bytes: dict[str, float] = {}
    monthly_record_date = ""

    if monthly_file and monthly_file.exists():
        logger.info("Using Monthly CMP file: %s", monthly_file.name)
        monthly_record_date, monthly_site_bytes = parse_cmp_raw_excel(monthly_file, mapping)
    else:
        logger.info("Monthly CMP file not found, will fall back to daily accumulation")

    # --- 2. Load Daily files for active_sessions and top_daily ---
    daily_site_bytes: dict[str, float] = {}
    target_sessions = 0

    # Keyed by record date so a date lands once even when several exports repeat it.
    daily_by_date: dict[str, dict[str, float]] = {}
    for f in _raw_files(raw_dir, "*DAILY_USAGE_by_SIM.xlsx"):
        rec_date, usage = parse_cmp_raw_excel(f, mapping)
        if not rec_date or not rec_date.startswith(month_prefix) or rec_date > target_date_iso:
            continue
        daily_by_date[rec_date] = usage

    # Target date when present, else the latest day of the month that has landed: today's
    # file only appears after Telkomsel finishes accumulating it.
    picked_date = (
        target_date_iso if daily_by_date.get(target_date_iso) else max(daily_by_date, default="")
    )
    if picked_date:
        daily_site_bytes = daily_by_date[picked_date]
        target_sessions = len([v for v in daily_site_bytes.values() if v > 0])
        if picked_date != target_date_iso:
            logger.info(
                "No daily CMP file for %s, falling back to latest available %s",
                target_date_iso,
                picked_date,
            )

    # --- 3. Determine cumulative bytes for MTD total and top_periodic ---
    if monthly_site_bytes:
        # Use monthly file data (already MTD cumulative)
        cumulative_site_bytes = monthly_site_bytes
        # Period string from monthly file record date
        if monthly_record_date:
            start_day = "01"
            end_day = (
                monthly_record_date[8:10]
                if len(monthly_record_date) >= 10
                else target_date_iso[8:10]
            )
            period_str = f"{start_day}-{end_day} (Month to Date)"
        else:
            period_str = f"01-{target_date_iso[8:10]} (Month to Date)"
    else:
        # Fallback: accumulate daily files up to target date (one per record date)
        logger.info("Falling back to daily file accumulation for MTD total")
        cumulative_site_bytes = {}
        for rec_date in sorted(daily_by_date):
            for loc, b in daily_by_date[rec_date].items():
                cumulative_site_bytes[loc] = cumulative_site_bytes.get(loc, 0.0) + b
        period_str = f"01-{target_date_iso[8:10]} (Month to Date)"

    # If daily_site_bytes still empty but we have cumulative, use cumulative for daily too
    if not daily_site_bytes and cumulative_site_bytes:
        daily_site_bytes = cumulative_site_bytes
        target_sessions = len([v for v in cumulative_site_bytes.values() if v > 0])

    # --- 4. Dashboard screenshot overrides the live Total Usage / User Session ---
    date_compact = target_date_iso.replace("-", "")
    search_dirs = (
        dashboard_dirs
        if dashboard_dirs is not None
        else [raw_dir / date_compact, raw_dir / "images"]
    )
    dashboard_image = find_dashboard_image(search_dirs, date_compact)
    dash_total_gb, dash_sessions = extract_dashboard_metrics(dashboard_image)
    if dash_sessions is not None:
        target_sessions = dash_sessions

    bytes_to_gb = 1024**3
    top_daily = sorted(
        [(loc, round(b / bytes_to_gb, 2)) for loc, b in daily_site_bytes.items()],
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    top_periodic = sorted(
        [(loc, round(b / bytes_to_gb, 2)) for loc, b in cumulative_site_bytes.items()],
        key=lambda x: x[1],
        reverse=True,
    )[:5]

    total_usage_gb = dash_total_gb if dash_total_gb is not None else 0.0

    return IotDailySummary(
        date_str=target_date,
        total_usage_gb=total_usage_gb,
        active_sessions=target_sessions,
        top_daily=top_daily,
        top_periodic=top_periodic,
        periodic_range_str=period_str,
    )
