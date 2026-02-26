import os
from datetime import datetime, timedelta
from telegram import Update
from telegram.ext import ApplicationBuilder, CommandHandler, ContextTypes

TOKEN = os.environ.get("BOT_TOKEN")

birthdays = {
    "Иван": "03-02",
    "Олег": "02-28"
}

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("🎂 Бот дней рождения работает! Команда: /tomorrow")

async def tomorrow(update: Update, context: ContextTypes.DEFAULT_TYPE):
    tomorrow_date = (datetime.now() + timedelta(days=1)).strftime("%m-%d")
    result = [name for name, d in birthdays.items() if d == tomorrow_date]

    if result:
        await update.message.reply_text("Завтра ДР у: " + ", ".join(result))
    else:
        await update.message.reply_text("Завтра дней рождения нет")

def main():
    if not TOKEN:
        raise RuntimeError("Переменная окружения BOT_TOKEN не задана")

    app = ApplicationBuilder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("tomorrow", tomorrow))
    app.run_polling()

if __name__ == "__main__":
    main()
