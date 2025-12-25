"""
Configuration and Environment Setup
Handles all environment variables, logging, and application constants
"""

import os
import logging
import sys
from dotenv import load_dotenv

# Load environment variables from .env file
load_dotenv()

# =====================================================
# LOGGING SETUP
# =====================================================
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler("app.log"),
        logging.StreamHandler(sys.stdout)
    ]
)
logger = logging.getLogger(__name__)

# =====================================================
# ENVIRONMENT VARIABLES
# =====================================================
try:
    OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY")
    TWILIO_ACCOUNT_SID = os.environ.get("TWILIO_ACCOUNT_SID")
    TWILIO_AUTH_TOKEN = os.environ.get("TWILIO_AUTH_TOKEN")
    TWILIO_PHONE_NUMBER = os.environ.get("TWILIO_PHONE_NUMBER")
    ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
    DATABASE_URL = os.environ.get("DATABASE_URL")

    if not all([OPENAI_API_KEY, TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, DATABASE_URL]):
        logger.error("Missing required environment variables!")
        raise ValueError("Missing environment variables")
    
    logger.info("✅ Environment variables loaded")
    logger.info(f"🌍 Environment: {ENVIRONMENT}")
    
except Exception as e:
    logger.error(f"❌ Failed to load environment variables: {e}")
    raise

# =====================================================
# APPLICATION CONSTANTS
# =====================================================
LOG_FILE_PATH = 'app.log'

# OpenAI Configuration
OPENAI_MODEL = "gpt-4o-mini"
OPENAI_TEMPERATURE = 0.3
OPENAI_MAX_TOKENS = 300

# Reminder Configuration
REMINDER_CHECK_INTERVAL = 60  # seconds

# Memory Configuration
MAX_MEMORIES_TO_DISPLAY = 20
MAX_MEMORIES_IN_CONTEXT = 10

# Reminder Formatting
MAX_COMPLETED_REMINDERS_DISPLAY = 5

# List Configuration
MAX_LISTS_PER_USER = 5
MAX_ITEMS_PER_LIST = 15

# Admin Authentication
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Rate Limiting
RATE_LIMIT_MESSAGES = 15  # Max messages per window
RATE_LIMIT_WINDOW = 60    # Window in seconds (1 minute)

# Input Validation Limits
MAX_LIST_NAME_LENGTH = 50
MAX_ITEM_TEXT_LENGTH = 200
MAX_MESSAGE_LENGTH = 500

# Request Timeout Configuration (in seconds)
OPENAI_TIMEOUT = 30  # OpenAI API call timeout
REQUEST_TIMEOUT = 60  # Overall request timeout

# Encryption Configuration
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
HASH_KEY = os.environ.get("HASH_KEY")
ENCRYPTION_ENABLED = bool(ENCRYPTION_KEY and HASH_KEY)

if ENCRYPTION_ENABLED:
    logger.info("Field-level encryption enabled")
else:
    logger.warning("ENCRYPTION_KEY or HASH_KEY not set - field encryption disabled")
