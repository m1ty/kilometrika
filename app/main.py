"""Kilometrika — FastAPI service.

Ingestion paths:
  1. Web UI / POST /api/upload — drag & drop .tcx files
  2. Watch folder /data/inbox — drop files there (SMB/FTP/rsync),
     they get parsed and moved to /data/archive (or /data/failed)

Optional: publishes summary of the latest activity + weekly totals to MQTT
(retained), so Home Assistant can pick it up via MQTT discovery.
"""
from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime
import logging
import os
import shutil
import tempfile
import threading
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, Field
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from db import Store
from parser import SUPPORTED_EXTENSIONS, parse_activity

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("kilometrika")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
INBOX = DATA_DIR / "inbox"
ARCHIVE = DATA_DIR / "archive"
FAILED = DATA_DIR / "failed"
PHOTOS = DATA_DIR / "photos"
WATCH_INTERVAL = int(os.environ.get("WATCH_INTERVAL", "30"))

MQTT_HOST = os.environ.get("MQTT_HOST", "")
MQTT_PORT = int(os.environ.get("MQTT_PORT", "1883"))
MQTT_USER = os.environ.get("MQTT_USER", "")
MQTT_PASS = os.environ.get("MQTT_PASS", "")
MQTT_PREFIX = os.environ.get("MQTT_PREFIX", "kilometrika")

store: Store | None = None


# ---------------------------------------------------------------- ingestion

class DuplicateError(ValueError):
    """Файл уже импортирован — не ошибка парсинга."""


def ingest_file(path: Path, orig_name: str | None = None) -> dict:
    """Parse one TCX file, store it, return summary. Raises on failure."""
    name = orig_name or path.name
    act = parse_activity(str(path))
    if store.has_file(act.file_hash):
        raise DuplicateError(f"duplicate: {name} already imported")
    try:
        act_id = store.save(act, name)
    except sqlite3.IntegrityError:
        # гонка has_file/save (параллельный upload + скан инбокса)
        raise DuplicateError(f"duplicate: {name} already imported")
    log.info("imported %s -> activity #%d (%s, %.1f km)",
             name, act_id, act.sport, act.distance_m / 1000)
    schedule_mqtt_publish()
    return {"id": act_id, "sport": act.sport, "distance_m": act.distance_m}


def move_safe(src: Path, dst_dir: Path, dst_name: str | None = None) -> None:
    """Move avoiding name collisions (adds -1, -2, ... suffix)."""
    base = Path(dst_name or src.name)
    dst = dst_dir / base.name
    i = 1
    while dst.exists():
        dst = dst_dir / f"{base.stem}-{i}{base.suffix}"
        i += 1
    shutil.move(str(src), dst)


def inbox_files() -> list[Path]:
    """Файлы поддерживаемых форматов. Расширение сравниваем без учёта регистра:
    glob('*.tcx') на Linux не видит ACTIVITY.TCX, и такой файл лежал бы вечно."""
    return sorted(f for f in INBOX.iterdir()
                  if f.is_file() and f.suffix.lower() in SUPPORTED_EXTENSIONS)


async def scan_inbox(stuck: set[tuple[str, float]],
                     pending: dict[str, tuple[int, float]]) -> None:
    """Один проход по инбоксу.

    stuck   — (path, mtime) файлов, которые не удалось вынести из инбокса
              (например, CIFS только на чтение): не импортируем их по кругу.
    pending — размер и mtime файлов с прошлого прохода. Файл берём в работу,
              только если он не менялся целый интервал: медленная копия по
              SMB/FTP с паузами иначе уезжала в failed/ недописанной.
    """
    seen: set[tuple[str, float]] = set()
    waiting: dict[str, tuple[int, float]] = {}
    for f in inbox_files():
        try:
            st = f.stat()
        except OSError:                  # исчез между листингом и stat
            continue
        key = (str(f), st.st_mtime)
        seen.add(key)
        if key in stuck:
            continue
        sig = (st.st_size, st.st_mtime)
        if pending.get(str(f)) != sig:   # новый или ещё пишется — ждём проход
            waiting[str(f)] = sig
            continue

        dest = ARCHIVE
        try:
            await asyncio.to_thread(ingest_file, f)
        except DuplicateError as e:
            log.warning("%s", e)
        except Exception as e:
            log.error("failed to parse %s: %s", f.name, e)
            dest = FAILED

        try:
            await asyncio.to_thread(move_safe, f, dest)
        except OSError as e:
            log.warning(
                "cannot move %s out of inbox (%s) — check write "
                "permissions on the mount (uid mapping); "
                "file imported but left in place", f.name, e)
            stuck.add(key)
    stuck &= seen            # забываем файлы, которых больше нет в инбоксе
    pending.clear()
    pending.update(waiting)


async def watch_inbox():
    """Poll the inbox folder; no inotify needed — works on any mount.
    Resilient: never lets an exception kill the task."""
    stuck: set[tuple[str, float]] = set()
    pending: dict[str, tuple[int, float]] = {}
    last_publish = 0.0

    while True:
        try:
            if time.monotonic() - last_publish > 3600:   # раз в час
                await asyncio.to_thread(publish_mqtt_state)
                last_publish = time.monotonic()
            await scan_inbox(stuck, pending)
        except Exception as e:
            log.error("inbox scan error: %s", e)
        await asyncio.sleep(WATCH_INTERVAL)


# ---------------------------------------------------------------- MQTT (optional)

def build_mqtt_payload() -> dict:
    """Раздельная статистика по видам спорта — та же логика, что на рамке:
    категории по названию, календарные границы в локальном поясе."""
    from stats import last_of as _last_of, local_dt as _local, totals as _totals
    acts = store.list_activities()

    def side(cat: str) -> dict:
        t = _totals(acts, cat)
        last = _last_of(acts, cat)
        out = {
            "week_km": round(t["week"], 1),
            "month_km": round(t["month"], 1),
            "year_km": round(t["year"], 1),
        }
        if last:
            out["last_date"] = _local(last["start_time"]).strftime("%Y-%m-%d %H:%M")
            out["last_km"] = round((last["distance_m"] or 0) / 1000, 2)
            out["last_hr_avg"] = round(last["hr_avg"], 1) if last["hr_avg"] else None
        return out

    return {
        "run": side("run"),
        "bike": side("bike"),
        "total_activities": len(acts),
        "updated": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def publish_mqtt_state():
    if not MQTT_HOST:
        return
    try:
        import paho.mqtt.publish as mqtt_publish
        payload = build_mqtt_payload()
        auth = {"username": MQTT_USER, "password": MQTT_PASS} if MQTT_USER else None
        msgs = [
            {"topic": f"{MQTT_PREFIX}/state",
             "payload": json.dumps(payload, ensure_ascii=False), "retain": True},
        ]
        # PNG кадра — для MQTT-камеры в Home Assistant (~30-50 КБ, retained).
        # Рендер отделён от статистики: если он упадёт (нет Pillow, нет шрифтов),
        # цифры в HA всё равно обновятся — молчать будет только картинка.
        try:
            import frame
            msgs.append({"topic": f"{MQTT_PREFIX}/frame",
                         "payload": frame.render_frame(store), "retain": True})
        except Exception as e:
            log.warning("frame render failed, publishing stats only: %s", e)
        mqtt_publish.multiple(msgs, hostname=MQTT_HOST, port=MQTT_PORT, auth=auth)
        log.info("published %s to mqtt %s/%s",
                 "state+frame" if len(msgs) > 1 else "state", MQTT_HOST, MQTT_PREFIX)
    except Exception as e:
        log.warning("mqtt publish failed: %s", e)


_mqtt_timer: threading.Timer | None = None
_mqtt_lock = threading.Lock()


def schedule_mqtt_publish(delay: float = 2.0) -> None:
    """Публикация в фоне с дебаунсом. HTTP-запрос не ждёт брокера (при
    недоступном MQTT это был TCP-таймаут на каждое действие), а пакетный
    импорт даёт одну публикацию и один рендер кадра, а не по штуке на файл."""
    global _mqtt_timer
    if not MQTT_HOST:
        return
    with _mqtt_lock:
        if _mqtt_timer:
            _mqtt_timer.cancel()
        _mqtt_timer = threading.Timer(delay, publish_mqtt_state)
        _mqtt_timer.daemon = True
        _mqtt_timer.start()


# ---------------------------------------------------------------- app

@asynccontextmanager
async def lifespan(app: FastAPI):
    global store
    for d in (DATA_DIR, INBOX, ARCHIVE, FAILED, PHOTOS):
        d.mkdir(parents=True, exist_ok=True)
    store = Store(str(DATA_DIR / "tcx.db"))
    task = asyncio.create_task(watch_inbox())
    yield
    task.cancel()


app = FastAPI(title="Kilometrika", lifespan=lifespan)


@app.post("/api/upload")
async def upload(file: UploadFile):
    if not file.filename or not file.filename.lower().endswith(SUPPORTED_EXTENSIONS):
        raise HTTPException(400, f"expected one of: {', '.join(SUPPORTED_EXTENSIONS)}")
    suffix = Path(file.filename).suffix.lower()
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(await file.read())
        tmp_path = Path(tmp.name)
    try:
        try:
            result = await asyncio.to_thread(ingest_file, tmp_path, file.filename)
        except DuplicateError as e:
            raise HTTPException(409, str(e))
        except Exception as e:
            raise HTTPException(422, f"parse error: {e}")
        # активность уже сохранена — проблемы с архивацией не должны ронять запрос
        try:
            move_safe(tmp_path, ARCHIVE, dst_name=Path(file.filename).name)
        except OSError as e:
            log.warning("imported %s but could not archive it: %s", file.filename, e)
        return result
    finally:
        tmp_path.unlink(missing_ok=True)


@app.get("/api/activities")
def activities():
    return store.list_activities()


@app.get("/api/activities/{act_id}")
def activity(act_id: int):
    act = store.get_activity(act_id)
    if not act:
        raise HTTPException(404, "not found")
    act["laps"] = json.loads(act.pop("laps_json") or "[]")
    act["photos"] = [
        {**p, "kind": media_kind(p["filename"])} for p in store.list_photos(act_id)
    ]
    return act


@app.get("/api/activities/{act_id}/trackpoints")
def trackpoints(act_id: int, step: int = Query(1, ge=1, le=1000)):
    """step > 1 thins the series for lighter charts (e.g. step=5).
    Пустой список — валидный ответ: бывают активности без точек."""
    if not store.get_activity(act_id):
        raise HTTPException(404, "not found")
    tps = store.get_trackpoints(act_id)
    out = tps[::step]
    if tps and out[-1] is not tps[-1]:   # прореживание не должно терять финиш
        out.append(tps[-1])
    return out


@app.get("/api/activities/{act_id}/splits")
def splits(act_id: int, size: float = Query(1000, ge=100, le=100_000)):
    """Разбивка по километрам (как Splits в Strava): темп и набор высоты.
    Границы километров интерполируются между трекпоинтами; хвост короче
    50 м отбрасывается. size ограничен снизу: при size <= 0 граница не
    сдвигалась и цикл ниже не завершался (запрос вешал сервер)."""
    if not store.get_activity(act_id):
        raise HTTPException(404, "not found")
    tps = [t for t in store.get_trackpoints(act_id)
           if t["distance"] is not None and t["time"]]
    if len(tps) < 2:
        return []
    # дистанция от первой точки: трек второго/возобновлённого круга может
    # начинаться с 1500 м, и тогда первая граница «1 км» оказывается позади
    base = tps[0]["distance"]
    pts = [(t["distance"] - base, datetime.fromisoformat(t["time"]).timestamp(),
            t["elevation"]) for t in tps]
    out = []
    boundary = size
    t_cross_prev = pts[0][1]
    gain = 0.0
    for (d0, s0, e0), (d1, s1, e1) in zip(pts, pts[1:]):
        if e0 is not None and e1 is not None and e1 > e0:
            gain += e1 - e0
        while d1 >= boundary and d1 > d0:
            frac = (boundary - d0) / (d1 - d0)
            t_cross = s0 + (s1 - s0) * frac
            out.append({"km": round(boundary / 1000, 1), "dist_m": size,
                        "time_s": round(t_cross - t_cross_prev, 1),
                        "elev_gain_m": round(gain, 1)})
            t_cross_prev = t_cross
            gain = 0.0
            boundary += size
    d_last, s_last, _ = pts[-1]
    rem = d_last - (boundary - size)
    if rem > 50:
        out.append({"km": round(d_last / 1000, 2), "dist_m": round(rem, 1),
                    "time_s": round(s_last - t_cross_prev, 1),
                    "elev_gain_m": round(gain, 1)})
    for s in out:
        s["pace_s_km"] = round(s["time_s"] / (s["dist_m"] / 1000), 1) if s["dist_m"] else None
    return out


@app.delete("/api/activities/{act_id}")
def delete(act_id: int):
    store.delete_activity(act_id)
    shutil.rmtree(PHOTOS / str(act_id), ignore_errors=True)
    schedule_mqtt_publish()
    return {"ok": True}




class CategoryBody(BaseModel):
    category: str


@app.patch("/api/activities/{act_id}/category")
def set_category(act_id: int, body: CategoryBody):
    """Ручная смена категории — маршрутные GPX (AllTrails) не несут вид
    спорта, да и эвристика по названию может ошибаться."""
    if body.category not in ("run", "bike", "other"):
        raise HTTPException(400, "category must be run | bike | other")
    if not store.get_activity(act_id):
        raise HTTPException(404, "not found")
    store.set_category(act_id, body.category)
    schedule_mqtt_publish()       # категория влияет на рамку и MQTT-итоги
    return {"ok": True}


class NotesBody(BaseModel):
    notes: str = Field("", max_length=20_000)


@app.patch("/api/activities/{act_id}/notes")
def set_notes(act_id: int, body: NotesBody):
    if not store.get_activity(act_id):
        raise HTTPException(404, "not found")
    store.set_notes(act_id, body.notes.strip())
    return {"ok": True}


_IMAGE_EXT = {".jpg", ".jpeg", ".png", ".webp", ".gif"}
_VIDEO_EXT = {".mp4", ".mov", ".webm", ".m4v"}
_MEDIA_EXT = _IMAGE_EXT | _VIDEO_EXT
_MEDIA_MAX = {ext: 25 * 1024 * 1024 for ext in _IMAGE_EXT}      # фото: 25 МБ
_MEDIA_MAX.update({ext: 500 * 1024 * 1024 for ext in _VIDEO_EXT})  # видео: 500 МБ
_MAX_BODY = max(_MEDIA_MAX.values()) + 1024 * 1024    # + запас на multipart-обёртку


@app.middleware("http")
async def limit_upload_size(request: Request, call_next):
    """Starlette принимает и спулит всё тело ДО вызова эндпоинта, поэтому
    лимит внутри add_photo срабатывает лишь после загрузки целиком.
    Здесь отбиваем заведомо огромное тело по Content-Length сразу."""
    if request.method == "POST":
        cl = request.headers.get("content-length", "")
        if cl.isdigit() and int(cl) > _MAX_BODY:
            return JSONResponse(
                {"detail": f"file too large ({_MAX_BODY // 1024 // 1024} MB max)"},
                status_code=413)
    return await call_next(request)


def media_kind(filename: str) -> str:
    return "video" if Path(filename).suffix.lower() in _VIDEO_EXT else "image"


THUMB_SIZE = 400          # длинная сторона миниатюры


def make_thumb(src_path: Path) -> None:
    """Миниатюра рядом с оригиналом: .thumb/<имя.расш>.jpg. Ошибки не фатальны.
    Неудачу помечаем файлом .bad: иначе битое изображение перекодировалось бы
    заново на каждый запрос миниатюры."""
    tp = thumb_path(src_path)
    try:
        from PIL import Image, ImageOps
        tp.parent.mkdir(exist_ok=True)
        img = Image.open(src_path)
        img = ImageOps.exif_transpose(img)      # фото с телефона: уважаем ориентацию
        img.thumbnail((THUMB_SIZE, THUMB_SIZE))
        img.convert("RGB").save(tp, quality=80)
    except Exception as e:
        log.warning("thumbnail failed for %s: %s", src_path.name, e)
        try:
            tp.unlink(missing_ok=True)          # недописанный JPEG
            thumb_failed_marker(src_path).touch()
        except OSError:
            pass


def thumb_path(orig: Path) -> Path:
    # полное имя, а не stem: у sunset.jpg и sunset.png должны быть разные миниатюры.
    # Старые миниатюры (по stem) просто не находятся и лениво перегенерируются.
    return orig.parent / ".thumb" / (orig.name + ".jpg")


def thumb_failed_marker(orig: Path) -> Path:
    return orig.parent / ".thumb" / (orig.name + ".bad")


def media_response(path: Path, name: str) -> FileResponse:
    """Отдаём под оригинальным именем: inline — просмотр в браузере,
    но «Сохранить как» подставит имя файла, а не номер из URL."""
    return FileResponse(path, filename=name, content_disposition_type="inline")


@app.post("/api/activities/{act_id}/photos")
async def add_photo(act_id: int, file: UploadFile):
    if not store.get_activity(act_id):
        raise HTTPException(404, "not found")
    ext = Path(file.filename).suffix.lower()
    if ext not in _MEDIA_EXT:
        raise HTTPException(400, f"expected image or video: {', '.join(sorted(_MEDIA_EXT))}")
    limit = _MEDIA_MAX[ext]
    pdir = PHOTOS / str(act_id)
    pdir.mkdir(parents=True, exist_ok=True)
    safe = Path(file.filename).name
    # "xb" — атомарное «создать, если нет»: между проверкой exists() и open()
    # были await'ы, и две параллельные загрузки с одним именем затирали друг друга
    dst, i = pdir / safe, 1
    while True:
        try:
            out = open(dst, "xb")
            break
        except FileExistsError:
            dst = pdir / f"{Path(safe).stem}-{i}{ext}"
            i += 1
    # кусками: видео не должно проезжать через память целиком. Лимит по типу
    # проверяется тут, общий потолок — раньше, в limit_upload_size (до спула тела)
    size = 0
    try:
        with out:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > limit:
                    raise HTTPException(413, f"file too large ({limit // 1024 // 1024} MB max)")
                await asyncio.to_thread(out.write, chunk)
        if media_kind(dst.name) == "image":
            await asyncio.to_thread(make_thumb, dst)
        try:
            photo_id = store.add_photo(act_id, dst.name)
        except sqlite3.IntegrityError:
            # тренировку удалили, пока шла загрузка
            raise HTTPException(404, "not found")
    except BaseException:
        # любой сбой (413, нет места, отмена, БД) — не оставляем файл без записи в БД
        dst.unlink(missing_ok=True)
        thumb_path(dst).unlink(missing_ok=True)
        thumb_failed_marker(dst).unlink(missing_ok=True)
        raise
    return {"id": photo_id, "filename": dst.name, "kind": media_kind(dst.name)}


@app.get("/api/photos/{photo_id}")
def get_photo(photo_id: int, thumb: int = 0):
    ph = store.get_photo(photo_id)
    if not ph:
        raise HTTPException(404, "not found")
    path = PHOTOS / str(ph["activity_id"]) / ph["filename"]
    if thumb and media_kind(ph["filename"]) == "image":
        tp = thumb_path(path)
        if (not tp.exists() and path.exists()
                and not thumb_failed_marker(path).exists()):
            make_thumb(path)              # ленивая генерация для старых фото
        if tp.exists():
            return media_response(tp, tp.name)
    if not path.exists():
        raise HTTPException(404, "file missing")
    return media_response(path, ph["filename"])


@app.delete("/api/photos/{photo_id}")
def delete_photo(photo_id: int):
    ph = store.get_photo(photo_id)
    if ph:
        orig = PHOTOS / str(ph["activity_id"]) / ph["filename"]
        orig.unlink(missing_ok=True)
        thumb_path(orig).unlink(missing_ok=True)
        thumb_failed_marker(orig).unlink(missing_ok=True)
        store.delete_photo(photo_id)
    return {"ok": True}


@app.get("/api/summary/weekly")
def weekly(weeks: int = Query(12, ge=1, le=1000)):
    return store.weekly_summary(weeks)


@app.get("/api/frame.png")
def frame_png(request: Request):
    """800x480 stats card for e-ink photo frames (URL rotation source).
    Supports If-None-Match / 304 so the frame skips redraws when
    nothing new was imported — saves battery and E6 refresh cycles."""
    from frame import frame_etag, render_frame
    etag = frame_etag(store)
    if request.headers.get("if-none-match") == etag:
        return Response(status_code=304, headers={"ETag": etag})
    png = render_frame(store)
    return Response(png, media_type="image/png",
                    headers={"ETag": etag, "Cache-Control": "no-cache"})


@app.get("/")
def index():
    # no-cache: браузер обязан перепроверять страницу (по ETag) на каждом заходе,
    # иначе после деплоя крутится старый index.html из кэша — загрузка «в пустоту».
    return FileResponse(Path(__file__).parent / "static" / "index.html",
                        headers={"Cache-Control": "no-cache"})


app.mount("/static", StaticFiles(directory=Path(__file__).parent / "static"))
