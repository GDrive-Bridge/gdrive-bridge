"""Minimal stand-in for Kodi's xbmc module, for local tests."""
import sys, time
LOGDEBUG, LOGINFO, LOGWARNING, LOGERROR = 0, 1, 2, 3
VERBOSE = True
def log(msg, level=LOGINFO):
    if VERBOSE or level >= LOGWARNING:
        sys.stderr.write('[kodi %d] %s\n' % (level, msg))
def getIPAddress():
    return '192.0.2.10'
def executeJSONRPC(s):
    return '{}'
def executebuiltin(s):
    pass
class Monitor(object):
    def abortRequested(self): return False
    def waitForAbort(self, t=0):
        time.sleep(t); return False
