# Remyndrs Onboarding Flow

## Overview
4-step onboarding process collecting: First Name, Last Name, Email, ZIP Code

---

## Step 0: Welcome (New User)

**Trigger:** User texts anything for the first time

**If user texted "START":**
```
Welcome to Remyndrs!

Thanks for texting START!

I help you remember anything - from grocery lists to important reminders.

Just 4 quick questions to get started (takes about 1 minute), then you're all set!

What's your first name?
```

**If user texted anything else:**
```
Welcome to Remyndrs!

I help you remember anything - from grocery lists to important reminders.

Just 4 quick questions to get started (takes about 1 minute), then you're all set!

What's your first name?
```

---

## Step 1: First Name

**User provides first name only:**
```
Nice to meet you, {first_name}! Last name?
```

**User provides full name (two words):**
```
Nice to meet you, {first_name} {last_name}!

Email for account recovery?

(We only use this for important updates - no spam!)
```
*(Skips to Step 3)*

**Error - User enters email instead of name:**
```
That looks like an email address! I'll ask for that in a moment.

What's your first name?
```

**Error - User texts START/UNSTOP/BEGIN again:**
```
You're already in setup! Let's continue.

What's your first name?
```

---

## Step 2: Last Name

**User provides last name:**
```
Got it! Email for account recovery?

(We only use this for important updates - no spam!)
```

---

## Step 3: Email

**User provides valid email:**
```
Perfect! Last question: ZIP code?

(This helps me send reminders at the right time in your timezone)
```

### Email Validation Errors

**Email has spaces:**
```
Email addresses can't have spaces in them!

What's your email address?
(Should look like: name@email.com)
```

**Email missing @ symbol:**
```
Oops! That email is missing the @ symbol.

It should look like: yourname@gmail.com

What's your email address?
```

**Email missing domain:**
```
Almost! But that email needs a domain (like @gmail.com or @yahoo.com).

What's your email address?
```

**User tries to skip email:**
```
I understand privacy concerns! But I need your email for account recovery - if you forget your info or get a new phone, this is how you get back in.

I promise we only use it for:
- Account recovery
- Critical service updates (like scheduled maintenance)
- No marketing emails
- No selling your data

What's your email address?
```

---

## Step 4: ZIP Code

**User provides valid ZIP code:**

*System automatically saves first memory: "Signed up for Remyndrs on {date}"*

```
Perfect! You're all set, {first_name}!

I just saved your first memory: "Signed up for Remyndrs on {date}"

Try asking me: "What do I have saved?"

(Tip: Check your next message to save me as a contact!)
```

**Follow-up MMS (5-second delay):**
```
Tap to save Remyndrs to your contacts!
[VCF contact card attached]
```

### ZIP Code Validation Errors

**International postal code (Canadian/UK):**
```
I recognize that's an international postal code!

Currently, Remyndrs only supports US ZIP codes for timezone detection.

If you're outside the US, you can enter a US ZIP code that matches your timezone:
- Eastern Time: 10001 (New York)
- Central Time: 60601 (Chicago)
- Mountain Time: 80202 (Denver)
- Pacific Time: 90001 (Los Angeles)

What ZIP code should I use?
```

**City name entered instead of ZIP:**
```
Hmm, that looks like a city name or address!

I just need the 5-digit ZIP code (like 45202).

What's your ZIP code?
```

**Wrong number of digits:**
```
ZIP codes are exactly 5 digits!

You entered {digit_count} digit(s). Try again?

What's your 5-digit ZIP code?
```

**User tries to skip ZIP:**
```
I totally get it! But here's why I need it:

Without your ZIP code, I can't figure out your timezone. That means reminders might arrive at the wrong time (imagine getting a 2pm reminder at 5am).

Your 5-digit ZIP code helps me send reminders when YOU need them.

What's your ZIP code?
```

---

## Global Commands (Available During Any Step)

### Help
**User texts "help" or "?":**
```
I'm helping you set up your account! It's quick - just 4 questions total.

You're currently on step {step} of 4:
{current_step_prompt}

Why I need this info:
- Name: Personalize your experience
- Email: Account recovery & important updates only
- ZIP: Set your timezone for accurate reminders

Text "cancel" to cancel setup, or just answer the question to continue!
```

### Cancel
**User texts "cancel", "nevermind", or "quit":**
```
No problem! Setup cancelled.

If you change your mind, just text me again and we'll start fresh.

Have a great day!
```

### Restart
**User texts "restart":**
```
No problem, {first_name}! Let's start over.

What's your first name?
```
*(Or without name if not yet collected)*

### Service Keywords During Onboarding
**User tries to use service before completing setup:**
```
Almost there! Please finish setup first.

You're on step {step} of 4 - just {remaining} more question(s)!

{current_step_prompt}
```

---

## Flow Diagram

```
[New User Texts]
       |
       v
   Step 0: Welcome --> Ask First Name
       |
       v
   Step 1: First Name
       |
       +--> Full name given? --> Skip to Step 3
       |
       v
   Step 2: Last Name --> Ask Email
       |
       v
   Step 3: Email --> Ask ZIP
       |
       v
   Step 4: ZIP Code --> Complete!
       |
       v
   [Send VCF Contact Card]
       |
       v
   [User Ready to Use Service]
```
