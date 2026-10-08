import base64
import glob
import json
import os
import random
import re
import shutil
import tempfile
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from common import (
    CONFIG_DIR, _random_headers, sanitize_filename,
    IMAGE_EXTENSIONS, VIDEO_EXTENSIONS,
)

SF_BASE         = "https://sofurry.com"
SF_COOKIES_FILE = CONFIG_DIR / "sf_cookies.json"

# Listing categories that are text — skipped, as Inkbunny and Weasyl skip them.
# The download route would hand back an .epub.
_SF_TEXT_CATEGORIES = {"writing", "story"}

# mkstemp creates files 0600; finished downloads get the normal umask-based mode.
_UMASK = os.umask(0)
os.umask(_UMASK)
_FILE_MODE = 0o666 & ~_UMASK

# sf_sfw is an unsigned cookie holding a base64 JSON string: "1" = SFW mode.
_SFW_ON = base64.b64encode(b'"1"').decode()


def _new_session() -> "requests.Session":
    session = requests.Session()
    session.headers.update(_random_headers())
    # requests can't decode "br" without the optional brotli package.
    session.headers["Accept-Encoding"] = "gzip, deflate"
    return session


def _decode_turbo(text: str):
    """
    Decode a React Router single-fetch (turbo-stream) payload, as served at
    <route>.data. The first line is a flat JSON array: objects are {"_<key
    index>": <value index>}, lists hold value indexes, negative indexes are
    undefined/null. Only the plain-JSON subset is needed here.
    """
    arr  = json.loads(text.split("\n", 1)[0])
    memo: dict = {}

    def val(i):
        if not isinstance(i, int) or i < 0:
            return None
        if i in memo:
            return memo[i]
        v = arr[i]
        if isinstance(v, dict):
            out: dict = {}
            memo[i] = out
            for k, vi in v.items():
                out[arr[int(k[1:])]] = val(vi)
            return out
        if isinstance(v, list):
            if v and isinstance(v[0], str) and len(v[0]) == 1:
                return None          # typed value (Date, Promise, …) — unused here
            out_l: list = []
            memo[i] = out_l
            out_l.extend(val(x) for x in v)
            return out_l
        return v

    return val(0)


def _root_data(session: "requests.Session") -> dict:
    """
    Fetch the root loader data (current user, CSRF token).

    The response may set `_session` twice: the first copy carries the CSRF
    token, the second is a stale copy without it. requests (like a browser)
    keeps the last one, and every later POST then fails with "CSRF token
    missing from session" — so pin the first copy.
    """
    r = session.get(f"{SF_BASE}/_root.data", stream=True, timeout=30)
    r.raise_for_status()
    set_cookies = r.raw._original_response.msg.get_all("Set-Cookie") or []
    body = r.content.decode("utf-8", errors="replace")
    sessions = [h.split(";", 1)[0].split("=", 1)[1] for h in set_cookies if h.startswith("_session=")]
    if len(sessions) > 1:
        session.cookies.set("_session", sessions[0], domain="sofurry.com", path="/")
    return (_decode_turbo(body) or {}).get("root", {}).get("data", {}) or {}


def _current_handle(session: "requests.Session") -> str:
    user = (_root_data(session).get("user") or {}).get("profile") or {}
    return user.get("handle") or ""


def _save_cookies(session: "requests.Session", email: str):
    jar = [
        {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
        for c in session.cookies
        if c.name != "sf_sfw"   # per-run setting, not part of the login
    ]
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    SF_COOKIES_FILE.write_text(json.dumps({"email": email.lower(), "cookies": jar}))
    SF_COOKIES_FILE.chmod(0o600)


def sf_login(
    email: str,
    password: str,
    allow_adult: bool = True,
    log_fn=print,
) -> tuple["requests.Session", str]:
    """
    Sign in to SoFurry. Returns (session, handle).

    SoFurry signs in with an email address, through its own OAuth round-trip:
    /fe/auth/sofurry → /oauth/authorize → /login form → back to /fe/auth/callback.
    Every fresh login posts a "New Login" notification to the account, so the
    cookie jar is saved and reused for as long as it stays valid.
    """
    session = None
    if SF_COOKIES_FILE.exists():
        try:
            saved = json.loads(SF_COOKIES_FILE.read_text())
            if saved.get("email") == email.lower():
                session = _new_session()
                for c in saved["cookies"]:
                    session.cookies.set(c["name"], c["value"], domain=c["domain"], path=c["path"])
                handle = _current_handle(session)
                if handle:
                    log_fn("Resumed previous SoFurry session from saved cookies.")
                    _save_cookies(session, email)   # keep rotated session cookies
                else:
                    session = None
        except Exception:
            session = None

    if session is None:
        session = _new_session()
        r = session.get(f"{SF_BASE}/fe/auth/sofurry", timeout=30)
        r.raise_for_status()
        form = BeautifulSoup(r.text, "lxml").find("form", action=re.compile(r"/login$"))
        token = form.find("input", {"name": "_token"}) if form else None
        if not token:
            raise ValueError("SoFurry login page did not load as expected.")
        r = session.post(
            f"{SF_BASE}/login",
            data={"_token": token.get("value", ""), "email": email, "password": password, "remember": "on"},
            headers={"Origin": SF_BASE, "Referer": r.url},
            timeout=30,
        )
        # Success lands back on the site; a failure re-renders the login form.
        if re.search(r"/(login|two-factor|2fa)", r.url):
            if re.search(r"two-factor|2fa", r.url):
                raise ValueError(
                    "This SoFurry account uses two-factor authentication, which the "
                    "downloader does not support."
                )
            soup = BeautifulSoup(r.text, "lxml")
            err  = soup.find(class_=re.compile(r"invalid-feedback|alert|error"))
            detail = err.get_text(" ", strip=True)[:200] if err else "check the email address and password"
            raise ValueError(f"SoFurry login failed: {detail}")
        handle = _current_handle(session)
        if not handle:
            raise ValueError("SoFurry login failed — the session was not accepted.")
        _save_cookies(session, email)

    # Leave the site's own sf_sfw (the account default) alone unless the user
    # asked for General-only; then force SFW mode for this run.
    if not allow_adult:
        session.cookies.set("sf_sfw", _SFW_ON, domain="sofurry.com", path="/")
    return session, handle


# ── Listing ────────────────────────────────────────────────────────────────────

def _item(sub_id: str, data: dict | None = None, notif_id: str | None = None) -> dict:
    data = data or {}
    return {
        "key":       sub_id,
        "title":     data.get("title", ""),
        "author":    data.get("author", ""),
        "category":  data.get("category", ""),
        "count":     data.get("contentCount") or 1,
        "notif_ids": [notif_id] if notif_id else [],
    }


def _merge(items: list[dict], new: list[dict]) -> int:
    index = {it["key"]: it for it in items}
    added = 0
    for it in new:
        have = index.get(it["key"])
        if have:
            have["notif_ids"].extend(it["notif_ids"])
            continue
        items.append(it)
        index[it["key"]] = it
        added += 1
    return added


def sf_fetch_profile(
    session: "requests.Session",
    handle: str,
    tab: str,                       # "gallery" | "likes"
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[dict]:
    """Page through /api/profile?tab=gallery|likes (0-based pages of 48)."""
    if not (handle or "").strip():
        raise ValueError(f"SoFurry {tab} scan needs a username.")
    label = "Gallery" if tab == "gallery" else "Favourites"

    items: list[dict] = []
    more = False
    for page in range(max_pages):
        if cancel_fn and cancel_fn():
            break
        log_fn(f"[SF] {label} — user='{handle}'  page={page + 1}  endpoint=/api/profile?tab={tab}")
        r = session.get(
            f"{SF_BASE}/api/profile",
            params={"handle": handle, "tab": tab, "page": page, "per_page": 48},
            timeout=30,
        )
        if r.status_code == 404:
            raise ValueError(f"SoFurry user '{handle}' not found.")
        r.raise_for_status()
        data = r.json()
        subs = data.get("submissions")
        if subs is None:
            raise ValueError(f"SoFurry did not return the {tab} of '{handle}' — it may be private.")

        rows = subs.get("data") or []
        _merge(items, [_item(x["id"], x) for x in rows])
        log_fn(f"  Page {page + 1}: {len(rows)} submissions  |  total collected: {len(items)}")

        more = bool(subs.get("hasNextPage"))
        if not rows or not more:
            break
        time.sleep(0.5)
    else:
        if more:
            log_fn(f"  Stopped at the {max_pages}-page limit — raise 'Max pages' for more.")
    return items


def sf_fetch_notifications(
    session: "requests.Session",
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[dict]:
    """
    Unread "new upload" notifications from followed artists:
    /api/notifications?category=uploads, 1-based pages (page 0 repeats page 1).
    Read entries are listed too and are skipped.
    """
    items: list[dict] = []
    more = False
    for page in range(1, max_pages + 1):
        if cancel_fn and cancel_fn():
            break
        log_fn(f"[SF] Submission Notifications — page={page}  endpoint=/api/notifications?category=uploads")
        r = session.get(
            f"{SF_BASE}/api/notifications",
            params={"category": "uploads", "page": page, "per_page": 50},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        rows = data.get("data") or []
        new  = []
        for n in rows:
            m = re.match(r"/s/([A-Za-z0-9]+)", n.get("url") or "")
            if n.get("isRead") or n.get("type") != "SubmissionPublished" or not m:
                continue
            new.append(_item(m.group(1), {
                "title":  (n.get("data") or {}).get("title", ""),
                "author": (n.get("data") or {}).get("handle", ""),
            }, notif_id=n.get("id")))
        _merge(items, new)
        log_fn(f"  Page {page}: {len(new)} new submissions  |  total collected: {len(items)}")

        more = bool(data.get("hasNextPage"))
        if not rows or not more:
            break
        time.sleep(0.5)
    else:
        if more:
            log_fn(f"  Stopped at the {max_pages}-page limit — raise 'Max pages' for more.")
    return items


def sf_fetch_feed(
    session: "requests.Session",
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[dict]:
    """
    Submissions from followed artists — the "Submissions" tab of /feed. There
    is no /api route for it: the page's own loader serves it at
    /feed.data?tab=submissions (turbo-stream), 1-based pages of 100, newest
    first. SoFurry caps it at 100 pages (10,000 submissions).
    """
    items: list[dict] = []
    more = False
    for page in range(1, max_pages + 1):
        if cancel_fn and cancel_fn():
            break
        log_fn(f"[SF] Following Feed — page={page}  endpoint=/feed.data?tab=submissions")
        r = session.get(
            f"{SF_BASE}/feed.data",
            params={"tab": "submissions", "page": page},
            timeout=30,
        )
        r.raise_for_status()
        feed = ((_decode_turbo(r.text) or {}).get("routes/feed") or {}).get("data") or {}
        subs = feed.get("submissions")
        if not isinstance(subs, dict):
            raise ValueError("SoFurry did not return the feed's submissions tab.")

        rows = subs.get("data") or []
        _merge(items, [_item(x["id"], x) for x in rows])
        last = subs.get("lastPage") or page
        log_fn(f"  Page {page}/{last}: {len(rows)} submissions  |  total collected: {len(items)}")

        more = page < last
        if not rows or not more:
            break
        time.sleep(0.5)
    else:
        if more:
            log_fn(f"  Stopped at the {max_pages}-page limit — raise 'Max pages' for more.")
    return items


# ── Download ───────────────────────────────────────────────────────────────────

def _disposition_name(header: str | None) -> str:
    """Filename from a Content-Disposition header (plain or RFC 5987 form)."""
    if not header:
        return ""
    m = re.search(r"filename\*\s*=\s*[^']*''([^;]+)", header)
    if m:
        return unquote(m.group(1).strip())
    m = re.search(r'filename\s*=\s*"([^"]*)"', header) or re.search(r"filename\s*=\s*([^;]+)", header)
    return m.group(1).strip() if m else ""


def _existing(output_dir: str, key: str) -> list[str]:
    pattern = f"{glob.escape(key)}_*"
    found = glob.glob(os.path.join(glob.escape(output_dir), pattern))
    found += glob.glob(os.path.join(glob.escape(output_dir), "video", pattern))
    return [f for f in found if not f.endswith(".part")]


def download_sf_submissions(
    session: "requests.Session",
    items: list[dict],
    output_dir: str,
    log_fn=print,
    error_fn=None,
    cancel_fn=None,
    progress_fn=None,
    file_progress_fn=None,
    preview_fn=None,
    delay_min: float = 1.0,
    delay_max: float = 3.0,
    max_workers: int = 2,
) -> dict:
    """
    Download each submission through /api/submission-download/<id> — the only
    source of the original file. `displayUrl` in the submission JSON is a
    recompressed (and for large images, downscaled) copy and must never be
    saved in its place. A multi-file submission arrives as one ZIP
    (N_content.<ext> + credits.txt); its files are unpacked individually.
    """
    if error_fn is None:
        error_fn = log_fn
    os.makedirs(output_dir, exist_ok=True)

    total   = len(items)
    counter = {"done": 0, "ok": 0, "bytes": 0}
    ok_keys: set[str] = set()
    lock    = threading.Lock()

    if progress_fn:
        progress_fn(0, total)

    def _place(name: str) -> str:
        ext = name.rsplit(".", 1)[-1].lower() if "." in name else ""
        dest_dir = os.path.join(output_dir, "video") if ext in VIDEO_EXTENSIONS else output_dir
        os.makedirs(dest_dir, exist_ok=True)
        return os.path.join(dest_dir, name)

    def _name(key: str, stem: str, ext: str, suffix: str = "") -> str:
        return f"{key}_{sanitize_filename(stem)}"[:180] + suffix + f".{ext.lower()[:10]}"

    def _process(item: dict):
        if cancel_fn and cancel_fn():
            return
        key = item["key"]
        try:
            title  = item.get("title") or key
            author = item.get("author", "")
            if item.get("category") in _SF_TEXT_CATEGORIES:
                log_fn(f"[SF {key}] '{title}' by {author} — SKIP (category={item['category']})")
                return

            have = _existing(output_dir, key)
            if have and len(have) >= item.get("count", 1):
                log_fn(f"[SF {key}] Skipped (exists): {os.path.basename(have[0])}"
                       + (f" +{len(have) - 1} more" if len(have) > 1 else ""))
                with lock:
                    ok_keys.add(key)
                return

            r = session.get(f"{SF_BASE}/api/submission-download/{key}", stream=True, timeout=120)
            if r.status_code == 403 and "json" in (r.headers.get("Content-Type") or ""):
                msg = (r.json() or {}).get("error", "forbidden")
                log_fn(f"[SF {key}] '{title}' by {author} — SKIP ({msg})")
                return
            r.raise_for_status()

            ctype = (r.headers.get("Content-Type") or "").split(";")[0].strip()
            if ctype == "application/epub+zip":
                r.close()
                log_fn(f"[SF {key}] '{title}' by {author} — SKIP (writing)")
                return

            fname_hdr = _disposition_name(r.headers.get("Content-Disposition")) or f"{title}.bin"
            stem, dot, ext = fname_hdr.rpartition(".")
            if not dot:
                stem, ext = fname_hdr, "bin"

            total_b = int(r.headers.get("Content-Length", 0))
            label   = _name(key, stem, ext)
            log_fn(f"[SF {key}] Downloading: {label}  (by {author}, '{title}')")

            # Stream to a temp file first; a failed download never leaves a
            # partial file that would later be mistaken for a complete one.
            fd, tmp = tempfile.mkstemp(prefix=f"{key}_", suffix=".part", dir=output_dir)
            written = 0
            try:
                with os.fdopen(fd, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=65536):
                        if chunk:
                            fh.write(chunk)
                            written += len(chunk)
                            if file_progress_fn:
                                file_progress_fn(label, written, total_b)

                saved: list[str] = []
                if ctype == "application/zip" or ext.lower() == "zip":
                    with zipfile.ZipFile(tmp) as z:
                        members = sorted(
                            (m for m in z.infolist() if re.match(r"\d+_content\.", m.filename)),
                            key=lambda m: int(m.filename.split("_", 1)[0]),
                        )
                        if not members:
                            raise ValueError("ZIP held no submission files")
                        for m in members:
                            idx   = int(m.filename.split("_", 1)[0]) + 1
                            m_ext = m.filename.rsplit(".", 1)[-1]
                            dest  = _place(_name(key, stem, m_ext, f"_{idx:02d}"))
                            part  = dest + ".part"
                            with z.open(m) as src, open(part, "wb") as dst:
                                shutil.copyfileobj(src, dst)
                            os.replace(part, dest)
                            saved.append(dest)
                else:
                    dest = _place(label)
                    os.chmod(tmp, _FILE_MODE)
                    os.replace(tmp, dest)
                    saved.append(dest)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

            size_str = (
                f"{written / 1_048_576:.1f} MB" if written >= 1_048_576
                else f"{written / 1024:.1f} KB"
            )
            log_fn(f"[SF {key}] Saved: {os.path.basename(saved[0])}"
                   + (f" +{len(saved) - 1} more" if len(saved) > 1 else "") + f"  ({size_str})")
            with lock:
                counter["ok"]    += len(saved)
                counter["bytes"] += written
                ok_keys.add(key)
            if preview_fn:
                for path in saved:
                    if path.rsplit(".", 1)[-1].lower() in IMAGE_EXTENSIONS:
                        preview_fn(path)
                        break
        except Exception as exc:
            error_fn(f"[SF {key}] {exc}")
        finally:
            with lock:
                counter["done"] += 1
                done_now = counter["done"]
            if progress_fn:
                progress_fn(done_now, total)
            time.sleep(random.uniform(delay_min, delay_max))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_process, it) for it in items]
        for fut in as_completed(futs):
            if cancel_fn and cancel_fn():
                for rem in futs:
                    rem.cancel()
                break
            fut.result()

    return {
        "images":   counter["ok"],
        "bytes":    counter["bytes"],
        "done_ids": sorted(ok_keys),
    }


def sf_clear_notifications(
    session: "requests.Session",
    notif_ids: list[str],
    log_fn=print,
    cancel_fn=None,
) -> int:
    """Mark each notification read (POST /api/notifications-read/<id>), as
    clicking it on the site does. Returns the count marked."""
    if not notif_ids:
        return 0
    token = _root_data(session).get("csrfToken") or ""
    cleared = 0
    for n, nid in enumerate(notif_ids, start=1):
        if cancel_fn and cancel_fn():
            break
        try:
            r = session.post(
                f"{SF_BASE}/api/notifications-read/{nid}",
                headers={"X-CSRF-Token": token, "Origin": SF_BASE},
                timeout=30,
            )
            r.raise_for_status()
            cleared += 1
        except Exception as exc:
            log_fn(f"  Failed to mark notification {n} as read: {exc}")
        if n % 10 == 0 or n == len(notif_ids):
            log_fn(f"  Marked {cleared}/{n} notifications as read.")
        time.sleep(random.uniform(0.3, 0.8))
    # SoFurry has no delete for notifications (only read one / read all), so
    # they stay listed on the site — but the next run skips read ones.
    if cleared:
        log_fn("  SoFurry keeps read notifications in its list (it has no delete); "
               "they will not be downloaded again.")
    return cleared
