"""Write structured quotes to CSV or JSON."""
from __future__ import annotations

import csv
import json
from typing import List

from .models import FIELDNAMES, Quote


def to_csv(quotes: List[Quote], path: str) -> None:
    with open(path, "w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=FIELDNAMES)
        writer.writeheader()
        for q in quotes:
            writer.writerow(q.as_row())


def to_json(quotes: List[Quote], path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump([q.as_row() for q in quotes], f, ensure_ascii=False, indent=2)


def write(quotes: List[Quote], path: str, fmt: str = "csv") -> None:
    if fmt == "csv":
        to_csv(quotes, path)
    elif fmt == "json":
        to_json(quotes, path)
    else:
        raise ValueError(f"Unknown output format: {fmt!r} (use 'csv' or 'json')")
