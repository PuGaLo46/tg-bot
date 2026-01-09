import os
import logging
from collections import defaultdict, deque
from typing import Deque, Dict, Tuple, Optional

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

# =========================
# CONFIG (env)
# =========================
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()

# Unified LLM vars (works with OpenAI / DeepSeek / Qwen OpenAI-compatible gateways)
LLM_API_KEY = os.getenv("LLM_API_KEY", os.getenv("OPENAI_API_KEY", "")).strip()
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()  # e.g. https://api.openai.com/v1 OR provider gateway base
LLM_MODEL = os.getenv("LLM_MODEL", os.getenv("OPENAI_MODEL", "gpt-4o-mini")).strip()

# Optional: restrict bot to only you (recommended)
# Put your numeric Telegram user id in Railway variable OWNER_ID
OWNER_ID = os.getenv("OWNER_ID", "").strip()

# Style file path in repo root
STYLE_PATH = os.getenv("STYLE_PATH", "style.txt").strip()

# Memory: keep last N messages (user+assistant) per (chat_id, thread_id, user_id)
MEMORY_MAXLEN = int(os.getenv("MEMORY_MAXLEN", "18"))

# =========================
# LOGGING
# =========================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-ai-bot")

# =========================
# STYLE LOADING
# =========================
_cached_style: str = ""


def load_style_text() -> str:
    global _cached_style
    try:
        with open(STYLE_PATH, "r", encoding="utf-8") as f:
            txt = f.read().strip()
            _cached_style = txt
            return txt
    except FileNotFoundError:
        _cached_style = ""
        return ""
    except Exception:
        log.exception("Failed to read style.txt")
        return _cached_style or ""


def build_system_prompt(style_text: str) -> str:
    # Важно: это жёсткая инструкция. Модель не должна "обсуждать стиль".
    base = (
        "Ты — Арт (Артём). Ты отвечаешь как реальный Арт, его манерой речи.\n"
        "Ты НЕ ассистент, НЕ ИИ, НЕ бот. Никогда не говори, что ты ИИ/бот/модель.\n"
        "Отвечай естественно, как Арт в переписке.\n\n"
        "ЖЁСТКИЕ ПРАВИЛА:\n"
        "- Копируй стиль Арта из STYLE ниже.\n"
        "- Никаких вопросов про стиль (типа: 'какой стиль ты хочешь?').\n"
        "- Не объясняй правила, не комментируй их.\n"
        "- Не придумывай факты о себе. Если не знаешь — ответь коротко по-людски.\n"
        "- Отвечай кратко, по делу, как в чате.\n"
        "- Используй лексику/фразы/подачу из STYLE.\n\n"
        "STYLE (примеры речи Арта):\n"
    )

    if style_text:
        return base + style_text
    else:
        # Если файла нет — хоть какая-то страховка
        return base + "(STYLE пуст. Пиши просто кратко и по-людски.)"


# Load style on startup
load_style_text()

# =========================
# OPENAI-COMPAT CLIENT
# =========================
client_kwargs = {"api_key": LLM_API_KEY}
if LLM_BASE_URL:
    client_kwargs["base_url"] = LLM_BASE_URL

client = OpenAI(**client_kwargs)

# =========================
# MEMORY STORE
# =========================
# key: (chat_id, thread_id, user_id) -> deque of messages
MemoryKey = Tuple[int, int, int]
memory: Dict[MemoryKey, Deque[dict]] = defaultdict(lambda: deque(maxlen=MEMORY_MAXLEN))


def parse_owner_id() -> Optional[int]:
    if not OWNER_ID:
        return None
    try:
        return int(OWNER_ID)
    except ValueError:
        return None


OWNER_ID_INT = parse_owner_id()


def get_thread_id(update: Update) -> int:
    # For forum topics: message_thread_id exists
    # If no topic -> use 0 as "main thread"
    mtid = getattr(update.message, "message_thread_id", None)
    return int(mtid) if mtid is not None else 0


async def safe_reply(update: Update, text: str):
    """Reply in the same topic/thread if it exists."""
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)
    # PTB supports message_thread_id in send_message
    if thread_id != 0:
        await update.get_bot().send_message(chat_id=chat_id, text=text[:4000], message_thread_id=thread_id)
    else:
        await update.message.reply_text(text[:4000])


def is_allowed_user(update: Update) -> bool:
    # If OWNER_ID is set -> only allow that user
    if OWNER_ID_INT is None:
        return True
    uid = update.effective_user.id if update.effective_user else None
    return uid == OWNER_ID_INT


# =========================
# COMMANDS
# =========================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_user(update):
        return
    await safe_reply(update, "Я на месте. Пиши.")


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_user(update):
        return
    await safe_reply(update, "OK ✅")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_user(update):
        return
    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)
    key = (chat_id, thread_id, uid)
    memory[key].clear()
    await safe_reply(update, "Сбросил.")


async def reload_style(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_allowed_user(update):
        return
    txt = load_style_text()
    if txt:
        await safe_reply(update, "Стиль обновил.")
    else:
        await safe_reply(update, "style.txt пустой или не найден.")


# =========================
# MAIN HANDLER
# =========================
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message:
        return
    if not is_allowed_user(update):
        return

    text = (update.message.text or "").strip()
    if not text:
        return

    # typing
    try:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)
    except Exception:
        pass

    # env checks (user-facing, without silent fail)
    if not BOT_TOKEN:
        await safe_reply(update, "Ошибка: BOT_TOKEN не задан.")
        return
    if not LLM_API_KEY:
        await safe_reply(update, "Ошибка: LLM_API_KEY (или OPENAI_API_KEY) не задан.")
        return
    if not LLM_MODEL:
        await safe_reply(update, "Ошибка: LLM_MODEL не задан.")
        return

    uid = update.effective_user.id
    chat_id = update.effective_chat.id
    thread_id = get_thread_id(update)
    key = (chat_id, thread_id, uid)

    style_text = _cached_style or load_style_text()
    system_prompt = build_system_prompt(style_text)

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(list(memory[key]))
    messages.append({"role": "user", "content": text})

    try:
        resp = client.chat.completions.create(
            model=LLM_MODEL,
            messages=messages,
            temperature=0.7,
        )
        answer = (resp.choices[0].message.content or "").strip()
        if not answer:
            answer = "Пусто. Напиши иначе."
    except Exception as e:
        # common cases: 402 balance, 429 rate limit, etc.
        msg = str(e)
        if "Insufficient Balance" in msg or "402" in msg:
            await safe_reply(update, "Бабки на апи кончились (402). Пополни баланс/проверь ключ.")
            return
        if "429" in msg or "Rate limit" in msg:
            await safe_reply(update, "Слишком часто. Подожди чуть-чуть и повтори.")
            return

        log.exception("LLM request failed")
        await safe_reply(update, f"Ошибка ИИ: {e}")
        return

    # save memory (per topic)
    memory[key].append({"role": "user", "content": text})
    memory[key].append({"role": "assistant", "content": answer})

    await safe_reply(update, answer)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is missing")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("reload_style", reload_style))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
