"""Сборка рыночного снимка: котировки + история + производные метрики.

Здесь считается всё, что можно посчитать без LLM: недельное изменение,
волатильность, уровень риска, точки спарклайна, сводка по выборке.
LLM получает уже готовые числа — так разбор опирается на факты, а не выдумывает их.
"""
import hashlib
import json
import statistics
import time

from . import config, db
from .freecrypto import FreeCryptoClient, FreeCryptoError, PlanError

NAMES = {
    "BTC": "Bitcoin", "ETH": "Ethereum", "SOL": "Solana", "TON": "Toncoin",
    "ARB": "Arbitrum", "LINK": "Chainlink", "BNB": "BNB", "XRP": "XRP",
    "ADA": "Cardano", "DOGE": "Dogecoin", "AVAX": "Avalanche", "DOT": "Polkadot",
    "MATIC": "Polygon", "OP": "Optimism", "SUI": "Sui", "NEAR": "NEAR",
    "ATOM": "Cosmos", "LTC": "Litecoin", "TRX": "TRON", "APT": "Aptos",
}

_client = None


def client():
    global _client
    if _client is None:
        _client = FreeCryptoClient()
    return _client


# ── форматирование ───────────────────────────────────────────────────────────
NBSP = " "  # тонкий пробел — как в макете «$118 420»


def fmt_price(value):
    if value is None:
        return "—"
    if value >= 1000:
        return "$" + f"{value:,.0f}".replace(",", NBSP)
    if value >= 1:
        return f"${value:,.2f}".replace(",", NBSP)
    if value >= 0.01:
        return f"${value:.4f}"
    return f"${value:.6f}"


def fmt_pct(value):
    return "—" if value is None else f"{value:+.1f}%"


def fmt_volume(value):
    if not value:
        return "—"
    for limit, suffix in ((1e12, "трлн"), (1e9, "млрд"), (1e6, "млн"), (1e3, "тыс")):
        if value >= limit:
            return f"${value / limit:.1f}{NBSP}{suffix}"
    return f"${value:.0f}"


def spark_points(series, width=120.0, height=34.0, pad=3.0):
    """Polyline для viewBox 0 0 120 34: рост цены = линия вверх."""
    values = [v for v in series if v is not None]
    if len(values) < 2:
        return ""
    low, high = min(values), max(values)
    span = (high - low) or 1.0
    inner = height - pad * 2
    points = []
    for i, value in enumerate(values):
        x = (i / (len(values) - 1)) * width
        y = pad + inner - ((value - low) / span) * inner
        points.append(f"{x:.1f},{y:.1f}")
    return " ".join(points)


# ── метрики ──────────────────────────────────────────────────────────────────

def daily_returns(closes):
    out = []
    for prev, cur in zip(closes, closes[1:]):
        if prev:
            out.append(cur / prev - 1)
    return out


def volatility_pct(closes):
    """Стандартное отклонение дневной доходности, в процентах."""
    rets = daily_returns(closes)
    if len(rets) < 3:
        return None
    return statistics.pstdev(rets) * 100


def vol_label(vol):
    if vol is None:
        return "—"
    if vol < 1.6:
        return "Низкие"
    if vol < 3.2:
        return "Средние"
    return "Высокие"


def risk_label(symbol, vol, change_7d):
    """Риск: волатильность + «первый эшелон» + свежий перегрев/провал."""
    score = 0
    if vol is not None:
        score += 0 if vol < 1.6 else (1 if vol < 3.2 else 2)
    else:
        score += 1
    if symbol in ("BTC", "ETH"):
        score -= 1
    if change_7d is not None and abs(change_7d) > 12:
        score += 1
    if score <= 0:
        return "Низкий"
    return "Средний" if score == 1 else "Высокий"


def change_over(closes, days):
    if len(closes) < days + 1:
        return None
    past = closes[-(days + 1)]
    return (closes[-1] / past - 1) * 100 if past else None


def trend_word(change_24h, change_7d):
    a = change_24h or 0
    b = change_7d or 0
    if b > 8 and a > 0:
        return "уверенный рост"
    if b > 2:
        return "спокойный рост"
    if b < -8:
        return "заметное падение"
    if b < -2:
        return "сползание вниз"
    if abs(a) < 1 and abs(b) < 2:
        return "боковик"
    return "без ясного направления"


# ── снимок ───────────────────────────────────────────────────────────────────

def build_snapshot(scope="web", force=False):
    """Котировки + история по всему списку наблюдения. Без обращения к LLM."""
    items = db.watchlist(scope)
    symbols = [i["symbol"] for i in items]
    warnings = []
    api = client()

    quotes = {}
    if symbols:
        try:
            quotes = api.quotes(symbols, force=force)
        except FreeCryptoError as exc:
            warnings.append(str(exc))

    tokens = []
    for item in items:
        sym = item["symbol"]
        quote = quotes.get(sym, {})
        price = quote.get("price")
        if price is not None:
            db.price_add(sym, price)

        closes, history_source = [], None
        if api.endpoint_available("getOHLC"):
            try:
                closes = [c["close"] for c in api.closes(sym, force=force)]
                history_source = "api" if closes else None
            except PlanError:
                pass  # тариф без истории — молча уходим на локальную
            except FreeCryptoError as exc:
                warnings.append(f"{sym}: {exc}")
        if not closes:
            closes = db.price_daily(sym, config.SPARK_DAYS)
            history_source = "local-daily" if len(closes) > 1 else None
        if len(closes) < 2:
            closes = db.price_recent(sym)
            history_source = "local-intraday" if len(closes) > 1 else None

        if price is None and closes:
            price = closes[-1]
        if closes and price:
            closes = closes[:-1] + [price]  # последняя точка = живая котировка

        change_24h = quote.get("change_24h")
        if change_24h is None and history_source == "api":
            change_24h = change_over(closes, 1)
        change_7d = change_over(closes, 7) if history_source in ("api", "local-daily") else None
        vol = volatility_pct(closes) if history_source in ("api", "local-daily") else None

        tokens.append({
            "symbol": sym,
            "name": item.get("name") or quote.get("name") or NAMES.get(sym, sym),
            "price": price,
            "price_text": fmt_price(price),
            "change_24h": change_24h,
            "change_24h_text": fmt_pct(change_24h),
            "change_7d": change_7d,
            "change_7d_text": fmt_pct(change_7d),
            "volume": quote.get("volume"),
            "volume_text": fmt_volume(quote.get("volume")),
            "volatility": round(vol, 2) if vol is not None else None,
            "volatility_label": vol_label(vol),
            "risk": risk_label(sym, vol, change_7d),
            "trend": trend_word(change_24h, change_7d),
            "series": [round(c, 8) for c in closes],
            "spark": spark_points(closes),
            "history_source": history_source,
            "day_high": quote.get("high"),
            "day_low": quote.get("low"),
            "quote_time": quote.get("quote_time"),
            "up": bool((change_24h or 0) >= 0),
        })

    fg = None
    if api.endpoint_available("getFearGreed"):
        try:
            fg = api.fear_greed(force=force)
        except PlanError:
            pass
        except FreeCryptoError as exc:
            warnings.append(str(exc))

    return {
        "scope": scope,
        "tokens": tokens,
        "portfolio": portfolio_stats(tokens, fg),
        "fear_greed": fg,
        "updated_at": time.time(),
        "updated_text": time.strftime("%H:%M"),
        "mode": "demo" if config.FCA_DEMO else "live",
        "capabilities": {
            "history": api.endpoint_available("getOHLC"),
            "fear_greed": api.endpoint_available("getFearGreed"),
            "technical": config.ENABLE_TECHNICAL and api.endpoint_available(
                "getTechnicalAnalysis"),
        },
        "warnings": warnings,
    }


def portfolio_stats(tokens, fg=None):
    changes = [t["change_24h"] for t in tokens if t["change_24h"] is not None]
    weekly = [t["change_7d"] for t in tokens if t["change_7d"] is not None]
    up = sum(1 for c in changes if c >= 0)
    avg = sum(changes) / len(changes) if changes else None
    avg_week = sum(weekly) / len(weekly) if weekly else None
    vols = [t["volatility"] for t in tokens if t["volatility"] is not None]
    avg_vol = sum(vols) / len(vols) if vols else None
    high_risk = sum(1 for t in tokens if t["risk"] == "Высокий")

    if avg is None:
        mood = "Нет данных"
    elif avg > 4:
        mood = "Сильный рост"
    elif avg > 0.7:
        mood = "Спокойный рост"
    elif avg > -0.7:
        mood = "Ровно"
    elif avg > -4:
        mood = "Осторожное снижение"
    else:
        mood = "Распродажа"

    if avg_vol is None:
        # Волатильности ещё нет (истории мало) — считаем по рискам самих токенов,
        # они оцениваются и без неё.
        if not tokens:
            risk = "—"
        elif high_risk > len(tokens) / 2:
            risk = "Повышенный"
        elif high_risk or sum(1 for t in tokens if t["risk"] == "Средний") > len(tokens) / 2:
            risk = "Умеренный"
        else:
            risk = "Низкий"
    elif avg_vol < 1.6 and high_risk <= 1:
        risk = "Низкий"
    elif avg_vol < 3.2 or high_risk <= len(tokens) // 2:
        risk = "Умеренный"
    else:
        risk = "Повышенный"

    return {
        "count": len(tokens),
        "up": up,
        "down": len(changes) - up,
        "avg_change_24h": round(avg, 2) if avg is not None else None,
        "avg_change_7d": round(avg_week, 2) if avg_week is not None else None,
        "avg_volatility": round(avg_vol, 2) if avg_vol is not None else None,
        "mood": mood,
        "risk": risk,
        "fear_greed": fg,
        "leaders": [t["symbol"] for t in sorted(
            [t for t in tokens if t["change_24h"] is not None],
            key=lambda t: t["change_24h"], reverse=True)[:2]],
        "laggards": [t["symbol"] for t in sorted(
            [t for t in tokens if t["change_24h"] is not None],
            key=lambda t: t["change_24h"])[:2]],
    }


def fingerprint(obj):
    """Отпечаток входных данных — чтобы не гонять LLM на тех же цифрах."""
    raw = json.dumps(obj, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(raw.encode()).hexdigest()[:16]


def _bucket(value, step):
    """Огрубление: разбор не должен переписываться из-за шума в четвёртом знаке."""
    if value is None:
        return None
    return round(value / step) * step


def notes_key(notes):
    """Контекст из Telegram — часть входа модели, значит и часть отпечатка."""
    return [n.get("id") for n in (notes or [])]


def token_fingerprint(token, notes=None):
    """Отпечаток входа модели.

    Абсолютной цены здесь нет намеренно: она шевелится на доли процента каждые
    пару минут, и вместе с ней разбор переписывался бы почти на каждом обновлении
    котировок — по 40 секунд работы модели на токен. Смысл текста задают
    изменения и уровень риска, а свежесть добирается TTL (CACHE_TTL_ANALYSIS).
    """
    return fingerprint({
        "s": token["symbol"],
        "notes": notes_key(notes),
        "d": _bucket(token["change_24h"], 0.5),
        "w": _bucket(token["change_7d"], 1.0),
        "r": token["risk"],
    })


def portfolio_fingerprint(snapshot, notes=None):
    p = snapshot["portfolio"]
    return fingerprint({
        "notes": notes_key(notes),
        "a": _bucket(p["avg_change_24h"], 0.5),
        "w": _bucket(p["avg_change_7d"], 1.0),
        "u": p["up"], "n": p["count"],
        "syms": sorted(t["symbol"] for t in snapshot["tokens"]),
        "fg": _bucket((snapshot.get("fear_greed") or {}).get("index"), 5),
    })
