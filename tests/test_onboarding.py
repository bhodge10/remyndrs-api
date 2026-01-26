"""
Tests for the onboarding flow.
Covers: new user signup, validation, error handling, and edge cases.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestOnboardingFlow:
    """Tests for the complete onboarding journey."""

    @pytest.mark.asyncio
    async def test_new_user_complete_onboarding(self, simulator, clean_test_user, sms_capture):
        """Test that a new user can complete the full onboarding flow."""
        phone = clean_test_user

        # Step 1: Initial message triggers onboarding
        result = await simulator.send_message(phone, "Hi")
        assert "name" in result["output"].lower() or "welcome" in result["output"].lower()

        # Step 2: Provide first name
        result = await simulator.send_message(phone, "John")
        assert "last name" in result["output"].lower()

        # Step 3: Provide last name
        result = await simulator.send_message(phone, "Smith")
        assert "email" in result["output"].lower()

        # Step 4: Provide email
        result = await simulator.send_message(phone, "john@example.com")
        assert "zip" in result["output"].lower() or "code" in result["output"].lower()

        # Step 5: Provide ZIP code - should complete onboarding
        result = await simulator.send_message(phone, "90210")
        # Should confirm setup complete and mention first saved memory
        assert any(word in result["output"].lower() for word in ["all set", "saved", "memory"])

    @pytest.mark.asyncio
    async def test_onboarding_with_full_name(self, simulator, clean_test_user):
        """Test that providing full name in one message works."""
        phone = clean_test_user

        # Initial message
        await simulator.send_message(phone, "Hi")

        # Provide full name
        result = await simulator.send_message(phone, "John Smith")

        # Should skip last name and go to email
        assert "email" in result["output"].lower()

    @pytest.mark.asyncio
    async def test_onboarding_invalid_email(self, simulator, clean_test_user):
        """Test that invalid email is rejected with helpful message."""
        phone = clean_test_user

        # Get to email step
        await simulator.send_message(phone, "Hi")
        await simulator.send_message(phone, "John")
        await simulator.send_message(phone, "Smith")

        # Try invalid email
        result = await simulator.send_message(phone, "not-an-email")
        assert "email" in result["output"].lower()  # Should ask again

        # Try another invalid format
        result = await simulator.send_message(phone, "test@")
        assert "email" in result["output"].lower()

        # Valid email should proceed
        result = await simulator.send_message(phone, "john@example.com")
        assert "zip" in result["output"].lower()

    @pytest.mark.asyncio
    async def test_onboarding_invalid_zip(self, simulator, clean_test_user):
        """Test that invalid ZIP code is handled."""
        phone = clean_test_user

        # Get to ZIP step
        await simulator.send_message(phone, "Hi")
        await simulator.send_message(phone, "John")
        await simulator.send_message(phone, "Smith")
        await simulator.send_message(phone, "john@example.com")

        # Try invalid ZIP
        result = await simulator.send_message(phone, "123")  # Too short
        # Should either ask again or explain

        # Valid ZIP
        result = await simulator.send_message(phone, "90210")
        assert any(word in result["output"].lower() for word in ["all set", "saved", "memory"])

    @pytest.mark.asyncio
    async def test_onboarding_cancel(self, simulator, clean_test_user):
        """Test that user can cancel onboarding."""
        phone = clean_test_user

        # Start onboarding
        await simulator.send_message(phone, "Hi")

        # Cancel
        result = await simulator.send_message(phone, "cancel")
        # Should acknowledge cancellation or offer to restart

    @pytest.mark.asyncio
    async def test_onboarding_restart(self, simulator, clean_test_user):
        """Test that user can restart onboarding mid-flow."""
        phone = clean_test_user

        # Start and provide name
        await simulator.send_message(phone, "Hi")
        await simulator.send_message(phone, "John")

        # Restart
        result = await simulator.send_message(phone, "restart")
        # Should go back to beginning
        assert "name" in result["output"].lower() or "welcome" in result["output"].lower()

    @pytest.mark.asyncio
    async def test_onboarding_skip_email(self, simulator, clean_test_user):
        """Test that user can skip optional email."""
        phone = clean_test_user

        # Get to email step
        await simulator.send_message(phone, "Hi")
        await simulator.send_message(phone, "John")
        await simulator.send_message(phone, "Smith")

        # Try to skip
        result = await simulator.send_message(phone, "skip")
        # Should either proceed or explain why email is needed


class TestOnboardingEdgeCases:
    """Edge cases and special scenarios for onboarding."""

    @pytest.mark.asyncio
    async def test_already_onboarded_user(self, simulator, onboarded_user):
        """Test that already onboarded user doesn't restart onboarding."""
        phone = onboarded_user["phone"]

        result = await simulator.send_message(phone, "Hi")
        # Should NOT ask for name, should treat as regular message
        assert "first name" not in result["output"].lower()

    @pytest.mark.asyncio
    async def test_onboarding_special_characters_in_name(self, simulator, clean_test_user):
        """Test handling of special characters in names."""
        phone = clean_test_user

        await simulator.send_message(phone, "Hi")

        # Name with special characters
        result = await simulator.send_message(phone, "José")
        assert "last name" in result["output"].lower()

        result = await simulator.send_message(phone, "O'Brien")
        assert "email" in result["output"].lower()

    @pytest.mark.asyncio
    async def test_onboarding_very_long_name(self, simulator, clean_test_user):
        """Test handling of very long names."""
        phone = clean_test_user

        await simulator.send_message(phone, "Hi")

        # Very long name
        long_name = "A" * 100
        result = await simulator.send_message(phone, long_name)
        # Should either truncate or reject gracefully

    @pytest.mark.asyncio
    async def test_onboarding_with_numbers_in_name(self, simulator, clean_test_user):
        """Test handling of numbers in name field."""
        phone = clean_test_user

        await simulator.send_message(phone, "Hi")

        # Name with numbers (possibly a mistake)
        result = await simulator.send_message(phone, "John123")
        # Should handle gracefully

    @pytest.mark.asyncio
    async def test_onboarding_empty_responses(self, simulator, clean_test_user):
        """Test handling of empty or whitespace-only responses."""
        phone = clean_test_user

        await simulator.send_message(phone, "Hi")

        # Empty-ish response
        result = await simulator.send_message(phone, "   ")
        # Should ask again or provide guidance


class TestOnboardingTimezones:
    """Tests for timezone detection during onboarding."""

    @pytest.mark.asyncio
    async def test_zip_to_eastern_timezone(self, simulator, clean_test_user):
        """Test that Eastern US ZIP codes get correct timezone."""
        phone = clean_test_user

        # Complete onboarding with NYC ZIP
        await simulator.send_message(phone, "Hi")
        await simulator.send_message(phone, "John")
        await simulator.send_message(phone, "Smith")
        await simulator.send_message(phone, "john@example.com")
        await simulator.send_message(phone, "10001")  # NYC

        # Verify timezone was set correctly
        from models.user import get_user_timezone
        tz = get_user_timezone(phone)
        assert tz == "America/New_York"

    @pytest.mark.asyncio
    async def test_zip_to_pacific_timezone(self, simulator, clean_test_user):
        """Test that Pacific US ZIP codes get correct timezone."""
        phone = clean_test_user

        await simulator.send_message(phone, "Hi")
        await simulator.send_message(phone, "John")
        await simulator.send_message(phone, "Smith")
        await simulator.send_message(phone, "john@example.com")
        await simulator.send_message(phone, "90210")  # Beverly Hills, CA

        from models.user import get_user_timezone
        tz = get_user_timezone(phone)
        assert tz == "America/Los_Angeles"

    @pytest.mark.asyncio
    async def test_zip_to_central_timezone(self, simulator, clean_test_user):
        """Test that Central US ZIP codes get correct timezone."""
        phone = clean_test_user

        await simulator.send_message(phone, "Hi")
        await simulator.send_message(phone, "John")
        await simulator.send_message(phone, "Smith")
        await simulator.send_message(phone, "john@example.com")
        await simulator.send_message(phone, "60601")  # Chicago

        from models.user import get_user_timezone
        tz = get_user_timezone(phone)
        assert tz == "America/Chicago"
