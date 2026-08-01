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


def _int_or(value, default: int) -> int:
    """int(value) if it can be read as one, else default. pages_count comes back as
    an int, a numeric string, or occasionally '' — the last one used to raise."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def ib_login(
    username: str,
    password: str,
    allow_adult: bool = True,
) -> tuple[str, str, str, "requests.Session"]:
    """
    Log in to the Inkbunny API.
    Returns (sid, user_id, ratingsmask, tags_set, session) where:
      sid         — API session token (passed to all API calls)
      user_id     — numeric user ID string
      ratingsmask — binary string of account's allowed ratings (e.g. "11111")
      tags_set    — the tag[N] rating flags api_userrating.php echoed back
      session     — requests.Session with PHP cookie (for notifications scraping)
    Raises ValueError on failure.

    allow_adult: if True (default), applies the account's saved ratingsmask via
    api_userrating.php so API responses include adult content. The IB wiki confirms
    the login response ratingsmask is the account's configured preference (a binary
    string like "11111"), NOT a session default. Falls back to "11111" if absent.
    api_userrating.php only affects API (sid) calls, not PHPSESSID web sessions.
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
    user_id     = str(data.get("user_id",     ""))
    # ratingsmask in the login response is a 5-char binary string of the account's
    # configured ratings (e.g. "11111" = all content, "10000" = General only).
    ratingsmask = str(data.get("ratingsmask", "")).strip()

    # api_userrating.php uses individual tag[N] params, NOT a ratingsmask string.
    # tag[2]=Nudity, tag[3]=Violence, tag[4]=Sexual/Adult, tag[5]=Strong Violence.
    # All default to "no" (General-only) for new sessions.
    tag_value = "yes" if allow_adult else "no"
    ra = session.get(
        f"{IB_API}/api_userrating.php",
        params={
            "sid":    sid,
            "tag[2]": tag_value,
            "tag[3]": tag_value,
            "tag[4]": tag_value,
            "tag[5]": tag_value,
        },
        timeout=15,
    )
    ra.raise_for_status()
    ra_data = ra.json()
    if "error_code" in ra_data:
        raise ValueError(
            f"api_userrating.php error {ra_data['error_code']}: "
            f"{ra_data.get('error_message', 'unknown')}"
        )
    # Response includes each tag as a field; build a summary for logging.
    tags_set = {k: v for k, v in ra_data.items() if k.startswith("tag")}

    return sid, user_id, ratingsmask, tags_set, session


def ib_lookup_user_id(sid: str, username: str, log_fn=print) -> str:
    """
    Resolve a username to its numeric user_id via api_search.php.
    Returns the user_id string, or "" if the user has no submissions.
    """
    r = requests.get(
        f"{IB_API}/api_search.php",
        params={
            "sid":                  sid,
            "username":             username,
            "submissions_per_page": 1,
            "submission_ids_only":  "no",
        },
        headers=_random_headers(),
        timeout=20,
    )
    r.raise_for_status()
    data = r.json()
    for sub in data.get("submissions", []):
        uid = str(sub.get("user_id", ""))
        if uid:
            log_fn(f"[IB] Resolved '{username}' → user_id={uid}")
            return uid
    log_fn(f"[IB] WARNING: could not resolve user_id for '{username}' (no submissions found)")
    return ""


def ib_fetch_submission_ids(
    sid: str,
    username: str,
    mode: str,                              # "gallery" | "favourites"
    max_pages: int,
    user_id: str = "",                      # numeric user ID; required for "favourites"
    session: "requests.Session | None" = None,  # MUST be passed so PHPSESSID cookie is
                                            # sent alongside sid; without it the server
                                            # can't match sid → session ratingsmask and
                                            # falls back to General-only content
    log_fn=print,
    cancel_fn=None,
) -> list[str]:
    """
    Paginate the Inkbunny search API for gallery or favourites.

    Gallery:    api_search.php?username=X&orderby=create_datetime
    Favourites: api_search.php?favs_user_id=N&orderby=fav_datetime
                (IB wiki: favs_user_id is the correct param, not favoritedby;
                 fav_datetime ordering is only supported with favs_user_id)

    session must be the requests.Session from ib_login so the PHPSESSID cookie is
    included in every api_search.php call. IB uses it to look up the session that
    was updated by api_userrating.php; bare requests.get() drops the cookie and the
    server falls back to guest-level General-only content filtering.
    Returns a flat list of submission IDs.
    """
    # An unscoped search is NOT a harmless empty search: api_search.php ignores a
    # blank username/favs_user_id and matches the whole site (18000 results, 180
    # pages of other people's submissions). ib_lookup_user_id returns "" when it
    # can't resolve a name, and that "" used to be passed straight through. Today
    # it happens to come back empty because orderby=fav_datetime rejects a blank
    # favs_user_id — but that is luck, not a guard, so refuse it outright.
    if mode == "gallery" and not (username or "").strip():
        raise ValueError("Inkbunny gallery scan needs a username.")
    if mode != "gallery" and not (user_id or "").strip():
        raise ValueError(
            f"Inkbunny favourites scan needs a numeric user_id"
            f"{f' — could not resolve {username!r}' if username else ''}."
        )

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
                "favs_user_id":         user_id,
            }

        log_fn(
            f"[IB] {mode.title()} — user='{username}'"
            + (f"  user_id={user_id}" if mode != "gallery" else "")
            + f"  page={page}  endpoint=api_search.php  orderby={orderby}"
        )

        if session:
            r = session.get(f"{IB_API}/api_search.php", params=params, timeout=20)
        else:
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

        if page >= _int_or(data.get("pages_count"), 1):
            break
        page += 1
        time.sleep(0.5)
    else:
        log_fn(f"  Stopped at the {max_pages}-page limit — raise 'Max pages' for more.")

    return all_ids


def ib_fetch_unread_submission_ids(
    sid: str,
    max_pages: int,
    session: "requests.Session | None" = None,
    log_fn=print,
    cancel_fn=None,
) -> list[str]:
    """
    Fetch unread submission IDs from watched artists via api_search.php?unread_submissions=yes.
    Uses the API sid (with tag[N]=yes applied by ib_login) so adult-rated notifications are
    included. session must be the requests.Session from ib_login so the PHPSESSID cookie is
    sent alongside sid (same requirement as ib_fetch_submission_ids).
    """
    all_ids: list[str] = []
    page = 1

    while page <= max_pages:
        if cancel_fn and cancel_fn():
            break

        params: dict = {
            "sid":                  sid,
            "page":                 page,
            "submissions_per_page": 100,
            "unread_submissions":   "yes",
            "orderby":              "unread_datetime",
            "random":               "no",
        }

        log_fn(f"[IB] Submission Notifications — page={page}  endpoint=api_search.php?unread_submissions=yes")

        if session:
            r = session.get(f"{IB_API}/api_search.php", params=params, timeout=20)
        else:
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
            log_fn(f"  No new submissions on page {page} — done.")
            break

        ids = [s["submission_id"] for s in subs]
        all_ids.extend(ids)
        pages_total = data.get("pages_count", "?")
        log_fn(
            f"  Page {page}/{pages_total}: {len(ids)} new submissions"
            f"  |  total collected: {len(all_ids)}"
        )

        if page >= _int_or(data.get("pages_count"), 1):
            break
        page += 1
        time.sleep(0.5)
    else:
        log_fn(f"  Stopped at the {max_pages}-page limit — raise 'Max pages' for more.")

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
    # A submission can hold several files. If any one of them fails, the
    # submission must NOT be reported as done — the caller marks done_ids as read
    # on Inkbunny, which would retire the notification with a file still missing.
    failed_sub_ids: set[str] = set()
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

            url_ext = url.split("?")[0].rsplit(".", 1)[-1][:10].lower()
            if orig:
                stem, dot, ext = orig.rpartition(".")
                if not dot:
                    stem, ext = orig, (url_ext or "bin")
            else:
                stem, ext = title, (url_ext or "bin")

            # Truncate the stem, never the extension — the old blanket fname[:200]
            # could cut the suffix off a long name, which then skipped the video/
            # sub-folder, skipped the image preview, and left the file suffixless.
            ext       = ext.lower()
            fname     = f"{sub_id}_{sanitize_filename(stem)}"[:190] + f".{ext}"
            ext_check = ext
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
                failed_sub_ids.add(info.get("submission_id", ""))

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

    return {
        "images":   counter["ok"],
        "bytes":    counter["bytes"],
        "done_ids": sorted(ok_sub_ids - failed_sub_ids),
    }


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
