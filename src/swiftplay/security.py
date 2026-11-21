"""Almacenamiento seguro local de sesión YouTube usando Windows DPAPI."""

from __future__ import annotations

import ctypes
import json
from ctypes import wintypes
from dataclasses import asdict
from pathlib import Path

from swiftplay.models import AccountProfile
from swiftplay.paths import ACCOUNT_PROFILE_PATH, DISCORD_TOKEN_PATH, SESSION_PATH

ALLOWED_COOKIE_DOMAINS: frozenset[str] = frozenset(
    {
        ".youtube.com",
        "youtube.com",
        "www.youtube.com",
        "music.youtube.com",
        ".youtu.be",
        "youtu.be",
    }
)

_CRYPTPROTECT_UI_FORBIDDEN = 0x1


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_ubyte)),
    ]


def _dpapi_protect(plaintext: bytes) -> bytes:
    if not plaintext:
        return b""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    buf = (ctypes.c_ubyte * len(plaintext)).from_buffer_copy(plaintext)
    in_blob = _DATA_BLOB(len(plaintext), buf)
    out_blob = _DATA_BLOB()

    ok = crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        "SwiftPlaySession",
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _dpapi_unprotect(ciphertext: bytes) -> bytes:
    if not ciphertext:
        return b""
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32

    buf = (ctypes.c_ubyte * len(ciphertext)).from_buffer_copy(ciphertext)
    in_blob = _DATA_BLOB(len(ciphertext), buf)
    out_blob = _DATA_BLOB()

    ok = crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        _CRYPTPROTECT_UI_FORBIDDEN,
        ctypes.byref(out_blob),
    )
    if not ok:
        raise ctypes.WinError()
    try:
        return ctypes.string_at(out_blob.pbData, out_blob.cbData)
    finally:
        kernel32.LocalFree(out_blob.pbData)


def _is_allowed_domain(domain: str) -> bool:
    d = domain.strip().lower()
    return d in ALLOWED_COOKIE_DOMAINS


def filter_youtube_netscape_cookies(raw_text: str) -> str:
    """
    Filtra cookies para conservar SOLO dominios de YouTube / YouTube Music.
    Acepta tanto formato Netscape (cookies.txt) como encabezado HTTP 'Cookie: k=v; ...'.
    """
    text = raw_text.strip()
    if not text:
        return ""

    kept_lines: list[str] = []
    seen_keys: set[tuple[str, str]] = set()

    for line in text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) >= 7:
            domain = parts[0].strip().lower()
            if _is_allowed_domain(domain):
                name = parts[5].strip()
                key = (domain, name)
                if name and key not in seen_keys:
                    seen_keys.add(key)
                    kept_lines.append("\t".join(parts[:7]))

    # Fallback: si no era formato Netscape pero parece un header Cookie: a=b; c=d
    if not kept_lines and "=" in text and "\t" not in text:
        header = text
        if header.lower().startswith("cookie:"):
            header = header.split(":", 1)[1].strip()
        for pair in header.split(";"):
            pair = pair.strip()
            if "=" not in pair:
                continue
            name, val = pair.split("=", 1)
            name = name.strip()
            val = val.strip()
            if not name or not val:
                continue
            key = (".youtube.com", name)
            if key not in seen_keys:
                seen_keys.add(key)
                kept_lines.append(f".youtube.com\tTRUE\t/\tTRUE\t0\t{name}\t{val}")

    if not kept_lines:
        return ""

    return "# Netscape HTTP Cookie File\n" + "\n".join(kept_lines) + "\n"


def count_cookie_lines(netscape_text: str) -> int:
    return sum(
        1
        for line in netscape_text.splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    )


def save_cookies_encrypted(raw_text: str, session_path: Path = SESSION_PATH) -> int:
    filtered = filter_youtube_netscape_cookies(raw_text)
    count = count_cookie_lines(filtered)
    if count == 0:
        raise ValueError(
            "No se encontraron cookies válidas de YouTube (.youtube.com / music.youtube.com)."
        )

    encrypted = _dpapi_protect(filtered.encode("utf-8"))
    session_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = session_path.with_suffix(".dpapi.tmp")
    tmp_path.write_bytes(encrypted)
    tmp_path.replace(session_path)
    return count


def load_cookies_decrypted(session_path: Path = SESSION_PATH) -> str | None:
    if not session_path.exists():
        return None
    try:
        blob = session_path.read_bytes()
        if not blob:
            return None
        decrypted = _dpapi_unprotect(blob).decode("utf-8")
        filtered = filter_youtube_netscape_cookies(decrypted)
        return filtered or None
    except Exception:
        return None


def has_saved_session(session_path: Path = SESSION_PATH) -> bool:
    return session_path.exists() and session_path.stat().st_size > 0


def clear_credentials(
    session_path: Path = SESSION_PATH,
    profile_path: Path = ACCOUNT_PROFILE_PATH,
) -> None:
    if session_path.exists():
        try:
            size = session_path.stat().st_size
            if size > 0:
                session_path.write_bytes(b"\x00" * size)
        finally:
            session_path.unlink(missing_ok=True)
    clear_account_profile(profile_path)


def netscape_to_cookie_dict(netscape_text: str) -> dict[str, str]:
    """Extrae pares nombre->valor de texto de cookies en formato Netscape."""
    cookies: dict[str, str] = {}
    for line in netscape_text.splitlines():
        s = line.strip()
        if not s or s.startswith("#"):
            continue
        parts = line.rstrip("\r\n").split("\t")
        if len(parts) >= 7:
            domain = parts[0].strip().lower()
            name = parts[5].strip()
            val = parts[6].strip()
            if name and _is_allowed_domain(domain):
                # Priorizar .youtube.com si hay colisión de nombres
                if name not in cookies or domain == ".youtube.com":
                    cookies[name] = val
    return cookies


def netscape_to_cookie_header(netscape_text: str) -> str:
    """Convierte cookies Netscape en una cadena de encabezado HTTP Cookie."""
    pairs = netscape_to_cookie_dict(netscape_text)
    return "; ".join(f"{k}={v}" for k, v in pairs.items())


def save_account_profile(
    profile: AccountProfile, profile_path: Path = ACCOUNT_PROFILE_PATH
) -> None:
    profile_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = profile_path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps(asdict(profile), indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    tmp.replace(profile_path)


def load_account_profile(
    profile_path: Path = ACCOUNT_PROFILE_PATH,
) -> AccountProfile | None:
    if not profile_path.exists():
        return None
    try:
        raw = json.loads(profile_path.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            return None
        return AccountProfile(
            name=str(raw.get("name") or ""),
            handle=str(raw.get("handle") or ""),
            avatar_url=str(raw.get("avatar_url") or ""),
        )
    except Exception:
        return None


def clear_account_profile(profile_path: Path = ACCOUNT_PROFILE_PATH) -> None:
    try:
        profile_path.unlink(missing_ok=True)
    except Exception:
        pass


def normalize_discord_token(raw_token: str) -> str:
    """Limpia espacios y prefijo 'Bot ' del token de bot oficial de Discord."""
    token = (raw_token or "").strip()
    if token[:4].lower() == "bot ":
        token = token[4:].strip()
    if not token or len(token) < 20 or any(ch.isspace() for ch in token):
        raise ValueError(
            "El token del bot no es válido. Pega el Bot Token completo del Discord Developer Portal."
        )
    return token


def save_discord_token_encrypted(
    raw_token: str, token_path: Path = DISCORD_TOKEN_PATH
) -> None:
    """Cifra el Discord Bot Token con Windows DPAPI y lo guarda atómicamente."""
    token = normalize_discord_token(raw_token)
    encrypted = _dpapi_protect(token.encode("utf-8"))
    token_path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = token_path.with_suffix(".dpapi.tmp")
    tmp_path.write_bytes(encrypted)
    tmp_path.replace(token_path)


def load_discord_token_decrypted(token_path: Path = DISCORD_TOKEN_PATH) -> str | None:
    """Descifra el Discord Bot Token almacenado con Windows DPAPI."""
    if not token_path.exists():
        return None
    try:
        blob = token_path.read_bytes()
        if not blob:
            return None
        decrypted = _dpapi_unprotect(blob).decode("utf-8")
        return normalize_discord_token(decrypted)
    except Exception:
        return None


def has_discord_token(token_path: Path = DISCORD_TOKEN_PATH) -> bool:
    return token_path.exists() and token_path.stat().st_size > 0


def clear_discord_token(token_path: Path = DISCORD_TOKEN_PATH) -> None:
    """Sobrescribe con ceros y elimina el Discord Bot Token cifrado."""
    if token_path.exists():
        try:
            size = token_path.stat().st_size
            if size > 0:
                token_path.write_bytes(b"\x00" * size)
        finally:
            token_path.unlink(missing_ok=True)
