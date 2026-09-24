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

    def test_exactly_eight_seconds_is_kept(self):
        for item in [event(1), event(2), click(9)]:
            self.collector.feed(item)
        self.assertEqual(len(self.saved), 1)
        self.assertEqual(len(self.saved[0]), 3)

    def test_long_segment_keeps_tail_and_next_interval_recovers(self):
        for item in [event(0), event(8), event(8.5), event(9), click(10), event(11), click(12)]:
            self.collector.feed(item)
        self.assertEqual(self.collector.discarded, 0)
        self.assertEqual(self.saved, [[event(8.5), event(9), click(10)], [event(11), click(12)]])

    def test_just_over_eight_seconds_trims(self):
        for item in [event(0), event(6), event(7), click(8.000000001)]:
            self.collector.feed(item)
        self.assertEqual(self.saved, [[event(7), click(8.000000001)]])

    def test_tail_starts_at_movement_not_button(self):
        for item in [event(0), event(8.5, "up", "left"), event(9), click(10)]:
            self.collector.feed(item)
        self.assertEqual(self.saved, [[event(9), click(10)]])

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

    def test_long_buffer_only_keeps_recent_events(self):
        self.collector.feed(event(0))
        for second in range(1, 100):
            self.collector.feed(event(second))
        self.assertEqual(list(self.collector.events), [event(98), event(99)])

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
            self.assertEqual(database.count_segments(), 0)
            session = database.start_session("test", 8_000_000_000, (-1920, 0, 3840, 1080),
                                              "darwin")
            collector = SegmentCollector(
                lambda items: database.save_segment(session, 0, items, 2.0))
            for item in [click(0), event(1), event(2), click(3), event(3.5), click(12)]:
                collector.feed(item)
            collector.finish()
            database.finish_session(session)
            database.close()
            database = Database(path)
            self.assertEqual(database.count_segments(), 1)
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
            self.assertEqual((rows[0]["platform"], rows[0]["pixel_scale"]), ("darwin", "2.0"))
            self.assertIsNotNone(database.connection.execute("SELECT ended_utc FROM sessions").fetchone()[0])
            database.close()

    def test_database_from_before_platform_columns_is_upgraded(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "old.sqlite3"
            database = Database(path)
            session = database.start_session("", 8_000_000_000, (0, 0, 1920, 1080))
            database.save_segment(session, 0, [event(0), click(1)])
            # Recreate the original schema: same rows, without the added columns.
            database.connection.executescript("""
                ALTER TABLE sessions DROP COLUMN platform;
                ALTER TABLE segments DROP COLUMN pixel_scale;
            """)
            database.close()
            database = Database(path)
            self.assertEqual(database.connection.execute(
                "SELECT ss.platform, s.pixel_scale FROM segments s "
                "JOIN sessions ss ON ss.id = s.session_id").fetchall(), [(None, None)])
            session = database.start_session("", 8_000_000_000, (0, 0, 1920, 1080), "win32")
            database.save_segment(session, 0, [event(0), click(1)], 1.0)
            self.assertEqual(database.count_segments(), 2)
            database.close()


if __name__ == "__main__":
    unittest.main()
