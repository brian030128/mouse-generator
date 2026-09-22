"""Run with `python recorder.py`; Windows, Python 3.10+, no dependencies."""

import argparse
from pathlib import Path
import queue
import sys
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from capture import MouseCapture, configure_desktop, stop_key_pressed
from segments import MAX_DURATION_NS, SegmentCollector
from storage import Database


class RecorderApp:
    def __init__(self, root, database, path, screen):
        self.root = root
        self.database = database
        self.screen = screen
        self.capture = None
        self.collector = None
        self.session_id = None
        root.title("Mouse dataset recorder")
        root.geometry("680x360")
        root.minsize(680, 360)
        root.protocol("WM_DELETE_WINDOW", self.close)
        panel = ttk.Frame(root, padding=20)
        panel.pack(fill="both", expand=True)
        ttk.Label(panel, text="Mouse dataset recorder", font=("Segoe UI", 17)).pack(anchor="w")
        ttk.Label(panel, text="Records desktop mouse events locally while enabled.").pack(anchor="w", pady=(5, 12))
        row = ttk.Frame(panel)
        row.pack(fill="x")
        ttk.Label(row, text="Session label:").pack(side="left")
        self.label = ttk.Entry(row)
        self.label.pack(side="left", fill="x", expand=True, padx=(10, 0))
        ttk.Label(panel, text="First cursor movement starts a segment; the next left-click ends it.\n"
                  "Over 8 seconds: keep the last 1.5 seconds. Unfinished intervals are discarded.").pack(anchor="w", pady=12)
        controls = ttk.Frame(panel)
        controls.pack(fill="x")
        self.start_button = ttk.Button(controls, text="Start", command=self.begin)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(controls, text="Stop (F8)", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        self.export_button = ttk.Button(controls, text="Export CSV", command=self.export)
        self.export_button.pack(side="left")
        self.status = tk.StringVar(value="Idle — recording is off")
        ttk.Label(panel, textvariable=self.status).pack(anchor="w", pady=(16, 5))
        self.counts = tk.StringVar(value="Saved: 0    Discarded: 0")
        ttk.Label(panel, textvariable=self.counts).pack(anchor="w")
        self.total_segments = self.database.count_segments()
        self.total = tk.StringVar(value=f"Total saved segments (all sessions): {self.total_segments:,}")
        ttk.Label(panel, textvariable=self.total).pack(anchor="w", pady=(5, 0))
        ttk.Label(panel, text=f"Database: {path}", wraplength=590).pack(anchor="w", pady=(12, 0))
        root.after(20, self.tick)

    def begin(self):
        if self.capture is not None:
            return
        self.start_button.configure(state="disabled")
        self.export_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        try:
            self.total_segments = self.database.count_segments()
            self.update_total()
            self.recording_start_ns = time.perf_counter_ns()
            self.session_id = self.database.start_session(
                self.label.get().strip(), MAX_DURATION_NS, self.screen)
            self.collector = SegmentCollector(self.save)
            self.update_counts()
            self.capture = MouseCapture()
            self.capture.start()
            self.status.set("Recording — press F8 anywhere to stop")
        except Exception as error:
            self.stop()
            messagebox.showerror("Cannot start recording", str(error))

    def save(self, events):
        self.database.save_segment(self.session_id, self.recording_start_ns, events)
        self.total_segments += 1
        self.update_total()

    def update_total(self):
        self.total.set(f"Total saved segments (all sessions): {self.total_segments:,}")

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
            if self.capture and stop_key_pressed():
                self.stop()
            if self.capture:
                if self.capture.error or not self.capture.thread.is_alive():
                    raise RuntimeError(self.capture.error or "Mouse capture stopped unexpectedly.")
                self.drain(limit=2000)
                self.update_counts()
        except Exception as error:
            self.stop(flush=False)
            messagebox.showerror("Recording stopped", str(error))
        finally:
            self.root.after(20, self.tick)

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
        self.status.set("Stopped — recording is off")
        self.start_button.configure(state="normal")
        self.export_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        if errors:
            messagebox.showerror("Recording error", "\n".join(errors))

    def export(self):
        filename = filedialog.asksaveasfilename(
            defaultextension=".csv", filetypes=[("CSV dataset", "*.csv")],
            initialfile="mouse_dataset.csv")
        if filename:
            try:
                self.database.export_csv(filename)
                self.status.set("Dataset exported")
            except Exception as error:
                messagebox.showerror("Export failed", str(error))

    def close(self):
        self.stop()
        self.database.close()
        self.root.destroy()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db", type=Path,
                        default=Path(__file__).resolve().parent / "data" / "mouse.sqlite3")
    parser.add_argument("--export", type=Path, metavar="CSV", help="Export the database and exit")
    parser.add_argument("--start", action="store_true", help="Start recording immediately on launch")
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
    if sys.platform != "win32":
        parser.error("Desktop recording requires Windows")
    screen = configure_desktop()
    database = Database(args.db)
    root = tk.Tk()
    app = RecorderApp(root, database, args.db.resolve(), screen)
    if args.start:
        app.begin()
    root.mainloop()


if __name__ == "__main__":
    main()
