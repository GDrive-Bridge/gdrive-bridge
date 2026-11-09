"""Build dist/service.gdrivebridge-<version>.zip for Kodi's "Install from zip file"."""
import os
import re
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ADDON = os.path.join(ROOT, 'service.gdrivebridge')

version = re.search(r'<addon [^>]*version="([^"]+)"',
                    open(os.path.join(ADDON, 'addon.xml'), encoding='utf-8').read()).group(1)
os.makedirs(os.path.join(ROOT, 'dist'), exist_ok=True)
out = os.path.join(ROOT, 'dist', 'service.gdrivebridge-%s.zip' % version)
with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as z:
    for base, dirs, files in os.walk(ADDON):
        dirs[:] = sorted(d for d in dirs if d != '__pycache__')
        for name in sorted(files):
            if name.endswith(('.pyc', '.pyo')):
                continue
            path = os.path.join(base, name)
            z.write(path, os.path.join('service.gdrivebridge', os.path.relpath(path, ADDON)))
print(out)
