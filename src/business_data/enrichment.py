"""Extract explicit contact links and structured contact fields only."""

import asyncio
from html.parser import HTMLParser
import json
from urllib.parse import unquote

from .normalize import email, phone


class ContactParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.found = set()
        self.structured = False
        self.buffer = []

    def add(self, kind, raw):
        if isinstance(raw, list):
            for item in raw:
                self.add(kind, item)
        elif isinstance(raw, str):
            value = phone(raw) if kind == "phone" else email(raw)
            if value:
                self.found.add((kind, value))

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "a":
            href = attrs.get("href", "")
            prefix, _, value = href.partition(":")
            if prefix.lower() in {"mailto", "tel"}:
                self.add("email" if prefix.lower() == "mailto" else "phone", unquote(value.split("?", 1)[0]))
        if tag == "script" and attrs.get("type", "").lower() == "application/ld+json":
            self.structured, self.buffer = True, []

    def handle_data(self, data):
        if self.structured:
            self.buffer.append(data)

    def handle_endtag(self, tag):
        if tag == "script" and self.structured:
            self.structured = False
            try:
                self.walk(json.loads("".join(self.buffer)))
            except (ValueError, RecursionError):
                pass

    def walk(self, value):
        if isinstance(value, list):
            for item in value:
                self.walk(item)
        elif isinstance(value, dict):
            for k, v in value.items():
                if k in {"telephone", "email"}:
                    self.add("phone" if k == "telephone" else "email", v)
                elif k in {"@graph", "contactPoint"}:
                    self.walk(v)


async def enrich(businesses, collector):
    urls = sorted({u for b in businesses for u in b["websites"]})

    async def fetch(url):
        response = await collector.get(url)
        if response.status != 200:
            return url, [], response.status
        parser = ContactParser()
        parser.feed(response.body)
        return url, sorted(parser.found), 200

    results = {url: (contacts, status) for url, contacts, status in await asyncio.gather(*(fetch(u) for u in urls))}
    contacts, issues = [], []
    for business in businesses:
        for url in business["websites"]:
            found, status = results[url]
            if status != 200:
                issues.append({"business_id": business["business_id"], "reason": "website_fetch_failed", "status": status})
            for kind, value in found:
                contacts.append({"business_id": business["business_id"], "kind": kind, "value": value,
                                 "evidence": url, "status": "unverified"})
    return contacts, issues
