"""
Database Module
Handles database initialization and connection management for PostgreSQL
"""

import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from config import DATABASE_URL, ENCRYPTION_ENABLED, logger

# Connection pool settings
MIN_CONNECTIONS = 2
MAX_CONNECTIONS = 10

# Initialize connection pool
_connection_pool = None


def init_connection_pool():
    """Initialize the database connection pool"""
    global _connection_pool
    try:
        _connection_pool = pool.ThreadedConnectionPool(
            MIN_CONNECTIONS,
            MAX_CONNECTIONS,
            DATABASE_URL
        )
        logger.info(f"Database connection pool initialized (min={MIN_CONNECTIONS}, max={MAX_CONNECTIONS})")
    except Exception as e:
        logger.error(f"Failed to initialize connection pool: {e}")
        raise


def get_db_connection():
    """Get a database connection from the pool"""
    global _connection_pool
    if _connection_pool is None:
        init_connection_pool()
    return _connection_pool.getconn()


def return_db_connection(conn):
    """Return a connection to the pool"""
    global _connection_pool
    if _connection_pool and conn:
        _connection_pool.putconn(conn)


@contextmanager
def get_db_cursor():
    """Context manager for database operations - handles connection lifecycle"""
    conn = None
    try:
        conn = get_db_connection()
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            return_db_connection(conn)

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

        # Lists table
        c.execute('''
            CREATE TABLE IF NOT EXISTS lists (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                list_name TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(phone_number, list_name)
            )
        ''')

        # List items table
        c.execute('''
            CREATE TABLE IF NOT EXISTS list_items (
                id SERIAL PRIMARY KEY,
                list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
                phone_number TEXT NOT NULL,
                item_text TEXT NOT NULL,
                completed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Broadcast logs table
        c.execute('''
            CREATE TABLE IF NOT EXISTS broadcast_logs (
                id SERIAL PRIMARY KEY,
                sender TEXT NOT NULL,
                message TEXT NOT NULL,
                audience TEXT NOT NULL,
                recipient_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                completed_at TIMESTAMP
            )
        ''')

        # User feedback table
        c.execute('''
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                user_phone TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved BOOLEAN DEFAULT FALSE
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
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_list_item TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_active_list TEXT",
            # Encryption: Add phone_hash columns for secure lookups
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS phone_hash TEXT",
            "ALTER TABLE memories ADD COLUMN IF NOT EXISTS phone_hash TEXT",
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS phone_hash TEXT",
            "ALTER TABLE logs ADD COLUMN IF NOT EXISTS phone_hash TEXT",
            "ALTER TABLE lists ADD COLUMN IF NOT EXISTS phone_hash TEXT",
            "ALTER TABLE list_items ADD COLUMN IF NOT EXISTS phone_hash TEXT",
            # Encryption: Add encrypted field columns
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS first_name_encrypted TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_name_encrypted TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS email_encrypted TEXT",
            "ALTER TABLE memories ADD COLUMN IF NOT EXISTS memory_text_encrypted TEXT",
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS reminder_text_encrypted TEXT",
            "ALTER TABLE logs ADD COLUMN IF NOT EXISTS message_in_encrypted TEXT",
            "ALTER TABLE logs ADD COLUMN IF NOT EXISTS message_out_encrypted TEXT",
            "ALTER TABLE list_items ADD COLUMN IF NOT EXISTS item_text_encrypted TEXT",
            # Delete reminder feature: stores search results when multiple matches found
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_reminder_delete TEXT",
            # Delete memory feature: stores search results when multiple matches or confirmation needed
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_memory_delete TEXT",
        ]

        # Create indexes on phone_hash columns for efficient lookups
        index_migrations = [
            "CREATE INDEX IF NOT EXISTS idx_users_phone_hash ON users(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_memories_phone_hash ON memories(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_reminders_phone_hash ON reminders(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_logs_phone_hash ON logs(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_lists_phone_hash ON lists(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_list_items_phone_hash ON list_items(phone_hash)",
        ]

        for migration in migrations:
            try:
                c.execute(migration)
            except Exception:
                pass  # Column likely already exists

        for index_migration in index_migrations:
            try:
                c.execute(index_migration)
            except Exception:
                pass  # Index likely already exists

        conn.commit()
        return_db_connection(conn)
        logger.info("Database initialized successfully")
    except Exception as e:
        logger.error(f"Database initialization failed: {e}")
        raise

def log_interaction(phone_number, message_in, message_out, intent, success):
    """Log an interaction to the database with optional encryption"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import encrypt_field, hash_phone
            phone_hash = hash_phone(phone_number)
            msg_in_encrypted = encrypt_field(message_in)
            msg_out_encrypted = encrypt_field(message_out)
            c.execute(
                '''INSERT INTO logs (phone_number, phone_hash, message_in, message_out,
                   message_in_encrypted, message_out_encrypted, intent, success)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s)''',
                (phone_number, phone_hash, message_in, message_out,
                 msg_in_encrypted, msg_out_encrypted, intent, success)
            )
        else:
            c.execute(
                'INSERT INTO logs (phone_number, message_in, message_out, intent, success) VALUES (%s, %s, %s, %s, %s)',
                (phone_number, message_in, message_out, intent, success)
            )
        conn.commit()
    except Exception as e:
        logger.error(f"Error logging interaction: {e}")
    finally:
        if conn:
            return_db_connection(conn)
