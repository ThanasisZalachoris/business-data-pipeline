"""One entity per identity; contacts never participate in entity counting."""

import hashlib
import json

from .normalize import key


def deduplicate(records):
    groups = {}
    for row in records:
        if row["source_id"]:
            identity = ("source", row["source_id"])
        elif row["address"]:
            identity = ("name_address", key(row["name"]), key(row["address"]))
        else:
            identity = ("unresolved", str(row["input_index"]))
        groups.setdefault(identity, []).append(row)
    businesses, contact_rows, conflicts = [], [], []
    for identity, members in sorted(groups.items()):
        business_id = "b_" + hashlib.sha256(json.dumps(identity).encode()).hexdigest()[:16]
        # Lexical order provides a stable representative, not an accuracy ranking.
        representative = min(members, key=lambda r: json.dumps(r, sort_keys=True, ensure_ascii=True))
        entity = {k: representative[k] for k in ("name", "address", "category", "lat", "lon")}
        entity.update(business_id=business_id, identity_method=identity[0],
                      source_ids=sorted({r["source_id"] for r in members if r["source_id"]}),
                      websites=sorted({r["website"] for r in members if r["website"]}),
                      observation_count=len(members))
        for field in ("name", "address", "category", "lat", "lon"):
            values = sorted({json.dumps(r[field], ensure_ascii=False) for r in members if r[field] not in (None, "")})
            if len(values) > 1:
                conflicts.append({"business_id": business_id, "field": field,
                                  "values": [json.loads(v) for v in values]})
        businesses.append(entity)
        for kind, field in (("phone", "phones"), ("email", "emails")):
            for value in sorted({v for r in members for v in r[field]}):
                contact_rows.append({"business_id": business_id, "kind": kind, "value": value,
                                     "evidence": "input", "status": "unverified"})
    return businesses, contact_rows, conflicts
