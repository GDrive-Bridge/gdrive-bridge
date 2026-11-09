# GDrive Bridge

<img src="service.gdrivebridge/resources/icon.png" width="96" align="right" alt="">

A Kodi add-on that serves your **Google Drive** to Kodi as a read-only **WebDAV source** on the
same device. Add `dav://127.0.0.1:8765/` as a video source and Kodi browses, scans and plays your
Drive like any network share, with seeking and subtitles.

- **My Drive**, **Shared with me** and **Shared drives**, kept current from Drive's change feed.
- **Set up from any phone, tablet or computer**: the add-on shows a QR code (or an address and PIN)
  for its setup page, which walks you through everything, including creating your Google client.
- **Your own Google client.** Sign-in goes straight between your device and Google; nothing passes
  through anyone else's server.
- **Read-only** (`drive.readonly`). It never changes or deletes anything in your Drive.

Tested on Kodi 21 (Omega) on an NVIDIA Shield (Android TV). It needs Kodi 19 or later.

## Install

1. Download `service.gdrivebridge-<version>.zip` from
   [Releases](https://github.com/GDrive-Bridge/gdrive-bridge/releases/latest).
2. In Kodi: **Settings > System > Add-ons**, turn on **Unknown sources**.
3. **Settings > Add-ons > Install from zip file**, choose the zip.

## Set up

1. Open **Add-ons > GDrive Bridge > Set up on phone or computer**.
2. Scan the QR code with your phone (or open the address shown and enter the PIN).
3. Follow **Step 1** on the setup page to create your own Google Cloud client (about 10 minutes,
   once). Paste the downloaded JSON file into the page.
4. **Step 2: Sign in with Google.** Your phone ends on a page that "can't be reached"
   (`127.0.0.1`); that's expected. Copy its address back into the setup page.
5. In Kodi: **Videos > Files > Add videos > Browse > Add network location**: protocol
   **WebDAV server (HTTP)**, server **127.0.0.1**, port **8765**. Pick a folder, name it, and set
   the content type for folders that hold films or TV shows.

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

## Settings

All settings are on the setup page and in Kodi's add-on settings:

| Setting | Default | |
|---|---|---|
| Show *Shared with me* / *Shared drives* | on | Top-level folders |
| Hide files and folders starting with a dot | on | `.git`, `.ipynb_checkpoints`… |
| Hide archives (.zip, .rar, .7z, .apk…) | on | See below |
| Check Drive for changes every | 60 s | |
| WebDAV port | 8765 | Update your Kodi source if you change it |
| Share Drive with other devices on my network | off | Basic-auth WebDAV for another Kodi |
| Setup page | always on, port 8766 | PIN or one-time QR link needed |

**Why archives are hidden:** Kodi treats `.zip`/`.rar`/`.apk` files as folders and opens them to
look inside, on its screen thread, when it lists a folder. Over Google Drive each read is a
separate download, so a folder containing an archive froze Kodi on "Content Loading" for tens of
seconds. Kodi can't play archives anyway.

## Privacy

See the [privacy policy](https://gdrive-bridge.github.io/gdrive-bridge/privacy.html). In short:
everything stays on your device and is exchanged only with Google.

## Development

- `service.gdrivebridge/` is the add-on. `python3 tools/build_zip.py` builds the release zip.
- Tests (standard library only): `python3 tools/test_local.py` and `python3 tools/test_drive.py`.
- `tools/make_icon.py` (needs Pillow) draws the icon and fanart.
- `docs/` is the GitHub Pages site (home page, privacy policy, optional sign-in relay page).

## Licence

MIT. See [LICENSE](LICENSE).
