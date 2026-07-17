#!/usr/bin/env bash
# Деплой kilometrika из git-клона на pve в LXC-контейнер.
# Запуск: ./deploy.sh [CTID]   (по умолчанию 130)
set -euo pipefail

CT="${1:-130}"
APP=/opt/tcx-analyzer/app

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

echo "== restart =="
pct exec "$CT" -- systemctl restart tcx-analyzer
sleep 2

echo "== проверка =="
pct exec "$CT" -- systemctl is-active tcx-analyzer
n=$(pct exec "$CT" -- ls "$APP/static/vendor" 2>/dev/null | wc -l)
echo "  vendor-файлов на месте: $n"
code=$(pct exec "$CT" -- python3 -c "
import urllib.request
print(urllib.request.urlopen('http://localhost:8000/api/activities').status)" 2>/dev/null || echo FAIL)
echo "  API отвечает: $code"
echo "OK. Не забудь Ctrl+F5 в браузере."
