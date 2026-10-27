import asyncio
import logging
import sqlite3
import musicbrainzngs
import requests
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    InlineQueryResultArticle,
    InputTextMessageContent,
    Update,
)
from telegram.constants import ChatAction
from telegram.ext import (
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    InlineQueryHandler,
    MessageHandler,
    filters,
)

# Enable console logging
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", level=logging.INFO
)
logger = logging.getLogger(__name__)

# Initialize APIs & Configuration
musicbrainzngs.set_useragent("UltimateMusicBot", "1.0", "support@example.com")
BOT_TOKEN = "8712362642:AAFMst1kuo4TN-GGltqIH0XtFiVj-sRIHp8"
ADMIN_CHAT_ID = 6472491155

# Temporary memory cache for song lookups
SONG_CACHE = {}

def init_db():
    conn = sqlite3.connect("music_bot.db")
    cursor = conn.cursor()
    cursor.execute(
        """CREATE TABLE IF NOT EXISTS favorites 
           (user_id INTEGER, item_title TEXT, item_type TEXT, UNIQUE(user_id, item_title))"""
    )
    conn.commit()
    conn.close()

init_db()

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Greeting command."""
    bot_info = await context.bot.get_me()
    bot_name = bot_info.first_name
    bot_username = f"@{bot_info.username}" if bot_info.username else "@Bot"

    await update.message.reply_text(
        f"Hey there! Welcome to ***{bot_name}*** 🎵\n\n"
        "Just send me any *artist name* and I'll pull up their **albums**, full *tracklists*, **cover art**, and *interactive track buttons*.\n\n"
        "⚡ **Quick Commands & Hacks:**\n"
        "• `/myfavorites` – View your *saved collection*\n"
        f"• `{bot_username} <artist>` – Search *inline* across any Telegram chat\n\n"
        "👉 Drop an *artist name* below to get started!",
        parse_mode="Markdown"
    )

async def my_favorites(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Displays user's saved favorites."""
    user_id = update.effective_user.id
    conn = sqlite3.connect("music_bot.db")
    cursor = conn.cursor()
    cursor.execute("SELECT item_title, item_type FROM favorites WHERE user_id = ?", (user_id,))
    rows = cursor.fetchall()
    conn.close()

    if not rows:
        await update.message.reply_text("📂 *Your favorites list is currently empty.*", parse_mode="Markdown")
        return

    response = "⭐ **Your Saved Favorites:**\n\n"
    for title, itype in rows:
        icon = "💿" if itype == "Album" else "🎧"
        response += f"{icon} ***{title}*** (*{itype}*)\n"

    await update.message.reply_text(response, parse_mode="Markdown")

async def search_artist(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Searches for artist."""
    artist_name = update.message.text.strip()
    chat_id = update.effective_chat.id

    await context.bot.send_chat_action(chat_id=chat_id, action=ChatAction.TYPING)

    try:
        mb_res = musicbrainzngs.search_artists(artist=artist_name, limit=1)
        artist_list = mb_res.get("artist-list", [])

        if not artist_list:
            await update.message.reply_text(f"❌ Artist *'{artist_name}'* not found.", parse_mode="Markdown")
            return

        artist = artist_list[0]
        artist_id = artist["id"]
        found_name = artist["name"]

        deezer_res = requests.get(f"https://api.deezer.com/search/artist?q={found_name}").json()
        deezer_artist_id = deezer_res["data"][0]["id"] if deezer_res.get("data") else None
        
        keyboard = []
        if deezer_artist_id:
            keyboard.append([InlineKeyboardButton(f"🔥 Top Tracks by {found_name}", callback_data=f"top_{deezer_artist_id}")])

        albums_res = musicbrainzngs.get_artist_by_id(artist_id, includes=["release-groups"])
        release_groups = albums_res["artist"].get("release-group-list", [])
        albums = [rg for rg in release_groups if rg.get("type") == "Album" and rg.get("primary-type") == "Album"]

        for album in albums[:15]:
            title = album["title"]
            album_id = album["id"]
            year = album.get("first-release-date", "")[:4]
            btn_text = f"💿 {title} ({year})" if year else f"💿 {title}"
            keyboard.append([InlineKeyboardButton(btn_text, callback_data=f"album_{album_id}")])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await update.message.reply_text(
            f"🎵 **Results for ***{found_name}***:**\nSelect an *album* or view *top tracks* below:",
            reply_markup=reply_markup,
            parse_mode="Markdown"
        )

    except Exception as e:
        logger.error(f"Search artist error: {e}")
        await update.message.reply_text("⚠️ *An error occurred while fetching artist data.*", parse_mode="Markdown")

async def handle_callbacks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handles inline button interactions for albums, tracks, lyrics, and favorites."""
    query = update.callback_query
    await query.answer()
    data = query.data
    user = query.from_user
    user_id = user.id
    username = user.username or user.first_name

    if data.startswith("album_"):
        album_id = data.replace("album_", "")
        try:
            rg_data = musicbrainzngs.get_release_group_by_id(album_id, includes=["releases", "artist-credits"])
            album_title = rg_data["release-group"]["title"]
            artist_name = rg_data["release-group"]["artist-credit"][0]["artist"]["name"]
            releases = rg_data["release-group"].get("release-list", [])
            
            if not releases:
                await query.message.reply_text("Could not load tracks.")
                return

            release_id = releases[0]["id"]
            track_data = musicbrainzngs.get_release_by_id(release_id, includes=["recordings"])
            tracks = track_data["release"]["medium-list"][0]["track-list"]

            cover_url = f"https://coverartarchive.org/release-group/{album_id}/front-250"
            check_cover = requests.head(cover_url, allow_redirects=True)
            has_cover = check_cover.status_code == 200

            response = f"🎧 ***{album_title}*** by ***{artist_name}***\n\nTap any track below to get its lyrics:"
            
            keyboard = []
            for idx, t in enumerate(tracks[:15]):
                track_title = t['recording']['title']
                cache_key = f"track_{user_id}_{idx}"
                SONG_CACHE[cache_key] = {"artist": artist_name, "title": track_title}
                
                keyboard.append([InlineKeyboardButton(f"📜 {t['position']}. {track_title}", callback_data=cache_key)])

            keyboard.append([InlineKeyboardButton("❤️ Save Album to Favorites", callback_data=f"save_Album_{album_title}")])
            reply_markup = InlineKeyboardMarkup(keyboard)

            if has_cover:
                await query.message.reply_photo(photo=cover_url, caption=response, reply_markup=reply_markup, parse_mode="Markdown")
            else:
                await query.message.reply_text(response, reply_markup=reply_markup, parse_mode="Markdown")

            # Notify Admin
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🔔 **Activity Log**\nUser: `@{username}` (`ID: {user_id}`)\nSelected Album: *{album_title}*",
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"Album callback error: {e}")
            await query.message.reply_text("⚠️ *Failed to load album details.*", parse_mode="Markdown")

    elif data.startswith("top_"):
        deezer_id = data.replace("top_", "")
        res = requests.get(f"https://api.deezer.com/artist/{deezer_id}/top?limit=10").json()
        tracks = res.get("data", [])

        if not tracks:
            await query.message.reply_text("No top tracks found.")
            return

        artist_name = tracks[0]["artist"]["name"] if tracks else "Artist"
        response = f"🔥 **Top Tracks for ***{artist_name}***:**\nTap a song button to load lyrics:"
        
        keyboard = []
        for idx, track in enumerate(tracks, 1):
            title = track["title"]
            cache_key = f"toptrack_{user_id}_{idx}"
            SONG_CACHE[cache_key] = {"artist": artist_name, "title": title}
            
            keyboard.append([InlineKeyboardButton(f"📜 {idx}. {title}", callback_data=cache_key)])

        reply_markup = InlineKeyboardMarkup(keyboard)
        await query.message.reply_text(response, reply_markup=reply_markup, parse_mode="Markdown")

        # Notify Admin
        await context.bot.send_message(
            chat_id=ADMIN_CHAT_ID,
            text=f"🔔 **Activity Log**\nUser: `@{username}` (`ID: {user_id}`)\nRequested Top Tracks for Deezer ID: `{deezer_id}`",
            parse_mode="Markdown"
        )

    elif data in SONG_CACHE:
        song_info = SONG_CACHE[data]
        artist = song_info["artist"]
        title = song_info["title"]
        
        await context.bot.send_chat_action(chat_id=query.message.chat_id, action=ChatAction.TYPING)
        
        try:
            # Fetch lyrics from LRCLIB (Free, no token required)
            res = requests.get("https://lrclib.net/api/get", params={"artist_name": artist, "track_name": title}, timeout=5)
            lyrics = None
            
            if res.status_code == 200:
                data_json = res.json()
                lyrics = data_json.get("plainLyrics")
            
            # Fallback to search if direct get fails
            if not lyrics:
                search_res = requests.get("https://lrclib.net/api/search", params={"q": f"{artist} {title}"}, timeout=5)
                if search_res.status_code == 200:
                    results = search_res.json()
                    if results and isinstance(results, list):
                        lyrics = results[0].get("plainLyrics")

            if not lyrics:
                await query.message.reply_text(f"❌ Could not find lyrics for *{title}* by *{artist}*.", parse_mode="Markdown")
                return

            if len(lyrics) > 3900:
                lyrics = lyrics[:3900] + "\n\n*(...lyrics truncated due to Telegram length limits)*"

            response = f"🎤 ***{title}*** by ***{artist}***\n\n{lyrics}"
            await query.message.reply_text(response, parse_mode="Markdown")

            # Notify Admin
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"🔔 **Lyrics Tapped**\nUser: `@{username}` (`ID: {user_id}`)\nTrack: *{title}* by *{artist}*",
                parse_mode="Markdown"
            )

        except Exception as e:
            logger.error(f"LRCLIB lyrics fetch error: {e}")
            await query.message.reply_text("⚠️ *An error occurred while fetching lyrics.*", parse_mode="Markdown")

    elif data.startswith("save_"):
        parts = data.split("_", 2)
        itype = parts[1]
        title = parts[2]

        conn = sqlite3.connect("music_bot.db")
        cursor = conn.cursor()
        try:
            cursor.execute("INSERT INTO favorites (user_id, item_title, item_type) VALUES (?, ?, ?)", (user_id, title, itype))
            conn.commit()
            await query.message.reply_text(f"✅ Saved ***{title}*** to your favorites!", parse_mode="Markdown")
            
            # Notify Admin
            await context.bot.send_message(
                chat_id=ADMIN_CHAT_ID,
                text=f"❤️ **Favorite Saved**\nUser: `@{username}` (`ID: {user_id}`)\nSaved: *{title}*",
                parse_mode="Markdown"
            )
        except sqlite3.IntegrityError:
            await query.message.reply_text(f"ℹ️ ***{title}*** is already in your favorites.", parse_mode="Markdown")
        finally:
            conn.close()

async def inline_search(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Enables inline querying in any chat."""
    query = update.inline_query.query
    if not query:
        return

    try:
        res = musicbrainzngs.search_artists(artist=query, limit=5)
        artists = res.get("artist-list", [])
        results = []

        for artist in artists:
            name = artist["name"]
            aid = artist["id"]
            country = artist.get("country", "Unknown")
            
            results.append(
                InlineQueryResultArticle(
                    id=aid,
                    title=name,
                    input_message_content=InputTextMessageContent(f"Check out artist: *{name}* (Country: {country})", parse_mode="Markdown"),
                    description=f"Country: {country}"
                )
            )

        await update.inline_query.answer(results, cache_time=1)
    except Exception as e:
        logger.error(f"Inline search error: {e}")

def main():
    app = ApplicationBuilder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("myfavorites", my_favorites))
    app.add_handler(MessageHandler(filters.TEXT & (~filters.COMMAND), search_artist))
    app.add_handler(CallbackQueryHandler(handle_callbacks))
    app.add_handler(InlineQueryHandler(inline_search))

    print("🤖 GrooveMaster Bot is running with LRCLIB lyrics integration...")
    app.run_polling(drop_pending_updates=True)

if __name__ == "__main__":
    main()