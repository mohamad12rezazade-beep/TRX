import logging
import os
import re
import sys
from datetime import datetime

import psycopg
from telegram import Update, ReplyKeyboardMarkup, KeyboardButton, InlineKeyboardMarkup, InlineKeyboardButton, ChatMember
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ConversationHandler,
    filters,
    ContextTypes,
)

# ----------------- 配置与常量 -----------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
OWNER_ID = os.environ.get("OWNER_ID")
DATABASE_URL = os.environ.get("DATABASE_URL")

if not BOT_TOKEN or not OWNER_ID or not DATABASE_URL:
    logging.basicConfig(level=logging.INFO)
    logging.error("❌ خطا: متغیرهای محیطی BOT_TOKEN, OWNER_ID, DATABASE_URL باید تنظیم شوند.")
    sys.exit(1)

try:
    OWNER_ID = int(OWNER_ID)
except ValueError:
    logging.error("❌ خطا: OWNER_ID باید یک عدد باشد.")
    sys.exit(1)

# وضعیت‌های مکالمه
WAITING_PHONE, WAITING_WITHDRAW_AMOUNT, WAITING_WITHDRAW_ADDRESS = range(3)

REQUIRED_CHANNELS = ["@BET_1XZX", "@GAP_BAZIN1"]
MIN_WITHDRAW = 2.0
REFERRAL_REWARD = 0.2

# تنظیم لاگ
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# ----------------- دیتابیس -----------------
def get_db_connection():
    """اتصال به دیتابیس Supabase/PostgreSQL"""
    return psycopg.connect(DATABASE_URL, autocommit=True)

def init_db():
    """ساخت جداول در صورت عدم وجود"""
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id BIGINT PRIMARY KEY,
                    username TEXT,
                    phone_number TEXT,
                    referrer_id BIGINT,
                    balance DOUBLE PRECISION DEFAULT 0.0,
                    is_verified BOOLEAN DEFAULT FALSE,
                    is_joined_channels BOOLEAN DEFAULT FALSE,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS withdrawals (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT REFERENCES users(user_id),
                    amount DOUBLE PRECISION NOT NULL,
                    wallet_address TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("""
                CREATE TABLE IF NOT EXISTS required_channels (
                    channel_username TEXT PRIMARY KEY
                );
            """)
            # درج کانال‌های اجباری
            for ch in REQUIRED_CHANNELS:
                cur.execute("INSERT INTO required_channels (channel_username) VALUES (%s) ON CONFLICT DO NOTHING", (ch,))
    logger.info("✅ دیتابیس آماده است.")

def get_user(user_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT * FROM users WHERE user_id = %s", (user_id,))
            return cur.fetchone()

def create_or_update_user(user_id, username=None, phone=None, referrer_id=None):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id FROM users WHERE user_id = %s", (user_id,))
            exists = cur.fetchone()
            if not exists:
                cur.execute(
                    "INSERT INTO users (user_id, username, phone_number, referrer_id) VALUES (%s, %s, %s, %s)",
                    (user_id, username, phone, referrer_id)
                )
            else:
                if username:
                    cur.execute("UPDATE users SET username = %s WHERE user_id = %s", (username, user_id))
                if phone:
                    cur.execute("UPDATE users SET phone_number = %s WHERE user_id = %s", (phone, user_id))

def mark_user_verified(user_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_verified = TRUE WHERE user_id = %s", (user_id,))

def mark_user_joined(user_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET is_joined_channels = TRUE WHERE user_id = %s", (user_id,))

def update_balance(user_id, amount):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, user_id))

def get_balance(user_id):
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT balance FROM users WHERE user_id = %s", (user_id,))
            res = cur.fetchone()
            return res[0] if res else 0.0

def check_referral_reward(referrer_id, referred_id):
    """بررسی و اعطای پاداش رفرال در صورت واجد شرایط بودن"""
    if not referrer_id or referrer_id == referred_id:
        return False
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            # بررسی اینکه آیا قبلاً پاداش داده شده (با چک کردن اینکه آیا referred_id اصلا referrer دارد)
            cur.execute("SELECT referrer_id FROM users WHERE user_id = %s", (referred_id,))
            row = cur.fetchone()
            if not row or row[0] != referrer_id:
                return False
            
            # چک کردن وضعیت referred
            cur.execute("SELECT is_verified, is_joined_channels FROM users WHERE user_id = %s", (referred_id,))
            ref_status = cur.fetchone()
            if not ref_status or not ref_status[0] or not ref_status[1]:
                return False
            
            # بررسی اینکه آیا قبلاً به این رفرال پاداش داده شده یا نه (با یک ستون جداگانه یا چک کردن لاگ)
            # برای سادگی، فرض می‌کنیم اگر referrer_id ثبت شده و شرایط تکمیل است، پاداش داده می‌شود.
            # اما برای اطمینان از عدم پاداش تکراری، می‌توانیم یک جدول لاگ بسازیم. اینجا از یک ترفند استفاده می‌کنیم:
            # اگر موجودی referrer را زیاد کنیم و بعد یک فلگ ذخیره کنیم.
            # بهترین راه: جدول rewards. اما برای سادگی در این ساختار، از یک فیلد reward_paid استفاده می‌کنیم.
            # چون ساختار جدول را از قبل تعریف کردیم، مجبوریم با ALTER TABLE اضافه کنیم یا از یک روش دیگر.
            # راه ساده: چک کنیم آیا referrer_id در جدول users برای referred_id وجود دارد و آیا referred_id verified شده.
            # اما این مانع پاداش تکراری نمی‌شود اگر تابع دوباره صدا زده شود.
            # پس اینجا یک جدول کوچک لاگ می‌سازیم.
            cur.execute("""
                CREATE TABLE IF NOT EXISTS referral_logs (
                    id SERIAL PRIMARY KEY,
                    referrer_id BIGINT,
                    referred_id BIGINT UNIQUE,
                    reward_amount DOUBLE PRECISION,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)
            cur.execute("SELECT id FROM referral_logs WHERE referred_id = %s", (referred_id,))
            if cur.fetchone():
                return False # قبلا پاداش داده شده
            
            # اعطای پاداش
            cur.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (REFERRAL_REWARD, referrer_id))
            cur.execute("INSERT INTO referral_logs (referrer_id, referred_id, reward_amount) VALUES (%s, %s, %s)", 
                        (referrer_id, referred_id, REFERRAL_REWARD))
            logger.info(f"🎁 پاداش رفرال {REFERRAL_REWARD} TRX به کاربر {referrer_id} برای دعوت {referred_id} داده شد.")
            return True

# ----------------- اعتبارسنجی‌ها -----------------
def normalize_iran_phone(phone: str) -> str | None:
    """نرمال‌سازی شماره ایران به فرمت 09xxxxxxxxx"""
    phone = phone.strip().replace(" ", "").replace("-", "")
    # الگوی +98 یا 0098 یا 98
    if phone.startswith("+98"):
        phone = "0" + phone[3:]
    elif phone.startswith("0098"):
        phone = "0" + phone[4:]
    elif phone.startswith("98") and len(phone) == 12:
        phone = "0" + phone[2:]
    
    if re.fullmatch(r"09\d{9}", phone):
        return phone
    return None

def is_valid_tron_address(address: str) -> bool:
    """اعتبارسنجی ساده آدرس TRON (شروع با T و طول 34)"""
    return bool(re.fullmatch(r"T[a-zA-Z0-9]{33}", address))

# ----------------- بررسی عضویت کانال -----------------
async def check_channel_membership(bot, user_id: int) -> bool:
    """بررسی عضویت کاربر در کانال‌های اجباری"""
    for channel in REQUIRED_CHANNELS:
        try:
            member = await bot.get_chat_member(chat_id=channel, user_id=user_id)
            if member.status not in [ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER]:
                logger.info(f"کاربر {user_id} عضو کانال {channel} نیست. وضعیت: {member.status}")
                return False
        except Exception as e:
            logger.error(f"خطا در بررسی عضویت کانال {channel} برای کاربر {user_id}: {e}")
            # اگر ربات ادمین نباشد یا خطای دیگری رخ دهد، برای امنیت false برمی‌گردانیم
            return False
    return True

# ----------------- منوها -----------------
def get_main_menu_keyboard():
    keyboard = [
        [KeyboardButton("💸 برداشت"), KeyboardButton("👥 رفرال‌گیری")],
        [KeyboardButton("💰 موجودی"), KeyboardButton("📋 برداشت‌های تایید شده")]
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)

async def show_main_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """نمایش منوی اصلی پس از تایید کامل"""
    user_id = update.effective_user.id
    await update.message.reply_text(
        "✅ به منوی اصلی خوش آمدید!\nلطفاً یکی از گزینه‌ها را انتخاب کنید:",
        reply_markup=get_main_menu_keyboard()
    )

async def send_join_channels_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """ارسال پیام عضویت در کانال‌ها"""
    text = "⚠️ برای استفاده از ربات، ابتدا باید در کانال‌های زیر عضو شوید:\n\n"
    text += "1️⃣ @BET_1XZX\n"
    text += "2️⃣ @GAP_BAZIN1\n\n"
    text += "پس از عضویت، روی دکمه «بررسی عضویت» کلیک کنید."
    
    keyboard = [
        [InlineKeyboardButton("📢 عضویت در کانال ۱", url="https://t.me/BET_1XZX")],
        [InlineKeyboardButton("📢 عضویت در کانال ۲", url="https://t.me/GAP_BAZIN1")],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership")]
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(text, reply_markup=reply_markup)

# ----------------- هندلرهای اصلی -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    logger.info(f"📥 دریافت /start از کاربر {user.id} (@{user.username})")
    
    # بررسی پارامتر رفرال
    referrer_id = None
    if context.args:
        arg = context.args[0]
        if arg.startswith("ref_"):
            try:
                referrer_id = int(arg[4:])
                if referrer_id == user.id:
                    referrer_id = None
            except ValueError:
                pass
    
    user_data = get_user(user.id)
    if not user_data:
        create_or_update_user(user.id, user.username, referrer_id=referrer_id)
        logger.info(f"کاربر جدید {user.id} ثبت شد. رفرال از: {referrer_id}")
        user_data = get_user(user.id)
    else:
        # آپدیت یوزرنیم
        create_or_update_user(user.id, user.username)
    
    # بررسی وضعیت تأیید شماره
    if not user_data[5]: # is_verified
        await update.message.reply_text(
            "📱 لطفاً شماره موبایل خود را با فرمت صحیح ارسال کنید.\n"
            "فرمت‌های مجاز: 09xxxxxxxxx یا +989xxxxxxxxx یا 00989xxxxxxxxx\n"
            "⚠️ فقط شماره‌های ایران پذیرفته می‌شوند.",
            reply_markup=ReplyKeyboardMarkup(
                [[KeyboardButton("ارسال شماره", request_contact=True)]],
                resize_keyboard=True, one_time_keyboard=True
            )
        )
        return WAITING_PHONE
    
    # بررسی وضعیت عضویت
    if not user_data[6]: # is_joined_channels
        # دوباره چک می‌کنیم چون ممکن است قبلا عضو شده باشد
        is_member = await check_channel_membership(context.bot, user.id)
        if is_member:
            mark_user_joined(user.id)
            # بررسی پاداش رفرال
            if user_data[3]: # referrer_id
                check_referral_reward(user_data[3], user.id)
            await show_main_menu(update, context)
            return ConversationHandler.END
        else:
            await send_join_channels_message(update, context)
            return ConversationHandler.END
    
    # کاربر کامل تایید شده است
    await show_main_menu(update, context)
    return ConversationHandler.END

async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    
    # اگر کاربر شماره را از طریق دکمه تماس ارسال کرده باشد
    if update.message.contact:
        phone = update.message.contact.phone_number
    else:
        phone = update.message.text.strip()
    
    normalized = normalize_iran_phone(phone)
    if not normalized:
        await update.message.reply_text(
            "❌ شماره وارد شده معتبر نیست یا مربوط به ایران نمی‌باشد.\n"
            "لطفاً شماره را با فرمت صحیح (09xxxxxxxxx) ارسال کنید."
        )
        return WAITING_PHONE
    
    # ذخیره شماره
    create_or_update_user(user.id, user.username, phone=normalized)
    mark_user_verified(user.id)
    logger.info(f"✅ شماره کاربر {user.id} تایید شد: {normalized}")
    
    # حالا بررسی عضویت کانال
    await update.message.reply_text("✅ شماره شما تایید شد. در حال بررسی عضویت کانال‌ها...")
    is_member = await check_channel_membership(context.bot, user.id)
    if is_member:
        mark_user_joined(user.id)
        user_data = get_user(user.id)
        # بررسی پاداش رفرال
        if user_data[3]: # referrer_id
            check_referral_reward(user_data[3], user.id)
        await show_main_menu(update, context)
        return ConversationHandler.END
    else:
        await send_join_channels_message(update, context)
        return ConversationHandler.END

async def check_membership_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    is_member = await check_channel_membership(context.bot, user_id)
    if is_member:
        mark_user_joined(user_id)
        user_data = get_user(user_id)
        # بررسی پاداش رفرال
        if user_data and user_data[3]:
            if check_referral_reward(user_data[3], user_id):
                await query.edit_message_text("✅ عضویت شما تایید شد و پاداش رفرال برای دعوت‌کننده شما ثبت گردید.")
            else:
                await query.edit_message_text("✅ عضویت شما تایید شد.")
        else:
            await query.edit_message_text("✅ عضویت شما تایید شد.")
        
        await context.bot.send_message(
            chat_id=user_id,
            text="به منوی اصلی خوش آمدید!",
            reply_markup=get_main_menu_keyboard()
        )
    else:
        await query.answer("❌ شما هنوز در کانال‌ها عضو نشده‌اید. لطفاً ابتدا عضو شوید.", show_alert=True)

async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """مدیریت دکمه‌های منوی اصلی"""
    text = update.message.text
    user_id = update.effective_user.id
    user_data = get_user(user_id)
    
    if not user_data or not user_data[5] or not user_data[6]:
        await update.message.reply_text("⚠️ لطفاً ابتدا /start را بزنید و مراحل را کامل کنید.")
        return
    
    if text == "💰 موجودی":
        balance = get_balance(user_id)
        await update.message.reply_text(f"💰 موجودی شما: {balance:.4f} TRX")
    
    elif text == "👥 رفرال‌گیری":
        bot_username = context.bot.username
        ref_link = f"https://t.me/{bot_username}?start=ref_{user_id}"
        
        # آمار رفرال
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT COUNT(*) FROM referral_logs WHERE referrer_id = %s", (user_id,))
                count = cur.fetchone()[0]
                cur.execute("SELECT COALESCE(SUM(reward_amount), 0) FROM referral_logs WHERE referrer_id = %s", (user_id,))
                total_reward = cur.fetchone()[0]
        
        text_msg = (
            f"👥 لینک رفرال اختصاصی شما:\n{ref_link}\n\n"
            f"📊 تعداد دعوت‌های موفق: {count}\n"
            f"💰 مجموع پاداش دریافتی: {total_reward:.2f} TRX\n\n"
            f"🎁 پاداش هر دعوت موفق: {REFERRAL_REWARD} TRX\n"
            f"(پاداش پس از تایید شماره و عضویت کانال کاربر دعوت‌شده اعطا می‌شود)"
        )
        await update.message.reply_text(text_msg)
    
    elif text == "💸 برداشت":
        balance = get_balance(user_id)
        if balance < MIN_WITHDRAW:
            await update.message.reply_text(f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است. موجودی شما: {balance:.4f} TRX")
            return
        
        await update.message.reply_text(
            f"💰 موجودی شما: {balance:.4f} TRX\n"
            f"مبلغ مورد نظر برای برداشت را وارد کنید (حداقل {MIN_WITHDRAW} TRX):"
        )
        return WAITING_WITHDRAW_AMOUNT
    
    elif text == "📋 برداشت‌های تایید شده":
        with get_db_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT amount, wallet_address, created_at FROM withdrawals WHERE user_id = %s AND status = 'approved' ORDER BY created_at DESC LIMIT 20",
                    (user_id,)
                )
                rows = cur.fetchall()
        
        if not rows:
            await update.message.reply_text("📋 هیچ برداشت تایید شده‌ای ندارید.")
            return
        
        msg = "📋 آخرین برداشت‌های تایید شده شما:\n\n"
        for row in rows:
            amount, wallet, created = row
            masked_wallet = wallet[:6] + "..." + wallet[-4:] if len(wallet) > 10 else wallet
            msg += f"✅ {amount:.4f} TRX - {masked_wallet} - {created.strftime('%Y-%m-%d')}\n"
        await update.message.reply_text(msg)

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    text = update.message.text.strip()
    
    try:
        amount = float(text)
    except ValueError:
        await update.message.reply_text("❌ لطفاً یک عدد معتبر وارد کنید.")
        return WAITING_WITHDRAW_AMOUNT
    
    if amount < MIN_WITHDRAW:
        await update.message.reply_text(f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است.")
        return WAITING_WITHDRAW_AMOUNT
    
    balance = get_balance(user_id)
    if amount > balance:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی شما: {balance:.4f} TRX")
        return WAITING_WITHDRAW_AMOUNT
    
    context.user_data['withdraw_amount'] = amount
    await update.message.reply_text(
        f"✅ مبلغ {amount:.4f} TRX ثبت شد.\n"
        "حالا آدرس کیف پول TRON خود را وارد کنید (شروع با T و 34 کاراکتر):"
    )
    return WAITING_WITHDRAW_ADDRESS

async def withdraw_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    address = update.message.text.strip()
    amount = context.user_data.get('withdraw_amount')
    
    if not amount:
        await update.message.reply_text("❌ خطا. لطفاً دوباره از منوی برداشت شروع کنید.")
        return ConversationHandler.END
    
    if not is_valid_tron_address(address):
        await update.message.reply_text("❌ آدرس TRON نامعتبر است. آدرس باید با T شروع شود و 34 کاراکتر باشد.")
        return WAITING_WITHDRAW_ADDRESS
    
    # بررسی مجدد موجودی (به دلیل احتمال تغییر)
    balance = get_balance(user_id)
    if amount > balance:
        await update.message.reply_text(f"❌ موجودی کافی نیست. موجودی فعلی: {balance:.4f} TRX")
        return ConversationHandler.END
    
    # کسر موجودی (رزرو)
    update_balance(user_id, -amount)
    
    # ذخیره درخواست
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO withdrawals (user_id, amount, wallet_address, status) VALUES (%s, %s, %s, 'pending') RETURNING id",
                (user_id, amount, address)
            )
            withdrawal_id = cur.fetchone()[0]
    
    # ارسال به ادمین
    admin_msg = (
        f"🔔 **درخواست برداشت جدید**\n\n"
        f"🆔 User ID: `{user_id}`\n"
        f"💰 Amount: {amount:.4f} TRX\n"
        f"🏦 Wallet: `{address}`\n"
        f"📝 Withdrawal ID: {withdrawal_id}"
    )
    
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("✅ تایید", callback_data=f"approve_{withdrawal_id}"),
            InlineKeyboardButton("❌ رد", callback_data=f"reject_{withdrawal_id}")
        ]
    ])
    
    try:
        await context.bot.send_message(OWNER_ID, admin_msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)
    except Exception as e:
        logger.error(f"خطا در ارسال پیام به ادمین: {e}")
    
    await update.message.reply_text(
        "✅ درخواست برداشت شما ثبت شد و برای بررسی به ادمین ارسال گردید.\n"
        "مبلغ تا زمان تعیین تکلیف از موجودی شما کسر شده است."
    )
    return ConversationHandler.END

async def admin_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user_id = update.effective_user.id
    if user_id != OWNER_ID:
        await update.message.reply_text("⛔ شما دسترسی به این دستور ندارید.")
        return
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) FROM users")
            total_users = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM users WHERE is_verified = TRUE")
            verified_users = cur.fetchone()[0]
            
            cur.execute("SELECT COALESCE(SUM(balance), 0) FROM users")
            total_balance = cur.fetchone()[0]
            
            cur.execute("SELECT COUNT(*) FROM withdrawals WHERE status = 'pending'")
            pending_withdrawals = cur.fetchone()[0]
    
    msg = (
        f"📊 **پنل ادمین**\n\n"
        f"👥 کل کاربران: {total_users}\n"
        f"✅ کاربران تایید شده: {verified_users}\n"
        f"💰 مجموع موجودی کاربران: {total_balance:.4f} TRX\n"
        f"⏳ برداشت‌های در انتظار: {pending_withdrawals}\n\n"
        f"دستورات:\n"
        f"/admin_users - لیست موجودی کاربران\n"
        f"/admin_pending - لیست برداشت‌های pending"
    )
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, username, balance, is_verified FROM users ORDER BY balance DESC LIMIT 50")
            rows = cur.fetchall()
    
    if not rows:
        await update.message.reply_text("هیچ کاربری یافت نشد.")
        return
    
    msg = "👥 **لیست کاربران (50 نفر برتر)**\n\n"
    for row in rows:
        uid, uname, bal, ver = row
        status = "✅" if ver else "❌"
        msg += f"{status} `{uid}` @{uname or 'N/A'} - {bal:.4f} TRX\n"
    
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN)

async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id, user_id, amount, wallet_address, created_at FROM withdrawals WHERE status = 'pending' ORDER BY created_at ASC")
            rows = cur.fetchall()
    
    if not rows:
        await update.message.reply_text("هیچ برداشت pending وجود ندارد.")
        return
    
    for row in rows:
        wid, uid, amount, wallet, created = row
        msg = (
            f"⏳ **Withdrawal #{wid}**\n"
            f"User: `{uid}`\n"
            f"Amount: {amount:.4f} TRX\n"
            f"Wallet: `{wallet}`\n"
            f"Date: {created.strftime('%Y-%m-%d %H:%M')}"
        )
        keyboard = InlineKeyboardMarkup([
            [
                InlineKeyboardButton("✅ تایید", callback_data=f"approve_{wid}"),
                InlineKeyboardButton("❌ رد", callback_data=f"reject_{wid}")
            ]
        ])
        await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=keyboard)

async def withdrawal_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    user_id = query.from_user.id
    
    if user_id != OWNER_ID:
        await query.answer("⛔ دسترسی ندارید.", show_alert=True)
        return
    
    data = query.data
    if data.startswith("approve_"):
        withdrawal_id = int(data.split("_")[1])
        action = "approve"
    elif data.startswith("reject_"):
        withdrawal_id = int(data.split("_")[1])
        action = "reject"
    else:
        return
    
    with get_db_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT user_id, amount, status FROM withdrawals WHERE id = %s", (withdrawal_id,))
            row = cur.fetchone()
            if not row:
                await query.edit_message_text("❌ درخواست یافت نشد.")
                return
            
            target_user_id, amount, current_status = row
            if current_status != 'pending':
                await query.edit_message_text(f"❌ این درخواست قبلاً {current_status} شده است.")
                return
            
            if action == "approve":
                cur.execute("UPDATE withdrawals SET status = 'approved', updated_at = CURRENT_TIMESTAMP WHERE id = %s", (withdrawal_id,))
                await query.edit_message_text(f"✅ درخواست #{withdrawal_id} تایید شد.")
                
                # اطلاع به کاربر
                try:
                    await context.bot.send_message(
                        target_user_id,
                        f"✅ درخواست برداشت شما به مبلغ {amount:.4f} TRX تایید شد.\n"
                        f"تراکنش متعاقباً انجام خواهد شد."
                    )
                except Exception as e:
                    logger.error(f"خطا در اطلاع به کاربر {target_user_id}: {e}")
                
                # انتشار در کانال (بدون آدرس کیف پول)
                try:
                    await context.bot.send_message(
                        "@BET_1XZX",
                        f"✅ **برداشت تایید شد**\n\n"
                        f"🆔 User ID: `{target_user_id}`\n"
                        f"💰 Amount: {amount:.4f} TRX",
                        parse_mode=ParseMode.MARKDOWN
                    )
                except Exception as e:
                    logger.error(f"خطا در ارسال به کانال: {e}")
                    
            else: # reject
                cur.execute("UPDATE withdrawals SET status = 'rejected', updated_at = CURRENT_TIMESTAMP WHERE id = %s", (withdrawal_id,))
                # برگشت مبلغ
                cur.execute("UPDATE users SET balance = balance + %s WHERE user_id = %s", (amount, target_user_id))
                await query.edit_message_text(f"❌ درخواست #{withdrawal_id} رد شد و مبلغ به موجودی کاربر برگشت داده شد.")
                
                try:
                    await context.bot.send_message(
                        target_user_id,
                        f"❌ درخواست برداشت شما به مبلغ {amount:.4f} TRX رد شد.\n"
                        f"مبلغ به موجودی شما برگشت داده شد."
                    )
                except Exception as e:
                    logger.error(f"خطا در اطلاع به کاربر {target_user_id}: {e}")

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عملیات لغو شد.", reply_markup=get_main_menu_keyboard())
    return ConversationHandler.END

# ----------------- راه‌اندازی -----------------
async def post_init(application: Application):
    """تست اتصال دیتابیس و ربات قبل از شروع polling"""
    logger.info("🔍 در حال بررسی اتصال دیتابیس...")
    try:
        init_db()
        logger.info("✅ اتصال دیتابیس موفق.")
    except Exception as e:
        logger.critical(f"❌ خطا در اتصال دیتابیس: {e}")
        raise
    
    logger.info("🔍 در حال بررسی ربات با get_me()...")
    try:
        me = await application.bot.get_me()
        logger.info(f"✅ ربات متصل شد: @{me.username} (ID: {me.id})")
    except Exception as e:
        logger.critical(f"❌ خطا در اتصال به Telegram API: {e}")
        raise

def main():
    if not BOT_TOKEN:
        logger.error("BOT_TOKEN تنظیم نشده است.")
        return
    
    logger.info("🚀 در حال ساخت اپلیکیشن...")
    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .post_init(post_init)
        .concurrent_updates(False) # برای ConversationHandler ضروری است
        .build()
    )
    
    # مکالمه ثبت‌نام/بررسی
    conv_handler = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_PHONE: [
                MessageHandler(filters.CONTACT | filters.TEXT & ~filters.COMMAND, handle_phone)
            ],
            WAITING_WITHDRAW_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)
            ],
            WAITING_WITHDRAW_ADDRESS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_address)
            ],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    
    # هندلرهای دکمه‌های منو (بعد از ConversationHandler اضافه می‌شوند تا تداخل نکنند)
    # این هندلر فقط زمانی اجرا می‌شود که کاربر در حالت مکالمه نباشد
    menu_handler = MessageHandler(
        filters.Regex("^(💰 موجودی|👥 رفرال‌گیری|💸 برداشت|📋 برداشت‌های تایید شده)$"),
        button_handler
    )
    
    # هندلرهای ادمین
    application.add_handler(CommandHandler("admin", admin_command))
    application.add_handler(CommandHandler("admin_users", admin_users))
    application.add_handler(CommandHandler("admin_pending", admin_pending))
    
    # کال‌بک‌ها
    application.add_handler(CallbackQueryHandler(check_membership_callback, pattern="^check_membership$"))
    application.add_handler(CallbackQueryHandler(withdrawal_callback, pattern="^(approve|reject)_"))
    
    # ترتیب مهم است: ConversationHandler اول، سپس منو
    application.add_handler(conv_handler)
    application.add_handler(menu_handler)
    
    logger.info("✅ هندلرها ثبت شدند. شروع polling...")
    try:
        application.run_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True  # جلوگیری از پردازش آپدیت‌های قدیمی
        )
    except Exception as e:
        logger.critical(f"❌ خطای بحرانی در polling: {e}", exc_info=True)

if __name__ == "__main__":
    main()
