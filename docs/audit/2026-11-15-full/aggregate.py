#!/usr/bin/env python3
"""Aggregate lane reports for the 2026-11-15 full audit.

Normalizes the finding schema defensively (the skill warns that lane reports
arrive with severity/file/fix_hint spelled differently), reconciles the union of
reviewed_files against the tracked-file inventory, and prints:
  - per-lane severity counts
  - the ranked finding list (P0 -> P3)
  - inventory gaps (files no lane reviewed)
Usage: python3 docs/audit/2026-11-15-full/aggregate.py [--json out.json]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[3]
AUDIT = ROOT / "docs" / "audit" / "2026-11-15-full"
LANES = [
    "L01-auth-config-backup", "L02-accounts-storage", "L03-tasks-runtime",
    "L04-proxies", "L05-icloud", "L06-mail", "L07-panels", "L08-frontend",
    "L09-gates", "L10-data-migrations", "L11-platform-chatgpt",
    "L12-platform-grok-kiro-aws", "L13-services-misc", "L14-docs-deploy",
    "L15-tests-deadcode", "L16-core-runtime", "L17-scripts",
]
SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def norm_sev(value) -> str:
    text = str(value or "").strip().upper()
    for key in ("P0", "P1", "P2", "P3"):
        if text.startswith(key):
            return key
    # tolerate "critical/high/medium/low"
    return {"CRITICAL": "P0", "HIGH": "P1", "MEDIUM": "P2", "LOW": "P3"}.get(text, "P2")


def norm_str(value) -> str:
    if value is None:
        return ""
    if isinstance(value, (list, tuple)):
        return "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def norm_loc(finding: dict) -> tuple[str, int]:
    loc = finding.get("file") or finding.get("loc") or finding.get("path") or ""
    line = finding.get("line") or finding.get("lineno") or 0
    if isinstance(loc, dict):
        line = loc.get("line", line)
        loc = loc.get("file", "")
    try:
        line = int(line)
    except (TypeError, ValueError):
        line = 0
    return str(loc), line


def load(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except json.JSONDecodeError as exc:
        print(f"!! {path.name}: invalid JSON ({exc})", file=sys.stderr)
        return {}


def main() -> int:
    inventory = load(AUDIT / "inventory.json")
    tracked = {f for key, files in inventory.items() if key != "_meta" for f in files}
    reviewed: set[str] = set()
    rows = []
    counts: dict[str, dict[str, int]] = {}
    missing = []

    for lane in LANES:
        path = AUDIT / f"{lane}.json"
        if not path.exists():
            missing.append(lane)
            continue
        data = load(path)
        counts[lane] = {k: 0 for k in SEV_ORDER}
        for f in data.get("findings") or []:
            if not isinstance(f, dict):
                continue
            sev = norm_sev(f.get("severity"))
            loc, line = norm_loc(f)
            counts[lane][sev] += 1
            rows.append({
                "lane": lane,
                "id": norm_str(f.get("id")),
                "severity": sev,
                "title": norm_str(f.get("title")),
                "file": loc,
                "line": line,
                "class": norm_str(f.get("class")),
                "impact": norm_str(f.get("impact")),
                "fix_hint": norm_str(f.get("fix_hint")),
                "evidence": norm_str(f.get("evidence")),
            })
        for name in data.get("reviewed_files") or []:
            reviewed.add(str(name))

    rows.sort(key=lambda r: (SEV_ORDER[r["severity"]], r["lane"], r["file"], r["line"]))
    gaps = sorted(tracked - reviewed)

    print("== per-lane severity ==")
    for lane in LANES:
        if lane in counts:
            c = counts[lane]
            print(f"{lane:28s} P0={c['P0']} P1={c['P1']} P2={c['P2']} P3={c['P3']}")
        else:
            print(f"{lane:28s} MISSING REPORT")
    total = {k: sum(c[k] for c in counts.values()) for k in SEV_ORDER}
    print(f"\nTOTAL P0={total['P0']} P1={total['P1']} P2={total['P2']} P3={total['P3']}")
    print(f"lanes with report: {len(counts)}/{len(LANES)}; missing: {missing}")
    print(f"reviewed_files union: {len(reviewed)}; inventory: {len(tracked)}; unreviewed: {len(gaps)}")

    print("\n== findings ranked ==")
    for r in rows:
        print(f"[{r['severity']}] {r['lane']}/{r['id']} {r['title']}  @ {r['file']}:{r['line']} ({r['class']})")
        if r["impact"]:
            print(f"      impact: {r['impact'][:220]}")
        if r["fix_hint"]:
            print(f"      fix:    {r['fix_hint'][:220]}")

    if gaps:
        print(f"\n== unreviewed files (first 40 of {len(gaps)}) ==")
        for g in gaps[:40]:
            print(" ", g)

    out = Path(sys.argv[sys.argv.index("--json") + 1]) if "--json" in sys.argv else AUDIT / "aggregate.json"
    out.write_text(json.dumps({
        "total": total, "counts": counts, "findings": rows,
        "reviewed_files": sorted(reviewed), "unreviewed": gaps, "missing_lanes": missing,
    }, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nwrote {out}")
    return 0 if not missing else 1


if __name__ == "__main__":
    raise SystemExit(main())
