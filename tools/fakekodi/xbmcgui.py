_PROPS = {}
class Window(object):
    def __init__(self, wid=None): pass
    def setProperty(self, k, v): _PROPS[k] = v
    def getProperty(self, k): return _PROPS.get(k, '')
    def clearProperty(self, k): _PROPS.pop(k, None)
class Dialog(object):
    def ok(self, *a): print('DIALOG', a)
    def notification(self, *a): print('NOTIFY', a)
