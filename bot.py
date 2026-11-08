import logging
import os
import re
import sys
import asyncio
import contextlib

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
from telegram.error import TelegramError, Forbidden, RetryAfter

# ---------- ENV ----------
def _clean_token(t: str) -> str:
    if not t:
        return ""
    t = t.strip().replace("\n", "").replace("\r", "").replace("\t", "").replace(" ", "")
    if t.lower().startswith("bot"):
        t = t[3:]
    return t

BOT_TOKEN = _clean_token(os.environ.get("BOT_TOKEN", ""))
OWNER_ID_RAW = (os.environ.get("OWNER_ID", "") or "").strip()
DATABASE_URL = (os.environ.get("DATABASE_URL", "") or "").strip()

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

if not BOT_TOKEN:
    logger.critical("❌ BOT_TOKEN خالی")
    sys.exit(1)
if not re.fullmatch(r"\d+:[A-Za-z0-9_-]+", BOT_TOKEN):
    logger.critical(f"❌ BOT_TOKEN shape invalid: len={len(BOT_TOKEN)}")
    sys.exit(1)
if not OWNER_ID_RAW.isdigit():
    logger.critical(f"❌ OWNER_ID باید عدد باشد: {OWNER_ID_RAW!r}")
    sys.exit(1)
if not DATABASE_URL:
    logger.critical("❌ DATABASE_URL خالی")
    sys.exit(1)

OWNER_ID = int(OWNER_ID_RAW)

# وضعیت‌ها
(
    WAITING_PHONE,
    WAITING_WITHDRAW_AMOUNT,
    WAITING_WITHDRAW_ADDRESS,
    WAITING_BROADCAST
) = range(4)

REQUIRED_CHANNELS = ["@BET_1XZX", "@GAP_BAZIN1"]
MIN_WITHDRAW = 2.0
REFERRAL_REWARD = 0.2

# ---------- DB ----------
def db():
    return psycopg.connect(DATABASE_URL, autocommit=True)

def _table_has_expected_columns(cur, table, expected):
    cur.execute("SELECT column_name FROM information_schema.columns WHERE table_name = %s", (table,))
    cols = {r[0] for r in cur.fetchall()}
    if not cols:
        return False
    missing = [c for c in expected if c not in cols]
    if missing:
        logger.warning(f"⚠️ جدول {table} ستون‌های {missing} را ندارد → بازسازی")
        return False
    return True

def init_db():
    with db() as conn, conn.cursor() as cur:
        if not _table_has_expected_columns(cur, "users", [
            "user_id", "username", "phone_number", "referrer_id",
            "balance", "is_verified", "is_joined_channels", "created_at"
        ]):
            cur.execute("DROP TABLE IF EXISTS users CASCADE;")
            cur.execute("""
                CREATE TABLE users (
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

        if not _table_has_expected_columns(cur, "withdrawals", [
            "id", "user_id", "amount", "wallet_address", "status", "created_at", "updated_at"
        ]):
            cur.execute("DROP TABLE IF EXISTS withdrawals CASCADE;")
            cur.execute("""
                CREATE TABLE withdrawals (
                    id SERIAL PRIMARY KEY,
                    user_id BIGINT,
                    amount DOUBLE PRECISION NOT NULL,
                    wallet_address TEXT NOT NULL,
                    status TEXT DEFAULT 'pending',
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

        if not _table_has_expected_columns(cur, "referral_logs", [
            "id", "referrer_id", "referred_id", "reward_amount", "created_at"
        ]):
            cur.execute("DROP TABLE IF EXISTS referral_logs CASCADE;")
            cur.execute("""
                CREATE TABLE referral_logs (
                    id SERIAL PRIMARY KEY,
                    referrer_id BIGINT,
                    referred_id BIGINT UNIQUE,
                    reward_amount DOUBLE PRECISION,
                    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                );
            """)

        if not _table_has_expected_columns(cur, "required_channels", ["channel_username"]):
            cur.execute("DROP TABLE IF EXISTS required_channels CASCADE;")
            cur.execute("""
                CREATE TABLE required_channels (
                    channel_username TEXT PRIMARY KEY
                );
            """)
        for ch in REQUIRED_CHANNELS:
            cur.execute(
                "INSERT INTO required_channels(channel_username) VALUES (%s) "
                "ON CONFLICT (channel_username) DO NOTHING",
                (ch,),
            )
    logger.info("✅ DB ready")

def get_user(uid):
    with db() as conn, conn.cursor() as cur:
        cur.execute("""
            SELECT user_id, username, phone_number, referrer_id,
                   balance, is_verified, is_joined_channels, created_at
            FROM users WHERE user_id=%s
        """, (uid,))
        return cur.fetchone()

def get_all_user_ids():
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id FROM users")
        return [row[0] for row in cur.fetchall()]

def create_user_if_needed(uid, username=None, referrer_id=None):
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT 1 FROM users WHERE user_id=%s", (uid,))
        if cur.fetchone():
            if username:
                cur.execute("UPDATE users SET username=%s WHERE user_id=%s", (username, uid))
        else:
            cur.execute(
                "INSERT INTO users(user_id, username, referrer_id) VALUES (%s,%s,%s)",
                (uid, username, referrer_id),
            )

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
    if not referrer_id or referrer_id == referred_id:
        return
    try:
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT is_verified, is_joined_channels FROM users WHERE user_id=%s", (referred_id,))
            r = cur.fetchone()
            if not r or not r[0] or not r[1]:
                return
            cur.execute("SELECT 1 FROM referral_logs WHERE referred_id=%s", (referred_id,))
            if cur.fetchone():
                return
            cur.execute(
                "INSERT INTO referral_logs(referrer_id, referred_id, reward_amount) VALUES (%s,%s,%s)",
                (referrer_id, referred_id, REFERRAL_REWARD),
            )
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (REFERRAL_REWARD, referrer_id))
        logger.info(f"🎁 referral reward to {referrer_id} for {referred_id}")
    except Exception as e:
        logger.error(f"referral reward failed: {e}", exc_info=True)

# ---------- helpers ----------
def normalize_iran_phone(p: str):
    p = (p or "").strip().replace(" ", "").replace("-", "")
    if p.startswith("+98"):
        p = "0" + p[3:]
    elif p.startswith("0098"):
        p = "0" + p[4:]
    elif p.startswith("98") and len(p) == 12:
        p = "0" + p[2:]
    return p if re.fullmatch(r"09\d{9}", p) else None

def is_valid_tron(a: str) -> bool:
    return bool(re.fullmatch(r"T[a-zA-Z0-9]{33}", a))

async def check_members(bot, uid) -> bool:
    for ch in REQUIRED_CHANNELS:
        try:
            m = await bot.get_chat_member(chat_id=ch, user_id=uid)
            if m.status not in (ChatMember.MEMBER, ChatMember.ADMINISTRATOR, ChatMember.OWNER):
                logger.info(f"user {uid} not in {ch} status={m.status}")
                return False
        except Exception as e:
            logger.error(f"get_chat_member {ch} for {uid}: {e}")
            return False
    return True

def main_menu_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("💸 برداشت"), KeyboardButton("👥 رفرال‌گیری")],
            [KeyboardButton("💰 موجودی"), KeyboardButton("📋 برداشت‌های تایید شده")],
        ],
        resize_keyboard=True,
    )

def admin_menu_kb():
    return ReplyKeyboardMarkup(
        [
            [KeyboardButton("📊 آمار کل"), KeyboardButton("👥 لیست کاربران")],
            [KeyboardButton("⏳ برداشت‌های Pending"), KeyboardButton("📢 پیام همگانی")],
            [KeyboardButton("⬅️ بازگشت به منوی اصلی")]
        ],
        resize_keyboard=True,
    )

def phone_kb():
    return ReplyKeyboardMarkup(
        [[KeyboardButton("📱 ارسال شماره من", request_contact=True)]],
        resize_keyboard=True,
        one_time_keyboard=False,
    )

async def ask_phone(msg):
    await msg.reply_text(
        "📱 شماره موبایل ایران را ارسال کنید:\n"
        "09xxxxxxxxx | +989xxxxxxxxx | 00989xxxxxxxxx\n\n"
        "می‌توانید از دکمه زیر استفاده کنید:",
        reply_markup=phone_kb(),
    )

async def ask_join(msg):
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("📢 کانال ۱", url="https://t.me/BET_1XZX")],
        [InlineKeyboardButton("📢 کانال ۲", url="https://t.me/GAP_BAZIN1")],
        [InlineKeyboardButton("✅ بررسی عضویت", callback_data="check_membership")],
    ])
    await msg.reply_text(
        "⚠️ برای استفاده، در این کانال‌ها عضو شوید:\n\n"
        "1️⃣ @BET_1XZX\n2️⃣ @GAP_BAZIN1\n\n"
        "سپس «بررسی عضویت» را بزنید.",
        reply_markup=kb,
    )

# ---------- debug ----------
async def debug_update(update: Update, context: ContextTypes.DEFAULT_TYPE):
    try:
        typ = "callback" if update.callback_query else "message" if update.message else "other"
        uid = update.effective_user.id if update.effective_user else "?"
        txt = update.effective_message.text if update.effective_message and update.effective_message.text else ""
        logger.info(f"🔔 UPDATE id={update.update_id} type={typ} from={uid} text={txt!r}")
    except Exception as e:
        logger.warning(f"debug_update err: {e}")

# ---------- handlers ----------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    logger.info(f"📥 ENTER start for {u.id}")
    try:
        referrer_id = None
        if context.args:
            a = context.args[0]
            if a.startswith("ref_"):
                try:
                    referrer_id = int(a[4:])
                except ValueError:
                    referrer_id = None
                if referrer_id == u.id:
                    referrer_id = None

        create_user_if_needed(u.id, u.username, referrer_id)
        row = get_user(u.id)

        if not row:
            await update.message.reply_text("❌ خطای دیتابیس. دوباره تلاش کن.")
            return ConversationHandler.END

        is_verified = bool(row[5])
        is_joined = bool(row[6])
        ref_id = row[3]

        if not is_verified:
            await ask_phone(update.message)
            return WAITING_PHONE

        if not is_joined:
            if await check_members(context.bot, u.id):
                set_joined(u.id)
                grant_referral_reward_if_eligible(ref_id, u.id)
                await update.message.reply_text("✅ خوش آمدید!", reply_markup=main_menu_kb())
                return ConversationHandler.END
            await ask_join(update.message)
            return ConversationHandler.END

        await update.message.reply_text("✅ به منوی اصلی خوش آمدید:", reply_markup=main_menu_kb())
        return ConversationHandler.END
    except Exception as e:
        logger.error(f"start crashed: {e}", exc_info=True)
        try:
            await update.message.reply_text("❌ خطای داخلی. دوباره تلاش کن.")
        except Exception:
            pass
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
    ref_id = row[3] if row else None

    if await check_members(context.bot, u.id):
        set_joined(u.id)
        grant_referral_reward_if_eligible(ref_id, u.id)
        await update.message.reply_text("✅ شماره و عضویت تایید شد.", reply_markup=main_menu_kb())
    else:
        await update.message.reply_text("✅ شماره تایید شد.")
        await ask_join(update.message)
    return ConversationHandler.END

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
    try:
        await q.edit_message_text("✅ عضویت تایید شد.")
    except Exception:
        pass
    await context.bot.send_message(chat_id=uid, text="به منوی اصلی خوش آمدید:", reply_markup=main_menu_kb())

async def menu_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = update.message.text
    u = update.effective_user
    row = get_user(u.id)
    if not row or not row[5] or not row[6]:
        await update.message.reply_text("ابتدا /start را بزن.")
        return

    if text == "💰 موجودی":
        await update.message.reply_text(f"💰 موجودی شما: {get_balance(u.id):.4f} TRX")

    elif text == "👥 رفرال‌گیری":
        link = f"https://t.me/{context.bot.username}?start=ref_{u.id}"
        with db() as conn, conn.cursor() as cur:
            cur.execute("SELECT COUNT(*), COALESCE(SUM(reward_amount),0) FROM referral_logs WHERE referrer_id=%s", (u.id,))
            cnt, total = cur.fetchone()
        await update.message.reply_text(
            f"👥 لینک رفرال:\n{link}\n\nتعداد دعوت موفق: {cnt}\nمجموع پاداش: {total:.2f} TRX\nپاداش هر دعوت: {REFERRAL_REWARD} TRX"
        )

    elif text == "💸 برداشت":
        bal = get_balance(u.id)
        if bal < MIN_WITHDRAW:
            await update.message.reply_text(f"❌ حداقل برداشت {MIN_WITHDRAW} TRX. موجودی: {bal:.4f}")
            return
        await update.message.reply_text(f"موجودی: {bal:.4f} TRX\nمبلغ برداشت (حداقل {MIN_WITHDRAW}):")
        return WAITING_WITHDRAW_AMOUNT

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

async def withdraw_amount(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    try:
        amt = float(update.message.text.strip())
    except ValueError:
        await update.message.reply_text("❌ عدد معتبر وارد کن.")
        return WAITING_WITHDRAW_AMOUNT
    if amt < MIN_WITHDRAW:
        await update.message.reply_text(f"❌ حداقل {MIN_WITHDRAW} TRX.")
        return WAITING_WITHDRAW_AMOUNT
    if amt > get_balance(u.id):
        await update.message.reply_text("❌ موجودی کافی نیست.")
        return WAITING_WITHDRAW_AMOUNT
    context.user_data["w_amount"] = amt
    await update.message.reply_text("آدرس TRON (شروع با T، 34 کاراکتر):")
    return WAITING_WITHDRAW_ADDRESS

async def withdraw_address(update: Update, context: ContextTypes.DEFAULT_TYPE):
    u = update.effective_user
    addr = update.message.text.strip()
    amt = context.user_data.get("w_amount")
    if not amt:
        await update.message.reply_text("از منوی برداشت شروع کن.")
        return ConversationHandler.END
    if not is_valid_tron(addr):
        await update.message.reply_text("❌ آدرس TRON نامعتبر.")
        return WAITING_WITHDRAW_ADDRESS
    if amt > get_balance(u.id):
        await update.message.reply_text("❌ موجودی کافی نیست.")
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
        await context.bot.send_message(
            OWNER_ID,
            f"🔔 برداشت #{wid}\nUser: `{u.id}`\nAmount: {amt:.4f} TRX\nWallet: `{addr}`",
            parse_mode=ParseMode.MARKDOWN, reply_markup=kb,
        )
    except Exception as e:
        logger.error(f"send owner: {e}")

    await update.message.reply_text("✅ ثبت شد و برای ادمین ارسال شد.", reply_markup=main_menu_kb())
    return ConversationHandler.END

async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("لغو شد.", reply_markup=main_menu_kb())
    return ConversationHandler.END

# ---------- ADMIN PANEL ----------
async def admin(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID:
        return
    await update.message.reply_text(
        "👨‍💻 **پنل مدیریت**\n\nلطفاً یکی از گزینه‌های زیر را انتخاب کنید:",
        reply_markup=admin_menu_kb(),
        parse_mode=ParseMode.MARKDOWN
    )

async def admin_stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT COUNT(*) FROM users"); total = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM users WHERE is_verified=TRUE"); ver = cur.fetchone()[0]
        cur.execute("SELECT COALESCE(SUM(balance),0) FROM users"); tb = cur.fetchone()[0]
        cur.execute("SELECT COUNT(*) FROM withdrawals WHERE status='pending'"); pw = cur.fetchone()[0]
    await update.message.reply_text(
        f"📊 **آمار کلی**\n\n👥 کاربران: {total}\n✅ تایید شده: {ver}\n💰 مجموع موجودی: {tb:.4f} TRX\n⏳ برداشت‌های Pending: {pw}",
        reply_markup=admin_menu_kb(),
        parse_mode=ParseMode.MARKDOWN
    )

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
        kb = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ تایید", callback_data=f"approve_{wid}"),
            InlineKeyboardButton("❌ رد", callback_data=f"reject_{wid}"),
        ]])
        await update.message.reply_text(f"#{wid} User `{uid}` {amt:.4f} TRX\n`{w}`", parse_mode=ParseMode.MARKDOWN, reply_markup=kb)

async def admin_back_to_user_menu(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    await update.message.reply_text("بازگشت به منوی اصلی.", reply_markup=main_menu_kb())

# ---------- BROADCAST ----------
async def admin_broadcast_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    await update.message.reply_text(
        "📢 **ارسال پیام همگانی**\n\n"
        "لطفاً پیام خود (متن، عکس، ویدیو یا فوروارد) را همینجا ارسال کنید تا برای تمام کاربران ربات ارسال شود.\n"
        "برای لغو، دستور /cancel را بزنید.",
        reply_markup=ReplyKeyboardMarkup([[KeyboardButton("❌ لغو")]], resize_keyboard=True),
        parse_mode=ParseMode.MARKDOWN
    )
    return WAITING_BROADCAST

async def broadcast_send(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    u_ids = get_all_user_ids()
    total = len(u_ids)
    
    status_msg = await update.message.reply_text(f"⏳ در حال ارسال به {total} کاربر...")
    
    success = 0
    failed = 0
    
    for uid in u_ids:
        try:
            await update.message.copy(chat_id=uid)
            success += 1
        except Forbidden:
            failed += 1
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await update.message.copy(chat_id=uid)
                success += 1
            except:
                failed += 1
        except Exception:
            failed += 1
        
        # تاخیر کوتاه برای جلوگیری از بلاک شدن توسط تلگرام (حدود ۳۰ پیام در ثانیه مجاز است)
        await asyncio.sleep(0.05)
        
        if (success + failed) % 20 == 0:
            with contextlib.suppress(Exception):
                await status_msg.edit_text(f"⏳ در حال ارسال... ({success + failed}/{total})")

    await status_msg.edit_text(
        f"✅ **ارسال همگانی پایان یافت**\n\n"
        f"موفق: {success}\n"
        f"ناموفق (بلاک شده یا حذف کرده): {failed}",
        reply_markup=admin_menu_kb(),
        parse_mode=ParseMode.MARKDOWN
    )
    return ConversationHandler.END

async def cancel_broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != OWNER_ID: return
    await update.message.reply_text("ارسال همگانی لغو شد.", reply_markup=admin_menu_kb())
    return ConversationHandler.END

# ---------- CALLBACKS ----------
async def withdraw_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
    if q.from_user.id != OWNER_ID: return
    action, sid = q.data.split("_", 1)
    wid = int(sid)
    with db() as conn, conn.cursor() as cur:
        cur.execute("SELECT user_id, amount, status FROM withdrawals WHERE id=%s", (wid,))
        r = cur.fetchone()
        if not r:
            await q.edit_message_text("یافت نشد.")
            return
        uid, amt, st = r
        if st != "pending":
            await q.edit_message_text(f"قبلاً {st} شده.")
            return
        if action == "approve":
            cur.execute("UPDATE withdrawals SET status='approved', updated_at=NOW() WHERE id=%s", (wid,))
            await q.edit_message_text(f"✅ #{wid} تایید شد.")
            try:
                await context.bot.send_message(uid, f"✅ برداشت {amt:.4f} TRX تایید شد.")
            except Exception as e:
                logger.error(f"notify user: {e}")
            try:
                await context.bot.send_message("@BET_1XZX", f"✅ برداشت تایید شد\nUser: `{uid}`\nAmount: {amt:.4f} TRX", parse_mode=ParseMode.MARKDOWN)
            except Exception as e:
                logger.error(f"publish channel: {e}")
        else:
            cur.execute("UPDATE withdrawals SET status='rejected', updated_at=NOW() WHERE id=%s", (wid,))
            cur.execute("UPDATE users SET balance=balance+%s WHERE user_id=%s", (amt, uid))
            await q.edit_message_text(f"❌ #{wid} رد شد و مبلغ برگشت.")
            try:
                await context.bot.send_message(uid, f"❌ برداشت {amt:.4f} TRX رد شد. مبلغ برگشت.")
            except Exception as e:
                logger.error(f"notify user: {e}")

async def on_error(update, context):
    logger.error(f"handler error: {context.error!r}", exc_info=context.error)


# ---------- MAIN ----------
def main():
    logger.info("🔍 init DB...")
    init_db()

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(False)
        .build()
    )

    # 1. مکالمه ثبت‌نام/شماره/برداشت
    user_conv = ConversationHandler(
        entry_points=[CommandHandler("start", start)],
        states={
            WAITING_PHONE: [MessageHandler(filters.CONTACT | (filters.TEXT & ~filters.COMMAND), handle_phone)],
            WAITING_WITHDRAW_AMOUNT: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_amount)],
            WAITING_WITHDRAW_ADDRESS: [MessageHandler(filters.TEXT & ~filters.COMMAND, withdraw_address)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        allow_reentry=True,
    )
    
    # 2. مکالمه ارسال پیام همگانی
    broadcast_conv = ConversationHandler(
        entry_points=[MessageHandler(filters.Regex("^📢 پیام همگانی$"), admin_broadcast_start)],
        states={
            WAITING_BROADCAST: [
                MessageHandler(filters.Regex("^❌ لغو$"), cancel_broadcast),
                CommandHandler("cancel", cancel_broadcast),
                MessageHandler(filters.ALL & ~filters.COMMAND, broadcast_send)
            ]
        },
        fallbacks=[CommandHandler("cancel", cancel_broadcast)],
        allow_reentry=True,
    )

    # 3. کیبوردهای متنی
    user_menu = MessageHandler(
        filters.Regex(r"^(💰 موجودی|👥 رفرال‌گیری|💸 برداشت|📋 برداشت‌های تایید شده)$"),
        menu_button,
    )
    
    admin_menu = MessageHandler(
        filters.Regex(r"^(📊 آمار کل|👥 لیست کاربران|⏳ برداشت‌های Pending|⬅️ بازگشت به منوی اصلی)$"),
        lambda u, c: (
            admin_stats(u, c) if u.message.text == "📊 آمار کل" else
            admin_users(u, c) if u.message.text == "👥 لیست کاربران" else
            admin_pending(u, c) if u.message.text == "⏳ برداشت‌های Pending" else
            admin_back_to_user_menu(u, c)
        )
    )

    # ترتیب مهم: دیباگر، سپس دستورات، سپس کیبوردها
    app.add_handler(MessageHandler(filters.ALL, debug_update), group=-1)
    app.add_handler(broadcast_conv) # مکالمه همگانی بالاتر از کیبورد ادمین باشد
    app.add_handler(user_conv)
    app.add_handler(CommandHandler("admin", admin))
    app.add_handler(admin_menu)
    app.add_handler(user_menu)
    app.add_handler(CallbackQueryHandler(check_membership_cb, pattern=r"^check_membership$"))
    app.add_handler(CallbackQueryHandler(withdraw_cb, pattern=r"^(approve|reject)_\d+$"))
    app.add_error_handler(on_error)

    async def preflight():
        try:
            me = await app.bot.get_me()
            logger.info(f"✅ getMe OK: @{me.username} (id={me.id})")
        except Exception as e:
            logger.critical(f"❌ getMe FAILED: {e!r}", exc_info=True)
            raise
        try:
            ok = await app.bot.delete_webhook(drop_pending_updates=True)
            logger.info(f"✅ deleteWebhook: {ok}")
        except Exception as e:
            logger.warning(f"deleteWebhook: {e}")

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    loop.run_until_complete(preflight())

    logger.info("✅ starting polling...")
    app.run_polling(
        allowed_updates=Update.ALL_TYPES,
        drop_pending_updates=False,
        close_loop=False,
    )

if __name__ == "__main__":
    main()
