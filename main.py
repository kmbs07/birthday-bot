import os
import json
import csv
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time as dtime
from zoneinfo import ZoneInfo

from telegram import ReplyKeyboardMarkup, KeyboardButton, Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

TOKEN = os.environ.get("BOT_TOKEN")
_admin = os.environ.get("ADMIN_ID")
if not _admin:
    raise RuntimeError("ENV ADMIN_ID is not set in Railway Variables")
ADMIN_ID = int(_admin)
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Kyiv"))
REMIND_AT = os.environ.get("REMIND_AT", "09:00")
DATA_FILE = "birthdays.csv"
ACCESS_FILE = "access.json"

# ----------- UI -----------
MAIN_MENU = ReplyKeyboardMarkup([
    ["🎂 Завтра", "📅 Неделя"],
    ["🗓 Месяц", "⏳ 30 дней"],
    ["⚙️ Напоминания"],
], resize_keyboard=True)

ADMIN_MENU = ReplyKeyboardMarkup([
    ["👥 Заявки"],
    ["📋 Допущенные"],
    ["⬅️ Назад"],
], resize_keyboard=True)

# ----------- Access control -----------

def load_access():
    try:
        with open(ACCESS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except:
        return {"allowed": [ADMIN_ID], "pending": []}

def save_access(data):
    with open(ACCESS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f)

def is_admin(user_id):
    return user_id == ADMIN_ID

def is_allowed(user_id):
    data = load_access()
    return user_id in data["allowed"]

# ----------- Birthdays -----------

@dataclass
class Person:
    name: str
    born: date

def parse_date(s):
    return datetime.strptime(s.strip(), "%d.%m.%Y").date()

def load_people():
    people = []
    with open(DATA_FILE, "r", encoding="utf-8-sig") as f:
        reader = csv.reader(f, delimiter=";")
        for row in reader:
            if len(row) >= 2:
                people.append(Person(row[0], parse_date(row[1])))
    return people

def today():
    return datetime.now(TZ).date()

def next_birthday(born):
    t = today()
    nb = born.replace(year=t.year)
    if nb < t:
        nb = born.replace(year=t.year + 1)
    return nb

def upcoming(days):
    t = today()
    result = []
    for p in load_people():
        nb = next_birthday(p.born)
        diff = (nb - t).days
        if 0 <= diff <= days:
            result.append(f"• {p.name} — {nb.strftime('%d.%m')} (через {diff} дн.)")
    return result

# ----------- Handlers -----------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    access = load_access()

    if is_admin(uid):
        await update.message.reply_text("Админ панель", reply_markup=ADMIN_MENU)
        return

    if is_allowed(uid):
        await update.message.reply_text("Выберите действие 👇", reply_markup=MAIN_MENU)
        return

    # новый пользователь
    if uid not in access["pending"]:
        access["pending"].append(uid)
        save_access(access)
        await context.bot.send_message(
            ADMIN_ID,
            f"Новая заявка:\nID: {uid}\nИмя: {update.effective_user.full_name}"
        )

    await update.message.reply_text("⛔ Доступ запрещён. Заявка отправлена администратору.")

async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = update.message.text

    if is_admin(uid):
        access = load_access()

        if text == "👥 Заявки":
            if not access["pending"]:
                await update.message.reply_text("Заявок нет", reply_markup=ADMIN_MENU)
                return
            msg = "Заявки:\n"
            for pid in access["pending"]:
                msg += f"{pid}\n"
            msg += "\nВведите: approve ID или reject ID"
            await update.message.reply_text(msg)
            return

        if text.startswith("approve"):
            pid = int(text.split()[1])
            access["pending"].remove(pid)
            access["allowed"].append(pid)
            save_access(access)
            await update.message.reply_text("Одобрено", reply_markup=ADMIN_MENU)
            return

        if text.startswith("reject"):
            pid = int(text.split()[1])
            access["pending"].remove(pid)
            save_access(access)
            await update.message.reply_text("Отклонено", reply_markup=ADMIN_MENU)
            return

        if text == "📋 Допущенные":
            msg = "Допущенные:\n" + "\n".join(map(str, access["allowed"]))
            await update.message.reply_text(msg, reply_markup=ADMIN_MENU)
            return

        if text == "⬅️ Назад":
            await update.message.reply_text("Админ панель", reply_markup=ADMIN_MENU)
            return

    if not is_allowed(uid):
        await update.message.reply_text("⛔ Доступ запрещён.")
        return

    if text == "🎂 Завтра":
        items = upcoming(1)
    elif text == "📅 Неделя":
        items = upcoming(7)
    elif text == "🗓 Месяц":
        items = upcoming(30)
    elif text == "⏳ 30 дней":
        items = upcoming(30)
    else:
        return

    if items:
        await update.message.reply_text("\n".join(items), reply_markup=MAIN_MENU)
    else:
        await update.message.reply_text("Нет ближайших дней рождения", reply_markup=MAIN_MENU)

# ----------- Run -----------

def main():
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_handler))
    app.run_polling()

if __name__ == "__main__":
    main()
