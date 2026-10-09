"""DMTG-style diffusion generator (Liu et al., arXiv:2410.18233).

The paper generates a fixed-length sequence of absolute cursor coordinates with
a 1D U-Net denoiser conditioned on the start and end points and a complexity
factor alpha, samples with DDIM from a mixture-of-Gaussians initial noise, and
adds an x0 reconstruction term and a path-length "style" term to the diffusion
loss. It does not model timing. Several hyperparameters are not disclosed in
the paper (sequence length N, layer widths, diffusion steps, loss weights), so
the values here are our own choices and are documented next to each one.

Differences from the paper, made so the output can be replayed and compared:
- coordinates are expressed relative to the start point and scaled;
- segments are resampled uniformly in time to N points, so point spacing
  encodes speed and a pause shows as clustered points;
- a small head predicts the total movement duration, giving each point a time;
- sampling can pin the first/last points to the requested start/end
  (replacement inpainting), which the paper's conditioning alone does not.

    python -m generator.diffusion --db data/mouse.sqlite3 --epochs 200
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

N_POINTS = 64          # paper: N undisclosed
COORD_SCALE = 500.0    # pixels per unit
STYLE_BINS = 16        # paper: S undisclosed
DIFFUSION_STEPS = 1000
W_X0 = 1.0             # paper: w2 undisclosed
W_STYLE = 0.1          # paper: w3 undisclosed


# ----------------------------------------------------------------------------
# data
# ----------------------------------------------------------------------------

def resample_segment(segment, n=N_POINTS):
    """Uniform-in-time resample of one segment to n points relative to its start.

    Returns (points (n, 2) in pixels relative to the start, duration_ms, alpha)
    where alpha = path length / displacement - 1 is the paper's complexity.
    """
    steps = segment.steps
    t = np.concatenate([[0.0], np.cumsum(steps[:, 2])])
    xy = np.vstack([np.zeros((1, 2), np.float32), np.cumsum(steps[:, :2], axis=0)])
    duration = float(t[-1])
    grid = np.linspace(0.0, duration, n)
    points = np.column_stack([np.interp(grid, t, xy[:, 0]), np.interp(grid, t, xy[:, 1])])
    seg = np.diff(xy, axis=0)
    path = float(np.hypot(seg[:, 0], seg[:, 1]).sum())
    disp = float(np.hypot(*xy[-1]))
    # Tiny displacements with long wanders give absurd ratios; cap the paper's
    # complexity so the style embedding and loss stay in a sane range.
    alpha = min(path / max(disp, 1.0) - 1.0, 20.0)
    return points.astype(np.float32), duration, alpha


def build_arrays(segments, n=N_POINTS, min_displacement=3.0):
    """Stack resampled segments; drop near-zero displacements (no path to shape)."""
    points, durations, alphas = [], [], []
    for s in segments:
        p, d, a = resample_segment(s, n)
        if np.hypot(*p[-1]) < min_displacement or d <= 0:
            continue
        points.append(p)
        durations.append(d)
        alphas.append(a)
    return (np.stack(points) / COORD_SCALE, np.array(durations, np.float32),
            np.array(alphas, np.float32))


def alpha_to_style(alpha):
    """Bucket alpha into STYLE_BINS categories: paper's StyleEmb(floor(alpha * S))."""
    # Alpha is heavy tailed (0 for a straight line, >5 for wandering); log-space
    # buckets spread the mass evenly enough.
    a = torch.as_tensor(alpha, dtype=torch.float32)
    idx = torch.floor(torch.log1p(a.clamp(min=0)) / math.log(1 + 8.0) * STYLE_BINS)
    return idx.clamp(0, STYLE_BINS - 1).long()


def condition_features(end, alpha):
    """Per-sample conditioning vector: relative end point and alpha."""
    end = torch.as_tensor(end, dtype=torch.float32)
    alpha = torch.as_tensor(alpha, dtype=torch.float32).reshape(-1, 1)
    dist = end.norm(dim=-1, keepdim=True)
    unit = end / dist.clamp(min=1e-6)
    return torch.cat([end, unit, torch.log1p(dist * COORD_SCALE) / 7.0, torch.log1p(alpha) / 2.0], -1)


COND_FEATURES = 6


# ----------------------------------------------------------------------------
# model
# ----------------------------------------------------------------------------

def sinusoidal(t, dim):
    half = dim // 2
    freqs = torch.exp(-math.log(10000) * torch.arange(half, device=t.device) / half)
    ang = t.float()[:, None] * freqs[None]
    return torch.cat([ang.sin(), ang.cos()], -1)


class ResBlock(nn.Module):
    def __init__(self, cin, cout, emb):
        super().__init__()
        self.norm1 = nn.GroupNorm(8, cin)
        self.conv1 = nn.Conv1d(cin, cout, 3, padding=1)
        self.emb = nn.Linear(emb, cout * 2)
        self.norm2 = nn.GroupNorm(8, cout)
        self.conv2 = nn.Conv1d(cout, cout, 3, padding=1)
        self.skip = nn.Conv1d(cin, cout, 1) if cin != cout else nn.Identity()

    def forward(self, x, emb):
        h = self.conv1(F.silu(self.norm1(x)))
        scale, shift = self.emb(emb)[:, :, None].chunk(2, dim=1)
        h = self.norm2(h) * (1 + scale) + shift
        h = self.conv2(F.silu(h))
        return h + self.skip(x)


class Attention(nn.Module):
    def __init__(self, ch, heads=4):
        super().__init__()
        self.norm = nn.GroupNorm(8, ch)
        self.attn = nn.MultiheadAttention(ch, heads, batch_first=True)

    def forward(self, x):
        h = self.norm(x).transpose(1, 2)
        h, _ = self.attn(h, h, h, need_weights=False)
        return x + h.transpose(1, 2)


class UNet1D(nn.Module):
    """Encoder/decoder pairs (paper: 2 x A units) over the point sequence.

    Input channels: noisy (x, y) plus the end point broadcast along the length.
    Conditioning on timestep, style bucket and the continuous condition vector
    enters every residual block, as the paper's decoding units receive both
    time step and alpha.
    """

    def __init__(self, channels=(64, 128, 256), emb=256):
        super().__init__()
        self.channels = channels
        self.time_mlp = nn.Sequential(nn.Linear(emb, emb), nn.SiLU(), nn.Linear(emb, emb))
        self.style = nn.Embedding(STYLE_BINS, emb)
        self.cond = nn.Sequential(nn.Linear(COND_FEATURES, emb), nn.SiLU(), nn.Linear(emb, emb))
        self.emb_dim = emb
        self.inp = nn.Conv1d(4, channels[0], 3, padding=1)
        self.down = nn.ModuleList()
        cin = channels[0]
        for i, c in enumerate(channels):
            self.down.append(nn.ModuleList([ResBlock(cin, c, emb), ResBlock(c, c, emb)]))
            cin = c
        self.mid = nn.ModuleList([ResBlock(cin, cin, emb), Attention(cin), ResBlock(cin, cin, emb)])
        self.up = nn.ModuleList()
        for i, c in reversed(list(enumerate(channels))):
            self.up.append(nn.ModuleList([ResBlock(cin + c, c, emb), ResBlock(c, c, emb)]))
            cin = c
        self.out = nn.Sequential(nn.GroupNorm(8, cin), nn.SiLU(), nn.Conv1d(cin, 2, 3, padding=1))

    def forward(self, x, t, style, cond):
        emb = self.time_mlp(sinusoidal(t, self.emb_dim)) + self.style(style) + self.cond(cond)
        end = cond[:, :2, None].expand(-1, -1, x.shape[-1])
        h = self.inp(torch.cat([x, end], 1))
        skips = []
        for i, (a, b) in enumerate(self.down):
            h = b(a(h, emb), emb)
            skips.append(h)
            if i < len(self.down) - 1:
                h = F.avg_pool1d(h, 2)
        h = self.mid[0](h, emb)
        h = self.mid[1](h)
        h = self.mid[2](h, emb)
        for i, (a, b) in enumerate(self.up):
            skip = skips.pop()
            if h.shape[-1] != skip.shape[-1]:
                h = F.interpolate(h, size=skip.shape[-1], mode="linear", align_corners=False)
            h = b(a(torch.cat([h, skip], 1), emb), emb)
        return self.out(h)


class DurationHead(nn.Module):
    """log duration ~ Normal(mu, sigma) given the condition vector (not in the paper)."""

    def __init__(self, hidden=128):
        super().__init__()
        self.net = nn.Sequential(nn.Linear(COND_FEATURES, hidden), nn.SiLU(),
                                 nn.Linear(hidden, hidden), nn.SiLU(), nn.Linear(hidden, 2))

    LOG_CENTER = 5.5   # log(245 ms): typical segment, so the head starts near the answer
    LOG_SPREAD = 1.5

    def forward(self, cond):
        mu, log_sigma = self.net(cond).unbind(-1)
        return mu, log_sigma.clamp(-4, 2)

    def loss(self, cond, duration_ms):
        mu, log_sigma = self(cond)
        target = (torch.log(duration_ms) - self.LOG_CENTER) / self.LOG_SPREAD
        z = (target - mu) / log_sigma.exp()
        return (0.5 * z ** 2 + log_sigma).mean()

    @torch.no_grad()
    def sample(self, cond, temperature=1.0, generator=None):
        mu, log_sigma = self(cond)
        eps = torch.randn(mu.shape, device=mu.device, generator=generator)
        z = mu + log_sigma.exp() * eps * temperature
        return torch.exp(z * self.LOG_SPREAD + self.LOG_CENTER)


class DMTG(nn.Module):
    def __init__(self, channels=(64, 128, 256), emb=256, steps=DIFFUSION_STEPS):
        super().__init__()
        self.config = dict(channels=tuple(channels), emb=emb, steps=steps)
        self.unet = UNet1D(channels, emb)
        self.duration = DurationHead()
        self.steps = steps
        # cosine schedule (Nichol & Dhariwal); the paper does not state a schedule.
        s = 0.008
        x = torch.linspace(0, steps, steps + 1)
        f = torch.cos((x / steps + s) / (1 + s) * math.pi / 2) ** 2
        alphas_bar = (f / f[0]).clamp(min=1e-5)
        self.register_buffer("alphas_bar", alphas_bar[1:])

    # -- training -----------------------------------------------------------
    def loss(self, x0, alpha, duration_ms):
        """x0: (B, N, 2) scaled points. Returns total loss and parts."""
        b = x0.shape[0]
        x0c = x0.transpose(1, 2)                                   # (B, 2, N)
        end = x0[:, -1]
        cond = condition_features(end, alpha)
        style = alpha_to_style(alpha)
        t = torch.randint(0, self.steps, (b,), device=x0.device)
        ab = self.alphas_bar[t][:, None, None]
        noise = torch.randn_like(x0c)
        xt = ab.sqrt() * x0c + (1 - ab).sqrt() * noise
        eps_hat = self.unet(xt, t, style, cond)
        l_eps = F.mse_loss(eps_hat, noise)
        x0_hat = (xt - (1 - ab).sqrt() * eps_hat) / ab.sqrt()
        # The x0 estimate is 1/sqrt(alpha_bar) noisier at high noise levels, so
        # weight both x0-based terms by alpha_bar to keep them from dominating.
        weight = ab.reshape(b)
        l_x0 = (weight * ((x0_hat - x0c) ** 2).mean(dim=(1, 2))).mean()
        # Style loss: path length / displacement of the reconstruction vs the
        # paper's alpha (path/disp - 1), compared in log space for stability.
        seg = x0_hat[:, :, 1:] - x0_hat[:, :, :-1]
        path = seg.norm(dim=1).sum(-1)
        disp = end.norm(dim=-1).clamp(min=3.0 / COORD_SCALE)
        ratio = (path / disp - 1.0).clamp(min=0.0)
        l_style = (weight * (torch.log1p(ratio) - torch.log1p(alpha)) ** 2).mean()
        l_dur = self.duration.loss(cond, duration_ms)
        total = l_eps + W_X0 * l_x0 + W_STYLE * l_style + l_dur
        return total, {"eps": l_eps.item(), "x0": l_x0.item(), "style": l_style.item(), "dur": l_dur.item()}

    # -- sampling -------------------------------------------------------------
    @torch.no_grad()
    def sample(self, end, alpha, sample_steps=50, eta=0.0, complexity=None,
               inpaint=True, temperature=1.0, generator=None):
        """DDIM sampling. end: (B, 2) scaled relative end points; alpha: (B,).

        complexity is the paper's alpha for the initial noise mixture; it
        defaults to the conditioning alpha. Returns (points (B, N, 2) scaled,
        duration_ms (B,)).
        """
        device = self.alphas_bar.device
        end = torch.as_tensor(end, dtype=torch.float32, device=device)
        alpha = torch.as_tensor(alpha, dtype=torch.float32, device=device)
        b = end.shape[0]
        cond = condition_features(end, alpha)
        style = alpha_to_style(alpha).to(device)
        x = self.initial_noise(end, alpha if complexity is None else complexity, generator)
        timesteps = torch.linspace(self.steps - 1, 0, sample_steps, device=device).long()
        start = torch.zeros(b, 2, device=device)
        for i, t in enumerate(timesteps):
            ab = self.alphas_bar[t]
            if inpaint:
                # Replacement conditioning: pin first/last points at their noised values.
                known = torch.stack([start, end], -1)                    # (B, 2, 2)
                noisy_known = ab.sqrt() * known + (1 - ab).sqrt() * torch.randn(
                    known.shape, device=device, generator=generator)
                x = x.clone()
                x[:, :, 0] = noisy_known[:, :, 0]
                x[:, :, -1] = noisy_known[:, :, 1]
            tt = torch.full((b,), int(t), device=device)
            eps = self.unet(x, tt, style, cond)
            x0_hat = (x - (1 - ab).sqrt() * eps) / ab.sqrt()
            # Clip the denoised estimate to the screen (about +-4 units); the
            # division by sqrt(alpha_bar) otherwise amplifies early errors.
            x0_hat = x0_hat.clamp(-4.0, 4.0)
            eps = (x - ab.sqrt() * x0_hat) / (1 - ab).sqrt().clamp(min=1e-4)
            if i + 1 < len(timesteps):
                ab_prev = self.alphas_bar[timesteps[i + 1]]
                sigma = eta * torch.sqrt((1 - ab_prev) / (1 - ab) * (1 - ab / ab_prev))
                noise = torch.randn(x.shape, device=device, generator=generator) if eta > 0 else 0.0
                x = ab_prev.sqrt() * x0_hat + torch.sqrt((1 - ab_prev - sigma ** 2).clamp(min=0)) * eps + sigma * noise
            else:
                x = x0_hat
        if inpaint:
            x[:, :, 0] = start
            x[:, :, -1] = end
        duration = self.duration.sample(cond, temperature, generator)
        return x.transpose(1, 2), duration

    def initial_noise(self, end, complexity, generator=None):
        """Paper eqs. 4-6: mix isotropic noise with noise directed along start->end.

        a = 1/(complexity+1) weights the isotropic part; the result is rescaled
        to unit variance so it stays a valid diffusion prior.
        """
        b, n = end.shape[0], N_POINTS
        device = end.device
        iso = torch.randn(b, 2, n, device=device, generator=generator)
        unit = end / end.norm(dim=-1, keepdim=True).clamp(min=1e-6)
        along = torch.randn(b, 1, n, device=device, generator=generator) * unit[:, :, None]
        directed = along * math.sqrt(2.0)          # unit total variance over the 2 axes
        complexity = torch.as_tensor(complexity, dtype=torch.float32, device=device).clamp(min=0.0)
        a = (1.0 / (complexity + 1.0)).reshape(b, 1, 1)
        mixed = a.sqrt() * iso + (1 - a).clamp(min=0.0).sqrt() * directed
        return mixed


# ----------------------------------------------------------------------------
# training and public helpers
# ----------------------------------------------------------------------------

def load_dmtg(path, device=None):
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    ckpt = torch.load(path, map_location=device, weights_only=False)
    model = DMTG(**ckpt["config"]).to(device)
    model.load_state_dict(ckpt["model"])
    model.eval()
    model.alpha_quantiles = ckpt.get("alpha_quantiles")
    return model


DEFAULT_TICK_MS = 7.5


def resample_to_ticks(points, duration_ms, tick_quantiles, rng):
    """Re-time a uniformly spaced path at recorder-like poll intervals.

    The paper's output has no timing; this draws successive gaps from the
    empirical distribution of recorded gaps (tick_quantiles, in ms) and linearly
    interpolates the path at those times, so the result can be replayed and
    compared like a recorded segment.
    """
    n = len(points)
    grid = np.linspace(0.0, duration_ms, n)
    times = [0.0]
    while True:
        gap = float(np.interp(rng.random(), np.linspace(0, 1, len(tick_quantiles)), tick_quantiles))
        if times[-1] + gap >= duration_ms:
            break
        times.append(times[-1] + gap)
    times = np.array(times + [duration_ms], dtype=np.float32)
    xy = np.column_stack([np.interp(times, grid, points[:, 0]), np.interp(times, grid, points[:, 1])])
    return times, xy


@torch.no_grad()
def generate_dmtg(model, starts, targets, alpha=None, sample_steps=50, complexity=None,
                  temperature=1.0, seed=None, inpaint=True, tick_quantiles=None):
    """Return trajectories as (t_ms, x, y, click) rows like generator.sample.

    With tick_quantiles (recorded gap quantiles in ms) the path is re-timed at
    poll intervals; otherwise the N points are spaced uniformly in time.
    """
    device = model.alphas_bar.device
    starts = np.asarray(starts, np.float32).reshape(-1, 2)
    targets = np.asarray(targets, np.float32).reshape(-1, 2)
    b = len(starts)
    generator = torch.Generator(device=device).manual_seed(seed) if seed is not None else None
    if alpha is None:
        # Draw alpha from the training distribution (stored quantiles) unless given.
        q = torch.as_tensor(model.alpha_quantiles, device=device)
        u = torch.rand(b, device=device, generator=generator) * (len(q) - 1)
        lo = u.floor().long().clamp(max=len(q) - 2)
        alpha = q[lo] + (u - lo) * (q[lo + 1] - q[lo])
    alpha = torch.as_tensor(alpha, dtype=torch.float32, device=device).expand(b).clamp(min=0.0)
    end = torch.as_tensor((targets - starts) / COORD_SCALE, device=device)
    points, duration = model.sample(end, alpha, sample_steps, complexity=complexity,
                                    inpaint=inpaint, temperature=temperature, generator=generator)
    points = points.cpu().numpy() * COORD_SCALE + starts[:, None, :]
    duration = duration.cpu().numpy()
    rng = np.random.default_rng(seed)
    rows = []
    for i in range(b):
        if tick_quantiles is not None:
            t, xy = resample_to_ticks(points[i], float(duration[i]), tick_quantiles, rng)
        else:
            t, xy = np.linspace(0.0, duration[i], N_POINTS), points[i]
        xy = np.round(xy)
        click = np.zeros(len(t), np.float32)
        click[-1] = 1.0
        rows.append(np.column_stack([t, xy, click]).astype(np.float32))
    return rows


def default_dmtg_checkpoint():
    return Path(__file__).resolve().parent.parent / "models" / "mouse_dmtg.pt"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/mouse.sqlite3")
    parser.add_argument("--out", default="models/mouse_dmtg.pt")
    parser.add_argument("--epochs", type=int, default=200)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--lr", type=float, default=2e-4)
    parser.add_argument("--holdout", type=float, default=0.2)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()
    torch.manual_seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    segments = load_segments(args.db)
    train_segments, val_segments = split_by_session(segments, args.holdout, args.seed)
    xtr, dtr, atr = build_arrays(train_segments)
    xva, dva, ava = build_arrays(val_segments)
    print(f"resampled: {len(xtr)} train, {len(xva)} val (N={N_POINTS})")
    xtr, dtr, atr = (torch.tensor(v, device=device) for v in (xtr, dtr, atr))
    xva, dva, ava = (torch.tensor(v, device=device) for v in (xva, dva, ava))
    alpha_quantiles = np.quantile(atr.cpu().numpy(), np.linspace(0, 1, 101)).astype(np.float32)

    model = DMTG().to(device)
    print(f"parameters: {sum(p.numel() for p in model.parameters()) / 1e6:.2f}M on {device}")
    ema = torch.optim.swa_utils.AveragedModel(model, multi_avg_fn=torch.optim.swa_utils.get_ema_multi_avg_fn(0.999))
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    updates_per_epoch = math.ceil(len(xtr) / args.batch_size)
    total = args.epochs * updates_per_epoch
    scheduler = torch.optim.lr_scheduler.OneCycleLR(optimizer, args.lr, total_steps=total, pct_start=0.05)

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    history = []
    best = float("inf")
    for epoch in range(1, args.epochs + 1):
        start = time.time()
        perm = torch.randperm(len(xtr), device=device)
        running = 0.0
        for i in range(0, len(xtr), args.batch_size):
            idx = perm[i:i + args.batch_size]
            loss, _ = model.loss(xtr[idx], atr[idx], dtr[idx])
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            scheduler.step()
            ema.update_parameters(model)
            running += loss.item()
        if epoch % 5 == 0 or epoch == args.epochs:
            ema.module.eval()
            with torch.no_grad():
                torch.manual_seed(1234)      # fixed timesteps/noise for comparable val loss
                vals = []
                parts_acc = {}
                for i in range(0, len(xva), 1024):
                    l, parts = ema.module.loss(xva[i:i + 1024], ava[i:i + 1024], dva[i:i + 1024])
                    vals.append(l.item() * len(xva[i:i + 1024]))
                    for k, v in parts.items():
                        parts_acc[k] = parts_acc.get(k, 0.0) + v * len(xva[i:i + 1024])
                torch.manual_seed(args.seed + epoch)
            val_loss = sum(vals) / len(xva)
            record = {"epoch": epoch, "train_loss": running / updates_per_epoch, "val_loss": val_loss,
                      **{k: v / len(xva) for k, v in parts_acc.items()}, "seconds": time.time() - start}
            history.append(record)
            print(json.dumps(record), flush=True)
            if val_loss < best:
                best = val_loss
                torch.save({"model": ema.module.state_dict(), "config": model.config, "epoch": epoch,
                            "val_loss": val_loss, "alpha_quantiles": alpha_quantiles}, out_path)
    with open(out_path.with_suffix(".history.json"), "w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=1)
    print(f"best val loss {best:.4f}; saved {out_path}")


if __name__ == "__main__":
    main()
