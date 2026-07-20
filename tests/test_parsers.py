"""Регрессионные тесты парсеров — самое хрупкое место проекта.

Фикстуры имитируют вендорские особенности:
- garmin_run.tcx — полные данные (HR, DistanceMeters, TPX Speed)
- huawei_run.tcx — «голый» экспорт Health: только GPS и высота
- strava_ride.gpx — GPX с пульсом/каденсом в gpxtpx-расширениях
- broken.tcx — мусор
"""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent.parent / "app"))

from parser import parse_activity, sport_category  # noqa: E402

FIX = Path(__file__).parent / "fixtures"


@pytest.mark.parametrize("sport,expected", [
    ("Бег на улице", "run"), ("Running", "run"), ("jogging", "run"),
    ("Езда на велосипеде", "bike"), ("Biking", "bike"), ("Cycling", "bike"), ("Ride", "bike"),
    ("Ходьба", "other"), ("Плавание", "other"), (None, "other"), ("", "other"),
])
def test_sport_category(sport, expected):
    assert sport_category(sport) == expected


def test_garmin_full_tcx():
    a = parse_activity(str(FIX / "garmin_run.tcx"))
    assert a.sport == "Running"
    assert a.category == "run"
    assert a.calories == 88
    # tcxreader считает hr_avg/max по трекпоинтам, а не по сводке Lap
    assert a.hr_avg == pytest.approx(145.6, abs=1)
    assert a.hr_max == 149
    assert len(a.trackpoints) == 60
    # родные значения не перетираются fallback'ом
    assert a.trackpoints[10].distance is not None
    assert a.trackpoints[10].speed == pytest.approx(2.8, abs=0.5)
    assert a.trackpoints[10].hr is not None
    assert a.duration_s == pytest.approx(295, abs=1)
    assert a.start_time.tzinfo is not None, "start_time обязан быть aware-UTC"


def test_huawei_degraded_tcx_haversine_fallback():
    a = parse_activity(str(FIX / "huawei_run.tcx"))
    assert a.category == "run"                     # русское название распознано
    assert a.hr_avg is None and a.hr_max is None   # пульса нет — не выдумываем
    tp = a.trackpoints[30]
    assert tp.distance is not None and tp.distance > 0
    assert tp.speed is not None and 0.5 < tp.speed < 10
    dists = [t.distance for t in a.trackpoints]
    assert dists == sorted(dists), "накопленная дистанция монотонна"
    assert a.trackpoints[-1].distance == pytest.approx(a.distance_m, rel=0.05)
    assert a.start_time.tzinfo is not None


def test_strava_gpx_extensions():
    a = parse_activity(str(FIX / "strava_ride.gpx"))
    assert a.category == "bike"                    # <type>cycling</type>
    assert a.hr_avg is not None and 120 < a.hr_avg < 140
    tp = a.trackpoints[20]
    assert tp.hr is not None
    assert tp.cadence == 88
    assert tp.distance is not None                 # GPX без дистанции -> хаверсин
    assert a.distance_m > 0 and a.duration_s == pytest.approx(295, abs=1)
    assert a.start_time.tzinfo is not None


def test_broken_file_raises():
    with pytest.raises(Exception):
        parse_activity(str(FIX / "broken.tcx"))


def test_unsupported_extension_rejected():
    with pytest.raises(ValueError, match="unsupported format"):
        parse_activity("/tmp/whatever.fit")


def test_gpx_route_without_timestamps(tmp_path):
    """AllTrails и подобные экспортируют маршруты без <time>: дистанция
    считается по координатам, дата и длительность отсутствуют."""
    gpx = tmp_path / "route.gpx"
    gpx.write_text(
        '<?xml version="1.0"?><gpx version="1.1" creator="AllTrails.com" '
        'xmlns="http://www.topografix.com/GPX/1/1"><trk>'
        '<name><![CDATA[Бег по Битце]]></name><trkseg>'
        '<trkpt lat="55.6088" lon="37.5741"><ele>200</ele></trkpt>'
        '<trkpt lat="55.6098" lon="37.5741"><ele>210</ele></trkpt>'
        '<trkpt lat="55.6108" lon="37.5741"><ele>205</ele></trkpt>'
        '</trkseg></trk></gpx>', encoding="utf-8")
    act = parse_activity(str(gpx))
    assert act.start_time is None and act.duration_s == 0.0
    assert 200 < act.distance_m < 250          # 2 шага по ~111 м
    assert act.category == "run"               # по названию трека
    assert len(act.trackpoints) == 3
    assert act.trackpoints[-1].distance == act.distance_m
