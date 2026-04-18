# Smart Suggestions — Technical Scope & Design

**Status:** Design complete, ready for review
**Priority:** Medium — engagement & retention differentiator
**Estimated Effort:** Medium — phased rollout across 3 categories

---

## Concept

After a user creates a reminder, list, or memory, the system analyzes the content and offers a contextually relevant follow-up suggestion. The user can accept with YES or ignore it. This turns Remyndrs from a passive tool into one that anticipates what the user needs next.

**Key principle:** Suggest, never auto-create. The user stays in control.

---

## Three Suggestion Categories

### 1. Smart Reminders — "Prep Reminder"

**Trigger:** User creates a reminder for a high-importance event.

**Detection keywords/patterns:**
| Pattern | Suggestion |
|---------|------------|
| doctor, dentist, appointment, checkup | "Want a reminder 12 hours before to prepare?" |
| interview, meeting (with company name) | "Want a reminder the night before to prep?" |
| flight, airport, travel | "Want a reminder 3 hours before to leave for the airport?" |
| birthday, anniversary | "Want a reminder 2 days before to get a gift?" |
| deadline, due date, submission | "Want a reminder the day before?" |
| exam, test, quiz | "Want a reminder the night before to review?" |
| moving, move-in, move-out | "Want a reminder 1 week before to start packing?" |
| wedding, ceremony | "Want a reminder 1 week before to confirm details?" |
| vet, pet appointment | "Want a reminder 12 hours before?" |
| payment, bill, rent | "Want a reminder 2 days before?" |

**Example flow:**
```
User:    "Remind me about my doctor appointment Thursday at 2pm"
System:  "Got it! I'll remind you Thursday at 2:00 PM about your doctor appointment.

          Want me to also remind you Wednesday evening to prepare?"
User:    "Yes"
System:  "Done! I'll remind you Wednesday at 7:00 PM to prepare for your doctor appointment."
```

**Prep reminder timing logic:**
- Events before noon → reminder the evening before (7 PM local)
- Events at noon or later → reminder 12 hours before
- Events days/weeks away (birthday, deadline) → reminder 1-2 days before
- Minimum gap: 4 hours (don't suggest a prep reminder for something 2 hours away)

### 2. Smart Lists — "List Companion Reminders"

**Trigger:** User creates a new list whose name suggests a time-bound activity.

**Detection keywords/patterns:**
| List name pattern | Suggestion |
|-------------------|------------|
| vacation, trip, travel, packing | "Want a reminder [X days] before your trip to review this list?" |
| grocery, groceries, shopping | "Want a weekly reminder to check your grocery list?" |
| party, event, gathering | "Want a reminder the day before to review this list?" |
| move, moving, packing | "Want a weekly reminder to check off items?" |
| christmas, holiday, gift | "Want a reminder each week to work through your list?" |
| school, supplies, back to school | "Want a reminder before school starts to review this?" |
| wedding, registry | "Want a monthly reminder to review progress?" |
| project, sprint | "Want a weekly reminder to check progress?" |

**Example flow:**
```
User:    "Create a vacation packing list"
System:  "Created your Vacation Packing List!

          When is your trip? I can remind you to review this list beforehand."
User:    "June 15th"
System:  "I'll remind you June 13th to review your Vacation Packing List."
```

**For recurring lists (grocery):**
```
User:    "Create a grocery list"
System:  "Created your Grocery List!

          Want a weekly reminder to check your grocery list before shopping?"
User:    "Yes"
System:  "What day works best? (e.g., Saturday morning)"
User:    "Saturday morning"
System:  "Done! I'll remind you every Saturday at 9:00 AM to check your Grocery List."
```

### 3. Smart Memories — "Maintenance Reminders"

**Trigger:** User saves a memory that implies a recurring maintenance task.

**Detection keywords/patterns:**
| Memory pattern | Suggestion |
|----------------|------------|
| changed air filter, replaced filter | "Want a reminder in 90 days to change it again?" |
| oil change, changed oil | "Want a reminder in 3 months for the next oil change?" |
| rotated tires, tire rotation | "Want a reminder in 6 months?" |
| replaced battery, new battery | "Want a reminder in 1 year to check it?" |
| paid rent, rent due | "Want a monthly reminder?" |
| renewed [license/subscription/insurance] | "Want a reminder before it expires?" → ask expiry date |
| planted [garden/flowers/seeds] | "Want a reminder in 2 weeks to water/check on them?" |
| cleaned gutters, pressure washed | "Want a reminder in 6 months?" |
| pest control, exterminator | "Want a reminder in 3 months?" |
| water heater flush, HVAC service | "Want a reminder in 12 months?" |
| smoke detector batteries | "Want a reminder in 6 months to replace them again?" |

**Example flow:**
```
User:    "Remember I changed the air filter today"
System:  "Saved! "Changed the air filter today"

          Want a reminder in 90 days to change it again?"
User:    "Yes"
System:  "Done! I'll remind you on [date] to change the air filter."
```

**For memories with variable intervals:**
```
User:    "Remember I renewed my car insurance today"
System:  "Saved! "Renewed car insurance today"

          Want a reminder before it expires? When does it renew next?"
User:    "January 2027"
System:  "I'll remind you December 15, 2026 to renew your car insurance."
```

---

## Architecture

### Detection Layer

A lightweight keyword/pattern matcher — NOT an AI call. This keeps it fast, predictable, and free of additional OpenAI costs. The detection runs synchronously right after the primary action succeeds.

```python
# services/smart_suggestion_service.py

def get_reminder_suggestion(reminder_text: str, reminder_date: datetime, timezone: str) -> dict | None:
    """Check if a reminder warrants a prep suggestion.
    Returns {suggestion_text, prep_date, prep_reminder_text} or None."""

def get_list_suggestion(list_name: str) -> dict | None:
    """Check if a new list name warrants a companion reminder suggestion.
    Returns {suggestion_text, needs_date: bool, recurrence: str|None} or None."""

def get_memory_suggestion(memory_text: str) -> dict | None:
    """Check if a saved memory warrants a maintenance reminder suggestion.
    Returns {suggestion_text, default_days: int, needs_date: bool} or None."""
```

### Suggestion Flow

```
User creates reminder/list/memory
  → Primary action executes (save to DB, send confirmation)
  → Detection layer checks content against patterns
  → If match found AND user is eligible:
      - Append suggestion to confirmation SMS
      - Store pending state for YES/NO handling
  → If no match: normal response, no suggestion
```

### Pending State

Reuse the existing `pending_reminder_confirmation` JSON pattern:

```json
{
  "type": "smart_suggestion",
  "category": "prep_reminder|list_companion|maintenance_reminder",
  "suggestion_data": {
    "reminder_text": "Prepare for doctor appointment",
    "reminder_date": "2026-04-15 19:00:00",
    "related_id": 456,
    "list_name": null,
    "recurrence": null
  }
}
```

**YES handler:** Creates the suggested reminder (one-time or recurring), sends confirmation.
**NO / ignore:** Clears the pending state on next message (existing pattern).
**Other message:** Clears pending state, processes the new message normally.

### Injection Points

| Action | File | Where to inject |
|--------|------|-----------------|
| Reminder created | `main.py` ~line 4662 | After usage counter, before `log_interaction` |
| List created | `routes/handlers/lists.py` ~line 73 | After list counter, before `log_interaction` |
| Memory saved | `main.py` ~line 4339 | After memory counter, before `log_interaction` |

The suggestion text is appended to `reply_text` with a `\n\n` separator, keeping it in a single SMS where possible.

---

## Tier Gating

| Tier | Smart Suggestions |
|------|-------------------|
| Free (v1 & v2) | Off — no suggestions |
| Premium / Trial | On — all 3 categories |
| Family | On — all 3 categories |

Smart Suggestions are opt-in by default for Premium users (no toggle needed — they just ignore suggestions they don't want). No separate enable/disable setting to start; revisit if users complain about frequency.

---

## Throttling

- **Max 1 suggestion per conversation turn** — if a user creates a list AND adds items in one message, only suggest for the list creation
- **No daily limit** — suggestions are contextual responses, not proactive nudges. They only appear when the user just did something, so they're always timely
- **Cooldown after decline** — if user ignores or declines a suggestion, don't suggest for the same category for the next 3 interactions (track via a counter on the user record)

This is different from Smart Nudges (which are proactive and rate-limited to 1/day). Suggestions are reactive — they ride on an action the user just took.

---

## SMS Cost Impact

Zero incremental cost. Suggestions are appended to the confirmation SMS the system was already sending. The only additional SMS is when the user says YES and gets the "Done!" confirmation — but that's a normal reminder confirmation.

---

## Edge Cases

- **User creates a reminder that already has a prep reminder** — don't suggest a duplicate. Check existing reminders for the same day with similar text.
- **User creates a list with a name that matches but already has a recurring reminder for it** — skip the suggestion.
- **Memory text is too vague to suggest a specific interval** — use `needs_date: true` and ask the user when.
- **User is mid-conversation with another pending state** — don't inject a suggestion. Check for existing pending states before suggesting.
- **Prep reminder would be in the past** — don't suggest if there isn't enough lead time (< 4 hours).
- **Character limit** — keep the suggestion line under 80 characters so the total SMS stays under 160 where possible (1 SMS segment). If the confirmation + suggestion exceeds 160, it's fine — Twilio handles multi-segment, and the suggestion is worth the extra segment.

---

## Implementation Order

### Phase 1 — Smart Reminders (Prep Reminders)
1. `services/smart_suggestion_service.py` — detection logic + timing calculation
2. `main.py` — inject after reminder creation, handle YES response
3. Tests — keyword detection, timing logic, YES/NO flow, edge cases
4. Manual QA with real SMS

### Phase 2 — Smart Memories (Maintenance Reminders)
1. Add memory detection patterns to suggestion service
2. `main.py` — inject after memory save, handle YES + date collection
3. Tests

### Phase 3 — Smart Lists (List Companion Reminders)
1. Add list detection patterns to suggestion service
2. `routes/handlers/lists.py` + `main.py` — inject after list creation
3. Handle the date-collection flow (grocery → "what day?", vacation → "when is your trip?")
4. Tests

### Phase 4 — Polish
- Cooldown tracking after declines
- Duplicate suggestion prevention
- Admin dashboard metrics (suggestion rate, acceptance rate)
- Consider AI-powered detection for edge cases keyword matching misses

---

## Open Questions

1. **Should suggestions be configurable per category?** e.g., "Turn off list suggestions but keep reminder suggestions." Adds UX complexity — defer unless users ask.
2. **Should the prep reminder text reference the original?** e.g., "Prepare for your doctor appointment tomorrow at 2 PM" vs just "Prepare for doctor appointment." Referencing the original is more helpful but longer.
3. **Should grocery list suggestion default to a specific day?** Or always ask? Asking is safer — people shop on different days.
4. **What about memories that aren't maintenance?** e.g., "Remember Sarah's birthday is March 15" — should this suggest a yearly reminder? Good idea but needs different detection logic (date extraction from memory text).
5. **Should we track suggestion acceptance rate?** Yes — this tells us which patterns are valuable vs annoying. Add to Phase 4.
