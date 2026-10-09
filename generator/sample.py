"""Generate mouse trajectories from a trained model."""

from pathlib import Path

import numpy as np
import torch

from .data import (DEFAULT_PROFILE, PIXEL_SCALE, PROFILE_FEATURES, STEP_FEATURES,
                   context_features, encode_step, sample_styles, timing_profile)
from .model import MouseModel


def load_model(path, device=None):
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    checkpoint = torch.load(path, map_location=device, weights_only=False)
    model = MouseModel(**checkpoint["config"]).to(device)
    model.load_state_dict(checkpoint["model"])
    model.eval()
    model.pooled_profile = np.array(checkpoint.get("pooled_profile", DEFAULT_PROFILE), np.float32)
    model.session_profiles = {k: np.array(v, np.float32)
                              for k, v in checkpoint.get("session_profiles", {}).items()}
    model.style_table = np.array(checkpoint.get("style_table", []), np.float32)
    return model


def profile_from_recording(db_path):
    """Timing profile of a machine from a recording made on it (any length of
    use; a few minutes of ordinary mouse work is enough)."""
    from .data import load_segments
    segments = load_segments(db_path)
    profile = timing_profile(np.concatenate([s.steps[:-1, 2] for s in segments]))
    if profile is None:
        raise ValueError("recording has too few move events to measure a timing profile")
    return profile


@torch.no_grad()
def generate_batch(model, starts, targets, temperature=0.8, dt_temperature=1.0,
                   click_temperature=1.0, max_steps=600, seed=None, device=None, profile=None,
                   style=None):
    """Sample one trajectory per (start, target) pair.

    temperature scales the step-displacement mixture, dt_temperature the time
    gap distribution and click_temperature the click decision. profile is the
    timing profile to reproduce: one (PROFILE_FEATURES,) vector for all, or
    one per pair; default is the pooled training profile. style, for a model
    trained with --style, is one (STYLE_FEATURES,) vector or one per pair
    (see data.style_features); by default each pair draws the style of a
    training segment with a similar displacement.

    Returns a list of float arrays with columns (t_ms, x, y, click); each begins
    at the start position at t=0 and ends with the click step. A trajectory
    that never clicked within max_steps ends without a click row.
    """
    device = device or next(model.parameters()).device
    starts = np.asarray(starts, dtype=np.float32).reshape(-1, 2)
    targets = np.asarray(targets, dtype=np.float32).reshape(-1, 2)
    batch = len(starts)
    if profile is None:
        profile = getattr(model, "pooled_profile", DEFAULT_PROFILE)
    profile = np.broadcast_to(np.asarray(profile, np.float32).reshape(-1, PROFILE_FEATURES),
                              (batch, PROFILE_FEATURES))
    conditioning = profile
    if model.style_features:
        if style is None:
            style = sample_styles(model.style_table, np.hypot(*(targets - starts).T),
                                  np.random.default_rng(seed))
        style = np.broadcast_to(np.asarray(style, np.float32).reshape(-1, model.style_features),
                                (batch, model.style_features))
        conditioning = np.concatenate([profile, style], -1)
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
        inputs = torch.from_numpy(np.concatenate([prev, context, conditioning], -1)).to(device)[:, None, :]
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
