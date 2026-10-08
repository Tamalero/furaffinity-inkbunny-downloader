import json
import os
import random
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.parse import unquote

import requests
from bs4 import BeautifulSoup

from common import (
    CONFIG_DIR, _random_headers, _stream_download, sanitize_filename,
    IMAGE_EXTENSIONS, VIDEO_EXTENSIONS,
)

WZ_BASE         = "https://www.weasyl.com"
WZ_COOKIES_FILE = CONFIG_DIR / "wz_cookies.json"

# Weasyl rejects any POST whose Origin header isn't its own (CSRF check) with a
# bare 403 — the sign-in form included.
_WZ_POST_HEADERS = {"Origin": WZ_BASE, "Referer": f"{WZ_BASE}/"}

# Weasyl submission subtypes that are text — skipped, as Inkbunny skips stories.
_WZ_TEXT_SUBTYPES = {"literary"}


def _new_session(allow_adult: bool) -> "requests.Session":
    session = requests.Session()
    session.headers.update(_random_headers())
    # Weasyl honours "br" for anything sizeable, and requests can only decode it
    # when the optional brotli package is installed — without it, every listing
    # came back as compressed bytes and failed to parse as JSON.
    session.headers["Accept-Encoding"] = "gzip, deflate"
    # Weasyl shows General-only content to any request carrying sfwmode=sfw, API
    # calls included; without the cookie it uses the account's own rating limit.
    if not allow_adult:
        session.cookies.set("sfwmode", "sfw", domain="www.weasyl.com")
    return session


def _whoami(session: "requests.Session") -> str:
    """The logged-in account's login name, or "" if the session is not signed in."""
    r = session.get(f"{WZ_BASE}/api/whoami", timeout=15)
    if r.status_code == 401:
        return ""
    r.raise_for_status()
    return r.json().get("login", "")


def _save_cookies(session: "requests.Session"):
    wzl = session.cookies.get("WZL", domain="www.weasyl.com") or session.cookies.get("WZL")
    if not wzl:
        return
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    WZ_COOKIES_FILE.write_text(json.dumps({"WZL": wzl}))
    WZ_COOKIES_FILE.chmod(0o600)


def _login_matches(login: str, username: str) -> bool:
    # Weasyl login names are the display name lower-cased with non-alphanumerics
    # removed, so "Some_User" signs in as "someuser".
    return login == re.sub(r"[^a-z0-9]", "", username.lower())


def wz_login(
    username: str,
    password: str,
    allow_adult: bool = True,
    log_fn=print,
) -> tuple["requests.Session", str]:
    """
    Sign in to Weasyl. Returns (session, login_name).

    Reuses the saved WZL cookie when it still belongs to `username`; otherwise
    posts the sign-in form (no captcha on it) and saves the new cookie.
    Raises ValueError on bad credentials or when the account uses 2FA.
    """
    session = _new_session(allow_adult)

    if WZ_COOKIES_FILE.exists():
        try:
            saved = json.loads(WZ_COOKIES_FILE.read_text())
            session.cookies.set("WZL", saved["WZL"], domain="www.weasyl.com")
            login = _whoami(session)
            if login and _login_matches(login, username):
                log_fn("Resumed previous Weasyl session from saved cookies.")
                return session, login
        except Exception:
            pass
        session = _new_session(allow_adult)

    r = session.post(
        f"{WZ_BASE}/signin",
        data={"username": username, "password": password, "referer": "/"},
        headers=_WZ_POST_HEADERS,
        allow_redirects=False,
        timeout=20,
    )
    # Success is a 303 that sets WZL. Every failure — wrong password, 2FA prompt,
    # ban — comes back as a rendered page instead.
    if r.status_code != 303 or not session.cookies.get("WZL"):
        if "2fa" in r.text.lower() and "authenticat" in r.text.lower():
            raise ValueError(
                "This Weasyl account uses two-factor authentication, which the "
                "downloader does not support."
            )
        soup = BeautifulSoup(r.text, "lxml")
        err  = soup.find(id="error_content") or soup.find(class_=re.compile(r"error"))
        detail = err.get_text(" ", strip=True)[:200] if err else f"HTTP {r.status_code}"
        raise ValueError(f"Weasyl login failed: {detail}")

    login = _whoami(session)
    if not login:
        raise ValueError("Weasyl login failed — the session was not accepted.")
    _save_cookies(session)
    return session, login


# ── Listing ────────────────────────────────────────────────────────────────────

def _item_from_api(sub: dict, welcomeid=None) -> dict:
    """Normalise a submission/character object from the Weasyl API."""
    is_char = sub.get("type") == "character"
    num     = sub.get("charid") if is_char else sub.get("submitid")
    return {
        # Characters number separately from submissions, so the key carries a
        # prefix to keep the two from sharing a filename.
        "key":        f"c{num}" if is_char else str(num),
        "kind":       "characters" if is_char else "submissions",
        "num":        num,
        "title":      sub.get("title", ""),
        "owner":      sub.get("owner_login") or sub.get("owner", ""),
        "subtype":    sub.get("subtype", ""),
        "rating":     sub.get("rating", ""),
        "media":      sub.get("media"),
        "welcomeids": [welcomeid] if welcomeid is not None else [],
    }


def _merge(items: list[dict], new: list[dict]) -> int:
    """Append new items, folding duplicates (same key) into the existing entry.
    Returns how many were genuinely new."""
    index = {it["key"]: it for it in items}
    added = 0
    for it in new:
        have = index.get(it["key"])
        if have:
            have["welcomeids"].extend(it["welcomeids"])
            continue
        items.append(it)
        index[it["key"]] = it
        added += 1
    return added


def _api_get(session: "requests.Session", path: str, params: dict | None = None) -> dict:
    r = session.get(f"{WZ_BASE}{path}", params=params, timeout=30)
    if r.status_code == 404:
        raise ValueError(f"Weasyl: not found ({path})")
    r.raise_for_status()
    return r.json()


def wz_fetch_gallery(
    session: "requests.Session",
    username: str,
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[dict]:
    """Page through /api/users/<login>/gallery (100 per page, `nextid` cursor)."""
    if not (username or "").strip():
        raise ValueError("Weasyl gallery scan needs a username.")

    items: list[dict] = []
    nextid = None
    for page in range(1, max_pages + 1):
        if cancel_fn and cancel_fn():
            break
        params = {"count": 100}
        if nextid:
            params["nextid"] = nextid
        log_fn(f"[WZ] Gallery — user='{username}'  page={page}  endpoint=/api/users/…/gallery")
        data = _api_get(session, f"/api/users/{username}/gallery", params)

        subs = data.get("submissions", [])
        _merge(items, [_item_from_api(s) for s in subs])
        log_fn(f"  Page {page}: {len(subs)} submissions  |  total collected: {len(items)}")

        nextid = data.get("nextid")
        if not subs or not nextid:
            break
        time.sleep(0.5)
    else:
        if nextid:
            log_fn(f"  Stopped at the {max_pages}-page limit — raise 'Max pages' for more.")
    return items


def wz_fetch_notifications(
    session: "requests.Session",
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[dict]:
    """Page through /api/messages/submissions (100 per page, `nexttime` cursor).
    Each item keeps the inbox `welcomeid`s needed to clear it afterwards."""
    items: list[dict] = []
    nexttime = None
    for page in range(1, max_pages + 1):
        if cancel_fn and cancel_fn():
            break
        params = {"count": 100}
        if nexttime:
            params["nexttime"] = nexttime
        log_fn(f"[WZ] Submission Notifications — page={page}  endpoint=/api/messages/submissions")
        data = _api_get(session, "/api/messages/submissions", params)

        subs = data.get("submissions", [])
        _merge(items, [_item_from_api(s, s.get("welcomeid")) for s in subs])
        log_fn(f"  Page {page}: {len(subs)} new submissions  |  total collected: {len(items)}")

        nexttime = data.get("nexttime")
        if not subs or not nexttime:
            break
        time.sleep(0.5)
    else:
        if nexttime:
            log_fn(f"  Stopped at the {max_pages}-page limit — raise 'Max pages' for more.")
    return items


def wz_fetch_favourites(
    session: "requests.Session",
    username: str,
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[dict]:
    """
    Scrape a user's favourite submissions. The API has no favourites endpoint, so:
    /favorites/<login> → numeric userid → /favorites?userid=N&feature=submit,
    60 per page, following <a rel="next">. Items carry no media; the download
    step fetches each one from /api/submissions/<id>/view.
    """
    if not (username or "").strip():
        raise ValueError("Weasyl favourites scan needs a username.")

    r = session.get(f"{WZ_BASE}/favorites/{username}", timeout=30)
    if r.status_code == 404:
        raise ValueError(f"Weasyl user '{username}' not found.")
    r.raise_for_status()
    m = re.search(r"/favorites\?userid=(\d+)&(?:amp;)?feature=submit", r.text)
    if not m:
        raise ValueError(
            f"Could not find the submission favourites of '{username}' — "
            f"they may be hidden by the user."
        )
    userid = m.group(1)
    log_fn(f"[WZ] Resolved '{username}' → userid={userid}")

    items: list[dict] = []
    url: str | None = f"{WZ_BASE}/favorites?userid={userid}&feature=submit"
    for page in range(1, max_pages + 1):
        if cancel_fn and cancel_fn():
            break
        log_fn(f"[WZ] Favourites — user='{username}'  page={page}")
        r = session.get(url, timeout=30)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        # Only the favourites grid — the page chrome links to other submissions.
        grid = soup.find(id="favorites-content")
        ids: list[str] = []
        if grid:
            for a in grid.find_all("a", href=re.compile(r"^/~[^/]+/submissions/\d+")):
                sid = re.search(r"/submissions/(\d+)", a["href"]).group(1)
                if sid not in ids:
                    ids.append(sid)
        new = [{
            "key": sid, "kind": "submissions", "num": int(sid), "title": "", "owner": "",
            "subtype": "", "rating": "", "media": None, "welcomeids": [],
        } for sid in ids]
        _merge(items, new)
        log_fn(f"  Page {page}: {len(ids)} favourites  |  total collected: {len(items)}")

        nxt = soup.find("a", rel="next")
        url = f"{WZ_BASE}{nxt['href']}" if nxt and nxt.get("href") else None
        if not ids or not url:
            break
        time.sleep(0.5)
    else:
        if url:
            log_fn(f"  Stopped at the {max_pages}-page limit — raise 'Max pages' for more.")
    return items


# ── Download ───────────────────────────────────────────────────────────────────

def _resolve(session: "requests.Session", item: dict) -> dict:
    """Fill in the full-file media for an item listed without it. Favourites are
    scraped as bare IDs, and listings carry only thumbnails for characters.
    /api/characters/<id>/view is undocumented but is in Weasyl's routes."""
    if (item.get("media") or {}).get("submission"):
        return item
    data = _api_get(session, f"/api/{item['kind']}/{item['num']}/view")
    full = _item_from_api(data)
    full["welcomeids"] = item["welcomeids"]
    return full


def download_wz_submissions(
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
    if error_fn is None:
        error_fn = log_fn
    os.makedirs(output_dir, exist_ok=True)

    total   = len(items)
    counter = {"done": 0, "ok": 0, "bytes": 0}
    ok_keys: set[str] = set()
    lock    = threading.Lock()

    if progress_fn:
        progress_fn(0, total)

    def _process(item: dict):
        if cancel_fn and cancel_fn():
            return
        key = item["key"]
        try:
            # Listings already carry the subtype — skip text without a lookup.
            if item.get("subtype") not in _WZ_TEXT_SUBTYPES:
                item = _resolve(session, item)
            title = item.get("title") or key
            owner = item.get("owner", "")

            if item.get("subtype") in _WZ_TEXT_SUBTYPES:
                log_fn(f"[WZ {key}] '{title}' by {owner} — SKIP (subtype={item['subtype']})")
                return
            # Only the "submission" entry is the real file. "cover" and the
            # thumbnails are scaled previews and must never be saved in its place.
            files = (item.get("media") or {}).get("submission") or []
            if not files or not files[0].get("url"):
                log_fn(f"[WZ {key}] '{title}' by {owner} — SKIP (no downloadable file; embedded media?)")
                return
            url = files[0]["url"]

            # The CDN name is already "<artist>-<title-slug>.<ext>".
            orig = unquote(url.split("?")[0].rsplit("/", 1)[-1])
            stem, dot, ext = orig.rpartition(".")
            if not dot:
                stem, ext = orig, "bin"
            ext      = ext.lower()[:10]
            fname    = f"{key}_{sanitize_filename(stem)}"[:190] + f".{ext}"
            dest_dir = os.path.join(output_dir, "video") if ext in VIDEO_EXTENSIONS else output_dir
            fpath    = os.path.join(dest_dir, fname)

            if os.path.exists(fpath):
                log_fn(f"[WZ {key}] Skipped (exists): {fname}")
                with lock:
                    ok_keys.add(key)
            else:
                os.makedirs(dest_dir, exist_ok=True)
                log_fn(f"[WZ {key}] Downloading: {fname}  (by {owner}, '{title}', {item.get('rating')})")
                nbytes = _stream_download(url, fpath, session, file_progress_fn)
                size_str = (
                    f"{nbytes / 1_048_576:.1f} MB" if nbytes >= 1_048_576
                    else f"{nbytes / 1024:.1f} KB"
                )
                log_fn(f"[WZ {key}] Saved: {fname}  ({size_str})")
                with lock:
                    counter["ok"]    += 1
                    counter["bytes"] += nbytes
                    ok_keys.add(key)
                if preview_fn and ext in IMAGE_EXTENSIONS:
                    preview_fn(fpath)
        except Exception as exc:
            error_fn(f"[WZ {key}] {exc}")
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


def wz_clear_notifications(
    session: "requests.Session",
    welcomeids: list[int],
    log_fn=print,
    cancel_fn=None,
    batch_size: int = 50,
) -> int:
    """
    Remove inbox entries by POSTing their welcomeids to /messages/remove — the
    same form the inbox's "Remove Checked" button submits. Returns the count sent.
    """
    cleared = 0
    for i in range(0, len(welcomeids), batch_size):
        if cancel_fn and cancel_fn():
            break
        batch = welcomeids[i : i + batch_size]
        data  = [("remove", str(w)) for w in batch] + [("recall", "")]
        try:
            resp = session.post(
                f"{WZ_BASE}/messages/remove",
                data=data,
                headers=_WZ_POST_HEADERS,
                allow_redirects=False,
                timeout=30,
            )
            # A 303 back to the inbox is success; anything else is an error page.
            if resp.status_code != 303:
                raise ValueError(f"HTTP {resp.status_code}")
            cleared += len(batch)
            log_fn(f"  Cleared {i + 1}–{i + len(batch)} (HTTP {resp.status_code}).")
        except Exception as exc:
            log_fn(f"  Failed to clear {i + 1}–{i + len(batch)}: {exc}")
        time.sleep(random.uniform(1.0, 2.0))
    return cleared
