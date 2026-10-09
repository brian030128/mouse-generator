"""BeCAPTCHA-Mouse generators (Acien et al., arXiv:2005.00890).

The paper describes two ways to synthesise mouse trajectories between two
points:

Function-based. A path shape (linear, quadratic or exponential) crossed with
a velocity profile (constant spacing, "logarithmic" spacing that accelerates,
or Gaussian spacing that accelerates then decelerates). The number of points
M is drawn from a Gaussian fitted to the human trajectories.

GAN. An LSTM generator maps a 100-number noise vector to an M x 2 coordinate
sequence; an LSTM discriminator (128 then 64 units) tells it from human
sequences. Adam with lr 2e-4 and betas (0.5, 0.999), 50 epochs, batch 128.

Adaptations for this dataset, each marked in the code:
- The paper's data was sampled at 200 Hz; here one point is one recorder
  poll, and gaps are drawn from the recorded gap distribution (about 7.5 ms).
- The paper's GAN is unconditional and generates raw coordinates for a fixed
  task. To reach an arbitrary target, it is trained in a canonical frame
  (start at the origin, end at (1, 0)) and each sample is rotated and scaled
  onto the requested start and target, which is how a bot would deploy it.
- The paper fits the quadratic/exponential curvature to human data without
  stating ranges; here the signed peak lateral deviation, as a fraction of the
  displacement, is sampled from the training segments' empirical distribution.
- M and, for the GAN, the duration are drawn per displacement range from the
  training segments, replacing the paper's per-button-pair Gaussians.

    python -m generator.becaptcha --db data/mouse.sqlite3      # trains the GAN
"""

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .data import load_segments, split_by_session

GAN_POINTS = 64
NOISE_DIM = 100
DISTANCE_EDGES = np.array([0, 20, 60, 150, 300, 600, 1200, 1e9])


# ----------------------------------------------------------------------------
# statistics of the human data that both generators draw from
# ----------------------------------------------------------------------------

def segment_summary(segment):
    steps = segment.steps
    xy = np.vstack([np.zeros((1, 2), np.float32), np.cumsum(steps[:, :2], axis=0)])
    d = xy[-1]
    disp = float(np.hypot(*d))
    if disp < 1e-6:
        lateral = 0.0
    else:
        unit = d / disp
        lateral_all = xy[:, 0] * -unit[1] + xy[:, 1] * unit[0]
        lateral = float(lateral_all[np.argmax(np.abs(lateral_all))] / disp)
    return disp, len(steps), float(steps[:, 2].sum()), lateral


def fit_human_stats(segments):
    """Per displacement range: log point count and log duration (mean, std),
    plus the pooled signed lateral-deviation fractions and recorded gaps."""
    rows = np.array([segment_summary(s) for s in segments], dtype=np.float64)
    disp, count, duration, lateral = rows.T
    keep = disp >= 3.0
    stats = {"bins": DISTANCE_EDGES[:-1].tolist(), "count": [], "duration": []}
    bins = np.digitize(disp, DISTANCE_EDGES[1:-1])
    for b in range(len(DISTANCE_EDGES) - 1):
        sel = keep & (bins == b)
        if sel.sum() < 20:
            sel = keep
        stats["count"].append([float(np.log(count[sel]).mean()), float(np.log(count[sel]).std())])
        stats["duration"].append([float(np.log(duration[sel]).mean()), float(np.log(duration[sel]).std())])
    lat = lateral[keep & (disp >= 20)]
    stats["lateral_quantiles"] = np.quantile(lat, np.linspace(0, 1, 201)).tolist()
    gaps = np.concatenate([s.steps[:, 2] for s in segments])
    stats["gap_quantiles"] = np.quantile(gaps[gaps < 20], np.linspace(0, 1, 201)).tolist()
    return stats


def sample_quantiles(quantiles, size, rng):
    q = np.asarray(quantiles)
    return np.interp(rng.random(size), np.linspace(0, 1, len(q)), q)


def sample_points(stats, disp, rng, minimum=2):
    b = int(np.digitize(disp, DISTANCE_EDGES[1:-1]))
    mu, sd = stats["count"][b]
    return max(minimum, int(round(math.exp(rng.normal(mu, sd)))))


def sample_duration(stats, disp, rng):
    b = int(np.digitize(disp, DISTANCE_EDGES[1:-1]))
    mu, sd = stats["duration"][b]
    return float(math.exp(rng.normal(mu, sd)))


def to_rows(local, start, target, gaps):
    """Map canonical points (start (0,0), end (1,0)) onto start->target, with times."""
    start = np.asarray(start, np.float64)
    d = np.asarray(target, np.float64) - start
    disp = np.hypot(*d)
    c, s = (d / disp) if disp > 0 else (1.0, 0.0)
    rot = np.array([[c, -s], [s, c]])
    xy = np.round(local @ rot.T * disp + start)
    t = np.concatenate([[0.0], np.cumsum(gaps[:len(local) - 1])])
    click = np.zeros(len(local), np.float32)
    click[-1] = 1.0
    return np.column_stack([t, xy, click]).astype(np.float32)


# ----------------------------------------------------------------------------
# function-based generator
# ----------------------------------------------------------------------------

SHAPES = ("linear", "quadratic", "exponential")
PROFILES = ("constant", "logarithmic", "gaussian")


def shape_lateral(u, shape, k):
    """Lateral offset as a function of progress u in [0, 1]; zero at both ends."""
    if shape == "linear":
        return np.zeros_like(u)
    if shape == "quadratic":
        return 4.0 * k * u * (1.0 - u)               # peak k at u = 0.5
    # exponential: rises like e^(cu), pulled back to zero at u = 1
    c = 3.0
    curve = (np.exp(c * u) - 1.0) / (np.exp(c) - 1.0) - u
    return k * curve / max(float(np.max(np.abs(curve))), 1e-9)


def profile_progress(m, profile, rng):
    """Progress values u_0..u_{m-1} along the path for each velocity profile."""
    i = np.linspace(0.0, 1.0, m)
    if profile == "constant":
        return i
    if profile == "logarithmic":                     # spacing grows: acceleration
        b = rng.uniform(4.0, 12.0)
        return (b ** i - 1.0) / (b - 1.0)
    # gaussian: accelerate then decelerate (CDF of a bell centred mid-way)
    width = rng.uniform(0.15, 0.3)
    z = (i - 0.5) / width
    cdf = 0.5 * (1.0 + np.vectorize(math.erf)(z / math.sqrt(2.0)))
    return (cdf - cdf[0]) / (cdf[-1] - cdf[0])


def generate_function_based(stats, starts, targets, seed=None, shape=None, profile=None):
    """One trajectory per (start, target); shape/profile random unless given."""
    rng = np.random.default_rng(seed)
    starts = np.asarray(starts, np.float64).reshape(-1, 2)
    targets = np.asarray(targets, np.float64).reshape(-1, 2)
    rows = []
    for s, t in zip(starts, targets):
        disp = float(np.hypot(*(t - s)))
        m = sample_points(stats, disp, rng)
        sh = shape or SHAPES[rng.integers(len(SHAPES))]
        pr = profile or PROFILES[rng.integers(len(PROFILES))]
        u = profile_progress(m, pr, rng)
        k = float(sample_quantiles(stats["lateral_quantiles"], 1, rng)[0])
        local = np.column_stack([u, shape_lateral(u, sh, k)])
        gaps = sample_quantiles(stats["gap_quantiles"], m, rng)
        rows.append(to_rows(local, s, t, gaps))
    return rows


# ----------------------------------------------------------------------------
# GAN
# ----------------------------------------------------------------------------

def canonical_paths(segments, n=GAN_POINTS, min_displacement=20.0):
    """Resample segments uniformly in time to n points in the canonical frame."""
    out = []
    for seg in segments:
        steps = seg.steps
        xy = np.vstack([np.zeros((1, 2), np.float32), np.cumsum(steps[:, :2], axis=0)])
        d = xy[-1]
        disp = float(np.hypot(*d))
        if disp < min_displacement:
            continue
        t = np.concatenate([[0.0], np.cumsum(steps[:, 2])])
        grid = np.linspace(0.0, t[-1], n)
        p = np.column_stack([np.interp(grid, t, xy[:, 0]), np.interp(grid, t, xy[:, 1])])
        c, s = d / disp
        rot = np.array([[c, s], [-s, c]])
        out.append((p @ rot.T) / disp)
    return np.stack(out).astype(np.float32)


class Generator(nn.Module):
    """Paper: seed -> LSTM(128) -> LSTM(64) -> TimeDistributed Dense(2)."""

    def __init__(self, n=GAN_POINTS, noise=NOISE_DIM):
        super().__init__()
        self.n = n
        self.noise = noise
        self.lstm1 = nn.LSTM(noise, 128, batch_first=True)
        self.lstm2 = nn.LSTM(128, 64, batch_first=True)
        self.out = nn.Linear(64, 2)

    def forward(self, z):
        h, _ = self.lstm1(z[:, None, :].expand(-1, self.n, -1))
        h, _ = self.lstm2(h)
        return self.out(h)


class Discriminator(nn.Module):
    """Paper: LSTM(128) -> LSTM(64) -> Dense(1, sigmoid)."""

    def __init__(self):
        super().__init__()
        self.lstm1 = nn.LSTM(2, 128, batch_first=True)
        self.lstm2 = nn.LSTM(128, 64, batch_first=True)
        self.out = nn.Linear(64, 1)

    def forward(self, x):
        h, _ = self.lstm1(x)
        h, _ = self.lstm2(F.leaky_relu(h, 0.2))
        return self.out(F.leaky_relu(h[:, -1], 0.2)).squeeze(-1)


def train_gan(paths, epochs=50, batch_size=128, lr=2e-4, device="cuda", log=print):
    x = torch.tensor(paths, device=device)
    g, d = Generator().to(device), Discriminator().to(device)
    opt_g = torch.optim.Adam(g.parameters(), lr, betas=(0.5, 0.999), eps=1e-8)
    opt_d = torch.optim.Adam(d.parameters(), lr, betas=(0.5, 0.999), eps=1e-8)
    history = []
    for epoch in range(1, epochs + 1):
        start = time.time()
        perm = torch.randperm(len(x), device=device)
        ld = lg = 0.0
        n = 0
        for i in range(0, len(x), batch_size):
            real = x[perm[i:i + batch_size]]
            b = len(real)
            z = torch.randn(b, NOISE_DIM, device=device)
            fake = g(z).detach()
            loss_d = F.binary_cross_entropy_with_logits(d(real), torch.ones(b, device=device)) + \
                F.binary_cross_entropy_with_logits(d(fake), torch.zeros(b, device=device))
            opt_d.zero_grad(set_to_none=True)
            loss_d.backward()
            opt_d.step()
            z = torch.randn(b, NOISE_DIM, device=device)
            loss_g = F.binary_cross_entropy_with_logits(d(g(z)), torch.ones(b, device=device))
            opt_g.zero_grad(set_to_none=True)
            loss_g.backward()
            opt_g.step()
            ld += loss_d.item()
            lg += loss_g.item()
            n += 1
        record = {"epoch": epoch, "loss_d": ld / n, "loss_g": lg / n, "seconds": time.time() - start}
        history.append(record)
        log(json.dumps(record))
    return g, history


def load_becaptcha(path, device=None):
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    ckpt = torch.load(path, map_location=device, weights_only=False)
    g = Generator().to(device)
    g.load_state_dict(ckpt["generator"])
    g.eval()
    return g, ckpt["stats"]


@torch.no_grad()
def generate_gan(generator, stats, starts, targets, seed=None):
    """Sample canonical paths and map them onto each start->target.

    The path is resampled at M poll-spaced points, with M drawn from the
    human point-count distribution for that displacement, so timing matches
    the recorder rather than the paper's 200 Hz.
    """
    rng = np.random.default_rng(seed)
    device = next(generator.parameters()).device
    starts = np.asarray(starts, np.float64).reshape(-1, 2)
    targets = np.asarray(targets, np.float64).reshape(-1, 2)
    z = torch.randn(len(starts), NOISE_DIM, device=device,
                    generator=torch.Generator(device=device).manual_seed(seed) if seed is not None else None)
    paths = generator(z).cpu().numpy().astype(np.float64)
    rows = []
    grid = np.linspace(0.0, 1.0, GAN_POINTS)
    for p, s, t in zip(paths, starts, targets):
        # A bot pins the sampled path's ends to the requested points.
        p = p - p[0]
        end = p[-1]
        norm = np.hypot(*end)
        if norm > 1e-6:
            c, sn = end / norm
            p = p @ np.array([[c, sn], [-sn, c]]).T / norm
        disp = float(np.hypot(*(t - s)))
        m = sample_points(stats, disp, rng)
        u = np.linspace(0.0, 1.0, m)
        local = np.column_stack([np.interp(u, grid, p[:, 0]), np.interp(u, grid, p[:, 1])])
        gaps = sample_quantiles(stats["gap_quantiles"], m, rng)
        rows.append(to_rows(local, s, t, gaps))
    return rows


def default_becaptcha_checkpoint():
    return Path(__file__).resolve().parent.parent / "models" / "mouse_becaptcha_gan.pt"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/mouse.sqlite3")
    parser.add_argument("--out", default="models/mouse_becaptcha_gan.pt")
    parser.add_argument("--epochs", type=int, default=50)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--holdout", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"
    segments = load_segments(args.db)
    train_segments, _ = split_by_session(segments, args.holdout, args.seed)
    stats = fit_human_stats(train_segments)
    paths = canonical_paths(train_segments)
    print(f"canonical training paths: {len(paths)} x {GAN_POINTS} points on {device}")
    g, history = train_gan(paths, args.epochs, args.batch_size, args.lr, device)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    torch.save({"generator": g.state_dict(), "stats": stats}, out)
    with open(out.with_suffix(".history.json"), "w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=1)
    print(f"saved {out}")


if __name__ == "__main__":
    main()
