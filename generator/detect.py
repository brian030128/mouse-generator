"""Population detector test: can a detector trained on known humans and known
generator output flag the generator on a person it has never seen?

    python -m generator.detect --db data/mouse.sqlite3 --gru models/mouse_gru.pt

generator.evaluate asks whether generated moves can be told from one held-out
session. Two real sessions of the same person are also easy to tell apart
there, so that test has a floor well above 0.5. Here the detectors are trained
on real moves from the training sessions against generator moves for the same
start and target points, then scored on the held-out session's real moves
against fresh generator moves for its targets. A perfect generator leaves
nothing to learn and scores 0.5.

Two detectors run: gradient boosting on the per-move summary features of
generator.evaluate, and a 1D CNN that reads each move's raw events (step and
time gap in a start-to-click frame). Per-move scores are also averaged over
bags of consecutive moves, as an account-level check would, so a weak
per-move signal that adds up shows as a rising bag AUC.

For scale, the same pipeline runs with one real training session standing in
for the generator (held out of the human side): how quickly a different
real person, or the same person on another day, gets flagged.
"""

import argparse
import json
from pathlib import Path

import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from .data import load_segments, session_profiles, split_by_session
from .evaluate import FEATURE_NAMES, real_to_rows, shape_features

MAX_EVENTS = 256
BAGS = (1, 5, 20, 50)
# Summary features grouped by what they describe, for explain(). Correlated
# features share credit under single-feature permutation, so groups are
# shuffled together. dt bins are log-ms bands: <4.5, 4.5-6.7, 6.7-8.2,
# 8.2-12, 12-33, 33-100, 100-400, >400 ms.
FEATURE_GROUPS = {
    "event timing (gaps between events)": [f"dt_bin{i}" for i in range(8)] + ["dt_median", "dt_max"],
    "duration and event count": ["log_duration", "log_steps"],
    "speed": ["speed_max", "speed_mean", "speed_std", "speed_median"],
    "acceleration (speed changes)": ["acc_max", "acc_mean"],
    "position over time (progress toward target)": [f"x{i}" for i in range(24)] + [f"y{i}" for i in range(24)],
    "directness and detours": ["efficiency", "lateral_max", "x_min", "x_max"],
    "turning": ["turn_mean", "turn_max", "sharp_turn_frac"],
    "standing still (zero-length steps)": ["zero_step_frac"],
    "when peak speed happens": ["peak_pos"],
}


# ----------------------------------------------------------------------------
# sequence detector
# ----------------------------------------------------------------------------

def sequence_input(rows):
    """(MAX_EVENTS, 4) events in the start-to-click frame, last events kept.

    Channels: asinh-scaled step along and across the start-to-click line,
    log time gap, and a validity flag (0 for padding).
    """
    xy = rows[:, 1:3].astype(np.float64)
    d = xy[-1] - xy[0]
    disp = max(float(np.hypot(*d)), 1.0)
    c, s = d[0] / disp, d[1] / disp
    step = np.diff(xy, axis=0) @ np.array([[c, -s], [s, c]])
    dt = np.maximum(np.diff(rows[:, 0]), 0.5)
    feats = np.column_stack([np.arcsinh(step / 10.0), np.log(dt) / 3.0, np.ones(len(dt))])[-MAX_EVENTS:]
    out = np.zeros((MAX_EVENTS, 4), np.float32)
    out[:len(feats)] = feats
    return out


class SequenceDetector(nn.Module):
    def __init__(self, width=64):
        super().__init__()
        self.convs = nn.ModuleList([nn.Conv1d(4, width, 5, padding=2),
                                    nn.Conv1d(width, width, 5, padding=2, dilation=1),
                                    nn.Conv1d(width, width, 5, padding=4, dilation=2),
                                    nn.Conv1d(width, width, 5, padding=8, dilation=4)])
        self.head = nn.Sequential(nn.Linear(2 * width + 1, width), nn.GELU(), nn.Linear(width, 1))

    def forward(self, x, log_disp):
        mask = x[..., 3:4].transpose(1, 2)                      # (B, 1, T)
        h = x.transpose(1, 2)
        for conv in self.convs:
            h = F.gelu(conv(h)) * mask
        mean = h.sum(-1) / mask.sum(-1).clamp(min=1)
        peak = (h - 1e4 * (1 - mask)).amax(-1)
        return self.head(torch.cat([mean, peak, log_disp[:, None]], -1)).squeeze(-1)


def train_sequence_detector(x, log_disp, y, seed=0, epochs=12, device=None):
    device = device or ("cuda" if torch.cuda.is_available() else "cpu")
    torch.manual_seed(seed)
    model = SequenceDetector().to(device)
    opt = torch.optim.AdamW(model.parameters(), lr=2e-3, weight_decay=1e-2)
    x, log_disp, y = (torch.tensor(a, dtype=torch.float32) for a in (x, log_disp, y))
    rng = np.random.default_rng(seed)
    order = rng.permutation(len(y))
    val, fit = order[:len(y) // 10], order[len(y) // 10:]
    best, state = float("inf"), None
    for _ in range(epochs):
        model.train()
        shuffled = fit[rng.permutation(len(fit))]
        for i in range(0, len(shuffled), 256):
            b = shuffled[i:i + 256]
            loss = F.binary_cross_entropy_with_logits(
                model(x[b].to(device), log_disp[b].to(device)), y[b].to(device))
            opt.zero_grad()
            loss.backward()
            opt.step()
        model.eval()
        with torch.no_grad():
            v = F.binary_cross_entropy_with_logits(
                model(x[val].to(device), log_disp[val].to(device)), y[val].to(device)).item()
        if v < best:
            best, state = v, {k: t.detach().clone() for k, t in model.state_dict().items()}
    model.load_state_dict(state)
    model.eval()
    return model


def sequence_logits(model, x, log_disp):
    device = next(model.parameters()).device
    out = []
    with torch.no_grad():
        for i in range(0, len(x), 1024):
            out.append(model(torch.tensor(x[i:i + 1024], device=device),
                             torch.tensor(log_disp[i:i + 1024], dtype=torch.float32, device=device)).cpu().numpy())
    return np.concatenate(out)


# ----------------------------------------------------------------------------
# scoring
# ----------------------------------------------------------------------------

def bag_auc(scores_human, scores_bot, size):
    """AUC over bags of `size` consecutive moves, scored by their mean."""
    from sklearn.metrics import roc_auc_score

    def bags(s):
        n = len(s) // size
        return np.asarray(s[:n * size]).reshape(n, size).mean(1)
    h, b = bags(scores_human), bags(scores_bot)
    if len(h) < 2 or len(b) < 2:
        return float("nan")
    return float(roc_auc_score(np.r_[np.zeros(len(h)), np.ones(len(b))], np.r_[h, b]))


def run_detectors(train_human, train_bot, test_human, test_bot, seed=0, explain_top=0):
    """Train both detectors on (human, bot) row lists; score the test lists.

    Returns {detector: {"bag_<k>": AUC}} with bags of consecutive moves, plus
    "explain" (see explain()) when explain_top > 0.
    """
    from sklearn.ensemble import HistGradientBoostingClassifier

    def summary(rows):
        x = np.stack([shape_features(r, r[-1, 1:3]) for r in rows])
        return np.nan_to_num(x, nan=0.0, posinf=1e6, neginf=-1e6)

    def seq(rows):
        return (np.stack([sequence_input(r) for r in rows]),
                np.array([np.log1p(np.hypot(*(r[-1, 1:3] - r[0, 1:3]))) for r in rows], np.float32))

    y = np.r_[np.zeros(len(train_human)), np.ones(len(train_bot))]
    gbm = HistGradientBoostingClassifier(random_state=seed).fit(summary(train_human + train_bot), y)
    margin = lambda rows: gbm.decision_function(summary(rows))
    xs, ds = seq(train_human + train_bot)
    cnn = train_sequence_detector(xs, ds, y, seed)
    scores = {"gbm": (margin(test_human), margin(test_bot)),
              "cnn": (sequence_logits(cnn, *seq(test_human)), sequence_logits(cnn, *seq(test_bot)))}
    out = {name: {f"bag_{k}": bag_auc(h, b, k) for k in BAGS} for name, (h, b) in scores.items()}
    if explain_top:
        out["explain"] = explain(gbm, summary(test_human), summary(test_bot), explain_top, seed)
    return out


def explain(gbm, x_human, x_bot, top=12, seed=0, repeats=3):
    """What the gradient-boosted detector relies on, on the test moves.

    importance = drop in per-move AUC when a feature (or a group of related
    features) is shuffled across the test moves; larger means the detector
    leans on it more. For single features the human and bot 10th/50th/90th
    percentiles show which way the bot is off.
    """
    from sklearn.metrics import roc_auc_score
    x = np.concatenate([x_human, x_bot])
    y = np.r_[np.zeros(len(x_human)), np.ones(len(x_bot))]
    base = roc_auc_score(y, gbm.decision_function(x))
    rng = np.random.default_rng(seed)

    def drop(columns):
        losses = []
        for _ in range(repeats):
            shuffled = x.copy()
            perm = rng.permutation(len(x))
            shuffled[:, columns] = x[perm][:, columns]
            losses.append(base - roc_auc_score(y, gbm.decision_function(shuffled)))
        return float(np.mean(losses))

    index = {n: i for i, n in enumerate(FEATURE_NAMES)}
    groups = sorted(((name, drop([index[f] for f in feats])) for name, feats in FEATURE_GROUPS.items()),
                    key=lambda g: -g[1])
    singles = sorted(((n, drop([i])) for n, i in index.items() if n != "log_disp"), key=lambda f: -f[1])[:top]
    pct = lambda v: [float(q) for q in np.percentile(v, [10, 50, 90])]
    return {"auc": float(base),
            "groups": [{"group": g, "importance": v} for g, v in groups],
            "features": [{"feature": n, "importance": v, "human_p10_50_90": pct(x_human[:, index[n]]),
                          "bot_p10_50_90": pct(x_bot[:, index[n]])} for n, v in singles]}


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--db", default="data/mouse.sqlite3")
    parser.add_argument("--gru", default="models/mouse_gru.pt")
    parser.add_argument("--temperature", type=float, default=0.8)
    parser.add_argument("--holdout", type=float, default=0.1, help="must match the GRU's training holdout")
    parser.add_argument("--train-moves", type=int, default=8000)
    parser.add_argument("--test-moves", type=int, default=3000)
    parser.add_argument("--reference-sessions", type=int, default=2,
                        help="how many of the largest training sessions to run as stand-in bots")
    parser.add_argument("--out", default="models/comparison/detect.json")
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    from .sample import generate_batch, load_model

    rng = np.random.default_rng(args.seed)
    segments = load_segments(args.db)
    train, held = split_by_session(segments, args.holdout, args.seed)
    moving = lambda s: np.hypot(*s.steps[:, :2].sum(0)) >= 3
    train = [s for s in train if moving(s)]
    held = [s for s in held if moving(s)]

    def pick(pool, n):
        """n segments, kept in recording order so bags hold consecutive moves."""
        idx = np.sort(rng.choice(len(pool), min(n, len(pool)), replace=False))
        return [pool[i] for i in idx]

    model = load_model(args.gru)
    profiles = {**model.session_profiles, **session_profiles(held)}

    def generated_for(segs, seed):
        rows = [real_to_rows(s) for s in segs]
        starts = np.array([r[0, 1:3] for r in rows])
        targets = np.array([r[-1, 1:3] for r in rows])
        prof = np.stack([profiles.get(s.session_id, model.pooled_profile) for s in segs])
        out = []
        for i in range(0, len(segs), 512):
            out.extend(generate_batch(model, starts[i:i + 512], targets[i:i + 512], temperature=args.temperature,
                                      seed=seed + i, profile=prof[i:i + 512]))
        return out

    test_segs = pick(held, args.test_moves)
    test_human = [real_to_rows(s) for s in test_segs]
    results = {"held_out_sessions": sorted({int(s.session_id) for s in held}),
               "test_moves": len(test_human), "temperature": args.temperature}

    # Generator as the bot: humans and generator moves share start and target points.
    train_segs = pick(train, args.train_moves)
    print(f"generator: training detectors on {len(train_segs)} real + generated moves")
    results["generator"] = run_detectors([real_to_rows(s) for s in train_segs], generated_for(train_segs, 1),
                                         test_human, generated_for(test_segs, 2), args.seed, explain_top=12)

    # Real sessions as stand-in bots: each is removed from the human side and
    # split in half (first half trains the detector, second half is tested).
    sizes = {}
    for s in train:
        sizes[s.session_id] = sizes.get(s.session_id, 0) + 1
    for sid in sorted(sizes, key=lambda k: -sizes[k])[:args.reference_sessions]:
        bot = [s for s in train if s.session_id == sid]
        half = len(bot) // 2
        humans = pick([s for s in train if s.session_id != sid], args.train_moves)
        bot_train = [bot[i] for i in np.sort(rng.choice(half, min(args.train_moves, half), replace=False))]
        rest = bot[half:]
        bot_test = [rest[i] for i in np.sort(rng.choice(len(rest), min(args.test_moves, len(rest)), replace=False))]
        print(f"reference: session {sid} as the bot")
        results[f"session_{sid}"] = run_detectors([real_to_rows(s) for s in humans],
                                                  [real_to_rows(s) for s in bot_train],
                                                  test_human, [real_to_rows(s) for s in bot_test], args.seed)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(results, indent=1), encoding="utf-8")
    print(f"held-out sessions {results['held_out_sessions']}, {len(test_human)} test moves per side")
    print("detector AUC on unseen people (0.5 = cannot flag); columns: moves scored together")
    print(f"{'bot':<14}{'detector':<10}" + "".join(f"{k:>8}" for k in BAGS))
    for name, res in results.items():
        if not isinstance(res, dict):
            continue
        for det, aucs in res.items():
            if det == "explain":
                continue
            print(f"{name:<14}{det:<10}" + "".join(f"{aucs[f'bag_{k}']:>8.3f}" for k in BAGS))
    ex = results["generator"]["explain"]
    print(f"what gives the generator away (drop in per-move AUC {ex['auc']:.3f} when shuffled):")
    for g in ex["groups"]:
        print(f"  {g['importance']:6.3f}  {g['group']}")
    print(f"{'feature':<18}{'importance':>11}  {'real p10 / median / p90':>28}  {'generator p10 / median / p90':>30}")
    for f in ex["features"]:
        fmt = lambda v: " / ".join(f"{q:.3g}" for q in v)
        print(f"{f['feature']:<18}{f['importance']:>11.3f}  {fmt(f['human_p10_50_90']):>28}  {fmt(f['bot_p10_50_90']):>30}")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
