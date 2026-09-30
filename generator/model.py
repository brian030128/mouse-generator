"""GRU that emits one mouse event per step.

Per step the head factorises the event as
    p(click | h) * p(dt | h, click) * p(dx, dy | h, click, dt)
where dt is a categorical over fine bins of the recorded gap distribution
(most gaps sit within 0.3 ms of the 7.5 ms poll interval, with jitter and
pauses in the tails; a Gaussian mixture on log dt smears that structure) and
(dx, dy) is a mixture of bivariate Gaussians as in SketchRNN.
"""

import math

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .data import INPUT_FEATURES, MIN_DT_MS

LOG_2PI = math.log(2 * math.pi)
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
    def __init__(self, hidden=512, layers=3, mixtures=20, dropout=0.1, dt_edges=None, dt_embed=32):
        super().__init__()
        if dt_edges is None:
            dt_edges = make_dt_edges(np.array([7.5]), 1)
        self.config = dict(hidden=hidden, layers=layers, mixtures=mixtures, dropout=dropout,
                           dt_edges=list(dt_edges), dt_embed=dt_embed)
        self.mixtures = mixtures
        self.register_buffer("dt_edges", torch.tensor(dt_edges, dtype=torch.float32))
        self.dt_bins = len(dt_edges) + 1
        self.embed = nn.Sequential(nn.Linear(INPUT_FEATURES, hidden), nn.GELU())
        self.rnn = nn.GRU(hidden, hidden, layers, batch_first=True,
                          dropout=dropout if layers > 1 else 0.0)
        self.click_head = nn.Linear(hidden, 1)
        self.dt_head = nn.Sequential(nn.Linear(hidden + 1, hidden // 2), nn.GELU(),
                                     nn.Linear(hidden // 2, self.dt_bins))
        self.dt_embedding = nn.Embedding(self.dt_bins, dt_embed)
        # per mixture: logit, mu_x, mu_y, log_sx, log_sy, rho -> 6
        self.xy_head = nn.Sequential(nn.Linear(hidden + 1 + dt_embed, hidden), nn.GELU(),
                                     nn.Linear(hidden, mixtures * 6))

    def forward(self, inputs, state=None):
        h, state = self.rnn(self.embed(inputs), state)
        return h, state

    # -- pieces ---------------------------------------------------------------
    def dt_bin(self, log_dt):
        return torch.bucketize(log_dt.contiguous(), self.dt_edges)

    def xy_params(self, h, click, dt_bin):
        out = self.xy_head(torch.cat([h, click[..., None], self.dt_embedding(dt_bin)], -1))
        params = out.reshape(*out.shape[:-1], self.mixtures, 6)
        logit_pi = params[..., 0]
        mu = params[..., 1:3]
        log_s = params[..., 3:5].clamp(-7, 5)
        rho = torch.tanh(params[..., 5]) * 0.99
        return logit_pi, mu, log_s, rho

    def dt_logits(self, h, click):
        return self.dt_head(torch.cat([h, click[..., None]], -1))

    # -- training ---------------------------------------------------------------
    def loss(self, h, targets, mask):
        click_t = targets[..., 3]
        dt_bin = self.dt_bin(targets[..., 2])
        click_logit = self.click_head(h).squeeze(-1)
        l_click = F.binary_cross_entropy_with_logits(click_logit, click_t, reduction="none")
        l_dt = F.cross_entropy(self.dt_logits(h, click_t).transpose(1, 2), dt_bin, reduction="none")
        logit_pi, mu, log_s, rho = self.xy_params(h, click_t, dt_bin)
        z = (targets[..., None, 0:2] - mu) / log_s.exp()
        zx, zy = z[..., 0], z[..., 1]
        one_m_rho2 = 1 - rho ** 2
        log_xy = -(zx ** 2 + zy ** 2 - 2 * rho * zx * zy) / (2 * one_m_rho2) \
            - log_s.sum(-1) - 0.5 * torch.log(one_m_rho2) - LOG_2PI
        l_xy = -torch.logsumexp(F.log_softmax(logit_pi, -1) + log_xy, dim=-1)
        denom = mask.sum().clamp(min=1)
        parts = {k: (v * mask).sum() / denom for k, v in
                 (("nll_xy", l_xy), ("nll_dt", l_dt), ("click_bce", l_click))}
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

        tau = max(temperature, 1e-3)
        logit_pi, mu, log_s, rho = self.xy_params(h, click_f, dt_bin)
        pi = F.softmax(logit_pi / tau, -1)
        comp = torch.multinomial(pi.reshape(-1, self.mixtures), 1, generator=generator)
        comp = comp.reshape(*pi.shape[:-1], 1)

        def pick(v):
            return v.gather(-1, comp).squeeze(-1)

        def noise():
            return torch.randn(comp.shape[:-1], device=h.device, generator=generator)

        sx = pick(log_s[..., 0]).exp() * math.sqrt(tau)
        sy = pick(log_s[..., 1]).exp() * math.sqrt(tau)
        r = pick(rho)
        e1, e2 = noise(), noise()
        dx = pick(mu[..., 0]) + sx * e1
        dy = pick(mu[..., 1]) + sy * (r * e1 + torch.sqrt(1 - r ** 2) * e2)
        return dx, dy, log_dt, click
