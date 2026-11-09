"""Local tests for GDrive Bridge with fake Kodi modules and a fake Drive.

Run:  PYTHONPATH=tools/fakekodi python3 tools/test_local.py   (from the repo root)
"""
import http.client
import json
import os
import re
import shutil
import sys
import tempfile
import threading
import urllib.parse
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, 'service.gdrivebridge')
TMP = tempfile.mkdtemp(prefix='gdb-test-')
os.environ['FAKEKODI_ROOT'] = TMP
os.environ['ADDON_PATH'] = ADDON
sys.path.insert(0, ADDON)
sys.path.insert(0, os.path.join(ROOT, 'tools', 'fakekodi'))

import xbmc  # noqa: E402
xbmc.VERBOSE = False
import xbmcaddon  # noqa: E402
from resources.lib import auth, cache, common, webdav  # noqa: E402
from resources.lib.drive import DriveError, FOLDER_MIME, SHORTCUT_MIME  # noqa: E402

FAILS = []


def check(cond, what):
    print(('ok    ' if cond else 'FAIL  ') + what)
    if not cond:
        FAILS.append(what)


# ---------------------------------------------------------------- fake Drive
class FakeResp(object):
    def __init__(self, status, headers, body):
        self.status, self._h, self._body, self._pos = status, headers, body, 0
        self.will_close = False

    def getheader(self, k, d=None):
        return self._h.get(k, d)

    def read(self, n=-1):
        if n < 0:
            n = len(self._body)
        chunk = self._body[self._pos:self._pos + n]
        self._pos += len(chunk)
        return chunk

    def isclosed(self):
        return self._pos >= len(self._body)


class FakeDrive(object):
    def __init__(self):
        self.files = {}
        self.changes_q = []
        self.token = 1
        self.calls = []
        self.pool = type('P', (), {'clear': lambda s: None})()
        t = '2024-05-01T10:00:00.000Z'

        def add(fid, name, parents, mime='video/x-matroska', content=b'', ctime=t, **kw):
            f = dict(id=fid, name=name, parents=parents, mimeType=mime, modifiedTime=t,
                     createdTime=ctime, **kw)
            if mime != FOLDER_MIME and not mime.startswith('application/vnd.google-apps'):
                f['size'] = str(len(content))
                f['_content'] = content
            self.files[fid] = f

        add('ROOT', 'My Drive', [], FOLDER_MIME)
        add('movies', 'Movies', ['ROOT'], FOLDER_MIME)
        add('m1', 'Alien (1979).mkv', ['movies'], content=bytes(range(256)) * 40)
        add('m1srt', 'Alien (1979).srt', ['movies'], 'application/x-subrip', content=b'1\n00:00 --> 00:01\nHi\n')
        add('dup1', 'Clip.mp4', ['movies'], 'video/mp4', content=b'first', ctime='2020-01-01T00:00:00Z')
        add('dup2', 'Clip.mp4', ['movies'], 'video/mp4', content=b'second', ctime='2021-01-01T00:00:00Z')
        add('slash', 'AC/DC: Live 100% loud.mp4', ['movies'], 'video/mp4', content=b'acdc')
        add('uni', 'Amélie – 2001 日本.mkv', ['movies'], content=b'amelie')
        add('doc', 'Notes', ['movies'], 'application/vnd.google-apps.document')
        add('dotdir', '.git', ['ROOT'], FOLDER_MIME)
        add('zipf', 'Backup.ZIP', ['ROOT'], 'application/zip', content=b'PK')
        add('sc_folder', 'Shortcut to TV', ['ROOT'], SHORTCUT_MIME,
            shortcutDetails={'targetId': 'tv', 'targetMimeType': FOLDER_MIME})
        add('sc_file', 'Alien link.mkv', ['ROOT'], SHORTCUT_MIME,
            shortcutDetails={'targetId': 'm1', 'targetMimeType': 'video/x-matroska'})
        add('tv', 'TV', ['otheruser'], FOLDER_MIME, sharedWithMeTime='2024-01-01T00:00:00Z')
        add('ep1', 'Show S01E01.mkv', ['tv'], content=b'episode1')
        add('sd1', 'Family', [], FOLDER_MIME, driveId='sd1')
        add('sdf', 'Home video.mp4', ['sd1'], 'video/mp4', content=b'home', driveId='sd1')

    def _pub(self, f):
        return {k: v for k, v in f.items() if not k.startswith('_')}

    def get(self, fid):
        self.calls.append(('get', fid))
        fid = 'ROOT' if fid == 'root' else fid
        if fid not in self.files:
            raise DriveError(404, 'notFound', fid)
        return self._pub(self.files[fid])

    def children(self, fid, drive_id=None):
        self.calls.append(('children', fid, drive_id))
        return [self._pub(f) for f in self.files.values() if fid in f['parents']]

    def shared_with_me(self):
        self.calls.append(('shared',))
        return [self._pub(f) for f in self.files.values() if f.get('sharedWithMeTime')]

    def shared_drives(self):
        return [{'id': 'sd1', 'name': 'Family'}]

    def about(self):
        return {'emailAddress': 'tester@example.com', 'displayName': 'Tester'}

    def start_page_token(self):
        return str(self.token)

    def changes(self, token):
        out = {'changes': self.changes_q, 'newStartPageToken': str(int(token) + 1)}
        self.changes_q = []
        return out

    def open_media(self, fid, rng=None):
        body = self.files[fid]['_content']
        if not rng:
            return FakeResp(200, {'Content-Length': str(len(body))}, body), None
        m = re.match(r'bytes=(\d*)-(\d*)', rng)
        a, b = m.group(1), m.group(2)
        if a == '':
            start, end = len(body) - int(b), len(body) - 1
        else:
            start, end = int(a), int(b) if b else len(body) - 1
        if start >= len(body):
            return FakeResp(416, {'Content-Range': 'bytes */%d' % len(body)}, b''), None
        end = min(end, len(body) - 1)
        part = body[start:end + 1]
        return FakeResp(206, {'Content-Length': str(len(part)),
                              'Content-Range': 'bytes %d-%d/%d' % (start, end, len(body))}, part), None

    def release(self, resp, conn, complete):
        pass


class FakeCreds(object):
    signed_in = True
    email = 'tester@example.com'
    error = None


# ---------------------------------------------------------------- helpers
def request(port, method, path, headers=None, body=None):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
    c.request(method, path, body=body, headers=headers or {})
    r = c.getresponse()
    data = r.read()
    c.close()
    return r.status, dict(r.getheaders()), data


def propfind(port, path, depth='1'):
    status, h, data = request(port, 'PROPFIND', path, {'Depth': depth})
    if status != 207:
        return status, []
    ns = {'D': 'DAV:'}
    out = []
    for resp in ET.fromstring(data).findall('D:response', ns):
        href = resp.find('D:href', ns).text
        prop = resp.find('D:propstat/D:prop', ns)
        is_dir = prop.find('D:resourcetype/D:collection', ns) is not None
        size = prop.find('D:getcontentlength', ns)
        out.append((href, is_dir, int(size.text) if size is not None else None))
    return status, out


def kodi_items(base_url, request_url, entries):
    """Mimic CDAVDirectory::GetDirectory: item paths and self-entry skipping."""
    items = []
    for href, is_dir, size in entries:
        path = href.rstrip('/') if href != '/' else ''
        item_path = base_url + path.lstrip('/')
        if is_dir:
            item_path = item_path.rstrip('/') + '/'
        if item_path != request_url:
            label = urllib.parse.unquote(path.rsplit('/', 1)[-1])
            items.append((label, item_path, is_dir, size))
    return items


# ---------------------------------------------------------------- WebDAV tests
def test_webdav():
    print('\n== WebDAV ==')
    fd, creds = FakeDrive(), FakeCreds()
    tree = cache.DriveTree(fd, creds)
    srv = webdav.WebDAVServer(('127.0.0.1', 0), tree, fd)
    port = srv.server_address[1]
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = 'dav://127.0.0.1:%d/' % port

    calls = []
    orig = {n: getattr(xbmcaddon.Addon, n) for n in ('getSettingBool', 'getSettingInt',
                                                      'getSettingString', '__init__')}
    for n, f in orig.items():
        setattr(xbmcaddon.Addon, n, (lambda f, n: lambda *a, **k: (calls.append(n), f(*a, **k))[1])(f, n))
    common.set_debug(True)
    xbmc.VERBOSE = False

    st, root = propfind(port, '/')
    names = [l for l, *_ in kodi_items(base, base, root)]
    check(st == 207 and names == ['My Drive', 'Shared with me', 'Shared drives'],
          'root lists the three top folders, self entry skipped: %s' % names)

    st, md = propfind(port, '/My%20Drive/')
    items = kodi_items(base, base + 'My%20Drive/', md)
    check('.git' not in [i[0] for i in items], 'dot folders hidden by default')
    check('Backup.ZIP' not in [i[0] for i in items], 'archives hidden by default')
    tree.configure(True, True, hide_dot=True, hide_archives=False)
    st, md3 = propfind(port, '/My%20Drive/')
    check(any('Backup.ZIP' in e[0] for e in md3), 'archives shown when the setting is off')
    tree.configure(True, True, hide_dot=True, hide_archives=True)
    tree.configure(True, True, hide_dot=False)
    st, md2 = propfind(port, '/My%20Drive/')
    check(any('.git' in e[0] for e in md2), 'dot folders shown when the setting is off')
    tree.configure(True, True, hide_dot=True)
    check([i[0] for i in items] == ['Movies', 'Shortcut to TV', 'Alien link.mkv'],
          'My Drive: folders first, shortcuts resolved: %s' % [i[0] for i in items])
    link = [i for i in items if i[0] == 'Alien link.mkv'][0]
    check(link[3] == 10240, 'file shortcut shows the target size')

    st, mv = propfind(port, '/My%20Drive/Movies/')
    items = kodi_items(base, base + 'My%20Drive/Movies/', mv)
    labels = [i[0] for i in items]
    check('Notes' not in labels, 'Google Docs are hidden')
    check('Clip.mp4' in labels and 'Clip (2).mp4' in labels, 'duplicates numbered: %s' % labels)
    check('AC_DC: Live 100% loud.mp4' in labels, "'/' replaced, '%' kept intact")
    check('Amélie – 2001 日本.mkv' in labels, 'unicode name round-trips')
    # Kodi then opens items by the paths it built
    for label, path, is_dir, size in items:
        p = path[len(base) - 1:]
        s, h, _ = request(port, 'HEAD', p)
        check(s == 200 and int(h['Content-Length']) == size, 'HEAD %s -> %s' % (label, s))
    s, h, body = request(port, 'GET', '/My%20Drive/Movies/Clip%20%282%29.mp4')
    check(body == b'second', 'Clip (2) is the newer of the duplicates')

    p = '/My%20Drive/Movies/Alien%20%281979%29.mkv'
    s, h, body = request(port, 'GET', p, {'Range': 'bytes=100-199'})
    full = bytes(range(256)) * 40
    check(s == 206 and body == full[100:200] and h.get('Content-Range') == 'bytes 100-199/10240',
          'range request returns 206 with the right bytes')
    s, h, body = request(port, 'GET', p, {'Range': 'bytes=10000-'})
    check(s == 206 and body == full[10000:], 'open-ended range')
    s, h, body = request(port, 'GET', p, {'Range': 'bytes=-40'})
    check(s == 206 and body == full[-40:], 'suffix range')
    s, h, body = request(port, 'GET', p)
    check(s == 200 and body == full, 'full GET')
    s, h, body = request(port, 'GET', p, {'Range': 'bytes=99999-'})
    check(s == 416, 'range past the end gives 416')

    check(not calls, 'WebDAV requests never call into Kodi settings (deadlock guard): %s' % calls[:3])
    for n, f in orig.items():
        setattr(xbmcaddon.Addon, n, f)
    common.set_debug(False)

    st, one = propfind(port, p, depth='0')
    check(st == 207 and len(one) == 1 and one[0][2] == 10240, 'PROPFIND depth 0 on a file')
    s, _, _ = request(port, 'PROPFIND', '/My%20Drive/nope.mkv', {'Depth': '0'})
    check(s == 404, 'missing file gives 404')
    s, _, _ = request(port, 'PUT', p, body=b'x')
    check(s == 405, 'writes are refused')
    s, h, _ = request(port, 'OPTIONS', '/')
    check(s == 200 and h.get('DAV') == '1', 'OPTIONS advertises DAV 1')

    st, sh = propfind(port, '/Shared%20with%20me/')
    check([e[0] for e in sh] == ['/Shared%20with%20me/', '/Shared%20with%20me/TV/'],
          'Shared with me lists the shared folder')
    st, ep = propfind(port, '/Shared%20with%20me/TV/')
    check(any(e[0].endswith('Show%20S01E01.mkv') for e in ep), 'shared folder contents')
    st, sd = propfind(port, '/Shared%20drives/Family/')
    check(any(e[0].endswith('Home%20video.mp4') for e in sd), 'shared drive contents')
    check(('children', 'sd1', 'sd1') in fd.calls, 'shared drive listed with its driveId')
    st, via = propfind(port, '/My%20Drive/Shortcut%20to%20TV/')
    check(any(e[0].endswith('Show%20S01E01.mkv') for e in via), 'folder shortcut is browsable')

    # change feed
    tree.sync()          # first sync takes a start token and resets listings
    propfind(port, '/My%20Drive/Movies/')
    n_calls = len(fd.calls)
    fd.files['new'] = dict(id='new', name='New.mkv', parents=['movies'], mimeType='video/x-matroska',
                           size='3', modifiedTime='2024-06-01T00:00:00Z', createdTime='2024-06-01T00:00:00Z',
                           _content=b'new')
    fd.changes_q = [{'fileId': 'new', 'file': fd._pub(fd.files['new'])},
                    {'fileId': 'dup1', 'removed': True},
                    {'fileId': 'uni', 'file': dict(fd._pub(fd.files['uni']), name='Amelie.mkv')}]
    tree.sync()
    st, mv = propfind(port, '/My%20Drive/Movies/')
    labels = [urllib.parse.unquote(e[0].rsplit('/', 1)[-1]) for e in mv[1:]]
    check('New.mkv' in labels and 'Amelie.mkv' in labels and 'Clip (2).mp4' not in labels
          and 'Clip.mp4' in labels, 'changes applied without re-listing: %s' % labels)
    check(len(fd.calls) == n_calls, 'no extra Drive calls after the change feed')
    propfind(port, '/Shared%20with%20me/TV/')          # cache the destination first
    fd.files['m1srt']['parents'] = ['tv']
    fd.changes_q = [{'fileId': 'm1srt', 'file': fd._pub(fd.files['m1srt'])}]
    n_calls = len(fd.calls)
    tree.sync()
    st, ep = propfind(port, '/Shared%20with%20me/TV/')
    st, mv = propfind(port, '/My%20Drive/Movies/')
    check(not any('srt' in e[0] for e in mv) and len(fd.calls) == n_calls,
          'moved file leaves its old folder, still no extra Drive calls')
    check(any('srt' in e[0] for e in ep), 'moved file appears in its new folder')

    # persistence
    tree.save(force=True)
    tree2 = cache.DriveTree(fd, creds)
    check(tree2.load() and tree2.page_token == tree.page_token and len(tree2.nodes) == len(tree.nodes),
          'cache saved and reloaded')

    # not signed in -> 503, never an empty listing
    creds.signed_in = False
    s, h, _ = request(port, 'PROPFIND', '/My%20Drive/', {'Depth': '1'})
    check(s == 503 and h.get('Retry-After'), 'not signed in gives 503 with Retry-After')
    creds.signed_in = True
    srv.close_all()


# ---------------------------------------------------------------- setup page tests
def test_setup():
    print('\n== Setup page ==')
    from resources.lib import bridge as bridge_mod
    from resources.lib.setup import SetupServer
    a = xbmcaddon.Addon(common.ADDON_ID)
    a.setSettingInt('webdav_port', 18765)
    a.setSettingInt('setup_port', 18766)
    b = bridge_mod.Bridge()
    b.drive = FakeDrive()
    b.tree = cache.DriveTree(b.drive, b.creds)
    b.start()
    port = 18766
    pin = common.get_setting('setup_pin')
    check(pin.isdigit() and len(pin) == 6, 'PIN generated: %s' % pin)

    s, h, body = request(port, 'GET', '/')
    check(s == 200 and b'Enter the PIN' in body, 'login page without a session')
    s, h, body = request(port, 'POST', '/login', {'Content-Type': 'application/x-www-form-urlencoded'},
                         'pin=000000' if pin != '000000' else 'pin=111111')
    check(s == 403, 'wrong PIN refused')
    s, h, body = request(port, 'POST', '/login', {'Content-Type': 'application/x-www-form-urlencoded'},
                         'pin=' + pin)
    cookie = h.get('Set-Cookie', '').split(';')[0]
    check(s == 303 and cookie.startswith('gdb_session='), 'right PIN gives a session cookie')

    s, h, body = request(port, 'GET', '/', {'Cookie': cookie})
    csrf = re.search(rb'name="csrf" value="([^"]+)"', body).group(1).decode()
    check(b'Step 1' in body and b'Not signed in' in body, 'main page asks for the client first')

    def post(path, fields):
        return request(port, 'POST', path, {'Cookie': cookie,
                                             'Content-Type': 'application/x-www-form-urlencoded'},
                       urllib.parse.urlencode(dict(fields, csrf=csrf)))

    s, h, _ = post('/client', {'client_id': 'bad', 'client_secret': 'x'})
    check('e=1' in h.get('Location', ''), 'invalid client ID rejected')
    s, h, _ = post('/client', {'client_json': json.dumps({'installed': {
        'client_id': '123-abc.apps.googleusercontent.com', 'client_secret': 'GOCSPX-test'}})})
    check(common.get_setting('client_id') == '123-abc.apps.googleusercontent.com'
          and common.get_setting('client_secret') == 'GOCSPX-test', 'client saved from pasted JSON')
    check(common.get_setting('relay_url') == '', 'Desktop (installed) client turns the relay off')
    post('/client', {'client_json': json.dumps({'web': {
        'client_id': '456-web.apps.googleusercontent.com', 'client_secret': 'GOCSPX-web',
        'redirect_uris': ['https://gdrive-bridge.github.io/gdrive-bridge/callback.html']}})})
    check(common.get_setting('relay_url') == 'https://gdrive-bridge.github.io/gdrive-bridge/callback.html',
          'Web client JSON turns the relay on with its redirect URI')
    post('/client', {'client_json': json.dumps({'installed': {
        'client_id': '123-abc.apps.googleusercontent.com', 'client_secret': 'GOCSPX-test'}})})
    s, h, _ = request(port, 'POST', '/client', {'Cookie': cookie,
                                                'Content-Type': 'application/x-www-form-urlencoded'},
                      'client_id=x&csrf=wrong')
    check(s == 403, 'form without the right CSRF token refused')

    s, h, _ = request(port, 'GET', '/google', {'Cookie': cookie})
    loc = urllib.parse.urlsplit(h['Location'])
    q = dict(urllib.parse.parse_qsl(loc.query))
    check(loc.netloc == 'accounts.google.com' and q['code_challenge_method'] == 'S256'
          and q['redirect_uri'] == 'http://127.0.0.1:18765/callback'
          and q['scope'].endswith('drive.readonly') and q['access_type'] == 'offline',
          'sign-in goes to Google with PKCE, loopback redirect and read-only scope')

    sent = {}

    def fake_post(url, fields):
        sent.update(fields)
        if fields.get('grant_type') == 'authorization_code':
            if fields['code'] != 'thecode':
                return 400, {'error': 'invalid_grant'}
            return 200, {'access_token': 'at', 'refresh_token': 'rt', 'expires_in': 3599}
        return 200, {}

    auth._post = fake_post
    pasted = 'http://127.0.0.1:8765/callback?state=%s&code=thecode&scope=x' % q['state']
    s, h, _ = post('/code', {'pasted': '  ' + pasted + '\n'})
    check('Signed+in+as+tester%40example.com' in h.get('Location', ''), 'pasted address completes sign-in')
    check(sent.get('code_verifier') and sent.get('client_secret') == 'GOCSPX-test',
          'token exchange sends the PKCE verifier')
    tok = json.load(open(auth.token_path()))
    check(tok['refresh_token'] == 'rt' and tok['email'] == 'tester@example.com', 'refresh token saved')
    check(oct(os.stat(auth.token_path()).st_mode & 0o777) == '0o600', 'token file is private')
    s, h, _ = post('/code', {'pasted': pasted})
    check('e=1' in h.get('Location', ''), 'the same sign-in can\'t be replayed')

    st = common.read_json(common.status_path())
    check(st['signed_in'] and st['email'] == 'tester@example.com', 'status published for Kodi')
    src = open(os.path.join(ADDON, 'resources', 'lib', 'bridge.py')).read() + \
        ''.join(open(os.path.join(ADDON, 'resources', 'lib', f)).read()
                for f in ('webdav.py', 'cache.py', 'drive.py', 'auth.py', 'setup.py'))
    check('xbmcgui' not in src, 'service code never touches xbmcgui (GUI-lock deadlock guard)')

    # the page's script posts with X-GDB-Fetch and gets JSON instead of a 303 (no Chrome warning)
    s, h, body = request(port, 'POST', '/refresh', {'Cookie': cookie, 'X-GDB-Fetch': '1',
                         'Content-Type': 'application/x-www-form-urlencoded'},
                         urllib.parse.urlencode({'csrf': csrf}))
    check(s == 200 and json.loads(body)['location'].startswith('/?m='),
          'script-sent form gets JSON with the next address')
    s, h, body = request(port, 'POST', '/login', {'X-GDB-Fetch': '1',
                         'Content-Type': 'application/x-www-form-urlencoded'}, 'pin=' + pin)
    check(s == 200 and 'gdb_session=' in h.get('Set-Cookie', ''), 'script-sent login still sets the cookie')
    s, h, body = request(port, 'GET', '/', {'Cookie': cookie})
    check(b"addEventListener('submit'" in body, 'pages include the form script')

    s, h, _ = post('/settings', {'show_shared': 'on', 'poll_interval': '120', 'webdav_port': '18765',
                                 'setup_port': '18766', 'setup_mode': '0', 'lan_user': 'kodi',
                                 'setup_pin': pin})
    check(not common.get_setting('show_drives') and common.get_setting('poll_interval') == 120,
          'settings saved from the page')
    check(h.get('Location', '').startswith('/?m=Settings') and h['Location'].endswith('#settings'),
          'saved message goes before the #fragment: %s' % h.get('Location'))
    s, h, _ = post('/settings', {'poll_interval': '5', 'webdav_port': '18765', 'setup_port': '18766',
                                 'setup_pin': '12'})
    check('e=1' in h.get('Location', ''), 'bad values rejected')

    # relay sign-in: Google -> relay page -> /relay on this device, no cookie needed
    b.set_settings({'relay_url': 'https://relay.example/gdb/callback'})
    s, h, _ = request(port, 'GET', '/google', {'Cookie': cookie})
    q2 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(h['Location']).query))
    import base64
    tgt = q2['state'].rsplit('.', 1)[1]
    tgt = base64.urlsafe_b64decode(tgt + '=' * (-len(tgt) % 4)).decode()
    check(q2['redirect_uri'] == 'https://relay.example/gdb/callback' and tgt == '%s:%d' % (b.address, port),
          'relay sign-in: redirect to the relay, state carries this device: %s' % tgt)
    sent.clear()
    s, h, body = request(port, 'GET', '/relay?' + urllib.parse.urlencode(
        {'state': q2['state'], 'code': 'thecode', 'scope': 'x'}))
    check(s == 200 and b'Signed in as' in body and sent.get('redirect_uri') ==
          'https://relay.example/gdb/callback', 'relay callback completes sign-in without a cookie')
    s, h, body = request(port, 'GET', '/relay?' + urllib.parse.urlencode(
        {'state': q2['state'], 'code': 'thecode'}))
    check(b'expired' in body, 'relay callback can\'t be replayed')
    s, h, body = request(port, 'GET', '/relay?error=access_denied&state=x')
    check(b'cancelled' in body, 'cancelled sign-in explained')
    b.set_settings({'relay_url': ''})

    # sign-in finished by a browser on this device: Google -> 127.0.0.1:<webdav>/callback
    s, h, _ = request(port, 'GET', '/google', {'Cookie': cookie})
    q3 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(h['Location']).query))
    b.main_actions.clear()
    s, h, body = request(18765, 'GET', '/callback?' + urllib.parse.urlencode(
        {'state': q3['state'], 'code': 'thecode', 'scope': 'x'}))
    check(s == 200 and b'Signed in as tester@example.com' in body,
          'loopback /callback finishes a sign-in made on this device')
    check(any(a.startswith('StartAndroidActivity(org.xbmc.kodi') for a in b.main_actions),
          'after a TV sign-in Kodi is brought back to the front')
    # a sign-in started by the menu script (another process) is found via pending.json
    other = auth.SignIn()
    url = other.start('123-abc.apps.googleusercontent.com', loopback_port=18765)
    st4 = dict(urllib.parse.parse_qsl(urllib.parse.urlsplit(url).query))['state']
    s, h, body = request(18765, 'GET', '/callback?' + urllib.parse.urlencode(
        {'state': st4, 'code': 'thecode'}))
    check(b'Signed in as' in body, 'sign-in started in another process completes')
    # expiry notification, once
    b.main_actions.clear()
    b.creds.error = 'Google no longer accepts this sign-in.'
    b._check_expiry(); b._check_expiry()
    check(sum('Sign-in expired' in a for a in b.main_actions) == 1,
          'expired sign-in raises one Kodi notification')
    b.creds.error = None
    b._check_expiry()

    # one-time link from the Kodi QR code
    b.setup.add_link('qrtoken')
    s, h, _ = request(port, 'GET', '/?t=qrtoken')
    check(s == 303 and 'gdb_session=' in h.get('Set-Cookie', ''), 'QR link signs the browser in')
    s, h, body = request(port, 'GET', '/?t=qrtoken')
    check(b'expired' in body, 'QR link works only once')

    # PIN lockout
    for _ in range(6):
        s, _, _ = request(port, 'POST', '/login', {'Content-Type': 'application/x-www-form-urlencoded'},
                          'pin=99999999')
    check(s == 429, 'repeated wrong PINs lock the login')

    s, h, _ = post('/signout', {})
    check(not b.creds.signed_in and not os.path.exists(auth.token_path()), 'sign out removes the token')
    b.stop()


try:
    test_webdav()
    test_setup()
finally:
    shutil.rmtree(TMP, ignore_errors=True)
print('\n%d failure(s)' % len(FAILS))
sys.exit(1 if FAILS else 0)
