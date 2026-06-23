#!/usr/bin/env python3
"""
sync_port_to_supabase.py — push portfolio_data.json → Supabase, server-side.

Runs in GitHub Actions (no app needed). Uses the SERVICE ROLE key (bypasses RLS),
so the anon key is NOT required for this path. Mirrors the app's reconcile model:
upsert current positions/transactions + the snapshot singleton, then delete rows
that no longer exist locally (except txn:- history and the snapshot).

Env (set as repo secrets):
  SUPABASE_URL          e.g. https://xxxx.supabase.co   (the ANALYTICS+PORT project)
  SUPABASE_SERVICE_KEY  the service_role key
"""
import json, os, sys, urllib.request, urllib.error
from datetime import datetime, timezone

URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
KEY = os.environ.get("SUPABASE_SERVICE_KEY", "")
TABLE = "portfolio_positions"
DATA = "data/portfolio_data.json"

def _req(method, path, body=None, headers=None):
    h = {"apikey": KEY, "Authorization": f"Bearer {KEY}", "Content-Type": "application/json"}
    if headers: h.update(headers)
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(URL + path, data=data, headers=h, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()

def main():
    if not URL or not KEY:
        print("✗ SUPABASE_URL / SUPABASE_SERVICE_KEY not set"); return 1
    if not os.path.exists(DATA):
        print(f"No {DATA} — nothing to sync."); return 0
    snap = json.load(open(DATA))
    positions = snap.get("portfolio", []) if isinstance(snap, dict) else (snap if isinstance(snap, list) else [])
    transactions = snap.get("transactions", []) if isinstance(snap, dict) else []
    now = datetime.now(timezone.utc).isoformat()

    rows, local_ids = [], set()
    # Positions (kind=entry).
    for p in positions:
        pid = p.get("id")
        if not pid: continue
        rec = dict(p); rec["_kind"] = "entry"
        rows.append({"id": pid, "position": rec, "updated_at": now}); local_ids.add(pid)
    # Transactions (immutable; txn:- prefix so reconcile keeps them).
    for t in transactions:
        tid = t.get("id") or ""
        if tid and not str(tid).startswith("txn:"): tid = "txn:" + str(tid)
        if not tid: continue
        rec = dict(t); rec["_kind"] = "transaction"
        rows.append({"id": tid, "position": rec, "updated_at": now}); local_ids.add(tid)
    # Snapshot singleton (cash + goodGlobe live here for the app to read).
    if isinstance(snap, dict):
        rows.append({"id": "__portfolio_snapshot__", "position": {
            "_kind": "snapshot", "cashPosition": snap.get("cashPosition"),
            "goodGlobeIndex": snap.get("goodGlobeIndex"), "goodGlobeCurve": snap.get("goodGlobeCurve"),
            "generatedAt": snap.get("generatedAt"),
        }, "updated_at": now})
        local_ids.add("__portfolio_snapshot__")

    # Upsert.
    sent = 0
    for i in range(0, len(rows), 100):
        chunk = rows[i:i+100]
        st, body = _req("POST", f"/rest/v1/{TABLE}?on_conflict=id", chunk,
                        {"Prefer": "resolution=merge-duplicates,return=minimal"})
        if st in (200, 201, 204): sent += len(chunk)
        else: print(f"  upsert chunk {i} → HTTP {st}: {body[:200]}")

    # Reconcile-delete: remove rows no longer present locally (keep txn:- + snapshot).
    st, body = _req("GET", f"/rest/v1/{TABLE}?select=id", None)
    deleted = 0
    if st == 200:
        try: existing = {r["id"] for r in json.loads(body)}
        except Exception: existing = set()
        stale = [i for i in existing if i not in local_ids
                 and not str(i).startswith("txn:") and i != "__portfolio_snapshot__"]
        for j in range(0, len(stale), 50):
            chunk = stale[j:j+50]
            inlist = ",".join(f'"{x}"' for x in chunk)
            st2, _ = _req("DELETE", f"/rest/v1/{TABLE}?id=in.({inlist})", None, {"Prefer": "return=minimal"})
            if st2 in (200, 204): deleted += len(chunk)

    print(f"✓ portfolio → Supabase: upserted {sent}, removed {deleted} stale")
    return 0

if __name__ == "__main__":
    sys.exit(main())
