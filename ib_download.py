import os
import re
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from common import _random_headers, _stream_download, sanitize_filename, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS

IB_API = "https://inkbunny.net"

# Inkbunny submission types that contain only text — skip entirely.
_IB_TEXT_TYPES = {"story", "poetry", "prose"}

# File extensions considered non-media — skipped even if the API type is unknown.
_TEXT_EXTENSIONS = {"txt", "doc", "docx", "rtf", "odt", "pdf", "epub", "html", "htm", "md"}


def ib_login(
    username: str,
    password: str,
    allow_adult: bool = True,
) -> tuple[str, str, "requests.Session"]:
    """
    Log in to the Inkbunny API.
    Returns (sid, user_id, session) where:
      sid     — API session token (passed to all API calls)
      user_id — numeric user ID string
      session — requests.Session carrying the PHP cookie (for web-page scraping
                of notifications inbox, which has no REST API equivalent)
    Raises ValueError on failure.

    allow_adult: if True (default), calls api_userrating.php with ratingsmask=11
    so API responses include Mature + Adult content. Has NO effect on web page
    rendering — PHP sessions always start General-only regardless.
    If False, keeps ratingsmask=0 (General-only) for API calls too.
    """
    session = requests.Session()
    session.headers.update(_random_headers())
    r = session.get(
        f"{IB_API}/api_login.php",
        params={"username": username, "password": password},
        timeout=15,
    )
    r.raise_for_status()
    data = r.json()
    if "error_code" in data:
        raise ValueError(f"Inkbunny login error: {data.get('error_message', 'unknown')}")
    sid = data.get("sid")
    if not sid:
        raise ValueError("Inkbunny login failed — no session ID returned.")
    user_id = str(data.get("user_id", ""))

    # api_userrating.php enables adult content for API calls that use sid.
    # The login response ratingsmask is the API session default (0 = General
    # only), NOT the account preference — passing it back would be a no-op.
    # Hardcode 11 (Mature + Adult) when allow_adult=True so API search results
    # include all ratings the account allows.
    # NOTE: this call only affects API responses (via sid). It does NOT enable
    # adult content for web page rendering (PHPSESSID sessions). Own-favourites
    # must therefore use ib_fetch_submission_ids (API) not ib_fetch_favourite_ids
    # (web scraping), which permanently sees only General content.
    ra = session.get(
        f"{IB_API}/api_userrating.php",
        params={"sid": sid, "ratingsmask": "11" if allow_adult else "0"},
        timeout=15,
    )
    ra.raise_for_status()

    return sid, user_id, session


def ib_fetch_submission_ids(
    sid: str,
    username: str,
    mode: str,          # "gallery" | "favourites"
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[str]:
    """
    Paginate the Inkbunny search API for gallery or favourites.
    For favourites, requests orderby=fav_datetime so IB returns them in
    the same order shown in the browser (newest-favorited first). This path
    uses sid which respects the ratingsmask set by api_userrating.php,
    so adult content is included when allow_adult=True was passed to ib_login.
    Returns a flat list of submission IDs.
    """
    all_ids: list[str] = []
    page = 1

    while page <= max_pages:
        if cancel_fn and cancel_fn():
            break

        if mode == "gallery":
            orderby = "create_datetime"
            params: dict = {
                "sid":                  sid,
                "page":                 page,
                "submissions_per_page": 100,
                "orderby":              orderby,
                "random":               "no",
                "username":             username,
            }
        else:
            orderby = "fav_datetime"
            params = {
                "sid":                  sid,
                "page":                 page,
                "submissions_per_page": 100,
                "orderby":              orderby,
                "random":               "no",
                "favoritedby":          username,
            }

        log_fn(
            f"[IB] {mode.title()} — user='{username}'  page={page}"
            f"  endpoint=api_search.php  orderby={orderby}"
        )

        r = requests.get(
            f"{IB_API}/api_search.php",
            params=params,
            headers=_random_headers(),
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        if "error_code" in data:
            raise ValueError(f"Inkbunny API error: {data.get('error_message', 'unknown')}")

        subs = data.get("submissions", [])
        if not subs:
            log_fn(f"  No results on page {page} — done.")
            break

        ids = [s["submission_id"] for s in subs]
        all_ids.extend(ids)
        pages_total = data.get("pages_count", "?")
        log_fn(
            f"  Page {page}/{pages_total}: {len(ids)} submissions"
            f"  |  total collected: {len(all_ids)}"
        )

        if page >= int(data.get("pages_count", "1")):
            break
        page += 1
        time.sleep(0.5)

    return all_ids


def ib_fetch_unread_submission_ids(
    session: "requests.Session",
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[str]:
    """
    Scrape the Inkbunny 'New Submissions' inbox (/submissionsviewall.php?mode=unreadsubs)
    for unread submission IDs from watched artists. Uses the web cookie session from
    ib_login() since there is no REST API endpoint for the unread inbox. Paginates up
    to max_pages. The 'rid' snapshot token is extracted from the first page URL and
    carried through subsequent pages to avoid drift from new arrivals during scraping.
    """
    all_ids: list[str] = []
    seen: set[str] = set()
    rid: str | None = None

    for page_num in range(1, max_pages + 1):
        if cancel_fn and cancel_fn():
            break
        log_fn(f"Scanning new submissions page {page_num}…")

        params: dict = {"mode": "unreadsubs", "page": page_num, "orderby": "unread_datetime"}
        if rid:
            params["rid"] = rid

        r = session.get(f"{IB_API}/submissionsviewall.php", params=params, timeout=20)
        r.raise_for_status()

        # Capture the rid from the final URL (server may redirect to add it)
        if rid is None:
            m = re.search(r"[?&]rid=([a-f0-9]+)", r.url)
            if m:
                rid = m.group(1)

        soup = BeautifulSoup(r.text, "lxml")

        ids: list[str] = []
        for a in soup.find_all("a", href=re.compile(r"^/s/\d+")):
            m = re.match(r"/s/(\d+)", a["href"])
            if m:
                sub_id = m.group(1)
                if sub_id not in seen:
                    seen.add(sub_id)
                    ids.append(sub_id)

        if not ids:
            log_fn(f"No new submissions on page {page_num} — stopping.")
            break

        all_ids.extend(ids)
        log_fn(f"  Page {page_num}: {len(ids)} new submissions (total: {len(all_ids)})")

        if not soup.find("a", string=re.compile(r"next", re.I)):
            break
        time.sleep(random.uniform(1.0, 2.0))

    return all_ids


def ib_fetch_favourite_ids(
    session: "requests.Session",
    user_id: str,
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[str]:
    """
    Scrape the Inkbunny favourites page using the same endpoint the web browser
    uses: /submissionsviewall.php?mode=userfavs&user_id=X&orderby=fav_datetime.
    This guarantees the results and ordering match exactly what you see in the
    browser.  user_id is the numeric ID returned by ib_login().
    """
    all_ids: list[str] = []
    seen: set[str] = set()
    rid: str | None = None

    for page_num in range(1, max_pages + 1):
        if cancel_fn and cancel_fn():
            break

        params: dict = {
            "mode":    "userfavs",
            "user_id": user_id,
            "page":    page_num,
            "orderby": "fav_datetime",
        }
        if rid:
            params["rid"] = rid

        log_fn(
            f"[IB] Favourites — user_id={user_id}  page={page_num}"
            f"  orderby=fav_datetime"
            + (f"  rid={rid}" if rid else "")
        )

        r = session.get(f"{IB_API}/submissionsviewall.php", params=params, timeout=20)
        r.raise_for_status()

        if rid is None:
            m = re.search(r"[?&]rid=([a-f0-9]+)", r.url)
            if m:
                rid = m.group(1)
                log_fn(f"  Snapshot token (rid): {rid}")

        soup = BeautifulSoup(r.text, "lxml")
        log_fn(f"  Final URL: {r.url}")

        ids: list[str] = []
        # Only collect <a> tags that contain an <img> child.
        # On the IB favourites page, thumbnail links always wrap an <img>.
        # Header notification links, sidebar links, and title-text links do NOT
        # have <img> children — filtering by img presence removes them.
        for a in soup.find_all("a", href=re.compile(r"^/s/\d+")):
            if not a.find("img"):
                continue
            m = re.match(r"/s/(\d+)", a["href"])
            if m:
                sub_id = m.group(1)
                if sub_id not in seen:
                    seen.add(sub_id)
                    ids.append(sub_id)

        if not ids:
            all_links = soup.find_all("a", href=re.compile(r"^/s/\d+"))
            if all_links:
                log_fn(
                    f"  No thumbnail links on page {page_num}"
                    f" ({len(all_links)} text-only /s/ links found — no <img> wrappers)."
                )
            else:
                log_fn(f"  No favourites on page {page_num} — done.")
            break

        all_ids.extend(ids)
        log_fn(
            f"  Page {page_num}: {len(ids)} favourites"
            f"  |  total collected: {len(all_ids)}"
            f"  |  first IDs: {', '.join(ids[:5])}"
        )

        if not soup.find("a", string=re.compile(r"next", re.I)):
            break
        time.sleep(random.uniform(1.0, 2.0))

    return all_ids


def ib_get_file_infos(sid: str, submission_ids: list[str], log_fn=print) -> list[dict]:
    """
    Resolve submission IDs to individual file metadata in batches.
    Returns a list of dicts: {url, filename, title, username, submission_id}.
    """
    results: list[dict] = []
    batch_size = 100

    total_batches = (len(submission_ids) + batch_size - 1) // batch_size
    for i in range(0, len(submission_ids), batch_size):
        batch      = submission_ids[i : i + batch_size]
        batch_num  = i // batch_size + 1
        log_fn(
            f"[IB] File info — batch {batch_num}/{total_batches}"
            f"  ({len(batch)} submissions, IDs {batch[0]}…{batch[-1]})"
        )
        r = requests.get(
            f"{IB_API}/api_submissions.php",
            params={
                "sid":              sid,
                "submission_ids":   ",".join(batch),
                "show_files":       "yes",
                "show_description": "no",
                "show_writing":     "no",
            },
            headers=_random_headers(),
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        for sub in data.get("submissions", []):
            sub_id   = sub.get("submission_id", "")
            sub_type = sub.get("type", "")
            title    = sub.get("title", sub_id)
            username = sub.get("username", "unknown")
            files    = sub.get("files", [])
            if sub_type in _IB_TEXT_TYPES:
                log_fn(f"  [{sub_id}] '{title}' by {username} — SKIP (type={sub_type})")
                continue
            log_fn(
                f"  [{sub_id}] '{title}' by {username}"
                f"  type={sub_type or 'unknown'}  files={len(files)}"
            )
            for f in files:
                url = f.get("file_url_full") or ""
                if not url:
                    log_fn(f"    ↳ WARNING: no file_url_full — skipping")
                    continue
                file_name = f.get("file_name", "")
                file_ext  = file_name.rsplit(".", 1)[-1].lower() if "." in file_name else ""
                if file_ext in _TEXT_EXTENSIONS:
                    log_fn(f"    ↳ SKIP text file: {file_name}")
                    continue
                log_fn(f"    ↳ {file_name}")
                results.append({
                    "url":           url,
                    "filename":      file_name,
                    "title":         title,
                    "username":      username,
                    "submission_id": sub_id,
                })
        time.sleep(0.3)

    return results


def download_ib_files(
    file_infos: list[dict],
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

    total       = len(file_infos)
    counter     = {"done": 0, "ok": 0, "bytes": 0}
    ok_sub_ids: set[str] = set()
    lock        = threading.Lock()

    if progress_fn:
        progress_fn(0, total)

    def _process(info: dict):
        if cancel_fn and cancel_fn():
            return
        try:
            url    = info["url"]
            sub_id = info.get("submission_id", "")
            orig   = info.get("filename", "")
            title  = info.get("title", sub_id)
            uname  = info.get("username", "")

            if orig:
                fname = f"{sub_id}_{sanitize_filename(orig)}"
            else:
                raw_ext = url.rsplit(".", 1)[-1].split("?")[0][:10].lower()
                ext     = raw_ext if raw_ext else "bin"
                fname   = f"{sub_id}_{sanitize_filename(title)[:80]}.{ext}"

            fname     = fname[:200]
            ext_check = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
            dest_dir  = os.path.join(output_dir, "video") if ext_check in VIDEO_EXTENSIONS else output_dir
            fpath     = os.path.join(dest_dir, fname)

            if os.path.exists(fpath):
                log_fn(f"[IB {sub_id}] Skipped (exists): {fname}")
                with lock:
                    ok_sub_ids.add(sub_id)
            else:
                os.makedirs(dest_dir, exist_ok=True)
                log_fn(f"[IB {sub_id}] Downloading: {fname}  (by {uname}, '{title}')")
                nbytes = _stream_download(url, fpath, None, file_progress_fn)
                size_str = (
                    f"{nbytes / 1_048_576:.1f} MB" if nbytes >= 1_048_576
                    else f"{nbytes / 1024:.1f} KB"
                )
                log_fn(f"[IB {sub_id}] Saved: {fname}  ({size_str})")
                with lock:
                    counter["ok"]    += 1
                    counter["bytes"] += nbytes
                    ok_sub_ids.add(sub_id)
                if preview_fn and ext_check in IMAGE_EXTENSIONS:
                    preview_fn(fpath)
        except Exception as exc:
            error_fn(f"[IB {info.get('submission_id', '?')}] {exc}")

        with lock:
            counter["done"] += 1
            done_now = counter["done"]
        if progress_fn:
            progress_fn(done_now, total)
        time.sleep(random.uniform(delay_min, delay_max))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_process, info) for info in file_infos]
        for fut in as_completed(futs):
            if cancel_fn and cancel_fn():
                for rem in futs:
                    rem.cancel()
                break
            fut.result()

    return {"images": counter["ok"], "bytes": counter["bytes"], "done_ids": list(ok_sub_ids)}


def ib_mark_submissions_read(
    session: "requests.Session",
    submission_ids: list[str],
    log_fn=print,
    cancel_fn=None,
    batch_size: int = 50,
) -> int:
    """
    Mark IB new-submission notifications as read by POSTing batches to
    /submissionsmarkread_process.php. Loads the inbox page once to extract the
    CSRF token embedded in the form. Returns the count sent for marking.
    """
    if not submission_ids:
        return 0

    token = ""
    try:
        r = session.get(
            f"{IB_API}/submissionsviewall.php",
            params={"mode": "unreadsubs", "page": 1},
            timeout=20,
        )
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")
        form = soup.find("form", action=re.compile(r"submissionsmarkread", re.I))
        if form:
            t = form.find("input", {"name": "token"})
            if t:
                token = t.get("value", "")
    except Exception as exc:
        log_fn(f"  Warning: could not fetch CSRF token ({exc}) — will try without.")

    cleared = 0
    for i in range(0, len(submission_ids), batch_size):
        if cancel_fn and cancel_fn():
            break
        batch = submission_ids[i : i + batch_size]
        data  = [("submissions[]", sid) for sid in batch]
        if token:
            data.append(("token", token))
        try:
            resp = session.post(
                f"{IB_API}/submissionsmarkread_process.php",
                data=data,
                timeout=30,
            )
            resp.raise_for_status()
            cleared += len(batch)
            log_fn(f"  Marked {i + 1}–{i + len(batch)} as read (HTTP {resp.status_code}).")
        except Exception as exc:
            log_fn(f"  Failed to mark {i + 1}–{i + len(batch)} as read: {exc}")
        time.sleep(random.uniform(1.0, 2.0))

    return cleared
