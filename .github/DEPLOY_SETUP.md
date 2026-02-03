# GitHub Actions Deploy Setup

This repository uses GitHub Actions to control Render deployments. This prevents documentation updates from triggering unnecessary deploys.

## How It Works

- **Code changes** (Python, YAML, etc.) → Triggers Render deployment via GitHub Actions
- **Docs changes** (Markdown files) → No deployment triggered
- **Saves ~5-7 minutes** per docs-only update

## One-Time Setup Required

You need to add Render deploy hook URLs as GitHub secrets.

### Step 1: Get Render Deploy Hooks

For each of your 4 services, get the deploy hook URL:

1. Go to https://dashboard.render.com
2. Click on a service (e.g., **sms-reminders-api**)
3. Go to **Settings** tab
4. Scroll to **Deploy Hook**
5. Click **Create Deploy Hook** (if not already created)
6. Copy the URL (looks like: `https://api.render.com/deploy/srv-xxxxx?key=yyyyy`)

Repeat for all 4 services:
- `sms-reminders-api`
- `sms-reminders-worker`
- `sms-reminders-beat`
- `sms-reminders-monitoring`

### Step 2: Add Secrets to GitHub

1. Go to https://github.com/bhodge10/sms-reminders/settings/secrets/actions
2. Click **New repository secret**
3. Add these 4 secrets:

| Secret Name | Value |
|-------------|-------|
| `RENDER_DEPLOY_HOOK_API` | Deploy hook URL for sms-reminders-api |
| `RENDER_DEPLOY_HOOK_WORKER` | Deploy hook URL for sms-reminders-worker |
| `RENDER_DEPLOY_HOOK_BEAT` | Deploy hook URL for sms-reminders-beat |
| `RENDER_DEPLOY_HOOK_MONITORING` | Deploy hook URL for sms-reminders-monitoring |

### Step 3: Test the Workflow

1. Merge a code change (not just docs) to `main`
2. Watch the GitHub Actions run: https://github.com/bhodge10/sms-reminders/actions
3. Verify all 4 services deploy on Render

## What's Ignored

The following changes will **NOT** trigger deployments:

- `**.md` files (README, CLAUDE.md, docs, etc.)
- `docs/**` directory
- `LICENSE` file
- `.gitignore` file
- `.github/**` (workflow files themselves)

## Manual Deploy

If you need to manually trigger a deploy:

**Option 1: Via Render Dashboard**
- Go to service → Manual Deploy → Deploy latest commit

**Option 2: Via Deploy Hook**
```bash
curl -X POST "https://api.render.com/deploy/srv-xxxxx?key=yyyyy"
```

**Option 3: Re-run GitHub Action**
- Go to failed/skipped workflow
- Click "Re-run jobs"

## Troubleshooting

**Workflow doesn't run:**
- Check `.github/workflows/deploy.yml` exists in `main` branch
- Check GitHub Actions are enabled for the repo

**Workflow runs but Render doesn't deploy:**
- Verify secrets are set correctly (no typos)
- Check deploy hook URLs are valid
- Look at workflow logs for curl errors

**Workflow skipped (docs-only change):**
- This is expected! Check the "Files changed" tab on the PR
- If only `.md` files changed, deployment correctly skipped

## Reverting to Auto-Deploy

If you want to go back to Render's automatic deployments:

1. Delete `.github/workflows/deploy.yml`
2. Change `autoDeploy: false` back to `autoDeploy: true` in `render.yaml`
3. Delete the GitHub secrets (optional)
