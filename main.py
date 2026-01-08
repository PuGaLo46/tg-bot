import os
import logging
import time
from collections import defaultdict, deque

from telegram import Update
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from openai import OpenAI

# ================== CONFIG ==================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# память: пользователь + тема → последние сообщения
memory = defaultdict(lambda: deque(maxlen=8))

# анти-спам / анти-429
last_request_time = defaultdict(float)
MIN_DELAY = 8  # секунд между запросами к OpenAI от одного юзера

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-ai-bot")

# ================== OPENAI ===================
client = OpenAI(api_key=OPENAI_API_KEY)


# ================== COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я ИИ-бот 🤖\n"
        "Отвечаю в той же теме.\n"
        "Пиши нормально — отвечу.\n\n"
        "Команды:\n"
        "/reset — сброс контекста\n"
        "/ping — проверка",
        message_thread_id=update.message.message_thread_id
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "OK ✅",
        message_thread_id=update.message.message_thread_id
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    thread_id = update.message.message_thread_id or 0
    memory[(uid, thread_id)].clear()
    await update.message.reply_text(
        "Контекст очищен 🧠",
        message_thread_id=thread_id
    )


# ================== MAIN HANDLER ==================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.message
    if not message or not message.text:
        return

    text = message.text.strip()
    if len(text) < 3:
        return  # режем мусор и экономим лимиты

    uid = update.effective_user.id
    thread_id = message.message_thread_id or 0
    key = (uid, thread_id)

    # анти-429
    now = time.time()
    if now - last_request_time[uid] < MIN_DELAY:
        await message.reply_text(
            "⏳ Подожди пару секунд, думаю…",
            message_thread_id=thread_id
        )
        return
    last_request_time[uid] = now

    # typing
    await context.bot.send_chat_action(
        chat_id=message.chat_id,
        action=ChatAction.TYPING
    )

    # системный промпт — копируем ТВОЙ стиль
    system_prompt = (
        "Ты — телеграм-бот, который копирует стиль автора.\n"
        "Пиши коротко, по-человечески, без пафоса.\n"
        "Разговорный стиль, как в чате.\n"
        "Если вопрос простой — ответ простой.\n"
        "Язык: русский."
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(list(memory[key]))
    messages.append({"role": "user", "content": text})

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.6,
        )
        answer = resp.choices[0].message.content.strip()
        if not answer:
            answer = "Хм… попробуй переформулировать."
    except Exception as e:
        log.exception("OpenAI error")
        await message.reply_text(
            f"⚠️ Ошибка ИИ: {e}",
            message_thread_id=thread_id
        )
        return

    # сохраняем контекст
    memory[key].append({"role": "user", "content": text})
    memory[key].append({"role": "assistant", "content": answer})

    await message.reply_text(
        answer[:4000],
        message_thread_id=thread_id
    )


# ================== APP ==================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN is missing")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
