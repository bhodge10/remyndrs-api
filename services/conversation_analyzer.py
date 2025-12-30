"""
Conversation Analyzer Service
Analyzes user conversations using AI to identify potential issues.
"""

import json
from openai import OpenAI
from config import OPENAI_API_KEY, OPENAI_MODEL, OPENAI_TIMEOUT, logger
from database import (
    get_unanalyzed_logs,
    mark_logs_analyzed,
    save_conversation_analysis,
    log_api_usage
)


def analyze_conversation_batch(conversations: list) -> list:
    """
    Analyze a batch of conversations using AI.
    Returns a list of flagged issues.
    """
    if not conversations:
        return []

    # Format conversations for analysis
    conv_text = ""
    for i, c in enumerate(conversations):
        conv_text += f"""
--- Conversation {i + 1} (ID: {c['id']}) ---
User: {c['message_in']}
System: {c['message_out']}
Intent: {c.get('intent', 'unknown')}
"""

    system_prompt = """You are a conversation quality analyzer for an SMS reminder service.
Analyze the provided conversations and identify any issues that need attention.

ISSUE TYPES TO FLAG:
1. "misunderstood_intent" - System misunderstood what the user was trying to do
2. "poor_response" - Response was unhelpful, confusing, or inappropriate
3. "frustrated_user" - User seems frustrated (repeated attempts, short messages after long ones)
4. "failed_action" - User tried to do something but it didn't work
5. "confused_user" - User seems confused about how to use the service
6. "sensitive_data" - Conversation contains concerning content
7. "error_response" - System responded with an error or fallback message

SEVERITY LEVELS:
- "high" - Urgent: user had a bad experience, system error, or security concern
- "medium" - Notable: user may have had trouble but likely recoverable
- "low" - Minor: potential improvement opportunity

For EACH conversation, determine if there's an issue. If yes, return it in the flagged array.
If a conversation looks fine (user got what they needed), don't include it.

Return JSON in this format:
{
    "flagged": [
        {
            "conversation_id": 123,
            "issue_type": "misunderstood_intent",
            "severity": "medium",
            "explanation": "User asked to set a reminder but system stored it as a memory instead"
        }
    ]
}

If no issues found, return: {"flagged": []}

Be conservative - only flag genuine issues, not minor imperfections.
Focus on cases where the user likely didn't get what they wanted."""

    try:
        client = OpenAI(api_key=OPENAI_API_KEY, timeout=OPENAI_TIMEOUT)

        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": conv_text}
            ],
            temperature=0.3,
            max_tokens=2000,
            response_format={"type": "json_object"}
        )

        # Log API usage
        if response.usage:
            log_api_usage(
                'system',
                'conversation_analysis',
                response.usage.prompt_tokens,
                response.usage.completion_tokens,
                response.usage.total_tokens,
                OPENAI_MODEL
            )

        result = json.loads(response.choices[0].message.content)
        return result.get('flagged', [])

    except json.JSONDecodeError as e:
        logger.error(f"JSON parse error in conversation analysis: {e}")
        return []
    except Exception as e:
        logger.error(f"Error analyzing conversations: {e}")
        return []


def analyze_recent_conversations(batch_size: int = 50):
    """
    Analyze recent unanalyzed conversations.
    Called manually from admin dashboard or by a scheduled job.
    """
    try:
        logger.info("Starting conversation analysis...")

        # Get unanalyzed logs
        logs = get_unanalyzed_logs(limit=batch_size)

        if not logs:
            logger.info("No unanalyzed conversations found")
            return {"analyzed": 0, "flagged": 0}

        logger.info(f"Analyzing {len(logs)} conversations...")

        # Analyze in smaller batches to avoid token limits
        batch_size = 10
        total_flagged = 0
        log_ids = []

        for i in range(0, len(logs), batch_size):
            batch = logs[i:i + batch_size]
            log_ids.extend([c['id'] for c in batch])

            # Analyze batch
            flagged = analyze_conversation_batch(batch)

            # Save flagged items
            for item in flagged:
                # Find the conversation to get phone number
                conv = next((c for c in batch if c['id'] == item['conversation_id']), None)
                if conv:
                    save_conversation_analysis(
                        log_id=item['conversation_id'],
                        phone_number=conv['phone_number'],
                        issue_type=item['issue_type'],
                        severity=item['severity'],
                        explanation=item['explanation']
                    )
                    total_flagged += 1

        # Mark all logs as analyzed
        mark_logs_analyzed(log_ids)

        logger.info(f"Analysis complete: {len(log_ids)} analyzed, {total_flagged} flagged")
        return {"analyzed": len(log_ids), "flagged": total_flagged}

    except Exception as e:
        logger.error(f"Error in analyze_recent_conversations: {e}")
        return {"analyzed": 0, "flagged": 0, "error": str(e)}
