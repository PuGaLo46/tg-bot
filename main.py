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

# LLM (OpenAI-compatible: OpenAI / DeepSeek / Qwen compatible endpoints)
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "gpt-4o-mini"
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip() or None

OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # твой user_id (число), нужен для админ-команд

# style file in repo
STYLE_FILE_PATH = os.getenv("STYLE_FILE_PATH", "style.txt")

# Память по темам: (chat_id, thread_id) -> deque
thread_memory = defaultdict(lambda: deque(maxlen=16))

# анти-спам по одному чату (чтобы не ловить лимиты)
last_request_ts = defaultdict(float)
MIN_DELAY_SEC = float(os.getenv("MIN_DELAY_SEC", "3.0"))

# ================== LOGGING ==================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-ai-bot")

# ================== LLM CLIENT ==================
client = OpenAI(api_key=LLM_API_KEY, base_url=LLM_BASE_URL) if LLM_BASE_URL else OpenAI(api_key=LLM_API_KEY)

# ================== STYLE LOADING ==================
STYLE_TEXT_CACHE = ""
STYLE_MTIME_CACHE = 0.0


def load_style_text(force: bool = False) -> str:
    """Loads style.txt from disk. Cached + reload on file mtime change."""
    global STYLE_TEXT_CACHE, STYLE_MTIME_CACHE

    try:
        mtime = os.path.getmtime(STYLE_FILE_PATH)
        if (not force) and STYLE_TEXT_CACHE and mtime == STYLE_MTIME_CACHE:
            return STYLE_TEXT_CACHE

        with open(STYLE_FILE_PATH, "r", encoding="utf-8") as f:
            txt = f.read().strip()

        STYLE_TEXT_CACHE = txt
        STYLE_MTIME_CACHE = mtime
        return txt

    except FileNotFoundError:
        return ""
    except Exception as e:
        log.exception("Failed to load style file: %s", e)
        return ""


def build_system_prompt(style_text: str) -> str:
    base_rules = (
        "Ты отвечаешь на русском и пишешь в стиле владельца бота.\n"
        "Правила:\n"
        "— копируй манеру речи, длину фраз, сленг/мат (если он в примерах), пунктуацию, эмодзи\n"
        "— не объясняй, что копируешь стиль; просто отвечай так\n"
        "— не становись официальным, если в примерах не так\n"
        "— если не уверен — честно скажи, что не уверен\n"
        "— отвечай по делу, без воды\n"
    )

    if not style_text:
        # fallback, если style.txt пустой/нет
        return base_rules + "\nПримеры стиля не заданы. Пиши просто, разговорно и кратко.\n"

    # чтобы не раздувать промпт, ограничим размер
    # (если style.txt гигантский — оставим хвост)
    max_chars = 6000
    if len(style_text) > max_chars:
        style_text = style_text[-max_chars:]

    return base_rules + "\nПримеры сообщений владельца (это эталон стиля):\n" + style_text


# ================== HELPERS ==================
def thread_id(update: Update) -> int | None:
    msg = update.effective_message
    return getattr(msg, "message_thread_id", None)


def key_for_thread(update: Update):
    return (update.effective_chat.id, thread_id(update) or 0)


async def reply_in_same_topic(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    tid = thread_id(update)
    chat_id = update.effective_chat.id
    if tid:
        await context.bot.send_message(chat_id=chat_id, message_thread_id=tid, text=text[:4000])
    else:
        await update.effective_message.reply_text(text[:4000])


def is_owner(update: Update) -> bool:
    return OWNER_ID != 0 and update.effective_user and update.effective_user.id == OWNER_ID


# ================== COMMANDS ==================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_in_same_topic(
        update, context,
        "Я ИИ-бот 🤖\n"
        "Стиль беру из файла style.txt (только стиль владельца, не учусь у других).\n"
        "Команды: /reset, /ping\n"
        "Админ: /style_reload"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await reply_in_same_topic(update, context, "OK ✅")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    thread_memory[key_for_thread(update)].clear()
    await reply_in_same_topic(update, context, "Контекст темы сброшен 🧠")


async def style_reload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_owner(update):
        await reply_in_same_topic(update, context, "Неа 🙂")
        return
    txt = load_style_text(force=True)
    if not txt:
        await reply_in_same_topic(update, context, "style.txt не найден или пустой.")
        return
    await reply_in_same_topic(update, context, f"style.txt перезагружен ✅ (символов: {len(txt)})")


# ================== MESSAGE HANDLER ==================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "").strip()
    if not text:
        return

    # мелкий мусор не шлём в LLM
    if len(text) < 3:
        return

    if not BOT_TOKEN:
        await reply_in_same_topic(update, context, "Ошибка: BOT_TOKEN не задан в Railway Variables.")
        return
    if not LLM_API_KEY:
        await reply_in_same_topic(update, context, "Ошибка: LLM_API_KEY не задан в Railway Variables.")
        return

    tid = thread_id(update)
    k = key_for_thread(update)

    # анти-спам на чат: не чаще MIN_DELAY_SEC
    now = time.time()
    if now - last_request_ts[k] < MIN_DELAY_SEC:
        await reply_in_same_topic(update, context, "⏳ Секунду…")
        return
    last_request_ts[k] = now

    # typing
    if tid:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING, message_thread_id=tid)
    else:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    style_text = load_style_text()
    system_prompt = build_system_prompt(style_text)

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(list(thread_memory[k]))
    messages.append({"role": "user", "content": text})

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.7,
        )
        answer = (resp.choices[0].message.content or "").strip()
        if not answer:
            answer = "Пустой ответ. Попробуй иначе сформулировать."
    except Exception as e:
        s = str(e)
        if "402" in s and "Insufficient" in s:
            await reply_in_same_topic(update, context, "Баланс провайдера закончился (402). Пополни баланс или смени LLM.")
            return
        if "429" in s or "rate" in s.lower():
            await reply_in_same_topic(update, context, "Лимит запросов 😵‍💫 Подожди 20–60 сек и повтори.")
            return
        log.exception("LLM error")
        await reply_in_same_topic(update, context, f"Ошибка ИИ: {e}")
        return

    # сохраняем контекст темы
    thread_memory[k].append({"role": "user", "content": text})
    thread_memory[k].append({"role": "assistant", "content": answer})

    await reply_in_same_topic(update, context, answer)


# ================== MAIN ==================
def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is missing")

    # preload style once at boot (not required)
    _ = load_style_text()

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("style_reload", style_reload))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
