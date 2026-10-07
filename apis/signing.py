"""Registered signing evidence, not an interpretation of who may sign alone.

Mapping: https://erhvervsstyrelsen.dk/vejledning-cvr-indeks-data-katalog
Only names, register unit IDs and role evidence leave this boundary. No addresses,
CPR numbers or ownership percentages are needed for this purpose.
"""
from datetime import date, datetime, timezone
import re
from zoneinfo import ZoneInfo


class IncompleteEvidence(ValueError):
    pass


def records(value):
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise IncompleteEvidence("Invalid register collection")
    return value


def text(value, limit):
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise IncompleteEvidence("Invalid register text")
    return value


def period(record, today):
    value = record.get("periode")
    if not isinstance(value, dict):
        raise IncompleteEvidence("Missing validity period")
    start, end = value.get("gyldigFra"), value.get("gyldigTil")
    try:
        first = register_date(start) if start is not None else None
        last = register_date(end) if end is not None else None
    except (ValueError, TypeError):
        raise IncompleteEvidence("Invalid validity date") from None
    if first and last and first > last:
        raise IncompleteEvidence("Reversed validity period")
    # Register periods are calendar dates; gyldigTil is inclusive.
    return (first is None or first <= today) and (last is None or today <= last), {
        "validFrom": first.isoformat() if first else None,
        "validTo": last.isoformat() if last else None,
    }


def register_date(value):
    if not isinstance(value, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}(?:T.+)?", value):
        raise ValueError("Invalid register date")
    if len(value) == 10:
        return date.fromisoformat(value)
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(ZoneInfo("Europe/Copenhagen"))
    return parsed.date()


def updated(record):
    value = record.get("sidstOpdateret")
    if value is None:
        return None
    try:
        register_date(value)
        if len(value) == 10:
            # Retain date-only precision; do not invent an update instant.
            return value
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            return parsed.isoformat()
        return parsed.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
    except (TypeError, ValueError, AttributeError):
        raise IncompleteEvidence("Invalid update timestamp") from None


def current_name(participant, today):
    names = set()
    for name in records(participant.get("navne")):
        active, _ = period(name, today)
        if active:
            names.add(text(name.get("navn"), 300))
    if len(names) != 1:
        raise IncompleteEvidence("Missing or conflicting current participant name")
    return names.pop()


def extract_signing(company, *, observed_at=None):
    observed_at = observed_at or datetime.now(timezone.utc)
    if observed_at.tzinfo is None:
        raise ValueError("observed_at must have a timezone")
    today = observed_at.astimezone(ZoneInfo("Europe/Copenhagen")).date()
    rules, participants, incomplete = [], {}, False
    try:
        company_updated = updated(company)
    except IncompleteEvidence:
        company_updated, incomplete = None, True
    try:
        attributes = records(company.get("attributter"))
        for attribute in attributes:
            if attribute.get("type") != "TEGNINGSREGEL":
                continue
            for value in records(attribute.get("vaerdier")):
                active, validity = period(value, today)
                if active:
                    rule = {"text": text(value.get("vaerdi"), 10000), **validity,
                            "updatedAt": updated(value)}
                    if rule not in rules:
                        rules.append(rule)
    except IncompleteEvidence:
        incomplete = True
    try:
        relations = records(company.get("deltagerRelation"))
        for relation in relations:
            try:
                roles = []
                for organisation in records(relation.get("organisationer")):
                    kind = organisation.get("hovedtype")
                    if kind not in {"LEDELSESORGAN", "TEGNINGSBERETTIGEDE", "FULDT_ANSVARLIG_DELTAGERE"}:
                        continue
                    members = records(organisation.get("medlemsData"))
                    if not members:
                        raise IncompleteEvidence("Missing registered membership")
                    for member in members:
                        has_function = False
                        for attribute in records(member.get("attributter")):
                            if attribute.get("type") != "FUNKTION":
                                continue
                            values = records(attribute.get("vaerdier"))
                            if not values:
                                raise IncompleteEvidence("Empty registered membership function")
                            has_function = True
                            for value in values:
                                active, validity = period(value, today)
                                if active:
                                    role = {"organisationType": kind,
                                            "role": text(value.get("vaerdi"), 200),
                                            **validity, "updatedAt": updated(value)}
                                    if role not in roles:
                                        roles.append(role)
                        if not has_function:
                            raise IncompleteEvidence("Missing registered membership function")
                if not roles:
                    continue
                participant = relation.get("deltager")
                if not isinstance(participant, dict):
                    raise IncompleteEvidence("Missing participant")
                identifier = str(participant.get("enhedsNummer", ""))
                if not identifier.isascii() or not identifier.isdigit() or len(identifier) != 10:
                    raise IncompleteEvidence("Missing register unit ID")
                name = current_name(participant, today)
                entity_type = text(participant.get("enhedstype"), 50)
                if entity_type not in {"PERSON", "VIRKSOMHED", "ANDEN DELTAGER"}:
                    raise IncompleteEvidence("Unknown participant type")
                previous = participants.get(identifier)
                if previous and (previous["name"] != name or previous["entityType"] != entity_type):
                    raise IncompleteEvidence("Conflicting participant identity")
                if previous:
                    roles = previous["roles"] + [role for role in roles if role not in previous["roles"]]
                participants[identifier] = {
                    "unitId": identifier, "name": name, "entityType": entity_type,
                    "registeredRepresentative": any(role["organisationType"] == "TEGNINGSBERETTIGEDE" for role in roles),
                    "roles": roles,
                }
            except IncompleteEvidence:
                incomplete = True
    except IncompleteEvidence:
        incomplete = True
    if len(rules) > 10 or len(participants) > 200 or any(len(p["roles"]) > 100 for p in participants.values()):
        # A truncated group cannot be represented as complete evidence.
        rules, participants, incomplete = [], {}, True
    status = "available"
    if incomplete:
        status = "incomplete"
    elif len({rule["text"] for rule in rules}) > 1:
        status = "conflicting_rules"
    elif not rules:
        status = "missing_rule"
    return {
        "schemaVersion": 1, "status": status, "rules": rules,
        "participants": sorted(participants.values(), key=lambda p: p["unitId"]),
        "source": {"register": "CVR", "observedAt": observed_at.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                   "companyUpdatedAt": company_updated},
    }
