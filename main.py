import os
import logging
from collections import defaultdict, deque

from telegram import Update
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# --- config ---
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# simple per-user memory (last 12 messages: user+assistant)
memory = defaultdict(lambda: deque(maxlen=12))

# --- logging ---
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-ai-bot")

# --- openai client ---
client = OpenAI(api_key=OPENAI_API_KEY)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я ИИ-бот 🤖\n"
        "Пиши сообщение — отвечу.\n"
        "Команды: /reset, /ping"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("OK ✅")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    memory[uid].clear()
    await update.message.reply_text("Контекст сброшен 🧠")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    # show typing
    await context.bot.send_chat_action(chat_id=update.effective_chat.id, action="typing")

    # hard checks with explicit user-facing errors (no silent failures)
    if not BOT_TOKEN:
        await update.message.reply_text("Ошибка: BOT_TOKEN не задан в Railway Variables.")
        return
    if not OPENAI_API_KEY:
        await update.message.reply_text("Ошибка: OPENAI_API_KEY не задан в Railway Variables.")
        return

    uid = update.effective_user.id

    messages = [{"role": "system", "content": "Отвечай кратко и по делу, на русском."}]
    messages.extend(list(memory[uid]))
    messages.append({"role": "user", "content": text})

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.7,
        )
        answer = (resp.choices[0].message.content or "").strip()
        if not answer:
            answer = "Пустой ответ от модели. Попробуй переформулировать."
    except Exception as e:
        log.exception("OpenAI request failed")
        await update.message.reply_text(f"Ошибка запроса к ИИ: {e}")
        return

    # save memory
    memory[uid].append({"role": "user", "content": text})
    memory[uid].append({"role": "assistant", "content": answer})

    # Telegram message limit safety
    await update.message.reply_text(answer[:4000])


def main():
    if not BOT_TOKEN:
        # Fail fast in logs (Railway), but user can't see this — that's ok.
        raise RuntimeError("BOT_TOKEN env var is missing")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
