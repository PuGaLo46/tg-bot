import os
from telegram import Update
from telegram.ext import ApplicationBuilder, MessageHandler, ContextTypes, filters

BOT_TOKEN = 8430668114:AAGnP4RGqC1Q3d5lsyFth8ubUY4lI6lHVrg("BOT_TOKEN")

async def handle(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Ку-ку Ёпта🤙")

def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is missing")
    app = ApplicationBuilder().token(BOT_TOKEN).build()
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle))
    app.run_polling()

if __name__ == "__main__":
    main()
