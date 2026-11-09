"""Google OAuth: PKCE sign-in (copy-paste of the loopback redirect), token refresh, revoke."""
import base64
import hashlib
import json
import os
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from . import common

AUTH_URL = 'https://accounts.google.com/o/oauth2/v2/auth'
TOKEN_URL = 'https://oauth2.googleapis.com/token'
REVOKE_URL = 'https://oauth2.googleapis.com/revoke'
SCOPE = 'https://www.googleapis.com/auth/drive.readonly'
# Desktop clients may only redirect to loopback. On a phone this page fails to
# load, which is expected: the user copies the address (it holds the code).
REDIRECT_URI = 'http://127.0.0.1:8765/callback'
PENDING_TTL = 30 * 60


class AuthError(Exception):
    """Sign-in or refresh failed. `revoked` means the user must sign in again."""

    def __init__(self, message, revoked=False):
        Exception.__init__(self, message)
        self.revoked = revoked


class NotSignedIn(Exception):
    pass


def token_path():
    return os.path.join(common.profile_dir(), 'token.json')


def _post(url, fields):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/x-www-form-urlencoded'})
    try:
        with urllib.request.urlopen(req, timeout=30, context=common.ssl_context()) as resp:
            body = resp.read()
            return resp.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read() or b'{}')
        except ValueError:
            return e.code, {}


def _b64url(raw):
    return base64.urlsafe_b64encode(raw).rstrip(b'=').decode()


class SignIn(object):
    """Pending PKCE sign-ins, keyed by `state`. Kept on disk so a Kodi restart
    between tapping "Sign in" and pasting the address doesn't lose them."""

    def __init__(self):
        self.lock = threading.Lock()
        self.path = os.path.join(common.profile_dir(), 'pending.json')
        now = time.time()
        self.pending = {k: v for k, v in (common.read_json(self.path, {}) or {}).items()
                        if now - v.get('t', 0) < PENDING_TTL}

    def _merge_disk(self):
        # the Kodi menu script starts TV sign-ins in its own process; pick those up
        for k, v in (common.read_json(self.path, {}) or {}).items():
            self.pending.setdefault(k, v)

    def _save(self):
        try:
            common.write_json(self.path, self.pending, private=True)
        except OSError as e:
            common.log('could not save pending sign-in: %s' % e)

    def start(self, client_id, relay_url='', return_to='', loopback_port=8765, target='main',
              choose_account=False):
        """Google sign-in URL. With a relay, Google redirects to the relay page, which
        forwards the phone to `return_to` (this device's setup page, host:port); the
        target rides in `state` after a '.' (token_urlsafe never contains one)."""
        state = secrets.token_urlsafe(16)
        if relay_url and return_to:
            state += '.' + _b64url(return_to.encode())
        redirect_uri = (relay_url if relay_url and return_to
                        else 'http://127.0.0.1:%d/callback' % loopback_port)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url(hashlib.sha256(verifier.encode()).digest())
        with self.lock:
            now = time.time()
            self._merge_disk()
            self.pending = {k: v for k, v in self.pending.items() if now - v['t'] < PENDING_TTL}
            # target: the account this sign-in is for ('main', an account id, or 'new')
            self.pending[state] = {'verifier': verifier, 't': now, 'client_id': client_id,
                                   'redirect_uri': redirect_uri, 'target': target}
            self._save()
        return AUTH_URL + '?' + urllib.parse.urlencode({
            'client_id': client_id,
            'redirect_uri': redirect_uri,
            'response_type': 'code',
            'scope': SCOPE,
            'code_challenge': challenge,
            'code_challenge_method': 'S256',
            'state': state,
            'access_type': 'offline',
            # when adding an account, make Google ask which one (the phone is usually
            # already signed in to the main account)
            'prompt': 'select_account consent' if choose_account else 'consent',
        })

    def finish(self, pasted, client_id, client_secret):
        """Exchange the pasted redirect address (or bare code).

        Returns (token response, target) where target says which account it's for."""
        code, state, error = parse_pasted(pasted)
        if error:
            if error == 'access_denied':
                raise AuthError('Sign-in was cancelled, or Drive access wasn\'t allowed. Try '
                                'again and tick the Drive permission.')
            if error == 'redirect_uri_mismatch':
                raise AuthError('Google doesn\'t know the relay address. Add it to the client\'s '
                                'Authorised redirect URIs, or clear the relay setting.')
            raise AuthError('Google returned an error: %s' % error)
        if not code:
            raise AuthError('No sign-in code found. Paste the whole address from the page that '
                            'failed to load (it starts with http://127.0.0.1:8765/callback).')
        with self.lock:
            self._merge_disk()
            if state:
                entry = self.pending.get(state)
                if entry is None:
                    raise AuthError('That sign-in has expired or came from another session. '
                                    'Tap "Sign in with Google" again.')
            else:
                # A bare code: use the most recent sign-in (PKCE still protects it).
                if not self.pending:
                    raise AuthError('Tap "Sign in with Google" first.')
                state = max(self.pending, key=lambda k: self.pending[k]['t'])
                entry = self.pending[state]
        status, body = _post(TOKEN_URL, {
            'code': code,
            'client_id': client_id,
            'client_secret': client_secret,
            'code_verifier': entry['verifier'],
            'grant_type': 'authorization_code',
            'redirect_uri': entry.get('redirect_uri', REDIRECT_URI),
        })
        if status != 200 or 'refresh_token' not in body:
            raise AuthError(_describe(body, status))
        with self.lock:
            self.pending.pop(state, None)
            self._save()
        return body, entry.get('target', 'main')


def parse_pasted(text):
    text = (text or '').strip().strip('"\'<> ')
    if not text:
        return None, None, None
    if '?' in text or 'code=' in text:
        query = text.split('?', 1)[1] if '?' in text else text
        query = query.split('#', 1)[0]
        q = urllib.parse.parse_qs(query)
        return (q.get('code', [None])[0], q.get('state', [None])[0], q.get('error', [None])[0])
    return text, None, None


def _describe(body, status):
    err = body.get('error') if isinstance(body, dict) else None
    desc = body.get('error_description') if isinstance(body, dict) else None
    if err == 'invalid_grant':
        return 'Google rejected the code (already used or expired). Sign in again.'
    if err == 'invalid_client':
        return 'Google rejected the client ID or secret. Check them and try again.'
    return 'Google sign-in failed (%s): %s' % (status, desc or err or 'no details')


class Credentials(object):
    """Holds the refresh token and hands out access tokens, refreshing them when due."""

    def __init__(self, path=None):
        self.path = path or token_path()
        self.lock = threading.Lock()
        self.token = None
        self.error = None
        self._access = None
        self._expiry = 0
        self.reload()

    def reload(self):
        with self.lock:
            self.token = common.read_json(self.path)
            if self.token and not self.token.get('refresh_token'):
                self.token = None
            self._access = None
            self._expiry = 0
            self.error = None

    @property
    def signed_in(self):
        return self.token is not None

    @property
    def email(self):
        return (self.token or {}).get('email', '')

    def save(self, token_response, client_id, client_secret, email='', name=''):
        data = {
            'refresh_token': token_response['refresh_token'],
            'client_id': client_id,
            'client_secret': client_secret,
            'email': email,
            'name': name,
            'saved': int(time.time()),
        }
        common.write_json(self.path, data, private=True)
        with self.lock:
            self.token = data
            self.error = None
            self._access = token_response.get('access_token')
            self._expiry = time.time() + int(token_response.get('expires_in', 0))

    def set_identity(self, email, name):
        with self.lock:
            if not self.token:
                return
            self.token['email'] = email
            self.token['name'] = name
            common.write_json(self.path, self.token, private=True)

    def access_token(self, force=False):
        with self.lock:
            if self.token is None:
                raise NotSignedIn()
            if not force and self._access and time.time() < self._expiry - 300:
                return self._access
            status, body = _post(TOKEN_URL, {
                'client_id': self.token['client_id'],
                'client_secret': self.token['client_secret'],
                'refresh_token': self.token['refresh_token'],
                'grant_type': 'refresh_token',
            })
            if status == 200 and body.get('access_token'):
                self._access = body['access_token']
                self._expiry = time.time() + int(body.get('expires_in', 3600))
                self.error = None
                return self._access
            revoked = body.get('error') in ('invalid_grant', 'invalid_client', 'unauthorized_client')
            self.error = ('Google no longer accepts this sign-in. Sign in again. (While your '
                          'Google app is in Testing, this happens every 7 days.)' if revoked
                          else _describe(body, status))
            raise AuthError(self.error, revoked=revoked)

    def sign_out(self):
        """Revoke with Google (best effort) and forget the token."""
        with self.lock:
            token = self.token
            self.token = None
            self._access = None
            self._expiry = 0
            self.error = None
        if token:
            try:
                _post(REVOKE_URL, {'token': token['refresh_token']})
            except Exception as e:  # offline: still forget locally
                common.log('revoke failed: %s' % e)
        try:
            os.remove(self.path)
        except OSError:
            pass
