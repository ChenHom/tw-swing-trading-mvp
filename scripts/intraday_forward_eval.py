#!/usr/bin/env python3
"""Offline preregistered market execution comparison; never submits broker orders.

Input JSON:
{
  "manifest": {...},
  "opportunities": [{...}],
  "books": [MarketBook.as_dict(), ...],
  "observations": [ObservationEvent.as_dict(), ...],
  "data_health": "UNKNOWN"    // default, fail-closed
}
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from src.application.research.intraday_evaluation import (
    Opportunity, StudyManifest, evaluate, write_report,
)
from src.market_data.intraday_book import MarketBook
from src.market_data.intraday_observations import ObservationEvent


def run(input_path: Path, report_dir: Path) -> Path:
    data = json.loads(input_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("study input must be a JSON object")
    manifest = StudyManifest(**data["manifest"])
    opportunities = [Opportunity(**o) for o in data["opportunities"]]
    books = [MarketBook(**b) for b in data.get("books", [])]
    events = [ObservationEvent(**e) for e in data.get("observations", [])]
    return write_report(
        evaluate(manifest, opportunities, books, events,
                 data_health=data.get("data_health", "UNKNOWN")),
        report_dir,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description="Offline study; returns no broker order intents")
    p.add_argument("--input", required=True, type=Path)
    p.add_argument("--output-dir", default=Path("artifacts/reports/intraday-evaluation"), type=Path)
    args = p.parse_args()
    print(run(args.input, args.output_dir))
