import os
import io
import re
import asyncio
import threading
import logging
import textwrap
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
from PIL import Image, ImageDraw, ImageFont, ImageFilter

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("TELEGRAM_BOT_TOKEN env var is not set.")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory cache: message_id -> {image_bytes, caption, user_id}
# Kept small on purpose; this is a demo bot, not a persistent store.
MEME_CACHE = {}
MAX_CACHE = 200

# -------------------------------------------------------------------
# HEALTH SERVER (Render Web Service)
# -------------------------------------------------------------------
class HealthCheckHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"Meme bot is alive!")

    def log_message(self, format, *args):
        pass


def run_health_server():
    port = int(os.environ.get("PORT", 10000))
    HTTPServer(("0.0.0.0", port), HealthCheckHandler).serve_forever()

# -------------------------------------------------------------------
# FONT LOADING (with fallbacks)
# -------------------------------------------------------------------
FONT_CANDIDATES = [
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    "/usr/share/fonts/truetype/impact/impact.ttf",
    "C:/Windows/Fonts/impact.ttf",  # local testing
]


def load_font(size: int) -> ImageFont.FreeTypeFont:
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    logger.warning("No system font found; falling back to Pillow default.")
    return ImageFont.load_default()

# -------------------------------------------------------------------
# TEXT RENDERING
# -------------------------------------------------------------------
def wrap_text(draw, text, font, max_width):
    """Greedy word-wrap using actual rendered width."""
    words = text.split()
    lines, current = [], ""
    for w in words:
        trial = (current + " " + w).strip()
        if draw.textlength(trial, font=font) <= max_width:
            current = trial
        else:
            if current:
                lines.append(current)
            current = w
    if current:
        lines.append(current)
    return lines


def fit_text(draw, text, max_width, max_height, start_size=120, min_size=20):
    """Shrink font until wrapped text fits in max_width x max_height."""
    size = start_size
    while size >= min_size:
        font = load_font(size)
        lines = wrap_text(draw, text, font, max_width)
        line_height = size + 8
        total_height = line_height * len(lines)
        if total_height <= max_height and all(
            draw.textlength(l, font=font) <= max_width for l in lines
        ):
            return font, lines, line_height
        size -= 4
    font = load_font(min_size)
    return font, wrap_text(draw, text, font, max_width), min_size + 8


def draw_styled_text(draw, lines, font, line_height, center_x, start_y,
                     style="classic", image_size=None):
    """Draw text with stroke/shadow/outline depending on style."""
    y = start_y
    for line in lines:
        w = draw.textlength(line, font=font)
        x = center_x - w / 2
        if style == "classic":
            draw.text((x, y), line, font=font, fill="white",
                      stroke_width=max(2, font.size // 12), stroke_fill="black")
        elif style == "outline":
            draw.text((x, y), line, font=font, fill="white",
                      stroke_width=max(2, font.size // 10), stroke_fill="black")
        elif style == "shadow":
            shadow_off = max(2, font.size // 20)
            draw.text((x + shadow_off, y + shadow_off), line,
                      font=font, fill="black")
            draw.text((x, y), line, font=font, fill="white")
        elif style == "clean":
            draw.text((x, y), line, font=font, fill="black")
        else:
            draw.text((x, y), line, font=font, fill="white",
                      stroke_width=2, stroke_fill="black")
        y += line_height


def make_meme(image_bytes: bytes, caption: str, style: str = "classic") -> io.BytesIO:
    """Render a meme and return a BytesIO PNG."""
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    # Cap size for performance (Telegram photos don't need 8K)
    max_dim = 1200
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        img = img.resize(
            (int(img.width * ratio), int(img.height * ratio)),
            Image.LANCZOS,
        )

    if style == "whiteborder":
        # Add white border around the image first
        border = max(10, img.width // 40)
        canvas = Image.new("RGB",
                           (img.width + border * 2, img.height + border * 2),
                           "white")
        canvas.paste(img, (border, border))
        img = canvas

    draw = ImageDraw.Draw(img)
    W, H = img.size

    # Split top/bottom on "|"
    if "|" in caption:
        top_text, bottom_text = [p.strip() for p in caption.split("|", 1)]
    else:
        top_text, bottom_text = caption.strip(), ""

    margin_x = max(20, W // 30)
    max_text_width = W - margin_x * 2
    max_text_height = H // 2 - 20

    if top_text:
        font, lines, lh = fit_text(draw, top_text.upper(), max_text_width,
                                   max_text_height, start_size=max(40, W // 12))
        draw_styled_text(draw, lines, font, lh, W / 2, 20, style=style)

    if bottom_text:
        font, lines, lh = fit_text(draw, bottom_text.upper(), max_text_width,
                                   max_text_height, start_size=max(40, W // 12))
        total_h = lh * len(lines)
        start_y = H - total_h - 20
        draw_styled_text(draw, lines, font, lh, W / 2, start_y, style=style)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    out.name = "meme.png"
    return out

# -------------------------------------------------------------------
# IMAGE FETCHING
# -------------------------------------------------------------------
def download_image(file_id: str) -> bytes:
    """Download a Telegram file by file_id using Bot API file URL.
    NOTE: we need the token; passed via closure in the handler instead."""
    raise NotImplementedError  # replaced below by handler-level download


async def fetch_telegram_file(context, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()

# -------------------------------------------------------------------
# HANDLERS
# -------------------------------------------------------------------
STYLE_LABELS = {
    "classic": "🖤 Classic Impact",
    "outline": "⚪ Heavy Outline",
    "shadow": "🌑 Drop Shadow",
    "clean": "📄 Clean Black",
    "whiteborder": "🖼️ White Border",
}


def build_style_keyboard(cache_key: str):
    rows = []
    row = []
    for i, (style, label) in enumerate(STYLE_LABELS.items()):
        row.append(InlineKeyboardButton(label, callback_data=f"style|{style}|{cache_key}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🎨 *Meme Generator Bot*\n\n"
        "Send me a *photo* with a caption to make a meme.\n\n"
        "*Caption format:*\n"
        "`top text` → top only\n"
        "`top text | bottom text` → top and bottom\n\n"
        "Then tap a style button to re-render.\n\n"
        "Try `/help` for tips."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "🆘 *How to use*\n\n"
        "1. Send a photo *with a caption* (or reply to an image with text).\n"
        "2. Use `|` to split top/bottom: `when the code | finally works`\n"
        "3. Tap a style button to re-render.\n\n"
        "*Styles:*\n"
        "• Classic Impact — white text, black stroke\n"
        "• Heavy Outline — thick white-on-black\n"
        "• Drop Shadow — subtle shadow\n"
        "• Clean Black — black text, no stroke\n"
        "• White Border — adds a white frame\n\n"
        "*Tips:*\n"
        "• Long captions auto-shrink and wrap.\n"
        "• Works best with landscape/square images."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def handle_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    msg = update.message
    if not msg or not msg.photo:
        return

    caption = (msg.caption or "").strip()
    if not caption:
        await msg.reply_text(
            "✏️ Send the photo *with a caption* so I know what to write.\n"
            "Format: `top text | bottom text`",
            parse_mode="Markdown",
        )
        return

    status = await msg.reply_text("🎨 Rendering your meme...")

    try:
        # Largest photo size is the last item
        file_id = msg.photo[-1].file_id
        image_bytes = await fetch_telegram_file(context, file_id)

        # Render default style
        meme_buf = await asyncio.to_thread(make_meme, image_bytes, caption, "classic")

        sent = await msg.reply_photo(photo=meme_buf, caption="✅ Here's your meme — pick a style to re-render:")

        # Cache for style re-renders
        cache_key = str(sent.message_id)
        MEME_CACHE[cache_key] = {
            "image_bytes": image_bytes,
            "caption": caption,
            "user_id": msg.from_user.id,
        }
        # Trim cache
        while len(MEME_CACHE) > MAX_CACHE:
            MEME_CACHE.pop(next(iter(MEME_CACHE)))

        await context.bot.edit_message_reply_markup(
            chat_id=sent.chat_id,
            message_id=sent.message_id,
            reply_markup=build_style_keyboard(cache_key),
        )
        await status.delete()
    except Exception as e:
        logger.exception("Meme render failed")
        await status.edit_text(f"❌ Couldn't render that meme: {str(e)[:200]}")


async def handle_text_reply_to_photo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Allow replying to a photo with text to make a meme."""
    msg = update.message
    if not msg or not msg.reply_to_message:
        return
    replied = msg.reply_to_message
    if not replied.photo:
        return

    caption = (msg.text or "").strip()
    if not caption:
        return

    status = await msg.reply_text("🎨 Rendering your meme...")
    try:
        file_id = replied.photo[-1].file_id
        image_bytes = await fetch_telegram_file(context, file_id)
        meme_buf = await asyncio.to_thread(make_meme, image_bytes, caption, "classic")
        sent = await msg.reply_photo(photo=meme_buf, caption="✅ Pick a style:")
        cache_key = str(sent.message_id)
        MEME_CACHE[cache_key] = {
            "image_bytes": image_bytes,
            "caption": caption,
            "user_id": msg.from_user.id,
        }
        while len(MEME_CACHE) > MAX_CACHE:
            MEME_CACHE.pop(next(iter(MEME_CACHE)))
        await context.bot.edit_message_reply_markup(
            chat_id=sent.chat_id,
            message_id=sent.message_id,
            reply_markup=build_style_keyboard(cache_key),
        )
        await status.delete()
    except Exception as e:
        logger.exception("Meme render (reply) failed")
        await status.edit_text(f"❌ Couldn't render that meme: {str(e)[:200]}")


async def handle_style_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer("Re-rendering...")

    try:
        _, style, cache_key = query.data.split("|", 2)
    except ValueError:
        await query.edit_message_caption(caption="❌ Invalid style selection.")
        return

    entry = MEME_CACHE.get(cache_key)
    if not entry:
        await query.edit_message_caption(
            caption="⌛ This meme expired. Send the photo again to re-render."
        )
        return

    try:
        meme_buf = await asyncio.to_thread(
            make_meme, entry["image_bytes"], entry["caption"], style
        )
        await query.edit_message_media(
            media=__import__("telegram").InputMediaPhoto(
                media=meme_buf,
                caption=f"✅ Style: {STYLE_LABELS.get(style, style)}\nPick another:",
            ),
            reply_markup=build_style_keyboard(cache_key),
        )
    except Exception as e:
        logger.exception("Style re-render failed")
        await query.edit_message_caption(caption=f"❌ Style render failed: {str(e)[:150]}")


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤔 I only understand photos with captions and /start, /help."
    )

# -------------------------------------------------------------------
# MAIN
# -------------------------------------------------------------------
def main():
    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(handle_style_callback, pattern=r"^style\|"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
    # Reply-to-photo with text (text messages that are replies)
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND & filters.REPLY,
            handle_text_reply_to_photo,
        )
    )
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, unknown))

    print("🎨 Meme bot is running...")
    app.run_polling()


if __name__ == "__main__":
    main()
