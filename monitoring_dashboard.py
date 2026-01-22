"""
Monitoring Dashboard UI
Visual dashboard for the multi-agent monitoring system.
"""

import secrets
from datetime import datetime
from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials

from config import ADMIN_USERNAME, ADMIN_PASSWORD, logger, APP_BASE_URL, ENVIRONMENT
from utils.validation import log_security_event

router = APIRouter()
security = HTTPBasic()


def verify_admin(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify admin credentials"""
    if not ADMIN_PASSWORD:
        raise HTTPException(status_code=500, detail="Admin password not configured")

    correct_username = secrets.compare_digest(credentials.username, ADMIN_USERNAME)
    correct_password = secrets.compare_digest(credentials.password, ADMIN_PASSWORD)

    if not (correct_username and correct_password):
        log_security_event("AUTH_FAILURE", {"username": credentials.username, "endpoint": "monitoring"})
        raise HTTPException(
            status_code=401,
            detail="Invalid credentials",
            headers={"WWW-Authenticate": "Basic"},
        )
    return credentials.username


@router.get("/admin/monitoring", response_class=HTMLResponse)
async def monitoring_dashboard(admin: str = Depends(verify_admin)):
    """Render the monitoring dashboard UI"""

    html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Monitoring Dashboard - Remyndrs</title>
    <style>
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}

        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: linear-gradient(135deg, #1a1a2e 0%, #16213e 100%);
            min-height: 100vh;
            color: #e0e0e0;
        }}

        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }}

        header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 30px;
            padding-bottom: 20px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }}

        header h1 {{
            font-size: 1.8em;
            font-weight: 600;
            color: #fff;
        }}

        header .env-badge {{
            background: {'#27ae60' if ENVIRONMENT == 'production' else '#e67e22'};
            color: white;
            padding: 5px 15px;
            border-radius: 20px;
            font-size: 0.8em;
            font-weight: 500;
        }}

        .nav-links {{
            display: flex;
            gap: 15px;
        }}

        .nav-links a {{
            color: #a0a0a0;
            text-decoration: none;
            padding: 8px 15px;
            border-radius: 5px;
            transition: all 0.2s;
        }}

        .nav-links a:hover {{
            background: rgba(255,255,255,0.1);
            color: #fff;
        }}

        /* Health Score Card */
        .health-card {{
            background: linear-gradient(135deg, #2d3436 0%, #1e272e 100%);
            border-radius: 20px;
            padding: 30px;
            margin-bottom: 30px;
            display: flex;
            align-items: center;
            gap: 40px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.3);
        }}

        .health-gauge {{
            position: relative;
            width: 180px;
            height: 180px;
        }}

        .health-gauge svg {{
            transform: rotate(-90deg);
        }}

        .health-gauge circle {{
            fill: none;
            stroke-width: 12;
        }}

        .health-gauge .bg {{
            stroke: #3d3d3d;
        }}

        .health-gauge .progress {{
            stroke-linecap: round;
            transition: stroke-dashoffset 1s ease, stroke 0.5s ease;
        }}

        .health-gauge .score {{
            position: absolute;
            top: 50%;
            left: 50%;
            transform: translate(-50%, -50%);
            text-align: center;
        }}

        .health-gauge .score-value {{
            font-size: 3em;
            font-weight: 700;
            color: #fff;
        }}

        .health-gauge .score-label {{
            font-size: 0.9em;
            color: #888;
            text-transform: uppercase;
        }}

        .health-stats {{
            flex: 1;
            display: grid;
            grid-template-columns: repeat(4, 1fr);
            gap: 20px;
        }}

        .stat-item {{
            text-align: center;
            padding: 15px;
            background: rgba(255,255,255,0.05);
            border-radius: 10px;
        }}

        .stat-value {{
            font-size: 1.8em;
            font-weight: 600;
            color: #fff;
        }}

        .stat-label {{
            font-size: 0.85em;
            color: #888;
            margin-top: 5px;
        }}

        /* Grid Layout */
        .grid {{
            display: grid;
            grid-template-columns: 2fr 1fr;
            gap: 20px;
        }}

        @media (max-width: 1000px) {{
            .grid {{ grid-template-columns: 1fr; }}
            .health-card {{ flex-direction: column; }}
            .health-stats {{ grid-template-columns: repeat(2, 1fr); }}
        }}

        /* Cards */
        .card {{
            background: #2d3436;
            border-radius: 15px;
            padding: 20px;
            margin-bottom: 20px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.2);
        }}

        .card-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 1px solid rgba(255,255,255,0.1);
        }}

        .card-header h2 {{
            font-size: 1.1em;
            font-weight: 600;
            color: #fff;
        }}

        .card-header .badge {{
            background: #3498db;
            color: white;
            padding: 3px 10px;
            border-radius: 10px;
            font-size: 0.8em;
        }}

        /* Issues List */
        .issue-list {{
            max-height: 400px;
            overflow-y: auto;
        }}

        .issue-item {{
            display: flex;
            align-items: center;
            padding: 12px;
            margin-bottom: 8px;
            background: rgba(255,255,255,0.05);
            border-radius: 8px;
            transition: background 0.2s;
        }}

        .issue-item:hover {{
            background: rgba(255,255,255,0.1);
        }}

        .issue-severity {{
            width: 10px;
            height: 10px;
            border-radius: 50%;
            margin-right: 12px;
        }}

        .issue-severity.critical {{ background: #e74c3c; box-shadow: 0 0 10px #e74c3c; }}
        .issue-severity.high {{ background: #e67e22; }}
        .issue-severity.medium {{ background: #f1c40f; }}
        .issue-severity.low {{ background: #27ae60; }}

        .issue-content {{
            flex: 1;
        }}

        .issue-type {{
            font-weight: 500;
            color: #fff;
        }}

        .issue-detail {{
            font-size: 0.85em;
            color: #888;
            margin-top: 3px;
        }}

        .issue-id {{
            color: #666;
            font-size: 0.85em;
        }}

        /* Patterns */
        .pattern-item {{
            display: flex;
            align-items: center;
            padding: 10px 0;
            border-bottom: 1px solid rgba(255,255,255,0.05);
        }}

        .pattern-bar {{
            flex: 1;
            height: 8px;
            background: #3d3d3d;
            border-radius: 4px;
            margin: 0 15px;
            overflow: hidden;
        }}

        .pattern-bar .fill {{
            height: 100%;
            background: linear-gradient(90deg, #3498db, #9b59b6);
            border-radius: 4px;
            transition: width 0.5s ease;
        }}

        .pattern-count {{
            color: #888;
            font-size: 0.9em;
            min-width: 40px;
            text-align: right;
        }}

        /* Trend Chart */
        .trend-chart {{
            height: 200px;
            display: flex;
            align-items: flex-end;
            gap: 8px;
            padding-top: 20px;
        }}

        .trend-bar {{
            flex: 1;
            background: linear-gradient(180deg, #3498db, #2980b9);
            border-radius: 4px 4px 0 0;
            min-height: 5px;
            transition: height 0.5s ease;
            position: relative;
        }}

        .trend-bar:hover {{
            opacity: 0.8;
        }}

        .trend-bar .tooltip {{
            position: absolute;
            bottom: 100%;
            left: 50%;
            transform: translateX(-50%);
            background: #1a1a2e;
            color: #fff;
            padding: 5px 10px;
            border-radius: 5px;
            font-size: 0.8em;
            white-space: nowrap;
            opacity: 0;
            transition: opacity 0.2s;
            pointer-events: none;
        }}

        .trend-bar:hover .tooltip {{
            opacity: 1;
        }}

        /* Alert Settings */
        .alert-settings {{
            display: grid;
            grid-template-columns: repeat(2, 1fr);
            gap: 15px;
        }}

        .setting-item {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 12px;
            background: rgba(255,255,255,0.05);
            border-radius: 8px;
        }}

        .setting-label {{
            color: #888;
            font-size: 0.9em;
        }}

        .setting-value {{
            color: #fff;
            font-weight: 500;
        }}

        .setting-value.active {{ color: #27ae60; }}
        .setting-value.inactive {{ color: #e74c3c; }}

        /* Buttons */
        .btn {{
            display: inline-block;
            padding: 10px 20px;
            border: none;
            border-radius: 8px;
            font-size: 0.9em;
            font-weight: 500;
            cursor: pointer;
            transition: all 0.2s;
            text-decoration: none;
        }}

        .btn-primary {{
            background: #3498db;
            color: white;
        }}

        .btn-primary:hover {{
            background: #2980b9;
        }}

        .btn-secondary {{
            background: rgba(255,255,255,0.1);
            color: #fff;
        }}

        .btn-secondary:hover {{
            background: rgba(255,255,255,0.2);
        }}

        .btn-danger {{
            background: #e74c3c;
            color: white;
        }}

        .btn-sm {{
            padding: 6px 12px;
            font-size: 0.8em;
        }}

        /* Actions Bar */
        .actions-bar {{
            display: flex;
            gap: 10px;
            margin-top: 15px;
        }}

        /* Loading */
        .loading {{
            text-align: center;
            padding: 40px;
            color: #888;
        }}

        .spinner {{
            display: inline-block;
            width: 30px;
            height: 30px;
            border: 3px solid rgba(255,255,255,0.1);
            border-top-color: #3498db;
            border-radius: 50%;
            animation: spin 1s linear infinite;
        }}

        @keyframes spin {{
            to {{ transform: rotate(360deg); }}
        }}

        /* Toast */
        .toast {{
            position: fixed;
            bottom: 20px;
            right: 20px;
            background: #2d3436;
            color: #fff;
            padding: 15px 25px;
            border-radius: 10px;
            box-shadow: 0 5px 20px rgba(0,0,0,0.3);
            transform: translateY(100px);
            opacity: 0;
            transition: all 0.3s ease;
            z-index: 1000;
        }}

        .toast.show {{
            transform: translateY(0);
            opacity: 1;
        }}

        .toast.success {{ border-left: 4px solid #27ae60; }}
        .toast.error {{ border-left: 4px solid #e74c3c; }}

        /* Empty State */
        .empty-state {{
            text-align: center;
            padding: 40px;
            color: #666;
        }}

        .empty-state .icon {{
            font-size: 3em;
            margin-bottom: 15px;
        }}
    </style>
</head>
<body>
    <div class="container">
        <header>
            <div>
                <h1>🔍 Monitoring Dashboard</h1>
            </div>
            <div class="nav-links">
                <a href="/admin/dashboard">← Main Dashboard</a>
                <span class="env-badge">{ENVIRONMENT.upper()}</span>
            </div>
        </header>

        <!-- Health Score Card -->
        <div class="health-card">
            <div class="health-gauge">
                <svg width="180" height="180" viewBox="0 0 180 180">
                    <circle class="bg" cx="90" cy="90" r="78"></circle>
                    <circle class="progress" id="healthProgress" cx="90" cy="90" r="78"
                            stroke-dasharray="490" stroke-dashoffset="490"></circle>
                </svg>
                <div class="score">
                    <div class="score-value" id="healthScore">--</div>
                    <div class="score-label" id="healthStatus">Loading...</div>
                </div>
            </div>
            <div class="health-stats">
                <div class="stat-item">
                    <div class="stat-value" id="totalInteractions">--</div>
                    <div class="stat-label">Interactions (7d)</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="issueRate">--%</div>
                    <div class="stat-label">Issue Rate</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="resolutionRate">--%</div>
                    <div class="stat-label">Resolution Rate</div>
                </div>
                <div class="stat-item">
                    <div class="stat-value" id="openIssues">--</div>
                    <div class="stat-label">Open Issues</div>
                </div>
            </div>
        </div>

        <div class="grid">
            <!-- Left Column -->
            <div>
                <!-- Open Issues -->
                <div class="card">
                    <div class="card-header">
                        <h2>📋 Open Issues</h2>
                        <span class="badge" id="issueCount">0</span>
                    </div>
                    <div class="issue-list" id="issueList">
                        <div class="loading"><div class="spinner"></div></div>
                    </div>
                    <div class="actions-bar">
                        <button class="btn btn-primary btn-sm" onclick="runPipeline()">
                            ▶ Run Pipeline
                        </button>
                        <button class="btn btn-secondary btn-sm" onclick="loadIssues()">
                            ↻ Refresh
                        </button>
                    </div>
                </div>

                <!-- Health Trend -->
                <div class="card">
                    <div class="card-header">
                        <h2>📈 Health Trend (30 Days)</h2>
                    </div>
                    <div class="trend-chart" id="trendChart">
                        <div class="loading"><div class="spinner"></div></div>
                    </div>
                </div>
            </div>

            <!-- Right Column -->
            <div>
                <!-- Issue Patterns -->
                <div class="card">
                    <div class="card-header">
                        <h2>🎯 Top Patterns</h2>
                    </div>
                    <div id="patternList">
                        <div class="loading"><div class="spinner"></div></div>
                    </div>
                </div>

                <!-- Alert Settings -->
                <div class="card">
                    <div class="card-header">
                        <h2>🔔 Alert Settings</h2>
                        <button class="btn btn-secondary btn-sm" onclick="testAlerts()">
                            Test Alerts
                        </button>
                    </div>
                    <div class="alert-settings" id="alertSettings">
                        <div class="loading"><div class="spinner"></div></div>
                    </div>
                </div>

                <!-- Quick Actions -->
                <div class="card">
                    <div class="card-header">
                        <h2>⚡ Quick Actions</h2>
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 10px;">
                        <button class="btn btn-primary" onclick="runPipeline()">
                            ▶ Run Full Pipeline
                        </button>
                        <button class="btn btn-secondary" onclick="saveSnapshot()">
                            📸 Save Health Snapshot
                        </button>
                        <button class="btn btn-secondary" onclick="generateReport()">
                            📊 Generate Weekly Report
                        </button>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Toast -->
    <div class="toast" id="toast"></div>

    <script>
        const API_BASE = '';

        // Toast notification
        function showToast(message, type = 'success') {{
            const toast = document.getElementById('toast');
            toast.textContent = message;
            toast.className = 'toast ' + type + ' show';
            setTimeout(() => toast.classList.remove('show'), 3000);
        }}

        // Fetch with auth
        async function fetchAPI(endpoint, options = {{}}) {{
            const response = await fetch(API_BASE + endpoint, {{
                ...options,
                headers: {{
                    'Content-Type': 'application/json',
                    ...options.headers
                }}
            }});
            if (!response.ok) throw new Error('API Error');
            return response.json();
        }}

        // Load health metrics
        async function loadHealth() {{
            try {{
                const data = await fetchAPI('/admin/tracker/health?days=7');

                // Update score
                const score = Math.round(data.health_score || 0);
                document.getElementById('healthScore').textContent = score;
                document.getElementById('healthStatus').textContent = data.health_status || 'unknown';

                // Update gauge
                const progress = document.getElementById('healthProgress');
                const circumference = 490;
                const offset = circumference - (score / 100) * circumference;
                progress.style.strokeDashoffset = offset;

                // Color based on score
                let color = '#27ae60'; // green
                if (score < 70) color = '#e74c3c'; // red
                else if (score < 85) color = '#f1c40f'; // yellow
                progress.style.stroke = color;

                // Update stats
                document.getElementById('totalInteractions').textContent =
                    (data.total_interactions || 0).toLocaleString();
                document.getElementById('issueRate').textContent =
                    (data.issue_rate || 0) + '%';
                document.getElementById('resolutionRate').textContent =
                    (data.resolution_rate || 0) + '%';
                document.getElementById('openIssues').textContent =
                    data.open_issues || 0;

            }} catch (e) {{
                console.error('Failed to load health:', e);
            }}
        }}

        // Load open issues
        async function loadIssues() {{
            try {{
                const data = await fetchAPI('/admin/tracker/open?limit=20');
                const list = document.getElementById('issueList');
                const issues = data.issues || [];

                document.getElementById('issueCount').textContent = issues.length;

                if (issues.length === 0) {{
                    list.innerHTML = `
                        <div class="empty-state">
                            <div class="icon">✅</div>
                            <p>No open issues! System is healthy.</p>
                        </div>
                    `;
                    return;
                }}

                list.innerHTML = issues.map(issue => `
                    <div class="issue-item">
                        <div class="issue-severity ${{issue.severity}}"></div>
                        <div class="issue-content">
                            <div class="issue-type">${{issue.issue_type}}</div>
                            <div class="issue-detail">
                                ${{issue.message_in ? issue.message_in.substring(0, 60) + '...' : 'No message'}}
                            </div>
                        </div>
                        <div class="issue-id">#${{issue.id}}</div>
                    </div>
                `).join('');

            }} catch (e) {{
                console.error('Failed to load issues:', e);
                document.getElementById('issueList').innerHTML =
                    '<div class="empty-state">Failed to load issues</div>';
            }}
        }}

        // Load patterns
        async function loadPatterns() {{
            try {{
                const data = await fetchAPI('/admin/validator/patterns');
                const list = document.getElementById('patternList');
                const types = data.type_distribution || [];

                if (types.length === 0) {{
                    list.innerHTML = '<div class="empty-state">No patterns detected yet</div>';
                    return;
                }}

                const maxCount = Math.max(...types.map(t => t.total));

                list.innerHTML = types.slice(0, 8).map(type => `
                    <div class="pattern-item">
                        <span style="min-width: 140px; font-size: 0.9em;">${{type.type}}</span>
                        <div class="pattern-bar">
                            <div class="fill" style="width: ${{(type.total / maxCount) * 100}}%"></div>
                        </div>
                        <span class="pattern-count">${{type.total}}</span>
                    </div>
                `).join('');

            }} catch (e) {{
                console.error('Failed to load patterns:', e);
            }}
        }}

        // Load trend
        async function loadTrend() {{
            try {{
                const data = await fetchAPI('/admin/tracker/trends?days=30');
                const chart = document.getElementById('trendChart');
                const trend = data.trend || [];

                if (trend.length === 0) {{
                    chart.innerHTML = '<div class="empty-state">No trend data yet</div>';
                    return;
                }}

                const maxScore = 100;
                chart.innerHTML = trend.map(day => {{
                    const height = ((day.health_score || 0) / maxScore) * 100;
                    const date = day.date ? day.date.substring(5) : '';
                    return `
                        <div class="trend-bar" style="height: ${{height}}%">
                            <div class="tooltip">${{date}}: ${{Math.round(day.health_score || 0)}}</div>
                        </div>
                    `;
                }}).join('');

            }} catch (e) {{
                console.error('Failed to load trend:', e);
            }}
        }}

        // Load alert settings
        async function loadAlertSettings() {{
            try {{
                const data = await fetchAPI('/admin/alerts/settings');
                const container = document.getElementById('alertSettings');

                container.innerHTML = `
                    <div class="setting-item">
                        <span class="setting-label">Alerts</span>
                        <span class="setting-value ${{data.alerts_enabled ? 'active' : 'inactive'}}">
                            ${{data.alerts_enabled ? 'Enabled' : 'Disabled'}}
                        </span>
                    </div>
                    <div class="setting-item">
                        <span class="setting-label">Teams</span>
                        <span class="setting-value ${{data.teams_configured ? 'active' : 'inactive'}}">
                            ${{data.teams_configured ? 'Connected' : 'Not configured'}}
                        </span>
                    </div>
                    <div class="setting-item">
                        <span class="setting-label">Email</span>
                        <span class="setting-value ${{data.email_recipients?.length ? 'active' : 'inactive'}}">
                            ${{data.email_recipients?.length || 0}} recipient(s)
                        </span>
                    </div>
                    <div class="setting-item">
                        <span class="setting-label">SMS</span>
                        <span class="setting-value ${{data.sms_enabled ? 'active' : 'inactive'}}">
                            ${{data.sms_enabled ? data.sms_recipients?.length + ' number(s)' : 'Disabled'}}
                        </span>
                    </div>
                `;

            }} catch (e) {{
                console.error('Failed to load alert settings:', e);
            }}
        }}

        // Actions
        async function runPipeline() {{
            showToast('Starting pipeline...', 'success');
            try {{
                const data = await fetchAPI('/admin/monitoring/run?hours=24');
                showToast(`Pipeline complete: ${{data.issues_found}} issues found`, 'success');
                loadHealth();
                loadIssues();
                loadPatterns();
            }} catch (e) {{
                showToast('Pipeline failed', 'error');
            }}
        }}

        async function saveSnapshot() {{
            try {{
                const data = await fetchAPI('/admin/tracker/snapshot', {{ method: 'POST' }});
                showToast(`Snapshot saved: Health ${{Math.round(data.health_score)}}`, 'success');
            }} catch (e) {{
                showToast('Failed to save snapshot', 'error');
            }}
        }}

        async function generateReport() {{
            showToast('Generating report...', 'success');
            try {{
                const data = await fetchAPI('/admin/tracker/report');
                showToast(`Report ready: Health ${{Math.round(data.current_health?.health_score || 0)}}`, 'success');
            }} catch (e) {{
                showToast('Failed to generate report', 'error');
            }}
        }}

        async function testAlerts() {{
            showToast('Sending test alerts...', 'success');
            try {{
                const data = await fetchAPI('/admin/alerts/test', {{ method: 'POST' }});
                const results = data.results || {{}};
                const channels = [];
                if (results.teams) channels.push('Teams');
                if (results.email) channels.push('Email');
                if (results.sms) channels.push('SMS');

                if (channels.length > 0) {{
                    showToast(`Test sent to: ${{channels.join(', ')}}`, 'success');
                }} else {{
                    showToast('No alerts configured', 'error');
                }}
            }} catch (e) {{
                showToast('Test failed', 'error');
            }}
        }}

        // Initial load
        document.addEventListener('DOMContentLoaded', () => {{
            loadHealth();
            loadIssues();
            loadPatterns();
            loadTrend();
            loadAlertSettings();

            // Auto-refresh every 60 seconds
            setInterval(() => {{
                loadHealth();
                loadIssues();
            }}, 60000);
        }});
    </script>
</body>
</html>
    """

    return HTMLResponse(content=html)
