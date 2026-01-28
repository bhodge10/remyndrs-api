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

        /* Form Inputs */
        .form-group {{
            margin-bottom: 15px;
        }}

        .form-group label {{
            display: block;
            color: #888;
            font-size: 0.85em;
            margin-bottom: 5px;
        }}

        .form-group input[type="text"],
        .form-group input[type="email"],
        .form-group input[type="number"],
        .form-group textarea {{
            width: 100%;
            padding: 10px 12px;
            background: rgba(255,255,255,0.1);
            border: 1px solid rgba(255,255,255,0.2);
            border-radius: 6px;
            color: #fff;
            font-size: 0.9em;
        }}

        .form-group input:focus,
        .form-group textarea:focus {{
            outline: none;
            border-color: #3498db;
        }}

        .form-group textarea {{
            min-height: 60px;
            resize: vertical;
        }}

        .form-row {{
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 15px;
        }}

        .toggle-switch {{
            display: flex;
            align-items: center;
            gap: 10px;
        }}

        .toggle-switch input[type="checkbox"] {{
            width: 40px;
            height: 22px;
            appearance: none;
            background: rgba(255,255,255,0.2);
            border-radius: 11px;
            position: relative;
            cursor: pointer;
            transition: background 0.3s;
        }}

        .toggle-switch input[type="checkbox"]:checked {{
            background: #27ae60;
        }}

        .toggle-switch input[type="checkbox"]::before {{
            content: '';
            position: absolute;
            width: 18px;
            height: 18px;
            background: white;
            border-radius: 50%;
            top: 2px;
            left: 2px;
            transition: transform 0.3s;
        }}

        .toggle-switch input[type="checkbox"]:checked::before {{
            transform: translateX(18px);
        }}

        .form-actions {{
            display: flex;
            gap: 10px;
            margin-top: 15px;
            padding-top: 15px;
            border-top: 1px solid rgba(255,255,255,0.1);
        }}

        .form-hint {{
            font-size: 0.75em;
            color: #666;
            margin-top: 4px;
        }}

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

        /* Modal */
        .modal-overlay {{
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.7);
            display: flex;
            align-items: center;
            justify-content: center;
            z-index: 2000;
        }}

        .modal-content {{
            background: #1e272e;
            border-radius: 12px;
            width: 90%;
            max-width: 600px;
            max-height: 80vh;
            overflow-y: auto;
            box-shadow: 0 10px 40px rgba(0,0,0,0.5);
        }}

        .modal-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            padding: 20px;
            border-bottom: 1px solid #333;
        }}

        .modal-header h3 {{
            margin: 0;
            color: #fff;
        }}

        .modal-close {{
            background: none;
            border: none;
            color: #888;
            font-size: 24px;
            cursor: pointer;
        }}

        .modal-close:hover {{
            color: #fff;
        }}

        .modal-body {{
            padding: 20px;
        }}

        .detail-row {{
            display: flex;
            justify-content: space-between;
            padding: 10px 0;
            border-bottom: 1px solid #333;
        }}

        .detail-label {{
            color: #888;
            font-size: 0.9em;
        }}

        .detail-value {{
            color: #fff;
            font-weight: 500;
        }}

        .detail-section {{
            margin-top: 15px;
        }}

        .detail-message {{
            background: #2d3436;
            padding: 15px;
            border-radius: 8px;
            margin-top: 8px;
            color: #fff;
            white-space: pre-wrap;
            word-break: break-word;
            font-family: inherit;
            line-height: 1.5;
        }}

        .detail-message.user-msg {{
            border-left: 3px solid #3498db;
        }}

        .detail-message.bot-msg {{
            border-left: 3px solid #27ae60;
        }}

        .detail-code {{
            background: #2d3436;
            padding: 10px;
            border-radius: 6px;
            margin-top: 8px;
            color: #e74c3c;
            font-family: monospace;
            font-size: 0.85em;
        }}

        .modal-footer {{
            display: flex;
            justify-content: flex-end;
            gap: 10px;
            padding: 15px 20px;
            border-top: 1px solid #333;
        }}

        .issue-item {{
            cursor: pointer;
            transition: background 0.2s;
        }}

        /* Code Analysis Styles */
        .analysis-section {{
            margin-top: 20px;
            padding: 15px;
            background: rgba(52, 152, 219, 0.1);
            border-radius: 10px;
            border: 1px solid rgba(52, 152, 219, 0.3);
        }}

        .analysis-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }}

        .analysis-header h4 {{
            margin: 0;
            color: #3498db;
            font-size: 0.95em;
        }}

        .confidence-badge {{
            padding: 3px 10px;
            border-radius: 12px;
            font-size: 0.75em;
            font-weight: 500;
        }}

        .confidence-high {{ background: #27ae60; color: white; }}
        .confidence-medium {{ background: #f39c12; color: white; }}
        .confidence-low {{ background: #95a5a6; color: white; }}

        .analysis-summary {{
            color: #fff;
            font-size: 0.9em;
            line-height: 1.5;
            margin-bottom: 10px;
        }}

        .analysis-files {{
            color: #888;
            font-size: 0.85em;
        }}

        .analysis-files code {{
            background: rgba(255,255,255,0.1);
            padding: 2px 6px;
            border-radius: 3px;
            margin-right: 5px;
            color: #e74c3c;
        }}

        .prompt-section {{
            margin-top: 20px;
            padding: 15px;
            background: #1a1a2e;
            border-radius: 10px;
            border: 1px solid #333;
        }}

        .prompt-header {{
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 12px;
        }}

        .prompt-header h4 {{
            margin: 0;
            color: #9b59b6;
            font-size: 0.95em;
        }}

        .prompt-box {{
            background: #0d0d1a;
            border: 1px solid #333;
            border-radius: 8px;
            padding: 15px;
            max-height: 300px;
            overflow-y: auto;
            font-family: 'Monaco', 'Menlo', monospace;
            font-size: 0.85em;
            color: #e0e0e0;
            white-space: pre-wrap;
            line-height: 1.6;
        }}

        .copy-btn {{
            background: #9b59b6;
            color: white;
            border: none;
            padding: 6px 12px;
            border-radius: 5px;
            font-size: 0.8em;
            cursor: pointer;
            transition: background 0.2s;
        }}

        .copy-btn:hover {{
            background: #8e44ad;
        }}

        .copy-btn.copied {{
            background: #27ae60;
        }}

        .generate-btn {{
            display: block;
            width: 100%;
            padding: 12px;
            background: rgba(52, 152, 219, 0.2);
            border: 1px dashed #3498db;
            border-radius: 8px;
            color: #3498db;
            font-size: 0.9em;
            cursor: pointer;
            transition: all 0.2s;
            margin-top: 15px;
        }}

        .generate-btn:hover {{
            background: rgba(52, 152, 219, 0.3);
        }}

        .analysis-loading {{
            text-align: center;
            padding: 20px;
            color: #888;
        }}

        .issue-item:hover {{
            background: rgba(52, 152, 219, 0.1);
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

                <!-- Resolution Tracker -->
                <div class="card">
                    <div class="card-header">
                        <h2>🔧 Resolution Tracker</h2>
                        <button class="btn btn-secondary btn-sm" onclick="loadResolutionTracker()">
                            ↻ Refresh
                        </button>
                    </div>
                    <div id="resolutionTracker">
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

                <!-- Code Analyzer Actions -->
                <div class="card">
                    <div class="card-header">
                        <h2>🔍 Code Analyzer</h2>
                    </div>
                    <div style="display: flex; flex-direction: column; gap: 10px;">
                        <button class="btn btn-primary" onclick="runCodeAnalyzer()" id="runAnalyzerBtn">
                            🤖 Run Code Analyzer
                        </button>
                        <button class="btn btn-secondary" onclick="showAnalyzerStats()">
                            📈 View Analyzer Stats
                        </button>
                        <div id="analyzerStatsPanel" style="display: none; margin-top: 10px; padding: 12px; background: rgba(255,255,255,0.05); border-radius: 8px; font-size: 0.9em;">
                            <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                                <span style="color: #888;">Total Analyses:</span>
                                <span id="statTotalAnalyses" style="color: #fff;">-</span>
                            </div>
                            <div style="display: flex; justify-content: space-between; margin-bottom: 8px;">
                                <span style="color: #888;">Avg Confidence:</span>
                                <span id="statAvgConfidence" style="color: #fff;">-</span>
                            </div>
                            <div style="display: flex; justify-content: space-between;">
                                <span style="color: #888;">Applied Fixes:</span>
                                <span id="statAppliedFixes" style="color: #27ae60;">-</span>
                            </div>
                        </div>
                    </div>
                </div>
            </div>
        </div>
    </div>

    <!-- Toast -->
    <div class="toast" id="toast"></div>

    <!-- Issue Detail Modal -->
    <div class="modal-overlay" id="issueModal" style="display: none;">
        <div class="modal-content">
            <div class="modal-header">
                <h3 id="modalTitle">Issue Details</h3>
                <button class="modal-close" onclick="closeIssueModal()">&times;</button>
            </div>
            <div class="modal-body">
                <div class="detail-row">
                    <span class="detail-label">Issue Type</span>
                    <span class="detail-value" id="modalIssueType">-</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">Severity</span>
                    <span class="detail-value" id="modalSeverity">-</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">Status</span>
                    <span class="detail-value" id="modalStatus">-</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">Phone</span>
                    <span class="detail-value" id="modalPhone">-</span>
                </div>
                <div class="detail-row">
                    <span class="detail-label">Detected</span>
                    <span class="detail-value" id="modalDetected">-</span>
                </div>
                <div class="detail-section">
                    <div class="detail-label">User Message</div>
                    <div class="detail-message user-msg" id="modalUserMsg">-</div>
                </div>
                <div class="detail-section">
                    <div class="detail-label">Bot Response</div>
                    <div class="detail-message bot-msg" id="modalBotMsg">-</div>
                </div>
                <div class="detail-section" id="modalPatternSection" style="display: none;">
                    <div class="detail-label">Pattern Matched</div>
                    <div class="detail-code" id="modalPattern">-</div>
                </div>

                <!-- Code Analysis Section -->
                <div id="codeAnalysisContainer">
                    <div id="codeAnalysisLoading" class="analysis-loading" style="display: none;">
                        <div class="spinner"></div>
                        <p>Generating code analysis...</p>
                    </div>

                    <div id="codeAnalysisContent" style="display: none;">
                        <div class="analysis-section">
                            <div class="analysis-header">
                                <h4>Root Cause Analysis</h4>
                                <span class="confidence-badge" id="analysisConfidence">-</span>
                            </div>
                            <div class="analysis-summary" id="analysisSummary">-</div>
                            <div class="analysis-files">
                                Files to review: <span id="analysisFiles">-</span>
                            </div>
                        </div>

                        <div class="prompt-section">
                            <div class="prompt-header">
                                <h4>Claude Code Prompt</h4>
                                <button class="copy-btn" onclick="copyPrompt()">Copy</button>
                            </div>
                            <div class="prompt-box" id="claudePrompt">-</div>
                        </div>
                    </div>

                    <button class="generate-btn" id="generateAnalysisBtn" onclick="generateAnalysis()">
                        Generate Code Analysis
                    </button>
                </div>
            </div>
            <div class="modal-footer">
                <button class="btn btn-secondary" onclick="markFalsePositive()">Mark False Positive</button>
                <button class="btn btn-primary" onclick="closeIssueModal()">Close</button>
            </div>
        </div>
    </div>

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

        // Store issues for modal access
        let issuesData = {{}};
        let currentIssueId = null;

        // Load open issues
        async function loadIssues() {{
            try {{
                const data = await fetchAPI('/admin/monitoring/issues?limit=20');
                const list = document.getElementById('issueList');
                const issues = data.issues || [];

                // Filter to unresolved issues only
                const openIssues = issues.filter(i => !i.resolution);
                document.getElementById('issueCount').textContent = openIssues.length;

                // Store for modal access
                issuesData = {{}};
                openIssues.forEach(i => issuesData[i.id] = i);

                if (openIssues.length === 0) {{
                    list.innerHTML = `
                        <div class="empty-state">
                            <div class="icon">✅</div>
                            <p>No open issues! System is healthy.</p>
                        </div>
                    `;
                    return;
                }}

                list.innerHTML = openIssues.map(issue => `
                    <div class="issue-item" onclick="showIssueDetail(${{issue.id}})">
                        <div class="issue-severity ${{issue.severity}}"></div>
                        <div class="issue-content">
                            <div class="issue-type">
                                ${{issue.issue_type}}
                                ${{issue.validated ? '<span style="color:#27ae60;font-size:0.7em;margin-left:5px;">✓ validated</span>' : '<span style="color:#f39c12;font-size:0.7em;margin-left:5px;">pending</span>'}}
                            </div>
                            <div class="issue-detail">
                                ${{issue.message_in ? issue.message_in.substring(0, 60) + (issue.message_in.length > 60 ? '...' : '') : 'No message'}}
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

        // Show issue detail modal
        function showIssueDetail(issueId) {{
            const issue = issuesData[issueId];
            if (!issue) return;

            currentIssueId = issueId;

            document.getElementById('modalTitle').textContent = `Issue #${{issue.id}}`;
            document.getElementById('modalIssueType').textContent = issue.issue_type;
            document.getElementById('modalSeverity').innerHTML =
                `<span style="color: ${{issue.severity === 'critical' ? '#e74c3c' : issue.severity === 'high' ? '#e67e22' : '#f1c40f'}}">${{issue.severity.toUpperCase()}}</span>`;
            document.getElementById('modalStatus').innerHTML =
                issue.validated
                    ? '<span style="color:#27ae60">✓ Validated</span>'
                    : '<span style="color:#f39c12">Pending Validation</span>';
            document.getElementById('modalPhone').textContent = issue.phone || 'Unknown';
            document.getElementById('modalDetected').textContent =
                issue.detected_at ? new Date(issue.detected_at).toLocaleString() : '-';

            document.getElementById('modalUserMsg').textContent = issue.message_in || '(no message)';
            document.getElementById('modalBotMsg').textContent = issue.message_out || '(no response)';

            // Show pattern if available
            const patternSection = document.getElementById('modalPatternSection');
            if (issue.details && issue.details.pattern_matched) {{
                patternSection.style.display = 'block';
                document.getElementById('modalPattern').textContent = issue.details.pattern_matched;
            }} else {{
                patternSection.style.display = 'none';
            }}

            document.getElementById('issueModal').style.display = 'flex';

            // Load code analysis for this issue
            loadCodeAnalysis(issueId);
        }}

        // Load code analysis for an issue
        async function loadCodeAnalysis(issueId) {{
            const container = document.getElementById('codeAnalysisContainer');
            const loading = document.getElementById('codeAnalysisLoading');
            const content = document.getElementById('codeAnalysisContent');
            const generateBtn = document.getElementById('generateAnalysisBtn');

            // Reset state
            content.style.display = 'none';
            loading.style.display = 'none';
            generateBtn.style.display = 'block';

            try {{
                // Try to get existing analysis (don't generate if not exists)
                const response = await fetch(`/admin/analyzer/issue/${{issueId}}`);
                if (response.ok) {{
                    const analysis = await response.json();
                    displayCodeAnalysis(analysis);
                }}
                // If 404, no analysis exists yet - show generate button
            }} catch (e) {{
                console.log('No existing analysis found');
            }}
        }}

        // Display code analysis in modal
        function displayCodeAnalysis(analysis) {{
            const content = document.getElementById('codeAnalysisContent');
            const generateBtn = document.getElementById('generateAnalysisBtn');

            // Set confidence badge
            const confidenceBadge = document.getElementById('analysisConfidence');
            const confidence = analysis.confidence_score || 50;
            let confClass = 'confidence-low';
            let confText = 'Low';
            if (confidence >= 80) {{ confClass = 'confidence-high'; confText = 'High'; }}
            else if (confidence >= 60) {{ confClass = 'confidence-medium'; confText = 'Medium'; }}
            confidenceBadge.className = 'confidence-badge ' + confClass;
            confidenceBadge.textContent = confText + ' (' + confidence + '%)';

            // Set summary
            document.getElementById('analysisSummary').textContent =
                analysis.root_cause_summary || 'Analysis not available';

            // Set files
            const files = analysis.likely_files || [];
            document.getElementById('analysisFiles').innerHTML =
                files.map(f => `<code>${{f}}</code>`).join('') || 'No files identified';

            // Set Claude prompt
            document.getElementById('claudePrompt').textContent =
                analysis.claude_prompt || 'No prompt available';

            // Show content, hide generate button
            content.style.display = 'block';
            generateBtn.style.display = 'none';
        }}

        // Generate code analysis
        async function generateAnalysis() {{
            if (!currentIssueId) return;

            const loading = document.getElementById('codeAnalysisLoading');
            const generateBtn = document.getElementById('generateAnalysisBtn');

            loading.style.display = 'block';
            generateBtn.style.display = 'none';

            try {{
                const analysis = await fetchAPI(`/admin/analyzer/issue/${{currentIssueId}}?force=true`);
                displayCodeAnalysis(analysis);
                loading.style.display = 'none';
                showToast('Analysis generated', 'success');
            }} catch (e) {{
                loading.style.display = 'none';
                generateBtn.style.display = 'block';
                showToast('Failed to generate analysis', 'error');
            }}
        }}

        // Copy Claude prompt to clipboard
        async function copyPrompt() {{
            const prompt = document.getElementById('claudePrompt').textContent;
            try {{
                await navigator.clipboard.writeText(prompt);
                const btn = document.querySelector('.copy-btn');
                btn.textContent = 'Copied!';
                btn.classList.add('copied');
                setTimeout(() => {{
                    btn.textContent = 'Copy';
                    btn.classList.remove('copied');
                }}, 2000);
            }} catch (e) {{
                showToast('Failed to copy', 'error');
            }}
        }}

        // Close modal
        function closeIssueModal() {{
            document.getElementById('issueModal').style.display = 'none';
            currentIssueId = null;
            // Reset code analysis section
            document.getElementById('codeAnalysisContent').style.display = 'none';
            document.getElementById('codeAnalysisLoading').style.display = 'none';
            document.getElementById('generateAnalysisBtn').style.display = 'block';
        }}

        // Mark as false positive
        async function markFalsePositive() {{
            if (!currentIssueId) return;
            try {{
                await fetchAPI(`/admin/monitoring/issues/${{currentIssueId}}/false-positive`, {{ method: 'POST' }});
                showToast('Marked as false positive', 'success');
                closeIssueModal();
                loadIssues();
            }} catch (e) {{
                showToast('Failed to update', 'error');
            }}
        }}

        // Close modal on overlay click
        document.getElementById('issueModal')?.addEventListener('click', (e) => {{
            if (e.target.id === 'issueModal') closeIssueModal();
        }});

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

        // Load resolution tracker
        async function loadResolutionTracker() {{
            try {{
                const data = await fetchAPI('/admin/tracker/report');
                const container = document.getElementById('resolutionTracker');

                const current = data.current_health || {{}};
                const recommendations = data.recommendations || [];

                // Resolution types breakdown (array of objects with type, count, label)
                const resolutionBreakdown = data.resolution_breakdown || [];
                const resolutionList = resolutionBreakdown
                    .filter(r => r.count > 0)
                    .map(r => `<span class="badge">${{r.label || r.type}}: ${{r.count}}</span>`)
                    .join(' ') || '<span style="color: #666;">No resolutions yet</span>';

                // Week over week change (health_change is at top level of report)
                const healthChange = data.health_change || 0;
                const changeIcon = healthChange > 0 ? '📈' : healthChange < 0 ? '📉' : '➡️';
                const changeColor = healthChange > 0 ? '#27ae60' : healthChange < 0 ? '#e74c3c' : '#888';

                // Top recommendations (uses title and description fields)
                const topRecs = recommendations.slice(0, 3).map(rec => `
                    <div style="padding: 8px; background: rgba(255,255,255,0.05); border-radius: 6px; margin-bottom: 8px; font-size: 0.85em;">
                        <span style="color: ${{rec.priority === 'high' ? '#e74c3c' : rec.priority === 'medium' ? '#f39c12' : '#27ae60'}};">●</span>
                        <strong>${{rec.title || 'Recommendation'}}</strong>: ${{rec.description || rec.action || ''}}
                    </div>
                `).join('') || '<div style="color: #666; font-size: 0.9em;">No recommendations</div>';

                container.innerHTML = `
                    <div style="margin-bottom: 15px;">
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                            <span style="color: #888; font-size: 0.85em;">Week over Week</span>
                            <span style="color: ${{changeColor}}; font-weight: 500;">
                                ${{changeIcon}} ${{healthChange > 0 ? '+' : ''}}${{healthChange.toFixed(1)}}%
                            </span>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 10px;">
                            <span style="color: #888; font-size: 0.85em;">Issues This Week</span>
                            <span style="font-weight: 500;">${{current.total_issues || 0}}</span>
                        </div>
                        <div style="display: flex; justify-content: space-between; align-items: center;">
                            <span style="color: #888; font-size: 0.85em;">Resolved</span>
                            <span style="font-weight: 500; color: #27ae60;">${{current.resolved_issues || 0}}</span>
                        </div>
                    </div>

                    <div style="margin-bottom: 15px;">
                        <div style="color: #888; font-size: 0.85em; margin-bottom: 8px;">Resolution Types</div>
                        <div style="display: flex; flex-wrap: wrap; gap: 5px;">
                            ${{resolutionList}}
                        </div>
                    </div>

                    <div>
                        <div style="color: #888; font-size: 0.85em; margin-bottom: 8px;">Recommendations</div>
                        ${{topRecs}}
                    </div>
                `;

            }} catch (e) {{
                console.error('Failed to load resolution tracker:', e);
                document.getElementById('resolutionTracker').innerHTML =
                    '<div class="empty-state">Failed to load data</div>';
            }}
        }}

        // Load alert settings
        async function loadAlertSettings() {{
            try {{
                const data = await fetchAPI('/admin/alerts/settings');
                const container = document.getElementById('alertSettings');

                container.innerHTML = `
                    <form id="alertSettingsForm" onsubmit="saveAlertSettings(event)" style="grid-column: 1 / -1;">
                        <div class="form-row">
                            <div class="form-group">
                                <label class="toggle-switch">
                                    <input type="checkbox" id="alertsEnabled" ${{data.alerts_enabled ? 'checked' : ''}}>
                                    <span>Alerts Enabled</span>
                                </label>
                            </div>
                            <div class="form-group">
                                <label>Health Threshold</label>
                                <input type="number" id="healthThreshold" min="0" max="100"
                                    value="${{data.health_threshold || 70}}" placeholder="70">
                                <div class="form-hint">Alert when health score drops below this</div>
                            </div>
                        </div>

                        <div class="form-group">
                            <label>Microsoft Teams Webhook URL</label>
                            <input type="text" id="teamsWebhook"
                                placeholder="${{data.teams_configured ? '(webhook configured - enter new to replace)' : 'https://outlook.office.com/webhook/...'}}"
                                value="">
                            <div class="form-hint">${{data.teams_configured ? '✓ Connected' : 'Get from Teams: Channel → Connectors → Incoming Webhook'}}</div>
                        </div>

                        <div class="form-group">
                            <label>Email Recipients</label>
                            <textarea id="emailRecipients" rows="2"
                                placeholder="email1@example.com, email2@example.com">${{(data.email_recipients || []).join(', ')}}</textarea>
                            <div class="form-hint">Comma-separated email addresses${{data.email_configured ? '' : ' (SMTP not configured in env vars)'}}</div>
                        </div>

                        <div class="form-row">
                            <div class="form-group">
                                <label class="toggle-switch">
                                    <input type="checkbox" id="smsEnabled" ${{data.sms_enabled ? 'checked' : ''}}>
                                    <span>SMS Alerts (Critical Only)</span>
                                </label>
                            </div>
                            <div class="form-group">
                                <label>SMS Numbers</label>
                                <input type="text" id="smsNumbers"
                                    placeholder="+15551234567, +15559876543"
                                    value="${{data.sms_recipients?.map(n => '...' + n).join(', ') || ''}}">
                                <div class="form-hint">Full phone numbers with country code</div>
                            </div>
                        </div>

                        <div class="form-actions">
                            <button type="submit" class="btn btn-primary">Save Settings</button>
                            <button type="button" class="btn btn-secondary" onclick="testAlerts()">Test Alerts</button>
                        </div>
                    </form>
                `;

            }} catch (e) {{
                console.error('Failed to load alert settings:', e);
            }}
        }}

        // Save alert settings
        async function saveAlertSettings(event) {{
            event.preventDefault();

            const settings = {{
                alerts_enabled: document.getElementById('alertsEnabled').checked,
                health_threshold: parseInt(document.getElementById('healthThreshold').value) || 70,
                email_recipients: document.getElementById('emailRecipients').value,
                sms_enabled: document.getElementById('smsEnabled').checked,
            }};

            // Only include Teams webhook if a new one was entered
            const teamsWebhook = document.getElementById('teamsWebhook').value.trim();
            if (teamsWebhook) {{
                settings.teams_webhook_url = teamsWebhook;
            }}

            // Only include SMS numbers if changed (doesn't start with ...)
            const smsNumbers = document.getElementById('smsNumbers').value.trim();
            if (smsNumbers && !smsNumbers.startsWith('...')) {{
                settings.sms_numbers = smsNumbers;
            }}

            try {{
                await fetchAPI('/admin/alerts/settings', {{
                    method: 'POST',
                    body: JSON.stringify(settings)
                }});
                showToast('Settings saved successfully', 'success');
                loadAlertSettings();  // Reload to show updated values
            }} catch (e) {{
                showToast('Failed to save settings', 'error');
            }}
        }}

        // Actions
        async function runPipeline() {{
            showToast('Running full pipeline (3 agents)...', 'success');
            try {{
                const data = await fetchAPI('/admin/pipeline/run?hours=24');
                const r = data.results;
                showToast(`Pipeline complete: ${{r.agent1.issues_found}} found, ${{r.agent2.validated}} validated`, 'success');
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

        // Code Analyzer Actions
        async function runCodeAnalyzer() {{
            const btn = document.getElementById('runAnalyzerBtn');
            const originalText = btn.innerHTML;
            btn.innerHTML = '⏳ Analyzing...';
            btn.disabled = true;

            try {{
                const data = await fetchAPI('/admin/analyzer/run?use_ai=true');
                showToast(`Analyzer complete: ${{data.analyses_generated}} analyses generated for ${{data.issues_analyzed}} issues`, 'success');
                // Refresh analyzer stats
                showAnalyzerStats();
            }} catch (e) {{
                showToast('Code analyzer failed', 'error');
            }} finally {{
                btn.innerHTML = originalText;
                btn.disabled = false;
            }}
        }}

        async function showAnalyzerStats() {{
            const panel = document.getElementById('analyzerStatsPanel');
            const isVisible = panel.style.display !== 'none';

            if (isVisible) {{
                panel.style.display = 'none';
                return;
            }}

            try {{
                const data = await fetchAPI('/admin/analyzer/stats');
                document.getElementById('statTotalAnalyses').textContent = data.total_analyses || 0;
                document.getElementById('statAvgConfidence').textContent = (data.avg_confidence || 0) + '%';
                document.getElementById('statAppliedFixes').textContent = data.by_status?.applied || 0;
                panel.style.display = 'block';
            }} catch (e) {{
                showToast('Failed to load analyzer stats', 'error');
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
            loadResolutionTracker();
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
