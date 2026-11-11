"""Render a 800x480 landscape stats card for a Spectra 6 (E6) e-ink frame.

Layout: two halves — running (left, green) and cycling (right, blue).
Each half: sport pictogram on the left, last workout (date + distance)
on the right, and week/month/year calendar totals below.
Pure palette colors only, so the firmware maps them 1:1 without dithering.
"""
from __future__ import annotations

import hashlib
import io
import json
import os
import logging
from datetime import datetime

from PIL import Image, ImageDraw, ImageFont

from stats import last_of as _last_of, local_dt as _local, totals as _totals

W, H = 800, 480
RENDER_VERSION = 10  # менять при правках макета — форсирует перерисовку рамки

FRAME_LANG = os.environ.get("FRAME_LANG", "ru")   # язык кадра: ru | en
_L10N = {
    "ru": {"week": "Неделя", "month": "Месяц", "year": "Год",
           "km": "км", "empty": "Нет тренировок"},
    "en": {"week": "Week", "month": "Month", "year": "Year",
           "km": "km", "empty": "No workouts yet"},
}
if FRAME_LANG not in _L10N:          # опечатка в env не должна ронять кадр
    logging.getLogger("kilometrika").warning(
        "unknown FRAME_LANG=%r, falling back to 'ru'", FRAME_LANG)
    FRAME_LANG = "ru"
L = _L10N[FRAME_LANG]

WHITE = (255, 255, 255)
BLACK = (0, 0, 0)
RED = (255, 0, 0)
YELLOW = (255, 255, 0)
GREEN = (0, 160, 70)
BLUE = (0, 70, 200)

_FONT_DIR = "/usr/share/fonts/truetype/dejavu"
_CANDIDATES = {
    True:  ["DejaVuSansCondensed-Bold.ttf", "DejaVuSans-Bold.ttf"],
    False: ["DejaVuSansCondensed.ttf", "DejaVuSans.ttf"],
}


def _font(size: int, bold: bool = True) -> ImageFont.FreeTypeFont:
    for name in _CANDIDATES[bold]:
        try:
            return ImageFont.truetype(f"{_FONT_DIR}/{name}", size)
        except OSError:
            continue
    return ImageFont.load_default(size)


# ---------------------------------------------------------------- pictograms
# Официальные Material Icons (directions_run / directions_bike), Apache 2.0,
# https://github.com/google/material-design-icons — PNG в app/assets/.
# Перекрашиваем чёрный глиф в палитрный цвет через альфа-маску.

_ASSET_DIR = os.path.join(os.path.dirname(__file__), "assets")
_ICON_CACHE: dict = {}


def _icon(name: str, size: int, color) -> Image.Image:
    key = (name, size, color)
    if key not in _ICON_CACHE:
        src = Image.open(os.path.join(_ASSET_DIR, f"{name}.png")).convert("RGBA")
        src = src.resize((size, size), Image.LANCZOS)
        tinted = Image.new("RGBA", src.size, color + (255,))
        tinted.putalpha(src.getchannel("A"))
        _ICON_CACHE[key] = tinted
    return _ICON_CACHE[key]


def paste_icon(img: Image.Image, name: str, cx: int, cy: int, size: int, color):
    ic = _icon(name, size, color)
    img.paste(ic, (cx - size // 2, cy - size // 2), ic)


# ---------------------------------------------------------------- data

# _totals / _last_of / _local живут в stats.py (без Pillow) — общие с MQTT

def _fmt_km(v: float) -> str:
    # Десятые везде, чтобы бег и велосипед выглядели одинаково; целое — только
    # для очень больших значений (годовой пробег ≥ 1000 км), где десятая доля
    # всё равно шум, а лишний знак не влезает в колонку.
    return f"{v:.0f}" if v >= 1000 else f"{v:.1f}"


# ---------------------------------------------------------------- layout

def _draw_half(img: Image.Image, d: ImageDraw.ImageDraw, x0: int, x1: int,
               icon_name: str, color, totals: dict, last: dict | None) -> None:
    cx = (x0 + x1) // 2
    d.rectangle([x0 + 24, 26, x1 - 24, 36], fill=color)

    # ── верх: пиктограмма слева, последняя тренировка справа ──
    paste_icon(img, icon_name, x0 + 95, 150, 120, color)
    tcx = (x0 + 190 + x1 - 30) // 2     # центр текстовой зоны правее иконки
    if last:
        dt = _local(last["start_time"])
        d.text((tcx, 82), dt.strftime("%d.%m.%Y"), font=_font(26, False),
               fill=BLACK, anchor="ma")
        v = _fmt_km((last["distance_m"] or 0) / 1000)
        f_big = _font(72)
        vw = d.textlength(v, font=f_big)
        kmw = d.textlength(L["km"], font=_font(28))
        sx = tcx - (vw + 8 + kmw) / 2
        d.text((sx, 118), v, font=f_big, fill=color)
        d.text((sx + vw + 8, 162), L["km"], font=_font(28), fill=BLACK)
    else:
        d.text((tcx, 130), "—", font=_font(56), fill=BLACK, anchor="ma")

    # ── низ: календарные итоги ──
    d.line([x0 + 40, 300, x1 - 40, 300], fill=BLACK, width=1)
    cols = [(L["week"], totals["week"]), (L["month"], totals["month"]),
            (L["year"], totals["year"])]
    cw = (x1 - x0) / 3
    for i, (label, km) in enumerate(cols):
        ccx = x0 + cw * i + cw / 2
        d.text((ccx, 330), label, font=_font(20, False), fill=BLACK, anchor="ma")
        d.text((ccx, 360), _fmt_km(km), font=_font(38), fill=color, anchor="ma")
        d.text((ccx, 412), L["km"], font=_font(18, False), fill=BLACK, anchor="ma")


def render_frame(store) -> bytes:
    acts = store.list_activities()
    img = Image.new("RGB", (W, H), WHITE)
    d = ImageDraw.Draw(img)

    if not acts:
        d.text((W // 2, H // 2), L["empty"], font=_font(36),
               fill=BLACK, anchor="mm")
        return _png(img)

    d.line([W // 2, 24, W // 2, H - 24], fill=BLACK, width=2)
    _draw_half(img, d, 0, W // 2, "run", GREEN,
               _totals(acts, "run"), _last_of(acts, "run"))
    _draw_half(img, d, W // 2, W, "bike", BLUE,
               _totals(acts, "bike"), _last_of(acts, "bike"))
    return _png(img)


def _png(img: Image.Image) -> bytes:
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def frame_etag(store) -> str:
    acts = store.list_activities()
    # отпечаток всех полей, от которых зависит кадр: импорт, удаление и ручная
    # смена категории (бег ↔ велосипед) — всё должно перерисовать рамку
    sig = hashlib.sha256(json.dumps(
        [(a["id"], a.get("category"), a["distance_m"], a["start_time"]) for a in acts]
    ).encode()).hexdigest()[:12]
    now = datetime.now()
    week = now.isocalendar()[1]
    return f'"v{RENDER_VERSION}{FRAME_LANG}-{sig}-{now:%Y%m}-w{week}"'
