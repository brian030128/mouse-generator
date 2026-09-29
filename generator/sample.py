"""Generate mouse trajectories from a trained model."""

from pathlib import Path

import numpy as np
import torch

from .data import PIXEL_SCALE, STEP_FEATURES, context_features, encode_step
from .model import MouseModel


def load_model(path, device=None):
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    checkpoint = torch.load(path, map_location=device, weights_only=False)
    model = MouseModel(**checkpoint["config"]).to(device)
    model.load_state_dict(checkpoint["model"])
    model.eval()
    return model


@torch.no_grad()
def generate_batch(model, starts, targets, temperature=0.8, dt_temperature=1.0,
                   click_temperature=1.0, max_steps=600, seed=None, device=None):
    """Sample one trajectory per (start, target) pair.

    temperature scales the step-displacement mixture, dt_temperature the time
    gap distribution and click_temperature the click decision.

    Returns a list of float arrays with columns (t_ms, x, y, click); each begins
    at the start position at t=0 and ends with the click step. A trajectory
    that never clicked within max_steps ends without a click row.
    """
    device = device or next(model.parameters()).device
    starts = np.asarray(starts, dtype=np.float32).reshape(-1, 2)
    targets = np.asarray(targets, dtype=np.float32).reshape(-1, 2)
    batch = len(starts)
    generator = None
    if seed is not None:
        generator = torch.Generator(device=device).manual_seed(seed)

    position = starts.copy()
    elapsed = np.zeros(batch, np.float32)
    prev = np.zeros((batch, STEP_FEATURES), np.float32)
    done = np.zeros(batch, bool)
    state = None
    rows = [[(0.0, float(x), float(y), 0.0)] for x, y in starts]

    for step in range(max_steps):
        is_start = np.full(batch, 1.0 if step == 0 else 0.0, np.float32)
        context = context_features(targets - position, elapsed, is_start)
        inputs = torch.from_numpy(np.concatenate([prev, context], -1)).to(device)[:, None, :]
        h, state = model(inputs, state)
        dx, dy, log_dt, click = model.sample_step(h[:, 0], temperature, dt_temperature,
                                                  click_temperature, generator)
        dx = dx.cpu().numpy() * PIXEL_SCALE
        dy = dy.cpu().numpy() * PIXEL_SCALE
        dt = np.exp(log_dt.cpu().numpy().clip(-1.0, 8.6)).clip(0.5, 5000.0)
        click = click.cpu().numpy()
        # Events are integer pixels; round the accumulated position, not the step.
        new_position = np.round(position + np.stack([dx, dy], -1))
        actual = new_position - position
        for i in np.nonzero(~done)[0]:
            rows[i].append((float(elapsed[i] + dt[i]), float(new_position[i, 0]),
                            float(new_position[i, 1]), float(click[i])))
        position = np.where(done[:, None], position, new_position)
        elapsed = np.where(done, elapsed, elapsed + dt)
        steps = np.stack([actual[:, 0], actual[:, 1], dt, click.astype(np.float32)], -1)
        prev = encode_step(steps)
        done |= click
        if done.all():
            break
    return [np.array(r, dtype=np.float32) for r in rows]


def generate(model, start, target, **kwargs):
    """Sample a single trajectory; see generate_batch."""
    return generate_batch(model, [start], [target], **kwargs)[0]


def default_checkpoint():
    return Path(__file__).resolve().parent.parent / "models" / "mouse_gru.pt"
