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

# ================= CONFIG =================
BOT_TOKEN = os.getenv("BOT_TOKEN")
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
MODEL = os.getenv("OPENAI_MODEL", "gpt-4o-mini")

# память диалога: (user_id, thread_id)
dialog_memory = defaultdict(lambda: deque(maxlen=10))

# память стиля: только ТВОИ сообщения
# user_id -> последние сообщения пользователя как примеры стиля
style_memory = defaultdict(lambda: deque(maxlen=20))

# ================= LOGGING =================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-ai-bot")

# ================= OPENAI =================
client = OpenAI(api_key=OPENAI_API_KEY)


# ================= HELPERS =================
def thread_id(update: Update):
    return update.message.message_thread_id


def dialog_key(update: Update):
    return (update.effective_user.id, thread_id(update))


# ================= COMMANDS =================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Я ИИ-бот 🤖\n"
        "Я подстраиваюсь под твой стиль общения.\n"
        "Чем больше ты пишешь — тем точнее стиль.\n\n"
        "Команды:\n"
        "/reset — сбросить контекст темы\n"
        "/style_reset — сбросить стиль",
        message_thread_id=thread_id(update),
    )


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    dialog_memory[dialog_key(update)].clear()
    await update.message.reply_text(
        "Контекст темы сброшен 🧠",
        message_thread_id=thread_id(update),
    )


async def style_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    style_memory[update.effective_user.id].clear()
    await update.message.reply_text(
        "Стиль сброшен. Начинаю учиться заново ✍️",
        message_thread_id=thread_id(update),
    )


# ================= TEXT HANDLER =================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    tid = thread_id(update)
    uid = update.effective_user.id

    # typing
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing",
        message_thread_id=tid,
    )

    if not BOT_TOKEN or not OPENAI_API_KEY:
        await update.message.reply_text(
            "Ошибка конфигурации. Проверь переменные Railway.",
            message_thread_id=tid,
        )
        return

    # === сохраняем стиль (ТОЛЬКО твои сообщения) ===
    style_memory[uid].append(text)

    # === system prompt из твоего стиля ===
    style_examples = "\n".join(f"- {m}" for m in style_memory[uid])

    system_prompt = (
        "Ты — ассистент, который отвечает в стиле пользователя.\n"
        "Копируй манеру речи, длину фраз, лексику, пунктуацию и тон.\n"
        "Не объясняй, что ты копируешь стиль.\n\n"
        "Примеры сообщений пользователя:\n"
        f"{style_examples}"
    )

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(dialog_memory[dialog_key(update)])
    messages.append({"role": "user", "content": text})

    try:
        resp = client.chat.completions.create(
            model=MODEL,
            messages=messages,
            temperature=0.8,
        )
        answer = (resp.choices[0].message.content or "").strip()
    except Exception as e:
        log.exception("OpenAI error")
        await update.message.reply_text(
            f"Ошибка ИИ: {e}",
            message_thread_id=tid,
        )
        return

    # сохраняем диалог
    dialog_memory[dialog_key(update)].append({"role": "user", "content": text})
    dialog_memory[dialog_key(update)].append({"role": "assistant", "content": answer})

    await update.message.reply_text(
        answer[:4000],
        message_thread_id=tid,
    )


# ================= MAIN =================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is missing")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("style_reset", style_reset))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot started")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
