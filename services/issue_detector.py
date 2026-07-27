"""
Issue Detector

Side-channel observer on the inbound SMS path. Users report outages in plain
language ("my reminders haven't come through in days") rather than with the
SUPPORT/BUG keywords, so those reports never reached the support inbox. This
module notices them, records them, and pushes a notification.

It never changes what the user sees. Every entry point swallows its own
exceptions -- a failure here must not affect message handling.

Three stages, cheapest first:
  1. looks_like_issue_report()  - free regex prefilter, generous by design
  2. classify_issue_report()    - one small AI call, kills the false positives
  3. record_and_notify()        - contact_messages row + rate-limited email,
                                  plus an outage escalation when several
                                  distinct users complain inside 24h
"""

import json
import re
from datetime import datetime, timedelta

from config import (
    OPENAI_API_KEY, OPENAI_MODEL, logger
)
from database import get_db_connection, return_db_connection, get_setting, set_setting

# Timeout for the confirmation call. Deliberately short: this runs as a
# FastAPI background task, but a hung call would still tie up a threadpool
# worker, and the classification is worthless if it isn't quick.
CLASSIFIER_TIMEOUT = 6

# Marks rows this module created, so they can be counted and digested
# separately from FEEDBACK/BUG submissions in the same table.
AUTO_SOURCE = 'sms_auto'

VALID_CATEGORIES = {
    'service_outage',
    'reminder_delivery',
    'billing',
    'confusion',
    'other',
}

VALID_SEVERITIES = {'low', 'medium', 'high'}

# Generous on purpose -- the AI stage is what decides. Anything that makes it
# past here costs one small API call, nothing more.
ISSUE_SIGNALS = re.compile(
    r"""
    (?:not|isn't|isnt|aren't|arent|ain't|aint|wasn't|wasnt|weren't|werent
      |doesn't|doesnt|don't|dont|didn't|didnt|won't|wont)\s+work(?:s|ing)?
  | stopped\s+working
  | quit\s+working
  | nothing\s+(?:\w+\s+){0,2}?
      (?:work(?:s|ing|ed)?|come|comes|coming|came|arriv\w+|show(?:s|ed|ing)?\s+up
        |went\s+(?:off|out|through)|happen(?:s|ed|ing)?)
  | (?:didn't|didnt|did\s+not|never|haven't|havent|have\s+not|hasn't|hasnt)
      \s+(?:\w+\s+){0,2}?(?:get|got|gotten|receive|received|come|coming|show(?:n|ed)?\s+up|go\s+off|went\s+off)
  | no\s+(?:reminder|reminders|text|texts|notification|notifications)
  | (?:reminder|reminders|text|texts|app|service|system|site|website)\s+(?:\w+\s+){0,2}?(?:broke|broken|down|dead|stuck)
  | is\s+(?:this|it|the\s+\w+)\s+(?:down|broken|working)
  | are\s+(?:you|y'all|yall|things)\s+(?:down|having)
  | outage
  | glitch(?:ing|y|es)?
  | having\s+(?:some\s+)?(?:issues?|trouble|problems?)
  | (?:issues?|problems?|trouble)\s+with
  | something(?:'s|s)?\s+(?:is\s+)?wrong
  | tech(?:nical)?\s+support
  | customer\s+(?:service|support)
  | (?:double|twice)\s+charged
  | charged\s+(?:me\s+)?twice
  | refund
  | what\s+happened\s+to
  | why\s+(?:didn't|didnt|isn't|isnt|aren't|arent|hasn't|hasnt|am\s+i\s+not)
    """,
    re.IGNORECASE | re.VERBOSE,
)

# If any of these match, the message is something else entirely and the
# signal above was incidental -- "remind me to fix the broken sink" is a
# reminder, not an outage report.
NOT_AN_ISSUE = re.compile(
    r"""
    ^\s*remind(?:\s+me)?\b
  | ^\s*(?:add|put)\b.{0,60}\blist\b
  | ^\s*(?:create|make|start)\s+(?:a\s+)?(?:new\s+)?list\b
  | ^\s*remember\b
    """,
    re.IGNORECASE | re.VERBOSE,
)

# These already notify through their own handlers in main.py; flagging them
# again would double-email.
ALREADY_HANDLED_COMMANDS = {
    'SUPPORT', 'BUG', 'FEEDBACK', 'QUESTION', 'HELP', 'STOP', 'START',
    'UNSUBSCRIBE', 'CANCEL', 'EXIT', 'CLOSE',
}


def is_detection_enabled() -> bool:
    """Kill switch, flippable from admin settings without a deploy."""
    return get_setting("auto_issue_detection_enabled", "true").lower() == "true"


def looks_like_issue_report(message: str) -> bool:
    """Cheap prefilter. True means 'worth one AI call', not 'is an issue'."""
    if not message:
        return False

    text = message.strip()
    if not text or len(text) < 4:
        return False

    # Bare keyword commands own their own notification path.
    first_word = text.split()[0].strip('.,!?:;').upper()
    if first_word in ALREADY_HANDLED_COMMANDS:
        return False

    if NOT_AN_ISSUE.search(text):
        return False

    return bool(ISSUE_SIGNALS.search(text))


def _recent_context(phone_number: str, limit: int = 4) -> str:
    """Last few exchanges, to help the model judge an ambiguous complaint."""
    try:
        from database import get_recent_logs
        logs = get_recent_logs(limit=limit, phone_filter=phone_number)
    except Exception as e:
        logger.warning(f"Issue detector: could not load context: {e}")
        return ""

    if not logs:
        return ""

    lines = []
    for row in reversed(logs):  # oldest first reads more naturally
        incoming = (row.get('message_in') or '').strip()
        outgoing = (row.get('message_out') or '').strip()
        if incoming:
            lines.append(f"User: {incoming[:160]}")
        if outgoing:
            lines.append(f"Remyndrs: {outgoing[:160]}")

    return "\n".join(lines)


CLASSIFIER_PROMPT = """You review inbound text messages sent to Remyndrs, an SMS reminder service.

Decide whether the LATEST message is the user reporting a problem, complaining that \
something is broken, or asking for technical/billing help.

Answer true for things like:
- "my reminders haven't come through in days"
- "is this thing broken? nothing went off this morning"
- "I got charged twice"
- "why didn't I get my 8am text"

Answer false for:
- normal requests to create/read/delete reminders, lists, or memories, even if the \
content mentions something broken (e.g. "remind me to fix the broken sink")
- general questions about how to use the service
- casual chat, thanks, greetings

Reply with JSON only:
{"is_issue_report": true|false, "category": "service_outage"|"reminder_delivery"|"billing"|"confusion"|"other", "severity": "low"|"medium"|"high", "summary": "one short sentence"}

severity: high = service appears broken or money is involved; medium = a specific \
failure the user is annoyed about; low = mild confusion."""


def classify_issue_report(message: str, phone_number: str) -> dict | None:
    """
    Confirm a prefilter hit with one small AI call.

    Returns the parsed classification, or None when it is not an issue report
    (including on any error -- silence is the safe failure mode here).
    """
    if not OPENAI_API_KEY:
        return None

    try:
        # Imported at call time so tests patching openai.OpenAI take effect.
        from openai import OpenAI
        from database import log_api_usage

        context = _recent_context(phone_number)
        user_content = message if not context else (
            f"Recent conversation:\n{context}\n\nLATEST message:\n{message}"
        )

        client = OpenAI(api_key=OPENAI_API_KEY, timeout=CLASSIFIER_TIMEOUT)
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": CLASSIFIER_PROMPT},
                {"role": "user", "content": user_content},
            ],
            temperature=0,
            max_tokens=150,
            response_format={"type": "json_object"},
        )

        try:
            if response.usage:
                log_api_usage(
                    phone_number,
                    'detect_issue_report',
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                    response.usage.total_tokens,
                    OPENAI_MODEL,
                )
        except Exception:
            pass  # cost logging must never block detection

        parsed = json.loads(response.choices[0].message.content)
        if not isinstance(parsed, dict) or parsed.get('is_issue_report') is not True:
            return None

        category = str(parsed.get('category', 'other')).strip().lower()
        severity = str(parsed.get('severity', 'medium')).strip().lower()

        return {
            'category': category if category in VALID_CATEGORIES else 'other',
            'severity': severity if severity in VALID_SEVERITIES else 'medium',
            'summary': str(parsed.get('summary', '')).strip()[:300],
        }

    except Exception as e:
        logger.warning(f"Issue detector: classification failed: {e}")
        return None


def _insert_flag(phone_number: str, message: str, classification: dict) -> int | None:
    """Record the flag in contact_messages. Returns the new row id."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """INSERT INTO contact_messages (phone_number, message, category, source)
               VALUES (%s, %s, %s, %s) RETURNING id""",
            (
                phone_number,
                message,
                f"auto_{classification['category']}",
                AUTO_SOURCE,
            )
        )
        row_id = c.fetchone()[0]
        conn.commit()
        return row_id
    except Exception as e:
        logger.error(f"Issue detector: failed to record flag: {e}")
        if conn:
            conn.rollback()
        return None
    finally:
        if conn:
            return_db_connection(conn)


def _emailed_recently(phone_number: str, hours: int) -> bool:
    """True if this user already generated an email inside the cooldown."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """SELECT EXISTS(
                   SELECT 1 FROM contact_messages
                   WHERE phone_number = %s AND source = %s
                     AND created_at > %s
               )""",
            (phone_number, AUTO_SOURCE, datetime.utcnow() - timedelta(hours=hours))
        )
        return bool(c.fetchone()[0])
    except Exception as e:
        logger.warning(f"Issue detector: cooldown check failed: {e}")
        return False  # prefer a duplicate email over a missed one
    finally:
        if conn:
            return_db_connection(conn)


def count_distinct_reporters(hours: int = 24) -> int:
    """How many different users have been auto-flagged recently."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute(
            """SELECT COUNT(DISTINCT phone_number) FROM contact_messages
               WHERE source = %s AND created_at > %s""",
            (AUTO_SOURCE, datetime.utcnow() - timedelta(hours=hours))
        )
        return int(c.fetchone()[0] or 0)
    except Exception as e:
        logger.warning(f"Issue detector: reporter count failed: {e}")
        return 0
    finally:
        if conn:
            return_db_connection(conn)


def _maybe_escalate(reporter_count: int):
    """
    Several distinct users complaining in one day is the signature of an
    outage, not of individual confusion. That case gets a louder channel.
    """
    try:
        threshold = int(get_setting("auto_issue_outage_threshold", "3"))
    except (TypeError, ValueError):
        threshold = 3

    if reporter_count < threshold:
        return

    last = get_setting("auto_issue_last_escalation", "")
    if last:
        try:
            if datetime.fromisoformat(last) > datetime.utcnow() - timedelta(hours=24):
                return  # already escalated today
        except ValueError:
            pass

    set_setting("auto_issue_last_escalation", datetime.utcnow().isoformat())

    try:
        from services.email_service import send_outage_escalation_notification
        send_outage_escalation_notification(reporter_count)
    except Exception as e:
        logger.error(f"Issue detector: escalation email failed: {e}")

    try:
        from services.sms_service import notify_admin
        notify_admin(
            f"[Remyndrs] POSSIBLE OUTAGE: {reporter_count} different users have "
            f"reported a problem in the last 24h. Check /cs."
        )
    except Exception as e:
        logger.error(f"Issue detector: escalation SMS failed: {e}")


def record_and_notify(phone_number: str, message: str, classification: dict) -> dict:
    """Persist the flag, email (rate-limited), and escalate if warranted."""
    try:
        cooldown_hours = int(get_setting("auto_issue_email_cooldown_hours", "6"))
    except (TypeError, ValueError):
        cooldown_hours = 6

    # Checked before the insert, or the row we are about to write would
    # always be its own "recent" match.
    suppress_email = _emailed_recently(phone_number, cooldown_hours)

    row_id = _insert_flag(phone_number, message, classification)
    if row_id is None:
        return {'recorded': False, 'emailed': False}

    emailed = False
    if not suppress_email:
        try:
            from services.email_service import send_issue_flag_notification
            from services.support_service import get_user_name
            emailed = send_issue_flag_notification(
                phone_number=phone_number,
                message=message,
                category=classification['category'],
                severity=classification['severity'],
                summary=classification.get('summary', ''),
                user_name=get_user_name(phone_number),
            )
        except Exception as e:
            logger.error(f"Issue detector: notification email failed: {e}")
    else:
        logger.info(
            f"Issue detector: email suppressed (cooldown) for ...{phone_number[-4:]}"
        )

    _maybe_escalate(count_distinct_reporters(24))

    return {'recorded': True, 'emailed': emailed, 'id': row_id}


def maybe_flag_issue_report(phone_number: str, message: str) -> dict | None:
    """
    Entry point for the /sms hook. Runs the full pipeline.

    Never raises. Returns the record result when something was flagged,
    otherwise None.
    """
    try:
        if not is_detection_enabled():
            return None

        if not looks_like_issue_report(message):
            return None

        classification = classify_issue_report(message, phone_number)
        if not classification:
            return None

        logger.info(
            f"Issue detector: flagged {classification['category']}/"
            f"{classification['severity']} from ...{phone_number[-4:]}"
        )
        return record_and_notify(phone_number, message, classification)

    except Exception as e:
        logger.error(f"Issue detector: unexpected failure: {e}")
        return None
