"""
PlagExpert Telegram Bot — v2
Tailored to the actual PlagExpert Laravel schema:
  - Users have role=client and belong to a Client record
  - Orders tracked by token_view, status via OrderStatus enum
  - Reports stored as ai_report_path / plag_report_path on OrderReport
  - Clients have slots, slots_consumed, plan_expiry
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
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ── Config ─────────────────────────────────────────────────────────────────────
def get_env_or_exit(var_name: str) -> str:
    val = os.environ.get(var_name)
    if not val:
        logger.critical(f"❌ Missing required environment variable: {var_name}")
        logger.critical(f"Please set {var_name} in your Railway variables.")
        exit(1)
    return val

BOT_TOKEN   = get_env_or_exit("BOT_TOKEN")          # From BotFather
API_BASE    = get_env_or_exit("LARAVEL_API_URL")     # https://plagexpert.in/api
API_SECRET  = get_env_or_exit("API_SECRET_KEY")      # Shared secret (same as Laravel .env)

# ── States ─────────────────────────────────────────────────────────────────────
WAITING_PHONE    = 1
WAITING_FEEDBACK = 2

# ── API helper ─────────────────────────────────────────────────────────────────
async def api(endpoint: str, params: dict = None, method="GET") -> dict | None:
    headers = {"X-Bot-Secret": API_SECRET, "Accept": "application/json"}
    url = f"{API_BASE}/{endpoint}"
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            r = await (client.get(url, params=params, headers=headers)
                       if method == "GET"
                       else client.post(url, json=params, headers=headers))
            r.raise_for_status()
            return r.json()
    except Exception as e:
        logger.error(f"API [{method} {endpoint}]: {e}")
        return None

# ── Keyboards ──────────────────────────────────────────────────────────────────
def main_menu() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        [["📋 My Orders", "📄 Download Reports"],
         ["🎯 Slot Balance", "⭐ Give Feedback"],
         ["📞 Contact Support"]],
        resize_keyboard=True
    )

def orders_inline(orders: list) -> InlineKeyboardMarkup:
    STATUS_ICON = {
        "pending":    "⏳",
        "processing": "🔄",
        "delivered":  "✅",
        "cancelled":  "❌",
    }
    rows = []
    for o in orders:
        icon  = STATUS_ICON.get(o["status"], "📌")
        label = f"{icon} #{o['id']} — {o['files_count']} file(s) — {o['status'].upper()}"
        rows.append([InlineKeyboardButton(label, callback_data=f"ord_{o['id']}")])
    rows.append([InlineKeyboardButton("🔄 Refresh", callback_data="orders_refresh")])
    return InlineKeyboardMarkup(rows)

# ── /start ─────────────────────────────────────────────────────────────────────
async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    user = update.effective_user
    data = await api("bot/user", {"telegram_id": str(user.id)})

    if data and data.get("found"):
        await update.message.reply_text(
            f"👋 Welcome back, *{data['name']}*!\n\nUse the menu below 👇",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
        return ConversationHandler.END

    btn = KeyboardButton("📱 Share my phone number", request_contact=True)
    await update.message.reply_text(
        "👋 Welcome to *PlagExpert* bot!\n\n"
        "I can help you:\n"
        "📋 Check order status\n"
        "📥 Download your plagiarism reports\n"
        "🎯 View your slot balance\n"
        "🔔 Get instant order updates\n\n"
        "Please share your phone number to verify your account 👇",
        parse_mode="Markdown",
        reply_markup=ReplyKeyboardMarkup([[btn]], resize_keyboard=True, one_time_keyboard=True),
    )
    return WAITING_PHONE

# ── Phone verification ─────────────────────────────────────────────────────────
async def receive_phone(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> int:
    contact = update.message.contact
    phone   = contact.phone_number.replace("+", "").replace(" ", "")
    user    = update.effective_user

    await update.message.reply_text("🔍 Verifying…", reply_markup=ReplyKeyboardRemove())

    data = await api("bot/register", {
        "phone":       phone,
        "telegram_id": str(user.id),
        "tg_name":     user.full_name,
        "tg_username": user.username or "",
    }, method="POST")

    if not data:
        await update.message.reply_text("⚠️ Server unavailable. Please try again shortly.")
        return ConversationHandler.END

    if data.get("found"):
        await update.message.reply_text(
            f"✅ Verified! Hello, *{data['name']}* 🎉\n\n"
            "Your account is linked. You'll receive instant order notifications here!",
            parse_mode="Markdown",
            reply_markup=main_menu(),
        )
    else:
        await update.message.reply_text(
            "❌ No client account found with this number.\n\n"
            "Please register at *plagexpert.in* or contact support.",
            parse_mode="Markdown",
        )
    return ConversationHandler.END

# ── Menu router ────────────────────────────────────────────────────────────────
async def menu_handler(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("awaiting_feedback"):
        await save_feedback(update, ctx)
        return

    text = update.message.text
    if   text == "📋 My Orders":         await show_orders(update, ctx)
    elif text == "📄 Download Reports":  await show_reports(update, ctx)
    elif text == "🎯 Slot Balance":      await slot_balance(update, ctx)
    elif text == "⭐ Give Feedback":     await start_feedback(update, ctx)
    elif text == "📞 Contact Support":   await contact_support(update, ctx)
    else:
        await update.message.reply_text("Use the menu below 👇", reply_markup=main_menu())

# ── My Orders ──────────────────────────────────────────────────────────────────
async def show_orders(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = await api("bot/orders", {"telegram_id": str(update.effective_user.id)})

    if not data or not data.get("orders"):
        await update.message.reply_text(
            "📭 No orders found.\n\nVisit *plagexpert.in* to submit documents.",
            parse_mode="Markdown",
        )
        return

    orders = data["orders"]
    await update.message.reply_text(
        f"📋 *Your Orders* — {len(orders)} found\n\nTap any order for details:",
        parse_mode="Markdown",
        reply_markup=orders_inline(orders),
    )

async def order_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if query.data == "orders_refresh":
        data = await api("bot/orders", {"telegram_id": str(query.from_user.id)})
        if data and data.get("orders"):
            await query.edit_message_reply_markup(reply_markup=orders_inline(data["orders"]))
        return

    order_id = query.data.replace("ord_", "")
    data = await api("bot/order-detail", {
        "telegram_id": str(query.from_user.id),
        "order_id":    order_id,
    })

    if not data or not data.get("order"):
        await query.edit_message_text("⚠️ Order not found.")
        return

    o = data["order"]
    STATUS_ICON = {"pending": "⏳", "processing": "🔄", "delivered": "✅", "cancelled": "❌"}
    icon = STATUS_ICON.get(o["status"], "📌")

    text = (
        f"📄 *Order #{o['id']}*\n"
        f"{'─' * 26}\n"
        f"🔖 Tracking: `{o['token_view']}`\n"
        f"📁 Files: {o['files_count']}\n"
        f"📅 Submitted: {o['created_at']}\n"
        f"⏰ Due: {o['due_at']}\n"
        f"{icon} Status: *{o['status'].upper()}*\n"
    )

    if o.get("delivered_at"):
        text += f"✅ Delivered: {o['delivered_at']}\n"

    if o.get("report_ready"):
        text += f"\n📥 *Report ready!* Use '📄 Download Reports' to get your files."
    elif o["status"] == "processing":
        text += f"\n⏱ Being processed — you'll get notified when done."
    elif o["status"] == "pending":
        text += f"\n🕐 Waiting to be picked up by our team."

    if o.get("notes"):
        text += f"\n📝 Notes: {o['notes']}"

    back = InlineKeyboardMarkup([[
        InlineKeyboardButton("← Back to Orders", callback_data="orders_refresh")
    ]])
    await query.edit_message_text(text, parse_mode="Markdown", reply_markup=back)

# ── Download Reports ───────────────────────────────────────────────────────────
async def show_reports(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = await api("bot/reports", {"telegram_id": str(update.effective_user.id)})

    if not data or not data.get("reports"):
        await update.message.reply_text(
            "📭 No reports available yet.\n\n"
            "You'll get a Telegram notification as soon as your report is ready! 🔔"
        )
        return

    buttons = []
    for r in data["reports"]:
        order_label = f"Order #{r['order_id']} — {r['files_count']} file(s)"
        row = []
        if r.get("plag_url"):
            row.append(InlineKeyboardButton("📄 Plag Report", url=r["plag_url"]))
        if r.get("ai_url"):
            row.append(InlineKeyboardButton("🤖 AI Report", url=r["ai_url"]))
        if row:
            buttons.append([InlineKeyboardButton(order_label, callback_data="noop")])
            buttons.append(row)

    if not buttons:
        await update.message.reply_text("⏳ Reports are being processed. You'll be notified!")
        return

    await update.message.reply_text(
        "📥 *Your Reports* — tap to download:",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(buttons),
    )

# ── Slot Balance ───────────────────────────────────────────────────────────────
async def slot_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    data = await api("bot/slots", {"telegram_id": str(update.effective_user.id)})

    if not data:
        await update.message.reply_text("⚠️ Could not fetch slot info. Try again later.")
        return

    used      = data.get("slots_consumed", 0)
    total     = data.get("slots", 0)
    remaining = total - used
    expiry    = data.get("plan_expiry", "N/A")
    bar_filled = int((used / total * 10)) if total else 0
    bar = "█" * bar_filled + "░" * (10 - bar_filled)

    text = (
        f"🎯 *Your Slot Balance*\n"
        f"{'─' * 26}\n"
        f"Total Slots:  {total}\n"
        f"Used:         {used}\n"
        f"Remaining:    *{remaining}*\n\n"
        f"[{bar}] {used}/{total}\n\n"
        f"📅 Plan Expiry: {expiry}\n"
    )

    if remaining == 0:
        text += "\n⚠️ *Slots exhausted!* Contact admin to top up."
    elif remaining <= 5:
        text += f"\n⚠️ *Low slots* — only {remaining} left. Request a top-up soon."

    await update.message.reply_text(text, parse_mode="Markdown")

# ── Feedback ───────────────────────────────────────────────────────────────────
async def start_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    stars = [
        [InlineKeyboardButton("⭐", callback_data="rate_1"),
         InlineKeyboardButton("⭐⭐", callback_data="rate_2"),
         InlineKeyboardButton("⭐⭐⭐", callback_data="rate_3")],
        [InlineKeyboardButton("⭐⭐⭐⭐", callback_data="rate_4"),
         InlineKeyboardButton("⭐⭐⭐⭐⭐", callback_data="rate_5")],
    ]
    await update.message.reply_text(
        "⭐ *How would you rate our service?*",
        parse_mode="Markdown",
        reply_markup=InlineKeyboardMarkup(stars),
    )

async def rate_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    rating = query.data.replace("rate_", "")
    ctx.user_data["rating"]           = rating
    ctx.user_data["awaiting_feedback"] = True
    await query.edit_message_text(
        f"You chose: {'⭐' * int(rating)}\n\n"
        "💬 Add a comment (or type /skip):",
        parse_mode="Markdown",
    )

async def save_feedback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    comment = "" if update.message.text == "/skip" else update.message.text
    rating  = ctx.user_data.pop("rating", "5")
    ctx.user_data.pop("awaiting_feedback", None)

    await api("bot/feedback", {
        "telegram_id": str(update.effective_user.id),
        "rating":      rating,
        "comment":     comment,
    }, method="POST")

    await update.message.reply_text(
        f"🙏 Thank you! {'⭐' * int(rating)}\nYour feedback helps us improve.",
        reply_markup=main_menu(),
    )

# ── Contact Support ────────────────────────────────────────────────────────────
async def contact_support(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📞 *PlagExpert Support*\n\n"
        "🌐 plagexpert.in\n"
        "📧 support@plagexpert.in\n"
        "⏰ Mon–Sat, 9AM–6PM IST",
        parse_mode="Markdown",
    )

# ── Fallback ───────────────────────────────────────────────────────────────────
async def unknown(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    if ctx.user_data.get("awaiting_feedback"):
        await save_feedback(update, ctx)
        return
    await update.message.reply_text("Use the menu below 👇", reply_markup=main_menu())

async def noop_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    await update.callback_query.answer()

# ── App ────────────────────────────────────────────────────────────────────────
def build_app() -> Application:
    app = Application.builder().token(BOT_TOKEN).build()

    conv = ConversationHandler(
        entry_points=[CommandHandler("start", cmd_start)],
        states={WAITING_PHONE: [MessageHandler(filters.CONTACT, receive_phone)]},
        fallbacks=[CommandHandler("start", cmd_start)],
    )

    app.add_handler(conv)
    app.add_handler(CallbackQueryHandler(order_callback,  pattern=r"^ord_|^orders_refresh$"))
    app.add_handler(CallbackQueryHandler(rate_callback,   pattern=r"^rate_"))
    app.add_handler(CallbackQueryHandler(noop_callback,   pattern=r"^noop$"))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, menu_handler))
    app.add_handler(MessageHandler(filters.ALL, unknown))

    return app

if __name__ == "__main__":
    application = build_app()
    logger.info("🚀 PlagExpert bot starting…")
    application.run_polling(drop_pending_updates=True)