import tempfile
import unittest
from pathlib import Path

from aim_game import AimDatabase, Target, TrialCollector
from mouse_event import MouseEvent

MS = 1_000_000
AREA = (100, 100, 600, 600)


def move(ms, x=150, y=150):
    return MouseEvent(round(ms * MS), x, y, "move")


def click(ms, x=300, y=300, button="left"):
    return MouseEvent(round(ms * MS), x, y, "down", button)


def target(spawn_ms=0):
    return Target(300, 300, 15, round(spawn_ms * MS), AREA)


class TrialTests(unittest.TestCase):
    def setUp(self):
        self.saved = []
        self.collector = TrialCollector(lambda events, t: self.saved.append((events, t)))
        self.collector.arm(target())

    def feed(self, *events):
        return [self.collector.feed(e) for e in events]

    def test_fast_hit_is_saved_from_first_move_to_click(self):
        outcomes = self.feed(move(10), move(50, 250, 260), click(209))
        self.assertEqual(outcomes, [None, None, "saved"])
        self.assertEqual(self.saved[0][0], [move(10), move(50, 250, 260), click(209)])
        self.assertIsNone(self.collector.target)

    def test_200_ms_or_more_is_too_slow(self):
        self.assertEqual(self.feed(move(10), click(210))[-1], "slow")
        self.assertEqual(self.collector.last_duration_ns, 200 * MS)
        self.assertEqual(self.saved, [])

    def test_click_off_the_dot_is_a_miss(self):
        self.assertEqual(self.feed(move(10), click(50, 320, 300))[-1], "missed")
        self.assertEqual(self.saved, [])
        self.assertEqual(self.collector.counts["missed"], 1)

    def test_hit_without_movement_is_not_saved(self):
        self.assertEqual(self.feed(click(50))[-1], "missed")
        self.assertEqual(self.saved, [])

    def test_click_outside_area_keeps_dot_and_restarts_movement(self):
        self.assertEqual(self.feed(move(10), click(20, 50, 50))[-1], "outside")
        self.assertIsNotNone(self.collector.target)
        self.assertEqual(self.feed(move(500), click(600))[-1], "saved")
        self.assertEqual(self.saved[0][0][0], move(500))

    def test_events_before_dot_appeared_are_ignored(self):
        self.collector.arm(target(spawn_ms=100))
        self.feed(move(50), move(120), click(250))
        self.assertEqual(self.saved[0][0], [move(120), click(250)])

    def test_right_click_is_kept_but_does_not_end_attempt(self):
        outcomes = self.feed(move(10), click(30, button="right"), click(60))
        self.assertEqual(outcomes, [None, None, "saved"])
        self.assertEqual(len(self.saved[0][0]), 3)

    def test_nothing_recorded_until_next_dot(self):
        self.feed(move(10), click(50))
        self.assertEqual(self.feed(move(60), click(70)), [None, None])
        self.assertEqual(len(self.saved), 1)


class AimDatabaseTests(unittest.TestCase):
    def test_saved_trial_has_target_row_and_separate_file(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "aim.sqlite3"
            database = AimDatabase(path)
            session = database.start_session("aim", 200 * MS, (0, 0, 1920, 1080), "win32")
            events = [move(130), click(250)]
            segment = database.save_trial(session, 0, events, target(spawn_ms=100), 1.0)
            database.close()
            database = AimDatabase(path)
            self.assertEqual(database.count_segments(), 1)
            self.assertEqual(database.connection.execute("SELECT * FROM targets").fetchall(),
                             [(segment, 300, 300, 15, -30 * MS)])
            self.assertEqual(database.connection.execute(
                "SELECT duration_ns, event_count FROM segments").fetchall(), [(120 * MS, 2)])
            database.close()


if __name__ == "__main__":
    unittest.main()
