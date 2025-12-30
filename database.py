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

        # Scheduled broadcasts table
        c.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_broadcasts (
                id SERIAL PRIMARY KEY,
                sender TEXT NOT NULL,
                message TEXT NOT NULL,
                audience TEXT NOT NULL,
                scheduled_date TIMESTAMP NOT NULL,
                status TEXT DEFAULT 'scheduled',
                recipient_count INTEGER DEFAULT 0,
                success_count INTEGER DEFAULT 0,
                fail_count INTEGER DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP
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

        # Conversation analysis table for AI-flagged issues
        c.execute('''
            CREATE TABLE IF NOT EXISTS conversation_analysis (
                id SERIAL PRIMARY KEY,
                log_id INTEGER REFERENCES logs(id),
                phone_number TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                severity TEXT DEFAULT 'low',
                ai_explanation TEXT,
                reviewed BOOLEAN DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # API usage tracking table for cost analytics
        c.execute('''
            CREATE TABLE IF NOT EXISTS api_usage (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                request_type TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                model TEXT,
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
            # Feedback table (created via migration for existing deployments)
            """CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                user_phone TEXT NOT NULL,
                message TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                resolved BOOLEAN DEFAULT FALSE
            )""",
            # API usage tracking table for cost analytics
            """CREATE TABLE IF NOT EXISTS api_usage (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                request_type TEXT NOT NULL,
                prompt_tokens INTEGER DEFAULT 0,
                completion_tokens INTEGER DEFAULT 0,
                total_tokens INTEGER DEFAULT 0,
                model TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            # Snooze feature: track last sent reminder for snooze detection
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_sent_reminder_id INTEGER",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_sent_reminder_at TIMESTAMP",
            # Track if a reminder was snoozed (to avoid showing duplicates)
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS snoozed BOOLEAN DEFAULT FALSE",
            # Celery: Add claimed_at column for atomic reminder claiming
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS claimed_at TIMESTAMP",
            # Settings table for app configuration
            """CREATE TABLE IF NOT EXISTS settings (
                key TEXT PRIMARY KEY,
                value TEXT,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            # Conversation analysis table for AI-flagged issues
            """CREATE TABLE IF NOT EXISTS conversation_analysis (
                id SERIAL PRIMARY KEY,
                log_id INTEGER REFERENCES logs(id),
                phone_number TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                severity TEXT DEFAULT 'low',
                ai_explanation TEXT,
                reviewed BOOLEAN DEFAULT FALSE,
                source TEXT DEFAULT 'ai',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            # Track which logs have been analyzed
            "ALTER TABLE logs ADD COLUMN IF NOT EXISTS analyzed BOOLEAN DEFAULT FALSE",
            # Add source column for flagging source (ai vs manual)
            "ALTER TABLE conversation_analysis ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'ai'",
        ]

        # Create indexes on phone_hash columns for efficient lookups
        index_migrations = [
            "CREATE INDEX IF NOT EXISTS idx_users_phone_hash ON users(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_memories_phone_hash ON memories(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_reminders_phone_hash ON reminders(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_logs_phone_hash ON logs(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_lists_phone_hash ON lists(phone_hash)",
            "CREATE INDEX IF NOT EXISTS idx_list_items_phone_hash ON list_items(phone_hash)",
            # Celery: Index for efficient querying of due reminders
            "CREATE INDEX IF NOT EXISTS idx_reminders_due ON reminders(reminder_date, sent, claimed_at) WHERE sent = FALSE",
            # Index for conversation analysis lookups
            "CREATE INDEX IF NOT EXISTS idx_logs_analyzed ON logs(analyzed) WHERE analyzed = FALSE",
            "CREATE INDEX IF NOT EXISTS idx_conversation_analysis_reviewed ON conversation_analysis(reviewed) WHERE reviewed = FALSE",
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


def log_api_usage(phone_number, request_type, prompt_tokens, completion_tokens, total_tokens, model):
    """Log API token usage for cost tracking"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''INSERT INTO api_usage (phone_number, request_type, prompt_tokens, completion_tokens, total_tokens, model)
               VALUES (%s, %s, %s, %s, %s, %s)''',
            (phone_number, request_type, prompt_tokens, completion_tokens, total_tokens, model)
        )
        conn.commit()
    except Exception as e:
        logger.error(f"Error logging API usage: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def get_setting(key, default=None):
    """Get a setting value from the database"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT value FROM settings WHERE key = %s', (key,))
        result = c.fetchone()
        return result[0] if result else default
    except Exception as e:
        logger.error(f"Error getting setting {key}: {e}")
        return default
    finally:
        if conn:
            return_db_connection(conn)


def set_setting(key, value):
    """Set a setting value in the database"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO settings (key, value, updated_at)
            VALUES (%s, %s, CURRENT_TIMESTAMP)
            ON CONFLICT (key) DO UPDATE SET value = %s, updated_at = CURRENT_TIMESTAMP
        ''', (key, value, value))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error setting {key}: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_recent_logs(limit=100, offset=0, phone_filter=None, intent_filter=None, hide_reviewed=False):
    """Get recent conversation logs for viewing"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Build query dynamically based on filters
        # Include subquery to check review status and join with users for timezone
        query = '''
            SELECT l.id, l.phone_number, l.message_in, l.message_out, l.intent, l.success, l.created_at, l.analyzed,
                   (SELECT ca.issue_type FROM conversation_analysis ca WHERE ca.log_id = l.id LIMIT 1) as review_status,
                   COALESCE(u.timezone, 'America/New_York') as user_timezone
            FROM logs l
            LEFT JOIN users u ON l.phone_number = u.phone_number
            WHERE 1=1
        '''
        params = []

        if phone_filter:
            query += ' AND l.phone_number LIKE %s'
            params.append(f'%{phone_filter}%')

        if intent_filter:
            query += ' AND l.intent = %s'
            params.append(intent_filter)

        if hide_reviewed:
            query += ' AND NOT EXISTS(SELECT 1 FROM conversation_analysis ca WHERE ca.log_id = l.id)'

        query += ' ORDER BY l.created_at DESC LIMIT %s OFFSET %s'
        params.extend([limit, offset])

        c.execute(query, params)
        rows = c.fetchall()
        return [
            {
                'id': row[0],
                'phone_number': row[1],
                'message_in': row[2],
                'message_out': row[3],
                'intent': row[4],
                'success': row[5],
                'created_at': row[6].isoformat() if row[6] else None,
                'analyzed': row[7] if len(row) > 7 else False,
                'review_status': row[8] if len(row) > 8 else None,
                'timezone': row[9] if len(row) > 9 else 'America/New_York'
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Error getting recent logs: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_unanalyzed_logs(limit=50):
    """Get logs that haven't been analyzed yet"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT id, phone_number, message_in, message_out, intent, success, created_at
            FROM logs
            WHERE analyzed = FALSE OR analyzed IS NULL
            ORDER BY created_at DESC
            LIMIT %s
        ''', (limit,))
        rows = c.fetchall()
        return [
            {
                'id': row[0],
                'phone_number': row[1],
                'message_in': row[2],
                'message_out': row[3],
                'intent': row[4],
                'success': row[5],
                'created_at': row[6].isoformat() if row[6] else None
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Error getting unanalyzed logs: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def mark_logs_analyzed(log_ids):
    """Mark logs as analyzed"""
    if not log_ids:
        return
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            UPDATE logs SET analyzed = TRUE WHERE id = ANY(%s)
        ''', (log_ids,))
        conn.commit()
    except Exception as e:
        logger.error(f"Error marking logs as analyzed: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def save_conversation_analysis(log_id, phone_number, issue_type, severity, explanation):
    """Save an AI analysis result for a conversation"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO conversation_analysis (log_id, phone_number, issue_type, severity, ai_explanation)
            VALUES (%s, %s, %s, %s, %s)
        ''', (log_id, phone_number, issue_type, severity, explanation))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error saving conversation analysis: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_flagged_conversations(limit=50, include_reviewed=False):
    """Get flagged conversations from AI or manual flagging"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        if include_reviewed:
            c.execute('''
                SELECT ca.id, ca.log_id, ca.phone_number, ca.issue_type, ca.severity,
                       ca.ai_explanation, ca.reviewed, ca.created_at,
                       l.message_in, l.message_out, COALESCE(ca.source, 'ai'),
                       COALESCE(u.timezone, 'America/New_York')
                FROM conversation_analysis ca
                LEFT JOIN logs l ON ca.log_id = l.id
                LEFT JOIN users u ON ca.phone_number = u.phone_number
                ORDER BY ca.created_at DESC
                LIMIT %s
            ''', (limit,))
        else:
            c.execute('''
                SELECT ca.id, ca.log_id, ca.phone_number, ca.issue_type, ca.severity,
                       ca.ai_explanation, ca.reviewed, ca.created_at,
                       l.message_in, l.message_out, COALESCE(ca.source, 'ai'),
                       COALESCE(u.timezone, 'America/New_York')
                FROM conversation_analysis ca
                LEFT JOIN logs l ON ca.log_id = l.id
                LEFT JOIN users u ON ca.phone_number = u.phone_number
                WHERE ca.reviewed = FALSE
                ORDER BY ca.created_at DESC
                LIMIT %s
            ''', (limit,))
        rows = c.fetchall()
        return [
            {
                'id': row[0],
                'log_id': row[1],
                'phone_number': row[2],
                'issue_type': row[3],
                'severity': row[4],
                'ai_explanation': row[5],
                'reviewed': row[6],
                'created_at': row[7].isoformat() if row[7] else None,
                'message_in': row[8],
                'message_out': row[9],
                'source': row[10] if len(row) > 10 else 'ai',
                'timezone': row[11] if len(row) > 11 else 'America/New_York'
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Error getting flagged conversations: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def mark_analysis_reviewed(analysis_id):
    """Mark a flagged conversation as reviewed"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            UPDATE conversation_analysis SET reviewed = TRUE WHERE id = %s
        ''', (analysis_id,))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error marking analysis reviewed: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def manual_flag_conversation(log_id, phone_number, issue_type, notes):
    """Manually flag a conversation for review"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Add 'source' to indicate manual vs AI flagging
        c.execute('''
            INSERT INTO conversation_analysis (log_id, phone_number, issue_type, severity, ai_explanation, source)
            VALUES (%s, %s, %s, 'medium', %s, 'manual')
        ''', (log_id, phone_number, issue_type, notes))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error manually flagging conversation: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def mark_conversation_good(log_id, phone_number, notes=""):
    """Mark a conversation as good/accurate"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO conversation_analysis (log_id, phone_number, issue_type, severity, ai_explanation, source)
            VALUES (%s, %s, 'good', 'none', %s, 'manual')
        ''', (log_id, phone_number, notes or 'Marked as good'))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error marking conversation as good: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_good_conversations(limit=50):
    """Get conversations marked as good"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            SELECT ca.id, ca.log_id, ca.phone_number, ca.ai_explanation, ca.created_at,
                   l.message_in, l.message_out, l.intent
            FROM conversation_analysis ca
            LEFT JOIN logs l ON ca.log_id = l.id
            WHERE ca.issue_type = 'good'
            ORDER BY ca.created_at DESC
            LIMIT %s
        ''', (limit,))
        rows = c.fetchall()
        return [
            {
                'id': row[0],
                'log_id': row[1],
                'phone_number': row[2],
                'notes': row[3],
                'created_at': row[4].isoformat() if row[4] else None,
                'message_in': row[5],
                'message_out': row[6],
                'intent': row[7]
            }
            for row in rows
        ]
    except Exception as e:
        logger.error(f"Error getting good conversations: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)
