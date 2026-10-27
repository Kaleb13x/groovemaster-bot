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
