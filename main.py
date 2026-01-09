import os
import logging
import asyncio
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


# ======================
# ENV CONFIG
# ======================
BOT_TOKEN = os.getenv("BOT_TOKEN")

# Who is the "style owner" (only this user's messages are used to learn style)
OWNER_ID = int(os.getenv("OWNER_ID", "0"))  # set this in Railway Variables

# Provider switch:
#   deepseek | qwen | openai | custom
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "deepseek").lower()

# Key for provider
LLM_API_KEY = os.getenv("LLM_API_KEY") or os.getenv("OPENAI_API_KEY")  # fallback for your old var

# Model name
LLM_MODEL = os.getenv("LLM_MODEL") or os.getenv("OPENAI_MODEL") or "deepseek-chat"

# Optional: override base url manually
LLM_BASE_URL = os.getenv("LLM_BASE_URL", "").strip()


def resolve_base_url(provider: str, override: str) -> str | None:
    if override:
        return override

    if provider == "deepseek":
        # Official docs say OpenAI-compatible with base_url https://api.deepseek.com (or /v1) 2
        return "https://api.deepseek.com"
    if provider == "qwen":
        # Common OpenAI-compatible endpoint for Qwen via DashScope compatible-mode 3
        return "https://dashscope-intl.aliyuncs.com/compatible-mode/v1"
    if provider == "openai":
        return None  # OpenAI default
    if provider == "custom":
        # must provide LLM_BASE_URL
        return override or None
    return None


BASE_URL = resolve_base_url(LLM_PROVIDER, LLM_BASE_URL)

# ======================
# MEMORY
# ======================
# Per topic/thread memory: key = (chat_id, thread_id)
thread_memory = defaultdict(lambda: deque(maxlen=20))

# Style samples ONLY from OWNER_ID (global, across chats)
owner_style_samples = deque(maxlen=60)

# ======================
# LOGGING
# ======================
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("tg-ai-bot")

# ======================
# LLM CLIENT (OpenAI SDK works for OpenAI-compatible providers)
# ======================
if not LLM_API_KEY:
    log.warning("LLM_API_KEY is missing. Bot will run, but AI replies will fail.")

client = OpenAI(api_key=LLM_API_KEY, base_url=BASE_URL) if BASE_URL else OpenAI(api_key=LLM_API_KEY)


def get_thread_id(update: Update) -> int | None:
    # In Telegram topics (supergroups), messages have message_thread_id
    msg = update.effective_message
    return getattr(msg, "message_thread_id", None)


def thread_key(update: Update):
    chat_id = update.effective_chat.id
    tid = get_thread_id(update) or 0
    return (chat_id, tid)


async def safe_reply(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    chat_id = update.effective_chat.id
    tid = get_thread_id(update)

    # reply in same topic if tid exists
    if tid:
        await context.bot.send_message(chat_id=chat_id, message_thread_id=tid, text=text[:4000])
    else:
        await update.effective_message.reply_text(text[:4000])


def build_style_system_prompt() -> str:
    # If no owner samples yet, fallback to a reasonable default
    if not owner_style_samples:
        return (
            "Ты отвечаешь на русском. Стиль: живо, коротко, по делу, без пафоса. "
            "Если уместно — лёгкий юмор. Не выдумывай факты."
        )

    examples = "\n".join([f"- {t}" for t in list(owner_style_samples)[-25:]])
    return (
        "Ты — помощник, который ПИШЕТ ТОЧНО В СТИЛЕ владельца бота.\n"
        "Правила:\n"
        "1) Копируй манеру: длину фраз, сленг, эмодзи, пунктуацию.\n"
        "2) Не говори, что ты копируешь стиль. Просто пиши так.\n"
        "3) Не перенимай стиль других пользователей, только владельца.\n"
        "4) Отвечай на русском, кратко и по делу.\n\n"
        "Примеры сообщений владельца (для стиля):\n"
        f"{examples}\n"
    )


async def call_llm(messages: list[dict], retries: int = 2) -> str:
    # Small retry to survive 429 / temporary issues
    last_err = None
    for attempt in range(retries + 1):
        try:
            resp = client.chat.completions.create(
                model=LLM_MODEL,
                messages=messages,
                temperature=0.7,
            )
            answer = (resp.choices[0].message.content or "").strip()
            return answer or "Пустой ответ. Попробуй иначе сформулировать."
        except Exception as e:
            last_err = e
            # simple backoff
            if attempt < retries:
                await asyncio.sleep(2 + attempt * 3)
                continue
            raise last_err


# ======================
# HANDLERS
# ======================
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(
        update, context,
        "Я ИИ-бот 🤖\n"
        "Пиши в этой теме — отвечу тут же.\n"
        "Команды: /reset (сбросить контекст темы), /ping"
    )


async def ping(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await safe_reply(update, context, "OK ✅")


async def reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    key = thread_key(update)
    thread_memory[key].clear()
    await safe_reply(update, context, "Контекст этой темы сброшен 🧠")


async def style_reset(update: Update, context: ContextTypes.DEFAULT_TYPE):
    uid = update.effective_user.id
    if OWNER_ID and uid != OWNER_ID:
        await safe_reply(update, context, "Неа 🙂")
        return
    owner_style_samples.clear()
    await safe_reply(update, context, "Стиль-память очищена ✅")


async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.effective_message
    text = (msg.text or "").strip()
    if not text:
        return

    # show typing (in thread if exists)
    tid = get_thread_id(update)
    if tid:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING, message_thread_id=tid)
    else:
        await context.bot.send_chat_action(chat_id=update.effective_chat.id, action=ChatAction.TYPING)

    if not BOT_TOKEN:
        await safe_reply(update, context, "Ошибка: BOT_TOKEN не задан в Railway Variables.")
        return
    if not LLM_API_KEY:
        await safe_reply(update, context, "Ошибка: LLM_API_KEY (или OPENAI_API_KEY) не задан в Railway Variables.")
        return
    if OWNER_ID == 0:
        await safe_reply(update, context, "Ошибка: OWNER_ID не задан. Добавь OWNER_ID в Railway Variables (твой Telegram user_id).")
        return

    uid = update.effective_user.id
    key = thread_key(update)

    # If owner writes anything, store as style sample (only owner's messages)
    if uid == OWNER_ID:
        # фильтр: не сохраняем команды
        if not text.startswith("/"):
            owner_style_samples.append(text)

    system_prompt = build_style_system_prompt()

    messages = [{"role": "system", "content": system_prompt}]
    messages.extend(list(thread_memory[key]))
    messages.append({"role": "user", "content": text})

    try:
        answer = await call_llm(messages, retries=2)
    except Exception as e:
        # If it's a 429-like error, show a clear message
        err_txt = str(e)
        if "429" in err_txt or "rate" in err_txt.lower():
            await safe_reply(update, context, "Лимит запросов 😵‍💫 Подожди 20–60 сек и повтори.")
            return
        log.exception("LLM request failed")
        await safe_reply(update, context, f"Ошибка ИИ: {e}")
        return

    # save per-thread memory (NOT per-user)
    thread_memory[key].append({"role": "user", "content": text})
    thread_memory[key].append({"role": "assistant", "content": answer})

    await safe_reply(update, context, answer)


def main():
    if not BOT_TOKEN:
        raise RuntimeError("BOT_TOKEN env var is missing")

    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("ping", ping))
    app.add_handler(CommandHandler("reset", reset))
    app.add_handler(CommandHandler("style_reset", style_reset))

    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))

    log.info("Bot started. Polling...")
    app.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
