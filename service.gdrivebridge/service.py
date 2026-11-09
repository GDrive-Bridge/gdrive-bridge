"""GDrive Bridge background service."""
import faulthandler
import os
import threading
import time

import xbmc

from resources.lib import common
from resources.lib.bridge import Bridge


class Monitor(xbmc.Monitor):
    def __init__(self, bridge):
        xbmc.Monitor.__init__(self)
        self.bridge = bridge

    def onSettingsChanged(self):
        self.bridge.on_settings_changed()

    def onNotification(self, sender, method, data):
        self.bridge.on_notification(sender, method, data)


def stall_recorder(stop):
    """Diagnostics: if this add-on's Python can't run for 10 s, faulthandler's C-level
    timer writes every thread's stack to stall.log (works even when the GIL is stuck)."""
    path = os.path.join(common.profile_dir(), 'stall.log')
    try:
        f = open(path, 'a')
        f.write('--- service start %s\n' % time.strftime('%Y-%m-%d %H:%M:%S'))
        f.flush()
        faulthandler.dump_traceback_later(10, repeat=True, file=f)
    except Exception as e:
        common.log('stall recorder unavailable: %r' % e)
        return
    common.log('stall recorder armed')
    while not stop.wait(2):
        faulthandler.cancel_dump_traceback_later()
        faulthandler.dump_traceback_later(10, repeat=True, file=f)
    faulthandler.cancel_dump_traceback_later()
    f.close()


def run():
    stop = threading.Event()
    t = threading.Thread(target=stall_recorder, args=(stop,), name='gdb-stall')
    t.daemon = True
    t.start()
    bridge = Bridge()
    monitor = Monitor(bridge)
    bridge.start()
    common.log('started')
    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break
        bridge.tick()
    bridge.stop()
    stop.set()
    common.log('stopped')


run()
