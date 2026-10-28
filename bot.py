import os
import io
import asyncio
import threading
import logging
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InputFile
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    MessageHandler,
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
logger = logging.getLogger(__name__)

# AudD client (sync)
audd_client = AudD(AUDD_API_TOKEN)

# Deezer / Lyrics endpoints
DEEZER_API = "https://api.deezer.com"
LRCLIB_API = "https://lrclib.net/api/get"

# -------------------------------------------------------------------
# HEALTH SERVER (Render)
# -------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Voice-ID bot alive")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

# -------------------------------------------------------------------
# HELPERS
# -------------------------------------------------------------------
async def fetch_telegram_file(context, file_id: str) -> bytes:
    """Download any Telegram file (voice/audio) into memory."""
    tg_file = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()


def recognize_audio(audio_bytes: bytes) -> dict | None:
    """Call AudD. Returns song dict or None. Raises on fatal errors."""
    # AudD SDK accepts bytes directly; we use the sync client in a thread.
    # The SDK returns a model with .artist, .title, etc., or None.
    result = audd_client.recognize(audio_bytes)
    if result is None:
        return None
    return {
        "artist": result.artist or "",
        "title": result.title or "",
        "album": result.album or "",
    }


def search_deezer_preview(artist: str, title: str) -> str | None:
    """Find the 30s preview URL on Deezer. Returns None if not found."""
    query = f"{artist} {title}".strip()
    if not query:
        return None
    try:
        resp = requests.get(
            f"{DEEZER_API}/search",
            params={"q": query, "limit": 1},
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()
        tracks = data.get("data", [])
        if not tracks:
            return None
        return tracks[0].get("preview")  # 30s MP3 URL
    except Exception as e:
        logger.warning(f"Deezer preview search failed: {e}")
        return None


def fetch_lyrics(artist: str, title: str) -> str:
    """Fetch plain lyrics from LRCLIB."""
    try:
        resp = requests.get(
            LRCLIB_API,
            params={"track_name": title, "artist_name": artist},
            timeout=10,
        )
        if resp.status_code == 404:
            return "❌ Lyrics not found on LRCLIB."
        resp.raise_for_status()
        data = resp.json()
        return data.get("plainLyrics") or "⚠️ Plain lyrics not available for this track."
    except Exception as e:
        logger.warning(f"LRCLIB failed: {e}")
        return "⚠️ Could not reach the lyrics service right now."


async def send_lyrics_chunked(chat_id: int, context, header: str, lyrics: str):
    """Split long lyrics into <4096 char messages."""
    MAX = 4000
    full = header + "\n\n" + lyrics
    if len(full) <= MAX:
        await context.bot.send_message(chat_id=chat_id, text=full)
        return

    # Send header first if it's long
    if len(header) > 200:
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

# -------------------------------------------------------------------
# HANDLERS
# -------------------------------------------------------------------
async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🎤 *Voice → Song ID Bot*\n\n"
        "Send me a **voice note** or **audio file** of a song playing, "
        "and I'll tell you what it is, send a 30s preview, and fetch the lyrics.\n\n"
        "Just record a few seconds — it works best with clear audio.",
        parse_mode="Markdown",
    )


async def handle_voice_or_audio(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg:
        return

    # Accept voice notes, audio files, or documents that are audio
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
        # 1. Download audio
        audio_bytes = await fetch_telegram_file(context, file_id)

        # 2. Recognize with AudD (run in thread to not block the loop)
        song = await asyncio.to_thread(recognize_audio, audio_bytes)

        if not song:
            await status.edit_text(
                "❌ I couldn't recognize that. Try again with clearer audio or a longer clip."
            )
            return

        artist = song["artist"]
        title = song["title"]
        album = song["album"]

        # 3. Fetch preview + lyrics in parallel
        preview_task = asyncio.to_thread(search_deezer_preview, artist, title)
        lyrics_task = asyncio.to_thread(fetch_lyrics, artist, title)

        preview_url, lyrics = await asyncio.gather(preview_task, lyrics_task)

        # 4. Send preview audio (if available)
        if preview_url:
            try:
                # Download preview bytes to avoid Telegram URL fetch issues
                preview_resp = await asyncio.to_thread(requests.get, preview_url, timeout=15)
                preview_resp.raise_for_status()
                audio_buf = io.BytesIO(preview_resp.content)
                audio_buf.name = f"{title}.mp3"

                await context.bot.send_audio(
                    chat_id=msg.chat_id,
                    audio=audio_buf,
                    title=title,
                    performer=artist,
                    caption=f"🎵 {title} — {artist}",
                )
            except Exception as e:
                logger.warning(f"Preview send failed: {e}")
                await msg.reply_text("⚠️ Couldn't send the preview audio, but here are the lyrics:")

        # 5. Send lyrics
        header = f"🎤 *{title}*\n👤 {artist}\n💿 {album}" if album else f"🎤 *{title}*\n👤 {artist}"
        await send_lyrics_chunked(msg.chat_id, context, header, lyrics)

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
    app.add_handler(MessageHandler(filters.VOICE | filters.AUDIO | filters.Document.AUDIO, handle_voice_or_audio))

    print("🎤 Voice-ID bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
