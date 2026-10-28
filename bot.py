import os
import io
import asyncio
import threading
import logging
import requests
from http.server import HTTPServer, BaseHTTPRequestHandler
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup, InputMediaPhoto
from telegram.ext import (
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from PIL import Image, ImageDraw, ImageFont

# -------------------------------------------------------------------
# CONFIG
# -------------------------------------------------------------------
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit(
        "TELEGRAM_BOT_TOKEN env var is not set. "
        "Add it in Render → Environment."
    )

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

# In-memory cache: message_id -> {image_bytes, caption, user_id}
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
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    "/usr/share/fonts/truetype/impact/impact.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
    "C:/Windows/Fonts/impact.ttf",
]


def load_font(size: int):
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


def draw_styled_text(draw, lines, font, line_height, center_x, start_y, style="classic"):
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
            draw.text((x + shadow_off, y + shadow_off), line, font=font, fill="black")
            draw.text((x, y), line, font=font, fill="white")
        elif style == "clean":
            draw.text((x, y), line, font=font, fill="black")
        else:
            draw.text((x, y), line, font=font, fill="white",
                      stroke_width=2, stroke_fill="black")
        y += line_height


def make_meme(image_bytes: bytes, caption: str, style: str = "classic") -> io.BytesIO:
    img = Image.open(io.BytesIO(image_bytes)).convert("RGB")

    max_dim = 1200
    if max(img.size) > max_dim:
        ratio = max_dim / max(img.size)
        img = img.resize(
            (int(img.width * ratio), int(img.height * ratio)), Image.LANCZOS
        )

    if style == "whiteborder":
        border = max(10, img.width // 40)
        canvas = Image.new(
            "RGB", (img.width + border * 2, img.height + border * 2), "white"
        )
        canvas.paste(img, (border, border))
        img = canvas

    draw = ImageDraw.Draw(img)
    W, H = img.size

    if "|" in caption:
        top_text, bottom_text = [p.strip() for p in caption.split("|", 1)]
    else:
        top_text, bottom_text = caption.strip(), ""

    margin_x = max(20, W // 30)
    max_text_width = W - margin_x * 2
    max_text_height = H // 2 - 20

    if top_text:
        font, lines, lh = fit_text(
            draw, top_text.upper(), max_text_width, max_text_height,
            start_size=max(40, W // 12),
        )
        draw_styled_text(draw, lines, font, lh, W / 2, 20, style=style)

    if bottom_text:
        font, lines, lh = fit_text(
            draw, bottom_text.upper(), max_text_width, max_text_height,
            start_size=max(40, W // 12),
        )
        total_h = lh * len(lines)
        draw_styled_text(draw, lines, font, lh, W / 2, H - total_h - 20, style=style)

    out = io.BytesIO()
    img.save(out, format="PNG", optimize=True)
    out.seek(0)
    out.name = "meme.png"
    return out

# -------------------------------------------------------------------
# TELEGRAM FILE FETCH
# -------------------------------------------------------------------
async def fetch_telegram_file(context, file_id: str) -> bytes:
    tg_file = await context.bot.get_file(file_id)
    buf = io.BytesIO()
    await tg_file.download_to_memory(buf)
    return buf.getvalue()

# -------------------------------------------------------------------
# HANDLERS
# -------------------------------------------------------------------
STYLE_LABELS = {
    "classic": "🖤 Classic",
    "outline": "⚪ Outline",
    "shadow": "🌑 Shadow",
    "clean": "📄 Clean",
    "whiteborder": "🖼️ Border",
}


def build_style_keyboard(cache_key: str):
    rows, row = [], []
    for style, label in STYLE_LABELS.items():
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
        "1. Send a photo *with a caption*.\n"
        "2. Use `|` to split top/bottom: `when the code | finally works`\n"
        "3. Tap a style button to re-render.\n\n"
        "*Styles:*\n"
        "• Classic — white text, black stroke\n"
        "• Outline — thicker stroke\n"
        "• Shadow — drop shadow\n"
        "• Clean — black text\n"
        "• Border — white frame\n\n"
        "*Tips:*\n"
        "• Long captions auto-shrink and wrap.\n"
        "• You can also reply to any image with text."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


def _cache(key: str, image_bytes: bytes, caption: str, user_id: int):
    MEME_CACHE[key] = {
        "image_bytes": image_bytes,
        "caption": caption,
        "user_id": user_id,
    }
    while len(MEME_CACHE) > MAX_CACHE:
        MEME_CACHE.pop(next(iter(MEME_CACHE)))


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
        file_id = msg.photo[-1].file_id
        image_bytes = await fetch_telegram_file(context, file_id)
        meme_buf = await asyncio.to_thread(make_meme, image_bytes, caption, "classic")

        sent = await msg.reply_photo(
            photo=meme_buf, caption="✅ Here's your meme — pick a style to re-render:"
        )

        cache_key = str(sent.message_id)
        _cache(cache_key, image_bytes, caption, msg.from_user.id)

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
        _cache(cache_key, image_bytes, caption, msg.from_user.id)

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
            media=InputMediaPhoto(
                media=meme_buf,
                caption=f"✅ Style: {STYLE_LABELS.get(style, style)}\nPick another:",
            ),
            reply_markup=build_style_keyboard(cache_key),
        )
    except Exception as e:
        logger.exception("Style re-render failed")
        await query.edit_message_caption(
            caption=f"❌ Style render failed: {str(e)[:150]}"
        )


async def unknown(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.message.reply_text(
        "🤔 I only understand photos with captions and /start, /help."
    )

# -------------------------------------------------------------------
# MAIN (Python 3.14 event-loop fix)
# -------------------------------------------------------------------
def main():
    # Python 3.14 removed the implicit event loop creation in
    # asyncio.get_event_loop(). python-telegram-bot calls that
    # internally, so we must create a loop in the main thread first.
    try:
        asyncio.get_event_loop()
    except RuntimeError:
        asyncio.set_event_loop(asyncio.new_event_loop())

    threading.Thread(target=run_health_server, daemon=True).start()

    app = ApplicationBuilder().token(TELEGRAM_BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CallbackQueryHandler(handle_style_callback, pattern=r"^style\|"))
    app.add_handler(MessageHandler(filters.PHOTO, handle_photo))
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
