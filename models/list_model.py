"""
List Model
Handles all list-related database operations
"""

from database import get_db_connection, return_db_connection
from config import logger, ENCRYPTION_ENABLED


def create_list(phone_number, list_name):
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


def get_lists(phone_number):
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


def get_list_by_name(phone_number, list_name):
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

        return result
    except Exception as e:
        logger.error(f"Error getting list by name: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def get_next_available_list_name(phone_number, base_name):
    """Get next available list name (e.g., 'Grocery list 2' if 'Grocery list' exists)"""
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
                # Check for pattern like "Grocery list 2"
                match = re.match(rf'{re.escape(base_lower)}\s+(\d+)$', name_lower)
                if match:
                    max_num = max(max_num, int(match.group(1)))

        return f"{base_name} {max_num + 1}"
    except Exception as e:
        logger.error(f"Error getting next available list name: {e}")
        return f"{base_name} 2"  # Fallback
    finally:
        if conn:
            return_db_connection(conn)


def get_list_by_id(list_id, phone_number):
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


def get_list_items(list_id):
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


def add_list_item(list_id, phone_number, item_text):
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


def mark_item_complete(phone_number, list_name, item_text):
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


def mark_item_incomplete(phone_number, list_name, item_text):
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


def find_item_in_any_list(phone_number, item_text):
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


def delete_list_item(phone_number, list_name, item_text):
    """Delete an item from a list"""
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
            'DELETE FROM list_items WHERE list_id = %s AND LOWER(item_text) = LOWER(%s)',
            (list_id, item_text)
        )
        deleted = c.rowcount > 0
        conn.commit()
        return deleted
    except Exception as e:
        logger.error(f"Error deleting list item: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)


def delete_list(phone_number, list_name):
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


def rename_list(phone_number, old_name, new_name):
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


def clear_list(phone_number, list_name):
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


def get_list_count(phone_number):
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


def get_item_count(list_id):
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
