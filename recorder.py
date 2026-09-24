"""Run with `python recorder.py`; Windows or macOS, Python 3.10+, no dependencies."""

import argparse
from pathlib import Path
import queue
import sys
import time
import tkinter as tk
from tkinter import messagebox, ttk

from capture import SUPPORTED, MouseCapture, configure_desktop, pixel_scale_at
from segments import MAX_DURATION_NS, SegmentCollector
from storage import Database

MACOS = sys.platform == "darwin"
SYSTEM_NAME = "macOS" if MACOS else "Windows"
TITLE_FONT = ("Helvetica Neue", 20) if MACOS else ("Segoe UI", 17)


class RecorderApp:
    def __init__(self, root, database, path, screen, startup=None):
        self.root = root
        self.database = database
        self.screen = screen
        self.capture = None
        self.collector = None
        self.session_id = None
        if startup is None:
            from startup import StartupRegistration
            startup = StartupRegistration(__file__, path)
        self.startup = startup
        root.title("Recorder")
        root.geometry("680x320")
        root.minsize(680, 320)
        root.protocol("WM_DELETE_WINDOW", self.close)
        panel = ttk.Frame(root, padding=20)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="Recorder", font=TITLE_FONT).pack(anchor="w")
        ttk.Label(panel, text="Recording starts automatically. Close the window to stop.").pack(anchor="w", pady=(5, 12))
        ttk.Label(panel, text="First cursor movement starts a segment; the next left-click ends it.\n"
                  "Over 8 seconds: keep the last 1.5 seconds. Unfinished intervals are discarded.").pack(anchor="w", pady=12)
        self.autostart = tk.BooleanVar(value=False)
        self.autostart_button = ttk.Checkbutton(
            panel, text=f"Start recording when I sign in to {SYSTEM_NAME}",
            variable=self.autostart, command=self.toggle_autostart)
        self.autostart_button.pack(anchor="w", pady=(12, 0))
        try:
            self.autostart.set(self.startup.is_enabled())
        except OSError as error:
            self.autostart_button.configure(state="disabled")
            messagebox.showerror(f"Cannot read {SYSTEM_NAME} startup setting", str(error))
        self.status = tk.StringVar(value="Idle — recording is off")
        ttk.Label(panel, textvariable=self.status).pack(anchor="w", pady=(16, 5))
        self.counts = tk.StringVar(value="Saved: 0    Discarded: 0")
        ttk.Label(panel, textvariable=self.counts).pack(anchor="w")
        self.total_segments = self.database.count_segments()
        self.total = tk.StringVar(value=f"Total saved segments: {self.total_segments:,}")
        ttk.Label(panel, textvariable=self.total).pack(anchor="w", pady=(5, 0))
        ttk.Label(panel, text=f"Database: {path}", wraplength=590).pack(anchor="w", pady=(12, 0))
        self.tick_id = root.after(20, self.tick)

    def toggle_autostart(self):
        requested = self.autostart.get()
        try:
            self.startup.set_enabled(requested)
            self.autostart.set(self.startup.is_enabled())
        except OSError as error:
            self.autostart.set(not requested)
            message = str(error)
            fix_command = getattr(error, "fix_command", None)
            if fix_command:
                # Dialog text cannot be selected on macOS, so offer the command to paste.
                self.root.clipboard_clear()
                self.root.clipboard_append(fix_command)
                message += "\n\nThis command has been copied to the clipboard."
            messagebox.showerror(f"Cannot change {SYSTEM_NAME} startup setting", message)

    def begin(self):
        if self.capture is not None:
            return
        try:
            self.total_segments = self.database.count_segments()
            self.update_total()
            self.recording_start_ns = time.perf_counter_ns()
            self.session_id = self.database.start_session(
                "", MAX_DURATION_NS, self.screen, sys.platform)
            self.collector = SegmentCollector(self.save)
            self.update_counts()
            self.capture = MouseCapture()
            self.capture.start()
            self.status.set("Recording — close the window to stop")
        except Exception as error:
            self.stop()
            messagebox.showerror("Cannot start recording", str(error))

    def save(self, events):
        first = events[0]
        self.database.save_segment(self.session_id, self.recording_start_ns, events,
                                   pixel_scale_at(first.x, first.y))
        self.total_segments += 1
        self.update_total()

    def update_total(self):
        self.total.set(f"Total saved segments: {self.total_segments:,}")

    def drain(self, before_ns=None, limit=None):
        processed = 0
        while self.capture and (limit is None or processed < limit):
            try:
                event = self.capture.events.get_nowait()
            except queue.Empty:
                break
            if before_ns is None or event.timestamp_ns < before_ns:
                self.collector.feed(event)
            processed += 1

    def tick(self):
        try:
            if self.capture:
                if self.capture.error or not self.capture.thread.is_alive():
                    raise RuntimeError(self.capture.error or "Mouse capture stopped unexpectedly.")
                self.drain(limit=2000)
                self.update_counts()
        except Exception as error:
            self.stop(flush=False)
            messagebox.showerror("Recording stopped", str(error))
        finally:
            self.tick_id = self.root.after(20, self.tick)

    def update_counts(self):
        if self.collector:
            self.counts.set(f"Saved: {self.collector.saved}    Discarded: {self.collector.discarded}")

    def stop(self, flush=True):
        cutoff_ns = time.perf_counter_ns()
        errors = []
        if self.capture:
            try:
                self.capture.stop()
                if flush:
                    self.drain(before_ns=cutoff_ns)
            except Exception as error:
                errors.append(str(error))
            finally:
                self.capture = None
        if self.collector:
            self.collector.finish()
            self.update_counts()
        if self.session_id is not None:
            try:
                self.database.finish_session(self.session_id)
            except Exception as error:
                errors.append(str(error))
            self.session_id = None
        self.status.set("Stopped — reopen Recorder to record again")
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
                        default=Path(__file__).resolve().parent / "data" / "mouse.sqlite3")
    parser.add_argument("--export", type=Path, metavar="CSV", help="Export the database and exit")
    # Accept the old launcher/startup flag; recording now starts on every GUI launch.
    parser.add_argument("--start", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.export:
        if args.export.resolve() == args.db.resolve():
            parser.error("CSV output must differ from the database path")
        database = Database(args.db)
        try:
            database.export_csv(args.export)
        finally:
            database.close()
        return
    if not SUPPORTED:
        parser.error("Desktop recording requires Windows or macOS")
    screen = configure_desktop()
    database = Database(args.db)
    root = tk.Tk()
    app = RecorderApp(root, database, args.db.resolve(), screen)
    app.begin()
    root.mainloop()


if __name__ == "__main__":
    main()
