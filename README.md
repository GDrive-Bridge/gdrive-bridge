# GDrive Bridge

<img src="service.gdrivebridge/resources/icon.png" width="96" align="right" alt="">

A Kodi add-on that serves your **Google Drive** to Kodi as a read-only **WebDAV source** on the
same device. Add `dav://127.0.0.1:8765/` as a video source and Kodi browses, scans and plays your
Drive like any network share, with seeking and subtitles.

- **My Drive**, **Shared with me**, **Shared drives** and **Starred**, kept current from Drive's
  change feed.
- **Search** (add-on menu → *Search Google Drive*): matching files and folders open as a folder
  you can browse and play.
- **Several Google accounts**: each extra account is one more top-level folder.
- **Videos, music and pictures**: add the same source under Music or Pictures as well. iPhone
  **HEIC** photos (which Kodi can't display) are shown as JPEGs made by Google.
- **Live slideshow**: a slideshow of a Drive folder that picks up new photos as they arrive.
- **Kodi's library** reads `.nfo` files, posters/fanart and subtitles stored next to your videos,
  exactly as from any network share.
- **Set up from any phone, tablet or computer**: the add-on shows a QR code (or an address and PIN)
  for its setup page, which walks you through everything, including creating your Google client.
- **Your own Google client.** Sign-in goes straight between your device and Google; nothing passes
  through anyone else's server. See [Your own Google client](#your-own-google-client).
- **Read-only** (`drive.readonly`). It never changes or deletes anything in your Drive.

Tested on Kodi 21 (Omega) on an NVIDIA Shield (Android TV). It needs Kodi 20 or later.

## Install

Install the GDrive Bridge repository once, then install the add-on from it. Kodi then updates
GDrive Bridge automatically when a new version is released.

1. In Kodi: **Settings > System > Add-ons**, turn on **Unknown sources**.
2. **Settings > File manager > Add source**, enter
   `https://gdrive-bridge.github.io/gdrive-bridge/repo/` and name it `GDrive Bridge`.
3. **Settings > Add-ons > Install from zip file > GDrive Bridge**, choose
   `repository.gdrivebridge-1.0.0.zip`.
4. **Install from repository > GDrive Bridge Repository**, and install **GDrive Bridge**.

Each release's zip is also on [Releases](https://github.com/GDrive-Bridge/gdrive-bridge/releases/latest)
for **Install from zip file**, but a copy installed that way doesn't update itself. The add-on's
menu then shows **Turn on automatic updates** with the steps to link it to the repository (your
sign-in and settings are kept). Once linked, the menu has **Check for updates** instead.

**Reset GDrive Bridge** (add-on menu) signs out every account, clears the cached folders and puts
the settings back to their defaults; your Google client ID and secret are kept.

## Set up

1. Open **Add-ons > GDrive Bridge > Set up on phone or computer**.
2. Scan the QR code with your phone (or open the address shown and enter the PIN). The QR code
   and PIN are new each time and work once.
3. Follow **Step 1** on the setup page to create your own Google Cloud client (about 10 minutes,
   once): a **Web application** client with the redirect URI
   `https://gdrive-bridge.github.io/gdrive-bridge/callback.html`. Paste the downloaded JSON
   file into the page.
4. **Step 2: Sign in with Google.** After you allow access, Google returns your phone to the
   relay page above, which hands the sign-in straight to the setup page on your Kodi device
   (your phone must be on the same network). Nothing to copy, and the relay is a static page:
   it never sees your client secret or your sign-in, which are exchanged with Google by your
   Kodi device itself.

   With a **Desktop app** client instead, your phone ends on a page that "can't be reached"
   (`127.0.0.1`); copy its address back into the setup page.
5. Back in Kodi, open the add-on and choose **Add Google Drive to Kodi**: pick Videos, Music or
   Pictures, choose a Drive folder and name it. Add as many as you like, then let it quit Kodi
   and open Kodi again (Kodi reads its sources when it starts). For films or TV shows, set the
   content type afterwards: highlight the source, open its menu and choose **Set content**.

   To do it by hand instead: **Videos > Files > Add videos > Browse > Add network location**,
   protocol **WebDAV server (HTTP)**, server **127.0.0.1**, port **8765**.

   Add the folders that hold your media, not the whole of *My Drive*. Kodi opens every folder
   below a source when it scans it, and each folder it hasn't seen yet is a request to Google:
   a Drive with thousands of folders takes hours to scan.

### Signing in every 7 days

While your Google app is in *Testing*, Google ends the sign-in after 7 days. Kodi shows a
notification; sign in again from the setup page. To stop this, **publish** your app (optional
last step in the setup guide). Google's verification isn't needed for your own use. Publishing
asks for a home page and privacy policy; you can use this project's:

- Application home page: `https://gdrive-bridge.github.io/gdrive-bridge/`
- Privacy policy: `https://gdrive-bridge.github.io/gdrive-bridge/privacy.html`
- Authorised domain: `gdrive-bridge.github.io`

Google then shows *"Your branding is not being shown to users"* (the pages aren't on your own
domain) and *"Your app requires verification"*. Both are expected and don't stop you publishing;
verification only matters for apps with more than 100 users. Sign-in shows an *unverified app*
warning instead: choose **Advanced > Go to … (unsafe)**.

Then sign in once more (sign-ins made while testing keep the 7-day limit).

## Several accounts

On the setup page, under **More Google accounts**, choose **Add another Google account** and sign
in with it (while your Google app is in *Testing*, add that account as a test user first). It
appears in Kodi as a folder named after the account, next to *My Drive*, with its own *My
Drive*, *Shared with me*, *Shared drives* and *Starred*. Rename or remove it on the same page.

## Live slideshow

**Add-ons > GDrive Bridge > Live slideshow**: pick a Drive folder and choose whether to include
its subfolders. When a new photo appears in that folder, the slideshow starts again at that photo
at the next check for changes (every 60 seconds unless you change it).

- It waits while the slideshow is paused or a video is playing, then restarts in the next gap.
- Shuffle follows Kodi's own Pictures setting, and a restart starts a new round.
- It watches the folder until you close the slideshow. It doesn't carry on after Kodi restarts.

## Music

Add a music folder with **Add Google Drive to Kodi** in the add-on menu, then browse and play. Each new part of a file has to be requested from Google, so a song takes several
seconds to start.

## Video quality

Google Drive's API only serves the original file, not the lower-quality copies the Drive website
streams. If a video stutters on your connection, give Kodi more memory for buffering (**Settings >
Services > Caching** on Kodi 21, at the Advanced settings level) or keep a lower-bitrate copy of
the video in your Drive.

## Settings

All settings are on the setup page and in Kodi's add-on settings:

| Setting | Default | |
|---|---|---|
| Show *Shared with me* / *Shared drives* / *Starred* | on | Top-level folders. Don't give the top level a content type: starred items would be scanned twice |
| Show iPhone HEIC photos as JPEG | on | Uses Google's JPEG rendering (up to 3840 px) |
| Live slideshows show pictures only | on | Skips the folder's videos and music. Off: Kodi's own Pictures setting decides |
| Hide files and folders starting with a dot | on | `.git`… |
| Hide archives (.zip, .rar, .7z, .apk…) | on | See below |
| Check Drive for changes every | 60 s | |
| WebDAV port | 8765 | Update your Kodi source if you change it |
| Share Drive with other devices on my network | off | Basic-auth WebDAV for another Kodi |
| Setup page | opens when you choose *Set up on phone or computer* in Kodi and closes after 15 minutes idle; can be set to always on (port 8766) | a new browser needs the one-time QR link or PIN shown in Kodi; it then stays logged in for 30 days |

**Why archives are hidden:** Kodi treats `.zip`/`.rar`/`.apk` files as folders and opens them to
look inside, on its screen thread, when it lists a folder. Over Google Drive each read is a
separate download, so a folder containing an archive froze Kodi on "Content Loading" for tens of
seconds. Kodi can't play archives anyway.

**Why folders are read ahead:** on Kodi 21 (Android) we saw that while Kodi's screen waits on
a slow folder, all of Kodi's Python add-ons can freeze until Kodi gives up after 20 seconds. So
GDrive Bridge answers from its cache wherever it can: it lists the subfolders of whatever Kodi
opens in the background, serves older copies at once while refreshing them, and *Search* runs
before its window opens. Only a folder that has never been listed makes Kodi wait.

## Why WebDAV

WebDAV is a file-sharing protocol that Kodi supports natively, alongside SMB and NFS. GDrive
Bridge runs a small read-only WebDAV server inside Kodi, so Kodi sees your Drive as an ordinary
network share:

- Folders are real folders, and every file comes with its size and modification date, so
  sorting by date or size works and Kodi's library scans Drive like a NAS.
- It works everywhere Kodi accepts a source: Videos, Pictures, the file manager and the
  library scanner. Nothing needs exporting first.
- Playback goes through Kodi's own network player, with seeking (byte ranges) and the
  subtitles, `.nfo` files and artwork stored next to each video.
- Films and TV shows go in Kodi's own library: set a content type on the source and scan. New
  files arrive with Kodi's normal library update, with no separate list of files to keep up to date.
- Browsing doesn't wait on Google: folders come from a local copy kept current by Drive's change
  feed.

A plain `http://` source would only give Kodi a web page of links to read, without sizes or
dates.

**Connections.** Everything between GDrive Bridge and Google is HTTPS. Kodi itself reaches the
WebDAV server on `127.0.0.1`, so that traffic never leaves the device. With **Share Drive with
other devices on my network** turned on, other devices connect over plain HTTP, protected by a
username and password. Only turn it on for a home network you trust.

## Your own Google client

Each person signs in with a Google client they create themselves (about 10 minutes, once). There
is no shared app behind GDrive Bridge, so:

- Nothing central can be blocked, rate-limited or retired for everyone at once.
- Google's verification isn't needed for your own use, and the Drive usage limits are your own.
- Your client secret is only ever exchanged between your Kodi device and Google. The optional
  relay is a static page and has no server of its own.

## Privacy

See the [privacy policy](https://gdrive-bridge.github.io/gdrive-bridge/privacy.html). In short:
everything stays on your device and is exchanged only with Google.

## Development

- `service.gdrivebridge/` is the add-on. `python3 tools/build_zip.py` builds the release zip.
- `repository.gdrivebridge/` is the repository add-on. `python3 tools/build_repo.py` rebuilds
  the Kodi repository in `docs/repo/` (published by GitHub Pages); run it for every release.
- Tests (standard library only): `python3 tools/test_local.py` and `python3 tools/test_drive.py`.
- `tools/make_icon.py` (needs Pillow) draws the icon and fanart.
- `docs/` is the GitHub Pages site (home page, privacy policy, optional sign-in relay page).

## Licence

MIT. See [LICENSE](LICENSE).
