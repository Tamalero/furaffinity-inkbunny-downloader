# FA & Inkbunny Downloader

A desktop application for downloading galleries, favourites, and submission notifications from **FurAffinity** and **Inkbunny**. It runs on Linux and is distributed as a self-contained AppImage — no Python or system dependencies required.

---

## Table of Contents

1. [Installation](#installation)
2. [First Launch](#first-launch)
3. [Configuring FurAffinity](#configuring-furaffinity)
4. [Configuring Inkbunny](#configuring-inkbunny)
5. [Download Options](#download-options)
6. [Output Folder](#output-folder)
7. [Troubleshooting — FurAffinity & Cloudflare](#troubleshooting--furaffinity--cloudflare)

---

## Installation

### AppImage (recommended)

1. Download `FurAffinityInkbunnyDownloader-x.y.z-x86_64.AppImage` from the [Releases](https://github.com/Tamalero/furaffinity-inkbunny-downloader/releases) page.
2. Make it executable:
   ```bash
   chmod +x FurAffinityInkbunnyDownloader-*.AppImage
   ```
3. Run it:
   ```bash
   ./FurAffinityInkbunnyDownloader-*.AppImage
   ```

Or double-click it in your file manager if your desktop environment supports AppImages.

> **Camoufox browser (FurAffinity only)**  
> The first time you use FurAffinity mode, the app needs a patched Firefox build called Camoufox to handle Cloudflare. Go to **Help → Setup Camoufox Browser…** and follow the prompt. This is a one-time download (~100 MB) stored in your home directory.

### From Source

```bash
git clone https://github.com/Tamalero/furaffinity-inkbunny-downloader.git
cd furaffinity-inkbunny-downloader
./run.fish
```

`run.fish` creates a virtual environment, installs all dependencies, and launches the app.

---

## First Launch

When the app opens you will see:

- **Credentials** — site selector, username, and password fields.
- **Download Options** — mode, target, pages, concurrency, delay, and notification settings.
- **Output Folder** — where files are saved.
- **Start / Cancel** buttons.
- **Progress bars** and a **Log** panel showing live download activity.
- A **Preview** panel showing the most recently downloaded image.

Credentials are encrypted and saved to `~/.config/faibdownloader/config.ini` the first time you click **Start Download**. They are restored automatically on the next launch.

---

## Configuring FurAffinity

### Credentials

Enter your FurAffinity **username** and **password** in the Credentials section. Select **FurAffinity** from the Site dropdown.

> **NSFW content:** To download mature or explicit submissions, your FurAffinity account must have adult content enabled. Log into FurAffinity in a browser and go to **Account Settings → Browsing Settings → Show adult content**. The downloader inherits your account's content rating.

### Login Process

FurAffinity is protected by Cloudflare. The app handles this automatically:

1. When you click **Start Download**, a visible **Firefox browser window** opens (powered by Camoufox, a privacy-hardened Firefox build).
2. The app types your credentials and attempts to solve the Cloudflare Turnstile checkbox automatically.
3. Once logged in, the browser closes and downloading begins in the background.
4. Your session cookies are saved to `~/.config/faibdownloader/fa_cookies.json`. On the next run the app reuses the saved session and **no browser window appears** unless the session has expired.

### Download Modes

| Mode | What it downloads |
|---|---|
| **User Gallery** | All submissions from the gallery of a specified username. The **Target Username** field is required. |
| **User Favourites** | All favourites of a specified username. Leave Target Username blank to use your own account. |
| **Submission Notifications** | Submissions from your notification inbox (new posts from artists you watch). |

### Clear Notifications (FA)

When using **Submission Notifications** mode, enabling **Clear notifications after download** removes each downloaded submission from your FA inbox after it lands on disk. Only submissions that were successfully saved are cleared — failed downloads are never removed from your inbox.

---

## Configuring Inkbunny

### Credentials

Enter your Inkbunny **username** and **password** in the Credentials section. Select **Inkbunny** from the Site dropdown.

Inkbunny uses a direct REST API — no browser window opens. Login is fast and works reliably with no Cloudflare challenges.

> **Content ratings:** The app downloads all content your Inkbunny account is permitted to see. To enable adult content on Inkbunny, go to your [account preferences](https://inkbunny.net/account.php) and set your content rating to **General, Mature, and Adult**.

### Download Modes

| Mode | What it downloads |
|---|---|
| **User Gallery** | All submissions from a specified artist's gallery. The **Target Username** field is required. |
| **User Favourites** | All submissions favourited by a specified user. Leave Target Username blank to use your own account. |
| **Submission Notifications** | New submissions from artists you watch (your unread new-submissions inbox). |

### Clear Notifications (IB)

When using **Submission Notifications** mode, enabling **Clear notifications after download** marks each downloaded submission as read in your Inkbunny new-submissions inbox. As with FA, only successfully downloaded submissions are marked.

---

## Download Options

### Target Username

Used in **User Gallery** and **User Favourites** modes to specify whose content to download. Leave blank in Favourites mode to download your own account's favourites. Not used in Submission Notifications mode.

### Max Pages

Controls how many gallery/inbox pages the app scans before stopping. Each page typically contains up to 48–100 submissions depending on the site. Default is **25 pages**. Increase this to retrieve larger archives (up to 500 pages).

### Concurrent Downloads

Number of files downloaded simultaneously (1–5). The default of **2** is a safe starting point. Increasing this speeds up downloads but risks temporary rate-limiting — particularly on FurAffinity. Keep this at 1 or 2 unless you know the target server handles it gracefully.

### Post Delay

Pause between requests to avoid triggering rate limits.

- **Fixed** — a constant wait (e.g. `2.0 s`) after each file.
- **Variable** — a random wait between a minimum and maximum (e.g. `1.0 s` to `4.0 s`). Variable delay is more human-like and generally safer for long sessions.

### Verbose (console)

Mirrors all log messages to your terminal and shows full error tracebacks. Useful for diagnosing problems. Leave it off for normal use.

---

## Output Folder

Files are saved to sub-folders inside the chosen output directory:

```
~/Pictures/FAIBDownload/
├── FurAffinity/
│   └── Title of Submission_12345678.png
└── Inkbunny/
    └── 9876543_original_filename.jpg
```

You can change the output folder at any time by clicking **Browse…** next to the Output Folder field. The selection is remembered between sessions.

The app skips files that already exist on disk, so re-running over the same folder only downloads new additions.

---

## Troubleshooting — FurAffinity & Cloudflare

FurAffinity uses Cloudflare Turnstile to verify that visitors are human. The app handles this automatically in most cases, but the challenge can occasionally require your attention.

### The browser window opened but nothing is happening

The Turnstile widget may still be loading. **Wait up to 15 seconds** — the app retries the click automatically. If the spinner stops and the checkbox remains, click it manually in the browser window. The app continues as soon as the checkbox is ticked.

### The browser window closed but the log shows a login error

The Turnstile was not solved in time, or Cloudflare returned a challenge the app could not handle. Try the following:

1. **Click Start Download again.** A new browser window will open. Watch for the Turnstile checkbox and click it yourself if it doesn't tick automatically.
2. **Disable a VPN or proxy.** Cloudflare frequently challenges or blocks requests from known VPN IP ranges. Disconnect and try again on your regular connection.
3. **Try a different time of day.** Cloudflare challenge difficulty varies. Evening and off-peak hours tend to be more lenient.

### Login succeeds but downloads fail with "403 Forbidden"

Your FA account may not have NSFW content enabled. The submission exists on FA but your account cannot see it. Enable adult content in your FA account settings (see [Credentials](#credentials) above).

### The app asks for credentials every time even though they were saved

The saved session cookies have expired (typically after a week of inactivity). Enter your credentials and click **Start Download** — the app logs in, saves fresh cookies, and won't ask again until they expire.

To force a fresh login at any time, delete the cookie file:

```bash
rm ~/.config/faibdownloader/fa_cookies.json
```

### The app can't find the Camoufox browser

Run **Help → Setup Camoufox Browser…** and wait for the download to finish. If the menu item fails, open a terminal and run:

```bash
python3 -m camoufox fetch
```

(or from within the project directory with the venv active: `source venv/bin/activate && python3 -m camoufox fetch`)
