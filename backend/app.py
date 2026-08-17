"""HTTP-слой: JSON API для веб-интерфейса и Telegram-бота + раздача фронтенда.

Область данных задаётся параметром scope: "web" — список в браузере,
"tg:<chat_id>" — личный список подписчика бота. К чужим tg-областям пускаем
только по общему секрету BOT_API_TOKEN (заголовок X-Bot-Token).
"""
import logging
import re

from flask import Flask, jsonify, request, send_from_directory

from . import analyst, config, db, market, ollama_client
from .freecrypto import FreeCryptoError

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("zerno.app")

SYMBOL_RE = re.compile(r"^[A-Z0-9]{2,15}$")
SCOPE_RE = re.compile(r"^(web|tg:-?\d+)$")

app = Flask(__name__, static_folder=None)
db.init()


# ── вспомогательное ──────────────────────────────────────────────────────────

class ApiError(Exception):
    def __init__(self, message, code=400):
        super().__init__(message)
        self.message = message
        self.code = code


@app.errorhandler(ApiError)
def _api_error(exc):
    return jsonify({"ok": False, "error": exc.message}), exc.code


@app.errorhandler(FreeCryptoError)
def _fca_error(exc):
    return jsonify({"ok": False, "error": str(exc)}), 502


def scope_arg(payload=None):
    raw = (payload or {}).get("scope") or request.args.get("scope") or "web"
    scope = str(raw).strip()
    if not SCOPE_RE.match(scope):
        raise ApiError("Некорректная область данных")
    if scope != "web":
        token = request.headers.get("X-Bot-Token", "")
        if not config.BOT_API_TOKEN or token != config.BOT_API_TOKEN:
            raise ApiError("Нет доступа к этой области", 403)
        db.seed_scope(scope, config.DEFAULT_WATCHLIST)
    return scope


def symbol_arg(value):
    sym = str(value or "").strip().upper()
    if not SYMBOL_RE.match(sym):
        raise ApiError("Тикер выглядит неправильно: ожидаются 2–15 латинских букв или цифр")
    return sym


def body():
    return request.get_json(silent=True) or {}


def truthy(value):
    return str(value).lower() in ("1", "true", "yes", "on")


# ── API ──────────────────────────────────────────────────────────────────────

@app.get("/api/health")
def health():
    ok, tags = ollama_client.available()
    return jsonify({
        "ok": True,
        "data_source": "demo" if config.FCA_DEMO else "freecryptoapi",
        "ollama": {"up": ok, "model": config.OLLAMA_MODEL, "ready": ollama_client.model_ready(),
                   "models": tags[:20]},
        "flags": {"technical": config.ENABLE_TECHNICAL, "news": config.ENABLE_NEWS,
                  "spark_days": config.SPARK_DAYS},
    })


@app.get("/api/snapshot")
def snapshot():
    """Быстрый ответ: рынок + уже готовые разборы из кэша. Модель не запускаем."""
    scope = scope_arg()
    force = truthy(request.args.get("refresh"))
    snap = market.build_snapshot(scope, force=force)
    notes = db.notes_list(scope, limit=5, max_age=7 * 86400)
    analyst.attach_cached(snap, notes)
    snap["notes"] = notes
    snap["ok"] = True
    return jsonify(snap)


@app.post("/api/analysis")
def analysis():
    """Разбор через локальную модель. kind = portfolio | token."""
    data = body()
    scope = scope_arg(data)
    kind = (data.get("kind") or "portfolio").strip()
    force = truthy(data.get("force"))
    snap = market.build_snapshot(scope)
    notes = db.notes_list(scope, limit=5, max_age=7 * 86400)

    if kind == "portfolio":
        result = analyst.analyze_portfolio(snap, notes, force=force)
        return jsonify({"ok": True, "kind": "portfolio", "analysis": result,
                        "portfolio": snap["portfolio"]})

    if kind == "token":
        sym = symbol_arg(data.get("symbol"))
        token = next((t for t in snap["tokens"] if t["symbol"] == sym), None)
        if not token:
            raise ApiError(f"{sym} нет в списке наблюдения", 404)
        extra = analyst.extra_for(snap, token, notes)
        result = analyst.analyze_token(scope, token, extra, force=force)
        return jsonify({"ok": True, "kind": "token", "symbol": sym, "analysis": result})

    raise ApiError("Неизвестный тип разбора")


@app.get("/api/digest")
def digest():
    """Готовый текстовый дайджест — им пользуется Telegram-бот."""
    scope = scope_arg()
    force = truthy(request.args.get("refresh"))
    snap = market.build_snapshot(scope, force=force)
    notes = db.notes_list(scope, limit=5, max_age=7 * 86400)
    snap["analysis"] = analyst.analyze_portfolio(snap, notes, force=force)
    for token in snap["tokens"]:
        token["analysis"] = analyst.cached_token(scope, token, notes)
    snap["disclaimer"] = analyst.DISCLAIMER
    snap["ok"] = True
    return jsonify(snap)


@app.get("/api/watchlist")
def watchlist_get():
    scope = scope_arg()
    return jsonify({"ok": True, "scope": scope, "items": db.watchlist(scope)})


@app.post("/api/watchlist")
def watchlist_post():
    data = body()
    scope = scope_arg(data)
    sym = symbol_arg(data.get("symbol"))
    items = db.watchlist(scope)
    if any(i["symbol"] == sym for i in items):
        return jsonify({"ok": True, "scope": scope, "items": items, "added": False})
    if len(items) >= 25:
        raise ApiError("В списке уже 25 токенов — уберите лишний")
    # проверяем, что провайдер знает такой тикер
    quotes = market.client().quotes([sym])
    if sym not in quotes or quotes[sym]["price"] is None:
        raise ApiError(f"{sym}: провайдер не отдал котировку по такому тикеру", 404)
    db.watchlist_add(scope, sym, quotes[sym].get("name") or market.NAMES.get(sym))
    return jsonify({"ok": True, "scope": scope, "items": db.watchlist(scope), "added": True})


@app.delete("/api/watchlist/<symbol>")
def watchlist_delete(symbol):
    scope = scope_arg()
    sym = symbol_arg(symbol)
    db.watchlist_remove(scope, sym)
    return jsonify({"ok": True, "scope": scope, "items": db.watchlist(scope)})


@app.get("/api/search")
def search():
    query = (request.args.get("q") or "").strip().upper()
    try:
        catalog = market.client().crypto_list()
    except FreeCryptoError as exc:
        return jsonify({"ok": False, "error": str(exc), "items": []}), 502
    if query:
        catalog = [c for c in catalog
                   if query in c["symbol"] or query in (c["name"] or "").upper()]
    return jsonify({"ok": True, "items": catalog[:30]})


@app.get("/api/notes")
def notes_get():
    scope = scope_arg()
    return jsonify({"ok": True, "items": db.notes_list(scope, limit=25)})


@app.post("/api/notes")
def notes_post():
    data = body()
    scope = scope_arg(data)
    text = (data.get("text") or "").strip()
    if len(text) < 10:
        raise ApiError("Слишком коротко — нужен хотя бы десяток знаков")
    note_id = db.notes_add(scope, text[:4000], data.get("source") or "web")
    return jsonify({"ok": True, "id": note_id, "items": db.notes_list(scope, limit=25)})


@app.delete("/api/notes/<int:note_id>")
def notes_delete(note_id):
    scope = scope_arg()
    db.notes_delete(scope, note_id)
    return jsonify({"ok": True, "items": db.notes_list(scope, limit=25)})


# ── фронтенд ─────────────────────────────────────────────────────────────────

@app.get("/")
def index():
    return send_from_directory(config.FRONTEND_DIR, "index.html")


@app.get("/<path:path>")
def static_files(path):
    return send_from_directory(config.FRONTEND_DIR, path)


def main():
    log.info("Зерно: источник данных — %s, модель — %s",
             "демо" if config.FCA_DEMO else "FreeCryptoAPI", config.OLLAMA_MODEL)
    app.run(host=config.HOST, port=config.PORT, debug=False, threaded=True)


if __name__ == "__main__":
    main()
