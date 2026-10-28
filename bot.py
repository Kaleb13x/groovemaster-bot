import os
import re
import asyncio
import threading
import logging
import requests
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
# Set this as an environment variable on Render (never hardcode it)
TELEGRAM_BOT_TOKEN = os.environ["8724485150:AAEbDfndk1cr1EbqikY_XNxSsJq4vJK7G6k"]

DEEZER = "https://api.deezer.com"

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
# DEEZER HELPERS
# -------------------------------------------------------------------
def deezer_get(path: str, params: dict = None):
    res = requests.get(f"{DEEZER}{path}", params=params, timeout=10)
    res.raise_for_status()
    data = res.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    return data


def search_artists(query: str):
    data = deezer_get("/search/artist", {"q": query, "limit": 5})
    return data.get("data", [])


# Titles/suffixes that mark non-studio or duplicate editions
SUFFIX_BAD = re.compile(
    r"\b(live|karaoke|instrumental|demos?|acoustic)\b",
    re.IGNORECASE,
)
# Re-releases of the same album: kept only if no plain version exists
SUFFIX_EDITION = re.compile(
    r"\b(deluxe|remaster(ed)?|expanded|anniversary|bonus|special edition|"
    r"tour edition|edition|version|reissue)\b",
    re.IGNORECASE,
)
TITLE_BAD = re.compile(
    r"\b(greatest hits|best of|the best|karaoke|unplugged|collection|"
    r"anthology|essentials|b-sides|rarities|live at|live in|live from|"
    r"mtv|sessions)\b",
    re.IGNORECASE,
)


def _is_extra(title: str) -> bool:
    if TITLE_BAD.search(title):
        return True
    parts = re.findall(r"[\(\[][^\)\]]*[\)\]]", title)
    if " - " in title:
        parts.append(title.split(" - ", 1)[1])
    return any(SUFFIX_BAD.search(p) for p in parts)


def _is_edition(title: str) -> bool:
    parts = re.findall(r"[\(\[][^\)\]]*[\)\]]", title)
    if " - " in title:
        parts.append(title.split(" - ", 1)[1])
    return any(SUFFIX_EDITION.search(p) for p in parts)


def _clean_title(title: str) -> str:
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", title)
    t = t.split(" - ")[0]
    return re.sub(r"\s+", " ", t).strip() or title


def _base_title(title: str) -> str:
    t = re.sub(r"[\(\[][^\)\]]*[\)\]]", "", title)
    t = t.split(" - ")[0]
    return re.sub(r"\s+", " ", t).strip().lower()


def get_studio_albums(artist_id: int):
    """Albums exactly as Deezer lists them (record_type == 'album').
    No keyword filtering: this matches the Albums tab in the Deezer app."""
    albums, index = [], 0
    while True:
        data = deezer_get(
            f"/artist/{artist_id}/albums", {"limit": 100, "index": index}
        )
        batch = data.get("data", [])
        albums.extend(batch)
        index += len(batch)
        if not batch or not data.get("next") or index >= 500:
            break

    seen, result = set(), []
    for alb in albums:
        if alb.get("record_type") != "album":
            continue
        title = alb.get("title", "")
        key = title.strip().lower()
        if not title or key in seen:
            continue
        seen.add(key)
        date = alb.get("release_date") or "9999-99-99"
        result.append({
            "id": alb["id"],
            "title": title,
            "year": date[:4],
            "date": date,
            "cover": alb.get("cover_xl") or alb.get("cover_big"),
        })

    return sorted(result, key=lambda a: a["date"])


def get_album_tracks(album_id: int):
    data = deezer_get(f"/album/{album_id}/tracks", {"limit": 100})
    tracks = []
    for t in data.get("data", []):
        title = t.get("title_short") or t.get("title")
        if title and title not in tracks:
            tracks.append(title)
    return tracks

# -------------------------------------------------------------------
# LYRICS
# -------------------------------------------------------------------
def fetch_lrclib_lyrics(track_name: str, artist_name: str) -> str:
    """Fetches plain lyrics from LRCLIB API."""
    url = "https://lrclib.net/api/get"
    params = {"track_name": track_name, "artist_name": artist_name}
    try:
        response = requests.get(url, params=params, timeout=10)
        if response.status_code == 200:
            lyrics = response.json().get("plainLyrics")
            return lyrics or "Plain lyrics not available for this track."
        elif response.status_code == 404:
            return "❌ Lyrics not found on LRCLIB."
        return f"⚠️ Server returned status code {response.status_code}."
    except Exception as e:
        logger.error(f"LRCLIB API Error: {e}")
        return "❌ Error connecting to LRCLIB lyrics service."


async def send_lyrics_in_chunks(update: Update, context: ContextTypes.DEFAULT_TYPE, text: str):
    """Splits long text into chunks under Telegram's 4096 character limit (plain text)."""
    MAX_LENGTH = 4000
    chat_id = update.effective_chat.id

    if len(text) <= MAX_LENGTH:
        await context.bot.send_message(chat_id=chat_id, text=text)
        return

    current_chunk = ""
    for line in text.split("\n"):
        line = line[:MAX_LENGTH]
        if len(current_chunk) + len(line) + 1 > MAX_LENGTH:
            await context.bot.send_message(chat_id=chat_id, text=current_chunk)
            current_chunk = line + "\n"
        else:
            current_chunk += line + "\n"

    if current_chunk.strip():
        await context.bot.send_message(chat_id=chat_id, text=current_chunk)

# -------------------------------------------------------------------
# TELEGRAM HANDLERS
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    bot_name = context.bot.first_name or "Music Bot"
    welcome_text = (
        f"🎵 *Welcome to {bot_name}!*\n\n"
        "Send me any *artist name* to browse their studio albums and lyrics!"
    )
    await update.message.reply_text(welcome_text, parse_mode="Markdown")


async def handle_text_input(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches Deezer for artists based on user input."""
    query = update.message.text.strip()
    user_id = update.effective_user.id

    await update.message.reply_text(f"🔍 Searching for artist: {query}...")

    try:
        artists = await asyncio.to_thread(search_artists, query)

        if not artists:
            await update.message.reply_text("❌ No artists found. Try a different name.")
            return

        artist_data, keyboard = [], []
        for idx, artist in enumerate(artists):
            name = artist.get("name", "Unknown Artist")
            fans = artist.get("nb_fan")
            label = f"{name} ({fans:,} fans)" if fans else name
            if len(label) > 60:
                label = label[:57] + "..."

            artist_data.append({"id": artist["id"], "name": name})
            keyboard.append([InlineKeyboardButton(label, callback_data=f"art_{idx}")])

        USER_STATES[user_id] = {"step": "artist_selected", "artists": artist_data}

        await update.message.reply_text(
            "👤 Select the artist:",
            reply_markup=InlineKeyboardMarkup(keyboard),
        )

    except Exception as e:
        logger.error(f"Deezer artist search error: {e}")
        await update.message.reply_text(f"❌ Search error: {str(e)[:200]}")


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles multi-step button selections: Artist ➔ Studio Album ➔ Track."""
    query = update.callback_query
    await query.answer()

    user_id = update.effective_user.id
    data = query.data
    user_state = USER_STATES.get(user_id)
    chat_id = update.effective_chat.id

    if not user_state:
        await query.edit_message_text("❌ Session expired. Send an artist name to start over.")
        return

    # STEP 1: Artist ➔ studio albums
    if data.startswith("art_"):
        idx = int(data.split("_")[1])
        artists = user_state.get("artists", [])
        if idx >= len(artists):
            await query.edit_message_text("❌ Selection expired. Send an artist name to search again.")
            return

        artist = artists[idx]
        await query.edit_message_text(f"💿 Fetching studio albums for {artist['name']}...")

        try:
            albums = await asyncio.to_thread(get_studio_albums, artist["id"])

            if not albums:
                await query.edit_message_text(f"❌ No studio albums found for {artist['name']}.")
                return

            albums = albums[:50]
            keyboard = []
            for i, alb in enumerate(albums):
                label = f"{alb['title']} ({alb['year']})"
                if len(label) > 60:
                    label = label[:57] + "..."
                keyboard.append([InlineKeyboardButton(label, callback_data=f"alb_{i}")])

            USER_STATES[user_id] = {
                "step": "album_selected",
                "artist_name": artist["name"],
                "albums": albums,
            }

            await query.edit_message_text(
                f"💿 Studio Albums by {artist['name']}:\nSelect an album below:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

        except Exception as e:
            logger.error(f"Deezer albums error: {e}")
            await query.edit_message_text(f"❌ Error loading albums: {str(e)[:200]}")

    # STEP 2: Album ➔ cover + tracklist
    elif data.startswith("alb_"):
        idx = int(data.split("_")[1])
        albums = user_state.get("albums", [])
        if idx >= len(albums):
            await query.edit_message_text("❌ Selection expired. Send an artist name to search again.")
            return

        album = albums[idx]
        artist_name = user_state["artist_name"]

        await query.edit_message_text(f"🎶 Loading tracklist for {album['title']}...")

        try:
            tracks = await asyncio.to_thread(get_album_tracks, album["id"])

            if not tracks:
                await query.edit_message_text("❌ No tracks found for this album.")
                return

            keyboard = []
            for i, track_title in enumerate(tracks[:50]):
                btn_text = f"{i + 1}. {track_title}"
                if len(btn_text) > 60:
                    btn_text = btn_text[:57] + "..."
                keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"trk_{i}")])

            USER_STATES[user_id] = {
                "step": "track_selected",
                "artist_name": artist_name,
                "album_title": album["title"],
                "tracks": tracks,
            }

            reply_markup = InlineKeyboardMarkup(keyboard)
            caption = (
                f"💿 {album['title']} ({album['year']})\n"
                f"👤 Artist: {artist_name}\n\nSelect a track to get lyrics:"
            )

            sent_photo = False
            if album.get("cover"):
                try:
                    await context.bot.send_photo(
                        chat_id=chat_id,
                        photo=album["cover"],
                        caption=caption,
                        reply_markup=reply_markup,
                    )
                    await query.delete_message()
                    sent_photo = True
                except Exception as e:
                    logger.error(f"Sending cover photo failed: {e}")

            if not sent_photo:
                await query.edit_message_text(caption, reply_markup=reply_markup)

        except Exception as e:
            logger.error(f"Deezer tracks error: {e}")
            await query.edit_message_text("❌ Error loading tracklist. Please try again.")

    # STEP 3: Track ➔ lyrics
    elif data.startswith("trk_"):
        idx = int(data.split("_")[1])
        tracks = user_state.get("tracks", [])

        if idx >= len(tracks):
            await context.bot.send_message(
                chat_id=chat_id,
                text="❌ Selection expired. Send an artist name to search again.",
            )
            return

        track_title = tracks[idx]
        artist_name = user_state["artist_name"]
        album_title = user_state["album_title"]

        status_msg = await context.bot.send_message(
            chat_id=chat_id, text=f"📥 Fetching lyrics for {track_title}..."
        )

        lyrics = await asyncio.to_thread(
            fetch_lrclib_lyrics, track_name=track_title, artist_name=artist_name
        )

        header = (
            f"🎤 {track_title}\n"
            f"👤 Artist: {artist_name}\n"
            f"💿 Album: {album_title}\n"
            f"────────────────────────\n\n"
        )

        await context.bot.delete_message(chat_id=chat_id, message_id=status_msg.message_id)
        await send_lyrics_in_chunks(update, context, header + lyrics)

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
