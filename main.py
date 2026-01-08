async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (update.message.text or "").strip()
    if not text:
        return

    thread_id = update.message.message_thread_id

    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action="typing",
        message_thread_id=thread_id
    )

    if not BOT_TOKEN:
        await update.message.reply_text(
            "Ошибка: BOT_TOKEN не задан в Railway Variables.",
            message_thread_id=thread_id
        )
        return

    if not OPENAI_API_KEY:
        await update.message.reply_text(
            "Ошибка: OPENAI_API_KEY не задан в Railway Variables.",
            message_thread_id=thread_id
        )
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
    except Exception as e:
        await update.message.reply_text(
            f"Ошибка запроса к ИИ: {e}",
            message_thread_id=thread_id
        )
        return

    memory[uid].append({"role": "user", "content": text})
    memory[uid].append({"role": "assistant", "content": answer})

    await update.message.reply_text(
        answer[:4000],
        message_thread_id=thread_id
    )
