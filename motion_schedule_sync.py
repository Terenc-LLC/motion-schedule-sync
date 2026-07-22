#!/usr/bin/env python3
"""
motion_schedule_sync.py  (v3 — prefix mapping, multi-user)
==========================================================
For EVERY team member listed in the keys file, stamps their auto-scheduled
Motion tasks with the schedule named after the task's PROJECT PREFIX — the
first 4 characters of the project name, followed by "+ " (the "+ " is the
separator only and is never part of the schedule name). If the assignee has
no schedule matching the prefix (or the task has
no project / no parseable prefix), the task is explicitly set to "Work Hours".

Convention:  project "DERM+ Substation study" ->  schedule "DERM"
             project "AXIS+ Onboarding flow"  ->  schedule "AXIS"
             project "Miscellaneous"          ->  "Work Hours" (no prefix)

Why per-user keys: Motion's API forbids setting a custom schedule on another
user's task. Each member therefore creates their own API key (Settings -> API)
and the script processes each person's tasks under their own key —
self-scheduling for everyone, no cross-user calls.

Requirements:  Python 3.9+, `requests`  (pip install requests)

Setup:
  1. Create motion_keys.json next to this script (chmod 600 — these are
     credentials):
       {
         "users": [
           { "name": "Chris",   "api_key": "..." },
           { "name": "Manager", "api_key": "..." }
         ]
       }
  2. Each member creates schedules named after the prefixes they use
     (e.g. "DERM", "AXIS"). Members without a given schedule simply get
     "Work Hours" for those tasks — safe by design.
  3. Safety pass:      python3 motion_schedule_sync.py --once --dry-run
  4. Verify one PATCH: python3 motion_schedule_sync.py --test TASK_ID --user Chris
  5. Cron — daily 7:00 AM guarantee plus workday polling to approximate
     "on task creation" (Motion's public API has no webhooks):
       0 7 * * *          /usr/bin/python3 /path/to/motion_schedule_sync.py --once >> ~/motion_sync.log 2>&1
       */15 8-18 * * 1-5  /usr/bin/python3 /path/to/motion_schedule_sync.py --once >> ~/motion_sync.log 2>&1

Optional env vars:
  MOTION_SYNC_KEYS      path to keys file        (default: ./motion_keys.json)
  MOTION_SYNC_STATE     path to state file       (default: ~/.motion_schedule_sync.json)
  MOTION_SYNC_THROTTLE  seconds between calls    (default: 6, ~10 req/min per key)
  MOTION_SYNC_SEPARATORS  separator chars after the prefix (default: "+")
  MOTION_SYNC_PREFIX_LEN  prefix length (default: 4)
  MOTION_SYNC_IGNORE_PREFIXES  comma-separated prefixes to leave untouched
                        entirely, e.g. "ZZZ" for archived projects (default: none)
  MOTION_SYNC_REDACT    "1" hides task names, project names, and emails from
                        logs — REQUIRED when logs are public (GitHub Actions
                        on a public repo)
  MOTION_SYNC_LOG       "full" (default) | "errors" | "none". "none" prints
                        nothing at all; success/failure is signaled only by
                        the process exit code

Guardrails:
  * Only touches tasks that are already auto-scheduled (scheduledStart present
    or schedulingIssue=true) — never switches auto-scheduling on.
  * Echoes existing deadlineType so manager-set HARD deadlines survive.
  * Skips completed tasks and recurring child instances (--include-recurring
    to override).
  * Per-user state avoids re-patching; a task is re-stamped only when its
    resolved schedule changes (e.g. project renamed with a new prefix).
    Delete the state file to force a full re-stamp.
  * Per-key throttling with automatic 429 backoff.
"""

import argparse
import json
import os
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

try:
    import requests
except ImportError:
    sys.exit("Missing dependency: pip install requests")

API_BASE = "https://api.usemotion.com/v1"
SCRIPT_DIR = Path(__file__).resolve().parent
KEYS_FILE = Path(os.environ.get("MOTION_SYNC_KEYS", SCRIPT_DIR / "motion_keys.json"))
STATE_FILE = Path(os.environ.get("MOTION_SYNC_STATE", Path.home() / ".motion_schedule_sync.json"))
THROTTLE_SECONDS = float(os.environ.get("MOTION_SYNC_THROTTLE", "6"))
SEPARATORS = os.environ.get("MOTION_SYNC_SEPARATORS", "+")
PREFIX_LEN = int(os.environ.get("MOTION_SYNC_PREFIX_LEN", "4"))
IGNORE_PREFIXES = {
    p.strip().upper()
    for p in os.environ.get("MOTION_SYNC_IGNORE_PREFIXES", "").split(",") if p.strip()
}
FALLBACK_SCHEDULE = "Work Hours"
# Redact task/project names and emails from logs (set to "1" when logs are
# public, e.g. GitHub Actions on a public repo).
REDACT = os.environ.get("MOTION_SYNC_REDACT", "") == "1"
# Log verbosity: "full" (default), "errors" (WARN/ERROR/ACTION only), or
# "none" (completely silent — the exit code is the only signal).
LOG_LEVEL = os.environ.get("MOTION_SYNC_LOG", "full").lower()


def tref(task: dict) -> str:
    """Loggable task reference; name is hidden when REDACT is on."""
    return task["id"] if REDACT else f"'{task['name']}' [{task['id']}]"

PREFIX_RE = re.compile(
    r"^([A-Za-z0-9]{" + str(PREFIX_LEN) + r"})[" + re.escape(SEPARATORS) + r"] ?"
)

_last_call = 0.0


def _throttle():
    global _last_call
    wait = THROTTLE_SECONDS - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)
    _last_call = time.monotonic()


def api(method: str, path: str, api_key: str, **kwargs):
    _throttle()
    resp = requests.request(
        method, f"{API_BASE}{path}",
        headers={"X-API-Key": api_key, "Content-Type": "application/json"},
        timeout=30, **kwargs,
    )
    if resp.status_code == 429:
        log("WARN", "Rate limited (429); backing off 65s")
        time.sleep(65)
        return api(method, path, api_key, **kwargs)
    if not resp.ok:
        raise RuntimeError(f"{method} {path} -> {resp.status_code}: {resp.text[:300]}")
    return resp.json() if resp.text else {}


def log(level: str, msg: str):
    if LOG_LEVEL == "none":
        return
    if LOG_LEVEL == "errors" and level not in ("ERROR", "WARN", "ACTION"):
        return
    print(f"{datetime.now(timezone.utc).isoformat(timespec='seconds')} [{level}] {msg}", flush=True)


# ---------------------------------------------------------------------------
# Keys and state
# ---------------------------------------------------------------------------
def load_users() -> list:
    if not KEYS_FILE.exists():
        sys.exit(f"Keys file not found: {KEYS_FILE}\nSee the setup notes in this "
                 f"script's docstring. Remember: chmod 600.")
    users = json.loads(KEYS_FILE.read_text()).get("users", [])
    if not users:
        sys.exit(f"No users defined in {KEYS_FILE}")
    return users


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except json.JSONDecodeError:
            log("WARN", f"State file {STATE_FILE} unreadable; starting fresh")
    return {}


def save_state(state: dict):
    STATE_FILE.write_text(json.dumps(state, indent=1))


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------
def resolve_schedule(project_name: str, schedule_lookup: dict):
    """Return (schedule_name_or_None_if_ignored). schedule_lookup maps
    UPPERCASE schedule name -> actual schedule name for this user."""
    m = PREFIX_RE.match(project_name or "")
    if not m:
        return FALLBACK_SCHEDULE            # no parseable prefix
    prefix = m.group(1).upper()
    if prefix in IGNORE_PREFIXES:
        return None                          # hands off entirely
    return schedule_lookup.get(prefix, FALLBACK_SCHEDULE)


# ---------------------------------------------------------------------------
# Per-user processing
# ---------------------------------------------------------------------------
def get_me(api_key: str) -> dict:
    return api("GET", "/users/me", api_key)


def get_schedule_lookup(api_key: str) -> dict:
    data = api("GET", "/schedules", api_key)
    schedules = data if isinstance(data, list) else data.get("schedules", [])
    lookup = {}
    for s in schedules:
        name = s.get("name")
        if name:
            lookup[name.upper()] = name      # case-insensitive match, real name kept
    return lookup


def iter_tasks(api_key: str, assignee_id: str):
    cursor = None
    while True:
        params = {"assigneeId": assignee_id}
        if cursor:
            params["cursor"] = cursor
        page = api("GET", "/tasks", api_key, params=params)
        yield from page.get("tasks", [])
        cursor = page.get("meta", {}).get("nextCursor")
        if not cursor:
            break


def should_consider(task: dict, include_recurring: bool) -> bool:
    if task.get("completed"):
        return False
    if task.get("parentRecurringTaskId") and not include_recurring:
        return False
    if not task.get("scheduledStart") and not task.get("schedulingIssue"):
        return False                         # never switch auto-scheduling ON
    return True


def patch_schedule(task: dict, schedule_name: str, api_key: str, dry_run: bool, user: str):
    # NOTE: the API docs mark workspaceId (and name) as required on PATCH, but
    # the live endpoint rejects workspaceId with 400 "property workspaceId
    # should not exist". Send only what the endpoint actually accepts.
    body = {
        "name": task["name"],                       # accepted; echoed unchanged
        "autoScheduled": {
            "schedule": schedule_name,
            "deadlineType": task.get("deadlineType") or "SOFT",  # preserve HARD
        },
    }
    if task.get("dueDate"):
        body["dueDate"] = task["dueDate"]
    if task.get("startOn"):
        body["autoScheduled"]["startDate"] = task["startOn"]

    if dry_run:
        log("DRY", f"[{user}] Would PATCH {tref(task)} -> '{schedule_name}'")
        return
    api("PATCH", f"/tasks/{task['id']}", api_key, data=json.dumps(body))
    log("OK", f"[{user}] PATCHED {tref(task)} -> '{schedule_name}'")


def run_user(user: dict, state: dict, dry_run: bool, include_recurring: bool):
    name, key = user.get("name", "?"), user.get("api_key", "")
    if not key:
        log("ERROR", f"[{name}] missing api_key; skipping")
        return
    try:
        me = get_me(key)
        schedule_lookup = get_schedule_lookup(key)
    except RuntimeError as e:
        log("ERROR", f"[{name}] auth/setup failed ({e}); skipping this user")
        return
    ident = "(email hidden)" if REDACT else me.get("email")
    log("INFO", f"[{name}] {ident} — schedules: {sorted(schedule_lookup.values())}")

    ustate = state.setdefault(me["id"], {})
    scanned = patched = fell_back = 0

    for task in iter_tasks(key, me["id"]):
        scanned += 1
        if not should_consider(task, include_recurring):
            continue
        project_name = (task.get("project") or {}).get("name")
        target = resolve_schedule(project_name, schedule_lookup)
        if target is None:                   # ignored prefix (e.g. archived)
            continue
        if ustate.get(task["id"]) == target:
            continue                         # already stamped with this schedule
        try:
            patch_schedule(task, target, key, dry_run, name)
            patched += 1
            if target == FALLBACK_SCHEDULE:
                fell_back += 1
            if not dry_run:
                ustate[task["id"]] = target
                save_state(state)            # incremental: crash-safe
        except RuntimeError as e:
            log("ERROR", f"[{name}] {e}")

    log("INFO", f"[{name}] done. scanned={scanned} patched={patched} "
                f"work_hours_fallback={fell_back}")


def run_once(dry_run: bool, include_recurring: bool, only_user: str = None):
    users = load_users()
    if only_user:
        users = [u for u in users if u.get("name", "").lower() == only_user.lower()]
        if not users:
            sys.exit(f"No user named '{only_user}' in {KEYS_FILE}")
    state = load_state()
    for user in users:
        run_user(user, state, dry_run, include_recurring)


def run_test(task_id: str, only_user: str):
    users = load_users()
    match = [u for u in users if u.get("name", "").lower() == (only_user or "").lower()]
    if not match:
        sys.exit(f"--test requires --user NAME matching an entry in {KEYS_FILE}")
    user = match[0]
    key = user["api_key"]
    schedule_lookup = get_schedule_lookup(key)
    task = api("GET", f"/tasks/{task_id}", key)
    project_name = (task.get("project") or {}).get("name")
    target = resolve_schedule(project_name, schedule_lookup)
    if target is None:
        sys.exit(f"Project '{project_name}' has an ignored prefix; pick another task.")
    log("INFO", f"[{user['name']}] Testing PATCH of {tref(task)} -> '{target}'")
    try:
        patch_schedule(task, target, key, dry_run=False, user=user["name"])
        log("OK", "API accepted the update. Confirm in the Motion UI that the task's "
                  "Schedule field shows the expected schedule (the API does not return "
                  "the schedule name, so the UI is the ground truth).")
    except RuntimeError as e:
        log("ERROR", f"Test failed: {e}")
        log("ERROR", "If the error indicates schedules for other users, verify this "
                     "task is assigned to the SAME user whose key is being used.")


def main():
    p = argparse.ArgumentParser(description="Stamp team members' Motion tasks with the "
                                            "schedule named after their project's 3-letter prefix.")
    p.add_argument("--once", action="store_true", help="run one sync pass and exit (cron)")
    p.add_argument("--loop", type=int, metavar="MIN", help="run continuously every MIN minutes")
    p.add_argument("--dry-run", action="store_true", help="log intended changes without patching")
    p.add_argument("--test", metavar="TASK_ID", help="verification: patch one task (use with --user)")
    p.add_argument("--user", metavar="NAME", help="limit run to one user from the keys file")
    p.add_argument("--include-recurring", action="store_true", help="also patch recurring children")
    args = p.parse_args()

    if args.test:
        run_test(args.test, args.user)
    elif args.loop:
        while True:
            run_once(args.dry_run, args.include_recurring, args.user)
            time.sleep(args.loop * 60)
    else:
        run_once(args.dry_run, args.include_recurring, args.user)


if __name__ == "__main__":
    main()
