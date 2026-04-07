# Family Plan Feature — Feasibility Analysis & Implementation Plan

**Status:** Planned (not yet implemented)
**Priority:** Post-launch
**Estimated Effort:** Large — multi-week project

---

## Context

Exploring whether a family plan feature would work for Remyndrs. The vision: multiple family members each text the Remyndrs number from their own phones, but share lists, reminders, memories, broadcasts, and task assignments behind the scenes — a full family collaboration platform via SMS.

---

## Key Constraint: SMS Is Not Group Chat

**SMS is point-to-point.** Twilio sends/receives messages 1-to-1 with each phone number. No shared group threads possible.

**What this means:** Each family member has their **own private SMS thread** with the Remyndrs number. Sharing happens in the backend. This works fine for all use cases described.

---

## UX Model: Family Session (Mirroring Support Modal)

The existing **Support mode** pattern is the perfect model. Currently:
- User texts `SUPPORT message` → enters support session
- All subsequent messages routed to support (intercepted before AI processing)
- Messages prefixed with `[Support Ticket #X]`
- User texts `EXIT` to leave, or auto-timeout after 30 minutes
- State tracked via `updated_at` timestamp on `support_tickets` table

### Family Session — How It Works

**Entering Family Mode:**
1. User texts "Add milk to grocery list"
2. System detects user is on a family plan and the list could be personal or shared
3. System asks: *"Is this for your personal list or family list? Reply FAMILY or PERSONAL."*
4. User replies `FAMILY` → enters **family session**
5. All subsequent messages are processed in family context (shared lists, shared memories, etc.)
6. Every response prefixed with `[Family]` so user always knows they're in family mode

**Or direct entry:**
- User texts `FAMILY` keyword → enters family session directly
- User texts `FAMILY add milk to grocery list` → enters session + processes command

**Exiting Family Mode:**
- Text `EXIT` → returns to personal mode
- Text `PERSONAL` → switches to personal mode
- Auto-timeout after 30 minutes of inactivity (configurable)

**Session Flow Example:**
```
Mom:     "Add milk to grocery list"
System:  "Is this for your personal list or family list? Reply FAMILY or PERSONAL."
Mom:     "Family"
System:  "[Family] ✓ Added 'milk' to Grocery List."
Mom:     "Also add eggs"
System:  "[Family] ✓ Added 'eggs' to Grocery List."
Mom:     "Remind Dad to pick up Johnny at 6pm"
System:  "[Family] ✓ Reminder set for Dad at 6:00 PM today."
Mom:     "Exit"
System:  "You've exited family mode. Messages will now be for your personal account."
Mom:     "Remind me to call dentist at 3pm"
System:  "✓ Reminder set for today at 3:00 PM."  (no [Family] prefix — personal mode)
```

### Implementation — Mirroring Support Modal

| Support Modal | Family Session |
|--------------|----------------|
| `support_tickets.updated_at` tracks timeout | New field: `users.family_session_active_at` (TIMESTAMP, nullable) |
| `get_active_support_ticket()` checks 30-min window | `is_in_family_session()` checks 30-min window |
| Checked in main.py before AI processing (~line 812) | Checked in same area, after support check |
| `EXIT` sets `updated_at` to old timestamp | `EXIT`/`PERSONAL` sets `family_session_active_at` to NULL |
| `[Support Ticket #X]` prefix on responses | `[Family]` prefix on responses |
| Messages routed to support thread | Messages processed with family_group_id context |

**Key difference from support:** In family mode, messages still go through normal AI processing — they're just scoped to the family's shared resources instead of personal ones. Support mode bypasses AI entirely.

---

## Feature Breakdown

### 1. Shared Lists — Very Feasible
- While in family session: "Add milk to grocery list" → shared family list
- While in personal mode: "Add milk to grocery list" → personal list
- **How:** Add `family_group_id` column to `lists`. When in family session, create/query lists by family_group_id.

### 2. Cross-User Reminders — Feasible
- "Remind Dad to pick up Johnny at 6pm" → Dad gets SMS at 6pm
- **How:** Add `target_phone_number` to reminders. Resolve "Dad" via family_members table.
- **Note:** Could work in family session OR personal mode (always targets another person).

### 3. Shared Memories — Feasible
- While in family session: "Remember Johnny's allergist is Dr. Smith" → shared
- Any member in family session: "What's Johnny's allergist?" → finds it
- **How:** Add `family_group_id` to memories. In family session, search family memories.

### 4. Family Broadcasts — Feasible
- "Tell everyone dinner is at 7" → sends to all family members
- **How:** Look up all family_members, send individual SMS to each.

### 5. Task Assignment — Feasible
- "Assign mow the lawn to Johnny" → Johnny sees it in his tasks
- **How:** `assigned_to` field on list_items, or dedicated tasks table.

---

## Architecture Changes

### New Database Tables

```sql
family_groups
├── id (SERIAL PRIMARY KEY)
├── owner_phone_number (TEXT)
├── owner_phone_hash (TEXT)
├── group_name (TEXT) — e.g., "The Hodge Family"
├── created_at (TIMESTAMP)
└── max_members (INTEGER) — from subscription tier

family_members
├── id (SERIAL PRIMARY KEY)
├── family_group_id (INTEGER FK → family_groups)
├── phone_number (TEXT)
├── phone_hash (TEXT)
├── role (TEXT) — "owner", "adult", "child"
├── display_name (TEXT) — "Mom", "Dad", "Johnny"
├── aliases (TEXT) — JSON array: ["Mom", "Mother", "Sarah"]
├── joined_at (TIMESTAMP)
├── invited_by (TEXT)
└── UNIQUE(family_group_id, phone_number)

family_invitations
├── id (SERIAL PRIMARY KEY)
├── family_group_id (INTEGER FK → family_groups)
├── phone_number (TEXT)
├── phone_hash (TEXT)
├── display_name (TEXT)
├── role (TEXT)
├── invited_by (TEXT)
├── invited_by_name (TEXT)
├── status (TEXT) — "pending", "accepted", "declined"
├── created_at (TIMESTAMP)
└── responded_at (TIMESTAMP)
```

### Modified Existing Tables

```
users         → add family_session_active_at (TIMESTAMP, nullable)
              → add pending_family_invitation (BOOLEAN)
lists         → add family_group_id (INTEGER, nullable) — NULL = personal
memories      → add family_group_id (INTEGER, nullable) — NULL = personal
reminders     → add target_phone_number (TEXT, nullable) — NULL = self
list_items    → add assigned_to (TEXT, nullable) — phone of assignee
```

### New Files

| File | Purpose |
|------|---------|
| `models/family.py` | CRUD for family_groups, family_members, family_invitations |
| `services/family_service.py` | Session management, invitation flow, name resolution |

### Key Code Changes

| Area | What Changes |
|------|-------------|
| `main.py` (~line 812) | Add family session check after support modal check, before AI processing |
| `main.py` keyword handlers | Add `FAMILY`, `PERSONAL`, `INVITE`, `MY FAMILY` keyword handlers |
| `main.py` START/YES handler | Include pending family invitations in `has_pending_action` check |
| `ai_service.py` | Pass `family_group_id` context to AI; new actions for cross-user reminders, broadcasts |
| `models/list_model.py` | Accept optional `family_group_id`; query by it when in family session |
| `models/reminder.py` | Support `target_phone_number` for cross-user reminders |
| `models/memory.py` | Search both personal + family memories when in session |
| `tasks/reminder_tasks.py` | Deliver to `target_phone_number` when set |
| `services/tier_service.py` | Family tier limits, member count enforcement |
| `models/user.py` | Add `family_session_active_at`, `pending_family_invitation` to ALLOWED_USER_FIELDS |
| `config.py` | Add `FAMILY_SESSION_TIMEOUT`, `family_features` tier flag |

### SMS Keyword Commands

| Command | Action |
|---------|--------|
| `FAMILY` | Enter family session, show member list |
| `FAMILY <message>` | Enter session + process message in family context |
| `PERSONAL` or `EXIT` | Leave family session |
| `INVITE 555-123-4567 as Dad` | Send family invitation |
| `MY FAMILY` | Show family member list |
| `YES` / `NO` (with pending invite) | Accept or decline family invitation |

### Message Flow (Modified)

```
SMS in → /sms webhook
  → Validate Twilio signature
  → Deduplicate by MessageSid
  → Rate limit check
  → User lookup
  → STOP/START/HELP handlers
  → Onboarding check
  → Support session check          ← existing
  → Family session check            ← NEW: if family_session_active_at within 30 min
      → Route through AI with family_group_id context
      → Prefix response with [Family]
  → Family invitation handler       ← NEW: YES/NO for pending invites
  → FAMILY keyword handler          ← NEW: enters family session
  → PERSONAL keyword handler        ← NEW: exits family session
  → Normal keyword handlers
  → AI processing (personal context)
```

---

## Name Resolution — Who Is "Dad"?

**Setup flow:**
1. Owner creates family: "Start family plan"
2. Invites members: "INVITE 555-123-4567 as Dad" or via website
3. Invited person gets SMS: "Sarah invited you to join The Hodge Family on Remyndrs! Reply YES to join or NO to decline."
4. On acceptance, member is added with display_name and auto-generated aliases

**Resolution logic in `services/family_service.py`:**
- Input: "Dad", sender's phone_number
- Look up sender's family_group_id
- Search family_members for matching display_name or alias (case-insensitive)
- Return target phone_number

---

## Billing

Current config already exists in `config.py`:
- **$14.99/mo** base (4 members) or **$164.89/yr**
- **+$3.50/mo** per additional member (max 10)
- Owner pays; members get premium features through the family group
- Need Stripe logic for member count changes mid-billing-cycle

---

## Effort Estimate

| Component | Scope |
|-----------|-------|
| Database tables + migrations | Small |
| Family session modal (mirror support) | Medium |
| Family CRUD model + service | Medium |
| Invitation/acceptance flow | Medium |
| Shared lists (family_group_id scoping) | Medium |
| Cross-user reminders | Medium-High |
| Shared memories | Medium |
| Broadcasts | Low-Medium |
| Task assignment | Medium |
| AI prompt engineering (name resolution, new actions) | High |
| Stripe member billing | Medium |
| Tests | High |
| **Total** | **Large — multi-week project** |

---

## Recommended Phased Approach

**Phase 1 — Foundation:** Family groups, member management, invitation flow, family session modal
**Phase 2 — Shared Lists:** Session-scoped shared lists (the everyday use case)
**Phase 3 — Cross-User Reminders:** "Remind Dad to..." (the killer feature)
**Phase 4 — Shared Memories + Broadcasts:** Lower complexity additions
**Phase 5 — Task Assignment:** Builds on shared lists infrastructure

---

## Open Questions

1. **Session timeout** — 30 minutes like support, or longer/shorter for family mode?
2. **Cross-user reminders outside family session** — Should "Remind Dad to..." work even in personal mode? (Probably yes — it's inherently a family action.)
3. **Child accounts** — Restricted capabilities? (Can't remove members, can't see billing, etc.)
4. **Plan cancellation** — Shared resources become owned by billing owner? Split to individual accounts?
5. **Member removal** — What happens to their shared contributions?
6. **Invited non-users** — If Dad doesn't have Remyndrs yet, does the invite trigger onboarding for them?
7. **Multiple families** — Can a user be in more than one family group? (Probably not for v1.)
