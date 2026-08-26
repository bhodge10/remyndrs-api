"""
Database Module
Handles database initialization and connection management for PostgreSQL
"""

import psycopg2
from psycopg2 import pool
from contextlib import contextmanager
from config import DATABASE_URL, MONITORING_DATABASE_URL, ENCRYPTION_ENABLED, logger

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
    """Return a connection to the pool, rolling back any aborted transaction first"""
    global _connection_pool
    if _connection_pool and conn:
        try:
            conn.rollback()
        except Exception:
            pass
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


# =====================================================
# MONITORING DATABASE CONNECTION (for staging to monitor production)
# =====================================================
_monitoring_pool = None

def init_monitoring_pool():
    """Initialize separate connection pool for monitoring database"""
    global _monitoring_pool
    try:
        _monitoring_pool = pool.ThreadedConnectionPool(
            1,  # Min connections
            5,  # Max connections (smaller pool for monitoring)
            MONITORING_DATABASE_URL
        )
        if MONITORING_DATABASE_URL != DATABASE_URL:
            logger.info("Monitoring database pool initialized (separate from main DB)")
        else:
            logger.info("Monitoring database pool initialized (same as main DB)")
    except Exception as e:
        logger.error(f"Failed to initialize monitoring connection pool: {e}")
        raise


def get_monitoring_connection():
    """Get a connection to the monitoring database"""
    global _monitoring_pool
    if _monitoring_pool is None:
        init_monitoring_pool()
    return _monitoring_pool.getconn()


def return_monitoring_connection(conn):
    """Return a monitoring connection to the pool, rolling back any aborted transaction first"""
    global _monitoring_pool
    if _monitoring_pool and conn:
        try:
            conn.rollback()
        except Exception:
            pass
        _monitoring_pool.putconn(conn)


@contextmanager
def get_monitoring_cursor():
    """Context manager for monitoring database operations"""
    conn = None
    try:
        conn = get_monitoring_connection()
        cursor = conn.cursor()
        yield cursor
        conn.commit()
    except Exception as e:
        if conn:
            conn.rollback()
        raise
    finally:
        if conn:
            return_monitoring_connection(conn)


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

        # Onboarding progress tracking for abandoned signup recovery
        c.execute('''
            CREATE TABLE IF NOT EXISTS onboarding_progress (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL UNIQUE,
                current_step INTEGER DEFAULT 1,
                first_name TEXT,
                last_name TEXT,
                email TEXT,
                started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_activity_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                followup_24h_sent BOOLEAN DEFAULT FALSE,
                followup_7d_sent BOOLEAN DEFAULT FALSE,
                cancelled BOOLEAN DEFAULT FALSE
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

        # List shares table (shared lists between users)
        c.execute('''
            CREATE TABLE IF NOT EXISTS list_shares (
                id SERIAL PRIMARY KEY,
                list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
                owner_phone TEXT NOT NULL,
                shared_with_phone TEXT NOT NULL,
                shared_with_name TEXT,
                permission TEXT NOT NULL DEFAULT 'edit',
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                accepted_at TIMESTAMP,
                owner_phone_hash TEXT,
                shared_with_phone_hash TEXT,
                UNIQUE(list_id, shared_with_phone)
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
                completed_at TIMESTAMP,
                source TEXT DEFAULT 'immediate'
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
                sent_at TIMESTAMP,
                target_phone TEXT
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

        # Public changelog for updates, fixes, and features
        c.execute('''
            CREATE TABLE IF NOT EXISTS changelog (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                description TEXT,
                entry_type TEXT NOT NULL DEFAULT 'improvement',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                published BOOLEAN DEFAULT TRUE
            )
        ''')

        # Support tickets for premium users
        c.execute('''
            CREATE TABLE IF NOT EXISTS support_tickets (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                status TEXT DEFAULT 'open',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Support messages (thread of messages for each ticket)
        c.execute('''
            CREATE TABLE IF NOT EXISTS support_messages (
                id SERIAL PRIMARY KEY,
                ticket_id INTEGER REFERENCES support_tickets(id) ON DELETE CASCADE,
                phone_number TEXT NOT NULL,
                message TEXT NOT NULL,
                direction TEXT NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Lightweight contact messages (feedback, bug reports, questions) - not full tickets
        c.execute('''
            CREATE TABLE IF NOT EXISTS contact_messages (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                message TEXT NOT NULL,
                category TEXT NOT NULL,
                source TEXT NOT NULL DEFAULT 'sms',
                resolved BOOLEAN DEFAULT FALSE,
                admin_reply TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Customer service notes
        c.execute('''
            CREATE TABLE IF NOT EXISTS customer_notes (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                note TEXT NOT NULL,
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Confidence score logging for AI calibration tracking
        c.execute('''
            CREATE TABLE IF NOT EXISTS confidence_logs (
                id SERIAL PRIMARY KEY,
                phone_number TEXT,
                action_type TEXT NOT NULL,
                confidence_score INTEGER NOT NULL,
                threshold INTEGER NOT NULL,
                confirmed BOOLEAN,
                user_message TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
        ''')

        # Recurring reminders table
        c.execute('''
            CREATE TABLE IF NOT EXISTS recurring_reminders (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                reminder_text TEXT NOT NULL,
                recurrence_type TEXT NOT NULL,
                recurrence_day INTEGER,
                reminder_time TIME NOT NULL,
                timezone TEXT NOT NULL,
                active BOOLEAN DEFAULT TRUE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_generated_date DATE,
                next_occurrence TIMESTAMP
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
            # Free trial support
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_end_date TIMESTAMP",
            # Trial expiration warning tracking
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_warning_7d_sent BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_warning_1d_sent BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_warning_0d_sent BOOLEAN DEFAULT FALSE",
            # Mid-trial value reminder (Day 7 engagement message)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS mid_trial_reminder_sent BOOLEAN DEFAULT FALSE",
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
            # Opt-out tracking for STOP command compliance
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS opted_out BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS opted_out_at TIMESTAMP",
            # Stripe subscription fields
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_customer_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS stripe_subscription_id TEXT",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS subscription_status TEXT",
            # Recurring reminders: link individual reminders to their recurring pattern
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS recurring_id INTEGER REFERENCES recurring_reminders(id)",
            # Timezone management: store local time for recalculation on timezone change
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS local_time TIME",
            "ALTER TABLE reminders ADD COLUMN IF NOT EXISTS original_timezone TEXT",
            # Pending reminder date for clarify_date_time action (date without time)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_reminder_date TEXT",
            # Pending list create for duplicate list handling
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_list_create TEXT",
            # Daily summary feature: opt-in morning summary of day's reminders
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_summary_enabled BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_summary_time TIME DEFAULT '08:00'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_summary_last_sent DATE",
            # Track if user has been prompted for daily summary after first action
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS daily_summary_prompted BOOLEAN DEFAULT FALSE",
            # Store pending time when confirming evening daily summary preference
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_daily_summary_time TEXT",
            # Store pending reminder for low-confidence confirmations (JSON with reminder details)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_reminder_confirmation TEXT",
            # 5-minute post-onboarding engagement nudge
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS five_minute_nudge_scheduled_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS five_minute_nudge_sent BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS post_onboarding_interactions INTEGER DEFAULT 0",
            # Trial info messaging (one-time after first real interaction)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS trial_info_sent BOOLEAN DEFAULT FALSE",
            # DELETE ACCOUNT: two-step confirmation flag
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_delete_account BOOLEAN DEFAULT FALSE",
            # Support ticket enhancements: category, source, priority, assignment
            "ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS category TEXT DEFAULT 'support'",
            "ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'sms'",
            "ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS priority TEXT DEFAULT 'normal'",
            "ALTER TABLE support_tickets ADD COLUMN IF NOT EXISTS assigned_to TEXT",
            # Cancellation feedback collection
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_cancellation_feedback BOOLEAN DEFAULT FALSE",
            # Canned responses for CS reps
            """CREATE TABLE IF NOT EXISTS canned_responses (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL,
                message TEXT NOT NULL,
                category TEXT DEFAULT 'general',
                created_by TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            # Broadcast system improvements
            "ALTER TABLE scheduled_broadcasts ADD COLUMN IF NOT EXISTS target_phone TEXT",
            "ALTER TABLE broadcast_logs ADD COLUMN IF NOT EXISTS source TEXT DEFAULT 'immediate'",
            # Day 1 & Day 2 lifecycle nudges
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS day_1_nudge_sent BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS day_2_nudge_sent BOOLEAN DEFAULT FALSE",
            # Lifecycle nudges (roundtable Phase 4)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS day_3_nudge_sent BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS post_trial_reengagement_sent BOOLEAN DEFAULT FALSE",
            # 30-day win-back (roundtable 2, Phase 3)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS winback_30d_sent BOOLEAN DEFAULT FALSE",
            # 14-day post-trial touchpoint (roundtable 3, Phase 3)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS post_trial_14d_sent BOOLEAN DEFAULT FALSE",
            # Backfill NULLs to FALSE — ALTER TABLE DEFAULT doesn't backfill existing rows
            "UPDATE users SET trial_warning_7d_sent = FALSE WHERE trial_warning_7d_sent IS NULL",
            "UPDATE users SET trial_warning_1d_sent = FALSE WHERE trial_warning_1d_sent IS NULL",
            "UPDATE users SET trial_warning_0d_sent = FALSE WHERE trial_warning_0d_sent IS NULL",
            "UPDATE users SET mid_trial_reminder_sent = FALSE WHERE mid_trial_reminder_sent IS NULL",
            "UPDATE users SET day_1_nudge_sent = FALSE WHERE day_1_nudge_sent IS NULL",
            "UPDATE users SET day_2_nudge_sent = FALSE WHERE day_2_nudge_sent IS NULL",
            "UPDATE users SET day_3_nudge_sent = FALSE WHERE day_3_nudge_sent IS NULL",
            "UPDATE users SET post_trial_reengagement_sent = FALSE WHERE post_trial_reengagement_sent IS NULL",
            "UPDATE users SET post_trial_14d_sent = FALSE WHERE post_trial_14d_sent IS NULL",
            "UPDATE users SET winback_30d_sent = FALSE WHERE winback_30d_sent IS NULL",
            # Inactivity re-engagement nudge
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS inactivity_nudge_sent_at TIMESTAMP",
            "UPDATE users SET inactivity_nudge_sent_at = NULL WHERE inactivity_nudge_sent_at IS NULL",
            # Cap how many inactivity nudges a single user receives (was weekly-forever)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS inactivity_nudge_count INTEGER DEFAULT 0",
            "UPDATE users SET inactivity_nudge_count = 0 WHERE inactivity_nudge_count IS NULL",
            # Smart Nudges: proactive AI intelligence layer
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS smart_nudges_enabled BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS smart_nudge_time TIME DEFAULT '09:00'",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS smart_nudge_last_sent DATE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_nudge_response TEXT",
            # Smart nudges history table
            """CREATE TABLE IF NOT EXISTS smart_nudges (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                nudge_type TEXT NOT NULL,
                nudge_text TEXT NOT NULL,
                ai_raw_response TEXT,
                sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                user_response TEXT,
                user_responded_at TIMESTAMP,
                action_taken TEXT,
                created_reminder_id INTEGER REFERENCES reminders(id),
                metadata TEXT
            )""",
            # Twilio actual cost tracking (polled daily from Usage Records API)
            """CREATE TABLE IF NOT EXISTS twilio_costs (
                id SERIAL PRIMARY KEY,
                cost_date DATE NOT NULL UNIQUE,
                inbound_count INTEGER DEFAULT 0,
                inbound_cost NUMERIC(10,4) DEFAULT 0,
                outbound_count INTEGER DEFAULT 0,
                outbound_cost NUMERIC(10,4) DEFAULT 0,
                total_cost NUMERIC(10,4) DEFAULT 0,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            # Outbound SMS log for accurate cost tracking
            """CREATE TABLE IF NOT EXISTS sms_outbound_log (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                message_type TEXT NOT NULL DEFAULT 'other',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sms_outbound_log_created ON sms_outbound_log(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_sms_outbound_log_phone ON sms_outbound_log(phone_number)",
            # Inbound SMS log: every validated /sms webhook hit, recorded before
            # any processing so texts that fail later still leave a trace
            """CREATE TABLE IF NOT EXISTS sms_inbound_log (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                body_preview TEXT,
                message_sid TEXT,
                is_new_user BOOLEAN,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_sms_inbound_log_created ON sms_inbound_log(created_at)",
            "CREATE INDEX IF NOT EXISTS idx_sms_inbound_log_phone ON sms_inbound_log(phone_number)",
            # Twilio costs: track failed messages separately
            "ALTER TABLE twilio_costs ADD COLUMN IF NOT EXISTS failed_count INTEGER DEFAULT 0",
            "ALTER TABLE twilio_costs ADD COLUMN IF NOT EXISTS failed_cost NUMERIC(10,4) DEFAULT 0",
            # Admin reply to contact messages
            "ALTER TABLE contact_messages ADD COLUMN IF NOT EXISTS admin_reply TEXT",
            # Day 4 email collection (shortened onboarding)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS day_4_email_sent BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS awaiting_email_collection BOOLEAN DEFAULT FALSE",
            # Backfill NULLs to FALSE
            "UPDATE users SET day_4_email_sent = FALSE WHERE day_4_email_sent IS NULL",
            "UPDATE users SET awaiting_email_collection = FALSE WHERE awaiting_email_collection IS NULL",
            # AI Analytics Summaries (Claude-generated daily analytics insights)
            """CREATE TABLE IF NOT EXISTS analytics_summaries (
                id SERIAL PRIMARY KEY,
                summary_date DATE NOT NULL,
                period_days INTEGER NOT NULL DEFAULT 7,
                raw_data JSONB,
                summary_text TEXT NOT NULL,
                trends JSONB,
                metrics_snapshot JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_analytics_summaries_date ON analytics_summaries(summary_date DESC)",
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_analytics_summaries_date_period ON analytics_summaries(summary_date, period_days)",
            # Threaded analytics chat (admin-only): conversations + per-turn messages
            """CREATE TABLE IF NOT EXISTS analytics_conversations (
                id SERIAL PRIMARY KEY,
                title TEXT NOT NULL DEFAULT 'New conversation',
                admin_user TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                last_active_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                archived BOOLEAN DEFAULT FALSE
            )""",
            """CREATE TABLE IF NOT EXISTS analytics_messages (
                id SERIAL PRIMARY KEY,
                conversation_id INTEGER NOT NULL REFERENCES analytics_conversations(id) ON DELETE CASCADE,
                role TEXT NOT NULL CHECK (role IN ('user', 'assistant')),
                content TEXT NOT NULL,
                model TEXT,
                effort TEXT,
                data_source TEXT,
                input_tokens INTEGER,
                output_tokens INTEGER,
                cache_read_input_tokens INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_analytics_messages_conv ON analytics_messages(conversation_id, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_analytics_conversations_active ON analytics_conversations(archived, last_active_at DESC)",
            # Attachments (images) uploaded to analytics chat messages
            """CREATE TABLE IF NOT EXISTS analytics_attachments (
                id SERIAL PRIMARY KEY,
                message_id INTEGER NOT NULL REFERENCES analytics_messages(id) ON DELETE CASCADE,
                filename TEXT,
                mime_type TEXT NOT NULL,
                size_bytes INTEGER NOT NULL,
                data BYTEA NOT NULL,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
            "CREATE INDEX IF NOT EXISTS idx_analytics_attachments_message ON analytics_attachments(message_id)",
            # Nudge tracking for pending onboarding users
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS last_nudged_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS nudge_count_24h INTEGER DEFAULT 0",
            # Backfill nudge timestamps for users nudged before tracking was added (2026-03-10)
            "UPDATE users SET last_nudged_at = '2026-03-10 16:35:00', nudge_count_24h = 1 WHERE phone_number LIKE '%4793' AND last_nudged_at IS NULL AND onboarding_complete = FALSE",
            "UPDATE users SET last_nudged_at = '2026-03-10 16:35:00', nudge_count_24h = 1 WHERE phone_number LIKE '%2936' AND last_nudged_at IS NULL AND onboarding_complete = FALSE",
            "UPDATE users SET last_nudged_at = '2026-03-10 16:35:00', nudge_count_24h = 1 WHERE phone_number LIKE '%6167' AND last_nudged_at IS NULL AND onboarding_complete = FALSE",
            # Free tier versioning: v1 = grandfathered existing users, v2 = new users (default)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS free_tier_version INTEGER DEFAULT 2",
            # Backfill all existing users to v1 (grandfathered)
            "UPDATE users SET free_tier_version = 1 WHERE free_tier_version = 2 OR free_tier_version IS NULL",
            # Shared lists: pending name prompt state (JSON with list_id, shared_with_phone, list_name)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS pending_share_name TEXT",
            # Shared lists: owner-assigned name for the person they shared with
            "ALTER TABLE list_shares ADD COLUMN IF NOT EXISTS shared_with_name TEXT",
            # Shared lists beta: user-initiated opt-in (Premium-only feature, but any user can opt in)
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS shared_lists_beta_opt_in BOOLEAN DEFAULT FALSE",
            "UPDATE users SET shared_lists_beta_opt_in = FALSE WHERE shared_lists_beta_opt_in IS NULL",
            # Soft opt-out: silence proactive lifecycle/marketing messages while still
            # allowing user-requested reminders. Hard STOP still uses opted_out.
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS lifecycle_messages_opted_out BOOLEAN DEFAULT FALSE",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS lifecycle_messages_opted_out_at TIMESTAMP",
            "UPDATE users SET lifecycle_messages_opted_out = FALSE WHERE lifecycle_messages_opted_out IS NULL",
            # NFL morning-after score beta (closed beta). Invite sending is NOT
            # scheduled — these columns only record a send if a later PR does it.
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS sports_invite_sent_at TIMESTAMP",
            "ALTER TABLE users ADD COLUMN IF NOT EXISTS sports_invite_cohort TEXT",
            """CREATE TABLE IF NOT EXISTS sports_optins (
                phone_number TEXT NOT NULL,
                team_abbr TEXT NOT NULL,
                team_short TEXT NOT NULL,
                cohort TEXT NOT NULL CHECK (cohort IN ('weekly', 'dormant')),
                opted_in_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                ignore_streak INTEGER NOT NULL DEFAULT 0,
                last_ask_date DATE,
                last_ask_at TIMESTAMP,
                last_ask_game_id TEXT,
                last_ask_replied BOOLEAN NOT NULL DEFAULT FALSE,
                pending_score_payload JSONB,
                beta_started_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
                pause_at TIMESTAMP,
                paused_at TIMESTAMP,
                stopped_silently BOOLEAN NOT NULL DEFAULT FALSE,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                PRIMARY KEY (phone_number, team_abbr)
            )""",
            "ALTER TABLE sports_optins ADD COLUMN IF NOT EXISTS last_ask_at TIMESTAMP",
            """CREATE TABLE IF NOT EXISTS sports_score_events (
                id SERIAL PRIMARY KEY,
                phone_number TEXT NOT NULL,
                cohort TEXT NOT NULL CHECK (cohort IN ('weekly', 'dormant')),
                event_type TEXT NOT NULL,
                metadata JSONB,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )""",
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
            # Recurring reminders indexes
            "CREATE INDEX IF NOT EXISTS idx_recurring_reminders_phone ON recurring_reminders(phone_number)",
            "CREATE INDEX IF NOT EXISTS idx_recurring_reminders_active ON recurring_reminders(active, next_occurrence) WHERE active = TRUE",
            "CREATE INDEX IF NOT EXISTS idx_reminders_recurring_id ON reminders(recurring_id) WHERE recurring_id IS NOT NULL",
            # Daily summary: index for efficient querying of users who need summary
            "CREATE INDEX IF NOT EXISTS idx_users_daily_summary ON users(daily_summary_enabled) WHERE daily_summary_enabled = TRUE",
            # Onboarding recovery: index for finding abandoned signups
            "CREATE INDEX IF NOT EXISTS idx_onboarding_progress_abandoned ON onboarding_progress(followup_24h_sent, last_activity_at) WHERE cancelled = FALSE",
            # Smart nudges: index for efficient querying of users who need nudge
            "CREATE INDEX IF NOT EXISTS idx_users_smart_nudges ON users(smart_nudges_enabled) WHERE smart_nudges_enabled = TRUE",
            "CREATE INDEX IF NOT EXISTS idx_smart_nudges_phone ON smart_nudges(phone_number, sent_at)",
            # Twilio costs: index for date-range queries
            "CREATE INDEX IF NOT EXISTS idx_twilio_costs_date ON twilio_costs(cost_date)",
            # Shared lists: indexes for efficient lookups
            "CREATE INDEX IF NOT EXISTS idx_list_shares_shared_with ON list_shares(shared_with_phone)",
            "CREATE INDEX IF NOT EXISTS idx_list_shares_list_id ON list_shares(list_id)",
            "CREATE INDEX IF NOT EXISTS idx_list_shares_owner ON list_shares(owner_phone)",
            "CREATE INDEX IF NOT EXISTS idx_sports_score_events_cohort_type ON sports_score_events(cohort, event_type, created_at)",
            "CREATE INDEX IF NOT EXISTS idx_sports_score_events_phone ON sports_score_events(phone_number)",
            "CREATE INDEX IF NOT EXISTS idx_sports_optins_stopped ON sports_optins(stopped_silently) WHERE stopped_silently = FALSE",
        ]

        for migration in migrations:
            try:
                c.execute(migration)
            except Exception as e:
                err_msg = str(e).lower()
                if 'already exists' in err_msg or 'duplicate' in err_msg:
                    pass  # Expected — column/table already exists
                else:
                    logger.error(f"Unexpected migration error: {migration[:80]}... — {e}")

        for index_migration in index_migrations:
            try:
                c.execute(index_migration)
            except Exception as e:
                err_msg = str(e).lower()
                if 'already exists' in err_msg or 'duplicate' in err_msg:
                    pass  # Expected — index already exists
                else:
                    logger.error(f"Unexpected index migration error: {index_migration[:80]}... — {e}")

        # Founder dual dry-run: one phone can have two teams. Production stays
        # one-team via application logic. Older DBs had phone_number as PK.
        try:
            c.execute(
                """
                SELECT a.attname
                FROM pg_index i
                JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = ANY(i.indkey)
                WHERE i.indrelid = 'sports_optins'::regclass AND i.indisprimary
                ORDER BY a.attnum
                """
            )
            pk_cols = [row[0] for row in c.fetchall()]
            if pk_cols == ['phone_number']:
                c.execute("ALTER TABLE sports_optins DROP CONSTRAINT sports_optins_pkey")
                c.execute("ALTER TABLE sports_optins ADD PRIMARY KEY (phone_number, team_abbr)")
                logger.info("Migrated sports_optins primary key to (phone_number, team_abbr)")
        except Exception as e:
            logger.error(f"sports_optins PK migration skipped/failed: {e}")

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


def log_confidence(phone_number, action_type, confidence_score, threshold, confirmed=None, user_message=None):
    """Log confidence score for AI calibration tracking.

    Args:
        phone_number: User's phone number
        action_type: Type of action (reminder, reminder_relative, reminder_recurring)
        confidence_score: AI's confidence score (0-100)
        threshold: The threshold used for comparison
        confirmed: True if user confirmed, False if rejected, None if no confirmation needed
        user_message: The original user message (for debugging)
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''INSERT INTO confidence_logs (phone_number, action_type, confidence_score, threshold, confirmed, user_message)
               VALUES (%s, %s, %s, %s, %s, %s)''',
            (phone_number, action_type, confidence_score, threshold, confirmed, user_message)
        )
        conn.commit()
        logger.debug(f"Logged confidence: {action_type} score={confidence_score} threshold={threshold} confirmed={confirmed}")
    except Exception as e:
        logger.error(f"Error logging confidence: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def get_recent_logs(limit=100, offset=0, phone_filter=None, intent_filter=None, hide_reviewed=False, start_date=None, end_date=None):
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

        if start_date:
            query += ' AND l.created_at >= %s'
            params.append(start_date)
        if end_date:
            query += ' AND l.created_at < %s'
            params.append(end_date)

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


def get_flagged_conversations(limit=50, include_reviewed=False, start_date=None, end_date=None):
    """Get flagged conversations from AI or manual flagging"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        base_select = '''
            SELECT ca.id, ca.log_id, ca.phone_number, ca.issue_type, ca.severity,
                   ca.ai_explanation, ca.reviewed, l.created_at,
                   l.message_in, l.message_out, COALESCE(ca.source, 'ai'),
                   COALESCE(u.timezone, 'America/New_York')
            FROM conversation_analysis ca
            LEFT JOIN logs l ON ca.log_id = l.id
            LEFT JOIN users u ON ca.phone_number = u.phone_number
        '''
        params = []

        if include_reviewed:
            query = base_select + ' WHERE 1=1'
        else:
            query = base_select + ' WHERE ca.reviewed = FALSE'

        if start_date:
            query += ' AND l.created_at >= %s'
            params.append(start_date)
        if end_date:
            query += ' AND l.created_at < %s'
            params.append(end_date)

        query += ' ORDER BY l.created_at DESC LIMIT %s'
        params.append(limit)
        c.execute(query, params)
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


def dismiss_conversation(log_id, phone_number):
    """Dismiss a conversation (already fixed, not applicable)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('''
            INSERT INTO conversation_analysis (log_id, phone_number, issue_type, severity, ai_explanation, source)
            VALUES (%s, %s, 'dismissed', 'none', 'Dismissed - already fixed or not applicable', 'manual')
        ''', (log_id, phone_number))
        conn.commit()
        return True
    except Exception as e:
        logger.error(f"Error dismissing conversation: {e}")
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
