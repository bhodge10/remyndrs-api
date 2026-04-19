# Shared Lists Feature — Technical Scope & Implementation Plan

**Status:** Phase 1 implemented (core sharing, no auto-notifications)
**Priority:** High — key Premium value prop differentiator
**Estimated Effort:** Medium — phased rollout

---

## Strategic Context

No paid Premium users yet. Shared Lists adds a **qualitatively different** capability to Premium (not just "more of the same" higher limits). The growth thesis:

1. **Viral loop:** Premium user shares a list → pulls up to 4 non-users into the platform → organic acquisition
2. **Stickiness:** Once a family relies on a shared grocery list, churn gets harder — you're disrupting a group workflow
3. **Premium differentiation:** Only Premium users can create/share lists. Free users can participate but not initiate
4. **Prioritized over Family Plan:** This is a lighter, shippable-now feature. Family Plan (`docs/family-plan.md`) is a larger multi-week project that can build on shared lists later

---

## Database Changes

### New table: `list_shares`

```sql
CREATE TABLE IF NOT EXISTS list_shares (
    id SERIAL PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    owner_phone TEXT NOT NULL,
    shared_with_phone TEXT NOT NULL,
    permission TEXT NOT NULL DEFAULT 'edit',
    status TEXT NOT NULL DEFAULT 'pending',  -- 'pending', 'accepted', 'declined'
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    accepted_at TIMESTAMP,
    owner_phone_hash TEXT,
    shared_with_phone_hash TEXT,
    UNIQUE(list_id, shared_with_phone)
);

CREATE INDEX IF NOT EXISTS idx_list_shares_shared_with ON list_shares(shared_with_phone);
CREATE INDEX IF NOT EXISTS idx_list_shares_list_id ON list_shares(list_id);
CREATE INDEX IF NOT EXISTS idx_list_shares_owner ON list_shares(owner_phone);
```

### New table: `list_change_log` (Phase 2 only)

```sql
CREATE TABLE IF NOT EXISTS list_change_log (
    id SERIAL PRIMARY KEY,
    list_id INTEGER NOT NULL REFERENCES lists(id) ON DELETE CASCADE,
    changed_by_phone TEXT NOT NULL,
    change_type TEXT NOT NULL,  -- 'item_added', 'item_removed', 'item_completed'
    item_text TEXT,
    notified BOOLEAN DEFAULT FALSE,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_list_change_log_unnotified ON list_change_log(list_id, notified) WHERE notified = FALSE;
```

**No changes to existing `lists` or `list_items` tables.** Sharing is a layer on top — the owner model stays the same.

---

## Permission Model

| Action | Owner (Premium) | Shared User (any tier) |
|--------|-----------------|----------------------|
| Create shared list | Yes | No |
| Delete shared list | Yes | No |
| Rename shared list | Yes | No |
| Share with others | Yes | No |
| Add items | Yes | Yes |
| Remove items | Yes | Yes |
| Mark complete/incomplete | Yes | Yes |
| View list | Yes | Yes |
| Leave shared list | N/A | Yes |

---

## Limits

| Constraint | Value | Rationale |
|-----------|-------|-----------|
| Max users per shared list | 4 | Household-sized, controls SMS costs |
| Max shared lists per Premium user | 3 | Prevents abuse, still generous |
| Max shared lists a non-premium user can be on | 5 | Enough for multiple households/contexts |
| Shared lists count against owner's list limit? | Yes | Owner's list, counts toward their 20 |
| Shared lists count against non-premium user's list limit? | No | They didn't create it — don't penalize |

Config constants to add in `config.py`:
```python
SHARED_LIST_MAX_MEMBERS = 4
SHARED_LIST_MAX_PER_USER = 3
SHARED_LIST_MAX_RECEIVED = 5
SHARED_LIST_NOTIFICATION_INTERVAL = 600  # seconds, Phase 2
```

---

## SMS Commands & AI Actions

### New AI actions

| Action | Trigger examples | Who can use |
|--------|-----------------|-------------|
| `share_list` | "Share grocery list with 555-123-4567" | Premium owner |
| `unshare_list` | "Stop sharing grocery list with 555-123-4567" | Premium owner |
| `unshare_list_all` | "Stop sharing grocery list" | Premium owner |
| `show_list_members` | "Who's on my grocery list?" | Premium owner |
| `leave_shared_list` | "Leave grocery list" | Shared user |

### Invitation flow

```
Owner:     "Share grocery list with 555-123-4567"
System:    Validates: owner is premium, list exists, under 4-share cap
           → to owner: "✓ Invitation sent to (555) 123-4567 for 'Grocery List'."
           → to recipient: "[Shared List] Brad shared 'Grocery List' with you!
                            Reply ACCEPT or DECLINE."

Recipient: "ACCEPT"
System:    → to recipient: "✓ You now have access to 'Grocery List'.
                            Text 'Show grocery list' to see it."
           → to owner: "(555) 123-4567 accepted your shared list 'Grocery List'."
```

**If recipient is NOT on Remyndrs:**
```
System:    → to phone: "Brad shared a list with you on Remyndrs!
                        Reply YES to join and see it."
           → streamlined onboarding (name + ZIP) → auto-accept share
```

### Existing commands that work on shared lists (no new syntax needed)

- "Show grocery list" — shows shared list if accepted
- "Add milk to grocery list" — adds item if user has access
- "Remove milk from grocery list" — removes item if user has access
- "Check off milk on grocery list" — marks complete if user has access
- "My lists" — shows personal lists + shared lists (with `[Shared]` prefix)

---

## Disambiguation: Personal vs Shared Lists

When a user has both a personal list and a shared list with the same name:

- Shared lists display with `[Shared]` prefix in list views: `"[Shared] Grocery List (3 items)"`
- If name collision on a command: prompt `"Did you mean your personal Grocery List or the shared one from Brad? Reply PERSONAL or SHARED."`
- Use existing `last_active_list` field to track which context the user was last in

---

## Code Changes by File

### Phase 1 — Core Sharing (MVP)

| File | Changes |
|------|---------|
| `database.py` | Add `list_shares` table creation in `init_db()` |
| `config.py` | Add `SHARED_LIST_MAX_MEMBERS`, `SHARED_LIST_MAX_PER_USER`, `SHARED_LIST_MAX_RECEIVED` |
| `models/list_model.py` | Add: `share_list()`, `unshare_list()`, `unshare_list_all()`, `get_shared_lists_for_user()`, `get_list_members()`, `is_list_shared_with()`, `accept_share()`, `decline_share()`, `leave_shared_list()`, `get_pending_shares()`. Modify: `get_lists()` to include accepted shared lists. Modify item operations (`add_list_item`, `delete_list_item`, `mark_item_complete`, etc.) to check share permissions via `can_user_access_list()` helper. |
| `services/tier_service.py` | Add `can_share_list(phone_number)` — checks premium status and shared list count |
| `services/ai_service.py` | Add `share_list`, `unshare_list`, `show_list_members`, `leave_shared_list` actions to AI prompt. Add phone number extraction to AI response parsing. |
| `routes/handlers/lists.py` | Add: `handle_share_list()`, `handle_unshare_list()`, `handle_show_list_members()`, `handle_leave_shared_list()`. Modify existing handlers to work with shared lists (permission checks before delete/rename). |
| `main.py` | Add keyword handlers for `ACCEPT` / `DECLINE` (shared list invitations). Wire up new AI action handlers in the action dispatch section. Modify "My Lists" display to include shared lists. |

### Phase 2 — Batched Notifications

| File | Changes |
|------|---------|
| `database.py` | Add `list_change_log` table creation |
| `models/list_model.py` | Add `log_list_change()`, `get_unnotified_changes()`, `mark_changes_notified()` |
| `tasks/reminder_tasks.py` | Add `notify_shared_list_changes()` periodic task |
| `celery_config.py` | Add beat schedule entry for notification task (every 10 min) |

### Phase 3 — Polish

- Disambiguation prompt for name collisions
- Shared list activity in admin dashboard (`admin_dashboard.py`)
- User preference to mute notifications per list
- "Leave list" confirmation flow

---

## SMS Cost Analysis (Phase 2 notifications)

| Scenario | SMS/day | Cost/day |
|----------|---------|----------|
| 1 shared list, 4 users, ~10 changes/day, 3 digest batches | 12 | $0.09 |
| 3 shared lists (max), 4 users each, active | 36 | $0.28 |
| 10 premium users at max usage | 360 | $2.84 |

At $8.99/mo per premium user, even worst-case max usage ($8.40/mo in SMS) leaves margin. Realistic usage will be much lower.

**Phase 1 has zero incremental SMS cost** — no auto-notifications, only invitation/acceptance messages.

---

## Implementation Order (Phase 1)

Suggested build sequence:

1. **Database & config** — `list_shares` table, config constants
2. **Model layer** — All sharing functions in `list_model.py`, permission checks
3. **Tier service** — `can_share_list()` premium gate
4. **AI service** — New actions in prompt, phone number parsing
5. **Handlers** — New share/unshare/accept/decline/leave handlers
6. **Main.py integration** — Keyword handlers (ACCEPT/DECLINE), action dispatch, modify "My Lists"
7. **Tests** — Share flow, permission checks, limit enforcement, edge cases
8. **Manual QA** — End-to-end SMS testing with two phones

---

## Edge Cases to Handle

- Owner deletes a shared list → cascade deletes shares, notify shared users "Grocery List was deleted by Brad"
- Owner downgrades from Premium → shared lists become read-only for all (owner can still delete). Show upgrade prompt if owner tries to share.
- Shared user blocks Remyndrs (Twilio 21610) → mark share as inactive, don't retry
- Recipient declines → owner gets notified, share row deleted
- Owner tries to share with themselves → reject with friendly message
- Phone number format variations → normalize before lookup (existing `normalize_phone()`)
- Shared user tries to delete the list → "Only the list owner can delete shared lists."
- Shared user tries to rename → "Only the list owner can rename shared lists."
- Owner at list limit tries to create a new list to share → standard tier limit applies

---

## Testing Strategy

Using existing `ConversationSimulator` pattern with two simulated users:

```python
# Test share flow
premium_user = create_test_user(premium_status='premium')
free_user = create_test_user(phone='+15559876544')

# Owner creates and shares
simulator.send(premium_user, "Create a grocery list")
simulator.send(premium_user, f"Share grocery list with {free_user.phone}")

# Recipient accepts
simulator.send(free_user, "ACCEPT")

# Both can add items
simulator.send(free_user, "Add milk to grocery list")
simulator.send(premium_user, "Show grocery list")  # should show milk

# Permission checks
simulator.send(free_user, "Delete grocery list")  # should be rejected
```

---

## Open Questions

1. **Should shared list items show who added them?** e.g., `"1. Milk (added by Brad)"` — nice for accountability but makes messages longer
2. **Should we allow the owner to set a shared list as "view-only"?** Permission column supports it but adds UX complexity
3. **What happens to pending shares after 7 days?** Auto-expire? Remind?
4. **Should we allow sharing by name instead of phone number?** e.g., "Share with Mom" — requires a contacts/family-member mapping that doesn't exist yet. Defer to Family Plan.

## Potential Future Features (Not Implemented)

### Transitive sharing (member-invited sharing)

**Idea:** Allow accepted members of a shared list (not just the owner) to invite new people to that list. Privacy is preserved because the inviter supplies the invitee's phone number directly.

**Status:** Deferred. Too many design variables to resolve before committing to an implementation.

**Key variables to resolve before building:**
- Who qualifies as an inviter? Owner only → any accepted member → premium-accepted members only
- Depth limit (prevent chain-invite trees). Likely depth=1 — owner-direct-invitees can invite, but their invitees cannot
- Rate limit (transitive invites per list per 24h) to prevent flooding the owner
- Owner veto window — notification with "REMOVE [name]" shortcut during a time window before the invitee sees the invite
- Cap accounting — does a transitive invite count against `SHARED_LIST_MAX_MEMBERS` only, or also against the member's `SHARED_LIST_MAX_PER_USER`?
- Attribution in Sarah's invite message ("Heather shared Brad's grocery list" vs "Heather shared grocery list")
- Behavior when owner is opted-out / blocked
- SMS cost: every transitive invite adds at least one extra SMS (owner notification)

**When to revisit:** If direct sharing shows strong adoption but owners report they'd rather let invited members add people themselves (common in household / roommate / project-collab scenarios).
