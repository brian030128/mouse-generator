# Mouse dataset recorder

A Windows desktop mouse recorder using Python's standard library. No packages
need installing. Data stays in a local SQLite database.

## Run

Requires Windows and Python 3.10+ with Tk (included in the normal Windows installer).

**Double-click `Start Recorder.bat`** to open the recorder and automatically start
the three-second countdown. Press **F8** anywhere to stop. You can also create a
desktop shortcut to this launcher. It uses Python's windowed interpreter so no
terminal stays open.

Recording continues while the window is minimized or another app is in front.
Keep the recorder running; closing its window stops recording.

To open the interface without starting recording, run:

```powershell
python recorder.py
```

1. Optionally enter a session label.
2. Click **Start**. A three-second countdown lets you return to your task.
3. Use the mouse normally. Press **F8** anywhere to stop.
4. Use **Export CSV** to export all accepted segments from all sessions.

## Five-second filtering

- The first cursor movement after recording starts, or after a left-click,
  starts a segment. The next **left-button press** ends it.
- Segments lasting **at most 5 seconds** are saved; longer segments are discarded
  entirely, including their clicks. After that click, the recorder waits for
  fresh cursor movement before starting another candidate.
- The unfinished segment at Stop is discarded. An interval with no movement
  still counts as elapsed time, so long pauses are rejected too.
- Clicks without preceding movement do not create segments. Right/middle/side
  clicks and scrolling are recorded inside segments but do not end them.
- Prefer **F8** to the Stop button: clicking the recorder's controls can otherwise
  produce an ordinary segment ending at that click.

Movement onset means the first delivered mouse-move event, with no minimum
distance threshold. Pauses after onset count toward the five-second limit;
they do not reset it. Once a segment exceeds the limit, the recorder waits
for the next left-click before allowing a fresh segment. Drags can occur
within a segment. An incomplete segment at Stop is never saved.

## Database

Default location: `data/mouse.sqlite3` beside the program. Each accepted segment
is committed immediately; an interrupted session can have a NULL `ended_utc`.

| Table | Contents |
| --- | --- |
| `sessions` | UTC start/end, label, duration limit, virtual desktop bounds |
| `segments` | Session ID, start offset, duration, event count |
| `events` | Segment ID, sequence, relative timestamp, x/y, event kind, button, wheel delta |

Positions are physical screen pixels, including negative coordinates on monitors
left of or above the primary monitor. Times use a monotonic clock in nanoseconds
(units do not imply nanosecond measurement accuracy). Events are recorded when
Windows delivers them, not at a fixed sampling rate; stationary time is represented
by gaps between timestamps. OS-flagged injected mouse events are ignored.

The recorder uses a mouse hook while recording and checks only F8 as a stop
control. It does not record typed text, window titles, page content or screenshots.
It does not upload data. Capture applies to the normal interactive desktop;
Windows secure desktop events are not captured. Keep monitor layout and scaling
unchanged during a run; restart the app after changing them.

Choose a different database or export without opening the interface:

```powershell
python recorder.py --db data/example.sqlite3
python recorder.py --export data/mouse.csv
python -m unittest -v
```

CSV rows include session and segment IDs so separate trajectories stay distinct.
The export contains every stored event; session labels and desktop metadata remain
in SQLite. Close the recorder before copying its database, so SQLite can finish
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
