"""Adds Google Drive folders to Kodi's own sources file (sources.xml).

Add-ons have no API for adding a source, so the entry is written the way Kodi writes one
when a source is added by hand. Kodi only reads the file at startup, so a restart follows.
Other sources are never touched, and the old file is kept as a backup.
"""
import os
import shutil
import urllib.parse
import xml.etree.ElementTree as ET

SECTIONS = ('programs', 'video', 'music', 'pictures', 'files', 'games')
BACKUP_SUFFIX = '.gdrivebridge.bak'


def _same_path(a, b):
    norm = lambda p: urllib.parse.unquote(p or '').rstrip('/').lower()
    return norm(a) == norm(b)


def _load(path):
    try:
        root = ET.parse(path).getroot()
        if root.tag != 'sources':
            raise ValueError('not a Kodi sources file')
    except FileNotFoundError:
        root = ET.Element('sources')
    for name in SECTIONS:
        if root.find(name) is None:
            section = ET.SubElement(root, name)
            ET.SubElement(section, 'default', pathversion='1')
    return root


def add(path, section, name, url):
    """Add a source. Returns the name used (made unique in its section), or None when the
    folder is already a source there."""
    root = _load(path)
    sec = root.find(section)
    names = set()
    for src in sec.findall('source'):
        if _same_path(src.findtext('path'), url):
            return None
        names.add((src.findtext('name') or '').lower())
    base = name.strip() or 'Google Drive'
    n, unique = 1, base
    while unique.lower() in names:
        n += 1
        unique = '%s (%d)' % (base, n)
    src = ET.SubElement(sec, 'source')
    ET.SubElement(src, 'name').text = unique
    ET.SubElement(src, 'path', pathversion='1').text = url
    ET.SubElement(src, 'allowsharing').text = 'true'
    if os.path.exists(path):
        shutil.copy2(path, path + BACKUP_SUFFIX)
    ET.indent(root, space='    ')
    tmp = path + '.tmp'
    ET.ElementTree(root).write(tmp, encoding='utf-8', xml_declaration=False)
    os.replace(tmp, path)
    return unique
