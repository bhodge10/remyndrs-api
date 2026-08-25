"""
List Model
Handles all list-related database operations
"""

import html
from datetime import datetime
from typing import Any, Optional

from database import get_db_connection, return_db_connection
from config import logger, ENCRYPTION_ENABLED, SHARED_LIST_MAX_MEMBERS, SHARED_LIST_MAX_PER_USER, SHARED_LIST_MAX_RECEIVED

# last_active_list context markers are not real list names
_CONTEXT_MARKERS = frozenset({
    "__RECURRING__", "__REMINDERS__", "__LISTS__", "__MEMORIES__",
})
_PARTIAL_MATCH_MIN = 3


def _norm_text(value: str) -> str:
    """Lowercase, strip, and HTML-unescape for lookup comparison.

    Leftover encoded names from before the storage-time escaping fix
    (e.g. Sam&#39;s Club) then match user/AI input like "Sam's Club".
    """
    if not value:
        return ""
    return html.unescape(value).strip().lower()


def create_list(phone_number: str, list_name: str) -> Optional[int]:
    """Create a new list for a user"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'INSERT INTO lists (phone_number, phone_hash, list_name) VALUES (%s, %s, %s) RETURNING id',
                (phone_number, phone_hash, list_name)
            )
        else:
            c.execute(
                'INSERT INTO lists (phone_number, list_name) VALUES (%s, %s) RETURNING id',
                (phone_number, list_name)
            )

        list_id = c.fetchone()[0]
        conn.commit()
        logger.info(f"Created list '{list_name}'")
        return list_id
    except Exception as e:
        logger.error(f"Error creating list: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_lists(phone_number: str) -> list[tuple[int, str, int, int]]:
    """Get all lists for a user with item counts"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('''
                SELECT l.id, l.list_name,
                       COUNT(li.id) as item_count,
                       SUM(CASE WHEN li.completed THEN 1 ELSE 0 END) as completed_count
                FROM lists l
                LEFT JOIN list_items li ON l.id = li.list_id
                WHERE l.phone_hash = %s
                GROUP BY l.id, l.list_name
                ORDER BY l.created_at DESC
            ''', (phone_hash,))
            results = c.fetchall()
            if not results:
                # Fallback for lists created before encryption
                c.execute('''
                    SELECT l.id, l.list_name,
                           COUNT(li.id) as item_count,
                           SUM(CASE WHEN li.completed THEN 1 ELSE 0 END) as completed_count
                    FROM lists l
                    LEFT JOIN list_items li ON l.id = li.list_id
                    WHERE l.phone_number = %s
                    GROUP BY l.id, l.list_name
                    ORDER BY l.created_at DESC
                ''', (phone_number,))
                results = c.fetchall()
        else:
            c.execute('''
                SELECT l.id, l.list_name,
                       COUNT(li.id) as item_count,
                       SUM(CASE WHEN li.completed THEN 1 ELSE 0 END) as completed_count
                FROM lists l
                LEFT JOIN list_items li ON l.id = li.list_id
                WHERE l.phone_number = %s
                GROUP BY l.id, l.list_name
                ORDER BY l.created_at DESC
            ''', (phone_number,))
            results = c.fetchall()

        return results
    except Exception as e:
        logger.error(f"Error getting lists: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_list_by_name(phone_number: str, list_name: str) -> Optional[tuple[int, str]]:
    """Find a list by name (case-insensitive)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT id, list_name FROM lists WHERE phone_hash = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_hash, list_name)
            )
            result = c.fetchone()
            if not result:
                # Fallback for lists created before encryption
                c.execute(
                    'SELECT id, list_name FROM lists WHERE phone_number = %s AND LOWER(list_name) = LOWER(%s)',
                    (phone_number, list_name)
                )
                result = c.fetchone()
        else:
            c.execute(
                'SELECT id, list_name FROM lists WHERE phone_number = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_number, list_name)
            )
            result = c.fetchone()

        if result:
            return result

        # Fallback: HTML-unescaped equality (issue #15 leftover encoded names)
        if list_name:
            if ENCRYPTION_ENABLED:
                from utils.encryption import hash_phone
                phone_hash = hash_phone(phone_number)
                c.execute(
                    'SELECT id, list_name FROM lists WHERE phone_hash = %s OR phone_number = %s',
                    (phone_hash, phone_number)
                )
            else:
                c.execute(
                    'SELECT id, list_name FROM lists WHERE phone_number = %s',
                    (phone_number,)
                )
            target = _norm_text(list_name)
            for row in c.fetchall():
                if _norm_text(row[1]) == target:
                    return row
        return None
    except Exception as e:
        logger.error(f"Error getting list by name: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_next_available_list_name(phone_number: str, base_name: str) -> str:
    """Get next available list name (e.g., 'Grocery list #2' if 'Grocery list' exists)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get all lists that start with the base name
        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT list_name FROM lists WHERE phone_hash = %s AND LOWER(list_name) LIKE LOWER(%s)',
                (phone_hash, f"{base_name}%")
            )
            results = c.fetchall()
            if not results:
                c.execute(
                    'SELECT list_name FROM lists WHERE phone_number = %s AND LOWER(list_name) LIKE LOWER(%s)',
                    (phone_number, f"{base_name}%")
                )
                results = c.fetchall()
        else:
            c.execute(
                'SELECT list_name FROM lists WHERE phone_number = %s AND LOWER(list_name) LIKE LOWER(%s)',
                (phone_number, f"{base_name}%")
            )
            results = c.fetchall()

        if not results:
            return base_name

        # Find the highest number suffix
        import re
        max_num = 1
        base_lower = base_name.lower()
        for (name,) in results:
            name_lower = name.lower()
            if name_lower == base_lower:
                max_num = max(max_num, 1)
            else:
                # Check for pattern like "Grocery list #2" or legacy "Grocery list 2"
                match = re.match(rf'{re.escape(base_lower)}\s*#?\s*(\d+)$', name_lower)
                if match:
                    max_num = max(max_num, int(match.group(1)))

        return f"{base_name} #{max_num + 1}"
    except Exception as e:
        logger.error(f"Error getting next available list name: {e}")
        return f"{base_name} #2"  # Fallback
    finally:
        if conn:
            return_db_connection(conn)


def get_list_by_id(list_id: int, phone_number: str) -> Optional[tuple[int, str]]:
    """Get a list by ID (with ownership check)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT id, list_name FROM lists WHERE id = %s AND phone_hash = %s',
                (list_id, phone_hash)
            )
        else:
            c.execute(
                'SELECT id, list_name FROM lists WHERE id = %s AND phone_number = %s',
                (list_id, phone_number)
            )

        result = c.fetchone()
        return result
    except Exception as e:
        logger.error(f"Error getting list by id: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_list_items(list_id: int) -> list[tuple[int, str, bool]]:
    """Get all items in a list"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'SELECT id, item_text, completed FROM list_items WHERE list_id = %s ORDER BY created_at',
            (list_id,)
        )
        results = c.fetchall()
        return results
    except Exception as e:
        logger.error(f"Error getting list items: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def add_list_item(list_id: int, phone_number: str, item_text: str) -> Optional[int]:
    """Add an item to a list"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import encrypt_field, hash_phone
            phone_hash = hash_phone(phone_number)
            item_text_encrypted = encrypt_field(item_text)
            c.execute(
                '''INSERT INTO list_items (list_id, phone_number, phone_hash, item_text, item_text_encrypted)
                   VALUES (%s, %s, %s, %s, %s) RETURNING id''',
                (list_id, phone_number, phone_hash, item_text, item_text_encrypted)
            )
        else:
            c.execute(
                'INSERT INTO list_items (list_id, phone_number, item_text) VALUES (%s, %s, %s) RETURNING id',
                (list_id, phone_number, item_text)
            )

        item_id = c.fetchone()[0]
        conn.commit()
        logger.info(f"Added item to list {list_id}")
        return item_id
    except Exception as e:
        logger.error(f"Error adding list item: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def mark_item_complete(phone_number: str, list_name: str, item_text: str) -> bool:
    """Mark an item as complete (case-insensitive match)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Find the list first
        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT id FROM lists WHERE phone_hash = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_hash, list_name)
            )
        else:
            c.execute(
                'SELECT id FROM lists WHERE phone_number = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_number, list_name)
            )

        list_result = c.fetchone()
        if not list_result:
            return False

        list_id = list_result[0]
        c.execute(
            '''UPDATE list_items SET completed = TRUE
               WHERE list_id = %s AND LOWER(item_text) = LOWER(%s) AND completed = FALSE''',
            (list_id, item_text)
        )
        updated = c.rowcount > 0
        conn.commit()
        return updated
    except Exception as e:
        logger.error(f"Error marking item complete: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def mark_item_incomplete(phone_number: str, list_name: str, item_text: str) -> bool:
    """Mark an item as incomplete (case-insensitive match)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Find the list first
        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT id FROM lists WHERE phone_hash = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_hash, list_name)
            )
        else:
            c.execute(
                'SELECT id FROM lists WHERE phone_number = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_number, list_name)
            )

        list_result = c.fetchone()
        if not list_result:
            return False

        list_id = list_result[0]
        c.execute(
            '''UPDATE list_items SET completed = FALSE
               WHERE list_id = %s AND LOWER(item_text) = LOWER(%s) AND completed = TRUE''',
            (list_id, item_text)
        )
        updated = c.rowcount > 0
        conn.commit()
        return updated
    except Exception as e:
        logger.error(f"Error marking item incomplete: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def find_item_in_any_list(phone_number: str, item_text: str) -> list[tuple[int, str, int, str]]:
    """Find an item across all user's lists (for check off without specifying list)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('''
                SELECT l.id, l.list_name, li.id as item_id, li.item_text
                FROM list_items li
                JOIN lists l ON li.list_id = l.id
                WHERE l.phone_hash = %s AND LOWER(li.item_text) = LOWER(%s) AND li.completed = FALSE
            ''', (phone_hash, item_text))
        else:
            c.execute('''
                SELECT l.id, l.list_name, li.id as item_id, li.item_text
                FROM list_items li
                JOIN lists l ON li.list_id = l.id
                WHERE l.phone_number = %s AND LOWER(li.item_text) = LOWER(%s) AND li.completed = FALSE
            ''', (phone_number, item_text))

        results = c.fetchall()
        return results
    except Exception as e:
        logger.error(f"Error finding item: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def delete_list_item(phone_number: str, list_name: str, item_text: str) -> bool:
    """Delete an item from a list"""
    # Resolve list via the same lookup as show/add so HTML-encoded leftover
    # names and encryption fallbacks stay consistent.
    list_info = get_list_by_name(phone_number, list_name)
    if not list_info:
        return False
    return delete_list_item_by_list_id(list_info[0], item_text)


def delete_list(phone_number: str, list_name: str) -> bool:
    """Delete an entire list and all its items"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        logger.info(f"delete_list called: phone={phone_number[-4:]}, list_name={list_name}, encryption={ENCRYPTION_ENABLED}")

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            # Try phone_hash first
            c.execute(
                'DELETE FROM lists WHERE phone_hash = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_hash, list_name)
            )
            if c.rowcount == 0:
                # Fallback to phone_number for lists created before encryption
                logger.info(f"No rows deleted with phone_hash, trying phone_number fallback")
                c.execute(
                    'DELETE FROM lists WHERE phone_number = %s AND LOWER(list_name) = LOWER(%s)',
                    (phone_number, list_name)
                )
        else:
            c.execute(
                'DELETE FROM lists WHERE phone_number = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_number, list_name)
            )

        deleted = c.rowcount > 0
        logger.info(f"Delete rowcount: {c.rowcount}, deleted={deleted}")
        conn.commit()
        if deleted:
            logger.info(f"Deleted list '{list_name}'")
        return deleted
    except Exception as e:
        logger.error(f"Error deleting list: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def rename_list(phone_number: str, old_name: str, new_name: str) -> bool:
    """Rename a list"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                '''UPDATE lists SET list_name = %s
                   WHERE phone_hash = %s AND LOWER(list_name) = LOWER(%s)''',
                (new_name, phone_hash, old_name)
            )
        else:
            c.execute(
                '''UPDATE lists SET list_name = %s
                   WHERE phone_number = %s AND LOWER(list_name) = LOWER(%s)''',
                (new_name, phone_number, old_name)
            )

        updated = c.rowcount > 0
        conn.commit()
        return updated
    except Exception as e:
        logger.error(f"Error renaming list: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def clear_list(phone_number: str, list_name: str) -> bool:
    """Remove all items from a list (but keep the list)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Find the list first
        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT id FROM lists WHERE phone_hash = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_hash, list_name)
            )
        else:
            c.execute(
                'SELECT id FROM lists WHERE phone_number = %s AND LOWER(list_name) = LOWER(%s)',
                (phone_number, list_name)
            )

        list_result = c.fetchone()
        if not list_result:
            return False

        list_id = list_result[0]
        c.execute('DELETE FROM list_items WHERE list_id = %s', (list_id,))
        conn.commit()
        logger.info(f"Cleared all items from list '{list_name}'")
        return True
    except Exception as e:
        logger.error(f"Error clearing list: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_list_count(phone_number: str) -> int:
    """Get the number of lists a user has"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            # Count from both phone_hash and phone_number to include pre-encryption lists
            c.execute('SELECT COUNT(*) FROM lists WHERE phone_hash = %s OR phone_number = %s', (phone_hash, phone_number))
        else:
            c.execute('SELECT COUNT(*) FROM lists WHERE phone_number = %s', (phone_number,))

        count = c.fetchone()[0]
        return count
    except Exception as e:
        logger.error(f"Error getting list count: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def get_item_count(list_id: int) -> int:
    """Get the number of items in a list"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute('SELECT COUNT(*) FROM list_items WHERE list_id = %s', (list_id,))
        count = c.fetchone()[0]
        return count
    except Exception as e:
        logger.error(f"Error getting item count: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def get_most_recent_list_item(phone_number: str) -> Optional[tuple[int, str, str, datetime]]:
    """Get the most recently added list item for a user (for undo functionality).

    Returns:
        tuple: (item_id, item_text, list_name, created_at) or None
    """
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                '''SELECT li.id, li.item_text, l.list_name, li.created_at
                   FROM list_items li
                   JOIN lists l ON li.list_id = l.id
                   WHERE (l.phone_hash = %s OR l.phone_number = %s)
                   ORDER BY li.created_at DESC
                   LIMIT 1''',
                (phone_hash, phone_number)
            )
        else:
            c.execute(
                '''SELECT li.id, li.item_text, l.list_name, li.created_at
                   FROM list_items li
                   JOIN lists l ON li.list_id = l.id
                   WHERE l.phone_number = %s
                   ORDER BY li.created_at DESC
                   LIMIT 1''',
                (phone_number,)
            )

        result = c.fetchone()
        if result:
            return (result[0], result[1], result[2], result[3])
        return None
    except Exception as e:
        logger.error(f"Error getting most recent list item: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def delete_list_item_by_id(item_id: int, phone_number: str) -> bool:
    """Delete a list item by its ID (for undo functionality)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                '''DELETE FROM list_items
                   WHERE id = %s AND list_id IN (
                       SELECT id FROM lists WHERE phone_hash = %s OR phone_number = %s
                   )''',
                (item_id, phone_hash, phone_number)
            )
        else:
            c.execute(
                '''DELETE FROM list_items
                   WHERE id = %s AND list_id IN (
                       SELECT id FROM lists WHERE phone_number = %s
                   )''',
                (item_id, phone_number)
            )

        deleted = c.rowcount > 0
        conn.commit()
        if deleted:
            logger.info(f"Deleted list item {item_id} via undo")
        return deleted
    except Exception as e:
        logger.error(f"Error deleting list item by id: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# SHARED LIST FUNCTIONS
# =====================================================


def share_list(owner_phone: str, list_id: int, shared_with_phone: str) -> tuple[bool, str]:
    """Share a list with another user. Returns (success, message)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Verify the list belongs to the owner
        c.execute('SELECT id, list_name FROM lists WHERE id = %s AND phone_number = %s', (list_id, owner_phone))
        list_row = c.fetchone()
        if not list_row:
            # Fallback for encryption
            if ENCRYPTION_ENABLED:
                from utils.encryption import hash_phone
                phone_hash = hash_phone(owner_phone)
                c.execute('SELECT id, list_name FROM lists WHERE id = %s AND phone_hash = %s', (list_id, phone_hash))
                list_row = c.fetchone()
            if not list_row:
                return False, "List not found."

        # Check if already shared with this user
        c.execute(
            'SELECT status FROM list_shares WHERE list_id = %s AND shared_with_phone = %s',
            (list_id, shared_with_phone)
        )
        existing = c.fetchone()
        if existing:
            status = existing[0]
            if status == 'accepted':
                return False, "This list is already shared with that user."
            elif status == 'pending':
                return False, "An invitation is already pending for that user."
            else:
                # Declined before — allow re-share by updating
                c.execute(
                    '''UPDATE list_shares SET status = 'pending', created_at = CURRENT_TIMESTAMP, accepted_at = NULL
                       WHERE list_id = %s AND shared_with_phone = %s''',
                    (list_id, shared_with_phone)
                )
                conn.commit()
                return True, "Invitation re-sent."

        # Check per-list member cap
        c.execute(
            "SELECT COUNT(*) FROM list_shares WHERE list_id = %s AND status IN ('pending', 'accepted')",
            (list_id,)
        )
        member_count = c.fetchone()[0]
        if member_count >= SHARED_LIST_MAX_MEMBERS:
            return False, f"This list already has {SHARED_LIST_MAX_MEMBERS} shared members (max)."

        # Check owner's total shared list count
        c.execute(
            """SELECT COUNT(DISTINCT list_id) FROM list_shares
               WHERE owner_phone = %s AND status IN ('pending', 'accepted')""",
            (owner_phone,)
        )
        owner_share_count = c.fetchone()[0]
        if owner_share_count >= SHARED_LIST_MAX_PER_USER:
            return False, f"You can share up to {SHARED_LIST_MAX_PER_USER} lists. You've reached the limit."

        # Check recipient's received share count
        c.execute(
            "SELECT COUNT(*) FROM list_shares WHERE shared_with_phone = %s AND status = 'accepted'",
            (shared_with_phone,)
        )
        received_count = c.fetchone()[0]
        if received_count >= SHARED_LIST_MAX_RECEIVED:
            return False, "That user has reached their shared list limit."

        # Create the share
        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            owner_hash = hash_phone(owner_phone)
            shared_hash = hash_phone(shared_with_phone)
            c.execute(
                '''INSERT INTO list_shares (list_id, owner_phone, shared_with_phone, owner_phone_hash, shared_with_phone_hash)
                   VALUES (%s, %s, %s, %s, %s)''',
                (list_id, owner_phone, shared_with_phone, owner_hash, shared_hash)
            )
        else:
            c.execute(
                'INSERT INTO list_shares (list_id, owner_phone, shared_with_phone) VALUES (%s, %s, %s)',
                (list_id, owner_phone, shared_with_phone)
            )

        conn.commit()
        logger.info(f"Shared list {list_id} with {shared_with_phone[-4:]}")
        return True, "Invitation sent."
    except Exception as e:
        logger.error(f"Error sharing list: {e}")
        return False, "Something went wrong. Please try again."
    finally:
        if conn:
            return_db_connection(conn)


def set_share_name(list_id: int, shared_with_phone: str, name: str) -> bool:
    """Set the owner-assigned name for a shared user."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'UPDATE list_shares SET shared_with_name = %s WHERE list_id = %s AND shared_with_phone = %s',
            (name, list_id, shared_with_phone)
        )
        conn.commit()
        return c.rowcount > 0
    except Exception as e:
        logger.error(f"Error setting share name: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def get_share_name(list_id: int, shared_with_phone: str) -> str | None:
    """Get the owner-assigned name for a shared user."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'SELECT shared_with_name FROM list_shares WHERE list_id = %s AND shared_with_phone = %s',
            (list_id, shared_with_phone)
        )
        row = c.fetchone()
        return row[0] if row else None
    except Exception as e:
        logger.error(f"Error getting share name: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_known_recipients(owner_phone: str, name: str) -> list[tuple[str, str]]:
    """Look up previous share recipients by name for this owner.
    Returns [(phone, name), ...] matching the given name (case-insensitive)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """SELECT DISTINCT shared_with_phone, shared_with_name
               FROM list_shares
               WHERE owner_phone = %s AND LOWER(shared_with_name) = LOWER(%s)""",
            (owner_phone, name)
        )
        return c.fetchall()
    except Exception as e:
        logger.error(f"Error looking up known recipients: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def accept_share(phone_number: str, list_id: int) -> tuple[bool, str]:
    """Accept a pending shared list invitation. Returns (success, message)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute(
            """UPDATE list_shares SET status = 'accepted', accepted_at = CURRENT_TIMESTAMP
               WHERE shared_with_phone = %s AND list_id = %s AND status = 'pending'""",
            (phone_number, list_id)
        )
        if c.rowcount == 0:
            return False, "No pending invitation found for this list."

        conn.commit()
        logger.info(f"User {phone_number[-4:]} accepted share for list {list_id}")
        return True, "Share accepted."
    except Exception as e:
        logger.error(f"Error accepting share: {e}")
        return False, "Something went wrong. Please try again."
    finally:
        if conn:
            return_db_connection(conn)


def decline_share(phone_number: str, list_id: int) -> tuple[bool, str]:
    """Decline a pending shared list invitation. Returns (success, message)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute(
            "DELETE FROM list_shares WHERE shared_with_phone = %s AND list_id = %s AND status = 'pending'",
            (phone_number, list_id)
        )
        if c.rowcount == 0:
            return False, "No pending invitation found."

        conn.commit()
        logger.info(f"User {phone_number[-4:]} declined share for list {list_id}")
        return True, "Invitation declined."
    except Exception as e:
        logger.error(f"Error declining share: {e}")
        return False, "Something went wrong. Please try again."
    finally:
        if conn:
            return_db_connection(conn)


def leave_shared_list(phone_number: str, list_id: int) -> tuple[bool, str]:
    """Leave a shared list. Returns (success, message)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute(
            "DELETE FROM list_shares WHERE shared_with_phone = %s AND list_id = %s AND status = 'accepted'",
            (phone_number, list_id)
        )
        if c.rowcount == 0:
            return False, "You're not a member of this shared list."

        conn.commit()
        logger.info(f"User {phone_number[-4:]} left shared list {list_id}")
        return True, "You've left the shared list."
    except Exception as e:
        logger.error(f"Error leaving shared list: {e}")
        return False, "Something went wrong. Please try again."
    finally:
        if conn:
            return_db_connection(conn)


def unshare_list(owner_phone: str, list_id: int, shared_with_phone: str) -> tuple[bool, str]:
    """Remove a specific user's access to a shared list. Returns (success, message)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute(
            'DELETE FROM list_shares WHERE list_id = %s AND owner_phone = %s AND shared_with_phone = %s',
            (list_id, owner_phone, shared_with_phone)
        )
        if c.rowcount == 0:
            return False, "That user doesn't have access to this list."

        conn.commit()
        logger.info(f"Unshared list {list_id} from {shared_with_phone[-4:]}")
        return True, "Access removed."
    except Exception as e:
        logger.error(f"Error unsharing list: {e}")
        return False, "Something went wrong. Please try again."
    finally:
        if conn:
            return_db_connection(conn)


def unshare_list_all(owner_phone: str, list_id: int) -> tuple[bool, str]:
    """Remove all sharing from a list. Returns (success, message)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute(
            'DELETE FROM list_shares WHERE list_id = %s AND owner_phone = %s',
            (list_id, owner_phone)
        )
        removed = c.rowcount
        if removed == 0:
            return False, "This list isn't shared with anyone."

        conn.commit()
        logger.info(f"Removed all {removed} shares from list {list_id}")
        return True, f"Stopped sharing with {removed} user{'s' if removed > 1 else ''}."
    except Exception as e:
        logger.error(f"Error unsharing list: {e}")
        return False, "Something went wrong. Please try again."
    finally:
        if conn:
            return_db_connection(conn)


def get_list_members(list_id: int) -> list[tuple[str, str, str, str | None]]:
    """Get all members of a shared list. Returns [(phone, status, permission, name), ...]."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute(
            """SELECT shared_with_phone, status, permission, shared_with_name
               FROM list_shares WHERE list_id = %s
               ORDER BY created_at""",
            (list_id,)
        )
        return c.fetchall()
    except Exception as e:
        logger.error(f"Error getting list members: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_pending_shares(phone_number: str) -> list[tuple[int, int, str, str]]:
    """Get pending share invitations for a user.
    Returns [(share_id, list_id, owner_phone, list_name), ...]."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute(
            """SELECT ls.id, ls.list_id, ls.owner_phone, l.list_name
               FROM list_shares ls
               JOIN lists l ON ls.list_id = l.id
               WHERE ls.shared_with_phone = %s AND ls.status = 'pending'
               ORDER BY ls.created_at DESC""",
            (phone_number,)
        )
        return c.fetchall()
    except Exception as e:
        logger.error(f"Error getting pending shares: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_shared_lists_for_user(phone_number: str) -> list[tuple[int, str, int, int, str]]:
    """Get all accepted shared lists for a user (lists others shared with them).
    Returns [(list_id, list_name, item_count, completed_count, owner_phone), ...]."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT l.id, l.list_name,
                   COUNT(li.id) as item_count,
                   SUM(CASE WHEN li.completed THEN 1 ELSE 0 END) as completed_count,
                   ls.owner_phone
            FROM list_shares ls
            JOIN lists l ON ls.list_id = l.id
            LEFT JOIN list_items li ON l.id = li.list_id
            WHERE ls.shared_with_phone = %s AND ls.status = 'accepted'
            GROUP BY l.id, l.list_name, ls.owner_phone
            ORDER BY l.created_at DESC
        ''', (phone_number,))
        return c.fetchall()
    except Exception as e:
        logger.error(f"Error getting shared lists: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def get_accessible_list_by_name(phone_number: str, list_name: str) -> Optional[tuple[int, str, bool, Optional[str]]]:
    """Find a list the user owns OR has shared access to.
    Returns (list_id, list_name, is_shared, owner_phone) or None.
    Checks own lists first, then shared lists."""
    # Check own lists first
    own_list = get_list_by_name(phone_number, list_name)
    if own_list:
        return (own_list[0], own_list[1], False, None)

    # Check shared lists
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute('''
            SELECT l.id, l.list_name, ls.owner_phone
            FROM list_shares ls
            JOIN lists l ON ls.list_id = l.id
            WHERE ls.shared_with_phone = %s AND ls.status = 'accepted'
              AND LOWER(l.list_name) = LOWER(%s)
        ''', (phone_number, list_name))
        result = c.fetchone()
        if result:
            return (result[0], result[1], True, result[2])

        # Fallback: HTML-unescaped equality for leftover encoded names
        if list_name:
            for lid, name, _count, _done, owner in get_shared_lists_for_user(phone_number):
                if _norm_text(name) == _norm_text(list_name):
                    return (lid, name, True, owner)
        return None
    except Exception as e:
        logger.error(f"Error getting accessible list: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def can_user_access_list(phone_number: str, list_id: int) -> tuple[bool, bool]:
    """Check if a user can access a list (owns it or has accepted share).
    Returns (has_access, is_owner)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Check ownership
        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('SELECT id FROM lists WHERE id = %s AND (phone_hash = %s OR phone_number = %s)',
                      (list_id, phone_hash, phone_number))
        else:
            c.execute('SELECT id FROM lists WHERE id = %s AND phone_number = %s', (list_id, phone_number))

        if c.fetchone():
            return True, True

        # Check shared access
        c.execute(
            "SELECT id FROM list_shares WHERE list_id = %s AND shared_with_phone = %s AND status = 'accepted'",
            (list_id, phone_number)
        )
        if c.fetchone():
            return True, False

        return False, False
    except Exception as e:
        logger.error(f"Error checking list access: {e}")
        return False, False
    finally:
        if conn:
            return_db_connection(conn)


def is_shared_list_read_only(list_id: int) -> tuple[bool, str | None]:
    """Check if a shared list is read-only because the owner is no longer Premium.
    Returns (is_read_only, owner_first_name)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Get owner phone
        c.execute('SELECT phone_number FROM lists WHERE id = %s', (list_id,))
        row = c.fetchone()
        if not row:
            return False, None
        owner_phone = row[0]

        # Check owner's tier
        from services.tier_service import get_user_tier
        tier = get_user_tier(owner_phone)
        if tier != 'premium':
            from models.user import get_user
            owner = get_user(owner_phone)
            owner_name = owner[1] if owner else None
            return True, owner_name

        return False, None
    except Exception as e:
        logger.error(f"Error checking shared list read-only: {e}")
        return False, None
    finally:
        if conn:
            return_db_connection(conn)


def get_share_count_for_list(list_id: int) -> int:
    """Get the number of users a list is shared with (pending + accepted)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            "SELECT COUNT(*) FROM list_shares WHERE list_id = %s AND status IN ('pending', 'accepted')",
            (list_id,)
        )
        return c.fetchone()[0]
    except Exception as e:
        logger.error(f"Error getting share count: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def get_owner_shared_list_count(owner_phone: str) -> int:
    """Get the number of distinct lists an owner has shared."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """SELECT COUNT(DISTINCT list_id) FROM list_shares
               WHERE owner_phone = %s AND status IN ('pending', 'accepted')""",
            (owner_phone,)
        )
        return c.fetchone()[0]
    except Exception as e:
        logger.error(f"Error getting owner share count: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def mark_item_complete_by_list_id(list_id: int, item_text: str) -> bool:
    """Mark an item complete by list_id (for shared list access without ownership check)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''UPDATE list_items SET completed = TRUE
               WHERE list_id = %s AND LOWER(item_text) = LOWER(%s) AND completed = FALSE''',
            (list_id, item_text)
        )
        updated = c.rowcount > 0
        conn.commit()
        return updated
    except Exception as e:
        logger.error(f"Error marking item complete by list_id: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def mark_item_incomplete_by_list_id(list_id: int, item_text: str) -> bool:
    """Mark an item incomplete by list_id (for shared list access)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            '''UPDATE list_items SET completed = FALSE
               WHERE list_id = %s AND LOWER(item_text) = LOWER(%s) AND completed = TRUE''',
            (list_id, item_text)
        )
        updated = c.rowcount > 0
        conn.commit()
        return updated
    except Exception as e:
        logger.error(f"Error marking item incomplete by list_id: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def delete_list_item_by_list_id(list_id: int, item_text: str) -> bool:
    """Delete an item by list_id and text (for shared list access)."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'DELETE FROM list_items WHERE list_id = %s AND LOWER(item_text) = LOWER(%s)',
            (list_id, item_text)
        )
        if c.rowcount == 0 and item_text:
            # Exact SQL miss — HTML-unescaped equality on this list's items
            target = _norm_text(item_text)
            c.execute('SELECT id, item_text FROM list_items WHERE list_id = %s', (list_id,))
            matches = [row for row in c.fetchall() if _norm_text(row[1]) == target]
            if len(matches) == 1:
                c.execute('DELETE FROM list_items WHERE id = %s', (matches[0][0],))
        deleted = c.rowcount > 0
        conn.commit()
        return deleted
    except Exception as e:
        logger.error(f"Error deleting list item by list_id: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def match_item_in_list(
    list_id: int,
    item_text: str,
    exact_only: bool = False
) -> Optional[tuple[int, str, bool]]:
    """Find an item in a list by exact (HTML-unescaped) match, then unique partial.

    Returns (item_id, item_text, completed) or None.
    """
    if not item_text:
        return None
    items = get_list_items(list_id)
    target = _norm_text(item_text)
    if not target:
        return None

    exact = [i for i in items if _norm_text(i[1]) == target]
    if exact:
        return exact[0]
    if exact_only or len(target) < _PARTIAL_MATCH_MIN:
        return None

    partial = [
        i for i in items
        if target in _norm_text(i[1]) or _norm_text(i[1]) in target
    ]
    if len(partial) == 1:
        return partial[0]
    return None


def _iter_accessible_lists(phone_number: str):
    """Yield (list_id, list_name, is_shared, owner_phone) for owned + accepted shared lists."""
    for lid, name, *_rest in get_lists(phone_number):
        yield lid, name, False, None
    for lid, name, _count, _done, owner in get_shared_lists_for_user(phone_number):
        yield lid, name, True, owner


def _unique_partial_list(phone_number: str, fragment: str) -> Optional[tuple[int, str, bool, Optional[str]]]:
    """Unique partial/substring list-name match across owned + shared lists."""
    frag = _norm_text(fragment)
    if len(frag) < _PARTIAL_MATCH_MIN:
        return None
    matches = []
    for lid, name, is_shared, owner in _iter_accessible_lists(phone_number):
        n = _norm_text(name)
        if frag == n or frag in n or n in frag:
            matches.append((lid, name, is_shared, owner))
    if len(matches) == 1:
        return matches[0]
    return None


def resolve_item_for_delete(
    phone_number: str,
    list_name: Optional[str] = None,
    item_text: Optional[str] = None,
    last_active_list: Optional[str] = None,
) -> Optional[dict[str, Any]]:
    """Resolve which list item a delete request refers to.

    Returns:
        None if nothing matched.
        {'ambiguous': True} if the item exists in more than one list.
        {'list_id', 'list_name', 'item_text', 'is_shared'} on a unique match.
    """
    if not item_text:
        return None

    accessible = None
    used_explicit_list = False
    if list_name:
        accessible = get_accessible_list_by_name(phone_number, list_name)
        if not accessible:
            accessible = _unique_partial_list(phone_number, list_name)
        used_explicit_list = accessible is not None

    if not accessible and last_active_list and last_active_list not in _CONTEXT_MARKERS:
        accessible = get_accessible_list_by_name(phone_number, last_active_list)
        if not accessible:
            accessible = _unique_partial_list(phone_number, last_active_list)

    if accessible:
        list_id, actual_name, is_shared, _owner = accessible
        item = match_item_in_list(list_id, item_text)
        if item:
            return {
                'list_id': list_id,
                'list_name': actual_name,
                'item_text': item[1],
                'is_shared': is_shared,
            }
        if used_explicit_list:
            return None

    matches = []
    for lid, name, is_shared, _owner in _iter_accessible_lists(phone_number):
        item = match_item_in_list(lid, item_text)
        if item:
            matches.append((lid, name, is_shared, item[1]))
    if len(matches) == 1:
        lid, name, is_shared, actual_item = matches[0]
        return {
            'list_id': lid,
            'list_name': name,
            'item_text': actual_item,
            'is_shared': is_shared,
        }
    if len(matches) > 1:
        return {'ambiguous': True}
    return None


def delete_list_item_from_pending(phone_number: str, delete_data: dict) -> bool:
    """Delete a list item from a pending confirmation/selection payload.

    Prefers list_id (works for owned and shared lists), then item id (undo),
    then name+text lookup. Accepts both `list_id` and legacy `shared_list_id`.
    """
    if not delete_data:
        return False

    list_id = delete_data.get('list_id') or delete_data.get('shared_list_id')
    item_text = delete_data.get('text')
    if list_id is not None and item_text:
        if delete_list_item_by_list_id(list_id, item_text):
            return True
        matched = match_item_in_list(list_id, item_text)
        if matched and delete_list_item_by_list_id(list_id, matched[1]):
            return True

    item_id = delete_data.get('id')
    if item_id and delete_list_item_by_id(item_id, phone_number):
        return True

    list_name = delete_data.get('list_name')
    if list_name and item_text:
        return delete_list_item(phone_number, list_name, item_text)
    return False
