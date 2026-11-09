"""Find and resize the client's window with plain Win32 calls.

Nothing here touches the game: resizing the window is a normal `WM_SIZE`, and the
engine resizes its viewport to the new client area, which is what changes the
rendered resolution. Works on every client version because it never reads code.
"""
import ctypes
import ctypes.wintypes as w

user32 = ctypes.WinDLL('user32', use_last_error=True)
user32.GetWindowThreadProcessId.argtypes = [w.HWND, ctypes.POINTER(w.DWORD)]
user32.GetWindowRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.GetClientRect.argtypes = [w.HWND, ctypes.POINTER(w.RECT)]
user32.SetWindowPos.argtypes = [w.HWND, w.HWND, ctypes.c_int, ctypes.c_int,
                                ctypes.c_int, ctypes.c_int, w.UINT]
user32.IsWindow.argtypes = [w.HWND]
user32.IsWindowVisible.argtypes = [w.HWND]
user32.IsIconic.argtypes = [w.HWND]
user32.GetWindowLongW.argtypes = [w.HWND, ctypes.c_int]
user32.GetWindowLongW.restype = w.LONG

def dpi_aware():
    """Physical pixels for every size we read and write.

    A DPI-unaware process gets its window coordinates scaled by the desktop scale
    (150% here), so asking for 1920x1080 produced a clamped, wrong window. The
    game itself is HighDpiAware, so physical pixels are also what it expects.
    """
    try:
        shcore = ctypes.WinDLL('shcore')
        shcore.SetProcessDpiAwareness(2)          # PROCESS_PER_MONITOR_DPI_AWARE
        return True
    except OSError:
        try:
            return bool(user32.SetProcessDPIAware())
        except OSError:
            return False


dpi_aware()

GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
SWP_NOMOVE = 0x0002
SWP_NOZORDER = 0x0004
SWP_NOACTIVATE = 0x0010


def window_size(client_width, client_height, frame_width, frame_height):
    """Window size that leaves a client area of the requested size."""
    return client_width + frame_width, client_height + frame_height


def client_size(hwnd):
    rect = w.RECT()
    if not user32.GetClientRect(hwnd, ctypes.byref(rect)):
        raise OSError('GetClientRect failed')
    return rect.right - rect.left, rect.bottom - rect.top


def frame_size(hwnd):
    """Non-client border around the client area (title bar, edges)."""
    window, client = w.RECT(), w.RECT()
    if not user32.GetWindowRect(hwnd, ctypes.byref(window)):
        raise OSError('GetWindowRect failed')
    if not user32.GetClientRect(hwnd, ctypes.byref(client)):
        raise OSError('GetClientRect failed')
    return ((window.right - window.left) - (client.right - client.left),
            (window.bottom - window.top) - (client.bottom - client.top))


def resize_client(hwnd, width, height):
    """Make the client area exactly width x height. Returns the real client size."""
    frame_width, frame_height = frame_size(hwnd)
    window_width, window_height = window_size(width, height, frame_width, frame_height)
    if not user32.SetWindowPos(hwnd, None, 0, 0, window_width, window_height,
                               SWP_NOMOVE | SWP_NOZORDER | SWP_NOACTIVATE):
        raise ctypes.WinError(ctypes.get_last_error())
    return client_size(hwnd)


def find_window(pid):
    """The process's main top-level window, or 0 when it has none yet."""
    found = []

    @ctypes.WINFUNCTYPE(w.BOOL, w.HWND, w.LPARAM)
    def visit(hwnd, _):
        owner = w.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
        if owner.value != pid:
            return True
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        if user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & WS_EX_TOOLWINDOW:
            return True
        found.append(hwnd)
        return False

    user32.EnumWindows(visit, 0)
    return found[0] if found else 0
