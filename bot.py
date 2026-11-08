import os
import logging
from decimal import Decimal
from datetime import datetime

from telegram import (
    Update,
    KeyboardButton,
    ReplyKeyboardMarkup,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from supabase import create_client, Client


# =========================
# SETTINGS
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_SERVICE_ROLE_KEY")

OWNER_ID = int(os.getenv("OWNER_ID", "0"))

MIN_WITHDRAW = Decimal("2")
REFERRAL_REWARD = Decimal("0.2")

REQUIRED_CHANNELS = [
    "@BET_1XZX",
    "@GAP_BAZIN1",
]

PAYMENT_CHANNEL = "@BET_1XZX"


# =========================
# DATABASE
# =========================

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not SUPABASE_URL:
    raise RuntimeError("SUPABASE_URL is missing")

if not SUPABASE_KEY:
    raise RuntimeError("SUPABASE_SERVICE_ROLE_KEY is missing")

supabase: Client = create_client(
    SUPABASE_URL,
    SUPABASE_KEY
)

logging.basicConfig(
    level=logging.INFO
)

logger = logging.getLogger(__name__)


# =========================
# KEYBOARDS
# =========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💰 برداشت", "👥 رفرالگیری"],
            ["💳 موجودی", "✅ برداشت‌های تایید شده"],
        ],
        resize_keyboard=True
    )


def admin_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["📊 آمار کاربران", "💰 موجودی کاربران"],
            ["👥 رفرال‌ها", "💸 برداشت‌ها"],
            ["➕ افزودن کانال", "➖ حذف کانال"],
        ],
        resize_keyboard=True
    )


# =========================
# USER
# =========================

def get_user(user_id):
    result = (
        supabase
        .table("users")
        .select("*")
        .eq("id", user_id)
        .limit(1)
        .execute()
    )

    if result.data:
        return result.data[0]

    return None


def create_user(tg_user):
    existing = get_user(tg_user.id)

    if existing:
        return existing

    data = {
        "id": tg_user.id,
        "username": tg_user.username,
        "first_name": tg_user.first_name,
        "balance": 0,
        "phone_verified": False,
        "referral_rewarded": False,
    }

    result = (
        supabase
        .table("users")
        .insert(data)
        .execute()
    )

    return result.data[0]


def update_user(user_id, data):
    return (
        supabase
        .table("users")
        .update(data)
        .eq("id", user_id)
        .execute()
    )


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user
    db_user = create_user(user)

    if context.args:
        try:
            referrer_id = int(context.args[0])

            if (
                referrer_id != user.id
                and not db_user.get("referred_by")
            ):
                update_user(
                    user.id,
                    {
                        "referred_by": referrer_id
                    }
                )

        except:
            pass

    keyboard = ReplyKeyboardMarkup(
        [
            [
                KeyboardButton(
                    "📱 ارسال شماره",
                    request_contact=True
                )
            ]
        ],
        resize_keyboard=True
        ,
        one_time_keyboard=True
    )

    await update.message.reply_text(
        "سلام 👋\n\n"
        "برای شروع باید شماره تلفنت رو ارسال کنی.\n"
        "فقط شماره ایران قابل قبول است 🇮🇷",
        reply_markup=keyboard
    )


# =========================
# PHONE
# =========================

async def phone_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):

    if not update.message.contact:
        return

    contact = update.message.contact

    if contact.user_id != update.effective_user.id:
        await update.message.reply_text(
            "❌ لطفاً شماره خودت رو ارسال کن."
        )
        return

    phone = contact.phone_number

    if phone.startswith("0098"):
        phone = "+" + phone[2:]

    if not phone.startswith("+98"):
        await update.message.reply_text(
            "❌ فقط شماره ایران (+98) قابل قبول است."
        )
        return

    update_user(
        update.effective_user.id,
        {
            "phone": phone,
            "phone_verified": True,
            "username": update.effective_user.username,
            "first_name": update.effective_user.first_name,
        }
    )

    await check_channels(update, context)


# =========================
# CHANNEL CHECK
# =========================

async def check_channels(update, context):

    user_id = update.effective_user.id

    for channel in REQUIRED_CHANNELS:

        try:
            member = await context.bot.get_chat_member(
                channel,
                user_id
            )

            if member.status in [
                "left",
                "kicked"
            ]:
                await update.message.reply_text(
                    "❌ برای استفاده از ربات باید عضو کانال‌های زیر باشی:\n\n"
                    "https://t.me/BET_1XZX\n"
                    "https://t.me/GAP_BAZIN1\n\n"
                    "بعد از عضویت دوباره /start رو بزن."
                )
                return

        except Exception as e:

            logger.error(
                f"Channel check error: {channel} - {e}"
            )

            await update.message.reply_text(
                f"❌ امکان بررسی عضویت در {channel} وجود ندارد.\n"
                f"مطمئن شو ربات داخل کانال ادمین است."
            )
            return

    await reward_referral(user_id)

    await update.message.reply_text(
        "عضویت و شماره تأیید شد ✅\n\n"
        "به ربات خوش اومدی 👑",
        reply_markup=main_keyboard()
    )


# =========================
# REFERRAL REWARD
# =========================

async def reward_referral(user_id):

    user = get_user(user_id)

    if not user:
        return

    if not user.get("phone_verified"):
        return

    if user.get("referral_rewarded"):
        return

    referrer_id = user.get("referred_by")

    if not referrer_id:
        return

    referrer = get_user(referrer_id)

    if not referrer:
        return

    old_balance = Decimal(
        str(referrer.get("balance", 0))
    )

    new_balance = old_balance + REFERRAL_REWARD

    update_user(
        referrer_id,
        {
            "balance": float(new_balance)
        }
    )

    update_user(
        user_id,
        {
            "referral_rewarded": True
        }
    )


# =========================
# BALANCE
# =========================

async def balance_handler(update: Update, context):

    user = get_user(
        update.effective_user.id
    )

    if not user:
        await update.message.reply_text(
            "ابتدا /start را بزن."
        )
        return

    balance = Decimal(
        str(user.get("balance", 0))
    )

    await update.message.reply_text(
        f"💳 موجودی شما:\n\n"
        f"💰 {balance} TRX"
    )


# =========================
# REFERRAL
# =========================

async def referral_handler(update: Update, context):

    user_id = update.effective_user.id

    bot = await context.bot.get_me()

    link = (
        f"https://t.me/{bot.username}?start={user_id}"
    )

    result = (
        supabase
        .table("users")
        .select("id")
        .eq("referred_by", user_id)
        .eq("referral_rewarded", True)
        .execute()
    )

    count = len(result.data or [])

    await update.message.reply_text(
        "👥 سیستم رفرال\n\n"
        f"🔗 لینک دعوت شما:\n{link}\n\n"
        f"👤 تعداد دعوت موفق: {count}\n"
        f"💰 پاداش هر نفر: {REFERRAL_REWARD} TRX\n"
        f"💵 مجموع پاداش: {count * float(REFERRAL_REWARD)} TRX"
    )


# =========================
# WITHDRAW START
# =========================

async def withdraw_handler(update: Update, context):

    user = get_user(
        update.effective_user.id
    )

    if not user:
        await update.message.reply_text(
            "ابتدا /start را بزن."
        )
        return

    balance = Decimal(
        str(user.get("balance", 0))
    )

    if balance < MIN_WITHDRAW:

        await update.message.reply_text(
            f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است.\n\n"
            f"موجودی شما: {balance} TRX"
        )

        return

    context.user_data["withdraw_step"] = "amount"

    await update.message.reply_text(
        f"💰 مقدار برداشت را وارد کن.\n\n"
        f"حداقل: {MIN_WITHDRAW} TRX\n"
        f"موجودی: {balance} TRX"
    )


# =========================
# WITHDRAW PROCESS
# =========================

async def text_handler(update: Update, context):

    text = update.message.text.strip()

    # ADMIN
    if update.effective_user.id == OWNER_ID:
        if text == "📊 آمار کاربران":
            await admin_stats(update, context)
            return

        if text == "💰 موجودی کاربران":
            await admin_balances(update, context)
            return

        if text == "👥 رفرال‌ها":
            await admin_referrals(update, context)
            return

        if text == "💸 برداشت‌ها":
            await admin_withdrawals(update, context)
            return

    # USER BUTTONS

    if text == "💳 موجودی":
        await balance_handler(update, context)
        return

    if text == "👥 رفرالگیری":
        await referral_handler(update, context)
        return

    if text == "💰 برداشت":
        await withdraw_handler(update, context)
        return

    if text == "✅ برداشت‌های تایید شده":
        await approved_withdrawals(update, context)
        return

    # WITHDRAW AMOUNT

    step = context.user_data.get("withdraw_step")

    if step == "amount":

        try:
            amount = Decimal(text)
        except:

            await update.message.reply_text(
                "❌ مقدار نامعتبر است."
            )
            return

        user = get_user(
            update.effective_user.id
        )

        balance = Decimal(
            str(user.get("balance", 0))
        )

        if amount < MIN_WITHDRAW:

            await update.message.reply_text(
                f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است."
            )
            return

        if amount > balance:

            await update.message.reply_text(
                "❌ موجودی کافی نیست."
            )
            return

        context.user_data["withdraw_amount"] = str(amount)
        context.user_data["withdraw_step"] = "wallet"

        await update.message.reply_text(
            "💳 حالا آدرس کیف پول TRON را ارسال کن."
        )

        return

    # WALLET

    if step == "wallet":

        wallet = text
        amount = Decimal(
            context.user_data["withdraw_amount"]
        )

        if len(wallet) < 20:

            await update.message.reply_text(
                "❌ آدرس کیف پول نامعتبر است."
            )
            return

        result = (
            supabase
            .table("withdrawals")
            .insert(
                {
                    "user_id": update.effective_user.id,
                    "amount": float(amount),
                    "wallet": wallet,
                    "status": "pending",
                }
            )
            .execute()
        )

        withdrawal = result.data[0]

        context.user_data.clear()

        await update.message.reply_text(
            "✅ درخواست برداشت ثبت شد.\n\n"
            f"💰 مبلغ: {amount} TRX\n"
            "⏳ وضعیت: در انتظار بررسی"
        )

        await notify_admin(
            update,
            context,
            withdrawal
        )

        return


# =========================
# ADMIN NOTIFY
# =========================

async def notify_admin(update, context, withdrawal):

    user = update.effective_user

    amount = withdrawal["amount"]
    wallet = withdrawal["wallet"]
    wid = withdrawal["id"]

    text = (
        "💸 درخواست برداشت جدید\n\n"
        f"🆔 ID: {user.id}\n"
        f"👤 Username: @{user.username or 'ندارد'}\n"
        f"💰 مبلغ: {amount} TRX\n"
        f"💳 Wallet: {wallet}\n\n"
        "یکی از گزینه‌ها را انتخاب کن:"
    )

    keyboard = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "✅ تایید",
                    callback_data=f"approve:{wid}"
                ),
                InlineKeyboardButton(
                    "❌ رد",
                    callback_data=f"reject:{wid}"
                ),
            ]
        ]
    )

    await context.bot.send_message(
        OWNER_ID,
        text,
        reply_markup=keyboard
    )


# =========================
# APPROVE / REJECT
# =========================

async def withdrawal_callback(update, context):

    query = update.callback_query

    await query.answer()

    if query.from_user.id != OWNER_ID:
        await query.answer(
            "دسترسی ندارید.",
            show_alert=True
        )
        return

    action, withdrawal_id = (
        query.data.split(":")
    )

    result = (
        supabase
        .table("withdrawals")
        .select("*")
        .eq("id", int(withdrawal_id))
        .limit(1)
        .execute()
    )

    if not result.data:
        await query.edit_message_text(
            "❌ درخواست پیدا نشد."
        )
        return

    withdrawal = result.data[0]

    if withdrawal["status"] != "pending":

        await query.edit_message_text(
            "⚠️ این درخواست قبلاً بررسی شده."
        )
        return

    user_id = withdrawal["user_id"]
    amount = Decimal(
        str(withdrawal["amount"])
    )

    if action == "approve":

        user = get_user(user_id)

        balance = Decimal(
            str(user["balance"])
        )

        if balance < amount:

            await query.edit_message_text(
                "❌ موجودی کاربر کافی نیست."
            )
            return

        new_balance = balance - amount

        update_user(
            user_id,
            {
                "balance": float(new_balance)
            }
        )

        supabase.table("withdrawals").update(
            {
                "status": "approved",
                "admin_id": OWNER_ID,
                "processed_at": datetime.utcnow().isoformat()
            }
        ).eq(
            "id",
            int(withdrawal_id)
        ).execute()

        await context.bot.send_message(
            user_id,
            "✅ برداشت شما تایید شد.\n\n"
            f"💰 مبلغ: {amount} TRX\n"
            "واریز انجام شد."
        )

        await context.bot.send_message(
            PAYMENT_CHANNEL,
            f"💸 برداشت انجام شد\n\n"
            f"🆔 ID: {user_id}\n"
            f"💰 مبلغ: {amount} TRX"
        )

        await query.edit_message_text(
            "✅ برداشت تایید شد."
        )

    else:

        supabase.table("withdrawals").update(
            {
                "status": "rejected",
                "admin_id": OWNER_ID,
                "processed_at": datetime.utcnow().isoformat()
            }
        ).eq(
            "id",
            int(withdrawal_id)
        ).execute()

        await context.bot.send_message(
            user_id,
            "❌ برداشت شما رد شد."
        )

        await query.edit_message_text(
            "❌ برداشت رد شد."
        )


# =========================
# APPROVED WITHDRAWALS
# =========================

async def approved_withdrawals(update, context):

    result = (
        supabase
        .table("withdrawals")
        .select("*")
        .eq(
            "user_id",
            update.effective_user.id
        )
        .eq(
            "status",
            "approved"
        )
        .order(
            "created_at",
            desc=True
        )
        .limit(20)
        .execute()
    )

    if not result.data:

        await update.message.reply_text(
            "هنوز برداشت تایید شده‌ای نداری."
        )
        return

    text = "✅ برداشت‌های تایید شده:\n\n"

    for w in result.data:

        text += (
            f"🆔 {w['id']}\n"
            f"💰 {w['amount']} TRX\n"
            f"📅 {w['created_at']}\n\n"
        )

    await update.message.reply_text(text)


# =========================
# ADMIN STATS
# =========================

async def admin_stats(update, context):

    users = (
        supabase
        .table("users")
        .select("id")
        .execute()
    )

    withdrawals = (
        supabase
        .table("withdrawals")
        .select("id")
        .eq("status", "pending")
        .execute()
    )

    await update.message.reply_text(
        "📊 آمار ربات\n\n"
        f"👤 کاربران: {len(users.data or [])}\n"
        f"💸 برداشت‌های در انتظار: "
        f"{len(withdrawals.data or [])}"
    )


# =========================
# ADMIN BALANCES
# =========================

async def admin_balances(update, context):

    result = (
        supabase
        .table("users")
        .select("id,username,balance")
        .order("balance", desc=True)
        .limit(50)
        .execute()
    )

    if not result.data:

        await update.message.reply_text(
            "کاربری وجود ندارد."
        )
        return

    text = "💰 موجودی کاربران:\n\n"

    for user in result.data:

        username = user.get("username") or "ندارد"

        text += (
            f"🆔 {user['id']}\n"
            f"👤 @{username}\n"
            f"💰 {user['balance']} TRX\n\n"
        )

    await update.message.reply_text(text)


# =========================
# ADMIN REFERRALS
# =========================

async def admin_referrals(update, context):

    users = (
        supabase
        .table("users")
        .select("id,username,referred_by,referral_rewarded")
        .execute()
    )

    counts = {}

    for user in users.data or []:

        ref = user.get("referred_by")

        if ref and user.get("referral_rewarded"):
            counts[ref] = counts.get(ref, 0) + 1

    if not counts:

        await update.message.reply_text(
            "هنوز رفرال موفقی ثبت نشده."
        )
        return

    text = "👥 رفرال‌ها:\n\n"

    for referrer_id, count in counts.items():

        referrer = get_user(referrer_id)

        username = (
            referrer.get("username")
            if referrer else None
        )

        total = Decimal(count) * REFERRAL_REWARD

        text += (
            f"🆔 {referrer_id}\n"
            f"👤 @{username or 'ندارد'}\n"
            f"👥 دعوت موفق: {count}\n"
            f"💰 پاداش: {total} TRX\n\n"
        )

    await update.message.reply_text(text)


# =========================
# ADMIN WITHDRAWALS
# =========================

async def admin_withdrawals(update, context):

    result = (
        supabase
        .table("withdrawals")
        .select("*")
        .eq("status", "pending")
        .order("created_at", desc=True)
        .limit(20)
        .execute()
    )

    if not result.data:

        await update.message.reply_text(
            "برداشت در انتظاری وجود ندارد."
        )
        return

    text = "💸 برداشت‌های در انتظار:\n\n"

    for w in result.data:

        text += (
            f"🆔 درخواست: {w['id']}\n"
            f"👤 User: {w['user_id']}\n"
            f"💰 مبلغ: {w['amount']} TRX\n"
            f"💳 Wallet: {w['wallet']}\n\n"
        )

    await update.message.reply_text(text)


# =========================
# ERROR
# =========================

async def error_handler(update, context):

    logger.error(
        "Exception while handling update:",
        exc_info=context.error
    )


# =========================
# RUN
# =========================

def main():

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        MessageHandler(
            filters.CONTACT,
            phone_handler
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            withdrawal_callback,
            pattern=r"^(approve|reject):"
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    application.add_error_handler(
        error_handler
    )

    print("Bot started...")

    application.run_polling()


if __name__ == "__main__":
    main()
