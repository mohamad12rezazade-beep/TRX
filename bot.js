import "dotenv/config";
import { Telegraf, Markup, session } from "telegraf";
import { createClient } from "@supabase/supabase-js";

const bot = new Telegraf(process.env.BOT_TOKEN);

const db = createClient(
  process.env.SUPABASE_URL,
  process.env.SUPABASE_SERVICE_ROLE_KEY
);

const ADMINS = (process.env.ADMIN_IDS || "")
  .split(",")
  .map(x => x.trim())
  .filter(Boolean);

const MIN_WITHDRAW = Number(process.env.MIN_WITHDRAW || 2);
const REFERRAL_REWARD = Number(process.env.REFERRAL_REWARD || 0.2);

const PAYMENT_CHANNEL =
  process.env.PAYMENT_CHANNEL || "@BET_1XZX";

bot.use(session());

const channels = [
  "@BET_1XZX",
  "@GAP_BAZIN1"
];

function admin(id) {
  return ADMINS.includes(String(id));
}

async function user(id) {
  const { data } = await db
    .from("users")
    .select("*")
    .eq("id", id)
    .single();

  return data;
}

async function createUser(ctx, ref = null) {
  const old = await user(ctx.from.id);

  if (old) {
    await db
      .from("users")
      .update({
        username: ctx.from.username || old.username,
        first_name: ctx.from.first_name || old.first_name
      })
      .eq("id", ctx.from.id);

    return old;
  }

  const { data } = await db
    .from("users")
    .insert({
      id: ctx.from.id,
      username: ctx.from.username || null,
      first_name: ctx.from.first_name || null,
      referred_by: ref,
      balance: 0
    })
    .select()
    .single();

  return data;
}

function menu() {
  return Markup.keyboard([
    ["💸 برداشت", "👥 رفرال‌گیری"],
    ["💰 موجودی", "📜 برداشت‌های تأییدشده"]
  ]).resize();
}

async function checkJoin(ctx) {
  for (const channel of channels) {
    try {
      const m = await ctx.telegram.getChatMember(
        channel,
        ctx.from.id
      );

      if (
        m.status === "left" ||
        m.status === "kicked"
      ) {
        return false;
      }
    } catch {
      return false;
    }
  }

  return true;
}

async function joinMessage(ctx) {
  return ctx.reply(
    "📢 برای استفاده از ربات ابتدا عضو کانال‌های زیر شوید:",
    Markup.inlineKeyboard([
      [
        Markup.button.url(
          "عضویت در کانال اول",
          "https://t.me/BET_1XZX"
        )
      ],
      [
        Markup.button.url(
          "عضویت در کانال دوم",
          "https://t.me/GAP_BAZIN1"
        )
      ],
      [
        Markup.button.callback(
          "✅ بررسی عضویت",
          "check_join"
        )
      ]
    ])
  );
}

bot.start(async ctx => {

  let ref = null;

  const arg = ctx.message.text.split(" ")[1];

  if (arg && /^\d+$/.test(arg)) {
    const id = Number(arg);

    if (id !== ctx.from.id) {
      const r = await user(id);

      if (r) {
        ref = id;
      }
    }
  }

  await createUser(ctx, ref);

  ctx.session.step = "phone";

  await ctx.reply(
    "🇮🇷 برای شروع شماره موبایل ایران خود را ارسال کنید.",
    Markup.keyboard([
      [
        Markup.button.contactRequest(
          "📱 ارسال شماره"
        )
      ]
    ])
      .resize()
      .oneTime()
  );
});

bot.on("contact", async ctx => {

  if (ctx.session.step !== "phone") {
    return;
  }

  let phone =
    ctx.message.contact.phone_number
      .replace(/\s/g, "");

  if (phone.startsWith("0098")) {
    phone = "+" + phone.substring(2);
  }

  if (!phone.startsWith("+98")) {
    return ctx.reply(
      "❌ فقط شماره‌های ایران قابل قبول است."
    );
  }

  await db
    .from("users")
    .update({
      phone,
      phone_verified: true
    })
    .eq("id", ctx.from.id);

  ctx.session.step = "join";

  await joinMessage(ctx);
});

bot.action("check_join", async ctx => {

  await ctx.answerCbQuery();

  const ok = await checkJoin(ctx);

  if (!ok) {
    return ctx.reply(
      "❌ هنوز در همه کانال‌ها عضو نشده‌اید."
    );
  }

  await rewardReferral(ctx.from.id);

  await ctx.reply(
    "✅ عضویت شما تأیید شد.",
    menu()
  );
});

async function rewardReferral(id) {

  const u = await user(id);

  if (!u) return;
  if (!u.phone_verified) return;
  if (!u.referred_by) return;
  if (u.referral_rewarded) return;

  const ref = await user(u.referred_by);

  if (!ref) return;

  await db
    .from("users")
    .update({
      balance:
        Number(ref.balance) +
        REFERRAL_REWARD
    })
    .eq("id", ref.id);

  await db
    .from("users")
    .update({
      referral_rewarded: true
    })
    .eq("id", id);
}

bot.hears("💰 موجودی", async ctx => {

  const u = await user(ctx.from.id);

  await ctx.reply(
    `💰 موجودی شما:\n\n${Number(u.balance).toFixed(2)} TRX`
  );
});

bot.hears("👥 رفرال‌گیری", async ctx => {

  const botUsername =
    process.env.BOT_USERNAME;

  const link =
    `https://t.me/${botUsername}?start=${ctx.from.id}`;

  const { count } = await db
    .from("users")
    .select("*", {
      count: "exact",
      head: true
    })
    .eq("referred_by", ctx.from.id)
    .eq("referral_rewarded", true);

  const total =
    (count || 0) * REFERRAL_REWARD;

  await ctx.reply(
    `👥 رفرال‌گیری\n\n` +
    `🔗 لینک شما:\n${link}\n\n` +
    `👤 تعداد رفرال: ${count || 0}\n` +
    `💰 درآمد: ${total.toFixed(2)} TRX`
  );
});

bot.hears("💸 برداشت", async ctx => {

  const u = await user(ctx.from.id);

  if (Number(u.balance) < MIN_WITHDRAW) {
    return ctx.reply(
      `❌ موجودی کافی نیست.\n\nحداقل برداشت: ${MIN_WITHDRAW} TRX`
    );
  }

  ctx.session.step =
    "withdraw_amount";

  await ctx.reply(
    `💸 مبلغ برداشت را وارد کنید.\n\nحداقل: ${MIN_WITHDRAW} TRX`
  );
});

bot.on("text", async ctx => {

  const step =
    ctx.session?.step;

  if (step === "withdraw_amount") {

    const amount =
      Number(ctx.message.text);

    if (!Number.isFinite(amount)) {
      return ctx.reply(
        "❌ مبلغ نامعتبر است."
      );
    }

    if (amount < MIN_WITHDRAW) {
      return ctx.reply(
        `❌ حداقل برداشت ${MIN_WITHDRAW} TRX است.`
      );
    }

    const u =
      await user(ctx.from.id);

    if (amount > Number(u.balance)) {
      return ctx.reply(
        "❌ موجودی شما کافی نیست."
      );
    }

    ctx.session.amount = amount;
    ctx.session.step =
      "withdraw_wallet";

    return ctx.reply(
      "🏦 آدرس کیف پول TRON را ارسال کنید."
    );
  }

  if (step === "withdraw_wallet") {

    const wallet =
      ctx.message.text.trim();

    const amount =
      ctx.session.amount;

    const u =
      await user(ctx.from.id);

    if (!u) {
      return ctx.reply(
        "❌ کاربر پیدا نشد."
      );
    }

    if (amount > Number(u.balance)) {
      return ctx.reply(
        "❌ موجودی کافی نیست."
      );
    }

    const { data, error } =
      await db
        .from("withdrawals")
        .insert({
          user_id: ctx.from.id,
          amount,
          wallet,
          status: "pending"
        })
        .select()
        .single();

    if (error) {
      console.log(error);

      return ctx.reply(
        "❌ خطا در ثبت برداشت."
      );
    }

    ctx.session = {};

    await ctx.reply(
      `✅ درخواست برداشت ثبت شد.\n\n` +
      `💰 مبلغ: ${amount} TRX\n` +
      `🆔 درخواست: ${data.id}`
    );

    for (const a of ADMINS) {

      await bot.telegram.sendMessage(
        a,

        `💸 برداشت جدید\n\n` +
        `🆔 User ID: ${ctx.from.id}\n` +
        `👤 Username: @${ctx.from.username || "ندارد"}\n` +
        `💰 مبلغ: ${amount} TRX\n` +
        `🏦 Wallet: ${wallet}\n` +
        `📋 ID: ${data.id}`,

        Markup.inlineKeyboard([
          [
            Markup.button.callback(
              "✅ تأیید",
              `approve_${data.id}`
            ),
            Markup.button.callback(
              "❌ رد",
              `reject_${data.id}`
            )
          ]
        ])
      );
    }
  }
});

bot.action(/^approve_(\d+)$/, async ctx => {

  if (!admin(ctx.from.id)) {
    return ctx.answerCbQuery(
      "⛔ دسترسی ندارید."
    );
  }

  const id =
    Number(ctx.match[1]);

  const { data: w } =
    await db
      .from("withdrawals")
      .select("*")
      .eq("id", id)
      .single();

  if (!w || w.status !== "pending") {
    return ctx.answerCbQuery(
      "این برداشت قبلاً بررسی شده."
    );
  }

  const u =
    await user(w.user_id);

  if (!u) return;

  if (Number(u.balance) < Number(w.amount)) {
    return ctx.answerCbQuery(
      "موجودی کافی نیست."
    );
  }

  await db
    .from("users")
    .update({
      balance:
        Number(u.balance) -
        Number(w.amount)
    })
    .eq("id", u.id);

  await db
    .from("withdrawals")
    .update({
      status: "approved",
      admin_id: ctx.from.id,
      processed_at:
        new Date().toISOString()
    })
    .eq("id", id);

  await bot.telegram.sendMessage(
    w.user_id,

    `✅ برداشت انجام شد.\n\n` +
    `💰 مبلغ: ${w.amount} TRX`
  );

  await bot.telegram.sendMessage(
    PAYMENT_CHANNEL,

    `💸 برداشت انجام شد\n\n` +
    `🆔 ID: ${w.user_id}\n` +
    `💰 مبلغ: ${w.amount} TRX`
  );

  await ctx.editMessageText(
    `✅ برداشت #${id} تأیید شد.`
  );
});

bot.action(/^reject_(\d+)$/, async ctx => {

  if (!admin(ctx.from.id)) {
    return ctx.answerCbQuery(
      "⛔ دسترسی ندارید."
    );
  }

  const id =
    Number(ctx.match[1]);

  const { data: w } =
    await db
      .from("withdrawals")
      .select("*")
      .eq("id", id)
      .single();

  if (!w || w.status !== "pending") {
    return ctx.answerCbQuery(
      "این برداشت قبلاً بررسی شده."
    );
  }

  await db
    .from("withdrawals")
    .update({
      status: "rejected",
      admin_id: ctx.from.id,
      processed_at:
        new Date().toISOString()
    })
    .eq("id", id);

  await bot.telegram.sendMessage(
    w.user_id,

    `❌ برداشت شما رد شد.\n\n` +
    `💰 مبلغ: ${w.amount} TRX`
  );

  await ctx.editMessageText(
    `❌ برداشت #${id} رد شد.`
  );
});

bot.hears(
  "📜 برداشت‌های تأییدشده",
  async ctx => {

    const { data } =
      await db
        .from("withdrawals")
        .select("*")
        .eq("user_id", ctx.from.id)
        .eq("status", "approved")
        .order("created_at", {
          ascending: false
        });

    if (!data?.length) {
      return ctx.reply(
        "📜 برداشت تأییدشده‌ای ندارید."
      );
    }

    let text =
      "📜 برداشت‌های تأییدشده:\n\n";

    for (const w of data) {

      text +=
        `🆔 ${w.id}\n` +
        `💰 ${w.amount} TRX\n` +
        `📅 ${new Date(w.created_at).toLocaleString("fa-IR")}\n\n`;
    }

    await ctx.reply(text);
  }
);

bot.command("admin", async ctx => {

  if (!admin(ctx.from.id)) {
    return ctx.reply(
      "⛔ دسترسی ندارید."
    );
  }

  await ctx.reply(
    "👑 پنل مدیریت",

    Markup.inlineKeyboard([
      [
        Markup.button.callback(
          "📊 آمار کاربران",
          "stats"
        )
      ],
      [
        Markup.button.callback(
          "💰 موجودی کاربران",
          "balances"
        )
      ],
      [
        Markup.button.callback(
          "👥 رفرال‌گیری‌ها",
          "refs"
        )
      ],
      [
        Markup.button.callback(
          "💸 برداشت‌های در انتظار",
          "withdrawals"
        )
      ]
    ])
  );
});

bot.action("stats", async ctx => {

  if (!admin(ctx.from.id)) return;

  const { count } =
    await db
      .from("users")
      .select("*", {
        count: "exact",
        head: true
      });

  const { data } =
    await db
      .from("users")
      .select("balance");

  const total =
    (data || []).reduce(
      (a, x) =>
        a + Number(x.balance),
      0
    );

  await ctx.reply(
    `📊 آمار کاربران\n\n` +
    `👤 کاربران: ${count || 0}\n` +
    `💰 مجموع موجودی: ${total.toFixed(2)} TRX`
  );
});

bot.action("balances", async ctx => {

  if (!admin(ctx.from.id)) return;

  const { data } =
    await db
      .from("users")
      .select("id,username,balance")
      .order("balance", {
        ascending: false
      })
      .limit(50);

  let text =
    "💰 موجودی کاربران\n\n";

  for (const u of data || []) {

    text +=
      `🆔 ${u.id}\n` +
      `👤 @${u.username || "ندارد"}\n` +
      `💰 ${Number(u.balance).toFixed(2)} TRX\n\n`;
  }

  await ctx.reply(text);
});

bot.action("refs", async ctx => {

  if (!admin(ctx.from.id)) return;

  const { data } =
    await db
      .from("users")
      .select("id,username")
      .not("referred_by", "is", null)
      .eq("referral_rewarded", true);

  const counts = {};

  for (const u of data || []) {
    counts[u.referred_by] =
      (counts[u.referred_by] || 0) + 1;
  }

  const ids =
    Object.keys(counts);

  if (!ids.length) {
    return ctx.reply(
      "👥 هنوز رفرالی ثبت نشده."
    );
  }

  let text =
    "👥 رفرال‌گیری‌ها\n\n";

  for (const id of ids) {

    const u =
      await user(Number(id));

    text +=
      `🆔 ${id}\n` +
      `👤 @${u?.username || "ندارد"}\n` +
      `👥 تعداد: ${counts[id]}\n` +
      `💰 پاداش: ${(counts[id] * REFERRAL_REWARD).toFixed(2)} TRX\n\n`;
  }

  await ctx.reply(text);
});

bot.action("withdrawals", async ctx => {

  if (!admin(ctx.from.id)) return;

  const { data } =
    await db
      .from("withdrawals")
      .select("*")
      .eq("status", "pending")
      .order("created_at", {
        ascending: false
      });

  if (!data?.length) {
    return ctx.reply(
      "✅ برداشت در انتظار وجود ندارد."
    );
  }

  for (const w of data) {

    await ctx.reply(

      `💸 برداشت\n\n` +
      `🆔 User ID: ${w.user_id}\n` +
      `💰 مبلغ: ${w.amount} TRX\n` +
      `🏦 Wallet: ${w.wallet}\n` +
      `📋 ID: ${w.id}`,

      Markup.inlineKeyboard([
        [
          Markup.button.callback(
            "✅ تأیید",
            `approve_${w.id}`
          ),
          Markup.button.callback(
            "❌ رد",
            `reject_${w.id}`
          )
        ]
      ])
    );
  }
});

bot.catch(err => {
  console.error(err);
});

bot.launch();

console.log("BOT STARTED");

process.once("SIGINT", () => bot.stop("SIGINT"));
process.once("SIGTERM", () => bot.stop("SIGTERM"));
