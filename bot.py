import os
import io
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
    MessageHandler,
    CallbackQueryHandler,
    ContextTypes,
    filters,
)
from audd import AudD, AudDAPIError, AudDInvalidAudioError
 
# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
AUDD_API_TOKEN = os.environ.get("AUDD_API_TOKEN")
 
if not TELEGRAM_BOT_TOKEN or not AUDD_API_TOKEN:
    raise SystemExit(
        "Missing env vars. Set TELEGRAM_BOT_TOKEN and AUDD_API_TOKEN on Render."
    )
 
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
# Stop httpx from printing request URLs (they contain the bot token)
logging.getLogger("httpx").setLevel(logging.WARNING)
logger = logging.getLogger(__name__)
 
audd_client = AudD(AUDD_API_TOKEN)
 
DEEZER_API = "https://api.deezer.com"
LRCLIB_GET = "https://lrclib.net/api/get"
LRCLIB_SEARCH = "https://lrclib.net/api/search"
 
# -------------------------------------------------------------------
# HEALTH SERVER (Render)
# -------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Lyrics bot alive")
 
    def log_message(self, format, *args):
        pass
 
 
def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()
 
# -------------------------------------------------------------------
# DEEZER (finding the right song)
# -------------------------------------------------------------------
def search_deezer_tracks(query: str, limit: int = 8) -> list[dict]:
    """Search Deezer and return distinct (title, artist) matches."""
    try:
        resp = requests.get(
            f"{DEEZER_API}/search",
            params={"q": query, "limit": 25},
            timeout=10,
        )
        resp.raise_for_status()
        results = []
        seen = set()
        for t in resp.json().get("data", []):
            key = (t["title"].lower(), t["artist"]["name"].lower())
            if key in seen:
                continue
            seen.add(key)
            results.append(
                {
                    "id": t["id"],
                    "title": t["title"],
                    "artist": t["artist"]["name"],
                    "album": t.get("album", {}).get("title", ""),
                    "duration": t.get("duration", 0),
                }
            )
            if len(results) >= limit:
                break
        return results
    except Exception as e:
        logger.warning(f"Deezer search failed: {e}")
        return []
 
 
def get_deezer_track(track_id: int) -> dict | None:
    try:
        resp = requests.get(f"{DEEZER_API}/track/{track_id}", timeout=10)
        resp.raise_for_status()
        t = resp.json()
        if "error" in t:
            return None
        return {
            "id": t["id"],
            "title": t["title"],
            "artist": t["artist"]["name"],
            "album": t.get("album", {}).get("title", ""),
            "duration": t.get("duration", 0),
        }
    except Exception as e:
        logger.warning(f"Deezer track fetch failed: {e}")
        return None
 
# -------------------------------------------------------------------
# LYRICS (LRCLIB)
# -------------------------------------------------------------------
def clean_title(title: str) -> str:
    """'Song (feat. X) - Remastered 2011' -> 'Song'"""
    t = re.sub(r"\s*[\(\[].*?[\)\]]", "", title)
    t = re.sub(r"\s+-\s+.*$", "", t)
    return t.strip() or title
 
 
def lrclib_get(artist, title, album="", duration=0) -> str | None:
    params = {"track_name": title, "artist_name": artist}
    if album:
        params["album_name"] = album
    if duration:
        params["duration"] = duration
    try:
        r = requests.get(LRCLIB_GET, params=params, timeout=10)
        if r.status_code == 200:
            return r.json().get("plainLyrics")
    except Exception as e:
        logger.warning(f"LRCLIB get failed: {e}")
    return None
 
 
def lrclib_search(artist, title, duration=0) -> str | None:
    try:
        r = requests.get(
            LRCLIB_SEARCH,
            params={"q": f"{artist} {title}"},
            timeout=10,
        )
        if r.status_code != 200:
            return None
        candidates = [
            c for c in r.json()
            if c.get("plainLyrics")
            and artist.lower() in (c.get("artistName") or "").lower()
        ]
        if not candidates:
            return None
        if duration:
            candidates.sort(key=lambda c: abs((c.get("duration") or 0) - duration))
        return candidates[0]["plainLyrics"]
    except Exception as e:
        logger.warning(f"LRCLIB search failed: {e}")
        return None
 
 
def fetch_lyrics(artist: str, title: str, album: str = "", duration: int = 0) -> str:
    """Try exact match first, then looser matches."""
    base = clean_title(title)
    attempts = [
        lambda: lrclib_get(artist, title, album, duration),
        lambda: lrclib_get(artist, title),
        lambda: lrclib_get(artist, base),
        lambda: lrclib_search(artist, base, duration),
    ]
    for attempt in attempts:
        lyrics = attempt()
        if lyrics:
            return lyrics
    return "❌ Lyrics not found for this one."
 
# -------------------------------------------------------------------
# SENDING
# -------------------------------------------------------------------
async def send_lyrics_chunked(chat_id: int, context, header: str, lyrics: str):
    """Split long lyrics into <4096 char messages."""
    MAX = 4000
    full = header + "\n\n" + lyrics
    if len(full) <= MAX:
        await context.bot.send_message(chat_id=chat_id, text=full)
        return
 
    await context.bot.send_message(chat_id=chat_id, text=header)
 
    current = ""
    for line in lyrics.split("\n"):
        line = line[:MAX]
        if len(current) + len(line) + 1 > MAX:
            await context.bot.send_message(chat_id=chat_id, text=current)
            current = line + "\n"
        else:
            current += line + "\n"
    if current.strip():
        await context.bot.send_message(chat_id=chat_id, text=current)
 
 
def make_header(title, artist, album):
    header = f"🎤 {title}\n👤 {artist}"
    if album:
        header += f"\n💿 {album}"
    return header
 
# -------------------------------------------------------------------
# HANDLERS
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎤 Lyrics Bot\n\n"
        "• Type a song name (add the artist for best results, "
        "like \"humble kendrick\") and pick the right version.\n"
        "• Or send a voice note / audio of a song playing and I'll identify it "
        "and send the lyrics."
    )
 
 
async def handle_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = (update.message.text or "").strip()
    if not query:
        return
 
    status = await update.message.reply_text("🔎 Searching...")
    tracks = await asyncio.to_thread(search_deezer_tracks, query)
 
    if not tracks:
        await status.edit_text("❌ No songs found. Try adding the artist's name.")
        return
 
    buttons = []
    for t in tracks:
        label = f"{t['title']} — {t['artist']}"
        if t["album"]:
            label += f" ({t['album']})"
        buttons.append(
            [InlineKeyboardButton(label[:60], callback_data=f"t:{t['id']}")]
        )
 
    await status.edit_text(
        "Which one do you mean? 👇",
        reply_markup=InlineKeyboardMarkup(buttons),
    )
 
 
async def handle_pick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    await q.answer()
 
    try:
        track_id = int(q.data.split(":", 1)[1])
    except (ValueError, IndexError):
        return
 
    await q.edit_message_text("📜 Getting lyrics...")
 
    track = await asyncio.to_thread(get_deezer_track, track_id)
    if not track:
        await q.edit_message_text("❌ Couldn't load that song. Try searching again.")
        return
 
    lyrics = await asyncio.to_thread(
        fetch_lyrics,
        track["artist"],
        track["title"],
        track["album"],
        track["duration"],
    )
 
    header = make_header(track["title"], track["artist"], track["album"])
    await send_lyrics_chunked(q.message.chat_id, context, header, lyrics)
    await q.delete_message()
 
 
async def fetch_telegram_file(context, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()
 
 
def recognize_audio(audio_bytes: bytes) -> dict | None:
    result = audd_client.recognize(audio_bytes)
    if result is None:
        return None
    return {
        "artist": result.artist or "",
        "title": result.title or "",
        "album": result.album or "",
    }
 
 
async def handle_voice_or_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return
 
    file_id = None
    if msg.voice:
        file_id = msg.voice.file_id
    elif msg.audio:
        file_id = msg.audio.file_id
    elif msg.document and msg.document.mime_type and msg.document.mime_type.startswith("audio/"):
        file_id = msg.document.file_id
 
    if not file_id:
        return
 
    status = await msg.reply_text("🎧 Listening...")
 
    try:
        audio_bytes = await fetch_telegram_file(context, file_id)
        song = await asyncio.to_thread(recognize_audio, audio_bytes)
 
        if not song:
            await status.edit_text(
                "❌ I couldn't recognize that. Try again with clearer audio or a longer clip."
            )
            return
 
        artist, title, album = song["artist"], song["title"], song["album"]
 
        lyrics = await asyncio.to_thread(fetch_lyrics, artist, title, album)
        await send_lyrics_chunked(msg.chat_id, context, make_header(title, artist, album), lyrics)
        await status.delete()
 
    except AudDInvalidAudioError:
        await status.edit_text("❌ AudD rejected the audio format. Try a different file.")
    except AudDAPIError as e:
        logger.error(f"AudD API error: {e}")
        await status.edit_text(f"❌ Music recognition service error: {str(e)[:150]}")
    except Exception as e:
        logger.exception("Unexpected error in voice handler")
        await status.edit_text(f"❌ Something went wrong: {str(e)[:200]}")
 
# -------------------------------------------------------------------
# MAIN (Python 3.14 event-loop fix)
# -------------------------------------------------------------------
def main():
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())
 
    threading.Thread(target=run_health_server, daemon=True).start()
 
    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()
 
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CallbackQueryHandler(handle_pick, pattern=r"^t:\d+$"))
    app.add_handler(
        MessageHandler(
            filters.VOICE | filters.AUDIO | filters.Document.AUDIO,
            handle_voice_or_audio,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_text))
 
    print("🎤 Lyrics bot is running...")
    app.run_polling()
 
 
if __name__ == "__main__":
    main()
