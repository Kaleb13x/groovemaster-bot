import os
import asyncio
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
# Set this as an environment variable on Render (never hardcode it)
TELEGRAM_BOT_TOKEN = os.environ["8724485150:AAEbDfndk1cr1EbqikY_XNxSsJq4vJK7G6k"]

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
