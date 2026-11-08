import os
import logging
from decimal import Decimal
from datetime import datetime

import psycopg
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

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
OWNER_ID = int(os.getenv("OWNER_ID", "0"))

MIN_WITHDRAW = Decimal("2")
REFERRAL_REWARD = Decimal("0.2")

REQUIRED_CHANNELS = [
    "@BET_1XZX",
    "@GAP_BAZIN1",
]

PAYMENT_CHANNEL = "@BET_1XZX"

logging.basicConfig(level=logging.INFO)


def db():
    return psycopg.connect(DATABASE_URL)


def init_db():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    id BIGINT PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    phone TEXT,
                    phone_verified BOOLEAN DEFAULT FALSE,
                    balance NUMERIC(18,6) DEFAULT 0,
                    referred_by BIGINT,
                    referral_rewarded BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id BIGSERIAL PRIMARY KEY,
                    user_id BIGINT NOT NULL,
                    amount NUMERIC(18,6) NOT NULL,
                    wallet TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    admin_id BIGINT,
                    created_at TIMESTAMP DEFAULT NOW(),
                    processed_at TIMESTAMP
                )
            """)

            conn.commit()


def get_user(user_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE id=%s",
                (user_id,)
            )
            return cur.fetchone()


def create_user(user):
    if get_user(user.id):
        return

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users
                (id, username, first_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (id) DO NOTHING
            """, (
                user.id,
                user.username,
                user.first_name
            ))
            conn.commit()


def update_user(user_id, **fields):
    if not fields:
        return

    keys = list(fields.keys())
    values = list(fields.values())

    query = (
        "UPDATE users SET " +
        ", ".join(f"{k}=%s" for k in keys) +
        " WHERE id=%s"
    )

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(query, values + [user_id])
            conn.commit()


def keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💰 برداشت", "👥 رفرالگیری"],
            ["💳 موجودی", "✅ برداشت‌های تایید شده"]
        ],
        resize_keyboard=True
    )


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    create_user(user)

    if context.args:
        try:
            ref = int(context.args[0])

            if ref != user.id:
                current = get_user(user.id)

                if current and not current[6]:
                    update_user(
                        user.id,
                        referred_by=ref
                    )
        except Exception:
            pass

    contact_button = KeyboardButton(
        "📱 ارسال شماره",
        request_contact=True
    )

    markup = ReplyKeyboardMarkup(
        [[contact_button]],
        resize_keyboard=True
    )

    await update.message.reply_text(
        "سلام 👋\n\n"
        "برای شروع شماره ایران خودت رو ارسال کن.",
        reply_markup=markup
    )


async def phone_handler(update: Update, context):
    contact = update.message.contact

    if not contact:
        return

    if contact.user_id != update.effective_user.id:
        await update.message.reply_text(
            "❌ شماره خودت رو ارسال کن."
        )
        return

    phone = contact.phone_number

    if not phone.startswith("+98"):
        await update.message.reply_text(
            "❌ فقط شماره ایران (+98) قابل قبول است."
        )
        return

    update_user(
        update.effective_user.id,
        phone=phone,
        phone_verified=True,
        username=update.effective_user.username,
        first_name=update.effective_user.first_name
    )

    for channel in REQUIRED_CHANNELS:
        try:
            member = await context.bot.get_chat_member(
                channel,
                update.effective_user.id
            )

            if member.status in ("left", "kicked"):
                await update.message.reply_text(
                    "❌ ابتدا عضو کانال‌های زیر شو:\n\n"
                    "https://t.me/BET_1XZX\n"
                    "https://t.me/GAP_BAZIN1"
                )
                return

        except Exception:
            await update.message.reply_text(
                f"❌ ربات نمی‌تواند عضویت در {channel} را بررسی کند."
            )
            return

    await reward_referral(update.effective_user.id)

    await update.message.reply_text(
        "✅ تأیید شد.\n\n"
        "به ربات خوش آمدی 👑",
        reply_markup=keyboard()
    )


def reward_referral(user_id):
    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT referred_by, referral_rewarded
                FROM users
                WHERE id=%s
            """, (user_id,))

            row = cur.fetchone()

            if not row:
                return

            referrer, rewarded = row

            if not referrer or rewarded:
                return

            cur.execute("""
                SELECT phone_verified
                FROM users
                WHERE id=%s
            """, (user_id,))

            verified = cur.fetchone()

            if not verified or not verified[0]:
                return

            cur.execute("""
                UPDATE users
                SET balance = balance + %s
                WHERE id=%s
            """, (
                REFERRAL_REWARD,
                referrer
            ))

            cur.execute("""
                UPDATE users
                SET referral_rewarded = TRUE
                WHERE id=%s
            """, (user_id,))

            conn.commit()


async def balance(update: Update, context):
    row = get_user(update.effective_user.id)

    if not row:
        await update.message.reply_text(
            "ابتدا /start را بزن."
        )
        return

    amount = Decimal(str(row[5]))

    await update.message.reply_text(
        f"💳 موجودی:\n\n"
        f"💰 {amount} TRX"
    )


async def referral(update: Update, context):
    bot = await context.bot.get_me()

    link = (
        f"https://t.me/{bot.username}"
        f"?start={update.effective_user.id}"
    )

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT COUNT(*)
                FROM users
                WHERE referred_by=%s
                AND referral_rewarded=TRUE
            """, (update.effective_user.id,))

            count = cur.fetchone()[0]

    total = Decimal(count) * REFERRAL_REWARD

    await update.message.reply_text(
        "👥 رفرالگیری\n\n"
        f"🔗 لینک شما:\n{link}\n\n"
        f"👤 دعوت موفق: {count}\n"
        f"💰 پاداش هر نفر: {REFERRAL_REWARD} TRX\n"
        f"💵 مجموع: {total} TRX"
    )


async def withdraw_start(update: Update, context):
    row = get_user(update.effective_user.id)

    if not row:
        return

    balance = Decimal(str(row[5]))

    if balance < MIN_WITHDRAW:
        await update.message.reply_text(
            f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است.\n"
            f"موجودی شما: {balance} TRX"
        )
        return

    context.user_data["withdraw_step"] = "amount"

    await update.message.reply_text(
        f"مقدار برداشت را وارد کن.\n"
        f"حداقل: {MIN_WITHDRAW} TRX"
    )


async def text_handler(update: Update, context):
    text = update.message.text

    if text == "💳 موجودی":
        await balance(update, context)
        return

    if text == "👥 رفرالگیری":
        await referral(update, context)
        return

    if text == "💰 برداشت":
        await withdraw_start(update, context)
        return

    if text == "✅ برداشت‌های تایید شده":
        await approved(update, context)
        return

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

    step = context.user_data.get("withdraw_step")

    if step == "amount":
        try:
            amount = Decimal(text)
        except:
            await update.message.reply_text(
                "❌ مقدار نامعتبر است."
            )
            return

        row = get_user(update.effective_user.id)
        balance = Decimal(str(row[5]))

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

        context.user_data["amount"] = str(amount)
        context.user_data["withdraw_step"] = "wallet"

        await update.message.reply_text(
            "💳 آدرس کیف پول TRON را ارسال کن."
        )
        return

    if step == "wallet":
        wallet = text
        amount = Decimal(context.user_data["amount"])

        if len(wallet) < 20:
            await update.message.reply_text(
                "❌ آدرس کیف پول نامعتبر است."
            )
            return

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    INSERT INTO withdrawals
                    (user_id, amount, wallet)
                    VALUES (%s, %s, %s)
                    RETURNING id
                """, (
                    update.effective_user.id,
                    amount,
                    wallet
                ))

                withdrawal_id = cur.fetchone()[0]
                conn.commit()

        context.user_data.clear()

        await update.message.reply_text(
            "✅ درخواست برداشت ثبت شد.\n\n"
            f"💰 مبلغ: {amount} TRX\n"
            "⏳ در انتظار بررسی ادمین"
        )

        await context.bot.send_message(
            OWNER_ID,
            f"💸 درخواست برداشت\n\n"
            f"🆔 درخواست: {withdrawal_id}\n"
            f"👤 کاربر: {update.effective_user.id}\n"
            f"💰 مبلغ: {amount} TRX\n"
            f"💳 Wallet: {wallet}",
            reply_markup=InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ تایید",
                        callback_data=f"approve:{withdrawal_id}"
                    ),
                    InlineKeyboardButton(
                        "❌ رد",
                        callback_data=f"reject:{withdrawal_id}"
                    )
                ]
            ])
        )


async def withdrawal_callback(update, context):
    query = update.callback_query
    await query.answer()

    if query.from_user.id != OWNER_ID:
        return

    action, wid = query.data.split(":")
    wid = int(wid)

    with db() as conn:
        with conn.cursor() as cur:

            cur.execute("""
                SELECT user_id, amount, status
                FROM withdrawals
                WHERE id=%s
            """, (wid,))

            row = cur.fetchone()

            if not row:
                await query.edit_message_text(
                    "❌ درخواست پیدا نشد."
                )
                return

            user_id, amount, status = row

            if status != "pending":
                await query.edit_message_text(
                    "⚠️ این درخواست قبلاً بررسی شده."
                )
                return

            if action == "approve":

                cur.execute("""
                    SELECT balance
                    FROM users
                    WHERE id=%s
                """, (user_id,))

                user_balance = Decimal(
                    str(cur.fetchone()[0])
                )

                if user_balance < Decimal(str(amount)):
                    await query.edit_message_text(
                        "❌ موجودی کاربر کافی نیست."
                    )
                    return

                cur.execute("""
                    UPDATE users
                    SET balance = balance - %s
                    WHERE id=%s
                """, (
                    amount,
                    user_id
                ))

                cur.execute("""
                    UPDATE withdrawals
                    SET status='approved',
                        admin_id=%s,
                        processed_at=NOW()
                    WHERE id=%s
                """, (
                    OWNER_ID,
                    wid
                ))

                conn.commit()

                await context.bot.send_message(
                    user_id,
                    f"✅ برداشت شما تایید شد.\n\n"
                    f"💰 مبلغ: {amount} TRX\n"
                    f"وضعیت: واریز شد."
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

                cur.execute("""
                    UPDATE withdrawals
                    SET status='rejected',
                        admin_id=%s,
                        processed_at=NOW()
                    WHERE id=%s
                """, (
                    OWNER_ID,
                    wid
                ))

                conn.commit()

                await context.bot.send_message(
                    user_id,
                    "❌ برداشت شما رد شد."
                )

                await query.edit_message_text(
                    "❌ برداشت رد شد."
                )


async def approved(update, context):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, amount, created_at
                FROM withdrawals
                WHERE user_id=%s
                AND status='approved'
                ORDER BY created_at DESC
                LIMIT 20
            """, (update.effective_user.id,))

            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "هنوز برداشت تایید شده‌ای نداری."
        )
        return

    text = "✅ برداشت‌های تایید شده:\n\n"

    for wid, amount, created_at in rows:
        text += (
            f"🆔 {wid}\n"
            f"💰 {amount} TRX\n"
            f"📅 {created_at}\n\n"
        )

    await update.message.reply_text(text)


async def admin_stats(update, context):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            users = cur.fetchone()[0]

            cur.execute("""
                SELECT COUNT(*)
                FROM withdrawals
                WHERE status='pending'
            """)
            pending = cur.fetchone()[0]

    await update.message.reply_text(
        f"📊 آمار\n\n"
        f"👤 کاربران: {users}\n"
        f"💸 برداشت‌های در انتظار: {pending}"
    )


async def admin_balances(update, context):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, username, balance
                FROM users
                ORDER BY balance DESC
                LIMIT 50
            """)
            rows = cur.fetchall()

    text = "💰 موجودی کاربران:\n\n"

    for uid, username, balance in rows:
        text += (
            f"🆔 {uid}\n"
            f"👤 @{username or 'ندارد'}\n"
            f"💰 {balance} TRX\n\n"
        )

    await update.message.reply_text(text)


async def admin_referrals(update, context):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT referred_by, COUNT(*)
                FROM users
                WHERE referred_by IS NOT NULL
                AND referral_rewarded=TRUE
                GROUP BY referred_by
                ORDER BY COUNT(*) DESC
            """)

            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "رفرال موفقی وجود ندارد."
        )
        return

    text = "👥 رفرال‌ها:\n\n"

    for referrer, count in rows:
        total = Decimal(count) * REFERRAL_REWARD

        text += (
            f"🆔 {referrer}\n"
            f"👥 تعداد: {count}\n"
            f"💰 پاداش: {total} TRX\n\n"
        )

    await update.message.reply_text(text)


async def admin_withdrawals(update, context):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT id, user_id, amount, wallet
                FROM withdrawals
                WHERE status='pending'
                ORDER BY created_at DESC
                LIMIT 20
            """)

            rows = cur.fetchall()

    if not rows:
        await update.message.reply_text(
            "برداشت در انتظاری وجود ندارد."
        )
        return

    for wid, uid, amount, wallet in rows:
        await update.message.reply_text(
            f"🆔 درخواست: {wid}\n"
            f"👤 User: {uid}\n"
            f"💰 مبلغ: {amount} TRX\n"
            f"💳 Wallet: {wallet}"
        )


async def admin_command(update, context):
    if update.effective_user.id != OWNER_ID:
        return

    await update.message.reply_text(
        "پنل مدیریت:",
        reply_markup=ReplyKeyboardMarkup(
            [
                ["📊 آمار کاربران", "💰 موجودی کاربران"],
                ["👥 رفرال‌ها", "💸 برداشت‌ها"]
            ],
            resize_keyboard=True
        )
    )


async def error_handler(update, context):
    logging.error(
        "Bot error: %s",
        context.error
    )


def main():
    init_db()

    app = (
        Application
        .builder()
        .token(BOT_TOKEN)
        .build()
    )

    app.add_handler(
        CommandHandler("start", start)
    )

    app.add_handler(
        CommandHandler("admin", admin_command)
    )

    app.add_handler(
        MessageHandler(
            filters.CONTACT,
            phone_handler
        )
    )

    app.add_handler(
        CallbackQueryHandler(
            withdrawal_callback,
            pattern=r"^(approve|reject):"
        )
    )

    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            text_handler
        )
    )

    app.add_error_handler(error_handler)

    print("Bot started...")

    app.run_polling()


if __name__ == "__main__":
    main()
