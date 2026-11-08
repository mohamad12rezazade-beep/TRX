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
    ReplyKeyboardRemove,
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

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN")
OWNER_ID_RAW = os.getenv("OWNER_ID")
DATABASE_URL = os.getenv("DATABASE_URL")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not OWNER_ID_RAW:
    raise RuntimeError("OWNER_ID is missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")

OWNER_ID = int(OWNER_ID_RAW)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)

logger = logging.getLogger(__name__)

MIN_WITHDRAW = Decimal("2")
REFERRAL_REWARD = Decimal("0.2")

PAYMENT_CHANNEL = "@BET_1XZX"

REQUIRED_CHANNELS = [
    "@BET_1XZX",
    "@GAP_BAZIN1",
]


# =========================
# DATABASE
# =========================

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

            for channel in REQUIRED_CHANNELS:
                cur.execute(
                    """
                    INSERT INTO required_channels(channel)
                    VALUES (%s)
                    ON CONFLICT (channel) DO NOTHING
                    """,
                    (channel,),
                )

        conn.commit()


# =========================
# USER FUNCTIONS
# =========================

def get_user(user_id: int):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE id = %s",
                (user_id,),
            )
            return cur.fetchone()


def create_user(user_id: int, referred_by=None):
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


def set_phone(user_id: int, phone: str):
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


def get_balance(user_id: int):
    user = get_user(user_id)

    if not user:
        return Decimal("0")

    return Decimal(str(user["balance"]))


def add_balance(user_id: int, amount: Decimal):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE users
                SET balance = balance + %s
                WHERE id = %s
                """,
                (amount, user_id),
            )
        conn.commit()


# =========================
# REFERRAL
# =========================

def reward_referral_if_needed(user_id: int):
    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT referred_by, referral_rewarded
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )

            user = cur.fetchone()

            if not user:
                return

            if not user["referred_by"]:
                return

            if user["referral_rewarded"]:
                return

            referrer_id = user["referred_by"]

            cur.execute(
                """
                SELECT verified
                FROM users
                WHERE id = %s
                """,
                (user_id,),
            )

            referred = cur.fetchone()

            if not referred or not referred["verified"]:
                return

            cur.execute(
                """
                UPDATE users
                SET balance = balance + %s
                WHERE id = %s
                """,
                (REFERRAL_REWARD, referrer_id),
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


# =========================
# CHANNELS
# =========================

def get_required_channels():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT channel
                FROM required_channels
                ORDER BY id
            """)
            rows = cur.fetchall()

    return [row["channel"] for row in rows]


async def check_membership(bot, user_id: int):
    channels = get_required_channels()

    for channel in channels:
        try:
            member = await bot.get_chat_member(channel, user_id)

            if member.status in (
                ChatMemberStatus.LEFT,
                ChatMemberStatus.KICKED,
            ):
                return False

        except Exception as e:
            logger.error(
                "Membership check failed for %s: %s",
                channel,
                e,
            )
            return False

    return True


def channel_keyboard():
    channels = get_required_channels()

    buttons = []

    for channel in channels:
        clean = channel.replace("@", "")
        buttons.append([
            InlineKeyboardButton(
                f"عضویت در {channel}",
                url=f"https://t.me/{clean}",
            )
        ])

    buttons.append([
        InlineKeyboardButton(
            "✅ بررسی عضویت",
            callback_data="check_membership",
        )
    ])

    return InlineKeyboardMarkup(buttons)


# =========================
# MAIN KEYBOARD
# =========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💸 برداشت", "👥 رفرالگیری"],
            ["💰 موجودی", "📋 برداشت های تایید شده"],
        ],
        resize_keyboard=True,
    )


# =========================
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user = update.effective_user

    if not user:
        return

    referred_by = None

    if context.args:
        try:
            ref_id = int(context.args[0])

            if ref_id != user.id:
                referred_by = ref_id

        except ValueError:
            pass

    create_user(user.id, referred_by)

    current = get_user(user.id)

    if current and current["verified"]:
        if await check_membership(context.bot, user.id):
            await update.message.reply_text(
                "به ربات خوش اومدی 👋",
                reply_markup=main_keyboard(),
            )
            return

    await update.message.reply_text(
        "سلام 👋\n\n"
        "برای استفاده از ربات ابتدا شماره موبایل ایرانیت رو ارسال کن.",
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
            one_time_keyboard=True,
        ),
    )


# =========================
# PHONE
# =========================

def normalize_iran_phone(phone: str):
    phone = phone.replace(" ", "").replace("-", "")

    if phone.startswith("+98"):
        if re.fullmatch(r"\+989\d{9}", phone):
            return phone
        return None

    if phone.startswith("0098"):
        if re.fullmatch(r"00989\d{9}", phone):
            return "+98" + phone[4:]
        return None

    if phone.startswith("09"):
        if re.fullmatch(r"09\d{9}", phone):
            return "+98" + phone[1:]
        return None

    return None


async def handle_contact(update: Update, context: ContextTypes.DEFAULT_TYPE):

    contact = update.message.contact
    user = update.effective_user

    if contact.user_id and contact.user_id != user.id:
        await update.message.reply_text(
            "❌ لطفاً شماره خودت رو ارسال کن."
        )
        return

    phone = normalize_iran_phone(contact.phone_number)

    if not phone:
        await update.message.reply_text(
            "❌ فقط شماره موبایل ایران (+98) قابل قبول است."
        )
        return

    create_user(user.id)
    set_phone(user.id, phone)

    if not await check_membership(context.bot, user.id):
        await update.message.reply_text(
            "شماره تأیید شد ✅\n\n"
            "حالا عضو کانال‌های زیر شو و سپس روی بررسی عضویت بزن:",
            reply_markup=channel_keyboard(),
        )
        return

    reward_referral_if_needed(user.id)

    await update.message.reply_text(
        "احراز هویت انجام شد ✅",
        reply_markup=main_keyboard(),
    )


# =========================
# CHECK MEMBERSHIP
# =========================

async def check_membership_callback(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    user_id = query.from_user.id

    user = get_user(user_id)

    if not user or not user["verified"]:
        await query.message.reply_text(
            "ابتدا شماره موبایل ایرانیت رو ارسال کن."
        )
        return

    if not await check_membership(context.bot, user_id):
        await query.message.reply_text(
            "❌ هنوز عضو همه کانال‌های لازم نیستی.",
            reply_markup=channel_keyboard(),
        )
        return

    reward_referral_if_needed(user_id)

    await query.message.reply_text(
        "عضویت تأیید شد ✅",
        reply_markup=main_keyboard(),
    )


# =========================
# BALANCE
# =========================

async def balance(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    user = get_user(user_id)

    if not user or not user["verified"]:
        await update.message.reply_text(
            "ابتدا /start رو بزن و احراز هویت کن."
        )
        return

    amount = get_balance(user_id)

    await update.message.reply_text(
        f"💰 موجودی شما:\n\n"
        f"{amount:.8f} TRX"
    )


# =========================
# REFERRAL
# =========================

async def referral(update: Update, context: ContextTypes.DEFAULT_TYPE):

    user_id = update.effective_user.id

    bot = await context.bot.get_me()

    link = f"https://t.me/{bot.username}?start={user_id}"

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
                SELECT COALESCE(
                    SUM(CASE WHEN referral_rewarded THEN 0.2 ELSE 0 END),
                    0
                ) AS total
                FROM users
                WHERE referred_by = %s
                """,
                (user_id,),
            )

            total = Decimal(str(cur.fetchone()["total"]))

    await update.message.reply_text(
        f"👥 رفرال شما:\n\n"
        f"تعداد زیرمجموعه: {count}\n"
        f"پاداش دریافت‌شده: {total:.2f} TRX\n\n"
        f"🔗 لینک دعوت:\n{link}"
    )


# =========================
# WITHDRAW
# =========================

async def withdraw_start(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    user = get_user(user_id)

    if not user or not user["verified"]:
        await update.message.reply_text(
            "ابتدا احراز هویت کن."
        )
        return

    balance = get_balance(user_id)

    if balance < MIN_WITHDRAW:
        await update.message.reply_text(
            f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است.\n\n"
            f"موجودی شما: {balance:.8f} TRX"
        )
        return

    context.user_data["withdraw_step"] = "amount"

    await update.message.reply_text(
        f"موجودی: {balance:.8f} TRX\n\n"
        "مقدار برداشت را به TRX وارد کن:"
    )


async def handle_withdraw_text(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    step = context.user_data.get("withdraw_step")

    if step == "amount":

        try:
            amount = Decimal(update.message.text.strip())

        except InvalidOperation:
            await update.message.reply_text(
                "❌ مقدار نامعتبر است."
            )
            return

        balance = get_balance(update.effective_user.id)

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
            "آدرس کیف پول TRON خودت رو ارسال کن:"
        )
        return

    if step == "wallet":

        wallet = update.message.text.strip()

        if not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", wallet):
            await update.message.reply_text(
                "❌ آدرس TRON نامعتبر است."
            )
            return

        amount = Decimal(
            context.user_data["withdraw_amount"]
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

                row = cur.fetchone()

                if not row:
                    await update.message.reply_text(
                        "❌ کاربر پیدا نشد."
                    )
                    return

                balance = Decimal(str(row["balance"]))

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
                    (amount, user_id),
                )

                cur.execute(
                    """
                    INSERT INTO withdrawals(
                        user_id,
                        amount,
                        wallet,
                        status
                    )
                    VALUES (%s, %s, %s, 'pending')
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
            f"مقدار: {amount} TRX\n"
            f"شماره درخواست: #{withdrawal_id}\n\n"
            "بعد از بررسی ادمین نتیجه اعلام می‌شود.",
            reply_markup=main_keyboard(),
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

        try:
            await context.bot.send_message(
                chat_id=OWNER_ID,
                text=(
                    "💸 درخواست برداشت جدید\n\n"
                    f"کاربر: {user_id}\n"
                    f"مقدار: {amount} TRX\n"
                    f"کیف پول: {wallet}\n"
                    f"درخواست: #{withdrawal_id}"
                ),
                reply_markup=keyboard,
            )
        except Exception as e:
            logger.error("Cannot send admin message: %s", e)


# =========================
# ADMIN
# =========================

def is_admin(user_id: int):
    return user_id == OWNER_ID


async def admin_command(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        await update.message.reply_text("❌ دسترسی ندارید.")
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

            cur.execute("""
                SELECT COUNT(*) AS total
                FROM users
            """)
            total = cur.fetchone()["total"]

            cur.execute("""
                SELECT COUNT(*) AS verified
                FROM users
                WHERE verified = TRUE
            """)
            verified = cur.fetchone()["verified"]

            cur.execute("""
                SELECT COUNT(*) AS referrals
                FROM users
                WHERE referred_by IS NOT NULL
            """)
            referrals = cur.fetchone()["referrals"]

            cur.execute("""
                SELECT COALESCE(SUM(balance), 0) AS balance
                FROM users
            """)
            balance = Decimal(
                str(cur.fetchone()["balance"])
            )

            cur.execute("""
                SELECT COUNT(*) AS pending
                FROM withdrawals
                WHERE status = 'pending'
            """)
            pending = cur.fetchone()["pending"]

    await update.message.reply_text(
        "📊 آمار کاربران\n\n"
        f"👤 کل کاربران: {total}\n"
        f"✅ احراز شده: {verified}\n"
        f"👥 رفرال‌ها: {referrals}\n"
        f"💰 مجموع موجودی: {balance:.8f} TRX\n"
        f"💸 برداشت‌های در انتظار: {pending}"
    )


async def admin_balances(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT
                    id,
                    phone,
                    balance,
                    verified
                FROM users
                ORDER BY balance DESC
                LIMIT 50
            """)

            users = cur.fetchall()

    if not users:
        await update.message.reply_text(
            "هیچ کاربری ثبت نشده."
        )
        return

    text = "💰 موجودی کاربران\n\n"

    for user in users:
        text += (
            f"ID: {user['id']}\n"
            f"شماره: {user['phone'] or '-'}\n"
            f"موجودی: {Decimal(str(user['balance'])):.8f} TRX\n"
            f"تأیید: {'✅' if user['verified'] else '❌'}\n"
            f"────────────\n"
        )

    await update.message.reply_text(text)


async def pending_withdrawals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT *
                FROM withdrawals
                WHERE status = 'pending'
                ORDER BY id DESC
                LIMIT 20
            """)

            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "برداشت در انتظاری وجود ندارد."
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
            f"کاربر: {row['user_id']}\n"
            f"مقدار: {Decimal(str(row['amount']))} TRX\n"
            f"کیف پول: {row['wallet']}",
            reply_markup=keyboard,
        )


# =========================
# APPROVE / REJECT
# =========================

async def withdrawal_action(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    query = update.callback_query
    await query.answer()

    if not is_admin(query.from_user.id):
        await query.answer(
            "دسترسی ندارید.",
            show_alert=True,
        )
        return

    action, withdrawal_id = query.data.split(":")
    withdrawal_id = int(withdrawal_id)

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute(
                """
                SELECT *
                FROM withdrawals
                WHERE id = %s
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

                status_text = "approved"

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

                status_text = "rejected"

        conn.commit()

    if status_text == "approved":

        await query.message.edit_text(
            f"✅ درخواست #{withdrawal_id} تایید شد."
        )

        try:
            await context.bot.send_message(
                chat_id=withdrawal["user_id"],
                text=(
                    "✅ درخواست برداشت شما تایید شد.\n\n"
                    f"مبلغ: {Decimal(str(withdrawal['amount']))} TRX"
                ),
            )

            await context.bot.send_message(
                chat_id=PAYMENT_CHANNEL,
                text=(
                    "💸 برداشت تایید شد\n\n"
                    f"👤 User ID: {withdrawal['user_id']}\n"
                    f"💰 Amount: {Decimal(str(withdrawal['amount']))} TRX"
                ),
            )

        except Exception as e:
            logger.error(
                "Could not notify approved withdrawal: %s",
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
                "Could not notify rejected withdrawal: %s",
                e,
            )


# =========================
# APPROVED WITHDRAWALS
# =========================

async def approved_withdrawals(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    user_id = update.effective_user.id

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT id, amount, created_at
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


# =========================
# REQUIRED CHANNELS ADMIN
# =========================

async def admin_channels(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    channels = get_required_channels()

    if not channels:
        text = "📢 هیچ کانال اجباری ثبت نشده."
    else:
        text = "📢 کانال‌های اجباری:\n\n"

        for i, channel in enumerate(channels, 1):
            text += f"{i}. {channel}\n"

    text += (
        "\nبرای اضافه کردن:\n"
        "/addchannel @channel\n\n"
        "برای حذف:\n"
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
            "مثال:\n/addchannel @channel"
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
        f"✅ کانال {channel} اضافه شد."
    )


async def remove_channel(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    if not is_admin(update.effective_user.id):
        return

    if not context.args:
        await update.message.reply_text(
            "مثال:\n/removechannel @channel"
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
        f"🗑 کانال {channel} حذف شد."
    )


# =========================
# TEXT HANDLER
# =========================

async def text_handler(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):

    text = update.message.text

    if context.user_data.get("withdraw_step"):
        await handle_withdraw_text(
            update,
            context,
        )
        return

    if text == "💰 موجودی":
        await balance(update, context)
        return

    if text == "👥 رفرالگیری":
        await referral(update, context)
        return

    if text == "💸 برداشت":
        await withdraw_start(update, context)
        return

    if text == "📋 برداشت های تایید شده":
        await approved_withdrawals(update, context)
        return

    if text == "📊 آمار کاربران":
        await admin_stats(update, context)
        return

    if text == "💰 موجودی کاربران":
        await admin_balances(update, context)
        return

    if text == "💸 برداشت‌های در انتظار":
        await pending_withdrawals(update, context)
        return

    if text == "📢 کانال‌های اجباری":
        await admin_channels(update, context)
        return


# =========================
# ERROR
# =========================

async def error_handler(
    update: object,
    context: ContextTypes.DEFAULT_TYPE,
):

    logger.exception(
        "Unhandled exception",
        exc_info=context.error,
    )


# =========================
# MAIN
# =========================

def main():

    logger.info("Starting bot...")

    init_db()

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .build()
    )

    application.add_handler(
        CommandHandler("start", start)
    )

    application.add_handler(
        CommandHandler("admin", admin_command)
    )

    application.add_handler(
        CommandHandler("addchannel", add_channel)
    )

    application.add_handler(
        CommandHandler("removechannel", remove_channel)
    )

    application.add_handler(
        MessageHandler(
            filters.CONTACT,
            handle_contact,
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            check_membership_callback,
            pattern="^check_membership$",
        )
    )

    application.add_handler(
        CallbackQueryHandler(
            withdrawal_action,
            pattern=r"^(approve|reject):\d+$",
        )
    )

    application.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler,
        )
    )

    application.add_error_handler(error_handler)

    logger.info("Bot is running...")

    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
