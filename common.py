import os
import re
import random
import configparser
from pathlib import Path

import requests
from cryptography.fernet import Fernet, InvalidToken

VERSION = "1.2.0"
GITHUB_REPO = "Tamalero/furaffinity-inkbunny-downloader"
GITHUB_RELEASES_URL = f"https://github.com/{GITHUB_REPO}/releases"

# ── XDG config paths ───────────────────────────────────────────────────────────
_cfg_home            = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config"))
CONFIG_DIR           = _cfg_home / "faibdownloader"
CONFIG_FILE          = CONFIG_DIR / "config.ini"
KEY_FILE             = CONFIG_DIR / "secret.key"
DEFAULT_DOWNLOAD_DIR = str(Path.home() / "Pictures" / "FAIBDownload")

SITES = ["FurAffinity", "Inkbunny"]

IMAGE_EXTENSIONS = {
    "jpg", "jpeg", "png", "gif", "webp", "bmp",
    "tiff", "tif", "avif",
}

VIDEO_EXTENSIONS = {
    "mp4", "webm", "mov", "avi", "mkv", "flv", "swf", "wmv", "m4v",
}

USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_4_1) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14.4; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (X11; Linux x86_64; rv:125.0) Gecko/20100101 Firefox/125.0",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36 Edg/123.0.0.0",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 13_6) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.6 Safari/605.1.15",
]


def _random_headers() -> dict:
    return {
        "User-Agent":                random.choice(USER_AGENTS),
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.5",
        "Accept-Encoding":           "gzip, deflate, br",
        "DNT":                       "1",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


def sanitize_filename(text: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", text).strip(" .")


# ── Config / encryption ────────────────────────────────────────────────────────

def _get_or_create_key() -> bytes:
    if KEY_FILE.exists():
        return KEY_FILE.read_bytes()
    key = Fernet.generate_key()
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    KEY_FILE.write_bytes(key)
    KEY_FILE.chmod(0o600)
    return key


def _encrypt(value: str) -> str:
    return Fernet(_get_or_create_key()).encrypt(value.encode()).decode()


def _decrypt(token: str) -> str | None:
    try:
        return Fernet(_get_or_create_key()).decrypt(token.encode()).decode()
    except (InvalidToken, Exception):
        return None if token.startswith("gAAAAA") else token


def _site_section(site: str) -> str:
    slug = site.lower().replace(" ", "_")
    return f"credentials_{slug}"


def load_config() -> configparser.ConfigParser:
    cfg = configparser.ConfigParser()
    if CONFIG_FILE.exists():
        cfg.read(CONFIG_FILE)
    return cfg


def save_config(site: str, username: str, password: str):
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    cfg = load_config()
    sec = _site_section(site)
    if not cfg.has_section(sec):
        cfg.add_section(sec)
    cfg.set(sec, "username", username)
    cfg.set(sec, "password", _encrypt(password))
    with open(CONFIG_FILE, "w") as fh:
        cfg.write(fh)


def get_credentials(cfg: configparser.ConfigParser, site: str) -> tuple[str, str | None]:
    sec      = _site_section(site)
    username = cfg.get(sec, "username", fallback="")
    raw_pw   = cfg.get(sec, "password",  fallback=None)
    password = _decrypt(raw_pw) if raw_pw else None
    return username, password


def save_ui_state(state: dict):
    cfg = load_config()
    if not cfg.has_section("last_run"):
        cfg.add_section("last_run")
    for k, v in state.items():
        cfg.set("last_run", k, str(v))
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as fh:
        cfg.write(fh)


# ── Update check ───────────────────────────────────────────────────────────────

def _version_tuple(v: str) -> tuple[int, ...]:
    return tuple(int(x) for x in v.split(".") if x.isdigit())


def check_for_updates() -> tuple[bool, str]:
    """Query GitHub releases API for a newer version. Returns (available, latest_tag).
    Silently returns (False, '') on any network or parse error."""
    try:
        import urllib.request
        import json
        url = f"https://api.github.com/repos/{GITHUB_REPO}/releases/latest"
        req = urllib.request.Request(
            url, headers={"User-Agent": f"faib-downloader/{VERSION}"}
        )
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
        tag = data.get("tag_name", "").lstrip("v")
        if not tag:
            return False, ""
        return _version_tuple(tag) > _version_tuple(VERSION), tag
    except Exception:
        return False, ""


# ── Download primitive ─────────────────────────────────────────────────────────

def _stream_download(
    url: str,
    dest: str,
    session: "requests.Session | None" = None,
    fprog_fn=None,
) -> int:
    """Stream-download url → dest. Returns bytes written.
    Writes to a .part file and renames atomically on success so a failed
    download never leaves a corrupt file that would be mistaken for complete."""
    kwargs: dict = dict(stream=True, timeout=120)
    r = session.get(url, **kwargs) if session else requests.get(url, headers=_random_headers(), **kwargs)
    r.raise_for_status()

    total = int(r.headers.get("Content-Length", 0))
    fname = os.path.basename(dest)
    if fprog_fn:
        fprog_fn(fname, 0, total)

    tmp = dest + ".part"
    try:
        written = 0
        with open(tmp, "wb") as fh:
            for chunk in r.iter_content(chunk_size=65536):
                if chunk:
                    fh.write(chunk)
                    written += len(chunk)
                    if fprog_fn:
                        fprog_fn(fname, written, total)
        os.replace(tmp, dest)   # atomic on POSIX; overwrites dest if it exists
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return written
