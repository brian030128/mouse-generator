# Recorder

A Windows and macOS desktop mouse recorder using Python's standard library. No
packages need installing. Data stays in a local SQLite database.

## Run

Requires Windows or macOS and Python 3.10+ with Tk (included in the normal
python.org installers; with Homebrew Python also run `brew install python-tk`).

On Windows, **double-click `Start Recorder.bat`**; on macOS, **double-click
`Start Recorder.command`**. Either one opens the recorder and immediately starts
recording. Close the window to stop; no keyboard is used. You can also create a
desktop shortcut (Windows) or Dock alias (macOS) to the launcher. Neither
leaves a terminal window open.

Recording continues while the window is minimized or another app is in front.
Keep the recorder running; closing its window stops recording.

You can also open the recorder from PowerShell or Terminal; recording starts immediately:

```powershell
python recorder.py
```

No Start, Stop, or Export buttons are needed. Data saves automatically.
Close the window to stop; reopen the app to record again. CSV export remains
available from the command line below.

## macOS permissions

The recorder does not ask for keyboard access on macOS. If macOS refuses mouse
capture, the recorder shows an error. Then allow the app that launched it
(Terminal, or Python itself when started at sign-in) under System Settings >
Privacy & Security > Input Monitoring, and reopen the recorder.

## Start at sign-in

Turn on **Start recording when I sign in to Windows** (or **…to macOS**) to launch the visible
recorder window and begin recording at your next sign-in. Turn it off
to remove this app's startup registration. The setting persists across restarts
and is off by default. Changing it does not start or stop the current recording.

On Windows this uses the current user's standard `Run` registry key (entry
`MouseDatasetRecorder`). On macOS it writes the per-user LaunchAgent
`~/Library/LaunchAgents/com.mousedatasetrecorder.recorder.plist`. Neither needs
administrator access, and both record only after you sign in, not before login.

If turning the setting on in macOS shows **Permission denied**, another installer
has left `~/Library/LaunchAgents` owned by the system. The error dialog shows the
fix and copies it to the clipboard. Run it in Terminal, enter your Mac password,
then turn the setting on again:

```bash
sudo chown "$USER" ~/Library/LaunchAgents
``` The registration uses absolute paths to Python,
the script, and your database. If you move the app or change Python installations,
turn the toggle off and on from the new location. Startup policies, or disabling the entry in
Windows settings or macOS Login Items, can prevent automatic startup.

## Segment filtering

- The first cursor movement after recording starts, or after a left-click,
  starts a segment. The next **left-button press** ends it.
- Segments lasting **at most 8 seconds** are saved in full.
- For longer segments, only events in the **last 1.5 seconds before the click**
  are retained, starting at the first movement in that window. This uses a fixed
  1.5-second window (the upper end of 0.8–1.5 seconds), without random trimming.
  If the window contains no movement, the segment is discarded. Sparse events
  can produce a saved segment shorter than 1.5 seconds; no points are fabricated.
- The unfinished segment at Stop is discarded. After a click, the recorder waits
  for fresh cursor movement before starting another candidate.
- Clicks without preceding movement do not create segments. Right/middle/side
  clicks and scrolling are recorded inside segments but do not end them.
- Clicking the recorder window can produce an ordinary segment ending at that
  click, just like clicking another app.

Movement onset means the first delivered mouse-move event, with no minimum
distance threshold. Pauses after onset count toward the eight-second threshold;
they do not reset it. Long segments retain a rolling 1.5-second buffer until the
next left-click. Drags can occur within a segment. An incomplete segment at Stop
is never saved. Existing saved recordings are not modified by this rule.

## Database

Default location: `data/mouse.sqlite3` beside the program. Each accepted segment
is committed immediately; an interrupted session can have a NULL `ended_utc`.
Every launch appends a new session to the same database, preserving earlier data.
You can open the launcher each time you turn on your PC; no manual save is needed.
Startup registration is controlled only by the sign-in toggle.
The window shows the current run's saved/discarded counts and **Total saved
segments**, which includes previous runs and updates after each save.
A record in this total means one accepted movement-to-click segment, not one event.
There is no session-label input. Internal session IDs and existing labels remain
in SQLite for compatibility and to associate timing and display metadata; new
labels are empty. Existing recordings are preserved.

| Table | Contents |
| --- | --- |
| `sessions` | UTC start/end, label, duration limit, virtual desktop bounds, platform |
| `segments` | Session ID, start offset, duration, event count, pixel scale |
| `events` | Segment ID, sequence, relative timestamp, x/y, event kind, button, wheel delta |

On Windows, positions are physical screen pixels. On macOS they are global
display points (rounded; a Retina display has 2 pixels per point) with the origin
at the top-left of the main display. Both platforms can produce negative
coordinates on monitors left of or above the primary monitor. Wheel deltas use Windows units:
120 per notch. On macOS, one scroll line counts as 120, including trackpad
scrolling and the natural scrolling direction.

To compare platforms, use `sessions.platform` (`win32` or `darwin`) and
`segments.pixel_scale`: physical pixels per recorded unit on the display where the
segment started. Multiply x/y by it for physical pixels. It is 1 on Windows and
usually 2 on Retina displays. A segment that crosses displays with different
scales keeps the starting display's value. Both columns are NULL for data recorded
before they were added; opening an older database adds them without changing
existing rows. Even in matching units, macOS and Windows pointer acceleration
differ, so keep the platform as a label rather than pooling data blindly. Times use a monotonic clock in nanoseconds
(units do not imply nanosecond measurement accuracy). Events are recorded when
the OS delivers them, not at a fixed sampling rate; stationary time is represented
by gaps between timestamps. OS-flagged injected mouse events are ignored (on macOS, events not originating
from the HID system).

The recorder uses a mouse hook (Windows) or listen-only event tap (macOS) while
recording and reads no keyboard state at all. It does not record typed text, window titles, page content or screenshots.
It does not upload data. Capture applies to the normal interactive desktop;
Windows secure desktop events are not captured. Keep monitor layout and scaling
unchanged during a run; restart the app after changing them.

Choose a different database or export without opening the interface:

```powershell
python recorder.py --db data/example.sqlite3
python recorder.py --export data/mouse.csv
python -m unittest -v
```

CSV rows include session and segment IDs so separate trajectories stay distinct,
plus each segment's `platform` and `pixel_scale`. The export contains every stored
event; session labels and desktop bounds remain in SQLite. Close the recorder before copying its database, so SQLite can finish
its WAL checkpoint.

## Storage estimate

A synthetic benchmark using this schema produced **45.33 MB for 1,000,000 events**
across 10,000 segments, including indexes, after closing SQLite. Actual size
depends on events per segment and stored values. As a rough planning estimate:

| Dataset | Approximate size |
| --- | --- |
| 1 million events | 45–50 MB |
| 1 million segments averaging 100 events each | 4.5–5 GB |
| 1 million segments averaging 500 events each | 23–25 GB |
| 1 million segments averaging 1,000 events each | 45–50 GB |

Segment estimates extrapolate the benchmark; allow additional disk space for
SQLite's active WAL file and any CSV exports. A segment is a whole movement
ending in a click, while an event is one position or button/wheel update.

## Trajectory generator

`generator/` trains a model on the recorded segments and samples new
movement-to-click paths between any two screen points. It needs PyTorch and
NumPy (matplotlib only for the plots); a CUDA GPU makes training fast but is
not required for sampling.

Two generators are included.

**GRU (default, `models/mouse_gru.pt`).** A 3-layer GRU (5.2M parameters)
emits one mouse event at a time, in the style of SketchRNN: a click
probability, then the time gap as a categorical over 78 fine bins of the
recorded gap distribution (most gaps sit within 0.3 ms of the 7.5 ms poll
interval, with jitter and pauses in the tails), then the step displacement as a
mixture of bivariate Gaussians conditioned on that gap. Every step is
conditioned on the vector still to travel to the target, so a sampled path ends
where you ask and decides on its own when to press the button. Events keep the
recorder's native timing, so a replay can move the cursor at the sampled times
and click on the final row.

**DMTG (`models/mouse_dmtg.pt`).** A reimplementation of *DMTG: A Human-Like
Mouse Trajectory Generation Bot Based on Entropy-Controlled Diffusion Networks*
(Liu et al., arXiv:2410.18233): a 1D U-Net denoiser over a fixed-length
sequence of 64 coordinates, conditioned on the end point and a complexity
factor alpha (path length over displacement), trained with the diffusion loss
plus an x0 reconstruction term and the paper's path-length style term, and
sampled with DDIM from the paper's mixture-of-Gaussians initial noise. The
paper generates coordinates only and leaves several hyperparameters
unspecified; `generator/diffusion.py` documents each choice made here. A small
head predicts the movement duration and the path is re-timed at recorded poll
gaps so that its output can be replayed and compared.

**BeCAPTCHA-Mouse (`generator/becaptcha.py`).** The two bot generators from
*BeCAPTCHA-Mouse: Synthetic Mouse Trajectories and Improved Bot Detection*
(Acien et al., arXiv:2005.00890), included as baselines. The function-based
generator crosses a path shape (linear, quadratic, exponential) with a
velocity profile (constant, accelerating, bell-shaped), drawing the point
count and curvature from the human recordings. The GAN is the paper's LSTM
generator and discriminator trained with its settings; since it is
unconditional, it is trained in a start-to-end frame and each sample is
rotated and scaled onto the requested points. Both use recorded poll gaps for
timing, which the paper (200 Hz data) does not model.

```powershell
python -m generator.train --db data/mouse.sqlite3 --epochs 100      # GRU, ~20 min on an RTX 4060
python -m generator.diffusion --db data/mouse.sqlite3 --epochs 150  # DMTG, ~25 min
python -m generator.becaptcha --db data/mouse.sqlite3               # BeCAPTCHA GAN, ~3 min
python -m generator.evaluate --db data/mouse.sqlite3                # held-out comparison + plots
python generate.py 400 300 1200 700                                 # one GRU path, printed
python generate.py 400 300 1200 700 --json                          # for another program
python generate.py 400 300 1200 700 --dmtg --plot path.png          # DMTG path with a picture
python -m unittest test_generator -v
```

From Python:

```python
from generator.sample import load_model, generate, default_checkpoint
model = load_model(default_checkpoint())
rows = generate(model, (400, 300), (1200, 700))   # columns: t_ms, x, y, click
```

Training holds out whole recording sessions (about 10% of segments) and keeps
the checkpoint with the best held-out loss. `--temperature` scales the GRU's
step-displacement mixture: 1.0 samples the learned distribution exactly, lower
values give smoother, more typical paths.

### How the generators compare

`generator.evaluate` asks each model to travel the same start-to-click vector
as each held-out real segment, compares summary statistics, and trains a random
forest to tell real from generated (the white-box test in the DMTG paper; 50%
means indistinguishable). Results are in `models/comparison/report.json` and
the figures `models/comparison/trajectories.png` and
`models/comparison/speed_profile.png`.

On 3,000 held-out segments from six recording sessions the models never saw
(medians; GRU at temperature 0.8, DMTG at its default settings):

| | real | GRU | DMTG | BeCAPTCHA fn | BeCAPTCHA GAN |
| --- | --- | --- | --- | --- | --- |
| events per segment | 21 | 21 | 44 | 24 | 24 |
| duration, ms | 210 | 259 | 333 | 187 | 185 |
| path length / displacement | 1.16 | 1.17 | 1.36 | 1.04 | 1.25 |
| peak speed, px/ms | 1.37 | 1.25 | 1.23 | 0.83 | 1.52 |
| duration for 150–300 px moves, ms | 795 | 806 | 648 | 414 | 447 |
| duration for 600–1200 px moves, ms | 1553 | 1946 | 1547 | 922 | 846 |
| click lands on target | always | 99.4% of samples | pinned | pinned | pinned |
| detector accuracy, all features | | 76.9% | 98.0% | 99.2% | 98.9% |
| detector accuracy, shape only | | 61.9% | 88.1% | 96.3% | 88.6% |

The detector is a 300-tree random forest scored by 5-fold cross-validation on
the 3,000 real and 3,000 generated trajectories, using the path resampled to
24 points in a start-to-click frame, a histogram of time gaps around the
7.5 ms poll interval, and speed, acceleration, turning and duration summaries.
The forest is explainable: a depth-3 decision tree reaches 69% against the
GRU and 93% against DMTG with a handful of rules.

- **GRU.** The main tell is poll-timing jitter. 66% of real trajectories
  contain a gap of 4.5–6.7 ms (an event delivered late, usually followed by an
  8–12 ms gap), but only 39% of GRU trajectories do. Most of that is a
  difference between sessions rather than a model error: such gaps make up
  3.1% of gaps in the training sessions and 6.1% in the held-out ones, and the
  GRU produces 3.4%. On training-session segments the detector drops to 71%.
  The weaker shape tells are a slightly higher overshoot rate past the target
  and a shorter final approach.
- **DMTG.** Three rules catch it: no gap over 20 ms (real paths pause; a
  timing-free model re-timed at poll rate never does), a 20% share of
  zero-length steps versus 5% (a 64-point path re-timed at 7.5 ms ticks
  repeats positions on short moves), and near-reversal heading changes from
  residual denoising noise. The paper itself reports 87–91% detection against
  strong classifiers.
- **BeCAPTCHA-Mouse.** Both baselines are caught almost every time, as in
  the paper (98–99% with its neuromotor detector). The function-based paths
  are too clean: no pauses, nearly straight (path/displacement 1.04), and
  a velocity profile that is either flat, still accelerating at the click
  (peak at 66% of the movement), or a symmetric bell. The GAN learns
  plausible shapes (shape-only detection 88.6%, on par with DMTG) but, like
  DMTG, has no timing of its own; both baselines also finish long moves in
  about half the real time because the human point count they draw from
  excludes the pauses inside real segments.

Use the GRU for replay; DMTG is kept for comparison and as a base for further
work. Because jitter is per machine, recording a short session on the machine
that will replay the paths and matching its gap distribution is the most
direct way to lower the GRU's detectability.
