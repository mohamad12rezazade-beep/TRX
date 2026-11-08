import os
import re
import logging
from decimal import Decimal, InvalidOperation

import psycopg
from psycopg.rows import dict_row

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)

# =========================================================
# CONFIG
# =========================================================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID = os.getenv("OWNER_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not OWNER_ID:
    raise RuntimeError("OWNER_ID is missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

OWNER_ID = int(OWNER_ID)

MIN_WITHDRAW = Decimal("2")
REFERRAL_REWARD = Decimal("0.2")

PAYMENT_CHANNEL = "@BET_1XZX"

DEFAULT_CHANNELS = [
    "@BET_1XZX",
    "@GAP_BAZIN1",
]

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(message)s",
)

logger = logging.getLogger(__name__)


# =========================================================
# DATABASE
# =========================================================

def db():
    return psycopg.connect(
        DATABASE_URL,
        row_factory=dict_row,
    )


def init_db():
    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    phone TEXT,
                    verified BOOLEAN NOT NULL DEFAULT FALSE,
                    balance NUMERIC(18,8) NOT NULL DEFAULT 0,
                    referred_by BIGINT,
                    referral_rewarded BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount NUMERIC(18,8) NOT NULL,
                    wallet TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    processed_at TIMESTAMPTZ
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS required_channels (
                    id BIGSERIAL PRIMARY KEY,
                    channel TEXT UNIQUE NOT NULL,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            for channel in DEFAULT_CHANNELS:
                cur.execute(
                    """
                    INSERT INTO required_channels(channel)
                    VALUES (%s)
                    ON CONFLICT (channel) DO NOTHING
                    """,
                    (channel,),
                )

        conn.commit()

    logger.info("Database initialized")


# =========================================================
# USER
# =========================================================

def create_user(user_id, referred_by=None):
    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                INSERT INTO users(id, referred_by)
                VALUES (%s, %s)
                ON CONFLICT (id) DO NOTHING
                """,
                (user_id, referred_by),
            )

        conn.commit()


def get_user(user_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE id = %s",
                (user_id,),
            )
            return cur.fetchone()


def set_phone(user_id, phone):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET phone = %s,
                    verified = TRUE
                WHERE id = %s
                """,
                (phone, user_id),
            )

        conn.commit()


def get_balance(user_id):
    user = get_user(user_id)

    if not user:
        return Decimal("0")

    return Decimal(str(user["balance"]))


# =========================================================
# CHANNELS
# =========================================================

def get_required_channels():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT channel
                FROM required_channels
                ORDER BY id
            """)
            rows = cur.fetchall()

    return [x["channel"] for x in rows]


def channel_keyboard():
    buttons = []

    for channel in get_required_channels():

        username = channel.replace("@", "").strip()

        buttons.append([
            InlineKeyboardButton(
                f"📢 عضویت در {channel}",
                url=f"https://t.me/{username}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "✅ بررسی عضویت",
            callback_data="check_membership",
        )
    ])

    return InlineKeyboardMarkup(buttons)


async def is_member(bot, user_id):
    for channel in get_required_channels():

        try:
            member = await bot.get_chat_member(
                chat_id=channel,
                user_id=user_id,
            )

            if member.status in (
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
            ):
                return False

        except Exception as e:
            logger.error(
                "Membership error for %s: %s",
                channel,
                e,
            )
            return False

    return True


# =========================================================
# MAIN MENU
# =========================================================

def main_menu():
    return ReplyKeyboardMarkup(
        [
            ["💸 برداشت", "👥 رفرالگیری"],
            ["💰 موجودی", "📋 برداشت های تایید شده"],
        ],
        resize_keyboard=True,
    )


# =========================================================
# /START
# =========================================================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    referred_by = None

    if context.args:

        try:
            ref = int(context.args[0])

            if ref != user.id:
                referred_by = ref

        except Exception:
            pass

    create_user(
        user.id,
        referred_by,
    )

    current = get_user(user.id)

    if current and current["verified"]:

        if await is_member(
            context.bot,
            user.id,
        ):
            await update.message.reply_text(
                "👋 خوش اومدی",
                reply_markup=main_menu(),
            )
            return

    await update.message.reply_text(
        "سلام 👋\n\n"
        "برای شروع، شماره موبایل ایرانیت رو ارسال کن:",
        reply_markup=ReplyKeyboardMarkup(
            [
                [
                    KeyboardButton(
                        "📱 ارسال شماره موبایل",
                        request_contact=True,
                    )
                ]
            ],
            resize_keyboard=True,
        ),
    )


# =========================================================
# PHONE
# =========================================================

def normalize_phone(phone):

    phone = (
        phone
        .replace(" ", "")
        .replace("-", "")
        .replace("(", "")
        .replace(")", "")
    )

    if re.fullmatch(r"\+989\d{9}", phone):
        return phone

    if re.fullmatch(r"00989\d{9}", phone):
        return "+98" + phone[4:]

    if re.fullmatch(r"09\d{9}", phone):
        return "+98" + phone[1:]

    return None


async def contact_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    contact = update.message.contact
    user = update.effective_user

    if (
        contact.user_id is not None
        and contact.user_id != user.id
    ):
        await update.message.reply_text(
            "❌ فقط شماره خودت رو ارسال کن."
        )
        return

    phone = normalize_phone(
        contact.phone_number
    )

    if not phone:
        await update.message.reply_text(
            "❌ فقط شماره موبایل ایران قابل قبول است."
        )
        return

    create_user(user.id)

    set_phone(
        user.id,
        phone,
    )

    if not await is_member(
        context.bot,
        user.id,
    ):

        await update.message.reply_text(
            "✅ شماره تأیید شد.\n\n"
            "حالا عضو کانال‌های زیر شو:",
            reply_markup=channel_keyboard(),
        )

        return

    reward_referral(user.id)

    await update.message.reply_text(
        "✅ ثبت‌نام کامل شد.",
        reply_markup=main_menu(),
    )


# =========================================================
# REFERRAL REWARD
# =========================================================

def reward_referral(user_id):

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT referred_by,
                       referral_rewarded,
                       verified
                FROM users
                WHERE id = %s
                FOR UPDATE
                """,
                (user_id,),
            )

            user = cur.fetchone()

            if not user:
                return

            if not user["verified"]:
                return

            if user["referral_rewarded"]:
                return

            referrer = user["referred_by"]

            if not referrer:
                return

            cur.execute(
                """
                UPDATE users
                SET balance = balance + %s
                WHERE id = %s
                """,
                (
                    REFERRAL_REWARD,
                    referrer,
                ),
            )

            cur.execute(
                """
                UPDATE users
                SET referral_rewarded = TRUE
                WHERE id = %s
                """,
                (user_id,),
            )

        conn.commit()


# =========================================================
# CHECK MEMBERSHIP
# =========================================================

async def membership_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    await query.answer()

    user_id = query.from_user.id

    user = get_user(user_id)

    if not user or not user["verified"]:
        await query.message.reply_text(
            "❌ ابتدا شماره موبایلت رو ارسال کن."
        )
        return

    if not await is_member(
        context.bot,
        user_id,
    ):
        await query.message.reply_text(
            "❌ هنوز عضو همه کانال‌ها نیستی.",
            reply_markup=channel_keyboard(),
        )
        return

    reward_referral(user_id)

    await query.message.reply_text(
        "✅ عضویت تأیید شد.",
        reply_markup=main_menu(),
    )


# =========================================================
# BALANCE
# =========================================================

async def balance_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = get_user(
        update.effective_user.id
    )

    if not user or not user["verified"]:
        await update.message.reply_text(
            "❌ ابتدا /start رو بزن."
        )
        return

    balance = get_balance(
        update.effective_user.id
    )

    await update.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"{balance:.8f} TRX"
    )


# =========================================================
# REFERRAL
# =========================================================

async def referral_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    bot = await context.bot.get_me()

    link = (
        f"https://t.me/{bot.username}"
        f"?start={user_id}"
    )

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT COUNT(*) AS count
                FROM users
                WHERE referred_by = %s
                """,
                (user_id,),
            )

            count = cur.fetchone()["count"]

            cur.execute(
                """
                SELECT COUNT(*) AS rewarded
                FROM users
                WHERE referred_by = %s
                AND referral_rewarded = TRUE
                """,
                (user_id,),
            )

            rewarded = cur.fetchone()["rewarded"]

    total = Decimal(str(rewarded)) * REFERRAL_REWARD

    await update.message.reply_text(
        "👥 رفرالگیری\n\n"
        f"تعداد زیرمجموعه: {count}\n"
        f"پاداش دریافت‌شده: {total:.2f} TRX\n\n"
        f"🔗 لینک دعوت:\n{link}"
    )


# =========================================================
# WITHDRAW START
# =========================================================

async def withdraw_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user = get_user(
        update.effective_user.id
    )

    if not user or not user["verified"]:
        await update.message.reply_text(
            "❌ ابتدا احراز هویت کن."
        )
        return

    balance = get_balance(
        update.effective_user.id
    )

    if balance < MIN_WITHDRAW:
        await update.message.reply_text(
            f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است.\n\n"
            f"موجودی: {balance:.8f} TRX"
        )
        return

    context.user_data.clear()
    context.user_data["withdraw_step"] = "amount"

    await update.message.reply_text(
        f"💰 موجودی: {balance:.8f} TRX\n\n"
        "مقدار برداشت را وارد کن:"
    )


# =========================================================
# WITHDRAW TEXT
# =========================================================

async def withdrawal_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    step = context.user_data.get(
        "withdraw_step"
    )

    if step == "amount":

        try:
            amount = Decimal(
                update.message.text.strip()
            )

        except InvalidOperation:
            await update.message.reply_text(
                "❌ مقدار نامعتبر است."
            )
            return

        balance = get_balance(
            update.effective_user.id
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

        context.user_data[
            "withdraw_amount"
        ] = str(amount)

        context.user_data[
            "withdraw_step"
        ] = "wallet"

        await update.message.reply_text(
            "آدرس کیف پول TRON را ارسال کن:"
        )

        return

    if step == "wallet":

        wallet = update.message.text.strip()

        if not re.fullmatch(
            r"T[1-9A-HJ-NP-Za-km-z]{33}",
            wallet,
        ):
            await update.message.reply_text(
                "❌ آدرس TRON معتبر نیست."
            )
            return

        amount = Decimal(
            context.user_data[
                "withdraw_amount"
            ]
        )

        user_id = update.effective_user.id

        with db() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    """
                    SELECT balance
                    FROM users
                    WHERE id = %s
                    FOR UPDATE
                    """,
                    (user_id,),
                )

                user = cur.fetchone()

                if not user:
                    await update.message.reply_text(
                        "❌ کاربر پیدا نشد."
                    )
                    return

                balance = Decimal(
                    str(user["balance"])
                )

                if amount > balance:
                    await update.message.reply_text(
                        "❌ موجودی کافی نیست."
                    )
                    return

                cur.execute(
                    """
                    UPDATE users
                    SET balance = balance - %s
                    WHERE id = %s
                    """,
                    (
                        amount,
                        user_id,
                    ),
                )

                cur.execute(
                    """
                    INSERT INTO withdrawals(
                        user_id,
                        amount,
                        wallet,
                        status
                    )
                    VALUES (
                        %s,
                        %s,
                        %s,
                        'pending'
                    )
                    RETURNING id
                    """,
                    (
                        user_id,
                        amount,
                        wallet,
                    ),
                )

                withdrawal = cur.fetchone()

            conn.commit()

        withdrawal_id = withdrawal["id"]

        context.user_data.clear()

        await update.message.reply_text(
            "✅ درخواست برداشت ثبت شد.\n\n"
            f"مبلغ: {amount} TRX\n"
            f"شماره درخواست: #{withdrawal_id}",
            reply_markup=main_menu(),
        )

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ تایید",
                        callback_data=f"approve:{withdrawal_id}",
                    ),
                    InlineKeyboardButton(
                        "❌ رد",
                        callback_data=f"reject:{withdrawal_id}",
                    ),
                ]
            ]
        )

        await context.bot.send_message(
            chat_id=OWNER_ID,
            text=(
                "💸 درخواست برداشت\n\n"
                f"👤 User ID: {user_id}\n"
                f"💰 Amount: {amount} TRX\n"
                f"💳 Wallet: {wallet}\n"
                f"🆔 Request: #{withdrawal_id}"
            ),
            reply_markup=keyboard,
        )


# =========================================================
# APPROVED WITHDRAWALS
# =========================================================

async def approved_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT id, amount
                FROM withdrawals
                WHERE user_id = %s
                AND status = 'approved'
                ORDER BY id DESC
                LIMIT 20
                """,
                (user_id,),
            )

            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "📋 هنوز برداشت تاییدشده‌ای نداری."
        )
        return

    text = "📋 برداشت‌های تایید شده:\n\n"

    for row in rows:
        text += (
            f"#{row['id']} — "
            f"{Decimal(str(row['amount']))} TRX\n"
        )

    await update.message.reply_text(text)


# =========================================================
# ADMIN
# =========================================================

def is_admin(user_id):
    return user_id == OWNER_ID


async def admin_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text(
            "❌ دسترسی ندارید."
        )
        return

    keyboard = ReplyKeyboardMarkup(
        [
            ["📊 آمار کاربران", "💰 موجودی کاربران"],
            ["💸 برداشت‌های در انتظار"],
            ["📢 کانال‌های اجباری"],
        ],
        resize_keyboard=True,
    )

    await update.message.reply_text(
        "🛠 پنل مدیریت",
        reply_markup=keyboard,
    )


async def admin_stats(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                "SELECT COUNT(*) AS n FROM users"
            )
            total = cur.fetchone()["n"]

            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM users
                WHERE verified = TRUE
                """
            )
            verified = cur.fetchone()["n"]

            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM users
                WHERE referred_by IS NOT NULL
                """
            )
            referrals = cur.fetchone()["n"]

            cur.execute(
                """
                SELECT COALESCE(SUM(balance), 0) AS total
                FROM users
                """
            )
            total_balance = Decimal(
                str(cur.fetchone()["total"])
            )

            cur.execute(
                """
                SELECT COUNT(*) AS n
                FROM withdrawals
                WHERE status = 'pending'
                """
            )
            pending = cur.fetchone()["n"]

    await update.message.reply_text(
        "📊 آمار کاربران\n\n"
        f"👤 کل کاربران: {total}\n"
        f"✅ احراز شده: {verified}\n"
        f"👥 دارای رفرال: {referrals}\n"
        f"💰 مجموع موجودی: {total_balance:.8f} TRX\n"
        f"💸 برداشت در انتظار: {pending}"
    )


async def admin_balances(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT
                    id,
                    phone,
                    verified,
                    balance
                FROM users
                ORDER BY id DESC
                LIMIT 50
                """
            )

            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "هیچ کاربری وجود ندارد."
        )
        return

    text = "💰 کاربران\n\n"

    for row in rows:

        text += (
            f"👤 {row['id']}\n"
            f"📱 {row['phone'] or '-'}\n"
            f"💰 {Decimal(str(row['balance'])):.8f} TRX\n"
            f"🔐 {'تأیید شده' if row['verified'] else 'تأیید نشده'}\n"
            f"────────────\n"
        )

    await update.message.reply_text(text)


async def admin_pending(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM withdrawals
                WHERE status = 'pending'
                ORDER BY id DESC
                LIMIT 20
                """
            )

            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "هیچ برداشت در انتظاری نیست."
        )
        return

    for row in rows:

        keyboard = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "✅ تایید",
                        callback_data=f"approve:{row['id']}",
                    ),
                    InlineKeyboardButton(
                        "❌ رد",
                        callback_data=f"reject:{row['id']}",
                    ),
                ]
            ]
        )

        await update.message.reply_text(
            f"💸 درخواست #{row['id']}\n\n"
            f"👤 کاربر: {row['user_id']}\n"
            f"💰 مبلغ: {Decimal(str(row['amount']))} TRX\n"
            f"💳 کیف پول: {row['wallet']}",
            reply_markup=keyboard,
        )


# =========================================================
# APPROVE / REJECT
# =========================================================

async def withdrawal_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query

    if not is_admin(query.from_user.id):
        await query.answer(
            "دسترسی ندارید.",
            show_alert=True,
        )
        return

    await query.answer()

    action, raw_id = query.data.split(":")
    withdrawal_id = int(raw_id)

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM withdrawals
                WHERE id = %s
                FOR UPDATE
                """,
                (withdrawal_id,),
            )

            withdrawal = cur.fetchone()

            if not withdrawal:
                await query.message.reply_text(
                    "❌ درخواست پیدا نشد."
                )
                return

            if withdrawal["status"] != "pending":
                await query.message.reply_text(
                    "⚠️ این درخواست قبلاً بررسی شده."
                )
                return

            if action == "approve":

                cur.execute(
                    """
                    UPDATE withdrawals
                    SET status = 'approved',
                        processed_at = NOW()
                    WHERE id = %s
                    """,
                    (withdrawal_id,),
                )

            else:

                cur.execute(
                    """
                    UPDATE users
                    SET balance = balance + %s
                    WHERE id = %s
                    """,
                    (
                        withdrawal["amount"],
                        withdrawal["user_id"],
                    ),
                )

                cur.execute(
                    """
                    UPDATE withdrawals
                    SET status = 'rejected',
                        processed_at = NOW()
                    WHERE id = %s
                    """,
                    (withdrawal_id,),
                )

        conn.commit()

    amount = Decimal(
        str(withdrawal["amount"])
    )

    if action == "approve":

        await query.message.edit_text(
            f"✅ درخواست #{withdrawal_id} تایید شد."
        )

        try:
            await context.bot.send_message(
                chat_id=withdrawal["user_id"],
                text=(
                    "✅ برداشت شما تأیید شد.\n\n"
                    f"مبلغ: {amount} TRX"
                ),
            )

            await context.bot.send_message(
                chat_id=PAYMENT_CHANNEL,
                text=(
                    "💸 برداشت تایید شد\n\n"
                    f"👤 User ID: {withdrawal['user_id']}\n"
                    f"💰 Amount: {amount} TRX"
                ),
            )

        except Exception as e:
            logger.error(
                "Notification error: %s",
                e,
            )

    else:

        await query.message.edit_text(
            f"❌ درخواست #{withdrawal_id} رد شد."
        )

        try:
            await context.bot.send_message(
                chat_id=withdrawal["user_id"],
                text=(
                    "❌ درخواست برداشت شما رد شد.\n\n"
                    "مبلغ به موجودی شما برگشت داده شد."
                ),
            )

        except Exception as e:
            logger.error(
                "Reject notification error: %s",
                e,
            )


# =========================================================
# CHANNEL ADMIN
# =========================================================

async def channels_admin(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    channels = get_required_channels()

    text = "📢 کانال‌های اجباری\n\n"

    if channels:
        for i, channel in enumerate(channels, 1):
            text += f"{i}. {channel}\n"
    else:
        text += "هیچ کانالی ثبت نشده.\n"

    text += (
        "\n➕ اضافه کردن:\n"
        "/addchannel @channel\n\n"
        "➖ حذف کردن:\n"
        "/removechannel @channel"
    )

    await update.message.reply_text(text)


async def add_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "مثال:\n/addchannel @mychannel"
        )
        return

    channel = context.args[0]

    if not channel.startswith("@"):
        channel = "@" + channel

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO required_channels(channel)
                VALUES (%s)
                ON CONFLICT (channel) DO NOTHING
                """,
                (channel,),
            )
        conn.commit()

    await update.message.reply_text(
        f"✅ {channel} اضافه شد."
    )


async def remove_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "مثال:\n/removechannel @mychannel"
        )
        return

    channel = context.args[0]

    if not channel.startswith("@"):
        channel = "@" + channel

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                DELETE FROM required_channels
                WHERE channel = %s
                """,
                (channel,),
            )
        conn.commit()

    await update.message.reply_text(
        f"🗑 {channel} حذف شد."
    )


# =========================================================
# TEXT
# =========================================================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = update.message.text

    if context.user_data.get("withdraw_step"):
        await withdrawal_text(
            update,
            context,
        )
        return

    if text == "💰 موجودی":
        await balance_handler(
            update,
            context,
        )
        return

    if text == "👥 رفرالگیری":
        await referral_handler(
            update,
            context,
        )
        return

    if text == "💸 برداشت":
        await withdraw_handler(
            update,
            context,
        )
        return

    if text == "📋 برداشت های تایید شده":
        await approved_handler(
            update,
            context,
        )
        return

    if text == "📊 آمار کاربران":
        await admin_stats(
            update,
            context,
        )
        return

    if text == "💰 موجودی کاربران":
        await admin_balances(
            update,
            context,
        )
        return

    if text == "💸 برداشت‌های در انتظار":
        await admin_pending(
            update,
            context,
        )
        return

    if text == "📢 کانال‌های اجباری":
        await channels_admin(
            update,
            context,
        )
        return


# =========================================================
# ERROR
# =========================================================

async def error_handler(
    update,
    context,
):

    logger.error(
        "Unhandled error:",
        exc_info=context.error,
    )


# =========================================================
# MAIN
# =========================================================

def main():

    logger.info("Starting Telegram bot...")

    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    # /start
    app.add_handler(
        CommandHandler(
            "start",
            start,
        )
    )

    # /admin
    app.add_handler(
        CommandHandler(
            "admin",
            admin_handler,
        )
    )

    # Admin channels
    app.add_handler(
        CommandHandler(
            "addchannel",
            add_channel,
        )
    )

    app.add_handler(
        CommandHandler(
            "removechannel",
            remove_channel,
        )
    )

    # Contact
    app.add_handler(
        MessageHandler(
            filters.CONTACT,
            contact_handler,
        )
    )

    # Membership
    app.add_handler(
        CallbackQueryHandler(
            membership_callback,
            pattern=r"^check_membership$",
        )
    )

    # Withdrawal approve/reject
    app.add_handler(
        CallbackQueryHandler(
            withdrawal_action,
            pattern=r"^(approve|reject):\d+$",
        )
    )

    # Normal text
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    app.add_error_handler(
        error_handler
    )

    logger.info("BOT STARTED")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
