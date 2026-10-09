"""GRU that emits one mouse event per step.

Per step the head factorises the event as
    p(click | h) * p(dt | h, click) * p(dx, dy | h, click, dt)
where dt is a categorical over fine bins of the recorded gap distribution
(most gaps sit within 0.3 ms of the 7.5 ms poll interval, with jitter and
pauses in the tails; a Gaussian mixture on log dt smears that structure) and
dx, then dy given dx, are mixtures of discretised logistics over integer
pixels, as in PixelCNN++. An earlier Gaussian step head (SketchRNN style)
could not be as peaked as the pixel lattice without losing the speed tail.
"""

import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .data import INPUT_FEATURES, MIN_DT_MS, PIXEL_SCALE

MAX_DT_MS = 5000.0
# Fixed edges: a fine grid through the jitter regions either side of the
# poll tick (a late poll lands at 5-6.7 ms, the catch-up at 8-12 ms) and a
# log-spaced pause tail. Quantiles of the data add resolution on the tick
# itself. Quantiles alone left one bin for everything below 6.5 ms, so a
# sampled "late poll" came out anywhere from 0.5 to 6.5 ms.
FIXED_EDGES_MS = tuple(np.round(np.arange(1.0, 7.01, 0.25), 2)) + \
    tuple(np.round(np.arange(7.75, 12.01, 0.25), 2)) + (13, 14, 15, 16.5, 18) + \
    (20, 23, 26, 30, 35, 45, 55, 65, 80, 100, 125, 150, 185, 220, 270, 330, 410, 500, 620,
     750, 900, 1100, 1400, 1700, 2100, 2500, 3000, 3600, 4300)


def make_dt_edges(dt_ms, bins=32):
    """Bin edges in log dt: fixed jitter/tail grid plus data quantiles on the tick."""
    log_dt = np.log(np.asarray(dt_ms, dtype=np.float64))
    quantiles = np.quantile(log_dt, np.linspace(0, 1, bins + 1)[1:-1])
    edges = np.concatenate([quantiles, np.log(FIXED_EDGES_MS)])
    edges = np.unique(np.round(edges, 4))
    edges = edges[(edges > math.log(MIN_DT_MS)) & (edges < math.log(MAX_DT_MS))]
    # Drop edges closer than 0.004 in log space (about 0.03 ms on the tick).
    kept = [edges[0]]
    for e in edges[1:]:
        if e - kept[-1] >= 0.004:
            kept.append(e)
    return [float(v) for v in kept]      # inner edges; bins = len + 1


class MouseModel(nn.Module):
    def __init__(self, hidden=512, layers=3, mixtures=20, dropout=0.1, dt_edges=None, dt_embed=32,
                 style_features=0):
        super().__init__()
        if dt_edges is None:
            dt_edges = make_dt_edges(np.array([7.5]), 1)
        self.config = dict(hidden=hidden, layers=layers, mixtures=mixtures, dropout=dropout,
                           dt_edges=list(dt_edges), dt_embed=dt_embed, style_features=style_features)
        # 0 for checkpoints trained before style conditioning existed.
        self.style_features = style_features
        self.mixtures = mixtures
        self.register_buffer("dt_edges", torch.tensor(dt_edges, dtype=torch.float32))
        self.dt_bins = len(dt_edges) + 1
        self.embed = nn.Sequential(nn.Linear(INPUT_FEATURES + style_features, hidden), nn.GELU())
        self.rnn = nn.GRU(hidden, hidden, layers, batch_first=True,
                          dropout=dropout if layers > 1 else 0.0)
        self.click_head = nn.Linear(hidden, 1)
        self.dt_head = nn.Sequential(nn.Linear(hidden + 1, hidden // 2), nn.GELU(),
                                     nn.Linear(hidden // 2, self.dt_bins))
        self.dt_embedding = nn.Embedding(self.dt_bins, dt_embed)
        # Integer step heads: a mixture of discretised logistics over dx, then
        # over dy given dx (PixelCNN++ style). The mouse delivers integer pixel
        # deltas, and at slow speed those are a few lattice points; a
        # categorical over the lattice can be as peaked as the data, where a
        # rounded Gaussian had to choose between heading wobble and a missing
        # speed tail. Per mixture: logit, mu, log_s -> 3.
        self.dx_head = nn.Sequential(nn.Linear(hidden + 1 + dt_embed, hidden), nn.GELU(),
                                     nn.Linear(hidden, mixtures * 3))
        self.dy_head = nn.Sequential(nn.Linear(hidden + 1 + dt_embed + 2, hidden), nn.GELU(),
                                     nn.Linear(hidden, mixtures * 3))

    def forward(self, inputs, state=None):
        h, state = self.rnn(self.embed(inputs), state)
        return h, state

    # -- pieces ---------------------------------------------------------------
    def dt_bin(self, log_dt):
        return torch.bucketize(log_dt.contiguous(), self.dt_edges)

    @staticmethod
    def _dx_encoding(dx_px):
        return torch.stack([torch.asinh(dx_px / 10.0), dx_px / 100.0], -1)

    def step_params(self, head, features):
        params = head(features).reshape(*features.shape[:-1], self.mixtures, 3)
        logit_pi = params[..., 0]
        mu = params[..., 1] * STEP_UNIT                       # pixels
        log_s = (params[..., 2] + math.log(STEP_UNIT)).clamp(LOG_S_MIN, LOG_S_MAX)
        return logit_pi, mu, log_s

    def dx_params(self, h, click, dt_bin):
        return self.step_params(self.dx_head, torch.cat([h, click[..., None], self.dt_embedding(dt_bin)], -1))

    def dy_params(self, h, click, dt_bin, dx_px):
        return self.step_params(self.dy_head, torch.cat(
            [h, click[..., None], self.dt_embedding(dt_bin), self._dx_encoding(dx_px)], -1))

    def dt_logits(self, h, click):
        return self.dt_head(torch.cat([h, click[..., None]], -1))

    # -- training ---------------------------------------------------------------
    def loss(self, h, targets, mask):
        click_t = targets[..., 3]
        dt_bin = self.dt_bin(targets[..., 2])
        dx_px = torch.round(targets[..., 0] * PIXEL_SCALE)
        dy_px = torch.round(targets[..., 1] * PIXEL_SCALE)
        click_logit = self.click_head(h).squeeze(-1)
        l_click = F.binary_cross_entropy_with_logits(click_logit, click_t, reduction="none")
        l_dt = F.cross_entropy(self.dt_logits(h, click_t).transpose(1, 2), dt_bin, reduction="none")
        l_dx = -discretised_logistic_mixture_log_prob(dx_px, *self.dx_params(h, click_t, dt_bin))
        l_dy = -discretised_logistic_mixture_log_prob(dy_px, *self.dy_params(h, click_t, dt_bin, dx_px))
        denom = mask.sum().clamp(min=1)
        parts = {k: (v * mask).sum() / denom for k, v in
                 (("nll_xy", l_dx + l_dy), ("nll_dt", l_dt), ("click_bce", l_click))}
        total = parts["nll_xy"] + parts["nll_dt"] + parts["click_bce"]
        return total, {k: v.item() for k, v in parts.items()}

    # -- sampling ---------------------------------------------------------------
    @torch.no_grad()
    def sample_step(self, h, temperature=1.0, dt_temperature=1.0, click_temperature=1.0,
                    generator=None):
        """Sample (scaled dx, dy, log dt, click) from hidden states h (..., H)."""
        p_click = torch.sigmoid(self.click_head(h).squeeze(-1) / max(click_temperature, 1e-3))
        click = torch.rand(p_click.shape, device=h.device, generator=generator) < p_click
        click_f = click.float()
        probs = F.softmax(self.dt_logits(h, click_f) / max(dt_temperature, 1e-3), -1)
        dt_bin = torch.multinomial(probs.reshape(-1, self.dt_bins), 1, generator=generator)
        dt_bin = dt_bin.reshape(probs.shape[:-1])
        lo = torch.cat([torch.full((1,), math.log(MIN_DT_MS), device=h.device), self.dt_edges])[dt_bin]
        hi = torch.cat([self.dt_edges, torch.full((1,), math.log(MAX_DT_MS), device=h.device)])[dt_bin]
        log_dt = lo + (hi - lo) * torch.rand(lo.shape, device=h.device, generator=generator)

        dx_px = sample_discretised_logistic_mixture(*self.dx_params(h, click_f, dt_bin), temperature, generator)
        dy_px = sample_discretised_logistic_mixture(*self.dy_params(h, click_f, dt_bin, dx_px), temperature, generator)
        return dx_px / PIXEL_SCALE, dy_px / PIXEL_SCALE, log_dt, click


# ----------------------------------------------------------------------------
# discretised logistic mixture over integers (Salimans et al., PixelCNN++)
# ----------------------------------------------------------------------------

STEP_UNIT = 10.0          # network outputs mu and log s in units of 10 px
LOG_S_MIN = math.log(0.03)  # a scale this small puts ~all mass on one integer
LOG_S_MAX = math.log(500.0)
MAX_STEP_PX = 3000.0


def discretised_logistic_mixture_log_prob(x, logit_pi, mu, log_s):
    """log p(x) for integer x (...,) under a mixture with params (..., K)."""
    x = x[..., None]
    inv_s = torch.exp(-log_s)
    plus = (x + 0.5 - mu) * inv_s
    minus = (x - 0.5 - mu) * inv_s
    cdf_delta = torch.sigmoid(plus) - torch.sigmoid(minus)
    # Where the bin's mass underflows, use the density at the bin centre.
    mid = (x - mu) * inv_s
    log_pdf_mid = mid - log_s - 2.0 * F.softplus(mid)
    log_prob = torch.where(cdf_delta > 1e-5, torch.log(cdf_delta.clamp(min=1e-12)), log_pdf_mid)
    return torch.logsumexp(F.log_softmax(logit_pi, -1) + log_prob, dim=-1)


def sample_discretised_logistic_mixture(logit_pi, mu, log_s, temperature=1.0, generator=None):
    """Sample integers (...,) from mixture params (..., K)."""
    tau = max(temperature, 1e-3)
    k = logit_pi.shape[-1]
    pi = F.softmax(logit_pi / tau, -1)
    comp = torch.multinomial(pi.reshape(-1, k), 1, generator=generator).reshape(*pi.shape[:-1], 1)
    mu_c = mu.gather(-1, comp).squeeze(-1)
    s_c = log_s.gather(-1, comp).squeeze(-1).exp() * tau
    u = torch.rand(mu_c.shape, device=mu.device, generator=generator).clamp(1e-5, 1 - 1e-5)
    x = mu_c + s_c * (torch.log(u) - torch.log1p(-u))
    return torch.round(x).clamp(-MAX_STEP_PX, MAX_STEP_PX)
