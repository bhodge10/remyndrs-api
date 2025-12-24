"""
Database Module
Handles database initialization and connection management for PostgreSQL
"""

import psycopg2
from config import DATABASE_URL, logger

def get_db_connection():
    """Get a database connection"""
    return psycopg2.connect(DATABASE_URL)

def init_db():
    """Initialize all database tables"""
    try:
        logger.info("Initializing database...")
        conn = get_db_connection()
        c = conn.cursor()

        # Memories table
        c.execute('''
            CREATE TABLE IF NOT EXISTS memories (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                memory_text TEXT NOT NULL,
                parsed_data TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Reminders table
        c.execute('''
            CREATE TABLE IF NOT EXISTS reminders (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                reminder_text TEXT NOT NULL,
                reminder_date TIMESTAMP NOT NULL,
                sent BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                delivery_status TEXT DEFAULT 'pending',
                sent_at TIMESTAMP,
                error_message TEXT
            )
        ''')

        # Users table with onboarding info
        c.execute('''
            CREATE TABLE IF NOT EXISTS users (
                phone_number TEXT PRIMARY KEY,
                first_name TEXT,
                last_name TEXT,
                email TEXT,
                zip_code TEXT,
                timezone TEXT,
                onboarding_complete BOOLEAN DEFAULT FALSE,
                onboarding_step INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                pending_delete BOOLEAN DEFAULT FALSE,
                pending_reminder_text TEXT,
                pending_reminder_time TEXT,
                referral_source TEXT,
                premium_status TEXT DEFAULT 'free',
                premium_since TIMESTAMP,
                last_active_at TIMESTAMP,
                signup_source TEXT,
                total_messages INTEGER DEFAULT 0
            )
        ''')

        # Logs table for monitoring
        c.execute('''
            CREATE TABLE IF NOT EXISTS logs (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                message_in TEXT NOT NULL,
                message_out TEXT NOT NULL,
                intent TEXT,
                success BOOLEAN,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Add new columns to existing tables (migrations)
        # These will silently fail if columns already exist
        migrations = [
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS referral_source TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_status TEXT DEFAULT 'free'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS premium_since TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS signup_source TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS total_messages INTEGER DEFAULT 0",
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS delivery_status TEXT DEFAULT 'pending'",
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS sent_at TIMESTAMP",
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS error_message TEXT",
        ]

        for migration in migrations:
            try:
                c.execute(migration)
            except Exception:
                pass  # Column likely already exists

        conn.commit()
        conn.close()
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

def log_interaction(phone_number, message_in, message_out, intent, success):
    """Log an interaction to the database"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'INSERT INTO logs (phone_number, message_in, message_out, intent, success) VALUES (%s, %s, %s, %s, %s)',
            (phone_number, message_in, message_out, intent, success)
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.error(f"Error logging interaction: {e}")
