"""Several Google accounts, each with its own sign-in, Drive client and cache.

The main account keeps the original files (token.json, cache.json) and its folders at
the top of the WebDAV tree, so sources added before accounts existed keep working.
Each extra account appears as one more top-level folder, named after the account.
"""
import os
import secrets
import threading

from . import common
from .auth import Credentials, NotSignedIn
from .cache import (DriveTree, NotFound, NotReady, PICTURES_ONLY, RESERVED, TOP, VIDEO_EXTS,
                    clean_name)
from .drive import Drive, FOLDER_MIME

MAIN = 'main'
ROOT = '~root'


def _tag(entry, account):
    tagged = dict(entry)
    tagged['acct'] = account.id
    return tagged


def _split_pictures(segments):
    """('Slideshow/...' path?, the path without it)"""
    if segments and segments[0] == PICTURES_ONLY:
        return True, segments[1:]
    return False, segments


def _slideshow_entry(e):
    """An entry seen through 'Slideshow/'. JPEG photos are served at TV size (webdav.py): a
    camera original is 4-6 MB, slow to download and to decode on a TV box, and the slideshow
    can't show the extra detail."""
    return dict(e, pics=True, resize=not e['dir'] and not e.get('convert')
                and e['mime'] == 'image/jpeg')


def _pictures_only(entries):
    """Folders and everything but video and music, for a slideshow that shows only pictures."""
    return [_slideshow_entry(e) for e in entries
            if e['dir'] or not (e['mime'].startswith(('video/', 'audio/'))
                                or e['name'].lower().endswith(VIDEO_EXTS))]


class Account(object):
    def __init__(self, aid, label='', drive_factory=Drive):
        self.id = aid
        self.label = label
        profile = common.profile_dir()
        self.token_path = os.path.join(profile, 'token.json' if aid == MAIN
                                       else 'token-%s.json' % aid)
        self.cache_path = os.path.join(profile, 'cache.json' if aid == MAIN
                                       else 'cache-%s.json' % aid)
        self.creds = Credentials(self.token_path)
        self.drive = drive_factory(self.creds)
        self.tree = DriveTree(self.drive, self.creds, self.cache_path)
        self.expiry_notified = False

    def describe(self):
        return self.label or self.creds.email or 'Google account'


class Router(object):
    """One account's tree seen as the whole WebDAV root (also used by the tests)."""

    def __init__(self, tree, drive):
        self.tree = tree
        self._drive = drive

    def resolve(self, segments):
        pics, segments = _split_pictures(segments)
        entry = self.tree.resolve(segments)
        return _slideshow_entry(entry) if pics else entry

    def children(self, entry):
        items = self.tree.children(entry['id'])[0]
        return _pictures_only(items) if entry.get('pics') else items

    def drive(self, entry):
        return self._drive

    def read_ahead(self, entry):
        self.tree.read_ahead_folder(entry['id'])


class Accounts(object):
    """All accounts, plus the WebDAV routing between them (same API as Router)."""

    def __init__(self, drive_factory=Drive):
        self.drive_factory = drive_factory
        self.lock = threading.RLock()
        self.path = os.path.join(common.profile_dir(), 'accounts.json')
        data = common.read_json(self.path, {}) or {}
        self.items = [Account(MAIN, data.get('main_label', ''), drive_factory)]
        for a in data.get('extra', []):
            if a.get('id') and a['id'] != MAIN:
                self.items.append(Account(a['id'], a.get('label', ''), drive_factory))

    # ---- bookkeeping ----------------------------------------------------
    @property
    def main(self):
        return self.items[0]

    def all(self):
        with self.lock:
            return list(self.items)

    def extras(self):
        with self.lock:
            return self.items[1:]

    def get(self, aid):
        with self.lock:
            for a in self.items:
                if a.id == aid:
                    return a
        return None

    def find_email(self, email, exclude=None):
        if not email:
            return None
        with self.lock:
            for a in self.items:
                if a.id != exclude and a.creds.email.lower() == email.lower():
                    return a
        return None

    def save(self):
        with self.lock:
            data = {'main_label': self.main.label,
                    'extra': [{'id': a.id, 'label': a.label} for a in self.items[1:]]}
        common.write_json(self.path, data)

    def create(self):
        with self.lock:
            account = Account(secrets.token_hex(3), '', self.drive_factory)
            self.items.append(account)
        return account

    def remove(self, account):
        """Forget an extra account and delete its files (the main account stays)."""
        if account.id == MAIN:
            return
        with self.lock:
            self.items = [a for a in self.items if a.id != account.id]
        for path in (account.token_path, account.cache_path):
            try:
                os.remove(path)
            except OSError:
                pass
        self.save()

    def folders(self):
        """Top-level folder name -> extra account. Never a reserved name, never twice."""
        names = {}
        for a in self.extras():
            if not a.creds.signed_in:
                continue
            base = clean_name(a.describe())
            name, n = base, 1
            while name in RESERVED or name in names:
                n += 1
                name = '%s (%d)' % (base, n)
            names[name] = a
        return names

    # ---- WebDAV routing ---------------------------------------------------
    def resolve(self, segments):
        pics, segments = _split_pictures(segments)
        entry = self._resolve(segments)
        return _slideshow_entry(entry) if pics else entry

    def _resolve(self, segments):
        if not segments:
            if not any(a.creds.signed_in for a in self.all()):
                raise NotReady('Not signed in')
            return {'name': '', 'id': ROOT, 'dir': True, 'size': None, 'mtime': 0,
                    'mime': FOLDER_MIME, 'acct': None}
        folders = self.folders()
        if segments[0] in folders:
            account = folders[segments[0]]
            entry = account.tree.resolve(segments[1:])
        else:
            account = self.main
            if not account.creds.signed_in:
                raise NotFound() if folders else NotReady('Not signed in')
            entry = account.tree.resolve(segments)
        return _tag(entry, account)

    def children(self, entry):
        items = self._children(entry)
        return _pictures_only(items) if entry.get('pics') else items

    def _children(self, entry):
        if entry['id'] == ROOT:
            items = []
            if self.main.creds.signed_in:
                items = [_tag(e, self.main) for e in self.main.tree.children(TOP)[0]]
            for name, account in sorted(self.folders().items(), key=lambda kv: kv[0].lower()):
                items.append({'name': name, 'id': TOP, 'dir': True, 'size': None, 'mtime': 0,
                              'mime': FOLDER_MIME, 'acct': account.id})
            return items
        account = self.get(entry.get('acct')) or self.main
        if not account.creds.signed_in:
            raise NotSignedIn()
        return [_tag(e, account) for e in account.tree.children(entry['id'])[0]]

    def drive(self, entry):
        account = self.get(entry.get('acct')) or self.main
        return account.drive

    def read_ahead(self, entry):
        account = self.get(entry.get('acct'))
        if account is not None and entry['id'] != ROOT:
            account.tree.read_ahead_folder(entry['id'])
