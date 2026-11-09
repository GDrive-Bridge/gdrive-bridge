"""Shared helpers: settings, paths, logging, TLS and small file utilities."""
import calendar
import json
import os
import secrets
import ssl
import time

import xbmc
import xbmcaddon
import xbmcvfs

ADDON_ID = 'service.gdrivebridge'
STATUS_FILE = 'status.json'   # written by the service, read by the menu script


def status_path():
    return os.path.join(profile_dir(), STATUS_FILE)

# setting id -> (type, default)
SETTINGS = {
    'client_id': ('str', ''),
    'client_secret': ('str', ''),
    'show_shared': ('bool', True),
    'show_drives': ('bool', True),
    'hide_dot': ('bool', True),
    'hide_archives': ('bool', True),
    'show_starred': ('bool', True),
    'heic_as_jpeg': ('bool', True),
    'poll_interval': ('int', 60),
    'webdav_port': ('int', 8765),
    'lan_share': ('bool', False),
    'lan_user': ('str', 'kodi'),
    'lan_password': ('str', ''),
    'setup_mode': ('int', 0),          # 0 = always on, 1 = only when opened from Kodi
    'setup_port': ('int', 8766),
    'setup_pin': ('str', ''),
    'lan_address': ('str', ''),
    'relay_url': ('str', ''),
    'debug': ('bool', False),
}
SETUP_ALWAYS, SETUP_ON_DEMAND = 0, 1

_ssl_ctx = None
_paths = {}
DEBUG = False   # set by the service; read without touching Kodi (see debug())


def addon():
    return xbmcaddon.Addon(ADDON_ID)


def addon_path():
    if 'addon' not in _paths:
        _paths['addon'] = xbmcvfs.translatePath(addon().getAddonInfo('path'))
    return _paths['addon']


def profile_dir():
    if 'profile' not in _paths:
        path = xbmcvfs.translatePath(addon().getAddonInfo('profile'))
        os.makedirs(path, exist_ok=True)
        _paths['profile'] = path
    return _paths['profile']


def log(msg, level=xbmc.LOGINFO):
    xbmc.log('[%s] %s' % (ADDON_ID, msg), level)


def debug(msg):
    # Must not call into Kodi's settings: the WebDAV handlers log through here, and
    # Kodi's GUI thread can be blocked waiting on that very request (deadlock until
    # Kodi's curl timeout; seen 11-09 on "Add network location").
    if DEBUG:
        log(msg, xbmc.LOGINFO)


def set_debug(value):
    global DEBUG
    DEBUG = bool(value)


def lang(string_id):
    return addon().getLocalizedString(string_id)


def get_setting(key, a=None):
    kind, default = SETTINGS[key]
    a = a or addon()
    try:
        if kind == 'bool':
            return a.getSettingBool(key)
        if kind == 'int':
            return a.getSettingInt(key)
        return a.getSettingString(key)
    except Exception:
        return default


def get_settings():
    a = addon()
    return {key: get_setting(key, a) for key in SETTINGS}


def set_setting(key, value):
    kind = SETTINGS[key][0]
    a = addon()
    if kind == 'bool':
        a.setSettingBool(key, bool(value))
    elif kind == 'int':
        a.setSettingInt(key, int(value))
    else:
        a.setSettingString(key, str(value))


def lan_address():
    """This device's home-network IPv4 address (not a VPN tunnel's).

    Kodi's getIPAddress() can return the VPN address when a VPN app is up, which
    would put an unreachable address in the QR code. Prefer a private address on
    a non-tunnel interface, then fall back to Kodi's answer.
    """
    override = get_setting('lan_address').strip()
    if override:
        return override
    candidates = []
    try:
        import subprocess
        out = subprocess.run(['ip', '-4', '-o', 'addr', 'show'], capture_output=True, text=True,
                             timeout=5).stdout
        for line in out.splitlines():
            parts = line.split()
            if len(parts) < 4 or parts[2] != 'inet':
                continue
            iface, addr = parts[1], parts[3].split('/')[0]
            if iface == 'lo' or iface.startswith(('tun', 'ppp', 'wg', 'ipsec', 'nord')):
                continue
            candidates.append(addr)
    except Exception:
        pass
    if not candidates:
        try:
            candidates = _ioctl_addresses()
        except Exception as e:
            log('address lookup failed: %r' % e)

    def private(a):
        return a.startswith(('192.168.', '10.')) or (
            a.startswith('172.') and 16 <= int(a.split('.')[1]) <= 31)

    for addr in candidates:
        if private(addr):
            return addr
    if candidates:
        return candidates[0]
    return xbmc.getIPAddress() or '127.0.0.1'


def _ioctl_addresses():
    """Interface addresses via SIOCGIFADDR, for when 'ip' is blocked (newer Android)."""
    import fcntl
    import socket
    import struct
    out = []
    names = []
    try:
        names = [n for _, n in socket.if_nameindex()]
    except (AttributeError, OSError):      # missing from Kodi's Android Python
        try:
            names = os.listdir('/sys/class/net')
        except OSError:
            pass
    names = names or ['eth0', 'wlan0', 'eth1', 'wlan1']
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        for name in names:
            if name == 'lo' or name.startswith(('tun', 'ppp', 'wg', 'ipsec', 'nord')):
                continue
            try:
                req = struct.pack('256s', name[:15].encode())
                res = fcntl.ioctl(sock.fileno(), 0x8915, req)   # SIOCGIFADDR
                out.append(socket.inet_ntoa(res[20:24]))
            except OSError:
                continue
    finally:
        sock.close()
    return out


def ensure_pin():
    pin = get_setting('setup_pin')
    if not (pin.isdigit() and 4 <= len(pin) <= 8):
        pin = '%06d' % secrets.randbelow(1000000)
        set_setting('setup_pin', pin)
    return pin


def ssl_context():
    """TLS context with a CA bundle that exists on every Kodi platform."""
    global _ssl_ctx
    if _ssl_ctx is None:
        cafile = None
        try:
            import certifi
            cafile = certifi.where()
        except ImportError:
            kodi_ca = xbmcvfs.translatePath('special://xbmc/system/certs/cacert.pem')
            if os.path.isfile(kodi_ca):
                cafile = kodi_ca
        _ssl_ctx = ssl.create_default_context(cafile=cafile)
    return _ssl_ctx


def read_json(path, default=None):
    try:
        with open(path, 'r', encoding='utf-8') as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def write_json(path, data, private=False):
    """Write atomically; private files are readable by the owner only."""
    tmp = path + '.tmp'
    flags = os.O_WRONLY | os.O_CREAT | os.O_TRUNC
    fd = os.open(tmp, flags, 0o600 if private else 0o644)
    with os.fdopen(fd, 'w', encoding='utf-8') as f:
        json.dump(data, f, separators=(',', ':'))
    os.replace(tmp, path)


def parse_rfc3339(value):
    """Drive timestamp ('2024-01-02T03:04:05.678Z') to epoch seconds."""
    if not value:
        return 0
    try:
        base = value[:19]
        t = time.strptime(base, '%Y-%m-%dT%H:%M:%S')
        return calendar.timegm(t)
    except ValueError:
        return 0


def human_size(n):
    if n is None:
        return ''
    for unit in ('B', 'KB', 'MB', 'GB', 'TB'):
        if n < 1024 or unit == 'TB':
            return ('%d %s' % (n, unit)) if unit == 'B' else ('%.1f %s' % (n, unit))
        n /= 1024.0
