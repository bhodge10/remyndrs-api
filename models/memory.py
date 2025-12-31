"""
Memory Model
Handles all memory-related database operations
"""

import json
from database import get_db_connection, return_db_connection
from config import logger, ENCRYPTION_ENABLED

def save_memory(phone_number, memory_text, parsed_data):
    """Save a new memory to the database with optional encryption"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import encrypt_field, hash_phone
            phone_hash = hash_phone(phone_number)
            memory_text_encrypted = encrypt_field(memory_text)
            c.execute(
                '''INSERT INTO memories (phone_number, phone_hash, memory_text, memory_text_encrypted, parsed_data)
                   VALUES (%s, %s, %s, %s, %s)''',
                (phone_number, phone_hash, memory_text, memory_text_encrypted, json.dumps(parsed_data))
            )
        else:
            c.execute(
                'INSERT INTO memories (phone_number, memory_text, parsed_data) VALUES (%s, %s, %s)',
                (phone_number, memory_text, json.dumps(parsed_data))
            )

        conn.commit()
        logger.info(f"Saved memory for user")
    except Exception as e:
        logger.error(f"Error saving memory: {e}")
    finally:
        if conn:
            return_db_connection(conn)

def get_memories(phone_number):
    """Get all memories for a user"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT id, memory_text, parsed_data, created_at FROM memories WHERE phone_hash = %s ORDER BY created_at DESC',
                (phone_hash,)
            )
            results = c.fetchall()
            if not results:
                # Fallback for data created before encryption
                c.execute(
                    'SELECT id, memory_text, parsed_data, created_at FROM memories WHERE phone_number = %s ORDER BY created_at DESC',
                    (phone_number,)
                )
                results = c.fetchall()
        else:
            c.execute(
                'SELECT id, memory_text, parsed_data, created_at FROM memories WHERE phone_number = %s ORDER BY created_at DESC',
                (phone_number,)
            )
            results = c.fetchall()

        return results
    except Exception as e:
        logger.error(f"Error getting memories: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)

def delete_all_memories(phone_number):
    """Delete all memories for a user"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        # Delete by both phone_hash and phone_number to catch all records
        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('DELETE FROM memories WHERE phone_hash = %s OR phone_number = %s', (phone_hash, phone_number))
        else:
            c.execute('DELETE FROM memories WHERE phone_number = %s', (phone_number,))

        conn.commit()
        logger.info(f"Deleted all memories for user")
    except Exception as e:
        logger.error(f"Error deleting memories: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def search_memories(phone_number, search_term):
    """Search memories by keyword (case-insensitive)"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        search_pattern = f'%{search_term}%'

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                '''SELECT id, memory_text, created_at FROM memories
                   WHERE phone_hash = %s AND LOWER(memory_text) LIKE LOWER(%s)
                   ORDER BY created_at DESC''',
                (phone_hash, search_pattern)
            )
            results = c.fetchall()
            if not results:
                # Fallback for memories created before encryption
                c.execute(
                    '''SELECT id, memory_text, created_at FROM memories
                       WHERE phone_number = %s AND LOWER(memory_text) LIKE LOWER(%s)
                       ORDER BY created_at DESC''',
                    (phone_number, search_pattern)
                )
                results = c.fetchall()
        else:
            c.execute(
                '''SELECT id, memory_text, created_at FROM memories
                   WHERE phone_number = %s AND LOWER(memory_text) LIKE LOWER(%s)
                   ORDER BY created_at DESC''',
                (phone_number, search_pattern)
            )
            results = c.fetchall()

        return results
    except Exception as e:
        logger.error(f"Error searching memories: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


def delete_memory(phone_number, memory_id):
    """Delete a specific memory by ID"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            # Only delete if it belongs to this user
            c.execute(
                'DELETE FROM memories WHERE id = %s AND phone_hash = %s',
                (memory_id, phone_hash)
            )
            if c.rowcount == 0:
                # Fallback for memories created before encryption
                c.execute(
                    'DELETE FROM memories WHERE id = %s AND phone_number = %s',
                    (memory_id, phone_number)
                )
        else:
            c.execute(
                'DELETE FROM memories WHERE id = %s AND phone_number = %s',
                (memory_id, phone_number)
            )

        deleted = c.rowcount > 0
        conn.commit()
        if deleted:
            logger.info(f"Deleted memory {memory_id}")
        return deleted
    except Exception as e:
        logger.error(f"Error deleting memory: {e}")
        return False
    finally:
        if conn:
            return_db_connection(conn)
