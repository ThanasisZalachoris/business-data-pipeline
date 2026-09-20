"""Offline tests for identity, contact provenance, and collection behavior."""

import asyncio
import copy
import json
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from business_data.collection import Collector, FixtureTransport, HostLimiter, HttpTransport, Response
from business_data.enrichment import ContactParser
from business_data.entities import deduplicate
from business_data.normalize import coordinate, email, normalize_elements, phone, website
from business_data.pipeline import build, csv_value, run


def fixture():
    return json.loads((ROOT / "data/sample/fixtures.json").read_text(encoding="utf-8"))


def elements():
    f = fixture()
    return f["responses"][f["collection_url"]][-1]["body"]


class NormalizationTests(unittest.TestCase):
    def test_international_phone_formats(self):
        self.assertEqual(phone("0012025550101"), "+12025550101")
        self.assertEqual(phone("+1 (202) 555-0101"), "+12025550101")

    def test_local_number_extensions_and_garbage_not_guessed(self):
        for value in ["2025550101", "+12025550101 ext 2", "garbage", "+0000000000", "+123"]:
            self.assertIsNone(phone(value))

    def test_email_normalization_and_invalid_email(self):
        self.assertEqual(email(" HELLO@cafe.example "), "hello@cafe.example")
        self.assertIsNone(email("bad@@cafe.example"))

    def test_url_normalization(self):
        self.assertEqual(website("Cafe.Example/contact#section"), "https://cafe.example/contact")
        for value in ["javascript:alert(1)", "https://user:pass@cafe.example/", "https://cafe.example:9000/"]:
            self.assertIsNone(website(value))

    def test_zero_coordinates_preserved(self):
        accepted, _ = normalize_elements(elements())
        row = next(r for r in accepted if r["source_id"] == "way:201")
        self.assertEqual((row["lat"], row["lon"]), (0, 0))

    def test_bad_coordinate_is_quarantined(self):
        accepted, rejected = normalize_elements(elements())
        self.assertEqual((len(accepted), len(rejected)), (6, 1))
        for value in ["nan", "inf", True, 91]:
            with self.assertRaises(ValueError):
                coordinate(value, 90)

    def test_partial_coordinates_and_invalid_identity(self):
        for element in [{"tags": {"name": "Example"}, "lat": 1},
                        {"tags": {"name": "Example"}, "type": "node", "id": True}]:
            accepted, rejected = normalize_elements({"elements": [element]})
            self.assertEqual(len(accepted), 0)
            self.assertEqual(len(rejected), 1)


class IdentityTests(unittest.TestCase):
    def test_duplicate_source_identity_merges_contacts(self):
        rows, _ = normalize_elements(elements())
        businesses, contacts, _ = deduplicate(rows)
        self.assertEqual(len(businesses), 5)
        cafe = next(b for b in businesses if b["source_ids"] == ["node:101"])
        self.assertEqual(cafe["observation_count"], 2)
        self.assertEqual(sum(c["business_id"] == cafe["business_id"] and c["kind"] == "phone" for c in contacts), 2)

    def test_shared_website_does_not_merge_branches(self):
        rows, _ = normalize_elements(elements())
        businesses, _, _ = deduplicate(rows)
        branches = [b for b in businesses if b["websites"] == ["https://fitness.example/contact"]]
        self.assertEqual(len(branches), 2)

    def test_idless_name_address_matching(self):
        rows, _ = normalize_elements({"elements": [{"tags": {"name": "Sample", "addr:full": "One Road"}},
                                                    {"tags": {"name": " SAMPLE ", "addr:full": "one road"}}]})
        self.assertEqual(len(deduplicate(rows)[0]), 1)

    def test_missing_identity_never_merges_on_empty_fields(self):
        rows, _ = normalize_elements({"elements": [{"tags": {"name": "Sample"}}, {"tags": {"name": "Sample"}}]})
        self.assertEqual(len(deduplicate(rows)[0]), 2)

    def test_conflicting_fields_preserved_as_exceptions(self):
        data = elements()
        data["elements"][1]["tags"]["addr:full"] = "Different Fictional Address"
        rows, _ = normalize_elements(data)
        _, _, conflicts = deduplicate(rows)
        self.assertTrue(any(c["field"] == "address" for c in conflicts))

    def test_source_identity_output_stable_when_input_reordered(self):
        data = elements()
        data["elements"] = [e for e in data["elements"] if e.get("id")]
        rows, _ = normalize_elements(data)
        first = deduplicate(rows)
        data["elements"].reverse()
        rows, _ = normalize_elements(data)
        self.assertEqual(first, deduplicate(rows))


class EnrichmentTests(unittest.TestCase):
    def test_structured_graph_and_links_are_deduplicated(self):
        parser = ContactParser()
        parser.feed('<a href="mailto:hello%40cafe.example?subject=Hi">Mail</a><script type="application/ld+json">'
                    '{"@graph":[{"contactPoint":[{"email":"hello@cafe.example","telephone":"+12025550101"}]}]}</script>')
        self.assertEqual(parser.found, {("email", "hello@cafe.example"), ("phone", "+12025550101")})

    def test_malformed_structured_data_does_not_hide_links(self):
        parser = ContactParser()
        parser.feed('<script type="application/ld+json">invalid</script><a href="tel:+12025550101">Call</a>')
        self.assertEqual(parser.found, {("phone", "+12025550101")})

    def test_no_regex_sweep_of_unrelated_numbers(self):
        parser = ContactParser()
        parser.feed("<p>Order 12025550101, random@example.example</p>")
        self.assertFalse(parser.found)


class CollectionTests(unittest.IsolatedAsyncioTestCase):
    async def test_retry_and_cache(self):
        url = "https://test.example/"
        transport = FixtureTransport({url: [{"status": 429}, {"status": 200, "body": "ok"}]})
        collector = Collector(transport, interval=0, backoff=0)
        self.assertEqual((await collector.get(url)).body, "ok")
        self.assertEqual((await collector.get(url)).body, "ok")
        self.assertEqual(collector.stats, {"requests": 2, "retries": 1, "cache_hits": 1})

    async def test_404_is_not_retried(self):
        collector = Collector(FixtureTransport({}), interval=0, backoff=0)
        self.assertEqual((await collector.get("https://missing.example/")).status, 404)
        self.assertEqual(collector.stats["requests"], 1)

    async def test_retry_exhaustion_is_bounded(self):
        async def failing(url):
            raise ConnectionError()
        collector = Collector(failing, attempts=2, interval=0, backoff=0)
        self.assertEqual((await collector.get("https://test.example/")).status, 503)
        self.assertEqual(collector.stats["requests"], 2)

    async def test_expired_cache_refetched(self):
        current = [0.0]
        transport = FixtureTransport({"https://test.example/": [{"status": 200}]})
        collector = Collector(transport, interval=0, ttl=2, clock=lambda: current[0])
        await collector.get("https://test.example/")
        current[0] = 3
        await collector.get("https://test.example/")
        self.assertEqual(collector.stats["requests"], 2)

    async def test_concurrent_identical_requests_coalesce(self):
        collector = Collector(FixtureTransport({"https://test.example/": [{"status": 200}]}), interval=0)
        await asyncio.gather(*(collector.get("https://test.example/") for _ in range(5)))
        self.assertEqual(collector.stats["requests"], 1)
        self.assertEqual(collector.stats["cache_hits"], 4)

    async def test_per_host_rate_limit(self):
        current, waits = [0.0], []
        async def sleep(delay):
            waits.append(delay)
            current[0] += delay
        limiter = HostLimiter(2, clock=lambda: current[0], sleep=sleep)
        await limiter.wait("one.example")
        await limiter.wait("two.example")
        await limiter.wait("one.example")
        self.assertEqual(waits, [2])

    async def test_numeric_retry_after_respected(self):
        waits = []
        async def sleep(delay):
            waits.append(delay)
        collector = Collector(FixtureTransport({"https://test.example/": [
            {"status": 429, "headers": {"Retry-After": "3"}}, {"status": 200}]}), interval=0, sleep=sleep)
        await collector.get("https://test.example/")
        self.assertEqual(waits, [3])

    async def test_concurrency_bound(self):
        active, maximum = 0, 0
        async def transport(url):
            nonlocal active, maximum
            active += 1
            maximum = max(maximum, active)
            await asyncio.sleep(0.001)
            active -= 1
            return Response(200, "ok")
        collector = Collector(transport, concurrency=2, interval=0)
        await asyncio.gather(*(collector.get(f"https://test.example/{i}") for i in range(8)))
        self.assertEqual(maximum, 2)

    async def test_unknown_or_long_retry_delay_does_not_retry_early(self):
        for delay in ["120", "Wed, 21 Oct 2030 07:28:00 GMT", "inf"]:
            collector = Collector(FixtureTransport({"https://test.example/": [
                {"status": 429, "headers": {"Retry-After": delay}}, {"status": 200}]}), interval=0)
            self.assertEqual((await collector.get("https://test.example/")).status, 429)
            self.assertEqual(collector.stats["requests"], 1)

    async def test_live_transport_rejects_unapproved_host_without_network(self):
        transport = HttpTransport({"allowed.example"}, "Example audit contact")
        with self.assertRaises(ValueError):
            await transport("https://unapproved.example/")

    async def test_failed_collection_is_not_empty_success(self):
        with self.assertRaises(ValueError):
            await build(Collector(FixtureTransport({}), interval=0), "https://missing.example/")


class PipelineTests(unittest.TestCase):
    def test_offline_end_to_end_and_reproducibility(self):
        with tempfile.TemporaryDirectory() as directory, patch("urllib.request.OpenerDirector.open", side_effect=AssertionError("Network forbidden")):
            first, second = Path(directory) / "one", Path(directory) / "two"
            report = run(ROOT / "data/sample/fixtures.json", first)
            run(ROOT / "data/sample/fixtures.json", second)
            self.assertEqual({p.name: p.read_bytes() for p in first.iterdir()}, {p.name: p.read_bytes() for p in second.iterdir()})
            self.assertTrue(all(report["checks"].values()))
            self.assertEqual(report["counts"]["retained_entities"], 5)
            self.assertEqual(report["counts"]["contact_records"], 8)
            self.assertEqual(report["counts"]["rejected_observations"], 1)
            self.assertEqual(report["counts"]["enrichment_failures"], 1)
            self.assertEqual(report["collection"]["retries"], 2)
            dataset = json.loads((first / "dataset.json").read_text())
            self.assertEqual(len(dataset["businesses"]), 5)
            self.assertTrue(all(c["status"] == "unverified" for c in dataset["contacts"]))

    def test_csv_formula_prefixes_are_escaped_but_numbers_are_not(self):
        self.assertEqual(csv_value("=1+1"), "'=1+1")
        self.assertEqual(csv_value("+12025550101"), "'+12025550101")
        self.assertEqual(csv_value(-2.0), -2.0)

    def test_empty_collection_outputs_headers_and_zero_counts(self):
        data = fixture()
        data["responses"][data["collection_url"]] = [{"status": 200, "body": {"elements": []}}]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "fixture.json").write_text(json.dumps(data))
            report = run(root / "fixture.json", root / "out")
            self.assertEqual(report["counts"]["retained_entities"], 0)
            self.assertEqual(len((root / "out/contacts.csv").read_text().splitlines()), 1)


if __name__ == "__main__":
    unittest.main()
