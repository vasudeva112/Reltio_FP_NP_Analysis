#!/usr/bin/env python3
"""
Generates a large synthetic entities.json (JSON array, Reltio export layout) for performance tests of the
FN analysis, using the validation rules (fpnp/reltio_testdata.py). A configurable share of entities gets a
near-duplicate (spelling variant, formatting difference, one-sided null), so the run has real candidates.

    python scripts/generate_scale_data.py --entities 200000 --out output/scale/entities.json
    python fpnp_analyzer.py reltio-fn --set L3_CONFIG_FILE=output/scale/validation_rules.json ^
        --set ENTITY_EXPORT_PATHS=output/scale/entities.json --set ENTITY_TYPES= --set REPORT_FILES= ^
        --set OUTPUT_DIR=output/scale/report --set RESUME=no --set WORKERS=4

Synthetic data says nothing about production FP / FN rates - it measures throughput and memory only.
"""

import argparse
import json
import os
import random
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fpnp import reltio_testdata as td  # noqa: E402

FIRST = ["James", "Mary", "John", "Patricia", "Robert", "Jennifer", "Michael", "Linda", "William", "Elizabeth",
         "David", "Barbara", "Richard", "Susan", "Joseph", "Jessica", "Thomas", "Sarah", "Charles", "Karen",
         "Christopher", "Nancy", "Daniel", "Lisa", "Matthew", "Betty", "Anthony", "Margaret", "Mark", "Sandra",
         "Katherine", "Catherine", "Jonathan", "Jonathon", "Steven", "Stephen", "Ahmed", "Ahmad", "Priya", "Wei"]
LAST = ["Smith", "Johnson", "Williams", "Brown", "Jones", "Garcia", "Miller", "Davis", "Rodriguez", "Martinez",
        "Hernandez", "Lopez", "Gonzalez", "Wilson", "Anderson", "Thomas", "Taylor", "Moore", "Jackson", "Martin",
        "Lee", "Perez", "Thompson", "White", "Harris", "Sanchez", "Clark", "Ramirez", "Lewis", "Robinson",
        "Walker", "Young", "Allen", "King", "Wright", "Scott", "Torres", "Nguyen", "Hill", "Flores", "Patel", "Khan"]
STREETS = ["Main St", "Oak Ave", "Pine Rd", "Maple Dr", "Cedar Ln", "Elm St", "Lake Rd", "Hill St"]


def variant(rng, e):
    fn, ln = e["fn"], e["ln"]
    k = rng.random()
    if k < 0.3:
        fn = fn.upper()
    elif k < 0.55 and len(ln) > 4:
        i = rng.randrange(1, len(ln) - 1)
        ln = ln[:i] + ln[i + 1] + ln[i] + ln[i + 2:]            # transposition
    elif k < 0.75:
        fn = fn[:-1] if len(fn) > 4 else fn
    return dict(e, fn=fn, ln=ln, mn=None if rng.random() < 0.5 else e["mn"])


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--entities", type=int, default=100_000)
    ap.add_argument("--dup-rate", type=float, default=0.1)
    ap.add_argument("--out", default="output/scale/entities.json")
    ap.add_argument("--seed", type=int, default=7)
    a = ap.parse_args(argv)
    rng = random.Random(a.seed)
    os.makedirs(os.path.dirname(a.out) or ".", exist_ok=True)
    with open(os.path.join(os.path.dirname(a.out) or ".", "validation_rules.json"), "w", encoding="utf-8") as fh:
        json.dump(td.rules_config(), fh)
    n = 0
    with open(a.out, "w", encoding="utf-8") as fh:
        fh.write("[\n")
        while n < a.entities:
            base = {"fn": rng.choice(FIRST), "ln": rng.choice(LAST),
                    "dob": f"19{rng.randint(40, 99)}-{rng.randint(1, 12):02d}-{rng.randint(1, 28):02d}",
                    "mn": rng.choice(["A", "B", "C", None, None]), "npi": f"{rng.randint(10**9, 10**10 - 1)}",
                    "addr": (f"{rng.randint(1, 9999)} {rng.choice(STREETS)}", "City", f"{rng.randint(10000, 99999)}"),
                    "phone": f"{rng.randint(200, 999)}555{rng.randint(1000, 9999)}"}
            copies = [base] + ([variant(rng, base)] if rng.random() < a.dup_rate else [])
            for e in copies:
                if n >= a.entities:
                    break
                ent = td.hcp(f"s{n:08d}", e["fn"], e["ln"], e["dob"], mn=e["mn"], npi=e["npi"], addr=e["addr"],
                             phones=[e["phone"]])
                fh.write(("," if n else "") + json.dumps(ent, separators=(",", ":")) + "\n")
                n += 1
        fh.write("]\n")
    print(f"{n} entities -> {a.out}")


if __name__ == "__main__":
    main()
