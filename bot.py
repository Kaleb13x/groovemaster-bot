import os
import threading
import logging
import requests
import musicbrainzngs
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

# -------------------------------------------------------------------
# CONFIGURATION
# -------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = "8724485150:AAEbDfndk1cr1EbqikY_XNxSsJq4vJK7G6k"

# Configure MusicBrainz API requirements
musicbrainzngs.set_useragent(
    "GrooveMasterBot",
    "1.0",
    "https://github.com/kaleb13x/groovemaster-bot"
)

# Setup logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

# User search state cache: { user_id: [ {artist, track, album}, ... ] }
USER_SEARCHES = {}

# -------------------------------------------------------------------
# HEALTH CHECK SERVER (For Render Web Service)
# -------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Bot is alive!")

    def log_message(self, format, *args):
        pass

def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    server = HTTPServer(("0.0.0.0", port), HealthCheckHandler)
    server.serve_forever()

# -------------------------------------------------------------------
# HELPER FUNCTIONS
# -------------------------------------------------------------------
async def send_lyrics_in_chunks(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Splits long text into chunks under Telegram's 4096 character limit line-by-line."""
    MAX_LENGTH = 4000
    chat_id = update.effective_chat.id

    if len(text) <= MAX_LENGTH:
        await context.bot.send_message(chat_id=chat_id, text=text, parse_mode="Markdown")
        return

    lines = text.split('\n')
    current_chunk = ""

    for line in lines:
        if len(current_chunk) + len(line) + 1 > MAX_LENGTH:
            await context.bot.send_message(chat_id=chat_id, text=current_chunk, parse_mode="Markdown")
            current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk:
        await context.bot.send_message(chat_id=chat_id, text=current_chunk, parse_mode="Markdown")

def fetch_lrclib_lyrics(track_name: str, artist_name: str) -> str:
    """Fetches plain lyrics from the free LRCLIB API."""
    url = "https://lrclib.net/api/get"
    params = {
        "track_name": track_name,
        "artist_name": artist_name
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            data = response.json()
            lyrics = data.get("plainLyrics")
            if lyrics:
                return lyrics
            return "_Plain lyrics not available for this track._"
        elif response.status_code == 404:
            return "_❌ Lyrics not found on LRCLIB._"
        else:
            return f"_⚠️ Server returned status code {response.status_code}._"
    except Exception as e:
        logger.error(f"LRCLIB API Error: {e}")
        return "_❌ Error connecting to LRCLIB lyrics service._"

# -------------------------------------------------------------------
# TELEGRAM HANDLERS
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles the /start command."""
    welcome_text = (
        "🎵 *Welcome to GrooveMaster Bot!*\n\n"
        "Send me any *song title* or *artist and song*, and I will find the best matches with albums and lyrics!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def search_song(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches MusicBrainz for tracks based on user input."""
    query = update.message.text.strip()
    user_id = update.effective_user.id
    
    await update.message.reply_text(f"🔍 Searching MusicBrainz for: *{query}*...", parse_mode="Markdown")

    try:
        result = musicbrainzngs.search_recordings(query=query, limit=5)
        recordings = result.get("recording-list", [])

        if not recordings:
            await update.message.reply_text("❌ *No tracks found.* Try refining your search query.")
            return

        results_list = []
        keyboard = []

        for idx, rec in enumerate(recordings):
            track_title = rec.get("title", "Unknown Title")
            
            artist_name = "Unknown Artist"
            if "artist-credit" in rec and len(rec["artist-credit"]) > 0:
                artist_name = rec["artist-credit"][0].get("artist", {}).get("name", "Unknown Artist")

            album_name = "Unknown Album"
            if "release-list" in rec and len(rec["release-list"]) > 0:
                album_name = rec["release-list"][0].get("title", "Unknown Album")

            results_list.append({
                "track": track_title,
                "artist": artist_name,
                "album": album_name
            })

            button_text = f"{idx + 1}. {track_title} ({album_name})"
            if len(button_text) > 60:
                button_text = button_text[:57] + "..."
                
            keyboard.append([InlineKeyboardButton(button_text, callback_data=f"select_{idx}")])

        USER_SEARCHES[user_id] = results_list
        reply_markup = InlineKeyboardMarkup(keyboard)

        await update.message.reply_text(
            "🎶 *Select the correct track and album below:*",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"MusicBrainz search error: {e}")
        await update.message.reply_text("❌ *An error occurred while searching. Please try again.*", parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button selections for song choice."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data

    if not data.startswith("select_"):
        return

    idx = int(data.split("_")[1])
    user_data = USER_SEARCHES.get(user_id)

    if not user_data or idx >= len(user_data):
        await query.edit_message_text("❌ *Search session expired. Please search again.*", parse_mode="Markdown")
        return

    selected_song = user_data[idx]
    track = selected_song["track"]
    artist = selected_song["artist"]
    album = selected_song["album"]

    await query.edit_message_text(
        f"📥 Fetching lyrics for *{track}* by *{artist}* _(Album: {album})_...",
        parse_mode="Markdown"
    )

    lyrics = fetch_lrclib_lyrics(track_name=track, artist_name=artist)
    
    header = (
        f"🎤 *{track}*\n"
        f"👤 Artist: *{artist}*\n"
        f"💿 Album: *{album}*\n"
        f"────────────────────────\n\n"
    )
    full_text = header + lyrics

    await send_lyrics_in_chunks(update, context, full_text)

# -------------------------------------------------------------------
# MAIN APPLICATION
# -------------------------------------------------------------------
def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, search_song))

    print("🤖 GrooveMaster Bot is running with LRCLIB lyrics integration...")
    app.run_polling()

if __name__ == "__main__":
    main()
