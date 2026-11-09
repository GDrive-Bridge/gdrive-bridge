"""Build the Kodi repository in docs/repo/ (served by GitHub Pages).

Kodi reads addons.xml (+ addons.xml.sha256) and fetches
<id>/<id>-<version>.zip (+ .sha256) and each add-on's icon/fanart from there.
Only the latest version of each add-on is kept. Zips are reproducible, so
rebuilding an unchanged add-on leaves git clean.
"""
import hashlib
import os
import re
import shutil
import zipfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPO = os.path.join(ROOT, 'docs', 'repo')
ADDONS = ['repository.gdrivebridge', 'service.gdrivebridge']
STAMP = (2026, 1, 1, 0, 0, 0)


def sha256(path):
    return hashlib.sha256(open(path, 'rb').read()).hexdigest()


def build(addon_id):
    src = os.path.join(ROOT, addon_id)
    xml = open(os.path.join(src, 'addon.xml'), encoding='utf-8').read()
    version = re.search(r'<addon [^>]*version="([^"]+)"', xml).group(1)
    out_dir = os.path.join(REPO, addon_id)
    if os.path.isdir(out_dir):
        shutil.rmtree(out_dir)
    os.makedirs(out_dir)
    zip_path = os.path.join(out_dir, '%s-%s.zip' % (addon_id, version))
    with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as z:
        for base, dirs, files in os.walk(src):
            dirs[:] = sorted(d for d in dirs if d != '__pycache__')
            for name in sorted(files):
                if name.endswith(('.pyc', '.pyo')):
                    continue
                path = os.path.join(base, name)
                info = zipfile.ZipInfo(os.path.join(addon_id, os.path.relpath(path, src)), STAMP)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                z.writestr(info, open(path, 'rb').read())
    with open(zip_path + '.sha256', 'w') as f:
        f.write(sha256(zip_path) + '\n')
    for asset in re.findall(r'<(?:icon|fanart)>([^<]+)</', xml):
        dest = os.path.join(out_dir, asset)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        shutil.copyfile(os.path.join(src, asset), dest)
    body = re.sub(r'^<\?xml[^>]*\?>\s*', '', xml).strip()
    return version, zip_path, body


def main():
    os.makedirs(REPO, exist_ok=True)
    entries, links = [], []
    for addon_id in ADDONS:
        version, zip_path, body = build(addon_id)
        entries.append(body)
        print('%s %s' % (addon_id, version))
        if addon_id.startswith('repository.'):
            # a copy at the top, for "Install from zip file" with the repo URL added as a source
            for old in os.listdir(REPO):
                if old.startswith(addon_id) and old.endswith('.zip'):
                    os.remove(os.path.join(REPO, old))
            name = os.path.basename(zip_path)
            shutil.copyfile(zip_path, os.path.join(REPO, name))
            links.append(name)
    index = os.path.join(REPO, 'addons.xml')
    with open(index, 'w', encoding='utf-8', newline='\n') as f:
        f.write('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n<addons>\n')
        f.write('\n'.join(entries) + '\n</addons>\n')
    with open(index + '.sha256', 'w') as f:
        f.write(sha256(index) + '\n')
    # Kodi lists an http source by reading its links; the link text must match the href
    with open(os.path.join(REPO, 'index.html'), 'w', encoding='utf-8', newline='\n') as f:
        f.write('<!doctype html>\n<html><head><meta charset="utf-8"><title>GDrive Bridge repository'
                '</title></head><body>\n<h1>GDrive Bridge repository</h1>\n')
        for name in links:
            f.write('<a href="%s">%s</a><br>\n' % (name, name))
        f.write('</body></html>\n')
    print(REPO)


if __name__ == '__main__':
    main()
