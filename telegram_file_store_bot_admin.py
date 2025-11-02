#!/usr/bin/env python3
"""
Telegram File Store & Share Bot (Admin-only uploads + 1-hour temporary user access)

Requirements:
    pip install python-telegram-bot==20.5

Env vars:
    TOKEN           - Bot token from BotFather
    DB_CHANNEL_ID   - Private channel ID where files are stored (bot must be admin), e.g. -1001234567890
    BOT_USERNAME    - (optional) bot username (without @) to create deep links

Admin:
    ADMIN_ID = 7681308594

Run:
    export TOKEN="123:ABC..."
    export DB_CHANNEL_ID="-1001234567890"
    export BOT_USERNAME="YourBotUsername"
    python3 telegram_file_store_bot_admin.py
"""
import os
import logging
import sqlite3
import datetime
import asyncio
from typing import Optional, Tuple, List

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, Message
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    filters,
)

# ---------------- CONFIG ----------------
# NOTE: these are direct values you provided. If you prefer environment vars instead,
# replace these with os.environ.get("TOKEN") etc. and set the env vars before running.
TOKEN = "8222645012:AAEQMNK31oa5hDo_9OEStfNL7FMBdZMkUFM"
DB_CHANNEL_ID = "-1003292247930"   # must be like -100xxxxxxxx
DB_PATH = "files.db"
BOT_USERNAME = "Cornsebot"         # without @
ADMIN_ID = 7681308594              # provided by user
TEMP_EXPIRY_SECONDS = 3600         # 1 hour

if not TOKEN or not DB_CHANNEL_ID:
    raise SystemExit("Please set TOKEN and DB_CHANNEL_ID before running.")

# ------- logging ----------
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
log = logging.getLogger(__name__)

# ------- SQLite helpers ------
def init_db():
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    # files: permanent records pointing to message stored in channel
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS files (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            channel_msg_id INTEGER NOT NULL,
            file_type TEXT,
            file_id TEXT,
            file_unique_id TEXT,
            caption TEXT,
            uploader_id INTEGER,
            uploader_name TEXT,
            timestamp TEXT
        )
        """
    )
    # views: records who viewed which file and when
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS views (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER,
            user_id INTEGER,
            user_name TEXT,
            timestamp TEXT
        )
        """
    )
    # temp_access: track message copies sent to users and expiry
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS temp_access (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            file_id INTEGER,
            user_chat_id INTEGER,
            sent_message_id INTEGER,
            expires_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()

def insert_file(channel_msg_id: int, file_type: str, file_id: str, file_unique_id: str,
                caption: Optional[str], uploader_id: int, uploader_name: Optional[str]) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO files (channel_msg_id, file_type, file_id, file_unique_id, caption, uploader_id, uploader_name, timestamp) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (channel_msg_id, file_type, file_id, file_unique_id, caption, uploader_id, uploader_name, datetime.datetime.utcnow().isoformat()),
    )
    file_db_id = cur.lastrowid
    conn.commit()
    conn.close()
    return file_db_id

def get_file_record(file_db_id: int) -> Optional[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, channel_msg_id, file_type, file_id, file_unique_id, caption, uploader_id, uploader_name, timestamp FROM files WHERE id = ?", (file_db_id,))
    row = cur.fetchone()
    conn.close()
    return row

def list_recent_files(limit: int = 20) -> List[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, file_type, caption, uploader_name, timestamp FROM files ORDER BY id DESC LIMIT ?", (limit,))
    rows = cur.fetchall()
    conn.close()
    return rows

def record_view(file_db_id: int, user_id: int, user_name: Optional[str]):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO views (file_id, user_id, user_name, timestamp) VALUES (?, ?, ?, ?)",
                (file_db_id, user_id, user_name, datetime.datetime.utcnow().isoformat()))
    conn.commit()
    conn.close()

def get_view_count(file_db_id: int) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM views WHERE file_id = ?", (file_db_id,))
    count = cur.fetchone()[0]
    conn.close()
    return count

def add_temp_access(file_db_id: int, user_chat_id: int, sent_message_id: int, expires_at_iso: str) -> int:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("INSERT INTO temp_access (file_id, user_chat_id, sent_message_id, expires_at) VALUES (?, ?, ?, ?)",
                (file_db_id, user_chat_id, sent_message_id, expires_at_iso))
    tid = cur.lastrowid
    conn.commit()
    conn.close()
    return tid

def remove_temp_access_by_rowid(rowid: int):
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("DELETE FROM temp_access WHERE id = ?", (rowid,))
    conn.commit()
    conn.close()

def get_pending_temp_access() -> List[Tuple]:
    conn = sqlite3.connect(DB_PATH)
    cur = conn.cursor()
    cur.execute("SELECT id, file_id, user_chat_id, sent_message_id, expires_at FROM temp_access")
    rows = cur.fetchall()
    conn.close()
    return rows

# ------- helper: schedule delete ----------
async def schedule_deletion(app, rowid: int, chat_id: int, message_id: int, expires_at_iso: str):
    """
    Schedule deletion of a message at or after expires_at.
    If expiry time already passed, attempt delete immediately and cleanup DB.
    """
    try:
        expires_at = datetime.datetime.fromisoformat(expires_at_iso)
    except Exception:
        # if invalid data, delete entry and return
        log.warning("Invalid expires_at for temp_access id %s", rowid)
        remove_temp_access_by_rowid(rowid)
        return

    now = datetime.datetime.utcnow()
    delay = (expires_at - now).total_seconds()
    if delay > 0:
        log.info("Scheduling deletion of msg %s in chat %s after %.1f seconds (row %s)", message_id, chat_id, delay, rowid)
        try:
            await asyncio.sleep(delay)
        except asyncio.CancelledError:
            # app shutting down maybe
            log.info("Deletion sleep cancelled for row %s", rowid)
    else:
        log.info("Expiry already passed or due now for row %s (deleting immediately)", rowid)

    # Attempt delete (ignore errors)
    try:
        await app.bot.delete_message(chat_id=chat_id, message_id=message_id)
        log.info("Deleted temporary message %s in chat %s (row %s)", message_id, chat_id, rowid)
    except Exception as e:
        log.info("Could not delete message %s in chat %s: %s", message_id, chat_id, e)
    # Clean DB
    try:
        remove_temp_access_by_rowid(rowid)
    except Exception as e:
        log.exception("Failed to remove temp_access row %s: %s", rowid, e)

# ------- Bot handlers -------
async def start_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    args = context.args
    if args and args[0].startswith("share_"):
        # Serve a share link
        try:
            file_db_id = int(args[0].split("share_")[1])
        except Exception:
            await update.message.reply_text("Invalid share id.")
            return
        await serve_file_to_user(update, context, file_db_id)
        return

    kb = []
    if BOT_USERNAME:
        kb = [[InlineKeyboardButton("Upload file (admin only)", switch_inline_query_current_chat="")]]
    text = f"Hello {user.first_name if user else 'there'}!\nSend me a file (admin only). Use a share link to get files. /help"
    await update.message.reply_text(text, reply_markup=InlineKeyboardMarkup(kb) if kb else None)

async def serve_file_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, file_db_id: int):
    """
    Copies the message stored in the DB channel to the user's chat,
    records a view, schedules deletion after TEMP_EXPIRY_SECONDS and
    stores temp_access record.
    """
    user = update.effective_user
    chat_id = update.effective_chat.id
    record = get_file_record(file_db_id)
    if not record:
        await update.message.reply_text("File not found.")
        return
    _, channel_msg_id, *_ = record

    # Copy message from channel to user
    try:
        copied: Message = await context.bot.copy_message(chat_id=chat_id, from_chat_id=int(DB_CHANNEL_ID), message_id=channel_msg_id)
    except Exception as e:
        log.exception("Failed to copy message from channel to user")
        await update.message.reply_text("Failed to deliver file. Contact admin.")
        return

    # Record view
    record_view(file_db_id, user.id if user else None, user.full_name if user else None)

    # Add temp_access entry and schedule deletion
    expires_at = (datetime.datetime.utcnow() + datetime.timedelta(seconds=TEMP_EXPIRY_SECONDS)).isoformat()
    sent_msg_id = copied.message_id
    rowid = add_temp_access(file_db_id, chat_id, sent_msg_id, expires_at)

    # schedule deletion in background
    # note: store task on application for potential cancellation (not strictly required)
    context.application.create_task(schedule_deletion(context.application, rowid, chat_id, sent_msg_id, expires_at))

    await context.bot.send_message(chat_id=chat_id,
                                   text=f"⚠️ This file will be available in this chat for {TEMP_EXPIRY_SECONDS//60} minutes. After that it will be removed from your chat (the original remains in the private channel).",
                                   reply_to_message_id=sent_msg_id)

async def media_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Accept media only from admin. Bot copies the received message to DB channel,
    stores metadata and returns a share link.
    """
    msg = update.message
    if not msg:
        return
    user = update.effective_user
    # Enforce admin-only uploads
    if not user or user.id != ADMIN_ID:
        await msg.reply_text("❌ Upload failed: only admin can upload files to this bot.")
        return

    # Determine media type & representative file_obj
    file_type = None
    file_obj = None
    caption = getattr(msg, "caption", None)

    if msg.document:
        file_type = "document"
        file_obj = msg.document
    elif msg.photo:
        file_type = "photo"
        file_obj = msg.photo[-1]
    elif msg.video:
        file_type = "video"
        file_obj = msg.video
    elif msg.audio:
        file_type = "audio"
        file_obj = msg.audio
    elif msg.voice:
        file_type = "voice"
        file_obj = msg.voice
    elif msg.sticker:
        file_type = "sticker"
        file_obj = msg.sticker
    else:
        await msg.reply_text("Please send a supported media type (photo, video, document, audio, voice, sticker).")
        return

    # Copy the whole message to the DB channel. Bot must be admin in the channel.
    try:
        copied = await context.bot.copy_message(chat_id=int(DB_CHANNEL_ID), from_chat_id=msg.chat.id, message_id=msg.message_id)
    except Exception as e:
        log.exception("Failed to copy to channel")
        await msg.reply_text("Failed to save file to channel. Make sure the bot is admin in the DB channel and the channel ID is correct.")
        return

    channel_msg_id = copied.message_id
    file_id = getattr(file_obj, "file_id", "")
    file_unique_id = getattr(file_obj, "file_unique_id", "")
    uploader_id = user.id
    uploader_name = user.full_name

    file_db_id = insert_file(channel_msg_id, file_type, file_id, file_unique_id, caption, uploader_id, uploader_name)

    if BOT_USERNAME:
        link = f"https://t.me/{BOT_USERNAME}?start=share_{file_db_id}"
    else:
        link = f"Use this id in /start: share_{file_db_id}"

    await msg.reply_text(f"✅ File saved! Share link: {link}\nID: {file_db_id}")

async def share_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not context.args:
        await update.message.reply_text("Usage: /share <id>")
        return
    try:
        file_db_id = int(context.args[0])
    except ValueError:
        await update.message.reply_text("ID must be a number.")
        return
    await serve_file_to_user(update, context, file_db_id)

async def list_my_files(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Public /list for quick recent files (shows IDs and types). This shows recent 20 files.
    """
    rows = list_recent_files(20)
    if not rows:
        await update.message.reply_text("No files saved yet.")
        return
    text = "📦 Recent Media Files:\n\n"
    for r in rows:
        fid, ftype, cap, uploader_name, ts = r
        text += f"ID {fid} | {ftype or 'unknown'} | {cap or 'no caption'} | {uploader_name or 'admin'} | {ts.split('T')[0]}\n"
    text += "\nUse /share <id> or open the share link to get the file."
    await update.message.reply_text(text)

async def user_stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only /user command that shows files + view counts (recent first)
    """
    user = update.effective_user
    if not user or user.id != ADMIN_ID:
        await update.message.reply_text("Only admin can use this command.")
        return

    rows = list_recent_files(50)
    if not rows:
        await update.message.reply_text("No files saved yet.")
        return
    text = "📂 Recent File Stats:\n\n"
    for r in rows:
        fid, ftype, cap, uploader_name, ts = r
        views = get_view_count(fid)
        text += f"ID {fid} | {ftype or 'unknown'} | {views} views | {ts.split('T')[0]}\n"
    await update.message.reply_text(text)

async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Commands:\n"
        "/start - start\n"
        "/list - recent files\n"
        "/share <id> - get a file\n"
        "/help - this message\n"
    )
    # admin-only
    user = update.effective_user
    if user and user.id == ADMIN_ID:
        text += "/user - admin: show file stats (views)\n"
    await update.message.reply_text(text)

# ------- startup: recover pending deletions -------
async def recover_pending_deletions(application):
    """
    On startup, schedule deletion tasks for pending temp_access rows.
    If expiry already passed, delete immediately (best-effort).
    """
    rows = get_pending_temp_access()
    if not rows:
        return
    now = datetime.datetime.utcnow()
    for r in rows:
        rowid, file_id, user_chat_id, sent_message_id, expires_at = r
        try:
            expires_dt = datetime.datetime.fromisoformat(expires_at)
        except Exception:
            # remove corrupt row
            remove_temp_access_by_rowid(rowid)
            continue
        # compute delay (could be negative)
        delay = (expires_dt - now).total_seconds()
        # schedule using application.create_task
        application.create_task(schedule_deletion(application, rowid, user_chat_id, sent_message_id, expires_at))
    log.info("Recovered and scheduled %d pending temp_access entries", len(rows))

# ------- main ---------
def main():
    init_db()
    app = ApplicationBuilder().token(TOKEN).build()

    app.add_handler(CommandHandler("start", start_handler))
    app.add_handler(CommandHandler("share", share_command))
    app.add_handler(CommandHandler("list", list_my_files))
    app.add_handler(CommandHandler("user", user_stats_command))
    app.add_handler(CommandHandler("help", help_command))

    media_filter = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO
        | filters.Sticker.ALL
    )
    app.add_handler(MessageHandler(media_filter, media_handler))

    # On startup, recover pending deletions
    async def on_startup(app):
        await recover_pending_deletions(app)
        log.info("Startup recovery done.")

    app.post_init = on_startup

    log.info("Bot started")
    app.run_polling()

if __name__ == "__main__":
    main()
