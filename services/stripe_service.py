"""
Stripe Service
Handles all Stripe payment and subscription operations
"""

import stripe
from datetime import datetime, timedelta
from database import get_db_connection, return_db_connection
from config import (
    logger, STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET,
    STRIPE_PRICE_IDS, APP_BASE_URL, STRIPE_ENABLED,
    TIER_FREE, TIER_PREMIUM, TIER_FAMILY, ENCRYPTION_ENABLED
)

# Initialize Stripe
if STRIPE_SECRET_KEY:
    stripe.api_key = STRIPE_SECRET_KEY


def create_checkout_session(phone_number: str, plan: str, billing_cycle: str) -> dict:
    """
    Create a Stripe Checkout session for subscription.

    Args:
        phone_number: User's phone number
        plan: 'premium' or 'family'
        billing_cycle: 'monthly' or 'annual'

    Returns:
        dict with 'url' for checkout or 'error' message
    """
    if not STRIPE_ENABLED:
        return {'error': 'Payment system is not configured'}

    # Get the appropriate price ID
    price_key = f"{plan}_{billing_cycle}"
    price_id = STRIPE_PRICE_IDS.get(price_key)

    if not price_id:
        logger.error(f"No price ID configured for {price_key}")
        return {'error': 'Invalid plan selected'}

    try:
        # Check if user already has a Stripe customer ID
        customer_id = get_stripe_customer_id(phone_number)

        # Create or retrieve customer
        if not customer_id:
            customer = stripe.Customer.create(
                metadata={'phone_number': phone_number}
            )
            customer_id = customer.id
            save_stripe_customer_id(phone_number, customer_id)

        # Check for existing active subscription to prevent double-billing
        existing_subs = stripe.Subscription.list(customer=customer_id, status='active', limit=1)
        if existing_subs.data:
            logger.warning(f"User {phone_number[-4:]} already has active subscription {existing_subs.data[0].id}")
            return {'error': 'You already have an active subscription. Text ACCOUNT to manage it.'}

        # Create checkout session
        session = stripe.checkout.Session.create(
            customer=customer_id,
            payment_method_types=['card'],
            line_items=[{
                'price': price_id,
                'quantity': 1,
            }],
            mode='subscription',
            success_url=f"{APP_BASE_URL}/payment/success?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{APP_BASE_URL}/payment/cancelled",
            metadata={
                'phone_number': phone_number,
                'plan': plan,
                'billing_cycle': billing_cycle,
            },
            subscription_data={
                'metadata': {
                    'phone_number': phone_number,
                    'plan': plan,
                }
            }
        )

        logger.info(f"Created checkout session for {phone_number[-4:]}: {plan} {billing_cycle}")
        return {'url': session.url, 'session_id': session.id}

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating checkout: {e}")
        return {'error': 'Unable to create payment session. Please try again.'}


def create_customer_portal_session(phone_number: str) -> dict:
    """
    Create a Stripe Customer Portal session for managing subscription.

    Returns:
        dict with 'url' for portal or 'error' message
    """
    if not STRIPE_ENABLED:
        return {'error': 'Payment system is not configured'}

    customer_id = get_stripe_customer_id(phone_number)
    if not customer_id:
        return {'error': 'No subscription found for this account'}

    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id,
            return_url=f"{APP_BASE_URL}/account",
        )
        return {'url': session.url}

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error creating portal session: {e}")
        return {'error': 'Unable to access account portal. Please try again.'}


def handle_webhook_event(payload: bytes, sig_header: str) -> dict:
    """
    Handle incoming Stripe webhook events.

    Returns:
        dict with 'success' bool and optional 'error' message
    """
    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError as e:
        logger.error(f"Invalid webhook payload: {e}")
        return {'success': False, 'error': 'Invalid payload'}
    except stripe.error.SignatureVerificationError as e:
        logger.error(f"Invalid webhook signature: {e}")
        return {'success': False, 'error': 'Invalid signature'}

    event_type = event['type']
    data = event['data']['object']

    logger.info(f"Processing Stripe webhook: {event_type}")

    # Handle different event types
    if event_type == 'checkout.session.completed':
        handle_checkout_completed(data)
    elif event_type == 'customer.subscription.created':
        handle_subscription_created(data)
    elif event_type == 'customer.subscription.updated':
        handle_subscription_updated(data)
    elif event_type == 'customer.subscription.deleted':
        handle_subscription_cancelled(data)
    elif event_type == 'invoice.payment_failed':
        handle_payment_failed(data)
    elif event_type == 'invoice.paid':
        handle_invoice_paid(data)
    else:
        logger.info(f"Unhandled webhook event type: {event_type}")

    return {'success': True}


def handle_checkout_completed(session):
    """Handle successful checkout session."""
    phone_number = session.get('metadata', {}).get('phone_number')
    plan = session.get('metadata', {}).get('plan', 'premium')

    if not phone_number:
        logger.error("Checkout completed but no phone number in metadata")
        return

    # Update user's subscription status
    tier = TIER_PREMIUM if plan == 'premium' else TIER_FAMILY
    update_user_subscription(
        phone_number=phone_number,
        tier=tier,
        stripe_subscription_id=session.get('subscription'),
        status='active'
    )

    logger.info(f"Checkout completed for {phone_number[-4:]}: {tier}")

    # Send confirmation SMS
    send_subscription_confirmation(phone_number, tier)


def handle_subscription_created(subscription):
    """Handle new subscription creation."""
    phone_number = subscription.get('metadata', {}).get('phone_number')
    plan = subscription.get('metadata', {}).get('plan', 'premium')

    if not phone_number:
        # Try to get from customer
        customer_id = subscription.get('customer')
        phone_number = get_phone_by_customer_id(customer_id)

    if not phone_number:
        logger.error("Subscription created but no phone number found")
        return

    tier = TIER_PREMIUM if plan == 'premium' else TIER_FAMILY
    update_user_subscription(
        phone_number=phone_number,
        tier=tier,
        stripe_subscription_id=subscription.get('id'),
        status='active'
    )

    logger.info(f"Subscription created for {phone_number[-4:]}: {tier}")


def handle_subscription_updated(subscription):
    """Handle subscription updates (plan changes, etc.)."""
    phone_number = subscription.get('metadata', {}).get('phone_number')

    if not phone_number:
        customer_id = subscription.get('customer')
        phone_number = get_phone_by_customer_id(customer_id)

    if not phone_number:
        logger.error("Subscription updated but no phone number found")
        return

    status = subscription.get('status')
    plan = subscription.get('metadata', {}).get('plan', 'premium')
    tier = TIER_PREMIUM if plan == 'premium' else TIER_FAMILY

    # Map Stripe status to our status
    if status in ['active', 'trialing']:
        update_user_subscription(phone_number, tier, subscription.get('id'), 'active')
    elif status == 'past_due':
        update_user_subscription(phone_number, tier, subscription.get('id'), 'past_due')
    elif status in ['canceled', 'unpaid']:
        update_user_subscription(phone_number, TIER_FREE, None, 'cancelled')

    logger.info(f"Subscription updated for {phone_number[-4:]}: {status}")


def handle_subscription_cancelled(subscription):
    """Handle subscription cancellation."""
    phone_number = subscription.get('metadata', {}).get('phone_number')

    if not phone_number:
        customer_id = subscription.get('customer')
        phone_number = get_phone_by_customer_id(customer_id)

    if not phone_number:
        logger.error("Subscription cancelled but no phone number found")
        return

    # If user is deleting their account, skip downgrade messaging —
    # the delete flow handles its own confirmation message.
    from models.user import get_user
    user = get_user(phone_number)
    if user and (user.get('pending_delete_account') or user.get('opted_out')):
        logger.info(f"Subscription cancelled for {phone_number[-4:]} (account deletion in progress, skipping cancellation notice)")
        return

    # Downgrade to free tier
    update_user_subscription(phone_number, TIER_FREE, None, 'cancelled')

    logger.info(f"Subscription cancelled for {phone_number[-4:]}")

    # Send cancellation notice
    send_cancellation_notice(phone_number)


def handle_payment_failed(invoice):
    """Handle failed payment."""
    customer_id = invoice.get('customer')
    phone_number = get_phone_by_customer_id(customer_id)

    if not phone_number:
        logger.error("Payment failed but no phone number found")
        return

    logger.warning(f"Payment failed for {phone_number[-4:]}")

    # Send payment failed notice
    send_payment_failed_notice(phone_number)


def handle_invoice_paid(invoice):
    """Handle successful invoice payment (renewal)."""
    customer_id = invoice.get('customer')
    phone_number = get_phone_by_customer_id(customer_id)

    if phone_number:
        logger.info(f"Invoice paid for {phone_number[-4:]}")


# =====================================================
# DATABASE HELPERS
# =====================================================

def get_stripe_customer_id(phone_number: str) -> str | None:
    """Get Stripe customer ID for a user."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                'SELECT stripe_customer_id FROM users WHERE phone_hash = %s',
                (phone_hash,)
            )
            result = c.fetchone()
            if not result:
                c.execute(
                    'SELECT stripe_customer_id FROM users WHERE phone_number = %s',
                    (phone_number,)
                )
                result = c.fetchone()
        else:
            c.execute(
                'SELECT stripe_customer_id FROM users WHERE phone_number = %s',
                (phone_number,)
            )
            result = c.fetchone()

        return result[0] if result and result[0] else None
    except Exception as e:
        logger.error(f"Error getting Stripe customer ID: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def save_stripe_customer_id(phone_number: str, customer_id: str):
    """Save Stripe customer ID for a user."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute(
            'UPDATE users SET stripe_customer_id = %s WHERE phone_number = %s',
            (customer_id, phone_number)
        )
        conn.commit()
        logger.info(f"Saved Stripe customer ID for {phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error saving Stripe customer ID: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def get_phone_by_customer_id(customer_id: str) -> str | None:
    """Get phone number by Stripe customer ID."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute(
            'SELECT phone_number FROM users WHERE stripe_customer_id = %s',
            (customer_id,)
        )
        result = c.fetchone()
        return result[0] if result else None
    except Exception as e:
        logger.error(f"Error getting phone by customer ID: {e}")
        return None
    finally:
        if conn:
            return_db_connection(conn)


def update_user_subscription(phone_number: str, tier: str, stripe_subscription_id: str | None, status: str):
    """Update user's subscription status in database."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if tier != TIER_FREE:
            c.execute(
                '''UPDATE users SET
                   premium_status = %s,
                   stripe_subscription_id = %s,
                   subscription_status = %s,
                   premium_since = COALESCE(premium_since, CURRENT_TIMESTAMP)
                   WHERE phone_number = %s''',
                (tier, stripe_subscription_id, status, phone_number)
            )
        else:
            # Downgrading to free
            c.execute(
                '''UPDATE users SET
                   premium_status = %s,
                   stripe_subscription_id = NULL,
                   subscription_status = %s
                   WHERE phone_number = %s''',
                (tier, status, phone_number)
            )

        conn.commit()
        logger.info(f"Updated subscription for {phone_number[-4:]}: {tier} ({status})")
    except Exception as e:
        logger.error(f"Error updating user subscription: {e}")
    finally:
        if conn:
            return_db_connection(conn)


def get_user_subscription(phone_number: str) -> dict:
    """Get user's subscription details."""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if ENCRYPTION_ENABLED:
            from utils.encryption import hash_phone
            phone_hash = hash_phone(phone_number)
            c.execute(
                '''SELECT premium_status, premium_since, stripe_subscription_id, subscription_status
                   FROM users WHERE phone_hash = %s''',
                (phone_hash,)
            )
            result = c.fetchone()
            if not result:
                c.execute(
                    '''SELECT premium_status, premium_since, stripe_subscription_id, subscription_status
                       FROM users WHERE phone_number = %s''',
                    (phone_number,)
                )
                result = c.fetchone()
        else:
            c.execute(
                '''SELECT premium_status, premium_since, stripe_subscription_id, subscription_status
                   FROM users WHERE phone_number = %s''',
                (phone_number,)
            )
            result = c.fetchone()

        if result:
            sub_data = {
                'tier': result[0] or TIER_FREE,
                'since': result[1],
                'subscription_id': result[2],
                'status': result[3] or 'none',
            }
            # Fetch current_period_end from Stripe if subscription exists
            if sub_data['subscription_id'] and STRIPE_ENABLED:
                try:
                    stripe_sub = stripe.Subscription.retrieve(sub_data['subscription_id'])
                    sub_data['current_period_end'] = stripe_sub.current_period_end
                except Exception as e:
                    logger.debug(f"Could not fetch Stripe subscription details: {e}")
            return sub_data
        return {'tier': TIER_FREE, 'since': None, 'subscription_id': None, 'status': 'none'}
    except Exception as e:
        logger.error(f"Error getting user subscription: {e}")
        return {'tier': TIER_FREE, 'since': None, 'subscription_id': None, 'status': 'none'}
    finally:
        if conn:
            return_db_connection(conn)


def cancel_stripe_subscription(phone_number: str) -> dict:
    """
    Cancel a user's Stripe subscription immediately.

    Returns:
        dict with 'success' bool and optional 'error' message
    """
    if not STRIPE_ENABLED:
        return {'success': False, 'error': 'Payment system is not configured'}

    subscription = get_user_subscription(phone_number)
    sub_id = subscription.get('subscription_id')

    if not sub_id:
        # No active subscription to cancel
        return {'success': True, 'message': 'No active subscription'}

    try:
        stripe.Subscription.cancel(sub_id)
        update_user_subscription(phone_number, TIER_FREE, None, 'cancelled')
        logger.info(f"Cancelled Stripe subscription {sub_id} for {phone_number[-4:]}")
        return {'success': True}
    except stripe.error.StripeError as e:
        logger.error(f"Stripe error cancelling subscription for {phone_number[-4:]}: {e}")
        return {'success': False, 'error': str(e)}


# =====================================================
# SMS NOTIFICATIONS
# =====================================================

def send_subscription_confirmation(phone_number: str, tier: str):
    """Send SMS confirmation of subscription."""
    try:
        from services.sms_service import send_sms

        plan_name = "Premium" if tier == TIER_PREMIUM else "Family Plan"
        message = f"Welcome to Remyndrs {plan_name}! Here's what you've unlocked:\n\n- Unlimited reminders per day\n- 20 lists (30 items each)\n- Unlimited saved memories\n- Recurring reminders\n- Priority support\n\nCancel anytime. Enjoy!"

        send_sms(phone_number, message, message_type="billing")
        logger.info(f"Sent subscription confirmation to {phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error sending subscription confirmation: {e}")


def send_cancellation_notice(phone_number: str):
    """Send SMS notice of subscription cancellation with feedback request."""
    try:
        from services.sms_service import send_sms

        message = (
            "Your Remyndrs subscription has been cancelled. You've been moved to the free plan.\n\n"
            "We'd love to know why you cancelled:\n"
            "1. Too expensive\n"
            "2. Not using enough\n"
            "3. Missing a feature\n"
            "4. Other\n\n"
            "Reply with a number, or text SKIP. Text UPGRADE anytime to resubscribe!"
        )

        # Set pending cancellation feedback flag
        conn_flag = None
        try:
            conn_flag = get_db_connection()
            c = conn_flag.cursor()
            c.execute(
                "UPDATE users SET pending_cancellation_feedback = TRUE WHERE phone_number = %s",
                (phone_number,)
            )
            conn_flag.commit()
        except Exception as db_err:
            logger.error(f"Error setting pending_cancellation_feedback: {db_err}")
        finally:
            if conn_flag:
                return_db_connection(conn_flag)

        send_sms(phone_number, message, message_type="billing")
        logger.info(f"Sent cancellation notice with feedback request to {phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error sending cancellation notice: {e}")


def send_payment_failed_notice(phone_number: str):
    """Send SMS notice of failed payment."""
    try:
        from services.sms_service import send_sms

        message = "We couldn't process your Remyndrs payment. Please update your payment method to keep your premium features. Text ACCOUNT to manage your subscription."

        send_sms(phone_number, message, message_type="billing")
        logger.info(f"Sent payment failed notice to {phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error sending payment failed notice: {e}")


def issue_refund(phone_number: str, amount_cents: int = None, reason: str = 'requested_by_customer') -> dict:
    """
    Issue a refund for a user's most recent payment.

    Args:
        phone_number: User's phone number
        amount_cents: Amount in cents to refund (None for full refund)
        reason: Stripe refund reason

    Returns:
        dict with 'success' bool and optional 'refund_id' or 'error'
    """
    if not STRIPE_ENABLED:
        return {'success': False, 'error': 'Payment system is not configured'}

    customer_id = get_stripe_customer_id(phone_number)
    if not customer_id:
        return {'success': False, 'error': 'No Stripe customer found for this user'}

    try:
        # Get the most recent charge for this customer
        charges = stripe.Charge.list(customer=customer_id, limit=1)
        if not charges.data:
            return {'success': False, 'error': 'No charges found for this customer'}

        charge = charges.data[0]

        # Build refund params
        refund_params = {
            'charge': charge.id,
            'reason': reason,
        }
        if amount_cents:
            refund_params['amount'] = amount_cents

        refund = stripe.Refund.create(**refund_params)

        logger.info(f"Refund {refund.id} issued for {phone_number[-4:]}: {amount_cents or 'full'} cents")
        return {'success': True, 'refund_id': refund.id}

    except stripe.error.StripeError as e:
        logger.error(f"Stripe error issuing refund for {phone_number[-4:]}: {e}")
        return {'success': False, 'error': str(e)}


def get_upgrade_message(phone_number: str) -> str:
    """Get the upgrade prompt message with pricing info."""
    from config import PRICING, APP_BASE_URL, PREMIUM_MONTHLY_PRICE

    premium_monthly = PRICING[TIER_PREMIUM]['monthly'] / 100

    return f"""Upgrade to Remyndrs Premium!

{PREMIUM_MONTHLY_PRICE}/month
- Unlimited reminders per day
- 20 lists (30 items each)
- Unlimited saved memories
- Recurring reminders
- Priority support tickets
- Cancel anytime

Text PREMIUM to get your upgrade link."""


# =====================================================
# INVESTMENT HEALTH METRICS
# =====================================================
# One fetch of all subscriptions powers five metrics. Cached briefly so the
# admin page can be opened repeatedly without re-paginating Stripe.

_INVESTMENT_METRICS_CACHE: dict = {"data": None, "fetched_at": 0.0}
_INVESTMENT_METRICS_TTL = 300  # 5 minutes


def _normalized_monthly_amount(subscription) -> float:
    """Return the monthly USD amount for a subscription, normalizing yearly to /12."""
    try:
        items = subscription.get('items', {}).get('data', [])
        if not items:
            return 0.0
        price = items[0].get('price', {}) or {}
        amount_cents = price.get('unit_amount') or 0
        recurring = price.get('recurring', {}) or {}
        interval = recurring.get('interval', 'month')
        interval_count = recurring.get('interval_count') or 1
        amount = amount_cents / 100.0
        if interval == 'year':
            return amount / (12 * interval_count)
        if interval == 'month':
            return amount / interval_count
        if interval == 'week':
            return amount * (52 / 12) / interval_count
        if interval == 'day':
            return amount * (365 / 12) / interval_count
        return amount
    except Exception:
        return 0.0


def get_investment_health_metrics(force_refresh: bool = False) -> dict:
    """
    Fetch and compute the live inputs for the Investment Health dashboard.

    Returns a dict with:
      - current_users: count of active subscriptions
      - net_now: new subs minus cancellations, this calendar month to date (UTC)
      - net_3mo: same metric, full calendar month from 3 months ago
      - churn_rate: trailing 90-day avg monthly churn (%)
      - arpu: avg MRR per active sub minus $1 SMS cost
      - fetched_at: ISO timestamp
      - error: optional error message (other fields still set to defaults)

    Falls back to defaults from services.investment_health.DEFAULT_INPUTS if
    Stripe is unavailable or returns no data.
    """
    import time
    from services.investment_health import DEFAULT_INPUTS, SMS_COST_PER_USER

    now_ts = time.time()
    if (
        not force_refresh
        and _INVESTMENT_METRICS_CACHE["data"] is not None
        and now_ts - _INVESTMENT_METRICS_CACHE["fetched_at"] < _INVESTMENT_METRICS_TTL
    ):
        return _INVESTMENT_METRICS_CACHE["data"]

    fallback = {
        "current_users": DEFAULT_INPUTS["current_users"],
        "net_now": DEFAULT_INPUTS["net_now"],
        "net_3mo": DEFAULT_INPUTS["net_3mo"],
        "churn_rate": DEFAULT_INPUTS["churn_rate"],
        "arpu": DEFAULT_INPUTS["arpu"],
        "fetched_at": datetime.utcnow().isoformat() + "Z",
    }

    if not STRIPE_ENABLED:
        fallback["error"] = "Stripe not configured"
        return fallback

    try:
        # Pull all subscriptions ever created (active + canceled). For an
        # account with hundreds of subs this is a few paginated calls.
        subs = list(stripe.Subscription.list(status='all', limit=100).auto_paging_iter())
    except stripe.error.StripeError as e:
        logger.error(f"Investment Health: Stripe fetch failed: {e}")
        fallback["error"] = str(e)
        return fallback
    except Exception as e:
        logger.error(f"Investment Health: unexpected error fetching subs: {e}")
        fallback["error"] = str(e)
        return fallback

    now = datetime.utcnow()
    start_of_this_month = datetime(now.year, now.month, 1)

    # 3 months ago: full calendar month
    three_months_ago_year = now.year
    three_months_ago_month = now.month - 3
    while three_months_ago_month <= 0:
        three_months_ago_month += 12
        three_months_ago_year -= 1
    start_of_3mo = datetime(three_months_ago_year, three_months_ago_month, 1)
    # Start of the month after the 3-months-ago month
    next_month = three_months_ago_month + 1
    next_year = three_months_ago_year
    if next_month > 12:
        next_month = 1
        next_year += 1
    end_of_3mo = datetime(next_year, next_month, 1)

    # Active count + ARPU
    active_subs = [s for s in subs if s.get('status') in ('active', 'trialing')]
    current_users = len(active_subs)
    if active_subs:
        total_monthly = sum(_normalized_monthly_amount(s) for s in active_subs)
        arpu_gross = total_monthly / len(active_subs)
        arpu = max(0.0, arpu_gross - SMS_COST_PER_USER)
    else:
        arpu = DEFAULT_INPUTS["arpu"]

    def _ts_to_dt(ts):
        return datetime.utcfromtimestamp(ts) if ts else None

    def _net_new_in_range(start_dt: datetime, end_dt: datetime) -> int:
        """New subs in [start, end) minus cancellations in [start, end)."""
        new_count = 0
        cancelled_count = 0
        for s in subs:
            created = _ts_to_dt(s.get('created'))
            if created and start_dt <= created < end_dt:
                new_count += 1
            cancelled_at = _ts_to_dt(s.get('canceled_at'))
            if cancelled_at and start_dt <= cancelled_at < end_dt:
                cancelled_count += 1
        return new_count - cancelled_count

    net_now = _net_new_in_range(start_of_this_month, now + timedelta(seconds=1))
    net_3mo = _net_new_in_range(start_of_3mo, end_of_3mo)

    # Trailing 90-day average monthly churn rate
    # For each of the 3 prior full calendar months, churn = cancellations
    # during the month / active subs at start of the month.
    churn_samples = []
    cursor_year = now.year
    cursor_month = now.month
    for _ in range(3):
        cursor_month -= 1
        if cursor_month <= 0:
            cursor_month += 12
            cursor_year -= 1
        month_start = datetime(cursor_year, cursor_month, 1)
        nm = cursor_month + 1
        ny = cursor_year
        if nm > 12:
            nm = 1
            ny += 1
        month_end = datetime(ny, nm, 1)

        # Subs active at month_start: created before month_start AND
        # (not canceled OR canceled on/after month_start)
        active_at_start = 0
        cancelled_in_month = 0
        for s in subs:
            created = _ts_to_dt(s.get('created'))
            cancelled_at = _ts_to_dt(s.get('canceled_at'))
            if created and created < month_start and (cancelled_at is None or cancelled_at >= month_start):
                active_at_start += 1
            if cancelled_at and month_start <= cancelled_at < month_end:
                cancelled_in_month += 1

        if active_at_start > 0:
            churn_samples.append(100.0 * cancelled_in_month / active_at_start)

    if churn_samples:
        churn_rate = sum(churn_samples) / len(churn_samples)
    else:
        churn_rate = DEFAULT_INPUTS["churn_rate"]

    result = {
        "current_users": current_users,
        "net_now": net_now,
        "net_3mo": net_3mo,
        "churn_rate": round(churn_rate, 2),
        "arpu": round(arpu, 2),
        "fetched_at": now.isoformat() + "Z",
    }

    _INVESTMENT_METRICS_CACHE["data"] = result
    _INVESTMENT_METRICS_CACHE["fetched_at"] = now_ts

    return result
