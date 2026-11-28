"""Print history: the printer cloud's task list is the truth; Druckwerk's own jobs only say who started a task.

attribute(tasks, jobs) is pure, so tests cover the rule without a server.
"""
import re

# A task belongs to a job when both ran on the same printer, a name of the job (file or source) equals a name of
# the task (title or design title, extensions and case ignored) and the task started 60 s before to 15 min after the job.
# Each task and each job is used once; the closest start wins, so two prints of one file go to the right people.
EARLY, LATE = 60, 900
JOB_STATES = ("sending", "starting", "printing", "done", "failed", "cancelled")


def key(name):
    name = re.sub(r"(\.gcode)?\.3mf$|\.gcode$", "", str(name or "").strip().lower())
    return re.sub(r"[^a-z0-9]+", "", name)


def names(*values):
    return {k for k in map(key, values) if k}


def attribute(tasks, jobs):
    """{task id: job} for the tasks that a job explains."""
    pairs = []
    for t in tasks:
        if t.get("started_at") is None:
            continue
        tn = names(t.get("title"), t.get("design_title"))
        for j in jobs:
            delta = t["started_at"] - j["started_at"]
            if j["printer"] == t["serial"] and -EARLY <= delta <= LATE and tn & names(j.get("file"), j.get("source")):
                pairs.append((abs(delta), str(t["id"]), t, j))
    used_tasks, used_jobs, out = set(), set(), {}
    for _, tid, t, j in sorted(pairs, key=lambda p: p[:2]):
        if tid not in used_tasks and j["id"] not in used_jobs:
            used_tasks.add(tid)
            used_jobs.add(j["id"])
            out[t["id"]] = j
    return out


def jobs_of(queue):
    """Jobs that reached a printer, running or finished."""
    with queue.lock:
        return [dict(j) for j in queue.jobs + queue.history if j.get("printer") and j.get("started_at") and j["state"] in JOB_STATES]


JOB_FIELDS = ("id", "name", "state", "error", "file", "source", "target", "model", "filaments", "options", "spools_used",
              "grams", "filament_grams", "print_seconds", "cost", "plate_png", "slice_job", "slice", "owner_user_id", "owner_name",
              "created_at", "started_at", "finished_at")
FINAL = ("done", "failed", "cancelled")
STATUS = {"done": "finished", "failed": "failed", "cancelled": "cancelled"}


def job_view(job):
    """What the detail view may show of a job; absent fields stay absent."""
    return {k: job[k] for k in JOB_FIELDS if job.get(k) is not None}


def item(task, job, printer):
    return dict(task, printer_name=printer.get("name") or task["serial"], job_id=job and job["id"],
                user_id=job and job.get("owner_user_id"), user_name=job and (job.get("owner_name") or None),
                cost=job and job.get("cost"), source="druckwerk" if job else "cloud", job=job and job_view(job),
                when=task.get("started_at") or task.get("ended_at"))


def job_item(job, printer):
    """A finished Druckwerk job that no cloud task explains (never sent, cancelled, or the cloud has no record)."""
    serial = job.get("printer") or (job["target"] if job.get("target") != "any" else "")
    name = (printer or {}).get("name") or ("Any " + job["model"] if job.get("target") == "any" and job.get("model") else serial)
    return {"id": "job-" + job["id"], "serial": serial, "title": job.get("name") or job.get("file"), "status": STATUS[job["state"]],
            "started_at": job.get("started_at"), "ended_at": job.get("finished_at"), "seconds": job.get("print_seconds"),
            "grams": job.get("grams"), "cover_url": None, "printer_name": name or None, "job_id": job["id"],
            "user_id": job.get("owner_user_id"), "user_name": job.get("owner_name") or None, "cost": job.get("cost"),
            "source": "job", "error": job.get("error"), "job": job_view(job),
            "when": job.get("started_at") or job.get("finished_at") or job.get("created_at")}
