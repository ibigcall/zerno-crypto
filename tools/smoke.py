"""Дымовой тест: гоняет живой бэкенд по всем ручкам.

Запуск (бэкенд уже поднят):
    python3 tools/smoke.py [--base http://127.0.0.1:5310] [--llm]

Без --llm разборы не запрашиваются с force, поэтому тест проходит за секунды.
С --llm гоняется полный цикл через локальную модель (минуты).
"""
import argparse
import sys

import requests

sys.path.insert(0, __file__.rsplit("/tools/", 1)[0])
from backend import config  # noqa: E402

FAILED = []


def check(name, condition, detail=""):
    mark = "ок " if condition else "ПАДЕНИЕ"
    print(f"[{mark}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILED.append(name)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=f"http://{config.HOST}:{config.PORT}")
    parser.add_argument("--llm", action="store_true", help="гонять разборы через модель")
    args = parser.parse_args()
    base = args.base.rstrip("/")
    bot = {"X-Bot-Token": config.BOT_API_TOKEN}

    health = requests.get(f"{base}/api/health", timeout=30).json()
    check("health отвечает", health.get("ok") is True, health.get("data_source"))
    check("Ollama видна", health["ollama"]["up"],
          f"{health['ollama']['model']}, скачана: {health['ollama']['ready']}")

    snap = requests.get(f"{base}/api/snapshot", timeout=120).json()
    check("снимок собран", snap.get("ok") is True, f"{len(snap['tokens'])} токенов")
    check("предупреждений нет", not snap["warnings"], "; ".join(snap["warnings"][:2]))
    caps = snap.get("capabilities") or {}
    check("возможности тарифа определены", "history" in caps,
          f"история: {caps.get('history')}, страх и жадность: {caps.get('fear_greed')}")
    for token in snap["tokens"]:
        check(f"{token['symbol']}: есть котировка",
              token["price"] is not None,
              f"{token['price_text']}, сутки {token['change_24h_text']}, риск {token['risk']}, "
              f"история: {token['history_source'] or 'ещё копится'}")
        # график есть только там, где история уже набралась — на тарифе без
        # исторических эндпоинтов это нормально в первые дни работы
        if token["history_source"]:
            check(f"{token['symbol']}: график построен", bool(token["spark"]),
                  f"{len(token['series'])} точек")

    added = requests.post(f"{base}/api/watchlist", json={"symbol": "DOGE"}, timeout=60).json()
    check("токен добавляется", added.get("ok") is True,
          ",".join(i["symbol"] for i in added["items"]))
    bad = requests.post(f"{base}/api/watchlist", json={"symbol": "плохо"}, timeout=30)
    check("мусорный тикер отбивается", bad.status_code == 400, bad.text[:60])
    removed = requests.delete(f"{base}/api/watchlist/DOGE", timeout=30).json()
    check("токен удаляется", all(i["symbol"] != "DOGE" for i in removed["items"]))

    note = requests.post(f"{base}/api/notes",
                         json={"text": "Дымовой тест: контекст из Telegram."}, timeout=30).json()
    check("заметка создаётся", note.get("ok") is True, f"id {note['id']}")
    dropped = requests.delete(f"{base}/api/notes/{note['id']}", timeout=30).json()
    check("заметка удаляется", all(n["id"] != note["id"] for n in dropped["items"]))

    denied = requests.get(f"{base}/api/snapshot?scope=tg:999", timeout=30)
    check("чужая область закрыта без токена", denied.status_code == 403)
    allowed = requests.get(f"{base}/api/snapshot?scope=tg:999", headers=bot, timeout=120)
    check("бот пускается по токену", allowed.status_code == 200,
          allowed.json().get("scope") if allowed.ok else allowed.text[:80])

    for path in ("/", "/app.js", "/app.css", "/vendor/organic.css"):
        resp = requests.get(f"{base}{path}", timeout=30)
        check(f"статика {path}", resp.status_code == 200, f"{len(resp.content)} байт")

    if args.llm:
        res = requests.post(f"{base}/api/analysis",
                            json={"kind": "portfolio", "force": True}, timeout=900).json()
        analysis = res.get("analysis") or {}
        check("сводка от модели", analysis.get("_source") == "llm",
              f"{analysis.get('_source')}: {analysis.get('headline')}")
        sym = snap["tokens"][0]["symbol"]
        res = requests.post(f"{base}/api/analysis",
                            json={"kind": "token", "symbol": sym, "force": True},
                            timeout=900).json()
        analysis = res.get("analysis") or {}
        check(f"разбор {sym} от модели", analysis.get("_source") == "llm",
              f"{analysis.get('_source')}: {analysis.get('short')}")

    print()
    if FAILED:
        print(f"Провалено проверок: {len(FAILED)} — {', '.join(FAILED)}")
        sys.exit(1)
    print("Все проверки прошли.")


if __name__ == "__main__":
    main()
