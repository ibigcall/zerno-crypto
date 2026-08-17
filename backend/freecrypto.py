"""Клиент FreeCryptoAPI (https://api.freecryptoapi.com/v1).

Тарификация у провайдера кредитная и местами «за каждую возвращённую строку»
(getData — 1 кредит за символ, getOHLC — 1 за свечу, getTechnicalAnalysis — 10,
getCorrelation/getSupportResistance — 100), поэтому каждый вызов идёт через
SQLite-кэш с собственным TTL, а дорогие эндпоинты выключены по умолчанию.

Схемы ответов провайдер описывает не для всех эндпоинтов, поэтому разбор полей
сделан терпимым: цена/изменение ищутся среди известных синонимов, числа
приходят и строками, и числами (в спеке встречается даже ключ `sembol`).
"""
import hashlib
import logging
import math
import random
import time

import requests

from . import config, db

log = logging.getLogger("zerno.fca")

# синонимы полей в ответах провайдера
_PRICE_KEYS = ("last", "last_price", "price", "close", "last_close", "current_price")
_CHANGE_KEYS = ("daily_change_percentage", "daily_change_percent", "change_24h",
                "change_percentage_24h", "percent_change_24h", "daily_change")
_SYMBOL_KEYS = ("symbol", "sembol", "coin", "ticker", "name")
_HIGH_KEYS = ("highest", "high", "high_24h", "daily_high")
_LOW_KEYS = ("lowest", "low", "low_24h", "daily_low")
_VOLUME_KEYS = ("volume", "volume_24h", "daily_volume", "quote_volume", "total_volume")


class FreeCryptoError(RuntimeError):
    pass


class PlanError(FreeCryptoError):
    """Тариф не включает эндпоинт: «Your plan does not include…», «No access…».

    Такую ошибку бессмысленно повторять — запоминаем отказ на CAP_TTL и не тратим
    ни запросы, ни время на заведомо закрытые ручки.
    """


CAP_TTL = 6 * 3600
_PLAN_MARKERS = ("plan does not include", "no access", "upgrade", "not included")


def _is_plan_error(message):
    text = str(message).lower()
    return any(marker in text for marker in _PLAN_MARKERS)


def to_num(value):
    """'+2.4%' / '118 420.00' / None → float | None."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return None if isinstance(value, float) and math.isnan(value) else float(value)
    text = str(value).strip().replace("%", "").replace(" ", "").replace(" ", "")
    if not text or text.lower() in ("null", "none", "-", "n/a"):
        return None
    text = text.replace(",", ".").lstrip("+")
    try:
        return float(text)
    except ValueError:
        return None


def _pick(item, keys):
    for k in keys:
        if k in item and item[k] not in (None, ""):
            return item[k]
    return None


def _rows(payload, *keys):
    """Достать список строк из конверта ответа."""
    if isinstance(payload, list):
        return payload
    if not isinstance(payload, dict):
        return []
    for k in keys + ("symbols", "result", "data", "results", "coins", "list"):
        val = payload.get(k)
        if isinstance(val, list):
            return val
        if isinstance(val, dict) and k in ("data",):
            return [val]
    return []


def _ok(payload):
    """Провайдер отдаёт status как true/'success'/false + error."""
    if not isinstance(payload, dict):
        return True
    status = payload.get("status")
    if status is None:
        return "error" not in payload
    if isinstance(status, bool):
        return status
    return str(status).lower() in ("success", "ok", "true", "1")


class FreeCryptoClient:
    def __init__(self, api_key=None, base_url=None, demo=None):
        self.api_key = api_key if api_key is not None else config.FCA_API_KEY
        self.base_url = (base_url or config.FCA_BASE_URL).rstrip("/")
        self.demo = config.FCA_DEMO if demo is None else demo
        self.session = requests.Session()

    # ── доступность эндпоинтов на тарифе ─────────────────────────────────────
    def endpoint_available(self, path):
        return db.cache_get(f"cap:{path}", CAP_TTL) is not False

    def _mark_unavailable(self, path, reason):
        db.cache_put(f"cap:{path}", False)
        log.warning("%s недоступен на тарифе: %s", path, reason)

    # ── транспорт ────────────────────────────────────────────────────────────
    def _request(self, path, params, ttl, force=False):
        key = "fca:" + path + ":" + "&".join(f"{k}={v}" for k, v in sorted(params.items()))
        if not force:
            cached = db.cache_get(key, ttl)
            if cached is not None:
                return cached, True
        if not self.demo and not self.endpoint_available(path):
            raise PlanError(f"FreeCryptoAPI {path}: не входит в тариф")

        if self.demo:
            payload = _demo_payload(path, params)
        else:
            url = f"{self.base_url}/{path.lstrip('/')}"
            headers = {"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"}
            last_err = None
            payload = None
            for attempt in range(3):
                try:
                    resp = self.session.get(url, params=params, headers=headers, timeout=25)
                    if resp.status_code in (429, 500, 502, 503, 504):
                        last_err = f"HTTP {resp.status_code}"
                        time.sleep(1.5 * (attempt + 1))
                        continue
                    if resp.status_code == 401:
                        raise FreeCryptoError("FreeCryptoAPI: ключ не принят (401)")
                    resp.raise_for_status()
                    payload = resp.json()
                    break
                except FreeCryptoError:
                    raise
                except (requests.RequestException, ValueError) as exc:
                    last_err = str(exc)
                    time.sleep(1.5 * (attempt + 1))
            if payload is None:
                # отдаём просроченный кэш, если он есть: лучше старые данные, чем пустой экран
                stale = db.cache_get(key, ttl=10 ** 9)
                if stale is not None:
                    log.warning("%s недоступен (%s), отдаю просроченный кэш", path, last_err)
                    return stale, True
                raise FreeCryptoError(f"FreeCryptoAPI {path}: {last_err}")
            if not _ok(payload):
                err = payload.get("error") or payload.get("message") or "неизвестная ошибка"
                if _is_plan_error(err):
                    self._mark_unavailable(path, err)
                    raise PlanError(f"FreeCryptoAPI {path}: {err}")
                raise FreeCryptoError(f"FreeCryptoAPI {path}: {err}")

        db.cache_put(key, payload)
        return payload, False

    # ── эндпоинты ────────────────────────────────────────────────────────────
    def quotes(self, symbols, force=False):
        """/getData — цена и суточное изменение. 1 кредит за каждый символ.

        Разделитель тикеров провайдер ждёт пробелом (на проводе `BTC+ETH`).
        Если передать закодированный «%2B», ответ приходит с пустым списком —
        поэтому склеиваем именно пробелом и отдаём requests кодировать.
        """
        symbols = [s.upper() for s in symbols if s]
        if not symbols:
            return {}
        out = self._quotes_request([self.alias(s) for s in symbols], symbols, force)

        # Часть тикеров провайдер знает только как биржевую пару (LINK → LINKUSDT@binance),
        # а неизвестное имя в общем запросе обнуляет всю выдачу — добираем по одному.
        for sym in [s for s in symbols if s not in out]:
            for candidate in self._candidates(sym):
                try:
                    got = self._quotes_request([candidate], [sym], force)
                except PlanError:
                    raise
                except FreeCryptoError as exc:
                    log.warning("котировка %s не получена: %s", sym, exc)
                    break
                if got:
                    out.update(got)
                    db.cache_put(f"alias:{sym}", candidate)
                    break
        return out

    def alias(self, symbol):
        """Имя, под которым провайдер отдаёт этот тикер (разрешается один раз)."""
        return db.cache_get(f"alias:{symbol}", config.CACHE_TTL_LIST) or symbol

    def _candidates(self, symbol):
        seen, out = set(), []
        for name in (symbol, f"{symbol}USDT@binance", f"{symbol}USDT@bybit"):
            if name not in seen:
                seen.add(name)
                out.append(name)
        return out

    def _quotes_request(self, query, want, force=False):
        payload, _ = self._request(
            "getData", {"symbol": " ".join(query)}, config.CACHE_TTL_QUOTES, force
        )
        symbols = want
        out = {}
        for item in _rows(payload):
            if not isinstance(item, dict):
                continue
            raw_sym = str(_pick(item, _SYMBOL_KEYS) or "").upper()
            # «LINKUSDT» → LINK. Сначала самое длинное совпадение, иначе ETHFI
            # прилетел бы в ETH.
            sym = next((s for s in sorted(symbols, key=len, reverse=True)
                        if raw_sym.startswith(s)), raw_sym)
            if not sym:
                continue
            out[sym] = {
                "symbol": sym,
                "price": to_num(_pick(item, _PRICE_KEYS)),
                "change_24h": to_num(_pick(item, _CHANGE_KEYS)),
                "high": to_num(_pick(item, _HIGH_KEYS)),
                "low": to_num(_pick(item, _LOW_KEYS)),
                "volume": to_num(_pick(item, _VOLUME_KEYS)),
                "name": item.get("name") or None,
                "quote_time": item.get("date") or item.get("last_update") or None,
            }
        return out

    def closes(self, symbol, days=None, force=False):
        """/getOHLC — дневные свечи. Схема ответа описана в спеке: result[].close."""
        days = days or config.SPARK_DAYS
        payload, _ = self._request(
            "getOHLC", {"symbol": symbol.upper(), "days": days}, config.CACHE_TTL_OHLC, force
        )
        series = []
        for item in _rows(payload, "result"):
            if not isinstance(item, dict):
                continue
            close = to_num(_pick(item, ("close", "close_price", "c")))
            if close is None:
                continue
            series.append({
                "t": item.get("time_close") or item.get("date") or item.get("time"),
                "close": close,
                "high": to_num(_pick(item, ("high", "h"))),
                "low": to_num(_pick(item, ("low", "l"))),
            })
        return series

    def fear_greed(self, force=False):
        """/getFearGreed — 1 кредит."""
        payload, _ = self._request("getFearGreed", {}, config.CACHE_TTL_FG, force)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, dict):
            data = payload if isinstance(payload, dict) else {}
        index = to_num(_pick(data, ("fg_index", "value", "index")))
        return {
            "index": int(index) if index is not None else None,
            "emotion": data.get("emotion") or data.get("value_classification"),
            "updated_at": data.get("last_update") or data.get("timestamp"),
        }

    def technical(self, symbol, force=False):
        """/getTechnicalAnalysis — MACD/RSI. 10 кредитов за запрос."""
        if not config.ENABLE_TECHNICAL:
            return None
        payload, _ = self._request(
            "getTechnicalAnalysis", {"symbol": symbol.upper()}, config.CACHE_TTL_OHLC, force
        )
        rows = _rows(payload)
        item = rows[0] if rows else (payload.get("data") if isinstance(payload, dict) else None)
        if not isinstance(item, dict):
            return None
        return {
            "rsi": to_num(_pick(item, ("rsi", "RSI", "rsi_14"))),
            "macd": to_num(_pick(item, ("macd", "MACD"))),
            "signal": to_num(_pick(item, ("signal", "signal_line", "macd_signal"))),
        }

    def news(self, symbol=None, force=False):
        """/getNews — 10 кредитов за запрос."""
        if not config.ENABLE_NEWS:
            return []
        params = {"symbol": symbol.upper()} if symbol else {}
        payload, _ = self._request("getNews", params, config.CACHE_TTL_FG, force)
        out = []
        for item in _rows(payload, "articles", "news"):
            if isinstance(item, dict) and item.get("title"):
                out.append({
                    "title": item["title"],
                    "source": item.get("source") or item.get("site"),
                    "date": item.get("published_at") or item.get("date"),
                })
        return out[:8]

    def crypto_list(self, force=False):
        """/getCryptoList — справочник тикеров, 1 кредит. Кэш на сутки."""
        payload, _ = self._request("getCryptoList", {}, config.CACHE_TTL_LIST, force)
        out = []
        for item in _rows(payload, "cryptos", "currencies"):
            if isinstance(item, str):
                out.append({"symbol": item.upper(), "name": item.upper()})
            elif isinstance(item, dict):
                sym = str(_pick(item, _SYMBOL_KEYS) or "").upper()
                if sym:
                    out.append({"symbol": sym, "name": item.get("name") or sym})
        return out


# ── демо-режим ───────────────────────────────────────────────────────────────
# Без ключа сервис должен подниматься и показывать живой интерфейс, поэтому
# генерируем правдоподобные ряды: детерминированно по символу и календарному дню.

_DEMO_BASE = {
    "BTC": (118420.0, "Bitcoin"), "ETH": (4260.0, "Ethereum"), "SOL": (214.3, "Solana"),
    "TON": (6.82, "Toncoin"), "ARB": (0.94, "Arbitrum"), "LINK": (22.15, "Chainlink"),
    "BNB": (842.0, "BNB"), "XRP": (2.94, "XRP"), "ADA": (0.78, "Cardano"),
    "DOGE": (0.21, "Dogecoin"), "AVAX": (31.4, "Avalanche"), "DOT": (5.1, "Polkadot"),
    "MATIC": (0.42, "Polygon"), "OP": (1.62, "Optimism"), "SUI": (3.28, "Sui"),
    "NEAR": (4.9, "NEAR"), "ATOM": (6.4, "Cosmos"), "LTC": (108.0, "Litecoin"),
}


def _demo_rng(symbol, salt=""):
    day = time.strftime("%Y-%m-%d")
    seed = int(hashlib.sha256(f"{symbol}|{day}|{salt}".encode()).hexdigest()[:12], 16)
    return random.Random(seed)


def _demo_base(symbol):
    if symbol in _DEMO_BASE:
        return _DEMO_BASE[symbol]
    rng = random.Random(int(hashlib.sha256(symbol.encode()).hexdigest()[:8], 16))
    return round(rng.uniform(0.2, 400.0), 4), symbol.capitalize()


def _demo_series(symbol, days):
    rng = _demo_rng(symbol, "series")
    price, _ = _demo_base(symbol)
    drift = rng.uniform(-0.006, 0.008)
    vol = rng.uniform(0.008, 0.055)
    closes = []
    value = price * (1 - drift * days)
    for _ in range(days):
        value *= 1 + drift + rng.gauss(0, vol)
        closes.append(max(value, price * 0.35))
    # приводим последнюю свечу к базовой цене, чтобы график и котировка не расходились
    shift = price / closes[-1]
    return [c * shift for c in closes]


def _demo_payload(path, params):
    path = path.lstrip("/")
    if path == "getData":
        out = []
        for sym in str(params.get("symbol", "")).split("+"):
            sym = sym.strip().upper()
            if not sym:
                continue
            series = _demo_series(sym, 3)
            price, name = _demo_base(sym)
            prev = series[-2] if len(series) > 1 else price
            change = (price / prev - 1) * 100 if prev else 0.0
            rng = _demo_rng(sym, "quote")
            out.append({
                "symbol": sym, "name": name,
                "last": f"{price:.6f}".rstrip("0").rstrip("."),
                "daily_change_percentage": f"{change:+.2f}",
                "highest": f"{price * (1 + abs(change) / 100 * 0.6):.6f}",
                "lowest": f"{price * (1 - abs(change) / 100 * 0.6):.6f}",
                "volume": f"{price * rng.uniform(2e5, 4e6):.0f}",
                "date": time.strftime("%Y-%m-%d %H:%M:%S"),
            })
        return {"status": "success", "symbols": out}

    if path == "getOHLC":
        sym = str(params.get("symbol", "BTC")).upper()
        days = int(params.get("days") or config.SPARK_DAYS)
        closes = _demo_series(sym, days)
        result = []
        for i, close in enumerate(closes):
            day = time.strftime("%Y-%m-%d", time.localtime(time.time() - (days - i - 1) * 86400))
            spread = abs(close) * 0.012
            result.append({
                "time_close": f"{day} 23:59:59",
                "open": round(close - spread / 2, 6),
                "high": round(close + spread, 6),
                "low": round(close - spread, 6),
                "close": round(close, 6),
            })
        return {"status": True, "symbol": sym, "resultset_size": len(result), "result": result}

    if path == "getFearGreed":
        rng = _demo_rng("market", "fg")
        idx = rng.randint(22, 78)
        emotion = "fear" if idx < 45 else ("greed" if idx > 55 else "neutral")
        return {"status": True, "data": {"fg_index": idx, "emotion": emotion,
                                        "last_update": time.strftime("%Y-%m-%d %H:%M:%S")}}

    if path == "getTechnicalAnalysis":
        sym = str(params.get("symbol", "BTC")).upper()
        rng = _demo_rng(sym, "ta")
        return {"status": True, "symbols": [{
            "symbol": sym, "rsi": round(rng.uniform(28, 72), 1),
            "macd": round(rng.uniform(-2, 2), 3), "signal": round(rng.uniform(-2, 2), 3),
        }]}

    if path == "getCryptoList":
        return {"status": True, "symbols": [
            {"symbol": s, "name": n} for s, (_, n) in _DEMO_BASE.items()
        ]}

    if path == "getNews":
        return {"status": True, "articles": []}

    return {"status": True, "symbols": []}
