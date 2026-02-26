import os
import json
import csv
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, date
from zoneinfo import ZoneInfo
from typing import Dict, List, Tuple, Optional, Set

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    ContextTypes,
    filters,
)

# ======================
# ENV / SETTINGS
# ======================
BOT_TOKEN = os.environ.get("BOT_TOKEN")
ADMIN_ID_RAW = os.environ.get("ADMIN_ID")
TZ_NAME = os.environ.get("TZ", "Europe/Kyiv")

if not BOT_TOKEN:
    raise RuntimeError("ENV BOT_TOKEN is not set")
if not ADMIN_ID_RAW:
    raise RuntimeError("ENV ADMIN_ID is not set")
ADMIN_ID = int(ADMIN_ID_RAW)

TZ = ZoneInfo(TZ_NAME)

DATA_FILE = "birthdays.csv"         # ФИО;ДД.ММ.ГГГГ
STATE_FILE = "state.json"           # allowlist + pending + reminder settings

MAX_ALLOWED_USERS = 5  # не считая админа

# ======================
# UI TEXTS
# ======================
BTN_TOMORROW = "🎂 Завтра"
BTN_AFTER_TOMORROW = "🎂 Послезавтра"
BTN_THIS_WEEK = "📅 Эта неделя"
BTN_NEXT_WEEK = "📅 След. неделя"
BTN_NEAREST = "⏳ Ближайшие дни"
BTN_SEARCH = "🔎 Поиск по ФИО"
BTN_REMINDERS = "⚙️ Напоминания"
BTN_ADMIN = "🛠 Админка"

# Callback data
CB_REQ_ACCESS = "req_access"
CB_ADMIN_PANEL = "admin_panel"
CB_ADMIN_PENDING = "admin_pending"
CB_ADMIN_ALLOWED = "admin_allowed"
CB_ADMIN_ADD = "admin_add"
CB_ADMIN_REMOVE = "admin_remove"
CB_ADMIN_APPROVE_PREFIX = "admin_approve:"
CB_ADMIN_DENY_PREFIX = "admin_deny:"
CB_NEAREST_PREFIX = "nearest:"  # nearest:7 / nearest:14 / nearest:30
CB_BACK_MAIN = "back_main"

# Conversation states
SEARCH_WAIT_QUERY = 10
ADMIN_WAIT_ADD_ID = 20
ADMIN_WAIT_REMOVE_ID = 30

# ======================
# DATA MODELS
# ======================
@dataclass(frozen=True)
class Person:
    fio: str
    day: int
    month: int
    year: int


def now_local() -> datetime:
    return datetime.now(TZ)


# ======================
# PERSISTENCE
# ======================
def load_state() -> dict:
    default = {
        "allowed_users": [],      # list[int] (не включает ADMIN_ID, он всегда админ)
        "pending_requests": [],   # list[int]
        "reminder_days": 7,       # будущий шаг
    }
    if not os.path.exists(STATE_FILE):
        return default
    try:
        with open(STATE_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        # Мягкая нормализация
        for k in default:
            if k not in data:
                data[k] = default[k]
        data["allowed_users"] = [int(x) for x in data.get("allowed_users", [])]
        data["pending_requests"] = [int(x) for x in data.get("pending_requests", [])]
        data["reminder_days"] = int(data.get("reminder_days", 7))
        return data
    except Exception:
        return default


def save_state(state: dict) -> None:
    tmp = dict(state)
    tmp["allowed_users"] = list({int(x) for x in tmp.get("allowed_users", [])})
    tmp["pending_requests"] = list({int(x) for x in tmp.get("pending_requests", [])})
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(tmp, f, ensure_ascii=False, indent=2)


def get_allowed(state: dict) -> Set[int]:
    return set(state.get("allowed_users", []))


def get_pending(state: dict) -> Set[int]:
    return set(state.get("pending_requests", []))


def is_admin(user_id: int) -> bool:
    return user_id == ADMIN_ID


def is_allowed(user_id: int, state: dict) -> bool:
    return is_admin(user_id) or (user_id in get_allowed(state))


# ======================
# BIRTHDAYS LOADING
# ======================
def load_birthdays() -> List[Person]:
    people: List[Person] = []
    if not os.path.exists(DATA_FILE):
        return people

    # CSV: ФИО;ДД.ММ.ГГГГ (без заголовков)
    # Бывает cp1251 — пробуем оба варианта
    encodings = ["utf-8-sig", "cp1251"]
    last_err = None

    for enc in encodings:
        try:
            with open(DATA_FILE, "r", encoding=enc, newline="") as f:
                reader = csv.reader(f, delimiter=";")
                for row in reader:
                    if not row or len(row) < 2:
                        continue
                    fio = (row[0] or "").strip()
                    dstr = (row[1] or "").strip()
                    m = re.match(r"^(\d{2})\.(\d{2})\.(\d{4})$", dstr)
                    if not fio or not m:
                        continue
                    dd, mm, yyyy = int(m.group(1)), int(m.group(2)), int(m.group(3))
                    # базовая валидация даты
                    try:
                        _ = date(yyyy, mm, dd)
                    except ValueError:
                        continue
                    people.append(Person(fio=fio, day=dd, month=mm, year=yyyy))
            return people
        except Exception as e:
            last_err = e

    # если вообще не прочитали — вернем пусто
    return []


# ======================
# DATE / FILTER HELPERS
# ======================
def next_birthday_date(p: Person, from_dt: datetime) -> date:
    """Следующая дата ДР начиная с from_dt (год текущий или следующий)."""
    y = from_dt.date().year
    try:
        d = date(y, p.month, p.day)
    except ValueError:
        # 29.02 — упрощенно: 28.02
        d = date(y, 2, 28)

    if d < from_dt.date():
        y2 = y + 1
        try:
            d = date(y2, p.month, p.day)
        except ValueError:
            d = date(y2, 2, 28)
    return d


def days_until(d: date, from_dt: datetime) -> int:
    return (d - from_dt.date()).days


def format_person_line(p: Person, bday: date, from_dt: datetime) -> str:
    du = days_until(bday, from_dt)
    return f"• {p.fio} — {bday.strftime('%d.%m')} (через {du} дн.)"


def filter_between(people: List[Person], start: date, end_exclusive: date, from_dt: datetime) -> List[Tuple[Person, date]]:
    """Возвращает тех, у кого ближайший ДР попадает в [start, end_exclusive)."""
    res: List[Tuple[Person, date]] = []
    for p in people:
        nb = next_birthday_date(p, from_dt)
        if start <= nb < end_exclusive:
            res.append((p, nb))
    res.sort(key=lambda x: (x[1].month, x[1].day, x[0].fio.lower()))
    return res


def week_range(from_dt: datetime, offset_weeks: int = 0) -> Tuple[date, date]:
    """Неделя: Пн..Пн следующей недели (end exclusive)."""
    today = from_dt.date()
    monday = today - timedelta(days=today.weekday())
    monday = monday + timedelta(weeks=offset_weeks)
    next_monday = monday + timedelta(days=7)
    return monday, next_monday


# ======================
# KEYBOARDS
# ======================
def main_menu_kb(user_id: int) -> ReplyKeyboardMarkup:
    row1 = [BTN_TOMORROW, BTN_AFTER_TOMORROW]
    row2 = [BTN_THIS_WEEK, BTN_NEXT_WEEK]
    row3 = [BTN_NEAREST, BTN_SEARCH]
    rows = [row1, row2, row3]

    # Напоминания пока оставим, но без функционала, чтобы не ломать.
    rows.append([BTN_REMINDERS])

    if is_admin(user_id):
        rows.append([BTN_ADMIN])

    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


def unauthorized_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("📩 Запросить доступ", callback_data=CB_REQ_ACCESS)]
    ])


def nearest_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("7 дней", callback_data=f"{CB_NEAREST_PREFIX}7"),
            InlineKeyboardButton("14 дней", callback_data=f"{CB_NEAREST_PREFIX}14"),
            InlineKeyboardButton("30 дней", callback_data=f"{CB_NEAREST_PREFIX}30"),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data=CB_BACK_MAIN)]
    ])


def admin_panel_kb(state: dict) -> InlineKeyboardMarkup:
    allowed_cnt = len(get_allowed(state))
    pending_cnt = len(get_pending(state))
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"✅ Допущенные: {allowed_cnt}/{MAX_ALLOWED_USERS}", callback_data=CB_ADMIN_ALLOWED)],
        [InlineKeyboardButton(f"📩 Заявки: {pending_cnt}", callback_data=CB_ADMIN_PENDING)],
        [
            InlineKeyboardButton("➕ Добавить по ID", callback_data=CB_ADMIN_ADD),
            InlineKeyboardButton("➖ Удалить по ID", callback_data=CB_ADMIN_REMOVE),
        ],
        [InlineKeyboardButton("⬅️ Назад", callback_data=CB_BACK_MAIN)],
    ])


def back_main_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Назад", callback_data=CB_BACK_MAIN)]])


# ======================
# AUTH / START
# ======================
async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    state = load_state()
    user = update.effective_user
    if not user:
        return
    uid = user.id

    if is_allowed(uid, state):
        await update.message.reply_text(
            "Выбери действие 👇",
            reply_markup=main_menu_kb(uid),
        )
    else:
        await update.message.reply_text(
            "⛔ Доступ закрыт.\n\nНажми кнопку ниже, чтобы отправить заявку админу.",
            reply_markup=unauthorized_kb(),
        )


async def cb_request_access(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    state = load_state()

    user = query.from_user
    uid = user.id

    if is_allowed(uid, state):
        await query.edit_message_text("✅ У тебя уже есть доступ.", reply_markup=back_main_kb())
        return

    pending = get_pending(state)
    if uid in pending:
        await query.edit_message_text("📩 Заявка уже отправлена. Жди подтверждения.", reply_markup=back_main_kb())
        return

    pending.add(uid)
    state["pending_requests"] = list(pending)
    save_state(state)

    # Пинганем админа
    try:
        await context.bot.send_message(
            chat_id=ADMIN_ID,
            text=f"📩 Новая заявка на доступ:\n• {user.full_name}\n• user_id: {uid}",
            reply_markup=admin_panel_kb(state),
        )
    except Exception:
        pass

    await query.edit_message_text("📩 Заявка отправлена админу. Жди подтверждения.", reply_markup=back_main_kb())


# ======================
# BIRTHDAY OUTPUTS
# ======================
async def send_list(update: Update, context: ContextTypes.DEFAULT_TYPE, title: str, items: List[Tuple[Person, date]]) -> None:
    if not update.effective_chat:
        return

    if not items:
        await context.bot.send_message(chat_id=update.effective_chat.id, text=f"{title}\n• Нет")
        return

    from_dt = now_local()
    lines = [title]
    for p, d in items:
        lines.append(format_person_line(p, d, from_dt))

    msg = "\n".join(lines)
    await context.bot.send_message(chat_id=update.effective_chat.id, text=msg)


def ensure_authorized(update: Update) -> Optional[str]:
    """Возвращает None если OK, иначе текст ошибки."""
    state = load_state()
    user = update.effective_user
    if not user:
        return "⛔ Не удалось определить пользователя."
    if not is_allowed(user.id, state):
        return "⛔ Нет доступа. Нажми /start и отправь заявку админу."
    return None


async def handle_menu_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> Optional[int]:
    # Общая проверка доступа
    err = ensure_authorized(update)
    if err:
        await update.message.reply_text(err, reply_markup=unauthorized_kb())
        return ConversationHandler.END

    txt = (update.message.text or "").strip()
    people = load_birthdays()
    from_dt = now_local()

    if txt == BTN_TOMORROW:
        start = (from_dt + timedelta(days=1)).date()
        end = start + timedelta(days=1)
        items = filter_between(people, start, end, from_dt)
        await send_list(update, context, "🎂 Завтра:", items)
        return ConversationHandler.END

    if txt == BTN_AFTER_TOMORROW:
        start = (from_dt + timedelta(days=2)).date()
        end = start + timedelta(days=1)
        items = filter_between(people, start, end, from_dt)
        await send_list(update, context, "🎂 Послезавтра:", items)
        return ConversationHandler.END

    if txt == BTN_THIS_WEEK:
        ws, we = week_range(from_dt, 0)
        items = filter_between(people, ws, we, from_dt)
        await send_list(update, context, f"📅 Эта неделя ({ws.strftime('%d.%m')}–{(we - timedelta(days=1)).strftime('%d.%m')}):", items)
        return ConversationHandler.END

    if txt == BTN_NEXT_WEEK:
        ws, we = week_range(from_dt, 1)
        items = filter_between(people, ws, we, from_dt)
        await send_list(update, context, f"📅 След. неделя ({ws.strftime('%d.%m')}–{(we - timedelta(days=1)).strftime('%d.%m')}):", items)
        return ConversationHandler.END

    if txt == BTN_NEAREST:
        await update.message.reply_text("Выбери период:", reply_markup=nearest_kb())
        return ConversationHandler.END

    if txt == BTN_SEARCH:
        await update.message.reply_text(
            "Введи часть ФИО для поиска.\nПример: `Пащ` (найдет Пащенко / Пащинда)\n\nЧтобы отменить — напиши `отмена`.",
            reply_markup=ReplyKeyboardRemove(),
            parse_mode="Markdown",
        )
        return SEARCH_WAIT_QUERY

    if txt == BTN_REMINDERS:
        await update.message.reply_text("⚙️ Напоминания (следующий шаг сделаем дальше).", reply_markup=main_menu_kb(update.effective_user.id))
        return ConversationHandler.END

    if txt == BTN_ADMIN:
        if not is_admin(update.effective_user.id):
            await update.message.reply_text("⛔ Админка доступна только админу.")
            return ConversationHandler.END
        state = load_state()
        await update.message.reply_text(
            f"🛠 Админка\n\nАдмин = обычный пользователь + админ-функции.\nВыбери действие:",
            reply_markup=admin_panel_kb(state),
        )
        return ConversationHandler.END

    # Неизвестный текст — просто игнор/подсказка
    await update.message.reply_text("Выбери действие кнопками 👇", reply_markup=main_menu_kb(update.effective_user.id))
    return ConversationHandler.END


# ======================
# CALLBACKS: NEAREST + ADMIN
# ======================
async def cb_nearest(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    # доступ
    state = load_state()
    uid = query.from_user.id
    if not is_allowed(uid, state):
        await query.edit_message_text("⛔ Нет доступа. Нажми /start и отправь заявку админу.")
        return

    data = query.data or ""
    try:
        n = int(data.split(":")[1])
    except Exception:
        n = 14

    people = load_birthdays()
    from_dt = now_local()
    start = from_dt.date()
    end = start + timedelta(days=n + 1)  # включим сегодня+N

    items = filter_between(people, start, end, from_dt)

    title = f"⏳ Ближайшие {n} дней:"
    if not items:
        await query.edit_message_text(f"{title}\n• Нет", reply_markup=back_main_kb())
        return

    lines = [title]
    for p, d in items:
        lines.append(format_person_line(p, d, from_dt))
    await query.edit_message_text("\n".join(lines), reply_markup=back_main_kb())


async def cb_back_main(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()

    state = load_state()
    uid = query.from_user.id

    if is_allowed(uid, state):
        await query.edit_message_text("✅ Ок. Открой меню кнопками ниже 👇")
        # Покажем клавиатуру отдельным сообщением (так надежнее)
        await context.bot.send_message(chat_id=query.message.chat_id, text="Выбери действие 👇", reply_markup=main_menu_kb(uid))
    else:
        await query.edit_message_text("⛔ Нет доступа. Нажми /start.", reply_markup=unauthorized_kb())


# ----- ADMIN -----
async def cb_admin_panel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Админка доступна только админу.")
        return

    state = load_state()
    await query.edit_message_text("🛠 Админка:", reply_markup=admin_panel_kb(state))


async def cb_admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Только админ.")
        return

    state = load_state()
    pending = sorted(list(get_pending(state)))
    allowed = get_allowed(state)

    if not pending:
        await query.edit_message_text("📩 Заявок нет.", reply_markup=admin_panel_kb(state))
        return

    kb_rows = []
    for uid in pending[:20]:
        kb_rows.append([
            InlineKeyboardButton(f"✅ Одобрить {uid}", callback_data=f"{CB_ADMIN_APPROVE_PREFIX}{uid}"),
            InlineKeyboardButton(f"❌ Отклонить", callback_data=f"{CB_ADMIN_DENY_PREFIX}{uid}"),
        ])
    kb_rows.append([InlineKeyboardButton("⬅️ Назад", callback_data=CB_ADMIN_PANEL)])
    await query.edit_message_text(
        f"📩 Заявки ({len(pending)}):\n\n"
        f"Лимит допущенных: {len(allowed)}/{MAX_ALLOWED_USERS} (админ не считается).",
        reply_markup=InlineKeyboardMarkup(kb_rows),
    )


async def cb_admin_allowed(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Только админ.")
        return

    state = load_state()
    allowed = sorted(list(get_allowed(state)))

    if not allowed:
        await query.edit_message_text("✅ Допущенных пока нет (кроме админа).", reply_markup=admin_panel_kb(state))
        return

    text = "✅ Допущенные пользователи (user_id):\n" + "\n".join([f"• {uid}" for uid in allowed])
    await query.edit_message_text(text, reply_markup=admin_panel_kb(state))


async def cb_admin_approve_or_deny(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query:
        return
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Только админ.")
        return

    state = load_state()
    allowed = get_allowed(state)
    pending = get_pending(state)
    data = query.data or ""

    is_approve = data.startswith(CB_ADMIN_APPROVE_PREFIX)
    is_deny = data.startswith(CB_ADMIN_DENY_PREFIX)

    try:
        uid = int(data.split(":")[1])
    except Exception:
        await query.edit_message_text("Ошибка: неверный user_id.", reply_markup=admin_panel_kb(state))
        return

    if uid in pending:
        pending.remove(uid)

    if is_approve:
        if uid in allowed:
            pass
        else:
            if len(allowed) >= MAX_ALLOWED_USERS:
                state["pending_requests"] = list(pending)
                save_state(state)
                await query.edit_message_text(
                    f"⛔ Лимит {MAX_ALLOWED_USERS} допущенных уже достигнут.\n"
                    f"Сначала удали кого-то, потом добавь нового.",
                    reply_markup=admin_panel_kb(state),
                )
                return
            allowed.add(uid)
            # уведомим пользователя
            try:
                await context.bot.send_message(
                    chat_id=uid,
                    text="✅ Доступ одобрен! Нажми /start",
                )
            except Exception:
                pass

    if is_deny:
        # уведомим пользователя
        try:
            await context.bot.send_message(
                chat_id=uid,
                text="❌ Доступ отклонен админом.",
            )
        except Exception:
            pass

    state["allowed_users"] = list(allowed)
    state["pending_requests"] = list(pending)
    save_state(state)

    await query.edit_message_text("Готово ✅", reply_markup=admin_panel_kb(state))


async def cb_admin_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Только админ.")
        return ConversationHandler.END

    await query.edit_message_text(
        "➕ Введи user_id (число).\n\nПример: `272545508`\n\nЧтобы отменить — напиши `отмена`.",
        parse_mode="Markdown",
    )
    return ADMIN_WAIT_ADD_ID


async def cb_admin_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    query = update.callback_query
    if not query:
        return ConversationHandler.END
    await query.answer()
    if not is_admin(query.from_user.id):
        await query.edit_message_text("⛔ Только админ.")
        return ConversationHandler.END

    await query.edit_message_text(
        "➖ Введи user_id (число), которого удалить.\n\nЧтобы отменить — напиши `отмена`.",
        parse_mode="Markdown",
    )
    return ADMIN_WAIT_REMOVE_ID


async def admin_add_id_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = (update.message.text or "").strip().lower()
    if text == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_menu_kb(update.effective_user.id))
        return ConversationHandler.END

    m = re.search(r"\d+", text)
    if not m:
        await update.message.reply_text("Не похоже на user_id. Введи число или `отмена`.")
        return ADMIN_WAIT_ADD_ID

    uid = int(m.group(0))
    if uid == ADMIN_ID:
        await update.message.reply_text("Админ и так имеет доступ.")
        return ConversationHandler.END

    state = load_state()
    allowed = get_allowed(state)

    if uid in allowed:
        await update.message.reply_text("Уже допущен ✅", reply_markup=main_menu_kb(update.effective_user.id))
        return ConversationHandler.END

    if len(allowed) >= MAX_ALLOWED_USERS:
        await update.message.reply_text(f"Лимит допущенных {MAX_ALLOWED_USERS} уже достигнут. Сначала удали кого-то.")
        return ConversationHandler.END

    allowed.add(uid)
    pending = get_pending(state)
    if uid in pending:
        pending.remove(uid)

    state["allowed_users"] = list(allowed)
    state["pending_requests"] = list(pending)
    save_state(state)

    try:
        await context.bot.send_message(chat_id=uid, text="✅ Тебе выдали доступ! Нажми /start")
    except Exception:
        pass

    await update.message.reply_text("Добавил ✅", reply_markup=main_menu_kb(update.effective_user.id))
    return ConversationHandler.END


async def admin_remove_id_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not update.effective_user or not is_admin(update.effective_user.id):
        return ConversationHandler.END

    text = (update.message.text or "").strip().lower()
    if text == "отмена":
        await update.message.reply_text("Отменено.", reply_markup=main_menu_kb(update.effective_user.id))
        return ConversationHandler.END

    m = re.search(r"\d+", text)
    if not m:
        await update.message.reply_text("Не похоже на user_id. Введи число или `отмена`.")
        return ADMIN_WAIT_REMOVE_ID

    uid = int(m.group(0))
    if uid == ADMIN_ID:
        await update.message.reply_text("Админа удалить нельзя 🙂")
        return ConversationHandler.END

    state = load_state()
    allowed = get_allowed(state)
    if uid not in allowed:
        await update.message.reply_text("Этого user_id нет в допущенных.", reply_markup=main_menu_kb(update.effective_user.id))
        return ConversationHandler.END

    allowed.remove(uid)
    state["allowed_users"] = list(allowed)
    save_state(state)

    try:
        await context.bot.send_message(chat_id=uid, text="⛔ Твой доступ к боту отключен админом.")
    except Exception:
        pass

    await update.message.reply_text("Удалил ✅", reply_markup=main_menu_kb(update.effective_user.id))
    return ConversationHandler.END


# ======================
# SEARCH
# ======================
def norm(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip().lower()


async def search_query_msg(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    err = ensure_authorized(update)
    if err:
        await update.message.reply_text(err, reply_markup=unauthorized_kb())
        return ConversationHandler.END

    q = (update.message.text or "").strip()
    if not q:
        await update.message.reply_text("Введи текст для поиска или `отмена`.")
        return SEARCH_WAIT_QUERY

    if q.strip().lower() == "отмена":
        await update.message.reply_text("Ок.", reply_markup=main_menu_kb(update.effective_user.id))
        return ConversationHandler.END

    people = load_birthdays()
    nq = norm(q)

    matches: List[Person] = []
    for p in people:
        if nq in norm(p.fio):
            matches.append(p)

    if not matches:
        await update.message.reply_text("Ничего не нашел. Попробуй другой кусок ФИО или `отмена`.")
        return SEARCH_WAIT_QUERY

    matches.sort(key=lambda x: x.fio.lower())

    # покажем максимум 30, чтобы не спамить
    lines = [f"🔎 Найдено: {len(matches)}"]
    for p in matches[:30]:
        lines.append(f"• {p.fio} — {p.day:02d}.{p.month:02d}.{p.year}")

    if len(matches) > 30:
        lines.append(f"…и еще {len(matches) - 30}")

    await update.message.reply_text("\n".join(lines), reply_markup=main_menu_kb(update.effective_user.id))
    return ConversationHandler.END


# ======================
# MAIN
# ======================
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # /start
    app.add_handler(CommandHandler("start", cmd_start))

    # Callbacks
    app.add_handler(CallbackQueryHandler(cb_request_access, pattern=f"^{CB_REQ_ACCESS}$"))
    app.add_handler(CallbackQueryHandler(cb_nearest, pattern=f"^{CB_NEAREST_PREFIX}\\d+$"))
    app.add_handler(CallbackQueryHandler(cb_back_main, pattern=f"^{CB_BACK_MAIN}$"))

    # Admin callbacks
    app.add_handler(CallbackQueryHandler(cb_admin_panel, pattern=f"^{CB_ADMIN_PANEL}$"))
    app.add_handler(CallbackQueryHandler(cb_admin_pending, pattern=f"^{CB_ADMIN_PENDING}$"))
    app.add_handler(CallbackQueryHandler(cb_admin_allowed, pattern=f"^{CB_ADMIN_ALLOWED}$"))
    app.add_handler(CallbackQueryHandler(cb_admin_approve_or_deny, pattern=f"^({CB_ADMIN_APPROVE_PREFIX}|{CB_ADMIN_DENY_PREFIX})\\d+$"))

    # Conversation: Search + Admin add/remove
    conv = ConversationHandler(
        entry_points=[
            MessageHandler(filters.TEXT & ~filters.COMMAND, handle_menu_text),
            CallbackQueryHandler(cb_admin_add, pattern=f"^{CB_ADMIN_ADD}$"),
            CallbackQueryHandler(cb_admin_remove, pattern=f"^{CB_ADMIN_REMOVE}$"),
        ],
        states={
            SEARCH_WAIT_QUERY: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, search_query_msg),
            ],
            ADMIN_WAIT_ADD_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_add_id_msg),
            ],
            ADMIN_WAIT_REMOVE_ID: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, admin_remove_id_msg),
            ],
        },
        fallbacks=[
            CommandHandler("start", cmd_start),
        ],
        allow_reentry=True,
    )
    app.add_handler(conv)

    return app


def main() -> None:
    app = build_app()
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
