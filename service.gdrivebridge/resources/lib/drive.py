"""Minimal Google Drive v3 client over pooled keep-alive HTTPS connections."""
import gzip
import http.client
import json
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import common
from .auth import AuthError

HOST = 'www.googleapis.com'
BASE = '/drive/v3'
FILE_FIELDS = ('id,name,mimeType,size,modifiedTime,createdTime,parents,driveId,trashed,'
               'sharedWithMeTime,starred,'
               'shortcutDetails(targetId,targetMimeType)')
FOLDER_MIME = 'application/vnd.google-apps.folder'
SHORTCUT_MIME = 'application/vnd.google-apps.shortcut'
GOOGLE_APPS = 'application/vnd.google-apps.'
USER_AGENT = 'GDriveBridge/1.0 (gzip)'
IDLE_MAX = 50          # seconds an idle connection may be reused


class DriveError(Exception):
    def __init__(self, status, reason, message):
        Exception.__init__(self, '%s %s: %s' % (status, reason, message))
        self.status = status
        self.reason = reason


class _Pool(object):
    def __init__(self, size=6):
        self.lock = threading.Lock()
        self.idle = []
        self.size = size

    def get(self):
        now = time.time()
        with self.lock:
            while self.idle:
                conn, t = self.idle.pop()
                if now - t < IDLE_MAX:
                    return conn
                conn.close()
        return http.client.HTTPSConnection(HOST, timeout=30, context=common.ssl_context())

    def put(self, conn):
        with self.lock:
            if len(self.idle) < self.size:
                self.idle.append((conn, time.time()))
                return
        conn.close()

    def clear(self):
        with self.lock:
            for conn, _ in self.idle:
                conn.close()
            self.idle = []


def _error_reason(body):
    try:
        err = json.loads(body).get('error', {})
        errors = err.get('errors') or [{}]
        return errors[0].get('reason', ''), err.get('message', '')
    except (ValueError, AttributeError):
        return '', body[:200].decode('utf-8', 'replace') if isinstance(body, bytes) else ''


class Drive(object):
    def __init__(self, creds):
        self.creds = creds
        self.pool = _Pool()

    # ---- JSON API -------------------------------------------------------
    def call(self, path, params=None, retries=4):
        url = BASE + path + ('?' + urllib.parse.urlencode(params) if params else '')
        refreshed = force = False
        delay = 1.0
        for attempt in range(retries + 1):
            token = self.creds.access_token(force=force)
            force = False
            conn = self.pool.get()
            try:
                conn.request('GET', url, headers={
                    'Authorization': 'Bearer ' + token,
                    'Accept-Encoding': 'gzip',
                    'User-Agent': USER_AGENT,
                })
                resp = conn.getresponse()
                body = resp.read()
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                if attempt < retries:
                    time.sleep(delay)
                    delay *= 2
                    continue
                raise DriveError(0, 'network', str(e))
            if resp.will_close:
                conn.close()
            else:
                self.pool.put(conn)
            if resp.getheader('Content-Encoding') == 'gzip':
                body = gzip.decompress(body)
            if resp.status == 200:
                return json.loads(body)
            reason, message = _error_reason(body)
            if resp.status == 401 and not refreshed:
                refreshed = force = True
                continue
            if attempt < retries and (resp.status in (429, 500, 502, 503, 504) or (
                    resp.status == 403 and reason in ('rateLimitExceeded', 'userRateLimitExceeded'))):
                time.sleep(delay)
                delay *= 2
                continue
            raise DriveError(resp.status, reason, message)
        raise DriveError(0, 'retries', 'gave up')

    def _list(self, path, key, params, limit=None):
        params = dict(params)
        out = []
        while True:
            res = self.call(path, params)
            out.extend(res.get(key, []))
            token = res.get('nextPageToken')
            if not token or (limit and len(out) >= limit):
                return out[:limit] if limit else out
            params['pageToken'] = token

    def about(self):
        return self.call('/about', {'fields': 'user(displayName,emailAddress)'})['user']

    def get(self, file_id):
        return self.call('/files/' + urllib.parse.quote(file_id, safe=''),
                         {'fields': FILE_FIELDS, 'supportsAllDrives': 'true'})

    def children(self, folder_id, drive_id=None):
        params = {
            'q': "'%s' in parents and trashed = false" % folder_id.replace("'", "\\'"),
            'fields': 'nextPageToken,files(%s)' % FILE_FIELDS,
            'pageSize': 1000,
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
        }
        if drive_id:
            params.update(corpora='drive', driveId=drive_id)
        return self._list('/files', 'files', params)

    def shared_with_me(self):
        return self._list('/files', 'files', {
            'q': 'sharedWithMe = true and trashed = false',
            'fields': 'nextPageToken,files(%s)' % FILE_FIELDS,
            'pageSize': 1000,
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
        })

    def starred(self):
        return self._list('/files', 'files', {
            'q': 'starred = true and trashed = false',
            'fields': 'nextPageToken,files(%s)' % FILE_FIELDS,
            'pageSize': 1000,
            'corpora': 'allDrives',
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
        })

    def search(self, term, limit=500):
        """Files and folders whose name contains `term` (case-insensitive, Drive-wide)."""
        quoted = term.replace('\\', '\\\\').replace("'", "\\'")
        return self._list('/files', 'files', {
            'q': "name contains '%s' and trashed = false" % quoted,
            'fields': 'nextPageToken,files(%s)' % FILE_FIELDS,
            'pageSize': 200,
            'corpora': 'allDrives',
            'supportsAllDrives': 'true',
            'includeItemsFromAllDrives': 'true',
        }, limit=limit)

    def thumbnail(self, file_id, size=3840):
        """Google's JPEG rendering of an image (used for formats Kodi can't show, e.g. HEIC).

        Returns (bytes, content type). The thumbnail link is short-lived, so it is fetched
        fresh each time; the bytes are cached by the caller."""
        meta = self.call('/files/' + urllib.parse.quote(file_id, safe=''),
                         {'fields': 'thumbnailLink', 'supportsAllDrives': 'true'})
        link = meta.get('thumbnailLink')
        if not link:
            raise DriveError(404, 'noThumbnail', 'Google has no preview for this file yet')
        # '=s<N>' caps the longest side (never enlarges); '-rj' forces JPEG output
        link = re.sub(r'=s\d+[^/]*$', '', link) + '=s%d-rj' % size
        force = False
        for attempt in range(2):
            token = self.creds.access_token(force=force)
            req = urllib.request.Request(link, headers={'Authorization': 'Bearer ' + token,
                                                        'User-Agent': USER_AGENT})
            try:
                with urllib.request.urlopen(req, timeout=30,
                                            context=common.ssl_context()) as resp:
                    return resp.read(), resp.headers.get('Content-Type', 'image/jpeg')
            except urllib.error.HTTPError as e:
                if e.code in (401, 403) and attempt == 0:
                    force = True
                    continue
                raise DriveError(e.code, 'thumbnail', str(e))
            except (OSError, http.client.HTTPException) as e:
                raise DriveError(0, 'network', str(e))
        raise DriveError(0, 'retries', 'gave up')

    def shared_drives(self):
        return self._list('/drives', 'drives', {
            'fields': 'nextPageToken,drives(id,name,createdTime)', 'pageSize': 100})

    def start_page_token(self):
        return self.call('/changes/startPageToken', {'supportsAllDrives': 'true'})['startPageToken']

    def changes(self, page_token):
        return self.call('/changes', {
            'pageToken': page_token,
            'pageSize': 1000,
            'includeRemoved': 'true',
            'includeItemsFromAllDrives': 'true',
            'supportsAllDrives': 'true',
            'fields': 'nextPageToken,newStartPageToken,changes(changeType,removed,fileId,'
                      'driveId,file(%s))' % FILE_FIELDS,
        })

    # ---- media ----------------------------------------------------------
    def open_media(self, file_id, range_header=None):
        """Start a download. Returns (response, connection); call release() when done.

        Range is passed through so seeking works. 401 refreshes the token once and
        Google's "abusive file" refusal is retried with acknowledgeAbuse.
        """
        params = {'alt': 'media', 'supportsAllDrives': 'true'}
        refreshed = force = False
        for attempt in range(4):
            token = self.creds.access_token(force=force)
            force = False
            headers = {'Authorization': 'Bearer ' + token, 'User-Agent': USER_AGENT}
            if range_header:
                headers['Range'] = range_header
            url = '%s/files/%s?%s' % (BASE, urllib.parse.quote(file_id, safe=''),
                                       urllib.parse.urlencode(params))
            conn = self.pool.get()
            try:
                conn.request('GET', url, headers=headers)
                resp = conn.getresponse()
            except (OSError, http.client.HTTPException) as e:
                conn.close()
                if attempt < 3:
                    time.sleep(0.5)
                    continue
                raise DriveError(0, 'network', str(e))
            if resp.status in (200, 206, 416):
                return resp, conn
            body = resp.read()
            self.release(resp, conn, True)
            reason, message = _error_reason(body)
            if resp.status == 401 and not refreshed:
                refreshed = force = True
                continue
            if resp.status == 403 and reason == 'cannotDownloadAbusiveFile' \
                    and 'acknowledgeAbuse' not in params:
                params['acknowledgeAbuse'] = 'true'
                continue
            if resp.status in (500, 502, 503, 504) and attempt < 3:
                time.sleep(1)
                continue
            raise DriveError(resp.status, reason, message)
        raise DriveError(0, 'retries', 'gave up')

    def release(self, resp, conn, complete):
        """Return the connection to the pool only if its response was fully read."""
        if complete and not resp.will_close and resp.isclosed():
            self.pool.put(conn)
        else:
            conn.close()


__all__ = ['Drive', 'DriveError', 'AuthError', 'FOLDER_MIME', 'SHORTCUT_MIME', 'GOOGLE_APPS']
