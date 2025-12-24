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
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
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
                pending_reminder_time TEXT
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
