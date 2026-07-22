# motion-schedule-sync

Keep per-project time blocks intact in [Motion](https://www.usemotion.com) when a manager creates and prioritizes tasks for the team.

## The problem

Motion resets a task's **Schedule** to *Work Hours* whenever a task is created for, or handed to, another user — and the API forbids setting a custom schedule for anyone but yourself (`Schedule MUST be 'Work Hours' if scheduling the task for another user`). So a manager can set priorities and deadlines, but cannot place a teammate's tasks into that teammate's custom scheduling blocks. The result: one hot project can eat your whole calendar, because everything lands in one undifferentiated Work Hours pool.

## The solution

A division of labor, enforced by a naming convention and a small script:

- **Manager owns priority.** They create tasks in the right project with a priority and (soft) deadline. That's their entire contract.
- **Each member owns allocation.** They create custom schedules (Settings → Schedules) that budget hours per project area — e.g. schedule `DERM` = Mon/Wed 8–12.
- **Projects carry a 4-character prefix** followed by `+ `: `DERM+ Substation study`, `AXIS+ Onboarding`. The `+ ` is only the separator — the schedule name is just the first 4 characters.
- **This script** runs on a schedule under **each member's own API key** (which makes every update self-scheduling, satisfying the API restriction) and stamps each auto-scheduled task with the schedule matching its project prefix. No matching schedule (or no prefix, or no project)? The task is explicitly set to **Work Hours** — a safe, self-healing fallback.

Motion's priority/deadline engine then reorders work *within* each block, so the manager's daily priority pass and each member's time budget coexist without fighting.

## Setup

1. **Adopt the prefix convention** for project names. Four alphanumeric characters, then `+ `. Case-insensitive; a missing space after the `+` still matches.
2. **Each member creates schedules** named after the prefixes they use (`DERM`, `AXIS`, …). Members who skip this simply get Work Hours — nothing breaks.
3. **Each member generates an API key** (Motion: Settings → API).
4. **Fork/clone this repo** and add a repository secret `MOTION_KEYS_JSON` (Settings → Secrets and variables → Actions):

   ```json
   { "users": [
       { "name": "Chris",   "api_key": "..." },
       { "name": "Manager", "api_key": "..." }
   ] }
   ```

5. The included workflow (`.github/workflows/motion-sync.yml`) runs daily at ~7:00 AM Central plus every 15 minutes on weekdays, persisting its state file via the Actions cache. Adjust the UTC crons for your timezone. You can also trigger a run manually from the Actions tab.

### Running locally instead (server/cron)

```bash
pip install requests
# motion_keys.json next to the script, chmod 600
python3 motion_schedule_sync.py --once --dry-run          # preview
python3 motion_schedule_sync.py --test TASK_ID --user You # verify one PATCH
# then cron:
# 0 7 * * *          python3 /path/motion_schedule_sync.py --once >> sync.log 2>&1
# */15 8-18 * * 1-5  python3 /path/motion_schedule_sync.py --once >> sync.log 2>&1
```

## Guardrails

- Only touches tasks that are **already auto-scheduled** (`scheduledStart` present or `schedulingIssue: true`) — never switches auto-scheduling on for reminders or backlog items.
- **Echoes the existing deadline type**, so a manager-set HARD deadline is never silently downgraded to SOFT.
- Skips completed tasks and recurring child instances (set the schedule on the master task once in the UI; `--include-recurring` overrides).
- Per-user state file prevents redundant PATCHes; a task is re-stamped only when its resolved schedule changes.
- Throttled to ~10 requests/min per key, with automatic backoff on 429.
- `MOTION_SYNC_REDACT=1` (set in the workflow) keeps task names, project names, and emails out of the publicly visible Actions logs.
- `MOTION_SYNC_IGNORE_PREFIXES` (e.g. `ZZZ`) marks prefixes the script must never touch — useful for archived projects.

## Limitations & honest caveats

- **The Motion API does not return a task's current schedule**, so the script can't verify its own work through the API — confirm the first `--test` run in the Motion UI.
- **Folders are invisible to the public API** (no endpoint, no `folderId` on projects), which is why mapping keys off project names rather than folder structure.
- **No webhooks** in the public API; the 15-minute poll is the closest available approximation of "on task creation."
- **Hard deadlines override schedules by design** in Motion — a HARD-deadline task may be placed outside its block to hit the date. Feature, not bug; use sparingly.
- GitHub `schedule` triggers commonly fire 5–15 minutes late.
- The API docs bar custom schedules "for another user"; running under each assignee's own key is what keeps updates self-scoped. Verify with `--test` before trusting a full rollout.

## License

MIT. Built because we hit this wall ourselves; PRs welcome — especially if Motion ships folder visibility or webhooks in the public API.
