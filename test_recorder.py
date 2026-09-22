import csv
import tempfile
import unittest
from pathlib import Path

from capture import MouseEvent
from segments import SegmentCollector
from storage import Database


def event(seconds, kind="move", button=""):
    return MouseEvent(round(seconds * 1_000_000_000), -100, 250, kind, button)


def click(seconds):
    return event(seconds, "down", "left")


class SegmentTests(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.collector = SegmentCollector(lambda events: self.saved.append(list(events)))

    def test_click_without_movement_and_incomplete_tail_discarded(self):
        for item in [click(0), click(1), event(2), event(3)]:
            self.collector.feed(item)
        self.collector.finish()
        self.assertEqual(self.saved, [])
        self.assertEqual(self.collector.discarded, 1)

    def test_exactly_five_seconds_is_kept(self):
        for item in [event(1), event(2), click(6)]:
            self.collector.feed(item)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.saved[0]), 3)

    def test_over_five_seconds_dropped_and_next_interval_recovers(self):
        for item in [event(0), event(5.000000001), click(6), event(7), click(8)]:
            self.collector.feed(item)
        self.assertEqual(self.collector.discarded, 1)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(self.saved[0][0], event(7))
        self.assertEqual(self.saved[0][-1], click(8))

    def test_long_idle_without_moves_is_dropped(self):
        self.collector.feed(event(0))
        self.collector.feed(click(10))
        self.assertEqual(self.collector.discarded, 1)
        self.assertEqual(self.saved, [])

    def test_right_click_is_event_not_boundary(self):
        for item in [event(0), event(1, "down", "right"), click(2), event(2.5), click(3)]:
            self.collector.feed(item)
        self.assertEqual([len(items) for items in self.saved], [3, 2])
        self.assertEqual(self.saved[1][0], event(2.5))

    def test_expired_buffer_is_released(self):
        self.collector.feed(event(0))
        for second in range(1, 100):
            self.collector.feed(event(second))
        self.assertEqual(self.collector.events, [])

    def test_initial_movement_saved_and_click_does_not_start_next_segment(self):
        for item in [event(0), click(1), click(1.1), event(2), click(3)]:
            self.collector.feed(item)
        self.collector.finish()
        self.assertEqual(self.saved, [[event(0), click(1)], [event(2), click(3)]])
        self.assertEqual(self.collector.discarded, 0)


class StorageTests(unittest.TestCase):
    def test_saved_segment_survives_reopen_and_exports_relative_time(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "dataset.sqlite3"
            database = Database(path)
            session = database.start_session("test", 5_000_000_000, (-1920, 0, 3840, 1080))
            collector = SegmentCollector(lambda items: database.save_segment(session, 0, items))
            for item in [click(0), event(1), event(2), click(3), event(3.5), click(9)]:
                collector.feed(item)
            collector.finish()
            database.finish_session(session)
            database.close()
            database = Database(path)
            with self.assertRaises(ValueError):
                database.export_csv(path)
            self.assertEqual(database.connection.execute(
                "SELECT duration_ns, event_count FROM segments").fetchall(), [(2_000_000_000, 3)])
            output = Path(directory) / "dataset.csv"
            database.export_csv(output)
            with output.open(newline="", encoding="utf-8") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual([int(row["t_ns"]) for row in rows], [0, 1_000_000_000, 2_000_000_000])
            self.assertEqual(rows[0]["x"], "-100")
            self.assertIsNotNone(database.connection.execute("SELECT ended_utc FROM sessions").fetchone()[0])
            database.close()


if __name__ == "__main__":
    unittest.main()
