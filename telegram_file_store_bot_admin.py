#!/usr/bin/env python3
"""
Telegram File Store Bot (Final Version ✅)
-----------------------------------------
✅ Admin-only uploads
✅ User messages expire after 1 hour
✅ Files stored permanently in private channel
✅ SQLite local database
✅ /user command for admin (view stats)
✅ Force join channel before using bot
"""

import os
import sqlite3
import datetime
import logging
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InputFile,
)
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# ---------------- CONFIG ----------------
TOKEN = "8222645012:AAEQMNK31oa5hDo_9OEStfNL7FMBdZMkUFM"
DB_CHANNEL_ID = -1003292247930
BOT_USERNAME = "Cornsebot"
ADMIN_ID = 7681308594
FORCE_JOIN_CHANNEL = "Cornsehub"  # without @
TEMP_EXPIRY_SECONDS = 3600  # 1 hour
DB_PATH = "files.db"

# ---------------- LOGGING ----------------
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# ---------------- DATABASE ----------------
conn = sqlite3.connect(DB_PATH, check_same_thread=False)
cur = conn.cursor()
cur.execute(
    """CREATE TABLE IF NOT EXISTS files (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        file_id TEXT,
        file_type TEXT,
        caption TEXT,
        date TEXT,
        views INTEGER DEFAULT 0
    )"""
)
conn.commit()

# ---------------- SAVE FILE ----------------
async def save_file(file_id, file_type, caption):
    cur.execute(
        "INSERT INTO files (file_id, file_type, caption, date) VALUES (?, ?, ?, ?)",
        (file_id, file_type, caption, datetime.datetime.utcnow().isoformat()),
    )
    conn.commit()
    return cur.lastrowid

# ---------------- /start ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_name = user.first_name or "User"
    args = context.args

    # Handle shared file link
    if args and args[0].startswith("share_"):
        file_id = args[0].split("_", 1)[1]
        await send_file_to_user(update, context, file_id)
        return

    # If admin
    if user.id == ADMIN_ID:
        await update.message.reply_text(
            f"Hello {user_name}!\n"
            "Send me a file (admin only). Use a share link to get files.\n/help"
        )
        return

    # Force join check
    try:
        member = await context.bot.get_chat_member(f"@{FORCE_JOIN_CHANNEL}", user.id)
        if member.status not in ["member", "administrator", "creator"]:
            join_button = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "📢 Join My Channel",
                            url=f"https://t.me/{FORCE_JOIN_CHANNEL}",
                        )
                    ]
                ]
            )
            await update.message.reply_text(
                f"Hello 👋 {user_name},\n\n"
                "You need to join my Channel to use me.\n\n"
                "Kindly please join the channel below 👇",
                reply_markup=join_button,
            )
            return
    except Exception:
        join_button = InlineKeyboardMarkup(
            [
                [
                    InlineKeyboardButton(
                        "📢 Join My Channel",
                        url=f"https://t.me/{FORCE_JOIN_CHANNEL}",
                    )
                ]
            ]
        )
        await update.message.reply_text(
            f"Hello 👋 {user_name},\n\n"
            "You need to join my Channel to use me.\n\n"
            "Kindly please join the channel below 👇",
            reply_markup=join_button,
        )
        return

    # Already joined
    await update.message.reply_text(
        f"Hello 👋 {user_name}!\n\n"
        "I can store private files in a secured channel.\n"
        "Other users can access them using special share links."
    )

# ---------------- FILE HANDLER ----------------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("Only admin can upload files.")
        return

    msg = update.message
    caption = msg.caption or ""

    # Detect media type and get file_id
    file_type, file_id = None, None
    if msg.document:
        file_type = "document"
        file_id = msg.document.file_id
    elif msg.photo:
        file_type = "photo"
        file_id = msg.photo[-1].file_id
    elif msg.video:
        file_type = "video"
        file_id = msg.video.file_id
    elif msg.audio:
        file_type = "audio"
        file_id = msg.audio.file_id
    elif msg.voice:
        file_type = "voice"
        file_id = msg.voice.file_id
    elif msg.sticker:
        file_type = "sticker"
        file_id = msg.sticker.file_id
    else:
        await msg.reply_text("Unsupported file type.")
        return

    # Forward file to channel
    sent = await context.bot.copy_message(
        chat_id=DB_CHANNEL_ID,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id,
    )

    # Save in DB
    file_db_id = await save_file(sent.message_id, file_type, caption)
    share_link = f"https://t.me/{BOT_USERNAME}?start=share_{file_db_id}"

    await msg.reply_text(
        f"✅ File saved!\nShare link: {share_link}\nID: {file_db_id}"
    )

# ---------------- Send file to user ----------------
async def send_file_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, file_db_id):
    user = update.effective_user
    chat_id = update.effective_chat.id

    cur.execute("SELECT * FROM files WHERE id=?", (file_db_id,))
    data = cur.fetchone()
    if not data:
        await update.message.reply_text("❌ File not found or deleted.")
        return

    _, file_id, file_type, caption, date, views = data

    # Send file
    if file_type == "photo":
        sent = await context.bot.send_photo(chat_id, file_id, caption=caption)
    elif file_type == "video":
        sent = await context.bot.send_video(chat_id, file_id, caption=caption)
    elif file_type == "document":
        sent = await context.bot.send_document(chat_id, file_id, caption=caption)
    elif file_type == "audio":
        sent = await context.bot.send_audio(chat_id, file_id, caption=caption)
    elif file_type == "voice":
        sent = await context.bot.send_voice(chat_id, file_id, caption=caption)
    elif file_type == "sticker":
        sent = await context.bot.send_sticker(chat_id, file_id)
    else:
        await update.message.reply_text("❌ Unsupported file type.")
        return

    # Update view count
    cur.execute("UPDATE files SET views = views + 1 WHERE id=?", (file_db_id,))
    conn.commit()

    # Auto delete after 1 hour
    await context.job_queue.run_once(
        lambda _: context.bot.delete_message(chat_id, sent.message_id),
        TEMP_EXPIRY_SECONDS,
    )

    await update.message.reply_text(
        f"⚠️ This file will be available in this chat for 60 minutes.\n"
        f"After that it will be removed (original stays safe)."
    )

# ---------------- /user command (admin only) ----------------
async def user_list(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ADMIN_ID:
        return await update.message.reply_text("Only admin can use this command.")

    cur.execute("SELECT * FROM files ORDER BY id DESC")
    rows = cur.fetchall()

    if not rows:
        await update.message.reply_text("No files saved yet.")
        return

    text = "📂 <b>Stored Files</b>\n\n"
    for r in rows:
        fid, _, ftype, caption, date, views = r
        text += f"ID: <b>{fid}</b> | Type: {ftype}\nViews: {views}\nCaption: {caption or '-'}\n\n"

    await update.message.reply_text(text, parse_mode="HTML")

# ---------------- MAIN ----------------
def main():
    app = ApplicationBuilder().token(TOKEN).build()

    media_filter = (
        filters.Document.ALL
        | filters.PHOTO
        | filters.AUDIO
        | filters.VOICE
        | filters.VIDEO
        | filters.Sticker.ALL
    )

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("user", user_list))
    app.add_handler(MessageHandler(media_filter, handle_file))

    app.run_polling()

if __name__ == "__main__":
    main()
