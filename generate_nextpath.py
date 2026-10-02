#!/usr/bin/env python3
"""
NextPath Africa learner progress dashboard generator
=====================================================
Fetches enrolled students from two Moodle courses (self-paced + cohort),
pulls their Raven360 activity, and renders two self-contained HTML dashboards
committed to GitHub Pages for Moodle iframe embedding.

Run:  python generate_nextpath.py
Env:  NEXTPATH_SELF_MOODLE_COURSE_ID, NEXTPATH_COHORT_MOODLE_COURSE_ID,
      MOODLE_TOKEN, MOODLE_URL (optional, defaults to edusignis),
      RAVEN360_CLIENT_ID, RAVEN360_CLIENT_SECRET, RAVEN360_XAPI_KEY
"""

import os
import html as html_lib
import base64
from collections import defaultdict
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path

import requests

BASE_DIR = Path(__file__).parent

# ── Load .env (local dev; GitHub Actions injects secrets directly) ────────
_env = BASE_DIR / ".env"
if not _env.exists():
    _env = BASE_DIR.parent / ".env"
if _env.exists():
    for _line in _env.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

RAVEN360_BASE_URL    = "https://api.raven360.com"
RAVEN360_CLIENT_ID   = os.environ["RAVEN360_CLIENT_ID"]
RAVEN360_CLIENT_SECRET = os.environ["RAVEN360_CLIENT_SECRET"]
RAVEN360_XAPI_KEY    = os.environ["RAVEN360_XAPI_KEY"]

MOODLE_URL   = os.environ.get("MOODLE_URL", "https://edusignis.praesignis.com")
MOODLE_TOKEN = os.environ["MOODLE_TOKEN"]

SELF_COURSE_ID   = int(os.environ["NEXTPATH_SELF_MOODLE_COURSE_ID"])
COHORT_COURSE_ID = int(os.environ["NEXTPATH_COHORT_MOODLE_COURSE_ID"])

# Matches config.json's trust.completion_threshold_pct in the main server (not
# loaded here since this script runs standalone in CI) -- a Raven360 100%
# only counts as a genuine completion once the learner's own time on that
# course reaches this fraction of its authored duration, so a two-minute
# click-through doesn't read the same as a finished course.
COMPLETION_THRESHOLD_PCT = 50

RAVEN360_SOURCE_TZ = ZoneInfo("America/New_York")
DISPLAY_TZ         = ZoneInfo("Africa/Johannesburg")


# ── Helpers ───────────────────────────────────────────────────────────────

def _convert_tz(s):
    if not s:
        return s
    try:
        dt = datetime.fromisoformat(s).replace(tzinfo=RAVEN360_SOURCE_TZ)
        return dt.astimezone(DISPLAY_TZ).replace(tzinfo=None).isoformat()
    except ValueError:
        return s


def _parse_dt(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def esc(s):
    return html_lib.escape(str(s or ""), quote=True)


# ── Raven360 ──────────────────────────────────────────────────────────────

def get_raven360_token():
    r = requests.post(
        f"{RAVEN360_BASE_URL}/gettoken",
        json={"client_id": RAVEN360_CLIENT_ID, "client_secret": RAVEN360_CLIENT_SECRET},
        headers={"x-api-key": RAVEN360_XAPI_KEY, "Accept": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["data"]["token"]


def fetch_all_progress(token):
    """All progress records (LO + paths + channels) from 2026 onwards."""
    from datetime import date as _date
    hdrs = {"Authorization": token, "x-api-key": RAVEN360_XAPI_KEY, "Accept": "application/json"}
    body = {"from_date": "01-01-2026", "to_date": _date.today().strftime("%m-%d-%Y")}
    results = []
    for path, id_field in [
        ("/administration/progress/learningobjects", "learningobject_id"),
        ("/administration/progress/learningpaths",   "learningpath_id"),
        ("/administration/progress/channels",        "channel_id"),
    ]:
        r = requests.post(f"{RAVEN360_BASE_URL}{path}", headers=hdrs,
                          json=body, timeout=120)
        if r.status_code == 500:
            try:
                if r.json().get("error", {}).get("message") == "No Results Found":
                    continue
            except Exception:
                pass
        r.raise_for_status()
        data = r.json()
        records = data.get("data", data) if isinstance(data, dict) else data
        if not isinstance(records, list):
            continue
        for rec in records:
            for f in ("first_access_date", "last_access_date", "completed_date"):
                if rec.get(f):
                    rec[f] = _convert_tz(rec[f])
            rec["_id_field"] = id_field
        results.extend(records)
    return results


_CATALOG_SOURCES = [
    ("/administration/catalog/learningobjects", "learningobject_id", {"learningobject_type": "Content"}),
    ("/administration/catalog/channels", "channel_id", {}),
    ("/administration/catalog/learningpaths", "learningpath_id", {}),
]


def parse_duration(s):
    """Catalog 'duration' is authored HH:MM:SS. Returns minutes, or None for
    missing/zero/malformed -- None means "unknown", never treated as zero."""
    if not s:
        return None
    try:
        h, m, sec = (int(p) for p in str(s).split(":"))
    except (ValueError, TypeError):
        return None
    total = h * 60 + m + sec / 60
    return total if total > 0 else None


def get_catalog_durations(token):
    """(id_field, course_id) -> duration minutes, across all 3 catalog endpoints."""
    hdrs = {"Authorization": token, "x-api-key": RAVEN360_XAPI_KEY, "Accept": "application/json"}
    durations = {}
    for path, id_field, extra in _CATALOG_SOURCES:
        try:
            r = requests.post(f"{RAVEN360_BASE_URL}{path}", headers=hdrs,
                               json={**extra, "from_date": "01-01-2015", "to_date": "12-31-2030"},
                               timeout=120)
            r.raise_for_status()
            for item in r.json().get("data", []):
                course_id = item.get(id_field)
                if course_id is not None:
                    durations[(id_field, course_id)] = parse_duration(item.get("duration"))
        except Exception:
            continue
    return durations


def get_enrolled_students(course_id):
    r = requests.post(
        f"{MOODLE_URL}/webservice/rest/server.php",
        params={"wstoken": MOODLE_TOKEN,
                "wsfunction": "core_enrol_get_enrolled_users",
                "moodlewsrestformat": "json",
                "courseid": course_id},
        timeout=30,
    )
    r.raise_for_status()
    data = r.json()
    if isinstance(data, dict) and data.get("exception"):
        raise RuntimeError(f"Moodle: {data.get('errorcode')} — {data.get('message')}")
    return [
        {"email": (u.get("email") or "").lower(),
         "fullname": u.get("fullname"),
         "lastaccess": u.get("lastaccess")}
        for u in data if u.get("email")
    ]


# ── Aggregation ───────────────────────────────────────────────────────────

def build_student_stats(roster, all_records, durations):
    """For each enrolled student, aggregate their Raven360 activity."""
    by_email = defaultdict(list)
    for rec in all_records:
        email = (rec.get("email_id") or "").lower()
        if email:
            by_email[email].append(rec)

    now = datetime.now(tz=DISPLAY_TZ).replace(tzinfo=None)
    students = []

    for person in roster:
        email = person["email"]
        records = by_email.get(email, [])

        # A Raven360-reported 100% only counts as "completed" once the
        # learner's own time on that course reaches COMPLETION_THRESHOLD_PCT
        # of its authored duration, otherwise a two-minute click-through
        # would display identically to a genuinely finished course. Duration-
        # unknown courses can't be judged, so they pass by default.
        completed = []
        for r in records:
            if r.get("completion_percentage") != 100:
                continue
            dur = durations.get((r.get("_id_field"), r.get(r.get("_id_field"))))
            fa = _parse_dt(r.get("first_access_date"))
            la = _parse_dt(r.get("last_access_date"))
            spent = (la - fa).total_seconds() / 60 if fa and la and la > fa else 0
            if dur is None or spent >= (COMPLETION_THRESHOLD_PCT / 100) * dur:
                completed.append(r)

        accessed  = [r for r in records if r.get("first_access_date")]

        # Time spent: sum (last_access - first_access) per course, cap at 8h each
        time_spent_min = 0
        for rec in records:
            fa = _parse_dt(rec.get("first_access_date"))
            la = _parse_dt(rec.get("last_access_date"))
            if fa and la and la > fa:
                raw = (la - fa).total_seconds() / 60
                time_spent_min += min(raw, 480)

        # Last active from Raven360
        last_dates = [_parse_dt(r.get("last_access_date")) for r in records]
        last_dates = [d for d in last_dates if d]
        last_active_r360 = max(last_dates) if last_dates else None

        # Last login from Moodle (epoch)
        moodle_last = person.get("lastaccess")
        last_moodle_str = None
        if moodle_last:
            try:
                last_moodle_str = datetime.fromtimestamp(int(moodle_last),
                                  tz=DISPLAY_TZ).replace(tzinfo=None).isoformat()
            except Exception:
                pass

        # Pick the later of the two
        candidates = [d for d in [last_active_r360,
                                   _parse_dt(last_moodle_str)] if d]
        last_active = max(candidates) if candidates else None

        days_ago = (now - last_active).days if last_active else None
        if last_active is None:
            status = "not-started"
        elif days_ago is not None and days_ago <= 7:
            status = "active"
        elif days_ago is not None and days_ago <= 30:
            status = "idle"
        else:
            status = "dormant"

        students.append({
            "name":           person["fullname"] or email,
            "email":          email,
            "accessed":       len(accessed),
            "completed":      len(completed),
            "timeSpentMin":   round(time_spent_min),
            "lastActive":     last_active.isoformat() if last_active else None,
            "daysAgo":        days_ago,
            "status":         status,
        })

    students.sort(key=lambda s: (
        0 if s["status"] == "active" else
        1 if s["status"] == "idle" else
        2 if s["status"] == "dormant" else 3,
        -(s["completed"] or 0),
        (s["name"] or "").lower(),
    ))
    return students


# ── HTML renderer ─────────────────────────────────────────────────────────

LOGO_PATH = BASE_DIR.parent / "clients" / "nextpath-africa" / "image-removebg-preview.png"


def logo_b64():
    if LOGO_PATH.exists():
        return base64.b64encode(LOGO_PATH.read_bytes()).decode()
    return ""


def fmt_mins(m):
    if not m:
        return "—"
    h, mins = divmod(int(m), 60)
    return f"{h}h {mins}m" if h else f"{mins}m"


def fmt_date(iso):
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%d %b %Y")
    except Exception:
        return iso[:10]


def render_dashboard(title, subtitle, students, generated_at):
    logo = logo_b64()
    logo_tag = (f'<img src="data:image/png;base64,{logo}" alt="NextPath Africa" '
                f'style="height:52px;width:auto;">')

    active_count  = sum(1 for s in students if s["status"] == "active")
    started_count = sum(1 for s in students if s["status"] != "not-started")
    total         = len(students)

    rows = []
    for s in students:
        status_label = {
            "active":      '<span class="badge green">Active</span>',
            "idle":        '<span class="badge amber">Idle</span>',
            "dormant":     '<span class="badge grey">Dormant</span>',
            "not-started": '<span class="badge red">Not started</span>',
        }.get(s["status"], "")

        days_txt = (f'{s["daysAgo"]}d ago' if s["daysAgo"] is not None
                    else "never")

        rows.append(f"""
    <tr>
      <td><div class="name">{esc(s['name'])}</div>
          <div class="email">{esc(s['email'])}</div></td>
      <td class="c">{status_label}</td>
      <td class="c">{fmt_date(s['lastActive'])}<div class="sub">{days_txt}</div></td>
      <td class="c num">{s['accessed']}</td>
      <td class="c num">{s['completed']}</td>
      <td class="c num">{fmt_mins(s['timeSpentMin'])}</td>
    </tr>""")

    rows_html = "".join(rows) or '<tr><td colspan="6" class="empty">No students enrolled yet.</td></tr>'
    gen_str = datetime.fromisoformat(generated_at).strftime("%d %b %Y %H:%M SAST")

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{esc(title)}</title>
<style>
  :root {{
    --navy:#1b3e72; --orange:#f5a624; --bg:#f5f3ee;
    --panel:#fff; --border:#d9d4c8; --text:#1a2a40;
    --muted:#6b7a99; --good:#16a34a; --warn:#d97706;
    --bad:#dc2626; --grey:#64748b;
  }}
  * {{ box-sizing:border-box; margin:0; padding:0; }}
  body {{ font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Arial,sans-serif;
         background:var(--bg); color:var(--text); }}
  .header {{ background:var(--bg); border-bottom:3px solid var(--orange);
             padding:12px 28px; display:flex; align-items:center;
             justify-content:space-between; gap:16px; }}
  .header-right {{ text-align:right; font-size:0.75rem; color:var(--muted); line-height:1.5; }}
  .header-right strong {{ color:var(--navy); display:block; font-size:0.85rem; }}
  .body {{ padding:24px 28px; max-width:1100px; margin:0 auto; }}
  h2 {{ font-size:1.15rem; color:var(--navy); margin-bottom:4px; }}
  .sub-title {{ font-size:0.82rem; color:var(--muted); margin-bottom:20px; }}
  .pills {{ display:flex; gap:10px; flex-wrap:wrap; margin-bottom:20px; }}
  .pill {{ background:var(--panel); border:1px solid var(--border); border-radius:999px;
           padding:5px 14px; font-size:0.78rem; color:var(--muted); }}
  .pill b {{ color:var(--navy); }}
  .pill.hi b {{ color:var(--good); }}
  .wrap {{ border:1px solid var(--border); border-radius:10px; overflow:auto;
           background:var(--panel); }}
  table {{ width:100%; border-collapse:collapse; font-size:0.83rem; white-space:nowrap; }}
  th,td {{ padding:9px 12px; border-bottom:1px solid var(--border); text-align:left; }}
  th {{ background:#f0ede6; color:var(--muted); font-size:0.68rem; text-transform:uppercase;
        letter-spacing:0.04em; font-weight:600; position:sticky; top:0; }}
  tbody tr:hover {{ background:#faf8f4; }}
  td.c {{ text-align:center; }}
  td.num {{ font-variant-numeric:tabular-nums; }}
  .name {{ font-weight:700; color:var(--navy); }}
  .email {{ font-size:0.72rem; color:var(--muted); }}
  .sub  {{ font-size:0.68rem; color:var(--muted); }}
  .badge {{ display:inline-block; padding:2px 9px; border-radius:999px;
            font-size:0.72rem; font-weight:700; }}
  .badge.green {{ background:#dcfce7; color:var(--good); }}
  .badge.amber {{ background:#fef3c7; color:var(--warn); }}
  .badge.grey  {{ background:#f1f5f9; color:var(--grey); }}
  .badge.red   {{ background:#fee2e2; color:var(--bad); }}
  .empty {{ text-align:center; padding:40px; color:var(--muted); }}
  .toolbar {{ display:flex; gap:10px; margin-bottom:14px; }}
  input[type=text] {{ flex:1; min-width:200px; background:var(--panel);
    border:1px solid var(--border); color:var(--text);
    padding:8px 12px; border-radius:8px; font-size:0.85rem; }}
  input[type=text]:focus {{ outline:none; border-color:var(--orange); }}
</style>
</head>
<body>
<div class="header">
  {logo_tag}
  <div class="header-right">
    <strong>{esc(title)}</strong>
    Updated {esc(gen_str)}
  </div>
</div>

<div class="body">
  <h2>{esc(subtitle)}</h2>
  <p class="sub-title">Internal view — actual names and emails visible to facilitators only.</p>

  <div class="pills">
    <div class="pill"><b>{total}</b> enrolled</div>
    <div class="pill"><b>{started_count}</b> started</div>
    <div class="pill hi"><b>{active_count}</b> active this week</div>
  </div>

  <div class="toolbar">
    <input type="text" id="search" placeholder="Search by name or email…" oninput="filter()">
  </div>

  <div class="wrap">
    <table id="tbl">
      <thead>
        <tr>
          <th>Student</th>
          <th>Status</th>
          <th>Last Active</th>
          <th style="text-align:center">Accessed</th>
          <th style="text-align:center">Completed</th>
          <th style="text-align:center">Time Spent</th>
        </tr>
      </thead>
      <tbody id="tbody">
        {rows_html}
      </tbody>
    </table>
  </div>
</div>

<script>
  const ROWS = Array.from(document.querySelectorAll('#tbody tr'));
  function filter() {{
    const q = document.getElementById('search').value.trim().toLowerCase();
    ROWS.forEach(r => {{
      r.style.display = (!q || r.textContent.toLowerCase().includes(q)) ? '' : 'none';
    }});
  }}
</script>
</body>
</html>
"""


# ── Main ──────────────────────────────────────────────────────────────────

def main():
    print("Fetching Raven360 token…")
    token = get_raven360_token()

    print("Fetching all Raven360 progress records…")
    all_records = fetch_all_progress(token)
    print(f"  {len(all_records)} records fetched.")

    print("Fetching catalog durations…")
    durations = get_catalog_durations(token)
    print(f"  {len(durations)} durations resolved.")

    generated_at = datetime.now(tz=DISPLAY_TZ).replace(tzinfo=None).isoformat()

    for course_id, slug, label, subtitle in [
        (SELF_COURSE_ID,   "AWS_NextPath_Self_Dashboard.html",
         "NextPath Self-Paced",   "Self-Paced AWS Cloud Learning — learner activity tracker"),
        (COHORT_COURSE_ID, "AWS_NextPath_Cohort_Dashboard.html",
         "NextPath Cohort",       "Guided Cohort Programme — learner activity tracker"),
    ]:
        print(f"\nFetching Moodle roster for course {course_id} ({label})…")
        try:
            roster = get_enrolled_students(course_id)
        except Exception as e:
            print(f"  ERROR fetching roster: {e}")
            roster = []
        print(f"  {len(roster)} students enrolled.")

        students = build_student_stats(roster, all_records, durations)
        html = render_dashboard(label, subtitle, students, generated_at)

        out = BASE_DIR / slug
        out.write_text(html, encoding="utf-8")
        print(f"  Written: {out}")

    print("\nDone.")


if __name__ == "__main__":
    main()
