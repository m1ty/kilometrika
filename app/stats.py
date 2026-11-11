"""Календарная статистика по категориям — общая для кадра рамки и MQTT.

Отдельный модуль без Pillow: цифры для Home Assistant обязаны считаться,
даже если рендер картинки невозможен (нет Pillow, нет шрифтов).
"""
from __future__ import annotations

from datetime import datetime, timezone


def local_dt(iso: str) -> datetime:
    """Строка из БД -> локальное время (naive считаем UTC)."""
    dt = datetime.fromisoformat(iso)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone()


def totals(acts, category: str) -> dict:
    """Километраж за текущие неделю (ISO), месяц и год в локальном поясе."""
    now = datetime.now().astimezone()
    iso_now = now.isocalendar()[:2]
    out = {"week": 0.0, "month": 0.0, "year": 0.0}
    for a in acts:
        if not a["start_time"] or a.get("category") != category:
            continue
        dt = local_dt(a["start_time"])
        km = (a["distance_m"] or 0) / 1000
        if dt.year == now.year:
            out["year"] += km
            if dt.month == now.month:
                out["month"] += km
        if dt.isocalendar()[:2] == iso_now:
            out["week"] += km
    return out


def last_of(acts, category: str) -> dict | None:
    """Последняя тренировка категории (acts отсортированы по дате DESC)."""
    for a in acts:
        if a["start_time"] and a.get("category") == category:
            return a
    return None
