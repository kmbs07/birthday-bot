import os
import json
import csv
import re
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo

from telegram import Update, ReplyKeyboardMarkup, ReplyKeyboardRemove
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# =========================
# ENV
# =========================
BOT_TOKEN = os.environ.get("BOT_TOKEN", "").strip()
ADMIN_ID_RAW = os.environ.get("ADMIN_ID", "").strip()
TZ_NAME = os.environ.get("TZ", "Europe/Kyiv").strip()

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is not set")
if not ADMIN_ID_RAW:
    raise RuntimeError("ENV ADMIN_ID is not set")
ADMIN_ID = int(ADMIN_ID_RAW)

# =========================
# FILES
# =========================
BIRTHDAYS_CSV = "birthdays.csv"        # repo root рядом с main.py
ALLOWED_JSON = "allowed_users.json"    # создастся автоматически

# =========================
# STATES (Conversation)
# =========================
SEARCH_WAIT = 10

# =========================
# HELPERS
# =========================
def tznow() -> datetime:
    return datetime.now(ZoneInfo(TZ_NAME))

def today_local() -> date:
    return tznow().date()

def normalize_name(s: str) -> str:
    """
    Нормализация для поиска:
    - lower
    - ё->е
    - убираем лишние символы
    - схлопываем пробелы
    """
    s = s.strip().lower()
    s = s.replace("ё", "е")
    # апострофы и дефисы оставим как разделители → заменим на пробел
    s = s.replace("’", "'").replace("`", "'")
    s = re.sub(r"[^a-zа-яіїєґ0-9'\-\s]+", " ", s, flags=re.IGNORECASE)
    s = re.sub(r"[\-']+", " ", s)  # дефисы/апострофы → пробел
    s = re.sub(r"\s+", " ", s).strip()
    return s

def load_birthdays() -> list[dict]:
    """
    Возвращает список:
    [{ "name": "...", "bday": date, "bday_str": "dd.mm.yyyy", "norm": "..." }, ...]
    CSV: "ФИО;dd.mm.yyyy"
    ВАЖНО: поддерживает cp1251 (твой файл) и utf-8-sig.
    """
    if not os.path.exists(BIRTHDAYS_CSV):
        raise RuntimeError(f"File not found: {BIRTHDAYS_CSV}")

    encodings_to_try = ["utf-8-sig", "cp1251", "utf-8", "latin1"]
    last_err = None

    for enc in encodings_to_try:
        try:
            rows = []
            with open(BIRTHDAYS_CSV, "r", encoding=enc, errors="strict", newline="") as f:
                reader = csv.reader(f, delimiter=";")
                for i, row in enumerate(reader, start=1):
                    if not row:
                        continue
                    if len(row) < 2:
                        # пропускаем мусорные строки
                        continue
                    name = row[0].strip()
                    bday_raw = row[1].strip()
                    # dd.mm.yyyy
                    dt = datetime.strptime(bday_raw, "%d.%m.%Y").date()
                    rows.append({
                        "name": name,
                        "bday": dt,
                        "bday_str": bday_raw,
                        "norm": normalize_name(name),
                    })
            # если успешно прочитали и не пусто — возвращаем
            if rows:
                return rows
        except Exception as e:
            last_err = e
            continue

    raise RuntimeError(f"Could not read {BIRTHDAYS_CSV}. Last error: {last_err}")

def load_allowed() -> set[int]:
    """
    allowed_users.json:
    { "allowed": [123, 456] }
    Админ всегда имеет доступ.
    """
    allowed = {ADMIN_ID}
    if os.path.exists(ALLOWED_JSON):
        try:
            with open(ALLOWED_JSON, "r", encoding="utf-8") as f:
                data = json.load(f)
            for x in data.get("allowed", []):
                try:
                    allowed.add(int(x))
                except Exception:
                    pass
        except Exception:
            pass
    return allowed

def save_allowed(allowed: set[int]) -> None:
    data = {"allowed": sorted(list(set(int(x) for x in allowed)))}
    with open(ALLOWED_JSON, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def ensure_access(update: Update) -> bool:
    uid = update.effective_user.id if update.effective_user else 0
    return uid in load_allowed()

def main_menu_keyboard(uid: int) -> ReplyKeyboardMarkup:
    # УБРАЛИ "Этот месяц / След. месяц"
    rows = [
        ["🎂 Завтра", "🎂 Послезавтра"],
        ["📅 Эта неделя", "📅 След. неделя"],
        ["⏳ Ближайшие дни", "🔎 Поиск"],
    ]
    if is_admin(uid):
        rows.append(["⚙️ Админка"])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

def days_until_next(bday: date, base: date) -> int:
    """
    Сколько дней до ближайшего дня рождения (по дню/месяцу),
    относительно base.
    """
    next_dt = date(base.year, bday.month, bday.day)
    if next_dt < base:
        next_dt = date(base.year + 1, bday.month, bday.day)
    return (next_dt - base).days

def list_nearest(days: int, data: list[dict]) -> list[dict]:
    base = today_local()
    items = []
    for r in data:
        du = days_until_next(r["bday"], base)
        if 0 <= du <= days:
            items.append((du, r))
    items.sort(key=lambda x: (x[0], normalize_name(x[1]["name"])))
    return [{"du": du, **r} for du, r in items]

def format_list(title: str, items: list[dict], show_days_left: bool = True) -> str:
    if not items:
        return f"{title}\n• Нет"
    lines = [title]
    for r in items:
        if show_days_left:
            lines.append(f"• {r['name']} — {r['bday'].strftime('%d.%m')} (через {r['du']} дн.)")
        else:
            # для поиска — показываем полную дату рождения
            lines.append(f"• {r['name']} — {r['bday_str']}")
    return "\n".join(lines)

def week_range(which: str) -> tuple[date, date]:
    """
    which: 'this' or 'next'
    Неделя: Пн..Вс
    """
    base = today_local()
    monday = base - timedelta(days=base.weekday())
    if which == "next":
        monday = monday + timedelta(days=7)
    sunday = monday + timedelta(days=6)
    return monday, sunday

def list_week(which: str, data: list[dict]) -> list[dict]:
    start, end = week_range(which)
    res = []
    base = today_local()
    for r in data:
        # считаем ближайшее ДР (день/месяц) и смотрим, попадает ли в диапазон
        next_dt = date(base.year, r["bday"].month, r["bday"].day)
        if next_dt < base:
            next_dt = date(base.year + 1, r["bday"].month, r["bday"].day)
        if start <= next_dt <= end:
            res.append((next_dt, r))
    res.sort(key=lambda x: (x[0], normalize_name(x[1]["name"])))
    out = []
    for next_dt, r in res:
        out.append({
            **r,
            "du": (next_dt - base).days
        })
    return out

# =========================
# HANDLERS
# =========================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not ensure_access(update):
        await update.message.reply_text(
            "⛔️ Нет доступа.\nНапиши админу, чтобы тебя добавили.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return

    await update.message.reply_text(
        "Выбери действие 👇",
        reply_markup=main_menu_keyboard(uid),
    )

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "Команды:\n"
        "/start — меню\n"
        "/search — поиск по ФИО (или кнопка 🔎 Поиск)\n"
        "\nАдмин:\n"
        "/allow <id>\n"
        "/deny <id>\n"
        "/allowed — список допущенных",
    )

async def cmd_search(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not ensure_access(update):
        await update.message.reply_text("⛔️ Нет доступа.")
        return ConversationHandler.END

    await update.message.reply_text(
        "🔎 Введи часть ФИО для поиска (например: Пащ)\n"
        "Отмена — напиши: /cancel",
        reply_markup=ReplyKeyboardMarkup([["Отмена"]], resize_keyboard=True),
    )
    return SEARCH_WAIT

async def search_wait_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not ensure_access(update):
        await update.message.reply_text("⛔️ Нет доступа.")
        return ConversationHandler.END

    text = (update.message.text or "").strip()
    if text.lower() in ("отмена", "/cancel"):
        uid = update.effective_user.id if update.effective_user else 0
        await update.message.reply_text("Ок, отменил.", reply_markup=main_menu_keyboard(uid))
        return ConversationHandler.END

    q = normalize_name(text)
    if not q:
        await update.message.reply_text("Введи хоть пару букв (например: Пащ).")
        return SEARCH_WAIT

    data = context.bot_data.get("birthdays", [])
    matches = [r for r in data if q in r["norm"]]

    # ограничим, чтобы не спамить простынями
    matches = sorted(matches, key=lambda r: r["norm"])[:30]

    if not matches:
        await update.message.reply_text("Ничего не нашёл. Попробуй по-другому (например короче).")
        return SEARCH_WAIT

    msg = format_list("Найдено:", [{"du": 0, **r} for r in matches], show_days_left=False)
    await update.message.reply_text(msg)
    # остаёмся в режиме поиска, чтобы можно было сразу искать дальше
    return SEARCH_WAIT

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    uid = update.effective_user.id if update.effective_user else 0
    await update.message.reply_text("Ок.", reply_markup=main_menu_keyboard(uid))
    return ConversationHandler.END

async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not ensure_access(update):
        await update.message.reply_text("⛔️ Нет доступа.")
        return

    uid = update.effective_user.id if update.effective_user else 0
    text = (update.message.text or "").strip()
    data = context.bot_data.get("birthdays", [])

    if text == "🎂 Завтра":
        base = today_local()
        items = []
        for r in data:
            next_dt = date(base.year, r["bday"].month, r["bday"].day)
            if next_dt < base:
                next_dt = date(base.year + 1, r["bday"].month, r["bday"].day)
            if (next_dt - base).days == 1:
                items.append({"du": 1, **r})
        items.sort(key=lambda r: r["norm"])
        await update.message.reply_text(format_list("Завтра:", items))
        return

    if text == "🎂 Послезавтра":
        base = today_local()
        items = []
        for r in data:
            next_dt = date(base.year, r["bday"].month, r["bday"].day)
            if next_dt < base:
                next_dt = date(base.year + 1, r["bday"].month, r["bday"].day)
            if (next_dt - base).days == 2:
                items.append({"du": 2, **r})
        items.sort(key=lambda r: r["norm"])
        await update.message.reply_text(format_list("Послезавтра:", items))
        return

    if text == "📅 Эта неделя":
        start, end = week_range("this")
        items = list_week("this", data)
        await update.message.reply_text(
            format_list(f"На этой неделе ({start.strftime('%d.%m')}–{end.strftime('%d.%m')}):", items)
        )
        return

    if text == "📅 След. неделя":
        start, end = week_range("next")
        items = list_week("next", data)
        await update.message.reply_text(
            format_list(f"На следующей неделе ({start.strftime('%d.%m')}–{end.strftime('%d.%m')}):", items)
        )
        return

    if text == "⏳ Ближайшие дни":
        items = list_nearest(14, data)
        await update.message.reply_text(format_list("Ближайшие 14 дней:", items))
        return

    if text == "🔎 Поиск":
        # запускаем тот же сценарий, что /search
        await cmd_search(update, context)
        return

    if text == "⚙️ Админка":
        if not is_admin(uid):
            await update.message.reply_text("⛔️ Нет прав.")
            return
        allowed = load_allowed()
        await update.message.reply_text(
            "⚙️ Админка\n"
            f"✅ Допущено: {len(allowed)}\n"
            "Команды:\n"
            "/allow <id> — добавить\n"
            "/deny <id> — удалить\n"
            "/allowed — список",
            reply_markup=main_menu_keyboard(uid),
        )
        return

    # если непонятно что нажали/написали
    await update.message.reply_text("Не понял. Нажми кнопку в меню 👇", reply_markup=main_menu_keyboard(uid))

# =========================
# ADMIN COMMANDS
# =========================
async def admin_allow(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        await update.message.reply_text("⛔️ Нет прав.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /allow 123456789")
        return
    try:
        new_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("ID должен быть числом.")
        return

    allowed = load_allowed()
    allowed.add(new_id)
    save_allowed(allowed)
    await update.message.reply_text(f"✅ Добавил в допуск: {new_id}")

async def admin_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        await update.message.reply_text("⛔️ Нет прав.")
        return

    if not context.args:
        await update.message.reply_text("Использование: /deny 123456789")
        return
    try:
        del_id = int(context.args[0])
    except Exception:
        await update.message.reply_text("ID должен быть числом.")
        return

    if del_id == ADMIN_ID:
        await update.message.reply_text("Админа удалить нельзя 🙂")
        return

    allowed = load_allowed()
    if del_id in allowed:
        allowed.remove(del_id)
        save_allowed(allowed)
        await update.message.reply_text(f"🗑 Удалил из допуска: {del_id}")
    else:
        await update.message.reply_text("Этого ID и так нет в списке.")

async def admin_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    uid = update.effective_user.id if update.effective_user else 0
    if not is_admin(uid):
        await update.message.reply_text("⛔️ Нет прав.")
        return

    allowed = sorted(list(load_allowed()))
    await update.message.reply_text("✅ Допущенные ID:\n" + "\n".join(str(x) for x in allowed))

# =========================
# MAIN
# =========================
def main() -> None:
    birthdays = load_birthdays()

    app = Application.builder().token(BOT_TOKEN).build()
    app.bot_data["birthdays"] = birthdays

    # базовые
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))

    # админ
    app.add_handler(CommandHandler("allow", admin_allow))
    app.add_handler(CommandHandler("deny", admin_deny))
    app.add_handler(CommandHandler("allowed", admin_allowed))

    # поиск (диалог)
    search_conv = ConversationHandler(
        entry_points=[
            CommandHandler("search", cmd_search),
            MessageHandler(filters.Regex(r"^🔎 Поиск$"), cmd_search),
        ],
        states={
            SEARCH_WAIT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_wait_text)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel), MessageHandler(filters.Regex(r"^Отмена$"), cancel)],
        allow_reentry=True,
    )
    app.add_handler(search_conv)

    # меню-кнопки
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_menu_click))

    app.run_polling(close_loop=False)

if __name__ == "__main__":
    main()
