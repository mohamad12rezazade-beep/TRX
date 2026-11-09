import logging
import os
import re
import sys
import asyncio

import psycopg
from telegram import (
    Update, ReplyKeyboardMarkup, KeyboardButton,
    InlineKeyboardMarkup, InlineKeyboardButton, ChatMember,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application, CommandHandler, MessageHandler, CallbackQueryHandler,
    ConversationHandler, filters, ContextTypes,
)
from telegram.error import Forbidden, RetryAfter

# ---------- ENV ----------
def _clean_token(t: str) -> str:
    if not t: return ""
    t = t.strip().replace("\n", "").replace("\r", "").replace("\t", "").replace(" ", "")
    if t.lower().startswith("bot"): t = t[3:]
    return t

BOT_TOKEN = _clean_token(os.environ.get("BOT_TOKEN", ""))
OWNER_ID_RAW = (os.environ.get("OWNER_ID", "") or "").strip()
DATABASE_URL = (os.environ.get("DATABASE_URL", "") or "").strip()

logging.basicConfig(format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO)
logger = logging.getLogger(__name__)

if not BOT_TOKEN or not OWNER_ID_RAW.isdigit() or not DATABASE_URL:
    logger.critical("❌ خطا در ENV ها (BOT_TOKEN, OWNER_ID, DATABASE_URL)")
    sys.exit(1)

OWNER_ID = int(OWNER_ID_RAW)

# وضعیت‌های مکالمه
(
    WAITING_PHONE,
    WAITING_WITHDRAW_AMOUNT,
    WAITING_WITHDRAW_ADDRESS,
    WAITING_BROADCAST,
    WAITING_CHANNEL_ID
) = range(5)

REQUIRED_CHANNELS = ["@BET_1XZX", "@GAP_BAZIN1", "@Starzivo"]
MIN_WITHDRAW = 2.0
REFERRAL_REWARD = 0.2

# ---------- DB ----------
def db():
    return psycopg.connect(DATABASE_URL, autocommit=True)

def _table_has_expected_columns(cur, table, expected):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    cols = {r[0] for r in cur.fetchall()}
    if not cols: return False
    return all(c in cols for c in expected)

def init_db():
    with db() as conn, conn.cursor() as cur:
        if not _table_has_expected_columns(cur, "users", ["user_id", "username", "phone_number", "referrer_id", "balance", "is_verified", "is_joined_channels", "created_at"]):
            cur.execute("DROP TABLE IF EXISTS users CASCADE;")
            cur.execute("CREATE TABLE users (user_id BIGINT PRIMARY KEY, username TEXT, phone_number TEXT, referrer_id BIGINT, balance DOUBLE PRECISION DEFAULT 0.0, is_verified BOOLEAN DEFAULT FALSE, is_joined_channels BOOLEAN DEFAULT FALSE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
        
        if not _table_has_expected_columns(cur, "withdrawals", ["id", "user_id", "amount", "wallet_address", "status", "created_at", "updated_at"]):
            cur.execute("DROP TABLE IF EXISTS withdrawals CASCADE;")
            cur.execute("CREATE TABLE withdrawals (id SERIAL PRIMARY KEY, user_id BIGINT, amount DOUBLE PRECISION NOT NULL, wallet_address TEXT NOT NULL, status TEXT DEFAULT 'pending', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
        
        if not _table_has_expected_columns(cur, "referral_logs", ["id", "referrer_id", "referred_id", "reward_amount", "created_at"]):
            cur.execute("DROP TABLE IF EXISTS referral_logs CASCADE;")
            cur.execute("CREATE TABLE referral_logs (id SERIAL PRIMARY KEY, referrer_id BIGINT, referred_id BIGINT UNIQUE, reward_amount DOUBLE PRECISION, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP);")
        
        if not _table_has_expected_columns(cur, "required_channels", ["channel_username"]):
            cur.execute("DROP TABLE IF EXISTS required_channels CASCADE;")
            cur.execute("CREATE TABLE required_channels (channel_username TEXT PRIMARY KEY);")
        
        if not _table_has_expected_columns(cur, "settings", ["key", "value"]):
            cur.execute("DROP TABLE IF EXISTS settings CASCADE;")
            cur.execute("CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT);")
            cur.execute("INSERT INTO settings (key, value) VALUES ('report_channel', '@BET_1XZX') ON CONFLICT DO NOTHING")

        for ch in REQUIRED_CHANNELS:
            cur.execute("INSERT INTO required_channels(channel_username) VALUES (%s) ON CONFLICT (channel_username) DO NOTHING", (ch,))
    logger.info("✅ DB ready")

def get_setting(key, default=""):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT value FROM settings WHERE key=%s", (key,))
        r = cur.fetchone()
        return r[0] if r else default

def set_setting(key, value):
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO settings (key, value) VALUES (%s, %s) ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value", (key, value))

def get_user(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, username, phone_number, referrer_id, balance, is_verified, is_joined_channels, created_at FROM users WHERE user_id=%s", (uid,))
        return cur.fetchone()

def get_all_user_ids():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users")
        return [row[0] for row in cur.fetchall()]

def create_user_if_needed(uid, username=None, referrer_id=None):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE user_id=%s", (uid,))
        if cur.fetchone():
            if username: cur.execute("UPDATE users SET username=%s WHERE user_id=%s", (username, uid))
        else:
            cur.execute("INSERT INTO users(user_id, username, referrer_id) VALUES (%s,%s,%s)", (uid, username, referrer_id))

def set_verified(uid, phone):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET phone_number=%s, is_verified=TRUE WHERE user_id=%s", (phone, uid))

def set_joined(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET is_joined_channels=TRUE WHERE user_id=%s", (uid,))

def get_balance(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT balance FROM users WHERE user_id=%s", (uid,))
        r = cur.fetchone()
        return float(r[0]) if r else 0.0

def add_balance(uid, amount):
    with db() as conn, conn.cursor() as cur:
        cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amount, uid))

def grant_referral_reward_if_eligible(referrer_id, referred_id):
    if not referrer_id or referrer_id == referred_id: return
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT is_verified, is_joined_channels FROM users WHERE user_id=%s", (referred_id,))
            r = cur.fetchone()
            if not r or not r[0] or not r[1]: return
            cur.execute("SELECT 1 FROM referral_logs WHERE referred_id=%s", (referred_id,))
            if cur.fetchone(): return
            cur.execute("INSERT INTO referral_logs(referrer_id, referred_id, reward_amount) VALUES (%s,%s,%s)", (referrer_id, referred_id, REFERRAL_REWARD))
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (REFERRAL_REWARD, referrer_id))
    except Exception as e:
        logger.error(f"referral reward failed: {e}")

# ---------- helpers ----------
def normalize_iran_phone(p: str):
    p = (p or "").strip().replace(" ", "").replace("-", "")
    if p.startswith("+98"): p = "0" + p[3:]
    elif p.startswith("0098"): p = "0" + p[4:]
    elif p.startswith("98") and len(p) == 12: p = "0" + p[2:]
    return p if re.fullmatch(r"09\d{9}", p) else None

def is_valid_tron(a: str) -> bool:
    return bool(re.fullmatch(r"T[a-zA-Z0-9]{33}", a))

async def check_members(bot, uid) -> bool:
    for ch in REQUIRED_CHANNELS:
        try:
            m = await bot.get_chat_member(chat_id=ch, user_id=uid)
            if m.status not in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER): return False
        except Exception as e:
            logger.error(f"get_chat_member {ch} for {uid}: {e}")
            return False
    return True

def main_menu_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("💸 برداشت"), KeyboardButton("👥 رفرال‌گیری")],
        [KeyboardButton("💰 موجودی"), KeyboardButton("📋 برداشت‌های تایید شده")],
    ], resize_keyboard=True)

def admin_menu_kb():
    return ReplyKeyboardMarkup([
        [KeyboardButton("📊 آمار کل"), KeyboardButton("👥 لیست کاربران")],
        [KeyboardButton("⏳ برداشت‌های Pending"), KeyboardButton("📢 پیام همگانی")],
        [KeyboardButton("⚙️ تنظیم کانال گزارش"), KeyboardButton("⬅️ بازگشت به منوی اصلی")]
    ], resize_keyboard=True)

def phone_kb():
    return ReplyKeyboardMarkup([[KeyboardButton("📱 ارسال شماره من", request_contact=True)]], resize_keyboard=True)

async def ask_phone(msg):
    await msg.reply_text("📱 شماره موبایل ایران را ارسال کنید:\n09xxxxxxxxx | +989xxxxxxxxx | 00989xxxxxxxxx", reply_markup=phone_kb())

async def ask_join(msg):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 کانال ۱", url="https://t.me/BET_1XZX")],
        [InlineKeyboardButton("📢 کانال ۲", url="https://t.me/GAP_BAZIN1")],
        [InlineKeyboardButton("📢 کانال ۳", url="https://t.me/Starzivo")],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership")],
    ])
    await msg.reply_text(
        "⚠️ برای استفاده، در این کانال‌ها عضو شوید:\n\n"
        "1️⃣ @BET_1XZX\n"
        "2️⃣ @GAP_BAZIN1\n"
        "3️⃣ @Starzivo\n\n"
        "سپس «بررسی عضویت» را بزنید.",
        reply_markup=kb
    )

# ---------- debug ----------
async def debug_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        typ = "callback" if update.callback_query else "message" if update.message else "other"
        uid = update.effective_user.id if update.effective_user else "?"
        txt = update.effective_message.text if update.effective_message and update.effective_message.text else ""
        logger.info(f"🔔 UPDATE id={update.update_id} type={typ} from={uid} text={txt!r}")
    except Exception:
        pass

# ---------- CONVERSATIONS ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    try:
        referrer_id = None
        if context.args:
            a = context.args[0]
            if a.startswith("ref_"):
                try: referrer_id = int(a[4:])
                except ValueError: referrer_id = None
                if referrer_id == u.id: referrer_id = None

        create_user_if_needed(u.id, u.username, referrer_id)
        row = get_user(u.id)
        if not row: return ConversationHandler.END

        if not row[5]:
            await ask_phone(update.message)
            return WAITING_PHONE
        
        if not row[6]:
            if await check_members(context.bot, u.id):
                set_joined(u.id)
                grant_referral_reward_if_eligible(row[3], u.id)
                await update.message.reply_text("✅ خوش آمدید!", reply_markup=main_menu_kb())
            else:
                await ask_join(update.message)
            return ConversationHandler.END

        await update.message.reply_text("✅ به منوی اصلی خوش آمدید:", reply_markup=main_menu_kb())
    except Exception as e:
        logger.error(f"start crashed: {e}")
    return ConversationHandler.END

async def handle_phone(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    phone = update.message.contact.phone_number if update.message.contact else (update.message.text or "")
    norm = normalize_iran_phone(phone)
    if not norm:
        await update.message.reply_text("❌ شماره معتبر ایران نیست. دوباره بفرست (09xxxxxxxxx).", reply_markup=phone_kb())
        return WAITING_PHONE

    set_verified(u.id, norm)
    row = get_user(u.id)
    if await check_members(context.bot, u.id):
        set_joined(u.id)
        grant_referral_reward_if_eligible(row[3], u.id)
        await update.message.reply_text("✅ شماره و عضویت تایید شد.", reply_markup=main_menu_kb())
    else:
        await update.message.reply_text("✅ شماره تایید شد.")
        await ask_join(update.message)
    return ConversationHandler.END

async def withdraw_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    row = get_user(u.id)
    if not row or not row[5] or not row[6]:
        await update.message.reply_text("ابتدا /start را بزنید.")
        return ConversationHandler.END
    
    bal = get_balance(u.id)
    if bal < MIN_WITHDRAW:
        await update.message.reply_text(f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است. موجودی شما: {bal:.4f} TRX", reply_markup=main_menu_kb())
        return ConversationHandler.END
    
    await update.message.reply_text(
        f"موجودی شما: {bal:.4f} TRX\n\nلطفاً مبلغ مورد نظر برای برداشت را به عدد وارد کنید (حداقل {MIN_WITHDRAW} TRX):\n(برای لغو /cancel را بزنید)",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ لغو")]], resize_keyboard=True)
    )
    return WAITING_WITHDRAW_AMOUNT

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    try:
        amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ لطفاً فقط یک عدد معتبر وارد کنید:")
        return WAITING_WITHDRAW_AMOUNT
        
    if amt < MIN_WITHDRAW:
        await update.message.reply_text(f"❌ حداقل برداشت {MIN_WITHDRAW} TRX است. دوباره وارد کنید:")
        return WAITING_WITHDRAW_AMOUNT
        
    if amt > get_balance(u.id):
        await update.message.reply_text(f"❌ موجودی کافی نیست (موجودی شما: {get_balance(u.id):.4f}). دوباره وارد کنید:")
        return WAITING_WITHDRAW_AMOUNT
        
    context.user_data["w_amount"] = amt
    await update.message.reply_text("✅ مبلغ ثبت شد.\n\nحالا آدرس کیف پول TRON خود را وارد کنید (شروع با T و 34 کاراکتر):")
    return WAITING_WITHDRAW_ADDRESS

async def withdraw_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    addr = update.message.text.strip()
    amt = context.user_data.get("w_amount")
    
    if not is_valid_tron(addr):
        await update.message.reply_text("❌ آدرس TRON نامعتبر است. باید با T شروع شده و 34 کاراکتر باشد. دوباره وارد کنید:")
        return WAITING_WITHDRAW_ADDRESS
        
    if amt > get_balance(u.id):
        await update.message.reply_text("❌ موجودی شما تغییر کرده و کافی نیست.", reply_markup=main_menu_kb())
        return ConversationHandler.END

    add_balance(u.id, -amt)
    with db() as conn, conn.cursor() as cur:
        cur.execute("INSERT INTO withdrawals(user_id, amount, wallet_address, status) VALUES (%s,%s,%s,'pending') RETURNING id", (u.id, amt, addr))
        wid = cur.fetchone()[0]

    kb = InlineKeyboardMarkup([[
        InlineKeyboardButton("✅ تایید", callback_data=f"approve_{wid}"),
        InlineKeyboardButton("❌ رد", callback_data=f"reject_{wid}"),
    ]])
    try:
        await context.bot.send_message(OWNER_ID, f"🔔 برداشت #{wid}\nUser: `{u.id}`\nAmount: {amt:.4f} TRX\nWallet: `{addr}`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)
    except Exception as e:
        logger.error(f"send owner: {e}")

    await update.message.reply_text("✅ درخواست شما ثبت و برای ادمین ارسال شد. مبلغ از موجودی شما کسر شد.", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def cancel_withdraw(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("عملیات لغو شد.", reply_markup=main_menu_kb())
    return ConversationHandler.END

# ---------- USER MENU ----------
async def user_menu_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    u = update.effective_user
    
    if text == "💰 موجودی":
        await update.message.reply_text(f"💰 موجودی شما: {get_balance(u.id):.4f} TRX")
    elif text == "👥 رفرال‌گیری":
        link = f"https://t.me/{context.bot.username}?start=ref_{u.id}"
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COALESCE(SUM(reward_amount),0) FROM referral_logs WHERE referrer_id=%s", (u.id,))
            cnt, total = cur.fetchone()
        await update.message.reply_text(f"👥 لینک رفرال:\n{link}\n\nتعداد دعوت موفق: {cnt}\nمجموع پاداش: {total:.2f} TRX")
    elif text == "📋 برداشت‌های تایید شده":
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT amount, wallet_address, created_at FROM withdrawals WHERE user_id=%s AND status='approved' ORDER BY created_at DESC LIMIT 20", (u.id,))
            rows = cur.fetchall()
        if not rows:
            await update.message.reply_text("📋 برداشت تایید شده‌ای نداری.")
            return
        msg = "📋 آخرین برداشت‌های تایید شده:\n\n"
        for amt, w, c in rows:
            masked = w[:6] + "..." + w[-4:] if len(w) > 10 else w
            msg += f"✅ {amt:.4f} TRX - {masked} - {c.strftime('%Y-%m-%d')}\n"
        await update.message.reply_text(msg)

# ---------- ADMIN PANEL ----------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    await update.message.reply_text("👨‍💻 پنل مدیریت:", reply_markup=admin_menu_kb())

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM users"); total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE is_verified=TRUE"); ver = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(balance),0) FROM users"); tb = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'"); pw = cur.fetchone()[0]
    await update.message.reply_text(f"📊 **آمار کلی**\n\n👥 کاربران: {total}\n✅ تایید شده: {ver}\n💰 مجموع موجودی: {tb:.4f} TRX\n⏳ برداشت‌های Pending: {pw}", reply_markup=admin_menu_kb(), parse_mode=ParseMode.MARKDOWN)

async def admin_users(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, username, balance, is_verified FROM users ORDER BY balance DESC LIMIT 50")
        rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("خالی.", reply_markup=admin_menu_kb())
        return
    msg = "👥 **۵۰ کاربر برتر:**\n\n"
    for uid, un, bal, ver in rows:
        msg += f"{'✅' if ver else '❌'} `{uid}` @{un or '-'} - {bal:.4f}\n"
    await update.message.reply_text(msg, parse_mode=ParseMode.MARKDOWN, reply_markup=admin_menu_kb())

async def admin_pending(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT id, user_id, amount, wallet_address FROM withdrawals WHERE status='pending' ORDER BY id")
        rows = cur.fetchall()
    if not rows:
        await update.message.reply_text("برداشت Pending وجود ندارد.", reply_markup=admin_menu_kb())
        return
    await update.message.reply_text(f"⏳ **{len(rows)} برداشت در انتظار:**", reply_markup=admin_menu_kb(), parse_mode=ParseMode.MARKDOWN)
    for wid, uid, amt, w in rows:
        kb = InlineKeyboardMarkup([[InlineKeyboardButton("✅ تایید", callback_data=f"approve_{wid}"), InlineKeyboardButton("❌ رد", callback_data=f"reject_{wid}")]])
        await update.message.reply_text(f"#{wid} User `{uid}` {amt:.4f} TRX\n`{w}`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def admin_back_to_user_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    await update.message.reply_text("بازگشت به منوی اصلی.", reply_markup=main_menu_kb())

async def admin_set_channel_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    curr = get_setting("report_channel", "@BET_1XZX")
    await update.message.reply_text(
        f"⚙️ کانال گزارش فعلی: `{curr}`\n\n"
        "لطفاً آیدی کانال جدید را با @ ارسال کنید (مثلاً @MyChannel):\n"
        "برای لغو /cancel را بزنید.",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ لغو")]], resize_keyboard=True),
        parse_mode=ParseMode.MARKDOWN
    )
    return WAITING_CHANNEL_ID

async def admin_set_channel_save(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return ConversationHandler.END
    ch = update.message.text.strip()
    if not ch.startswith("@"):
        await update.message.reply_text("❌ آیدی باید با @ شروع شود. دوباره وارد کنید یا /cancel بزنید.")
        return WAITING_CHANNEL_ID
    
    try:
        chat = await context.bot.get_chat(chat_id=ch)
        bot_member = await context.bot.get_chat_member(chat_id=ch, user_id=context.bot.id)
        if bot_member.status not in (ChatMember.ADMINISTRATOR, ChatMember.OWNER):
            await update.message.reply_text(f"❌ ربات در کانال {ch} ادمین نیست! ابتدا ربات را ادمین کنید.", reply_markup=admin_menu_kb())
            return ConversationHandler.END
    except Exception as e:
        await update.message.reply_text(f"❌ خطا در دسترسی به کانال {ch}: {e}\nمطمئن شوید ربات ادمین است و آیدی درست است.", reply_markup=admin_menu_kb())
        return ConversationHandler.END

    set_setting("report_channel", ch)
    await update.message.reply_text(f"✅ کانال گزارش با موفقیت به {ch} تغییر یافت.", reply_markup=admin_menu_kb())
    return ConversationHandler.END

async def admin_set_channel_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لغو شد.", reply_markup=admin_menu_kb())
    return ConversationHandler.END

# ---------- BROADCAST ----------
async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    await update.message.reply_text("📢 **ارسال پیام همگانی**\n\nپیام خود را بفرستید (متن، عکس، ویدیو یا فوروارد).", reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ لغو")]], resize_keyboard=True), parse_mode=ParseMode.MARKDOWN)
    return WAITING_BROADCAST

async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    u_ids = get_all_user_ids()
    status_msg = await update.message.reply_text(f"⏳ در حال ارسال به {len(u_ids)} کاربر...")
    success = failed = 0
    for uid in u_ids:
        try:
            await update.message.copy(chat_id=uid)
            success += 1
        except Forbidden: failed += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await update.message.copy(chat_id=uid)
                success += 1
            except: failed += 1
        except: failed += 1
        await asyncio.sleep(0.05)
    await status_msg.edit_text(f"✅ **پایان ارسال**\n\nموفق: {success}\nناموفق: {failed}", reply_markup=admin_menu_kb(), parse_mode=ParseMode.MARKDOWN)
    return ConversationHandler.END

async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    await update.message.reply_text("لغو شد.", reply_markup=admin_menu_kb())
    return ConversationHandler.END

# ---------- CALLBACKS ----------
async def check_membership_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    uid = q.from_user.id
    if not await check_members(context.bot, uid):
        await q.answer("❌ هنوز عضو کانال‌ها نیستی.", show_alert=True)
        return
    set_joined(uid)
    row = get_user(uid)
    grant_referral_reward_if_eligible(row[3] if row else None, uid)
    try: await q.edit_message_text("✅ عضویت تایید شد.")
    except: pass
    await context.bot.send_message(chat_id=uid, text="به منوی اصلی خوش آمدید:", reply_markup=main_menu_kb())

async def withdraw_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID: return
    action, sid = q.data.split("_", 1)
    wid = int(sid)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, amount, status FROM withdrawals WHERE id=%s", (wid,))
        r = cur.fetchone()
        if not r: return
        uid, amt, st = r
        if st != "pending":
            await q.edit_message_text(f"قبلاً {st} شده.")
            return
        if action == "approve":
            cur.execute("UPDATE withdrawals SET status='approved', updated_at=NOW() WHERE id=%s", (wid,))
            await q.edit_message_text(f"✅ #{wid} تایید شد.")
            try: await context.bot.send_message(uid, f"✅ برداشت {amt:.4f} TRX تایید شد.")
            except: pass
            
            report_ch = get_setting("report_channel", "@BET_1XZX")
            channel_msg = f"✅ برداشت تایید شد\nUser: `{uid}`\nAmount: {amt:.4f} TRX"
            try:
                await context.bot.send_message(chat_id=report_ch, text=channel_msg, parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"❌ ارسال به کانال {report_ch} با خطا مواجه شد: {e}")
                try:
                    await context.bot.send_message(OWNER_ID, f"⚠️ برداشت #{wid} تایید شد، اما ارسال به کانال `{report_ch}` ناموفق بود:\n{e}")
                except: pass
        else:
            cur.execute("UPDATE withdrawals SET status='rejected', updated_at=NOW() WHERE id=%s", (wid,))
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amt, uid))
            await q.edit_message_text(f"❌ #{wid} رد شد و مبلغ برگشت.")
            try: await context.bot.send_message(uid, f"❌ برداشت {amt:.4f} TRX رد شد. مبلغ برگشت.")
            except: pass

async def on_error(update, context):
    logger.error(f"handler error: {context.error!r}", exc_info=context.error)

# ---------- MAIN ----------
def main():
    init_db()
    app = Application.builder().token(BOT_TOKEN).concurrent_updates(False).build()

    user_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={WAITING_PHONE: [MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), handle_phone)]},
        fallbacks=[CommandHandler("cancel", cancel_withdraw)],
        allow_reentry=True,
    )

    withdraw_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^💸 برداشت$"), withdraw_start)],
        states={
            WAITING_WITHDRAW_AMOUNT: [
                MessageHandler(filters.Regex("^❌ لغو$"), cancel_withdraw),
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)
            ],
            WAITING_WITHDRAW_ADDRESS: [
                MessageHandler(filters.Regex("^❌ لغو$"), cancel_withdraw),
                MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_address)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_withdraw)],
        allow_reentry=True,
    )

    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 پیام همگانی$"), admin_broadcast_start)],
        states={WAITING_BROADCAST: [MessageHandler(filters.Regex("^❌ لغو$"), cancel_broadcast), CommandHandler("cancel", cancel_broadcast), MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_send)]},
        fallbacks=[CommandHandler("cancel", cancel_broadcast)],
        allow_reentry=True,
    )

    set_channel_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^⚙️ تنظیم کانال گزارش$"), admin_set_channel_start)],
        states={WAITING_CHANNEL_ID: [
            MessageHandler(filters.Regex("^❌ لغو$"), admin_set_channel_cancel),
            CommandHandler("cancel", admin_set_channel_cancel),
            MessageHandler(filters.TEXT & ~filters.COMMAND, admin_set_channel_save)
        ]},
        fallbacks=[CommandHandler("cancel", admin_set_channel_cancel)],
        allow_reentry=True,
    )

    # هندلرهای منوی کاربر
    user_menu_h = MessageHandler(filters.Regex(r"^(💰 موجودی|👥 رفرال‌گیری|📋 برداشت‌های تایید شده)$"), user_menu_handler)
    
    # هندلرهای منوی ادمین
    admin_menu_h = MessageHandler(
        filters.Regex(r"^(📊 آمار کل|👥 لیست کاربران|⏳ برداشت‌های Pending|⬅️ بازگشت به منوی اصلی)$"),
        lambda u, c: (
            admin_stats(u, c) if u.message.text == "📊 آمار کل" else
            admin_users(u, c) if u.message.text == "👥 لیست کاربران" else
            admin_pending(u, c) if u.message.text == "⏳ برداشت‌های Pending" else
            admin_back_to_user_menu(u, c)
        )
    )

    app.add_handler(MessageHandler(filters.ALL, debug_update), group=-1)
    app.add_handler(broadcast_conv)
    app.add_handler(set_channel_conv)
    app.add_handler(withdraw_conv)
    app.add_handler(user_conv)
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(admin_menu_h)
    app.add_handler(user_menu_h)
    app.add_handler(CallbackQueryHandler(check_membership_cb, pattern=r"^check_membership$"))
    app.add_handler(CallbackQueryHandler(withdraw_cb, pattern=r"^(approve|reject)_\d+$"))
    app.add_error_handler(on_error)

    async def preflight():
        me = await app.bot.get_me()
        logger.info(f"✅ Bot: @{me.username}")
        await app.bot.delete_webhook(drop_pending_updates=True)

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(preflight())
    app.run_polling(allowed_updates=Update.ALL_TYPES, drop_pending_updates=False, close_loop=False)

if __name__ == "__main__":
    main()
