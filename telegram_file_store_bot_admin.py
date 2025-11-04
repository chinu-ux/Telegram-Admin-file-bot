#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Mobile-friendly Telegram File Store Bot (SQLite)
Features:
 - Force-subscribe (multi-channel) with "I Joined" check
 - /genlink, /batch, /custom_batch (basic)
 - admin system: add/del admins, list admins
 - broadcast and dbroadcast (auto-delete)
 - ban/unban/banlist
 - dlt_time / check_dlt_time
 - users, stats
 - addchnl / delchnl / listchnl / fsub_mode (toggle)
 - pbroadcast (pins in DB channel if possible)
Notes:
 - Replace BOT_TOKEN, DB_CHANNEL_ID, ADMINS.
 - Tested with python-telegram-bot v20.x async API.
"""

import asyncio, logging, sqlite3, os, time
from datetime import datetime
from typing import List, Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Message,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from telegram.error import Forbidden, TelegramError

# ---------------- CONFIG ----------------
BOT_TOKEN = "8222645012:AAEQMNK31oa5hDo_9OEStfNL7FMBdZMkUFM"
DB_CHANNEL_ID = -1003292247930   # channel where bot will copy/store files (use negative for channels)
ADMINS = [7681308594]             # your Telegram user id (int). you can add more.
DEFAULT_DELETE_AFTER = 3600      # seconds for dbroadcast auto-delete
# ----------------------------------------

logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ---------------- DATABASE ----------------
DB_FILE = "mobile_bot.db"
conn = sqlite3.connect(DB_FILE, check_same_thread=False)
cur = conn.cursor()

cur.execute("""CREATE TABLE IF NOT EXISTS users (
    user_id INTEGER PRIMARY KEY,
    joined_at TEXT
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS banned (
    user_id INTEGER PRIMARY KEY
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS admins (
    user_id INTEGER PRIMARY KEY
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS dlt_time (
    time INTEGER
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS fsub_channels (
    channel TEXT PRIMARY KEY
)""")
cur.execute("""CREATE TABLE IF NOT EXISTS fsub_mode (
    enabled INTEGER PRIMARY KEY
)""")
conn.commit()

# Ensure initial admin rows (mirror ADMINS config)
for adm in ADMINS:
    cur.execute("INSERT OR IGNORE INTO admins VALUES (?)", (adm,))
conn.commit()

# Default fsub mode ON
cur.execute("INSERT OR IGNORE INTO fsub_mode VALUES (1)")
conn.commit()

# ---------------- HELPERS ----------------
async def is_banned(user_id: int) -> bool:
    cur.execute("SELECT 1 FROM banned WHERE user_id=?", (user_id,))
    return cur.fetchone() is not None

def is_admin_local(user_id: int) -> bool:
    cur.execute("SELECT 1 FROM admins WHERE user_id=?", (user_id,))
    return cur.fetchone() is not None

async def get_fsub_channels() -> List[str]:
    cur.execute("SELECT channel FROM fsub_channels")
    return [r[0] for r in cur.fetchall()]

def fsub_enabled() -> bool:
    cur.execute("SELECT enabled FROM fsub_mode")
    row = cur.fetchone()
    return bool(row[0]) if row else True

async def user_register(user_id: int):
    cur.execute("INSERT OR IGNORE INTO users VALUES (?, ?)", (user_id, datetime.utcnow().isoformat()))
    conn.commit()

async def is_user_member(bot, channel: str, user_id: int) -> bool:
    # channel can be @username or channel_id
    try:
        member = await bot.get_chat_member(channel, user_id)
        return member.status in ("member", "creator", "administrator")
    except TelegramError:
        return False

def get_delete_time() -> int:
    cur.execute("SELECT time FROM dlt_time")
    r = cur.fetchone()
    return int(r[0]) if r else DEFAULT_DELETE_AFTER

# -------------- COMMANDS ----------------

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if await is_banned(user.id):
        return await update.message.reply_text("🚫 You are banned from using this bot.")

    # Force-sub check if enabled
    if fsub_enabled():
        chans = await get_fsub_channels()
        if chans:
            joined_all = True
            for ch in chans:
                if not await is_user_member(context.bot, ch, user.id):
                    joined_all = False
                    break
            if not joined_all:
                # build keyboard: join button(s) and check button
                kb = []
                for ch in chans:
                    # show first button per channel
                    kb.append([InlineKeyboardButton(f"Join {ch}", url=f"https://t.me/{ch.replace('@','')}")])
                kb.append([InlineKeyboardButton("✅ I Joined", callback_data="forcecheck")])
                await update.message.reply_text(
                    "⚠️ To use this bot, please join the required channel(s) first.",
                    reply_markup=InlineKeyboardMarkup(kb),
                )
                return

    # register user and welcome
    await user_register(user.id)
    await update.message.reply_text(f"👋 Hello {user.first_name}! Use /help to see commands.")

async def forcecheck_cb(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = query.from_user
    await query.answer()
    if not fsub_enabled():
        return await query.edit_message_text("✅ Force-subscription is currently disabled by admin.")
    chans = await get_fsub_channels()
    if not chans:
        return await query.edit_message_text("✅ No force channels are configured. You can use the bot now.")
    # check membership
    for ch in chans:
        if not await is_user_member(context.bot, ch, user.id):
            # still not joined
            kb = [[InlineKeyboardButton(f"Join {ch}", url=f"https://t.me/{ch.replace('@','')}")],
                  [InlineKeyboardButton("✅ I Joined", callback_data="forcecheck")]]
            return await query.edit_message_text("⚠️ You still haven't joined. Please join then press I Joined.", reply_markup=InlineKeyboardMarkup(kb))
    # all good
    await user_register(user.id)
    await query.edit_message_text(f"✅ Thanks {user.first_name}! You have joined required channels. You can now use the bot.")

async def genlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # reply to a media message -> copy to DB channel -> return start link
    user = update.effective_user
    if await is_banned(user.id):
        return await update.message.reply_text("🚫 You are banned.")
    if fsub_enabled():
        chans = await get_fsub_channels()
        for ch in chans:
            if not await is_user_member(context.bot, ch, user.id):
                return await update.message.reply_text("⚠️ Please join the required channel(s) first.")
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to a media message (photo/video/document/voice) to create a link.")
    try:
        copied = await update.message.reply_to_message.copy(chat_id=DB_CHANNEL_ID)
    except Exception as e:
        log.exception("Failed to copy message to DB channel")
        return await update.message.reply_text("❌ Failed to store file. Check DB_CHANNEL_ID and bot permissions.")
    # link uses message id in DB_CHANNEL and bot username start param
    mid = copied.message_id
    link = f"https://t.me/{(await context.bot.get_me()).username}?start=share_{mid}"
    await update.message.reply_text(f"🔗 Link created:\n{link}")

async def batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Simple batch: user replies to multiple messages? Telegram doesn't support multi-reply.
    # Implementation: expects a text message with pairs "channel_or_chat_id:msgid,msgid,..." or message with space separated message ids when replying to a channel message that contains multiple media.
    text = " ".join(context.args) if context.args else ""
    if not text and not update.message.reply_to_message:
        return await update.message.reply_text("Usage examples:\n1) Reply to a message that has multiple media and use /batch (bot will copy all media if present).\n2) /batch <channel_id_or_username>:<msgid1>,<msgid2>,...")
    results = []
    # Case A: replied to a message (if it contains album/media group, copy)
    if update.message.reply_to_message:
        # if reply contains media (photo, document, video, audio), copy that single message
        try:
            copied = await update.message.reply_to_message.copy(chat_id=DB_CHANNEL_ID)
            mid = copied.message_id
            link = f"https://t.me/{(await context.bot.get_me()).username}?start=share_{mid}"
            return await update.message.reply_text(f"🔗 Copied replied message and created link:\n{link}")
        except Exception as e:
            log.exception("batch (reply) failed")
            return await update.message.reply_text("❌ Failed to copy replied message.")
    # Case B: parse arg style channel:mid,mid
    if ":" in text:
        try:
            target, ids = text.split(":", 1)
            mids = [s.strip() for s in ids.split(",") if s.strip()]
            links = []
            for m in mids:
                # copy specific message id from target to DB_CHANNEL_ID
                try:
                    copied = await context.bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=target, message_id=int(m))
                    links.append(f"https://t.me/{(await context.bot.get_me()).username}?start=share_{copied.message_id}")
                    await asyncio.sleep(0.05)
                except Exception:
                    log.exception("copy single in batch failed")
            if links:
                await update.message.reply_text("🔗 Batch links:\n" + "\n".join(links))
            else:
                await update.message.reply_text("❌ No links created.")
        except Exception:
            return await update.message.reply_text("❌ Invalid format. Use /batch <channel_or_username>:<msgid1>,<msgid2>")
        return
    await update.message.reply_text("❗ Couldn't detect media to batch. Use the examples in the help.")

async def custom_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # Admin only: /custom_batch <channel_or_username> <msgid1,msgid2,...>
    user = update.effective_user
    if not is_admin_local(user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if len(context.args) < 2:
        return await update.message.reply_text("Usage: /custom_batch <channel> <msgid1,msgid2,...>")
    target = context.args[0]
    mids = context.args[1].split(",")
    links = []
    for m in mids:
        try:
            copied = await context.bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=target, message_id=int(m))
            links.append(f"https://t.me/{(await context.bot.get_me()).username}?start=share_{copied.message_id}")
            await asyncio.sleep(0.05)
        except Exception:
            log.exception("custom_batch copy failed")
    if links:
        await update.message.reply_text("🔗 Custom batch links:\n" + "\n".join(links))
    else:
        await update.message.reply_text("❌ No links created.")

async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    cur.execute("SELECT COUNT(*) FROM users")
    c = cur.fetchone()[0]
    await update.message.reply_text(f"👥 Total users: {c}")

async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to a message to broadcast.")
    cur.execute("SELECT user_id FROM users")
    rows = cur.fetchall()
    sent = 0
    for (uid,) in rows:
        try:
            await update.message.reply_to_message.copy(chat_id=uid)
            sent += 1
            await asyncio.sleep(0.06)
        except Forbidden:
            # user blocked bot or cannot PM
            pass
        except Exception:
            pass
    await update.message.reply_text(f"✅ Broadcast attempted to {sent} users (sent or accepted).")

async def dbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if not update.message.reply_to_message:
        return await update.message.reply_text("Reply to a message to dbroadcast.")
    delete_after = get_delete_time()
    cur.execute("SELECT user_id FROM users")
    rows = cur.fetchall()
    sent = 0
    for (uid,) in rows:
        try:
            m = await update.message.reply_to_message.copy(chat_id=uid)
            sent += 1
            # schedule deletion asynchronously
            async def delete_later(msg, wait):
                await asyncio.sleep(wait)
                try:
                    await msg.delete()
                except Exception:
                    pass
            # run delete in background
            asyncio.create_task(delete_later(m, delete_after))
            await asyncio.sleep(0.06)
        except Exception:
            pass
    await update.message.reply_text(f"✅ dbroadcast sent to {sent} users; messages will be deleted after {delete_after} sec (if possible).")

async def dlt_time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if not context.args:
        return await update.message.reply_text("Usage: /dlt_time <seconds>")
    try:
        t = int(context.args[0])
        cur.execute("DELETE FROM dlt_time")
        cur.execute("INSERT INTO dlt_time VALUES (?)", (t,))
        conn.commit()
        await update.message.reply_text(f"✅ Set auto-delete time to {t} seconds.")
    except ValueError:
        await update.message.reply_text("Please provide an integer number of seconds.")

async def check_dlt_time_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    t = get_delete_time()
    await update.message.reply_text(f"🕒 Current auto-delete time: {t} sec")

async def ban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if not context.args:
        return await update.message.reply_text("Usage: /ban <user_id>")
    try:
        uid = int(context.args[0])
        cur.execute("INSERT OR IGNORE INTO banned VALUES (?)", (uid,))
        conn.commit()
        await update.message.reply_text(f"🚫 Banned {uid}")
    except ValueError:
        await update.message.reply_text("Invalid user id.")

async def unban_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if not context.args:
        return await update.message.reply_text("Usage: /unban <user_id>")
    try:
        uid = int(context.args[0])
        cur.execute("DELETE FROM banned WHERE user_id=?", (uid,))
        conn.commit()
        await update.message.reply_text(f"✅ Unbanned {uid}")
    except ValueError:
        await update.message.reply_text("Invalid user id.")

async def banlist_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    cur.execute("SELECT user_id FROM banned")
    rows = cur.fetchall()
    if not rows:
        return await update.message.reply_text("✅ No banned users.")
    await update.message.reply_text("🚫 Banned users:\n" + "\n".join(str(r[0]) for r in rows))

# fsub channel management
async def addchnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if not context.args:
        return await update.message.reply_text("Usage: /addchnl @channel_username_or_id")
    ch = context.args[0].strip()
    cur.execute("INSERT OR IGNORE INTO fsub_channels VALUES (?)", (ch,))
    conn.commit()
    await update.message.reply_text(f"✅ Added force channel {ch}")

async def delchnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if not context.args:
        return await update.message.reply_text("Usage: /delchnl @channel_username_or_id")
    ch = context.args[0].strip()
    cur.execute("DELETE FROM fsub_channels WHERE channel=?", (ch,))
    conn.commit()
    await update.message.reply_text(f"✅ Removed force channel {ch}")

async def listchnl_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    cur.execute("SELECT channel FROM fsub_channels")
    rows = cur.fetchall()
    if not rows:
        return await update.message.reply_text("No force channels configured.")
    await update.message.reply_text("Force channels:\n" + "\n".join(r[0] for r in rows))

async def fsub_mode_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    cur.execute("SELECT enabled FROM fsub_mode")
    r = cur.fetchone()
    cur.execute("DELETE FROM fsub_mode")
    if r and r[0]:
        cur.execute("INSERT INTO fsub_mode VALUES (0)")
        conn.commit()
        return await update.message.reply_text("✅ Force-sub mode disabled.")
    else:
        cur.execute("INSERT INTO fsub_mode VALUES (1)")
        conn.commit()
        return await update.message.reply_text("✅ Force-sub mode enabled.")

# admin management
async def add_admin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if not context.args:
        return await update.message.reply_text("Usage: /add_admin <user_id>")
    try:
        uid = int(context.args[0])
        cur.execute("INSERT OR IGNORE INTO admins VALUES (?)", (uid,))
        conn.commit()
        await update.message.reply_text(f"✅ Added admin {uid}")
    except ValueError:
        await update.message.reply_text("Invalid user id.")

async def deladmin_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    if not context.args:
        return await update.message.reply_text("Usage: /deladmin <user_id>")
    try:
        uid = int(context.args[0])
        cur.execute("DELETE FROM admins WHERE user_id=?", (uid,))
        conn.commit()
        await update.message.reply_text(f"✅ Removed admin {uid}")
    except ValueError:
        await update.message.reply_text("Invalid user id.")

async def admins_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin_local(update.effective_user.id):
        return await update.message.reply_text("🚫 Admin only.")
    cur.execute("SELECT user_id FROM admins")
    rows = cur.fetchall()
    await update.message.reply_text("Admins:\n" + "\n".join(str(r[0]) for r in rows))

async def stats_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # show simple uptime + DB sizes
    uptime = time.time() - os.path.getctime(__file__) if os.path.exists(__file__) else 0
    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM banned")
    banned_count = cur.fetchone()[0]
    await update.message.reply_text(f"🤖 Bot running\nUptime: {int(uptime)} sec\nUsers: {users_count}\nBanned: {banned_count}")
        
