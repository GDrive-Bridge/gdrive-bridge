"""The background service: owns sign-in, the Drive cache, the WebDAV server and the setup page."""
import json
import os
import threading
import time

import xbmc

from . import common
from .accounts import MAIN, Accounts
from .auth import AuthError, NotSignedIn, SignIn
from .cache import NotReady
from .drive import Drive, DriveError
from .setup import SetupServer
from .webdav import WebDAVServer

SETUP_IDLE = 15 * 60


class Bridge(object):
    def __init__(self, drive_factory=Drive):
        self.accounts = Accounts(drive_factory)
        self.signin = SignIn()
        common.profile_dir()          # resolve paths here, on the service thread
        common.addon_path()
        self.settings = common.get_settings()   # live view, also updated by the setup page
        self.applied = dict(self.settings)      # what the servers were started with
        self.webdav = None
        self.webdav_error = ''
        self.setup = None
        self.setup_error = ''
        self.setup_requested = 0
        self.wake = threading.Event()
        self.stopping = threading.Event()
        self.lock = threading.RLock()
        self.sync_thread = None
        self._published = None
        self.main_actions = []          # Kodi calls to make from the service's main thread
        self.address = common.lan_address()

    # the main account (the only one before multiple accounts existed)
    @property
    def creds(self):
        return self.accounts.main.creds

    @property
    def drive(self):
        return self.accounts.main.drive

    @property
    def tree(self):
        return self.accounts.main.tree

    def _configure(self, account):
        s = self.settings
        account.tree.configure(s['show_shared'], s['show_drives'], s['hide_dot'],
                               s['hide_archives'], s['show_starred'], s['heic_as_jpeg'])

    # ---- lifecycle ------------------------------------------------------
    def start(self):
        common.ensure_pin()
        self.settings = common.get_settings()
        self.applied = dict(self.settings)
        common.set_debug(self.settings['debug'])
        for account in self.accounts.all():
            self._configure(account)
            account.tree.load()
        self._start_webdav()
        if self.settings['setup_mode'] == common.SETUP_ALWAYS:
            self._start_setup()
        self.sync_thread = threading.Thread(target=self._sync_loop, name='gdb-sync')
        self.sync_thread.daemon = True
        self.sync_thread.start()
        self.publish()

    def stop(self):
        self.stopping.set()
        self.wake.set()
        self._stop_setup()
        self._stop_webdav()
        for account in self.accounts.all():
            account.tree.save(force=True)
            account.drive.pool.clear()
        try:
            os.remove(common.status_path())
        except OSError:
            pass

    def tick(self):
        """Called about once a second from the service loop."""
        # Kodi calls happen here, on the service's main thread, never on server threads
        while self.main_actions:
            builtin = self.main_actions.pop(0)
            try:
                xbmc.executebuiltin(builtin)
            except Exception as e:
                common.log('builtin failed: %s' % e)
        if self.setup and self.settings['setup_mode'] == common.SETUP_ON_DEMAND:
            idle_since = max(self.setup.last_activity, self.setup_requested)
            if time.time() - idle_since > SETUP_IDLE:
                common.log('setup page closed after %d minutes idle' % (SETUP_IDLE // 60))
                self._stop_setup()
                self.publish()

    # ---- servers --------------------------------------------------------
    def _start_webdav(self):
        s = self.settings
        lan = s['lan_share'] and bool(s['lan_password'])
        host = '0.0.0.0' if lan else '127.0.0.1'
        try:
            self.webdav = WebDAVServer((host, s['webdav_port']), self.accounts,
                                       (s['lan_user'], s['lan_password']) if lan else None,
                                       on_callback=self._tv_callback)
        except OSError as e:
            self.webdav = None
            self.webdav_error = 'WebDAV port %d unavailable: %s' % (s['webdav_port'], e)
            common.log(self.webdav_error, xbmc.LOGERROR)
            return
        self.webdav_error = ('Sharing is on but has no password, so only this Kodi can use it.'
                             if s['lan_share'] and not lan else '')
        t = threading.Thread(target=self.webdav.serve_forever, name='gdb-webdav')
        t.daemon = True
        t.start()
        common.log('WebDAV on %s:%d' % (host, s['webdav_port']))

    def _stop_webdav(self):
        if self.webdav:
            self.webdav.close_all()
            self.webdav = None

    def _start_setup(self):
        if self.setup:
            return True
        try:
            self.setup = SetupServer(('0.0.0.0', self.settings['setup_port']), self)
        except OSError as e:
            self.setup = None
            self.setup_error = 'Setup page port %d unavailable: %s' % (self.settings['setup_port'], e)
            common.log(self.setup_error, xbmc.LOGERROR)
            return False
        self.setup_error = ''
        t = threading.Thread(target=self.setup.serve_forever, name='gdb-setup')
        t.daemon = True
        t.start()
        common.log('setup page on port %d' % self.settings['setup_port'])
        return True

    def _restart_setup(self):
        with self.lock:
            was_running = self.setup is not None
            self._stop_setup()
            if was_running or self.settings['setup_mode'] == common.SETUP_ALWAYS:
                self.setup_requested = time.time()
                self._start_setup()
            self.publish()

    def _stop_setup(self):
        if self.setup:
            self.setup.shutdown()
            self.setup.server_close()
            self.setup = None

    # ---- events ---------------------------------------------------------
    def on_settings_changed(self):
        with self.lock:
            old, new = self.applied, common.get_settings()
            self.settings = dict(new)
            self.applied = new
            common.set_debug(new['debug'])
            changed = {k for k in new if new[k] != old.get(k)}
            if not changed:
                return
            common.debug('settings changed: %s' % ', '.join(sorted(changed - {'client_secret',
                                                                             'lan_password'})))
            if changed & {'webdav_port', 'lan_share', 'lan_user', 'lan_password'}:
                self._stop_webdav()
                self._start_webdav()
            if 'setup_port' in changed:
                # let the page that saved this finish its response first
                timer = threading.Timer(2.0, self._restart_setup)
                timer.daemon = True
                timer.start()
            elif 'setup_mode' in changed:
                if new['setup_mode'] == common.SETUP_ALWAYS:
                    self._start_setup()
                else:
                    self.setup_requested = time.time()   # closes once idle
            if changed & {'show_shared', 'show_drives', 'hide_dot', 'hide_archives',
                          'show_starred', 'heic_as_jpeg'}:
                for account in self.accounts.all():
                    self._configure(account)
            if 'lan_address' in changed:
                self.address = common.lan_address()
            if 'poll_interval' in changed:
                self.wake.set()
            self.publish()

    def set_settings(self, values):
        """Save settings from the setup page. The live view updates at once; servers
        are reconfigured when Kodi reports the change (on_settings_changed)."""
        for key, value in values.items():
            common.set_setting(key, value)
            self.settings[key] = value

    def on_notification(self, sender, method, data):
        if sender != common.ADDON_ID:
            return
        command = method.split('.')[-1]
        try:
            payload = json.loads(data) if data else {}
        except ValueError:
            payload = {}
        if command == 'setup':
            with self.lock:
                self.setup_requested = time.time()
                if self._start_setup() and payload.get('token'):
                    self.setup.add_link(payload['token'])
            self.publish()
        elif command == 'refresh':
            self.request_refresh()
        elif command == 'signout':
            self.sign_out(payload.get('account', MAIN))

    # ---- account --------------------------------------------------------
    def finish_signin(self, pasted):
        """Complete a sign-in for the account it was started for. Returns the email."""
        s = self.settings
        if not (s['client_id'] and s['client_secret']):
            raise AuthError('Enter the client ID and secret first.')
        response, target = self.signin.finish(pasted, s['client_id'], s['client_secret'])
        # adding an account before the main one is signed in just fills the main slot
        new = target == 'new' and self.accounts.main.creds.signed_in
        account = self.accounts.create() if new else (self.accounts.get(target)
                                                      or self.accounts.main)
        previous = account.creds.email
        account.creds.save(response, s['client_id'], s['client_secret'])
        try:
            user = account.drive.about()
            email, name = user.get('emailAddress', ''), user.get('displayName', '')
        except (DriveError, AuthError) as e:
            common.log('signed in, but reading the account failed: %s' % e)
            email = name = ''
        account.creds.set_identity(email, name)
        if new:
            existing = self.accounts.find_email(email, exclude=account.id)
            if existing is not None:
                # the same Google account again: renew that one instead of adding a copy
                existing.creds.save(response, s['client_id'], s['client_secret'])
                existing.creds.set_identity(email, name)
                self.accounts.remove(account)
                account = existing
            else:
                account.label = name or email
                self._configure(account)
                self.accounts.save()
        elif previous and previous != email:
            account.tree.clear()
        account.expiry_notified = False
        common.log('signed in as %s' % email)
        self.wake.set()
        self.publish()
        return email

    def _tv_callback(self, query):
        """Runs on a WebDAV thread: finish the sign-in, then switch back to Kodi."""
        try:
            email = self.finish_signin('?' + query)
        except AuthError as e:
            return False, str(e)
        self.main_actions.append('Notification(GDrive Bridge,Signed in as %s,6000,%s)'
                                 % (email.replace(',', ' '), self._icon()))
        self.main_actions.append('StartAndroidActivity(org.xbmc.kodi)')
        return True, 'Signed in as %s. Returning to Kodi...' % email

    def _icon(self):
        return os.path.join(common.addon_path(), 'resources', 'icon.png')

    def _check_expiry(self):
        """Tell the user (once per account) when Google stops accepting a sign-in."""
        for account in self.accounts.all():
            creds = account.creds
            if creds.signed_in and creds.error and not account.expiry_notified:
                account.expiry_notified = True
                self.main_actions.append(
                    'Notification(GDrive Bridge,Sign-in for %s expired. Open GDrive Bridge to '
                    'sign in again.,15000,%s)' % ((creds.email or 'Google').replace(',', ' '),
                                                 self._icon()))
            elif not creds.error:
                account.expiry_notified = False

    def sign_out(self, aid=MAIN):
        """Revoke and forget an account. Extra accounts are removed altogether."""
        account = self.accounts.get(aid)
        if account is None:
            return
        account.creds.sign_out()
        account.tree.clear()
        if account.id != MAIN:
            self.accounts.remove(account)
        common.log('signed out %s' % aid)
        self.publish()

    def rename_account(self, aid, label):
        account = self.accounts.get(aid)
        if account is not None:
            account.label = label.strip()
            self.accounts.save()
            self.publish()

    def request_refresh(self):
        for account in self.accounts.all():
            account.tree.clear()
        self.wake.set()
        self.publish()

    # ---- background sync ------------------------------------------------
    def _sync_loop(self):
        while not self.stopping.is_set():
            for account in self.accounts.all():
                if not account.creds.signed_in:
                    continue
                tree = account.tree
                try:
                    count = tree.sync()
                    tree.warm()             # top folders cached before Kodi asks
                    tree.last_error = ''
                    if count:
                        common.debug('%s: applied %d Drive changes' % (account.id, count))
                except (NotReady, NotSignedIn):
                    pass
                except (DriveError, AuthError) as e:
                    tree.last_error = str(e)
                    common.log('sync failed (%s): %s' % (account.id, e))
                except Exception as e:      # never let the loop die
                    tree.last_error = 'internal error: %s' % e
                    common.log('sync crashed (%s): %r' % (account.id, e), xbmc.LOGERROR)
                tree.save()
            self._check_expiry()
            self.publish()
            self.wake.wait(max(15, self.settings['poll_interval']))
            self.wake.clear()

    # ---- status ---------------------------------------------------------
    def status(self):
        s = self.settings
        ip = self.address
        stats = self.tree.stats()
        return {
            'signed_in': self.creds.signed_in,
            'email': self.creds.email,
            'auth_error': self.creds.error or '',
            'items': stats['items'],
            'folders': stats['folders'],
            'synced': stats['synced'],
            'error': self.webdav_error or stats['error'] or '',
            'webdav': 'dav://127.0.0.1:%d/' % s['webdav_port'],
            'webdav_lan': 'dav://%s:%d/' % (ip, s['webdav_port']),
            'webdav_running': self.webdav is not None,
            'setup_running': self.setup is not None,
            'setup_url': 'http://%s:%d/' % (ip, s['setup_port']),
            'setup_error': self.setup_error,
            'pin': s['setup_pin'],
            'has_client': bool(s['client_id'] and s['client_secret']),
            'accounts': [self._account_status(a) for a in self.accounts.all()],
        }

    def _account_status(self, account):
        stats = account.tree.stats()
        folder = None
        for name, a in self.accounts.folders().items():
            if a is account:
                folder = name
        return {'id': account.id, 'email': account.creds.email, 'label': account.label,
                'folder': folder, 'signed_in': account.creds.signed_in,
                'auth_error': account.creds.error or '', 'items': stats['items'],
                'error': stats['error'] or ''}

    def publish(self):
        # A file, not a Kodi window property: Window.setProperty waits for Kodi's GUI
        # lock while holding Python's GIL. When the GUI thread is itself waiting on a
        # WebDAV request, that froze the WebDAV threads too (deadlock, 11-09).
        status = self.status()
        if status == self._published:
            return
        try:
            common.write_json(common.status_path(), dict(status, updated=time.time()))
            self._published = status
        except OSError as e:
            common.debug('publish failed: %s' % e)
