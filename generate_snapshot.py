#!/usr/bin/env python3
"""
Praesignis Cloud Skills Challenge — public snapshot generator
================================================================
Standalone extraction of server.py's build_leaderboard() + render_static_snapshot()
(the same public-safe export already used for WordPress — masked names only, no
admin/farming/anti-gaming data, no emails or user IDs). Meant to run unattended
on a schedule (see .github/workflows/publish.yml) and write a self-contained
Cloud_Skills_Leaderboard.html into this GitHub Pages repo, which the WP site iframes.

This intentionally does NOT include any of server.py's Flask routes or the
admin-mutation helpers (draw, approve/reject, redact, set-linkedin-follow,
CSV imports) — this script only ever reads and renders, never mutates.

Config and manual_data are written to disk by the GitHub Action from repo
secrets immediately before this script runs, and are gitignored — never
committed to this (public) repo.
"""

import os
import csv
import base64
import calendar
import html as html_lib
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo
from pathlib import Path
from collections import defaultdict
from statistics import mean, median

import requests

BASE_DIR = Path(__file__).parent
MANUAL_DIR = BASE_DIR / "manual_data"
CONFIG_FILE = BASE_DIR / "config.json"
LOGO_FILE = BASE_DIR / "Secondary White New Tagline -02.png"
BANNER_FILE = BASE_DIR / "CSC2026Banner.png"

# ── Env (GitHub Actions injects these directly as secrets; .env is only for local testing) ──
_env_path = BASE_DIR / ".env"
if _env_path.exists():
    for _line in _env_path.read_text(encoding="utf-8").splitlines():
        _line = _line.strip()
        if _line and not _line.startswith("#") and "=" in _line:
            _k, _, _v = _line.partition("=")
            os.environ.setdefault(_k.strip(), _v.strip())

RAVEN360_BASE_URL = "https://api.raven360.com"
RAVEN360_CLIENT_ID = os.environ.get("RAVEN360_CLIENT_ID")
RAVEN360_CLIENT_SECRET = os.environ.get("RAVEN360_CLIENT_SECRET")
RAVEN360_XAPI_KEY = os.environ.get("RAVEN360_XAPI_KEY")

# See server.py for the empirical basis of this offset — Raven360 timestamps
# are naive strings that are actually US Eastern time, not South African time.
RAVEN360_SOURCE_TZ = ZoneInfo("America/New_York")
DISPLAY_TZ = ZoneInfo("Africa/Johannesburg")
_TZ_DATE_FIELDS = ("first_access_date", "last_access_date", "completed_date", "created_date", "updated_date")


def _convert_tz(s):
    if not s:
        return s
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return s
    dt = dt.replace(tzinfo=RAVEN360_SOURCE_TZ).astimezone(DISPLAY_TZ)
    return dt.replace(tzinfo=None).isoformat()


MOODLE_URL = "https://edusignis.praesignis.com"
MOODLE_TOKEN = os.environ.get("MOODLE_TOKEN")

PROGRESS_ENDPOINTS = [
    ("/administration/progress/channels", "channel_id"),
    ("/administration/progress/learningpaths", "learningpath_id"),
    ("/administration/progress/learningobjects", "learningobject_id"),
]


def load_config():
    import json
    return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))


CONFIG = load_config()


def get_competition_start_date():
    raw = CONFIG.get("competition", {}).get("start_date")
    if not raw:
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        return None


def get_from_date():
    start = get_competition_start_date()
    if start:
        return start.strftime("%m-%d-%Y")
    return (date.today() - timedelta(days=365)).strftime("%m-%d-%Y")


def get_competition_months():
    start = get_competition_start_date()
    if not start:
        return []
    num_months = CONFIG.get("competition", {}).get("num_months", 5)
    today = date.today()
    months = []
    period_start = start
    for i in range(num_months):
        last_day = calendar.monthrange(period_start.year, period_start.month)[1]
        period_end = date(period_start.year, period_start.month, last_day)
        if today < period_start:
            status = "upcoming"
        elif today > period_end:
            status = "ended"
        else:
            status = "active"
        label = period_start.strftime("%B %Y")
        months.append({"index": i + 1, "start": period_start, "end": period_end, "label": label, "status": status})
        next_month = period_start.month % 12 + 1
        next_year = period_start.year + (1 if period_start.month == 12 else 0)
        period_start = date(next_year, next_month, 1)
    return months


def resolve_month(requested_index):
    months = get_competition_months()
    if not months:
        return None, None
    if requested_index is not None:
        match = next((m for m in months if m["index"] == requested_index), None)
        if match:
            return match["start"], match["end"]
    active = next((m for m in months if m["status"] == "active"), None)
    if active:
        return active["start"], active["end"]
    if date.today() < months[0]["start"]:
        return months[0]["start"], months[0]["end"]
    return months[-1]["start"], months[-1]["end"]


# ── Raven360 ─────────────────────────────────────────────────────────────

def get_raven360_token():
    resp = requests.post(
        f"{RAVEN360_BASE_URL}/gettoken",
        json={"client_id": RAVEN360_CLIENT_ID, "client_secret": RAVEN360_CLIENT_SECRET},
        headers={"x-api-key": RAVEN360_XAPI_KEY, "Accept": "application/json"},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["data"]["token"]


def fetch_progress(token, path, id_field):
    resp = requests.post(
        f"{RAVEN360_BASE_URL}{path}",
        headers={"Authorization": token, "x-api-key": RAVEN360_XAPI_KEY, "Accept": "application/json"},
        json={"from_date": get_from_date(), "to_date": date.today().strftime("%m-%d-%Y")},
        timeout=120,
    )
    if resp.status_code == 500:
        try:
            if resp.json().get("error", {}).get("message") == "No Results Found":
                return []
        except ValueError:
            pass
    resp.raise_for_status()
    data = resp.json()
    records = data.get("data", data) if isinstance(data, dict) else data
    records = records if isinstance(records, list) else []
    for r in records:
        r["_content_key"] = (id_field, r.get(id_field))
        for f in _TZ_DATE_FIELDS:
            if r.get(f):
                r[f] = _convert_tz(r[f])
    return records


_CATALOG_CACHE = {}

_CATALOG_SOURCES = [
    ("/administration/catalog/learningobjects", "learningobject_id", {"learningobject_type": "Content"}),
    ("/administration/catalog/channels", "channel_id", {}),
    ("/administration/catalog/learningpaths", "learningpath_id", {}),
]


def parse_duration(s):
    if not s:
        return None
    try:
        h, m, sec = (int(p) for p in str(s).split(":"))
    except (ValueError, TypeError):
        return None
    total = h * 60 + m + sec / 60
    return total if total > 0 else None


def parse_domains(category):
    if not category:
        return []
    domains = []
    for group in category:
        if group.get("title") == "Domain":
            domains.extend(t.get("title") for t in group.get("tags", []) if t.get("title"))
    return domains


def get_catalog_info(token):
    if _CATALOG_CACHE:
        return _CATALOG_CACHE
    for path, id_field, extra in _CATALOG_SOURCES:
        try:
            resp = requests.post(
                f"{RAVEN360_BASE_URL}{path}",
                headers={"Authorization": token, "x-api-key": RAVEN360_XAPI_KEY, "Accept": "application/json"},
                json={**extra, "from_date": "01-01-2015", "to_date": "12-31-2030"},
                timeout=120,
            )
            resp.raise_for_status()
            for item in resp.json().get("data", []):
                course_id = item.get(id_field)
                if course_id is None:
                    continue
                _CATALOG_CACHE[(id_field, course_id)] = {
                    "name": item.get("display_name") or item.get("name") or f"Course #{course_id}",
                    "durationMinutes": parse_duration(item.get("duration")),
                    "domains": parse_domains(item.get("category")),
                    "contentType": item.get("content_type") or None,
                }
        except Exception:
            continue
    return _CATALOG_CACHE


def course_name(id_field, course_id):
    info = _CATALOG_CACHE.get((id_field, course_id))
    return info["name"] if info else f"Course #{course_id}"


def course_domains(id_field, course_id):
    info = _CATALOG_CACHE.get((id_field, course_id))
    return info["domains"] if info else []


def course_duration_minutes(id_field, course_id):
    info = _CATALOG_CACHE.get((id_field, course_id))
    return info["durationMinutes"] if info else None


def course_content_type(id_field, course_id):
    info = _CATALOG_CACHE.get((id_field, course_id))
    return info["contentType"] if info else None


# ── Moodle (display names — resolved then immediately masked, see mask_name) ──

def _moodle_lookup_chunk(field, chunk):
    if not chunk:
        return {}
    params = {
        "wstoken": MOODLE_TOKEN,
        "wsfunction": "core_user_get_users_by_field",
        "moodlewsrestformat": "json",
        "field": field,
    }
    for idx, v in enumerate(chunk):
        params[f"values[{idx}]"] = v
    try:
        resp = requests.post(f"{MOODLE_URL}/webservice/rest/server.php", params=params, timeout=30)
        resp.raise_for_status()
        users = resp.json()
        if not isinstance(users, list):
            raise ValueError(f"unexpected response: {users}")
        return {
            (u.get(field) or "").lower(): u.get("fullname")
            for u in users if u.get(field)
        }
    except Exception:
        if len(chunk) == 1:
            return {}
        mid = len(chunk) // 2
        result = _moodle_lookup_chunk(field, chunk[:mid])
        result.update(_moodle_lookup_chunk(field, chunk[mid:]))
        return result


def _moodle_lookup_by_field(field, values):
    result = {}
    values = list(values)
    CHUNK = 50
    for i in range(0, len(values), CHUNK):
        result.update(_moodle_lookup_chunk(field, values[i:i + CHUNK]))
    return result


_NAME_CACHE = {}


def moodle_get_fullnames(emails):
    emails = [e.lower() for e in emails]
    unresolved = [e for e in emails if e not in _NAME_CACHE]

    if unresolved:
        found = _moodle_lookup_by_field("email", unresolved)
        still_missing = [e for e in unresolved if e not in found]
        if still_missing:
            found.update(_moodle_lookup_by_field("username", still_missing))
        _NAME_CACHE.update(found)

    return {e: _NAME_CACHE[e] for e in emails if e in _NAME_CACHE}


# ── Manual data (CSV imports, written from a repo secret at runtime — read-only here) ──

MANUAL_FILES = {
    "assessments": (MANUAL_DIR / "assessments.csv", ["courseId", "score"]),
    "social_shares": (MANUAL_DIR / "social_shares.csv", ["url", "verified"]),
    "linkedin_follows": (MANUAL_DIR / "linkedin_follows.csv", ["followed"]),
}
APPROVED_FILE = MANUAL_DIR / "approved_completions.csv"
REJECTED_FILE = MANUAL_DIR / "rejected_completions.csv"
QUALITY_REDACTIONS_FILE = MANUAL_DIR / "quality_redactions.csv"

_EMAIL_TO_USERID = {}


def _read_csv_rows(path):
    if not path.exists():
        return []
    with path.open("r", encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def _to_int(v):
    try:
        return int(str(v).strip())
    except (ValueError, TypeError):
        return None


def _to_bool(v):
    return str(v).strip().lower() in ("true", "1", "yes", "y")


def _resolve_user_id(row):
    uid = _to_int(row.get("userId"))
    if uid is not None:
        return uid
    email = (row.get("email") or "").strip().lower()
    return _EMAIL_TO_USERID.get(email) if email else None


def load_assessments():
    out = defaultdict(list)
    for row in _read_csv_rows(MANUAL_FILES["assessments"][0]):
        uid = _resolve_user_id(row)
        cid = _to_int(row.get("courseId"))
        try:
            score = float(row.get("score"))
        except (ValueError, TypeError):
            continue
        if uid is None:
            continue
        out[uid].append({"courseId": cid, "score": score})
    return out


def load_social_shares():
    out = defaultdict(list)
    for row in _read_csv_rows(MANUAL_FILES["social_shares"][0]):
        uid = _resolve_user_id(row)
        if uid is None:
            continue
        out[uid].append({"url": row.get("url", ""), "verified": _to_bool(row.get("verified"))})
    return out


def load_linkedin_follows():
    out = set()
    for row in _read_csv_rows(MANUAL_FILES["linkedin_follows"][0]):
        uid = _resolve_user_id(row)
        if uid is not None and _to_bool(row.get("followed")):
            out.add(uid)
    return out


def load_approved_completions():
    out = set()
    for row in _read_csv_rows(APPROVED_FILE):
        uid = _to_int(row.get("userId"))
        cid = _to_int(row.get("courseId"))
        if uid is not None and cid is not None:
            out.add((uid, cid))
    return out


def load_rejected_completions():
    out = set()
    for row in _read_csv_rows(REJECTED_FILE):
        uid = _to_int(row.get("userId"))
        cid = _to_int(row.get("courseId"))
        if uid is not None and cid is not None:
            out.add((uid, cid))
    return out


def load_quality_redactions():
    out = set()
    for row in _read_csv_rows(QUALITY_REDACTIONS_FILE):
        uid = _to_int(row.get("userId"))
        cid = _to_int(row.get("courseId"))
        if uid is not None and cid is not None and _to_bool(row.get("redacted")):
            out.add((uid, cid))
    return out


# ── Aggregation ──────────────────────────────────────────────────────────

def _parse_dt(s):
    try:
        return datetime.fromisoformat(s) if s else None
    except ValueError:
        return None


def aggregate_progress(all_records, month_start=None, month_end=None):
    TYPE_PRIORITY = ["learningobject_id", "learningpath_id", "channel_id"]

    raw_by_key = defaultdict(list)
    for r in all_records:
        email = (r.get("email_id") or "").lower()
        id_field, content_id = r["_content_key"]
        if not email or content_id is None:
            continue
        if month_start:
            fa = _parse_dt(r.get("first_access_date"))
            if not fa or not (month_start <= fa.date() <= month_end):
                continue
        raw_by_key[(email, id_field, content_id)].append(r)

    by_email_type = defaultdict(lambda: defaultdict(list))
    for (email, id_field, content_id), rows in raw_by_key.items():
        auth = max(rows, key=lambda r: r.get("updated_date") or r.get("created_date") or "")
        by_email_type[email][id_field].append((content_id, auth, rows))

    per_user = {}
    for email, type_records in by_email_type.items():
        for id_field in TYPE_PRIORITY:
            entries = type_records.get(id_field)
            if entries:
                break
        else:
            continue

        last_active = None
        touched = []
        for content_id, auth, rows in entries:
            last_seen = auth.get("last_access_date") or auth.get("updated_date")
            if last_seen and (last_active is None or last_seen > last_active):
                last_active = last_seen

            days = set()
            for r in rows:
                for f in ("first_access_date", "last_access_date", "completed_date"):
                    v = r.get(f)
                    if v:
                        days.add(v[:10])

            multi_row_gap_minutes = None
            if len(rows) > 1:
                stamps = sorted(t for r in rows if (t := _parse_dt(r.get("first_access_date"))))
                if len(stamps) > 1:
                    gaps = [(b - a).total_seconds() / 60 for a, b in zip(stamps, stamps[1:])]
                    multi_row_gap_minutes = max(gaps)

            touched.append({
                "courseId": content_id,
                "completionPct": auth.get("completion_percentage"),
                "firstAccess": auth.get("first_access_date"),
                "lastAccess": auth.get("last_access_date"),
                "completedDate": auth.get("completed_date"),
                "distinctDays": days,
                "multiRowGapMinutes": multi_row_gap_minutes,
            })

        per_user[email] = {
            "user_id": entries[0][1].get("user_id"),
            "id_field": id_field,
            "total_touched": len(touched),
            "touched": touched,
            "last_active": last_active,
        }

    return per_user


# ── Trust signals & farming detection ───────────────────────────────────

def compute_day_median_gaps(touched):
    by_day = defaultdict(list)
    for t in touched:
        dt = _parse_dt(t["firstAccess"])
        if dt:
            by_day[t["firstAccess"][:10]].append(dt)

    result = {}
    for day, stamps in by_day.items():
        stamps.sort()
        if len(stamps) < 2:
            result[day] = None
        else:
            gaps = [(b - a).total_seconds() for a, b in zip(stamps, stamps[1:])]
            result[day] = median(gaps)
    return result


def click_points_for_duration(duration_minutes, tiers):
    if duration_minutes is None:
        return tiers[0]["points"]
    for tier in tiers:
        if tier["max_minutes"] is None or duration_minutes <= tier["max_minutes"]:
            return tier["points"]
    return tiers[-1]["points"]


def compute_click_gaps(touched, id_field, cap_multiplier):
    items = sorted(
        (t for t in touched if t.get("firstAccess") and _parse_dt(t["firstAccess"])),
        key=lambda t: t["firstAccess"],
    )
    gaps = {}
    for i, t in enumerate(items):
        if i + 1 >= len(items):
            gaps[t["courseId"]] = (None, None)
            continue
        raw = (_parse_dt(items[i + 1]["firstAccess"]) - _parse_dt(t["firstAccess"])).total_seconds() / 60
        duration = course_duration_minutes(id_field, t["courseId"])
        capped = min(raw, duration * cap_multiplier) if duration else raw
        gaps[t["courseId"]] = (round(raw, 1), round(capped, 1))
    return gaps


def compute_trust(t, id_field, click_gaps, day_median_gaps, assessments_for_course, trust_cfg):
    raw_gap, capped_gap = click_gaps.get(t["courseId"], (None, None))
    duration = course_duration_minutes(id_field, t["courseId"])
    quality_signal = (
        capped_gap is not None and duration is not None
        and capped_gap >= (trust_cfg["completion_threshold_pct"] / 100) * duration
    )

    sessions_signal = len(t["distinctDays"]) >= 2 or (
        t["multiRowGapMinutes"] is not None and t["multiRowGapMinutes"] > trust_cfg["idle_cap_minutes"]
    )

    day = t["completedDate"][:10] if t["completedDate"] else None
    day_gap = day_median_gaps.get(day) if day else None
    pace_signal = day_gap is None or day_gap > trust_cfg["rapid_hop_seconds"]

    kc_signal = any(a["score"] >= trust_cfg["_assessment_pass_score"] for a in assessments_for_course)

    score = int(quality_signal) + int(sessions_signal) + int(pace_signal) + (trust_cfg["kc_pass_signal_value"] if kc_signal else 0)
    validated = score >= trust_cfg["signals_required"]

    return validated, score, {
        "quality": quality_signal, "sessions": sessions_signal, "pace": pace_signal, "kc": kc_signal,
    }, capped_gap


def build_leaderboard(month_index=None):
    cfg = CONFIG
    P, T, V, TR, F, CS = cfg["points"], cfg["thresholds"], cfg["vouchers"], cfg["trust"], cfg["farming"], cfg["click_scoring"]
    trust_cfg = {**TR, "_assessment_pass_score": T["assessment_pass_score"]}

    month_start, month_end = resolve_month(month_index)

    token = get_raven360_token()
    all_records = []
    comp_start = get_competition_start_date()
    if not comp_start or comp_start <= date.today():
        for path, id_field in PROGRESS_ENDPOINTS:
            all_records.extend(fetch_progress(token, path, id_field))

    get_catalog_info(token)
    per_user = aggregate_progress(all_records, month_start, month_end)
    names = moodle_get_fullnames(per_user.keys())

    cumulative_per_user = aggregate_progress(all_records, None, None)
    clicks_since_start = sum(cu["total_touched"] for cu in cumulative_per_user.values())

    _EMAIL_TO_USERID.clear()
    _EMAIL_TO_USERID.update({email: u["user_id"] for email, u in per_user.items()})

    assessments_by_user = load_assessments()
    social_by_user = load_social_shares()
    linkedin_followed = load_linkedin_follows()
    approved = load_approved_completions()
    rejected = load_rejected_completions()
    quality_redactions = load_quality_redactions()

    week_cutoff = datetime.now() - timedelta(days=T["course_of_the_week_days"])

    course_week_counts = defaultdict(int)
    pending_list = []
    zero_kc_list = []
    learners = []

    for email, u in per_user.items():
        user_id = u["user_id"]
        id_field = u["id_field"]
        touched = u["touched"]
        my_assessments = assessments_by_user.get(user_id, [])

        day_median_gaps = compute_day_median_gaps(touched)
        click_gaps = compute_click_gaps(touched, id_field, trust_cfg["quality_gap_cap_multiplier"])
        raw_completions = [t for t in touched if t["completionPct"] == 100]

        completions_trust = []
        for t in raw_completions:
            assessments_for_course = [a for a in my_assessments if a["courseId"] == t["courseId"]]
            validated, score, signals, verified_minutes = compute_trust(
                t, id_field, click_gaps, day_median_gaps, assessments_for_course, trust_cfg
            )
            manually_approved = (user_id, t["courseId"]) in approved
            manually_rejected = (user_id, t["courseId"]) in rejected
            completions_trust.append({
                "courseId": t["courseId"],
                "completedDate": t["completedDate"],
                "firstAccess": t["firstAccess"],
                "engagedMinutes": verified_minutes,
                "trustScore": score,
                "signals": signals,
                "validated": validated or manually_approved,
                "reviewed": manually_approved or manually_rejected,
            })

        validated_completions = [c for c in completions_trust if c["validated"]]

        timeline = []
        for t in touched:
            duration = course_duration_minutes(id_field, t["courseId"])
            base_pts = click_points_for_duration(duration, CS["duration_tiers"])
            raw_gap, capped_gap = click_gaps.get(t["courseId"], (None, None))
            trust_row = next((c for c in completions_trust if c["courseId"] == t["courseId"]), None)
            auto_quality = bool(trust_row and trust_row["signals"]["quality"])
            redacted = (user_id, t["courseId"]) in quality_redactions
            timeline.append({
                "courseId": t["courseId"],
                "courseName": course_name(id_field, t["courseId"]),
                "contentType": course_content_type(id_field, t["courseId"]),
                "durationMinutes": duration,
                "firstAccess": t["firstAccess"],
                "completedDate": t["completedDate"] if t["completionPct"] == 100 else None,
                "rawGapMinutes": raw_gap,
                "verifiedMinutes": capped_gap,
                "qualityAutoVerified": auto_quality,
                "qualityRedacted": redacted,
                "qualityVerified": auto_quality and not redacted,
                "basePoints": base_pts,
                "bonusPoints": round(base_pts * CS["quality_bonus_multiplier"]) if (trust_row and trust_row["validated"] and auto_quality and not redacted) else 0,
            })
        timeline.sort(key=lambda x: x["firstAccess"] or "")

        domains_touched = set()
        for c in validated_completions:
            domains_touched.update(course_domains(id_field, c["courseId"]))

        active_days = {t["firstAccess"][:10] for t in touched if t.get("firstAccess")}

        for c in completions_trust:
            if not c["validated"] and not c["reviewed"]:
                pending_list.append({
                    "userId": user_id,
                    "name": names.get(email) or email,
                    "courseId": c["courseId"],
                    "courseName": course_name(id_field, c["courseId"]),
                    "minutes": c["engagedMinutes"],
                    "completedDate": c["completedDate"],
                    "trustScore": c["trustScore"],
                    "signals": c["signals"],
                })

        for c in raw_completions:
            dt = _parse_dt(c["completedDate"])
            if dt and dt >= week_cutoff:
                course_week_counts[c["courseId"]] += 1

        completed_this_week = sum(
            1 for c in validated_completions
            if (dt := _parse_dt(c["completedDate"])) and dt >= week_cutoff
        )

        passed = [a for a in my_assessments if a["score"] >= T["assessment_pass_score"]]
        avg_score = round(mean(a["score"] for a in my_assessments), 1) if my_assessments else None

        if raw_completions and not my_assessments:
            zero_kc_list.append({
                "userId": user_id,
                "name": names.get(email) or email,
                "completionsCount": len(raw_completions),
            })

        my_shares = social_by_user.get(user_id, [])
        verified_shares = [s for s in my_shares if s["verified"]]

        pts_click = sum(row["basePoints"] for row in timeline)
        pts_quality = sum(row["bonusPoints"] for row in timeline)
        pts_breadth = len(domains_touched) * P["domain_breadth"]
        pts_active_days = len(active_days) * P["active_day"]
        pts_assessment = len(passed) * P["assessment_pass"]
        pts_social = len(verified_shares) * P["social_share"]
        total_points = pts_click + pts_quality + pts_breadth + pts_active_days + pts_assessment + pts_social

        badge_courses = len(validated_completions) >= T["qualification_min_courses_completed"]
        badge_breadth = len(domains_touched) >= T["qualification_min_domains"]
        badge_active_days = len(active_days) >= T["qualification_min_active_days"]
        badge_linkedin = user_id in linkedin_followed
        qualified = badge_courses and badge_breadth and badge_active_days and badge_linkedin

        learners.append({
            "userId": user_id,
            "name": names.get(email) or email,
            "email": email,
            "coursesTouched": u["total_touched"],
            "coursesCompleted": len(validated_completions),
            "pendingCompletions": sum(1 for c in completions_trust if not c["validated"] and not c["reviewed"]),
            "domainsCount": len(domains_touched),
            "domains": sorted(domains_touched),
            "activeDaysCount": len(active_days),
            "avgAssessmentScore": avg_score,
            "assessmentAttempts": len(my_assessments),
            "assessmentPasses": len(passed),
            "verifiedShares": len(verified_shares),
            "completedThisWeek": completed_this_week,
            "points": {
                "click": pts_click,
                "quality": pts_quality,
                "breadth": pts_breadth,
                "activeDays": pts_active_days,
                "assessment": pts_assessment,
                "social": pts_social,
            },
            "totalPoints": total_points,
            "timeline": timeline,
            "badges": {
                "courses": badge_courses,
                "breadth": badge_breadth,
                "activeDays": badge_active_days,
                "linkedin": badge_linkedin,
            },
            "qualified": qualified,
            "lastActive": u["last_active"],
        })

    learners.sort(key=lambda x: -x["totalPoints"])
    for i, l in enumerate(learners, start=1):
        l["rank"] = i
        if i > 20:
            l["timeline"] = None

    qualified_learners = [l for l in learners if l["qualified"]]
    qualified_learners.sort(key=lambda x: -x["totalPoints"])

    top5 = qualified_learners[: T["top_performers_count"]]
    for i, l in enumerate(top5, start=1):
        l["voucher"] = V.get(str(i))
    lucky_pool = qualified_learners[T["top_performers_count"]:]

    course_of_week = None
    if course_week_counts:
        top_course_id = max(course_week_counts, key=course_week_counts.get)
        course_of_week = {
            "courseId": top_course_id,
            "courseName": course_name("learningobject_id", top_course_id),
            "completions": course_week_counts[top_course_id],
            "windowDays": T["course_of_the_week_days"],
        }

    total_learners = len(learners)
    total_points_sum = sum(l["totalPoints"] for l in learners)

    resolved_month = next(
        (m for m in get_competition_months() if m["start"] == month_start and m["end"] == month_end),
        None,
    )

    return {
        "generatedAt": datetime.now().isoformat(),
        "source": "live",
        "config": cfg,
        "month": {
            "index": resolved_month["index"] if resolved_month else None,
            "start": month_start.isoformat() if month_start else None,
            "end": month_end.isoformat() if month_end else None,
            "label": resolved_month["label"] if resolved_month else None,
            "status": resolved_month["status"] if resolved_month else None,
        },
        "summary": {
            "totalLearners": total_learners,
            "totalPoints": total_points_sum,
            "avgPoints": round(total_points_sum / total_learners, 1) if total_learners else 0,
            "qualifiedCount": len(qualified_learners),
            "pendingCount": len(pending_list),
        },
        "leaderboard": learners,
        "top5Winners": top5,
        "luckyDrawPool": lucky_pool,
        "courseOfTheWeek": course_of_week,
        "awsClickProgress": {
            "baselineClicks": cfg.get("aws_partner", {}).get("baseline_clicks", 0),
            "clicksSinceStart": clicks_since_start,
            "totalClicks": cfg.get("aws_partner", {}).get("baseline_clicks", 0) + clicks_since_start,
            "goal": cfg.get("aws_partner", {}).get("click_goal", 20000),
            "since": comp_start.isoformat() if comp_start else None,
        },
    }


# ── Static snapshot export ──────────────────────────────────────────────
# Public-facing (WordPress). No full names, emails, user IDs, or admin-only
# data (farming flags, anti-gaming detail) may appear anywhere in this output.

def mask_name(name):
    name = (name or "").strip()
    if not name or "@" in name:
        return "Learner"
    parts = name.split()
    if len(parts) == 1:
        return parts[0]
    return f"{parts[0]} {parts[-1][0].upper()}."


def build_masked_names(leaderboard):
    masked = {l["userId"]: mask_name(l["name"]) for l in leaderboard}
    groups = defaultdict(list)
    for uid, m in masked.items():
        groups[m].append(uid)
    result = {}
    for m, uids in groups.items():
        if len(uids) == 1:
            result[uids[0]] = m
        else:
            for i, uid in enumerate(uids, start=1):
                result[uid] = f"{m}{i}"
    return result


_LOGO_DATA_URI = None
_BANNER_DATA_URI = None


def get_logo_data_uri():
    global _LOGO_DATA_URI
    if _LOGO_DATA_URI is None:
        try:
            _LOGO_DATA_URI = "data:image/png;base64," + base64.b64encode(LOGO_FILE.read_bytes()).decode("ascii")
        except OSError:
            _LOGO_DATA_URI = ""
    return _LOGO_DATA_URI


def get_banner_data_uri():
    global _BANNER_DATA_URI
    if _BANNER_DATA_URI is None:
        try:
            _BANNER_DATA_URI = "data:image/png;base64," + base64.b64encode(BANNER_FILE.read_bytes()).decode("ascii")
        except OSError:
            _BANNER_DATA_URI = ""
    return _BANNER_DATA_URI


def _static_badge_dot(label, on, title):
    style = (
        "background:#34d399;color:#06281a;border-color:#34d399;" if on
        else "background:transparent;color:#93a0c0;border-color:#2a3a5f;"
    )
    return (
        f'<span title="{html_lib.escape(title)}" '
        f'style="display:inline-flex;align-items:center;justify-content:center;width:20px;height:20px;'
        f'border-radius:5px;border:1px solid;font-size:0.65rem;font-weight:bold;margin-right:3px;{style}">{label}</span>'
    )


def render_badges_static(b):
    return (
        _static_badge_dot("C", b["courses"], "Completions")
        + _static_badge_dot("B", b["breadth"], "Breadth")
        + _static_badge_dot("D", b["activeDays"], "Consistency")
        + _static_badge_dot("L", b["linkedin"], "LinkedIn")
    )


def render_static_snapshot(payload, is_month_end=False):
    esc = html_lib.escape
    T = payload["config"]["thresholds"]
    masked_names = build_masked_names(payload["leaderboard"])
    top_n_for_gap = T.get("top_performers_count", 3)

    def _is_anon(l):
        raw = (l.get("name") or "").strip()
        return not raw or "@" in raw

    any_anon = any(_is_anon(l) for l in payload["leaderboard"])
    badge_legend = (
        f'{_static_badge_dot("C", True, "Completions")} Completions &middot; '
        f'{_static_badge_dot("B", True, "Breadth")} Breadth &middot; '
        f'{_static_badge_dot("D", True, "Consistency")} Consistency &middot; '
        f'{_static_badge_dot("L", True, "LinkedIn")} LinkedIn'
    )
    rows = "\n".join(f"""
        <tr class="{'cutoff-row' if l['rank'] == top_n_for_gap else ''}" data-name="{esc(masked_names[l['userId']].lower())}">
          <td>#{l['rank']}</td>
          <td>{esc(masked_names[l['userId']])}{' <span class="anon-mark" title="We\'re missing your profile details — we\'ll be in touch to fix this shortly">†</span>' if _is_anon(l) else ''}</td>
          <td>{render_badges_static(l['badges'])}</td>
          <td><b>{l['totalPoints']}</b></td>
        </tr>""" for l in payload["leaderboard"])

    top5_cards = "\n".join(f"""
        <div class="card rank-{i}">
          <div class="rank">#{i}</div>
          <div class="name">{esc(masked_names[w['userId']])}</div>
          <div class="pts">{w['totalPoints']} pts</div>
          <div style="margin-top:6px">{render_badges_static(w['badges'])}</div>
          <div class="voucher">🎟 {esc(w.get('voucher') or '')}</div>
        </div>""" for i, w in enumerate(payload["top5Winners"], start=1))

    cow = payload.get("courseOfTheWeek")
    cow_html = (
        f"<div class=\"cow-name\">{esc(cow['courseName'])}</div><div class=\"muted\">{cow['completions']} completions in the last {cow['windowDays']} days</div>"
        if cow else '<div class="muted">No completions in the current window.</div>'
    )

    generated_dt = payload["generatedAt"][:16].replace("T", " ")
    generated = esc(generated_dt)
    month = payload.get("month") or {}
    month_range = f"{month['start']} – {month['end']}" if month.get("index") else "No monthly structure set"
    month_of = month.get("label") or "—"
    top_n = payload["config"]["thresholds"].get("top_performers_count", 3)
    banner_uri = get_banner_data_uri()
    logo_uri = get_logo_data_uri()
    hero_html = (
        f'<img src="{banner_uri}" alt="Praesignis Cloud Skills Challenge 2026" class="banner-img">' if banner_uri
        else f'<img src="{logo_uri}" alt="Praesignis" style="height:64px;width:auto;display:block;">' if logo_uri
        else "<h1>Praesignis Cloud Skills Challenge 2026</h1>"
    )

    stat_cards = "".join(f"""
        <div class="stat-card">
          <div class="stat-label">{esc(label)}</div>
          <div class="stat-value">{esc(str(value))}</div>
        </div>""" for label, value in [
        ("Competition Month", month_of),
        ("Window", month_range),
    ])
    join_cta_html = f"""
        <a class="join-card" href="https://praesignis.com/product/cloud-skills-challenge-2026/" target="_blank" rel="noopener">
          <div class="join-label">Not in the challenge yet?</div>
          <div class="join-cta">Click here to Join or Participate →</div>
        </a>"""

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Praesignis Cloud Skills Challenge — Snapshot</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    font-family: Arial, Helvetica, sans-serif;
    background: linear-gradient(180deg, #0f172a 0%, #0c1526 55%, #0b1220 100%);
    background-attachment: fixed;
    color:#e8ecf7; padding:24px; margin:0;
  }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  h1 {{ margin:0 0 4px; }}
  h2 {{ margin: 0 0 12px; font-size: 1.15rem; }}
  .muted {{ color:#93a0c0; font-size:0.85rem; }}

  .banner-wrap {{ border-radius:14px; overflow:hidden; box-shadow: 0 6px 24px rgba(0,0,0,0.4); margin-bottom:10px; line-height:0; }}
  .banner-img {{ width:100%; height:auto; display:block; }}
  .meta-strip {{ text-align:center; margin: 0 0 20px; }}

  .panel {{
    background: linear-gradient(160deg, #18213a, #141c30);
    border:1px solid #2a3a5f; border-radius:14px; padding:20px 22px; margin-bottom:20px;
  }}

  .stats-grid {{ display:grid; grid-template-columns: 1fr 1.4fr 1.6fr; gap:12px; margin-bottom:20px; }}
  @media (max-width: 640px) {{ .stats-grid {{ grid-template-columns: 1fr; }} }}
  .stat-card {{
    background: linear-gradient(160deg, #1c2846, #16213a);
    border:1px solid #2a3a5f; border-radius:12px; padding:14px 16px;
  }}
  .stat-label {{ color:#93a0c0; font-size:0.7rem; text-transform:uppercase; letter-spacing:0.04em; white-space:nowrap; }}
  .stat-value {{ font-size:1.3rem; font-weight:bold; margin-top:4px; }}

  .join-card {{
    display:flex; flex-direction:column; justify-content:center; align-items:flex-start; gap:4px;
    background: linear-gradient(135deg, #ff9900, #ffb84d);
    border:1px solid #ffb84d; border-radius:12px; padding:14px 18px;
    text-decoration:none; box-shadow:0 4px 16px rgba(255,153,0,0.25);
    transition: transform 0.15s ease, box-shadow 0.15s ease;
  }}
  .join-card:hover {{ transform:translateY(-1px); box-shadow:0 6px 20px rgba(255,153,0,0.35); }}
  .join-label {{ color:#4a2e00; font-size:0.7rem; text-transform:uppercase; letter-spacing:0.04em; font-weight:700; }}
  .join-cta {{ color:#1a1200; font-size:1.15rem; font-weight:800; }}

  .cards {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(180px,1fr)); gap:12px; }}
  .card {{
    background: linear-gradient(160deg, #1c2846, #16213a);
    border:1px solid #2a3a5f; border-radius:12px; padding:16px 18px;
  }}
  .card.rank-1 {{ border-color:#ffd166; box-shadow:0 0 0 1px #ffd166 inset; }}
  .card.rank-2 {{ border-color:#cbd5e1; box-shadow:0 0 0 1px #cbd5e1 inset; }}
  .card.rank-3 {{ border-color:#d99a6c; box-shadow:0 0 0 1px #d99a6c inset; }}
  .rank {{ color:#22d3ee; font-weight:bold; font-size:0.8rem; }}
  .name {{ font-weight:bold; margin:6px 0 4px; font-size:1.05rem; }}
  .pts {{ color:#ff9900; font-weight:bold; }}
  .voucher {{
    font-size:0.78rem; color:#34d399; margin-top:10px; background:#142a1f;
    border:1px solid #1f4a34; border-radius:6px; padding:5px 9px; display:inline-block;
  }}

  .cow-card {{ display:flex; align-items:center; gap:14px; }}
  .cow-icon {{ font-size:2rem; }}
  .cow-name {{ font-weight:bold; font-size:1.05rem; }}

  table {{ width:100%; border-collapse:collapse; }}
  th, td {{ text-align:left; padding:9px 12px; border-bottom:1px solid #2a3a5f; font-size:0.9rem; }}
  th {{ color:#93a0c0; text-transform:uppercase; font-size:0.72rem; letter-spacing:0.03em; }}
  .table-wrap {{ border:1px solid #2a3a5f; border-radius:12px; overflow:hidden; }}
  .table-wrap table {{ background: linear-gradient(160deg, #1a2540, #16213a); }}
  .table-wrap thead th {{ background:#1c2846; }}
  .table-wrap tbody tr:last-child td {{ border-bottom:none; }}

  .search-box {{
    width:100%; max-width:360px;
    background:#16213a; border:1px solid #2a3a5f; color:#e8ecf7;
    padding:10px 14px; border-radius:8px; font-size:0.9rem; margin:0 0 6px;
  }}
  .search-box:focus {{ outline:none; border-color:#22d3ee; }}
  .cutoff-row td {{ border-bottom:2px dashed #ff9900; }}
  .anon-mark {{ color:#ff9900; font-weight:bold; cursor:help; }}
  .anon-legend {{ color:#ffb84d; }}

  .proof-notice {{ border-color:#b54708; }}
  .proof-notice-title {{ font-weight:800; font-size:1rem; margin-bottom:10px; color:#ffd166; }}
  .proof-notice p {{ font-size:0.88rem; line-height:1.6; margin:0 0 8px; color:#ffd166; }}
  .proof-notice p:last-child {{ margin-bottom:0; }}
  .proof-notice b {{ color:#fff2d1; }}
  .badge-legend span {{ vertical-align:middle; margin-left:2px; }}
</style>
</head>
<body>
<div class="container">
  <div class="banner-wrap">{hero_html}</div>
  <p class="muted meta-strip">Snapshot generated {generated} · static export, no live data</p>

  <div class="stats-grid">{stat_cards}{join_cta_html}</div>

  <div class="panel proof-notice">
    <div class="proof-notice-title">📋 Prize Verification: Social Media Proof Required</div>
    <p>Following Praesignis on LinkedIn/Facebook and sharing our official Cloud Skills Challenge post are <b>required to qualify for a prize</b> — not just a formality after winning. Even with the highest points, you won't be counted as a Top 3 or Lucky Draw winner without this.</p>
    <p>Upload your proof via the assignment on Moodle: a screenshot showing you follow Praesignis, and a screenshot or link showing you shared our official post.</p>
    <p><b>Do this before the month ends.</b> If you're a top contender but haven't submitted proof, you'll be skipped and the next qualifying learner takes your spot.</p>
  </div>

  {f'''<div class="panel">
    <h2>🏆 Top {top_n} Performer{'s' if top_n != 1 else ''} — Exam Voucher Winners</h2>
    <div class="cards">{top5_cards or '<p class="muted">No qualified learners yet.</p>'}</div>
  </div>''' if is_month_end else ''}

  <div class="panel cow-card">
    <div class="cow-icon">📅</div>
    <div>
      <div class="muted" style="text-transform:uppercase;font-size:0.72rem;letter-spacing:0.04em;margin-bottom:4px;">Course of the Week</div>
      {cow_html}
    </div>
  </div>

  <div class="panel">
    <h2>Full Leaderboard</h2>
    <p class="muted badge-legend">Badges: {badge_legend} — earn all four to qualify for prizes. The line below rank {top_n_for_gap} marks this month's Top {top_n_for_gap} cutoff.</p>
    {f'<p class="muted anon-legend">† See a name marked like this and think it might be you? We\'re missing your profile details, so we couldn\'t show your real name here — we\'ll be in contact with you shortly to sort it out.</p>' if any_anon else ''}
    <input type="text" id="searchBox" class="search-box" placeholder="Find your name…" oninput="filterRows(this.value)" autocomplete="off">
    <p class="muted" id="searchCount"></p>
    <div class="table-wrap">
      <table>
        <thead><tr><th>Rank</th><th>Learner</th><th>Badges</th><th>Points</th></tr></thead>
        <tbody id="leaderboardBody">{rows}</tbody>
      </table>
    </div>
  </div>
</div>

  <script>
    function filterRows(query) {{
      var q = query.trim().toLowerCase();
      var rows = document.querySelectorAll('#leaderboardBody tr');
      var shown = 0;
      rows.forEach(function(row) {{
        var match = !q || row.getAttribute('data-name').indexOf(q) !== -1;
        row.style.display = match ? '' : 'none';
        if (match) shown++;
      }});
      var countEl = document.getElementById('searchCount');
      countEl.textContent = q ? (shown + ' of ' + rows.length + ' learners match "' + query + '"') : '';
    }}
  </script>
</body>
</html>"""


def main():
    is_month_end = os.environ.get("SNAPSHOT_MONTH_END", "").lower() in ("1", "true", "yes")
    payload = build_leaderboard()
    html_out = render_static_snapshot(payload, is_month_end=is_month_end)
    (BASE_DIR / "Cloud_Skills_Leaderboard.html").write_text(html_out, encoding="utf-8")
    print(f"Wrote Cloud_Skills_Leaderboard.html — {payload['summary']['totalLearners']} learners, generated {payload['generatedAt']}")


if __name__ == "__main__":
    main()
