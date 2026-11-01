"""Download and hand off to a verified newer GitHub release."""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from version import APP_VERSION, ASSET_NAME, GITHUB_REPOSITORY

API_URL = f"https://api.github.com/repos/{GITHUB_REPOSITORY}/releases/latest"
MAX_EXE_BYTES = 250 * 1024 * 1024


def _version_tuple(value: str) -> tuple[int, int, int] | None:
    match = re.fullmatch(r"v?(\d+)\.(\d+)\.(\d+)", value.strip())
    return tuple(map(int, match.groups())) if match else None


def _request(url: str, timeout: int = 8, limit: int = 2 * 1024 * 1024) -> bytes:
    request = urllib.request.Request(
        url,
        headers={
            "User-Agent": f"StarkinsDashboard/{APP_VERSION}",
            "Accept": "application/vnd.github+json",
        },
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        data = response.read(limit + 1)
    if len(data) > limit:
        raise ValueError("Server response exceeded the allowed size")
    return data


def _release_info() -> dict[str, Any]:
    release = json.loads(_request(API_URL).decode("utf-8"))
    if release.get("draft") or release.get("prerelease"):
        raise ValueError("No stable release is available")
    return release


def _expected_digest(release: dict[str, Any], exe_asset: dict[str, Any]) -> str:
    digest = exe_asset.get("digest")
    if isinstance(digest, str) and digest.lower().startswith("sha256:"):
        return digest.split(":", 1)[1].lower()

    checksum_asset = next(
        (item for item in release.get("assets", [])
         if item.get("name") == ASSET_NAME + ".sha256"),
        None,
    )
    if not checksum_asset:
        raise ValueError("Release is missing the SHA-256 checksum")
    checksum_text = _request(checksum_asset["browser_download_url"], timeout=8, limit=4096).decode("ascii", "strict")
    match = re.search(r"\b([0-9a-fA-F]{64})\b", checksum_text)
    if not match:
        raise ValueError("Release checksum is invalid")
    return match.group(1).lower()


def _file_digest(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _launch(executable: Path) -> bool:
    if not executable.is_file():
        return False
    flags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    subprocess.Popen([str(executable)], cwd=str(executable.parent), close_fds=True, creationflags=flags)
    return True


def _cache_root() -> Path:
    base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
    return Path(base) / "StarkinsDashboard" / "versions"


def _best_cached() -> tuple[tuple[int, int, int], Path] | None:
    root = _cache_root()
    if not root.is_dir():
        return None
    candidates = []
    for folder in root.iterdir():
        version = _version_tuple(folder.name)
        exe = folder / ASSET_NAME
        checksum = folder / (ASSET_NAME + ".sha256")
        if not version or not exe.is_file() or not checksum.is_file():
            continue
        try:
            expected = checksum.read_text(encoding="ascii").strip().lower()
            if re.fullmatch(r"[0-9a-f]{64}", expected) and _file_digest(exe) == expected:
                candidates.append((version, exe))
        except (OSError, UnicodeError):
            continue
    return max(candidates, key=lambda item: item[0]) if candidates else None


def _download_release(release: dict[str, Any], version_text: str, asset: dict[str, Any]) -> Path:
    size = asset.get("size")
    if isinstance(size, int) and (size <= 0 or size > MAX_EXE_BYTES):
        raise ValueError("Release executable has an unexpected size")
    expected = _expected_digest(release, asset)
    folder = _cache_root() / version_text
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / ASSET_NAME
    checksum_path = folder / (ASSET_NAME + ".sha256")
    if target.is_file() and checksum_path.is_file():
        try:
            if checksum_path.read_text(encoding="ascii").strip().lower() == expected and _file_digest(target) == expected:
                return target
        except (OSError, UnicodeError):
            pass

    temporary = folder / (ASSET_NAME + ".download")
    request = urllib.request.Request(
        asset["browser_download_url"],
        headers={"User-Agent": f"StarkinsDashboard/{APP_VERSION}", "Accept": "application/octet-stream"},
    )
    digest = hashlib.sha256()
    total = 0
    try:
        with urllib.request.urlopen(request, timeout=30) as response, temporary.open("wb") as output:
            while True:
                block = response.read(1024 * 1024)
                if not block:
                    break
                total += len(block)
                if total > MAX_EXE_BYTES:
                    raise ValueError("Downloaded file exceeded the allowed size")
                digest.update(block)
                output.write(block)
        if total == 0 or digest.hexdigest() != expected:
            raise ValueError("Downloaded file failed SHA-256 verification")
        os.replace(temporary, target)
        checksum_path.write_text(expected, encoding="ascii")
        return target
    except Exception:
        try:
            temporary.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def launch_updated_copy() -> bool:
    """Launch a newer cached/downloaded executable; return whether to exit this process."""
    if not getattr(sys, "frozen", False):
        return False
    current = _version_tuple(APP_VERSION)
    if current is None:
        return False
    cached = _best_cached()
    try:
        release = _release_info()
        latest = _version_tuple(str(release.get("tag_name", "")))
        if latest is None or latest <= current:
            return bool(cached and cached[0] > current and _launch(cached[1]))
        asset = next((item for item in release.get("assets", []) if item.get("name") == ASSET_NAME), None)
        if not asset or not asset.get("browser_download_url"):
            raise ValueError("Release executable was not found")
        executable = _download_release(release, str(release["tag_name"]), asset)
        return _launch(executable)
    except (OSError, ValueError, KeyError, TypeError, json.JSONDecodeError, urllib.error.URLError):
        return bool(cached and cached[0] > current and _launch(cached[1]))

