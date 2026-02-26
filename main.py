import os
import json
import csv
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo

from telegram import (
    Update,
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN")
_admin = os.environ.get("ADMIN_ID")
if not _admin:
    raise RuntimeError("ENV ADMIN_ID is not set in Railway Variables")
ADMIN_ID = int(_admin)

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Kyiv"))
DATA_FILE = "birthdays.csv"
ACCESS_FILE = "access.json"

# ---------- UI (Reply keyboard) ----------
BTN_TOMORROW = "🎂 Завтра"
BTN_WEEK = "📅 Неделя"
BTN_MONTH = "🗓 Месяц"
BTN_30 = "⏳ 30 дней"
BTN_REMIND = "⚙️ Напоминания"
BTN_ADMIN = "🛡 Админка"

def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_TOMORROW), KeyboardButton(BTN_WEEK)],
        [KeyboardButton(BTN_MONTH), KeyboardButton(BTN_30)],
        [KeyboardButton(BTN_REMIND)],
    ]
    if is_admin:
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

# ---------- Access storage ----------
def load_access() -> dict:
    try:
        with open(ACCESS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        data = {"allowed": [], "pending": []}

    # гарантируем, что админ всегда allowed
    allowed = set(map(int, data.get("allowed", [])))
    allowed.add(ADMIN_ID)
    data["allowed"] = sorted(list(allowed))
    data["pending"] = sorted(list(set(map(int, data.get("pending", []))))) if data.get("pending") else []
    return data

def save_access(data: dict) -> None:
    with open(ACCESS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)

def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID

def is_allowed(user_id: int) -> bool:
    data = load_access()
    return user_id in set(map(int, data.get("allowed", [])))

# ---------- Birthdays ----------
@dataclass
class Person:
    name: str
    born: date

def parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%d.%m.%Y", "%Y-%m-%d", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Bad date: {s}")

def load_people() -> list[Person]:
    encs = ["utf-8-sig", "utf-8", "cp1251"]
    last_err = None
    for enc in encs:
        try:
            with open(DATA_FILE, "r", encoding=enc, newline="") as f:
                raw = f.read()
            if not raw.strip():
                return []
            # delimiter guess
            sample = raw[:2000]
            delim = ";" if sample.count(";") >= sample.count(",") else ","
            lines = [ln for ln in raw.splitlines() if ln.strip()]
            first = lines[0].lower()

            people = []

            # with headers: name,date,notes (notes optional)
            if "name" in first and "date" in first:
                with open(DATA_FILE, "r", encoding=enc, newline="") as f:
                    r = csv.DictReader(f, delimiter=delim)
                    for row in r:
                        name = (row.get("name") or "").strip()
                        d = (row.get("date") or "").strip()
                        if not name or not d:
                            continue
                        people.append(Person(name=name, born=parse_date(d)))
                return people

            # without headers: name;date;...
            with open(DATA_FILE, "r", encoding=enc, newline="") as f:
                r = csv.reader(f, delimiter=delim)
                for row in r:
                    if not row or len(row) < 2:
                        continue
                    name = str(row[0]).strip()
                    d = str(row[1]).strip()
                    if not name or not d:
                        continue
                    people.append(Person(name=name, born=parse_date(d)))
            return people

        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Can't read birthdays file: {last_err}")

def today() -> date:
    return datetime.now(TZ).date()

def next_bday(born: date, t: date) -> date:
    m, d = born.month, born.day
    y = t.year

    def safe(y_):
        if m == 2 and d == 29:
            try:
                return date(y_, 2, 29)
            except ValueError:
                return date(y_, 2, 28)
        return date(y_, m, d)

    nb = safe(y)
    if nb < t:
        nb = safe(y + 1)
    return nb

def upcoming(days: int) -> list[str]:
    t = today()
    people = load_people()
    rows = []
    for p in people:
        nb = next_bday(p.born, t)
        diff = (nb - t).days
        if 0 <= diff <= days:
            rows.append((nb, p.name.lower(), f"• {p.name} — {nb.strftime('%d.%m')} (через {diff} дн.)"))
    rows.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in rows]

# ---------- Admin UI ----------
def admin_panel_text(access: dict) -> str:
    return (
        "🛡 Админка\n"
        f"✅ Допущено: {len(access['allowed'])}\n"
        f"📥 Заявок: {len(access['pending'])}\n\n"
        "Выбери действие:"
    )

def admin_panel_keyboard(access: dict) -> InlineKeyboardMarkup:
    btns = []
    btns.append([InlineKeyboardButton("📥 Заявки", callback_data="admin:pending")])
    btns.append([InlineKeyboardButton("📋 Допущенные", callback_data="admin:allowed")])
    return InlineKeyboardMarkup(btns)

def pending_keyboard(pending_ids: list[int]) -> InlineKeyboardMarkup:
    # показываем по одной заявке кнопками Approve/Reject
    # для простоты: листинг и выбор конкретной заявки
    rows = []
    for uid in pending_ids[:10]:  # ограничим 10
        rows.append([InlineKeyboardButton(f"Заявка {uid}", callback_data=f"admin:pick:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:back")])
    return InlineKeyboardMarkup(rows)

def picked_keyboard(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"admin:approve:{uid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:reject:{uid}"),
        ],
        [InlineKeyboardButton("⬅️ Назад к заявкам", callback_data="admin:pending")]
    ])

def allowed_keyboard(allowed_ids: list[int]) -> InlineKeyboardMarkup:
    rows = []
    # дать возможность удалить (кроме админа)
    for uid in allowed_ids[:10]:
        if uid == ADMIN_ID:
            continue
        rows.append([InlineKeyboardButton(f"🚫 Удалить {uid}", callback_data=f"admin:remove:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:back")])
    return InlineKeyboardMarkup(rows)

# ---------- Handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    access = load_access()

    if is_allowed(uid):
        await update.message.reply_text("Выбери действие 👇", reply_markup=main_menu(is_admin(uid)))
        return

    # не допущен — создаём заявку
    if uid not in access["pending"]:
        access["pending"].append(uid)
        save_access(access)

        # сообщаем админу
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📥 Новая заявка:\nID: {uid}\nИмя: {update.effective_user.full_name}"
            )
        except Exception:
            pass

    await update.message.reply_text("⛔ Доступ запрещён. Заявка отправлена администратору.")

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    await update.message.reply_text(f"Твой user_id: {uid}")

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    # не допущен
    if not is_allowed(uid):
        await update.message.reply_text("⛔ Доступ запрещён. Напиши /start чтобы отправить заявку.")
        return

    # меню пользователя (и админа тоже)
    if text == BTN_TOMORROW:
        items = upcoming(1)
        return await update.message.reply_text("Завтра:\n" + ("\n".join(items) if items else "• Нет"), reply_markup=main_menu(is_admin(uid)))

    if text == BTN_WEEK:
        items = upcoming(7)
        return await update.message.reply_text("Ближайшие 7 дней:\n" + ("\n".join(items) if items else "• Нет"), reply_markup=main_menu(is_admin(uid)))

    if text == BTN_MONTH:
        items = upcoming(30)
        return await update.message.reply_text("Ближайшие 30 дней:\n" + ("\n".join(items) if items else "• Нет"), reply_markup=main_menu(is_admin(uid)))

    if text == BTN_30:
        items = upcoming(30)
        return await update.message.reply_text("Ближайшие 30 дней:\n" + ("\n".join(items) if items else "• Нет"), reply_markup=main_menu(is_admin(uid)))

    if text == BTN_REMIND:
        return await update.message.reply_text("⚙️ Напоминания (следующий шаг сделаем дальше).", reply_markup=main_menu(is_admin(uid)))

    if text == BTN_ADMIN:
        if not is_admin(uid):
            return await update.message.reply_text("⛔ Недостаточно прав.", reply_markup=main_menu(False))
        access = load_access()
        return await update.message.reply_text(admin_panel_text(access), reply_markup=main_menu(True)) or await update.message.reply_text(
            "Админ-действия:",
            reply_markup=admin_panel_keyboard(access)
        )

    await update.message.reply_text("Пользуйся кнопками меню 👇", reply_markup=main_menu(is_admin(uid)))

async def admin_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    uid = query.from_user.id
    await query.answer()

    if not is_admin(uid):
        await query.edit_message_text("⛔ Недостаточно прав.")
        return

    access = load_access()
    data = query.data or ""

    if data == "admin:back":
        await query.edit_message_text("Админ-действия:", reply_markup=admin_panel_keyboard(access))
        return

    if data == "admin:pending":
        if not access["pending"]:
            await query.edit_message_text("📥 Заявок нет.", reply_markup=admin_panel_keyboard(access))
            return
        await query.edit_message_text("📥 Заявки (выбери):", reply_markup=pending_keyboard(access["pending"]))
        return

    if data.startswith("admin:pick:"):
        pid = int(data.split(":")[-1])
        await query.edit_message_text(f"Заявка {pid}:", reply_markup=picked_keyboard(pid))
        return

    if data.startswith("admin:approve:"):
        pid = int(data.split(":")[-1])
        if pid in access["pending"]:
            access["pending"].remove(pid)
        if pid not in access["allowed"]:
            access["allowed"].append(pid)
        save_access(access)

        # уведомим пользователя
        try:
            await context.bot.send_message(pid, "✅ Доступ одобрен. Напиши /start")
        except Exception:
            pass

        await query.edit_message_text(f"✅ Одобрено: {pid}", reply_markup=admin_panel_keyboard(load_access()))
        return

    if data.startswith("admin:reject:"):
        pid = int(data.split(":")[-1])
        if pid in access["pending"]:
            access["pending"].remove(pid)
        save_access(access)
        await query.edit_message_text(f"❌ Отклонено: {pid}", reply_markup=admin_panel_keyboard(load_access()))
        return

    if data == "admin:allowed":
        await query.edit_message_text(
            "📋 Допущенные (можно удалить кнопкой):",
            reply_markup=allowed_keyboard(access["allowed"])
        )
        return

    if data.startswith("admin:remove:"):
        rid = int(data.split(":")[-1])
        if rid == ADMIN_ID:
            await query.edit_message_text("Админа удалить нельзя.", reply_markup=admin_panel_keyboard(load_access()))
            return
        if rid in access["allowed"]:
            access["allowed"].remove(rid)
        save_access(access)
        await query.edit_message_text(f"🚫 Удалён доступ: {rid}", reply_markup=admin_panel_keyboard(load_access()))
        return

# ---------- Main ----------
def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN not set")

    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("me", cmd_me))  # на будущее
    app.add_handler(CallbackQueryHandler(admin_callbacks, pattern=r"^admin:"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    app.run_polling()

if __name__ == "__main__":
    main()
