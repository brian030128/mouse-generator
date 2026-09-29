"""Turn recorded segments into step sequences the model can learn from.

A segment becomes a sequence of steps. Every step is one delivered mouse event:
its displacement from the previous event (pixels), the time since the previous
event (milliseconds) and whether it is the final left-button press. The target
of a segment is the position of that press, so the model can be conditioned on
the vector still to travel.
"""

import sqlite3
from dataclasses import dataclass

import numpy as np
import torch

# Feature scales. Deltas are divided by PIXEL_SCALE before modelling; the
# remaining-distance features use both a linear and a compressed encoding so
# that 2-pixel nudges and 1500-pixel sweeps both land in a sensible range.
PIXEL_SCALE = 10.0
REMAINING_SCALE = 500.0
MIN_DT_MS = 0.5
STEP_FEATURES = 4      # dx, dy, log dt, click
CONTEXT_FEATURES = 9   # remaining vector encodings + elapsed time + start flag
INPUT_FEATURES = STEP_FEATURES + CONTEXT_FEATURES


@dataclass
class Segment:
    session_id: int
    steps: np.ndarray   # (n, 4): dx, dy, dt_ms, click
    start: np.ndarray   # (2,) absolute position of the first move event


def load_segments(db_path, max_steps=512, min_steps=1):
    """Read all segments; keep moves and the closing left press only."""
    con = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    rows = con.execute(
        "SELECT e.segment_id, s.session_id, e.t_ns, e.x, e.y, e.kind "
        "FROM events e JOIN segments s ON s.id = e.segment_id "
        "WHERE e.kind = 'move' OR (e.kind = 'down' AND e.button = 'left') "
        "ORDER BY e.segment_id, e.sequence"
    ).fetchall()
    con.close()
    segments = []
    current = None
    buffer = []

    def flush():
        if current is None or not buffer:
            return
        ev = np.array(buffer, dtype=np.float64)
        if ev[-1, 3] != 1 or len(ev) < min_steps + 1 or len(ev) > max_steps + 1:
            return
        steps = np.empty((len(ev) - 1, 4), dtype=np.float32)
        steps[:, 0] = np.diff(ev[:, 1])
        steps[:, 1] = np.diff(ev[:, 2])
        steps[:, 2] = np.maximum(np.diff(ev[:, 0]) / 1e6, MIN_DT_MS)
        steps[:, 3] = ev[1:, 3]
        segments.append(Segment(current[1], steps, ev[0, 1:3].astype(np.float32)))

    for seg_id, session_id, t_ns, x, y, kind in rows:
        if current is None or current[0] != seg_id:
            flush()
            current = (seg_id, session_id)
            buffer = []
        buffer.append((t_ns, x, y, 1.0 if kind == "down" else 0.0))
    flush()
    return segments


def split_by_session(segments, holdout_fraction=0.1, seed=0):
    """Hold out whole sessions so validation measures generalisation."""
    counts = {}
    for s in segments:
        counts[s.session_id] = counts.get(s.session_id, 0) + 1
    sessions = sorted(counts)
    rng = np.random.default_rng(seed)
    rng.shuffle(sessions)
    total = len(segments)
    held = set()
    running = 0
    for sid in sessions:
        if running + counts[sid] > total * holdout_fraction * 1.5:
            continue
        held.add(sid)
        running += counts[sid]
        if running >= total * holdout_fraction:
            break
    train = [s for s in segments if s.session_id not in held]
    val = [s for s in segments if s.session_id in held]
    return train, val


def context_features(remaining, elapsed_ms, is_start):
    """Encode what the model needs to know about the goal at each step.

    remaining: (..., 2) pixels still to travel to the target.
    elapsed_ms: (...,) time since the segment began.
    is_start: (...,) 1 for the first step of a segment.
    """
    r = np.asarray(remaining, dtype=np.float32)
    elapsed_ms = np.asarray(elapsed_ms, dtype=np.float32)
    is_start = np.asarray(is_start, dtype=np.float32)
    dist = np.sqrt((r ** 2).sum(-1, keepdims=True))
    unit = r / np.maximum(dist, 1e-6)
    return np.concatenate([
        r / REMAINING_SCALE,
        np.arcsinh(r / 20.0) / 4.0,
        np.log1p(dist) / 7.0,
        unit,
        np.log1p(elapsed_ms)[..., None] / 8.0,
        is_start[..., None],
    ], axis=-1).astype(np.float32)


def encode_step(steps):
    """Normalise raw steps (dx, dy, dt_ms, click) for model input."""
    steps = np.asarray(steps, dtype=np.float32)
    out = np.empty_like(steps)
    out[..., 0] = np.arcsinh(steps[..., 0] / PIXEL_SCALE)
    out[..., 1] = np.arcsinh(steps[..., 1] / PIXEL_SCALE)
    out[..., 2] = np.log(steps[..., 2]) / 3.0
    out[..., 3] = steps[..., 3]
    return out


def encode_targets(steps):
    """Targets the mixture head predicts: scaled dx, dy, log dt, click."""
    steps = np.asarray(steps, dtype=np.float32)
    out = np.empty_like(steps)
    out[..., 0] = steps[..., 0] / PIXEL_SCALE
    out[..., 1] = steps[..., 1] / PIXEL_SCALE
    out[..., 2] = np.log(steps[..., 2])
    out[..., 3] = steps[..., 3]
    return out


def segment_to_arrays(segment):
    """Build (inputs, targets) for teacher forcing. Both have length n."""
    steps = segment.steps
    n = len(steps)
    positions = np.cumsum(steps[:, :2], axis=0)          # after each step
    target = positions[-1]
    before = np.vstack([np.zeros((1, 2), np.float32), positions[:-1]])
    remaining = target - before
    elapsed = np.concatenate([[0.0], np.cumsum(steps[:-1, 2])]).astype(np.float32)
    is_start = np.zeros(n, np.float32)
    is_start[0] = 1.0
    prev = np.vstack([np.zeros((1, 4), np.float32), encode_step(steps[:-1])])
    inputs = np.concatenate([prev, context_features(remaining, elapsed, is_start)], axis=-1)
    return inputs, encode_targets(steps)


class StepDataset(torch.utils.data.Dataset):
    def __init__(self, segments):
        self.items = [segment_to_arrays(s) for s in segments]
        self.lengths = [len(x) for x, _ in self.items]

    def __len__(self):
        return len(self.items)

    def __getitem__(self, index):
        inputs, targets = self.items[index]
        return torch.from_numpy(inputs), torch.from_numpy(targets)


def collate(batch):
    lengths = torch.tensor([len(x) for x, _ in batch])
    inputs = torch.nn.utils.rnn.pad_sequence([x for x, _ in batch], batch_first=True)
    targets = torch.nn.utils.rnn.pad_sequence([y for _, y in batch], batch_first=True)
    mask = torch.arange(inputs.shape[1])[None, :] < lengths[:, None]
    return inputs, targets, mask.float()


class BucketSampler(torch.utils.data.Sampler):
    """Batch sequences of similar length together to waste less padding."""

    def __init__(self, lengths, batch_size, shuffle=True, seed=0):
        self.lengths = np.asarray(lengths, dtype=np.float64)
        self.batch_size = batch_size
        self.shuffle = shuffle
        self.rng = np.random.default_rng(seed)

    def __iter__(self):
        jitter = self.rng.random(len(self.lengths)) * 8 if self.shuffle else 0
        order = np.argsort(self.lengths + jitter)
        batches = [order[i:i + self.batch_size] for i in range(0, len(order), self.batch_size)]
        if self.shuffle:
            self.rng.shuffle(batches)
        for b in batches:
            yield b.tolist()

    def __len__(self):
        return (len(self.lengths) + self.batch_size - 1) // self.batch_size
