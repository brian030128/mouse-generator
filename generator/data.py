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
# Timing profile: how a machine's poll gaps spread around the ~7.5 ms tick.
# Jitter differs per machine and session, so the model is told which
# signature to reproduce instead of learning one blend of all sessions.
TICK_BANDS_MS = (0.0, 4.5, 6.0, 7.0, 7.25, 7.5, 7.75, 8.0, 9.0, 12.0, 20.0)
PROFILE_FEATURES = len(TICK_BANDS_MS) - 1
INPUT_FEATURES = STEP_FEATURES + CONTEXT_FEATURES + PROFILE_FEATURES
# Style: per-segment descriptors the model is told up front (duration, how
# roundabout the path is, when it gets halfway), so free-running sampling
# reproduces the spread of human moves instead of collapsing onto the most
# likely direct path. Generation draws them from training segments of similar
# displacement; see sample_styles.
STYLE_FEATURES = 3
DEFAULT_PROFILE = np.array([0.010, 0.020, 0.063, 0.038, 0.336, 0.318, 0.035, 0.063, 0.052, 0.065],
                           dtype=np.float32)


@dataclass
class Segment:
    session_id: int
    steps: np.ndarray   # (n, 4): dx, dy, dt_ms, click
    start: np.ndarray   # (2,) absolute position of the first move event
    profile: np.ndarray = None   # (PROFILE_FEATURES,) timing profile of its session


def timing_profile(gaps_ms, minimum=50):
    """Fractions of sub-20 ms gaps falling in each tick band, or None if too few."""
    g = np.asarray(gaps_ms, dtype=np.float64)
    g = g[g < TICK_BANDS_MS[-1]]
    if len(g) < minimum:
        return None
    return (np.histogram(g, TICK_BANDS_MS)[0] / len(g)).astype(np.float32)


def session_profiles(segments):
    """Timing profile per session id, from all move gaps recorded in it."""
    gaps = {}
    for s in segments:
        gaps.setdefault(s.session_id, []).append(s.steps[:-1, 2])
    profiles = {}
    for sid, parts in gaps.items():
        p = timing_profile(np.concatenate(parts))
        if p is not None:
            profiles[sid] = p
    return profiles


def assign_profiles(segments, profiles, default):
    for s in segments:
        s.profile = profiles.get(s.session_id, default)


def assign_local_profiles(segments, default, rng, window=(200, 5000), minimum=50):
    """Give each segment a profile from a random-sized window of gaps around
    it within its session (segments are in recording order).

    Training on these rather than one profile per session gives the model
    many distinct profiles, so it learns how a profile maps to gap behaviour
    instead of memorising sessions, and it follows jitter that drifts within a
    session. Sessions with too few gaps fall back to the default.
    """
    by_session = {}
    for s in segments:
        by_session.setdefault(s.session_id, []).append(s)
    for group in by_session.values():
        parts = [s.steps[:-1, 2] for s in group]
        gaps = np.concatenate(parts)
        if len(gaps) < minimum:
            for s in group:
                s.profile = default
            continue
        ends = np.cumsum([len(p) for p in parts])
        for s, end in zip(group, ends):
            centre = end - len(s.steps) // 2
            half = int(np.exp(rng.uniform(np.log(window[0]), np.log(window[1])))) // 2
            lo, hi = max(0, centre - half), min(len(gaps), centre + half)
            if hi - lo < minimum:
                lo, hi = max(0, centre - minimum), min(len(gaps), centre + minimum)
            s.profile = timing_profile(gaps[lo:hi], minimum=1)


def style_features(steps):
    """(STYLE_FEATURES,) descriptors of a recorded or wanted segment.

    log duration (scaled like the elapsed-time input), log path length minus
    log displacement (0 for a straight line), and the fraction of the
    duration at which the cursor first gets within half the displacement of
    the target.
    """
    steps = np.asarray(steps, dtype=np.float64)
    duration = float(steps[:, 2].sum())
    path = float(np.hypot(steps[:, 0], steps[:, 1]).sum())
    end = steps[:, :2].sum(0)
    disp = float(np.hypot(*end))
    remaining = np.hypot(*(end - np.cumsum(steps[:, :2], axis=0)).T)
    first = int(np.argmax(remaining <= 0.5 * disp))
    half_time = float(np.cumsum(steps[:, 2])[first]) / max(duration, 1e-6)
    return np.array([np.log1p(duration) / 8.0, np.log1p(path) - np.log1p(disp), half_time], np.float32)


def style_table(segments):
    """Rows of (log1p displacement, style...) sorted by displacement, for sample_styles."""
    rows = np.array([np.concatenate([[np.log1p(np.hypot(*s.steps[:, :2].sum(0)))], style_features(s.steps)])
                     for s in segments], np.float32)
    return rows[np.argsort(rows[:, 0], kind="stable")]


def sample_styles(table, displacements, rng, neighbours=64):
    """Draw one style per displacement from the training segments whose
    displacement is closest, keeping the joint spread of the descriptors."""
    table = np.asarray(table, np.float32)
    query = np.log1p(np.asarray(displacements, np.float64).reshape(-1))
    k = min(neighbours, len(table))
    idx = np.searchsorted(table[:, 0], query)
    lo = np.clip(idx - k // 2, 0, len(table) - k)
    return table[lo + rng.integers(0, k, len(query)), 1:].copy()


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


def segment_to_arrays(segment, style=False):
    """Build (inputs, targets) for teacher forcing. Both have length n.

    With style, the segment's own style_features are appended to every input.
    """
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
    profile = DEFAULT_PROFILE if segment.profile is None else segment.profile
    profile = np.broadcast_to(profile.astype(np.float32), (n, PROFILE_FEATURES))
    parts = [prev, context_features(remaining, elapsed, is_start), profile]
    if style:
        parts.append(np.broadcast_to(style_features(steps), (n, STYLE_FEATURES)))
    return np.concatenate(parts, axis=-1).astype(np.float32), encode_targets(steps)


class StepDataset(torch.utils.data.Dataset):
    def __init__(self, segments, style=False):
        self.items = [segment_to_arrays(s, style) for s in segments]
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
