#!/usr/bin/env python3
"""Seed FII/DII history for March-August 2026 from a transcribed table.

Sensibull's API serves a rolling ~23-day window. The seven months its payload
advertises in `key_list` are visible in the UI but not reachable through any
endpoint shape found, so those months are transcribed from the rendered table.

Transcription is error-prone, so nothing is merged until it has been checked:
the seed overlaps the live API window by about a fortnight, and every field on
every overlapping date must agree before any row is written. If the check
fails the seed is rejected outright rather than partially applied.

    python seed_fii_history.py --check    # validate only, write nothing
    python seed_fii_history.py            # validate, then merge on success
"""
import argparse
import sys
from pathlib import Path

import fetch

SEED = Path(__file__).resolve().parent / "seed" / "fii_dii_mar_aug_2026.txt"
FIELDS = ["fii_call_oi_chg", "fii_put_oi_chg", "fii_fut_amt",
          "fii_fut_oi_chg", "fii_fut_oi", "fii_net", "dii_net"]


def parse_value(tok):
    """'-2.09L' -> (-209000.0, True), '1,234' -> (1234.0, False).

    The flag matters: a lakh-formatted cell carries only about three
    significant figures, so it cannot be compared as tightly as one printed in
    full. Sensibull prints 108,625 as "1.09L".
    """
    tok = tok.strip().replace(",", "")
    if tok.upper().endswith("L"):
        return float(tok[:-1]) * 100_000, True
    return float(tok), False


def load_seed():
    rows = []
    for line in SEED.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("|")
        if len(parts) != 8:
            raise SystemExit("malformed seed line: " + line)
        row, coarse = {"date": parts[0]}, set()
        for name, tok in zip(FIELDS, parts[1:]):
            val, was_lakh = parse_value(tok)
            row[name] = round(val, 2)
            if was_lakh:
                coarse.add(name)
        row["_coarse"] = coarse
        rows.append(row)
    return rows


def validate(seed_rows, api_rows):
    """Every shared date must match. The table rounds, so tolerances mirror how
    it prints: whole crore for amounts, and lakh-rounded OI to 3 s.f."""
    api = {r["date"]: r for r in api_rows}
    shared = [r for r in seed_rows if r["date"] in api]
    problems = []
    for row in shared:
        live = api[row["date"]]
        for f in FIELDS:
            want, got = row.get(f), live.get(f)
            if want is None or got is None:
                continue
            # Tolerance follows how the cell was PRINTED, not which field it
            # is: anything shown in lakhs has ~3 s.f. and nothing more.
            coarse = f in row.get("_coarse", ())
            tol = max(abs(got) * (0.01 if coarse else 0.002), 1.0)
            if abs(want - got) > tol:
                problems.append((row["date"], f, want, got))
    return shared, problems


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true",
                    help="validate against the live window and stop")
    args = ap.parse_args()

    seed_rows = load_seed()
    api_rows, _, _ = fetch._sensibull_fii_dii()
    shared, problems = validate(seed_rows, api_rows)

    print("seed rows      : %d  (%s .. %s)"
          % (len(seed_rows), seed_rows[-1]["date"], seed_rows[0]["date"]))
    print("live api rows  : %d" % len(api_rows))
    print("overlapping    : %d dates, %d values"
          % (len(shared), len(shared) * len(FIELDS)))

    if problems:
        print("\nMISMATCHES (%d) - refusing to merge:" % len(problems))
        for d, f, want, got in problems[:20]:
            print("   %s %-18s seed %-12s api %s" % (d, f, want, got))
        return 1
    print("overlap agrees on every field.")

    if args.check:
        return 0

    live_dates = {x["date"] for x in api_rows}
    new = [{k: v for k, v in r.items() if k != "_coarse"}
           for r in seed_rows if r["date"] not in live_dates]
    hist = fetch.merge_rows(fetch.load_history(), new)
    fetch.save_history(hist)
    print("\nmerged %d rows the API does not cover; history now %d rows."
          % (len(new), len(hist["rows"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
