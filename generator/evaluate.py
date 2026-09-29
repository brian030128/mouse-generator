"""Compare generated trajectories with held-out real ones.

    python -m generator.evaluate --db data/mouse.sqlite3 --gru models/mouse_gru.pt --dmtg models/mouse_dmtg.pt

For every held-out segment each model is asked to travel the same start-to-click
vector. The script compares summary statistics of real and generated
trajectories, trains a real-vs-generated classifier (the white-box test from
the DMTG paper, arXiv:2410.18233) and draws side-by-side figures.
"""

import argparse
import json
from pathlib import Path

import numpy as np

from .data import load_segments, split_by_session


def real_to_rows(segment):
    """Convert a Segment into the (t_ms, x, y, click) row format the samplers emit."""
    steps = segment.steps
    t = np.concatenate([[0.0], np.cumsum(steps[:, 2])])
    xy = np.vstack([segment.start, segment.start + np.cumsum(steps[:, :2], axis=0)])
    click = np.concatenate([[0.0], steps[:, 3]])
    return np.column_stack([t, xy, click]).astype(np.float32)


def describe(rows, target):
    """Per-trajectory statistics. rows: (n, 4) t_ms, x, y, click."""
    xy = rows[:, 1:3]
    t = rows[:, 0]
    clicked = bool(rows[-1, 3] > 0.5)
    disp = float(np.hypot(*(target - xy[0])))
    step = np.diff(xy, axis=0)
    dist = np.hypot(step[:, 0], step[:, 1])
    path = float(dist.sum())
    dt = np.diff(t)
    speed = dist / np.maximum(dt, 1e-3)               # px per ms
    peak = int(np.argmax(speed)) if len(speed) else 0
    return {
        "clicked": clicked,
        "steps": len(rows) - 1,
        "duration_ms": float(t[-1]),
        "displacement": disp,
        "end_error": float(np.hypot(*(target - xy[-1]))),
        "efficiency": path / max(disp, 1.0),
        "peak_speed": float(speed.max()) if len(speed) else 0.0,
        "peak_time_frac": float(t[peak + 1] / t[-1]) if len(speed) and t[-1] > 0 else 0.0,
        "median_dt": float(np.median(dt)) if len(dt) else 0.0,
        "long_pause_frac": float((dt > 100).mean()) if len(dt) else 0.0,
    }


def summarise(stats):
    keys = [k for k in stats[0] if k != "clicked"]
    out = {"count": len(stats), "clicked_frac": float(np.mean([s["clicked"] for s in stats]))}
    for k in keys:
        v = np.array([s[k] for s in stats], dtype=np.float64)
        out[k] = {"p25": float(np.percentile(v, 25)), "median": float(np.median(v)),
                  "p75": float(np.percentile(v, 75)), "mean": float(v.mean())}
    return out


def duration_by_distance(stats, edges=(0, 20, 60, 150, 300, 600, 1200, 5000)):
    disp = np.array([s["displacement"] for s in stats])
    dur = np.array([s["duration_ms"] for s in stats])
    table = []
    for lo, hi in zip(edges[:-1], edges[1:]):
        sel = (disp >= lo) & (disp < hi)
        if sel.sum() >= 5:
            table.append({"range": f"{lo}-{hi}", "n": int(sel.sum()),
                          "median_ms": float(np.median(dur[sel]))})
    return table


# ----------------------------------------------------------------------------
# real-vs-generated classifier
# ----------------------------------------------------------------------------

def shape_features(rows, target, n=24):
    """Features a bot detector would plausibly use.

    The path is resampled uniformly in time to n points, expressed in a frame
    where the start is the origin and the target lies at (1, 0), so the
    classifier sees shape and speed profile rather than screen position.
    """
    xy = rows[:, 1:3] - rows[0, 1:3]
    t = rows[:, 0]
    d = target - rows[0, 1:3]
    disp = max(float(np.hypot(*d)), 1.0)
    c, s = d[0] / disp, d[1] / disp
    rot = np.array([[c, s], [-s, c]])
    local = xy @ rot.T / disp
    duration = max(float(t[-1]), 1.0)
    grid = np.linspace(0, duration, n)
    rx = np.interp(grid, t, local[:, 0])
    ry = np.interp(grid, t, local[:, 1])
    step = np.diff(local, axis=0)
    dist = np.hypot(step[:, 0], step[:, 1]) * disp
    dt = np.maximum(np.diff(t), 1e-3)
    speed = dist / dt
    acc = np.diff(speed) / np.maximum(dt[1:], 1e-3) if len(speed) > 1 else np.zeros(1)
    # Curvature proxy: heading changes between successive steps.
    heading = np.arctan2(step[:, 1], step[:, 0])
    turn = np.abs(np.angle(np.exp(1j * np.diff(heading)))) if len(heading) > 1 else np.zeros(1)
    hist_dt = np.histogram(np.log(dt), bins=[-10, 1.5, 1.9, 2.1, 2.5, 3.5, 4.6, 6, 20])[0] / len(dt)
    stats = [
        np.log(duration), np.log(len(rows)), np.log(disp), dist.sum() / disp,
        speed.max(), speed.mean(), speed.std(), np.median(speed),
        np.abs(acc).max(), np.abs(acc).mean(),
        turn.mean(), turn.max(), (turn > 1.0).mean(),
        np.abs(local[:, 1]).max(), local[:, 0].min(), local[:, 0].max(),
        (dist == 0).mean(), np.median(dt), dt.max(), np.argmax(speed) / max(len(speed), 1),
    ]
    # The distance from the click to the target is left out: a real segment's
    # target is defined as its own click point, so it would be a giveaway.
    return np.concatenate([rx, ry, hist_dt, stats]).astype(np.float32)


FEATURE_NAMES = [f"x{i}" for i in range(24)] + [f"y{i}" for i in range(24)] + [f"dt_bin{i}" for i in range(8)] + [
    "log_duration", "log_steps", "log_disp", "efficiency", "speed_max", "speed_mean", "speed_std",
    "speed_median", "acc_max", "acc_mean", "turn_mean", "turn_max", "sharp_turn_frac", "lateral_max",
    "x_min", "x_max", "zero_step_frac", "dt_median", "dt_max", "peak_pos"]
# Features that depend only on the path's geometry, not on event timing.
SHAPE_FEATURES = [i for i, n in enumerate(FEATURE_NAMES) if n[0] in "xy" and n[1:].isdigit()
                  or n in ("log_disp", "efficiency", "turn_mean", "turn_max", "sharp_turn_frac",
                           "lateral_max", "x_min", "x_max")]


def classifier_test(real_rows, gen_rows, targets, seed=0):
    """5-fold accuracy of a random forest separating real from generated.

    50% means indistinguishable with these features; 100% means trivially
    detectable. Reported for all features and for shape-only features, with
    the most important features of the full model.
    """
    from sklearn.ensemble import RandomForestClassifier
    from sklearn.model_selection import StratifiedKFold, cross_val_predict

    x = np.stack([shape_features(r, t) for r, t in zip(real_rows, targets)] +
                 [shape_features(r, t) for r, t in zip(gen_rows, targets)])
    y = np.concatenate([np.zeros(len(real_rows)), np.ones(len(gen_rows))])
    x = np.nan_to_num(x, nan=0.0, posinf=1e6, neginf=-1e6)

    def run(columns):
        clf = RandomForestClassifier(300, min_samples_leaf=3, n_jobs=-1, random_state=seed)
        cv = StratifiedKFold(5, shuffle=True, random_state=seed)
        pred = cross_val_predict(clf, x[:, columns], y, cv=cv)
        clf.fit(x[:, columns], y)
        return float((pred == y).mean()), clf.feature_importances_

    accuracy, importances = run(list(range(x.shape[1])))
    shape_accuracy, _ = run(SHAPE_FEATURES)
    order = np.argsort(importances)[::-1][:8]
    return {"accuracy": accuracy, "shape_accuracy": shape_accuracy,
            "top_features": [(FEATURE_NAMES[i], float(importances[i])) for i in order]}


# ----------------------------------------------------------------------------
# figures
# ----------------------------------------------------------------------------

def plot(real_rows, generated, targets, path, n=18, seed=0):
    """generated: dict name -> rows list."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    rng = np.random.default_rng(seed)
    disp = np.array([np.hypot(*(t - r[0, 1:3])) for r, t in zip(real_rows, targets)])
    candidates = np.nonzero(disp > 150)[0]
    idx = rng.choice(candidates, size=min(n, len(candidates)), replace=False)
    cols = 6
    rows_n = int(np.ceil(len(idx) / cols))
    fig, axes = plt.subplots(rows_n, cols, figsize=(cols * 3, rows_n * 3))
    colors = ["tab:red", "tab:green", "tab:purple"]
    for ax, i in zip(axes.flat, idx):
        series = [("real", real_rows[i], "tab:blue")] + [
            (name, rows[i], colors[k]) for k, (name, rows) in enumerate(generated.items())]
        title = []
        for label, rows, color in series:
            xy = rows[:, 1:3] - real_rows[i][0, 1:3]
            ax.plot(xy[:, 0], -xy[:, 1], color=color, lw=1, marker=".", ms=2, label=label, alpha=0.85)
            title.append(f"{label} {rows[-1, 0]:.0f}ms")
        ax.scatter([0], [0], color="k", s=12, zorder=3)
        tx, ty = targets[i] - real_rows[i][0, 1:3]
        ax.scatter([tx], [-ty], marker="x", color="k", s=30, zorder=3)
        ax.set_title(" / ".join(title), fontsize=7)
        ax.set_aspect("equal")
        ax.tick_params(labelsize=6)
    for ax in axes.flat[len(idx):]:
        ax.axis("off")
    axes.flat[0].legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


def plot_speed_profiles(real_rows, generated, path, bins=20):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    def profiles(all_rows):
        acc = np.zeros(bins)
        cnt = np.zeros(bins)
        for rows in all_rows:
            if len(rows) < 6 or rows[-1, 0] <= 0:
                continue
            xy = rows[:, 1:3]
            step = np.hypot(*np.diff(xy, axis=0).T)
            dt = np.maximum(np.diff(rows[:, 0]), 1e-3)
            speed = step / dt
            frac = rows[1:, 0] / rows[-1, 0]
            b = np.minimum((frac * bins).astype(int), bins - 1)
            norm = speed / max(speed.max(), 1e-6)
            np.add.at(acc, b, norm)
            np.add.at(cnt, b, 1)
        return acc / np.maximum(cnt, 1)

    fig, ax = plt.subplots(figsize=(6, 3.5))
    x = (np.arange(bins) + 0.5) / bins
    ax.plot(x, profiles(real_rows), label="real", color="tab:blue")
    for (name, rows), color in zip(generated.items(), ["tab:red", "tab:green", "tab:purple"]):
        ax.plot(x, profiles(rows), label=name, color=color)
    ax.set_xlabel("fraction of movement time")
    ax.set_ylabel("speed / peak speed (mean)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(path, dpi=110)
    plt.close(fig)


# ----------------------------------------------------------------------------
# main
# ----------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--db", default="data/mouse.sqlite3")
    parser.add_argument("--gru", default="models/mouse_gru.pt", help="GRU checkpoint, or '' to skip")
    parser.add_argument("--dmtg", default="models/mouse_dmtg.pt", help="DMTG checkpoint, or '' to skip")
    parser.add_argument("--out", default="models/comparison")
    parser.add_argument("--limit", type=int, default=3000)
    parser.add_argument("--temperature", type=float, default=0.8, help="GRU sampling temperature")
    parser.add_argument("--min-displacement", type=float, default=3.0,
                        help="skip held-out segments shorter than this (DMTG has no path to shape)")
    parser.add_argument("--holdout", type=float, default=0.1)
    parser.add_argument("--seed", type=int, default=0)
    args = parser.parse_args()

    segments = load_segments(args.db)
    _, val = split_by_session(segments, args.holdout, args.seed)
    val = [s for s in val if np.hypot(*s.steps[:, :2].sum(0)) >= args.min_displacement]
    rng = np.random.default_rng(args.seed)
    if len(val) > args.limit:
        val = [val[i] for i in rng.choice(len(val), args.limit, replace=False)]
    real_rows = [real_to_rows(s) for s in val]
    starts = np.array([r[0, 1:3] for r in real_rows])
    targets = np.array([r[-1, 1:3] for r in real_rows])

    generated = {}
    if args.gru and Path(args.gru).exists():
        from .sample import generate_batch, load_model
        model = load_model(args.gru)
        rows = []
        for i in range(0, len(val), 512):
            rows.extend(generate_batch(model, starts[i:i + 512], targets[i:i + 512],
                                       temperature=args.temperature, seed=args.seed + i))
        generated["gru"] = rows
    if args.dmtg and Path(args.dmtg).exists():
        from .diffusion import generate_dmtg, load_dmtg
        model = load_dmtg(args.dmtg)
        # DMTG emits no timing; re-time its paths at recorded poll gaps (<20 ms).
        gaps = np.concatenate([np.diff(r[:, 0]) for r in real_rows])
        tick_quantiles = np.quantile(gaps[gaps < 20], np.linspace(0, 1, 201))
        rows = []
        for i in range(0, len(val), 512):
            rows.extend(generate_dmtg(model, starts[i:i + 512], targets[i:i + 512], seed=args.seed + i,
                                      tick_quantiles=tick_quantiles))
        generated["dmtg"] = rows
    if not generated:
        raise SystemExit("no checkpoint found; pass --gru and/or --dmtg")

    real_stats = [describe(r, t) for r, t in zip(real_rows, targets)]
    report = {"real": summarise(real_stats), "duration_by_distance": {"real": duration_by_distance(real_stats)},
              "classifier": {}, "temperature": args.temperature}
    for name, rows in generated.items():
        stats = [describe(r, t) for r, t in zip(rows, targets)]
        report[name] = summarise(stats)
        report["duration_by_distance"][name] = duration_by_distance(stats)
        report["classifier"][name] = classifier_test(real_rows, rows, targets, args.seed)

    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    with open(out / "report.json", "w", encoding="utf-8") as handle:
        json.dump(report, handle, indent=1)
    plot(real_rows, generated, targets, out / "trajectories.png", seed=args.seed)
    plot_speed_profiles(real_rows, generated, out / "speed_profile.png")

    names = ["real"] + list(generated)
    print(f"held-out segments compared: {len(val)}")
    print(f"{'metric (median, p25-p75)':<24}" + "".join(f"{n:>26}" for n in names))

    def line(label, key, fmt="{:.1f}"):
        cells = []
        for n in names:
            s = report[n][key]
            cells.append(f"{fmt.format(s['median'])} ({fmt.format(s['p25'])}-{fmt.format(s['p75'])})")
        print(f"{label:<24}" + "".join(f"{c:>26}" for c in cells))

    line("steps", "steps")
    line("duration ms", "duration_ms")
    line("end error px", "end_error")
    line("path/displacement", "efficiency", "{:.3f}")
    line("peak speed px/ms", "peak_speed", "{:.2f}")
    line("peak time fraction", "peak_time_frac", "{:.2f}")
    line("median dt ms", "median_dt", "{:.2f}")
    line("pause frac (>100ms)", "long_pause_frac", "{:.3f}")
    print(f"{'clicked fraction':<24}" + "".join(f"{report[n]['clicked_frac']:>26.3f}" for n in names))
    print("median duration ms by displacement px")
    tables = {n: {row["range"]: row for row in report["duration_by_distance"][n]} for n in names}
    for row in report["duration_by_distance"]["real"]:
        cells = [f"{tables[n].get(row['range'], {}).get('median_ms', float('nan')):.0f}" for n in names]
        print(f"  {row['range']:>10} n={row['n']:<5}" + "".join(f"{c:>12}" for c in cells))
    print("real-vs-generated random forest accuracy (50% = indistinguishable):")
    for n in generated:
        c = report["classifier"][n]
        top = ", ".join(f"{f} {v:.2f}" for f, v in c["top_features"][:5])
        print(f"  {n:<6} all features {c['accuracy'] * 100:.1f}%, shape only {c['shape_accuracy'] * 100:.1f}%"
              f"   top features: {top}")
    print(f"wrote {out / 'report.json'}, {out / 'trajectories.png'}, {out / 'speed_profile.png'}")


if __name__ == "__main__":
    main()
