import os
import re
import csv
import io
import sqlite3
import json
import base64
import urllib.request
import urllib.error
from datetime import datetime, timedelta
from flask import Flask, request, jsonify, render_template_string, Response
from flask_cors import CORS

app = Flask(__name__)
CORS(app)

API_KEY = os.environ.get("BIRDNET_API_KEY", "changeme-secret-key")
OPENAI_API_KEY = os.environ.get("OPENAI_API_KEY", "")
OPENAI_IMAGE_MODEL = os.environ.get("OPENAI_IMAGE_MODEL", "gpt-image-1")
DB_PATH = os.environ.get("DB_PATH", "birdnet.db")


def get_db():
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    db_dir = os.path.dirname(os.path.abspath(DB_PATH))
    if db_dir:
        os.makedirs(db_dir, exist_ok=True)
    conn = get_db()
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
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
    existing_cols = {row["name"] for row in conn.execute("PRAGMA table_info(detections)")}
    for col, ddl in (
        ("scientific_name", "ALTER TABLE detections ADD COLUMN scientific_name TEXT"),
        ("start_offset", "ALTER TABLE detections ADD COLUMN start_offset REAL"),
        ("end_offset", "ALTER TABLE detections ADD COLUMN end_offset REAL"),
    ):
        if col not in existing_cols:
            conn.execute(ddl)
    conn.execute("CREATE INDEX IF NOT EXISTS idx_timestamp ON detections(timestamp)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_species ON detections(species)")
    conn.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_dedup "
        "ON detections(timestamp, scientific_name, start_offset)"
    )
    # bird image cache: one entry per species, image bytes stored as PNG
    conn.execute("""
        CREATE TABLE IF NOT EXISTS bird_images (
            species TEXT PRIMARY KEY,
            scientific_name TEXT,
            image_b64 TEXT,
            mime TEXT DEFAULT 'image/png',
            generated_at TEXT DEFAULT (datetime('now')),
            error TEXT
        )
    """)
    conn.commit()
    conn.close()


def cleanup_old_records():
    conn = get_db()
    cutoff = (datetime.utcnow() - timedelta(days=7)).isoformat()
    conn.execute("DELETE FROM detections WHERE timestamp < ?", (cutoff,))
    conn.commit()
    conn.close()


# --- API: detections ingest ---

@app.route("/api/detect", methods=["POST"])
def add_detection():
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
                d["species"], float(d["confidence"]),
                d.get("timestamp", datetime.utcnow().isoformat()),
                d.get("latitude"), d.get("longitude"), d.get("audio_file"),
            ),
        )
        count += 1
    conn.commit()
    conn.close()
    cleanup_old_records()
    return jsonify({"status": "ok", "inserted": count}), 201


_RUN_TS_RE = re.compile(r"run_(\d{8})_(\d{6})")


def _parse_run_timestamp(path):
    if not path:
        return None
    m = _RUN_TS_RE.search(path)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1) + m.group(2), "%Y%m%d%H%M%S")
    except ValueError:
        return None


@app.route("/api/upload", methods=["POST"])
def upload_csv():
    if request.headers.get("X-API-Key", "") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    f = request.files.get("file")
    if f is None:
        return jsonify({"error": "Missing 'file' form field"}), 400
    raw = f.read()
    if not raw:
        return jsonify({"error": "Empty file"}), 400
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        return jsonify({"error": "File is not UTF-8 text"}), 400

    candidate_paths = [f.filename or "", request.form.get("source_path", "")]
    base_dt = next((dt for dt in (_parse_run_timestamp(p) for p in candidate_paths) if dt), None)
    if base_dt is None:
        base_dt = datetime.utcnow()

    try:
        try:
            dialect = csv.Sniffer().sniff(text[:4096], delimiters=",\t;")
        except csv.Error:
            dialect = csv.excel
        reader = csv.DictReader(io.StringIO(text), dialect=dialect)
        fields = reader.fieldnames or []
    except csv.Error as e:
        return jsonify({"error": f"Malformed CSV: {e}"}), 400

    header_map = {h.strip().lower(): h for h in fields}
    required = ["start (s)", "end (s)", "scientific name", "common name", "confidence"]
    missing = [r for r in required if r not in header_map]
    if missing:
        return jsonify({"error": f"Missing CSV columns: {missing}"}), 400

    col_start = header_map["start (s)"]
    col_end = header_map["end (s)"]
    col_sci = header_map["scientific name"]
    col_com = header_map["common name"]
    col_conf = header_map["confidence"]

    rows = []
    try:
        for line_no, row in enumerate(reader, start=2):
            if row is None:
                continue
            try:
                start = float(row[col_start])
                end = float(row[col_end])
                conf = float(row[col_conf])
            except (TypeError, ValueError, KeyError) as e:
                return jsonify({"error": f"Malformed row at line {line_no}: {e}"}), 400
            sci = (row.get(col_sci) or "").strip()
            com = (row.get(col_com) or "").strip() or sci
            if not com:
                continue
            ts = (base_dt + timedelta(seconds=start)).isoformat()
            rows.append((com, sci, conf, ts, start, end, f.filename))
    except csv.Error as e:
        return jsonify({"error": f"Malformed CSV: {e}"}), 400

    inserted = 0
    duplicates = 0
    conn = get_db()
    try:
        for r in rows:
            try:
                conn.execute(
                    "INSERT INTO detections "
                    "(species, scientific_name, confidence, timestamp, start_offset, end_offset, audio_file) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?)",
                    r,
                )
                inserted += 1
            except sqlite3.IntegrityError:
                duplicates += 1
        conn.commit()
    finally:
        conn.close()
    cleanup_old_records()
    return jsonify({"inserted": inserted, "duplicates": duplicates}), 200


# --- API: read endpoints ---

@app.route("/api/detections")
def get_detections():
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
        """SELECT species,
                  MAX(scientific_name) as scientific_name,
                  COUNT(*) as count,
                  ROUND(AVG(confidence), 2) as avg_confidence
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
    conn = get_db()
    rows = conn.execute(
        "SELECT * FROM detections ORDER BY timestamp DESC LIMIT 20"
    ).fetchall()
    conn.close()
    return jsonify([dict(r) for r in rows])


@app.route("/api/health")
def health():
    return jsonify({"status": "ok", "time": datetime.utcnow().isoformat()})


# --- Bird image generation (OpenAI gpt-image-1) ---

def _generate_bird_image_b64(common_name, scientific_name=None):
    """Call OpenAI Images API and return base64 PNG, or raise."""
    if not OPENAI_API_KEY:
        raise RuntimeError("OPENAI_API_KEY not configured")

    sci_part = f" ({scientific_name})" if scientific_name else ""
    prompt = (
        f"A naturalist field illustration of a {common_name}{sci_part} bird, "
        f"shown perched on a branch in soft natural light. "
        f"Painterly style reminiscent of a vintage Audubon plate, "
        f"muted forest palette, subtle parchment background, "
        f"detailed plumage, scientifically accurate proportions, "
        f"side profile, no text, no watermark, no border."
    )
    body = json.dumps({
        "model": OPENAI_IMAGE_MODEL,
        "prompt": prompt,
        "n": 1,
        "size": "1024x1024",
    }).encode("utf-8")
    req = urllib.request.Request(
        "https://api.openai.com/v1/images/generations",
        data=body,
        headers={
            "Authorization": f"Bearer {OPENAI_API_KEY}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", errors="ignore")[:300]
        raise RuntimeError(f"OpenAI HTTP {e.code}: {detail}")
    except urllib.error.URLError as e:
        raise RuntimeError(f"OpenAI URL error: {e}")

    item = (payload.get("data") or [{}])[0]
    if item.get("b64_json"):
        return item["b64_json"]
    if item.get("url"):
        # fall back to fetching the URL → base64
        with urllib.request.urlopen(item["url"], timeout=60) as r:
            return base64.b64encode(r.read()).decode("ascii")
    raise RuntimeError("OpenAI response missing image data")


@app.route("/api/bird-image")
def bird_image():
    """Return a PNG for `species`, generating on first request and caching forever.

    Query params:
      species   (required) – common name, e.g. "Northern Cardinal"
      scientific (optional) – binomial, helps prompt accuracy
      format    (optional) – "json" returns {url:"data:..."} else raw PNG
    """
    species = (request.args.get("species") or "").strip()
    if not species:
        return jsonify({"error": "missing species"}), 400
    scientific = (request.args.get("scientific") or "").strip() or None
    fmt = request.args.get("format", "png")

    conn = get_db()
    row = conn.execute(
        "SELECT image_b64, mime, error FROM bird_images WHERE species = ?",
        (species,),
    ).fetchone()

    if row and row["image_b64"]:
        b64 = row["image_b64"]
        mime = row["mime"] or "image/png"
        conn.close()
        if fmt == "json":
            return jsonify({"species": species, "url": f"data:{mime};base64,{b64}", "cached": True})
        return Response(base64.b64decode(b64), mimetype=mime,
                        headers={"Cache-Control": "public, max-age=31536000, immutable"})

    # Not cached → try to generate
    if not OPENAI_API_KEY:
        conn.close()
        return jsonify({"error": "OPENAI_API_KEY not configured"}), 503

    try:
        b64 = _generate_bird_image_b64(species, scientific)
    except Exception as e:
        # cache the failure briefly to avoid hammering on every poll
        try:
            conn.execute(
                "INSERT OR REPLACE INTO bird_images (species, scientific_name, image_b64, error) VALUES (?, ?, NULL, ?)",
                (species, scientific, str(e)[:500]),
            )
            conn.commit()
        finally:
            conn.close()
        return jsonify({"error": "generation failed", "detail": str(e)[:300]}), 502

    conn.execute(
        "INSERT OR REPLACE INTO bird_images (species, scientific_name, image_b64, mime, error) VALUES (?, ?, ?, 'image/png', NULL)",
        (species, scientific, b64),
    )
    conn.commit()
    conn.close()

    if fmt == "json":
        return jsonify({"species": species, "url": f"data:image/png;base64,{b64}", "cached": False})
    return Response(base64.b64decode(b64), mimetype="image/png",
                    headers={"Cache-Control": "public, max-age=31536000, immutable"})


@app.route("/api/bird-images/clear", methods=["POST"])
def clear_bird_images():
    """Admin: drop the image cache (forces regeneration)."""
    if request.headers.get("X-API-Key", "") != API_KEY:
        return jsonify({"error": "Unauthorized"}), 401
    species = request.args.get("species")
    conn = get_db()
    if species:
        conn.execute("DELETE FROM bird_images WHERE species = ?", (species,))
    else:
        conn.execute("DELETE FROM bird_images")
    conn.commit()
    n = conn.total_changes
    conn.close()
    return jsonify({"cleared": n})


# --- Dashboard UI ---

@app.route("/")
def dashboard():
    return render_template_string(DASHBOARD_HTML)


DASHBOARD_HTML = r"""
<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>BirdNET — Field Dashboard</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=Fraunces:ital,opsz,wght@0,9..144,400;0,9..144,500;0,9..144,600;0,9..144,700;1,9..144,400&family=Inter:wght@400;500;600;700&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet">
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.1"></script>
<style>
  :root {
    --forest-deepest: #0a1810;
    --forest-deep:    #0f2418;
    --forest:         #14301f;
    --forest-mid:     #1a3a2a;
    --forest-light:   #244a35;
    --moss:           #3d6b4a;
    --sage:           #7fa68a;
    --mint:           #a8d4b6;
    --bone:           #f3eed9;
    --bone-dim:       #d8d2bd;
    --parchment:      #ebe4cd;
    --ink:            #0a1810;
    --ink-2:          #1a2a20;
    --rust:           #b8623a;
    --amber:          #d8a64a;
    --line:           rgba(243, 238, 217, 0.10);
    --line-strong:    rgba(243, 238, 217, 0.18);
  }

  * { margin: 0; padding: 0; box-sizing: border-box; }
  html, body { background: var(--forest-deepest); color: var(--bone); }
  body {
    font-family: 'Inter', system-ui, sans-serif;
    font-size: 14px;
    line-height: 1.5;
    min-height: 100vh;
    background:
      radial-gradient(ellipse 80% 50% at 50% -10%, rgba(61,107,74,0.18), transparent 60%),
      radial-gradient(ellipse 60% 40% at 100% 100%, rgba(26,58,42,0.4), transparent 60%),
      var(--forest-deepest);
  }

  .wrap { max-width: 1320px; margin: 0 auto; padding: 0 32px; }

  /* ─────────── HEADER ─────────── */
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    padding: 22px 32px;
    border-bottom: 1px solid var(--line);
    background: linear-gradient(180deg, rgba(10,24,16,0.9), rgba(10,24,16,0.6));
    backdrop-filter: blur(8px);
    position: sticky; top: 0; z-index: 50;
  }
  .brand { display: flex; align-items: center; gap: 14px; }
  .crest {
    width: 38px; height: 38px;
    border: 1px solid var(--sage);
    border-radius: 50%;
    display: grid; place-items: center;
    color: var(--mint);
    background: radial-gradient(circle at 30% 30%, rgba(127,166,138,0.2), transparent 70%);
  }
  .brand h1 {
    font-family: 'Fraunces', serif;
    font-size: 22px;
    font-weight: 500;
    letter-spacing: -0.01em;
    color: var(--bone);
  }
  .brand h1 em {
    font-style: italic;
    color: var(--mint);
    font-weight: 400;
  }
  .brand .sub {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--sage);
    margin-top: 2px;
  }
  .topbar-meta { display: flex; align-items: center; gap: 28px; }
  .live-pill {
    display: flex; align-items: center; gap: 8px;
    padding: 7px 14px;
    border: 1px solid var(--line-strong);
    border-radius: 999px;
    background: rgba(127,166,138,0.06);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--mint);
  }
  .live-pill .dot {
    width: 7px; height: 7px; border-radius: 50%;
    background: var(--mint);
    box-shadow: 0 0 0 0 rgba(168,212,182,0.7);
    animation: pulse 2.4s infinite;
  }
  @keyframes pulse {
    0%   { box-shadow: 0 0 0 0 rgba(168,212,182,0.7); }
    70%  { box-shadow: 0 0 0 10px rgba(168,212,182,0); }
    100% { box-shadow: 0 0 0 0 rgba(168,212,182,0); }
  }
  .topbar-meta .clock {
    font-family: 'JetBrains Mono', monospace;
    font-size: 12px;
    color: var(--bone-dim);
    letter-spacing: 0.05em;
  }

  /* ─────────── HERO ─────────── */
  .hero {
    display: grid;
    grid-template-columns: 1.05fr 1fr;
    gap: 28px;
    margin: 36px 0 28px;
  }
  @media (max-width: 980px) { .hero { grid-template-columns: 1fr; } }

  .hero-text { padding: 8px 4px; }
  .eyebrow {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.22em;
    text-transform: uppercase;
    color: var(--sage);
    margin-bottom: 18px;
    display: flex; align-items: center; gap: 12px;
  }
  .eyebrow::before {
    content: ''; display: inline-block;
    width: 28px; height: 1px; background: var(--sage);
  }
  .hero h2 {
    font-family: 'Fraunces', serif;
    font-weight: 400;
    font-size: 56px;
    line-height: 1.04;
    letter-spacing: -0.025em;
    color: var(--bone);
    margin-bottom: 6px;
    text-wrap: balance;
  }
  .hero h2 .latin {
    display: block;
    font-style: italic;
    font-size: 22px;
    color: var(--sage);
    font-weight: 400;
    margin-top: 8px;
    letter-spacing: 0;
  }
  .hero-meta {
    display: grid;
    grid-template-columns: repeat(3, auto);
    gap: 32px;
    margin-top: 32px;
    padding-top: 24px;
    border-top: 1px solid var(--line);
    max-width: 520px;
  }
  .hero-meta .field { }
  .hero-meta .k {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--sage);
    margin-bottom: 6px;
  }
  .hero-meta .v {
    font-family: 'Fraunces', serif;
    font-size: 24px;
    font-weight: 500;
    color: var(--bone);
  }
  .hero-meta .v small { font-size: 13px; color: var(--bone-dim); font-family: 'Inter', sans-serif; font-weight: 400; }

  /* hero image: AI-generated bird */
  .hero-img-wrap {
    position: relative;
    border-radius: 4px;
    overflow: hidden;
    aspect-ratio: 4/3;
    background: var(--forest);
    border: 1px solid var(--line-strong);
    box-shadow: 0 30px 60px -20px rgba(0,0,0,0.6);
  }
  .hero-img {
    width: 100%; height: 100%;
    object-fit: cover;
    display: block;
  }
  .hero-img-placeholder {
    width: 100%; height: 100%;
    display: grid; place-items: center;
    background:
      repeating-linear-gradient(135deg, transparent 0 18px, rgba(127,166,138,0.05) 18px 19px),
      linear-gradient(180deg, var(--forest-light), var(--forest));
    color: var(--sage);
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    letter-spacing: 0.15em;
    text-transform: uppercase;
  }
  .hero-img-overlay {
    position: absolute; left: 0; right: 0; bottom: 0;
    padding: 16px 18px;
    background: linear-gradient(0deg, rgba(10,24,16,0.92), transparent);
    display: flex; justify-content: space-between; align-items: flex-end;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--bone-dim);
  }
  .hero-img-overlay .conf {
    font-family: 'Fraunces', serif;
    font-size: 22px;
    font-weight: 500;
    color: var(--mint);
    text-transform: none;
    letter-spacing: 0;
  }

  /* ─────────── STAT STRIP ─────────── */
  .stat-strip {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    border: 1px solid var(--line);
    border-radius: 4px;
    background: rgba(20,48,31,0.4);
    margin-bottom: 28px;
  }
  @media (max-width: 760px) { .stat-strip { grid-template-columns: repeat(2, 1fr); } }
  .stat {
    padding: 24px 28px;
    border-right: 1px solid var(--line);
  }
  .stat:last-child { border-right: none; }
  @media (max-width: 760px) {
    .stat:nth-child(2) { border-right: none; }
    .stat:nth-child(-n+2) { border-bottom: 1px solid var(--line); }
  }
  .stat .k {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--sage);
    margin-bottom: 10px;
  }
  .stat .v {
    font-family: 'Fraunces', serif;
    font-size: 42px;
    font-weight: 500;
    color: var(--bone);
    line-height: 1;
    letter-spacing: -0.02em;
  }
  .stat .delta {
    margin-top: 8px;
    font-size: 12px;
    color: var(--mint);
    display: flex; align-items: center; gap: 6px;
  }
  .stat .delta.muted { color: var(--bone-dim); }

  /* ─────────── GRID ─────────── */
  .grid {
    display: grid;
    grid-template-columns: 1.4fr 1fr;
    gap: 24px;
    margin-bottom: 28px;
  }
  @media (max-width: 980px) { .grid { grid-template-columns: 1fr; } }

  .panel {
    background: rgba(20,48,31,0.35);
    border: 1px solid var(--line);
    border-radius: 4px;
    padding: 24px 26px;
  }
  .panel h3 {
    font-family: 'Fraunces', serif;
    font-weight: 500;
    font-size: 20px;
    color: var(--bone);
    margin-bottom: 4px;
    letter-spacing: -0.01em;
  }
  .panel .panel-sub {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--sage);
    margin-bottom: 22px;
  }
  .panel-head {
    display: flex; justify-content: space-between; align-items: flex-start;
    margin-bottom: 22px;
  }
  .panel-head h3 + .panel-sub { margin-bottom: 0; margin-top: 4px; }
  .panel-head .right {
    display: flex; gap: 6px;
  }
  .seg {
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    padding: 6px 10px;
    letter-spacing: 0.1em;
    text-transform: uppercase;
    border: 1px solid var(--line-strong);
    color: var(--bone-dim);
    border-radius: 2px;
    background: transparent;
    cursor: pointer;
  }
  .seg.active { background: var(--moss); color: var(--bone); border-color: var(--moss); }

  canvas { max-height: 240px !important; }

  /* species list with images */
  .species-list { display: flex; flex-direction: column; gap: 14px; }
  .species-row {
    display: grid;
    grid-template-columns: 56px 1fr auto;
    gap: 14px;
    align-items: center;
    padding-bottom: 14px;
    border-bottom: 1px dashed var(--line);
  }
  .species-row:last-child { border-bottom: none; padding-bottom: 0; }
  .species-thumb {
    width: 56px; height: 56px;
    border-radius: 4px;
    overflow: hidden;
    background: var(--forest);
    border: 1px solid var(--line-strong);
    flex-shrink: 0;
    position: relative;
  }
  .species-thumb img { width: 100%; height: 100%; object-fit: cover; display: block; }
  .species-thumb .ph {
    width: 100%; height: 100%;
    display: grid; place-items: center;
    background: repeating-linear-gradient(135deg, var(--forest-mid) 0 8px, var(--forest) 8px 9px);
    font-family: 'JetBrains Mono', monospace;
    font-size: 9px;
    color: var(--sage);
    letter-spacing: 0.1em;
  }
  .species-info .name {
    font-family: 'Fraunces', serif;
    font-size: 16px;
    color: var(--bone);
    line-height: 1.2;
  }
  .species-info .latin {
    font-family: 'Fraunces', serif;
    font-style: italic;
    font-size: 12px;
    color: var(--sage);
    margin-top: 2px;
  }
  .species-bar-mini {
    margin-top: 6px;
    height: 3px;
    background: rgba(127,166,138,0.12);
    border-radius: 2px;
    overflow: hidden;
  }
  .species-bar-mini .fill {
    height: 100%;
    background: linear-gradient(90deg, var(--moss), var(--mint));
    border-radius: 2px;
  }
  .species-count {
    text-align: right;
  }
  .species-count .n {
    font-family: 'Fraunces', serif;
    font-size: 22px;
    color: var(--bone);
    font-weight: 500;
    line-height: 1;
  }
  .species-count .l {
    font-family: 'JetBrains Mono', monospace;
    font-size: 9px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--sage);
    margin-top: 4px;
  }

  /* live feed table */
  .feed-panel { padding: 0; }
  .feed-head {
    padding: 24px 26px 16px;
    display: flex; justify-content: space-between; align-items: flex-start;
    border-bottom: 1px solid var(--line);
  }
  table { width: 100%; border-collapse: collapse; }
  thead th {
    text-align: left;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--sage);
    padding: 14px 26px;
    background: rgba(10,24,16,0.4);
    border-bottom: 1px solid var(--line);
  }
  thead th:last-child, tbody td:last-child { text-align: right; }
  tbody td {
    padding: 14px 26px;
    border-bottom: 1px solid var(--line);
    font-size: 14px;
    vertical-align: middle;
  }
  tbody tr:last-child td { border-bottom: none; }
  tbody tr:hover { background: rgba(127,166,138,0.04); }

  .feed-species {
    display: flex; align-items: center; gap: 12px;
  }
  .feed-thumb {
    width: 36px; height: 36px;
    border-radius: 3px;
    background: var(--forest);
    border: 1px solid var(--line-strong);
    overflow: hidden;
    flex-shrink: 0;
  }
  .feed-thumb img { width: 100%; height: 100%; object-fit: cover; }
  .feed-thumb .ph {
    width: 100%; height: 100%;
    background: repeating-linear-gradient(135deg, var(--forest-mid) 0 6px, var(--forest) 6px 7px);
  }
  .feed-name {
    font-family: 'Fraunces', serif;
    font-size: 15px;
    color: var(--bone);
  }
  .feed-name .l {
    display: block;
    font-style: italic;
    font-size: 11px;
    color: var(--sage);
    margin-top: 1px;
  }
  .feed-time {
    font-family: 'JetBrains Mono', monospace;
    font-size: 11px;
    color: var(--bone-dim);
    letter-spacing: 0.04em;
  }

  .badge {
    display: inline-flex; align-items: center; gap: 6px;
    padding: 4px 10px;
    border-radius: 999px;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    font-weight: 500;
    letter-spacing: 0.1em;
    border: 1px solid;
  }
  .badge::before {
    content: ''; width: 5px; height: 5px; border-radius: 50%;
    background: currentColor;
  }
  .badge.high { color: var(--mint); border-color: rgba(168,212,182,0.4); background: rgba(168,212,182,0.06); }
  .badge.med  { color: var(--amber); border-color: rgba(216,166,74,0.4); background: rgba(216,166,74,0.06); }
  .badge.low  { color: var(--rust); border-color: rgba(184,98,58,0.4); background: rgba(184,98,58,0.06); }

  .empty {
    padding: 60px 20px;
    text-align: center;
    color: var(--sage);
    font-family: 'Fraunces', serif;
    font-style: italic;
    font-size: 15px;
  }

  /* footer */
  footer {
    margin-top: 60px;
    padding: 30px 0 50px;
    border-top: 1px solid var(--line);
    display: flex; justify-content: space-between; align-items: center;
    font-family: 'JetBrains Mono', monospace;
    font-size: 10px;
    letter-spacing: 0.16em;
    text-transform: uppercase;
    color: var(--sage);
  }
  footer .stamps { display: flex; gap: 24px; }

  /* generation overlay */
  .gen-shimmer {
    position: absolute; inset: 0;
    background: linear-gradient(90deg, transparent, rgba(168,212,182,0.08), transparent);
    background-size: 200% 100%;
    animation: shimmer 1.6s infinite;
  }
  @keyframes shimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
  }
</style>
</head>
<body>

<!-- ─── topbar ─── -->
<header class="topbar">
  <div class="brand">
    <div class="crest">
      <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round">
        <path d="M16 7a4 4 0 0 0-4-4 4 4 0 0 0-4 4c0 2 1 3 1 5l-3 3 3 1 1 3 3-3c2 0 3-1 5-1a4 4 0 0 0 4-4 4 4 0 0 0-4-4z"/>
        <circle cx="15" cy="8" r="0.5" fill="currentColor"/>
      </svg>
    </div>
    <div>
      <h1>Bird<em>NET</em> · Field Log</h1>
      <div class="sub">Acoustic Survey · Station 01</div>
    </div>
  </div>
  <div class="topbar-meta">
    <span class="clock" id="clock">—</span>
    <span class="live-pill"><span class="dot"></span><span>Live · 30s</span></span>
  </div>
</header>

<main class="wrap">

  <!-- ─── hero ─── -->
  <section class="hero">
    <div class="hero-text">
      <div class="eyebrow">Latest sighting · <span id="latest-time">just now</span></div>
      <h2>
        <span id="latest-species">Northern Cardinal</span>
        <span class="latin" id="latest-latin">Cardinalis cardinalis</span>
      </h2>
      <div class="hero-meta">
        <div class="field">
          <div class="k">Confidence</div>
          <div class="v" id="latest-conf">94<small>%</small></div>
        </div>
        <div class="field">
          <div class="k">Recorded</div>
          <div class="v" id="latest-recorded" style="font-size:18px;">06:42 <small>EDT</small></div>
        </div>
        <div class="field">
          <div class="k">Station</div>
          <div class="v" style="font-size:18px;">01 <small>· Pi</small></div>
        </div>
      </div>
    </div>
    <div class="hero-img-wrap">
      <img id="hero-img" class="hero-img" alt="" style="display:none;">
      <div id="hero-img-ph" class="hero-img-placeholder">
        <div style="text-align:center;">
          <div style="margin-bottom:8px;">◉ Generating image</div>
          <div style="font-size:9px; color: var(--bone-dim);">via gpt-image-1</div>
        </div>
      </div>
      <div class="hero-img-overlay">
        <span>AI rendering · cached</span>
        <span class="conf" id="hero-conf">94%</span>
      </div>
    </div>
  </section>

  <!-- ─── stats ─── -->
  <section class="stat-strip">
    <div class="stat">
      <div class="k">Total · 7d</div>
      <div class="v" id="stat-total">1,247</div>
      <div class="delta">↗ +18% vs prev week</div>
    </div>
    <div class="stat">
      <div class="k">Unique Species</div>
      <div class="v" id="stat-species">34</div>
      <div class="delta">↗ +3 newly observed</div>
    </div>
    <div class="stat">
      <div class="k">Peak Hour</div>
      <div class="v" id="stat-peak">06<small style="font-size:18px;color:var(--bone-dim);">:00</small></div>
      <div class="delta muted">Dawn chorus</div>
    </div>
    <div class="stat">
      <div class="k">Pi Status</div>
      <div class="v" id="stat-pi" style="color: var(--mint);">Online</div>
      <div class="delta muted" id="stat-pi-sub">Last seen 12s ago</div>
    </div>
  </section>

  <!-- ─── charts row ─── -->
  <section class="grid">
    <div class="panel">
      <div class="panel-head">
        <div>
          <h3>Daily activity</h3>
          <div class="panel-sub">Detections · last 7 days</div>
        </div>
        <div class="right">
          <button class="seg active">7d</button>
          <button class="seg">24h</button>
        </div>
      </div>
      <canvas id="dailyChart"></canvas>
    </div>
    <div class="panel">
      <div class="panel-head">
        <div>
          <h3>By hour</h3>
          <div class="panel-sub">Circadian pattern · UTC</div>
        </div>
      </div>
      <canvas id="hourlyChart"></canvas>
    </div>
  </section>

  <!-- ─── species + donut ─── -->
  <section class="grid">
    <div class="panel">
      <div class="panel-head">
        <div>
          <h3>Most observed</h3>
          <div class="panel-sub">Top species · 7 days</div>
        </div>
      </div>
      <div id="top-species" class="species-list"></div>
    </div>
    <div class="panel">
      <div class="panel-head">
        <div>
          <h3>Distribution</h3>
          <div class="panel-sub">Composition · 7 days</div>
        </div>
      </div>
      <canvas id="speciesChart"></canvas>
    </div>
  </section>

  <!-- ─── live feed ─── -->
  <section class="panel feed-panel" style="margin-bottom: 40px;">
    <div class="feed-head">
      <div>
        <h3>Field log</h3>
        <div class="panel-sub">Live detections · auto-refresh 30s</div>
      </div>
    </div>
    <table>
      <thead>
        <tr>
          <th style="width:60%;">Species</th>
          <th>Time</th>
          <th>Confidence</th>
        </tr>
      </thead>
      <tbody id="live-table"></tbody>
    </table>
    <div id="empty-msg" class="empty" style="display:none;">Awaiting transmissions from the station…</div>
  </section>

  <footer>
    <div>BirdNET · v2.0 · Field Edition</div>
    <div class="stamps">
      <span>Pi → Railway</span>
      <span>SQLite WAL</span>
      <span>gpt-image-1</span>
    </div>
  </footer>

</main>

<script>
  // ────── MOCK DATA (the real backend will replace fetch() targets) ──────
  const MOCK = {
    stats: {
      total_detections: 1247,
      unique_species: 34,
      top_species: [
        { species: 'Northern Cardinal', scientific_name: 'Cardinalis cardinalis', count: 187, avg_confidence: 0.91 },
        { species: 'American Robin',    scientific_name: 'Turdus migratorius',    count: 142, avg_confidence: 0.88 },
        { species: 'Carolina Wren',     scientific_name: 'Thryothorus ludovicianus', count: 124, avg_confidence: 0.86 },
        { species: 'Tufted Titmouse',   scientific_name: 'Baeolophus bicolor',    count: 98,  avg_confidence: 0.84 },
        { species: 'Black-capped Chickadee', scientific_name: 'Poecile atricapillus', count: 76, avg_confidence: 0.82 },
        { species: 'Blue Jay',          scientific_name: 'Cyanocitta cristata',   count: 64,  avg_confidence: 0.79 },
        { species: 'Mourning Dove',     scientific_name: 'Zenaida macroura',      count: 58,  avg_confidence: 0.77 },
        { species: 'Song Sparrow',      scientific_name: 'Melospiza melodia',     count: 41,  avg_confidence: 0.74 },
      ],
      daily_counts: [
        { day: '2026-04-26', count: 142 },
        { day: '2026-04-27', count: 168 },
        { day: '2026-04-28', count: 195 },
        { day: '2026-04-29', count: 211 },
        { day: '2026-04-30', count: 178 },
        { day: '2026-05-01', count: 189 },
        { day: '2026-05-02', count: 164 },
      ],
      hourly_counts: [
        {hour:'00',count:4},{hour:'01',count:2},{hour:'02',count:1},{hour:'03',count:3},
        {hour:'04',count:8},{hour:'05',count:42},{hour:'06',count:88},{hour:'07',count:74},
        {hour:'08',count:51},{hour:'09',count:38},{hour:'10',count:29},{hour:'11',count:24},
        {hour:'12',count:21},{hour:'13',count:19},{hour:'14',count:22},{hour:'15',count:28},
        {hour:'16',count:35},{hour:'17',count:48},{hour:'18',count:52},{hour:'19',count:39},
        {hour:'20',count:18},{hour:'21',count:9},{hour:'22',count:5},{hour:'23',count:3},
      ]
    },
    live: [
      { species: 'Northern Cardinal', scientific_name: 'Cardinalis cardinalis', confidence: 0.94, timestamp: new Date(Date.now()-12000).toISOString().slice(0,-1) },
      { species: 'Carolina Wren', scientific_name: 'Thryothorus ludovicianus', confidence: 0.87, timestamp: new Date(Date.now()-95000).toISOString().slice(0,-1) },
      { species: 'American Robin', scientific_name: 'Turdus migratorius', confidence: 0.82, timestamp: new Date(Date.now()-240000).toISOString().slice(0,-1) },
      { species: 'Tufted Titmouse', scientific_name: 'Baeolophus bicolor', confidence: 0.79, timestamp: new Date(Date.now()-410000).toISOString().slice(0,-1) },
      { species: 'Black-capped Chickadee', scientific_name: 'Poecile atricapillus', confidence: 0.71, timestamp: new Date(Date.now()-720000).toISOString().slice(0,-1) },
      { species: 'Blue Jay', scientific_name: 'Cyanocitta cristata', confidence: 0.66, timestamp: new Date(Date.now()-1100000).toISOString().slice(0,-1) },
      { species: 'Mourning Dove', scientific_name: 'Zenaida macroura', confidence: 0.58, timestamp: new Date(Date.now()-1680000).toISOString().slice(0,-1) },
      { species: 'Song Sparrow', scientific_name: 'Melospiza melodia', confidence: 0.49, timestamp: new Date(Date.now()-2200000).toISOString().slice(0,-1) },
    ],
  };

  // demo flag — when true, use mock data + skip image fetches (showing placeholders)
  const DEMO = false;

  const COLORS = {
    bone:'#f3eed9', sage:'#7fa68a', mint:'#a8d4b6', moss:'#3d6b4a',
    forest:'#14301f', amber:'#d8a64a', rust:'#b8623a',
  };
  const PALETTE = ['#a8d4b6','#7fa68a','#3d6b4a','#d8a64a','#b8623a','#5a8a6e','#bcd4a0','#244a35'];

  function timeAgo(ts) {
    const d = new Date(ts.endsWith('Z') ? ts : ts+'Z');
    const diff = (Date.now() - d.getTime()) / 1000;
    if (diff < 60) return Math.floor(diff) + 's ago';
    if (diff < 3600) return Math.floor(diff/60) + 'm ago';
    if (diff < 86400) return Math.floor(diff/3600) + 'h ago';
    return Math.floor(diff/86400) + 'd ago';
  }

  function fmtTime(ts) {
    const d = new Date(ts.endsWith('Z') ? ts : ts+'Z');
    return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  }

  function confBadge(c) {
    const pct = (c*100).toFixed(0) + '%';
    if (c >= 0.8) return `<span class="badge high">${pct}</span>`;
    if (c >= 0.5) return `<span class="badge med">${pct}</span>`;
    return `<span class="badge low">${pct}</span>`;
  }

  // image cache (in real backend this is the SQLite bird_images table)
  const imgCache = new Map();
  async function getBirdImage(species, scientific) {
    if (imgCache.has(species)) return imgCache.get(species);
    if (DEMO) {
      // demo: never resolve, leave placeholder shimmer
      imgCache.set(species, null);
      return null;
    }
    try {
      const r = await fetch('/api/bird-image?format=json&species=' + encodeURIComponent(species) + (scientific ? '&scientific=' + encodeURIComponent(scientific) : ''));
      if (!r.ok) throw new Error();
      const j = await r.json();
      imgCache.set(species, j.url);
      return j.url;
    } catch {
      imgCache.set(species, null);
      return null;
    }
  }

  let dailyChart, hourlyChart, speciesChart;

  function renderHourly(hc) {
    const labels = hc.map(d => d.hour);
    const data = hc.map(d => d.count);
    const ctx = document.getElementById('hourlyChart');
    if (hourlyChart) hourlyChart.destroy();
    const grad = ctx.getContext('2d').createLinearGradient(0,0,0,240);
    grad.addColorStop(0, 'rgba(168,212,182,0.35)');
    grad.addColorStop(1, 'rgba(168,212,182,0)');
    hourlyChart = new Chart(ctx, {
      type: 'line',
      data: { labels, datasets: [{
        data, borderColor: COLORS.mint, backgroundColor: grad,
        fill: true, tension: 0.4, pointRadius: 0, borderWidth: 2,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: true, ticks: { color: '#7fa68a', font: { family: 'JetBrains Mono', size: 10 } }, grid: { color: 'rgba(243,238,217,0.05)' }, border: { display: false } },
          x: { ticks: { color: '#7fa68a', font: { family: 'JetBrains Mono', size: 10 }, maxRotation: 0, autoSkip: true, maxTicksLimit: 8 }, grid: { display: false }, border: { display: false } },
        },
      },
    });
  }

  function renderDaily(dc) {
    const labels = dc.map(d => {
      const dt = new Date(d.day);
      return dt.toLocaleDateString([], { weekday: 'short' });
    });
    const data = dc.map(d => d.count);
    const ctx = document.getElementById('dailyChart');
    if (dailyChart) dailyChart.destroy();
    dailyChart = new Chart(ctx, {
      type: 'bar',
      data: { labels, datasets: [{
        data, backgroundColor: COLORS.moss, hoverBackgroundColor: COLORS.mint,
        borderRadius: 2, barThickness: 32,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        plugins: { legend: { display: false } },
        scales: {
          y: { beginAtZero: true, ticks: { color: '#7fa68a', font: { family: 'JetBrains Mono', size: 10 } }, grid: { color: 'rgba(243,238,217,0.05)' }, border: { display: false } },
          x: { ticks: { color: '#7fa68a', font: { family: 'JetBrains Mono', size: 11 } }, grid: { display: false }, border: { display: false } },
        },
      },
    });
  }

  function renderDonut(top) {
    const ctx = document.getElementById('speciesChart');
    if (speciesChart) speciesChart.destroy();
    const data = top.slice(0, 8);
    speciesChart = new Chart(ctx, {
      type: 'doughnut',
      data: { labels: data.map(s => s.species), datasets: [{
        data: data.map(s => s.count),
        backgroundColor: PALETTE,
        borderColor: COLORS.forest,
        borderWidth: 2,
      }]},
      options: {
        responsive: true, maintainAspectRatio: false,
        cutout: '62%',
        plugins: {
          legend: { position: 'right', labels: { color: '#d8d2bd', boxWidth: 10, boxHeight: 10, font: { family: 'Inter', size: 11 }, padding: 10 } },
        },
      },
    });
  }

  async function renderTopSpecies(top) {
    const max = top[0]?.count || 1;
    const container = document.getElementById('top-species');
    container.innerHTML = top.slice(0, 8).map((s, i) => `
      <div class="species-row" data-species="${s.species}">
        <div class="species-thumb">
          <div class="ph">◉</div>
        </div>
        <div class="species-info">
          <div class="name">${s.species}</div>
          ${s.scientific_name ? `<div class="latin">${s.scientific_name}</div>` : ''}
          <div class="species-bar-mini"><div class="fill" style="width:${(s.count/max*100)}%"></div></div>
        </div>
        <div class="species-count">
          <div class="n">${s.count}</div>
          <div class="l">obs</div>
        </div>
      </div>
    `).join('');

    // hydrate images (cached)
    for (const s of top.slice(0, 8)) {
      const url = await getBirdImage(s.species, s.scientific_name);
      if (url) {
        const row = container.querySelector(`[data-species="${CSS.escape(s.species)}"] .species-thumb`);
        if (row) row.innerHTML = `<img src="${url}" alt="${s.species}">`;
      }
    }
  }

  async function renderLive(live) {
    const tbody = document.getElementById('live-table');
    document.getElementById('empty-msg').style.display = live.length ? 'none' : 'block';
    tbody.innerHTML = live.map(d => `
      <tr data-species="${d.species}">
        <td>
          <div class="feed-species">
            <div class="feed-thumb"><div class="ph"></div></div>
            <div class="feed-name">
              ${d.species}
              ${d.scientific_name ? `<span class="l">${d.scientific_name}</span>` : ''}
            </div>
          </div>
        </td>
        <td><span class="feed-time">${fmtTime(d.timestamp)} · ${timeAgo(d.timestamp)}</span></td>
        <td>${confBadge(d.confidence)}</td>
      </tr>
    `).join('');

    // hydrate thumbs
    for (const d of live) {
      const url = await getBirdImage(d.species, d.scientific_name);
      if (url) {
        const cell = tbody.querySelector(`[data-species="${CSS.escape(d.species)}"] .feed-thumb`);
        if (cell) cell.innerHTML = `<img src="${url}" alt="${d.species}">`;
      }
    }
  }

  async function renderHero(latest) {
    if (!latest) return;
    document.getElementById('latest-species').textContent = latest.species;
    document.getElementById('latest-latin').textContent = latest.scientific_name || '—';
    document.getElementById('latest-time').textContent = timeAgo(latest.timestamp);
    document.getElementById('latest-conf').innerHTML = (latest.confidence*100).toFixed(0) + '<small>%</small>';
    document.getElementById('latest-recorded').innerHTML = fmtTime(latest.timestamp) + ' <small>local</small>';
    document.getElementById('hero-conf').textContent = (latest.confidence*100).toFixed(0) + '%';

    const url = await getBirdImage(latest.species, latest.scientific_name);
    if (url) {
      const img = document.getElementById('hero-img');
      img.src = url;
      img.style.display = 'block';
      document.getElementById('hero-img-ph').style.display = 'none';
    }
  }

  function renderPiStatus(live) {
    const piEl = document.getElementById('stat-pi');
    const piSub = document.getElementById('stat-pi-sub');
    if (live.length) {
      piEl.textContent = 'Online';
      piEl.style.color = COLORS.mint;
      piSub.textContent = 'Last seen ' + timeAgo(live[0].timestamp);
    } else {
      piEl.textContent = 'Idle';
      piEl.style.color = COLORS.amber;
      piSub.textContent = 'No recent transmissions';
    }
  }

  function tickClock() {
    const d = new Date();
    document.getElementById('clock').textContent =
      d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' }) +
      ' · ' + d.toLocaleDateString([], { month: 'short', day: 'numeric' });
  }

  async function refresh() {
    let stats, live;
    if (DEMO) { stats = MOCK.stats; live = MOCK.live; }
    else {
      [stats, live] = await Promise.all([
        fetch('/api/stats').then(r => r.json()),
        fetch('/api/live').then(r => r.json()),
      ]);
    }

    document.getElementById('stat-total').textContent = stats.total_detections.toLocaleString();
    document.getElementById('stat-species').textContent = stats.unique_species;

    // peak hour
    if (stats.hourly_counts && stats.hourly_counts.length) {
      const peak = stats.hourly_counts.reduce((a,b) => a.count > b.count ? a : b);
      document.getElementById('stat-peak').innerHTML = peak.hour + '<small style="font-size:18px;color:var(--bone-dim);">:00</small>';
    }

    renderPiStatus(live);
    renderDaily(stats.daily_counts || []);
    renderHourly(stats.hourly_counts || []);
    renderDonut(stats.top_species || []);
    await renderTopSpecies(stats.top_species || []);
    await renderLive(live);
    await renderHero(live[0]);
  }

  tickClock();
  setInterval(tickClock, 1000);
  refresh();
  setInterval(refresh, 30000);
</script>
</body>
</html>

"""

init_db()

if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port, debug=False)
