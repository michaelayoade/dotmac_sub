#!/usr/bin/env python3
"""Run the post-deploy Huawei dual-stack canary evidence gate."""

from __future__ import annotations

import argparse
import json

from app.db import SessionLocal
from app.services.dual_stack_canary import evaluate_dual_stack_canary


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ont-id", required=True)
    parser.add_argument("--probe", action="store_true")
    args = parser.parse_args()

    with SessionLocal() as db:
        result = evaluate_dual_stack_canary(db, args.ont_id, run_probes=args.probe)
    print(json.dumps(result, indent=2, default=str))
    return 0 if result["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
