#!/usr/bin/env python3
"""
Telegram File Store Bot (Fully Fixed ✅)
-----------------------------------------
✅ Admin-only uploads
✅ Working share links
✅ Force-join before access
✅ SQLite database
✅ /help and /user commands
✅ File auto delete (user copy) after 1 hour
"""

import os
import sqlite3
import datetime
import logging
from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
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
DB_CHANNEL_ID = -1003292247930  # your private DB channel id
BOT_USERNAME = "Cornsebot"
ADMIN_ID = 7681308594
FORCE_JOIN_CHANNEL = "Cornsehub"  # channel username without @
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


# ---------------- FORCE JOIN CHECK ----------------
async def check_join(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    try:
        member = await context.bot.get_chat_member(f"@{FORCE_JOIN_CHANNEL}", user.id)
        if member.status in ["member", "administrator", "creator"]:
            return True
    except Exception:
        pass

    join_button = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton(
                    "📢 Join Channel",
                    url=f"https://t.me/{FORCE_JOIN_CHANNEL}",
                )
            ]
        ]
    )
    await update.message.reply_text(
        f"Hello 👋 {user.first_name},\n\n"
        "You need to join my Channel to use me.\n\n"
        "Please join below 👇",
        reply_markup=join_button,
    )
    return False


# ---------------- /start ----------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    user_name = user.first_name or "User"
    args = context.args

    # handle share links
    if args and args[0].startswith("share_"):
        file_db_id = args[0].split("_", 1)[1]
        # check join before sending
        if not await check_join(update, context):
            return
        await send_file_to_user(update, context, file_db_id)
        return

    # admin welcome
    if user.id == ADMIN_ID:
        await update.message.reply_text(
            f"Hello {user_name}!\n"
            "Send me a file (admin only). Use a share link to get files.\n/help"
        )
        return

    # check join for user
    if not await check_join(update, context):
        return

    # already joined
    await update.message.reply_text(
        f"Hello 👋 {user_name}!\n\n"
        "I can store private files in a secured channel.\n"
        "Other users can access them using special share links."
    )


# ---------------- HANDLE FILE (ADMIN) ----------------
async def handle_file(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if user.id != ADMIN_ID:
        await update.message.reply_text("Only admin can upload files.")
        return

    msg = update.message
    caption = msg.caption or ""

    # detect media
    file_type, file_id = None, None
    if msg.document:
        file_type, file_id = "document", msg.document.file_id
    elif msg.photo:
        file_type, file_id = "photo", msg.photo[-1].file_id
    elif msg.video:
        file_type, file_id = "video", msg.video.file_id
    elif msg.audio:
        file_type, file_id = "audio", msg.audio.file_id
    elif msg.voice:
        file_type, file_id = "voice", msg.voice.file_id
    elif msg.sticker:
        file_type, file_id = "sticker", msg.sticker.file_id
    else:
        await msg.reply_text("Unsupported file type.")
        return

    # forward to DB channel
    sent = await context.bot.copy_message(
        chat_id=DB_CHANNEL_ID,
        from_chat_id=msg.chat_id,
        message_id=msg.message_id,
    )

    # save to DB
    file_db_id = await save_file(sent.message_id, file_type, caption)
    share_link = f"https://t.me/{BOT_USERNAME}?start=share_{file_db_id}"

    await msg.reply_text(
        f"✅ File saved!\n"
        f"🔗 Share link: {share_link}\n"
        f"🆔 ID: {file_db_id}"
    )


# ---------------- SEND FILE TO USER ----------------
async def send_file_to_user(update: Update, context: ContextTypes.DEFAULT_TYPE, file_db_id):
    cur.execute("SELECT * FROM files WHERE id=?", (file_db_id,))
    data = cur.fetchone()
    if not data:
        await update.message.reply_text("❌ File not found.")
        return

    _, file_id, file_type, caption, date, views = data
    chat_id = update.effective_chat.id

    # fetch actual message from DB channel
    try:
        file_message = await context.bot.copy_message(
            chat_id=chat_id,
            from_chat_id=DB_CHANNEL_ID,
            message_id=file_id,
        )
    except Exception as e:
        await update.message.reply_text("⚠️ File couldn't be fetched. It may be missing.")
        logger.error(str(e))
        return

    # increase view count
    cur.execute("UPDATE files SET views = views + 1 WHERE id=?", (file_db_id,))
    conn.commit()

    # auto delete after 1h
    await context.job_queue.run_once(
        lambda ctx: context.bot.delete_message(chat_id, file_message.message_id),
        TEMP_EXPIRY_SECONDS,
    )

    await update.message.reply_text(
        "⏳ This file will stay for 1 hour. Original stays safe in storage."
    )


# ---------------- /help ----------------
async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "📝 <b>Help Menu</b>\n\n"
        "/start - Start bot or get a file via link\n"
        "/help - Show this help message\n"
        "/user - Admin only: List stored files\n\n"
        "Send media to save (admin only).",
        parse_mode="HTML",
    )


# ---------------- /user ----------------
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
        text += f"🆔 {fid} | {ftype}\n👁 {views} views\n📄 {caption or '-'}\n\n"

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
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("user", user_list))
    app.add_handler(MessageHandler(media_filter, handle_file))

    app.run_polling()


if __name__ == "__main__":
    main()
