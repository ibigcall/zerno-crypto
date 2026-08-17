#!/usr/bin/env bash
# Запуск: ./run.sh web | ./run.sh bot | ./run.sh prod
set -euo pipefail
cd "$(dirname "$0")"

PY=${PYTHON:-python3}
[ -d .venv ] && PY=.venv/bin/python
[ -f .env ] || { echo "Нет .env — скопируйте .env.example и заполните"; exit 1; }

case "${1:-web}" in
  web)  exec "$PY" -m backend.app ;;
  bot)  exec "$PY" -m bot.bot ;;
  prod) exec "$PY" -m waitress --host "${HOST:-127.0.0.1}" --port "${PORT:-5310}" \
          --threads 8 backend.app:app ;;
  *)    echo "Использование: $0 [web|bot|prod]"; exit 1 ;;
esac
