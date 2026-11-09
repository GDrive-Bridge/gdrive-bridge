"""Exercise the real Drive client (pooling, gzip, 401 refresh, retries, ranges) against a local server.

Run:  python3 tools/test_drive.py   (from the repo root)
"""
import gzip
import http.client
import http.server
import json
import os
import sys
import tempfile
import threading

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
os.environ['FAKEKODI_ROOT'] = tempfile.mkdtemp(prefix='gdb-drive-')
sys.path.insert(0, os.path.join(ROOT, 'service.gdrivebridge'))
sys.path.insert(0, os.path.join(ROOT, 'tools', 'fakekodi'))
import xbmc  # noqa: E402
xbmc.VERBOSE = False
from resources.lib import drive as drive_mod  # noqa: E402

BODY = bytes(range(256)) * 4096          # 1 MiB
STATE = {'about_401': 1, 'busy': 1, 'conns': 0, 'auth': []}
FAILS = []


def check(cond, what):
    print(('ok    ' if cond else 'FAIL  ') + what)
    if not cond:
        FAILS.append(what)


class H(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        STATE['conns'] += 1
        http.server.BaseHTTPRequestHandler.setup(self)

    def log_message(self, *a):
        pass

    def _json(self, code, obj, gz=False):
        raw = json.dumps(obj).encode()
        if gz:
            raw = gzip.compress(raw)
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        if gz:
            self.send_header('Content-Encoding', 'gzip')
        self.send_header('Content-Length', str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def do_GET(self):
        STATE['auth'].append(self.headers.get('Authorization'))
        if self.path.startswith('/drive/v3/about'):
            if STATE['about_401']:
                STATE['about_401'] -= 1
                return self._json(401, {'error': {'errors': [{'reason': 'authError'}], 'message': 'x'}})
            gz = 'gzip' in (self.headers.get('Accept-Encoding') or '')
            return self._json(200, {'user': {'emailAddress': 'a@b.c', 'displayName': 'A'}}, gz=gz)
        if self.path.startswith('/drive/v3/files?'):
            if STATE['busy']:
                STATE['busy'] -= 1
                return self._json(503, {'error': {'errors': [{'reason': 'backendError'}], 'message': 'busy'}})
            page = 'pageToken=p2' in self.path
            return self._json(200, {'files': [{'id': 'b' if page else 'a'}],
                                    **({} if page else {'nextPageToken': 'p2'})})
        if '/drive/v3/files/abuse?' in self.path and 'acknowledgeAbuse' not in self.path:
            return self._json(403, {'error': {'errors': [{'reason': 'cannotDownloadAbusiveFile'}],
                                              'message': 'flagged'}})
        if 'alt=media' in self.path:
            rng = self.headers.get('Range')
            if rng:
                a, b = rng[6:].split('-')
                a, b = int(a), (int(b) if b else len(BODY) - 1)
                part = BODY[a:b + 1]
                self.send_response(206)
                self.send_header('Content-Range', 'bytes %d-%d/%d' % (a, b, len(BODY)))
            else:
                part = BODY
                self.send_response(200)
            self.send_header('Content-Length', str(len(part)))
            self.end_headers()
            self.wfile.write(part)
            return
        self._json(404, {'error': {'errors': [{'reason': 'notFound'}], 'message': 'nf'}})


class FakeCreds(object):
    def __init__(self):
        self.n = 0
        self.forced = 0

    def access_token(self, force=False):
        if force or self.n == 0:
            self.n += 1
            self.forced += int(force)
        return 'tok%d' % self.n


srv = http.server.ThreadingHTTPServer(('127.0.0.1', 0), H)
threading.Thread(target=srv.serve_forever, daemon=True).start()
port = srv.server_address[1]
drive_mod.http.client.HTTPSConnection = \
    lambda host, timeout=None, context=None: http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
drive_mod.time.sleep = lambda s: None

creds = FakeCreds()
d = drive_mod.Drive(creds)

user = d.about()
check(user['emailAddress'] == 'a@b.c', 'gzip JSON decoded')
check(creds.forced == 1 and STATE['auth'][-1] == 'Bearer tok2', '401 forces exactly one token refresh')

files = d.children('folder')
check([f['id'] for f in files] == ['a', 'b'], 'retried a 503, then followed nextPageToken')
check(creds.forced == 1, 'retries after a 503 do not force more refreshes')

before = STATE['conns']
for _ in range(5):
    d.about()
check(STATE['conns'] == before, 'JSON calls reuse one keep-alive connection')

resp, conn = d.open_media('f', 'bytes=1000-1999')
data = resp.read()
d.release(resp, conn, True)
check(resp.status == 206 and data == BODY[1000:2000], 'range download')
before = STATE['conns']
resp, conn = d.open_media('f', 'bytes=0-99')
resp.read()
d.release(resp, conn, True)
check(STATE['conns'] == before, 'fully read download returns its connection to the pool')

resp, conn = d.open_media('f')
resp.read(4096)                   # client went away mid-stream
d.release(resp, conn, False)
before = STATE['conns']
resp, conn = d.open_media('f', 'bytes=0-9')
check(resp.read() == BODY[:10], 'download after an abandoned stream works')
d.release(resp, conn, True)
check(STATE['conns'] == before + 1, 'abandoned stream\'s connection was closed, not reused')

resp, conn = d.open_media('abuse', 'bytes=0-9')
check(resp.status == 206, 'flagged file retried with acknowledgeAbuse')
resp.read()
d.release(resp, conn, True)

try:
    d.get('missing')
    check(False, '404 raises DriveError')
except drive_mod.DriveError as e:
    check(e.status == 404 and e.reason == 'notFound', '404 raises DriveError(notFound)')

srv.shutdown()
print('\n%d failure(s)' % len(FAILS))
sys.exit(1 if FAILS else 0)
