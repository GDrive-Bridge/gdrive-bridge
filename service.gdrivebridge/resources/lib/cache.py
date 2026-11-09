"""Drive as a path tree for WebDAV, cached and kept current from Drive's change feed.

Top level:  My Drive/   Shared with me/   Shared drives/
Names are made path-safe and duplicates in one folder get " (2)", " (3)"... in
creation order, so a given path keeps pointing at the same file.
"""
import collections
import os
import threading
import time

from . import common
from .auth import AuthError, NotSignedIn
from .drive import DriveError, FOLDER_MIME, SHORTCUT_MIME, GOOGLE_APPS

TOP, SHARED, DRIVES, STARRED = '~top', '~shared', '~drives', '~starred'
SEARCH_ROOT, SEARCH = '~searchroot', '~search:'   # '~search:<term>' listings
SEARCH_TTL = 10 * 60
# Top-level names an extra account's folder can never take
RESERVED = ('My Drive', 'Shared with me', 'Shared drives', 'Starred', 'Search')
# Image formats Kodi can't decode; served as Google's JPEG rendering instead
CONVERT_EXTS = ('.heic', '.heif')
CONVERT_MIMES = ('image/heic', 'image/heif', 'image/heic-sequence', 'image/heif-sequence')
LISTING_TTL = 12 * 3600   # safety net only; the change feed keeps listings current
CACHE_VERSION = 1
# Kodi browses these as folders (zip://, rar://, apk is a zip), reading them over the
# network with many ranged requests while its GUI waits; hidden by default.
ARCHIVE_EXTS = ('.zip', '.rar', '.7z', '.tar', '.gz', '.tgz', '.bz2', '.xz', '.apk',
                '.cbz', '.cbr', '.jar', '.xpi')
PREFETCH_PER_FOLDER = 40   # subfolders read ahead after a folder is listed
MAX_DEPTH = 48            # stops runaway recursion through folder shortcut loops


class NotFound(Exception):
    pass


class NotReady(Exception):
    pass


def _node(f):
    shortcut = f.get('shortcutDetails') or {}
    size = f.get('size')
    return {
        'n': f.get('name', ''),
        'm': f.get('mimeType', ''),
        's': int(size) if size is not None else None,
        't': common.parse_rfc3339(f.get('modifiedTime')),
        'c': common.parse_rfc3339(f.get('createdTime')),
        'p': f.get('parents') or [],
        'd': f.get('driveId'),
        'w': bool(f.get('sharedWithMeTime')),
        'r': bool(f.get('starred')),
        'x': [shortcut['targetId'], shortcut.get('targetMimeType', '')]
        if shortcut.get('targetId') else None,
    }


def clean_name(name):
    name = ''.join(ch for ch in name if ch >= ' ' and ch != '\x7f')
    name = name.replace('/', '_').replace('\\', '_').strip()
    if name in ('', '.', '..'):
        name = '_' + name
    return name


def numbered(name, n, is_dir):
    if not is_dir:
        base, ext = os.path.splitext(name)
        if base and ext and len(ext) <= 8:
            return '%s (%d)%s' % (base, n, ext)
    return '%s (%d)' % (name, n)


class DriveTree(object):
    def __init__(self, drive, creds, path=None):
        self.drive = drive
        self.creds = creds
        self.lock = threading.RLock()
        self.fetch_locks = {}
        self.path = path or os.path.join(common.profile_dir(), 'cache.json')
        self.show_shared = True
        self.show_drives = True
        self.show_starred = True
        self.heic_as_jpeg = True
        self.hide_dot = True
        self.hide_archives = True
        self.last_sync = 0
        self.last_error = ''
        # Read-ahead: Kodi's GUI thread must not wait on Google. While it waits on us,
        # another add-on can block holding Python's GIL (Kodi's info calls don't release
        # it), and we can't answer until Kodi's 20 s timeout. Answers from the cache are
        # instant, so folders are fetched in the background before Kodi asks for them.
        self.prefetch_queue = collections.deque()
        self.prefetch_wake = threading.Event()
        self.prefetch_thread = None
        self._reset()

    def _reset(self):
        self.nodes = {}       # file id -> node
        self.listings = {}    # folder key -> {'t': fetched, 'ids': [child ids]}
        self.entries = {}     # folder key -> (entries, {name: entry})
        self.root_id = None
        self.page_token = None
        self.dirty = True

    def configure(self, show_shared, show_drives, hide_dot=True, hide_archives=True,
                  show_starred=True, heic_as_jpeg=True):
        with self.lock:
            self.show_shared = show_shared
            self.show_drives = show_drives
            self.show_starred = show_starred
            if (hide_dot != self.hide_dot or hide_archives != self.hide_archives
                    or heic_as_jpeg != self.heic_as_jpeg):
                self.entries.clear()
            self.hide_dot = hide_dot
            self.hide_archives = hide_archives
            self.heic_as_jpeg = heic_as_jpeg
            self.entries.pop(TOP, None)

    # ---- persistence ----------------------------------------------------
    def load(self):
        data = common.read_json(self.path)
        if not data or data.get('v') != CACHE_VERSION:
            return False
        if self.creds.email and data.get('account') and data['account'] != self.creds.email:
            return False
        with self.lock:
            self.nodes = data.get('nodes', {})
            self.listings = data.get('listings', {})
            self.entries = {}
            self.root_id = data.get('root')
            self.page_token = data.get('token')
            self.last_sync = data.get('synced', 0)
            self.dirty = False
        common.log('cache loaded: %d items, %d folders' % (len(self.nodes), len(self.listings)))
        return True

    def save(self, force=False):
        with self.lock:
            if not (self.dirty or force):
                return
            data = {
                'v': CACHE_VERSION,
                'account': self.creds.email,
                'root': self.root_id,
                'token': self.page_token,
                'synced': self.last_sync,
                'nodes': dict(self.nodes),
                'listings': {k: {'t': v['t'], 'ids': list(v['ids'])}
                             for k, v in self.listings.items()
                             if not k.startswith(SEARCH)},
            }
            self.dirty = False
        try:
            common.write_json(self.path, data)
        except OSError as e:
            common.log('cache save failed: %s' % e)

    def clear(self):
        with self.lock:
            self._reset()
        try:
            os.remove(self.path)
        except OSError:
            pass

    def stats(self):
        with self.lock:
            return {'items': len(self.nodes), 'folders': len(self.listings),
                    'synced': self.last_sync, 'error': self.last_error}

    # ---- listings -------------------------------------------------------
    def ensure_root(self):
        if self.root_id:
            return
        if not self.creds.signed_in:
            raise NotReady('Not signed in')
        f = self.drive.get('root')
        with self.lock:
            self.root_id = f['id']
            self.nodes[f['id']] = _node(f)
            self.dirty = True

    def _fetch(self, key):
        if key == SHARED:
            return self.drive.shared_with_me()
        if key == STARRED:
            return self.drive.starred()
        if key == SEARCH_ROOT:
            return []
        if key.startswith(SEARCH):
            return self.drive.search(key[len(SEARCH):])
        if key == DRIVES:
            return [{'id': d['id'], 'name': d['name'], 'mimeType': FOLDER_MIME,
                     'driveId': d['id'], 'createdTime': d.get('createdTime')}
                    for d in self.drive.shared_drives()]
        with self.lock:
            node = self.nodes.get(key)
        if node is None:
            # reached through a folder shortcut: learn which shared drive it is in
            f = self.drive.get(key)
            node = _node(f)
            with self.lock:
                self.nodes[key] = node
        return self.drive.children(key, node.get('d'))

    def _fresh(self, key):
        listing = self.listings.get(key)
        ttl = SEARCH_TTL if key.startswith(SEARCH) else LISTING_TTL
        return listing is not None and time.time() - listing['t'] < ttl

    def listing(self, key):
        """A folder's child ids. A stale copy is returned at once and refreshed in the
        background (Kodi's screen may be waiting on this answer); only a folder that has
        never been listed is fetched while the caller waits."""
        with self.lock:
            listing = self.listings.get(key)
            if listing is not None:
                if not self._fresh(key):
                    self._queue_refresh(key)
                return listing['ids']
        return self._refresh(key)

    def _queue_refresh(self, key):
        # caller holds self.lock
        if key not in self.prefetch_queue:
            self.prefetch_queue.appendleft(key)
        self._ensure_prefetcher()
        self.prefetch_wake.set()

    def _ensure_prefetcher(self):
        # caller holds self.lock
        if self.prefetch_thread is None or not self.prefetch_thread.is_alive():
            self.prefetch_thread = threading.Thread(target=self._prefetch_loop,
                                                    name='gdb-prefetch')
            self.prefetch_thread.daemon = True
            self.prefetch_thread.start()

    def _refresh(self, key):
        """Fetch a listing from Drive now (one fetch per folder at a time)."""
        with self.lock:
            flock = self.fetch_locks.setdefault(key, threading.Lock())
        with flock:
            with self.lock:
                if self._fresh(key):
                    return self.listings[key]['ids']
            try:
                files = self._fetch(key)
            except (DriveError, AuthError, NotSignedIn) as e:
                with self.lock:
                    stale = self.listings.get(key)
                    if stale is not None:
                        common.log('listing %s failed, serving cached copy: %s' % (key, e))
                        return stale['ids']
                raise
            with self.lock:
                ids = []
                for f in files:
                    self.nodes[f['id']] = _node(f)
                    ids.append(f['id'])
                self.listings[key] = {'t': time.time(), 'ids': ids}
                self.entries.pop(key, None)
                self.dirty = True
            return ids

    # ---- read-ahead -----------------------------------------------------
    def read_ahead_folder(self, key):
        """Kodi just opened folder `key`: list its subfolders in the background (one
        level only, so this never crawls the whole Drive)."""
        with self.lock:
            listing = self.listings.get(key)
            ids = list(listing['ids']) if listing else []
        if ids:
            self.read_ahead(ids)

    def read_ahead(self, ids):
        """Queue the subfolders (and folder shortcuts) among `ids` for background listing."""
        queued = 0
        with self.lock:
            for cid in ids:
                n = self.nodes.get(cid)
                if n is None or n['n'].startswith('.'):
                    continue
                if n['m'] == FOLDER_MIME:
                    key = cid
                elif n['m'] == SHORTCUT_MIME and n['x'] and n['x'][1] == FOLDER_MIME:
                    key = n['x'][0]
                else:
                    continue
                if not self._fresh(key) and key not in self.prefetch_queue:
                    self.prefetch_queue.append(key)
                    queued += 1
                    if queued >= PREFETCH_PER_FOLDER:
                        break
            if queued:
                self._ensure_prefetcher()
        if queued:
            self.prefetch_wake.set()

    def warm(self):
        """Fetch the top-level folders ahead of Kodi (after sign-in / startup)."""
        try:
            self.ensure_root()
        except (NotReady, NotSignedIn, DriveError, AuthError):
            return
        keys = [self.root_id] + [k for k, on in ((SHARED, self.show_shared),
                                                  (DRIVES, self.show_drives),
                                                  (STARRED, self.show_starred)) if on]
        with self.lock:
            for key in keys:
                if not self._fresh(key) and key not in self.prefetch_queue:
                    self.prefetch_queue.appendleft(key)
            self._ensure_prefetcher()
        self.prefetch_wake.set()

    def _prefetch_loop(self):
        idle_since = time.time()
        while True:
            with self.lock:
                key = self.prefetch_queue.popleft() if self.prefetch_queue else None
            if key is None:
                if time.time() - idle_since > 60:
                    return                  # idle a minute: the next read_ahead restarts it
                self.prefetch_wake.wait(5)
                self.prefetch_wake.clear()
                continue
            idle_since = time.time()
            if not self.creds.signed_in:
                continue
            with self.lock:
                if self._fresh(key):
                    continue
            try:
                self._refresh(key)
            except Exception as e:           # a failed read-ahead is never fatal
                common.debug('read-ahead of %s failed: %s' % (key, e))

    def _target(self, target_id):
        with self.lock:
            node = self.nodes.get(target_id)
        if node is not None:
            return node
        try:
            f = self.drive.get(target_id)
        except DriveError as e:
            common.debug('shortcut target %s unavailable: %s' % (target_id, e))
            return None
        node = _node(f)
        with self.lock:
            self.nodes[target_id] = node
            self.dirty = True
        return node

    def _top(self):
        self.ensure_root()
        items = [{'name': 'My Drive', 'id': self.root_id, 'dir': True, 'size': None,
                  'mtime': 0, 'mime': FOLDER_MIME}]
        if self.show_shared:
            items.append({'name': 'Shared with me', 'id': SHARED, 'dir': True, 'size': None,
                          'mtime': 0, 'mime': FOLDER_MIME})
        if self.show_drives:
            items.append({'name': 'Shared drives', 'id': DRIVES, 'dir': True, 'size': None,
                          'mtime': 0, 'mime': FOLDER_MIME})
        if self.show_starred:
            items.append({'name': 'Starred', 'id': STARRED, 'dir': True, 'size': None,
                          'mtime': 0, 'mime': FOLDER_MIME})
        # 'Search' is deliberately not listed: a scanned root source would otherwise pull
        # search results into the library (and a clean would later drop them).
        return items

    def children(self, key):
        """Entries of a folder as (list, {name: entry})."""
        with self.lock:
            cached = self.entries.get(key)
            if cached is not None and (key == TOP or self._fresh(key)):
                return cached
        if key == TOP:
            result = self._top()
        else:
            items = []
            for cid in list(self.listing(key)):
                with self.lock:
                    n = self.nodes.get(cid)
                if n is None:
                    continue
                entry = self._entry(cid, n)
                if entry is not None:
                    items.append(entry)
            result = self._dedupe(items)
        by_name = {e['name']: e for e in result}
        with self.lock:
            self.entries[key] = (result, by_name)
        return result, by_name

    def _entry(self, cid, n):
        if self.hide_dot and n['n'].startswith('.'):
            return None             # .git, .ipynb_checkpoints, .DS_Store...
        mime = n['m']
        if self.hide_archives and mime != FOLDER_MIME and n['n'].lower().endswith(ARCHIVE_EXTS):
            return None
        entry = {'name': clean_name(n['n']), 'id': cid, 'dir': False, 'size': n['s'],
                 'mtime': n['t'], 'ctime': n['c'], 'mime': mime}
        if mime == SHORTCUT_MIME:
            if not n['x']:
                return None
            target_id, target_mime = n['x']
            if target_mime == FOLDER_MIME:
                entry.update(id=target_id, dir=True, size=None, mime=FOLDER_MIME)
                return entry
            if target_mime.startswith(GOOGLE_APPS):
                return None
            target = self._target(target_id)
            if target is None:
                return None
            entry.update(id=target_id, size=target['s'], mtime=target['t'], mime=target['m'])
            return entry
        if mime == FOLDER_MIME:
            entry.update(dir=True, size=None)
            return entry
        if mime.startswith(GOOGLE_APPS):     # Docs, Sheets...: nothing to download
            return None
        if self.heic_as_jpeg and (mime in CONVERT_MIMES
                                  or entry['name'].lower().endswith(CONVERT_EXTS)):
            # Kodi can't decode HEIC: list it as a JPEG and serve Google's rendering
            entry.update(name=entry['name'] + '.jpg', mime='image/jpeg', convert=True)
        return entry

    @staticmethod
    def _dedupe(items):
        groups = {}
        for e in items:
            groups.setdefault(e['name'], []).append(e)
        taken = set(groups)
        result = []
        for name, group in groups.items():
            group.sort(key=lambda e: (e['ctime'], e['id']))
            result.append(group[0])
            n = 1
            for e in group[1:]:
                n += 1
                while numbered(name, n, e['dir']) in taken:
                    n += 1
                e['name'] = numbered(name, n, e['dir'])
                taken.add(e['name'])
                result.append(e)
        result.sort(key=lambda e: (not e['dir'], e['name'].lower()))
        return result

    def resolve(self, segments):
        """Path segments (decoded) -> entry. Raises NotReady / NotFound / DriveError."""
        if not self.creds.signed_in:
            raise NotReady('Not signed in')
        if len(segments) > MAX_DEPTH:
            raise NotFound()
        entry = {'name': '', 'id': TOP, 'dir': True, 'size': None, 'mtime': 0,
                 'mime': FOLDER_MIME}
        if segments and segments[0] == 'Search':
            # /Search/<term>/... : opened from the add-on's menu, never listed at the top
            term = segments[1].strip() if len(segments) > 1 else ''
            entry = {'name': 'Search', 'id': SEARCH + term if term else SEARCH_ROOT,
                     'dir': True, 'size': None, 'mtime': 0, 'mime': FOLDER_MIME}
            segments = segments[2:]
        for seg in segments:
            if not entry['dir']:
                raise NotFound()
            entry = self.children(entry['id'])[1].get(seg)
            if entry is None:
                raise NotFound()
        return entry

    # ---- change feed ----------------------------------------------------
    def sync(self):
        """Apply Drive's changes since the last sync. Returns the number applied."""
        if not self.creds.signed_in:
            return 0
        self.ensure_root()
        if not self.page_token:
            token = self.drive.start_page_token()
            with self.lock:
                # anything cached before we had a token may have missed changes
                self.listings.clear()
                self.entries.clear()
                self.page_token = token
                self.dirty = True
            self.last_sync = time.time()
            return 0
        token = self.page_token
        count = 0
        while True:
            try:
                res = self.drive.changes(token)
            except DriveError as e:
                if e.status in (400, 404):
                    common.log('change token rejected (%s); rebuilding the cache' % e)
                    with self.lock:
                        self.listings.clear()
                        self.entries.clear()
                        self.page_token = None
                        self.dirty = True
                    return self.sync()
                raise
            changes = res.get('changes', [])
            count += len(changes)
            self._apply(changes)
            if 'newStartPageToken' in res:
                token = res['newStartPageToken']
                break
            token = res['nextPageToken']
        with self.lock:
            if token != self.page_token:
                self.page_token = token
                self.dirty = True
        self.last_sync = time.time()
        return count

    def _apply(self, changes):
        if not changes:
            return
        with self.lock:
            for ch in changes:
                if ch.get('changeType') == 'drive':
                    self.listings.pop(DRIVES, None)
                    continue
                fid = ch.get('fileId')
                f = ch.get('file')
                old = self.nodes.get(fid)
                old_parents = old['p'] if old else []
                gone = ch.get('removed') or not f or f.get('trashed')
                new = None if gone else _node(f)
                new_parents = new['p'] if new else []
                for p in old_parents:
                    if p not in new_parents:
                        self._unlink(p, fid)
                for p in new_parents:
                    self._link(p, fid)
                if new is None:
                    self.nodes.pop(fid, None)
                    self.listings.pop(fid, None)
                    self._unlink(SHARED, fid)
                    self._unlink(STARRED, fid)
                else:
                    self.nodes[fid] = new
                    for flag, key in (('w', SHARED), ('r', STARRED)):
                        if new.get(flag):
                            self._link(key, fid)
                        else:
                            self._unlink(key, fid)
            # search results are a snapshot: mark them stale, so the next look serves the
            # old copy at once and refreshes it in the background
            for key, listing in self.listings.items():
                if key.startswith(SEARCH):
                    listing['t'] = 0
            self.entries.clear()
            self.dirty = True

    def _link(self, key, fid):
        listing = self.listings.get(key)
        if listing is not None and fid not in listing['ids']:
            listing['ids'].append(fid)

    def _unlink(self, key, fid):
        listing = self.listings.get(key)
        if listing is not None and fid in listing['ids']:
            listing['ids'].remove(fid)
