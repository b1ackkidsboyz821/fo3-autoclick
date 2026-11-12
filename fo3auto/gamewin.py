"""Win32 helpers: find the game window, capture its client area, send clicks."""
import ctypes
import os
import threading
import time
from ctypes import wintypes

import numpy as np
import win32api
import win32con
import win32gui
import win32process
import win32ui

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.QueryFullProcessImageNameW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
DWMWA_EXTENDED_FRAME_BOUNDS = 9


def set_dpi_aware():
    """Work in physical pixels so capture and click coordinates agree."""
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        user32.SetProcessDPIAware()


def is_admin():
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def process_name(pid):
    """Executable name without extension, lower case ('' if unknown)."""
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buf = ctypes.create_unicode_buffer(1024)
        size = wintypes.DWORD(1024)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return os.path.splitext(os.path.basename(buf.value))[0].lower()
    finally:
        kernel32.CloseHandle(handle)
    return ""


def client_size(hwnd):
    _, _, w, h = win32gui.GetClientRect(hwnd)
    return w, h


def find_game(name):
    """Return the largest visible top-level window owned by process `name`."""
    name = name.lower()
    found = []

    def visit(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return True
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        if process_name(pid) == name:
            w, h = client_size(hwnd)
            if w > 0 and h > 0:
                found.append((w * h, hwnd))
        return True

    win32gui.EnumWindows(visit, None)
    return max(found)[1] if found else None


def is_minimized(hwnd):
    return bool(win32gui.IsIconic(hwnd))


_hidden = set()   # windows this process made invisible
_GHOST = win32con.WS_EX_LAYERED | win32con.WS_EX_TRANSPARENT


def _ex_style(hwnd):
    return win32gui.GetWindowLong(hwnd, win32con.GWL_EXSTYLE)


def is_hidden(hwnd):
    return hwnd in _hidden


def is_ghost(hwnd):
    """Invisible + click-through, whoever made it so (e.g. a run that crashed)."""
    return _ex_style(hwnd) & _GHOST == _GHOST


def hide_window(hwnd):
    """Make the game fully transparent and click-through, in place.

    A minimized window stops drawing and a window moved off-screen delivers
    one frozen frame. A transparent one keeps rendering at full rate, so
    capture and background clicks carry on while the user works on top of it.
    """
    if hwnd in _hidden:
        return
    if is_minimized(hwnd):
        win32gui.ShowWindow(hwnd, win32con.SW_SHOWNOACTIVATE)
        time.sleep(0.3)
    win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _ex_style(hwnd) | _GHOST)
    user32.SetLayeredWindowAttributes(hwnd, 0, 0, win32con.LWA_ALPHA)
    _hidden.add(hwnd)


def show_window(hwnd):
    """Undo hide_window (also for a game left invisible by an earlier run)."""
    _hidden.discard(hwnd)
    if win32gui.IsWindow(hwnd) and is_ghost(hwnd):
        user32.SetLayeredWindowAttributes(hwnd, 0, 255, win32con.LWA_ALPHA)
        win32gui.SetWindowLong(hwnd, win32con.GWL_EXSTYLE, _ex_style(hwnd) & ~_GHOST)


def _grab(dc_hwnd, w, h, draw):
    """Create a w*h bitmap, let `draw(mem_dc, src_dc)` fill it, return BGR."""
    hdc = win32gui.GetWindowDC(dc_hwnd)
    src = win32ui.CreateDCFromHandle(hdc)
    mem = src.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(src, w, h)
    mem.SelectObject(bmp)
    try:
        draw(mem, src)
        raw = bmp.GetBitmapBits(True)
        return np.frombuffer(raw, np.uint8).reshape(h, w, 4)[:, :, :3].copy()
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        mem.DeleteDC()
        src.DeleteDC()
        win32gui.ReleaseDC(dc_hwnd, hdc)


class _Session:
    """Windows Graphics Capture stream of one window.

    WGC is the only method that returns the real picture of a DirectX game
    while other windows cover it (PrintWindow gives a black frame).
    """

    def __init__(self, hwnd):
        from windows_capture import WindowsCapture
        self.hwnd = hwnd
        self.lock = threading.Lock()
        self.frame = None
        self.count = 0
        self.closed = False
        try:
            cap = WindowsCapture(cursor_capture=False, draw_border=False, window_hwnd=hwnd)
        except Exception:  # borderless capture needs Windows 11
            cap = WindowsCapture(cursor_capture=False, window_hwnd=hwnd)

        @cap.event
        def on_frame_arrived(frame, control):
            img = frame.frame_buffer[:, :, :3].copy()
            with self.lock:
                self.frame = img
                self.count += 1

        @cap.event
        def on_closed():
            self.closed = True

        self.control = cap.start_free_threaded()

    def latest(self):
        with self.lock:
            return self.frame

    def stop(self):
        try:
            self.control.stop()
        except Exception:
            pass


_session = None
_session_lock = threading.Lock()


def stop_capture():
    global _session
    with _session_lock:
        if _session:
            _session.stop()
            _session = None


def _wgc_frame(hwnd, timeout=1.5):
    """Latest whole-window frame from WGC, starting a session if needed."""
    global _session
    with _session_lock:
        if _session is None or _session.hwnd != hwnd or _session.closed:
            if _session:
                _session.stop()
            _session = _Session(hwnd)
        session = _session
    end = time.time() + timeout
    while session.latest() is None and time.time() < end:
        time.sleep(0.03)
    return session.latest()


def _crop_client(hwnd, img):
    """Cut the client area out of a whole-window frame (drops title bar/borders)."""
    bounds = wintypes.RECT()
    ctypes.windll.dwmapi.DwmGetWindowAttribute(
        wintypes.HWND(hwnd), DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(bounds), ctypes.sizeof(bounds))
    cx, cy = win32gui.ClientToScreen(hwnd, (0, 0))
    w, h = client_size(hwnd)
    x0, y0 = cx - bounds.left, cy - bounds.top
    crop = img[y0:y0 + h, x0:x0 + w]
    return crop.copy() if crop.shape[:2] == (h, w) else None


def _unobscured(hwnd):
    """True if the game is the top window at several points of its client area."""
    w, h = client_size(hwnd)
    for fx, fy in ((0.5, 0.5), (0.1, 0.1), (0.9, 0.1), (0.1, 0.9), (0.9, 0.9)):
        pt = win32gui.ClientToScreen(hwnd, (int(w * fx), int(h * fy)))
        top = win32gui.WindowFromPoint(pt)
        if top != hwnd and not win32gui.IsChild(hwnd, top):
            return False
    return True


def capture(hwnd):
    """Capture the client area as a BGR image, or None if it cannot be seen.

    Uses Windows Graphics Capture (works while covered). If that fails, the
    visible screen is copied, but only when nothing covers the game.
    """
    w, h = client_size(hwnd)
    if w <= 0 or h <= 0:
        return None
    try:
        whole = _wgc_frame(hwnd)
        if whole is not None:
            crop = _crop_client(hwnd, whole)
            if crop is not None:
                return crop
    except Exception:
        pass
    if not _unobscured(hwnd):
        return None
    x, y = win32gui.ClientToScreen(hwnd, (0, 0))
    return _grab(win32gui.GetDesktopWindow(), w, h, lambda mem, src: mem.BitBlt(
        (0, 0), (w, h), src, (x, y), win32con.SRCCOPY))


def click_background(hwnd, x, y):
    """Post mouse messages to the window; the real cursor does not move."""
    lparam = win32api.MAKELONG(int(x), int(y))
    win32gui.PostMessage(hwnd, win32con.WM_MOUSEMOVE, 0, lparam)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONDOWN, win32con.MK_LBUTTON, lparam)
    time.sleep(0.06)
    win32gui.PostMessage(hwnd, win32con.WM_LBUTTONUP, 0, lparam)


def click_foreground(hwnd, x, y):
    """Real click: bring the window forward, click, put the cursor back."""
    sx, sy = win32gui.ClientToScreen(hwnd, (int(x), int(y)))
    old = win32api.GetCursorPos()
    try:
        win32gui.SetForegroundWindow(hwnd)
    except Exception:
        pass
    win32api.SetCursorPos((sx, sy))
    time.sleep(0.08)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
    time.sleep(0.06)
    win32api.mouse_event(win32con.MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.08)
    win32api.SetCursorPos(old)
