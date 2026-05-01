import os
import sqlite3
import json
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("BIRDNET_API_KEY", "changeme-secret-key")
DB_PATH = os.environ.get("DB_PATH", "birdnet.db")


def get_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    conn = get_db()
    conn.execute("""
        CREATE TABLE IF NOT EXISTS detections (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            species TEXT NOT NULL,
            confidence REAL NOT NULL,
            timestamp TEXT NOT NULL,
            latitude REAL,
            longitude REAL,
            audio_file TEXT,
            created_at TEXT DEFAULT (datetime('now'))
        )
    """)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON detections(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_species ON detections(species)")
    conn.commit()
    conn.close()


def cleanup_old_records():
    """Delete records older than 7 days."""
    conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    conn.execute("DELETE FROM detections WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()


# --- API Endpoints ---

@app.route("/api/detect", methods=["POST"])
def add_detection():
    """Pi pushes detection results here."""
    key = request.headers.get("X-API-Key", "")
    if key != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401

    data = request.json
    if not data:
        return jsonify({"error": "No JSON body"}), 400

    detections = data if isinstance(data, list) else [data]
    conn = get_db()
    count = 0
    for d in detections:
        if not d.get("species") or d.get("confidence") is None:
            continue
        conn.execute(
            "INSERT INTO detections (species, confidence, timestamp, latitude, longitude, audio_file) VALUES (?, ?, ?, ?, ?, ?)",
            (
                d["species"],
                float(d["confidence"]),
                d.get("timestamp", datetime.utcnow().isoformat()),
                d.get("latitude"),
                d.get("longitude"),
                d.get("audio_file"),
            ),
        )
        count += 1
    conn.commit()
    conn.close()
    cleanup_old_records()
    return jsonify({"status": "ok", "inserted": count}), 201


@app.route("/api/detections")
def get_detections():
    """Get recent detections with optional filters."""
    hours = request.args.get("hours", 24, type=int)
    species = request.args.get("species", None)
    limit = request.args.get("limit", 200, type=int)

    cutoff = (datetime.utcnow() - timedelta(hours=hours)).isoformat()
    conn = get_db()

    query = "SELECT * FROM detections WHERE timestamp > ?"
    params = [cutoff]
    if species:
        query += " AND species = ?"
        params.append(species)
    query += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)

    rows = conn.execute(query, params).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/stats")
def get_stats():
    """Dashboard stats: top species, daily counts, totals."""
    conn = get_db()
    cutoff_7d = (datetime.utcnow() - timedelta(days=7)).isoformat()

    total = conn.execute(
        "SELECT COUNT(*) as c FROM detections WHERE timestamp > ?", (cutoff_7d,)
    ).fetchone()["c"]

    unique_species = conn.execute(
        "SELECT COUNT(DISTINCT species) as c FROM detections WHERE timestamp > ?",
        (cutoff_7d,),
    ).fetchone()["c"]

    top_species = conn.execute(
        """SELECT species, COUNT(*) as count, ROUND(AVG(confidence), 2) as avg_confidence
           FROM detections WHERE timestamp > ?
           GROUP BY species ORDER BY count DESC LIMIT 15""",
        (cutoff_7d,),
    ).fetchall()

    daily_counts = conn.execute(
        """SELECT DATE(timestamp) as day, COUNT(*) as count
           FROM detections WHERE timestamp > ?
           GROUP BY DATE(timestamp) ORDER BY day""",
        (cutoff_7d,),
    ).fetchall()

    hourly_counts = conn.execute(
        """SELECT strftime('%H', timestamp) as hour, COUNT(*) as count
           FROM detections WHERE timestamp > ?
           GROUP BY strftime('%H', timestamp) ORDER BY hour""",
        (cutoff_7d,),
    ).fetchall()

    conn.close()
    return jsonify({
        "total_detections": total,
        "unique_species": unique_species,
        "top_species": [dict(r) for r in top_species],
        "daily_counts": [dict(r) for r in daily_counts],
        "hourly_counts": [dict(r) for r in hourly_counts],
    })


@app.route("/api/live")
def live_feed():
    """Last 20 detections for live feed."""
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM detections ORDER BY timestamp DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


# --- Dashboard UI ---

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


DASHBOARD_HTML = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>BirdNET Dashboard</title>
    <script src="https://cdn.jsdelivr.net/npm/chart.js@4"></script>
    <style>
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', sans-serif; background: #0f1117; color: #e0e0e0; }
        .header { background: linear-gradient(135deg, #1a472a 0%, #2d5a3f 100%); padding: 20px 30px; display: flex; align-items: center; justify-content: space-between; }
        .header h1 { font-size: 24px; color: #fff; }
        .header h1 span { color: #6fcf97; }
        .header .status { font-size: 13px; color: #a0a0a0; }
        .header .status .dot { display: inline-block; width: 8px; height: 8px; border-radius: 50%; background: #6fcf97; margin-right: 6px; animation: pulse 2s infinite; }
        @keyframes pulse { 0%, 100% { opacity: 1; } 50% { opacity: 0.4; } }
        .stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 16px; padding: 20px 30px; }
        .stat-card { background: #1a1d27; border-radius: 12px; padding: 20px; border: 1px solid #2a2d37; }
        .stat-card .label { font-size: 12px; color: #888; text-transform: uppercase; letter-spacing: 1px; }
        .stat-card .value { font-size: 36px; font-weight: 700; color: #6fcf97; margin-top: 4px; }
        .stat-card .sub { font-size: 12px; color: #666; margin-top: 4px; }
        .grid { display: grid; grid-template-columns: 1fr 1fr; gap: 16px; padding: 0 30px 20px; }
        @media (max-width: 900px) { .grid { grid-template-columns: 1fr; } }
        .card { background: #1a1d27; border-radius: 12px; padding: 20px; border: 1px solid #2a2d37; }
        .card h2 { font-size: 16px; color: #ccc; margin-bottom: 16px; }
        .full-width { grid-column: 1 / -1; }
        table { width: 100%; border-collapse: collapse; }
        th { text-align: left; font-size: 11px; color: #666; text-transform: uppercase; letter-spacing: 1px; padding: 8px 12px; border-bottom: 1px solid #2a2d37; }
        td { padding: 10px 12px; border-bottom: 1px solid #1f2230; font-size: 14px; }
        tr:hover { background: #1f2230; }
        .badge { display: inline-block; padding: 2px 8px; border-radius: 10px; font-size: 12px; font-weight: 600; }
        .badge-high { background: #1a3a2a; color: #6fcf97; }
        .badge-med { background: #3a3a1a; color: #f2c94c; }
        .badge-low { background: #3a1a1a; color: #eb5757; }
        .species-bar { display: flex; align-items: center; gap: 10px; margin-bottom: 8px; }
        .species-bar .name { width: 160px; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
        .species-bar .bar-bg { flex: 1; height: 20px; background: #2a2d37; border-radius: 4px; overflow: hidden; }
        .species-bar .bar-fill { height: 100%; background: linear-gradient(90deg, #27ae60, #6fcf97); border-radius: 4px; transition: width 0.5s; }
        .species-bar .count { width: 40px; text-align: right; font-size: 13px; color: #888; }
        .empty-state { text-align: center; padding: 40px; color: #555; }
        canvas { max-height: 250px; }
    </style>
</head>
<body>
    <div class="header">
        <h1>Bird<span>NET</span> Dashboard</h1>
        <div class="status"><span class="dot"></span>Live &mdash; refreshes every 30s</div>
    </div>
    <div class="stats-row">
        <div class="stat-card"><div class="label">Total Detections (7d)</div><div class="value" id="total">--</div></div>
        <div class="stat-card"><div class="label">Unique Species (7d)</div><div class="value" id="species-count">--</div></div>
        <div class="stat-card"><div class="label">Latest Detection</div><div class="value" id="latest-species" style="font-size:20px;">--</div><div class="sub" id="latest-time"></div></div>
        <div class="stat-card"><div class="label">Pi Status</div><div class="value" id="pi-status" style="font-size:20px;">--</div><div class="sub" id="pi-last-seen"></div></div>
    </div>
    <div class="grid">
        <div class="card"><h2>Daily Detections</h2><canvas id="dailyChart"></canvas></div>
        <div class="card"><h2>Activity by Hour</h2><canvas id="hourlyChart"></canvas></div>
        <div class="card"><h2>Top Species (7 days)</h2><div id="top-species"></div></div>
        <div class="card"><h2>Species Distribution</h2><canvas id="speciesChart"></canvas></div>
        <div class="card full-width"><h2>Live Feed &mdash; Recent Detections</h2>
            <table><thead><tr><th>Time</th><th>Species</th><th>Confidence</th></tr></thead><tbody id="live-table"></tbody></table>
            <div class="empty-state" id="empty-msg">Waiting for detections from your Pi...</div>
        </div>
    </div>
    <script>
        let dailyChart, hourlyChart, speciesChart;
        const COLORS = ['#6fcf97','#56ccf2','#f2c94c','#eb5757','#bb6bd9','#f2994a','#27ae60','#2d9cdb','#e0e0e0','#828282'];

        function confidenceBadge(c) {
            if (c >= 0.8) return '<span class="badge badge-high">' + (c*100).toFixed(0) + '%</span>';
            if (c >= 0.5) return '<span class="badge badge-med">' + (c*100).toFixed(0) + '%</span>';
            return '<span class="badge badge-low">' + (c*100).toFixed(0) + '%</span>';
        }

        function timeAgo(ts) {
            const diff = (Date.now() - new Date(ts+'Z').getTime()) / 1000;
            if (diff < 60) return Math.floor(diff) + 's ago';
            if (diff < 3600) return Math.floor(diff/60) + 'm ago';
            if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
            return Math.floor(diff/86400) + 'd ago';
        }

        async function fetchStats() {
            try {
                const [stats, live] = await Promise.all([
                    fetch('/api/stats').then(r => r.json()),
                    fetch('/api/live').then(r => r.json())
                ]);

                document.getElementById('total').textContent = stats.total_detections;
                document.getElementById('species-count').textContent = stats.unique_species;

                if (live.length > 0) {
                    document.getElementById('latest-species').textContent = live[0].species;
                    document.getElementById('latest-time').textContent = timeAgo(live[0].timestamp);
                    document.getElementById('pi-status').textContent = 'Online';
                    document.getElementById('pi-status').style.color = '#6fcf97';
                    document.getElementById('pi-last-seen').textContent = 'Last seen: ' + timeAgo(live[0].timestamp);
                    document.getElementById('empty-msg').style.display = 'none';
                } else {
                    document.getElementById('pi-status').textContent = 'Waiting';
                    document.getElementById('pi-status').style.color = '#f2c94c';
                }

                // Live table
                const tbody = document.getElementById('live-table');
                tbody.innerHTML = live.map(d =>
                    '<tr><td>' + timeAgo(d.timestamp) + '</td><td>' + d.species + '</td><td>' + confidenceBadge(d.confidence) + '</td></tr>'
                ).join('');

                // Top species bars
                const maxCount = stats.top_species.length ? stats.top_species[0].count : 1;
                document.getElementById('top-species').innerHTML = stats.top_species.slice(0, 10).map(s =>
                    '<div class="species-bar"><div class="name">' + s.species + '</div><div class="bar-bg"><div class="bar-fill" style="width:' + (s.count/maxCount*100) + '%"></div></div><div class="count">' + s.count + '</div></div>'
                ).join('') || '<div class="empty-state">No data yet</div>';

                // Daily chart
                const dailyLabels = stats.daily_counts.map(d => d.day.slice(5));
                const dailyData = stats.daily_counts.map(d => d.count);
                if (dailyChart) dailyChart.destroy();
                dailyChart = new Chart(document.getElementById('dailyChart'), {
                    type: 'bar', data: { labels: dailyLabels, datasets: [{ data: dailyData, backgroundColor: '#27ae60', borderRadius: 6 }] },
                    options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { color: '#666' }, grid: { color: '#2a2d37' } }, x: { ticks: { color: '#666' }, grid: { display: false } } } }
                });

                // Hourly chart
                const hourlyLabels = stats.hourly_counts.map(d => d.hour + ':00');
                const hourlyData = stats.hourly_counts.map(d => d.count);
                if (hourlyChart) hourlyChart.destroy();
                hourlyChart = new Chart(document.getElementById('hourlyChart'), {
                    type: 'line', data: { labels: hourlyLabels, datasets: [{ data: hourlyData, borderColor: '#56ccf2', backgroundColor: 'rgba(86,204,242,0.1)', fill: true, tension: 0.4, pointRadius: 3 }] },
                    options: { plugins: { legend: { display: false } }, scales: { y: { beginAtZero: true, ticks: { color: '#666' }, grid: { color: '#2a2d37' } }, x: { ticks: { color: '#666' }, grid: { display: false } } } }
                });

                // Species donut
                const topN = stats.top_species.slice(0, 8);
                if (speciesChart) speciesChart.destroy();
                if (topN.length) {
                    speciesChart = new Chart(document.getElementById('speciesChart'), {
                        type: 'doughnut', data: { labels: topN.map(s => s.species), datasets: [{ data: topN.map(s => s.count), backgroundColor: COLORS }] },
                        options: { plugins: { legend: { position: 'right', labels: { color: '#aaa', boxWidth: 12, font: { size: 11 } } } } }
                    });
                }
            } catch (e) { console.error('Fetch error:', e); }
        }

        fetchStats();
        setInterval(fetchStats, 30000);
    </script>
</body>
</html>
"""

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
