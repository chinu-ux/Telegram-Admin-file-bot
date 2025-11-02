#!/usr/bin/env python3
# filestore_bot.py
# Requires: python-telegram-bot v20+
# pip install python-telegram-bot --upgrade

import logging
import sqlite3
import time
import asyncio
from datetime import datetime, timedelta
from typing import Optional, List
from telegram import Update, BotCommand
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# ----------------- CONFIG -----------------
TOKEN = "8215586109:AAFOVHoapHMw6kAT9kzoLHuB2gTkqoJ-AZc"
OWNER_ID = 7681308594
DB_CHANNEL_ID = -1003292247930
DEFAULT_DELETE_SECONDS = 0
ADMINS = set([7681308594])
# -------------------------------------------

application = ApplicationBuilder().token(TOKEN).build()

# -------------------------------------------------------
# Logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Simple SQLite persistence
DB_PATH = "filestore.db"


def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        saved_chat_id INTEGER,
        saved_message_id INTEGER,
        owner_id INTEGER,
        caption TEXT,
        created_at INTEGER,
        delete_at INTEGER,
        deleted INTEGER DEFAULT 0
    )
    """
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS users (
        chat_id INTEGER PRIMARY KEY,
        first_seen INTEGER
    )
    """
    )
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS meta (
        key TEXT PRIMARY KEY,
        value TEXT
    )
    """
    )
    # store default delete time if not set
    cur.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", ("dlt_time", str(DEFAULT_DELETE_SECONDS)))
    cur.execute("INSERT OR IGNORE INTO meta (key, value) VALUES (?, ?)", ("fsub_mode", "off"))
    conn.commit()
    conn.close()


def db_get(key: str, default: Optional[str] = None) -> Optional[str]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT value FROM meta WHERE key=?", (key,))
    r = cur.fetchone()
    conn.close()
    return r[0] if r else default


def db_set(key: str, value: str):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, value))
    conn.commit()
    conn.close()


def is_admin(user_id: int) -> bool:
    return user_id in ADMINS or user_id == OWNER_ID


# Helper: record user
def record_user(chat_id: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT OR IGNORE INTO users (chat_id, first_seen) VALUES (?, ?)", (chat_id, int(time.time())))
    conn.commit()
    conn.close()


# Helper: schedule deletion job
async def schedule_deletion(context: ContextTypes.DEFAULT_TYPE, file_row_id: int, when_seconds: int):
    if when_seconds <= 0:
        return
    # Use job_queue to run delete later
    context.job_queue.run_once(_delete_file_job, when_seconds, data={"file_id": file_row_id})


async def _delete_file_job(context: ContextTypes.DEFAULT_TYPE):
    data = context.job.data or {}
    file_id = data.get("file_id")
    if not file_id:
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT saved_chat_id, saved_message_id, deleted FROM files WHERE id=?", (file_id,))
    row = cur.fetchone()
    if not row:
        conn.close()
        return
    saved_chat_id, saved_message_id, deleted = row
    if deleted:
        conn.close()
        return
    try:
        # attempt deletion from DB channel
        await context.bot.delete_message(saved_chat_id, saved_message_id)
    except Exception as e:
        logger.warning("Could not delete stored message: %s", e)
    cur.execute("UPDATE files SET deleted=1 WHERE id=?", (file_id,))
    conn.commit()
    conn.close()
    logger.info("Auto-deleted stored file id=%s", file_id)


# ----------------- COMMAND HANDLERS -----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /start or /start payload (deep-linking).
    If payload like share_<id>, fetch that file from DB channel and send to user.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id
    record_user(chat_id)

    # If there is payload: telegram sends it in update.message.text after '/start '
    payload = None
    if update.message and update.message.text:
        parts = update.message.text.split(maxsplit=1)
        if len(parts) > 1:
            payload = parts[1].strip()

    if payload and payload.startswith("share_"):
        try:
            file_id = int(payload.split("_", 1)[1])
        except Exception:
            await update.message.reply_text("Invalid link payload.")
            return

        conn = sqlite3.connect(DB_PATH)
        cur = conn.cursor()
        cur.execute("SELECT saved_chat_id, saved_message_id, caption, deleted FROM files WHERE id=?", (file_id,))
        row = cur.fetchone()
        conn.close()
        if not row:
            await update.message.reply_text("File not found.")
            return
        saved_chat_id, saved_message_id, caption, deleted = row
        if deleted:
            await update.message.reply_text("Sorry, that file was deleted.")
            return
        # copy stored message to user
        try:
            await context.bot.copy_message(chat_id=chat_id, from_chat_id=saved_chat_id, message_id=saved_message_id, caption=caption or None)
        except Exception as e:
            logger.exception("Error copying stored file: %s", e)
            await update.message.reply_text("Unable to send the file. Contact admin.")
        return

    # default welcome
    text = (
        "FileStore Bot — Commands:\n"
        "/genlink - reply to a message (file) to save & get single link\n"
        "/batch - reply to multiple message ids (or use /custom_batch) to create a batch link\n"
        "/users - view user count (admin)\n"
        "/broadcast - broadcast message (admin)\n"
        "/dbroadcast - broadcast with auto-delete (admin)\n"
        "/stats - bot uptime\n"
        "For more admin commands use /help_admin"
    )
    await update.message.reply_text(text)


async def genlink(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Reply to a single message (file/media). Bot will copy it into DB_CHANNEL and return a share link.
    Usage: reply to a media or file with /genlink
    """
    user = update.effective_user
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to a message containing the file (photo, video, document) with /genlink")
        return
    reply = update.message.reply_to_message

    # copy message to DB channel
    try:
        sent = await context.bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=reply.chat_id, message_id=reply.message_id, caption=reply.caption or None)
    except Exception as e:
        logger.exception("Copy to DB channel failed: %s", e)
        await update.message.reply_text("Failed to save file. Make sure the bot is admin in DB channel.")
        return

    saved_chat_id = sent.chat_id
    saved_message_id = sent.message_id
    created_at = int(time.time())
    # calculate delete_at if configured
    dlt_time = int(db_get("dlt_time", "0"))
    delete_at = (created_at + dlt_time) if int(dlt_time) > 0 else None

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO files (saved_chat_id, saved_message_id, owner_id, caption, created_at, delete_at) VALUES (?, ?, ?, ?, ?, ?)",
        (saved_chat_id, saved_message_id, user.id, reply.caption or "", created_at, delete_at or 0),
    )
    file_row_id = cur.lastrowid
    conn.commit()
    conn.close()

    # schedule deletion if needed
    if delete_at:
        when_seconds = delete_at - created_at
        await schedule_deletion(context, file_row_id, when_seconds)

    share_link = f"https://t.me/{(await context.bot.get_me()).username}?start=share_{file_row_id}"
    await update.message.reply_text(f"Saved ✅\nShare link:\n{share_link}")


async def batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /batch - create links for multiple messages provided as message ids in the command text OR
    reply to a message containing multiple media? For simplicity: expects a list of message ids from the same chat.
    Example: /batch 12345 12346 12347   (in same chat where messages are)
    Or reply to a message and provide message ids (not robust). If you want custom batch from channel use /custom_batch.
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    args = context.args
    if not args:
        await update.message.reply_text("Usage: /batch <message_id1> <message_id2> ... (message ids from a channel or chat where bot can access)")
        return

    created_links = []
    for mid_str in args:
        try:
            mid = int(mid_str)
        except:
            continue
        # attempt to copy from DB channel or current chat
        from_chat = update.effective_chat.id
        try:
            sent = await context.bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=from_chat, message_id=mid)
            created_at = int(time.time())
            dlt_time = int(db_get("dlt_time", "0"))
            delete_at = (created_at + dlt_time) if int(dlt_time) > 0 else None
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("INSERT INTO files (saved_chat_id, saved_message_id, owner_id, caption, created_at, delete_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (sent.chat_id, sent.message_id, update.effective_user.id, sent.caption or "", created_at, delete_at or 0))
            file_row_id = cur.lastrowid
            conn.commit()
            conn.close()
            if delete_at:
                await schedule_deletion(context, file_row_id, delete_at - created_at)
            created_links.append(f"https://t.me/{(await context.bot.get_me()).username}?start=share_{file_row_id}")
        except Exception as e:
            logger.warning("Batch copy failed for mid %s: %s", mid, e)
            continue

    if created_links:
        await update.message.reply_text("Batch created:\n" + "\n".join(created_links))
    else:
        await update.message.reply_text("No links created.")


async def custom_batch(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /custom_batch <channel_id> <msgid1> <msgid2> ...
    Admin only — copies messages from specified channel/group into DB channel and returns links.
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    args = context.args
    if len(args) < 2:
        await update.message.reply_text("Usage: /custom_batch <source_chat_id_or_username> <msgid1> <msgid2> ...")
        return
    src = args[0]
    mids = []
    for m in args[1:]:
        try:
            mids.append(int(m))
        except:
            continue
    created_links = []
    for mid in mids:
        try:
            sent = await context.bot.copy_message(chat_id=DB_CHANNEL_ID, from_chat_id=src, message_id=mid)
            created_at = int(time.time())
            dlt_time = int(db_get("dlt_time", "0"))
            delete_at = (created_at + dlt_time) if int(dlt_time) > 0 else None
            conn = sqlite3.connect(DB_PATH)
            cur = conn.cursor()
            cur.execute("INSERT INTO files (saved_chat_id, saved_message_id, owner_id, caption, created_at, delete_at) VALUES (?, ?, ?, ?, ?, ?)",
                        (sent.chat_id, sent.message_id, update.effective_user.id, sent.caption or "", created_at, delete_at or 0))
            file_row_id = cur.lastrowid
            conn.commit()
            conn.close()
            if delete_at:
                await schedule_deletion(context, file_row_id, delete_at - created_at)
            created_links.append(f"https://t.me/{(await context.bot.get_me()).username}?start=share_{file_row_id}")
        except Exception as e:
            logger.warning("custom_batch failed for %s:%s => %s", src, mid, e)
    if created_links:
        await update.message.reply_text("Custom batch created:\n" + "\n".join(created_links))
    else:
        await update.message.reply_text("No links created.")


async def users_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM users")
    cnt = cur.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"Total users: {cnt}")


async def broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /broadcast <message text or reply to a message>
    Admin only. Sends to all known users.
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    text = None
    # if reply to a message, forward that content to all
    if update.message.reply_to_message:
        to_broadcast = update.message.reply_to_message
        is_media = bool(to_broadcast.photo or to_broadcast.document or to_broadcast.video or to_broadcast.audio)
    else:
        text = " ".join(context.args).strip()
        is_media = False

    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users")
    rows = cur.fetchall()
    conn.close()
    sent = 0
    for (chat_id,) in rows:
        try:
            if is_media:
                await context.bot.copy_message(chat_id=chat_id, from_chat_id=to_broadcast.chat_id, message_id=to_broadcast.message_id)
            else:
                await context.bot.send_message(chat_id=chat_id, text=text)
            sent += 1
            await asyncio.sleep(0.05)  # small delay to avoid flood limits
        except Exception as e:
            logger.debug("Broadcast to %s failed: %s", chat_id, e)
            continue
    await update.message.reply_text(f"Broadcast sent to {sent} users.")


async def dbroadcast(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Broadcast with auto-delete after specified seconds.
    Usage: /dbroadcast <seconds> (reply to a message to broadcast)
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not update.message.reply_to_message:
        await update.message.reply_text("Reply to the message you want to broadcast with /dbroadcast <seconds>")
        return
    args = context.args
    if not args:
        await update.message.reply_text("Usage: /dbroadcast <seconds> (reply to message)")
        return
    try:
        seconds = int(args[0])
    except:
        await update.message.reply_text("Provide seconds as integer.")
        return

    to_broadcast = update.message.reply_to_message
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT chat_id FROM users")
    rows = cur.fetchall()
    conn.close()
    sent_count = 0
    for (chat_id,) in rows:
        try:
            sent_msg = None
            # copy content to user
            if to_broadcast.photo or to_broadcast.document or to_broadcast.video or to_broadcast.audio:
                sent_msg = await context.bot.copy_message(chat_id=chat_id, from_chat_id=to_broadcast.chat_id, message_id=to_broadcast.message_id)
            else:
                sent_msg = await context.bot.send_message(chat_id=chat_id, text=to_broadcast.text or "")
            # schedule deletion in user's chat
            if sent_msg:
                # schedule deletion job for this message in user's chat
                context.job_queue.run_once(lambda c, s=sent_msg: asyncio.create_task(_try_delete_message(c, s.chat_id, s.message_id)), seconds)
            sent_count += 1
            await asyncio.sleep(0.03)
        except Exception as e:
            logger.debug("dbroadcast fail: %s", e)
            continue
    await update.message.reply_text(f"dbroadcast sent to {sent_count} users. Messages will be auto-deleted after {seconds}s.")


async def _try_delete_message(context: ContextTypes.DEFAULT_TYPE, chat_id: int, message_id: int):
    # wrapper because lambda can't be async
    try:
        await asyncio.sleep(0)  # allow proper context
        await context.bot.delete_message(chat_id, message_id)
    except Exception:
        pass


async def stats(update: Update, context: ContextTypes.DEFAULT_TYPE):
    # show uptime and some stats
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    # uptime = bot start time stored in meta
    started_at = db_get("started_at", None)
    if not started_at:
        started_at = str(int(time.time()))
        db_set("started_at", started_at)
    uptime_seconds = int(time.time()) - int(started_at)
    uptime = str(timedelta(seconds=uptime_seconds))
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM files WHERE deleted=0")
    files_count = cur.fetchone()[0]
    cur.execute("SELECT COUNT(*) FROM users")
    users_count = cur.fetchone()[0]
    conn.close()
    await update.message.reply_text(f"Uptime: {uptime}\nStored files: {files_count}\nKnown users: {users_count}")


async def dlt_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    /dlt_time <seconds>  (admin) - set default auto delete time for saved files
    """
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /dlt_time <seconds> (0 means never auto-delete)")
        return
    try:
        sec = int(context.args[0])
    except:
        await update.message.reply_text("Invalid number")
        return
    db_set("dlt_time", str(sec))
    await update.message.reply_text(f"Default delete time set to {sec} seconds.")


async def check_dlt_time(update: Update, context: ContextTypes.DEFAULT_TYPE):
    val = db_get("dlt_time", "0")
    await update.message.reply_text(f"Current default delete time: {val} seconds.")


# --- Ban / Unban / Banlist ---
async def ban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /ban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("Invalid user id.")
        return
    db_set(f"ban_{uid}", "1")
    await update.message.reply_text(f"User {uid} banned.")


async def unban(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    if not context.args:
        await update.message.reply_text("Usage: /unban <user_id>")
        return
    try:
        uid = int(context.args[0])
    except:
        await update.message.reply_text("Invalid user id.")
        return
    db_set(f"ban_{uid}", "0")
    await update.message.reply_text(f"User {uid} unbanned.")


async def banlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
async def banlist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update.effective_user.id):
        await update.message.reply_text("Admin only.")
        return

    conn = sqlite
