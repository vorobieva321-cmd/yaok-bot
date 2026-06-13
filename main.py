import asyncio
import logging
import os

import anthropic
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    LabeledPrice,
    Update,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    PreCheckoutQueryHandler,
    filters,
)

from db import (
    FREE_MESSAGE_LIMIT,
    clear_topic_history,
    get_all_user_ids,
    get_conversation_history,
    get_or_create_user,
    get_payment_charge,
    get_stats,
    get_users_to_remind,
    increment_message_count,
    init_db,
    mark_reminded,
    save_message,
    save_payment_charge,
    set_premium,
    set_topic,
    toggle_reminders,
)
from prompts import (
    HELP_MESSAGE,
    PAYWALL_MESSAGE,
    REMINDER_MESSAGES,
    SYSTEM_PROMPTS,
    TOPIC_INTRO,
    TOPIC_NAMES,
    WELCOME_MESSAGE,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

TELEGRAM_BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

STARS_PRICE = 500  # Telegram Stars for premium subscription

# Admin user IDs (comma-separated in env var ADMIN_IDS)
ADMIN_IDS = set(
    int(x.strip())
    for x in os.environ.get("ADMIN_IDS", "").split(",")
    if x.strip().isdigit()
)

anthropic_client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)


def build_topic_keyboard():
    buttons = []
    row = []
    for key, label in TOPIC_NAMES.items():
        row.append(InlineKeyboardButton(label, callback_data=f"topic:{key}"))
        if len(row) == 2:
            buttons.append(row)
            row = []
    if row:
        buttons.append(row)
    return InlineKeyboardMarkup(buttons)


def build_change_topic_keyboard():
    return InlineKeyboardMarkup(
        [[InlineKeyboardButton("🔄 Змінити тему", callback_data="change_topic")]]
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    await get_or_create_user(user.id, user.username, user.first_name)
    await update.message.reply_text(
        WELCOME_MESSAGE,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_topic_keyboard(),
    )


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        HELP_MESSAGE,
        parse_mode=ParseMode.MARKDOWN,
    )


async def topic_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "Обери тему для розмови:",
        reply_markup=build_topic_keyboard(),
    )


async def grant_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /grant <user_id> — grants premium to a user."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас немає прав для цієї команди.")
        return
    if not context.args:
        await update.message.reply_text("Використання: /grant <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Невірний user_id.")
        return
    await get_or_create_user(target_id)
    await set_premium(target_id, True)
    await update.message.reply_text(f"✅ Користувач {target_id} отримав преміум доступ.")


async def revoke_premium(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /revoke <user_id> — revokes premium from a user."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас немає прав для цієї команди.")
        return
    if not context.args:
        await update.message.reply_text("Використання: /revoke <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Невірний user_id.")
        return
    await set_premium(target_id, False)
    await update.message.reply_text(f"✅ Преміум доступ користувача {target_id} скасовано.")


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /stats — shows bot usage statistics."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас немає прав для цієї команди.")
        return

    s = await get_stats()

    topic_icons = {
        "anxiety": "😰", "burnout": "😮‍💨", "relationships": "💔",
        "sleep": "😴", "sadness": "😢", "anger": "😤",
    }

    topic_lines = ""
    for topic, count in s["topics"].items():
        icon = topic_icons.get(topic, "•")
        topic_lines += f"  {icon} {TOPIC_NAMES.get(topic, topic)}: {count}\n"
    if not topic_lines:
        topic_lines = "  _немає даних_\n"

    free_pct = round(s["free_users"] / s["total_users"] * 100) if s["total_users"] else 0
    premium_pct = round(s["premium_users"] / s["total_users"] * 100) if s["total_users"] else 0

    text = (
        f"📊 *Статистика бота*\n"
        f"{'─' * 28}\n\n"
        f"👥 *Користувачі*\n"
        f"  Всього: {s['total_users']}\n"
        f"  💙 Преміум: {s['premium_users']} ({premium_pct}%)\n"
        f"  🆓 Безкоштовні: {s['free_users']} ({free_pct}%)\n"
        f"  🆕 Нові сьогодні: {s['new_today']}\n\n"
        f"💬 *Активність*\n"
        f"  Активних сьогодні: {s['active_today']}\n"
        f"  Повідомлень сьогодні: {s['messages_today']}\n"
        f"  Всього повідомлень: {s['total_messages']}\n\n"
        f"🏷 *Популярні теми (всього)*\n"
        f"{topic_lines}"
    )

    await update.message.reply_text(text, parse_mode=ParseMode.MARKDOWN)


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /broadcast <message> — sends a message to all users."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас немає прав для цієї команди.")
        return
    if not context.args:
        await update.message.reply_text(
            "Використання: /broadcast <текст повідомлення>\n\n"
            "Підтримується Markdown-форматування."
        )
        return

    text = " ".join(context.args)
    user_ids = await get_all_user_ids()

    if not user_ids:
        await update.message.reply_text("❌ Немає користувачів для розсилки.")
        return

    status_msg = await update.message.reply_text(
        f"📤 Починаю розсилку для {len(user_ids)} користувачів..."
    )

    sent = 0
    failed = 0

    for uid in user_ids:
        try:
            await context.bot.send_message(
                chat_id=uid,
                text=text,
                parse_mode=ParseMode.MARKDOWN,
            )
            sent += 1
        except Exception as e:
            logger.warning(f"Broadcast failed for user {uid}: {e}")
            failed += 1
        # Respect Telegram rate limits (30 messages/sec)
        await asyncio.sleep(0.05)

    await status_msg.edit_text(
        f"✅ Розсилку завершено!\n\n"
        f"📨 Надіслано: {sent}\n"
        f"❌ Не вдалося: {failed}"
    )


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "change_topic":
        await query.message.reply_text(
            "Обери нову тему:",
            reply_markup=build_topic_keyboard(),
        )
        return

    if query.data.startswith("topic:"):
        topic_key = query.data.split(":")[1]
        user = query.from_user
        await get_or_create_user(user.id, user.username, user.first_name)
        await set_topic(user.id, topic_key)
        await clear_topic_history(user.id, topic_key)

        topic_label = TOPIC_NAMES.get(topic_key, topic_key)
        intro = TOPIC_INTRO.get(topic_key, "Розкажи мені, що тебе турбує? 🤍")

        await query.message.reply_text(
            f"*{topic_label}*\n\n{intro}",
            parse_mode=ParseMode.MARKDOWN,
            reply_markup=build_change_topic_keyboard(),
        )


async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_text = update.message.text

    db_user = await get_or_create_user(user.id, user.username, user.first_name)

    # Check paywall
    if not db_user["is_premium"] and db_user["message_count"] >= FREE_MESSAGE_LIMIT:
        await update.message.reply_text(PAYWALL_MESSAGE, parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title="⭐ Преміум підписка",
            description="Необмежений доступ до психологічної підтримки на всіх темах.",
            payload="premium_subscription",
            currency="XTR",
            prices=[LabeledPrice("Преміум підписка", STARS_PRICE)],
        )
        return

    topic = db_user.get("current_topic")
    if not topic:
        await update.message.reply_text(
            "Будь ласка, спочатку обери тему розмови 👇",
            reply_markup=build_topic_keyboard(),
        )
        return

    # Save user message
    await save_message(user.id, "user", user_text, topic)

    # Increment count (for free users)
    if not db_user["is_premium"]:
        new_count = await increment_message_count(user.id)
        remaining = FREE_MESSAGE_LIMIT - new_count
    else:
        remaining = None

    # Get conversation history
    history = await get_conversation_history(user.id, topic, limit=10)

    # Send typing action
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id, action="typing"
    )

    # Call Claude
    try:
        system_prompt = SYSTEM_PROMPTS.get(topic, SYSTEM_PROMPTS["anxiety"])
        response = anthropic_client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=8192,
            system=system_prompt,
            messages=history,
        )
        assistant_text = response.content[0].text
    except Exception as e:
        logger.error(f"Anthropic error: {e}")
        await update.message.reply_text(
            "Вибач, сталась помилка. Спробуй ще раз 🙏"
        )
        return

    # Save assistant reply
    await save_message(user.id, "assistant", assistant_text, topic)

    # Build reply with remaining message hint if near limit
    reply = assistant_text
    if remaining is not None and remaining <= 2 and remaining > 0:
        reply += f"\n\n_💬 Залишилось {remaining} безкоштовних повідомлень._"
    elif remaining is not None and remaining <= 0:
        reply += "\n\n_💬 Це було твоє останнє безкоштовне повідомлення._"

    await update.message.reply_text(
        reply,
        parse_mode=ParseMode.MARKDOWN,
        reply_markup=build_change_topic_keyboard(),
    )

    # Show paywall after last free message
    if remaining is not None and remaining <= 0:
        await update.message.reply_text(PAYWALL_MESSAGE, parse_mode=ParseMode.MARKDOWN)
        await context.bot.send_invoice(
            chat_id=update.effective_chat.id,
            title="⭐ Преміум підписка",
            description="Необмежений доступ до психологічної підтримки на всіх темах.",
            payload="premium_subscription",
            currency="XTR",
            prices=[LabeledPrice("Преміум підписка", STARS_PRICE)],
        )


async def remindme_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """User command: /remindme — toggle reminder notifications on or off."""
    user = update.effective_user
    await get_or_create_user(user.id, user.username, user.first_name)
    enabled = await toggle_reminders(user.id)
    if enabled:
        await update.message.reply_text(
            "🔔 Нагадування *увімкнено*.\n\n"
            "Я напишу тобі, якщо ти не з'являтимешся кілька днів 🤍\n\n"
            "_Щоб вимкнути — надішли /remindme ще раз._",
            parse_mode=ParseMode.MARKDOWN,
        )
    else:
        await update.message.reply_text(
            "🔕 Нагадування *вимкнено*.\n\n"
            "Я не буду турбувати тебе — повертайся, коли сам(а) захочеш 🌿\n\n"
            "_Щоб увімкнути знову — надішли /remindme._",
            parse_mode=ParseMode.MARKDOWN,
        )


async def send_reminders(context: ContextTypes.DEFAULT_TYPE):
    """Scheduled job: sends gentle reminders to users inactive for 3+ days."""
    import random
    users = await get_users_to_remind(inactive_days=3, min_remind_gap_days=7)
    if not users:
        logger.info("Reminders: no inactive users to notify.")
        return

    logger.info(f"Reminders: sending to {len(users)} inactive users.")
    sent = 0
    failed = 0

    for user in users:
        message = random.choice(REMINDER_MESSAGES)
        # If user had a topic, add a topic-specific nudge
        topic = user.get("current_topic")
        if topic and topic in TOPIC_NAMES:
            message += f"\n\n↩️ Остання тема: {TOPIC_NAMES[topic]}"

        try:
            await context.bot.send_message(
                chat_id=user["user_id"],
                text=message,
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("💬 Продовжити розмову", callback_data="change_topic")]]
                ),
            )
            await mark_reminded(user["user_id"])
            sent += 1
        except Exception as e:
            logger.warning(f"Reminder failed for user {user['user_id']}: {e}")
            failed += 1
        await asyncio.sleep(0.05)

    logger.info(f"Reminders done: sent={sent}, failed={failed}")


async def refund_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin command: /refund <user_id> — refunds Stars and revokes premium."""
    if update.effective_user.id not in ADMIN_IDS:
        await update.message.reply_text("❌ У вас немає прав для цієї команди.")
        return
    if not context.args:
        await update.message.reply_text("Використання: /refund <user_id>")
        return
    try:
        target_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("❌ Невірний user_id.")
        return

    charge_id = await get_payment_charge(target_id)
    if not charge_id:
        await update.message.reply_text(
            f"❌ Для користувача {target_id} не знайдено запису про оплату."
        )
        return

    try:
        await context.bot.refund_star_payment(
            user_id=target_id,
            telegram_payment_charge_id=charge_id,
        )
        await set_premium(target_id, False)
        await save_payment_charge(target_id, None)
        logger.info(f"Refunded Stars for user {target_id}, charge_id={charge_id}")
        await update.message.reply_text(
            f"✅ Зірки повернено користувачу {target_id}.\n"
            f"Преміум доступ скасовано."
        )
    except Exception as e:
        logger.error(f"Refund failed for user {target_id}: {e}")
        await update.message.reply_text(f"❌ Помилка при поверненні: {e}")


async def pre_checkout_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Approve all Stars payment pre-checkout queries immediately."""
    await update.pre_checkout_query.answer(ok=True)


async def successful_payment_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Grant premium access after a successful Stars payment."""
    user = update.effective_user
    charge_id = update.message.successful_payment.telegram_payment_charge_id
    await get_or_create_user(user.id, user.username, user.first_name)
    await set_premium(user.id, True)
    await save_payment_charge(user.id, charge_id)
    logger.info(f"User {user.id} purchased premium via Stars. charge_id={charge_id}")
    await update.message.reply_text(
        "🎉 Оплата успішна! Тепер у тебе необмежений доступ 💙\n\n"
        "Продовжуй розмову — я поруч.",
        reply_markup=build_change_topic_keyboard(),
    )


async def post_init(application: Application):
    await init_db()
    logger.info("Database initialized.")
    # Schedule daily reminder job — runs every 24 hours, first check after 1 hour
    application.job_queue.run_repeating(
        send_reminders,
        interval=86400,   # 24 hours in seconds
        first=3600,       # first run 1 hour after startup
        name="daily_reminders",
    )
    logger.info("Reminder job scheduled (every 24h, first in 1h).")


def main():
    application = (
        Application.builder()
        .token(TELEGRAM_BOT_TOKEN)
        .post_init(post_init)
        .build()
    )

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_command))
    application.add_handler(CommandHandler("topic", topic_command))
    application.add_handler(CommandHandler("grant", grant_premium))
    application.add_handler(CommandHandler("revoke", revoke_premium))
    application.add_handler(CommandHandler("broadcast", broadcast))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("remindme", remindme_command))
    application.add_handler(CommandHandler("refund", refund_command))
    application.add_handler(PreCheckoutQueryHandler(pre_checkout_callback))
    application.add_handler(MessageHandler(filters.SUCCESSFUL_PAYMENT, successful_payment_callback))
    application.add_handler(CallbackQueryHandler(button_callback))
    application.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message)
    )

    logger.info("Bot is starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
