"""Live Win32 check: create a real window, resize it through sf2mod.window."""
import ctypes
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from sf2mod import window


def main():
    ps = ("Add-Type -AssemblyName System.Windows.Forms;"
          "$f = New-Object System.Windows.Forms.Form;"
          "$f.Text = 'sf2 window probe'; $f.Width = 800; $f.Height = 600;"
          "[System.Windows.Forms.Application]::Run($f)")
    process = subprocess.Popen(['powershell.exe', '-NoProfile', '-Command', ps],
                               creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    try:
        pid = process.pid
        hwnd = 0
        for _ in range(40):
            hwnd = window.find_window(pid)
            if hwnd:
                break
            time.sleep(0.25)
        if not hwnd:
            print('FAIL: no window found for pid', pid)
            return 1
        before = window.client_size(hwnd)
        frame = window.frame_size(hwnd)
        print('before:', before, 'frame:', frame)
        actual = window.resize_client(hwnd, 1920, 1080)
        print('after :', actual)
        ok = actual == (1920, 1080)
        print('OK' if ok else 'FAIL', '-> client area is exactly 1920x1080' if ok else '')
        return 0 if ok else 1
    finally:
        process.kill()


if __name__ == '__main__':
    raise SystemExit(main())
