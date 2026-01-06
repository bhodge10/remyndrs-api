"""
Customer Service Portal
A standalone portal for CS reps to search customers and handle support tickets
"""

import secrets
from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from database import get_db_connection, return_db_connection
from config import logger, CS_USERNAME, CS_PASSWORD, ADMIN_USERNAME, ADMIN_PASSWORD

router = APIRouter()
security = HTTPBasic()


def verify_cs_auth(credentials: HTTPBasicCredentials = Depends(security)):
    """Verify CS portal credentials (accepts CS or Admin credentials)"""
    # Check CS credentials
    cs_user_ok = secrets.compare_digest(credentials.username.encode("utf8"), CS_USERNAME.encode("utf8"))
    cs_pass_ok = CS_PASSWORD and secrets.compare_digest(credentials.password.encode("utf8"), CS_PASSWORD.encode("utf8"))

    # Also accept admin credentials
    admin_user_ok = secrets.compare_digest(credentials.username.encode("utf8"), ADMIN_USERNAME.encode("utf8"))
    admin_pass_ok = ADMIN_PASSWORD and secrets.compare_digest(credentials.password.encode("utf8"), ADMIN_PASSWORD.encode("utf8"))

    if (cs_user_ok and cs_pass_ok) or (admin_user_ok and admin_pass_ok):
        return credentials.username

    raise HTTPException(
        status_code=401,
        detail="Invalid credentials",
        headers={"WWW-Authenticate": "Basic"},
    )


@router.get("/cs", response_class=HTMLResponse)
async def cs_portal(request: Request, user: str = Depends(verify_cs_auth)):
    """Customer Service Portal main page"""

    # Get open ticket count for badge
    open_tickets = 0
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("SELECT COUNT(*) FROM support_tickets WHERE status = 'open'")
        result = c.fetchone()
        open_tickets = result[0] if result else 0
    except Exception as e:
        logger.error(f"Error getting ticket count: {e}")
    finally:
        if conn:
            return_db_connection(conn)

    html_content = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Remyndrs CS Portal</title>
    <meta name="viewport" content="width=device-width, initial-scale=1">
    <style>
        * {{
            box-sizing: border-box;
            margin: 0;
            padding: 0;
        }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
            background: #f5f6fa;
            min-height: 100vh;
        }}
        .header {{
            background: #2c3e50;
            color: white;
            padding: 15px 20px;
            display: flex;
            justify-content: space-between;
            align-items: center;
            position: sticky;
            top: 0;
            z-index: 100;
        }}
        .header h1 {{
            font-size: 1.3em;
            font-weight: 600;
        }}
        .header-nav {{
            display: flex;
            gap: 10px;
        }}
        .header-nav button {{
            background: #3498db;
            color: white;
            border: none;
            padding: 8px 16px;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.9em;
        }}
        .header-nav button:hover {{
            background: #2980b9;
        }}
        .header-nav button.active {{
            background: #27ae60;
        }}
        .badge {{
            background: #e74c3c;
            color: white;
            padding: 2px 8px;
            border-radius: 10px;
            font-size: 0.8em;
            margin-left: 5px;
        }}
        .container {{
            max-width: 1200px;
            margin: 0 auto;
            padding: 20px;
        }}
        .search-section {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        .search-box {{
            display: flex;
            gap: 10px;
        }}
        .search-box input {{
            flex: 1;
            padding: 12px 16px;
            border: 2px solid #ddd;
            border-radius: 6px;
            font-size: 1em;
        }}
        .search-box input:focus {{
            outline: none;
            border-color: #3498db;
        }}
        .search-box button {{
            background: #3498db;
            color: white;
            border: none;
            padding: 12px 24px;
            border-radius: 6px;
            cursor: pointer;
            font-size: 1em;
        }}
        .search-box button:hover {{
            background: #2980b9;
        }}
        .section {{
            background: white;
            padding: 20px;
            border-radius: 8px;
            box-shadow: 0 2px 4px rgba(0,0,0,0.1);
            margin-bottom: 20px;
        }}
        .section h2 {{
            margin-bottom: 15px;
            color: #2c3e50;
            font-size: 1.2em;
        }}
        table {{
            width: 100%;
            border-collapse: collapse;
        }}
        th, td {{
            padding: 12px;
            text-align: left;
            border-bottom: 1px solid #eee;
        }}
        th {{
            background: #f8f9fa;
            font-weight: 600;
            color: #2c3e50;
        }}
        tr:hover {{
            background: #f8f9fa;
        }}
        .btn {{
            padding: 6px 12px;
            border: none;
            border-radius: 4px;
            cursor: pointer;
            font-size: 0.85em;
        }}
        .btn-primary {{
            background: #3498db;
            color: white;
        }}
        .btn-primary:hover {{
            background: #2980b9;
        }}
        .btn-success {{
            background: #27ae60;
            color: white;
        }}
        .btn-danger {{
            background: #e74c3c;
            color: white;
        }}
        .btn-warning {{
            background: #f39c12;
            color: white;
        }}
        .tier-badge {{
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.8em;
            font-weight: 600;
        }}
        .tier-free {{
            background: #ecf0f1;
            color: #7f8c8d;
        }}
        .tier-premium {{
            background: #f39c12;
            color: white;
        }}
        .tier-family {{
            background: #9b59b6;
            color: white;
        }}
        .status-open {{
            background: #27ae60;
            color: white;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.8em;
        }}
        .status-closed {{
            background: #95a5a6;
            color: white;
            padding: 4px 8px;
            border-radius: 4px;
            font-size: 0.8em;
        }}

        /* Customer Profile Styles */
        .profile-header {{
            display: flex;
            justify-content: space-between;
            align-items: flex-start;
            margin-bottom: 20px;
            padding-bottom: 15px;
            border-bottom: 1px solid #eee;
        }}
        .profile-info h3 {{
            margin-bottom: 5px;
            color: #2c3e50;
        }}
        .profile-info p {{
            color: #7f8c8d;
            margin: 3px 0;
        }}
        .profile-actions {{
            display: flex;
            gap: 10px;
        }}
        .tabs {{
            display: flex;
            gap: 5px;
            margin-bottom: 15px;
            border-bottom: 2px solid #eee;
            padding-bottom: 10px;
        }}
        .tab {{
            padding: 8px 16px;
            background: #f8f9fa;
            border: none;
            border-radius: 4px 4px 0 0;
            cursor: pointer;
            font-size: 0.9em;
        }}
        .tab.active {{
            background: #3498db;
            color: white;
        }}
        .tab-content {{
            display: none;
        }}
        .tab-content.active {{
            display: block;
        }}

        /* Modal Styles */
        .modal {{
            display: none;
            position: fixed;
            top: 0;
            left: 0;
            right: 0;
            bottom: 0;
            background: rgba(0,0,0,0.5);
            z-index: 1000;
        }}
        .modal-content {{
            background: white;
            max-width: 600px;
            margin: 50px auto;
            border-radius: 8px;
            max-height: 80vh;
            overflow: hidden;
            display: flex;
            flex-direction: column;
        }}
        .modal-header {{
            padding: 15px 20px;
            border-bottom: 1px solid #ddd;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        .modal-header h3 {{
            margin: 0;
        }}
        .modal-close {{
            background: none;
            border: none;
            font-size: 24px;
            cursor: pointer;
            color: #7f8c8d;
        }}
        .modal-body {{
            flex: 1;
            overflow-y: auto;
            padding: 20px;
            background: #f5f6fa;
        }}
        .modal-footer {{
            padding: 15px 20px;
            border-top: 1px solid #ddd;
            background: white;
        }}

        /* Chat Messages */
        .message {{
            display: flex;
            margin-bottom: 10px;
        }}
        .message.inbound {{
            justify-content: flex-start;
        }}
        .message.outbound {{
            justify-content: flex-end;
        }}
        .message-bubble {{
            max-width: 80%;
            padding: 10px 15px;
            border-radius: 12px;
            box-shadow: 0 1px 2px rgba(0,0,0,0.1);
        }}
        .message.inbound .message-bubble {{
            background: white;
            color: #333;
        }}
        .message.outbound .message-bubble {{
            background: #3498db;
            color: white;
        }}
        .message-meta {{
            font-size: 0.75em;
            opacity: 0.8;
            margin-bottom: 4px;
        }}
        .reply-box {{
            display: flex;
            gap: 10px;
        }}
        .reply-box input {{
            flex: 1;
            padding: 10px;
            border: 1px solid #ddd;
            border-radius: 4px;
        }}

        /* Notes */
        .note {{
            background: #fffbcc;
            padding: 10px;
            border-radius: 4px;
            margin-bottom: 10px;
            border-left: 4px solid #f39c12;
        }}
        .note-meta {{
            font-size: 0.8em;
            color: #7f8c8d;
            margin-bottom: 5px;
        }}

        /* View Toggle */
        .view-section {{
            display: none;
        }}
        .view-section.active {{
            display: block;
        }}

        /* Empty State */
        .empty-state {{
            text-align: center;
            padding: 40px;
            color: #7f8c8d;
        }}

        /* Loading */
        .loading {{
            text-align: center;
            padding: 20px;
            color: #7f8c8d;
        }}
    </style>
</head>
<body>
    <div class="header">
        <h1>Remyndrs Customer Service</h1>
        <div class="header-nav">
            <button onclick="showView('customers')" id="btn-customers" class="active">Customers</button>
            <button onclick="showView('tickets')" id="btn-tickets">
                Support Tickets
                <span class="badge" id="ticketBadge">{open_tickets}</span>
            </button>
        </div>
    </div>

    <div class="container">
        <!-- Customer Search View -->
        <div id="view-customers" class="view-section active">
            <div class="search-section">
                <div class="search-box">
                    <input type="text" id="searchInput" placeholder="Search by phone number or name..."
                           onkeyup="if(event.key === 'Enter') searchCustomers()">
                    <button onclick="searchCustomers()">Search</button>
                </div>
            </div>

            <div id="searchResults" class="section" style="display: none;">
                <h2>Search Results <span id="resultCount" style="color: #7f8c8d; font-weight: normal;"></span></h2>
                <table>
                    <thead>
                        <tr>
                            <th>Phone</th>
                            <th>Name</th>
                            <th>Tier</th>
                            <th>Status</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="searchResultsBody">
                    </tbody>
                </table>
            </div>

            <!-- Customer Profile -->
            <div id="customerProfile" class="section" style="display: none;">
                <div class="profile-header">
                    <div class="profile-info">
                        <h3 id="profileName">Customer Name</h3>
                        <p id="profilePhone">Phone Number</p>
                        <p id="profileTier">Tier</p>
                        <p id="profileSince">Member since</p>
                    </div>
                    <div class="profile-actions">
                        <button class="btn btn-primary" onclick="openTierModal()">Change Tier</button>
                        <button class="btn btn-success" onclick="openNoteModal()">Add Note</button>
                        <button class="btn" onclick="closeProfile()" style="background: #95a5a6; color: white;">Close</button>
                    </div>
                </div>

                <div class="tabs">
                    <button class="tab active" onclick="showProfileTab('reminders')">Reminders</button>
                    <button class="tab" onclick="showProfileTab('lists')">Lists</button>
                    <button class="tab" onclick="showProfileTab('memories')">Memories</button>
                    <button class="tab" onclick="showProfileTab('tickets')">Support Tickets</button>
                    <button class="tab" onclick="showProfileTab('notes')">Notes</button>
                </div>

                <div id="tab-reminders" class="tab-content active">
                    <div class="loading">Loading reminders...</div>
                </div>
                <div id="tab-lists" class="tab-content">
                    <div class="loading">Loading lists...</div>
                </div>
                <div id="tab-memories" class="tab-content">
                    <div class="loading">Loading memories...</div>
                </div>
                <div id="tab-tickets" class="tab-content">
                    <div class="loading">Loading support tickets...</div>
                </div>
                <div id="tab-notes" class="tab-content">
                    <div class="loading">Loading notes...</div>
                </div>
            </div>
        </div>

        <!-- Support Tickets View -->
        <div id="view-tickets" class="view-section">
            <div class="section">
                <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 15px;">
                    <h2>Support Tickets</h2>
                    <label style="cursor: pointer;">
                        <input type="checkbox" id="showClosedTickets" onchange="loadAllTickets()"> Show closed tickets
                    </label>
                </div>
                <table>
                    <thead>
                        <tr>
                            <th>ID</th>
                            <th>Customer</th>
                            <th>Status</th>
                            <th>Last Message</th>
                            <th>Updated</th>
                            <th>Actions</th>
                        </tr>
                    </thead>
                    <tbody id="ticketsBody">
                        <tr><td colspan="6" class="loading">Loading tickets...</td></tr>
                    </tbody>
                </table>
            </div>
        </div>
    </div>

    <!-- Ticket Modal -->
    <div id="ticketModal" class="modal">
        <div class="modal-content">
            <div class="modal-header">
                <div style="display: flex; align-items: center; gap: 15px;">
                    <h3 id="ticketModalTitle" style="margin: 0;">Ticket #</h3>
                    <button class="btn btn-primary" onclick="viewTicketCustomer()" style="font-size: 0.85em; padding: 5px 10px; background: #9b59b6;">Customer Profile</button>
                </div>
                <button class="modal-close" onclick="closeTicketModal()">&times;</button>
            </div>
            <div class="modal-body" id="ticketMessages">
            </div>
            <div class="modal-footer">
                <div class="reply-box">
                    <input type="text" id="ticketReplyInput" placeholder="Type your reply..."
                           onkeyup="if(event.key === 'Enter') sendTicketReply()">
                    <button class="btn btn-success" onclick="sendTicketReply()">Send</button>
                </div>
                <div style="margin-top: 10px; display: flex; gap: 10px;">
                    <button class="btn btn-danger" id="closeTicketBtn" onclick="closeCurrentTicket()">Close Ticket</button>
                    <button class="btn btn-warning" id="reopenTicketBtn" onclick="reopenCurrentTicket()" style="display: none;">Reopen</button>
                </div>
            </div>
        </div>
    </div>

    <!-- Tier Change Modal -->
    <div id="tierModal" class="modal">
        <div class="modal-content" style="max-width: 400px;">
            <div class="modal-header">
                <h3>Change Subscription Tier</h3>
                <button class="modal-close" onclick="closeTierModal()">&times;</button>
            </div>
            <div class="modal-body" style="background: white;">
                <p style="margin-bottom: 15px;">Select new tier for this customer:</p>
                <select id="tierSelect" style="width: 100%; padding: 10px; border: 1px solid #ddd; border-radius: 4px;">
                    <option value="free">Free</option>
                    <option value="premium">Premium</option>
                    <option value="family">Family</option>
                </select>
            </div>
            <div class="modal-footer">
                <button class="btn btn-primary" onclick="saveTierChange()">Save Changes</button>
                <button class="btn" onclick="closeTierModal()" style="background: #95a5a6; color: white;">Cancel</button>
            </div>
        </div>
    </div>

    <!-- Add Note Modal -->
    <div id="noteModal" class="modal">
        <div class="modal-content" style="max-width: 500px;">
            <div class="modal-header">
                <h3>Add Customer Note</h3>
                <button class="modal-close" onclick="closeNoteModal()">&times;</button>
            </div>
            <div class="modal-body" style="background: white;">
                <textarea id="noteText" placeholder="Enter note..."
                          style="width: 100%; height: 150px; padding: 10px; border: 1px solid #ddd; border-radius: 4px; resize: vertical;"></textarea>
            </div>
            <div class="modal-footer">
                <button class="btn btn-success" onclick="saveNote()">Save Note</button>
                <button class="btn" onclick="closeNoteModal()" style="background: #95a5a6; color: white;">Cancel</button>
            </div>
        </div>
    </div>

    <script>
        let currentCustomer = null;
        let currentTicketId = null;
        let currentTicketStatus = null;
        let ticketRefreshInterval = null;

        // View switching
        function showView(view) {{
            document.querySelectorAll('.view-section').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.header-nav button').forEach(el => el.classList.remove('active'));
            document.getElementById('view-' + view).classList.add('active');
            document.getElementById('btn-' + view).classList.add('active');

            if (view === 'tickets') {{
                loadAllTickets();
            }}
        }}

        // Customer Search - uses admin endpoint which returns {customers: [...]}
        async function searchCustomers() {{
            const query = document.getElementById('searchInput').value.trim();
            if (!query) return;

            const resultsDiv = document.getElementById('searchResults');
            const tbody = document.getElementById('searchResultsBody');
            const countSpan = document.getElementById('resultCount');

            resultsDiv.style.display = 'block';
            tbody.innerHTML = '<tr><td colspan="5" class="loading">Searching...</td></tr>';

            try {{
                // Use admin search endpoint - CS credentials work for both
                const response = await fetch(`/admin/cs/search?q=${{encodeURIComponent(query)}}`);

                if (!response.ok) {{
                    throw new Error(`HTTP ${{response.status}}: ${{response.statusText}}`);
                }}

                const data = await response.json();
                console.log('Search response:', data);

                // Admin endpoint returns {customers: [...]}
                const results = data.customers || [];
                countSpan.textContent = `(${{results.length}} found)`;

                if (results.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="5" class="empty-state">No customers found</td></tr>';
                    return;
                }}

                tbody.innerHTML = results.map(c => `
                    <tr>
                        <td>${{c.phone}}</td>
                        <td>${{c.first_name || 'Unknown'}}</td>
                        <td><span class="tier-badge tier-${{c.tier || 'free'}}">${{(c.tier || 'free').toUpperCase()}}</span></td>
                        <td>${{c.subscription_status || 'N/A'}}</td>
                        <td><button class="btn btn-primary" onclick="viewCustomer('${{c.phone}}')">View</button></td>
                    </tr>
                `).join('');
            }} catch (e) {{
                console.error('Search error:', e);
                countSpan.textContent = '';
                tbody.innerHTML = `<tr><td colspan="5" class="empty-state" style="color: #e74c3c;">Error: ${{e.message}}</td></tr>`;
            }}
        }}

        // Customer Profile
        async function viewCustomer(phone) {{
            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(phone)}}`);
                const customer = await response.json();

                currentCustomer = customer;

                document.getElementById('profileName').textContent = customer.name || 'Unknown';
                document.getElementById('profilePhone').textContent = customer.phone;
                document.getElementById('profileTier').innerHTML = `Tier: <span class="tier-badge tier-${{customer.tier || 'free'}}">${{(customer.tier || 'free').toUpperCase()}}</span>`;
                document.getElementById('profileSince').textContent = customer.created_at ? `Member since: ${{new Date(customer.created_at).toLocaleDateString()}}` : '';

                document.getElementById('customerProfile').style.display = 'block';
                showProfileTab('reminders');
            }} catch (e) {{
                console.error('Error loading customer:', e);
            }}
        }}

        function closeProfile() {{
            document.getElementById('customerProfile').style.display = 'none';
            currentCustomer = null;
        }}

        async function showProfileTab(tab) {{
            document.querySelectorAll('.tabs .tab').forEach(el => el.classList.remove('active'));
            document.querySelectorAll('.tab-content').forEach(el => el.classList.remove('active'));

            event.target.classList.add('active');
            document.getElementById('tab-' + tab).classList.add('active');

            if (!currentCustomer) return;

            const container = document.getElementById('tab-' + tab);
            container.innerHTML = '<div class="loading">Loading...</div>';

            try {{
                if (tab === 'tickets') {{
                    await loadCustomerTickets();
                }} else if (tab === 'notes') {{
                    await loadCustomerNotes();
                }} else {{
                    const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(currentCustomer.phone)}}/${{tab}}`);
                    const data = await response.json();

                    if (tab === 'reminders') {{
                        renderReminders(data);
                    }} else if (tab === 'lists') {{
                        renderLists(data);
                    }} else if (tab === 'memories') {{
                        renderMemories(data);
                    }}
                }}
            }} catch (e) {{
                container.innerHTML = '<div class="empty-state">Error loading data</div>';
            }}
        }}

        function renderReminders(reminders) {{
            const container = document.getElementById('tab-reminders');
            if (!reminders || reminders.length === 0) {{
                container.innerHTML = '<div class="empty-state">No reminders found</div>';
                return;
            }}
            container.innerHTML = `
                <table>
                    <tr><th>Reminder</th><th>Time</th><th>Status</th></tr>
                    ${{reminders.map(r => `
                        <tr>
                            <td>${{r.text}}</td>
                            <td>${{r.reminder_time ? new Date(r.reminder_time).toLocaleString() : 'N/A'}}</td>
                            <td>${{r.status}}</td>
                        </tr>
                    `).join('')}}
                </table>
            `;
        }}

        function renderLists(lists) {{
            const container = document.getElementById('tab-lists');
            if (!lists || lists.length === 0) {{
                container.innerHTML = '<div class="empty-state">No lists found</div>';
                return;
            }}
            container.innerHTML = lists.map(list => `
                <div style="margin-bottom: 15px; padding: 10px; background: #f8f9fa; border-radius: 4px;">
                    <strong>${{list.name}}</strong> (${{list.items ? list.items.length : 0}} items)
                    ${{list.items && list.items.length > 0 ? `
                        <ul style="margin-top: 8px; margin-left: 20px;">
                            ${{list.items.map(item => `<li style="${{item.completed ? 'text-decoration: line-through; color: #95a5a6;' : ''}}">${{item.text}}</li>`).join('')}}
                        </ul>
                    ` : ''}}
                </div>
            `).join('');
        }}

        function renderMemories(memories) {{
            const container = document.getElementById('tab-memories');
            if (!memories || memories.length === 0) {{
                container.innerHTML = '<div class="empty-state">No memories found</div>';
                return;
            }}
            container.innerHTML = `
                <table>
                    <tr><th>Memory</th><th>Stored</th></tr>
                    ${{memories.map(m => `
                        <tr>
                            <td>${{m.content}}</td>
                            <td>${{m.created_at ? new Date(m.created_at).toLocaleDateString() : 'N/A'}}</td>
                        </tr>
                    `).join('')}}
                </table>
            `;
        }}

        async function loadCustomerTickets() {{
            const container = document.getElementById('tab-tickets');
            try {{
                const response = await fetch(`/cs/customer/${{encodeURIComponent(currentCustomer.phone)}}/tickets`);
                const tickets = await response.json();

                if (!tickets || tickets.length === 0) {{
                    container.innerHTML = '<div class="empty-state">No support tickets</div>';
                    return;
                }}

                container.innerHTML = `
                    <table>
                        <tr><th>ID</th><th>Status</th><th>Messages</th><th>Last Updated</th><th>Actions</th></tr>
                        ${{tickets.map(t => `
                            <tr>
                                <td>#${{t.id}}</td>
                                <td><span class="status-${{t.status}}">${{t.status.toUpperCase()}}</span></td>
                                <td>${{t.message_count}}</td>
                                <td>${{t.updated_at ? new Date(t.updated_at).toLocaleString() : 'N/A'}}</td>
                                <td><button class="btn btn-primary" onclick="openTicket(${{t.id}}, '${{t.status}}', '${{currentCustomer.name || 'Unknown'}}', '${{currentCustomer.phone}}')">View</button></td>
                            </tr>
                        `).join('')}}
                    </table>
                `;
            }} catch (e) {{
                container.innerHTML = '<div class="empty-state">Error loading tickets</div>';
            }}
        }}

        async function loadCustomerNotes() {{
            const container = document.getElementById('tab-notes');
            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(currentCustomer.phone)}}`);
                const customer = await response.json();

                if (!customer.notes || customer.notes.length === 0) {{
                    container.innerHTML = '<div class="empty-state">No notes yet</div>';
                    return;
                }}

                container.innerHTML = customer.notes.map(n => `
                    <div class="note">
                        <div class="note-meta">${{n.created_by}} - ${{new Date(n.created_at).toLocaleString()}}</div>
                        <div>${{n.note}}</div>
                    </div>
                `).join('');
            }} catch (e) {{
                container.innerHTML = '<div class="empty-state">Error loading notes</div>';
            }}
        }}

        // Support Tickets
        async function loadAllTickets() {{
            const includeClosed = document.getElementById('showClosedTickets').checked;
            const tbody = document.getElementById('ticketsBody');

            try {{
                const response = await fetch(`/cs/support/tickets?include_closed=${{includeClosed}}`);
                const data = await response.json();

                // Update badge
                const openCount = data.tickets.filter(t => t.status === 'open').length;
                document.getElementById('ticketBadge').textContent = openCount;

                if (!data.tickets || data.tickets.length === 0) {{
                    tbody.innerHTML = '<tr><td colspan="6" class="empty-state">No tickets found</td></tr>';
                    return;
                }}

                tbody.innerHTML = data.tickets.map(t => `
                    <tr>
                        <td>#${{t.id}}</td>
                        <td>${{t.user_name || 'Unknown'}} (...${{t.phone_number.slice(-4)}})</td>
                        <td><span class="status-${{t.status}}">${{t.status.toUpperCase()}}</span></td>
                        <td style="max-width: 300px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap;">${{t.last_message || 'No messages'}}</td>
                        <td>${{t.updated_at ? new Date(t.updated_at).toLocaleString() : 'N/A'}}</td>
                        <td><button class="btn btn-primary" onclick="openTicket(${{t.id}}, '${{t.status}}', '${{t.user_name || 'Unknown'}}', '${{t.phone_number}}')">View</button></td>
                    </tr>
                `).join('');
            }} catch (e) {{
                tbody.innerHTML = '<tr><td colspan="6" class="empty-state">Error loading tickets</td></tr>';
            }}
        }}

        let currentTicketUserName = null;
        let currentTicketPhone = null;

        async function openTicket(ticketId, status, userName, phoneNumber) {{
            currentTicketId = ticketId;
            currentTicketStatus = status;
            currentTicketUserName = userName || 'Unknown';
            currentTicketPhone = phoneNumber;

            document.getElementById('ticketModalTitle').textContent = `Ticket #${{ticketId}}`;
            document.getElementById('ticketModal').style.display = 'block';
            document.getElementById('closeTicketBtn').style.display = status === 'open' ? 'block' : 'none';
            document.getElementById('reopenTicketBtn').style.display = status === 'closed' ? 'block' : 'none';

            await loadTicketMessages();

            // Start auto-refresh
            if (ticketRefreshInterval) clearInterval(ticketRefreshInterval);
            ticketRefreshInterval = setInterval(loadTicketMessages, 5000);
        }}

        function viewTicketCustomer() {{
            if (currentTicketPhone) {{
                closeTicketModal();
                showView('customers');
                document.getElementById('searchInput').value = currentTicketPhone;
                searchCustomers();
            }}
        }}

        async function loadTicketMessages() {{
            if (!currentTicketId) return;

            try {{
                const response = await fetch(`/cs/support/tickets/${{currentTicketId}}/messages`);
                const messages = await response.json();

                const container = document.getElementById('ticketMessages');

                if (!messages || messages.length === 0) {{
                    container.innerHTML = '<div class="empty-state">No messages yet</div>';
                    return;
                }}

                container.innerHTML = messages.map(m => `
                    <div class="message ${{m.direction}}">
                        <div class="message-bubble">
                            <div class="message-meta">${{m.direction === 'inbound' ? currentTicketUserName : 'Support'}} - ${{new Date(m.created_at).toLocaleString()}}</div>
                            <div>${{m.message}}</div>
                        </div>
                    </div>
                `).join('');

                container.scrollTop = container.scrollHeight;
            }} catch (e) {{
                console.error('Error loading messages:', e);
            }}
        }}

        function closeTicketModal() {{
            if (ticketRefreshInterval) {{
                clearInterval(ticketRefreshInterval);
                ticketRefreshInterval = null;
            }}
            document.getElementById('ticketModal').style.display = 'none';
            document.getElementById('ticketReplyInput').value = '';
            currentTicketId = null;
            currentTicketStatus = null;
        }}

        async function sendTicketReply() {{
            const input = document.getElementById('ticketReplyInput');
            const message = input.value.trim();
            if (!message || !currentTicketId) return;

            try {{
                const response = await fetch(`/cs/support/tickets/${{currentTicketId}}/reply`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ message }})
                }});

                if (response.ok) {{
                    input.value = '';
                    await loadTicketMessages();
                    loadAllTickets();
                }} else {{
                    alert('Error sending reply');
                }}
            }} catch (e) {{
                alert('Error sending reply');
            }}
        }}

        async function closeCurrentTicket() {{
            if (!currentTicketId) return;
            if (!confirm('Close this ticket?')) return;

            try {{
                const response = await fetch(`/cs/support/tickets/${{currentTicketId}}/close`, {{
                    method: 'POST'
                }});

                if (response.ok) {{
                    closeTicketModal();
                    loadAllTickets();
                    if (currentCustomer) loadCustomerTickets();
                }}
            }} catch (e) {{
                alert('Error closing ticket');
            }}
        }}

        async function reopenCurrentTicket() {{
            if (!currentTicketId) return;

            try {{
                const response = await fetch(`/cs/support/tickets/${{currentTicketId}}/reopen`, {{
                    method: 'POST'
                }});

                if (response.ok) {{
                    closeTicketModal();
                    loadAllTickets();
                    if (currentCustomer) loadCustomerTickets();
                }}
            }} catch (e) {{
                alert('Error reopening ticket');
            }}
        }}

        // Tier Management
        function openTierModal() {{
            if (!currentCustomer) return;
            document.getElementById('tierSelect').value = currentCustomer.tier || 'free';
            document.getElementById('tierModal').style.display = 'block';
        }}

        function closeTierModal() {{
            document.getElementById('tierModal').style.display = 'none';
        }}

        async function saveTierChange() {{
            if (!currentCustomer) return;
            const newTier = document.getElementById('tierSelect').value;

            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(currentCustomer.phone)}}/tier`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ tier: newTier }})
                }});

                if (response.ok) {{
                    closeTierModal();
                    viewCustomer(currentCustomer.phone);
                }} else {{
                    alert('Error updating tier');
                }}
            }} catch (e) {{
                alert('Error updating tier');
            }}
        }}

        // Notes
        function openNoteModal() {{
            document.getElementById('noteText').value = '';
            document.getElementById('noteModal').style.display = 'block';
        }}

        function closeNoteModal() {{
            document.getElementById('noteModal').style.display = 'none';
        }}

        async function saveNote() {{
            if (!currentCustomer) return;
            const note = document.getElementById('noteText').value.trim();
            if (!note) return;

            try {{
                const response = await fetch(`/admin/cs/customer/${{encodeURIComponent(currentCustomer.phone)}}/notes`, {{
                    method: 'POST',
                    headers: {{ 'Content-Type': 'application/json' }},
                    body: JSON.stringify({{ note }})
                }});

                if (response.ok) {{
                    closeNoteModal();
                    showProfileTab('notes');
                }} else {{
                    alert('Error saving note');
                }}
            }} catch (e) {{
                alert('Error saving note');
            }}
        }}

        // Initialize
        loadAllTickets();
    </script>
</body>
</html>
"""
    return HTMLResponse(content=html_content)


# API endpoint to get customer tickets
@router.get("/cs/customer/{phone_number}/tickets")
async def get_customer_tickets(phone_number: str, user: str = Depends(verify_cs_auth)):
    """Get support tickets for a specific customer"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            SELECT id, status, created_at, updated_at,
                   (SELECT COUNT(*) FROM support_messages WHERE ticket_id = support_tickets.id) as message_count
            FROM support_tickets
            WHERE phone_number = %s
            ORDER BY updated_at DESC
        """, (phone_number,))

        tickets = c.fetchall()
        return [
            {
                'id': t[0],
                'status': t[1],
                'created_at': t[2].isoformat() if t[2] else None,
                'updated_at': t[3].isoformat() if t[3] else None,
                'message_count': t[4]
            }
            for t in tickets
        ]
    except Exception as e:
        logger.error(f"Error getting customer tickets: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


# =====================================================
# SUPPORT TICKET ENDPOINTS (CS Auth)
# =====================================================

@router.get("/cs/support/tickets")
async def cs_get_all_tickets(include_closed: bool = False, user: str = Depends(verify_cs_auth)):
    """Get all support tickets"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if include_closed:
            c.execute("""
                SELECT t.id, t.phone_number, t.status, t.created_at, t.updated_at,
                       u.first_name,
                       (SELECT COUNT(*) FROM support_messages WHERE ticket_id = t.id) as message_count,
                       (SELECT message FROM support_messages WHERE ticket_id = t.id ORDER BY created_at DESC LIMIT 1) as last_message
                FROM support_tickets t
                LEFT JOIN users u ON t.phone_number = u.phone_number
                ORDER BY t.updated_at DESC
            """)
        else:
            c.execute("""
                SELECT t.id, t.phone_number, t.status, t.created_at, t.updated_at,
                       u.first_name,
                       (SELECT COUNT(*) FROM support_messages WHERE ticket_id = t.id) as message_count,
                       (SELECT message FROM support_messages WHERE ticket_id = t.id ORDER BY created_at DESC LIMIT 1) as last_message
                FROM support_tickets t
                LEFT JOIN users u ON t.phone_number = u.phone_number
                WHERE t.status = 'open'
                ORDER BY t.updated_at DESC
            """)

        tickets = c.fetchall()
        return {
            'tickets': [
                {
                    'id': t[0],
                    'phone_number': t[1],
                    'status': t[2],
                    'created_at': t[3].isoformat() if t[3] else None,
                    'updated_at': t[4].isoformat() if t[4] else None,
                    'user_name': t[5],
                    'message_count': t[6],
                    'last_message': t[7][:100] + '...' if t[7] and len(t[7]) > 100 else t[7]
                }
                for t in tickets
            ]
        }
    except Exception as e:
        logger.error(f"Error getting tickets: {e}")
        return {'tickets': []}
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/cs/support/tickets/{ticket_id}/messages")
async def cs_get_ticket_messages(ticket_id: int, user: str = Depends(verify_cs_auth)):
    """Get messages for a specific ticket"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()
        c.execute("""
            SELECT id, message, direction, created_at
            FROM support_messages
            WHERE ticket_id = %s
            ORDER BY created_at ASC
        """, (ticket_id,))

        messages = c.fetchall()
        return [
            {
                'id': m[0],
                'message': m[1],
                'direction': m[2],
                'created_at': m[3].isoformat() if m[3] else None
            }
            for m in messages
        ]
    except Exception as e:
        logger.error(f"Error getting ticket messages: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/cs/support/tickets/{ticket_id}/reply")
async def cs_reply_to_ticket(ticket_id: int, request: Request, user: str = Depends(verify_cs_auth)):
    """Reply to a support ticket"""
    from services.support_service import reply_to_ticket

    try:
        body = await request.json()
        message = body.get('message', '').strip()

        if not message:
            raise HTTPException(status_code=400, detail="Message is required")

        result = reply_to_ticket(ticket_id, message)

        if result['success']:
            logger.info(f"Support reply sent to ticket #{ticket_id} by {user}")
            return {'success': True, 'message': 'Reply sent'}
        else:
            raise HTTPException(status_code=400, detail=result.get('error', 'Failed to send reply'))

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error replying to ticket: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/cs/support/tickets/{ticket_id}/close")
async def cs_close_ticket(ticket_id: int, user: str = Depends(verify_cs_auth)):
    """Close a support ticket"""
    from services.support_service import close_ticket

    try:
        success = close_ticket(ticket_id, notify_user=True)
        if success:
            logger.info(f"Ticket #{ticket_id} closed by {user}")
            return {'success': True}
        else:
            raise HTTPException(status_code=400, detail="Failed to close ticket")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error closing ticket: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


@router.post("/cs/support/tickets/{ticket_id}/reopen")
async def cs_reopen_ticket(ticket_id: int, user: str = Depends(verify_cs_auth)):
    """Reopen a support ticket"""
    from services.support_service import reopen_ticket

    try:
        success = reopen_ticket(ticket_id)
        if success:
            logger.info(f"Ticket #{ticket_id} reopened by {user}")
            return {'success': True}
        else:
            raise HTTPException(status_code=400, detail="Failed to reopen ticket")
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error reopening ticket: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")


# =====================================================
# CUSTOMER SERVICE ENDPOINTS (CS Auth)
# =====================================================

@router.get("/cs/search")
async def cs_search_customers(q: str = "", user: str = Depends(verify_cs_auth)):
    """Search customers by phone number or name"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        if not q or len(q) < 2:
            return {'results': [], 'message': 'Enter at least 2 characters to search'}

        # Clean up search term - remove common phone formatting
        clean_query = q.strip().replace('-', '').replace('(', '').replace(')', '').replace(' ', '')

        # Build search pattern
        search_term = f"%{clean_query}%"

        # Also try with + prefix for phone numbers (digits only)
        if clean_query.isdigit():
            # Search for phone with or without + prefix
            c.execute("""
                SELECT phone_number, first_name, premium_status, active, created_at
                FROM users
                WHERE phone_number LIKE %s
                   OR phone_number LIKE %s
                ORDER BY created_at DESC
                LIMIT 50
            """, (search_term, f"%+{clean_query}%"))
        else:
            # Search by name or phone
            c.execute("""
                SELECT phone_number, first_name, premium_status, active, created_at
                FROM users
                WHERE phone_number LIKE %s
                   OR LOWER(first_name) LIKE LOWER(%s)
                ORDER BY created_at DESC
                LIMIT 50
            """, (search_term, search_term))

        results = c.fetchall()
        logger.info(f"CS search for '{q}' returned {len(results)} results")

        return {
            'results': [
                {
                    'phone': r[0],
                    'name': r[1],
                    'tier': r[2] or 'free',
                    'active': r[3],
                    'created_at': r[4].isoformat() if r[4] else None
                }
                for r in results
            ]
        }
    except Exception as e:
        logger.error(f"Error searching customers: {e}")
        return {'results': [], 'error': str(e)}
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/cs/customer/{phone_number}")
async def cs_get_customer(phone_number: str, user: str = Depends(verify_cs_auth)):
    """Get customer details"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            SELECT phone_number, first_name, timezone, premium_status, active,
                   created_at, last_interaction, premium_since
            FROM users WHERE phone_number = %s
        """, (phone_number,))

        result = c.fetchone()
        if not result:
            raise HTTPException(status_code=404, detail="Customer not found")

        # Get notes
        c.execute("""
            SELECT note, created_by, created_at FROM customer_notes
            WHERE phone_number = %s ORDER BY created_at DESC
        """, (phone_number,))
        notes = c.fetchall()

        return {
            'phone': result[0],
            'name': result[1],
            'timezone': result[2],
            'tier': result[3] or 'free',
            'active': result[4],
            'created_at': result[5].isoformat() if result[5] else None,
            'last_interaction': result[6].isoformat() if result[6] else None,
            'premium_since': result[7].isoformat() if result[7] else None,
            'notes': [
                {'note': n[0], 'created_by': n[1], 'created_at': n[2].isoformat() if n[2] else None}
                for n in notes
            ]
        }
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error getting customer: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/cs/customer/{phone_number}/reminders")
async def cs_get_customer_reminders(phone_number: str, user: str = Depends(verify_cs_auth)):
    """Get customer reminders"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            SELECT id, reminder_text, reminder_time, status, created_at
            FROM reminders WHERE phone_number = %s
            ORDER BY reminder_time DESC LIMIT 50
        """, (phone_number,))

        results = c.fetchall()
        return [
            {
                'id': r[0],
                'text': r[1],
                'reminder_time': r[2].isoformat() if r[2] else None,
                'status': r[3],
                'created_at': r[4].isoformat() if r[4] else None
            }
            for r in results
        ]
    except Exception as e:
        logger.error(f"Error getting customer reminders: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/cs/customer/{phone_number}/lists")
async def cs_get_customer_lists(phone_number: str, user: str = Depends(verify_cs_auth)):
    """Get customer lists with items"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            SELECT id, list_name, created_at FROM lists
            WHERE phone_number = %s ORDER BY created_at DESC
        """, (phone_number,))

        lists = c.fetchall()
        result = []

        for lst in lists:
            c.execute("""
                SELECT id, item_text, completed FROM list_items
                WHERE list_id = %s ORDER BY created_at
            """, (lst[0],))
            items = c.fetchall()

            result.append({
                'id': lst[0],
                'name': lst[1],
                'created_at': lst[2].isoformat() if lst[2] else None,
                'items': [{'id': i[0], 'text': i[1], 'completed': i[2]} for i in items]
            })

        return result
    except Exception as e:
        logger.error(f"Error getting customer lists: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


@router.get("/cs/customer/{phone_number}/memories")
async def cs_get_customer_memories(phone_number: str, user: str = Depends(verify_cs_auth)):
    """Get customer memories"""
    conn = None
    try:
        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            SELECT id, content, created_at FROM memories
            WHERE phone_number = %s ORDER BY created_at DESC LIMIT 50
        """, (phone_number,))

        results = c.fetchall()
        return [
            {
                'id': r[0],
                'content': r[1],
                'created_at': r[2].isoformat() if r[2] else None
            }
            for r in results
        ]
    except Exception as e:
        logger.error(f"Error getting customer memories: {e}")
        return []
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/cs/customer/{phone_number}/tier")
async def cs_update_customer_tier(phone_number: str, request: Request, user: str = Depends(verify_cs_auth)):
    """Update customer subscription tier"""
    conn = None
    try:
        body = await request.json()
        new_tier = body.get('tier', 'free')

        if new_tier not in ['free', 'premium', 'family']:
            raise HTTPException(status_code=400, detail="Invalid tier")

        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            UPDATE users SET premium_status = %s WHERE phone_number = %s
        """, (new_tier, phone_number))
        conn.commit()

        logger.info(f"Updated {phone_number[-4:]} tier to {new_tier} by {user}")
        return {'success': True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error updating tier: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)


@router.post("/cs/customer/{phone_number}/notes")
async def cs_add_customer_note(phone_number: str, request: Request, user: str = Depends(verify_cs_auth)):
    """Add a note to customer record"""
    conn = None
    try:
        body = await request.json()
        note = body.get('note', '').strip()

        if not note:
            raise HTTPException(status_code=400, detail="Note is required")

        conn = get_db_connection()
        c = conn.cursor()

        c.execute("""
            INSERT INTO customer_notes (phone_number, note, created_by)
            VALUES (%s, %s, %s)
        """, (phone_number, note, user))
        conn.commit()

        logger.info(f"Added note for {phone_number[-4:]} by {user}")
        return {'success': True}
    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error adding note: {e}")
        raise HTTPException(status_code=500, detail="Internal server error")
    finally:
        if conn:
            return_db_connection(conn)
