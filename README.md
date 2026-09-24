# Recorder

A Windows and macOS desktop mouse recorder using Python's standard library. No
packages need installing. Data stays in a local SQLite database.

## Run

Requires Windows or macOS and Python 3.10+ with Tk (included in the normal
python.org installers; with Homebrew Python also run `brew install python-tk`).

On Windows, **double-click `Start Recorder.bat`**; on macOS, **double-click
`Start Recorder.command`**. Either one opens the recorder and immediately starts
recording. On Windows, press **F8** anywhere to stop; on macOS, close the window
(no keyboard is used). You can also create a
desktop shortcut (Windows) or Dock alias (macOS) to the launcher. Neither
leaves a terminal window open.

Recording continues while the window is minimized or another app is in front.
Keep the recorder running; closing its window stops recording.

You can also open the recorder from PowerShell or Terminal; recording starts immediately:

```powershell
python recorder.py
```

No Start, Stop, or Export buttons are needed. Data saves automatically.
Close the window to stop, or on Windows press **F8** anywhere. If you use F8, close
and reopen the app when you want to record again. CSV export remains available from the
command line below.

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

The recorder uses a mouse hook (Windows) or listen-only event tap (macOS) while recording. On Windows it checks only F8 as a stop
control; on macOS it reads no keyboard state at all. It does not record typed text, window titles, page content or screenshots.
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
