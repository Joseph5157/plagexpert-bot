"""
PlagExpert Telegram Bot
Features: Phone auth, Order status, File updates, Notifications
"""

import os
import logging
import httpx
from telegram import (
    Update, KeyboardButton, ReplyKeyboardMarkup,
    ReplyKeyboardRemove, InlineKeyboardButton, InlineKeyboardMarkup
)
from telegram.ext import (
    Application, CommandHandler, MessageHandler,
    CallbackQueryHandler, ConversationHandler, filters, ContextTypes
)

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# ── Config (from environment variables) ───────────────────────────────────────
BOT_TOKEN      = os.environ["BOT_TOKEN"]           # From BotFather
LARAVEL_API    = os.environ["LARAVEL_API_URL"]      # e.g. https://plagexpert.in/api
API_SECRET     = os.environ["API_SECRET_KEY"]       # Shared secret for your Laravel API
ADMIN_CHAT_IDS = os.environ.get("ADMIN_CHAT_IDS", "").split(",")  # Comma-separated admin IDs

# ── Conversation states ────────────────────────────────────────────────────────
WAITING_PHONE = 1

# ── Helpers ────────────────────────────────────────────────────────────────────

async def call_api(endpoint: str, params: dict = None, method="GET") -> dict | None:
    """Call Laravel API with shared secret header."""
    headers = {"X-Bot-Secret": API_SECRET, "Accept": "application/json"}
    url = f"{LARAVEL_API}/{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            if method == "GET":
                r = await client.get(url, params=params, headers=headers)
            else:
                r = await client.post(url, json=params, headers=headers)
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.error(f"API error [{endpoint}]: {e}")
        return None


def orders_keyboard(orders: list) -> InlineKeyboardMarkup:
    """Build inline keyboard from orders list."""
    buttons = []
    for o in orders:
        label = f"#{o['id']} — {o['service']} — {o['status'].upper()}"
        buttons.append([InlineKeyboardButton(label, callback_data=f"order_{o['id']}")])
    buttons.append([InlineKeyboardButton("🔄 Refresh", callback_data="refresh_orders")])
    return InlineKeyboardMarkup(buttons)


STATUS_EMOJI = {
    "pending":    "⏳",
    "processing": "🔄",
    "completed":  "✅",
    "cancelled":  "❌",
    "refunded":   "💸",
}

# ── /start ─────────────────────────────────────────────────────────────────────

async def start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Entry point — ask for phone number."""
    user = update.effective_user

    # Check if already registered
    data = await call_api("bot/user", {"telegram_id": str(user.id)})
    if data and data.get("found"):
        await update.message.reply_text(
            f"👋 Welcome back, *{data['name']}*!\n\nUse the menu below 👇",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
        return ConversationHandler.END

    # New user — request phone
    phone_btn = KeyboardButton("📱 Share my phone number", request_contact=True)
    markup = ReplyKeyboardMarkup([[phone_btn]], resize_keyboard=True, one_time_keyboard=True)

    await update.message.reply_text(
        "👋 Welcome to *PlagExpert* support bot!\n\n"
        "I can help you:\n"
        "📄 Check your order status\n"
        "📥 Get your report files\n"
        "🔔 Receive instant updates\n\n"
        "Please share your phone number to get started 👇",
        parse_mode="Markdown",
        reply_markup=markup
    )
    return WAITING_PHONE


async def receive_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    """Receive phone number, verify against Laravel DB."""
    contact = update.message.contact
    phone   = contact.phone_number.replace("+", "").replace(" ", "")
    user    = update.effective_user

    await update.message.reply_text("🔍 Verifying your number...", reply_markup=ReplyKeyboardRemove())

    data = await call_api("bot/register", {
        "phone":       phone,
        "telegram_id": str(user.id),
        "name":        user.full_name,
        "username":    user.username or ""
    }, method="POST")

    if not data:
        await update.message.reply_text(
            "⚠️ Our server is temporarily unavailable. Please try again in a moment."
        )
        return ConversationHandler.END

    if data.get("found"):
        await update.message.reply_text(
            f"✅ Verified! Hello, *{data['name']}* 🎉\n\n"
            f"Your account is now linked to this bot.\n"
            f"You'll receive instant updates here for all your orders!",
            parse_mode="Markdown",
            reply_markup=main_menu_keyboard()
        )
    else:
        await update.message.reply_text(
            "❌ No account found with this number.\n\n"
            "Please visit *plagexpert.in* to place an order first,\n"
            "or contact support if you think this is a mistake.",
            parse_mode="Markdown"
        )

    return ConversationHandler.END

# ── Main menu ──────────────────────────────────────────────────────────────────

def main_menu_keyboard() -> ReplyKeyboardMarkup:
    keys = [
        ["📋 My Orders", "📄 Get Report"],
        ["💰 Payment Status", "⭐ Give Feedback"],
        ["📞 Contact Support"]
    ]
    return ReplyKeyboardMarkup(keys, resize_keyboard=True)


async def menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """Route keyboard menu taps."""
    text = update.message.text

    if text == "📋 My Orders":
        await show_orders(update, ctx)
    elif text == "📄 Get Report":
        await get_report_prompt(update, ctx)
    elif text == "💰 Payment Status":
        await payment_status(update, ctx)
    elif text == "⭐ Give Feedback":
        await start_feedback(update, ctx)
    elif text == "📞 Contact Support":
        await contact_support(update, ctx)

# ── My Orders ──────────────────────────────────────────────────────────────────

async def show_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await call_api("bot/orders", {"telegram_id": str(user.id)})

    if not data or not data.get("orders"):
        await update.message.reply_text(
            "📭 You have no orders yet.\n\nVisit *plagexpert.in* to submit a document.",
            parse_mode="Markdown"
        )
        return

    orders = data["orders"]
    await update.message.reply_text(
        f"📋 *Your Orders* ({len(orders)} found)\n\nTap any order for details:",
        parse_mode="Markdown",
        reply_markup=orders_keyboard(orders)
    )


async def order_detail_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "refresh_orders":
        # Rebuild orders list
        data = await call_api("bot/orders", {"telegram_id": str(query.from_user.id)})
        if data and data.get("orders"):
            await query.edit_message_reply_markup(reply_markup=orders_keyboard(data["orders"]))
        return

    order_id = query.data.replace("order_", "")
    data = await call_api("bot/order-detail", {
        "telegram_id": str(query.from_user.id),
        "order_id":    order_id
    })

    if not data or not data.get("order"):
        await query.edit_message_text("⚠️ Order not found or access denied.")
        return

    o     = data["order"]
    emoji = STATUS_EMOJI.get(o["status"], "📌")

    text = (
        f"📄 *Order #{o['id']}*\n"
        f"{'─' * 28}\n"
        f"🧾 Service: {o['service']}\n"
        f"📅 Date: {o['created_at']}\n"
        f"{emoji} Status: *{o['status'].upper()}*\n"
        f"💰 Amount: ₹{o['amount']}\n"
        f"💳 Payment: {o['payment_status'].upper()}\n"
    )

    if o.get("report_url"):
        text += f"\n📥 *Report ready!* [Download here]({o['report_url']})"
    elif o["status"] == "processing":
        text += f"\n⏱ Estimated: {o.get('eta', 'Within 24 hours')}"

    back_btn = InlineKeyboardMarkup([[
        InlineKeyboardButton("← Back to Orders", callback_data="refresh_orders")
    ]])

    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back_btn)

# ── Get Report ─────────────────────────────────────────────────────────────────

async def get_report_prompt(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await call_api("bot/orders", {
        "telegram_id": str(user.id),
        "status":      "completed"
    })

    if not data or not data.get("orders"):
        await update.message.reply_text(
            "📭 No completed orders found yet.\n\n"
            "You'll get a notification here as soon as your report is ready! 🔔"
        )
        return

    orders  = data["orders"]
    buttons = []
    for o in orders:
        if o.get("report_url"):
            buttons.append([InlineKeyboardButton(
                f"📄 #{o['id']} — {o['service']}",
                url=o["report_url"]
            )])

    if not buttons:
        await update.message.reply_text("⏳ Reports are being processed. You'll be notified when ready!")
        return

    await update.message.reply_text(
        "📥 *Your Reports* — tap to download:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons)
    )

# ── Payment Status ─────────────────────────────────────────────────────────────

async def payment_status(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    data = await call_api("bot/payments", {"telegram_id": str(user.id)})

    if not data:
        await update.message.reply_text("⚠️ Could not fetch payment info. Try again later.")
        return

    payments = data.get("payments", [])
    if not payments:
        await update.message.reply_text("💰 No payment records found.")
        return

    text = "💰 *Your Payment History*\n" + "─" * 28 + "\n"
    for p in payments[:5]:  # Show last 5
        emoji = "✅" if p["status"] == "paid" else "⏳"
        text += f"{emoji} ₹{p['amount']} — Order #{p['order_id']} — {p['date']}\n"

    pending = [p for p in payments if p["status"] != "paid"]
    if pending:
        total_due = sum(float(p["amount"]) for p in pending)
        text += f"\n⚠️ *Pending: ₹{total_due:.0f}*"
        text += f"\n💳 Pay here: {LARAVEL_API.replace('/api', '')}/payment"

    await update.message.reply_text(text, parse_mode="Markdown")

# ── Feedback ───────────────────────────────────────────────────────────────────

WAITING_FEEDBACK = 2

async def start_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stars = [
        [InlineKeyboardButton("⭐", callback_data="rate_1"),
         InlineKeyboardButton("⭐⭐", callback_data="rate_2"),
         InlineKeyboardButton("⭐⭐⭐", callback_data="rate_3")],
        [InlineKeyboardButton("⭐⭐⭐⭐", callback_data="rate_4"),
         InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data="rate_5")]
    ]
    await update.message.reply_text(
        "⭐ *How would you rate our service?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(stars)
    )


async def rate_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rating = query.data.replace("rate_", "")
    ctx.user_data["rating"] = rating

    stars_display = "⭐" * int(rating)
    await query.edit_message_text(
        f"You rated: {stars_display}\n\n"
        "💬 *Please share any comments* (or type /skip):",
        parse_mode="Markdown"
    )
    ctx.user_data["awaiting_feedback_text"] = True


async def feedback_text(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if not ctx.user_data.get("awaiting_feedback_text"):
        return

    comment = update.message.text if update.message.text != "/skip" else ""
    rating  = ctx.user_data.get("rating", "5")
    user    = update.effective_user

    await call_api("bot/feedback", {
        "telegram_id": str(user.id),
        "rating":      rating,
        "comment":     comment
    }, method="POST")

    ctx.user_data["awaiting_feedback_text"] = False

    await update.message.reply_text(
        f"🙏 Thank you for your feedback!\n\n"
        f"Rating: {'⭐' * int(rating)}\n"
        f"Your review helps us improve our service.",
        reply_markup=main_menu_keyboard()
    )

# ── Contact Support ────────────────────────────────────────────────────────────

async def contact_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📞 *Contact PlagExpert Support*\n\n"
        "🌐 Website: plagexpert.in\n"
        "📧 Email: support@plagexpert.in\n"
        "⏰ Hours: Mon–Sat, 9AM–6PM IST\n\n"
        "Or reply here and our team will get back to you shortly!",
        parse_mode="Markdown"
    )

# ── Notification sender (called by Laravel webhook) ───────────────────────────

async def send_notification(app: Application, telegram_id: str, message: str):
    """Send a message to a specific user. Called from webhook."""
    try:
        await app.bot.send_message(
            chat_id=int(telegram_id),
            text=message,
            parse_mode="Markdown"
        )
        logger.info(f"Notification sent to {telegram_id}")
    except Exception as e:
        logger.error(f"Failed to notify {telegram_id}: {e}")

# ── /help ──────────────────────────────────────────────────────────────────────

async def help_cmd(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤖 *PlagExpert Bot Commands*\n\n"
        "/start — Register or return to menu\n"
        "/orders — View all your orders\n"
        "/report — Download your reports\n"
        "/help — Show this message\n\n"
        "Or use the menu buttons below 👇",
        parse_mode="Markdown",
        reply_markup=main_menu_keyboard()
    )

# ── Unknown messages ───────────────────────────────────────────────────────────

async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    # If waiting for feedback text, route there
    if ctx.user_data.get("awaiting_feedback_text"):
        await feedback_text(update, ctx)
        return

    await update.message.reply_text(
        "I didn't understand that. Use the menu below 👇",
        reply_markup=main_menu_keyboard()
    )

# ── App setup ──────────────────────────────────────────────────────────────────

def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    # Registration conversation
    conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_PHONE: [MessageHandler(filters.CONTACT, receive_phone)],
        },
        fallbacks=[CommandHandler("start", start)],
    )

    app.add_handler(conv)
    app.add_handler(CommandHandler("help",    help_cmd))
    app.add_handler(CommandHandler("orders",  show_orders))
    app.add_handler(CommandHandler("report",  get_report_prompt))
    app.add_handler(CallbackQueryHandler(order_detail_callback, pattern=r"^order_|^refresh_orders"))
    app.add_handler(CallbackQueryHandler(rate_callback,         pattern=r"^rate_"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
    app.add_handler(MessageHandler(filters.ALL, unknown))

    return app


if __name__ == "__main__":
    application = build_app()
    logger.info("🚀 PlagExpert bot starting...")
    application.run_polling(drop_pending_updates=True)