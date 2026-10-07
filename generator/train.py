"""Train the trajectory model.

    python -m generator.train --db data/mouse.sqlite3 --epochs 30
"""

import argparse
import json
import math
import time
from pathlib import Path

import numpy as np
import torch

from .data import (MIN_DT_MS, PIXEL_SCALE, STEP_FEATURES, BucketSampler, StepDataset,
                   assign_local_profiles, collate, load_segments, session_profiles,
                   split_by_session, timing_profile)
from .model import MouseModel, make_dt_edges


def mix_in_sampled_steps(model, inputs, mask, prob):
    """Two-pass scheduled sampling: replace the previous-step features of a
    random subset of positions with a step sampled from the model's own
    teacher-forced prediction at the preceding position."""
    was_training = model.training
    model.eval()
    with torch.no_grad():
        h, _ = model(inputs)
        dx, dy, log_dt, click = model.sample_step(h, temperature=1.0)
        dx_px, dy_px = dx * PIXEL_SCALE, dy * PIXEL_SCALE
        dt_ms = torch.exp(log_dt).clamp(MIN_DT_MS, 5000.0)
        sampled = torch.stack([torch.asinh(dx_px / PIXEL_SCALE), torch.asinh(dy_px / PIXEL_SCALE),
                               torch.log(dt_ms) / 3.0, click.float()], -1)
        choose = (torch.rand(mask.shape, device=inputs.device) < prob) & mask.bool()
        choose[:, 0] = False
        mixed = inputs.clone()
        mixed[:, 1:, :STEP_FEATURES] = torch.where(choose[:, 1:, None], sampled[:, :-1], inputs[:, 1:, :STEP_FEATURES])
    model.train(was_training)
    return mixed


def evaluate(model, loader, device):
    model.eval()
    total = 0.0
    parts = {"nll_xy": 0.0, "nll_dt": 0.0, "click_bce": 0.0}
    steps = 0.0
    with torch.no_grad():
        for inputs, targets, mask in loader:
            inputs, targets, mask = inputs.to(device), targets.to(device), mask.to(device)
            out, _ = model(inputs)
            loss, info = model.loss(out, targets, mask)
            n = mask.sum().item()
            total += loss.item() * n
            for k in parts:
                parts[k] += info[k] * n
            steps += n
    model.train()
    return total / steps, {k: v / steps for k, v in parts.items()}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/mouse.sqlite3")
    parser.add_argument("--out", default="models/mouse_gru.pt")
    parser.add_argument("--epochs", type=int, default=30)
    parser.add_argument("--batch-size", type=int, default=128)
    parser.add_argument("--lr", type=float, default=1e-3)
    parser.add_argument("--hidden", type=int, default=512)
    parser.add_argument("--layers", type=int, default=3)
    parser.add_argument("--mixtures", type=int, default=20)
    parser.add_argument("--dt-bins", type=int, default=64, help="quantile bins for the time gap")
    parser.add_argument("--dropout", type=float, default=0.1)
    parser.add_argument("--scheduled-sampling-fraction", type=float, default=0.3,
                        help="fraction of epochs at the end that use scheduled sampling (0 disables)")
    parser.add_argument("--scheduled-sampling-max", type=float, default=0.3,
                        help="probability, reached at the last epoch, that an input step is the model's own sample")
    parser.add_argument("--holdout", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    torch.manual_seed(args.seed)
    np.random.seed(args.seed)
    device = "cuda" if torch.cuda.is_available() else "cpu"

    segments = load_segments(args.db)
    train_segments, val_segments = split_by_session(segments, args.holdout, args.seed)
    print(f"segments: {len(segments)} total, {len(train_segments)} train, {len(val_segments)} val")
    # Each segment is conditioned on a timing profile measured from a window
    # of its own session; the pooled training profile is the fallback.
    profiles = session_profiles(train_segments)
    pooled = timing_profile(np.concatenate([s.steps[:-1, 2] for s in train_segments]))
    rng = np.random.default_rng(args.seed)
    assign_local_profiles(train_segments, pooled, rng)
    assign_local_profiles(val_segments, pooled, rng)
    print(f"timing profiles: {len(profiles)} training sessions, local windows per segment")
    train_set, val_set = StepDataset(train_segments), StepDataset(val_segments)
    print(f"steps: {sum(train_set.lengths)} train, {sum(val_set.lengths)} val")

    train_loader = torch.utils.data.DataLoader(
        train_set, batch_sampler=BucketSampler(train_set.lengths, args.batch_size, True, args.seed),
        collate_fn=collate)
    val_loader = torch.utils.data.DataLoader(
        val_set, batch_sampler=BucketSampler(val_set.lengths, args.batch_size, False),
        collate_fn=collate)

    dt_edges = make_dt_edges(np.concatenate([s.steps[:, 2] for s in train_segments]), args.dt_bins)
    print(f"time-gap bins: {len(dt_edges) + 1}")
    model = MouseModel(args.hidden, args.layers, args.mixtures, args.dropout, dt_edges).to(device)
    print(f"parameters: {sum(p.numel() for p in model.parameters()) / 1e6:.2f}M on {device}")
    optimizer = torch.optim.AdamW(model.parameters(), lr=args.lr, weight_decay=0.01)
    total_updates = args.epochs * len(train_loader)
    warmup = min(500, total_updates // 10)

    def lr_at(update):
        if update < warmup:
            return args.lr * (update + 1) / warmup
        progress = (update - warmup) / max(1, total_updates - warmup)
        return args.lr * (0.02 + 0.98 * 0.5 * (1 + math.cos(math.pi * progress)))

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    history = []
    best = float("inf")
    update = 0
    for epoch in range(1, args.epochs + 1):
        start = time.time()
        running = 0.0
        count = 0
        # Scheduled sampling: in the final part of training, some inputs carry
        # the model's own sampled previous step instead of the recorded one, so
        # free-running generation behaves like teacher-forced prediction.
        ss_start = args.epochs * (1 - args.scheduled_sampling_fraction)
        ss_prob = 0.0
        if args.scheduled_sampling_fraction > 0 and epoch > ss_start:
            ss_prob = args.scheduled_sampling_max * (epoch - ss_start) / max(1, args.epochs - ss_start)
        for inputs, targets, mask in train_loader:
            for group in optimizer.param_groups:
                group["lr"] = lr_at(update)
            inputs, targets, mask = inputs.to(device), targets.to(device), mask.to(device)
            if ss_prob > 0:
                inputs = mix_in_sampled_steps(model, inputs, mask, ss_prob)
            out, _ = model(inputs)
            loss, _ = model.loss(out, targets, mask)
            optimizer.zero_grad(set_to_none=True)
            loss.backward()
            torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
            optimizer.step()
            running += loss.item()
            count += 1
            update += 1
        val_loss, parts = evaluate(model, val_loader, device)
        record = {"epoch": epoch, "train_loss": running / count, "val_loss": val_loss,
                  **parts, "seconds": time.time() - start}
        history.append(record)
        print(json.dumps(record))
        if val_loss < best:
            best = val_loss
            torch.save({"model": model.state_dict(), "config": model.config,
                        "epoch": epoch, "val_loss": val_loss,
                        "pooled_profile": pooled.tolist(),
                        "session_profiles": {int(k): v.tolist() for k, v in profiles.items()}},
                       out_path)
    with open(out_path.with_suffix(".history.json"), "w", encoding="utf-8") as handle:
        json.dump(history, handle, indent=1)
    print(f"best val loss {best:.4f}; saved {out_path}")


if __name__ == "__main__":
    main()
