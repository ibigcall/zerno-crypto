"""Telegram-бот «Зерно»: тот же разбор, что в браузере, только в чате.

Бот — второй интерфейс к тому же бэкенду:
* у каждого чата свой список наблюдения (область данных tg:<chat_id>);
* данные и разборы берутся по HTTP из backend/app.py (заголовок X-Bot-Token);
* подписки на утренний дайджест лежат в общей SQLite;
* любой присланный или пересланный текст оседает в «рыночном контексте» и уходит
  в локальную модель вместе с числами.

Запуск: python -m bot.bot (нужен TELEGRAM_BOT_TOKEN в .env и поднятый бэкенд).
"""
import asyncio
import html
import logging
import time

import requests
from telegram import InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.ext import (Application, CallbackQueryHandler, CommandHandler,
                          MessageHandler, filters)

from backend import config, db

logging.basicConfig(
    level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
)
log = logging.getLogger("zerno.bot")

HELP = """<b>Зерно</b> — спокойный разбор вашего списка криптовалют.

/list — сводка по всей выборке
/t BTC — разбор одного токена (можно просто написать тикер)
/add SUI — добавить токен, /del SUI — убрать
/fg — индекс страха и жадности
/digest on 10 — утренний дайджест в 10:00, /digest off — отключить
/notes — что лежит в рыночном контексте, /clear — очистить его

Любое другое сообщение (в том числе пересланный пост из канала) я сохраню как
рыночный контекст — модель учтёт его в следующем разборе.

Разбор — это наблюдение, а не совет, куда вложить."""


# ── доступ к бэкенду ─────────────────────────────────────────────────────────

def scope_of(chat_id):
    return f"tg:{chat_id}"


def _headers():
    return {"X-Bot-Token": config.BOT_API_TOKEN, "Content-Type": "application/json"}


def _get(path, params=None):
    resp = requests.get(f"{config.API_BASE}{path}", params=params or {},
                        headers=_headers(), timeout=300)
    data = _payload(resp)
    return data


def _post(path, body):
    resp = requests.post(f"{config.API_BASE}{path}", json=body, headers=_headers(), timeout=300)
    return _payload(resp)


def _delete(path, params=None):
    resp = requests.delete(f"{config.API_BASE}{path}", params=params or {},
                           headers=_headers(), timeout=60)
    return _payload(resp)


def _payload(resp):
    try:
        data = resp.json()
    except ValueError:
        raise RuntimeError(f"бэкенд ответил не JSON ({resp.status_code})")
    if not resp.ok or data.get("ok") is False:
        raise RuntimeError(data.get("error") or f"бэкенд ответил {resp.status_code}")
    return data


async def call(fn, *args, **kwargs):
    """Запросы к бэкенду синхронные и долгие (модель думает) — уводим в поток."""
    return await asyncio.to_thread(fn, *args, **kwargs)


def allowed(chat_id):
    if not config.TELEGRAM_ALLOWED_CHATS:
        return True
    return str(chat_id) in config.TELEGRAM_ALLOWED_CHATS


# ── форматирование сообщений ─────────────────────────────────────────────────

def e(text):
    return html.escape(str(text if text is not None else "—"))


def portfolio_message(snap):
    p = snap.get("portfolio") or {}
    a = snap.get("analysis") or {}
    fg = snap.get("fear_greed") or {}
    lines = [f"<b>{e(a.get('headline') or 'Сводка по выборке')}</b>"]
    if a.get("summary"):
        lines.append(e(a["summary"]))
    lines.append("")
    lines.append(
        f"Настроение: <b>{e(p.get('mood'))}</b> · риск: <b>{e(p.get('risk'))}</b>"
        + (f" · страх и жадность: <b>{e(fg.get('index'))}</b>" if fg.get("index") is not None else "")
    )
    lines.append("")
    for t in snap.get("tokens", []):
        mark = "▲" if t.get("up") else "▼"
        week = (f" · неделя {e(t['change_7d_text'])}"
                if t.get("change_7d") is not None else "")
        lines.append(
            f"{mark} <b>{e(t['symbol'])}</b> {e(t['price_text'])} · "
            f"сутки {e(t['change_24h_text'])}{week}"
        )
    lines.append("")
    source = a.get("_source")
    if source == "rules":
        lines.append("<i>Текст собран по правилам: локальная модель была недоступна.</i>")
    elif a.get("_model"):
        lines.append(f"<i>Разбор: {e(a['_model'])} · котировки в {e(snap.get('updated_text'))}</i>")
    lines.append("<i>Это наблюдение, а не совет, куда вложить.</i>")
    return "\n".join(lines)


def token_message(token, analysis):
    week = (f" · неделя {e(token['change_7d_text'])}"
            if token.get("change_7d") is not None else "")
    swing = (f"колебания {e(token['volatility_label'].lower())} · "
             if token.get("volatility") is not None else "")
    lines = [
        f"<b>{e(token['name'])} ({e(token['symbol'])})</b>",
        f"{e(token['price_text'])} · сутки {e(token['change_24h_text'])}{week}",
        f"{swing}риск {e(token['risk'].lower())}",
    ]
    if analysis:
        lines += ["", e(analysis.get("summary")), "",
                  f"<b>Что за.</b> {e(analysis.get('pro'))}",
                  f"<b>Что против.</b> {e(analysis.get('contra'))}"]
        if analysis.get("_source") == "rules":
            lines.append("")
            lines.append("<i>Текст собран по правилам: локальная модель была недоступна.</i>")
    lines.append("")
    lines.append("<i>Это наблюдение, а не совет, куда вложить.</i>")
    return "\n".join(lines)


def token_keyboard(snap, exclude=None):
    buttons, row = [], []
    for t in snap.get("tokens", []):
        if t["symbol"] == exclude:
            continue
        row.append(InlineKeyboardButton(
            f"{t['symbol']} {t['change_24h_text']}", callback_data=f"tok:{t['symbol']}"))
        if len(row) == 3:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons) if buttons else None


# ── обработчики ──────────────────────────────────────────────────────────────

async def guard(update):
    chat = update.effective_chat
    if not allowed(chat.id):
        await update.effective_message.reply_text("Этот чат не в списке разрешённых.")
        return False
    db.tg_upsert(chat.id, (update.effective_user.username if update.effective_user else None))
    return True


async def cmd_start(update, context):
    if not await guard(update):
        return
    db.seed_scope(scope_of(update.effective_chat.id), config.DEFAULT_WATCHLIST)
    await update.effective_message.reply_text(HELP, parse_mode=ParseMode.HTML)


async def cmd_list(update, context):
    if not await guard(update):
        return
    msg = await update.effective_message.reply_text("Собираю разбор — модель думает…")
    scope = scope_of(update.effective_chat.id)
    try:
        snap = await call(_get, "/api/digest", {"scope": scope})
    except (RuntimeError, requests.RequestException) as exc:
        await msg.edit_text(f"Не получилось: {e(exc)}", parse_mode=ParseMode.HTML)
        return
    await msg.edit_text(portfolio_message(snap), parse_mode=ParseMode.HTML,
                        reply_markup=token_keyboard(snap))


async def edit_target(target, text, keyboard=None):
    """У Message метод edit_text, у CallbackQuery — edit_message_text."""
    if hasattr(target, "edit_message_text"):
        await target.edit_message_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)
    else:
        await target.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=keyboard)


async def send_token(target, chat_id, symbol):
    scope = scope_of(chat_id)
    try:
        snap = await call(_get, "/api/snapshot", {"scope": scope})
        token = next((t for t in snap["tokens"] if t["symbol"] == symbol), None)
        if not token:
            await edit_target(target, f"{e(symbol)} нет в вашем списке. "
                                      f"Добавьте командой /add {e(symbol)}")
            return
        res = await call(_post, "/api/analysis",
                         {"scope": scope, "kind": "token", "symbol": symbol})
        text = token_message(token, res.get("analysis"))
        keyboard = token_keyboard(snap, exclude=symbol)
    except (RuntimeError, requests.RequestException) as exc:
        text, keyboard = f"Не получилось: {e(exc)}", None
    await edit_target(target, text, keyboard)


async def cmd_token(update, context):
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Укажите тикер: /t BTC")
        return
    symbol = context.args[0].strip().upper()
    msg = await update.effective_message.reply_text(f"Смотрю {symbol}…")
    await send_token(msg, update.effective_chat.id, symbol)


async def cmd_add(update, context):
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Укажите тикер: /add SUI")
        return
    symbol = context.args[0].strip().upper()
    scope = scope_of(update.effective_chat.id)
    try:
        res = await call(_post, "/api/watchlist", {"scope": scope, "symbol": symbol})
    except (RuntimeError, requests.RequestException) as exc:
        await update.effective_message.reply_text(f"Не вышло: {e(exc)}", parse_mode=ParseMode.HTML)
        return
    total = len(res.get("items", []))
    await update.effective_message.reply_text(
        f"{'Добавил' if res.get('added') else 'Уже был в списке'}: {symbol}. Всего {total}."
    )


async def cmd_del(update, context):
    if not await guard(update):
        return
    if not context.args:
        await update.effective_message.reply_text("Укажите тикер: /del SUI")
        return
    symbol = context.args[0].strip().upper()
    scope = scope_of(update.effective_chat.id)
    try:
        await call(_delete, f"/api/watchlist/{symbol}", {"scope": scope})
    except (RuntimeError, requests.RequestException) as exc:
        await update.effective_message.reply_text(f"Не вышло: {e(exc)}", parse_mode=ParseMode.HTML)
        return
    await update.effective_message.reply_text(f"Убрал {symbol} из списка.")


async def cmd_fg(update, context):
    if not await guard(update):
        return
    scope = scope_of(update.effective_chat.id)
    try:
        snap = await call(_get, "/api/snapshot", {"scope": scope})
    except (RuntimeError, requests.RequestException) as exc:
        await update.effective_message.reply_text(f"Не вышло: {e(exc)}", parse_mode=ParseMode.HTML)
        return
    fg = snap.get("fear_greed") or {}
    if fg.get("index") is None:
        await update.effective_message.reply_text("Индекс сейчас недоступен.")
        return
    words = {"fear": "страх", "greed": "жадность", "neutral": "нейтрально"}
    await update.effective_message.reply_text(
        f"Индекс страха и жадности: <b>{e(fg['index'])}</b> "
        f"({e(words.get(fg.get('emotion'), fg.get('emotion')))}), "
        f"обновлён {e(fg.get('updated_at'))}.", parse_mode=ParseMode.HTML)


async def cmd_digest(update, context):
    if not await guard(update):
        return
    chat_id = update.effective_chat.id
    args = [a.lower() for a in context.args]
    if not args or args[0] not in ("on", "off"):
        user = db.tg_get(chat_id) or {}
        status = ("включён" if user.get("digest_enabled") else "выключен")
        await update.effective_message.reply_text(
            f"Дайджест {status}, час отправки {user.get('digest_hour', 10)}:00.\n"
            "Включить: /digest on 10 · выключить: /digest off")
        return
    if args[0] == "off":
        db.tg_set_digest(chat_id, False)
        await update.effective_message.reply_text("Дайджест выключен.")
        return
    hour = 10
    if len(args) > 1:
        try:
            hour = max(0, min(23, int(args[1])))
        except ValueError:
            await update.effective_message.reply_text("Час не разобрал. Пример: /digest on 9")
            return
    db.tg_set_digest(chat_id, True, hour)
    await update.effective_message.reply_text(
        f"Буду присылать разбор каждый день около {hour}:00.")


async def cmd_notes(update, context):
    if not await guard(update):
        return
    scope = scope_of(update.effective_chat.id)
    items = db.notes_list(scope, limit=10)
    if not items:
        await update.effective_message.reply_text(
            "Контекст пуст. Пришлите или перешлите сюда любой текст — он попадёт в разбор.")
        return
    lines = ["<b>Рыночный контекст</b>"]
    for n in items:
        when = time.strftime("%d.%m %H:%M", time.localtime(n["created_at"]))
        lines.append(f"· <i>{when}</i> {e(n['text'][:300])}")
    await update.effective_message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML)


async def cmd_clear(update, context):
    if not await guard(update):
        return
    scope = scope_of(update.effective_chat.id)
    for note in db.notes_list(scope, limit=500):
        db.notes_delete(scope, note["id"])
    await update.effective_message.reply_text("Контекст очищен.")


async def on_callback(update, context):
    query = update.callback_query
    await query.answer()
    if not allowed(query.message.chat.id):
        return
    data = query.data or ""
    if data.startswith("tok:"):
        await send_token(query, query.message.chat.id, data[4:])


async def on_text(update, context):
    """Свободный текст и пересланные посты — в рыночный контекст.

    Короткое сообщение из одного слова трактуем как тикер: «BTC» → разбор BTC.
    """
    if not await guard(update):
        return
    message = update.effective_message
    text = (message.text or message.caption or "").strip()
    if not text:
        return
    chat_id = update.effective_chat.id

    compact = text.replace("$", "").strip()
    if len(compact) <= 15 and compact.replace(".", "").isalnum() and " " not in compact:
        msg = await message.reply_text(f"Смотрю {compact.upper()}…")
        await send_token(msg, chat_id, compact.upper())
        return

    if len(text) < 10:
        await message.reply_text("Не понял. /help — что я умею.")
        return

    source = "telegram"
    forward = getattr(message, "forward_origin", None)
    if forward is not None:
        source = "telegram: пересланное"
    db.notes_add(scope_of(chat_id), text[:4000], source)
    await message.reply_text(
        "Записал в рыночный контекст — учту в следующем разборе. /list чтобы пересобрать.")


# ── ежедневный дайджест ──────────────────────────────────────────────────────
# JobQueue у PTB требует отдельного extra, поэтому крутим свою простую петлю.

async def digest_loop(app):
    await asyncio.sleep(10)
    while True:
        try:
            today = time.strftime("%Y-%m-%d")
            hour = int(time.strftime("%H"))
            for user in db.tg_digest_subscribers():
                if user.get("last_digest") == today or int(user.get("digest_hour", 10)) != hour:
                    continue
                chat_id = user["chat_id"]
                try:
                    snap = await call(_get, "/api/digest",
                                      {"scope": scope_of(chat_id), "refresh": 1})
                    await app.bot.send_message(chat_id, portfolio_message(snap),
                                               parse_mode=ParseMode.HTML,
                                               reply_markup=token_keyboard(snap))
                    db.tg_mark_digest(chat_id, today)
                    log.info("дайджест отправлен в %s", chat_id)
                except Exception as exc:  # noqa: BLE001 — один чат не должен ломать рассылку
                    log.warning("дайджест для %s не ушёл: %s", chat_id, exc)
        except Exception as exc:  # noqa: BLE001
            log.exception("петля дайджеста: %s", exc)
        await asyncio.sleep(60)


_digest_task = None


async def post_init(app):
    # Своя задача, а не Application.create_task: та ругается, что приложение ещё
    # не запущено. Ссылку держим, чтобы корректно снять её при остановке.
    global _digest_task
    _digest_task = asyncio.get_running_loop().create_task(digest_loop(app))
    log.info("бот поднят, бэкенд %s", config.API_BASE)


async def post_shutdown(app):
    if _digest_task and not _digest_task.done():
        _digest_task.cancel()


def main():
    if not config.TELEGRAM_BOT_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN в .env")
    if not config.BOT_API_TOKEN:
        raise SystemExit("Не задан BOT_API_TOKEN в .env — бэкенд не пустит бота к чужим спискам")
    db.init()

    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).post_init(post_init).post_shutdown(post_shutdown).build()
    app.add_handler(CommandHandler(["start", "help"], cmd_start))
    app.add_handler(CommandHandler(["list", "portfolio"], cmd_list))
    app.add_handler(CommandHandler(["t", "token"], cmd_token))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler(["del", "remove"], cmd_del))
    app.add_handler(CommandHandler("fg", cmd_fg))
    app.add_handler(CommandHandler("digest", cmd_digest))
    app.add_handler(CommandHandler("notes", cmd_notes))
    app.add_handler(CommandHandler("clear", cmd_clear))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(~filters.COMMAND & (filters.TEXT | filters.CAPTION), on_text))
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
