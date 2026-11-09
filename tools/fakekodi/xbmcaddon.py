import json, os
import xbmcvfs
_STORE = os.path.join(xbmcvfs.ROOT, 'settings.json')
DEFAULTS = {}
def _load():
    try:
        return json.load(open(_STORE))
    except Exception:
        return {}
def _save(d):
    os.makedirs(os.path.dirname(_STORE), exist_ok=True)
    json.dump(d, open(_STORE, 'w'))
LISTENERS = []
class Addon(object):
    def __init__(self, id=None):
        self.id = id
    def getAddonInfo(self, key):
        return {'profile': 'special://profile/addon_data/%s/' % self.id,
                'path': os.environ.get('ADDON_PATH', '.'), 'id': self.id}[key]
    def _get(self, key):
        d = _load()
        if key not in d:
            raise KeyError(key)
        return d[key]
    def getSettingBool(self, k): return bool(self._get(k))
    def getSettingInt(self, k): return int(self._get(k))
    def getSettingString(self, k): return str(self._get(k))
    def _set(self, k, v):
        d = _load(); d[k] = v; _save(d)
        for f in LISTENERS: f()
    def setSettingBool(self, k, v): self._set(k, bool(v))
    def setSettingInt(self, k, v): self._set(k, int(v))
    def setSettingString(self, k, v): self._set(k, str(v))
    def getLocalizedString(self, i): return str(i)
    def openSettings(self): pass
