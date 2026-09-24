"""Windows mouse events, captured on a dedicated message-loop thread."""

import ctypes
from ctypes import wintypes
import queue
import sys
import threading
import time

from mouse_event import MouseEvent


class MouseCapture:
    def __init__(self):
        if sys.platform != "win32":
            raise RuntimeError("Desktop capture requires Windows.")
        self.events = queue.SimpleQueue()
        self.ready = threading.Event()
        self.error = None
        self.thread_id = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()
        if not self.ready.wait(5):
            raise RuntimeError("Mouse capture did not initialize in time.")
        if self.error:
            raise RuntimeError(self.error)

    def stop(self):
        if self.thread and self.thread.is_alive():
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                                 wintypes.WPARAM, wintypes.LPARAM]
            if not user32.PostThreadMessageW(self.thread_id, 0x0012, 0, 0):
                raise ctypes.WinError(ctypes.get_last_error())
            self.thread.join(5)
            if self.thread.is_alive():
                raise RuntimeError("Mouse capture did not stop in time.")

    def _run(self):
        hook = None
        try:
            user32 = ctypes.WinDLL("user32", use_last_error=True)
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            result_type = ctypes.c_ssize_t
            callback_type = ctypes.WINFUNCTYPE(result_type, ctypes.c_int,
                                               wintypes.WPARAM, wintypes.LPARAM)

            class HookData(ctypes.Structure):
                _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                            ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                            ("extra", ctypes.c_size_t)]

            user32.SetWindowsHookExW.argtypes = [ctypes.c_int, callback_type,
                                                wintypes.HINSTANCE, wintypes.DWORD]
            user32.SetWindowsHookExW.restype = wintypes.HANDLE
            user32.CallNextHookEx.argtypes = [wintypes.HANDLE, ctypes.c_int,
                                             wintypes.WPARAM, wintypes.LPARAM]
            user32.CallNextHookEx.restype = result_type
            user32.UnhookWindowsHookEx.argtypes = [wintypes.HANDLE]
            user32.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                           wintypes.HWND, wintypes.UINT, wintypes.UINT]
            user32.GetMessageW.restype = wintypes.BOOL
            user32.PeekMessageW.argtypes = [ctypes.POINTER(wintypes.MSG),
                                            wintypes.HWND, wintypes.UINT,
                                            wintypes.UINT, wintypes.UINT]
            kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
            kernel32.GetModuleHandleW.restype = wintypes.HMODULE
            kernel32.GetCurrentThreadId.restype = wintypes.DWORD
            kinds = {0x0200: ("move", ""), 0x0201: ("down", "left"),
                     0x0202: ("up", "left"), 0x0204: ("down", "right"),
                     0x0205: ("up", "right"), 0x0207: ("down", "middle"),
                     0x0208: ("up", "middle"), 0x020A: ("wheel", ""),
                     0x020E: ("horizontal_wheel", ""),
                     0x020B: ("down", "x"), 0x020C: ("up", "x")}

            @callback_type
            def callback(code, message, address):
                if code >= 0 and message in kinds:
                    data = ctypes.cast(address, ctypes.POINTER(HookData)).contents
                    # Keep programmatically injected events out of the dataset.
                    if not data.flags & 0x01:
                        kind, button = kinds[message]
                        high = (data.mouseData >> 16) & 0xFFFF
                        if button == "x":
                            button = "x1" if high == 1 else "x2"
                        delta = ctypes.c_short(high).value if "wheel" in kind else 0
                        self.events.put(MouseEvent(time.perf_counter_ns(),
                                                   data.pt.x, data.pt.y,
                                                   kind, button, delta))
                return user32.CallNextHookEx(None, code, message, address)

            self.thread_id = kernel32.GetCurrentThreadId()
            msg = wintypes.MSG()
            user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)
            hook = user32.SetWindowsHookExW(14, callback,
                                          kernel32.GetModuleHandleW(None), 0)
            if not hook:
                raise ctypes.WinError(ctypes.get_last_error())
            self.ready.set()
            while True:
                result = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
        except Exception as error:
            self.error = str(error)
            self.ready.set()
        finally:
            if hook:
                user32.UnhookWindowsHookEx(hook)


def configure_desktop():
    user32 = ctypes.WinDLL("user32", use_last_error=True)
    # Per-monitor DPI awareness keeps coordinates in physical pixels.
    try:
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))
    except AttributeError:
        user32.SetProcessDPIAware()
    return tuple(user32.GetSystemMetrics(index) for index in (76, 77, 78, 79))


def pixel_scale_at(x, y):
    # Per-monitor DPI awareness already reports physical pixels.
    return 1.0
