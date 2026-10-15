"""Re-verify eval/sample_150.json against its invariants (read-only, no API).

Checks: exactly 150 ids, no duplicates, no dev_10 id, every db_id present, and the
stored sha256 matches a fresh recomputation. Prints counts per db_id and per
difficulty. Exits non-zero on any failure.

Usage (PowerShell):
    uv run python -m eval.check_sample
"""

from __future__ import annotations

import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DATA = PROJECT_ROOT / "data" / "mini_dev_postgresql.json"
SAMPLE = PROJECT_ROOT / "eval" / "sample_150.json"
DEV10 = PROJECT_ROOT / "eval" / "dev_10.json"


def sha256_of_ids(ids: list[int]) -> str:
    """The sample file's sha256 convention: comma-joined sorted decimal ids, no spaces."""
    return hashlib.sha256(",".join(str(i) for i in sorted(ids)).encode("utf-8")).hexdigest()


def main() -> int:
    doc = json.loads(SAMPLE.read_text(encoding="utf-8"))
    ids = doc["question_ids"]
    dev10 = set(json.loads(DEV10.read_text(encoding="utf-8"))["question_ids"])
    questions = {q["question_id"]: q for q in json.loads(DATA.read_text(encoding="utf-8"))}

    failures: list[str] = []

    if len(ids) != 150:
        failures.append(f"expected 150 ids, got {len(ids)}")
    dupes = [i for i, c in Counter(ids).items() if c > 1]
    if dupes:
        failures.append(f"duplicate ids: {dupes}")
    bad_dev = sorted(set(ids) & dev10)
    if bad_dev:
        failures.append(f"dev_10 ids present: {bad_dev}")
    unknown = [i for i in ids if i not in questions]
    if unknown:
        failures.append(f"ids not in dataset: {unknown}")

    recomputed = sha256_of_ids(ids)
    if recomputed != doc.get("sha256"):
        failures.append(f"sha256 mismatch: stored {doc.get('sha256')} vs computed {recomputed}")

    all_db_ids = {q["db_id"] for q in questions.values()}
    present_db_ids = {questions[i]["db_id"] for i in ids if i in questions}
    missing_db = sorted(all_db_ids - present_db_ids)
    if missing_db:
        failures.append(f"db_ids with no question in the sample: {missing_db}")

    per_db = Counter(questions[i]["db_id"] for i in ids if i in questions)
    per_diff = Counter(questions[i]["difficulty"] for i in ids if i in questions)

    print(f"sample file: {SAMPLE.name}")
    print(f"ids: {len(ids)}   seed: {doc.get('seed')}   created: {doc.get('created')}")
    print(f"sha256 stored:   {doc.get('sha256')}")
    print(f"sha256 computed: {recomputed}")
    print(f"db_ids present:  {len(present_db_ids)}/{len(all_db_ids)}")
    print("\nper difficulty:")
    for d in ("simple", "moderate", "challenging"):
        print(f"  {d:<12} {per_diff.get(d, 0)}")
    print("\nper db_id:")
    for db in sorted(per_db):
        print(f"  {db:<26} {per_db[db]}")

    if failures:
        print("\nFAIL:")
        for f in failures:
            print(f"  - {f}")
        return 1
    print("\nPASS: all invariants hold.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
