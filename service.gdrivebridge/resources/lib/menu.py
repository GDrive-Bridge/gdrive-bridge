"""GDrive Bridge menu: setup QR, status, refresh, sign out, help."""
import glob
import json
import os
import secrets
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

import xbmc
import xbmcgui

from . import common, qr

TITLE = 'GDrive Bridge'
REPO_ID = 'repository.gdrivebridge'
REPO_URL = 'https://gdrive-bridge.github.io/gdrive-bridge/repo/'
UPDATES_HELP = (
    'This copy of GDrive Bridge doesn\'t update itself: it was installed from a zip file, and '
    'Kodi only updates add-ons from the repository they came from.[CR][CR]'
    '[B]1.[/B] Settings > File manager > Add source: enter [B]%s[/B] and name it '
    'GDrive Bridge.[CR]'
    '[B]2.[/B] Settings > Add-ons > Install from zip file > GDrive Bridge: choose '
    'repository.gdrivebridge-1.0.0.zip.[CR]'
    '[B]3.[/B] Add-ons > My add-ons > Services > GDrive Bridge: choose [B]Versions[/B] and pick '
    'the version from GDrive Bridge Repository.[CR][CR]'
    'Your sign-in and settings are kept. After that Kodi installs new versions automatically.'
    % REPO_URL)
BACK_ACTIONS = (9, 10, 92, 216, 247, 257, 275, 61448)


def service_status():
    return common.read_json(common.status_path())


def notify(command, data=None):
    xbmc.executeJSONRPC(json.dumps({
        'jsonrpc': '2.0', 'id': 1, 'method': 'JSONRPC.NotifyAll',
        'params': {'sender': common.ADDON_ID, 'message': command, 'data': data or {}}}))


def wait_for(check, seconds):
    monitor = xbmc.Monitor()
    end = time.time() + seconds
    while time.time() < end:
        st = service_status()
        if st and check(st):
            return st
        if monitor.waitForAbort(0.25):
            break
    return service_status()


class SetupDialog(xbmcgui.WindowDialog):
    """Full-screen panel with a QR code for the setup page; closes itself after sign-in."""

    def __init__(self, link, st, pin):
        xbmcgui.WindowDialog.__init__(self)
        self.done = threading.Event()
        self.was_signed_in = st['signed_in']
        profile = common.profile_dir()
        for old in glob.glob(os.path.join(profile, 'qr-*.png')):
            try:
                os.remove(old)
            except OSError:
                pass
        qr_path = os.path.join(profile, 'qr-%s.png' % secrets.token_hex(4))   # new name: no texture cache
        qr.save_qr(link, qr_path, scale=12)
        bg = os.path.join(profile, 'panel.png')
        if not os.path.exists(bg):
            qr.write_png(bg, 1, 1, lambda x, y: (14, 20, 27, 244))
        icon = os.path.join(common.addon_path(), 'resources', 'icon.png')

        white, muted, accent = '0xFFFFFFFF', '0xFFB8C7D4', '0xFF17B2E7'
        self.addControl(xbmcgui.ControlImage(0, 0, 1280, 720, bg))
        self.addControl(xbmcgui.ControlImage(80, 46, 72, 72, icon))
        self.addControl(xbmcgui.ControlLabel(172, 62, 1000, 40, '[B]GDrive Bridge setup[/B]',
                                             font='font13', textColor=white))
        self.addControl(xbmcgui.ControlImage(80, 150, 420, 420, qr_path, aspectRatio=0))
        body = xbmcgui.ControlTextBox(540, 150, 680, 130, font='font13', textColor=white)
        self.addControl(body)
        body.setText('Scan the code with your phone\'s camera to open the setup page. Everything '
                     'can be set there: your Google client, sign-in and all settings.[CR][CR]'
                     'On a computer or tablet, open this address and enter the PIN:')
        self.addControl(xbmcgui.ControlLabel(540, 290, 680, 40, '[B]%s[/B]' % st['setup_url'],
                                             font='font13', textColor=accent))
        self.addControl(xbmcgui.ControlLabel(540, 334, 680, 40, 'PIN: [B]%s[/B]' % pin,
                                             font='font13', textColor=white))
        self.state = xbmcgui.ControlLabel(540, 420, 680, 40, '', font='font13', textColor=muted)
        self.addControl(self.state)
        self.addControl(xbmcgui.ControlLabel(540, 530, 680, 40, 'Press Back to close.',
                                             font='font13', textColor=muted))
        self._show_state(st)

    def _show_state(self, st):
        if st['signed_in'] and not st['auth_error']:
            self.state.setLabel('Signed in as %s' % (st['email'] or 'your account'))
        elif not st['has_client']:
            self.state.setLabel('Waiting for your Google client details...')
        else:
            self.state.setLabel('Waiting for Google sign-in...')

    def poll(self):
        monitor = xbmc.Monitor()
        while not self.done.is_set() and not monitor.abortRequested():
            st = service_status()
            if st:
                self._show_state(st)
                if st['signed_in'] and not st['auth_error'] and not self.was_signed_in:
                    xbmcgui.Dialog().notification(TITLE, 'Signed in as %s' % st['email'],
                                                  os.path.join(common.addon_path(), 'resources',
                                                               'icon.png'), 4000)
                    self.done.wait(3)
                    self.close()
                    return
            self.done.wait(1)

    def onAction(self, action):
        if action.getId() in BACK_ACTIONS:
            self.close()


def open_setup():
    token = secrets.token_urlsafe(9)
    pin = '%06d' % secrets.randbelow(1000000)     # one-time, like the QR link
    asked = time.time() - 1
    notify('setup', {'token': token, 'pin': pin})
    # the status file outlives a killed Kodi, so wait for one written after this request
    st = wait_for(lambda s: s.get('updated', 0) >= asked and (s['setup_running'] or
                                                              s['setup_error']), 5)
    if st is None:
        xbmcgui.Dialog().ok(TITLE, 'The GDrive Bridge service isn\'t running. Restart Kodi and try '
                                   'again.')
        return
    if not st['setup_running']:
        xbmcgui.Dialog().ok(TITLE, st['setup_error'] or 'The setup page couldn\'t start.')
        return
    dialog = SetupDialog(st['setup_url'] + '?t=' + token, st, pin)
    poller = threading.Thread(target=dialog.poll)
    poller.daemon = True
    poller.start()
    dialog.doModal()
    dialog.done.set()
    del dialog


def show_status(st):
    synced = time.strftime('%d %b %H:%M:%S', time.localtime(st['synced'])) if st['synced'] \
        else 'not yet'
    lines = [
        '[B]Account:[/B] %s' % (st['email'] or 'signed in') if st['signed_in'] else
        '[B]Account:[/B] not signed in',
    ]
    if st['auth_error']:
        lines.append('[B]Sign-in problem:[/B] %s' % st['auth_error'])
    lines += [
        '[B]Kodi source path:[/B] %s' % st['webdav'],
        '[B]Drive items cached:[/B] %d in %d folders' % (st['items'], st['folders']),
        '[B]Last change check:[/B] %s' % synced,
        '[B]Setup page:[/B] %s%s' % (st['setup_url'],
                                     '' if st['setup_running'] else ', closed now'),
    ]
    lines.append('[B]Updates:[/B] %s' % ('automatic, from GDrive Bridge Repository'
                                         if has_repository() else
                                         'not automatic (see "Turn on automatic updates")'))
    if st['error']:
        lines.append('[B]Last error:[/B] %s' % st['error'])
    for a in st.get('accounts', [])[1:]:
        lines.append('[B]Also:[/B] %s, in folder /%s/%s' % (
            a['email'] or 'unknown', a['folder'] or '?',
            ' - sign-in problem: ' + a['auth_error'] if a['auth_error'] else ''))
    xbmcgui.Dialog().textviewer('%s status' % TITLE, '[CR]'.join(lines))


def search(st):
    """Ask for a term and open Drive's matching files and folders as a Kodi folder."""
    accounts = [a for a in st.get('accounts', []) if a['signed_in']]
    prefix = ''
    if len(accounts) > 1:
        labels = ['%s (%s)' % (a['email'], 'main' if a['id'] == 'main' else a['folder'])
                  for a in accounts]
        choice = xbmcgui.Dialog().select('Search which account?', labels)
        if choice < 0:
            return
        if accounts[choice]['id'] != 'main':
            prefix = urllib.parse.quote(accounts[choice]['folder'], safe='') + '/'
    term = xbmcgui.Dialog().input('Search Google Drive (file or folder name)').strip()
    if not term:
        return
    rel = '%sSearch/%s/' % (prefix, urllib.parse.quote(term, safe=''))
    # Run the search here first, so the window opens from the add-on's cache: Kodi's
    # screen must not wait on Google (see cache.py, read-ahead).
    busy = xbmcgui.DialogProgressBG()
    busy.create(TITLE, 'Searching Google Drive for "%s"...' % term)
    try:
        port = st['webdav'].rstrip('/').rsplit(':', 1)[1]
        req = urllib.request.Request('http://127.0.0.1:%s/%s' % (port, rel), method='PROPFIND',
                                     headers={'Depth': '1'})
        with urllib.request.urlopen(req, timeout=90) as resp:
            found = resp.read().count(b'<D:response>') - 1
    except (urllib.error.URLError, OSError, ValueError) as e:
        busy.close()
        xbmcgui.Dialog().ok(TITLE, 'The search didn\'t work: %s' % e)
        return
    busy.close()
    if found <= 0:
        xbmcgui.Dialog().ok(TITLE, 'Nothing in Google Drive is named like "%s".' % term)
        return
    xbmc.executebuiltin('ActivateWindow(Videos,"%s%s",return)' % (st['webdav'], rel))


def add_pictures_help(st):
    xbmcgui.Dialog().textviewer('Add Google Drive to Pictures', '[CR]'.join([
        '1. Go to [B]Pictures[/B] and choose [B]Add pictures...[/B]',
        '2. Choose [B]Browse[/B], then [B]Add network location...[/B]',
        '   Protocol: [B]WebDAV server (HTTP)[/B], Server address: [B]127.0.0.1[/B], '
        'Port: [B]%s[/B]. Leave the rest empty and choose OK.' % st['webdav'].rsplit(':', 1)[1]
        .strip('/'),
        '   (Or type [B]%s[/B] as the path.)' % st['webdav'],
        '3. Open it, pick a photo folder, choose OK and name it.',
        '',
        'Browse it and press [B]Play[/B] on a folder for a slideshow. iPhone HEIC photos are',
        'shown using Google\'s JPEG version (setting: Show iPhone HEIC photos as JPEG).',
    ]))
    if xbmcgui.Dialog().yesno(TITLE, 'Open the Pictures sources now?'):
        xbmc.executebuiltin('ActivateWindow(Pictures,sources://pictures/,return)')


def add_source_help(st):
    xbmcgui.Dialog().textviewer('Add Google Drive to Videos', '[CR]'.join([
        '1. Go to [B]Videos > Files > Add videos...[/B]',
        '2. Choose [B]Browse[/B], then [B]Add network location...[/B]',
        '   Protocol: [B]WebDAV server (HTTP)[/B], Server address: [B]127.0.0.1[/B], '
        'Port: [B]%s[/B]. Leave the rest empty and choose OK.' % st['webdav'].rsplit(':', 1)[1]
        .strip('/'),
        '   (Or type [B]%s[/B] as the path.)' % st['webdav'],
        '3. Open it, pick a folder such as My Drive/Movies, and choose OK.',
        '4. Name the source, then set the content type (Movies or TV shows) so Kodi scans it.',
        '',
        'Leave "Clean library on startup" off. A scan never removes items; only cleaning does.',
    ]))
    if xbmcgui.Dialog().yesno(TITLE, 'Open the Videos sources now?'):
        xbmc.executebuiltin('ActivateWindow(Videos,sources://video/,return)')


def has_repository():
    return xbmc.getCondVisibility('System.HasAddon(%s)' % REPO_ID)


def updates_help():
    xbmcgui.Dialog().textviewer('%s: automatic updates' % TITLE, UPDATES_HELP)


def updates_hint_once():
    """Point a zip install at the repository, once (the menu keeps an entry for it)."""
    marker = os.path.join(common.profile_dir(), 'updates_hint_shown')
    if has_repository() or os.path.exists(marker):
        return
    try:
        open(marker, 'w').close()
    except OSError:
        return
    updates_help()


def check_updates():
    xbmc.executebuiltin('UpdateAddonRepos')
    xbmcgui.Dialog().notification(TITLE, 'Checking for updates. A new version installs by itself.')


def reset(st):
    if not xbmcgui.Dialog().yesno(TITLE, 'Reset GDrive Bridge? This signs out every account (also '
                                         'with Google), clears the cached Drive folders and puts '
                                         'all settings back to their defaults, with a new PIN. '
                                         'Your Google client ID and secret are kept.'):
        return
    notify('reset')
    wait_for(lambda s: not s['signed_in'] and len(s.get('accounts', [])) <= 1, 10)
    xbmcgui.Dialog().notification(TITLE, 'Reset done. Set up again from the menu.')


def sign_out(st):
    if not st['signed_in']:
        xbmcgui.Dialog().ok(TITLE, 'Not signed in.')
        return
    if xbmcgui.Dialog().yesno(TITLE, 'Sign out %s? The sign-in is also revoked with Google.'
                              % (st['email'] or '')):
        notify('signout')
        wait_for(lambda s: not s['signed_in'], 10)
        xbmcgui.Dialog().notification(TITLE, 'Signed out')


def menu():
    st = service_status()
    if st is None:
        xbmcgui.Dialog().ok(TITLE, 'The GDrive Bridge service isn\'t running yet. Try again in a '
                                   'moment.')
        return
    updates_hint_once()
    entries = [('Set up on phone or computer', open_setup),
               ('Status', lambda: show_status(service_status() or st)),
               ('Refresh Drive folders', lambda: (notify('refresh'), xbmcgui.Dialog().notification(
                   TITLE, 'Re-reading Drive folders'))),
               ('Search Google Drive', lambda: search(service_status() or st)),
               ('Add Google Drive to Videos', lambda: add_source_help(st)),
               ('Add Google Drive to Pictures', lambda: add_pictures_help(st))]
    if st['signed_in']:
        entries.append(('Sign out', lambda: sign_out(st)))
    if has_repository():
        entries.append(('Check for updates', check_updates))
    else:
        entries.insert(1, ('Turn on automatic updates', updates_help))
    entries.append(('Reset GDrive Bridge', lambda: reset(st)))
    entries.append(('Settings', lambda: common.addon().openSettings()))
    header = '%s - %s' % (TITLE, st['email'] if st['signed_in'] else 'not signed in')
    choice = xbmcgui.Dialog().select(header, [label for label, _ in entries])
    if choice >= 0:
        entries[choice][1]()


def main():
    arg = sys.argv[1] if len(sys.argv) > 1 else ''
    if arg == 'setup':
        open_setup()
    elif arg == 'signout':
        st = service_status()
        if st:
            sign_out(st)
    elif arg == 'refresh':
        notify('refresh')
    elif arg == 'search':
        st = service_status()
        if st:
            search(st)
    elif arg == 'stopscan':
        # UpdateLibrary(video) cancels a running video scan instead of starting one
        if xbmc.getCondVisibility('Library.IsScanningVideo'):
            xbmc.executebuiltin('UpdateLibrary(video)')
    else:
        menu()
