"""
Support Service
Handles support ticket creation and management for premium users
"""

from database import get_db_connection, return_db_connection
from config import logger
from services.email_service import send_support_notification
from services.sms_service import send_sms


def is_premium_user(phone_number: str) -> bool:
    """Check if user has premium status"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT premium_status FROM users WHERE phone_number = %s",
            (phone_number,)
        )
        result = c.fetchone()
        return result and result[0] == 'premium'
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

        # Send email notification for inbound messages
        if direction == 'inbound':
            user_name = get_user_name(phone_number)
            send_support_notification(ticket_id, phone_number, message, user_name)

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

        # Send SMS to user
        sms_message = f"[Remyndrs Support] {message}"
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


def close_ticket(ticket_id: int) -> bool:
    """Close a support ticket"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "UPDATE support_tickets SET status = 'closed', updated_at = CURRENT_TIMESTAMP WHERE id = %s",
            (ticket_id,)
        )
        conn.commit()
        return c.rowcount > 0
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


def get_all_tickets(include_closed: bool = False) -> list:
    """Get all support tickets for admin dashboard"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if include_closed:
            c.execute("""
                SELECT t.id, t.phone_number, t.status, t.created_at, t.updated_at,
                       u.first_name,
                       (SELECT COUNT(*) FROM support_messages WHERE ticket_id = t.id) as message_count,
                       (SELECT message FROM support_messages WHERE ticket_id = t.id ORDER BY created_at DESC LIMIT 1) as last_message
                FROM support_tickets t
                LEFT JOIN users u ON t.phone_number = u.phone_number
                ORDER BY t.updated_at DESC
            """)
        else:
            c.execute("""
                SELECT t.id, t.phone_number, t.status, t.created_at, t.updated_at,
                       u.first_name,
                       (SELECT COUNT(*) FROM support_messages WHERE ticket_id = t.id) as message_count,
                       (SELECT message FROM support_messages WHERE ticket_id = t.id ORDER BY created_at DESC LIMIT 1) as last_message
                FROM support_tickets t
                LEFT JOIN users u ON t.phone_number = u.phone_number
                WHERE t.status = 'open'
                ORDER BY t.updated_at DESC
            """)

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
                'last_message': t[7][:100] + '...' if t[7] and len(t[7]) > 100 else t[7]
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
