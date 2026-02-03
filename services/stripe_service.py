"""
Stripe Service
Handles all Stripe payment and subscription operations
"""

import stripe
from datetime import datetime
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
            return {
                'tier': result[0] or TIER_FREE,
                'since': result[1],
                'subscription_id': result[2],
                'status': result[3] or 'none',
            }
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
        message = f"Welcome to Remyndrs {plan_name}! You now have access to unlimited reminders, recurring reminders, and priority support. Enjoy!"

        send_sms(phone_number, message)
        logger.info(f"Sent subscription confirmation to {phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error sending subscription confirmation: {e}")


def send_cancellation_notice(phone_number: str):
    """Send SMS notice of subscription cancellation."""
    try:
        from services.sms_service import send_sms

        message = "Your Remyndrs subscription has been cancelled. You've been moved to the free plan. Text UPGRADE anytime to resubscribe!"

        send_sms(phone_number, message)
        logger.info(f"Sent cancellation notice to {phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error sending cancellation notice: {e}")


def send_payment_failed_notice(phone_number: str):
    """Send SMS notice of failed payment."""
    try:
        from services.sms_service import send_sms

        message = "We couldn't process your Remyndrs payment. Please update your payment method to keep your premium features. Text ACCOUNT to manage your subscription."

        send_sms(phone_number, message)
        logger.info(f"Sent payment failed notice to {phone_number[-4:]}")
    except Exception as e:
        logger.error(f"Error sending payment failed notice: {e}")


def get_upgrade_message(phone_number: str) -> str:
    """Get the upgrade prompt message with pricing info."""
    from config import PRICING, APP_BASE_URL

    premium_monthly = PRICING[TIER_PREMIUM]['monthly'] / 100
    premium_annual = PRICING[TIER_PREMIUM]['annual'] / 100
    family_monthly = PRICING[TIER_FAMILY]['monthly'] / 100
    family_annual = PRICING[TIER_FAMILY]['annual'] / 100

    return f"""Upgrade to Remyndrs Premium!

PREMIUM - ${premium_monthly:.2f}/month (or ${premium_annual:.2f}/year)
- Unlimited reminders
- Recurring reminders (daily, weekly, monthly)
- Unlimited lists & memories
- Priority support

FAMILY - ${family_monthly:.2f}/month (or ${family_annual:.2f}/year)
- Everything in Premium
- Up to 4 family members
- Add more members for $3.50/each

Visit {APP_BASE_URL}/upgrade to subscribe!

Reply PREMIUM or FAMILY to get your upgrade link."""
