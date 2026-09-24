"""The mouse event record shared by capture backends, segmentation and storage."""

from dataclasses import dataclass


@dataclass(frozen=True)
class MouseEvent:
    timestamp_ns: int
    x: int
    y: int
    kind: str
    button: str = ""
    wheel_delta: int = 0
