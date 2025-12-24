"""
Memory Model
Handles all memory-related database operations
"""

import json
from database import get_db_connection
from config import logger

def save_memory(phone_number, memory_text, parsed_data):
    """Save a new memory to the database"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'INSERT INTO memories (phone_number, memory_text, parsed_data) VALUES (%s, %s, %s)',
            (phone_number, memory_text, json.dumps(parsed_data))
        )
        conn.commit()
        conn.close()
        logger.info(f"Saved memory for {phone_number}")
    except Exception as e:
        logger.error(f"Error saving memory: {e}")

def get_memories(phone_number):
    """Get all memories for a user"""
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            'SELECT memory_text, parsed_data, created_at FROM memories WHERE phone_number = %s ORDER BY created_at DESC',
            (phone_number,)
        )
        results = c.fetchall()
        conn.close()
        return results
    except Exception as e:
        logger.error(f"Error getting memories: {e}")
        return []
