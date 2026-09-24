"""Local SQLite storage and CSV export for mouse trajectories."""

import csv
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now():
    return datetime.now(timezone.utc).isoformat()


class Database:
    def __init__(self, path):
        path = Path(path)
        self.path = path.resolve()
        path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(path)
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY,
                started_utc TEXT NOT NULL,
                ended_utc TEXT,
                label TEXT NOT NULL,
                max_duration_ns INTEGER NOT NULL,
                screen_left INTEGER NOT NULL,
                screen_top INTEGER NOT NULL,
                screen_width INTEGER NOT NULL,
                screen_height INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS segments (
                id INTEGER PRIMARY KEY,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                start_offset_ns INTEGER NOT NULL,
                duration_ns INTEGER NOT NULL,
                event_count INTEGER NOT NULL
            );
            CREATE TABLE IF NOT EXISTS events (
                segment_id INTEGER NOT NULL REFERENCES segments(id),
                sequence INTEGER NOT NULL,
                t_ns INTEGER NOT NULL,
                x INTEGER NOT NULL,
                y INTEGER NOT NULL,
                kind TEXT NOT NULL,
                button TEXT NOT NULL,
                wheel_delta INTEGER NOT NULL,
                PRIMARY KEY (segment_id, sequence)
            );
            CREATE INDEX IF NOT EXISTS segments_session ON segments(session_id);
        """)
        # Columns added after the first release; rows recorded earlier stay NULL.
        self._add_column("sessions", "platform", "TEXT")
        self._add_column("segments", "pixel_scale", "REAL")

    def _add_column(self, table, column, kind):
        columns = {row[1] for row in self.connection.execute(f"PRAGMA table_info({table})")}
        if column not in columns:
            with self.connection:
                self.connection.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    def start_session(self, label, max_duration_ns, screen, platform=None):
        with self.connection:
            result = self.connection.execute(
                "INSERT INTO sessions(started_utc, label, max_duration_ns, "
                "screen_left, screen_top, screen_width, screen_height, platform) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (utc_now(), label, max_duration_ns, *screen, platform),
            )
        return result.lastrowid

    def finish_session(self, session_id):
        with self.connection:
            self.connection.execute(
                "UPDATE sessions SET ended_utc = ? WHERE id = ?",
                (utc_now(), session_id),
            )

    def count_segments(self):
        return self.connection.execute("SELECT COUNT(*) FROM segments").fetchone()[0]

    def save_segment(self, session_id, recording_start_ns, events, pixel_scale=None):
        start = events[0].timestamp_ns
        duration = events[-1].timestamp_ns - start
        with self.connection:
            result = self.connection.execute(
                "INSERT INTO segments(session_id, start_offset_ns, duration_ns, "
                "event_count, pixel_scale) VALUES (?, ?, ?, ?, ?)",
                (session_id, start - recording_start_ns, duration, len(events), pixel_scale),
            )
            self.connection.executemany(
                "INSERT INTO events VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                [(result.lastrowid, i, e.timestamp_ns - start, e.x, e.y,
                  e.kind, e.button, e.wheel_delta) for i, e in enumerate(events)],
            )

    def export_csv(self, path):
        if Path(path).resolve() in {
            self.path, Path(str(self.path) + "-wal"), Path(str(self.path) + "-shm")
        }:
            raise ValueError("Choose a CSV output path separate from the database files.")
        cursor = self.connection.execute("""
            SELECT s.session_id, s.id AS segment_id, s.start_offset_ns,
                   s.duration_ns, e.sequence, e.t_ns, e.x, e.y,
                   e.kind, e.button, e.wheel_delta, ss.platform, s.pixel_scale
            FROM segments s JOIN events e ON e.segment_id = s.id
            JOIN sessions ss ON ss.id = s.session_id
            ORDER BY s.id, e.sequence
        """)
        with open(path, "w", newline="", encoding="utf-8") as handle:
            writer = csv.writer(handle)
            writer.writerow([column[0] for column in cursor.description])
            writer.writerows(cursor)

    def close(self):
        self.connection.close()
