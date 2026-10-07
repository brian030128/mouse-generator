import csv
import sys
import tempfile
import time
import unittest
from pathlib import Path

from mouse_event import MouseEvent
from storage import Database
from targets import Target, TargetLedger, TargetProbe

BOX = Target(10, 20, 30, 40, 50000, 2.5)


class LedgerTests(unittest.TestCase):
    def test_result_after_segment(self):
        ledger = TargetLedger()
        self.assertEqual(ledger.segment_saved(5, 101), [])
        self.assertEqual(ledger.result(5, BOX), [(101, BOX)])
        self.assertEqual((ledger.results, ledger.segments), ({}, {}))

    def test_result_before_segment(self):
        ledger = TargetLedger()
        self.assertEqual(ledger.result(5, BOX), [])
        self.assertEqual(ledger.segment_saved(5, 101), [(101, BOX)])

    def test_failed_lookup_writes_nothing(self):
        ledger = TargetLedger()
        ledger.segment_saved(5, 101)
        self.assertEqual(ledger.result(5, None), [])
        ledger.result(6, None)
        self.assertEqual(ledger.segment_saved(6, 102), [])

    def test_unmatched_entries_expire(self):
        ledger = TargetLedger(keep_ns=100)
        ledger.result(5, BOX)                 # press that ended no saved segment
        ledger.segment_saved(7, 101)          # segment whose lookup was skipped
        ledger.result(500, BOX)
        ledger.expire(now_ns=200)
        self.assertEqual(list(ledger.results), [500])
        self.assertEqual(ledger.segments, {})


class FakeLocator:
    def __init__(self):
        self.closed = False

    def __call__(self, x, y):
        if x < 0:
            raise OSError("element went away")
        return (x, y, 30, 40, 50005)

    def close(self):
        self.closed = True


def drain(probe, count, timeout=2.0):
    out = []
    deadline = time.time() + timeout
    while len(out) < count and time.time() < deadline:
        try:
            out.append(probe.results.get(timeout=0.05))
        except Exception:
            pass
    return out


class ProbeTests(unittest.TestCase):
    def test_lookups_stale_requests_and_errors(self):
        locators = []
        probe = TargetProbe(max_age_ms=500, locator_factory=lambda: locators.append(FakeLocator()) or locators[-1])
        probe.start()
        now = time.perf_counter_ns()
        probe.request(now, 100, 200)
        probe.request(now - 2_000_000_000, 1, 1)          # two seconds old: skipped
        probe.request(now, -5, 0)                          # lookup raises
        results = drain(probe, 3)
        probe.stop()
        self.assertEqual(len(results), 3)
        self.assertEqual(results[0][0], now)
        found = results[0][1]
        self.assertEqual((found.left, found.top, found.width, found.height, found.control_type),
                         (100, 200, 30, 40, 50005))
        self.assertGreaterEqual(found.delay_ms, 0)
        self.assertIsNone(results[1][1])
        self.assertIsNone(results[2][1])
        self.assertTrue(locators[0].closed)

    def test_unavailable_locator_leaves_recording_alone(self):
        def broken():
            raise OSError("UI Automation unavailable")
        probe = TargetProbe(locator_factory=broken)
        probe.start()
        probe.thread.join(1)
        probe.request(time.perf_counter_ns(), 1, 1)        # still accepted, never answered
        probe.stop()
        self.assertEqual(probe.error, "UI Automation unavailable")


class StorageTargetTests(unittest.TestCase):
    def test_target_written_exported_and_old_database_upgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "t.sqlite3"
            database = Database(path)
            session = database.start_session("", 8_000_000_000, (0, 0, 1920, 1080), "win32")
            events = [MouseEvent(0, 1, 2, "move"), MouseEvent(5_000_000, 3, 4, "down", "left")]
            first = database.save_segment(session, 0, events, 1.0)
            second = database.save_segment(session, 0, events, 1.0)
            database.set_target(first, BOX)
            self.assertEqual(database.connection.execute(
                "SELECT id, target_left, target_top, target_width, target_height, "
                "target_control_type, target_delay_ms FROM segments ORDER BY id").fetchall(),
                [(first, 10, 20, 30, 40, 50000, 2.5), (second, None, None, None, None, None, None)])
            output = Path(directory) / "t.csv"
            database.export_csv(output)
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual((rows[0]["target_width"], rows[0]["target_control_type"]), ("30", "50000"))
            self.assertEqual(rows[-1]["target_width"], "")
            # A database from before these columns gains them, rows untouched.
            database.connection.executescript(
                "ALTER TABLE segments DROP COLUMN target_left; ALTER TABLE segments DROP COLUMN target_top;"
                "ALTER TABLE segments DROP COLUMN target_width; ALTER TABLE segments DROP COLUMN target_height;"
                "ALTER TABLE segments DROP COLUMN target_control_type; ALTER TABLE segments DROP COLUMN target_delay_ms;")
            database.close()
            database = Database(path)
            self.assertEqual(database.connection.execute(
                "SELECT COUNT(*), COUNT(target_width) FROM segments").fetchone(), (2, 0))
            database.close()


@unittest.skipUnless(sys.platform == "win32", "UI Automation is Windows-only")
class LiveUiaTests(unittest.TestCase):
    def test_button_box_matches_tk_geometry(self):
        import tkinter as tk
        from capture_windows import configure_desktop
        configure_desktop()
        try:
            root = tk.Tk()
        except tk.TclError as error:
            self.skipTest(f"no display: {error}")
        try:
            root.geometry("+200+200")
            root.attributes("-topmost", True)
            button = tk.Button(root, text="Target probe test", width=24, height=3)
            button.pack(padx=40, pady=40)
            root.update()
            root.lift()
            root.update()
            left, top = button.winfo_rootx(), button.winfo_rooty()
            width, height = button.winfo_width(), button.winfo_height()
            probe = TargetProbe()
            probe.start()
            # The worker queries our own window, so keep Tk pumping messages.
            probe.request(time.perf_counter_ns(), left + width // 2, top + height // 2)
            result = None
            deadline = time.time() + 5
            while result is None and time.time() < deadline:
                root.update()
                try:
                    result = probe.results.get(timeout=0.02)
                except Exception:
                    pass
            probe.stop()
            self.assertIsNone(probe.error)
            self.assertIsNotNone(result, "no lookup result within 5 s")
            target = result[1]
            self.assertIsNotNone(target)
            self.assertEqual(target.control_type, 50000)        # button
            for got, want in ((target.left, left), (target.top, top),
                              (target.width, width), (target.height, height)):
                self.assertLessEqual(abs(got - want), 2)
        finally:
            root.destroy()


if __name__ == "__main__":
    unittest.main()
