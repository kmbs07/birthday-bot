import os
import json
import csv
from dataclasses import dataclass
from datetime import datetime, date, timedelta, time as dtime
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

# ---------- ENV ----------
TOKEN = os.environ.get("BOT_TOKEN")
_admin = os.environ.get("ADMIN_ID")
if not TOKEN:
    raise RuntimeError("BOT_TOKEN not set")
if not _admin:
    raise RuntimeError("ENV ADMIN_ID is not set in Railway Variables")
ADMIN_ID = int(_admin)

TZ = ZoneInfo(os.environ.get("TZ", "Europe/Kyiv"))
DATA_FILE = "birthdays.csv"
STATE_FILE = "state.json"  # access + settings stored here

# ---------- UI texts ----------
BTN_TOMORROW = "🎂 Завтра"
BTN_AFTER = "🎂 Послезавтра"

BTN_THISWEEK = "📅 Эта неделя"
BTN_NEXTWEEK = "📅 След. неделя"

BTN_THISMONTH = "🗓 Этот месяц"
BTN_NEXTMONTH = "🗓 След. месяц"

BTN_NEAREST = "⏳ Ближайшие дни"
BTN_REMIND = "⚙️ Напоминания"
BTN_ADMIN = "🛡 Админка"

def main_menu(is_admin: bool) -> ReplyKeyboardMarkup:
    rows = [
        [KeyboardButton(BTN_TOMORROW), KeyboardButton(BTN_AFTER)],
        [KeyboardButton(BTN_THISWEEK), KeyboardButton(BTN_NEXTWEEK)],
        [KeyboardButton(BTN_THISMONTH), KeyboardButton(BTN_NEXTMONTH)],
        [KeyboardButton(BTN_NEAREST)],
        [KeyboardButton(BTN_REMIND)],
    ]
    if is_admin:
        rows.append([KeyboardButton(BTN_ADMIN)])
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)

# ---------- storage ----------
def load_state() -> dict:
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            s = json.load(f)
    except Exception:
        s = {}

    s.setdefault("allowed", [])
    s.setdefault("pending", [])
    s.setdefault("settings", {})  # per user: {"remind_enabled": bool, "remind_days": int, "remind_time": "09:00"}

    # admin always allowed
    allowed = set(map(int, s.get("allowed", [])))
    allowed.add(ADMIN_ID)
    s["allowed"] = sorted(list(allowed))

    s["pending"] = sorted(list(set(map(int, s.get("pending", []) or []))))
    # normalize settings keys to str
    ss = {}
    for k, v in (s.get("settings") or {}).items():
        ss[str(k)] = v
    s["settings"] = ss
    return s

def save_state(s: dict) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False)

def is_admin(uid: int) -> bool:
    return uid == ADMIN_ID

def is_allowed(uid: int) -> bool:
    s = load_state()
    return uid in set(map(int, s.get("allowed", [])))

def get_user_settings(uid: int) -> dict:
    s = load_state()
    u = s["settings"].get(str(uid), {})
    u.setdefault("remind_enabled", False)
    u.setdefault("remind_days", 1)
    u.setdefault("remind_time", "09:00")
    return u

def set_user_settings(uid: int, patch: dict) -> None:
    s = load_state()
    u = s["settings"].get(str(uid), {})
    u.update(patch)
    s["settings"][str(uid)] = u
    save_state(s)

# ---------- birthdays ----------
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
    # tolerate delimiter and encoding
    encs = ["utf-8-sig", "utf-8", "cp1251"]
    last_err = None
    for enc in encs:
        try:
            with open(DATA_FILE, "r", encoding=enc, newline="") as f:
                raw = f.read()
            if not raw.strip():
                return []
            sample = raw[:2000]
            delim = ";" if sample.count(";") >= sample.count(",") else ","

            lines = [ln for ln in raw.splitlines() if ln.strip()]
            first = lines[0].lower()

            people: list[Person] = []

            # header: name,date
            if ("name" in first and "date" in first) or ("имя" in first and "дат" in first):
                with open(DATA_FILE, "r", encoding=enc, newline="") as f:
                    r = csv.DictReader(f, delimiter=delim)
                    for row in r:
                        name = (row.get("name") or row.get("имя") or "").strip()
                        d = (row.get("date") or row.get("дата") or "").strip()
                        if not name or not d:
                            continue
                        people.append(Person(name=name, born=parse_date(d)))
                return people

            # no header: name;date
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

def safe_month_day(year: int, m: int, d: int) -> date:
    if m == 2 and d == 29:
        try:
            return date(year, 2, 29)
        except ValueError:
            return date(year, 2, 28)
    return date(year, m, d)

def next_occurrence(born: date, base: date) -> date:
    m, d = born.month, born.day
    occ = safe_month_day(base.year, m, d)
    if occ < base:
        occ = safe_month_day(base.year + 1, m, d)
    return occ

def format_list(items: list[str]) -> str:
    return "• Нет" if not items else "\n".join(items)

def list_by_range(d_from: date, d_to: date) -> list[str]:
    # inclusive range
    people = load_people()
    rows = []
    for p in people:
        occ = next_occurrence(p.born, d_from)
        if d_from <= occ <= d_to:
            diff = (occ - today()).days
            rows.append((occ, p.name.lower(), f"• {p.name} — {occ.strftime('%d.%m')} (через {diff} дн.)"))
    rows.sort(key=lambda x: (x[0], x[1]))
    return [x[2] for x in rows]

def list_next_days(days: int) -> list[str]:
    t = today()
    return list_by_range(t, t + timedelta(days=days))

def week_bounds(which: str) -> tuple[date, date]:
    t = today()
    monday = t - timedelta(days=t.weekday())  # 0=Mon
    if which == "this":
        start = monday
    else:
        start = monday + timedelta(days=7)
    end = start + timedelta(days=6)
    return start, end

def month_bounds(which: str) -> tuple[date, date]:
    t = today()
    y, m = t.year, t.month
    if which == "next":
        if m == 12:
            y, m = y + 1, 1
        else:
            m = m + 1
    start = date(y, m, 1)
    # end = last day of month
    if m == 12:
        end = date(y + 1, 1, 1) - timedelta(days=1)
    else:
        end = date(y, m + 1, 1) - timedelta(days=1)
    return start, end

# ---------- admin inline keyboards ----------
def admin_home_kb(state: dict) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"📥 Заявки ({len(state['pending'])})", callback_data="admin:pending")],
        [InlineKeyboardButton(f"📋 Допущенные ({len(state['allowed'])})", callback_data="admin:allowed")],
    ])

def pending_list_kb(pending: list[int]) -> InlineKeyboardMarkup:
    rows = []
    for uid in pending[:15]:
        rows.append([InlineKeyboardButton(f"Заявка {uid}", callback_data=f"admin:pick:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)

def pending_pick_kb(uid: int) -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ Одобрить", callback_data=f"admin:approve:{uid}"),
            InlineKeyboardButton("❌ Отклонить", callback_data=f"admin:reject:{uid}"),
        ],
        [InlineKeyboardButton("⬅️ Назад к заявкам", callback_data="admin:pending")]
    ])

def allowed_kb(allowed: list[int]) -> InlineKeyboardMarkup:
    rows = []
    for uid in allowed[:20]:
        if uid == ADMIN_ID:
            continue
        rows.append([InlineKeyboardButton(f"🚫 Удалить {uid}", callback_data=f"admin:remove:{uid}")])
    rows.append([InlineKeyboardButton("⬅️ Назад", callback_data="admin:home")])
    return InlineKeyboardMarkup(rows)

# ---------- reminders inline keyboards ----------
def remind_kb(uid: int) -> InlineKeyboardMarkup:
    s = get_user_settings(uid)
    en = "✅ Вкл" if s["remind_enabled"] else "❌ Выкл"
    days = int(s["remind_days"])
    tm = s["remind_time"]
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"{en}", callback_data="remind:toggle")],
        [
            InlineKeyboardButton("0", callback_data="remind:days:0"),
            InlineKeyboardButton("1", callback_data="remind:days:1"),
            InlineKeyboardButton("3", callback_data="remind:days:3"),
            InlineKeyboardButton("7", callback_data="remind:days:7"),
            InlineKeyboardButton("14", callback_data="remind:days:14"),
        ],
        [
            InlineKeyboardButton("🕘 09:00", callback_data="remind:time:09:00"),
            InlineKeyboardButton("🕛 12:00", callback_data="remind:time:12:00"),
            InlineKeyboardButton("🕕 18:00", callback_data="remind:time:18:00"),
        ],
        [InlineKeyboardButton("⬅️ Закрыть", callback_data="remind:close")]
    ])

def remind_status_text(uid: int) -> str:
    s = get_user_settings(uid)
    return (
        "⚙️ Напоминания\n"
        f"Статус: {'ВКЛ' if s['remind_enabled'] else 'ВЫКЛ'}\n"
        f"За сколько дней: {s['remind_days']}\n"
        f"Время: {s['remind_time']} (TZ={TZ.key})\n\n"
        "Настрой кнопками ниже:"
    )

# ---------- jobs ----------
def parse_hhmm(v: str) -> tuple[int, int]:
    hh, mm = v.split(":")
    return int(hh), int(mm)

async def remind_job(context: ContextTypes.DEFAULT_TYPE):
    # runs periodically; checks per-user settings and sends if needed
    state = load_state()
    allowed = list(map(int, state.get("allowed", [])))
    tnow = datetime.now(TZ)
    tdate = tnow.date()
    hh = tnow.hour
    mm = tnow.minute

    for uid in allowed:
        s = get_user_settings(uid)
        if not s.get("remind_enabled"):
            continue
        try:
            rh, rm = parse_hhmm(s.get("remind_time", "09:00"))
        except Exception:
            continue
        if hh != rh or mm != rm:
            continue

        days = int(s.get("remind_days", 1))
        items = list_next_days(days)
        text = f"🔔 Напоминание (на {days} дн.)\n" + format_list(items)
        try:
            await context.bot.send_message(chat_id=uid, text=text)
        except Exception:
            pass

# ---------- handlers ----------
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    state = load_state()

    if is_allowed(uid):
        await update.message.reply_text("Выбери действие 👇", reply_markup=main_menu(is_admin(uid)))
        return

    # create request
    if uid not in state["pending"]:
        state["pending"].append(uid)
        save_state(state)
        try:
            await context.bot.send_message(
                chat_id=ADMIN_ID,
                text=f"📥 Новая заявка:\nID: {uid}\nИмя: {update.effective_user.full_name}"
            )
        except Exception:
            pass

    await update.message.reply_text("⛔ Доступ запрещён. Заявка отправлена администратору.")

async def cmd_me(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(f"Твой user_id: {update.effective_user.id}")

async def text_router(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    text = (update.message.text or "").strip()

    if not is_allowed(uid):
        await update.message.reply_text("⛔ Доступ запрещён. Напиши /start чтобы отправить заявку.")
        return

    # user functions
    if text == BTN_TOMORROW:
        items = list_by_range(today() + timedelta(days=1), today() + timedelta(days=1))
        await update.message.reply_text("Завтра:\n" + format_list(items), reply_markup=main_menu(is_admin(uid)))
        return

    if text == BTN_AFTER:
        items = list_by_range(today() + timedelta(days=2), today() + timedelta(days=2))
        await update.message.reply_text("Послезавтра:\n" + format_list(items), reply_markup=main_menu(is_admin(uid)))
        return

    if text == BTN_THISWEEK:
        a, b = week_bounds("this")
        items = list_by_range(a, b)
        await update.message.reply_text(f"Эта неделя ({a.strftime('%d.%m')}–{b.strftime('%d.%m')}):\n" + format_list(items),
                                        reply_markup=main_menu(is_admin(uid)))
        return

    if text == BTN_NEXTWEEK:
        a, b = week_bounds("next")
        items = list_by_range(a, b)
        await update.message.reply_text(f"След. неделя ({a.strftime('%d.%m')}–{b.strftime('%d.%m')}):\n" + format_list(items),
                                        reply_markup=main_menu(is_admin(uid)))
        return

    if text == BTN_THISMONTH:
        a, b = month_bounds("this")
        items = list_by_range(a, b)
        await update.message.reply_text(f"Этот месяц ({a.strftime('%m.%Y')}):\n" + format_list(items),
                                        reply_markup=main_menu(is_admin(uid)))
        return

    if text == BTN_NEXTMONTH:
        a, b = month_bounds("next")
        items = list_by_range(a, b)
        await update.message.reply_text(f"След. месяц ({a.strftime('%m.%Y')}):\n" + format_list(items),
                                        reply_markup=main_menu(is_admin(uid)))
        return

    if text == BTN_NEAREST:
        # quick select inline
        kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("7 дней", callback_data="near:7"),
             InlineKeyboardButton("14 дней", callback_data="near:14"),
             InlineKeyboardButton("30 дней", callback_data="near:30")],
        ])
        await update.message.reply_text("Выбери период:", reply_markup=kb)
        return

    if text == BTN_REMIND:
        await update.message.reply_text(remind_status_text(uid), reply_markup=main_menu(is_admin(uid)))
        await update.message.reply_text("Настройки:", reply_markup=remind_kb(uid))
        return

    # admin entry
    if text == BTN_ADMIN:
        if not is_admin(uid):
            await update.message.reply_text("⛔ Недостаточно прав.", reply_markup=main_menu(False))
            return
        state = load_state()
        await update.message.reply_text(
            f"🛡 Админка\n✅ Допущено: {len(state['allowed'])}\n📥 Заявок: {len(state['pending'])}",
            reply_markup=main_menu(True)
        )
        await update.message.reply_text("Админ-действия:", reply_markup=admin_home_kb(state))
        return

    await update.message.reply_text("Пользуйся кнопками меню 👇", reply_markup=main_menu(is_admin(uid)))

async def callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    uid = q.from_user.id
    data = q.data or ""
    await q.answer()

    # ---- nearest ----
    if data.startswith("near:"):
        if not is_allowed(uid):
            await q.edit_message_text("⛔ Доступ запрещён.")
            return
        days = int(data.split(":")[1])
        items = list_next_days(days)
        await q.edit_message_text(f"Ближайшие {days} дней:\n" + format_list(items))
        return

    # ---- reminders ----
    if data.startswith("remind:"):
        if not is_allowed(uid):
            await q.edit_message_text("⛔ Доступ запрещён.")
            return
        if data == "remind:close":
            await q.edit_message_text("Ок.")
            return
        if data == "remind:toggle":
            s = get_user_settings(uid)
            set_user_settings(uid, {"remind_enabled": not s["remind_enabled"]})
            await q.edit_message_text(remind_status_text(uid), reply_markup=remind_kb(uid))
            return
        if data.startswith("remind:days:"):
            v = int(data.split(":")[-1])
            set_user_settings(uid, {"remind_days": v})
            await q.edit_message_text(remind_status_text(uid), reply_markup=remind_kb(uid))
            return
        if data.startswith("remind:time:"):
            v = data.split(":")[-2] + ":" + data.split(":")[-1]
            set_user_settings(uid, {"remind_time": v})
            await q.edit_message_text(remind_status_text(uid), reply_markup=remind_kb(uid))
            return

    # ---- admin ----
    if data.startswith("admin:"):
        if not is_admin(uid):
            await q.edit_message_text("⛔ Недостаточно прав.")
            return
        state = load_state()

        if data == "admin:home":
            await q.edit_message_text("Админ-действия:", reply_markup=admin_home_kb(state))
            return

        if data == "admin:pending":
            if not state["pending"]:
                await q.edit_message_text("📥 Заявок нет.", reply_markup=admin_home_kb(state))
                return
            await q.edit_message_text("📥 Заявки (выбери):", reply_markup=pending_list_kb(state["pending"]))
            return

        if data.startswith("admin:pick:"):
            pid = int(data.split(":")[-1])
            await q.edit_message_text(f"Заявка {pid}:", reply_markup=pending_pick_kb(pid))
            return

        if data.startswith("admin:approve:"):
            pid = int(data.split(":")[-1])
            if pid in state["pending"]:
                state["pending"].remove(pid)
            if pid not in state["allowed"]:
                state["allowed"].append(pid)
            save_state(state)
            try:
                await context.bot.send_message(pid, "✅ Доступ одобрен. Напиши /start")
            except Exception:
                pass
            state = load_state()
            await q.edit_message_text(f"✅ Одобрено: {pid}", reply_markup=admin_home_kb(state))
            return

        if data.startswith("admin:reject:"):
            pid = int(data.split(":")[-1])
            if pid in state["pending"]:
                state["pending"].remove(pid)
            save_state(state)
            state = load_state()
            await q.edit_message_text(f"❌ Отклонено: {pid}", reply_markup=admin_home_kb(state))
            return

        if data == "admin:allowed":
            await q.edit_message_text("📋 Допущенные (удаление кнопкой):", reply_markup=allowed_kb(state["allowed"]))
            return

        if data.startswith("admin:remove:"):
            rid = int(data.split(":")[-1])
            if rid == ADMIN_ID:
                await q.edit_message_text("Админа удалить нельзя.", reply_markup=admin_home_kb(load_state()))
                return
            if rid in state["allowed"]:
                state["allowed"].remove(rid)
            save_state(state)
            await q.edit_message_text(f"🚫 Удалён доступ: {rid}", reply_markup=admin_home_kb(load_state()))
            return

# ---------- main ----------
def main():
    app = Application.builder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("me", cmd_me))
    app.add_handler(CallbackQueryHandler(callbacks))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, text_router))

    # reminder checker: every minute
    app.job_queue.run_repeating(remind_job, interval=60, first=10)

    app.run_polling()

if __name__ == "__main__":
    main()
