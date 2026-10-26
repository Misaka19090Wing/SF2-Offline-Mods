"""Write or check the integrity manifest, optionally build a zip.

    runtime\\python\\python.exe -X utf8 -B tools\\build_manifest.py          # write MANIFEST.sha256
    runtime\\python\\python.exe -X utf8 -B tools\\build_manifest.py --check  # verify (exit 1 on drift)
    runtime\\python\\python.exe -X utf8 -B tools\\build_manifest.py --zip corpse-limit-1.0.0.zip
"""
import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD = HERE.parent
MANIFEST = MOD / 'MANIFEST.sha256'
EXCLUDE = {'logs', '__pycache__'}
SKIP_SUFFIX = ('.pyc', '.zip')


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def files():
    for path in sorted(MOD.rglob('*')):
        if not path.is_file() or path == MANIFEST:
            continue
        relative = path.relative_to(MOD)
        if any(part in EXCLUDE for part in relative.parts) or path.suffix in SKIP_SUFFIX:
            continue
        yield relative


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--check', action='store_true')
    parser.add_argument('--zip', type=Path)
    args = parser.parse_args()
    if args.check:
        expected = {}
        for line in MANIFEST.read_text('utf-8').splitlines():
            if line.strip():
                value, name = line.split('  ', 1)
                expected[name] = value
        actual = {str(name.as_posix()): digest(MOD / name) for name in files()}
        missing = sorted(set(expected) - set(actual))
        extra = sorted(set(actual) - set(expected))
        changed = sorted(n for n in set(expected) & set(actual) if expected[n] != actual[n])
        print('manifest: %d files, missing=%d changed=%d extra=%d'
              % (len(actual), len(missing), len(changed), len(extra)))
        for name in missing:
            print('  missing', name)
        for name in changed:
            print('  changed', name)
        for name in extra:
            print('  extra', name)
        return 0 if not (missing or changed or extra) else 1
    lines = ['%s  %s' % (digest(MOD / name), name.as_posix()) for name in files()]
    MANIFEST.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    print('manifest: %d files -> %s' % (len(lines), MANIFEST))
    if args.zip:
        with zipfile.ZipFile(args.zip, 'w', zipfile.ZIP_DEFLATED) as archive:
            for name in files():
                archive.write(MOD / name, (Path(MOD.name) / name).as_posix())
            archive.write(MANIFEST, (Path(MOD.name) / MANIFEST.name).as_posix())
        print('zip: %s' % args.zip)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
