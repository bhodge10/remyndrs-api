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
                'SELECT memory_text, parsed_data, created_at FROM memories WHERE phone_hash = %s ORDER BY created_at DESC',
                (phone_hash,)
            )
        else:
            c.execute(
                'SELECT memory_text, parsed_data, created_at FROM memories WHERE phone_number = %s ORDER BY created_at DESC',
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

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute('DELETE FROM memories WHERE phone_hash = %s', (phone_hash,))
        else:
            c.execute('DELETE FROM memories WHERE phone_number = %s', (phone_number,))

        conn.commit()
        logger.info(f"Deleted all memories for user")
    except Exception as e:
        logger.error(f"Error deleting memories: {e}")
    finally:
        if conn:
            return_db_connection(conn)
