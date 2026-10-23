"""Score the benchmark selection with one model through jobscout's own path.

Runs evaluate_job against a private copy of the database, so the real
data/jobscout.sqlite3 is never written. One JSON line per call.
"""
import argparse, json, shutil, sys, time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(REPO / "src"))
from jobscout.config import Settings
from jobscout.db import connect, get_job, init_db
from jobscout.scoring import evaluate_job, load_profile

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("model")
parser.add_argument("out_dir", type=Path)
parser.add_argument("--variant", default="", help="model variant; empty means none")
# The 2026-10-20 offers are the default so later runs compare like with like.
parser.add_argument("--selection", type=Path, default=HERE / "selection-2026-10-20.json")
parser.add_argument("--contrast-profile", type=Path, default=HERE / "contrast_profile.yaml")
# Free OpenCode models share one rolling rate limit; without waiting, a model's
# result depends on when it ran rather than on how well it scores.
parser.add_argument("--quota-wait", type=int, default=0,
                    help="seconds to wait and retry after 'Free usage limit reached'; 0 disables")
parser.add_argument("--quota-retries", type=int, default=12)
args = parser.parse_args()
QUOTA_MARK = "Free usage limit reached"

model, out_dir = args.model, args.out_dir
slug = model.replace("/", "_")
work = out_dir / slug
work.mkdir(parents=True, exist_ok=True)
db_copy = work / "bench.sqlite3"
shutil.copyfile(REPO / "data" / "jobscout.sqlite3", db_copy)

settings = Settings.from_env(dotenv_path=str(REPO / ".env")).with_overrides(
    db_path=db_copy, opencode_model=model, opencode_variant=args.variant,
    opencode_workdir=str(work),
)
real = load_profile(REPO / "profile.yaml")
contrast = load_profile(args.contrast_profile)
sel = json.loads(args.selection.read_text())

plan = [(g, i, "real", 1) for g in "ABC" for i in sel[g]]
plan += [(g, i, "contrast", 1) for g, i in [("A", i) for i in sel["A"]]]
plan += [("A", i, "real", 2) for i in sel["A"][:4]]

conn = connect(db_copy)
# The copy may predate a schema migration; bring it up to date before scoring.
init_db(conn)
with open(out_dir / f"{slug}.jsonl", "w") as out:
    for group, job_id, prof, rep in plan:
        job = get_job(conn, job_id)
        waits = 0
        while True:
            t0 = time.monotonic()
            try:
                ev = evaluate_job(conn, job, real if prof == "real" else contrast,
                                  scorer_name="opencode", settings=settings)
                row = dict(status=str(ev.status), score=ev.overall_score,
                           rec=str(ev.recommendation), conf=ev.confidence,
                           used_model=ev.model, rationale=ev.rationale,
                           error=(ev.error or "")[:300])
            except Exception as exc:  # keep going: one crash must not end the run
                row = dict(status="crash", error=repr(exc)[:300])
            if not (args.quota_wait and QUOTA_MARK in row["error"] and waits < args.quota_retries):
                break
            waits += 1
            print(f"{model}: quota hit on job {job_id}, waiting {args.quota_wait}s ({waits})", flush=True)
            time.sleep(args.quota_wait)
        row.update(model=model, group=group, job_id=job_id, profile=prof,
                   rep=rep, secs=round(time.monotonic() - t0, 1), quota_waits=waits)
        out.write(json.dumps(row, ensure_ascii=False) + "\n"); out.flush()
conn.close()
print("done", model)
