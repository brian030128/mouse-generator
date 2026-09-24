"""Selects the desktop mouse capture backend for this platform."""

import sys

from mouse_event import MouseEvent

SUPPORTED = sys.platform in ("win32", "darwin")

if sys.platform == "darwin":
    from capture_macos import (MouseCapture, configure_desktop, pixel_scale_at,
                               stop_key_pressed)
elif sys.platform == "win32":
    from capture_windows import (MouseCapture, configure_desktop, pixel_scale_at,
                                 stop_key_pressed)
