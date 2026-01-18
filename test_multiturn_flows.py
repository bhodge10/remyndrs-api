#!/usr/bin/env python
"""
Multi-turn Conversation Flow Tests for Remyndrs SMS Service

Tests scenarios that require multiple messages and state management:
- Undo confirmations
- Delete confirmations
- Time/date clarifications
- Confidence-based confirmations

These tests MUST be run against a live server (local or staging).
"""

import requests
import re
import time
import json
from datetime import datetime

# Configuration
API_URL = "http://localhost:8000/sms"
TEST_PHONE = "+15559999001"  # Dedicated multi-turn test phone

# Track test results
results = []

def parse_twiml_response(xml_text):
    """Extract message text from TwiML response."""
    match = re.search(r'<Message>(.*?)</Message>', xml_text, re.DOTALL)
    if match:
        return match.group(1).strip()
    return xml_text

def safe_print(text):
    """Print text safely (handle encoding issues)."""
    if text:
        print(text.encode('ascii', 'replace').decode('ascii'))
    else:
        print("(empty response)")

def send_message(message, phone=TEST_PHONE):
    """Send a message and return parsed response."""
    try:
        response = requests.post(
            API_URL,
            data={"Body": message, "From": phone},
            timeout=60
        )
        return parse_twiml_response(response.text)
    except Exception as e:
        return f"ERROR: {e}"

def run_flow(name, steps, phone=TEST_PHONE):
    """
    Run a multi-turn conversation flow.

    steps: list of (message, expected_keywords, description)
    Returns: (passed, details)
    """
    print(f"\n{'='*60}")
    print(f"FLOW: {name}")
    print(f"{'='*60}")

    flow_passed = True
    details = []

    for i, (message, expected_keywords, description) in enumerate(steps, 1):
        print(f"\nStep {i}: {description}")
        print(f"  Send: '{message}'")

        response = send_message(message, phone)
        print(f"  Response: ", end="")
        safe_print(response[:150] + "..." if len(response) > 150 else response)

        # Check for expected keywords
        response_lower = response.lower()
        found_keywords = []
        missing_keywords = []

        for kw in expected_keywords:
            if kw.lower() in response_lower:
                found_keywords.append(kw)
            else:
                missing_keywords.append(kw)

        if missing_keywords:
            print(f"  FAIL: Missing keywords: {missing_keywords}")
            flow_passed = False
        else:
            print(f"  PASS: Found: {found_keywords}")

        details.append({
            "step": i,
            "message": message,
            "response": response[:200],
            "expected": expected_keywords,
            "found": found_keywords,
            "missing": missing_keywords,
            "passed": len(missing_keywords) == 0
        })

        time.sleep(1.5)  # Rate limiting

    result = {"flow": name, "passed": flow_passed, "details": details}
    results.append(result)

    print(f"\nFLOW RESULT: {'PASS' if flow_passed else 'FAIL'}")
    return flow_passed, details


def ensure_onboarded(phone):
    """Make sure test phone is onboarded."""
    print(f"\nEnsuring {phone} is onboarded...")

    # Try sending a message to see if onboarded
    response = send_message("hello", phone)
    response_lower = response.lower()

    # Check for any onboarding indicators
    onboarding_indicators = [
        "first name", "welcome", "what's your", "email", "zip code",
        "step 1", "step 2", "step 3", "step 4", "finish setup",
        "almost there", "quick questions"
    ]

    needs_onboarding = any(ind in response_lower for ind in onboarding_indicators)

    if needs_onboarding:
        # Run through all onboarding steps
        print("  Running onboarding...")

        # If stuck mid-onboarding, provide all steps in order
        # Name
        if "first name" in response_lower or "step 1" in response_lower:
            send_message("MultiTest", phone)
            time.sleep(1)
            response = send_message("hello", phone)  # Check next step
            response_lower = response.lower()

        # Email
        if "email" in response_lower or "step 2" in response_lower:
            send_message("multitest@example.com", phone)
            time.sleep(1)
            response = send_message("hello", phone)
            response_lower = response.lower()

        # ZIP code
        if "zip" in response_lower or "step 3" in response_lower:
            send_message("90210", phone)
            time.sleep(1)
            response = send_message("hello", phone)
            response_lower = response.lower()

        # First action prompt
        if "want to try" in response_lower or "set your first" in response_lower or "step 4" in response_lower:
            send_message("no", phone)
            time.sleep(1)

        print("  Onboarding complete")
    else:
        print("  Already onboarded")

    time.sleep(1)


def setup_reminder_for_undo(phone):
    """Create a reminder so we have something to undo."""
    print("\n  Setting up reminder for undo test...")
    response = send_message("remind me tomorrow at 11am to test undo", phone)
    if "remind" in response.lower() or "got it" in response.lower():
        print("  Reminder created")
        time.sleep(1)
        return True
    else:
        safe_print(f"  Could not create reminder: {response[:100]}")
        return False


# =============================================================================
# TEST FLOWS
# =============================================================================

def test_undo_yes_flow(phone):
    """Test: undo -> yes -> deletion confirmed"""
    # First create a reminder to undo
    if not setup_reminder_for_undo(phone):
        results.append({"flow": "Undo -> Yes", "passed": False, "details": [{"error": "Setup failed"}]})
        return False

    return run_flow(
        "Undo -> Yes (confirm deletion)",
        [
            ("undo", ["delete", "yes"], "Should offer to delete recent reminder"),
            ("yes", ["deleted"], "Should confirm deletion"),
        ],
        phone
    )[0]


def test_undo_no_flow(phone):
    """Test: undo -> no -> cancelled"""
    if not setup_reminder_for_undo(phone):
        results.append({"flow": "Undo -> No", "passed": False, "details": [{"error": "Setup failed"}]})
        return False

    return run_flow(
        "Undo -> No (cancel deletion)",
        [
            ("undo", ["delete"], "Should offer to delete recent reminder"),
            ("no", ["cancelled"], "Should cancel and keep reminder"),
        ],
        phone
    )[0]


def test_undo_nothing_flow(phone):
    """Test: undo with no recent reminders"""
    # First delete all reminders for this user
    send_message("show reminders", phone)
    time.sleep(1)

    return run_flow(
        "Undo -> Nothing to undo",
        [
            ("undo", ["nothing", "undo", "help"], "Should say nothing to undo"),
        ],
        phone
    )[0]


def test_delete_reminder_yes_flow(phone):
    """Test: delete reminder -> yes -> deleted"""
    # Create a reminder first
    send_message("remind me tomorrow at 2pm to test delete flow", phone)
    time.sleep(1.5)

    return run_flow(
        "Delete Reminder -> Yes",
        [
            ("delete test delete flow", ["delete", "yes"], "Should ask for confirmation"),
            ("yes", ["deleted"], "Should confirm deletion"),
        ],
        phone
    )[0]


def test_delete_reminder_cancel_flow(phone):
    """Test: delete reminder -> cancel -> kept"""
    # Create a reminder first
    send_message("remind me tomorrow at 3pm to test cancel flow", phone)
    time.sleep(1.5)

    return run_flow(
        "Delete Reminder -> Cancel",
        [
            ("delete test cancel flow", ["delete"], "Should ask for confirmation"),
            ("no", ["cancelled"], "Should cancel and keep reminder"),
        ],
        phone
    )[0]


def test_time_clarification_flow(phone):
    """Test: reminder without time -> provide time -> created"""
    return run_flow(
        "Reminder -> Time Clarification -> Created",
        [
            ("remind me tomorrow to call the bank", ["time"], "Should ask for time"),
            ("2pm", ["remind", "bank"], "Should create reminder with specified time"),
        ],
        phone
    )[0]


def test_ampm_clarification_flow(phone):
    """Test: ambiguous time -> clarify AM/PM -> created"""
    # Note: AI may just assume 8am or 8pm, so this might pass step 1 directly
    return run_flow(
        "Ambiguous Time -> AM/PM Clarification",
        [
            ("remind me tomorrow at 8 to exercise", ["8"], "Should ask AM/PM or assume one"),
            ("am", ["8"], "Should create or confirm 8 AM"),
        ],
        phone
    )[0]


def test_list_add_flow(phone):
    """Test: add to list -> confirm -> added"""
    return run_flow(
        "Add to List Flow",
        [
            ("add milk to grocery list", ["added", "milk", "grocery"], "Should add and confirm"),
        ],
        phone
    )[0]


def test_list_delete_yes_flow(phone):
    """Test: delete from list -> yes -> deleted"""
    # First add an item
    send_message("add test item to shopping list", phone)
    time.sleep(1.5)

    return run_flow(
        "Delete List Item -> Yes",
        [
            ("delete test item from shopping", ["remove", "yes"], "Should ask confirmation"),
            ("yes", ["removed"], "Should confirm removal"),
        ],
        phone
    )[0]


def test_memory_save_flow(phone):
    """Test: save memory -> confirmed"""
    return run_flow(
        "Save Memory Flow",
        [
            ("remember my favorite color is blue", ["saved", "blue"], "Should save and confirm"),
        ],
        phone
    )[0]


def test_recurring_reminder_flow(phone):
    """Test: recurring reminder -> created"""
    return run_flow(
        "Recurring Reminder Flow",
        [
            ("remind me every day at 7am to stretch", ["every day", "7", "am", "stretch", "recurring"], "Should create recurring reminder"),
        ],
        phone
    )[0]


def test_snooze_flow(phone):
    """Test: snooze -> snoozed"""
    # This would need a reminder that was just sent - harder to test
    # Skip for now
    pass


# =============================================================================
# MAIN
# =============================================================================

def main():
    print("="*60)
    print("MULTI-TURN CONVERSATION FLOW TESTS")
    print("="*60)
    print(f"API: {API_URL}")
    print(f"Time: {datetime.now().isoformat()}")

    # Use different phone numbers for different test groups to avoid state conflicts
    phones = {
        "undo_yes": "+15559999002",
        "undo_no": "+15559999003",
        "undo_nothing": "+15559999004",
        "delete_yes": "+15559999005",
        "delete_cancel": "+15559999006",
        "time_clarify": "+15559999007",
        "ampm_clarify": "+15559999008",
        "list_add": "+15559999009",
        "list_delete": "+15559999010",
        "memory": "+15559999011",
        "recurring": "+15559999012",
    }

    # Ensure all phones are onboarded
    print("\n" + "="*60)
    print("SETUP: Ensuring test phones are onboarded")
    print("="*60)
    for name, phone in phones.items():
        ensure_onboarded(phone)

    # Run tests
    print("\n" + "="*60)
    print("RUNNING MULTI-TURN FLOW TESTS")
    print("="*60)

    # Core multi-turn flows
    test_undo_yes_flow(phones["undo_yes"])
    test_undo_no_flow(phones["undo_no"])
    test_delete_reminder_yes_flow(phones["delete_yes"])
    test_delete_reminder_cancel_flow(phones["delete_cancel"])
    test_time_clarification_flow(phones["time_clarify"])
    test_ampm_clarification_flow(phones["ampm_clarify"])

    # Single-turn but important flows
    test_list_add_flow(phones["list_add"])
    test_list_delete_yes_flow(phones["list_delete"])
    test_memory_save_flow(phones["memory"])
    test_recurring_reminder_flow(phones["recurring"])

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)

    passed = sum(1 for r in results if r["passed"])
    failed = sum(1 for r in results if not r["passed"])

    print(f"\nTotal: {len(results)} flows")
    print(f"Passed: {passed}")
    print(f"Failed: {failed}")

    if failed > 0:
        print("\nFailed flows:")
        for r in results:
            if not r["passed"]:
                print(f"  - {r['flow']}")
                for detail in r.get("details", []):
                    if detail.get("missing"):
                        print(f"    Step {detail.get('step')}: Missing {detail.get('missing')}")

    # Save results
    with open("multiturn_test_results.json", "w", encoding="utf-8") as f:
        json.dump({
            "timestamp": datetime.now().isoformat(),
            "api_url": API_URL,
            "summary": {"total": len(results), "passed": passed, "failed": failed},
            "results": results
        }, f, indent=2, ensure_ascii=False)

    print("\nResults saved to multiturn_test_results.json")

    return failed == 0


if __name__ == "__main__":
    success = main()
    exit(0 if success else 1)
