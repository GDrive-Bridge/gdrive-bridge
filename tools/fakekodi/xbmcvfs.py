import os
ROOT = os.environ.get('FAKEKODI_ROOT', '/tmp/fakekodi')
def translatePath(p):
    return p.replace('special://profile/', ROOT + '/profile/').replace('special://xbmc/', ROOT + '/xbmc/')
