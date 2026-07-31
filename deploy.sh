#!/usr/bin/env bash
# Деплой kilometrika из git-клона на pve в LXC-контейнер.
# Запуск: ./deploy.sh [CTID]   (по умолчанию 130)
set -euo pipefail

CT="${1:-130}"
BASE=/opt/kilometrika
APP="$BASE/app"

cd "$(dirname "$0")"

if [ ! -d .git ]; then
    echo "ВНИМАНИЕ: это не git-клон — деплой продолжится, но версии не под контролем" >&2
fi

echo "== push дерева app/ в CT $CT =="
find app -type f \( -name "*.py" -o -name "*.html" -o -name "*.css" -o -name "*.js" \
                    -o -name "*.png" -o -name "*.jpg" -o -name "*.svg" \) \
    ! -path "*__pycache__*" | while read -r f; do
    rel="${f#app/}"
    dir="$(dirname "$rel")"
    [ "$dir" != "." ] && pct exec "$CT" -- mkdir -p "$APP/$dir"
    pct push "$CT" "$f" "$APP/$rel"
    echo "  $rel"
done

echo "== зависимости =="
# requirements.txt тоже едет в контейнер: иначе пересоздание venv там ставит
# библиотеки по устаревшему списку (так однажды потерялся Pillow)
local_sum=$(md5sum requirements.txt | cut -d' ' -f1)
remote_sum=$(pct exec "$CT" -- md5sum "$BASE/requirements.txt" 2>/dev/null | cut -d' ' -f1 || true)
if [ "$local_sum" != "$remote_sum" ]; then
    pct push "$CT" requirements.txt "$BASE/requirements.txt"
    pct exec "$CT" -- "$BASE/venv/bin/pip" install -q -r "$BASE/requirements.txt"
    pct exec "$CT" -- chown -R tcx:tcx "$BASE/venv"
    echo "  список обновлён, зависимости доустановлены"
else
    echo "  без изменений"
fi

echo "== restart =="
pct exec "$CT" -- systemctl restart kilometrika
sleep 2

echo "== проверка =="
pct exec "$CT" -- systemctl is-active kilometrika
n=$(pct exec "$CT" -- ls "$APP/static/vendor" 2>/dev/null | wc -l)
echo "  vendor-файлов на месте: $n"
code=$(pct exec "$CT" -- python3 -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8000/api/activities').status)" 2>/dev/null || echo FAIL)
echo "  API отвечает: $code"
echo "OK. Не забудь Ctrl+F5 в браузере."
