"""Live slideshow: a Kodi slideshow of a Drive folder that picks up new pictures.

Kodi's slideshow reads its folder once. The service watches that folder after each
change-feed sync, and when new pictures arrive it starts the slideshow again at the newest
one (Kodi can't add a PNG to a running slideshow, and appended pictures would only come up
after a full round). Every folder is listed before Kodi is asked to start, so Kodi's
screen never waits on Google (see cache.py, read-ahead).
"""
import json
import time
import urllib.parse

from .cache import MAX_DEPTH, PICTURES_ONLY, NotFound, NotReady

# What Kodi shows in a slideshow, less archives and video (a new video never restarts it)
PICTURE_EXTS = ('.jpg', '.jpeg', '.png', '.gif', '.bmp', '.webp', '.tif', '.tiff')
MAX_FOLDERS = 2000
MAX_PICTURES = 20000
START_GRACE = 30        # seconds after a (re)start before a missing slideshow ends the watch


class SlideshowError(Exception):
    pass


def encode(segments):
    """A path as Kodi spells it (the WebDAV hrefs: every segment percent-encoded)."""
    return '/'.join(urllib.parse.quote(s, safe='') for s in segments)


def walk(router, segments, recursive, stopping=None):
    """{encoded path: modified time} of the pictures in a folder (and its subfolders).
    Uses the WebDAV tree, so hidden files, archives and HEIC naming match what Kodi sees.
    Folders never listed before are fetched here, on the caller's thread."""
    try:
        top = router.resolve(segments)
    except NotFound:
        raise SlideshowError('That folder is no longer in Google Drive.')
    if not top['dir']:
        raise SlideshowError('That isn\'t a folder.')
    pictures = {}
    seen = set()
    todo = [(list(segments), top)]
    while todo:
        if stopping is not None and stopping.is_set():
            raise NotReady('stopping')
        segs, entry = todo.pop()
        key = (entry.get('acct'), entry['id'])
        if key in seen or len(segs) > MAX_DEPTH:
            continue                # a folder shortcut loop, or one folder reached twice
        seen.add(key)
        if len(seen) > MAX_FOLDERS:
            break
        for child in router.children(entry):
            path = segs + [child['name']]
            if child['dir']:
                if recursive:
                    todo.append((path, child))
            elif child['name'].lower().endswith(PICTURE_EXTS):
                pictures[encode(path)] = child.get('mtime') or 0
                if len(pictures) >= MAX_PICTURES:
                    return pictures
    return pictures


def rpc(method, params=None):
    """A JSON-RPC call into Kodi (executeJSONRPC lets other Python threads run meanwhile)."""
    import xbmc
    reply = json.loads(xbmc.executeJSONRPC(json.dumps(
        {'jsonrpc': '2.0', 'id': 1, 'method': method, 'params': params or {}})) or '{}')
    return reply.get('result')


class LiveSlideshow(object):
    """One watched slideshow. `base` is the WebDAV root as Kodi addresses it. With
    `pictures_only`, Kodi reads the folder through the hidden 'Slideshow' copy of the tree,
    which leaves out videos and music (Kodi's own setting would hide them everywhere)."""

    def __init__(self, base, segments, recursive, pictures_only=False):
        self.base = base
        self.segments = list(segments)
        self.path = ([PICTURES_ONLY] if pictures_only else []) + self.segments
        self.recursive = recursive
        self.known = {}
        self.pending = None         # newest new picture, waiting for the slideshow to resume
        self.started = 0

    @property
    def label(self):
        return '/'.join(self.segments)

    def folder_url(self):
        path = encode(self.path)
        return self.base + path + ('/' if path else '')

    def builtin(self, begin=None):
        """The SlideShow call. Encoded paths have no comma, quote or bracket to escape."""
        args = [self.folder_url()]
        if self.recursive:
            args.append('recursive')
        if begin:
            args.append('beginslide=' + self.base + begin)
        self.started = time.time()
        return 'SlideShow(%s)' % ','.join(args)

    def start(self, router, stopping=None):
        self.known = walk(router, self.path, self.recursive, stopping)
        if not self.known:
            raise SlideshowError('There are no pictures in %s.' % (
                'that folder or its subfolders' if self.recursive else 'that folder'))
        return self.builtin()

    def check(self, router, call=rpc, stopping=None):
        """After a sync. Returns ('stop', None) once the slideshow has been closed (or
        replaced by another one), ('restart', builtin) for new pictures, else (None, None)."""
        players = call('Player.GetActivePlayers') or []
        video = any(p.get('type') == 'video' for p in players)
        picture = [p for p in players if p.get('type') == 'picture']
        if not picture:
            if video or time.time() - self.started < START_GRACE:
                # a video playing from the slideshow may hide it, or Kodi is still opening it
                return None, None
            return 'stop', None
        items = (call('Playlist.GetItems', {'playlistid': picture[0]['playerid'],
                                            'properties': ['file'],
                                            'limits': {'start': 0, 'end': 1}}) or {}).get('items')
        first = (items or [{}])[0].get('file', '')
        # by folder, not by picture: Kodi also puts the folder's videos in a slideshow
        if first and not urllib.parse.unquote(first).startswith(
                urllib.parse.unquote(self.folder_url())):
            return 'stop', None     # someone started a different slideshow
        current = walk(router, self.path, self.recursive, stopping)
        new = [p for p in current if p not in self.known]
        self.known = current
        if new:
            self.pending = max(new, key=lambda p: (current[p], p))
        elif self.pending not in current:
            self.pending = None
        if not self.pending:
            return None, None
        if video:
            return None, None       # a restart would cut off the folder's video playing now
        # (not the player's speed: that is 0 whenever the current picture isn't loaded yet)
        paused = (call('XBMC.GetInfoBooleans', {'booleans': ['Slideshow.IsPaused']})
                  or {}).get('Slideshow.IsPaused')
        if paused:
            return None, None       # paused on a picture: don't pull it away
        begin, self.pending = self.pending, None
        return 'restart', self.builtin(begin)
