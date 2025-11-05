#!/usr/bin/env python3
"""
File Store Bot
- python-telegram-bot v20+ (async)
- Stores admin-uploaded files into a private channel, creates deep links,
  and serves files to users only if they joined the main channel.

Usage:
  export BOT_TOKEN="..." 
  export MAIN_CHANNEL="@Cornsehub"     # or channel username
  export PRIVATE_CHANNEL_ID="-1003292247930"
  export ADMIN_IDS="7681308594"       # comma-separated admin user IDs
  python filestore_bot.py
"""

import logging
import os
import sqlite3
import uuid
from typing import Optional

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.ext import (
    ApplicationBuilder,
    ContextTypes,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)

# ------------ Config (use environment variables) -------------
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MAIN_CHANNEL = os.environ.get("MAIN_CHANNEL")  # e.g. "@Cornsehub" or channel ID
PRIVATE_CHANNEL_ID = int(os.environ.get("PRIVATE_CHANNEL_ID", "-1003292247930"))
ADMIN_IDS = {int(x.strip()) for x in os.environ.get("ADMIN_IDS", "").split(",") if x.strip()}

if not BOT_TOKEN:
    raise RuntimeError("Set BOT_TOKEN as environment variable")

DB_PATH = "filestore.db"

# ------------ Logging -------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)


# ------------ Database helpers -------------
def init_db():
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        """
    CREATE TABLE IF NOT EXISTS files (
        file_key TEXT PRIMARY KEY,
        chat_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        uploader_id INTEGER,
        title TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )"""
    )
    con.commit()
    con.close()


def save_file_mapping(file_key: str, chat_id: int, message_id: int, uploader_id: Optional[int], title: Optional[str]):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute(
        "INSERT INTO files (file_key, chat_id, message_id, uploader_id, title) VALUES (?, ?, ?, ?, ?)",
        (file_key, chat_id, message_id, uploader_id, title),
    )
    con.commit()
    con.close()


def get_file_mapping(file_key: str):
    con = sqlite3.connect(DB_PATH)
    cur = con.cursor()
    cur.execute("SELECT chat_id, message_id FROM files WHERE file_key = ?", (file_key,))
    row = cur.fetchone()
    con.close()
    return row  # None or (chat_id, message_id)


# ------------ Bot Handlers -------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles /start and deep links like /start file_<key>"""
    user = update.effective_user
    args = context.args or []

    def join_keyboard():
        return InlineKeyboardMarkup(
            [
                [InlineKeyboardButton("JOIN CHANNEL", url=f"https://t.me/{MAIN_CHANNEL.lstrip('@')}")],
                [InlineKeyboardButton("CLOSE", callback_data="close_msg")],
            ]
        )

    def close_keyboard():
        return InlineKeyboardMarkup([[InlineKeyboardButton("CLOSE", callback_data="close_msg")]])

    # If user clicked deep link with file key
    if args and args[0].startswith("file_"):
        file_key = args[0]  # e.g. file_1234-uuid
        # check membership
        try:
            member = await context.bot.get_chat_member(MAIN_CHANNEL, user.id)
            # 'member.status' in ['creator','administrator','member'] means joined
            joined = member.status not in ("left", "kicked")
        except Exception as e:
            # Could not determine membership (maybe bot not admin in channel)
            logger.warning("get_chat_member error: %s", e)
            joined = False

        if not joined:
            await update.message.reply_text(
                f"Hello 👋, {user.first_name}\n\nYou need to join my Channel/Group to use me.\n\nKindly please join the channel and then open this link again.",
                reply_markup=join_keyboard(),
            )
            return

        # user is a member — fetch file mapping and send file
        mapping = get_file_mapping(file_key)
        if not mapping:
            await update.message.reply_text("Sorry, the file link is invalid or has expired.", reply_markup=close_keyboard())
            return

        src_chat_id, src_message_id = mapping
        try:
            # Copy the message (file) from private channel to user
            await context.bot.copy_message(chat_id=update.effective_chat.id, from_chat_id=src_chat_id, message_id=src_message_id)
            await update.message.reply_text("Here is your file. (Message will be deleted if you press CLOSE)", reply_markup=close_keyboard())
        except Exception as e:
            logger.exception("Failed to copy message: %s", e)
            await update.message.reply_text("Failed to deliver the file. Contact admin.", reply_markup=close_keyboard())
        return

    # Default /start (no args)
    await update.message.reply_text(
        f"Hello 👋, {user.first_name}\n\nYou need to join in my Channel/Group to use me\n\nKindly Please join Channel 👇",
        reply_markup=InlineKeyboardMarkup([[InlineKeyboardButton("JOIN CHANNEL", url=f"https://t.me/{MAIN_CHANNEL.lstrip('@')}")]]),
    )


async def close_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Deletes the message that had the close button"""
    query = update.callback_query
    await query.answer()
    try:
        await query.message.delete()
    except Exception as e:
        logger.warning("Could not delete message: %s", e)


async def handle_admin_upload(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Admin: send any file (document/photo/audio/video/voice) directly to bot to store it."""
    user = update.effective_user
    if user.id not in ADMIN_IDS:
        await update.message.reply_text("You are not authorized to upload files.")
        return

    # The incoming message may contain any media; we'll forward/copy it to the private channel.
    sent = None
    try:
        # Use copy_message to preserve as a channel message (avoids forwarding 'via' text)
        copied = await context.bot.copy_message(chat_id=PRIVATE_CHANNEL_ID, from_chat_id=update.effective_chat.id, message_id=update.message.message_id)
        # copied is a Message object from which we can get message_id
        src_message_id = getattr(copied, "message_id", None)
        file_key = "file_" + uuid.uuid4().hex
        # Save mapping
        title = None
        if update.message.caption:
            title = update.message.caption
        save_file_mapping(file_key=file_key, chat_id=PRIVATE_CHANNEL_ID, message_id=src_message_id, uploader_id=user.id, title=title)

        # create deep link for your bot username
        bot_username = (await context.bot.get_me()).username
        deep_link = f"https://t.me/{bot_username}?start={file_key}"

        await update.message.reply_text(
            "File saved successfully.\n\nShare this link with users:\n" + deep_link + "\n\nNote: users must join the main channel to access the file."
        )
    except Exception as e:
        logger.exception("Error saving file: %s", e)
        await update.message.reply_text("Failed to save file. Make sure the bot is admin of the private channel and can post there.")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Admins: send a file to bot (in private) to create a shareable link.\nUsers: open the provided link and join the channel to receive the file.")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text("Unknown command. Use /help.")


# ------------ Start the bot -------------
def main():
    init_db()
    application = ApplicationBuilder().token(BOT_TOKEN).build()

    application.add_handler(CommandHandler("start", start))
    application.add_handler(CommandHandler("help", help_cmd))
    application.add_handler(CallbackQueryHandler(close_callback, pattern="^close_msg$"))

    # Admin file uploads — accept any message that contains media/document/photo/video/audio/voice
    media_filter = filters.Document.ALL | filters.PHOTO | filters.VIDEO | filters.AUDIO | filters.VOICE
    application.add_handler(MessageHandler(media_filter & filters.ChatType.PRIVATE, handle_admin_upload))

    # fallback
    application.add_handler(MessageHandler(filters.COMMAND, unknown))

    logger.info("Starting bot")
    application.run_polling()


if __name__ == "__main__":
    main()
