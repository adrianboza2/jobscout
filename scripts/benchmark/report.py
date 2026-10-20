"""Aggregate benchmark JSONL into a metrics table and a local HTML report."""
import argparse, html, json, sqlite3, statistics as st
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

parser = argparse.ArgumentParser(description=__doc__)
parser.add_argument("out_dir", type=Path)
parser.add_argument("html_path", type=Path)
parser.add_argument("--db", type=Path, default=REPO / "data" / "jobscout.sqlite3")
args = parser.parse_args()

out_dir, html_path = args.out_dir, args.html_path
rows = [json.loads(l) for f in sorted(out_dir.glob("*.jsonl")) for l in f.open()]
models = list(dict.fromkeys(r["model"] for r in rows))
# Titles and human verdicts are read without any write capability.
db = sqlite3.connect(f"{args.db.resolve().as_uri()}?mode=ro", uri=True)
title = {i: t for i, t in db.execute("select id, title from jobs")}
verdict = {i: d for i, d in db.execute("select job_id, decision from job_feedback")}


def ok(r):
    # A heuristic rescue is not the model's answer, so it does not count as ok.
    return r["status"] == "ok" and "+fallback" not in (r.get("used_model") or "")


def auc(pos, neg):
    pairs = [(p > n) + 0.5 * (p == n) for p in pos for n in neg]
    return sum(pairs) / len(pairs) if pairs else None


metrics = []
for m in models:
    mr = [r for r in rows if r["model"] == m]
    good = [r for r in mr if ok(r)]
    real1 = {(r["job_id"]): r for r in good if r["profile"] == "real" and r["rep"] == 1}
    A = [real1[i]["score"] for i in real1 if real1[i]["group"] == "A"]
    N = [real1[i]["score"] for i in real1 if real1[i]["group"] in "BC"]
    contrast = {r["job_id"]: r["score"] for r in good if r["profile"] == "contrast"}
    rep2 = {r["job_id"]: r["score"] for r in good if r["rep"] == 2}
    sens = [real1[i]["score"] - contrast[i] for i in contrast if i in real1]
    drift = [abs(real1[i]["score"] - rep2[i]) for i in rep2 if i in real1]
    a_recs = [real1[i]["rec"] for i in real1 if real1[i]["group"] == "A"]
    metrics.append(dict(
        model=m,
        ok=f"{len(good)}/{len(mr)}",
        secs=round(st.median([r["secs"] for r in mr]), 1),
        mean_A=round(st.mean(A), 1) if A else None,
        mean_neg=round(st.mean(N), 1) if N else None,
        gap=round(st.mean(A) - st.mean(N), 1) if A and N else None,
        auc=round(auc(A, N), 2) if A and N else None,
        A_not_skip=f"{sum(r in ('apply', 'consider') for r in a_recs)}/{len(a_recs)}",
        profile_drop=round(st.mean(sens), 1) if sens else None,
        drift=round(st.mean(drift), 1) if drift else None,
    ))

cols = list(metrics[0])
print("\t".join(cols))
for m in metrics:
    print("\t".join(str(m[c]) for c in cols))

# Per-offer detail for the owner's own review.
offers = []
for g in "ABC":
    for i in dict.fromkeys(r["job_id"] for r in rows if r["group"] == g):
        offers.append((g, i))
cells = {(r["model"], r["job_id"]): r for r in rows if r["profile"] == "real" and r["rep"] == 1}
group_name = {"A": "Tus «apply»", "B": "Tus «consider/reject»", "C": "Probable no-encaje (senior/lead)"}
esc = html.escape
parts = ["<table class='m'><tr>" + "".join(f"<th>{esc(c)}</th>" for c in cols) + "</tr>"]
for m in metrics:
    parts.append("<tr>" + "".join(f"<td>{esc(str(m[c]))}</td>" for c in cols) + "</tr>")
parts.append("</table>")
for g, i in offers:
    parts.append(f"<h3>[{g}] #{i} · {esc(title.get(i, '?'))} <small>({esc(group_name[g])}; tu veredicto: {esc(str(verdict.get(i, '—')))})</small></h3><div class='grid'>")
    for m in models:
        r = cells.get((m, i), {})
        bad = "" if r and ok(r) else " bad"
        body = esc(r.get("rationale") or r.get("error") or "sin respuesta")
        parts.append(f"<div class='card{bad}'><b>{esc(m)}</b><div class='s'>{r.get('score')} · {esc(str(r.get('rec')))}</div><p>{body}</p></div>")
    parts.append("</div>")
legend = ("<p><b>ok</b>: respuestas válidas del modelo (sin rescate heurístico). <b>secs</b>: mediana por oferta. "
          "<b>mean_A / mean_neg</b>: nota media de tus «apply» frente a grupos B+C. <b>gap</b> y <b>auc</b>: cuánto separa "
          "lo bueno de lo malo (auc 1 = siempre pone tus apply por encima; 0,5 = azar). <b>A_not_skip</b>: tus apply que no "
          "descarta. <b>profile_drop</b>: cuánto baja la nota de tus apply con un perfil frontend senior (más = entiende el perfil). "
          "<b>drift</b>: diferencia media al repetir la misma oferta (menos = más estable).</p>")
style = """:root{--bg:#fff;--fg:#1d1d1f;--mut:#666;--card:#f4f4f6;--bad:#fde8e8;--line:#ddd}
@media (prefers-color-scheme:dark){:root{--bg:#141416;--fg:#ececf0;--mut:#9a9aa3;--card:#202024;--bad:#3a1d1d;--line:#333}}
body{background:var(--bg);color:var(--fg);font:14px/1.45 system-ui,sans-serif;margin:0 auto;max-width:1400px;padding:16px}
table.m{border-collapse:collapse;width:100%;overflow-x:auto;display:block}th,td{border:1px solid var(--line);padding:4px 8px;text-align:right}
th:first-child,td:first-child{text-align:left}small{color:var(--mut);font-weight:normal}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(260px,1fr));gap:8px}
.card{background:var(--card);border-radius:8px;padding:8px}.card.bad{background:var(--bad)}.s{font-size:18px;font-weight:600}p{margin:4px 0 0;font-size:13px}"""
html_path.write_text(f"<!doctype html><meta charset=utf-8><meta name=viewport content='width=device-width,initial-scale=1'>"
                     f"<title>Benchmark modelos</title><style>{style}</style><h1>Benchmark de modelos · 2026-10-20</h1>{legend}"
                     + "".join(parts), encoding="utf-8")
print("wrote", html_path)
