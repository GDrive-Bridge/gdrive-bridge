"""GDrive Bridge background service."""
import faulthandler
import os
import threading
import time

import xbmc

from . import common
from .bridge import Bridge


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
    if monitor.abortRequested():
        # Kodi can stop a service as it starts (an add-on update at startup): start nothing
        common.log('stop requested before start')
    else:
        bridge.start()
        common.log('started')
    while not monitor.abortRequested():
        if monitor.waitForAbort(1):
            break
        bridge.tick()
    started = time.time()
    try:
        bridge.stop()
    finally:
        stop.set()
        t.join(3)
        report_survivors(time.time() - started)


def report_survivors(took):
    """Kodi waits for every thread of this script before it can finish stopping it (and
    install an update), so name any that are still running, with their stacks."""
    me = threading.current_thread()
    left = [t for t in threading.enumerate() if t is not me]
    if not left:
        common.log('stopped (%.1f s)' % took)
        return
    common.log('stopped (%.1f s), but threads still running: %s'
               % (took, ', '.join(t.name for t in left)), xbmc.LOGWARNING)
    try:
        with open(os.path.join(common.profile_dir(), 'stall.log'), 'a') as f:
            f.write('--- threads left at stop %s: %s\n'
                    % (time.strftime('%Y-%m-%d %H:%M:%S'), ', '.join(t.name for t in left)))
            f.flush()
            faulthandler.dump_traceback(file=f, all_threads=True)
    except Exception as e:
        common.log('could not record the threads: %r' % e)
