"""Reproducible offline orchestration and relational exports."""

import argparse
import asyncio
import csv
import hashlib
import json
from pathlib import Path

from .collection import Collector, FixtureTransport
from .entities import deduplicate
from .enrichment import enrich
from .normalize import normalize_elements


async def build(collector, collection_url):
    response = await collector.get(collection_url)
    if response.status != 200:
        raise ValueError("Collection failed; refusing to report an empty market")
    raw = json.loads(response.body)
    normalized, rejected = normalize_elements(raw)
    businesses, contacts, conflicts = deduplicate(normalized)
    enriched, issues = await enrich(businesses, collector)
    merged = {}
    for contact in contacts + enriched:
        key = (contact["business_id"], contact["kind"], contact["value"])
        merged.setdefault(key, set()).add(contact["evidence"])
    contacts = [{"business_id": bid, "kind": kind, "value": value,
                 "evidence": sorted(evidence), "status": "unverified"}
                for (bid, kind, value), evidence in sorted(merged.items())]
    ids = {b["business_id"] for b in businesses}
    if len(ids) != len(businesses) or any(c["business_id"] not in ids for c in contacts):
        raise ValueError("Relational integrity failure")
    counts = {"input_observations": len(raw["elements"]), "accepted_observations": len(normalized),
              "rejected_observations": len(rejected), "retained_entities": len(businesses),
              "merged_observations": len(normalized) - len(businesses), "contact_records": len(contacts),
              "field_conflicts": len(conflicts), "enrichment_failures": len(issues)}
    counts["entities_with_phone"] = len({c["business_id"] for c in contacts if c["kind"] == "phone"})
    counts["entities_with_email"] = len({c["business_id"] for c in contacts if c["kind"] == "email"})
    checks = {"observation_accounting": counts["input_observations"] == len(normalized) + len(rejected),
              "entity_accounting": len(normalized) == len(businesses) + counts["merged_observations"],
              "unique_entity_ids": len(ids) == len(businesses), "contact_foreign_keys": True,
              "unique_contacts": len(contacts) == len(merged)}
    report = {"counts": counts, "checks": checks,
              "status": "review_required" if rejected or conflicts or issues else "pass",
              "collection": dict(collector.stats)}
    return businesses, contacts, {"rejected": rejected, "conflicts": conflicts, "enrichment": issues}, report


def csv_value(value):
    if isinstance(value, (list, dict)):
        return json.dumps(value, ensure_ascii=False, sort_keys=True)
    # Text contacts/labels must not be interpreted as spreadsheet formulas.
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def write_csv(path, rows, fields):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows({k: csv_value(row.get(k)) for k in fields} for row in rows)


def run(fixture_path, output_dir):
    raw = Path(fixture_path).read_bytes()
    fixture = json.loads(raw)
    async def offline():
        collector = Collector(FixtureTransport(fixture["responses"]), interval=0, backoff=0)
        return await build(collector, fixture["collection_url"])
    businesses, contacts, issues, report = asyncio.run(offline())
    report["fixture_sha256"] = hashlib.sha256(raw).hexdigest()
    report["data_kind"] = "synthetic_offline_fixture"
    output = Path(output_dir)
    if output.resolve() in Path(fixture_path).resolve().parents or output.resolve() == Path(fixture_path).resolve():
        raise ValueError("Output must be separate from input")
    output.mkdir(parents=True, exist_ok=True)
    write_csv(output / "businesses.csv", businesses,
              ["business_id", "name", "address", "category", "lat", "lon", "identity_method", "source_ids", "websites", "observation_count"])
    write_csv(output / "contacts.csv", contacts, ["business_id", "kind", "value", "evidence", "status"])
    for name, data in [("dataset.json", {"businesses": businesses, "contacts": contacts}),
                       ("issues.json", issues), ("validation.json", report)]:
        with (output / name).open("w", encoding="utf-8", newline="\n") as handle:
            handle.write(json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n")
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="Run the offline business-data fixture")
    parser.add_argument("--fixture", type=Path, default=Path("data/sample/fixtures.json"))
    parser.add_argument("--output", type=Path, default=Path("outputs/sample"))
    args = parser.parse_args(argv)
    try:
        report = run(args.fixture, args.output)
    except (ValueError, KeyError, TypeError, OSError):
        print("Pipeline failed: check fixture structure and output permissions")
        return 1
    print(json.dumps(report, sort_keys=True))
    return 0 if all(report["checks"].values()) else 2
