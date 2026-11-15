"""Print the conductor board in a terminal. Usage: python3 scripts/field-test/board.py [port]"""
import json, sys, time, urllib.request

PORT = sys.argv[1] if len(sys.argv) > 1 else "7700"
d = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/api/runs"))
print(time.strftime("%H:%M:%S"), "needs_you=%s" % d["stats"].get("needs_you"), "worktrees=%s" % d["stats"].get("worktrees"))
for r in sorted(d["runs"], key=lambda r: r["id"]):
    ov = ",".join("#%d" % o["run_id"] for o in r["overlaps"])
    print(f'  #{r["id"]} {r["state"]:13} ${r["cost_usd"]:.2f} a{r["attempt"]} tests={r["test_status"]} {r["diff_stat"] or ""} ov=[{ov}] | {(r["activity"] or "")[:70]}')
    if r["error"]: print("      error:", r["error"][:150])
    if r["question"]: print("      QUESTION:", r["question"][:300])
