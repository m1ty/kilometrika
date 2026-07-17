"""SQLite storage. Self-contained: no external DB needed."""
from __future__ import annotations

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone

from parser import Activity

SCHEMA = """
CREATE TABLE IF NOT EXISTS activities (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    file_hash TEXT UNIQUE NOT NULL,
    filename TEXT,
    sport TEXT,
    category TEXT,
    start_time TEXT,
    duration_s REAL,
    distance_m REAL,
    calories INTEGER,
    hr_avg REAL,
    hr_max REAL,
    ascent_m REAL,
    descent_m REAL,
    cadence_avg REAL,
    laps_json TEXT
);
CREATE INDEX IF NOT EXISTS idx_act_start ON activities(start_time);

CREATE TABLE IF NOT EXISTS photos (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    filename TEXT NOT NULL,
    created TEXT DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_photo_act ON photos(activity_id);
DROP INDEX IF EXISTS idx_ph_act;

CREATE TABLE IF NOT EXISTS trackpoints (
    activity_id INTEGER NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
    time TEXT NOT NULL,
    lat REAL, lon REAL,
    elevation REAL,
    distance REAL,
    hr INTEGER,
    cadence INTEGER,
    speed REAL,
    watts REAL
);
CREATE INDEX IF NOT EXISTS idx_tp_act ON trackpoints(activity_id);
"""


class Store:
    def __init__(self, path: str):
        self.path = path
        with self._conn() as c:
            c.executescript(SCHEMA)
            # миграция старых БД: колонки заметок и категории
            cols = [r["name"] for r in c.execute("PRAGMA table_info(activities)")]
            if "notes" not in cols:
                c.execute("ALTER TABLE activities ADD COLUMN notes TEXT")
            if "category" not in cols:
                c.execute("ALTER TABLE activities ADD COLUMN category TEXT")
                # бэкфилл старых записей той же функцией, что и импорт
                from parser import sport_category
                rows = c.execute("SELECT id, sport FROM activities").fetchall()
                c.executemany(
                    "UPDATE activities SET category = ? WHERE id = ?",
                    [(sport_category(r["sport"]), r["id"]) for r in rows])

    @contextmanager
    def _conn(self):
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        finally:
            conn.close()

    def has_file(self, file_hash: str) -> bool:
        with self._conn() as c:
            row = c.execute(
                "SELECT 1 FROM activities WHERE file_hash = ?", (file_hash,)
            ).fetchone()
        return row is not None

    def save(self, act: Activity, filename: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                """INSERT INTO activities
                   (file_hash, filename, sport, category, start_time, duration_s, distance_m,
                    calories, hr_avg, hr_max, ascent_m, descent_m, cadence_avg, laps_json)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (act.file_hash, filename, act.sport, act.category,
                 act.start_time.isoformat() if act.start_time else None,
                 act.duration_s, act.distance_m, act.calories,
                 act.hr_avg, act.hr_max, act.ascent_m, act.descent_m,
                 act.cadence_avg, json.dumps(act.laps)),
            )
            act_id = cur.lastrowid
            c.executemany(
                """INSERT INTO trackpoints
                   (activity_id, time, lat, lon, elevation, distance, hr, cadence, speed, watts)
                   VALUES (?,?,?,?,?,?,?,?,?,?)""",
                [(act_id, tp.time.isoformat(), tp.lat, tp.lon, tp.elevation,
                  tp.distance, tp.hr, tp.cadence, tp.speed, tp.watts)
                 for tp in act.trackpoints],
            )
        return act_id

    def list_activities(self) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM activities ORDER BY start_time DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def get_activity(self, act_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT * FROM activities WHERE id = ?", (act_id,)
            ).fetchone()
        return dict(row) if row else None

    def get_trackpoints(self, act_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT * FROM trackpoints WHERE activity_id = ? ORDER BY time",
                (act_id,),
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_activity(self, act_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM activities WHERE id = ?", (act_id,))

    def weekly_summary(self, weeks: int = 12) -> list[dict]:
        """Totals per ISO week (local timezone) for the trend chart."""
        buckets: dict[str, dict] = {}
        for a in self.list_activities():
            if not a["start_time"]:
                continue
            dt = datetime.fromisoformat(a["start_time"])
            if dt.tzinfo is None:               # naive в БД считаем UTC
                dt = dt.replace(tzinfo=timezone.utc)
            y, w, _ = dt.astimezone().isocalendar()
            key = f"{y}-W{w:02d}"
            b = buckets.setdefault(key, {
                "week": key, "n": 0, "distance_m": 0.0, "duration_s": 0.0, "_hr": []})
            b["n"] += 1
            b["distance_m"] += a["distance_m"] or 0
            b["duration_s"] += a["duration_s"] or 0
            if a["hr_avg"]:
                b["_hr"].append(a["hr_avg"])
        out = [buckets[k] for k in sorted(buckets)][-weeks:]
        for b in out:
            hr = b.pop("_hr")
            b["hr_avg"] = round(sum(hr) / len(hr), 1) if hr else None
        return out

    # ------------------------------------------------------------- notes & photos

    def set_notes(self, act_id: int, notes: str) -> None:
        with self._conn() as c:
            c.execute("UPDATE activities SET notes = ? WHERE id = ?", (notes, act_id))

    def add_photo(self, act_id: int, filename: str) -> int:
        with self._conn() as c:
            cur = c.execute(
                "INSERT INTO photos (activity_id, filename) VALUES (?, ?)",
                (act_id, filename))
            return cur.lastrowid

    def list_photos(self, act_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT id, filename, created FROM photos WHERE activity_id = ? ORDER BY id",
                (act_id,)).fetchall()
        return [dict(r) for r in rows]

    def get_photo(self, photo_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute("SELECT * FROM photos WHERE id = ?", (photo_id,)).fetchone()
        return dict(row) if row else None

    def delete_photo(self, photo_id: int) -> None:
        with self._conn() as c:
            c.execute("DELETE FROM photos WHERE id = ?", (photo_id,))
