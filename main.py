import os
import csv
from dataclasses import dataclass
from datetime import datetime, date, timedelta
from zoneinfo import ZoneInfo
from telegram.ext import Application, CommandHandler

TOKEN = os.environ.get("BOT_TOKEN")
TZ = ZoneInfo(os.environ.get("TZ", "Europe/Kyiv"))
DATA_FILE = "birthdays.csv"

@dataclass
class Person:
    name: str
    born: date
    notes: str = ""

def _parse_date(s: str) -> date:
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    raise ValueError(f"Bad date format: {s}")

def load_people() -> list[Person]:
    # пробуем разные кодировки (часто файл в Windows-1251)
    encodings = ["utf-8-sig", "utf-8", "cp1251"]
    last_err = None

    for enc in encodings:
        try:
            with open(DATA_FILE, "r", encoding=enc, newline="") as f:
                raw = f.read()
            # пустой файл
            if not raw.strip():
                return []

            # определим разделитель
            sample = raw[:2000]
            delim = ";" if sample.count(";") >= sample.count(",") else ","

            lines = [ln for ln in raw.splitlines() if ln.strip()]
            first = lines[0].lower()

            people: list[Person] = []

            # 1) Если есть заголовки name/date/notes — читаем как таблицу
            if "name" in first and "date" in first:
                with open(DATA_FILE, "r", encoding=enc, newline="") as f:
                    r = csv.DictReader(f, delimiter=delim)
                    for row in r:
                        name = (row.get("name") or "").strip()
                        d = (row.get("date") or "").strip()
                        notes = (row.get("notes") or "").strip()
                        if not name or not d:
                            continue
                        people.append(Person(name=name, born=_parse_date(d), notes=notes))
                return people

            # 2) Иначе считаем, что это формат: "ФИО;dd.mm.yyyy" (без заголовка)
            with open(DATA_FILE, "r", encoding=enc, newline="") as f:
                r = csv.reader(f, delimiter=delim)
                for row in r:
                    if not row or len(row) < 2:
                        continue
                    name = str(row[0]).strip()
                    d = str(row[1]).strip()
                    notes = str(row[2]).strip() if len(row) >= 3 else ""
                    if not name or not d:
                        continue
                    people.append(Person(name=name, born=_parse_date(d), notes=notes))
            return people

        except Exception as e:
            last_err = e
            continue

    # если совсем не вышло
    raise RuntimeError(f"Can't read {DATA_FILE}: {last_err}")

def today() -> date:
    return datetime.now(TZ).date()

def next_birthday(born: date, t: date) -> date:
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

def fmt(p: Person, nb: date, t: date) -> str:
    days_left = (nb - t).days
    note = f" — {p.notes}" if p.notes else ""
    return f"• {p.name}: {nb.strftime('%d.%m')} (через {days_left} дн.){note}"

def items_in_range(start: date, end: date) -> list[str]:
    t = today()
    items = []
    for p in load_people():
        nb = next_birthday(p.born, t)
        if start <= nb <= end:
            items.append((nb, p.name.lower(), fmt(p, nb, t)))
    items.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in items]

def week_range(t: date, next_week: bool) -> tuple[date, date]:
    monday = t - timedelta(days=t.weekday())
    start = monday + timedelta(days=7 if next_week else 0)
    end = start + timedelta(days=6)
    return start, end

def month_range(t: date, next_month: bool) -> tuple[date, date]:
    y, m = t.year, t.month
    if next_month:
        if m == 12:
            y, m = y + 1, 1
        else:
            m += 1
    start = date(y, m, 1)
    if m == 12:
        ny, nm = y + 1, 1
    else:
        ny, nm = y, m + 1
    end = date(ny, nm, 1) - timedelta(days=1)
    return start, end

async def start_cmd(update, context):
    await update.message.reply_text(
        "🎂 Бот ДР работает.\n"
        "Команды: /tomorrow /aftertomorrow /thisweek /nextweek /thismonth /nextmonth /nearest 14\n"
        "Диагностика: /count"
    )

async def count_cmd(update, context):
    people = load_people()
    msg = f"В файле найдено людей: {len(people)}"
    if people:
        t = today()
        # покажем первые 5 ближайших
        upcoming = []
        for p in people:
            nb = next_birthday(p.born, t)
            upcoming.append((nb, p.name))
        upcoming.sort(key=lambda x: x[0])
        preview = "\n".join([f"• {name} — {nb.strftime('%d.%m')}" for nb, name in upcoming[:5]])
        msg += "\nБлижайшие:\n" + preview
    await update.message.reply_text(msg)

async def tomorrow_cmd(update, context):
    t = today()
    d = t + timedelta(days=1)
    items = items_in_range(d, d)
    await update.message.reply_text("Завтра:\n" + ("\n".join(items) if items else "• Нет"))

async def aftertomorrow_cmd(update, context):
    t = today()
    d = t + timedelta(days=2)
    items = items_in_range(d, d)
    await update.message.reply_text("Послезавтра:\n" + ("\n".join(items) if items else "• Нет"))

async def thisweek_cmd(update, context):
    t = today()
    start, end = week_range(t, False)
    items = items_in_range(start, end)
    await update.message.reply_text(
        f"На этой неделе ({start.strftime('%d.%m')}–{end.strftime('%d.%m')}):\n" +
        ("\n".join(items) if items else "• Нет")
    )

async def nextweek_cmd(update, context):
    t = today()
    start, end = week_range(t, True)
    items = items_in_range(start, end)
    await update.message.reply_text(
        f"На следующей неделе ({start.strftime('%d.%m')}–{end.strftime('%d.%m')}):\n" +
        ("\n".join(items) if items else "• Нет")
    )

async def thismonth_cmd(update, context):
    t = today()
    start, end = month_range(t, False)
    items = items_in_range(start, end)
    await update.message.reply_text(
        f"В этом месяце ({start.strftime('%m.%Y')}):\n" +
        ("\n".join(items) if items else "• Нет")
    )

async def nextmonth_cmd(update, context):
    t = today()
    start, end = month_range(t, True)
    items = items_in_range(start, end)
    await update.message.reply_text(
        f"В следующем месяце ({start.strftime('%m.%Y')}):\n" +
        ("\n".join(items) if items else "• Нет")
    )

async def nearest_cmd(update, context):
    n = 14
    if context.args:
        try:
            n = max(1, min(366, int(context.args[0])))
        except ValueError:
            pass
    t = today()
    start, end = t, t + timedelta(days=n)
    items = items_in_range(start, end)
    await update.message.reply_text(
        f"Ближайшие {n} дней:\n" + ("\n".join(items) if items else "• Нет")
    )

def main():
    if not TOKEN:
        raise RuntimeError("BOT_TOKEN not set")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start_cmd))
    app.add_handler(CommandHandler("count", count_cmd))
    app.add_handler(CommandHandler("tomorrow", tomorrow_cmd))
    app.add_handler(CommandHandler("aftertomorrow", aftertomorrow_cmd))
    app.add_handler(CommandHandler("thisweek", thisweek_cmd))
    app.add_handler(CommandHandler("nextweek", nextweek_cmd))
    app.add_handler(CommandHandler("thismonth", thismonth_cmd))
    app.add_handler(CommandHandler("nextmonth", nextmonth_cmd))
    app.add_handler(CommandHandler("nearest", nearest_cmd))
    app.run_polling()

if __name__ == "__main__":
    main()
