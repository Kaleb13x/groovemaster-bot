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
    """Clean /start command without intro filler."""
    welcome_text = "Send me any **artist name** to browse their albums, cover art, and lyrics!"
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
    """Handles multi-step button selections: Artist ➔ Album (with Cover Art) ➔ Track."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data
    user_state = USER_STATES.get(user_id)

    if not user_state:
        await query.edit_message_text("❌ *Session expired. Send an artist name to start over.*", parse_mode="Markdown")
        return

    # STEP 1: Artist Selected ➔ Fetch Albums
    if data.startswith("art_"):
        idx = int(data.split("_")[1])
        selected_artist = user_state["artists"][idx]
        artist_id = selected_artist["id"]
        artist_name = selected_artist["name"]

        await query.edit_message_text(f"💿 Fetching albums for *{artist_name}*...", parse_mode="Markdown")

        try:
            releases_result = musicbrainzngs.browse_releases(artist=artist_id, release_type=["album"], limit=15)
            releases = releases_result.get("release-list", [])

            if not releases:
                await query.edit_message_text(f"❌ *No albums found for {artist_name}.*", parse_mode="Markdown")
                return

            album_data = []
            keyboard = []

            for idx, rel in enumerate(releases):
                title = rel.get("title", "Unknown Album")
                date = rel.get("date", "Unknown Year")[:4]
                
                album_label = f"{title} ({date})"
                if len(album_label) > 60:
                    album_label = album_label[:57] + "..."

                album_data.append({
                    "id": rel.get("id"),
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
                f"💿 *Albums by {artist_name}:*\nSelect an album below:",
                reply_markup=reply_markup,
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"MusicBrainz browse releases error: {e}")
            await query.edit_message_text("❌ *Error loading albums. Please try searching again.*", parse_mode="Markdown")

    # STEP 2: Album Selected ➔ Fetch Cover Art + Tracklist
    elif data.startswith("alb_"):
        idx = int(data.split("_")[1])
        selected_album = user_state["albums"][idx]
        album_id = selected_album["id"]
        album_title = selected_album["title"]
        artist_name = user_state["artist_name"]

        await query.edit_message_text(f"🎶 Loading tracklist & cover art for *{album_title}*...", parse_mode="Markdown")

        try:
            release_details = musicbrainzngs.get_release_by_id(album_id, includes=["recordings"])
            release_info = release_details.get("release", {})
            
            tracks = []
            for medium in release_info.get("medium-list", []):
                for track in medium.get("track-list", []):
                    rec = track.get("recording", {})
                    title = rec.get("title")
                    if title and title not in tracks:
                        tracks.append(title)

            if not tracks:
                await query.edit_message_text(f"❌ *No tracks found for this album.*", parse_mode="Markdown")
                return

            keyboard = []
            for idx, track_title in enumerate(tracks[:20]):
                btn_text = f"{idx + 1}. {track_title}"
                if len(btn_text) > 60:
                    btn_text = btn_text[:57] + "..."
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"trk_{idx}")])

            USER_STATES[user_id] = {
                "step": "track_selected",
                "artist_name": artist_name,
                "album_title": album_title,
                "tracks": tracks
            }

            reply_markup = InlineKeyboardMarkup(keyboard)
            caption_text = f"💿 *{album_title}*\n👤 Artist: *{artist_name}*\n\nSelect a track to get lyrics:"
            
            cover_url = get_album_cover_url(album_id)

            if cover_url:
                await query.delete_message()
                await context.bot.send_photo(
                    chat_id=update.effective_chat.id,
                    photo=cover_url,
                    caption=caption_text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )
            else:
                await query.edit_message_text(
                    caption_text,
                    reply_markup=reply_markup,
                    parse_mode="Markdown"
                )

        except Exception as e:
            logger.error(f"MusicBrainz get release error: {e}")
            await query.edit_message_text("❌ *Error loading tracklist. Please try again.*", parse_mode="Markdown")

    # STEP 3: Track Selected ➔ Fetch Lyrics
    elif data.startswith("trk_"):
        idx = int(data.split("_")[1])
        tracks = user_state.get("tracks", [])
        
        if idx >= len(tracks):
            await query.edit_message_text("❌ *Selection expired. Send an artist name to search again.*", parse_mode="Markdown")
            return

        track_title = tracks[idx]
        artist_name = user_state["artist_name"]
        album_title = user_state["album_title"]

        status_msg = await context.bot.send_message(
            chat_id=update.effective_chat.id,
            text=f"📥 Fetching lyrics for *{track_title}*...",
            parse_mode="Markdown"
        )

        lyrics = fetch_lrclib_lyrics(track_name=track_title, artist_name=artist_name)
        
        header = (
            f"🎤 *{track_title}*\n"
            f"👤 Artist: *{artist_name}*\n"
            f"💿 Album: *{album_title}*\n"
            f"────────────────────────\n\n"
        )
        full_text = header + lyrics

        await context.bot.delete_message(chat_id=update.effective_chat.id, message_id=status_msg.message_id)
        await send_lyrics_in_chunks(update, context, full_text)

# -------------------------------------------------------------------
# MAIN APPLICATION
# -------------------------------------------------------------------
def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(button_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text_input))

    print("🤖 Bot is running...")
    app.run_polling()

if __name__ == "__main__":
    main()
