import tempfile
import unittest
from pathlib import Path

import numpy as np
import torch

from generator.data import (INPUT_FEATURES, StepDataset, collate, load_segments,
                            segment_to_arrays, split_by_session)
from generator.model import MouseModel, make_dt_edges
from generator.sample import generate_batch
from mouse_event import MouseEvent
from storage import Database


def make_database(path, segments=6):
    db = Database(path)
    for session in range(2):
        sid = db.start_session("", 0, (0, 0, 1920, 1080), "win32")
        for k in range(segments):
            events = [MouseEvent(i * 8_000_000, 100 + 5 * i, 200 + 3 * i, "move") for i in range(10)]
            if k % 2:
                events.append(MouseEvent(80_000_000, 145, 227, "wheel", "", 120))
            events.append(MouseEvent(90_000_000, 146, 228, "down", "left"))
            db.save_segment(sid, 0, events, 1.0)
    db.close()


class DataTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.sqlite3"
        make_database(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_segments_keep_moves_and_click_only(self):
        segments = load_segments(self.path)
        self.assertEqual(len(segments), 12)
        steps = segments[1].steps
        self.assertEqual(len(steps), 10)           # 9 move deltas + click step
        self.assertEqual(steps[-1, 3], 1.0)
        self.assertTrue((steps[:-1, 3] == 0).all())
        np.testing.assert_allclose(steps[0, :3], [5, 3, 8])
        np.testing.assert_allclose(steps[-1, :3], [1, 1, 18])

    def test_split_holds_out_whole_sessions(self):
        segments = load_segments(self.path)
        train, val = split_by_session(segments, holdout_fraction=0.5)
        self.assertEqual(len(train) + len(val), 12)
        self.assertFalse({s.session_id for s in train} & {s.session_id for s in val})

    def test_arrays_track_remaining_vector(self):
        segment = load_segments(self.path)[0]
        inputs, targets = segment_to_arrays(segment)
        self.assertEqual(inputs.shape, (10, INPUT_FEATURES))
        self.assertEqual(inputs[0, -1], 1.0)                 # start flag
        self.assertTrue((inputs[1:, -1] == 0).all())
        np.testing.assert_allclose(inputs[0, 4:6] * 500, [46, 28], atol=1e-3)
        self.assertEqual(targets[-1, 3], 1.0)

    def test_model_trains_and_samples(self):
        segments = load_segments(self.path)
        dataset = StepDataset(segments)
        inputs, targets, mask = collate([dataset[i] for i in range(4)])
        edges = make_dt_edges(np.concatenate([s.steps[:, 2] for s in segments]), 8)
        model = MouseModel(hidden=32, layers=1, mixtures=3, dt_edges=edges)
        h, _ = model(inputs)
        loss, info = model.loss(h, targets, mask)
        self.assertTrue(torch.isfinite(loss))
        self.assertEqual(set(info), {"nll_xy", "nll_dt", "click_bce"})
        loss.backward()
        rows = generate_batch(model, [(0, 0), (10, 10)], [(100, 50), (10, 10)], max_steps=5, seed=1)
        self.assertEqual(len(rows), 2)
        for r in rows:
            self.assertEqual(r.shape[1], 4)
            self.assertEqual(r[0, 0], 0.0)
            self.assertTrue((np.diff(r[:, 0]) > 0).all())
            self.assertLessEqual(len(r), 6)


class DiffusionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "t.sqlite3"
        make_database(self.path)
        self.segments = load_segments(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_resample_keeps_endpoints_and_alpha(self):
        from generator.diffusion import N_POINTS, resample_segment
        points, duration, alpha = resample_segment(self.segments[0])
        self.assertEqual(points.shape, (N_POINTS, 2))
        np.testing.assert_allclose(points[0], [0, 0])
        np.testing.assert_allclose(points[-1], [46, 28])
        self.assertAlmostEqual(duration, 90.0)
        self.assertLess(alpha, 0.01)                    # nearly a straight line

    def test_model_loss_and_sampling(self):
        from generator.diffusion import DMTG, build_arrays, generate_dmtg
        x, d, a = build_arrays(self.segments)
        model = DMTG(channels=(8, 16), emb=16, steps=20)
        loss, parts = model.loss(torch.tensor(x), torch.tensor(a), torch.tensor(d))
        self.assertTrue(torch.isfinite(loss))
        loss.backward()
        model.eval()
        model.alpha_quantiles = np.linspace(0, 1, 101, dtype=np.float32)
        rows = generate_dmtg(model, [(10, 10), (0, 0)], [(60, 40), (300, 0)], sample_steps=4, seed=0,
                             tick_quantiles=[7.5])
        for r, end in zip(rows, [(60, 40), (300, 0)]):
            self.assertTrue(np.isfinite(r).all())
            np.testing.assert_allclose(r[-1, 1:3], end)
            self.assertEqual(r[-1, 3], 1.0)
            self.assertTrue((np.diff(r[:, 0]) > 0).all())


if __name__ == "__main__":
    unittest.main()
