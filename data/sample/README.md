# Synthetic fixtures

All names, IDs, addresses, coordinates, and contacts are invented. Domains use
the reserved `.example` suffix; phone examples use the fictional 555-01xx range.
The fixture is not a transformed or anonymized copy of collected businesses.

Seven observations include one duplicate source identity and one invalid
coordinate. Five entities remain. Two branches share a website and central
contacts but remain distinct businesses. A missing page is an explicit
enrichment failure. Scripted 429 and 503 responses exercise retry handling.

No network request is made by the CLI. Fixture responses are data, not a cache
of actual HTTP traffic. A missing fixture URL returns 404; it never falls back
to the internet.
