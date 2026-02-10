"""
Unit tests for progressive education tier limit functions.
These tests verify the new tier_service functions work correctly.
"""

import pytest
from unittest.mock import patch, MagicMock


class TestProgressiveCounters:
    """Test progressive counter functions (Level 2/3)."""

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('models.list_model.get_item_count')
    def test_list_item_counter_silent_zone(self, mock_count, mock_limits, mock_tier):
        """Counter should not appear below 70% (Level 1: Silent)."""
        from services.tier_service import add_list_item_counter_to_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_items_per_list': 10}
        mock_count.return_value = 5  # 50%

        result = add_list_item_counter_to_message('+15551234567', 1, 'Added 5 items')
        assert result == 'Added 5 items'
        assert '(' not in result  # No counter shown

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('models.list_model.get_item_count')
    def test_list_item_counter_nudge_zone(self, mock_count, mock_limits, mock_tier):
        """Counter should appear at 70%+ (Level 2: Nudge)."""
        from services.tier_service import add_list_item_counter_to_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_items_per_list': 10}
        mock_count.return_value = 7  # 70%

        result = add_list_item_counter_to_message('+15551234567', 1, 'Added 7 items')
        assert '(7 of 10 items)' in result
        assert 'Almost full' not in result  # Not yet at 90%

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('models.list_model.get_item_count')
    def test_list_item_counter_warning_zone(self, mock_count, mock_limits, mock_tier):
        """Counter should show warning at 90%+ (Level 3: Warning)."""
        from services.tier_service import add_list_item_counter_to_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_items_per_list': 10}
        mock_count.return_value = 9  # 90%

        result = add_list_item_counter_to_message('+15551234567', 1, 'Added 9 items')
        assert '(9 of 10 items)' in result
        assert 'Almost full!' in result

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('models.list_model.get_item_count')
    def test_list_item_counter_premium_no_counter(self, mock_count, mock_limits, mock_tier):
        """Premium users should never see counters."""
        from services.tier_service import add_list_item_counter_to_message

        mock_tier.return_value = 'premium'
        mock_limits.return_value = {'max_items_per_list': 30}
        mock_count.return_value = 25  # 83% - would show for free

        result = add_list_item_counter_to_message('+15551234567', 1, 'Added 25 items')
        assert result == 'Added 25 items'
        assert '(' not in result  # No counter for premium

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('services.tier_service.get_memory_count')
    def test_memory_counter_last_one(self, mock_count, mock_limits, mock_tier):
        """Memory counter should show 'Last one!' at 100%."""
        from services.tier_service import add_memory_counter_to_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_memories': 5}
        mock_count.return_value = 5  # 100%

        result = add_memory_counter_to_message('+15551234567', 'Saved memory')
        assert '(5 of 5 memories)' in result
        assert 'Last one!' in result

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('models.list_model.get_list_count')
    def test_list_counter_last_one(self, mock_count, mock_limits, mock_tier):
        """List counter should show 'Last one!' at 100%."""
        from services.tier_service import add_list_counter_to_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_lists': 5}
        mock_count.return_value = 5  # 100%

        result = add_list_counter_to_message('+15551234567', 'Created list')
        assert '(5 of 5 lists)' in result
        assert 'Last one!' in result


class TestLevel4Formatters:
    """Test Level 4 blocked message formatters (WHY-WHAT-HOW)."""

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('services.tier_service.get_trial_info')
    def test_list_item_limit_nothing_added(self, mock_trial, mock_limits, mock_tier):
        """Test clear message when no items can be added."""
        from services.tier_service import format_list_item_limit_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_items_per_list': 10}
        mock_trial.return_value = {'is_trial': False, 'trial_end_date': None}

        items = ['apple', 'banana', 'orange']
        result = format_list_item_limit_message('+15551234567', 'grocery list', items, 0)

        # Check WHY
        assert 'full' in result.lower()
        assert '10 items max' in result.lower()
        assert 'free plan' in result.lower()

        # Check WHAT
        assert 'apple' in result.lower()
        assert 'banana' in result.lower()
        assert 'orange' in result.lower()

        # Check HOW
        assert 'upgrade' in result.lower()
        assert 'remove' in result.lower()

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('services.tier_service.get_trial_info')
    def test_list_item_limit_partial_add(self, mock_trial, mock_limits, mock_tier):
        """Test message when some items added, some skipped."""
        from services.tier_service import format_list_item_limit_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_items_per_list': 10}
        mock_trial.return_value = {'is_trial': False, 'trial_end_date': None}

        items = ['apple', 'banana', 'orange']
        result = format_list_item_limit_message('+15551234567', 'grocery list', items, 1)

        # Should mention what was added
        assert 'added' in result.lower()
        assert 'apple' in result.lower()

        # Should explain what couldn't be added
        assert "couldn't add" in result.lower() or "can't add" in result.lower()
        assert 'banana' in result.lower()
        assert 'orange' in result.lower()

        # Should provide solutions
        assert 'upgrade' in result.lower()

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('services.tier_service.get_trial_info')
    def test_list_item_limit_trial_hint(self, mock_trial, mock_limits, mock_tier):
        """Test trial hint appears for expired trial users."""
        from services.tier_service import format_list_item_limit_message
        from datetime import datetime, timedelta

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_items_per_list': 10}
        # User had a trial but it expired
        expired = datetime.utcnow() - timedelta(days=1)
        mock_trial.return_value = {'is_trial': False, 'trial_end_date': expired}

        items = ['apple']
        result = format_list_item_limit_message('+15551234567', 'grocery list', items, 0)

        # Should show trial hint
        assert 'still on trial' in result.lower()
        assert 'status' in result.lower()

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('services.tier_service.get_trial_info')
    def test_memory_limit_message(self, mock_trial, mock_limits, mock_tier):
        """Test memory limit message structure."""
        from services.tier_service import format_memory_limit_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_memories': 5}
        mock_trial.return_value = {'is_trial': False, 'trial_end_date': None}

        result = format_memory_limit_message('+15551234567')

        # Check WHY
        assert 'limit' in result.lower()
        assert '5 memories' in result.lower()
        assert 'free plan' in result.lower()

        # Check HOW
        assert 'upgrade' in result.lower()
        assert 'delete' in result.lower()
        assert 'memories' in result.lower()

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('services.tier_service.get_trial_info')
    def test_list_limit_message(self, mock_trial, mock_limits, mock_tier):
        """Test list limit message structure."""
        from services.tier_service import format_list_limit_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_lists': 5}
        mock_trial.return_value = {'is_trial': False, 'trial_end_date': None}

        result = format_list_limit_message('+15551234567')

        # Check WHY
        assert 'limit' in result.lower()
        assert '5 lists' in result.lower()
        assert 'free plan' in result.lower()

        # Check HOW
        assert 'upgrade' in result.lower()
        assert 'delete' in result.lower()
        assert 'lists' in result.lower()


class TestMessageStructure:
    """Test that messages follow best practices."""

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('services.tier_service.get_trial_info')
    def test_messages_not_apologetic(self, mock_trial, mock_limits, mock_tier):
        """Messages should be helpful, not apologetic."""
        from services.tier_service import format_list_item_limit_message

        mock_tier.return_value = 'free'
        mock_limits.return_value = {'max_items_per_list': 10}
        mock_trial.return_value = {'is_trial': False, 'trial_end_date': None}

        items = ['apple']
        result = format_list_item_limit_message('+15551234567', 'grocery list', items, 0)

        # Should not be apologetic
        assert 'sorry' not in result.lower()
        assert 'apologize' not in result.lower()
        assert 'unfortunately' not in result.lower()

    @patch('services.tier_service.get_user_tier')
    @patch('services.tier_service.get_tier_limits')
    @patch('services.tier_service.get_trial_info')
    def test_messages_provide_upgrade_path(self, mock_trial, mock_limits, mock_tier):
        """All limit messages should mention UPGRADE."""
        from services.tier_service import (
            format_list_item_limit_message,
            format_memory_limit_message,
            format_list_limit_message
        )

        mock_tier.return_value = 'free'
        mock_limits.return_value = {
            'max_items_per_list': 10,
            'max_memories': 5,
            'max_lists': 5
        }
        mock_trial.return_value = {'is_trial': False, 'trial_end_date': None}

        # All should mention upgrade
        items_msg = format_list_item_limit_message('+15551234567', 'test', ['item'], 0)
        assert 'upgrade' in items_msg.lower()

        memory_msg = format_memory_limit_message('+15551234567')
        assert 'upgrade' in memory_msg.lower()

        list_msg = format_list_limit_message('+15551234567')
        assert 'upgrade' in list_msg.lower()
