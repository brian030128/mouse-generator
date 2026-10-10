"""Preload generated JSON rows on an ESP32-S3, then play relative USB HID motion.

python replay.py path.json --dry-run
python replay.py path.json --port COM3               # movement only
python replay.py path.json --port COM3 --clicks      # enable generated clicks
"""

import argparse
import binascii
from dataclasses import asdict, dataclass
import json
import math
from pathlib import Path
import sys
import time

MAX_EVENTS = 4096
MAX_DURATION_US = 60_000_000
MIN_GAP_US = 10000
MAX_AXIS = 127


def circle_rows(radius=120.0, turns=3, period=2.0):
    """A visible, click-free hardware diagnostic, starting at the circle's edge."""
    if not math.isfinite(radius) or radius <= 0 or radius > 2000:
        raise ValueError("circle radius must be in (0, 2000] counts before scaling")
    if not isinstance(turns, int) or not 1 <= turns <= 20:
        raise ValueError("circle turns must be an integer in 1..20")
    if not math.isfinite(period) or period < 0.5 or period * turns > 60:
        raise ValueError("circle period must be at least 0.5 s and total duration at most 60 s")
    steps = 100 * turns
    return [[i * period * 1000 / 100,
             round(radius * math.cos(i * 2 * math.pi / 100)),
             round(radius * math.sin(i * 2 * math.pi / 100)), 0]
            for i in range(steps + 1)]


@dataclass(frozen=True)
class Event:
    due_us: int
    dx: int
    dy: int
    buttons: int = 0
    wheel: int = 0


def plan(rows, *, counts_per_pixel=1.0, clicks=False, click_hold_ms=30.0):
    """Convert absolute positions to counts with cumulative rounding.

    The first row is an anchor; it does not place the receiving cursor.
    Crowded events are delayed to fit the advertised 10 ms polling interval.
    Larger deltas are split into bounded reports without losing counts.
    """
    if not math.isfinite(counts_per_pixel) or counts_per_pixel <= 0:
        raise ValueError("counts-per-pixel must be finite and positive")
    if not math.isfinite(click_hold_ms) or click_hold_ms < 1:
        raise ValueError("click-hold-ms must be at least 1")
    if not isinstance(rows, list) or len(rows) < 2 or len(rows) > MAX_EVENTS:
        raise ValueError(f"expected 2..{MAX_EVENTS} [t_ms, x, y, click] rows")
    clean = []
    for i, row in enumerate(rows):
        if not isinstance(row, (list, tuple)) or len(row) != 4:
            raise ValueError(f"row {i}: expected [t_ms, x, y, click]")
        if any(isinstance(v, bool) or not isinstance(v, (float, int)) or
               not math.isfinite(v) for v in row):
            raise ValueError(f"row {i}: values must be finite numbers")
        if row[0] < 0 or (i and row[0] < clean[-1][0]):
            raise ValueError(f"row {i}: timestamps must be nonnegative and ordered")
        if row[3] not in (0, 1):
            raise ValueError(f"row {i}: click must be 0 or 1")
        clean.append(row)
    if clean[0][3]:
        raise ValueError("the anchor row cannot contain a click")
    events = []
    previous = (0, 0)
    shifted = 0
    for t, x, y, click in clean[1:]:
        position = (round((x - clean[0][1]) * counts_per_pixel),
                    round((y - clean[0][2]) * counts_per_pixel))
        dx, dy = position[0] - previous[0], position[1] - previous[1]
        if max(abs(dx), abs(dy)) > 32767:
            raise ValueError("single-row displacement exceeds the planner's 32767-count safety limit")
        requested = round((t - clean[0][0]) * 1000)
        parts = max(1, math.ceil(max(abs(dx), abs(dy)) / MAX_AXIS))
        # Allocate displacement evenly using cumulative rounding, rather than
        # clamping (which loses counts) or appending one long-axis tail.
        allocated_x = allocated_y = 0
        button = int(clicks and click)
        for part in range(1, parts + 1):
            due = max(requested, events[-1].due_us + MIN_GAP_US if events else 0)
            if part == 1:
                shifted += due != requested
            next_x, next_y = round(dx * part / parts), round(dy * part / parts)
            events.append(Event(due, next_x - allocated_x, next_y - allocated_y,
                                button if part == parts else 0))
            allocated_x, allocated_y = next_x, next_y
        previous = position
        if clicks and click:
            events.append(Event(due + max(MIN_GAP_US, round(click_hold_ms * 1000)), 0, 0))
        if len(events) > MAX_EVENTS:
            raise ValueError("plan exceeds firmware limits (4096 events / 60 seconds)")
    if len(events) > MAX_EVENTS or events[-1].due_us > MAX_DURATION_US:
        raise ValueError("plan exceeds firmware limits (4096 events / 60 seconds)")
    return events, shifted


def frame(command):
    payload = command.encode("ascii")
    return payload + f"*{binascii.crc_hqx(payload, 0xffff):04X}\n".encode("ascii")


class Relay:
    def __init__(self, port):
        self.port = port

    def read_line(self, deadline):
        while time.monotonic() < deadline:
            line = self.port.readline().decode("ascii", errors="replace").strip()
            if not line or line.startswith("READY "):
                continue
            if line.startswith("ERR "):
                raise RuntimeError(f"board stopped: {line}")
            return line
        raise TimeoutError("no board acknowledgement; check UART, firmware and USB connection")

    def request(self, command, expected, timeout=2.0):
        self.port.write(frame(command))
        reply = self.read_line(time.monotonic() + timeout)
        if reply != expected:
            raise RuntimeError(f"expected {expected!r}, received {reply!r}")

    def probe(self):
        self.port.reset_input_buffer()
        self.port.write(b"\nSTOP\n")
        self.request_stop_reply()
        self.port.reset_input_buffer()
        self.port.write(frame("HELLO"))
        return self.read_line(time.monotonic() + 3)

    def request_stop_reply(self):
        reply = self.read_line(time.monotonic() + 3)
        if reply != "OK STOP":
            raise RuntimeError(f"expected stop acknowledgement, received {reply!r}")

    def identity(self):
        self.port.write(frame("IDENTITY"))
        reply = self.read_line(time.monotonic() + 3)
        if not reply.startswith("OK IDENTITY "):
            raise RuntimeError(f"unexpected identity response: {reply}")
        return reply

    def play(self, events):
        try:
            # STOP and flush stale replies before the protocol handshake.
            hello = self.probe()
            if hello != "OK HELLO 2 4096 10000 60000000 1500 USB=1":
                raise RuntimeError(f"incompatible firmware or target USB unavailable: {hello}")
            self.request(f"LOAD {len(events)}", "OK LOAD")
            for i, e in enumerate(events):
                self.request(f"E {i} {e.due_us} {e.dx} {e.dy} {e.buttons} {e.wheel}", f"OK E {i}")
            self.request("RUN", "OK RUN")
            deadline = time.monotonic() + events[-1].due_us / 1_000_000 + 5
            while time.monotonic() < deadline:
                self.port.write(frame("PING"))
                reply = self.read_line(min(deadline, time.monotonic() + 1))
                if reply.startswith("DONE "):
                    parts = reply.split()
                    if len(parts) != 3 or int(parts[1]) != len(events):
                        raise RuntimeError(f"invalid completion response: {reply}")
                    return {"reports": int(parts[1]), "max_dispatch_lateness_us": int(parts[2])}
                if reply != "OK PING":
                    raise RuntimeError(f"unexpected board response: {reply}")
                # Short reads also catch DONE while waiting for the next heartbeat.
                until = min(deadline, time.monotonic() + 0.25)
                while time.monotonic() < until:
                    if self.port.in_waiting:
                        reply = self.read_line(deadline)
                        if reply.startswith("DONE "):
                            parts = reply.split()
                            if len(parts) != 3 or int(parts[1]) != len(events):
                                raise RuntimeError(f"invalid completion response: {reply}")
                            return {"reports": int(parts[1]), "max_dispatch_lateness_us": int(parts[2])}
                        raise RuntimeError(f"unexpected board response: {reply}")
                    time.sleep(0.01)
            raise TimeoutError("playback did not finish in time")
        finally:
            # Independent of successful completion, errors, or Ctrl+C. Firmware's
            # watchdog handles cases where the serial connection has disappeared.
            try:
                self.port.write(b"\nSTOP\n")
            except OSError:
                pass


def load_rows(path):
    raw = sys.stdin.buffer.read() if path == "-" else Path(path).read_bytes()
    # Windows PowerShell 5 redirection may produce UTF-16 JSON.
    encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
    return json.loads(raw.decode(encoding))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", help="generated JSON file, or - for stdin")
    parser.add_argument("--port", help="controller UART port, e.g. COM3 or /dev/ttyUSB0")
    parser.add_argument("--list-ports", action="store_true")
    parser.add_argument("--probe", action="store_true", help="stop the board and query firmware/USB readiness; no motion")
    parser.add_argument("--identity", action="store_true", help="with --probe, print configured USB identity over UART")
    parser.add_argument("--circle", action="store_true", help="send a visible click-free circle diagnostic instead of a JSON path")
    parser.add_argument("--radius", type=float, default=120, help="circle radius in counts before scaling")
    parser.add_argument("--turns", type=int, default=3)
    parser.add_argument("--period", type=float, default=2, help="seconds per circle")
    parser.add_argument("--dry-run", action="store_true", help="print HID plan without opening any port")
    parser.add_argument("--clicks", action="store_true", help="enable generated left-clicks")
    parser.add_argument("--counts-per-pixel", type=float, default=1.0)
    parser.add_argument("--click-hold-ms", type=float, default=30)
    args = parser.parse_args()
    try:
        if args.list_ports:
            from serial.tools.list_ports import comports
            for port in comports():
                print(f"{port.device}: {port.description} ({port.hwid})")
            return
        if (not args.probe and not args.circle and not args.path) or (not args.dry_run and not args.port):
            parser.error("provide a JSON path and --port (or --dry-run)")
        if args.circle and (args.path or args.probe or args.clicks):
            parser.error("--circle cannot be combined with a path, --probe or --clicks")
        if args.probe and args.dry_run:
            parser.error("--probe requires a real serial port and cannot use --dry-run")
        if args.identity and not args.probe:
            parser.error("--identity requires --probe")
        if not args.probe:
            rows = circle_rows(args.radius, args.turns, args.period) if args.circle else load_rows(args.path)
            events, shifted = plan(rows, counts_per_pixel=args.counts_per_pixel,
                                   clicks=args.clicks, click_hold_ms=args.click_hold_ms)
        if args.dry_run:
            print(json.dumps({"events": [asdict(e) for e in events], "shifted_rows": shifted}, indent=2))
            return
        import serial
        if not args.probe:
            print(f"Preloading {len(events)} reports; {shifted} rows delayed for USB/click spacing. "
                  f"Clicks {'enabled' if args.clicks else 'disabled'}.")
        # Prevent opening a dev board's UART bridge from asserting reset/boot pins.
        port = serial.Serial(port=None, baudrate=115200, timeout=0.05, write_timeout=2)
        port.dtr = port.rts = False
        port.port = args.port
        with port:
            relay = Relay(port)
            print(relay.probe() if args.probe else json.dumps(relay.play(events)))
            if args.identity:
                print(relay.identity())
    except ImportError:
        parser.exit(2, "Install hardware dependencies: python -m pip install -r requirements-hardware.txt\n")
    except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
        parser.exit(2, f"Replay failed: {exc}\n")
    except KeyboardInterrupt:
        parser.exit(130, "Playback stopped.\n")


if __name__ == "__main__":
    main()
