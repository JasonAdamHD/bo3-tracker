"""BO3 Workshop map tracker: a small local website backed by SQLite."""
import csv
import html
import io
import re
import json
import os
import sqlite3
import threading
import time
from datetime import date, datetime, timezone
from pathlib import Path

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

BASE = Path(__file__).parent
# In Docker, DATA_DIR points at a mounted folder so the list survives updates.
DATA_DIR = Path(os.environ.get("DATA_DIR", BASE))
DATA_DIR.mkdir(parents=True, exist_ok=True)
DB_PATH = DATA_DIR / "maps.db"
CONFIG_PATH = DATA_DIR / "config.json"

APP_ID = 311210          # Call of Duty: Black Ops III
TAG = "Map"              # same as requiredtags[]=Map in the workshop URL
PAGE_SIZE = 100          # max items Steam returns per API call
PAGE_DELAY = 1.0         # seconds between API calls, to stay polite
BACKFILL_CALLS = 10      # detail calls per "Fetch new" for older rows (1000 maps)
QUERY_URL = "https://api.steampowered.com/IPublishedFileService/QueryFiles/v1/"
DETAILS_URL = "https://api.steampowered.com/IPublishedFileService/GetDetails/v1/"
PLAYERS_URL = "https://api.steampowered.com/ISteamUser/GetPlayerSummaries/v2/"
LINK = "https://steamcommunity.com/sharedfiles/filedetails/?id={}"
PROFILE = "https://steamcommunity.com/profiles/{}"

app = Flask(__name__, static_folder=None)


# ---------------------------------------------------------------- storage

def api_key():
    key = os.environ.get("STEAM_API_KEY", "").strip()
    if not key and CONFIG_PATH.exists():
        key = json.loads(CONFIG_PATH.read_text()).get("steam_api_key", "").strip()
    return key


def db():
    con = sqlite3.connect(DB_PATH, timeout=30)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    with db() as con:
        con.execute("""
            CREATE TABLE IF NOT EXISTS maps (
                id             TEXT PRIMARY KEY,   -- Steam publishedfileid
                name           TEXT NOT NULL,
                date_uploaded  TEXT NOT NULL,      -- YYYY-MM-DD
                uploaded_ts    INTEGER NOT NULL,   -- unix time, for sorting
                completed      INTEGER NOT NULL DEFAULT 0,
                date_completed TEXT,
                added_ts       INTEGER NOT NULL
            )""")
        con.execute("CREATE TABLE IF NOT EXISTS creators (steamid TEXT PRIMARY KEY, name TEXT NOT NULL)")
        con.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT)")
        # Columns added later; existing databases get them on startup.
        cols = {r["name"] for r in con.execute("PRAGMA table_info(maps)")}
        for col, kind in (("preview_url", "TEXT"), ("creator_id", "TEXT"),
                          ("has_ee", "TEXT"),          # NULL = unknown, 'yes', 'no'
                          ("notes", "TEXT"),           # rich text (sanitized HTML)
                          ("my_rating", "INTEGER"),    # 1-5, NULL = unrated
                          ("steam_score", "REAL"),     # Steam's 0-1 rating score
                          ("votes_up", "INTEGER"), ("votes_down", "INTEGER"),
                          ("file_size", "INTEGER")):   # download size in bytes
            if col not in cols:
                con.execute(f"ALTER TABLE maps ADD COLUMN {col} {kind}")


def get_setting(key):
    with db() as con:
        r = con.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return r["value"] if r else None


def set_setting(key, value):
    with db() as con:
        if value is None:
            con.execute("DELETE FROM settings WHERE key = ?", (key,))
        else:
            con.execute("INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value))


def known_ids():
    with db() as con:
        return {r["id"] for r in con.execute("SELECT id FROM maps")}


def votes_of(it):
    v = it.get("vote_data") or {}
    if not v:
        return None
    return (v.get("score"), v.get("votes_up"), v.get("votes_down"))


def size_of(it):
    try:
        return int(it.get("file_size"))
    except (TypeError, ValueError):
        return None


def save_items(items, known):
    """Insert maps we don't have yet; refresh ratings of ones we do. Returns # new."""
    rows, rating_updates = [], []
    for it in items:
        fid = str(it.get("publishedfileid"))
        votes = votes_of(it)
        if fid in known:
            if votes or size_of(it) is not None:
                rating_updates.append((*(votes or (None, None, None)), size_of(it), fid))
            continue
        ts = int(it.get("time_created") or 0)
        rows.append((
            fid,
            (it.get("title") or f"(untitled {fid})").strip(),
            datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d"),
            ts,
            int(time.time()),
            it.get("preview_url") or None,
            str(it["creator"]) if it.get("creator") else None,
            *(votes or (None, None, None)),
            size_of(it),
        ))
        known.add(fid)
    with db() as con:
        con.executemany(
            "INSERT OR IGNORE INTO maps (id, name, date_uploaded, uploaded_ts, added_ts,"
            " preview_url, creator_id, steam_score, votes_up, votes_down, file_size)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
        con.executemany(
            "UPDATE maps SET steam_score = COALESCE(?, steam_score),"
            " votes_up = COALESCE(?, votes_up), votes_down = COALESCE(?, votes_down),"
            " file_size = COALESCE(?, file_size) WHERE id = ?",
            rating_updates)
    return len(rows)


# ---------------------------------------------------------------- Steam calls

def steam_get(url, params=None, data=None):
    if data is not None:
        r = requests.post(url, data=data, timeout=30)
    else:
        r = requests.get(url, params=params, timeout=30)
    if r.status_code == 429:
        raise RuntimeError("Steam says too many requests. Stopped; try again later.")
    if r.status_code == 403:
        raise RuntimeError("Steam rejected the API key (403). Check your key.")
    r.raise_for_status()
    return r.json()


def fetch_page(key, cursor):
    """One QueryFiles call, newest uploads first. Returns (items, next_cursor, total)."""
    resp = steam_get(QUERY_URL, params={
        "key": key,
        "appid": APP_ID,
        "query_type": 1,              # ranked by publication date (most recent)
        "filetype": 0,                # ready-to-use items
        "numperpage": PAGE_SIZE,
        "cursor": cursor,
        "requiredtags[0]": TAG,
        "return_metadata": "true",
        "return_vote_data": "true",
    }).get("response", {})
    return resp.get("publishedfiledetails", []) or [], resp.get("next_cursor"), resp.get("total")


def fetch_details(key, ids):
    """Picture, creator and rating for up to 100 existing items."""
    params = {"key": key, "includevotes": "true"}
    for i, fid in enumerate(ids):
        params[f"publishedfileids[{i}]"] = fid
    resp = steam_get(DETAILS_URL, params=params).get("response", {})
    return resp.get("publishedfiledetails", []) or []


def fetch_player_names(key, steamids):
    """Steam display names for up to 100 accounts. Returns {steamid: name}."""
    resp = steam_get(PLAYERS_URL, params={"key": key, "steamids": ",".join(steamids)})
    return {p["steamid"]: p.get("personaname", "")
            for p in resp.get("response", {}).get("players", [])}


# ---------------------------------------------------------------- background jobs

class Stopped(Exception):
    pass


class Job:
    """One background fetch. Only one runs at a time."""
    def __init__(self, mode):
        self.mode = mode              # "new", "all" or "details"
        self.running = True
        self.stop_requested = False
        self.checked = 0
        self.added = 0
        self.updated = 0
        self.calls = 0
        self.total = None             # total maps on the workshop, if Steam says
        self.stage = "Starting"
        self.error = None

    def pace(self):
        """Wait between Steam calls; also the point where Stop takes effect."""
        if self.stop_requested:
            raise Stopped()
        if self.calls:
            time.sleep(PAGE_DELAY)
        if self.stop_requested:
            raise Stopped()
        self.calls += 1

    def status(self):
        return {k: getattr(self, k) for k in (
            "mode", "running", "stop_requested", "checked", "added", "updated",
            "calls", "total", "stage", "error")}


JOB = None
JOB_LOCK = threading.Lock()


def fetch_new(job, key, limit):
    """Newest maps until we reach one we already have (or `limit` checked)."""
    known, cursor = known_ids(), "*"
    while job.checked < limit and cursor:
        job.pace()
        items, next_cursor, job.total = fetch_page(key, cursor)
        if not items or next_cursor == cursor:
            break
        cursor = next_cursor
        fresh = []
        for it in items[:limit - job.checked]:
            job.checked += 1
            if str(it.get("publishedfileid")) in known:
                cursor = None          # caught up
                break
            fresh.append(it)
        job.added += save_items(fresh, known)


def fetch_all(job, key):
    """Walk the whole workshop. Saves its place so Stop / restarts resume."""
    known = known_ids()
    cursor = get_setting("import_cursor") or "*"
    job.checked = int(get_setting("import_checked") or 0)
    while True:
        job.pace()
        try:
            items, next_cursor, total = fetch_page(key, cursor)
        except requests.HTTPError:
            if cursor == "*":
                raise
            # A saved cursor can expire; start over (known maps are skipped).
            cursor, job.checked = "*", 0
            continue
        job.total = total
        job.checked += len(items)
        job.added += save_items(items, known)
        if not items or not next_cursor or next_cursor == cursor:
            set_setting("import_cursor", None)
            set_setting("import_checked", None)
            set_setting("import_done", str(int(time.time())))
            return
        cursor = next_cursor
        set_setting("import_cursor", cursor)
        set_setting("import_checked", str(job.checked))
        resolve_creators(job, key)       # names show up as the import goes


def apply_details(job, key, ids):
    """Ask Steam about `ids` and store picture, creator and rating."""
    job.pace()
    found = {str(d.get("publishedfileid")): d for d in fetch_details(key, ids)}
    with db() as con:
        for fid in ids:
            d = found.get(fid, {})
            votes = votes_of(d)
            # '' marks "Steam has nothing", so we don't ask again.
            con.execute(
                "UPDATE maps SET preview_url = ?, creator_id = ?,"
                " steam_score = COALESCE(?, steam_score), votes_up = COALESCE(?, votes_up),"
                " votes_down = COALESCE(?, votes_down),"
                " file_size = COALESCE(?, file_size, 0) WHERE id = ?",
                (d.get("preview_url") or "", str(d.get("creator") or ""),
                 *(votes or (None, None, None)), size_of(d), fid))
    job.updated += len(ids)


def backfill_details(job, key, max_calls=None):
    """Fill picture/creator/rating for maps that don't have them yet."""
    done = 0
    while max_calls is None or done < max_calls:
        with db() as con:
            ids = [r["id"] for r in con.execute(
                "SELECT id FROM maps WHERE preview_url IS NULL OR creator_id IS NULL"
                " OR file_size IS NULL"
                " ORDER BY uploaded_ts DESC LIMIT 100")]
        if not ids:
            return
        job.stage = "Filling in pictures, creators, ratings and sizes"
        apply_details(job, key, ids)
        done += 1


def refresh_all_details(job, key):
    """Re-download picture, creator and Steam rating for every map."""
    with db() as con:
        ids = [r["id"] for r in con.execute("SELECT id FROM maps ORDER BY uploaded_ts DESC")]
    job.total = len(ids)
    job.stage = "Refreshing pictures, creators, ratings and sizes"
    for i in range(0, len(ids), 100):
        apply_details(job, key, ids[i:i + 100])
        job.checked = min(i + 100, len(ids))


def resolve_creators(job, key):
    """Look up display names for creators we haven't seen yet."""
    with db() as con:
        ids = [r["creator_id"] for r in con.execute(
            "SELECT DISTINCT creator_id FROM maps WHERE creator_id IS NOT NULL"
            " AND creator_id != '' AND creator_id NOT IN (SELECT steamid FROM creators)")]
    for i in range(0, len(ids), 100):
        batch = ids[i:i + 100]
        job.pace()
        names = fetch_player_names(key, batch)
        with db() as con:
            con.executemany("INSERT OR REPLACE INTO creators (steamid, name) VALUES (?, ?)",
                            [(sid, names.get(sid) or sid) for sid in batch])


def run_job(job, key, limit):
    try:
        if job.mode == "all":
            job.stage = "Importing all maps"
            fetch_all(job, key)
            backfill_details(job, key)                 # no cap: run until done
        elif job.mode == "details":
            refresh_all_details(job, key)
        else:
            job.stage = "Checking for new maps"
            fetch_new(job, key, limit)
            backfill_details(job, key, BACKFILL_CALLS)
        job.stage = "Looking up creator names"
        resolve_creators(job, key)
        job.stage = "Done"
    except Stopped:
        job.stage = "Stopped. Get all maps will resume where it left off." if job.mode == "all" else "Stopped"
    except Exception as e:
        job.error = str(e)
        job.stage = "Error"
    finally:
        job.running = False


def start_job(mode, limit=100):
    global JOB
    key = api_key()
    if not key:
        raise RuntimeError("No Steam API key. Set STEAM_API_KEY (see README).")
    with JOB_LOCK:
        if JOB and JOB.running:
            raise RuntimeError("A fetch is already running.")
        JOB = Job(mode)
        threading.Thread(target=run_job, args=(JOB, key, limit), daemon=True).start()
        return JOB


# ---------------------------------------------------------------- web routes

MAP_QUERY = """
    SELECT m.*, c.name AS creator_name FROM maps m
    LEFT JOIN creators c ON c.steamid = m.creator_id
"""


def row_out(r):
    cid = r["creator_id"] or None
    return {
        "id": r["id"],
        "name": r["name"],
        "link": LINK.format(r["id"]),
        "preview_url": r["preview_url"] or None,
        "creator": r["creator_name"] or None,
        "creator_link": PROFILE.format(cid) if cid else None,
        "date_uploaded": r["date_uploaded"],
        "completed": bool(r["completed"]),
        "date_completed": r["date_completed"],
        "has_ee": r["has_ee"],
        "notes": r["notes"] or "",
        "my_rating": r["my_rating"],
        "steam_score": r["steam_score"],
        "votes": (r["votes_up"] or 0) + (r["votes_down"] or 0),
        "votes_up": r["votes_up"],
        "file_size": r["file_size"] or None,
    }


@app.get("/")
def index():
    return send_from_directory(BASE / "static", "index.html")


@app.get("/api/maps")
def list_maps():
    with db() as con:
        rows = con.execute(MAP_QUERY + " ORDER BY m.uploaded_ts DESC").fetchall()
    return jsonify({"maps": [row_out(r) for r in rows], "has_key": bool(api_key())})


@app.post("/api/jobs/start")
def api_job_start():
    body = request.get_json(silent=True) or {}
    mode = body.get("mode") if body.get("mode") in ("new", "all", "details") else "new"
    limit = max(1, min(int(body.get("limit", 100)), 50000))
    try:
        return jsonify(start_job(mode, limit).status())
    except Exception as e:
        return jsonify({"error": str(e)}), 400


@app.post("/api/jobs/stop")
def api_job_stop():
    if JOB and JOB.running:
        JOB.stop_requested = True
    return jsonify(JOB.status() if JOB else {"running": False})


@app.get("/api/jobs/status")
def api_job_status():
    s = JOB.status() if JOB else {"running": False}
    s["import_paused"] = get_setting("import_cursor") is not None
    s["import_done"] = get_setting("import_done") is not None
    return jsonify(s)


@app.patch("/api/maps/<fid>")
def update_map(fid):
    body = request.get_json(silent=True) or {}
    with db() as con:
        r = con.execute("SELECT * FROM maps WHERE id = ?", (fid,)).fetchone()
        if not r:
            return jsonify({"error": "not found"}), 404
        completed = bool(body.get("completed", r["completed"]))
        done_date = body.get("date_completed", r["date_completed"])
        if completed and not done_date:
            done_date = date.today().isoformat()
        if not completed:
            done_date = None
        has_ee = body.get("has_ee", r["has_ee"])
        if has_ee not in ("yes", "no"):
            has_ee = None
        my_rating = body.get("my_rating", r["my_rating"])
        my_rating = int(my_rating) if my_rating in (1, 2, 3, 4, 5, "1", "2", "3", "4", "5") else None
        notes = body.get("notes", r["notes"]) or None
        if notes and len(notes) > 500_000:
            return jsonify({"error": "Notes are too long"}), 400
        con.execute(
            "UPDATE maps SET completed = ?, date_completed = ?, has_ee = ?, my_rating = ?,"
            " notes = ? WHERE id = ?",
            (int(completed), done_date, has_ee, my_rating, notes, fid))
        r = con.execute(MAP_QUERY + " WHERE m.id = ?", (fid,)).fetchone()
    return jsonify(row_out(r))


def html_to_text(s):
    """Notes are stored as HTML; the CSV gets plain text."""
    if not s:
        return ""
    s = re.sub(r"(?i)<br\s*/?>|</(p|div|li|h\d)>", "\n", s)
    s = re.sub(r"(?i)<li[^>]*>", "- ", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n{3,}", "\n\n", s).strip()


@app.get("/export.csv")
def export_csv():
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["MapName", "SteamWorkshopLink", "DateUploaded", "Completed",
                "DateCompleted", "Creator", "HasEasterEgg", "SteamStars", "SteamVotes",
                "MyRating", "SizeMB", "Notes"])
    with db() as con:
        for r in con.execute(MAP_QUERY + " ORDER BY m.uploaded_ts DESC"):
            score = r["steam_score"]
            w.writerow([r["name"], LINK.format(r["id"]), r["date_uploaded"],
                        "Yes" if r["completed"] else "No", r["date_completed"] or "",
                        r["creator_name"] or "",
                        {"yes": "Yes", "no": "No"}.get(r["has_ee"], ""),
                        f"{score * 5:.1f}" if score is not None else "",
                        (r["votes_up"] or 0) + (r["votes_down"] or 0),
                        r["my_rating"] or "",
                        f"{r['file_size'] / 1e6:.1f}" if r["file_size"] else "",
                        html_to_text(r["notes"])])
    return Response(buf.getvalue(), mimetype="text/csv",
                    headers={"Content-Disposition": "attachment; filename=bo3_maps.csv"})


init_db()

if __name__ == "__main__":
    print("BO3 map tracker running at http://localhost:5000")
    app.run(host="127.0.0.1", port=5000)
