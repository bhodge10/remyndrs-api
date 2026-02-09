"""
Pytest configuration and shared fixtures for Remyndrs testing.
Provides mocks for SMS, AI, and database, plus a conversation simulator.
"""

import os
import sys

# ============================================================
# CRITICAL: Load .env.test BEFORE any other imports
# This must happen before config.py is loaded
# ============================================================

# Try to load .env.test file
_env_test_path = os.path.join(os.path.dirname(os.path.dirname(__file__)), '.env.test')
if os.path.exists(_env_test_path):
    with open(_env_test_path) as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith('#') and '=' in line:
                key, value = line.split('=', 1)
                os.environ.setdefault(key.strip(), value.strip())

# Set defaults for any missing variables
os.environ.setdefault("ENVIRONMENT", "development")
os.environ.setdefault("TWILIO_ACCOUNT_SID", "test_sid")
os.environ.setdefault("TWILIO_AUTH_TOKEN", "test_token")
os.environ.setdefault("TWILIO_PHONE_NUMBER", "+15551234567")
os.environ.setdefault("OPENAI_API_KEY", "sk-test-key-not-real")
os.environ.setdefault("DATABASE_URL", "postgresql://localhost/remyndrs_test")
os.environ.setdefault("ADMIN_PASSWORD", "test_admin_password")
os.environ.setdefault("PUBLIC_PHONE_NUMBER", "+15551234567")

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Add custom library path (for Windows long path workaround)
if os.path.exists("C:/pylibs"):
    sys.path.insert(0, "C:/pylibs")

import pytest
from unittest.mock import patch, MagicMock, AsyncMock
from datetime import datetime, timedelta
from collections import defaultdict


class SMSCapture:
    """Captures outbound SMS messages instead of sending them."""

    def __init__(self):
        self.messages = []
        self.call_count = 0

    def send_sms(self, to_number, message, media_url=None):
        """Capture SMS instead of sending via Twilio."""
        self.messages.append({
            "to": to_number,
            "message": message,
            "media_url": media_url,
            "timestamp": datetime.utcnow()
        })
        self.call_count += 1

    def get_last_message(self):
        """Get the most recent message sent."""
        return self.messages[-1] if self.messages else None

    def get_messages_to(self, phone_number):
        """Get all messages sent to a specific number."""
        return [m for m in self.messages if m["to"] == phone_number]

    def clear(self):
        """Clear captured messages."""
        self.messages = []
        self.call_count = 0

    def __len__(self):
        return len(self.messages)


class AIResponseMock:
    """Mock AI responses for testing without calling OpenAI."""

    def __init__(self):
        self.responses = {}
        self.default_responses = self._build_default_responses()
        self.call_history = []

    def _build_default_responses(self):
        """Build pattern-based default responses."""
        return {
            # Reminder patterns
            r"remind.*tomorrow.*(\d+).*pm": {
                "action": "reminder",
                "reminder_text": "Test reminder",
                "reminder_date": (datetime.utcnow() + timedelta(days=1)).strftime("%Y-%m-%d 14:00:00")
            },
            r"remind.*in\s+(\d+)\s+minute": {
                "action": "reminder_relative",
                "reminder_text": "Test reminder",
                "offset_minutes": 30
            },
            r"remind.*every\s+day": {
                "action": "reminder_recurring",
                "reminder_text": "Daily reminder",
                "recurrence_type": "daily",
                "time": "09:00"
            },
            r"remind.*at\s+(\d+):?(\d*)(?!\s*(am|pm))": {
                "action": "clarify_time",
                "reminder_text": "Test reminder",
                "time_mentioned": "4:00"
            },
            # Memory patterns
            r"remember|store|save.*that": {
                "action": "store",
                "memory_text": "Test memory"
            },
            r"what.*stored|recall|my\s+memories": {
                "action": "retrieve",
                "query": "all"
            },
            # List patterns
            r"create.*list|new.*list": {
                "action": "create_list",
                "list_name": "Test List"
            },
            r"add.*to.*list|put.*on.*list": {
                "action": "add_to_list",
                "item_text": "test item",
                "list_name": None
            },
            # Delete patterns
            r"delete.*reminder": {
                "action": "delete",
                "delete_type": "reminder",
                "query": "test"
            },
            # Chitchat/unknown
            r"hello|hi|hey": {
                "action": "chitchat",
                "response": "Hello! How can I help you today?"
            }
        }

    def set_response(self, message_pattern, response):
        """Set a custom response for a specific message pattern."""
        self.responses[message_pattern.lower()] = response

    def get_response(self, message, phone_number, context=None):
        """Get AI response for a message."""
        import re
        message_lower = message.lower()

        self.call_history.append({
            "message": message,
            "phone_number": phone_number,
            "context": context,
            "timestamp": datetime.utcnow()
        })

        # Check custom responses first (exact match)
        if message_lower in self.responses:
            return self.responses[message_lower]

        # Also try de-normalized form: main.py normalizes "3pm" → "3:PM" before
        # calling AI, so we reverse that to match the original registered key.
        # Remove the colon that main.py inserts: "3:pm" → "3pm", "10:am" → "10am"
        denormalized = re.sub(r'(\d+):(\s*)(am|pm)', r'\1\3', message_lower)
        if denormalized != message_lower and denormalized in self.responses:
            return self.responses[denormalized]

        # Check pattern-based defaults
        for pattern, response in self.default_responses.items():
            if re.search(pattern, message_lower):
                # Deep copy to avoid mutation
                return dict(response)

        # Default fallback
        return {
            "action": "unknown",
            "response": "I'm not sure what you'd like me to do."
        }

    def clear_history(self):
        """Clear call history."""
        self.call_history = []


class ConversationSimulator:
    """
    Simulates user conversations with the SMS service.
    Allows sending messages and capturing responses without real SMS/AI calls.
    """

    def __init__(self, sms_capture, ai_mock, db_connection=None):
        self.sms_capture = sms_capture
        self.ai_mock = ai_mock
        self.db_connection = db_connection
        self.users = {}  # phone -> user state

    async def send_message(self, phone_number, message):
        """
        Simulate sending an SMS message from a user.
        Returns the response from the service.
        """
        from main import sms_reply
        from fastapi import Request

        # Create mock request
        request = AsyncMock(spec=Request)
        request.headers = {"X-Twilio-Signature": "test"}
        request.url = "http://localhost:8000/sms"
        request.form = AsyncMock(return_value={
            "Body": message,
            "From": phone_number
        })
        request.client = MagicMock()
        request.client.host = "127.0.0.1"

        # Call the webhook handler
        response = await sms_reply(request, Body=message, From=phone_number)

        # Extract response text from TwiML
        response_text = self._extract_twiml_message(response)

        return {
            "input": message,
            "output": response_text,
            "raw_response": response
        }

    def _extract_twiml_message(self, response):
        """Extract message text from TwiML response."""
        import re
        content = response.body.decode() if hasattr(response.body, 'decode') else str(response.body)
        # Parse <Message>text</Message> from TwiML
        match = re.search(r'<Message>(.*?)</Message>', content, re.DOTALL)
        if match:
            return match.group(1).strip()
        return content

    async def run_conversation(self, phone_number, messages):
        """
        Run a sequence of messages and collect all responses.

        Args:
            phone_number: The simulated user's phone number
            messages: List of message strings to send in sequence

        Returns:
            List of {input, output} dicts for each message
        """
        results = []
        for message in messages:
            result = await self.send_message(phone_number, message)
            results.append(result)
        return results

    async def complete_onboarding(self, phone_number, first_name="Test", last_name="User",
                                   email="test@example.com", zip_code="10001"):
        """
        Complete the onboarding flow for a test user.
        Returns the conversation history.
        """
        messages = [
            "START",  # Initial trigger
            first_name,  # First name
            last_name,  # Last name
            email,  # Email
            zip_code  # ZIP code
        ]
        return await self.run_conversation(phone_number, messages)


@pytest.fixture
def sms_capture():
    """Fixture providing SMS capture for tests."""
    capture = SMSCapture()

    def mock_delayed_sms_apply_async(args=None, kwargs=None, countdown=None):
        """Mock for send_delayed_sms.apply_async - captures the SMS immediately"""
        if args:
            to_number = args[0]
            message = args[1] if len(args) > 1 else ""
            media_url = kwargs.get('media_url') if kwargs else None
            capture.send_sms(to_number, message, media_url=media_url)
        return None

    def mock_engagement_nudge_apply_async(args=None, kwargs=None, countdown=None):
        """Mock for send_engagement_nudge.apply_async - no-op in tests"""
        return None

    # Patch send_sms in ALL modules that import it to prevent real Twilio calls
    # Each module that does "from services.sms_service import send_sms" gets its own reference
    with patch('services.sms_service.send_sms', side_effect=capture.send_sms), \
         patch('services.first_action_service.send_sms', side_effect=capture.send_sms), \
         patch('services.reminder_service.send_sms', side_effect=capture.send_sms), \
         patch('services.support_service.send_sms', side_effect=capture.send_sms), \
         patch('services.onboarding_recovery_service.send_sms', side_effect=capture.send_sms), \
         patch('services.onboarding_service.send_sms', side_effect=capture.send_sms), \
         patch('tasks.reminder_tasks.send_sms', side_effect=capture.send_sms), \
         patch('services.onboarding_service.send_delayed_sms.apply_async', side_effect=mock_delayed_sms_apply_async), \
         patch('services.onboarding_service.send_engagement_nudge.apply_async', side_effect=mock_engagement_nudge_apply_async), \
         patch('main.send_sms', side_effect=capture.send_sms), \
         patch('admin_dashboard.send_sms', side_effect=capture.send_sms):
        yield capture


@pytest.fixture
def ai_mock():
    """Fixture providing AI response mock for tests."""
    mock = AIResponseMock()

    def mock_process_with_ai(message, phone_number, context=None):
        return mock.get_response(message, phone_number, context)

    with patch('services.ai_service.process_with_ai', side_effect=mock_process_with_ai), \
         patch('main.process_with_ai', side_effect=mock_process_with_ai):
        yield mock


@pytest.fixture(autouse=True)
def disable_openai_globally():
    """
    CRITICAL: Disable ALL OpenAI API calls for ALL tests by default.
    This prevents API charges from any test that might trigger AI processing.

    Set USE_REAL_OPENAI=true environment variable to allow real OpenAI calls
    (useful for AI accuracy testing).

    This patches the OpenAI client to return mock responses.
    """
    # Allow real OpenAI if explicitly requested
    if os.environ.get("USE_REAL_OPENAI", "").lower() == "true":
        yield None
        return

    mock_response = MagicMock()
    mock_response.choices = [MagicMock()]
    mock_response.choices[0].message.content = '{"action": "unknown", "response": "Mock AI response"}'
    mock_response.usage = MagicMock()
    mock_response.usage.prompt_tokens = 10
    mock_response.usage.completion_tokens = 10
    mock_response.usage.total_tokens = 20

    def mock_create(*args, **kwargs):
        return mock_response

    # Patch at the OpenAI client level
    with patch('openai.OpenAI') as mock_openai_class:
        mock_client = MagicMock()
        mock_client.chat.completions.create = mock_create
        mock_openai_class.return_value = mock_client
        yield mock_client


@pytest.fixture
def simulator(sms_capture, ai_mock):
    """Fixture providing conversation simulator."""
    return ConversationSimulator(sms_capture, ai_mock)


@pytest.fixture
def real_ai_simulator(sms_capture):
    """Simulator using real OpenAI — no ai_mock patching."""
    return ConversationSimulator(sms_capture, ai_mock=None)


@pytest.fixture
def test_phone():
    """Fixture providing a consistent test phone number."""
    return "+15559876543"


@pytest.fixture
def onboarded_user(test_phone):
    """
    Fixture that creates an onboarded test user in the database.
    Cleans up BEFORE and AFTER test to ensure clean slate.
    """
    from models.user import create_or_update_user, get_user
    from database import get_db_connection, return_db_connection

    # CLEANUP BEFORE: Ensure no leftover data (especially support tickets!)
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Delete in order respecting foreign keys
        c.execute("DELETE FROM conversation_analysis WHERE log_id IN (SELECT id FROM logs WHERE phone_number = %s)", (test_phone,))
        c.execute("DELETE FROM list_items WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM lists WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM reminders WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM recurring_reminders WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM memories WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM support_tickets WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM logs WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM users WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM onboarding_progress WHERE phone_number = %s", (test_phone,))
        conn.commit()
    except Exception as e:
        print(f"Pre-cleanup error: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            return_db_connection(conn)

    # Create the user
    create_or_update_user(
        test_phone,
        first_name="Test",
        last_name="User",
        email="test@example.com",
        zip_code="10001",
        timezone="America/New_York",
        onboarding_complete=True
    )

    yield {
        "phone": test_phone,
        "first_name": "Test",
        "last_name": "User",
        "email": "test@example.com",
        "zip_code": "10001",
        "timezone": "America/New_York"
    }

    # Cleanup: Delete the test user and related data
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        # Delete in order respecting foreign keys
        c.execute("DELETE FROM conversation_analysis WHERE log_id IN (SELECT id FROM logs WHERE phone_number = %s)", (test_phone,))
        c.execute("DELETE FROM list_items WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM lists WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM reminders WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM recurring_reminders WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM memories WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM support_tickets WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM logs WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM users WHERE phone_number = %s", (test_phone,))
        c.execute("DELETE FROM onboarding_progress WHERE phone_number = %s", (test_phone,))
        conn.commit()
    except Exception as e:
        print(f"Cleanup error: {e}")
        if conn:
            conn.rollback()
    finally:
        if conn:
            return_db_connection(conn)


@pytest.fixture
def clean_test_user(test_phone):
    """
    Fixture that ensures a clean slate for the test phone number.
    Does NOT create a user - just ensures cleanup before and after.
    """
    from database import get_db_connection, return_db_connection

    def cleanup():
        conn = None
        try:
            conn = get_db_connection()
            c = conn.cursor()
            # Delete in order respecting foreign keys
            c.execute("DELETE FROM conversation_analysis WHERE log_id IN (SELECT id FROM logs WHERE phone_number = %s)", (test_phone,))
            c.execute("DELETE FROM list_items WHERE phone_number = %s", (test_phone,))
            c.execute("DELETE FROM lists WHERE phone_number = %s", (test_phone,))
            c.execute("DELETE FROM reminders WHERE phone_number = %s", (test_phone,))
            c.execute("DELETE FROM recurring_reminders WHERE phone_number = %s", (test_phone,))
            c.execute("DELETE FROM memories WHERE phone_number = %s", (test_phone,))
            c.execute("DELETE FROM support_tickets WHERE phone_number = %s", (test_phone,))
            c.execute("DELETE FROM logs WHERE phone_number = %s", (test_phone,))
            c.execute("DELETE FROM users WHERE phone_number = %s", (test_phone,))
            c.execute("DELETE FROM onboarding_progress WHERE phone_number = %s", (test_phone,))
            conn.commit()
        except Exception as e:
            print(f"Cleanup error: {e}")
            if conn:
                conn.rollback()
        finally:
            if conn:
                return_db_connection(conn)

    # Clean before test
    cleanup()

    yield test_phone

    # Clean after test
    cleanup()


@pytest.fixture
def mock_datetime():
    """Fixture for mocking datetime to control time in tests."""
    from datetime import datetime

    class MockDateTime:
        def __init__(self):
            self._now = datetime(2025, 6, 15, 10, 0, 0)  # Default: June 15, 2025, 10:00 AM

        def set_now(self, dt):
            self._now = dt

        def now(self, tz=None):
            if tz:
                return self._now.replace(tzinfo=tz)
            return self._now

        def utcnow(self):
            return self._now

    return MockDateTime()


# Rate limit fixture to reset rate limiting between tests
@pytest.fixture(autouse=True)
def reset_rate_limits():
    """Reset rate limiting store between tests."""
    try:
        from main import rate_limit_store
        rate_limit_store.clear()
        yield
        rate_limit_store.clear()
    except ImportError:
        # Skip if main can't be imported (missing deps)
        yield


@pytest.fixture(autouse=True)
def disable_twilio_globally():
    """
    CRITICAL: Disable ALL Twilio calls for ALL tests.
    This prevents accidental charges from any test that might trigger SMS.

    This patches:
    1. The Twilio Client class itself - ANY new Client() becomes a mock
    2. send_sms in all modules that import it
    3. RequestValidator.validate to always return True (bypass signature validation)

    Even tests that don't use sms_capture will be protected.
    """
    blocked_calls = []

    def mock_twilio_create(**kwargs):
        """Capture instead of sending - logs for debugging."""
        blocked_calls.append({
            'to': kwargs.get('to'),
            'body': kwargs.get('body', '')[:100],  # Truncate for logs
            'from': kwargs.get('from_'),
        })
        # Return a mock message object
        mock_msg = MagicMock()
        mock_msg.sid = 'MOCK_SID_TEST'
        mock_msg.status = 'queued'
        return mock_msg

    def mock_send_sms(to_number, message, media_url=None):
        """Mock send_sms that doesn't call Twilio."""
        blocked_calls.append({
            'to': to_number,
            'body': message[:100] if message else '',
            'media_url': media_url
        })
        return None

    def mock_validate(*args, **kwargs):
        """Always return True for signature validation in tests."""
        return True

    # Create a mock Twilio Client class that captures all calls
    class MockTwilioClient:
        """Mock Twilio Client that captures messages instead of sending."""
        def __init__(self, *args, **kwargs):
            self.messages = MagicMock()
            self.messages.create = mock_twilio_create

    # Patch at multiple levels for safety
    patches = [
        # CRITICAL: Patch the Twilio Client class itself - catches ANY direct Client() creation
        patch('twilio.rest.Client', MockTwilioClient),
        # Patch Twilio RequestValidator to always return True (bypass signature check)
        patch('twilio.request_validator.RequestValidator.validate', return_value=True),
        patch('main.RequestValidator.validate', return_value=True),
        # Patch the Twilio client's create method directly (for already-imported modules)
        patch('services.sms_service.twilio_client.messages.create', side_effect=mock_twilio_create),
        # Patch send_sms in all modules
        patch('services.sms_service.send_sms', side_effect=mock_send_sms),
        patch('services.onboarding_service.send_sms', side_effect=mock_send_sms),
        patch('services.first_action_service.send_sms', side_effect=mock_send_sms),
        patch('services.reminder_service.send_sms', side_effect=mock_send_sms),
        patch('services.support_service.send_sms', side_effect=mock_send_sms),
        patch('services.onboarding_recovery_service.send_sms', side_effect=mock_send_sms),
        patch('services.stripe_service.send_sms', side_effect=mock_send_sms),
        patch('tasks.reminder_tasks.send_sms', side_effect=mock_send_sms),
        patch('main.send_sms', side_effect=mock_send_sms),
        patch('admin_dashboard.send_sms', side_effect=mock_send_sms),
    ]

    # Start all patches
    started_patches = []
    for p in patches:
        try:
            started_patches.append(p.start())
        except Exception:
            # Module might not be imported yet, that's OK
            pass

    yield blocked_calls

    # Stop all patches
    for p in patches:
        try:
            p.stop()
        except Exception:
            pass
