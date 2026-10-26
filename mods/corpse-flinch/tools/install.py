# -*- coding: utf-8 -*-
"""安装 corpse-flinch 到游戏目录。

用法（一般由 安装.cmd 调用，也可以手动）：

    <game>\\runtime\\python\\python.exe -X utf8 -B tools\\install.py
    <game>\\runtime\\python\\python.exe -X utf8 -B tools\\install.py --game "D:\\Games\\SF2-Offline"
    ... --no-start     只装不启动
    ... --no-selftest  跳过只读自检（自动化测试用）

做的事：
  1. 找游戏目录（参数 → 从本文件往上找 runtime\\tools\\local_battle_bridge.py → 弹窗问）
  2. 停掉还在跑的旧 worker（它握着旧代码和日志文件）
  3. 把整个补丁目录复制到 <game>\\mods\\corpse-flinch（已经在那儿就跳过）
  4. 逐项校验装完的文件，缺一个就报错，不假装成功
  5. 在游戏根目录生成 ASCII 启动器 启动-鞭尸模式.cmd
  6. 跑一次只读自检（可关），再问你要不要现在就挂上 worker

不写注册表、不改游戏文件、不需要管理员权限。
"""
import argparse
import ctypes
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # ...\corpse-flinch
MARKER = ('runtime', 'tools', 'local_battle_bridge.py')
LAUNCHER = '启动-鞭尸模式.cmd'
SKIP_DIRS = {'logs', '__pycache__', '.git'}
SKIP_SUFFIX = {'.zip', '.pyc'}
CREATE_NO_WINDOW = 0x08000000
# Without these the patch is installed but dead; check them instead of hoping.
REQUIRED = ('corpse_flinch.py', 'config.json', 'sf2mod\\game.py',
            'sf2mod\\attach.py', 'tools\\launch.py', 'tools\\uninstall.py')


def console():
    """Make Chinese output readable in a cp936 console."""
    try:
        ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        ctypes.windll.kernel32.SetConsoleCP(65001)
    except Exception:                                  # noqa: BLE001
        pass
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding='utf-8', errors='replace')
        except Exception:                              # noqa: BLE001
            pass


def is_game(path):
    base = Path(path)
    return (base.joinpath(*MARKER).is_file()
            and (base / 'runtime' / 'python' / 'python.exe').is_file())


def find_game(explicit=None):
    if explicit:
        return Path(explicit).resolve()
    for parent in [ROOT, *ROOT.parents]:
        if is_game(parent):
            return parent
    return ask_game()


def ask_game():
    print('没找到游戏目录。请选择包含 runtime 和 test-client 的那个文件夹。')
    try:
        import tkinter
        from tkinter import filedialog
        root = tkinter.Tk()
        root.withdraw()
        chosen = filedialog.askdirectory(title='选择游戏目录（含 runtime 文件夹）')
        root.destroy()
    except Exception:                                  # noqa: BLE001
        chosen = ''
    if not chosen:
        chosen = input('游戏目录: ').strip().strip('"')
    return Path(chosen).resolve() if chosen else None


def alive(pid):
    """True while pid is a live process (OpenProcess + GetExitCodeProcess)."""
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, int(pid))   # QUERY_LIMITED
        if not handle:
            return False
        try:
            code = ctypes.c_ulong(0)
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259                            # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                                  # noqa: BLE001
        return False


def terminate(pid):
    try:
        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x0001, False, int(pid))   # TERMINATE
        if not handle:
            return False
        try:
            return bool(kernel32.TerminateProcess(handle, 0))
        finally:
            kernel32.CloseHandle(handle)
    except Exception:                                  # noqa: BLE001
        return False


def worker_pids(dest):
    """Every process that is running *this copy's* worker.

    logs\\worker.pid is the fast path; the process table is the fallback for a
    worker started by an older build (no pid file) or by hand.  The command line
    must contain this pack's own path: several copies (a second game directory, a
    test install, a backup) can run side by side, and installing one must not kill
    the worker of another.
    """
    pids = set()
    pidfile = Path(dest) / 'logs' / 'worker.pid'
    try:
        pids.add(int(pidfile.read_text(encoding='ascii').strip()))
    except (OSError, ValueError):
        pass
    script = 'corpse_flinch.py'
    wanted = str(Path(dest).resolve()).casefold()
    try:
        out = subprocess.run(
            ['powershell', '-NoProfile', '-Command',
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, encoding='utf-8', errors='replace',
            timeout=20, creationflags=CREATE_NO_WINDOW)
        data = json.loads(out.stdout or '[]')
        if isinstance(data, dict):
            data = [data]
        for row in data or []:
            line = str(row.get('CommandLine') or '').casefold()
            if script in line and wanted in line:
                pids.add(int(row['ProcessId']))
    except Exception:                                  # noqa: BLE001
        pass
    return {pid for pid in pids if pid > 0 and alive(pid)}


def stop_worker(dest):
    pids = worker_pids(dest)
    for pid in sorted(pids):
        print(f'      停掉旧 worker  PID {pid}')
        terminate(pid)
    for _ in range(40):
        if not any(alive(pid) for pid in pids):
            break
        time.sleep(0.1)
    try:
        (Path(dest) / 'logs' / 'worker.pid').unlink()
    except OSError:
        pass
    return len(pids)


def copy_tree(source, target):
    """Copy the package, skipping logs/zips; tolerate source == target."""
    if Path(source).resolve() == Path(target).resolve():
        print(f'  补丁已经在 {target}，跳过复制。')
        return target
    target.mkdir(parents=True, exist_ok=True)
    for path in sorted(Path(source).rglob('*')):
        rel = path.relative_to(source)
        if any(part in SKIP_DIRS for part in rel.parts):
            continue
        if path.is_dir():
            (target / rel).mkdir(parents=True, exist_ok=True)
            continue
        if path.suffix in SKIP_SUFFIX:
            continue
        (target / rel).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target / rel)
    return target


def verify(dest):
    missing = [rel for rel in REQUIRED if not (Path(dest) / rel).is_file()]
    if missing:
        print('[错误] 装完缺文件，安装没有成功：')
        for rel in missing:
            print(f'    {dest}\\{rel}')
        return False
    count = sum(1 for path in Path(dest).rglob('*')
                if path.is_file() and path.suffix not in SKIP_SUFFIX)
    print(f'      校验      {count} 个文件齐全')
    return True


def write_launcher(game, dest):
    """ASCII-only batch file: cmd.exe never has to parse non-ASCII text.

    Paths come from %~dp0 (the launcher's own folder = game root), so even a
    game installed under a Chinese path stays out of the batch file.
    """
    launcher = Path(game) / LAUNCHER
    text = '\r\n'.join([
        '@echo off',
        'setlocal',
        '"%~dp0runtime\\python\\pythonw.exe" -X utf8 -B '
        '"%~dp0mods\\corpse-flinch\\tools\\launch.py"',
        'exit /b',
        '',
    ])
    launcher.write_text(text, encoding='ascii', newline='')
    return launcher


def start_worker(game, dest):
    """Run the launcher detached, then confirm a worker really came up."""
    python = Path(game) / 'runtime' / 'python'
    exe = python / 'pythonw.exe'
    if not exe.is_file():
        exe = python / 'python.exe'
    console = Path(dest) / 'logs' / 'worker-console.log'
    try:
        console.parent.mkdir(parents=True, exist_ok=True)
        err = console.open('ab')
    except OSError:
        err = subprocess.DEVNULL
    try:
        subprocess.Popen([str(exe), '-X', 'utf8', '-B',
                          str(Path(dest) / 'tools' / 'launch.py')],
                         cwd=str(dest), stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=err,
                         creationflags=CREATE_NO_WINDOW)
    finally:
        if err is not subprocess.DEVNULL:
            err.close()
    pidfile = Path(dest) / 'logs' / 'worker.pid'
    for _ in range(60):                                # up to 12 s: game start
        try:
            pid = int(pidfile.read_text(encoding='ascii').strip())
        except (OSError, ValueError):
            pid = 0
        if pid and alive(pid):
            return pid
        time.sleep(0.2)
    return 0


def main():
    console()
    parser = argparse.ArgumentParser()
    parser.add_argument('--game', default=None, help='游戏目录')
    parser.add_argument('--no-selftest', action='store_true')
    parser.add_argument('--no-start', action='store_true')
    parser.add_argument('--start', action='store_true',
                        help='装完直接挂 worker，不再询问')
    args = parser.parse_args()

    game = find_game(args.game)
    if not game or not is_game(game):
        print('[错误] 这不是游戏目录：缺少 runtime\\python\\python.exe 或 '
              'runtime\\tools\\local_battle_bridge.py')
        print(f'       当前判断的目录：{game}')
        print('       请把 corpse-flinch 整个文件夹放进 <游戏>\\mods\\ 再运行安装。')
        return 1

    print(f'[1/4] 游戏目录  {game}')
    dest = Path(game) / 'mods' / 'corpse-flinch'
    stop_worker(dest)

    copy_tree(ROOT, dest)
    print(f'[2/4] 已安装到  {dest}')
    if not verify(dest):
        return 1

    launcher = write_launcher(game, dest)
    print(f'[3/4] 启动器    {launcher}')

    print('[4/4] 自检 ...')
    if not args.no_selftest:
        result = subprocess.run(
            [str(Path(game) / 'runtime' / 'python' / 'python.exe'),
             '-X', 'utf8', '-B', str(dest / 'tools' / 'selftest.py')],
            cwd=str(dest), capture_output=True, text=True,
            encoding='utf-8', errors='replace')
        print(result.stdout.strip())
        if result.returncode != 0:
            print('     （自检没全过。游戏没开、或还没进对局时这是正常的，'
                  '见 docs\\04-排错.md。）')

    print()
    if args.no_start:
        start = False
    elif args.start:
        start = True
    else:
        print('现在就把鞭尸挂上吗？（游戏没开的话会先启动游戏）')
        try:
            answer = input('  回车 = 是，输入 n = 稍后自己双击启动器: ')
        except EOFError:
            answer = 'n'
        start = answer.strip().lower() not in ('n', 'no', '否')

    if start:
        print('正在启动 ...')
        pid = start_worker(game, dest)
        if pid:
            print(f'      worker 已运行  PID {pid}')
        else:
            print(f'      [警告] worker 没起来。先看 {dest}\\logs\\launcher.log，'
                  f'再看 worker-console.log。')
    print()
    print('装好了。以后这样用：')
    print(f'  双击 {launcher}')
    print('  ——它会先启动游戏，再在后台挂上鞭尸。')
    print(f'日志：{dest}\\logs\\   配置：{dest}\\config.json')
    print('卸载：双击 mods\\corpse-flinch\\卸载.cmd')
    return 0


if __name__ == '__main__':
    sys.exit(main())
