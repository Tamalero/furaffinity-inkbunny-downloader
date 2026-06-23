import json
import os
import re
import random
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
from bs4 import BeautifulSoup

from common import (
    CONFIG_DIR, USER_AGENTS, IMAGE_EXTENSIONS, VIDEO_EXTENSIONS,
    _random_headers, _stream_download, sanitize_filename,
)

FA_BASE            = "https://www.furaffinity.net"
FA_COOKIES_FILE    = CONFIG_DIR / "fa_cookies.json"
FA_SUBMISSIONS_URL = f"{FA_BASE}/msg/submissions/"


def fa_save_cookies(session: "requests.Session"):
    """Persist session cookies and user-agent to disk (chmod 600)."""
    data = {
        "user_agent": session.headers.get("User-Agent", ""),
        "cookies": [
            {"name": c.name, "value": c.value, "domain": c.domain, "path": c.path}
            for c in session.cookies
        ],
    }
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    FA_COOKIES_FILE.write_text(json.dumps(data))
    FA_COOKIES_FILE.chmod(0o600)


def fa_resume_session() -> "requests.Session | None":
    """
    Try to restore a previous FA session from saved cookies.
    Returns an authenticated requests.Session, or None if cookies are
    missing, unreadable, or no longer valid.
    """
    if not FA_COOKIES_FILE.exists():
        return None
    try:
        data = json.loads(FA_COOKIES_FILE.read_text())
    except Exception:
        return None

    session = requests.Session()
    session.headers.update({
        "User-Agent":                data.get("user_agent", random.choice(USER_AGENTS)),
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.5",
        "Accept-Encoding":           "gzip, deflate, br",
        "DNT":                       "1",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer":                   f"{FA_BASE}/",
    })
    for c in data.get("cookies", []):
        session.cookies.set(
            c["name"], c["value"], domain=c.get("domain", ".furaffinity.net")
        )

    try:
        r = session.get(f"{FA_BASE}/", timeout=15)
        r.raise_for_status()
        if "/logout" in r.text:
            return session
    except Exception:
        pass
    return None


def _clone_session(base: "requests.Session") -> "requests.Session":
    """Return a fresh session sharing the base session's cookies (thread-safe copy)."""
    s = requests.Session()
    s.cookies.update(base.cookies)
    s.headers.update({
        "User-Agent": random.choice(USER_AGENTS),
        "Referer":    f"{FA_BASE}/",
    })
    return s


def _fa_turnstile_click(page, log_fn, token_predicate) -> bool:
    """
    Locate and click the Cloudflare Turnstile checkbox, then wait for CF to fill
    the hidden response token. Returns True if solved, False otherwise. Emits
    diagnostic lines via log_fn (silent failures were impossible to debug before).
    Never raises — login can still proceed with a manual click if this fails.

    The CF <iframe> lives inside a CLOSED shadow root, so page.locator('iframe…')
    and frame_locator both return nothing. Frames are tracked at the browser level
    independent of shadow-DOM encapsulation, so we find it via page.frames instead.
    """
    # Wait for the CF challenge frame to appear in the browser's frame tree.
    cf_frame = None
    deadline = time.time() + 15
    while time.time() < deadline:
        cf_frame = next(
            (f for f in page.frames if "challenges.cloudflare.com" in (f.url or "")),
            None,
        )
        if cf_frame is not None:
            break
        time.sleep(0.5)

    if cf_frame is None:
        log_fn("Turnstile: no challenge frame found — skipping.")
        return False

    # Already solved? (token present from a prior attempt / managed auto-pass)
    try:
        if page.evaluate(token_predicate):
            log_fn("Turnstile: already solved (token present).")
            return True
    except Exception:
        pass

    # Primary: operate INSIDE the frame and force-click the real <input>
    # (opacity:0, on top). force=True bypasses the visibility check.
    try:
        cf_box = cf_frame.locator('input[type="checkbox"]').first
        cf_box.wait_for(state="attached", timeout=15_000)
        log_fn("Turnstile: checkbox found inside frame — clicking.")
        time.sleep(random.uniform(1.2, 2.5))
        cf_box.click(force=True, delay=random.randint(90, 200))
        page.wait_for_function(token_predicate, timeout=30_000)
        log_fn("Turnstile: solved (token received).")
        return True
    except Exception as exc:
        log_fn(f"Turnstile: in-frame click failed ({exc!r}) — trying iframe-element fallback.")

    # Fallback: get the owner <iframe> element handle (works through the closed
    # shadow root) and click its checkbox position via the page mouse —
    # ~20 px from the left, vertically centred in the 65 px widget.
    try:
        handle = cf_frame.frame_element()
        box    = handle.bounding_box()
        if box:
            time.sleep(random.uniform(0.8, 1.6))
            page.mouse.click(
                box["x"] + random.uniform(16.0, 24.0),
                box["y"] + random.uniform(28.0, 37.0),
                delay=random.randint(90, 200),
            )
            page.wait_for_function(token_predicate, timeout=30_000)
            log_fn("Turnstile: solved via iframe-element fallback (token received).")
            return True
        log_fn("Turnstile: iframe element had no bounding box — solve it manually.")
        return False
    except Exception as exc:
        log_fn(f"Turnstile: fallback failed ({exc!r}) — solve it manually.")
        return False


def fa_login(username: str, password: str, log_fn=print) -> "requests.Session":
    """
    Authenticate with FurAffinity via Camoufox (a patched Firefox build that removes all
    Playwright/Juggler automation fingerprints Cloudflare would otherwise detect).
    Transfers FA cookies to a requests.Session after login and closes the browser.
    Raises ValueError on login failure or timeout. `log_fn` receives progress lines
    (visible in the GUI's verbose console mode) for diagnosing the CF Turnstile step.
    """
    from camoufox.sync_api import Camoufox
    from playwright.sync_api import TimeoutError as PWTimeout, Error as PWError

    # humanize=True   — Camoufox moves the cursor on a natural arc before every click.
    # disable_coop=True — disables COOP so cross-origin CF Turnstile iframes are clickable.
    #   i_know_what_im_doing silences Camoufox's COOP LeakWarning (intentional here).
    # uBlock Origin is left enabled (camoufox's default addon): it had no effect on the
    # Turnstile click — the widget bbox is read fresh just before clicking, after the
    # page has settled — and a real user usually has an adblocker, so it aids the
    # fingerprint.
    with Camoufox(
        headless=False,
        humanize=True,
        i_know_what_im_doing=True,
        disable_coop=True,
    ) as browser:
        page = browser.new_page()
        try:
            page.goto(f"{FA_BASE}/login/", wait_until="domcontentloaded", timeout=60_000)

            # Wait for the login form to be present in the DOM first.
            page.wait_for_selector('input[name="name"]', timeout=120_000)

            # ── Credentials FIRST — type character-by-character (not fill) ──────
            # Doing this before the Turnstile click gives the challenge iframe
            # several seconds to finish loading its (async) inner content, and
            # matches natural human order (fill form, then tick the box).
            time.sleep(random.uniform(1.5, 3.0))
            page.click('input[name="name"]')
            time.sleep(random.uniform(0.25, 0.55))
            page.keyboard.type(username, delay=random.randint(60, 150))

            time.sleep(random.uniform(0.8, 1.8))
            page.click('input[name="pass"]')
            time.sleep(random.uniform(0.25, 0.55))
            page.keyboard.type(password, delay=random.randint(60, 150))

            # ── Cloudflare Turnstile auto-click ────────────────────────────────
            # Widget structure: shadow-root → iframe → #document → shadow-root →
            # <label class="mabZ4"><input type="checkbox"><span>…</span></label>.
            # Per CF's own CSS the real <input> is a 24×24 hit target with
            # opacity:0 and z-index:9999 sitting ON TOP of the visible box — so it
            # IS clickable but never "visible" to Playwright (that is why every
            # wait_for(state="visible") timed out). We force-click the input
            # dead-centre via frame_locator (which crosses the iframe boundary and
            # pierces the inner open shadow root). No coordinate math.
            _cf_token = (
                "() => (document.querySelector('input[name=\"cf-turnstile-response\"]')"
                "?.value?.length ?? 0) > 0"
            )
            _fa_turnstile_click(page, log_fn, _cf_token)

            # ── Submit ──────────────────────────────────────────────────────────
            time.sleep(random.uniform(0.5, 1.2))
            page.click('input[name="login"], button[type="submit"]')

            # Detect a logged-in page. FA no longer uses a logout <a> link — it is
            # now a POST <form action="/logout/">. We also accept the logged-in
            # avatar / username link. state="attached" because some of these live
            # inside hover dropdowns (display:none until opened) — we only need the
            # DOM to exist, not be visible. The check survives CF redirect chains.
            logged_in_sel = (
                'form[action="/logout/"], .loggedin_user_avatar, '
                '#my-username, a[href="/controls/settings/"]'
            )
            try:
                page.wait_for_selector(logged_in_sel, state="attached", timeout=60_000)
            except (PWTimeout, PWError):
                try:
                    if "/login" in page.url:
                        soup = BeautifulSoup(page.content(), "lxml")
                        err  = soup.find(class_=re.compile(r"notice|error|message", re.I))
                        msg  = err.get_text(strip=True)[:300] if err else "Invalid credentials."
                        raise ValueError(f"FurAffinity login failed: {msg}")
                except ValueError:
                    raise
                except Exception:
                    pass
                raise ValueError("FurAffinity login timed out after submission — check credentials.")

            all_cookies = page.context.cookies()
            cookies     = [c for c in all_cookies if "furaffinity.net" in c.get("domain", "")]
            user_agent  = page.evaluate("() => navigator.userAgent")

        except ValueError:
            raise
        except (PWTimeout, PWError) as exc:
            raise ValueError(f"FurAffinity login failed: {exc}") from exc

    session = requests.Session()
    session.headers.update({
        "User-Agent":                user_agent,
        "Accept":                    "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
        "Accept-Language":           "en-US,en;q=0.5",
        "Accept-Encoding":           "gzip, deflate, br",
        "DNT":                       "1",
        "Connection":                "keep-alive",
        "Upgrade-Insecure-Requests": "1",
        "Referer":                   f"{FA_BASE}/",
    })
    for c in cookies:
        session.cookies.set(c["name"], c["value"], domain=c["domain"])

    fa_save_cookies(session)
    return session


def _fa_parse_page(session: "requests.Session", url: str) -> tuple[list[str], str | None]:
    """
    Scrape one FA gallery/favourites page.
    Returns (submission_ids, next_page_url | None).
    """
    r = session.get(url, timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    # Primary: <figure id="sid-XXXXXXXX">
    ids: list[str] = [
        fig["id"][4:]
        for fig in soup.find_all("figure", id=re.compile(r"^sid-\d+$"))
    ]

    if not ids:
        # Fallback: parse /view/NNN/ hrefs
        seen: set[str] = set()
        for a in soup.find_all("a", href=re.compile(r"^/view/\d+/")):
            m = re.search(r"/view/(\d+)/", a["href"])
            if m and m.group(1) not in seen:
                seen.add(m.group(1))
                ids.append(m.group(1))

    # Locate the "Next" button / link
    next_url: str | None = None
    for a in soup.find_all("a", href=True):
        if a.get_text(strip=True).lower() in ("next", "»", ">"):
            href = a["href"]
            next_url = (FA_BASE + href) if href.startswith("/") else href
            break

    return ids, next_url


def fa_fetch_submission_ids(
    session: "requests.Session",
    username: str,
    mode: str,          # "gallery" | "favourites"
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[str]:
    base_path = "favorites" if mode == "favourites" else "gallery"
    next_url: str | None = f"{FA_BASE}/{base_path}/{username}/"
    all_ids: list[str] = []

    for page_num in range(1, max_pages + 1):
        if (cancel_fn and cancel_fn()) or not next_url:
            break
        log_fn(f"Scanning {mode} page {page_num}…")
        ids, next_url = _fa_parse_page(session, next_url)
        if not ids:
            log_fn(f"No submissions found on page {page_num} — stopping.")
            break
        all_ids.extend(ids)
        log_fn(f"  Page {page_num}: {len(ids)} submissions (total: {len(all_ids)})")
        time.sleep(random.uniform(1.0, 2.5))

    return all_ids


def fa_fetch_notification_ids(
    session: "requests.Session",
    max_pages: int,
    log_fn=print,
    cancel_fn=None,
) -> list[str]:
    """
    Scrape the FA 'Submission Notifications' inbox (/msg/submissions/) for new
    submission IDs, in inbox order. Paginates up to max_pages following the
    inbox's 'Next' link. Same <figure id="sid-NNNN"> markup as the gallery.
    """
    next_url: str | None = FA_SUBMISSIONS_URL
    all_ids: list[str] = []
    seen: set[str] = set()

    for page_num in range(1, max_pages + 1):
        if (cancel_fn and cancel_fn()) or not next_url:
            break
        log_fn(f"Scanning notifications page {page_num}…")
        r = session.get(next_url, timeout=20)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "lxml")

        ids = [
            fig["id"][4:]
            for fig in soup.find_all("figure", id=re.compile(r"^sid-\d+$"))
            if fig["id"][4:] not in seen
        ]
        if not ids:
            log_fn(f"No new submission notifications on page {page_num} — stopping.")
            break
        seen.update(ids)
        all_ids.extend(ids)
        log_fn(f"  Page {page_num}: {len(ids)} notifications (total: {len(all_ids)})")

        # Inbox 'Next' link is <a class="button standard more" href="/msg/submissions/new~ID@48/">.
        next_url = None
        more = soup.find("a", class_="more", href=True)
        if more:
            href = more["href"]
            next_url = (FA_BASE + href) if href.startswith("/") else href
        time.sleep(random.uniform(1.0, 2.0))

    return all_ids


def fa_clear_notifications(
    session: "requests.Session",
    submission_ids: list[str],
    log_fn=print,
    cancel_fn=None,
    batch_size: int = 48,
) -> int:
    """
    Remove submission notifications from the FA inbox in page-sized batches
    (48 = FA's page size) so a failure never wipes the whole inbox. Mirrors the
    inbox's own "Remove Selected" button — POST repeated `submissions[]` plus
    `messagecenter-action=remove_checked` (NEVER `nuke_notifications`). The form
    carries no CSRF nonce; the session cookie alone authorises it. Returns the
    count submitted for removal; each batch's HTTP status is logged.
    """
    if not submission_ids:
        return 0

    cleared = 0
    for i in range(0, len(submission_ids), batch_size):
        if cancel_fn and cancel_fn():
            break
        batch = submission_ids[i : i + batch_size]
        data  = [("submissions[]", sid) for sid in batch]
        data.append(("messagecenter-action", "remove_checked"))
        try:
            resp = session.post(FA_SUBMISSIONS_URL, data=data, timeout=30)
            resp.raise_for_status()
            cleared += len(batch)
            log_fn(f"  Cleared notifications {i + 1}–{i + len(batch)} (HTTP {resp.status_code}).")
        except Exception as exc:
            log_fn(f"  Failed to clear notifications {i + 1}–{i + len(batch)}: {exc}")
        time.sleep(random.uniform(1.0, 2.0))

    return cleared


def fa_get_download_info(
    session: "requests.Session",
    submission_id: str,
) -> tuple[str, str, str]:
    """
    Returns (download_url, title, artist) for a FA submission.
    Raises ValueError if no download URL is found.
    """
    r = session.get(f"{FA_BASE}/view/{submission_id}/", timeout=20)
    r.raise_for_status()
    soup = BeautifulSoup(r.text, "lxml")

    # Title
    title = submission_id
    for sel in (
        lambda s: s.find("div",  class_="submission-title"),
        lambda s: s.find("h2",   class_="submission-title"),
        lambda s: s.find("h1"),
    ):
        el = sel(soup)
        if el:
            t = el.get_text(strip=True)
            if t:
                title = t
                break

    # Artist
    artist = "unknown"
    a_el = soup.find("a", href=re.compile(r"^/user/"))
    if a_el:
        artist = a_el.get_text(strip=True) or a_el["href"].strip("/").split("/")[-1]

    # Download URL — prefer FA CDN links (//d*.furaffinity.net/…)
    dl_url = ""
    for a in soup.find_all("a", href=re.compile(r"//d\d*\.furaffinity\.net")):
        dl_url = a["href"]
        break
    if not dl_url:
        for a in soup.find_all("a", string=re.compile(r"\bdownload\b", re.I)):
            href = a.get("href", "")
            if href:
                dl_url = href
                break
    if not dl_url:
        # Video / Flash submissions: check <video src>, <source src>, or <a href> with
        # a media extension — covers FA's HTML5 player where no separate download link exists.
        for tag in soup.find_all(["video", "source"], src=True):
            src = tag["src"]
            if src:
                dl_url = src
                break
    if not dl_url:
        for img in (
            soup.find("img", id="submissionImg"),
            soup.find("img", class_=re.compile(r"submission-image")),
        ):
            if img and img.get("src"):
                dl_url = img["src"]
                break

    if not dl_url:
        raise ValueError(f"No download URL for submission {submission_id}")
    if dl_url.startswith("//"):
        dl_url = "https:" + dl_url

    return dl_url, title, artist


def download_fa_submissions(
    session: "requests.Session",
    submission_ids: list[str],
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

    total   = len(submission_ids)
    counter = {"done": 0, "ok": 0, "bytes": 0}
    ok_ids: list[str] = []   # submissions confirmed on disk — safe to clear later
    lock    = threading.Lock()

    if progress_fn:
        progress_fn(0, total)

    def _process(sub_id: str):
        if cancel_fn and cancel_fn():
            return
        ts = _clone_session(session)   # per-thread session copy
        try:
            dl_url, title, artist = fa_get_download_info(ts, sub_id)
            raw_ext  = dl_url.rsplit(".", 1)[-1].split("?")[0][:10].lower()
            ext      = raw_ext if raw_ext else "bin"
            fname    = f"{sanitize_filename(artist)[:40]}_{sanitize_filename(title)[:80]}_{sub_id}.{ext}"
            dest_dir = os.path.join(output_dir, "video") if ext in VIDEO_EXTENSIONS else output_dir
            fpath    = os.path.join(dest_dir, fname)

            if os.path.exists(fpath):
                log_fn(f"Skipped (exists): {fname}")
                with lock:
                    ok_ids.append(sub_id)
            else:
                os.makedirs(dest_dir, exist_ok=True)
                nbytes = _stream_download(dl_url, fpath, ts, file_progress_fn)
                log_fn(f"Saved: {fname}")
                with lock:
                    counter["ok"]    += 1
                    counter["bytes"] += nbytes
                    ok_ids.append(sub_id)
                if preview_fn and ext in IMAGE_EXTENSIONS:
                    preview_fn(fpath)
        except Exception as exc:
            error_fn(f"[FA {sub_id}] {exc}")

        with lock:
            counter["done"] += 1
            done_now = counter["done"]
        if progress_fn:
            progress_fn(done_now, total)
        time.sleep(random.uniform(delay_min, delay_max))

    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futs = [pool.submit(_process, sid) for sid in submission_ids]
        for fut in as_completed(futs):
            if cancel_fn and cancel_fn():
                for rem in futs:
                    rem.cancel()
                break
            fut.result()

    return {"images": counter["ok"], "bytes": counter["bytes"], "done_ids": list(ok_ids)}
