"""Name/role suggestions only; no personal addresses or signing determination."""
from datetime import datetime, timezone
from math import isfinite
from zoneinfo import ZoneInfo

from apis.signing import IncompleteEvidence, current_name, period, records, register_date, text


def _group(entries):
    people = {}
    for name, role in entries:
        try:
            name, role = text(name, 300).strip(), text(role, 200).strip()
        except IncompleteEvidence:
            continue
        key = " ".join(name.split()).casefold()
        person = people.setdefault(key, {"name": name, "roles": []})
        if role not in person["roles"]:
            person["roles"].append(role)
        if len(people) > 200 or len(person["roles"]) > 20:
            return []
    return sorted(people.values(), key=lambda person: person["name"].casefold())


def hosted_contact_suggestions(document, *, observed_at=None):
    today = (observed_at or datetime.now(timezone.utc)).astimezone(ZoneInfo("Europe/Copenhagen")).date()
    entries = []
    for key in ("direktion", "fuldt_ansvarlige", "ejere"):
        try:
            people = records(document.get(key, []))
            if len(people) > 200:
                return []
        except IncompleteEvidence:
            continue
        for person in people:
            if person.get("type") != "PERSON":
                continue
            try:
                start, end = person.get("startdate"), person.get("enddate")
                if (start is not None and register_date(start) > today) or (end is not None and register_date(end) < today):
                    continue
            except (ValueError, TypeError, OverflowError):
                continue
            entries.append((person.get("name"), "LEGAL_OWNER" if key == "ejere" else person.get("role")))
    return _group(entries)


def official_contact_suggestions(company, signing):
    today = datetime.fromisoformat(signing["source"]["observedAt"].replace("Z", "+00:00")).astimezone(ZoneInfo("Europe/Copenhagen")).date()
    entries = [(person["name"], role["role"]) for person in signing["participants"]
               if person["entityType"] == "PERSON" for role in person["roles"]]
    try:
        relations = records(company.get("deltagerRelation", []))
        if len(relations) > 1000:
            return []
    except IncompleteEvidence:
        return _group(entries)
    for relation in relations:
        person = relation.get("deltager")
        if not isinstance(person, dict) or person.get("enhedstype") != "PERSON":
            continue
        try:
            name = current_name(person, today)
            for organisation in records(relation.get("organisationer")):
                if organisation.get("hovedtype") != "REGISTER" or not any(
                    item.get("navn") == "EJERREGISTER" for item in records(organisation.get("organisationsNavn", []))
                ):
                    continue
                for member in records(organisation.get("medlemsData")):
                    for attribute in records(member.get("attributter")):
                        if attribute.get("type") not in {"EJERANDEL_PROCENT", "EJERANDEL_STEMMERET_PROCENT"}:
                            continue
                        for value in records(attribute.get("vaerdier")):
                            active, _ = period(value, today)
                            try:
                                raw_fraction = value.get("vaerdi")
                                if isinstance(raw_fraction, bool):
                                    continue
                                fraction = float(raw_fraction)
                            except (ValueError, TypeError, OverflowError):
                                continue
                            if active and isfinite(fraction) and 0 < fraction <= 1:
                                entries.append((name, "LEGAL_OWNER"))
        except IncompleteEvidence:
            continue
    return _group(entries)
