# FA & Inkbunny Downloader

A desktop application for downloading galleries, favourites, and submission notifications from **FurAffinity**, **Inkbunny**, **Weasyl** and **SoFurry**. It runs on Linux and is distributed as a self-contained AppImage — no Python or system dependencies required.

---

## Table of Contents

1. [Installation](#installation)
2. [First Launch](#first-launch)
3. [Configuring FurAffinity](#configuring-furaffinity)
4. [Configuring Inkbunny](#configuring-inkbunny)
5. [Configuring Weasyl](#configuring-weasyl)
6. [Configuring SoFurry](#configuring-sofurry)
7. [Download Options](#download-options)
8. [Output Folder](#output-folder)
9. [Troubleshooting — FurAffinity & Cloudflare](#troubleshooting--furaffinity--cloudflare)

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
```

Then launch with whichever shell you use:

| Shell | Command |
|---|---|
| Bash / Zsh / sh | `./run.sh` |
| Fish | `./run.fish` |

Both launchers do the same thing: create a Python virtual environment if one does not exist, install or update all dependencies from `requirements.txt`, download the Camoufox browser on first run, and start the app.

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

## Configuring Weasyl

### Credentials

Select **Weasyl** from the Site dropdown and enter your Weasyl **username** and **password**. No browser window opens. After the first login the session is remembered, so later runs don't sign in again.

> **Two-factor authentication** is not supported. If your Weasyl account has 2FA turned on, the login will stop with a message saying so.

> **Content ratings and filters:** the app downloads what your Weasyl account can see. That means your account's maximum rating setting applies, and so do your **blocked tags**: anything they hide on the website is skipped here too. Uncheck **Include adult content** to limit a run to General-rated work.

### Download Modes

| Mode | What it downloads |
|---|---|
| **User Gallery** | All submissions in a specified artist's gallery. The **Target Username** field is required. |
| **User Favourites** | All submissions favourited by a specified user. Leave Target Username blank to use your own account. |
| **Submission Notifications** | New submissions from artists you watch (your Weasyl submission inbox). |

Pictures, characters and audio files are downloaded. Writing (Literary) is skipped, as are submissions with only an embedded video or player and no file to download.

### Clear Notifications (Weasyl)

When using **Submission Notifications** mode, enabling **Clear notifications after download** removes each downloaded submission from your Weasyl inbox. As with the other sites, only submissions that were successfully saved are removed.

---

## Configuring SoFurry

### Credentials

Select **SoFurry** from the Site dropdown and enter your SoFurry **email address** (not your username) and **password**. No browser window opens.

SoFurry sends you a "New login" notification every time an app signs in, so the session is remembered after the first run and reused for later ones.

> **Two-factor authentication** is not supported.

> **Content ratings:** the app downloads what your SoFurry account can see. Uncheck **Include adult content** to switch the run to SoFurry's SFW mode.

### Download Modes

| Mode | What it downloads |
|---|---|
| **User Gallery** | All submissions in a specified artist's gallery. The **Target Username** field is required; use the name shown in their profile address (`sofurry.com/u/<name>`). |
| **User Favourites** | All submissions a user has liked. Leave Target Username blank to use your own account. |
| **Submission Notifications** | Your unread "new upload" notifications from artists you follow. |
| **Following Feed** | The newest submissions from artists you follow — the **Submissions** tab of your SoFurry feed. Set **Max Submissions** to choose how many (for example the latest 50). SoFurry's feed goes back at most 10,000 submissions. |

Artwork, photos, 3D work and music are downloaded. A submission with several pictures is saved as separate numbered files (`…_01.png`, `…_02.png`, …). Writing is skipped. Submissions whose artist has turned off downloads are skipped too: SoFurry only offers a reduced-quality copy of those, and the app never saves a reduced copy in place of the original.

### Clear Notifications (SoFurry)

When using **Submission Notifications** mode, enabling **Clear notifications after download** marks each downloaded submission's notification as read. Only submissions that were successfully saved are marked.

SoFurry has no way to delete notifications, so read ones stay in your notification list on the site, shown as read. The app only downloads unread ones, so they are not downloaded again.

---

## Download Options

### Target Username

Used in **User Gallery** and **User Favourites** modes to specify whose content to download. Leave blank in Favourites mode to download your own account's favourites. Not used in Submission Notifications mode.

### Max Pages

Controls how many gallery/inbox pages the app scans before stopping. Each page holds 48 (FurAffinity, SoFurry galleries and favourites), 50 (SoFurry notifications), 60 (Weasyl favourites) or 100 (Inkbunny, Weasyl galleries and notifications, SoFurry feed) submissions. Default is **25 pages**. Increase this to retrieve larger archives (up to 500 pages).

### Max Submissions

**Weasyl and SoFurry only.** Stops after this many submissions, newest first. Use it when you want "the latest 20" rather than a number of pages; it is the natural way to use SoFurry's **Following Feed**. FurAffinity and Inkbunny use Max Pages alone, and the field is greyed out for them. **No limit** (0) is the default. When a limit is set, the app only scans as many pages as it needs to reach it, and Max Pages still applies on top. With **Clear notifications after download**, only the submissions actually downloaded are cleared; the rest stay in your inbox for the next run.

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
│   └── 12345678_artistname_original_filename.png
├── Inkbunny/
│   └── 9876543_original_filename.jpg
├── Weasyl/
│   └── 1234567_artistname-submission-title.jpg
└── SoFurry/
    ├── AbCd1234_Submission Title_artistname.png
    └── EfGh5678_Submission Title_artistname_01.png
```

All sites start the file name with the submission ID; the FurAffinity name also
carries the artist's account name. Weasyl does not keep the uploader's original
file name, so its files are named after the artist and the submission title.
Weasyl characters start with `c` (`c12345_…`) so they never clash with a
submission of the same number. SoFurry submission IDs are short letter codes
rather than numbers, and its file names use the name SoFurry gives the download.
Multi-picture submissions get `_01`, `_02`, … on the end.

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
