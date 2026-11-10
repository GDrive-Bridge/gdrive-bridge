"""Read-only WebDAV server (class 1) that Kodi can use as a dav:// source.

Kodi needs PROPFIND (Depth 0/1), GET with Range, HEAD and OPTIONS. Writes get 405.
Until the bridge is signed in and Drive answers, requests get 503, never an empty
folder, so a library update can't mistake a not-ready server for an empty source.
"""
import base64
import collections
import email.utils
import re
import hmac
import html
import http.client
import http.server
import socket
import threading
import urllib.parse
from xml.sax.saxutils import escape

from . import common
from .auth import AuthError, NotSignedIn
from .cache import NotFound, NotReady
from .drive import DriveError

CHUNK = 512 * 1024
MAX_BODY = 1024 * 1024    # largest request body read (and ignored)
CONVERTED_CACHE = 16       # converted photos kept in memory (slideshows revisit them)


def quote(segment):
    return urllib.parse.quote(segment, safe='')


class WebDAVServer(http.server.ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, router, credentials=None, on_callback=None):
        self.on_callback = on_callback      # finishes a sign-in made in this device's browser
        self.router = router                # accounts.Accounts (or accounts.Router for one tree)
        self.credentials = credentials      # (user, password) for LAN clients, or None
        self.converted = collections.OrderedDict()   # (acct, file id) -> JPEG bytes, LRU
        self.converted_lock = threading.Lock()
        self.conns = set()
        self.conns_lock = threading.Lock()
        http.server.ThreadingHTTPServer.__init__(self, address, Handler)

    def process_request(self, request, client_address):
        with self.conns_lock:
            self.conns.add(request)
        http.server.ThreadingHTTPServer.process_request(self, request, client_address)

    def converted_bytes(self, entry):
        """Google's JPEG rendering of an image Kodi can't decode (e.g. HEIC), cached."""
        key = (entry.get('acct'), entry['id'])
        with self.converted_lock:
            if key in self.converted:
                self.converted.move_to_end(key)
                return self.converted[key]
        data, _ = self.router.drive(entry).thumbnail(entry['id'])
        with self.converted_lock:
            self.converted[key] = data
            while len(self.converted) > CONVERTED_CACHE:
                self.converted.popitem(last=False)
        return data

    def shutdown_request(self, request):
        with self.conns_lock:
            self.conns.discard(request)
        http.server.ThreadingHTTPServer.shutdown_request(self, request)

    def close_all(self):
        """Stop serving and drop open connections (they'd otherwise sit in keep-alive, and
        a handler blocked on one would keep its thread alive)."""
        self.shutdown()
        with self.conns_lock:
            conns = list(self.conns)
        for sock in conns:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
        self.server_close()


def is_loopback(addr):
    return addr.startswith('127.') or addr in ('::1', '::ffff:127.0.0.1')


class Handler(http.server.BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'
    server_version = 'GDriveBridge'
    sys_version = ''
    timeout = 120

    def log_message(self, fmt, *args):
        common.debug('webdav %s %s' % (self.client_address[0], fmt % args))

    # ---- helpers --------------------------------------------------------
    def _drain(self):
        """Read and discard a request body (Kodi's are small or empty). Returns False after
        answering one whose length is missing a real value or too large to bother with."""
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.close_connection = True        # where the body ends is unknown or too far
            self._send(400 if length < 0 else 413, 'Bad request\n')
            return False
        while length > 0:
            chunk = self.rfile.read(min(length, 65536))
            if not chunk:
                break
            length -= len(chunk)
        return True

    def _segments(self):
        path = urllib.parse.urlsplit(self.path).path
        return [urllib.parse.unquote(p) for p in path.split('/') if p]

    def _send(self, code, body=b'', ctype='text/plain; charset=utf-8', headers=None):
        if isinstance(body, str):
            body = body.encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(body)

    def _authorised(self):
        if is_loopback(self.client_address[0]):
            return True
        creds = self.server.credentials
        if not creds:
            self._send(403, 'Forbidden\n')
            return False
        header = self.headers.get('Authorization', '')
        if header.startswith('Basic '):
            try:
                user, _, password = base64.b64decode(header[6:]).decode('utf-8').partition(':')
            except (ValueError, UnicodeDecodeError):
                user = password = ''
            if hmac.compare_digest(user.encode(), creds[0].encode()) and \
                    hmac.compare_digest(password.encode(), creds[1].encode()):
                return True
        self._send(401, 'Sign-in required\n',
                   headers={'WWW-Authenticate': 'Basic realm="GDrive Bridge", charset="UTF-8"'})
        return False

    def _unavailable(self, why):
        common.debug('webdav 503: %s' % why)
        self._send(503, 'GDrive Bridge is not ready: %s\n' % why, headers={'Retry-After': '15'})

    def _lookup(self, segments):
        try:
            return self.server.router.resolve(segments)
        except NotFound:
            self._send(404, 'Not found\n')
        except (NotReady, NotSignedIn) as e:
            self._unavailable(str(e) or 'not signed in')
        except (DriveError, AuthError) as e:
            self._unavailable(str(e))
        return None

    def _children(self, entry):
        try:
            return self.server.router.children(entry)
        except (NotReady, NotSignedIn, DriveError, AuthError) as e:
            self._unavailable(str(e) or 'not signed in')
            return None

    # ---- methods --------------------------------------------------------
    def do_OPTIONS(self):
        if not self._drain():
            return
        self.send_response(200)
        self.send_header('DAV', '1')
        self.send_header('Allow', 'OPTIONS, GET, HEAD, PROPFIND')
        self.send_header('MS-Author-Via', 'DAV')
        self.send_header('Content-Length', '0')
        self.end_headers()

    def do_PROPFIND(self):
        if not self._drain() or not self._authorised():
            return
        segments = self._segments()
        entry = self._lookup(segments)
        if entry is None:
            return
        base = '/' + '/'.join(quote(s) for s in segments)
        if entry['dir'] and segments:
            base += '/'
        parts = [self._response(base, entry)]
        if entry['dir'] and self.headers.get('Depth', 'infinity') != '0':
            children = self._children(entry)
            if children is None:
                return
            self.server.router.read_ahead(entry)
            prefix = base if base.endswith('/') else base + '/'
            for child in children:
                href = prefix + quote(child['name']) + ('/' if child['dir'] else '')
                parts.append(self._response(href, child))
        body = ('<?xml version="1.0" encoding="utf-8"?>\n<D:multistatus xmlns:D="DAV:">'
                + ''.join(parts) + '</D:multistatus>\n')
        self._send(207, body, ctype='application/xml; charset=utf-8')

    @staticmethod
    def _response(href, e):
        # No displayname: Kodi URL-decodes it, which would mangle names containing '%'.
        # Without it Kodi takes the label from the (encoded) href, which is exact.
        if e['dir']:
            props = ['<D:resourcetype><D:collection/></D:resourcetype>']
        else:
            props = ['<D:resourcetype/>',
                     '<D:getcontentlength>%d</D:getcontentlength>' % (e['size'] or 0),
                     '<D:getcontenttype>%s</D:getcontenttype>' % escape(e['mime'] or
                                                                         'application/octet-stream')]
        if e.get('mtime'):
            props.append('<D:getlastmodified>%s</D:getlastmodified>'
                         % email.utils.formatdate(e['mtime'], usegmt=True))
        return ('<D:response><D:href>%s</D:href><D:propstat><D:prop>%s</D:prop>'
                '<D:status>HTTP/1.1 200 OK</D:status></D:propstat></D:response>'
                % (escape(href), ''.join(props)))

    def do_HEAD(self):
        self._get(head=True)

    def do_GET(self):
        self._get(head=False)

    def _get(self, head):
        if urllib.parse.urlsplit(self.path).path == '/callback':
            self._callback()
            return
        if not self._authorised():
            return
        segments = self._segments()
        entry = self._lookup(segments)
        if entry is None:
            return
        if entry['dir']:
            if segments and not urllib.parse.urlsplit(self.path).path.endswith('/'):
                location = '/' + '/'.join(quote(s) for s in segments) + '/'
                self._send(301, '', headers={'Location': location})
                return
            children = self._children(entry)
            if children is not None:
                self._send(200, self._index(segments, children), ctype='text/html; charset=utf-8')
            return
        last_modified = email.utils.formatdate(entry['mtime'], usegmt=True) if entry['mtime'] else None
        if entry.get('convert'):
            self._send_converted(entry, last_modified, head)
            return
        if head:
            self.send_response(200)
            self.send_header('Content-Type', entry['mime'] or 'application/octet-stream')
            self.send_header('Content-Length', str(entry['size'] or 0))
            self.send_header('Accept-Ranges', 'bytes')
            if last_modified:
                self.send_header('Last-Modified', last_modified)
            self.end_headers()
            return
        self._stream(entry, last_modified)

    def _callback(self):
        """Google's redirect after signing in with a browser on this device (loopback only)."""
        if not is_loopback(self.client_address[0]) or not self.server.on_callback:
            self._send(404, 'Not found\n')
            return
        ok, message = self.server.on_callback(urllib.parse.urlsplit(self.path).query)
        colour = '#1e8e3e' if ok else '#c5221f'
        body = ('<!doctype html><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width,initial-scale=1"><title>GDrive Bridge</title>'
                '<body style="font:28px/1.5 sans-serif;margin:8vh 8vw;background:#121a22;'
                'color:#e8eef3"><h1 style="color:%s">%s</h1><p>%s</p></body>'
                % (colour, 'Signed in' if ok else 'Sign-in failed', html.escape(message)))
        self._send(200, body, ctype='text/html; charset=utf-8')

    def _send_converted(self, entry, last_modified, head):
        try:
            data = self.server.converted_bytes(entry)
        except (AuthError, NotSignedIn) as e:
            self._unavailable(str(e) or 'not signed in')
            return
        except DriveError as e:
            common.log('preview of %s failed: %s' % (entry['name'], e))
            self._send(404 if e.status == 404 else 502, 'No preview from Drive: %s\n' % e)
            return
        status, start, end = 200, 0, len(data) - 1
        m = re.match(r'bytes=(\d*)-(\d*)$', (self.headers.get('Range') or '').strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = min(int(m.group(2)), end) if m.group(2) else end
            else:
                start = max(0, len(data) - int(m.group(2)))
            if start > end:
                self._send(416, '', headers={'Content-Range': 'bytes */%d' % len(data)})
                return
            status = 206
        self.send_response(status)
        self.send_header('Content-Type', 'image/jpeg')
        self.send_header('Content-Length', str(end - start + 1))
        self.send_header('Accept-Ranges', 'bytes')
        if status == 206:
            self.send_header('Content-Range', 'bytes %d-%d/%d' % (start, end, len(data)))
        if last_modified:
            self.send_header('Last-Modified', last_modified)
        self.end_headers()
        if not head:
            self.wfile.write(data[start:end + 1])

    def _stream(self, entry, last_modified):
        rng = self.headers.get('Range')
        if rng and (',' in rng or not rng.strip().lower().startswith('bytes=')):
            rng = None                       # multi-range: send the whole file instead
        drive = self.server.router.drive(entry)
        try:
            resp, conn = drive.open_media(entry['id'], rng)
        except (AuthError, NotSignedIn) as e:
            self._unavailable(str(e) or 'not signed in')
            return
        except DriveError as e:
            common.log('download %s failed: %s' % (entry['name'], e))
            self._send(404 if e.status == 404 else 502, 'Drive refused the download: %s\n' % e)
            return
        complete = False
        try:
            self.send_response(resp.status)
            self.send_header('Content-Type', entry['mime'] or 'application/octet-stream')
            self.send_header('Accept-Ranges', 'bytes')
            for name in ('Content-Range', 'Content-Length'):
                value = resp.getheader(name)
                if value:
                    self.send_header(name, value)
            if not resp.getheader('Content-Length'):
                self.send_header('Connection', 'close')
                self.close_connection = True
            if last_modified:
                self.send_header('Last-Modified', last_modified)
            self.end_headers()
            while True:
                buf = resp.read(CHUNK)
                if not buf:
                    break
                self.wfile.write(buf)
            complete = True
        except (OSError, http.client.HTTPException) as e:
            # Kodi closes the connection to seek; Google may drop a long-paused stream.
            common.debug('stream %s ended early: %s' % (entry['name'], e))
            self.close_connection = True
        finally:
            drive.release(resp, conn, complete)

    def _index(self, segments, children):
        title = '/' + '/'.join(segments)
        rows = []
        if segments:
            rows.append('<li><a href="../">..</a></li>')
        for c in children:
            href = quote(c['name']) + ('/' if c['dir'] else '')
            size = '' if c['dir'] else ' <small>%s</small>' % common.human_size(c['size'])
            rows.append('<li><a href="%s">%s%s</a>%s</li>' % (
                html.escape(href), html.escape(c['name']), '/' if c['dir'] else '', size))
        return ('<!doctype html><meta charset="utf-8"><meta name="viewport" '
                'content="width=device-width,initial-scale=1"><title>%s</title>'
                '<style>body{font-family:sans-serif;margin:16px}li{margin:4px 0}</style>'
                '<h3>Google Drive %s</h3><ul>%s</ul>' % (html.escape(title), html.escape(title),
                                                         ''.join(rows)))

    def _read_only(self):
        if not self._drain():
            return
        self._send(405, 'GDrive Bridge is read-only\n',
                   headers={'Allow': 'OPTIONS, GET, HEAD, PROPFIND'})

    do_PUT = do_DELETE = do_MKCOL = do_MOVE = do_COPY = do_PROPPATCH = do_POST = _read_only
    do_LOCK = do_UNLOCK = _read_only
