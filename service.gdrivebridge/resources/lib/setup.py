"""Setup web page: configure everything from any browser on the network.

Access needs either the one-time link in the QR code Kodi shows, or the PIN
shown in Kodi. A session cookie then keeps that browser signed in for 30 days.
"""
import hmac
import html
import http.server
import json
import os
import secrets
import threading
import time
import urllib.parse

from . import common
from .auth import AuthError

SESSION_DAYS = 30
LINK_TTL = 15 * 60
MAX_PIN_TRIES = 5
LOCKOUT = 5 * 60
COOKIE = 'gdb_session'

GUIDE = [
    ('Create a project', 'https://console.cloud.google.com/projectcreate',
     'Name it anything, for example <i>Kodi Drive</i>, then tap <b>Create</b>.'),
    ('Turn on the Drive API', 'https://console.cloud.google.com/apis/library/drive.googleapis.com',
     'Check the new project is selected at the top, then tap <b>Enable</b>.'),
    ('Set up sign-in', 'https://console.cloud.google.com/auth/overview',
     'Tap <b>Get started</b>. App name: <i>GDrive Bridge</i>; support and contact email: '
     'yours; Audience: <b>External</b>. Agree and tap <b>Create</b>. Don\'t upload a logo: '
     'it makes Google require verification.'),
    ('Allow read-only Drive access', 'https://console.cloud.google.com/auth/scopes',
     'Tap <b>Add or remove scopes</b>, search <i>drive.readonly</i>, tick '
     '<code>.../auth/drive.readonly</code>, tap <b>Update</b>, then <b>Save</b>.'),
    ('Let yourself sign in', 'https://console.cloud.google.com/auth/audience',
     'Under <b>Test users</b>, tap <b>Add users</b>, enter the Google account whose Drive you '
     'want in Kodi, and tap <b>Save</b>. <b>Don\'t skip this</b>: otherwise Google says '
     '<i>Access blocked: ... has not completed the Google verification process</i>. Google '
     'ends a test user\'s sign-in after 7 days; just sign in here again, or see the optional '
     'last step.'),
    ('Create the client', 'https://console.cloud.google.com/auth/clients',
     'Tap <b>Create client</b>, Application type <b>Desktop app</b>, name <i>Kodi</i>, '
     '<b>Create</b>. Copy the <b>Client ID</b> and <b>Client secret</b> straight away (Google '
     'shows the secret only once), or download the JSON file.'),
]

# Not needed to use the add-on; only stops the 7-day sign-in limit for test users.
OPTIONAL_STEP = (
    'Optional: publish the app', 'https://console.cloud.google.com/auth/branding',
    'Publishing stops the 7-day sign-in limit. On the <b>Branding</b> page enter Application '
    'home page <code>https://gdrive-bridge.github.io/gdrive-bridge/</code>, Application privacy policy link <code>https://gdrive-bridge.github.io/gdrive-bridge/privacy.html</code>, and '
    'add <code>gdrive-bridge.github.io</code> under <b>Authorised domains</b>; no logo. Save, '
    'then on <a href="https://console.cloud.google.com/auth/audience" target="_blank" '
    'rel="noopener">Audience</a> tap <b>Publish app</b>. Ignore <i>Your branding is not being '
    'shown</i> (the pages aren\'t on your own domain) and <i>Your app requires verification</i>: '
    'neither is needed for your own use; you\'ll see an <i>unverified app</i> warning at sign-in '
    'instead. Afterwards, sign '
    'in here once more: sign-ins made while testing still expire after 7 days.')

# Chrome warns "The information you're about to submit is not secure" for every classic
# form POST over http. The same POST sent by fetch() doesn't warn, so forms go through
# this; without JavaScript they still submit normally.
SCRIPT = """
document.addEventListener('submit', function (e) {
  var f = e.target;
  if (!window.fetch || (f.getAttribute('method') || '').toLowerCase() !== 'post') return;
  e.preventDefault();
  var b = f.querySelector('button');
  if (b) b.disabled = true;
  fetch(f.action, {method: 'POST', credentials: 'same-origin',
                   headers: {'X-GDB-Fetch': '1'}, body: new URLSearchParams(new FormData(f))})
    .then(function (r) {
      var type = r.headers.get('Content-Type') || '';
      if (type.indexOf('application/json') === 0) {
        return r.json().then(function (d) { window.location.assign(d.location); });
      }
      return r.text().then(function (t) { document.open(); document.write(t); document.close(); });
    })
    .catch(function () { if (b) b.disabled = false; f.submit(); });
});
"""

CSS = """
:root{--bg:#f4f6f8;--card:#fff;--fg:#1d2733;--muted:#5d6b7a;--line:#dde3e9;--accent:#17b2e7;
--btn:#1a73e8;--ok:#1e8e3e;--warn:#b06000;--err:#c5221f;--field:#fff}
@media (prefers-color-scheme:dark){:root{--bg:#121a22;--card:#1c2630;--fg:#e8eef3;--muted:#9fb0bf;
--line:#2c3a47;--btn:#4a90e2;--field:#151e27;--ok:#5bb974;--warn:#f0a03c;--err:#f28b82}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);
font:16px/1.5 system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:720px;margin:0 auto;padding:16px}
header{display:flex;align-items:center;gap:12px;margin:8px 0 16px}
header img{width:48px;height:48px;border-radius:10px}header h1{font-size:22px;margin:0}
header small{display:block;color:var(--muted);font-size:14px}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px;margin:0 0 16px}
h2{font-size:18px;margin:0 0 8px}p{margin:8px 0}a{color:var(--btn)}
label{display:block;margin:12px 0 4px;font-weight:600}
input[type=text],input[type=password],input[type=number],select,textarea{width:100%;padding:10px;
font:inherit;color:var(--fg);background:var(--field);border:1px solid var(--line);border-radius:8px}
textarea{min-height:90px}
.check{display:flex;gap:10px;align-items:center;margin:12px 0;font-weight:600}
.check input{width:20px;height:20px}
button,.button{display:inline-block;margin:12px 8px 0 0;padding:10px 16px;font:inherit;font-weight:600;
color:#fff;background:var(--btn);border:0;border-radius:8px;text-decoration:none;cursor:pointer}
.secondary{background:transparent;color:var(--btn);border:1px solid var(--line)}
.danger{background:var(--err)}
.pill{display:inline-block;padding:2px 10px;border-radius:999px;font-size:14px;font-weight:600;color:#fff}
.ok{background:var(--ok)}.warn{background:var(--warn)}.err{background:var(--err)}
.msg{padding:10px 12px;border-radius:8px;margin:0 0 16px;border:1px solid var(--line);background:var(--card)}
.msg.bad{border-color:var(--err)}.msg.good{border-color:var(--ok)}
ol{padding-left:22px}li{margin:6px 0}code{word-break:break-all}
dl{display:grid;grid-template-columns:max-content 1fr;gap:4px 12px;margin:8px 0}dt{color:var(--muted)}
dd{margin:0;word-break:break-all}details{margin:8px 0}summary{cursor:pointer;font-weight:600}
.hint{color:var(--muted);font-size:14px}
"""


class SetupServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, bridge):
        self.bridge = bridge
        self.lock = threading.Lock()
        self.sessions_path = os.path.join(common.profile_dir(), 'sessions.json')
        self.sessions = common.read_json(self.sessions_path, {}) or {}
        self.links = {}
        self.failures = []
        self.last_activity = time.time()
        http.server.ThreadingHTTPServer.__init__(self, address, Handler)

    def add_link(self, token):
        with self.lock:
            now = time.time()
            self.links = {k: v for k, v in self.links.items() if v > now}
            self.links[token] = now + LINK_TTL

    def use_link(self, token):
        with self.lock:
            expiry = self.links.pop(token, 0)
        return expiry > time.time()

    def new_session(self):
        sid = secrets.token_urlsafe(24)
        with self.lock:
            now = time.time()
            self.sessions = {k: v for k, v in self.sessions.items() if v['exp'] > now}
            self.sessions[sid] = {'exp': now + SESSION_DAYS * 86400,
                                  'csrf': secrets.token_urlsafe(16)}
            common.write_json(self.sessions_path, self.sessions, private=True)
        return sid

    def session(self, sid):
        with self.lock:
            s = self.sessions.get(sid)
            return s if s and s['exp'] > time.time() else None

    def end_session(self, sid):
        with self.lock:
            if self.sessions.pop(sid, None):
                common.write_json(self.sessions_path, self.sessions, private=True)

    def check_pin(self, pin):
        with self.lock:
            now = time.time()
            self.failures = [t for t in self.failures if now - t < LOCKOUT]
            if len(self.failures) >= MAX_PIN_TRIES:
                return None
            ok = hmac.compare_digest(pin.encode(), self.bridge.settings['setup_pin'].encode())
            if not ok:
                self.failures.append(now)
            return ok


class Handler(http.server.BaseHTTPRequestHandler):
    server_version = 'GDriveBridge'
    sys_version = ''
    timeout = 30

    def log_message(self, fmt, *args):
        common.debug('setup %s %s' % (self.client_address[0], fmt % args))

    # ---- plumbing -------------------------------------------------------
    def _sid(self):
        for part in (self.headers.get('Cookie') or '').split(';'):
            k, _, v = part.strip().partition('=')
            if k == COOKIE:
                return v
        return ''

    def _send(self, code, body, ctype='text/html; charset=utf-8', headers=None):
        body = body.encode('utf-8') if isinstance(body, str) else body
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Frame-Options', 'DENY')
        self.send_header('Referrer-Policy', 'no-referrer')
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _redirect(self, location, cookie=None, msg=None):
        path, _, fragment = location.partition('#')
        if msg:
            path += ('&' if '?' in path else '?') + urllib.parse.urlencode({'m': msg})
        location = path + ('#' + fragment if fragment else '')
        headers = {}
        if cookie is not None:
            headers['Set-Cookie'] = cookie
        if self.headers.get('X-GDB-Fetch'):
            # sent by the page's script: answer with the next address instead of a redirect
            self._send(200, json.dumps({'location': location}), ctype='application/json',
                       headers=headers)
            return
        headers['Location'] = location
        self._send(303, '', headers=headers)

    def _form(self):
        length = min(int(self.headers.get('Content-Length') or 0), 65536)
        raw = self.rfile.read(length).decode('utf-8', 'replace')
        return {k: v[0] for k, v in urllib.parse.parse_qs(raw, keep_blank_values=True).items()}

    def _cookie(self, sid, max_age):
        return '%s=%s; Path=/; Max-Age=%d; HttpOnly; SameSite=Strict' % (COOKIE, sid, max_age)

    # ---- routes ---------------------------------------------------------
    def do_GET(self):
        self.server.last_activity = time.time()
        url = urllib.parse.urlsplit(self.path)
        query = {k: v[0] for k, v in urllib.parse.parse_qs(url.query).items()}
        if url.path == '/icon.png':
            with open(os.path.join(common.addon_path(), 'resources', 'icon.png'), 'rb') as f:
                self._send(200, f.read(), ctype='image/png',
                           headers={'Cache-Control': 'max-age=86400'})
            return
        if url.path == '/favicon.ico':
            self._send(404, '')
            return
        if 't' in query:
            if self.server.use_link(query['t']):
                sid = self.server.new_session()
                self._redirect('/', cookie=self._cookie(sid, SESSION_DAYS * 86400))
            else:
                self._send(200, self._login_page('That link has expired or was already used. '
                                                 'Enter the PIN shown in Kodi instead.'))
            return
        if url.path == '/relay':
            self._finish_relay(url.query)
            return
        session = self.server.session(self._sid())
        if session is None:
            self._send(200, self._login_page(query.get('m')))
            return
        if url.path == '/google':
            cid = self.server.bridge.settings['client_id']
            if not cid:
                self._redirect('/', msg='Enter the client ID and secret first.')
                return
            relay = self.server.bridge.settings['relay_url'].strip()
            target = '%s:%d' % (self.server.bridge.address, self.server.server_address[1])
            self._redirect(self.server.bridge.signin.start(
                cid, relay, target, self.server.bridge.settings['webdav_port']))
            return
        if url.path == '/status.json':
            self._send(200, json.dumps(self.server.bridge.status()), ctype='application/json')
            return
        self._send(200, self._main_page(session, query.get('m'), query.get('e')))

    def do_POST(self):
        self.server.last_activity = time.time()
        path = urllib.parse.urlsplit(self.path).path
        form = self._form()
        if path == '/login':
            ok = self.server.check_pin(form.get('pin', '').strip())
            if ok:
                sid = self.server.new_session()
                self._redirect('/', cookie=self._cookie(sid, SESSION_DAYS * 86400))
            elif ok is None:
                self._send(429, self._login_page('Too many wrong PINs. Wait 5 minutes.'))
            else:
                self._send(403, self._login_page('Wrong PIN. It is shown in Kodi: GDrive Bridge '
                                                 '> Set up on phone or computer.'))
            return
        sid = self._sid()
        session = self.server.session(sid)
        if session is None:
            self._redirect('/')
            return
        if not hmac.compare_digest(form.get('csrf', ''), session['csrf']):
            self._send(403, self._login_page('That form expired. Please try again.'))
            return
        bridge = self.server.bridge
        if path == '/logout':
            self.server.end_session(sid)
            self._redirect('/', cookie=self._cookie('', 0))
        elif path == '/client':
            self._save_client(form)
        elif path == '/code':
            try:
                email = bridge.finish_signin(form.get('pasted', ''))
                self._redirect('/', msg='Signed in as %s.' % email)
            except AuthError as e:
                self._redirect('/?e=1', msg=str(e))
        elif path == '/signout':
            bridge.sign_out()
            self._redirect('/', msg='Signed out. The sign-in was also revoked with Google.')
        elif path == '/refresh':
            bridge.request_refresh()
            self._redirect('/', msg='Drive folders will be re-read now.')
        elif path == '/settings':
            self._save_settings(form)
        else:
            self._send(404, 'Not found')

    def _finish_relay(self, query):
        # Authorised by the unguessable state of a pending sign-in plus PKCE, not by
        # the session cookie (SameSite=Strict keeps it off this cross-site redirect).
        try:
            email = self.server.bridge.finish_signin('?' + query)
            body = ('<div class="msg good">Signed in as <b>%s</b>.</div><section><h2>All done'
                    '</h2><p>Kodi can now use your Drive. You can close this tab.</p>'
                    '<p><a class="button" href="/">Open the setup page</a></p></section>'
                    % html.escape(email or 'your account'))
        except AuthError as e:
            body = ('<div class="msg bad">%s</div><section><p><a class="button" href="/">Back '
                    'to the setup page</a></p></section>' % html.escape(str(e)))
        self._send(200, self._page(body))

    def _save_client(self, form):
        cid = form.get('client_id', '').strip()
        secret = form.get('client_secret', '').strip()
        blob = form.get('client_json', '').strip()
        if blob:
            try:
                data = json.loads(blob)
                data = data.get('installed') or data.get('web') or data
                cid, secret = data['client_id'], data['client_secret']
            except (ValueError, KeyError, AttributeError):
                self._redirect('/?e=1', msg='That doesn\'t look like the JSON file from Google.')
                return
        if not cid.endswith('.apps.googleusercontent.com') or not secret:
            self._redirect('/?e=1', msg='The client ID ends with .apps.googleusercontent.com, '
                                        'and the secret can\'t be empty.')
            return
        self.server.bridge.set_settings({'client_id': cid, 'client_secret': secret})
        self._redirect('/', msg='Client saved. Now sign in with Google.')

    def _save_settings(self, form):
        errors = []
        cur = self.server.bridge.settings
        values = {}
        for key in ('show_shared', 'show_drives', 'hide_dot', 'hide_archives', 'lan_share',
                    'debug'):
            values[key] = form.get(key) == 'on'
        for key, lo, hi in (('poll_interval', 15, 3600), ('webdav_port', 1024, 65535),
                            ('setup_port', 1024, 65535)):
            try:
                value = int(form.get(key, ''))
                if not lo <= value <= hi:
                    raise ValueError
                values[key] = value
            except ValueError:
                errors.append('%s must be %d-%d' % (key.replace('_', ' '), lo, hi))
        values['setup_mode'] = 1 if form.get('setup_mode') == '1' else 0
        values['lan_user'] = form.get('lan_user', '').strip() or 'kodi'
        if form.get('lan_password'):
            values['lan_password'] = form['lan_password']
        relay = form.get('relay_url', '').strip()
        if relay and not (relay.startswith('https://') and len(relay) > 12):
            errors.append('the relay address must start with https://')
        else:
            values['relay_url'] = relay
        pin = form.get('setup_pin', '').strip()
        if pin and pin != cur['setup_pin']:
            if pin.isdigit() and 4 <= len(pin) <= 8:
                values['setup_pin'] = pin
            else:
                errors.append('the PIN must be 4-8 digits')
        if errors:
            self._redirect('/?e=1#settings', msg='Not saved: ' + '; '.join(errors) + '.')
            return
        self.server.bridge.set_settings({k: v for k, v in values.items() if v != cur.get(k)})
        port = self.server.bridge.settings['setup_port']
        if port != self.server.server_address[1]:
            # the page moves to the new port in a moment; cookies aren't per port
            host = (self.headers.get('Host') or '').rsplit(':', 1)[0] or 'localhost'
            self._send(200, self._page(
                '<section><h2>Settings saved</h2><p>This page is moving to port %d.</p>'
                '<p><a class="button" href="http://%s:%d/">Open it there</a></p></section>'
                % (port, html.escape(host), port)))
            return
        self._redirect('/#settings', msg='Settings saved.')

    # ---- pages ----------------------------------------------------------
    def _page(self, body, subtitle=''):
        return ('<!doctype html><html lang="en"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                '<title>GDrive Bridge</title><link rel="icon" href="/icon.png">'
                '<style>%s</style></head><body><main><header><img src="/icon.png" alt="">'
                '<div><h1>GDrive Bridge</h1><small>%s</small></div></header>%s</main>'
                '<script>%s</script></body></html>'
                % (CSS, html.escape(subtitle or 'Google Drive for Kodi'), body, SCRIPT))

    def _login_page(self, message=None):
        msg = '<div class="msg bad">%s</div>' % html.escape(message) if message else ''
        return self._page(
            msg + '<section><h2>Enter the PIN</h2><p>In Kodi, open <b>GDrive Bridge &gt; Set up '
            'on phone or computer</b>. Scan the QR code, or enter the PIN it shows.</p>'
            '<form method="post" action="/login"><label for="pin">PIN</label>'
            '<input id="pin" name="pin" type="text" inputmode="numeric" autocomplete="off" '
            'autofocus><button>Continue</button></form></section>')

    def _main_page(self, session, message, is_error):
        b = self.server.bridge
        st = b.status()
        s = self.server.bridge.settings
        csrf = '<input type="hidden" name="csrf" value="%s">' % session['csrf']
        out = []
        if message:
            out.append('<div class="msg %s">%s</div>' % ('bad' if is_error else 'good',
                                                         html.escape(message)))
        # account
        if st['signed_in']:
            pill = ('<span class="pill err">Sign-in problem</span>' if st['auth_error']
                    else '<span class="pill ok">Signed in</span>')
        else:
            pill = '<span class="pill warn">Not signed in</span>'
        acct = ['<section><h2>Google account %s</h2>' % pill]
        has_client = bool(s['client_id'] and s['client_secret'])
        if st['signed_in']:
            acct.append('<p>Signed in as <b>%s</b>.</p>' % html.escape(st['email'] or 'unknown'))
            if st['auth_error']:
                acct.append('<p>%s</p>' % html.escape(st['auth_error']))
            acct.append('<form method="post" action="/signout">%s<button class="danger">Sign out'
                        '</button></form>' % csrf)
        if not has_client:
            acct.append(self._guide(csrf, s, open_=True))
        elif not st['signed_in'] or st['auth_error']:
            acct.append(self._signin_steps(csrf))
        else:
            acct.append('<details><summary>Sign in again (or with another account)</summary>%s</details>'
                        % self._signin_steps(csrf))
        if has_client:
            acct.append(self._guide(csrf, s, open_=False))
        acct.append('</section>')
        out.append(''.join(acct))
        # use in Kodi
        out.append(
            '<section><h2>Add it to Kodi</h2><ol>'
            '<li>In Kodi go to <b>Videos &gt; Files &gt; Add videos...</b></li>'
            '<li>Choose <b>Browse</b>, then <b>Add network location...</b> Protocol <b>WebDAV '
            'server (HTTP)</b>, server address <b>127.0.0.1</b>, port <b>%d</b>; leave the rest '
            'empty. Or type <code>%s</code> as the path.</li>'
            '<li>Pick a folder (e.g. <i>My Drive/Movies</i>), name it, then set the content type '
            '(Movies or TV shows) so Kodi scans it into the library.</li></ol>'
            '<p class="hint">Keep <b>Clean library</b> on startup off. A scan never removes '
            'items; only cleaning does.</p>%s</section>'
            % (s['webdav_port'], html.escape(st['webdav']), self._lan_note(st, s)))
        # status
        synced = (time.strftime('%d %b %H:%M:%S', time.localtime(st['synced']))
                  if st['synced'] else 'not yet')
        out.append(
            '<section><h2>Status</h2><dl><dt>Drive items cached</dt><dd>%d</dd>'
            '<dt>Folders cached</dt><dd>%d</dd><dt>Last change check</dt><dd>%s</dd>'
            '<dt>WebDAV</dt><dd>%s</dd>%s</dl><form method="post" action="/refresh">%s'
            '<button class="secondary">Refresh Drive folders</button></form></section>'
            % (st['items'], st['folders'], synced, html.escape(st['webdav']),
               '<dt>Last error</dt><dd>%s</dd>' % html.escape(st['error']) if st['error'] else '',
               csrf))
        out.append(self._settings_form(csrf, s))
        out.append('<form method="post" action="/logout">%s<button class="secondary">Log out of '
                   'this page</button></form>' % csrf)
        return self._page(''.join(out), 'Signed in as %s' % st['email'] if st['email'] else '')

    def _guide(self, csrf, s, open_):
        steps = ''.join('<li><a href="%s" target="_blank" rel="noopener">%s</a>. %s</li>'
                        % (url, title, text) for title, url, text in GUIDE)
        title, url, text = OPTIONAL_STEP
        optional = ('<p class="hint"><a href="%s" target="_blank" rel="noopener"><b>%s</b></a>. '
                    '%s</p>' % (url, title, text))
        return (
            '<details%s><summary>%s</summary>'
            '<p>GDrive Bridge signs in with your own Google Cloud client, so nothing goes through '
            'anyone else\'s server. This takes about 10 minutes, once. On a phone, turn on your '
            'browser\'s <i>Desktop site</i> option if the Cloud console is cramped.</p>'
            '<ol>%s<li>Paste them below and tap <b>Save client</b>.</li></ol>%s'
            '<form method="post" action="/client">%s'
            '<label for="cid">Client ID</label><input id="cid" name="client_id" type="text" '
            'autocomplete="off" value="%s" placeholder="....apps.googleusercontent.com">'
            '<label for="cs">Client secret</label><input id="cs" name="client_secret" '
            'type="password" autocomplete="off" placeholder="%s">'
            '<details><summary>Or paste the downloaded JSON file</summary>'
            '<textarea name="client_json" placeholder=\'{"installed":{"client_id":...}}\'>'
            '</textarea></details><button>Save client</button></form></details>'
            % (' open' if open_ else '',
               'Step 1: create your Google sign-in client' if open_ else 'Change the Google client',
               steps, optional, csrf, html.escape(s['client_id']),
               'saved (enter to replace)' if s['client_secret'] else ''))

    def _signin_steps(self, csrf):
        if self.server.bridge.settings['relay_url']:
            return (
                '<h2>Step 2: sign in</h2><ol>'
                '<li>Tap <a class="button" href="/google">Sign in with Google</a></li>'
                '<li>Choose your account. Google says it <i>hasn\'t verified this app</i>; it\'s '
                'your own, so tap <b>Advanced</b>, then <b>Go to GDrive Bridge</b>.</li>'
                '<li>Allow <b>See and download all your Google Drive files</b> and tap '
                '<b>Continue</b>. You come back here, signed in.</li></ol>'
                '<details><summary>If it stops on a page that can\'t be reached</summary>'
                '<form method="post" action="/code">%s<label for="pasted">Paste its address'
                '</label><textarea id="pasted" name="pasted" autocomplete="off"></textarea>'
                '<button>Finish sign-in</button></form></details>' % csrf)
        return (
            '<h2>Step 2: sign in</h2><ol>'
            '<li>Tap <a class="button" href="/google" target="_blank" rel="noopener">Sign in with '
            'Google</a> (it opens a new tab).</li>'
            '<li>Choose your account. Google says it <i>hasn\'t verified this app</i>; it\'s your '
            'own, so tap <b>Continue</b> (or <b>Advanced</b>, then <b>Go to GDrive Bridge</b>). '
            'If it says <i>Access blocked</i> instead, add yourself as a test user (step 1, '
            '<i>Let yourself sign in</i>).</li>'
            '<li>Allow <b>See and download all your Google Drive files</b> and tap '
            '<b>Continue</b>.</li>'
            '<li>The tab ends on a page that <b>can\'t be reached</b> (127.0.0.1). That\'s '
            'expected. Copy the whole address from its address bar.</li>'
            '<li>Come back to this tab and paste it here:</li></ol>'
            '<form method="post" action="/code">%s<label for="pasted">Address from the failed '
            'page</label><textarea id="pasted" name="pasted" autocomplete="off" '
            'placeholder="http://127.0.0.1:8765/callback?state=...&amp;code=..."></textarea>'
            '<button>Finish sign-in</button></form>' % csrf)

    @staticmethod
    def _lan_note(st, s):
        if not s['lan_share']:
            return ''
        return ('<p>Other devices on your network can use <code>%s</code> (user <b>%s</b>, '
                'the password set below). It only works while Kodi is running.</p>'
                % (html.escape(st['webdav_lan']), html.escape(s['lan_user'])))

    @staticmethod
    def _settings_form(csrf, s):
        def check(key, label, hint=''):
            return ('<label class="check"><input type="checkbox" name="%s"%s> %s</label>%s'
                    % (key, ' checked' if s[key] else '', label,
                       '<p class="hint">%s</p>' % hint if hint else ''))

        def num(key, label, hint=''):
            return ('<label for="%s">%s</label><input id="%s" name="%s" type="number" value="%d">%s'
                    % (key, label, key, key, s[key], '<p class="hint">%s</p>' % hint if hint else ''))

        return (
            '<section id="settings"><h2>Settings</h2><form method="post" action="/settings">%s'
            '%s%s%s%s%s'
            '<h2 style="margin-top:20px">Sharing</h2>%s'
            '<label for="lan_user">LAN user name</label><input id="lan_user" name="lan_user" '
            'type="text" value="%s"><label for="lan_password">LAN password</label>'
            '<input id="lan_password" name="lan_password" type="password" autocomplete="new-password" '
            'placeholder="%s">'
            '<h2 style="margin-top:20px">This setup page</h2>'
            '<label for="setup_mode">Available</label><select id="setup_mode" name="setup_mode">'
            '<option value="0"%s>Always (PIN or QR link needed)</option>'
            '<option value="1"%s>Only after opening it from Kodi (closes after 15 minutes idle)'
            '</option></select>%s'
            '<label for="setup_pin">PIN</label><input id="setup_pin" name="setup_pin" type="text" '
            'inputmode="numeric" value="%s">'
            '<h2 style="margin-top:20px">Sign-in relay (optional)</h2>'
            '<label for="relay_url">Relay address</label><input id="relay_url" name="relay_url" '
            'type="text" inputmode="url" value="%s" placeholder="https://gdrive-bridge.github.io/gdrive-bridge/callback.html">'
            '<p class="hint">A copy of GDrive Bridge\'s callback page on your own web server. '
            'With it, sign-in finishes by itself (no copy and paste). The Google client must '
            'then be a <b>Web application</b> with this exact address under Authorised redirect '
            'URIs. Leave empty for the copy-and-paste sign-in (Desktop app client).</p>'
            '<h2 style="margin-top:20px">Advanced</h2>%s%s'
            '<button>Save settings</button></form></section>'
            % (csrf,
               check('show_shared', 'Show <i>Shared with me</i>'),
               check('show_drives', 'Show <i>Shared drives</i>'),
               check('hide_dot', 'Hide files and folders starting with a dot',
                     'Hides things like <i>.git</i> or <i>.ipynb_checkpoints</i> that '
                     'aren\'t media.'),
               check('hide_archives', 'Hide archives (.zip, .rar, .7z, .apk...)',
                     'Kodi opens archives like folders, reading them over the internet, which '
                     'makes folders slow to open.'),
               num('poll_interval', 'Check Drive for changes every (seconds)'),
               check('lan_share', 'Share Drive with other devices on my network',
                     'Lets another Kodi or a WebDAV app use it, with the user name and password '
                     'below. Off: only this Kodi can use it.'),
               html.escape(s['lan_user']),
               'saved (enter to change)' if s['lan_password'] else 'required when sharing',
               '' if s['setup_mode'] else ' selected', ' selected' if s['setup_mode'] else '',
               num('setup_port', 'Setup page port'),
               html.escape(s['setup_pin']),
               html.escape(s['relay_url']),
               num('webdav_port', 'WebDAV port', 'If you change it, update the Kodi source too.'),
               check('debug', 'Detailed logging')))
