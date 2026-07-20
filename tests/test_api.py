"""API поверх FastAPI TestClient: загрузка, дедуп, заметки, фото, кадр рамки."""
import io
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

FIX = Path(__file__).parent / "fixtures"


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    # main читает env на импорте — сбрасываем модуль между тестами
    for m in ("main",):
        sys.modules.pop(m, None)
    import main
    from fastapi.testclient import TestClient
    with TestClient(main.app) as c:
        yield c


def _upload(client, name):
    data = (FIX / name).read_bytes()
    return client.post("/api/upload", files={"file": (name, io.BytesIO(data))})


def test_upload_list_detail(client):
    r = _upload(client, "garmin_run.tcx")
    assert r.status_code == 200
    assert r.json()["sport"] == "Running"
    acts = client.get("/api/activities").json()
    assert len(acts) == 1 and acts[0]["category"] == "run"
    detail = client.get(f"/api/activities/{acts[0]['id']}").json()
    assert detail["photos"] == [] and "laps" in detail


def test_duplicate_409(client):
    assert _upload(client, "garmin_run.tcx").status_code == 200
    assert _upload(client, "garmin_run.tcx").status_code == 409


def test_bad_extension_400(client):
    r = client.post("/api/upload", files={"file": ("x.fit", io.BytesIO(b"123"))})
    assert r.status_code == 400


def test_gpx_upload(client):
    r = _upload(client, "strava_ride.gpx")
    assert r.status_code == 200 and r.json()["sport"] == "Biking"


def test_notes_roundtrip(client):
    _upload(client, "garmin_run.tcx")
    r = client.patch("/api/activities/1/notes", json={"notes": "жара +30"})
    assert r.status_code == 200
    assert client.get("/api/activities/1").json()["notes"] == "жара +30"


def test_photos_roundtrip(client):
    _upload(client, "garmin_run.tcx")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = client.post("/api/activities/1/photos", files={"file": ("p.png", io.BytesIO(png))})
    assert r.status_code == 200
    pid = r.json()["id"]
    assert client.get(f"/api/photos/{pid}").status_code == 200
    assert client.post("/api/activities/1/photos",
                       files={"file": ("x.txt", io.BytesIO(b"no"))}).status_code == 400
    assert client.delete(f"/api/photos/{pid}").json()["ok"]


def test_frame_png_and_etag(client):
    _upload(client, "garmin_run.tcx")
    r = client.get("/api/frame.png")
    assert r.status_code == 200
    assert r.headers["content-type"] == "image/png"
    etag = r.headers["etag"]
    r2 = client.get("/api/frame.png", headers={"If-None-Match": etag})
    assert r2.status_code == 304


def test_video_roundtrip(client):
    _upload(client, "garmin_run.tcx")
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 200      # минимальная mp4-сигнатура
    r = client.post("/api/activities/1/photos", files={"file": ("clip.mp4", io.BytesIO(mp4))})
    assert r.status_code == 200
    assert r.json()["kind"] == "video"
    detail = client.get("/api/activities/1").json()
    kinds = {p["filename"]: p["kind"] for p in detail["photos"]}
    assert kinds["clip.mp4"] == "video"
    assert client.get(f"/api/photos/{r.json()['id']}").status_code == 200


def test_media_kind_image(client):
    _upload(client, "garmin_run.tcx")
    png = b"\x89PNG\r\n\x1a\n" + b"0" * 100
    r = client.post("/api/activities/1/photos", files={"file": ("p.png", io.BytesIO(png))})
    assert r.json()["kind"] == "image"


def test_photo_thumbnail(client, tmp_path):
    _upload(client, "garmin_run.tcx")
    from PIL import Image
    big = tmp_path / "big.jpg"
    Image.new("RGB", (2000, 1500), (30, 120, 60)).save(big, quality=90)
    r = client.post("/api/activities/1/photos",
                    files={"file": ("big.jpg", big.read_bytes())})
    pid = r.json()["id"]
    orig = client.get(f"/api/photos/{pid}")
    th = client.get(f"/api/photos/{pid}?thumb=1")
    assert th.status_code == 200
    assert len(th.content) < len(orig.content) / 3, "тумба обязана быть сильно меньше"
    # у видео thumb молча отдаёт оригинал
    mp4 = b"\x00\x00\x00\x18ftypmp42" + b"0" * 200
    vid = client.post("/api/activities/1/photos",
                      files={"file": ("c.mp4", io.BytesIO(mp4))}).json()["id"]
    assert client.get(f"/api/photos/{vid}?thumb=1").status_code == 200


def test_splits_endpoint(client):
    """Синтетический GPX 2.5 км ровным темпом 5:00/км: два полных километра
    и хвост; темп восстанавливается интерполяцией границ."""
    v = 1000 / 300                       # м/с при темпе 5:00/км
    pts = []
    for i in range(76):                  # 750 c => 2.5 км
        t = i * 10
        lat = 55.55 + v * t / 111320
        pts.append(f'<trkpt lat="{lat:.6f}" lon="37.55"><ele>{100 + i * 0.1:.1f}</ele>'
                   f'<time>2026-07-10T06:{t // 60:02d}:{t % 60:02d}Z</time></trkpt>')
    gpx = ('<?xml version="1.0"?><gpx version="1.1" creator="t" '
           'xmlns="http://www.topografix.com/GPX/1/1"><trk><name>Running</name>'
           f'<trkseg>{"".join(pts)}</trkseg></trk></gpx>').encode()
    r = client.post("/api/upload", files={"file": ("run25.gpx", io.BytesIO(gpx))})
    assert r.status_code == 200
    s = client.get("/api/activities/1/splits").json()
    assert [x["km"] for x in s] == [1.0, 2.0, 2.5]
    assert all(abs(x["pace_s_km"] - 300) < 6 for x in s)
    assert abs(s[-1]["dist_m"] - 500) < 30
    assert s[0]["elev_gain_m"] > 0


def test_set_category(client):
    _upload(client, "garmin_run.tcx")
    assert client.get("/api/activities/1").json()["category"] == "run"
    r = client.patch("/api/activities/1/category", json={"category": "bike"})
    assert r.status_code == 200
    assert client.get("/api/activities/1").json()["category"] == "bike"
    assert client.patch("/api/activities/1/category",
                        json={"category": "swim"}).status_code == 400
    assert client.patch("/api/activities/99/category",
                        json={"category": "run"}).status_code == 404
