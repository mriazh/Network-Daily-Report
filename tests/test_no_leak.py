"""Leak guard test: ensure no private IPs or unneutralised secrets in tracked files."""

from __future__ import annotations

import ipaddress
import os
import re
import subprocess
from pathlib import Path


SKIP_DIRS = {".git", "output", ".venv", "__pycache__", "docs", ".pytest_cache"}
SKIP_FILES = {".env", "circuits.json", "Template-Daily-WAG.md", "ICCID-Location.xlsx"}
MAX_FILE_SIZE = 2 * 1024 * 1024  # 2 MB

SECRET_SUFFIXES = (
    "PASSWORD",
    "SECRET",
    "API_KEY",
    "API_KEYS",
    "TOKEN",
    "TOTP_SECRET",
    "USERNAME",
)

IP_REGEX = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
ASSIGN_REGEX = re.compile(r"^\s*(?:export\s+)?([A-Za-z0-9_]+)\s*[:=]\s*(.*)$")


def _get_tracked_files(repo_root: Path) -> list[Path]:
    try:
        env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
        res = subprocess.run(
            ["git", "ls-files"],
            cwd=repo_root,
            check=True,
            capture_output=True,
            text=True,
            env=env,
        )
        return [repo_root / line.strip() for line in res.stdout.splitlines() if line.strip()]
    except Exception:
        # Only scan files that are not gitignored
        files = []
        for p in repo_root.rglob("*"):
            if not p.is_file():
                continue
            if p.name in {".env", "circuits.json", "Template-Daily-WAG.md", "ICCID-Location.xlsx"}:
                continue
            files.append(p)
        return files


def _is_skipped(file_path: Path, repo_root: Path) -> bool:
    try:
        rel = file_path.relative_to(repo_root)
    except ValueError:
        rel = file_path
    if any(part in SKIP_DIRS for part in rel.parts):
        return True
    if file_path.name in SKIP_FILES:
        return True
    try:
        if file_path.stat().st_size > MAX_FILE_SIZE:
            return True
    except OSError:
        return True
    return False


def test_no_private_ip_leaks():
    repo_root = Path(__file__).resolve().parents[1]
    net_192 = ipaddress.ip_network("192.168.0.0/16")
    net_172 = ipaddress.ip_network("172.16.0.0/12")
    net_10 = ipaddress.ip_network("10.0.0.0/8")
    allowed_10_10 = ipaddress.ip_network("10.10.0.0/16")

    this_file = Path(__file__).resolve()
    leaks = []

    for file_path in _get_tracked_files(repo_root):
        if file_path.resolve() == this_file:
            continue
        if _is_skipped(file_path, repo_root):
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        for line_no, line in enumerate(content.splitlines(), 1):
            for match in IP_REGEX.finditer(line):
                ip_str = match.group()
                try:
                    ip = ipaddress.ip_address(ip_str)
                except ValueError:
                    continue
                if ip in net_192 or ip in net_172 or (ip in net_10 and ip not in allowed_10_10):
                    leaks.append(
                        f"{file_path.relative_to(repo_root)}:{line_no} contains private IP {ip_str}"
                    )

    assert not leaks, "Found private IP leaks:\n" + "\n".join(leaks)


def test_no_secret_assignments():
    repo_root = Path(__file__).resolve().parents[1]
    this_file = Path(__file__).resolve()
    leaks = []

    for file_path in _get_tracked_files(repo_root):
        if file_path.resolve() == this_file:
            continue
        if _is_skipped(file_path, repo_root):
            continue
        try:
            lines = file_path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue

        for line_no, line in enumerate(lines, 1):
            s = line.strip()
            if not s or s.startswith("#") or s.startswith("//"):
                continue
            m = ASSIGN_REGEX.match(s)
            if not m:
                continue
            var_name, val = m.group(1), m.group(2).strip()
            if not any(var_name.endswith(suffix) for suffix in SECRET_SUFFIXES):
                continue
            # Strip quotes and whitespace
            val_clean = val.strip().strip("'\"").strip()
            if not val_clean:
                continue
            if "CHANGE_ME" in val_clean:
                continue
            if val_clean in {"#username", "#password"} or val_clean.startswith("#"):
                continue
            leaks.append(
                f"{file_path.relative_to(repo_root)}:{line_no} assigns {var_name}={val_clean}"
            )

    assert not leaks, "Found secret assignment leaks:\n" + "\n".join(leaks)


def test_no_csirt_pattern():
    repo_root = Path(__file__).resolve().parents[1]
    this_file = Path(__file__).resolve()
    leaks = []

    pattern = re.compile(r"(?i)" + "telkom" + r"_\w+")

    for file_path in _get_tracked_files(repo_root):
        if file_path.resolve() == this_file:
            continue
        if _is_skipped(file_path, repo_root):
            continue
        try:
            content = file_path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue

        rel_path = str(file_path.relative_to(repo_root)).replace("\\", "/")

        for line_no, line in enumerate(content.splitlines(), 1):
            if pattern.search(line):
                leaks.append(
                    f"{rel_path}:{line_no} contains forbidden CSIRT pattern: {line.strip()}"
                )

    assert not leaks, "Found forbidden pattern leaks:\n" + "\n".join(leaks)
