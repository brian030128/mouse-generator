"""macOS mouse events, captured by a listen-only Quartz event tap on its own run loop."""

import ctypes
import ctypes.util
import queue
import sys
import threading
import time

from mouse_event import MouseEvent


class CGPoint(ctypes.Structure):
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class CGRect(ctypes.Structure):
    _fields_ = [("origin", CGPoint), ("width", ctypes.c_double), ("height", ctypes.c_double)]


TapCallback = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_uint32,
                               ctypes.c_void_p, ctypes.c_void_p)

_quartz = None
_cf = None


def _libraries():
    global _quartz, _cf
    if _quartz is None:
        quartz = ctypes.CDLL(ctypes.util.find_library("ApplicationServices"))
        cf = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
        quartz.CGEventTapCreate.argtypes = [ctypes.c_uint32, ctypes.c_uint32, ctypes.c_uint32,
                                            ctypes.c_uint64, TapCallback, ctypes.c_void_p]
        quartz.CGEventTapCreate.restype = ctypes.c_void_p
        quartz.CGEventTapEnable.argtypes = [ctypes.c_void_p, ctypes.c_bool]
        quartz.CGEventGetLocation.argtypes = [ctypes.c_void_p]
        quartz.CGEventGetLocation.restype = CGPoint
        quartz.CGEventGetIntegerValueField.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        quartz.CGEventGetIntegerValueField.restype = ctypes.c_int64
        quartz.CGGetActiveDisplayList.argtypes = [ctypes.c_uint32,
                                                  ctypes.POINTER(ctypes.c_uint32),
                                                  ctypes.POINTER(ctypes.c_uint32)]
        quartz.CGGetActiveDisplayList.restype = ctypes.c_int32
        quartz.CGDisplayBounds.argtypes = [ctypes.c_uint32]
        quartz.CGDisplayBounds.restype = CGRect
        quartz.CGGetDisplaysWithPoint.argtypes = [CGPoint, ctypes.c_uint32,
                                                  ctypes.POINTER(ctypes.c_uint32),
                                                  ctypes.POINTER(ctypes.c_uint32)]
        quartz.CGGetDisplaysWithPoint.restype = ctypes.c_int32
        quartz.CGDisplayCopyDisplayMode.argtypes = [ctypes.c_uint32]
        quartz.CGDisplayCopyDisplayMode.restype = ctypes.c_void_p
        quartz.CGDisplayModeGetPixelWidth.argtypes = [ctypes.c_void_p]
        quartz.CGDisplayModeGetPixelWidth.restype = ctypes.c_size_t
        quartz.CGDisplayModeRelease.argtypes = [ctypes.c_void_p]
        quartz.CGEventSourceKeyState.argtypes = [ctypes.c_int32, ctypes.c_uint16]
        quartz.CGEventSourceKeyState.restype = ctypes.c_bool
        quartz.CGRequestListenEventAccess.restype = ctypes.c_bool
        cf.CFMachPortCreateRunLoopSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_long]
        cf.CFMachPortCreateRunLoopSource.restype = ctypes.c_void_p
        cf.CFMachPortInvalidate.argtypes = [ctypes.c_void_p]
        cf.CFRunLoopGetCurrent.restype = ctypes.c_void_p
        cf.CFRunLoopAddSource.argtypes = [ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p]
        cf.CFRunLoopStop.argtypes = [ctypes.c_void_p]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        _quartz, _cf = quartz, cf
    return _quartz, _cf


# CGEventType values mapped to the same kinds and buttons the Windows hook records.
# Dragging reports as movement, as WM_MOUSEMOVE does on Windows.
KINDS = {5: ("move", ""), 6: ("move", ""), 7: ("move", ""), 27: ("move", ""),
         1: ("down", "left"), 2: ("up", "left"),
         3: ("down", "right"), 4: ("up", "right"),
         25: ("down", None), 26: ("up", None)}
OTHER_BUTTONS = {2: "middle", 3: "x1", 4: "x2"}
SCROLL = 22
TAP_DISABLED = (0xFFFFFFFE, 0xFFFFFFFF)
BUTTON_NUMBER = 3
SCROLL_FIXED_Y = 93
SCROLL_FIXED_X = 94
SOURCE_STATE_ID = 45
HID_SYSTEM_STATE = 1
WHEEL_DELTA = 120  # Windows units per wheel notch; one macOS scroll line maps to one notch.


class MouseCapture:
    def __init__(self):
        if sys.platform != "darwin":
            raise RuntimeError("macOS capture requires macOS.")
        self.events = queue.SimpleQueue()
        self.ready = threading.Event()
        self.error = None
        self.run_loop = None
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
            _libraries()[1].CFRunLoopStop(self.run_loop)
            self.thread.join(5)
            if self.thread.is_alive():
                raise RuntimeError("Mouse capture did not stop in time.")

    def _run(self):
        tap = source = None
        quartz, cf = _libraries()
        try:
            mask = sum(1 << kind for kind in (*KINDS, SCROLL))

            @TapCallback
            def callback(proxy, kind, event, info):
                if kind in TAP_DISABLED:
                    # macOS disables slow taps; resume rather than silently stop recording.
                    quartz.CGEventTapEnable(tap, True)
                    return event
                # Keep programmatically posted events out of the dataset.
                if quartz.CGEventGetIntegerValueField(event, SOURCE_STATE_ID) != HID_SYSTEM_STATE:
                    return event
                now = time.perf_counter_ns()
                point = quartz.CGEventGetLocation(event)
                x, y = round(point.x), round(point.y)
                if kind == SCROLL:
                    for field, name, sign in ((SCROLL_FIXED_Y, "wheel", 1),
                                              (SCROLL_FIXED_X, "horizontal_wheel", -1)):
                        lines = quartz.CGEventGetIntegerValueField(event, field) / 65536
                        delta = sign * round(lines * WHEEL_DELTA)
                        if delta:
                            self.events.put(MouseEvent(now, x, y, name, "", delta))
                elif kind in KINDS:
                    name, button = KINDS[kind]
                    if button is None:
                        number = quartz.CGEventGetIntegerValueField(event, BUTTON_NUMBER)
                        button = OTHER_BUTTONS.get(number, f"button{number}")
                    self.events.put(MouseEvent(now, x, y, name, button))
                return event

            # Session tap, head insert, listen-only: events pass through unchanged.
            tap = quartz.CGEventTapCreate(1, 0, 1, mask, callback, None)
            if not tap:
                raise RuntimeError(
                    "macOS refused mouse capture. Allow this app (Terminal or Python) under "
                    "System Settings > Privacy & Security > Input Monitoring, then reopen it.")
            source = cf.CFMachPortCreateRunLoopSource(None, tap, 0)
            self.run_loop = cf.CFRunLoopGetCurrent()
            common_modes = ctypes.c_void_p.in_dll(cf, "kCFRunLoopCommonModes")
            cf.CFRunLoopAddSource(self.run_loop, source, common_modes)
            quartz.CGEventTapEnable(tap, True)
            self.ready.set()
            cf.CFRunLoopRun()
        except Exception as error:
            self.error = str(error)
            self.ready.set()
        finally:
            if tap:
                quartz.CGEventTapEnable(tap, False)
                cf.CFMachPortInvalidate(tap)
                cf.CFRelease(tap)
            if source:
                cf.CFRelease(source)


def configure_desktop():
    quartz, _ = _libraries()
    # F8 polling needs Input Monitoring; this prompts once and is a no-op afterwards.
    quartz.CGRequestListenEventAccess()
    ids = (ctypes.c_uint32 * 32)()
    count = ctypes.c_uint32()
    if quartz.CGGetActiveDisplayList(32, ids, ctypes.byref(count)) or not count.value:
        raise RuntimeError("Cannot read the display layout.")
    rects = [quartz.CGDisplayBounds(ids[i]) for i in range(count.value)]
    left = min(r.origin.x for r in rects)
    top = min(r.origin.y for r in rects)
    right = max(r.origin.x + r.width for r in rects)
    bottom = max(r.origin.y + r.height for r in rects)
    return tuple(round(value) for value in (left, top, right - left, bottom - top))


def pixel_scale_at(x, y):
    """Physical pixels per point on the display containing (x, y), or None if unknown."""
    quartz, _ = _libraries()
    display = ctypes.c_uint32()
    count = ctypes.c_uint32()
    if quartz.CGGetDisplaysWithPoint(CGPoint(x, y), 1, ctypes.byref(display),
                                     ctypes.byref(count)) or not count.value:
        return None
    mode = quartz.CGDisplayCopyDisplayMode(display.value)
    if not mode:
        return None
    try:
        pixels = quartz.CGDisplayModeGetPixelWidth(mode)
    finally:
        quartz.CGDisplayModeRelease(mode)
    points = quartz.CGDisplayBounds(display.value).width
    return pixels / points if points else None


def stop_key_pressed():
    # Only this fixed control key is checked; no typed text is collected.
    # Keycode 100 is F8; on Apple keyboards this may need fn+F8.
    return bool(_libraries()[0].CGEventSourceKeyState(HID_SYSTEM_STATE, 100))
