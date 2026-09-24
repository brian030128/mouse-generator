"""Selects the desktop mouse capture backend for this platform."""

import sys

from mouse_event import MouseEvent

SUPPORTED = sys.platform in ("win32", "darwin")

if sys.platform == "darwin":
    from capture_macos import MouseCapture, configure_desktop, pixel_scale_at
    # No stop key on macOS: closing the window stops recording, and no keyboard
    # access (Input Monitoring) is requested.
    stop_key_pressed = None
elif sys.platform == "win32":
    from capture_windows import (MouseCapture, configure_desktop, pixel_scale_at,
                                 stop_key_pressed)
