#!/usr/bin/env python3
"""Back up the scraped handles to local files in the repo.

MinIO is the app's source of truth, but when it's down or degraded the handles a
scrape collected still live in the RUNNING server's in-memory progress. This pulls
them from the live app (per account, with bios) and writes two files next to this
script, so nothing is lost while MinIO is unavailable:

    scraped_handles_backup.json   full data: per-account + combined, with bios
    scraped_handles_backup.txt    one unique @handle per line (easy to re-import)

Usage:
    python export_scraped_handles.py                 # app on http://127.0.0.1:4000
    python export_scraped_handles.py --url http://127.0.0.1:8000
    python export_scraped_handles.py --out C:/some/dir

The app must be running (it holds the handles in memory).
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path


def _get(base: str, path: str):
    with urllib.request.urlopen(base + path, timeout=15) as resp:
        return json.load(resp)


def main() -> int:
    ap = argparse.ArgumentParser(description="Back up scraped handles from the running app.")
    ap.add_argument("--url", default="http://127.0.0.1:4000", help="app base URL")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent), help="output directory")
    args = ap.parse_args()

    base = args.url.rstrip("/") + "/api"
    outdir = Path(args.out)

    try:
        accts = _get(base, "/accounts")
    except Exception as exc:  # noqa: BLE001
        print(f"Could not reach the app at {args.url} — is it running?  ({exc})", file=sys.stderr)
        return 1
    accts = accts.get("accounts", accts if isinstance(accts, list) else [])
    label_of = {a["id"]: (a.get("label") or a.get("login") or "") for a in accts}

    per_account: list[dict] = []
    combined: dict[str, dict] = {}          # username_lower -> {username, bio}

    for aid, label in label_of.items():
        try:
            prog = _get(base, f"/automation/visit-progress?account_id={aid}")
        except Exception:  # noqa: BLE001 — an account with no run just 404s / errors
            continue
        names = prog.get("names") or []
        profiles = prog.get("profiles") or []
        if not (names or profiles):
            continue

        # Merge every profile's bio into the combined map (first real bio wins).
        for prof in profiles:
            user = str(prof.get("username") or "").strip()
            if not user:
                continue
            key = user.lower()
            bio = prof.get("bio")
            if key not in combined:
                combined[key] = {"username": user, "bio": bio}
            elif not combined[key].get("bio") and bio:
                combined[key]["bio"] = bio
        # Names collected without a profile record still count as handles.
        for name in names:
            user = str(name or "").strip()
            if user and user.lower() not in combined:
                combined[user.lower()] = {"username": user, "bio": None}

        handles = profiles if profiles else [{"username": n, "bio": None} for n in names]
        per_account.append({
            "account_id": aid,
            "label": label,
            "count": len(handles),
            "handles": handles,
        })

    combined_list = sorted(combined.values(), key=lambda r: r["username"].lower())
    payload = {
        "exported_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "in-memory scrape progress from the running app (MinIO backup)",
        "accounts_with_data": len(per_account),
        "total_unique_handles": len(combined_list),
        "per_account": per_account,
        "all_handles": combined_list,
    }

    json_path = outdir / "scraped_handles_backup.json"
    txt_path = outdir / "scraped_handles_backup.txt"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text("\n".join(r["username"] for r in combined_list) + "\n", encoding="utf-8")

    print(f"accounts with data : {len(per_account)}")
    for acct in per_account:
        print(f"  - {acct['account_id']} ({acct['label']}): {acct['count']} handles")
    print(f"unique handles     : {len(combined_list)}")
    print(f"wrote              : {json_path.name}")
    print(f"                     {txt_path.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
