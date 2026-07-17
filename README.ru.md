# Километрика

Self-hosted контейнер для обработки и анализа тренировок в формате TCX (Garmin Training Center XML). Парсит файлы, складывает в SQLite, показывает дашборд с графиками пульса/скорости/рельефа и недельным километражем. Опционально публикует сводку в MQTT для Home Assistant.

## Запуск

```bash
docker compose up -d --build
# UI: http://<host>:8000
```

Данные живут в `./data`:

```
data/
├── tcx.db      # SQLite: активности + трекпоинты
├── inbox/      # сюда кидать .tcx (SMB/FTP/rsync/scp) — подхватятся сами
├── archive/    # успешно импортированные
└── failed/     # файлы с ошибками парсинга
```

Два пути загрузки: drag&drop в веб-интерфейсе или watch-папка `inbox` (скан каждые `WATCH_INTERVAL` секунд, по умолчанию 30). Дубликаты отсекаются по SHA-256 содержимого.

## API

```
POST   /api/upload                          — загрузка .tcx
GET    /api/activities                      — список тренировок
GET    /api/activities/{id}                 — детали + круги (laps)
GET    /api/activities/{id}/trackpoints?step=N  — серия точек (step прореживает)
DELETE /api/activities/{id}
GET    /api/summary/weekly?weeks=12         — итоги по неделям
```

Через API удобно тянуть данные в pandas/Jupyter для более глубокого анализа, чем показывает дашборд.

## Интеграция с Home Assistant (MQTT)

Раскомментируй переменные `MQTT_*` в `docker-compose.yml`. После каждого импорта контейнер публикует retained-сообщение в `tcx_analyzer/state`:

```json
{
  "last_sport": "Running",
  "last_distance_km": 5.2,
  "last_duration_min": 30.0,
  "last_hr_avg": 134.7,
  "week_distance_km": 5.2,
  "week_activities": 1,
  "total_activities": 1
}
```

Сенсоры в `configuration.yaml`:

```yaml
mqtt:
  sensor:
    - name: "Последняя тренировка, км"
      state_topic: "tcx_analyzer/state"
      value_template: "{{ value_json.last_distance_km }}"
      unit_of_measurement: "km"
    - name: "Километраж за неделю"
      state_topic: "tcx_analyzer/state"
      value_template: "{{ value_json.week_distance_km }}"
      unit_of_measurement: "km"
    - name: "Средний пульс последней тренировки"
      state_topic: "tcx_analyzer/state"
      value_template: "{{ value_json.last_hr_avg }}"
      unit_of_measurement: "bpm"
```

## Заметки

- Парсер — библиотека `tcxreader`; читает Garmin/Polar/Suunto/Zwift и др., включая TPX-расширения (Speed, Watts).
- Для тяжёлых графиков используй `?step=3..10` — фронтенд уже так делает.
- SQLite достаточно для тысяч активностей; если захочется TimescaleDB/Grafana — схема `trackpoints` переносится напрямую.
