"""Хранилище: сохранение, миграция старых БД, заметки, фото."""
import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from db import Store          # noqa: E402
from parser import parse_activity  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def store(tmp_path):
    return Store(str(tmp_path / "t.db"))


@pytest.fixture
def act():
    return parse_activity(str(FIX / "garmin_run.tcx"))


def test_save_and_read(store, act):
    act_id = store.save(act, "garmin_run.tcx")
    a = store.get_activity(act_id)
    assert a["sport"] == "Running"
    assert a["category"] == "run"
    assert len(store.get_trackpoints(act_id)) == 60
    assert store.has_file(act.file_hash)


def test_migration_backfills_category_and_notes(tmp_path, act):
    """БД старой схемы (без notes/category) должна мигрировать с бэкфиллом."""
    db = tmp_path / "old.db"
    conn = sqlite3.connect(db)
    conn.executescript("""
        CREATE TABLE activities (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_hash TEXT UNIQUE NOT NULL, filename TEXT, sport TEXT,
            start_time TEXT, duration_s REAL, distance_m REAL, calories INTEGER,
            hr_avg REAL, hr_max REAL, ascent_m REAL, descent_m REAL,
            cadence_avg REAL, laps_json TEXT);
        INSERT INTO activities (file_hash, sport, start_time, distance_m, duration_s)
        VALUES ('aaa', 'Бег на улице', '2026-06-01T07:00:00', 5000, 1800),
               ('bbb', 'Biking', '2026-06-02T07:00:00', 20000, 3600),
               ('ccc', 'Ходьба', '2026-06-03T07:00:00', 3000, 2400);
    """)
    conn.commit(); conn.close()

    store = Store(str(db))    # миграция происходит здесь
    cats = {a["sport"]: a["category"] for a in store.list_activities()}
    assert cats == {"Бег на улице": "run", "Biking": "bike", "Ходьба": "other"}
    store.set_notes(1, "мигрировало")
    assert store.get_activity(1)["notes"] == "мигрировало"


def test_duplicate_hash(store, act):
    store.save(act, "a.tcx")
    assert store.has_file(act.file_hash)
    with pytest.raises(Exception):    # UNIQUE constraint
        store.save(act, "b.tcx")


def test_photos_crud(store, act):
    act_id = store.save(act, "a.tcx")
    pid = store.add_photo(act_id, "x.jpg")
    assert store.list_photos(act_id)[0]["filename"] == "x.jpg"
    assert store.get_photo(pid)["activity_id"] == act_id
    store.delete_photo(pid)
    assert store.list_photos(act_id) == []


def test_delete_activity_cascades(store, act):
    act_id = store.save(act, "a.tcx")
    store.add_photo(act_id, "x.jpg")
    store.delete_activity(act_id)
    assert store.get_activity(act_id) is None
    assert store.get_trackpoints(act_id) == []
    assert store.list_photos(act_id) == []
