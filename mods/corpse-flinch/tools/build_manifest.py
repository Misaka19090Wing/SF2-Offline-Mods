"""重新生成 MANIFEST.sha256（可选：顺便打一个 zip）。

    runtime\\python\\python.exe -X utf8 tools\\build_manifest.py
    runtime\\python\\python.exe -X utf8 tools\\build_manifest.py --zip ..\\..\\..\\dist\\corpse-flinch-1.0.0.zip

改过任何文件之后跑一次，别人拿到包就能用清单核对内容是否被改过。
"""
import argparse
import hashlib
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKIP_DIRS = {'logs', '__pycache__', '.git'}
SKIP_SUFFIX = {'.zip', '.pyc'}
SKIP_NAMES = {'MANIFEST.sha256'}
# The zip is extracted straight into the game folder, so it carries the mods
# layer itself: <game>\mods\corpse-flinch\...  No "extract into <game>\mods\"
# step, and no chance of ending up with <game>\mods\mods\corpse-flinch\.
PREFIX = Path('mods') / 'corpse-flinch'


def files():
    for path in sorted(ROOT.rglob('*')):
        if not path.is_file():
            continue
        rel = path.relative_to(ROOT)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.suffix in SKIP_SUFFIX or path.name in SKIP_NAMES:
            continue
        yield path, rel


def sha256(path):
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--zip', default=None, help='额外打出这个 zip')
    parser.add_argument('--check', action='store_true',
                        help='只核对现有 MANIFEST.sha256，不重写')
    args = parser.parse_args()

    if args.check:
        return check()

    rows = [(sha256(path), rel.as_posix()) for path, rel in files()]
    manifest = ROOT / 'MANIFEST.sha256'
    manifest.write_text(''.join(f'{digest}  {rel}\n' for digest, rel in rows),
                        encoding='utf-8', newline='\n')
    print(f'{manifest}  {len(rows)} 个文件')

    if args.zip:
        target = Path(args.zip).resolve()
        target.parent.mkdir(parents=True, exist_ok=True)
        with zipfile.ZipFile(target, 'w', zipfile.ZIP_DEFLATED) as archive:
            for path, rel in files():
                archive.write(path, (PREFIX / rel).as_posix())
            archive.write(manifest, (PREFIX / 'MANIFEST.sha256').as_posix())
        print(f'{target}  {target.stat().st_size / 1024:.0f} KB')
    return 0


def check():
    """Compare the tree against MANIFEST.sha256; report added/missing/modified."""
    manifest = ROOT / 'MANIFEST.sha256'
    if not manifest.is_file():
        print(f'{manifest} 不存在')
        return 1
    expected = {}
    for line in manifest.read_text(encoding='utf-8').splitlines():
        digest, _, rel = line.partition('  ')
        if rel:
            expected[rel] = digest
    actual = {rel.as_posix(): sha256(path) for path, rel in files()}
    problems = 0
    for rel in sorted(expected.keys() - actual.keys()):
        print(f'缺失  {rel}')
        problems += 1
    for rel in sorted(actual.keys() - expected.keys()):
        print(f'多出  {rel}')
        problems += 1
    for rel in sorted(expected.keys() & actual.keys()):
        if expected[rel] != actual[rel]:
            print(f'被改  {rel}')
            problems += 1
    print('一致' if not problems else f'{problems} 处不一致')
    return 1 if problems else 0


if __name__ == '__main__':
    sys.exit(main())
