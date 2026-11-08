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
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    ConversationHandler,
    filters,
)

# =========================
# CONFIG
# =========================

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0").strip())
DATABASE_URL = os.getenv("DATABASE_URL", "").strip()

MIN_WITHDRAW = Decimal("2.0")
REF_REWARD = Decimal("0.2")

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger("telegram_bot")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN is missing")

if not OWNER_ID:
    raise RuntimeError("OWNER_ID is missing")

if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL is missing")


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
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT UNIQUE NOT NULL,
                    username TEXT,
                    first_name TEXT,
                    phone TEXT,
                    balance NUMERIC(18, 6) NOT NULL DEFAULT 0,
                    referrer_id BIGINT,
                    referral_rewarded BOOLEAN NOT NULL DEFAULT FALSE,
                    verified BOOLEAN NOT NULL DEFAULT FALSE,
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id BIGSERIAL PRIMARY KEY,
                    telegram_id BIGINT NOT NULL,
                    amount NUMERIC(18, 6) NOT NULL,
                    wallet TEXT NOT NULL,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                    processed_at TIMESTAMPTZ
                )
            """)

            cur.execute("""
                CREATE TABLE IF NOT EXISTS required_channels (
                    id BIGSERIAL PRIMARY KEY,
                    channel TEXT UNIQUE NOT NULL
                )
            """)

            # کانال‌های پیش‌فرض
            cur.execute("""
                INSERT INTO required_channels (channel)
                VALUES
                    ('@BET_1XZX'),
                    ('@GAP_BAZIN1')
                ON CONFLICT (channel) DO NOTHING
            """)

        conn.commit()


# =========================
# USER FUNCTIONS
# =========================

def get_user(tg_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT * FROM users WHERE telegram_id = %s",
                (tg_id,),
            )
            return cur.fetchone()


def create_user(tg_id, username, first_name):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                INSERT INTO users
                    (telegram_id, username, first_name)
                VALUES
                    (%s, %s, %s)
                ON CONFLICT (telegram_id)
                DO UPDATE SET
                    username = EXCLUDED.username,
                    first_name = EXCLUDED.first_name
            """, (tg_id, username, first_name))
        conn.commit()


def set_phone(tg_id, phone):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET phone = %s,
                    verified = TRUE
                WHERE telegram_id = %s
            """, (phone, tg_id))
        conn.commit()


def set_referrer(tg_id, referrer_id):
    if tg_id == referrer_id:
        return

    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                UPDATE users
                SET referrer_id = %s
                WHERE telegram_id = %s
                  AND referrer_id IS NULL
            """, (referrer_id, tg_id))
        conn.commit()


def reward_referral(tg_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT referrer_id, referral_rewarded, verified
                FROM users
                WHERE telegram_id = %s
            """, (tg_id,))
            user = cur.fetchone()

            if not user:
                return

            if not user["verified"]:
                return

            if user["referral_rewarded"]:
                return

            referrer_id = user["referrer_id"]

            if not referrer_id:
                return

            cur.execute("""
                UPDATE users
                SET balance = balance + %s
                WHERE telegram_id = %s
            """, (REF_REWARD, referrer_id))

            cur.execute("""
                UPDATE users
                SET referral_rewarded = TRUE
                WHERE telegram_id = %s
            """, (tg_id,))

        conn.commit()


def get_balance(tg_id):
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT balance FROM users WHERE telegram_id = %s",
                (tg_id,),
            )
            row = cur.fetchone()

            if not row:
                return Decimal("0")

            return Decimal(str(row["balance"]))


def get_channels():
    with db() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                SELECT channel
                FROM required_channels
                ORDER BY id
            """)
            return [x["channel"] for x in cur.fetchall()]


# =========================
# HELPERS
# =========================

def main_keyboard():
    return ReplyKeyboardMarkup(
        [
            ["💸 برداشت", "👥 رفرالگیری"],
            ["💰 موجودی", "📋 برداشت های تایید شده"],
        ],
        resize_keyboard=True,
    )


def admin_keyboard():
    return InlineKeyboardMarkup([
        [
            InlineKeyboardButton("📊 آمار کاربران", callback_data="admin_stats"),
            InlineKeyboardButton("💰 موجودی کاربران", callback_data="admin_balances"),
        ],
        [
            InlineKeyboardButton("💸 برداشت‌های در انتظار", callback_data="admin_withdrawals"),
        ],
    ])


def is_admin(tg_id):
    return tg_id == OWNER_ID


def normalize_phone(phone):
    phone = phone.strip()
    phone = phone.replace(" ", "").replace("-", "").replace("(", "").replace(")", "")

    if phone.startswith("0098"):
        phone = "+98" + phone[4:]

    elif phone.startswith("98"):
        phone = "+" + phone

    elif phone.startswith("09"):
        phone = "+98" + phone[1:]

    return phone


def is_iranian_phone(phone):
    return bool(re.fullmatch(r"\+989\d{9}", phone))


def normalize_tron_address(wallet):
    wallet = wallet.strip()

    # TRON addresses معمولاً با T شروع می‌شوند و طول 34 دارند
    if not re.fullmatch(r"T[1-9A-HJ-NP-Za-km-z]{33}", wallet):
        return None

    return wallet


async def check_channels(user_id, bot):
    channels = get_channels()

    if not channels:
        return True

    for channel in channels:
        try:
            member = await bot.get_chat_member(
                chat_id=channel,
                user_id=user_id,
            )

            if member.status in ("left", "kicked"):
                return False

        except Exception as e:
            logger.warning(
                "Channel check failed for %s: %s",
                channel,
                e,
            )
            return False

    return True


def channel_keyboard():
    channels = get_channels()

    buttons = []

    for channel in channels:
        clean = channel.replace("@", "")
        buttons.append([
            InlineKeyboardButton(
                f"📢 عضویت در {channel}",
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
# START
# =========================

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user

    if not user:
        return

    logger.info("START received from %s", user.id)

    create_user(
        user.id,
        user.username,
        user.first_name,
    )

    # referral
    if context.args:
        arg = context.args[0]

        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg.replace("ref_", ""))

                if referrer_id != user.id:
                    set_referrer(user.id, referrer_id)

            except ValueError:
                pass

    u = get_user(user.id)

    if not u["verified"]:
        keyboard = ReplyKeyboardMarkup(
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
        )

        await update.message.reply_text(
            "سلام 👋\n\n"
            "برای استفاده از ربات ابتدا شماره موبایل ایرانیت رو ارسال کن.\n"
            "شماره فقط باید با +98 یا 09 شروع بشه.",
            reply_markup=keyboard,
        )
        return

    if not await check_channels(user.id, context.bot):
        await update.message.reply_text(
            "برای استفاده از ربات ابتدا داخل کانال‌های زیر عضو شو:",
            reply_markup=channel_keyboard(),
        )
        return

    await update.message.reply_text(
        "به ربات خوش اومدی 👋",
        reply_markup=main_keyboard(),
    )


# =========================
# CONTACT
# =========================

async def contact_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    contact = update.message.contact

    if contact.user_id and contact.user_id != user.id:
        await update.message.reply_text(
            "❌ لطفاً شماره خودت رو با دکمه ارسال کن."
        )
        return

    phone = normalize_phone(contact.phone_number)

    if not is_iranian_phone(phone):
        await update.message.reply_text(
            "❌ فقط شماره موبایل ایران (+98) قابل قبول است."
        )
        return

    set_phone(user.id, phone)
    reward_referral(user.id)

    if not await check_channels(user.id, context.bot):
        await update.message.reply_text(
            "شماره تأیید شد ✅\n\n"
            "حالا داخل کانال‌های زیر عضو شو:",
            reply_markup=channel_keyboard(),
        )
        return

    await update.message.reply_text(
        "شماره با موفقیت تأیید شد ✅",
        reply_markup=main_keyboard(),
    )


# =========================
# MEMBERSHIP
# =========================

async def check_membership(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    if await check_channels(query.from_user.id, context.bot):
        await query.message.reply_text(
            "عضویتت تأیید شد ✅",
            reply_markup=main_keyboard(),
        )
    else:
        await query.message.reply_text(
            "هنوز عضویت کامل نیست ❌",
            reply_markup=channel_keyboard(),
        )


# =========================
# MAIN MENU
# =========================

async def menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    text = update.message.text

    if not user:
        return

    db_user = get_user(user.id)

    if not db_user or not db_user["verified"]:
        await update.message.reply_text(
            "اول /start رو بزن و شماره موبایلت رو تأیید کن."
        )
        return

    if not await check_channels(user.id, context.bot):
        await update.message.reply_text(
            "اول داخل کانال‌های اجباری عضو شو.",
            reply_markup=channel_keyboard(),
        )
        return

    if text == "💰 موجودی":
        balance = get_balance(user.id)

        await update.message.reply_text(
            f"💰 موجودی شما:\n\n"
            f"{balance:.2f} TRX"
        )

    elif text == "👥 رفرالگیری":
        bot = await context.bot.get_me()

        link = f"https://t.me/{bot.username}?start=ref_{user.id}"

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT COUNT(*) AS count
                    FROM users
                    WHERE referrer_id = %s
                      AND referral_rewarded = TRUE
                """, (user.id,))

                count = cur.fetchone()["count"]

        await update.message.reply_text(
            "👥 سیستم رفرال\n\n"
            f"تعداد دعوت‌های موفق: {count}\n"
            f"پاداش هر نفر: {REF_REWARD} TRX\n\n"
            f"لینک دعوت شما:\n{link}"
        )

    elif text == "💸 برداشت":
        balance = get_balance(user.id)

        if balance < MIN_WITHDRAW:
            await update.message.reply_text(
                f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است.\n\n"
                f"موجودی شما: {balance:.2f} TRX"
            )
            return

        context.user_data["withdraw_step"] = "amount"

        await update.message.reply_text(
            f"💸 برداشت TRX\n\n"
            f"موجودی: {balance:.2f} TRX\n"
            f"حداقل برداشت: {MIN_WITHDRAW} TRX\n\n"
            f"مقدار برداشت را به TRX وارد کن:",
            reply_markup=ReplyKeyboardRemove(),
        )

    elif text == "📋 برداشت های تایید شده":
        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT amount, created_at
                    FROM withdrawals
                    WHERE telegram_id = %s
                      AND status = 'approved'
                    ORDER BY id DESC
                    LIMIT 20
                """, (user.id,))

                rows = cur.fetchall()

        if not rows:
            await update.message.reply_text(
                "هنوز برداشت تأیید شده‌ای نداری."
            )
            return

        msg = "📋 برداشت‌های تایید شده:\n\n"

        for row in rows:
            msg += f"💸 {Decimal(str(row['amount'])):.2f} TRX\n"

        await update.message.reply_text(msg)


# =========================
# WITHDRAWAL AMOUNT
# =========================

async def withdrawal_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("withdraw_step") != "amount":
        return

    user = update.effective_user
    text = update.message.text.strip()

    try:
        amount = Decimal(text)
    except InvalidOperation:
        await update.message.reply_text(
            "❌ مقدار نامعتبره. مثلاً 2 یا 5.5 وارد کن."
        )
        return

    if amount < MIN_WITHDRAW:
        await update.message.reply_text(
            f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است."
        )
        return

    balance = get_balance(user.id)

    if amount > balance:
        await update.message.reply_text(
            f"❌ موجودی کافی نیست.\n\n"
            f"موجودی: {balance:.2f} TRX"
        )
        return

    context.user_data["withdraw_amount"] = str(amount)
    context.user_data["withdraw_step"] = "wallet"

    await update.message.reply_text(
        "حالا آدرس کیف پول TRON رو ارسال کن:\n\n"
        "آدرس باید با T شروع بشه."
    )


# =========================
# WITHDRAWAL WALLET
# =========================

async def withdrawal_wallet(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if context.user_data.get("withdraw_step") != "wallet":
        return

    user = update.effective_user
    wallet = normalize_tron_address(update.message.text)

    if not wallet:
        await update.message.reply_text(
            "❌ آدرس TRON معتبر نیست.\n"
            "لطفاً آدرس صحیح کیف پول TRON رو بفرست."
        )
        return

    amount = Decimal(context.user_data["withdraw_amount"])
    balance = get_balance(user.id)

    if amount > balance:
        await update.message.reply_text(
            "❌ موجودی کافی نیست."
        )
        context.user_data.clear()
        return

    with db() as conn:
        with conn.cursor() as cur:

            # دوباره موجودی را چک می‌کنیم
            cur.execute("""
                SELECT balance
                FROM users
                WHERE telegram_id = %s
                FOR UPDATE
            """, (user.id,))

            row = cur.fetchone()

            if not row:
                await update.message.reply_text("❌ کاربر پیدا نشد.")
                return

            current_balance = Decimal(str(row["balance"]))

            if amount > current_balance:
                await update.message.reply_text(
                    "❌ موجودی کافی نیست."
                )
                return

            # رزرو مبلغ
            cur.execute("""
                UPDATE users
                SET balance = balance - %s
                WHERE telegram_id = %s
            """, (amount, user.id))

            cur.execute("""
                INSERT INTO withdrawals
                    (telegram_id, amount, wallet)
                VALUES
                    (%s, %s, %s)
                RETURNING id
            """, (user.id, amount, wallet))

            withdrawal_id = cur.fetchone()["id"]

        conn.commit()

    context.user_data.clear()

    await update.message.reply_text(
        f"درخواست برداشت ثبت شد ✅\n\n"
        f"مبلغ: {amount:.2f} TRX\n"
        f"شماره درخواست: #{withdrawal_id}\n\n"
        f"بعد از بررسی ادمین نتیجه اعلام میشه.",
        reply_markup=main_keyboard(),
    )

    username = (
        f"@{user.username}"
        if user.username
        else "بدون یوزرنیم"
    )

    admin_text = (
        "🚨 درخواست برداشت جدید\n\n"
        f"🆔 User ID: {user.id}\n"
        f"👤 Username: {username}\n"
        f"💸 Amount: {amount:.2f} TRX\n"
        f"🧾 Request: #{withdrawal_id}\n"
        f"💼 Wallet: {wallet}"
    )

    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton(
                "✅ تایید",
                callback_data=f"approve_{withdrawal_id}",
            ),
            InlineKeyboardButton(
                "❌ رد",
                callback_data=f"reject_{withdrawal_id}",
            ),
        ]
    ])

    try:
        await context.bot.send_message(
            OWNER_ID,
            admin_text,
            reply_markup=keyboard,
        )
    except Exception as e:
        logger.error("Cannot send admin withdrawal message: %s", e)


# =========================
# ADMIN CALLBACK
# =========================

async def admin_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        await query.answer("دسترسی ندارید.", show_alert=True)
        return

    await query.answer()

    data = query.data

    # ---------- APPROVE ----------

    if data.startswith("approve_"):
        withdrawal_id = int(data.split("_")[1])

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT *
                    FROM withdrawals
                    WHERE id = %s
                    FOR UPDATE
                """, (withdrawal_id,))

                w = cur.fetchone()

                if not w:
                    await query.message.reply_text(
                        "❌ درخواست پیدا نشد."
                    )
                    return

                if w["status"] != "pending":
                    await query.message.reply_text(
                        "⚠️ این درخواست قبلاً بررسی شده."
                    )
                    return

                cur.execute("""
                    UPDATE withdrawals
                    SET status = 'approved',
                        processed_at = NOW()
                    WHERE id = %s
                """, (withdrawal_id,))

            conn.commit()

        await query.message.edit_reply_markup(reply_markup=None)

        await query.message.reply_text(
            f"✅ برداشت #{withdrawal_id} تایید شد."
        )

        try:
            await context.bot.send_message(
                w["telegram_id"],
                f"✅ برداشت شما تایید شد.\n\n"
                f"💸 مبلغ: {Decimal(str(w['amount'])):.2f} TRX\n"
                f"واریز شد.",
            )

            await context.bot.send_message(
                "@BET_1XZX",
                "💸 برداشت تایید شد\n\n"
                f"🆔 User ID: {w['telegram_id']}\n"
                f"💰 Amount: {Decimal(str(w['amount'])):.2f} TRX",
            )

        except Exception as e:
            logger.error("Approve notification error: %s", e)

    # ---------- REJECT ----------

    elif data.startswith("reject_"):
        withdrawal_id = int(data.split("_")[1])

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT *
                    FROM withdrawals
                    WHERE id = %s
                    FOR UPDATE
                """, (withdrawal_id,))

                w = cur.fetchone()

                if not w:
                    await query.message.reply_text(
                        "❌ درخواست پیدا نشد."
                    )
                    return

                if w["status"] != "pending":
                    await query.message.reply_text(
                        "⚠️ این درخواست قبلاً بررسی شده."
                    )
                    return

                cur.execute("""
                    UPDATE withdrawals
                    SET status = 'rejected',
                        processed_at = NOW()
                    WHERE id = %s
                """, (withdrawal_id,))

                # برگشت موجودی
                cur.execute("""
                    UPDATE users
                    SET balance = balance + %s
                    WHERE telegram_id = %s
                """, (
                    w["amount"],
                    w["telegram_id"],
                ))

            conn.commit()

        await query.message.edit_reply_markup(reply_markup=None)

        await query.message.reply_text(
            f"❌ برداشت #{withdrawal_id} رد شد."
        )

        try:
            await context.bot.send_message(
                w["telegram_id"],
                f"❌ درخواست برداشت شما رد شد.\n\n"
                f"💸 مبلغ {Decimal(str(w['amount'])):.2f} TRX "
                f"به موجودی شما برگشت داده شد.",
            )

        except Exception as e:
            logger.error("Reject notification error: %s", e)


# =========================
# ADMIN PANEL
# =========================

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        await update.message.reply_text("❌ دسترسی ندارید.")
        return

    await update.message.reply_text(
        "👑 پنل مدیریت",
        reply_markup=admin_keyboard(),
    )


async def admin_buttons(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query

    if query.from_user.id != OWNER_ID:
        await query.answer("دسترسی ندارید.", show_alert=True)
        return

    await query.answer()

    if query.data == "admin_stats":

        with db() as conn:
            with conn.cursor() as cur:

                cur.execute("SELECT COUNT(*) AS c FROM users")
                users = cur.fetchone()["c"]

                cur.execute("""
                    SELECT COUNT(*) AS c
                    FROM users
                    WHERE verified = TRUE
                """)
                verified = cur.fetchone()["c"]

                cur.execute("""
                    SELECT COALESCE(SUM(balance), 0) AS total
                    FROM users
                """)
                total = Decimal(
                    str(cur.fetchone()["total"])
                )

                cur.execute("""
                    SELECT COUNT(*) AS c
                    FROM withdrawals
                    WHERE status = 'pending'
                """)
                pending = cur.fetchone()["c"]

        await query.message.reply_text(
            "📊 آمار ربات\n\n"
            f"👤 کل کاربران: {users}\n"
            f"✅ کاربران تأیید شده: {verified}\n"
            f"💰 مجموع موجودی: {total:.2f} TRX\n"
            f"⏳ برداشت در انتظار: {pending}"
        )

    elif query.data == "admin_balances":

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT telegram_id, username, balance
                    FROM users
                    ORDER BY balance DESC
                    LIMIT 50
                """)

                rows = cur.fetchall()

        if not rows:
            await query.message.reply_text("کاربری وجود ندارد.")
            return

        msg = "💰 موجودی کاربران:\n\n"

        for r in rows:
            username = (
                f"@{r['username']}"
                if r["username"]
                else "-"
            )

            msg += (
                f"🆔 {r['telegram_id']}\n"
                f"👤 {username}\n"
                f"💰 {Decimal(str(r['balance'])):.2f} TRX\n\n"
            )

        await query.message.reply_text(msg)

    elif query.data == "admin_withdrawals":

        with db() as conn:
            with conn.cursor() as cur:
                cur.execute("""
                    SELECT *
                    FROM withdrawals
                    WHERE status = 'pending'
                    ORDER BY id ASC
                    LIMIT 30
                """)

                rows = cur.fetchall()

        if not rows:
            await query.message.reply_text(
                "✅ برداشت در انتظاری وجود ندارد."
            )
            return

        for w in rows:
            keyboard = InlineKeyboardMarkup([
                [
                    InlineKeyboardButton(
                        "✅ تایید",
                        callback_data=f"approve_{w['id']}",
                    ),
                    InlineKeyboardButton(
                        "❌ رد",
                        callback_data=f"reject_{w['id']}",
                    ),
                ]
            ])

            await query.message.reply_text(
                f"🧾 درخواست #{w['id']}\n\n"
                f"🆔 User: {w['telegram_id']}\n"
                f"💸 Amount: {Decimal(str(w['amount'])):.2f} TRX\n"
                f"💼 Wallet: {w['wallet']}",
                reply_markup=keyboard,
            )


# =========================
# ERROR HANDLER
# =========================

async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE):
    logger.exception(
        "BOT ERROR",
        exc_info=context.error,
    )


# =========================
# MAIN
# =========================

def main():
    logger.info("Initializing database...")
    init_db()

    logger.info("Creating Telegram application...")

    app = Application.builder().token(BOT_TOKEN).build()

    # START
    app.add_handler(CommandHandler("start", start))

    # ADMIN
    app.add_handler(CommandHandler("admin", admin_command))

    # CONTACT
    app.add_handler(
        MessageHandler(
            filters.CONTACT,
            contact_handler,
        )
    )

    # MEMBERSHIP
    app.add_handler(
        CallbackQueryHandler(
            check_membership,
            pattern=r"^check_membership$",
        )
    )

    # ADMIN BUTTONS
    app.add_handler(
        CallbackQueryHandler(
            admin_buttons,
            pattern=r"^admin_(stats|balances|withdrawals)$",
        )
    )

    # APPROVE / REJECT
    app.add_handler(
        CallbackQueryHandler(
            admin_callback,
            pattern=r"^(approve|reject)_\d+$",
        )
    )

    # WITHDRAWAL AMOUNT
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            withdrawal_amount,
        ),
        group=1,
    )

    # WALLET + MAIN MENU
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            withdrawal_wallet,
        ),
        group=2,
    )

    # MAIN MENU
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            menu_handler,
        ),
        group=3,
    )

    app.add_error_handler(error_handler)

    logger.info("BOT STARTING...")

    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
    )


if __name__ == "__main__":
    main()
