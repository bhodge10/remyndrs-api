"""
Support Service
Handles support ticket creation and management for premium users

Support Mode:
- When a user has an open ticket that was active in the last 30 minutes,
  they are considered "in support mode"
- Messages are automatically routed to their support ticket
- User can text "EXIT" to leave support mode (keeps ticket open)
- User can text "CLOSE TICKET" to close the ticket and exit support mode
"""

from datetime import datetime, timedelta
from database import get_db_connection, return_db_connection
from config import logger
from services.email_service import send_support_notification
from services.sms_service import send_sms

# How long after last activity to keep user in support mode (minutes)
SUPPORT_MODE_TIMEOUT = 30


def is_premium_user(phone_number: str) -> bool:
    """Check if user has premium or family status (both can access support)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT premium_status FROM users WHERE phone_number = %s",
            (phone_number,)
        )
        result = c.fetchone()
        return result and result[0] in ('premium', 'family')
    except Exception as e:
        logger.error(f"Error checking premium status: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_user_name(phone_number: str) -> str:
    """Get user's first name"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT first_name FROM users WHERE phone_number = %s",
            (phone_number,)
        )
        result = c.fetchone()
        return result[0] if result and result[0] else None
    except Exception as e:
        logger.error(f"Error getting user name: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def create_categorized_ticket(phone_number: str, message: str, category: str, source: str = 'sms') -> dict:
    """
    Create a support ticket for feedback, bug reports, and web contact submissions.
    Unlike regular support tickets, these do NOT enter support mode (updated_at is set
    to an old timestamp so the 30-minute timeout doesn't activate).

    Args:
        phone_number: User's phone number
        message: The message content
        category: 'feedback', 'bug', 'question', or 'support'
        source: 'sms' or 'web'

    Returns:
        dict with ticket_id and success status
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Create ticket with category and source, but set updated_at to old time
        # so the user does NOT enter support mode
        old_time = datetime.utcnow() - timedelta(minutes=SUPPORT_MODE_TIMEOUT + 10)
        c.execute(
            """INSERT INTO support_tickets (phone_number, category, source, updated_at)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (phone_number, category, source, old_time)
        )
        ticket_id = c.fetchone()[0]

        # Add the message
        c.execute(
            """INSERT INTO support_messages (ticket_id, phone_number, message, direction)
               VALUES (%s, %s, %s, 'inbound')""",
            (ticket_id, phone_number, message)
        )

        conn.commit()

        # Send email notification
        user_name = get_user_name(phone_number)
        send_support_notification(ticket_id, phone_number, message, user_name)

        logger.info(f"Created {category} ticket #{ticket_id} from {source} for ...{phone_number[-4:]}")
        return {'success': True, 'ticket_id': ticket_id}

    except Exception as e:
        logger.error(f"Error creating categorized ticket: {e}")
        if conn:
            conn.rollback()
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            return_db_connection(conn)


def get_or_create_open_ticket(phone_number: str) -> int:
    """Get existing open ticket or create a new one"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Check for existing open ticket
        c.execute(
            "SELECT id FROM support_tickets WHERE phone_number = %s AND status = 'open'",
            (phone_number,)
        )
        result = c.fetchone()

        if result:
            return result[0]

        # Create new ticket
        c.execute(
            "INSERT INTO support_tickets (phone_number) VALUES (%s) RETURNING id",
            (phone_number,)
        )
        ticket_id = c.fetchone()[0]
        conn.commit()

        logger.info(f"Created support ticket #{ticket_id} for {phone_number[-4:]}")
        return ticket_id

    except Exception as e:
        logger.error(f"Error getting/creating ticket: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def has_technician_replied(ticket_id: int) -> bool:
    """
    Check if a technician has replied to a ticket.
    Used to determine if we need to send email notifications.
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM support_messages WHERE ticket_id = %s AND direction = 'outbound'",
            (ticket_id,)
        )
        result = c.fetchone()
        return result and result[0] > 0
    except Exception as e:
        logger.error(f"Error checking technician replies: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


# How long after last tech reply to consider conversation "active" (minutes)
ACTIVE_CONVERSATION_TIMEOUT = 10


def is_technician_actively_engaged(ticket_id: int) -> bool:
    """
    Check if a technician has replied recently (within ACTIVE_CONVERSATION_TIMEOUT minutes).
    Used to suppress automated acknowledgment messages during active conversations.
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """SELECT created_at FROM support_messages
               WHERE ticket_id = %s AND direction = 'outbound'
               ORDER BY created_at DESC LIMIT 1""",
            (ticket_id,)
        )
        result = c.fetchone()

        if not result:
            return False

        last_reply_time = result[0]

        # Handle timezone-naive comparison
        now = datetime.utcnow()
        if last_reply_time.tzinfo:
            from datetime import timezone
            now = datetime.now(timezone.utc)

        age_minutes = (now - last_reply_time).total_seconds() / 60
        return age_minutes <= ACTIVE_CONVERSATION_TIMEOUT

    except Exception as e:
        logger.error(f"Error checking technician engagement: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def is_first_message_in_ticket(ticket_id: int) -> bool:
    """Check if this is the first message in the ticket (new ticket)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM support_messages WHERE ticket_id = %s",
            (ticket_id,)
        )
        result = c.fetchone()
        # If count is 0, this will be the first message
        return result and result[0] == 0
    except Exception as e:
        logger.error(f"Error checking first message: {e}")
        return True  # Default to sending email if we can't check
    finally:
        if conn:
            return_db_connection(conn)


def add_support_message(phone_number: str, message: str, direction: str = 'inbound') -> dict:
    """
    Add a message to a support ticket.

    Args:
        phone_number: User's phone number
        message: The message content
        direction: 'inbound' (from user) or 'outbound' (from support)

    Returns:
        dict with ticket_id and success status
    """
    conn = None
    try:
        ticket_id = get_or_create_open_ticket(phone_number)
        if not ticket_id:
            return {'success': False, 'error': 'Could not create ticket'}

        # Check if we should send email BEFORE adding the message
        # Send email if: first message OR technician hasn't replied yet
        should_send_email = False
        if direction == 'inbound':
            is_first = is_first_message_in_ticket(ticket_id)
            tech_replied = has_technician_replied(ticket_id)
            # Send email if it's the first message OR if tech hasn't replied
            should_send_email = is_first or not tech_replied

        conn = get_db_connection()
        c = conn.cursor()

        # Add message to ticket
        c.execute(
            """INSERT INTO support_messages (ticket_id, phone_number, message, direction)
               VALUES (%s, %s, %s, %s)""",
            (ticket_id, phone_number, message, direction)
        )

        # Update ticket timestamp
        c.execute(
            "UPDATE support_tickets SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (ticket_id,)
        )

        conn.commit()

        # Send email notification only if needed
        if direction == 'inbound' and should_send_email:
            user_name = get_user_name(phone_number)
            send_support_notification(ticket_id, phone_number, message, user_name)
            logger.info(f"Email sent for ticket #{ticket_id} (first msg or awaiting tech reply)")
        elif direction == 'inbound':
            logger.info(f"Skipping email for ticket #{ticket_id} (tech already engaged)")

        return {'success': True, 'ticket_id': ticket_id}

    except Exception as e:
        logger.error(f"Error adding support message: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            return_db_connection(conn)


def reply_to_ticket(ticket_id: int, message: str) -> dict:
    """
    Send a reply to a support ticket (sends SMS to user)

    Args:
        ticket_id: The ticket ID to reply to
        message: The reply message

    Returns:
        dict with success status
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get ticket phone number
        c.execute(
            "SELECT phone_number FROM support_tickets WHERE id = %s",
            (ticket_id,)
        )
        result = c.fetchone()

        if not result:
            return {'success': False, 'error': 'Ticket not found'}

        phone_number = result[0]

        # Send SMS to user with ticket number (so they stay in support mode context)
        # Include instructions for exiting support mode
        sms_message = f"[Support Ticket #{ticket_id}]\n\n{message}\n\n(Reply to continue, or text EXIT to return to normal use)"
        send_sms(phone_number, sms_message)

        # Record outbound message
        c.execute(
            """INSERT INTO support_messages (ticket_id, phone_number, message, direction)
               VALUES (%s, %s, %s, 'outbound')""",
            (ticket_id, phone_number, message)
        )

        # Update ticket timestamp
        c.execute(
            "UPDATE support_tickets SET updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (ticket_id,)
        )

        conn.commit()
        logger.info(f"Sent support reply to ticket #{ticket_id}")

        return {'success': True}

    except Exception as e:
        logger.error(f"Error replying to ticket: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            return_db_connection(conn)


def close_ticket(ticket_id: int, notify_user: bool = True) -> bool:
    """
    Close a support ticket and optionally notify the user via SMS.

    Args:
        ticket_id: The ticket ID to close
        notify_user: Whether to send SMS notification to user (default True)

    Returns:
        True if ticket was closed successfully
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get phone number before closing (for SMS notification)
        phone_number = None
        if notify_user:
            c.execute(
                "SELECT phone_number FROM support_tickets WHERE id = %s",
                (ticket_id,)
            )
            result = c.fetchone()
            if result:
                phone_number = result[0]

        # Close the ticket
        c.execute(
            "UPDATE support_tickets SET status = 'closed', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (ticket_id,)
        )
        conn.commit()

        if c.rowcount > 0:
            # Send SMS notification to user
            if notify_user and phone_number:
                try:
                    sms_message = f"[Support Ticket #{ticket_id}] Your support ticket has been closed. Thank you for contacting Remyndrs support! Text SUPPORT anytime to open a new ticket."
                    send_sms(phone_number, sms_message)
                    logger.info(f"Sent closure notification for ticket #{ticket_id}")
                except Exception as e:
                    logger.error(f"Failed to send closure SMS for ticket #{ticket_id}: {e}")
                    # Don't fail the close operation if SMS fails
            return True
        return False

    except Exception as e:
        logger.error(f"Error closing ticket: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def reopen_ticket(ticket_id: int) -> bool:
    """Reopen a closed support ticket"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE support_tickets SET status = 'open', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (ticket_id,)
        )
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        logger.error(f"Error reopening ticket: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_all_tickets(include_closed: bool = False, category_filter: str = None, source_filter: str = None) -> list:
    """Get all support tickets for admin/CS dashboard"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        query = """
            SELECT t.id, t.phone_number, t.status, t.created_at, t.updated_at,
                   u.first_name,
                   (SELECT COUNT(*) FROM support_messages WHERE ticket_id = t.id) as message_count,
                   (SELECT message FROM support_messages WHERE ticket_id = t.id ORDER BY created_at DESC LIMIT 1) as last_message,
                   COALESCE(t.category, 'support') as category,
                   COALESCE(t.source, 'sms') as source,
                   COALESCE(t.priority, 'normal') as priority,
                   t.assigned_to
            FROM support_tickets t
            LEFT JOIN users u ON t.phone_number = u.phone_number
            WHERE 1=1
        """
        params = []

        if not include_closed:
            query += " AND t.status = 'open'"

        if category_filter:
            query += " AND COALESCE(t.category, 'support') = %s"
            params.append(category_filter)

        if source_filter:
            query += " AND COALESCE(t.source, 'sms') = %s"
            params.append(source_filter)

        query += " ORDER BY t.updated_at DESC"

        c.execute(query, params)
        tickets = c.fetchall()
        return [
            {
                'id': t[0],
                'phone_number': t[1],
                'status': t[2],
                'created_at': t[3].isoformat() if t[3] else None,
                'updated_at': t[4].isoformat() if t[4] else None,
                'user_name': t[5],
                'message_count': t[6],
                'last_message': t[7][:100] + '...' if t[7] and len(t[7]) > 100 else t[7],
                'category': t[8],
                'source': t[9],
                'priority': t[10],
                'assigned_to': t[11]
            }
            for t in tickets
        ]
    except Exception as e:
        logger.error(f"Error getting tickets: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_ticket_messages(ticket_id: int) -> list:
    """Get all messages for a specific ticket"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT id, message, direction, created_at
            FROM support_messages
            WHERE ticket_id = %s
            ORDER BY created_at ASC
        """, (ticket_id,))

        messages = c.fetchall()
        return [
            {
                'id': m[0],
                'message': m[1],
                'direction': m[2],
                'created_at': m[3].isoformat() if m[3] else None
            }
            for m in messages
        ]
    except Exception as e:
        logger.error(f"Error getting ticket messages: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_active_support_ticket(phone_number: str) -> dict:
    """
    Check if user has an active support session.

    A user is in support mode if they have an open ticket that was
    updated within the last SUPPORT_MODE_TIMEOUT minutes.

    Returns:
        dict with ticket_id if in support mode, None otherwise
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            SELECT id, updated_at
            FROM support_tickets
            WHERE phone_number = %s AND status = 'open'
            ORDER BY updated_at DESC
            LIMIT 1
        """, (phone_number,))

        result = c.fetchone()

        if not result:
            return None

        ticket_id, updated_at = result

        # Check if ticket was active recently
        if updated_at:
            # Handle timezone-naive comparison
            now = datetime.utcnow()
            if updated_at.tzinfo:
                from datetime import timezone
                now = datetime.now(timezone.utc)

            age_minutes = (now - updated_at).total_seconds() / 60
            if age_minutes <= SUPPORT_MODE_TIMEOUT:
                return {'ticket_id': ticket_id}

        return None

    except Exception as e:
        logger.error(f"Error checking active support ticket: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def exit_support_mode(phone_number: str) -> dict:
    """
    Exit support mode without closing the ticket.
    Updates the ticket timestamp to an old time so user exits support mode.

    Returns:
        dict with success status and ticket_id
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get the open ticket
        c.execute("""
            SELECT id FROM support_tickets
            WHERE phone_number = %s AND status = 'open'
            ORDER BY updated_at DESC
            LIMIT 1
        """, (phone_number,))

        result = c.fetchone()
        if not result:
            return {'success': False, 'error': 'No open ticket found'}

        ticket_id = result[0]

        # Set updated_at to an old time to exit support mode
        # (ticket stays open, but user exits the conversation mode)
        old_time = datetime.utcnow() - timedelta(minutes=SUPPORT_MODE_TIMEOUT + 10)
        c.execute("""
            UPDATE support_tickets
            SET updated_at = %s
            WHERE id = %s
        """, (old_time, ticket_id))

        conn.commit()
        logger.info(f"User exited support mode for ticket #{ticket_id}")

        return {'success': True, 'ticket_id': ticket_id}

    except Exception as e:
        logger.error(f"Error exiting support mode: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            return_db_connection(conn)


def assign_ticket(ticket_id: int, assigned_to: str) -> bool:
    """Assign a ticket to a CS rep"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE support_tickets SET assigned_to = %s WHERE id = %s",
            (assigned_to, ticket_id)
        )
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        logger.error(f"Error assigning ticket: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_ticket_sla_info() -> dict:
    """Get SLA metrics for the ticket dashboard (computed, no new tables)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get open ticket count
        c.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'")
        open_count = c.fetchone()[0]

        # Get tickets waiting for response (no outbound message after last inbound)
        c.execute("""
            SELECT t.id, t.created_at,
                   (SELECT MAX(sm.created_at) FROM support_messages sm
                    WHERE sm.ticket_id = t.id AND sm.direction = 'inbound') as last_inbound,
                   (SELECT MAX(sm.created_at) FROM support_messages sm
                    WHERE sm.ticket_id = t.id AND sm.direction = 'outbound') as last_outbound
            FROM support_tickets t
            WHERE t.status = 'open'
        """)
        rows = c.fetchall()

        now = datetime.utcnow()
        waiting_times = []
        oldest_unanswered_minutes = 0

        for row in rows:
            last_inbound = row[2]
            last_outbound = row[3]

            # Ticket is unanswered if no outbound, or last inbound is newer than last outbound
            if last_inbound and (not last_outbound or last_inbound > last_outbound):
                if last_inbound.tzinfo:
                    from datetime import timezone
                    now_tz = datetime.now(timezone.utc)
                    wait_minutes = (now_tz - last_inbound).total_seconds() / 60
                else:
                    wait_minutes = (now - last_inbound).total_seconds() / 60
                waiting_times.append(wait_minutes)
                if wait_minutes > oldest_unanswered_minutes:
                    oldest_unanswered_minutes = wait_minutes

        avg_wait = sum(waiting_times) / len(waiting_times) if waiting_times else 0

        return {
            'open_count': open_count,
            'unanswered_count': len(waiting_times),
            'avg_wait_minutes': round(avg_wait),
            'oldest_unanswered_minutes': round(oldest_unanswered_minutes)
        }
    except Exception as e:
        logger.error(f"Error getting SLA info: {e}")
        return {'open_count': 0, 'unanswered_count': 0, 'avg_wait_minutes': 0, 'oldest_unanswered_minutes': 0}
    finally:
        if conn:
            return_db_connection(conn)


def close_ticket_by_phone(phone_number: str) -> dict:
    """
    Close the user's open support ticket.

    Returns:
        dict with success status and ticket_id
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get and close the open ticket
        c.execute("""
            UPDATE support_tickets
            SET status = 'closed', updated_at = CURRENT_TIMESTAMP
            WHERE phone_number = %s AND status = 'open'
            RETURNING id
        """, (phone_number,))

        result = c.fetchone()
        conn.commit()

        if not result:
            return {'success': False, 'error': 'No open ticket found'}

        ticket_id = result[0]
        logger.info(f"Closed support ticket #{ticket_id} by user request")

        return {'success': True, 'ticket_id': ticket_id}

    except Exception as e:
        logger.error(f"Error closing ticket by phone: {e}")
        return {'success': False, 'error': str(e)}
    finally:
        if conn:
            return_db_connection(conn)
