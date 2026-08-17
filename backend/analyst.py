"""Аналитический слой: локальная LLM пишет разбор по готовым числам.

Правила игры:
* модель получает только посчитанные факты — цену, изменения, волатильность,
  индекс страха и жадности, рыночный контекст из Telegram; выдумывать метрики
  ей нечем и незачем;
* ответ приходит строго по JSON-схеме (structured output Ollama);
* если Ollama недоступна или отвечает мусором — включается текст по правилам,
  и в ответе честно стоит source = "rules";
* тон — спокойный разговорный русский, без советов «покупать/продавать».
"""
import logging
import threading

from . import config, db, market, ollama_client

log = logging.getLogger("zerno.analyst")

# Локальная модель всё равно считает запросы по очереди — держим одну очередь и
# на нашей стороне, иначе параллельные вкладки разогревают её вхолостую.
_LLM_LOCK = threading.Lock()

DISCLAIMER = "Это наблюдение, а не совет, куда вложить."

SYSTEM = """Ты — аналитик сервиса «Зерно». Пишешь по-русски для человека, который
держит несколько криптовалют и не работает на рынке профессионально.

Как писать:
- спокойно и по делу, короткими фразами, живым языком;
- опирайся только на переданные числа, ничего не додумывай и не выдумывай метрики;
- если число есть — назови его; если данных нет — так и скажи;
- направление движения не переворачивай: плюс перед процентом — это рост, минус — снижение;
- никаких советов покупать, продавать или держать, никаких прогнозов цены;
- не пиши «рекомендуется», «стоит», «следует», «советуем» — только наблюдения;
- не путай понятия: волатильность — это размах колебаний, а не изменение цены;
- без биржевого жаргона, без англицизмов, без эмодзи, без восклицательных знаков;
- не называй вероятностей и не обещай, что будет дальше;
- проценты пиши со знаком % и без знака валюты, цены — со знаком $;
- не обращайся к читателю по имени и не начинай с приветствия.
"""

TOKEN_SCHEMA = {
    "type": "object",
    "properties": {
        "short": {"type": "string"},
        "summary": {"type": "string"},
        "pro": {"type": "string"},
        "contra": {"type": "string"},
    },
    "required": ["short", "summary", "pro", "contra"],
}

PORTFOLIO_SCHEMA = {
    "type": "object",
    "properties": {
        "headline": {"type": "string"},
        "summary": {"type": "string"},
    },
    "required": ["headline", "summary"],
}


# ── подготовка фактов для промпта ────────────────────────────────────────────

def _direction(value):
    if value is None:
        return "нет данных"
    if value > 0.1:
        return "РОСТ"
    if value < -0.1:
        return "СНИЖЕНИЕ"
    return "БЕЗ ИЗМЕНЕНИЙ"


def _token_facts(token, extra=None):
    lines = [
        f"Токен: {token['name']} ({token['symbol']})",
        f"Цена: {token['price_text']}",
        f"Изменение за сутки: {token['change_24h_text']} — это "
        f"{_direction(token['change_24h'])}",
        (f"Изменение за неделю: {token['change_7d_text']} — это "
         f"{_direction(token['change_7d'])}")
        if token["change_7d"] is not None else
        "Недельных данных пока нет: тариф провайдера не отдаёт историю, сервис копит её сам — "
        "про неделю и месяц не пиши вообще",
        f"Дневная волатильность: {token['volatility']}% ({token['volatility_label'].lower()})"
        if token["volatility"] is not None else "Дневная волатильность: нет данных",
        f"Оценка риска (посчитана сервисом): {token['risk']}",
        f"Характер движения (посчитан сервисом): {token['trend']}",
    ]
    if token.get("volume"):
        lines.append(f"Объём за сутки: {token['volume_text']}")
    if token.get("day_low") and token.get("day_high"):
        lines.append(f"Диапазон за сутки: {market.fmt_price(token['day_low'])} — "
                     f"{market.fmt_price(token['day_high'])}")
    series = token.get("series") or []
    if len(series) >= 3:
        label = {"api": "Цены закрытия по дням",
                 "local-daily": "Цены по дням (накоплены сервисом)"}.get(
                     token.get("history_source"), "Последние наблюдения цены за сегодня")
        lines.append(label + ": " + ", ".join(market.fmt_price(v) for v in series[-10:]))
    extra = extra or {}
    if extra.get("technical"):
        ta = extra["technical"]
        lines.append(
            f"Технические показатели: RSI {ta.get('rsi')}, MACD {ta.get('macd')}, "
            f"сигнальная линия {ta.get('signal')}"
        )
    if extra.get("fear_greed", {}).get("index") is not None:
        fg = extra["fear_greed"]
        lines.append(f"Индекс страха и жадности по рынку: {fg['index']} ({fg.get('emotion')})")
    if extra.get("news"):
        lines.append("Заголовки новостей: " + "; ".join(n["title"] for n in extra["news"][:5]))
    if extra.get("notes"):
        lines.append("Рыночный контекст, присланный пользователем в Telegram:")
        lines += [f"— {n['text'][:400]}" for n in extra["notes"][:5]]
    return "\n".join(lines)


TOKEN_TASK = """{facts}

Напиши разбор в JSON:
- "short": одна фраза до 100 знаков — что происходит с токеном прямо сейчас;
- "summary": 3–4 предложения: что показывают числа, чем это объясняется, на что смотреть дальше;
- "pro": одно предложение — сильная сторона картины, желательно с числом;
- "contra": одно предложение — слабое место или риск, желательно с числом."""


def _portfolio_facts(snapshot, notes=None):
    p = snapshot["portfolio"]
    lines = [
        f"Токенов в списке: {p['count']}; выросло за сутки: {p['up']}, снизилось: {p['down']}",
        f"Среднее изменение за сутки: {market.fmt_pct(p['avg_change_24h'])} — это "
        f"{_direction(p['avg_change_24h'])}",
        (f"Среднее изменение за неделю: {market.fmt_pct(p['avg_change_7d'])} — это "
         f"{_direction(p['avg_change_7d'])}")
        if p["avg_change_7d"] is not None else
        "Недельных данных пока нет: тариф провайдера не отдаёт историю, сервис копит её сам — "
        "про неделю и месяц не пиши вообще",
        f"Средняя дневная волатильность: {p['avg_volatility']}%"
        if p["avg_volatility"] is not None else "Средняя волатильность: нет данных",
        f"Настроение выборки (посчитано сервисом): {p['mood']}",
        f"Общий риск (посчитан сервисом): {p['risk']}",
    ]
    fg = snapshot.get("fear_greed") or {}
    if fg.get("index") is not None:
        lines.append(f"Индекс страха и жадности: {fg['index']} ({fg.get('emotion')})")
    lines.append("Отдельные токены:")
    for t in snapshot["tokens"]:
        lines.append(
            f"— {t['name']} ({t['symbol']}): {t['price_text']}, за сутки {t['change_24h_text']} "
            f"({_direction(t['change_24h']).lower()}), за неделю {t['change_7d_text']}, "
            f"риск {t['risk'].lower()}, {t['trend']}"
        )
    if notes:
        lines.append("Рыночный контекст из Telegram:")
        lines += [f"— {n['text'][:400]}" for n in notes[:5]]
    return "\n".join(lines)


PORTFOLIO_TASK = """{facts}

Напиши сводку по всей выборке в JSON:
- "headline": заголовок до 60 знаков, спокойная констатация про то, что видно в цифрах
  (например «Выборка держится ровно»); не упоминай период, данных по которому нет;
- "summary": 3–5 предложений: что происходит с выборкой в целом, кто тянет вверх, кто вниз,
  насколько нервный рынок и на что смотреть в первую очередь."""


# ── разбор одного токена ─────────────────────────────────────────────────────

def cached_token(token, notes=None):
    fp = market.token_fingerprint(token, notes)
    return db.analysis_get(token["symbol"], "token", fp, config.CACHE_TTL_ANALYSIS)


def cached_portfolio(snapshot, notes=None):
    fp = market.portfolio_fingerprint(snapshot, notes)
    return db.analysis_get(snapshot["scope"], "portfolio", fp, config.CACHE_TTL_ANALYSIS)


def attach_cached(snapshot, notes=None):
    """Прицепить к снимку уже готовые разборы, не запуская модель."""
    for token in snapshot["tokens"]:
        token["analysis"] = cached_token(token, notes)
    snapshot["analysis"] = cached_portfolio(snapshot, notes)
    snapshot["disclaimer"] = DISCLAIMER
    return snapshot


def analyze_token(token, extra=None, force=False):
    fp = market.token_fingerprint(token, (extra or {}).get("notes"))
    if not force:
        cached = db.analysis_get(token["symbol"], "token", fp, config.CACHE_TTL_ANALYSIS)
        if cached:
            return cached

    with _LLM_LOCK:
        if not force:
            cached = db.analysis_get(token["symbol"], "token", fp, config.CACHE_TTL_ANALYSIS)
            if cached:
                return cached
        return _analyze_token_locked(token, extra, fp)


def _analyze_token_locked(token, extra, fp):
    payload, source, model = None, "rules", None
    try:
        data = ollama_client.chat_json(
            SYSTEM, TOKEN_TASK.format(facts=_token_facts(token, extra)), TOKEN_SCHEMA
        )
        payload = {
            "short": _clean(data.get("short"), 160),
            "summary": _clean(data.get("summary"), 900),
            "pro": _clean(data.get("pro"), 400),
            "contra": _clean(data.get("contra"), 400),
        }
        if not payload["short"] or not payload["summary"]:
            raise ollama_client.OllamaError("модель вернула пустые поля")
        source, model = "llm", config.OLLAMA_MODEL
    except ollama_client.OllamaError as exc:
        log.warning("LLM-разбор %s не удался: %s", token["symbol"], exc)
        payload = _token_rules(token)

    db.analysis_put(token["symbol"], "token", fp, payload, model, source)
    out = dict(payload)
    out["_source"] = source
    out["_model"] = model
    return out


def analyze_portfolio(snapshot, notes=None, force=False):
    fp = market.portfolio_fingerprint(snapshot, notes)
    if not force:
        cached = db.analysis_get(snapshot["scope"], "portfolio", fp, config.CACHE_TTL_ANALYSIS)
        if cached:
            return cached
    with _LLM_LOCK:
        if not force:
            cached = db.analysis_get(snapshot["scope"], "portfolio", fp,
                                     config.CACHE_TTL_ANALYSIS)
            if cached:
                return cached
        return _analyze_portfolio_locked(snapshot, notes, fp)


def _analyze_portfolio_locked(snapshot, notes, fp):
    payload, source, model = None, "rules", None
    try:
        data = ollama_client.chat_json(
            SYSTEM,
            PORTFOLIO_TASK.format(facts=_portfolio_facts(snapshot, notes)),
            PORTFOLIO_SCHEMA,
        )
        payload = {
            "headline": _clean(data.get("headline"), 120),
            "summary": _clean(data.get("summary"), 1200),
        }
        if not payload["headline"] or not payload["summary"]:
            raise ollama_client.OllamaError("модель вернула пустые поля")
        source, model = "llm", config.OLLAMA_MODEL
    except ollama_client.OllamaError as exc:
        log.warning("LLM-сводка не удалась: %s", exc)
        payload = _portfolio_rules(snapshot)

    db.analysis_put(snapshot["scope"], "portfolio", fp, payload, model, source)
    out = dict(payload)
    out["_source"] = source
    out["_model"] = model
    return out


def extra_for(snapshot, token, notes=None):
    """Всё, что кладём в промпт помимо самого токена.

    Дорогие эндпоинты (технический анализ — 10 кредитов, новости — 10) берём
    только при включённых флагах и только если тариф их отдаёт.
    """
    extra = {"fear_greed": snapshot.get("fear_greed") or {}, "notes": notes}
    if config.ENABLE_TECHNICAL or config.ENABLE_NEWS:
        api = market.client()
        try:
            extra["technical"] = api.technical(token["symbol"])
            extra["news"] = api.news(token["symbol"])
        except Exception as exc:  # noqa: BLE001 — разбор не должен падать из-за добавки
            log.warning("доп. данные для %s недоступны: %s", token["symbol"], exc)
    return extra


def _clean(text, limit):
    if not text:
        return ""
    out = " ".join(str(text).split())
    return out[:limit].strip()


# ── текст по правилам (когда LLM недоступна) ─────────────────────────────────

def _token_rules(token):
    d, w = token["change_24h"], token["change_7d"]
    week = f", за неделю {token['change_7d_text']}" if w is not None else ""
    short = f"{token['trend'].capitalize()}: за сутки {token['change_24h_text']}{week}."
    parts = [f"{token['name']} стоит {token['price_text']}: {token['change_24h_text']} за сутки"
             + (f" и {token['change_7d_text']} за неделю." if w is not None else ".")]
    if token["volatility"] is not None:
        parts.append(
            f"Колебания {token['volatility_label'].lower()} — {token['volatility']}% в день, "
            f"поэтому риск оценён как {token['risk'].lower()}."
        )
    if d is not None and w is not None:
        parts.append("Сутки и неделя смотрят в разные стороны — картина пока не сложилась."
                     if d * w < 0 else "Сутки и неделя не противоречат друг другу.")
    else:
        parts.append("Недельной истории пока нет — сервис копит её сам.")
    if token.get("day_low") and token.get("day_high"):
        parts.append(f"За сутки цена ходила между {market.fmt_price(token['day_low'])} "
                     f"и {market.fmt_price(token['day_high'])}.")
    parts.append("Разбор собран по правилам: локальная модель была недоступна.")
    if (w or 0) > 0:
        pro = f"Недельный итог положительный: {token['change_7d_text']}."
    elif (d or 0) >= 0:
        pro = f"Суточное движение в плюсе: {token['change_24h_text']}."
    else:
        pro = f"Просадка умеренная: {token['change_24h_text']} за сутки."
    if (token["volatility"] or 0) > 3.2:
        contra = f"Колебания высокие — {token['volatility']}% в день."
    elif w is not None and w <= 0:
        contra = f"Запаса в неделе нет: {token['change_7d_text']}."
    else:
        contra = "Истории пока мало, поэтому картину не на что опереть."
    return {"short": _clean(short, 160), "summary": _clean(" ".join(parts), 900),
            "pro": _clean(pro, 400), "contra": _clean(contra, 400)}


def _portfolio_rules(snapshot):
    p = snapshot["portfolio"]
    headline = {
        "Сильный рост": "Выборка идёт вверх широким фронтом",
        "Спокойный рост": "Выборка спокойно подрастает",
        "Ровно": "Рынок стоит на месте",
        "Осторожное снижение": "Выборка сползает вниз",
        "Распродажа": "Выборка под распродажей",
    }.get(p["mood"], "Сводка по выборке")
    lines = [
        f"Из {p['count']} токенов за сутки выросло {p['up']}, снизилось {p['down']}; "
        f"среднее изменение {market.fmt_pct(p['avg_change_24h'])}."
    ]
    if p["leaders"]:
        lines.append("Вверх тянут " + ", ".join(p["leaders"]) + ".")
    if p["laggards"]:
        lines.append("Слабее рынка — " + ", ".join(p["laggards"]) + ".")
    if p["avg_volatility"] is not None:
        lines.append(
            f"Средние колебания {p['avg_volatility']}% в день, общий риск — {p['risk'].lower()}."
        )
    lines.append("Сводка собрана по правилам: локальная модель была недоступна.")
    return {"headline": _clean(headline, 120), "summary": _clean(" ".join(lines), 1200)}
