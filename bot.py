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

musicbrainzngs.set_useragent(
    "MusicBot",
    "1.0",
    "https://github.com/kaleb13x/groovemaster-bot"
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger(__name__)

USER_STATES = {}

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

def get_album_cover_url(release_id: str) -> str:
    """Fetches front cover image URL from Cover Art Archive."""
    try:
        url = f"https://coverartarchive.org/release/{release_id}"
        res = requests.get(url, timeout=5)
        if res.status_code == 200:
            data = res.json()
            for img in data.get("images", []):
                if img.get("front"):
                    return img.get("image")
    except Exception as e:
        logger.error(f"Cover Art Archive Error: {e}")
    return None

def fetch_lrclib_lyrics(track_name: str, artist_name: str) -> str:
    """Fetches plain lyrics from LRCLIB API."""
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
    """Dynamically uses the bot's name for the welcome greeting."""
    bot_name = context.bot.first_name or "Music Bot"
    welcome_text = f"🎵 *Welcome to {bot_name}!*\n\nSend me any **artist name** to browse their official studio albums and lyrics!"
    await update.message.reply_text(welcome_text, parse_mode="Markdown")

async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches MusicBrainz for artists based on user input."""
    query = update.message.text.strip()
    user_id = update.effective_user.id
    
    await update.message.reply_text(f"🔍 Searching for artist: *{query}*...", parse_mode="Markdown")

    try:
        result = musicbrainzngs.search_artists(query=query, limit=5)
        artists = result.get("artist-list", [])

        if not artists:
            await update.message.reply_text("❌ *No artists found.* Try a different name.")
            return

        artist_data = []
        keyboard = []

        for idx, artist in enumerate(artists):
            name = artist.get("name", "Unknown Artist")
            country = artist.get("country", "")
            disambiguation = artist.get("disambiguation", "")
            
            label = name
            if disambiguation:
                label += f" ({disambiguation})"
            elif country:
                label += f" [{country}]"

            artist_data.append({
                "id": artist.get("id"),
                "name": name
            })

            keyboard.append([InlineKeyboardButton(label, callback_data=f"art_{idx}")])

        USER_STATES[user_id] = {
            "step": "artist_selected",
            "artists": artist_data
        }

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            "👤 *Select the artist:*",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"MusicBrainz artist search error: {e}")
        await update.message.reply_text("❌ *An error occurred while searching. Please try again.*", parse_mode="Markdown")

async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles multi-step button selections: Artist ➔ Studio Album ➔ Track."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data
    user_state = USER_STATES.get(user_id)

    if not user_state:
        await query.edit_message_text("❌ *Session expired. Send an artist name to start over.*", parse_mode="Markdown")
        return

    # STEP 1: Artist Selected ➔ Fetch ONLY Studio Albums (Strictly Filtered & Sorted)
    if data.startswith("art_"):
        idx = int(data.split("_")[1])
        selected_artist = user_state["artists"][idx]
        artist_id = selected_artist["id"]
        artist_name = selected_artist["name"]

        await query.edit_message_text(f"💿 Fetching official studio albums for *{artist_name}*...", parse_mode="Markdown")

        try:
            # Request releases matching primary type 'Album' and secondary type 'Studio'
            releases_result = musicbrainzngs.browse_releases(
                artist=artist_id, 
                release_type=["album"], 
                strict=True, 
                limit=100
            )
            releases = releases_result.get("release-list", [])

            if not releases:
                await query.edit_message_text(f"❌ *No albums found for {artist_name}.*", parse_mode="Markdown")
                return

            # Filter out non-studio albums and deduplicate by title
            seen_titles = set()
            studio_albums = []
            
            for rel in releases:
                title = rel.get("title")
                date = rel.get("date", "9999")[:4] # Default high year if missing
                
                # Basic cleanup to avoid duplicate regional variants or live recordings in titles
                lower_title = title.lower()
                if any(bad in lower_title for _ in [""], for bad in ["live", "greatest hits", "best of", "collection", "karaoke", "instrumental"]):
                    continue

                if title and title not in seen_titles:
                    seen_titles.add(title)
                    studio_albums.append({
                        "id": rel.get("id"),
                        "title": title,
                        "year": date
                    })

            # Sort chronologically by release year
            studio_albums = sorted(studio_albums, key=lambda x: x["year"])

            if not studio_albums:
                await query.edit_message_text(f"❌ *No studio albums found for {artist_name}.*", parse_mode="Markdown")
                return

            album_data = []
            keyboard = []

            for idx, alb in enumerate(studio_albums[:15]): # Limit to 15 cleanest albums
                title = alb["title"]
                year = alb["year"]
                
                album_label = f"{title} ({year})"
                if len(album_label) > 60:
                    album_label = album_label[:57] + "..."

                album_data.append({
                    "id": alb["id"],
                    "title": title
                })

                keyboard.append([InlineKeyboardButton(album_label, callback_data=f"alb_{idx}")])

            USER_STATES[user_id] = {
                "step": "album_selected",
                "artist_name": artist_name,
                "albums": album_data
            }

            reply_markup = InlineKeyboardMarkup(keyboard)
            await query.edit_message_text(
                f"💿 *Studio Albums by {artist_name}:*\nSelect an
