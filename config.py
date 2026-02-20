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
    # Public phone number for contact card (may differ from sending number)
    PUBLIC_PHONE_NUMBER = os.environ.get("PUBLIC_PHONE_NUMBER", "+18555521950")
    ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
    DATABASE_URL = os.environ.get("DATABASE_URL")
    # Optional: Separate DB for monitoring (allows staging to monitor production)
    MONITORING_DATABASE_URL = os.environ.get("MONITORING_DATABASE_URL", DATABASE_URL)

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
OPENAI_MAX_TOKENS = 800

# Reminder Configuration
REMINDER_CHECK_INTERVAL = 30  # seconds (used by Celery Beat)

# Celery/Redis Configuration (Upstash)
UPSTASH_REDIS_URL = os.environ.get("UPSTASH_REDIS_URL", "redis://localhost:6379/0")

# Memory Configuration
MAX_MEMORIES_TO_DISPLAY = 20
MAX_MEMORIES_IN_CONTEXT = 10

# Reminder Formatting
MAX_COMPLETED_REMINDERS_DISPLAY = 5

# List Configuration
MAX_LISTS_PER_USER = 20
MAX_ITEMS_PER_LIST = 40

# Admin Authentication
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD")

# Customer Service Portal Authentication
# Falls back to admin credentials if not set
CS_USERNAME = os.environ.get("CS_USERNAME", ADMIN_USERNAME)
CS_PASSWORD = os.environ.get("CS_PASSWORD", ADMIN_PASSWORD)

# Rate Limiting
RATE_LIMIT_MESSAGES = 15  # Max messages per window
RATE_LIMIT_WINDOW = 60    # Window in seconds (1 minute)

# Input Validation Limits
MAX_LIST_NAME_LENGTH = 50
MAX_ITEM_TEXT_LENGTH = 200
MAX_MESSAGE_LENGTH = 500

# Request Timeout Configuration (in seconds)
OPENAI_TIMEOUT = 12  # OpenAI API call timeout (must be < Twilio's 15s webhook timeout)
REQUEST_TIMEOUT = 60  # Overall request timeout

# Encryption Configuration
ENCRYPTION_KEY = os.environ.get("ENCRYPTION_KEY")
HASH_KEY = os.environ.get("HASH_KEY")
ENCRYPTION_ENABLED = bool(ENCRYPTION_KEY and HASH_KEY)

if ENCRYPTION_ENABLED:
    logger.info("Field-level encryption enabled")
else:
    logger.warning("ENCRYPTION_KEY or HASH_KEY not set - field encryption disabled")

# SMTP Configuration (SMTP2GO)
SMTP_HOST = os.environ.get("SMTP_HOST", "mail.smtp2go.com")
SMTP_PORT = int(os.environ.get("SMTP_PORT", "2525"))
SMTP_USERNAME = os.environ.get("SMTP_USERNAME")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD")
SMTP_FROM_EMAIL = os.environ.get("SMTP_FROM_EMAIL", "support@remyndrs.com")
SUPPORT_EMAIL = os.environ.get("SUPPORT_EMAIL")  # Where support tickets are sent

SMTP_ENABLED = bool(SMTP_USERNAME and SMTP_PASSWORD and SUPPORT_EMAIL)
if SMTP_ENABLED:
    logger.info(f"SMTP enabled - support emails will be sent to {SUPPORT_EMAIL}")
else:
    logger.warning("SMTP not fully configured - support email notifications disabled")

# Beta Mode - allows all users to access premium features like support
BETA_MODE = os.environ.get("BETA_MODE", "false").lower() == "true"
if BETA_MODE:
    logger.info("Beta mode enabled - all users can access support")

# =====================================================
# STRIPE CONFIGURATION
# =====================================================
STRIPE_SECRET_KEY = os.environ.get("STRIPE_SECRET_KEY")
STRIPE_PUBLISHABLE_KEY = os.environ.get("STRIPE_PUBLISHABLE_KEY")
STRIPE_WEBHOOK_SECRET = os.environ.get("STRIPE_WEBHOOK_SECRET")

# Stripe Price IDs (create these in Stripe Dashboard)
STRIPE_PRICE_IDS = {
    'premium_monthly': os.environ.get("STRIPE_PRICE_PREMIUM_MONTHLY"),
    'premium_annual': os.environ.get("STRIPE_PRICE_PREMIUM_ANNUAL"),
    'family_monthly': os.environ.get("STRIPE_PRICE_FAMILY_MONTHLY"),
    'family_annual': os.environ.get("STRIPE_PRICE_FAMILY_ANNUAL"),
}

# Base URL for redirects (set in environment)
APP_BASE_URL = os.environ.get("APP_BASE_URL", "https://remyndrs.com")

STRIPE_ENABLED = bool(STRIPE_SECRET_KEY and STRIPE_WEBHOOK_SECRET)
if STRIPE_ENABLED:
    logger.info("Stripe payments enabled")
else:
    logger.warning("Stripe not configured - payment features disabled")

# =====================================================
# SUBSCRIPTION TIERS
# =====================================================
# Tier names (stored in users.premium_status)
TIER_FREE = 'free'
TIER_PREMIUM = 'premium'
TIER_FAMILY = 'family'

# Free trial configuration
FREE_TRIAL_DAYS = 14  # New users get 14-day Premium trial
PREMIUM_MONTHLY_PRICE = "$8.99"
PREMIUM_ANNUAL_PRICE = "$89.99"

# Pricing (in cents for Stripe)
PRICING = {
    TIER_PREMIUM: {
        'monthly': 899,           # $8.99/month
        'annual': 8999,           # $89.99/year (~$7.50/mo, save $18)
    },
    TIER_FAMILY: {
        'monthly': 1499,          # $14.99/month (base, 4 members)
        'annual': 16489,          # $164.89/year (1 month free)
        'additional_member': 350,  # $3.50/month per extra member
        'base_members': 4,
        'max_members': 10,
    },
}

# Tier Limits
TIER_LIMITS = {
    TIER_FREE: {
        'reminders_per_day': 2,
        'max_lists': 5,
        'max_items_per_list': 10,
        'max_memories': 5,
        'recurring_reminders': False,
        'support_tickets': False,
    },
    TIER_PREMIUM: {
        'reminders_per_day': None,  # Unlimited
        'max_lists': 20,
        'max_items_per_list': 30,
        'max_memories': None,       # Unlimited
        'recurring_reminders': True,
        'support_tickets': True,
    },
    TIER_FAMILY: {
        'reminders_per_day': None,  # Unlimited
        'max_lists': 20,
        'max_items_per_list': 30,
        'max_memories': None,       # Unlimited
        'recurring_reminders': True,
        'support_tickets': True,
    },
}

def get_tier_limits(tier: str) -> dict:
    """Get limits for a given tier. Defaults to free tier if unknown."""
    return TIER_LIMITS.get(tier, TIER_LIMITS[TIER_FREE])
