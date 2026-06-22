import os
import re
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from common import _random_headers, _stream_download, sanitize_filename, IMAGE_EXTENSIONS

IB_API = "https://inkbunny.net"


def ib_login(username: str, password: str) -> tuple[str, "requests.Session"]:
    """
    Log in to the Inkbunny API.
    Returns (sid, session) where sid is the API session ID and session is a
    requests.Session carrying the PHP cookie for web-page access.
    Raises ValueError on failure.
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
    return sid, session


def ib_fetch_submission_ids(
    sid: str,
    username: str,
    mode: str,          # "gallery" | "favourites"
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[str]:
    """Paginate the Inkbunny search API. Returns a flat list of submission IDs."""
    all_ids: list[str] = []
    page = 1

    while page <= max_pages:
        if cancel_fn and cancel_fn():
            break
        log_fn(f"Scanning {mode} page {page}…")

        params: dict = {
            "sid":                  sid,
            "page":                 page,
            "submissions_per_page": 100,
            "orderby":              "create_datetime",
            "random":               "no",
        }
        if mode == "gallery":
            params["username"] = username
        else:
            params["favoritedby"] = username

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
            log_fn(f"No more pages (stopped at page {page}).")
            break

        ids = [s["submission_id"] for s in subs]
        all_ids.extend(ids)
        log_fn(f"  Page {page}: {len(ids)} submissions (total: {len(all_ids)})")

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


def ib_get_file_infos(sid: str, submission_ids: list[str], log_fn=print) -> list[dict]:
    """
    Resolve submission IDs to individual file metadata in batches.
    Returns a list of dicts: {url, filename, title, username, submission_id}.
    """
    results: list[dict] = []
    batch_size = 100

    for i in range(0, len(submission_ids), batch_size):
        batch = submission_ids[i : i + batch_size]
        log_fn(f"Fetching file info for submissions {i + 1}–{i + len(batch)}…")
        r = requests.get(
            f"{IB_API}/api_submissions.php",
            params={
                "sid":              sid,
                "submission_ids":   ",".join(batch),
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
            title    = sub.get("title", sub_id)
            username = sub.get("username", "unknown")
            for f in sub.get("files", []):
                url = f.get("file_url_full") or f.get("file_url_screen") or ""
                if url:
                    results.append({
                        "url":           url,
                        "filename":      f.get("file_name", ""),
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

            if orig:
                fname = f"{sub_id}_{sanitize_filename(orig)}"
            else:
                raw_ext = url.rsplit(".", 1)[-1].split("?")[0][:10].lower()
                ext     = raw_ext if raw_ext else "bin"
                title   = sanitize_filename(info.get("title", sub_id))[:80]
                fname   = f"{sub_id}_{title}.{ext}"

            fname = fname[:200]
            fpath = os.path.join(output_dir, fname)

            if os.path.exists(fpath):
                log_fn(f"Skipped (exists): {fname}")
                with lock:
                    ok_sub_ids.add(sub_id)
            else:
                nbytes = _stream_download(url, fpath, None, file_progress_fn)
                log_fn(f"Saved: {fname}")
                with lock:
                    counter["ok"]    += 1
                    counter["bytes"] += nbytes
                    ok_sub_ids.add(sub_id)
                ext_check = fname.rsplit(".", 1)[-1].lower() if "." in fname else ""
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
