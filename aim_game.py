"""Aim-game recorder: click random dots in a 350x350 window; hits are saved, slow ones trimmed.

Run with `python aim_game.py`; Windows or macOS, Python 3.10+, no dependencies.
Saves to its own database (data/aim.sqlite3), separate from the everyday recorder.
"""

import argparse
from dataclasses import dataclass
from pathlib import Path
import queue
import random
import sys
import time
import tkinter as tk
from tkinter import messagebox

from capture import SUPPORTED, MouseCapture, configure_desktop, pixel_scale_at
from storage import Database

SIZE = 350
RADIUS = 15
MAX_DURATION_NS = 200_000_000
TRIM_WINDOW_NS = (150_000_000, 250_000_000)
MIN_JUMP = 60
MACOS = sys.platform == "darwin"
FONT = ("Helvetica Neue", 13) if MACOS else ("Segoe UI", 10)


@dataclass(frozen=True)
class Target:
    x: int  # dot centre, screen coordinates
    y: int
    radius: int
    spawn_ns: int
    area: tuple  # play area on screen: left, top, right, bottom

    def contains(self, x, y):
        return (x - self.x) ** 2 + (y - self.y) ** 2 <= self.radius ** 2

    def in_area(self, x, y):
        left, top, right, bottom = self.area
        return left <= x < right and top <= y < bottom


class TrialCollector:
    """One attempt per dot: first movement after the dot appears, to the next left-click.

    The attempt is saved only when the click lands on the dot. A hit taking
    max_duration_ns or longer from movement onset to click keeps only the last
    150-250 ms (drawn uniformly) before the click, starting at the first movement
    in that window. Clicks outside the play area discard the movement but keep
    the same dot.
    """

    def __init__(self, save, max_duration_ns=MAX_DURATION_NS, trim_window_ns=TRIM_WINDOW_NS,
                 rng=None):
        self.save = save
        self.max_duration_ns = max_duration_ns
        self.trim_window_ns = trim_window_ns
        self.rng = rng or random.Random()
        self.target = None
        self.events = []
        self.counts = {"saved": 0, "trimmed": 0, "slow": 0, "missed": 0, "outside": 0}
        self.last_duration_ns = None

    def arm(self, target):
        self.target = target
        self.events = []

    def feed(self, event):
        """Returns the attempt's outcome when a left-click ends it, else None."""
        target = self.target
        # Events delivered before the dot was drawn belong to the previous attempt.
        if target is None or event.timestamp_ns < target.spawn_ns:
            return None
        if not self.events and event.kind != "down":
            if event.kind == "move":
                self.events.append(event)
            return None
        if not (event.kind == "down" and event.button == "left"):
            if self.events:
                self.events.append(event)
            return None
        if not target.in_area(event.x, event.y):
            outcome = "outside"
        elif not target.contains(event.x, event.y) or not self.events:
            outcome = "missed"
        else:
            self.events.append(event)
            self.last_duration_ns = event.timestamp_ns - self.events[0].timestamp_ns
            if self.last_duration_ns < self.max_duration_ns:
                self.save(list(self.events), target, self.last_duration_ns)
                outcome = "saved"
            else:
                cutoff = event.timestamp_ns - self.rng.randint(*self.trim_window_ns)
                kept = [e for e in self.events if e.timestamp_ns >= cutoff]
                # A trajectory starts at movement, as in the everyday recorder.
                while kept and kept[0].kind != "move":
                    kept.pop(0)
                if kept:
                    self.save(kept, target, self.last_duration_ns)
                    outcome = "trimmed"
                else:
                    outcome = "slow"
        self.counts[outcome] += 1
        self.events = []
        if outcome != "outside":
            self.target = None
        return outcome


class AimDatabase(Database):
    """The recorder's schema plus the dot each saved segment was aimed at."""

    def __init__(self, path):
        super().__init__(path)
        self.connection.execute("""
            CREATE TABLE IF NOT EXISTS targets (
                segment_id INTEGER PRIMARY KEY REFERENCES segments(id),
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                radius INTEGER NOT NULL,
                spawn_offset_ns INTEGER NOT NULL
            )
        """)
        # Added with trimming; rows saved before it stay NULL (they were never trimmed).
        self._add_column("targets", "full_duration_ns", "INTEGER")

    def save_trial(self, session_id, recording_start_ns, events, target,
                   full_duration_ns=None, pixel_scale=None):
        segment_id = self.save_segment(session_id, recording_start_ns, events, pixel_scale)
        with self.connection:
            self.connection.execute(
                "INSERT INTO targets(segment_id, x, y, radius, spawn_offset_ns, full_duration_ns) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (segment_id, target.x, target.y, target.radius,
                 target.spawn_ns - events[0].timestamp_ns, full_duration_ns))
        return segment_id


class AimGame:
    def __init__(self, root, database, path, screen, radius=RADIUS,
                 max_duration_ns=MAX_DURATION_NS):
        self.root = root
        self.database = database
        self.screen = screen
        self.radius = radius
        self.max_duration_ns = max_duration_ns
        self.capture = None
        self.session_id = None
        self.dot = None
        self.centre = None
        self.collector = TrialCollector(self.save, max_duration_ns)
        root.title("Aim Recorder")
        root.resizable(False, False)
        root.protocol("WM_DELETE_WINDOW", self.close)
        self.canvas = tk.Canvas(root, width=SIZE, height=SIZE, bg="#1e1f24",
                                highlightthickness=0, cursor="crosshair")
        self.canvas.pack()
        root.bind("<Configure>", self.moved)
        self.result = tk.StringVar(value=f"Click each dot; hits over {max_duration_ns // 1_000_000} ms are trimmed")
        self.result_label = tk.Label(root, textvariable=self.result, font=FONT, anchor="w", padx=8,
                                     wraplength=SIZE - 24, justify="left")
        self.result_label.pack(fill="x")
        self.counts = tk.StringVar()
        tk.Label(root, textvariable=self.counts, font=FONT, anchor="w", padx=8,
                 wraplength=SIZE - 24, justify="left").pack(fill="x")
        tk.Label(root, text=f"Database: {path}", font=FONT, anchor="w", padx=8,
                 fg="gray", wraplength=SIZE - 24, justify="left").pack(fill="x", pady=(0, 6))
        self.total_segments = database.count_segments()
        self.update_counts()
        self.tick_id = root.after(10, self.tick)

    def begin(self):
        try:
            self.recording_start_ns = time.perf_counter_ns()
            self.session_id = self.database.start_session(
                "aim", self.max_duration_ns, self.screen, sys.platform)
            self.capture = MouseCapture()
            self.capture.start()
            self.root.update()
            self.spawn()
        except Exception as error:
            self.stop()
            messagebox.showerror("Cannot start recording", str(error))

    def spawn(self):
        margin = self.radius + 5
        for _ in range(100):
            centre = (random.randint(margin, SIZE - margin), random.randint(margin, SIZE - margin))
            if self.centre is None or (centre[0] - self.centre[0]) ** 2 + \
                    (centre[1] - self.centre[1]) ** 2 >= MIN_JUMP ** 2:
                break
        self.centre = centre
        if self.dot is not None:
            self.canvas.delete(self.dot)
        r = self.radius
        self.dot = self.canvas.create_oval(centre[0] - r, centre[1] - r, centre[0] + r,
                                           centre[1] + r, fill="#f2b134", outline="")
        self.root.update_idletasks()
        self.collector.arm(self.locate(time.perf_counter_ns()))

    def locate(self, spawn_ns):
        # Hook and Tk coordinates match: both are physical pixels on Windows
        # (per-monitor DPI aware) and points on macOS.
        left, top = self.canvas.winfo_rootx(), self.canvas.winfo_rooty()
        return Target(left + self.centre[0], top + self.centre[1], self.radius, spawn_ns,
                      (left, top, left + SIZE, top + SIZE))

    def moved(self, _event):
        if self.collector.target is not None:
            self.collector.target = self.locate(self.collector.target.spawn_ns)

    def save(self, events, target, full_duration_ns):
        first = events[0]
        self.database.save_trial(self.session_id, self.recording_start_ns, events, target,
                                 full_duration_ns, pixel_scale_at(first.x, first.y))
        self.total_segments += 1

    def report(self, outcome):
        ms = (self.collector.last_duration_ns or 0) / 1e6
        text, colour = {
            "saved": (f"{ms:.0f} ms — saved", "#2e9d4f"),
            "trimmed": (f"{ms:.0f} ms — saved the last 150–250 ms", "#b7791f"),
            "slow": (f"{ms:.0f} ms — no movement near the click, not saved", "#c0392b"),
            "missed": ("Missed — not saved", "#c0392b"),
            "outside": ("Clicked outside the game — same dot", "gray"),
        }[outcome]
        self.result.set(text)
        self.result_label.configure(fg=colour)
        self.update_counts()

    def update_counts(self):
        c = self.collector.counts
        self.counts.set(f"Saved {c['saved']}    Trimmed {c['trimmed']}    Missed {c['missed']}"
                        f"    Total saved: {self.total_segments:,}")

    def tick(self):
        try:
            if self.capture:
                if self.capture.error or not self.capture.thread.is_alive():
                    raise RuntimeError(self.capture.error or "Mouse capture stopped unexpectedly.")
                for _ in range(2000):
                    try:
                        event = self.capture.events.get_nowait()
                    except queue.Empty:
                        break
                    outcome = self.collector.feed(event)
                    if outcome:
                        self.report(outcome)
                        if outcome != "outside":
                            self.spawn()
        except Exception as error:
            self.stop()
            messagebox.showerror("Recording stopped", str(error))
        finally:
            self.tick_id = self.root.after(10, self.tick)

    def stop(self):
        errors = []
        if self.capture:
            try:
                self.capture.stop()
            except Exception as error:
                errors.append(str(error))
            self.capture = None
        if self.session_id is not None:
            try:
                self.database.finish_session(self.session_id)
            except Exception as error:
                errors.append(str(error))
            self.session_id = None
        if errors:
            messagebox.showerror("Recording error", "\n".join(errors))

    def close(self):
        self.root.after_cancel(self.tick_id)
        self.stop()
        self.database.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path,
                        default=Path(__file__).resolve().parent / "data" / "aim.sqlite3")
    parser.add_argument("--max-ms", type=int, default=MAX_DURATION_NS // 1_000_000,
                        help="Trim hits that take this long or longer (default 200)")
    parser.add_argument("--radius", type=int, default=RADIUS, help="Dot radius in pixels")
    parser.add_argument("--export", type=Path, metavar="CSV", help="Export the database and exit")
    args = parser.parse_args()
    if args.export:
        if args.export.resolve() == args.db.resolve():
            parser.error("CSV output must differ from the database path")
        database = AimDatabase(args.db)
        try:
            database.export_csv(args.export)
        finally:
            database.close()
        return
    if not SUPPORTED:
        parser.error("Desktop recording requires Windows or macOS")
    screen = configure_desktop()
    database = AimDatabase(args.db)
    root = tk.Tk()
    game = AimGame(root, database, args.db.resolve(), screen, args.radius,
                   args.max_ms * 1_000_000)
    game.begin()
    root.mainloop()


if __name__ == "__main__":
    main()
