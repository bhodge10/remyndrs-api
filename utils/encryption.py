"""
Field-Level Encryption Utilities
AES-256-GCM for field encryption, HMAC-SHA256 for phone number hashing
"""

import os
import base64
import hashlib
import hmac
from typing import Tuple

from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from config import logger

# Keys loaded from environment
_encryption_key = None
_hash_key = None


def _get_encryption_key() -> bytes:
    """Get or initialize the AES-256 encryption key"""
    global _encryption_key
    if _encryption_key is None:
        key_b64 = os.environ.get("ENCRYPTION_KEY")
        if not key_b64:
            raise ValueError("ENCRYPTION_KEY environment variable not set")
        _encryption_key = base64.b64decode(key_b64)
        if len(_encryption_key) != 32:
            raise ValueError("ENCRYPTION_KEY must be 32 bytes (256 bits) when decoded")
    return _encryption_key


def _get_hash_key() -> bytes:
    """Get or initialize the HMAC hash key"""
    global _hash_key
    if _hash_key is None:
        key_b64 = os.environ.get("HASH_KEY")
        if not key_b64:
            raise ValueError("HASH_KEY environment variable not set")
        _hash_key = base64.b64decode(key_b64)
    return _hash_key


def generate_keys() -> Tuple[bytes, bytes]:
    """Generate new encryption and hash keys (run once for setup).
    Returns the raw key bytes. Caller must securely store them."""
    encryption_key = os.urandom(32)  # 256 bits for AES-256
    hash_key = os.urandom(32)  # 256 bits for HMAC

    logger.info("Generated new encryption and hash keys — store them securely in environment variables")
    return encryption_key, hash_key


def encrypt_field(plaintext: str) -> str:
    """
    Encrypt a field using AES-256-GCM
    Returns base64-encoded string: nonce + ciphertext + tag
    """
    if not plaintext:
        return ""

    try:
        key = _get_encryption_key()
        aesgcm = AESGCM(key)

        # Generate random 12-byte nonce (recommended for GCM)
        nonce = os.urandom(12)

        # Encrypt (GCM automatically appends auth tag)
        ciphertext = aesgcm.encrypt(nonce, plaintext.encode('utf-8'), None)

        # Combine nonce + ciphertext and base64 encode
        encrypted = base64.b64encode(nonce + ciphertext).decode('utf-8')
        return encrypted

    except Exception as e:
        logger.error(f"Encryption error: {e}")
        raise


def decrypt_field(encrypted: str) -> str:
    """
    Decrypt a field encrypted with AES-256-GCM
    Expects base64-encoded string: nonce + ciphertext + tag
    """
    if not encrypted:
        return ""

    try:
        key = _get_encryption_key()
        aesgcm = AESGCM(key)

        # Decode from base64
        data = base64.b64decode(encrypted)

        # Extract nonce (first 12 bytes) and ciphertext
        nonce = data[:12]
        ciphertext = data[12:]

        # Decrypt
        plaintext = aesgcm.decrypt(nonce, ciphertext, None)
        return plaintext.decode('utf-8')

    except Exception as e:
        logger.error(f"Decryption error: {e}")
        raise


def hash_phone(phone_number: str) -> str:
    """
    Create HMAC-SHA256 hash of phone number for lookups
    Returns hex-encoded hash string
    """
    if not phone_number:
        return ""

    try:
        key = _get_hash_key()
        # Normalize phone number (remove non-digits)
        normalized = ''.join(c for c in phone_number if c.isdigit())

        # Create HMAC-SHA256
        h = hmac.new(key, normalized.encode('utf-8'), hashlib.sha256)
        return h.hexdigest()

    except Exception as e:
        logger.error(f"Hash error: {e}")
        raise


def safe_decrypt(encrypted: str, fallback: str = "") -> str:
    """
    Safely decrypt a field, returning fallback if decryption fails.
    Useful during migration when some fields may not be encrypted yet.
    """
    if not encrypted:
        return fallback

    try:
        return decrypt_field(encrypted)
    except Exception as e:
        # Field might not be encrypted yet (during migration)
        logger.warning(f"Decryption failed (possibly unencrypted migration data): {type(e).__name__}")
        return encrypted if encrypted else fallback


def is_encrypted(value: str) -> bool:
    """
    Check if a value appears to be encrypted (base64 with correct length)
    Used during migration to avoid double-encryption
    """
    if not value:
        return False

    try:
        decoded = base64.b64decode(value)
        # Encrypted values should have at least nonce (12) + some ciphertext + tag (16)
        return len(decoded) >= 28
    except (ValueError, Exception):
        return False
