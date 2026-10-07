"""Size of the clicked on-screen element, from Windows UI Automation.

At every left press the capture hook queues the click point; a worker thread
asks UI Automation (the accessibility interface screen readers use) which
element is there and records its bounding box and control type. Only geometry
and the numeric control type are kept: element names, values and window
titles are never read, so no on-screen text reaches the database.

The lookup runs off the hook thread because a low-level mouse hook must return
within milliseconds, and it happens at the press, before most applications
react on release. Requests older than MAX_AGE_MS when the worker reaches them
are skipped rather than probing a screen that may have changed.

Coverage: native Windows applications, Explorer, the Windows 11 taskbar,
Office, and Chromium-based browsers and Electron apps report real controls
(a browser builds its accessibility tree at the first query, so the first
click after the recorder starts may only resolve to the whole page). Games,
canvas applications and remote desktops report only a container (pane,
window, document), which training should treat as an unknown target size.
UI Automation is called through ctypes, so no packages are needed.
"""

import ctypes
import queue
import sys
import threading
import time
from dataclasses import dataclass

MAX_AGE_MS = 500.0
KEEP_UNMATCHED_NS = 60_000_000_000

# UI Automation control type ids.
CONTROL_TYPES = {
    50000: "button", 50001: "calendar", 50002: "check box", 50003: "combo box",
    50004: "edit", 50005: "hyperlink", 50006: "image", 50007: "list item",
    50008: "list", 50009: "menu", 50010: "menu bar", 50011: "menu item",
    50012: "progress bar", 50013: "radio button", 50014: "scroll bar",
    50015: "slider", 50016: "spinner", 50017: "status bar", 50018: "tab",
    50019: "tab item", 50020: "text", 50021: "tool bar", 50022: "tool tip",
    50023: "tree", 50024: "tree item", 50025: "custom", 50026: "group",
    50027: "thumb", 50028: "data grid", 50029: "data item", 50030: "document",
    50031: "split button", 50032: "window", 50033: "pane", 50034: "header",
    50035: "header item", 50036: "table", 50037: "title bar", 50038: "separator",
    50039: "semantic zoom", 50040: "app bar",
}
# Types that describe a whole surface rather than a clickable control.
CONTAINER_TYPES = frozenset({50025, 50026, 50030, 50032, 50033})


@dataclass(frozen=True)
class Target:
    left: int
    top: int
    width: int
    height: int
    control_type: int | None
    delay_ms: float      # press to completed lookup


# COM vtable slots.
_RELEASE = 2
_ELEMENT_FROM_POINT = 7          # IUIAutomation
_CURRENT_CONTROL_TYPE = 21       # IUIAutomationElement
_CURRENT_BOUNDING_RECTANGLE = 43
_CLSID_CUIAUTOMATION = "{FF48DBA4-60EF-4201-AA87-54103EEF594E}"
_IID_IUIAUTOMATION = "{30CBE57D-D9D0-452A-AB13-7AC5AC4825EE}"


def _method(pointer, index, *argtypes):
    vtable = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    return ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)(vtable[index])


class UiaLocator:
    """Element geometry at a screen point. Create and call it on one thread."""

    def __init__(self):
        from ctypes import wintypes

        class Guid(ctypes.Structure):
            _fields_ = [("a", ctypes.c_ulong), ("b", ctypes.c_ushort), ("c", ctypes.c_ushort),
                        ("d", ctypes.c_ubyte * 8)]

        self.wintypes = wintypes
        self.ole32 = ctypes.WinDLL("ole32")
        self.ole32.CoInitializeEx(None, 0)                 # multithreaded apartment

        def guid(text):
            value = Guid()
            if self.ole32.CLSIDFromString(ctypes.c_wchar_p(text), ctypes.byref(value)) < 0:
                raise OSError(f"bad GUID {text}")
            return value

        self.automation = ctypes.c_void_p()
        result = self.ole32.CoCreateInstance(
            ctypes.byref(guid(_CLSID_CUIAUTOMATION)), None, 1,
            ctypes.byref(guid(_IID_IUIAUTOMATION)), ctypes.byref(self.automation))
        if result < 0 or not self.automation:
            raise OSError(f"UI Automation unavailable (0x{result & 0xFFFFFFFF:08X})")
        self.element_from_point = _method(self.automation, _ELEMENT_FROM_POINT,
                                          wintypes.POINT, ctypes.POINTER(ctypes.c_void_p))

    def __call__(self, x, y):
        """(left, top, width, height, control type) at (x, y), or None."""
        wintypes = self.wintypes
        element = ctypes.c_void_p()
        if self.element_from_point(self.automation, wintypes.POINT(x, y),
                                   ctypes.byref(element)) < 0 or not element:
            return None
        try:
            rect = wintypes.RECT()
            if _method(element, _CURRENT_BOUNDING_RECTANGLE, ctypes.POINTER(wintypes.RECT))(
                    element, ctypes.byref(rect)) < 0:
                return None
            control = ctypes.c_int()
            control_type = None
            if _method(element, _CURRENT_CONTROL_TYPE, ctypes.POINTER(ctypes.c_int))(
                    element, ctypes.byref(control)) >= 0:
                control_type = control.value
            return rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, control_type
        finally:
            _method(element, _RELEASE)(element)

    def close(self):
        if self.automation:
            _method(self.automation, _RELEASE)(self.automation)
            self.automation = ctypes.c_void_p()
        self.ole32.CoUninitialize()


class TargetProbe:
    """Worker thread that turns click points into Targets.

    request() is safe to call from the mouse hook: it only enqueues. Results
    appear on .results as (timestamp_ns, Target or None). locator_factory runs
    on the worker thread, which UI Automation requires of its COM objects.
    """

    def __init__(self, max_age_ms=MAX_AGE_MS, locator_factory=UiaLocator):
        self.requests = queue.SimpleQueue()
        self.results = queue.SimpleQueue()
        self.max_age_ms = max_age_ms
        self.locator_factory = locator_factory
        self.error = None
        self.thread = None

    def start(self):
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def request(self, timestamp_ns, x, y):
        self.requests.put((timestamp_ns, x, y))

    def stop(self, timeout=1.0):
        if self.thread and self.thread.is_alive():
            self.requests.put(None)
            self.thread.join(timeout)

    def _run(self):
        try:
            locator = self.locator_factory()
        except OSError as error:
            # Recording continues without target sizes.
            self.error = str(error)
            return
        try:
            while True:
                item = self.requests.get()
                if item is None:
                    break
                timestamp_ns, x, y = item
                target = None
                if (time.perf_counter_ns() - timestamp_ns) / 1e6 <= self.max_age_ms:
                    try:
                        found = locator(x, y)
                    except OSError:
                        found = None
                    if found is not None:
                        delay_ms = (time.perf_counter_ns() - timestamp_ns) / 1e6
                        target = Target(*found, delay_ms=round(delay_ms, 2))
                self.results.put((timestamp_ns, target))
        finally:
            close = getattr(locator, "close", None)
            if close:
                close()


class TargetLedger:
    """Matches lookup results to saved segments, whichever arrives first.

    A segment ends at a left press, and both the segment and the lookup are
    keyed by that press's timestamp. Unmatched entries expire after a minute
    (lookups for presses that did not end a saved segment, or segments whose
    lookup was skipped).
    """

    def __init__(self, keep_ns=KEEP_UNMATCHED_NS):
        self.keep_ns = keep_ns
        self.results = {}       # timestamp_ns -> Target or None
        self.segments = {}      # timestamp_ns -> segment id

    def segment_saved(self, timestamp_ns, segment_id):
        """Returns [(segment_id, Target)] ready to write."""
        if timestamp_ns in self.results:
            target = self.results.pop(timestamp_ns)
            return [(segment_id, target)] if target is not None else []
        self.segments[timestamp_ns] = segment_id
        return []

    def result(self, timestamp_ns, target):
        """Returns [(segment_id, Target)] ready to write."""
        if timestamp_ns in self.segments:
            segment_id = self.segments.pop(timestamp_ns)
            return [(segment_id, target)] if target is not None else []
        self.results[timestamp_ns] = target
        return []

    def expire(self, now_ns):
        cutoff = now_ns - self.keep_ns
        for table in (self.results, self.segments):
            for key in [k for k in table if k < cutoff]:
                del table[key]


SUPPORTED = sys.platform == "win32"
