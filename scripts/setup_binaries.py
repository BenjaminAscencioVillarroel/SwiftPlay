"""Descarga libmpv-2.dll, ffmpeg.exe y ffprobe.exe en ./bin/."""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import tempfile
import urllib.request
import zipfile
from pathlib import Path

ROOT_DIR = Path(__file__).resolve().parent.parent
BIN_DIR = ROOT_DIR / "bin"
USER_AGENT = "swiftplay-setup/0.1"


def _fetch_json(url: str) -> dict:
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _download_file(url: str, dest: Path) -> None:
    print(f"Descargando {url} -> {dest.name}...")
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(req, timeout=180) as resp, dest.open("wb") as fh:
        shutil.copyfileobj(resp, fh, length=1024 * 256)


def setup_libmpv(force: bool = False) -> Path:
    target = BIN_DIR / "libmpv-2.dll"
    if target.exists() and not force:
        print(f"OK (ya existe): {target}")
        return target

    release = _fetch_json(
        "https://api.github.com/repos/shinchiro/mpv-winbuild-cmake/releases/latest"
    )
    pattern = re.compile(r"^mpv-dev-x86_64-\d+.*\.7z$")
    asset = next(
        (a for a in release.get("assets", []) if pattern.match(a["name"])),
        None,
    )
    if asset is None:
        raise RuntimeError(
            "No se encontró el asset mpv-dev-x86_64-*.7z en el último release."
        )

    with tempfile.TemporaryDirectory(prefix="swiftplay_mpv_") as tmp:
        tmp_path = Path(tmp)
        archive_path = tmp_path / asset["name"]
        extract_dir = tmp_path / "extracted"
        extract_dir.mkdir(parents=True, exist_ok=True)

        _download_file(asset["browser_download_url"], archive_path)
        print("Extrayendo libmpv-2.dll con tar...")
        subprocess.run(
            ["tar", "-xf", str(archive_path), "-C", str(extract_dir)],
            check=True,
        )

        matches = list(extract_dir.rglob("libmpv-2.dll"))
        if not matches:
            matches = list(extract_dir.rglob("*mpv*.dll"))
        if not matches:
            raise FileNotFoundError("No se encontró libmpv-2.dll dentro del archivo.")

        BIN_DIR.mkdir(parents=True, exist_ok=True)
        shutil.copy2(matches[0], target)
        print(f"Instalado: {target} ({target.stat().st_size // 1024} KB)")
    return target


def setup_ffmpeg(force: bool = False) -> tuple[Path, Path]:
    ffmpeg_target = BIN_DIR / "ffmpeg.exe"
    ffprobe_target = BIN_DIR / "ffprobe.exe"
    if ffmpeg_target.exists() and ffprobe_target.exists() and not force:
        print(f"OK (ya existen): {ffmpeg_target}, {ffprobe_target}")
        return ffmpeg_target, ffprobe_target

    release = _fetch_json(
        "https://api.github.com/repos/BtbN/FFmpeg-Builds/releases/latest"
    )
    asset = next(
        (
            a
            for a in release.get("assets", [])
            if a["name"] == "ffmpeg-master-latest-win64-gpl.zip"
        ),
        None,
    )
    if asset is None:
        raise RuntimeError(
            "No se encontró ffmpeg-master-latest-win64-gpl.zip en el último release."
        )

    with tempfile.TemporaryDirectory(prefix="swiftplay_ffmpeg_") as tmp:
        tmp_path = Path(tmp)
        zip_path = tmp_path / asset["name"]
        _download_file(asset["browser_download_url"], zip_path)

        BIN_DIR.mkdir(parents=True, exist_ok=True)
        print("Extrayendo ffmpeg.exe y ffprobe.exe...")
        found = set()
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.infolist():
                filename = Path(member.filename).name
                if filename in {"ffmpeg.exe", "ffprobe.exe"} and not member.is_dir():
                    dest = BIN_DIR / filename
                    with zf.open(member) as src, dest.open("wb") as out:
                        shutil.copyfileobj(src, out, length=1024 * 256)
                    found.add(filename)
                    print(f"Instalado: {dest} ({dest.stat().st_size // 1024} KB)")

        if found != {"ffmpeg.exe", "ffprobe.exe"}:
            raise FileNotFoundError(
                f"Faltaron binarios en el zip de FFmpeg. Extraídos: {found}"
            )
    return ffmpeg_target, ffprobe_target


def main() -> None:
    parser = argparse.ArgumentParser(description="Descarga binarios nativos de SwiftPlay")
    parser.add_argument("--force", action="store_true", help="Re-descargar aunque existan")
    args = parser.parse_args()
    BIN_DIR.mkdir(parents=True, exist_ok=True)
    setup_libmpv(force=args.force)
    setup_ffmpeg(force=args.force)
    print("Binarios listos en:", BIN_DIR)


if __name__ == "__main__":
    main()
