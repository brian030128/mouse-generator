"""Generate a human-like mouse path from one screen point to another.

    python generate.py 400 300 1200 700            # print (t_ms, x, y, click) rows
    python generate.py 400 300 1200 700 --json     # JSON list for another program
    python generate.py 400 300 1200 700 --plot path.png

From Python:

    from generator.sample import load_model, generate, default_checkpoint
    model = load_model(default_checkpoint())
    rows = generate(model, (400, 300), (1200, 700))   # columns: t_ms, x, y, click

Replay by moving the cursor to (x, y) at each t_ms and pressing the left button
on the final row. Positions are physical pixels, like the recorder's data.
"""

import argparse
import json

from generator.sample import default_checkpoint, generate, load_model


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("x0", type=float)
    parser.add_argument("y0", type=float)
    parser.add_argument("x1", type=float)
    parser.add_argument("y1", type=float)
    parser.add_argument("--model", default=None, help="checkpoint path (default: models/mouse_gru.pt "
                        "or models/mouse_dmtg.pt with --dmtg)")
    parser.add_argument("--dmtg", action="store_true", help="use the DMTG diffusion model")
    parser.add_argument("--alpha", type=float, default=None,
                        help="DMTG complexity: path length / displacement - 1 (default: sampled)")
    parser.add_argument("--temperature", type=float, default=0.8,
                        help="lower is smoother and more typical; 1.0 samples the learned distribution")
    parser.add_argument("--seed", type=int, default=None)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--plot", help="save a PNG of the path")
    args = parser.parse_args()

    if args.dmtg:
        from generator.diffusion import default_dmtg_checkpoint, generate_dmtg, load_dmtg
        model = load_dmtg(args.model or default_dmtg_checkpoint())
        rows = generate_dmtg(model, [(args.x0, args.y0)], [(args.x1, args.y1)], alpha=args.alpha,
                             seed=args.seed, tick_quantiles=[7.5])[0]
    else:
        model = load_model(args.model or default_checkpoint())
        rows = generate(model, (args.x0, args.y0), (args.x1, args.y1),
                        temperature=args.temperature, seed=args.seed)
    if args.json:
        print(json.dumps([[round(float(t), 2), int(x), int(y), int(c)] for t, x, y, c in rows]))
    else:
        print("t_ms\tx\ty\tclick")
        for t, x, y, c in rows:
            print(f"{t:.1f}\t{int(x)}\t{int(y)}\t{int(c)}")
        print(f"# {len(rows) - 1} steps, {rows[-1, 0]:.0f} ms, "
              f"ended {abs(rows[-1, 1] - args.x1):.0f}/{abs(rows[-1, 2] - args.y1):.0f} px from target"
              f"{'' if rows[-1, 3] > 0.5 else ', no click'}")
    if args.plot:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(9, 4))
        ax1.plot(rows[:, 1], rows[:, 2], marker=".", ms=3, lw=1)
        ax1.scatter([args.x1], [args.y1], marker="x", color="k")
        ax1.invert_yaxis()
        ax1.set_aspect("equal")
        ax1.set_title("path (screen pixels)")
        ax2.plot(rows[1:, 0], (abs(rows[1:, 1:3] - rows[:-1, 1:3]).sum(1)) / (rows[1:, 0] - rows[:-1, 0]).clip(0.5))
        ax2.set_xlabel("ms")
        ax2.set_ylabel("speed px/ms")
        fig.tight_layout()
        fig.savefig(args.plot, dpi=110)


if __name__ == "__main__":
    main()
