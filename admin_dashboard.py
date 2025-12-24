"""
Admin Dashboard
HTML dashboard for viewing metrics
"""

from fastapi import APIRouter
from fastapi.responses import HTMLResponse
from services.metrics_service import get_all_metrics

router = APIRouter()


@router.get("/admin/dashboard", response_class=HTMLResponse)
async def admin_dashboard():
    """Render HTML admin dashboard"""
    metrics = get_all_metrics()

    # Build referral rows
    referral_rows = ""
    for source, count in metrics.get('referrals', []):
        referral_rows += f"<tr><td>{source}</td><td>{count}</td></tr>"

    # Build daily signups data for simple chart
    signups = metrics.get('daily_signups', [])
    signup_labels = [str(row[0]) for row in signups[:14]]  # Last 14 days
    signup_values = [row[1] for row in signups[:14]]

    # Reverse to show oldest first
    signup_labels.reverse()
    signup_values.reverse()

    # Premium stats
    premium = metrics.get('premium_stats', {})
    reminder_stats = metrics.get('reminder_stats', {})
    engagement = metrics.get('engagement', {})

    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Remyndrs Admin Dashboard</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Oxygen, Ubuntu, sans-serif;
            background: #f5f5f5;
            padding: 20px;
            color: #333;
        }}
        h1 {{ margin-bottom: 20px; color: #2c3e50; }}
        h2 {{ margin: 20px 0 10px; color: #34495e; font-size: 1.2em; }}

        .cards {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 15px;
            margin-bottom: 30px;
        }}
        .card {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .card-title {{
            font-size: 0.9em;
            color: #7f8c8d;
            margin-bottom: 5px;
        }}
        .card-value {{
            font-size: 2em;
            font-weight: bold;
            color: #2c3e50;
        }}
        .card-subtitle {{
            font-size: 0.8em;
            color: #95a5a6;
        }}
        .card.green .card-value {{ color: #27ae60; }}
        .card.blue .card-value {{ color: #3498db; }}
        .card.orange .card-value {{ color: #e67e22; }}
        .card.purple .card-value {{ color: #9b59b6; }}

        table {{
            width: 100%;
            background: white;
            border-collapse: collapse;
            border-radius: 8px;
            overflow: hidden;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        th, td {{
            padding: 12px 15px;
            text-align: left;
            border-bottom: 1px solid #ecf0f1;
        }}
        th {{
            background: #34495e;
            color: white;
            font-weight: 500;
        }}
        tr:hover {{
            background: #f8f9fa;
        }}

        .section {{
            margin-bottom: 30px;
        }}

        .chart {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
        }}
        .bar-chart {{
            display: flex;
            align-items: flex-end;
            height: 150px;
            gap: 8px;
            padding-top: 20px;
        }}
        .bar {{
            flex: 1;
            background: #3498db;
            border-radius: 4px 4px 0 0;
            min-width: 20px;
            position: relative;
        }}
        .bar:hover {{
            background: #2980b9;
        }}
        .bar-label {{
            position: absolute;
            bottom: -20px;
            left: 50%;
            transform: translateX(-50%);
            font-size: 0.7em;
            color: #7f8c8d;
            white-space: nowrap;
        }}
        .bar-value {{
            position: absolute;
            top: -18px;
            left: 50%;
            transform: translateX(-50%);
            font-size: 0.75em;
            color: #2c3e50;
            font-weight: bold;
        }}

        .grid-2 {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(300px, 1fr));
            gap: 20px;
        }}

        .refresh-note {{
            text-align: center;
            color: #95a5a6;
            font-size: 0.9em;
            margin-top: 30px;
        }}
    </style>
</head>
<body>
    <h1>Remyndrs Dashboard</h1>

    <div class="cards">
        <div class="card">
            <div class="card-title">Total Users</div>
            <div class="card-value">{metrics.get('total_users', 0)}</div>
            <div class="card-subtitle">completed onboarding</div>
        </div>
        <div class="card green">
            <div class="card-title">Active (7 days)</div>
            <div class="card-value">{metrics.get('active_7d', 0)}</div>
            <div class="card-subtitle">sent a message</div>
        </div>
        <div class="card blue">
            <div class="card-title">Active (30 days)</div>
            <div class="card-value">{metrics.get('active_30d', 0)}</div>
            <div class="card-subtitle">sent a message</div>
        </div>
        <div class="card purple">
            <div class="card-title">Premium Users</div>
            <div class="card-value">{premium.get('premium', 0)}</div>
            <div class="card-subtitle">free: {premium.get('free', 0)}</div>
        </div>
    </div>

    <div class="grid-2">
        <div class="section">
            <h2>Engagement Stats</h2>
            <table>
                <tr><th>Metric</th><th>Value</th></tr>
                <tr><td>Avg Messages / User</td><td>{engagement.get('avg_messages_per_user', 0)}</td></tr>
                <tr><td>Avg Memories / User</td><td>{engagement.get('avg_memories_per_user', 0)}</td></tr>
                <tr><td>Avg Reminders / User</td><td>{engagement.get('avg_reminders_per_user', 0)}</td></tr>
                <tr><td>Total Messages</td><td>{engagement.get('total_messages', 0)}</td></tr>
                <tr><td>Total Memories</td><td>{engagement.get('total_memories', 0)}</td></tr>
                <tr><td>Total Reminders</td><td>{engagement.get('total_reminders', 0)}</td></tr>
            </table>
        </div>

        <div class="section">
            <h2>Reminder Delivery</h2>
            <table>
                <tr><th>Status</th><th>Count</th></tr>
                <tr><td>Pending</td><td>{reminder_stats.get('pending', 0)}</td></tr>
                <tr><td>Sent</td><td>{reminder_stats.get('sent', 0)}</td></tr>
                <tr><td>Failed</td><td>{reminder_stats.get('failed', 0)}</td></tr>
                <tr><td><strong>Completion Rate</strong></td><td><strong>{reminder_stats.get('completion_rate', 0)}%</strong></td></tr>
            </table>
        </div>
    </div>

    <div class="section">
        <h2>Daily Signups (Last 14 Days)</h2>
        <div class="chart">
            <div class="bar-chart">
                {"".join([
                    f'<div class="bar" style="height: {max(10, (v / max(signup_values) * 100) if signup_values and max(signup_values) > 0 else 10)}%"><span class="bar-value">{v}</span><span class="bar-label">{signup_labels[i][-5:]}</span></div>'
                    for i, v in enumerate(signup_values)
                ]) if signup_values else '<div style="color: #95a5a6; padding: 40px;">No signup data yet</div>'}
            </div>
        </div>
    </div>

    <div class="section">
        <h2>Referral Sources</h2>
        <table>
            <tr><th>Source</th><th>Users</th></tr>
            {referral_rows if referral_rows else '<tr><td colspan="2" style="color: #95a5a6;">No referral data yet</td></tr>'}
        </table>
    </div>

    <p class="refresh-note">Refresh page to update metrics</p>
</body>
</html>
    """

    return HTMLResponse(content=html)
