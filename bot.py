#!/usr/bin/env python3
"""Telegram reminder bot.

- Upload a picture of a schedule -> OCR extracts times (+ labels) -> confirm -> reminders
- Or just type times as text: "09:30 gym, 14:00 lunch"
- When a reminder fires: buttons DONE / SKIPPED / COMPENSATED
    Done        -> logged, no charge
    Skipped     -> charged the person's configured amount
    Compensated -> deducts HALF the configured amount from the outstanding balance
- Daily summary send at SUMMARY_TIME (default 23:00 Asia/Tashkent)

Env vars: BOT_TOKEN (required), TZ_NAME, SUMMARY_TIME, OCR_LANG, DB_PATH.
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from datetime import datetime, timedelta, time as dtime, timezone
from zoneinfo import ZoneInfo

from telegram import BotCommand, InlineKeyboardButton, InlineKeyboardMarkup, ReplyKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.error import BadRequest
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import extract
import storage

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO
)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("reminder-bot")

TOKEN = os.getenv("BOT_TOKEN", "").strip()
if not TOKEN:
    sys.exit("BOT_TOKEN is not set. Create .env (see README) or `export BOT_TOKEN=...`.")

TZ_NAME = os.getenv("TZ_NAME", "Asia/Tashkent")
TZ = ZoneInfo(TZ_NAME)
SUMMARY_HH, SUMMARY_MM = (int(x) for x in os.getenv("SUMMARY_TIME", "23:00").split(":"))

PRESET_CHARGES = [100_000, 500_000, 1_000_000]  # quick-pick buttons for charge amount
MAX_CHARGE = 50_000_000

# CI mode (GitHub Actions): stay awake N seconds per run, then exit cleanly.
# 0 = run forever (normal server deployment).
AWAKE_SECONDS = int(os.getenv("AWAKE_SECONDS", "0") or "0")

EMOJI = {"pending": "⏳", "fired": "❓", "done": "✅", "skipped": "❌", "compensated": "🟩", "cancelled": "🗑"}

# Permanent on-screen button keyboard (labels are intercepted in on_text)
ANTICIPATING = [
    "📸 Photo of schedule",
    "⌨️ Type times",
    "📋 Reminders",
    "💳 Balance",
    "💰 Charge",
    "📊 Summary",
    "📖 Help",
]

COMMANDS = [
    ("start", "Account status / setup"),
    ("help", "Full guide"),
    ("list", "Upcoming reminders"),
    ("cancel", "Cancel a reminder: /cancel 12"),
    ("charge", "View or change your charge amount"),
    ("balance", "Outstanding balance"),
    ("summary", "Today's summary right now"),
]


def main_menu() -> ReplyKeyboardMarkup:
    rows = [ANTICIPATING[0:2], ANTICIPATING[2:4], ANTICIPATING[4:6], ANTICIPATING[6:8]]
    return ReplyKeyboardMarkup(rows, resize_keyboard=True)


# Shortcut buttons (left to right) -> handler keys
COMMAND_KEYS = ["photo", "times", "list", "balance", "charge", "summary", "help"]
BUTTON_ACTIONS = dict(zip(ANTICIPATING, COMMAND_KEYS))


# ------------------------------------------------------------------ helpers

def money(amount: int) -> str:
    """Format so'm with space thousand separators, sign preserved."""
    sign = "-" if amount < 0 else ""
    return f"{sign}{abs(amount):,}".replace(",", " ") + " so'm"


def now_tz() -> datetime:
    return datetime.now(TZ)


def utc_iso(dt_local: datetime) -> str:
    return dt_local.astimezone(timezone.utc).isoformat()


def local_from_iso(iso: str) -> datetime:
    return datetime.fromisoformat(iso).astimezone(TZ)


def fmt_local(dt_local: datetime) -> str:
    return dt_local.strftime("%H:%M · %a %d %b")


def charge_keyboard(prefix: str) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(f"{money(a)}", callback_data=f"{prefix}charge:set:{a}")]
        for a in PRESET_CHARGES
    ]
    rows.append([InlineKeyboardButton("✍️ Enter custom amount", callback_data=f"{prefix}charge:custom")])
    return InlineKeyboardMarkup(rows)


def ask_charge_amount(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Prompt a brand-new (or re-configuring) user for their per-skip charge amount."""
    context.user_data["awaiting_charge"] = "set"
    update.effective_message.reply_text(
        "💰 First things first - what's the charge you pay <b>per skipped reminder</b>?\n\n"
        "Pick one or enter your own amount:",
        parse_mode=ParseMode.HTML,
        reply_markup=charge_keyboard(""),
    )


# -------------------------------------------------------------- user flows

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = storage.get_user(update.effective_user.id)
    if user is not None:
        upcoming = storage.user_reminders(user["user_id"], ("pending", "fired"))
        await update.message.reply_text(
            f"👋 <b>Welcome back, {user['name']}</b>\n\n"
            f"💰 Charge per skip: <b>{money(user['charge_amount'])}</b>\n"
            f"💳 Outstanding balance: <b>{money(user['balance'])}</b>\n"
            f"⏰ Upcoming reminders: <b>{len(upcoming)}</b>\n\n"
            "📸 Send a photo of today's schedule (or type times like <code>09:30 gym</code>) "
            "to add reminders.\n"
            "Use /help to see all commands.",
            parse_mode=ParseMode.HTML,
        )
        return

    await update.message.reply_text(
        "👋 Hi! I'm your reminder bot.\n\n"
        "Here's what I do:\n"
        "• You send me a 📸 picture of your schedule -> I read the times and set reminders\n"
        "• You can also just type times: <code>09:30 gym</code>\n"
        "• When a reminder fires you press <b>Done</b>, <b>Skipped</b> or <b>Compensated</b>\n"
        "• Skips add a charge to your balance, compensation refunds half - and you get a "
        "📊 daily summary\n",
        parse_mode=ParseMode.HTML,
    )
    ask_charge_amount(update, context)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>📖 How to use me</b>\n\n"
        "📸 <b>Add reminders from a picture</b>\n"
        "Just send a photo containing times (a schedule, screenshot, handwritten list). "
        "I read the times with OCR, show them to you, and create reminders after you confirm.\n\n"
        "⌨️ <b>Add reminders by typing</b>\n"
        "<code>09:30 gym</code> or a whole line like <code>09:00 breakfast, 14:30 lunch, 20:00 run</code>\n\n"
        "<b>⏰ When a reminder fires</b>\n"
        "• ✅ <b>Done</b> - completed, no charge\n"
        "• ❌ <b>Skipped</b> - your charge amount is added to your balance\n"
        "• 🟩 <b>Compensated</b> - half your charge amount is deducted from the balance\n\n"
        "<b>Commands</b>\n"
        "/list - upcoming reminders\n"
        "/cancel &lt;id&gt; - cancel a reminder\n"
        "/charge - view or change your charge amount\n"
        "/balance - your outstanding balance\n"
        "/summary - today's summary right now\n"
        "/start - account status",
        parse_mode=ParseMode.HTML,
        reply_markup=main_menu(),
    )


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    rows = storage.user_reminders(update.effective_user.id, ("pending", "fired"))
    if not rows:
        await update.message.reply_text(
            "You have no upcoming reminders. Send a photo or type some times!",
            reply_markup=main_menu(),
        )
        return
    lines = ["<b>⏰ Upcoming reminders</b>", ""]
    for r in rows:
        label = r["label"] or "Reminder"
        lines.append(f"{EMOJI[r['status']]} <code>#{r['id']}</code> {fmt_local(local_from_iso(r['fire_at']))} — {label}")
    lines.append("")
    lines.append("Cancel with /cancel &lt;id&gt;")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, reply_markup=main_menu())


async def cmd_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("Usage: /cancel 12  (the reminder id from /list)")
        return
    rid = int(context.args[0].lstrip("#"))
    r = storage.get_reminder(rid)
    if r is None or r["user_id"] != update.effective_user.id:
        await update.message.reply_text("No reminder with that id belongs to you.")
        return
    if r["status"] not in ("pending", "fired"):
        await update.message.reply_text("That reminder is already resolved.")
        return
    storage.set_reminder_status(rid, "cancelled")
    for job in context.job_queue.get_jobs_by_name(f"rem-{rid}"):
        job.schedule_removal()
    await update.message.reply_text(f"🗑 Reminder #{rid} cancelled.")


async def cmd_charge(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = storage.get_user(update.effective_user.id)
    if user is None:
        ask_charge_amount(update, context)
        return
    await update.message.reply_text(
        f"💰 Current charge per skip: <b>{money(user['charge_amount'])}</b>\n"
        f"💳 Outstanding balance: <b>{money(user['balance'])}</b>\n\n"
        "Pick a new amount or press the button to type one:",
        parse_mode=ParseMode.HTML,
        reply_markup=charge_keyboard(""),
    )


async def cmd_balance(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    user = storage.get_user(update.effective_user.id)
    if user is None:
        await update.message.reply_text("Press /start first to set up your account.")
        return
    await update.message.reply_text(
        f"💳 Your outstanding balance: <b>{money(user['balance'])}</b>\n\n"
        "❌ Skips add your charge amount.\n"
        "🟩 Compensation deducts half of it.",
        parse_mode=ParseMode.HTML,
    )


# ------------------------------------------------------------- photo & text

async def handle_times(update: Update, context: ContextTypes.DEFAULT_TYPE, items: list[dict]) -> None:
    """Show the extracted list and ask for confirmation."""
    if not items:
        await update.message.reply_text(
            "❌ I couldn't find any times (like <code>09:30</code>) in that.\n\n"
            "Tips:\n"
            "• Send a clearer / brighter picture\n"
            "• Or just type them: <code>09:30 gym 14:00 lunch</code>",
            parse_mode=ParseMode.HTML,
        )
        return

    context.user_data["pending_times"] = items

    lines = [f"🔍 <b>Found {len(items)} time(s)</b>:", ""]
    for it in items:
        dd = datetime.now(TZ).date()
        h, m = (int(x) for x in it["time"].split(":"))
        passed = datetime.combine(dd, dtime(h, m), tzinfo=TZ) <= now_tz()
        suffix = "  <i>(already passed today — will be set for tomorrow)</i>" if passed else ""
        label = it["label"] or "—"
        lines.append(f"• ⏰ <b>{it['time']}</b> — {label}{suffix}")
    lines.append("")
    lines.append("Add all of these?")
    await update.message.reply_text(
        "\n".join(lines),
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Add reminders", callback_data="confirm:add"),
                    InlineKeyboardButton("✖️ Cancel", callback_data="confirm:cancel"),
                ]
            ]
        ),
    )


async def on_photo(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if storage.get_user(update.effective_user.id) is None:
        await update.message.reply_text("Press /start first so I can set up your charge amount.")
        return
    temp_path = None
    try:
        await update.message.reply_text("👀 Reading the picture…")
        photo = update.message.photo[-1]
        file = await photo.get_file()
        temp_path = f"ocr_{update.update_id}.jpg"
        await file.download_to_drive(temp_path)
        items = extract.parse_times_from_image(temp_path)
    except Exception:  # noqa: BLE001 - OCR can fail in many env-specific ways
        log.exception("OCR failed")
        await update.message.reply_text(
            "⚠️ I couldn't read that picture. Try a clearer one, or type the times "
            "like <code>09:30 gym</code>.",
            parse_mode=ParseMode.HTML,
        )
        return
    finally:
        if temp_path and os.path.exists(temp_path):
            os.remove(temp_path)
    await handle_times(update, context, items)


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    text = update.message.text.strip()

    # --- charge amount input (first-time setup or /charge -> custom)
    if context.user_data.get("awaiting_charge"):
        context.user_data.pop("awaiting_charge", None)
        digits = "".join(ch for ch in text if ch.isdigit())
        amount = int(digits) if digits else 0
        if not 0 < amount <= MAX_CHARGE:
            context.user_data["awaiting_charge"] = "set"
            await update.message.reply_text(
                f"That doesn't look like a valid amount (must be a number up to {MAX_CHARGE:,}). Try again:"
            )
            return
        await _save_charge(update, context, amount)
        return

    # --- normal text: treat as typed times
    if storage.get_user(update.effective_user.id) is None:
        ask_charge_amount(update, context)
        return
    items = extract.parse_times_from_text(text)
    await handle_times(update, context, items)


# ------------------------------------------------------------ charge flows

async def _save_charge(update: Update, context: ContextTypes.DEFAULT_TYPE, amount: int) -> None:
    tg_user = update.effective_user
    storage.upsert_user(tg_user.id, update.effective_chat.id, tg_user.first_name or "friend", amount)
    await update.message.reply_text(
        f"✅ Got it - charge per skip: <b>{money(amount)}</b>\n\n"
        "📸 Now send a photo of today's schedule (or type times like <code>09:30 gym</code>) "
        "and I'll set the reminders.",
        parse_mode=ParseMode.HTML,
    )


async def on_charge_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action = query.data.split(":", 2)[1]  # "set" | "custom"
    await query.answer()

    if action == "set":
        amount = int(query.data.rsplit(":", 1)[1])
        context.user_data.pop("awaiting_charge", None)
        await query.edit_message_text(
            f"💰 Charge per skip saved: <b>{money(amount)}</b>",
            parse_mode=ParseMode.HTML,
        )
        first_time = storage.get_user(update.effective_user.id) is None
        if first_time:
            await _save_charge(update, context, amount)
        else:
            tg_user = update.effective_user
            storage.upsert_user(tg_user.id, update.effective_chat.id, tg_user.first_name or "friend", amount)
            await context.bot.send_message(
                update.effective_chat.id,
                "Updated! Send me a picture or times anytime.",
            )
        return

    if action == "custom":
        context.user_data["awaiting_charge"] = "set"
        await query.answer("Type your amount now (e.g. 25000 or 25 000).")
        await context.bot.send_message(
            update.effective_chat.id,
            "✍️ Type the charge amount (e.g. <code>25000</code>):",
            parse_mode=ParseMode.HTML,
        )


# ------------------------------------------------------------ confirm flow

async def on_confirm_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    action = query.data.split(":", 1)[1]
    await query.answer()

    if action == "cancel":
        context.user_data.pop("pending_times", None)
        await query.edit_message_text("✖️ Cancelled - nothing was created.")
        return

    items = context.user_data.get("pending_times")
    if not items:
        await query.edit_message_text("That list is gone - send the picture or times again.")
        return

    user = storage.get_user(update.effective_user.id)
    if user is None:
        context.user_data.pop("pending_times", None)
        await query.edit_message_text("You need to set your charge amount first.")
        ask_charge_amount(update, context)
        return

    chat_id = query.message.chat_id
    now = now_tz()
    created, moved_total = 0, 0
    for it in items:
        h, m = (int(x) for x in it["time"].split(":"))
        fire_local = datetime.combine(now.date(), dtime(h, m), tzinfo=TZ)
        if fire_local <= now:
            fire_local += timedelta(days=1)
            moved_total += 1
        rid = storage.create_reminder(user["user_id"], chat_id, utc_iso(fire_local), it["label"])
        schedule_reminder_job(context.job_queue, rid, chat_id, fire_local)
        created += 1

    context.user_data.pop("pending_times", None)
    text = f"✅ Created <b>{created}</b> reminder(s)."
    if moved_total:
        text += f"\n↪️ {moved_total} time(s) had already passed today, so I set them for tomorrow."
    text += "\n\nCheck with /list"
    await query.edit_message_text(text, parse_mode=ParseMode.HTML)


# ------------------------------------------------------- reminder firing

def schedule_reminder_job(job_queue, reminder_id: int, chat_id: int, fire_local: datetime) -> None:
    job_queue.run_once(
        fire_reminder,
        when=fire_local,
        name=f"rem-{reminder_id}",
        data={"reminder_id": reminder_id, "chat_id": chat_id},
    )


async def fire_reminder(context: ContextTypes.DEFAULT_TYPE) -> None:
    rid = context.job.data["reminder_id"]
    chat_id = context.job.data["chat_id"]
    reminder = storage.get_reminder(rid)
    if reminder is None or reminder["status"] != "pending":
        return  # cancelled or already resolved
    storage.set_reminder_status(rid, "fired")

    fire_local = local_from_iso(reminder["fire_at"])
    label = reminder["label"] or "Reminder"
    text = (
        f"⏰ <b>Reminder — {fire_local.strftime('%H:%M')}</b>\n\n"
        f"📌 {label}\n\n"
        "What happened?"
    )
    await context.bot.send_message(
        chat_id,
        text,
        parse_mode=ParseMode.HTML,
        reply_markup=InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton("✅ Done", callback_data=f"outcome:{rid}:done"),
                    InlineKeyboardButton("❌ Skipped", callback_data=f"outcome:{rid}:skip"),
                ],
                [InlineKeyboardButton("🟩 Compensated", callback_data=f"outcome:{rid}:compensate")],
            ]
        ),
    )


async def on_outcome_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, rid_s, kind = query.data.split(":")
    rid = int(rid_s)
    reminder = storage.get_reminder(rid)

    if reminder is None:
        await query.answer("Reminder not found.", show_alert=True)
        return
    if reminder["user_id"] != update.effective_user.id:
        await query.answer("This reminder isn't yours.", show_alert=True)
        return
    if reminder["status"] != "fired":
        await query.answer("Already recorded.")
        return

    user = storage.get_user(reminder["user_id"])
    charge = user["charge_amount"] if user else 0
    fire_local = local_from_iso(reminder["fire_at"])
    label = reminder["label"] or "Reminder"
    header = f"{fire_local.strftime('%H:%M')} · {label}"

    if kind == "done":
        storage.set_reminder_status(rid, "done")
        storage.add_event(reminder["user_id"], rid, "done", 0)
        new_balance = user["balance"] if user else 0
        body = "Recently completed. No charge."
        emoji_line = "✅ Done"
    elif kind == "skip":
        storage.set_reminder_status(rid, "skipped")
        new_balance = storage.add_balance(reminder["user_id"], charge)
        storage.add_event(reminder["user_id"], rid, "skip", charge)
        body = f"💸 +{money(charge)} charged."
        emoji_line = "❌ Skipped"
    elif kind == "compensate":
        deducted = min(charge // 2, user["balance"] if user else 0)
        storage.set_reminder_status(rid, "compensated")
        new_balance = storage.add_balance(reminder["user_id"], -deducted) if user else 0
        storage.add_event(reminder["user_id"], rid, "compensate", -deducted)
        body = f"💳 −{money(deducted)} deducted from your balance."
        emoji_line = "🟩 Compensated"
    else:
        await query.answer("Unknown action.")
        return

    text = (
        f"<b>{emoji_line}</b> — {header}\n"
        f"{body}\n"
        f"💳 Outstanding balance: <b>{money(new_balance)}</b>"
    )
    try:
        await query.edit_message_text(text, parse_mode=ParseMode.HTML)
    except BadRequest:
        pass
    await query.answer("Saved!")


# ----------------------------------------------------------- daily summary

def _build_summary_text(users: list[dict], events: list[dict], day_date) -> str:
    stats = {u["user_id"]: {"done": 0, "skip": 0, "skip_amt": 0, "comp": 0, "comp_amt": 0} for u in users}
    for ev in events:
        s = stats.get(ev["user_id"])
        if s is None:
            continue
        if ev["kind"] == "done":
            s["done"] += 1
        elif ev["kind"] == "skip":
            s["skip"] += 1
            s["skip_amt"] += max(ev["delta"], 0)
        elif ev["kind"] == "compensate":
            s["comp"] += 1
            s["comp_amt"] += abs(min(ev["delta"], 0))

    names = {u["user_id"]: (u["name"] or "Someone") for u in users}
    balances = {u["user_id"]: u["balance"] for u in users}

    lines = [f"📊 <b>Daily Summary — {day_date.strftime('%d %b %Y')}</b>", ""]
    participated = [uid for uid, s in stats.items()
                    if s["done"] or s["skip"] or s["comp"]]
    if participated:
        for uid in participated:
            s = stats[uid]
            lines.append(f"👤 <b>{names[uid]}</b>")
            lines.append(f"   ✅ Done: {s['done']}")
            if s["skip"]:
                lines.append(f"   ❌ Skipped: {s['skip']}  →  +{money(s['skip_amt'])}")
            else:
                lines.append(f"   ❌ Skipped: {s['skip']}")
            if s["comp"]:
                lines.append(f"   🟩 Compensated: {s['comp']}  →  −{money(s['comp_amt'])}")
            else:
                lines.append(f"   🟩 Compensated: {s['comp']}")
            lines.append("")
    else:
        lines.append("No one logged anything today. 🤨")
        lines.append("")

    lines.append("💳 <b>Balances owed</b>")
    any_balance = False
    for uid in stats:
        if balances[uid]:
            any_balance = True
            lines.append(f"• {names[uid]}: <b>{money(balances[uid])}</b>")
    if not any_balance:
        lines.append("• Everyone's square ✨")
    return "\n".join(lines)


async def send_daily_summary(context: ContextTypes.DEFAULT_TYPE) -> None:
    users = storage.all_users()
    if not users:
        return
    start_local = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
    storage.set_kv("last_summary_date", start_local.date().isoformat())
    events = storage.events_since(utc_iso(start_local))
    text = _build_summary_text(users, events, start_local.date())
    for u in users:
        try:
            await context.bot.send_message(u["chat_id"], text, parse_mode=ParseMode.HTML)
        except Exception:  # noqa: BLE001 - one failed chat shouldn't stop the rest
            log.warning("Couldn't send daily summary to %s", u["user_id"])
    log.info("Daily summary sent to %d user(s)", len(users))


async def cmd_summary(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    users = storage.all_users()
    start_local = now_tz().replace(hour=0, minute=0, second=0, microsecond=0)
    events = storage.events_since(utc_iso(start_local))
    await update.message.reply_text(
        _build_summary_text(users, events, start_local.date()), parse_mode=ParseMode.HTML
    )


# ------------------------------------------------------------------ startup

def plan_daily_summary(job_queue) -> None:
    """Schedule the daily summary so it survives restarts and CI wake-up windows.

    The last_summary_date marker in the DB makes a summary that was missed
    (bot offline at 23:00) get sent on the very next wake-up.
    """
    now = now_tz()
    today = now.date()
    summary_today = datetime.combine(today, dtime(SUMMARY_HH, SUMMARY_MM), tzinfo=TZ)
    sent = storage.get_kv("last_summary_date")
    if sent == today.isoformat():
        first_run = summary_today + timedelta(days=1)
    elif summary_today <= now:
        first_run = now + timedelta(seconds=5)  # missed earlier today -> send now
    else:
        first_run = summary_today
    job_queue.run_repeating(send_daily_summary, interval=timedelta(days=1), first=first_run)
    log.info("Daily summary: next at %s", first_run.strftime("%d %b %H:%M %Z"))


async def on_startup(app: Application) -> None:
    """Re-schedule pending reminders after a restart, and the daily summary job."""
    now = now_tz()
    for r in storage.all_pending():
        fire_local = local_from_iso(r["fire_at"])
        if fire_local <= now:
            fire_local = now + timedelta(seconds=5)  # overdue -> fire right away
        schedule_reminder_job(app.job_queue, r["id"], r["chat_id"], fire_local)
    log.info("Re-scheduled %d pending reminder(s)", len(storage.all_pending()))

    plan_daily_summary(app.job_queue)


async def run_windowed(app: Application, seconds: int) -> None:
    """CI mode: live for a defined window, then exit cleanly.

    On boot it first drains every update Telegram queued while the bot was
    offline (button presses, photos, times…), fires any overdue reminders,
    and stays available for the rest of the window.
    """
    await app.initialize()
    await on_startup(app)
    await app.updater.start_polling(allowed_updates=["message", "callback_query"])
    await app.start()
    log.info("Awake for %ss — processing updates…", seconds)
    await asyncio.sleep(seconds)
    log.info("Awake window over — shutting down cleanly.")
    await app.updater.stop()
    await app.stop()
    await app.shutdown()


def main() -> None:
    storage.init()
    builder = ApplicationBuilder().token(TOKEN)
    if AWAKE_SECONDS <= 0:
        builder = builder.post_init(on_startup)
    app = builder.build()

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("list", cmd_list))
    app.add_handler(CommandHandler("cancel", cmd_cancel))
    app.add_handler(CommandHandler("charge", cmd_charge))
    app.add_handler(CommandHandler("balance", cmd_balance))
    app.add_handler(CommandHandler("summary", cmd_summary))

    app.add_handler(CallbackQueryHandler(on_charge_callback, pattern=r"^charge:"))
    app.add_handler(CallbackQueryHandler(on_confirm_callback, pattern=r"^confirm:"))
    app.add_handler(CallbackQueryHandler(on_outcome_callback, pattern=r"^outcome:"))

    app.add_handler(MessageHandler(filters.PHOTO & filters.ChatType.PRIVATE, on_photo))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND & filters.ChatType.PRIVATE, on_text))

    if AWAKE_SECONDS > 0:
        asyncio.run(run_windowed(app, AWAKE_SECONDS))
    else:
        log.info("Bot starting (forever mode)…")
        app.run_polling(allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    main()
