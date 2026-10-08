"""Bounded auxiliary company data. Never participant addresses or signer authority."""
from datetime import datetime, timezone
from math import isfinite
import re
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from pydantic import HttpUrl, TypeAdapter, ValidationError

from apis.signing import IncompleteEvidence, current_name, period, records, register_date


MAX_COLLECTION = 200
_HTTP_URL = TypeAdapter(HttpUrl)


def bounded_text(value, limit):
    if not isinstance(value, str):
        return None
    value = value.strip()
    if not value or len(value) > limit or any(ord(char) < 32 for char in value):
        return None
    return value


def digits(value, length):
    if type(value) not in (int, str):
        return None
    value = str(value)
    return value if re.fullmatch(r"[0-9]{%d}" % length, value) else None


def company_cvr(value):
    if type(value) not in (int, str):
        return None
    value = re.sub(r"^(?:CVR[\s:-]*|DK\s*)", "", str(value).strip(), flags=re.IGNORECASE)
    value = re.sub(r"\s", "", value)
    if re.fullmatch(r"[1-9][0-9]{7}", value) is None:
        return None
    checksum = sum(int(digit) * weight for digit, weight in zip(value, [2, 7, 6, 5, 4, 3, 2, 1]))
    return value if checksum % 11 == 0 else None


def integer(value, low, high=None):
    return value if type(value) is int and value >= low and (high is None or value <= high) else None


def positive_ownership_fraction(value):
    if isinstance(value, bool):
        return None
    try:
        fraction = float(value)
    except (ValueError, TypeError, OverflowError):
        return None
    return fraction if isfinite(fraction) and 0 < fraction <= 1 else None


def mapping(value):
    return value if isinstance(value, dict) else {}


def calendar_date(value, today):
    try:
        parsed = register_date(value)
        return parsed.isoformat() if parsed <= today else None
    except (ValueError, TypeError):
        return None


def website(value):
    value = bounded_text(value, 2000)
    if value is None or "\\" in value or any(char.isspace() for char in value):
        return None
    try:
        parsed = urlsplit(value)
        # Accessing port also validates its syntax and range.
        parsed.port
        if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return None
        _HTTP_URL.validate_python(value)
        return value
    except (ValueError, ValidationError):
        return None


def employee_period(value, today):
    value = mapping(value)
    year = integer(value.get("year"), 1900, 2200)
    raw_month = value.get("month")
    month = integer(raw_month, 1, 12) if raw_month is not None else None
    if year is None or (raw_month is not None and month is None) or (year, month or 1) > (today.year, today.month):
        return None
    return {"year": year, "month": month}


def active(record, today):
    try:
        return period(record, today)[0]
    except IncompleteEvidence:
        return None


def sanitize_units(raw, today):
    if not isinstance(raw, list):
        return None, None
    units, conflicts, complete = {}, set(), len(raw) <= MAX_COLLECTION
    for item in raw:
        if not isinstance(item, dict):
            complete = False
            continue
        first, last = item.get("startdate"), item.get("enddate")
        try:
            start = register_date(first) if first is not None else None
            end = register_date(last) if last is not None else None
            if start and end and start > end:
                complete = False
                continue
            if (start and start > today) or (end and end < today):
                continue
        except (ValueError, TypeError):
            complete = False
            continue
        number = digits(item.get("p_number"), 10)
        if number is None:
            complete = False
            continue
        zipcode = item.get("zipcode")
        unit = {"pNumber": number, "name": bounded_text(item.get("name"), 300),
                "address": bounded_text(item.get("address"), 250),
                "postalCode": bounded_text(str(zipcode), 32) if type(zipcode) in (str, int) else None,
                "city": bounded_text(item.get("city"), 100)}
        if number in conflicts:
            continue
        if number in units and units[number] != unit:
            complete = False
            conflicts.add(number)
            units.pop(number)
            continue
        units[number] = unit
    return sorted(units.values(), key=lambda unit: unit["pNumber"])[:MAX_COLLECTION], complete


def sanitize_owners(raw, today):
    if not isinstance(raw, list):
        return None
    owners, conflicts = {}, set()
    for owner in raw:
        if not isinstance(owner, dict) or owner.get("type") != "VIRKSOMHED":
            continue
        identifier, name = company_cvr(owner.get("cvr")), bounded_text(owner.get("name"), 300)
        if not identifier or not name:
            continue
        try:
            start, end = owner.get("startdate"), owner.get("enddate")
            first = register_date(start) if start is not None else None
            last = register_date(end) if end is not None else None
            if (first and last and first > last) or (first and first > today) or (last and last < today):
                continue
        except (ValueError, TypeError):
            continue
        supplied_fractions = [owner.get(key) for key in ("ownership_percent", "voting_rights_percent") if owner.get(key) is not None]
        if supplied_fractions and not any(positive_ownership_fraction(value) is not None for value in supplied_fractions):
            continue
        item = {"cvr": identifier, "name": name,
                "ownershipRange": bounded_text(owner.get("ownership_range"), 100),
                "votingRightsRange": bounded_text(owner.get("voting_rights_range"), 100)}
        if identifier in conflicts:
            continue
        previous = owners.get(identifier)
        if previous and previous != item:
            conflicts.add(identifier)
            owners.pop(identifier)
            continue
        owners[identifier] = item
    # No completeness field exists for owners: oversized data remains missing.
    return sorted(owners.values(), key=lambda owner: (owner["cvr"], owner["name"], owner["ownershipRange"] or "", owner["votingRightsRange"] or "")) if len(owners) <= MAX_COLLECTION else None


def hosted_profile(document, *, today=None):
    today = today or datetime.now(timezone.utc).astimezone(ZoneInfo("Europe/Copenhagen")).date()
    founded, closed = calendar_date(document.get("startdate"), today), calendar_date(document.get("enddate"), today)
    if founded and closed and closed < founded:
        closed = None
    units, complete = sanitize_units(document.get("p_units"), today)
    # The hosted contract has no total count, and its legacy unit helper can
    # return [] on failure. A supplied array cannot prove completeness.
    if complete is True:
        complete = None
    return {"companyEmail": bounded_text(document.get("email"), 320),
            "companyPhone": bounded_text(document.get("phone"), 50),
            "website": website(document.get("website")),
            "industryCode": bounded_text(str(document["industrycode"]), 20) if type(document.get("industrycode")) in (int, str) else None,
            "industryDescription": bounded_text(document.get("industrydesc"), 300),
            "companyStatus": bounded_text(document.get("status"), 200),
            "bankrupt": document.get("bankrupt") if type(document.get("bankrupt")) is bool else None,
            "foundedOn": founded, "closedOn": closed,
            "employeeCount": integer(document.get("employees"), 0, 100000000),
            "employeePeriod": None,
            "productionUnits": units, "productionUnitsComplete": complete,
            "companyOwners": sanitize_owners(document.get("ejere"), today)}


def official_owners(company, today, range_for_fraction):
    relations = company.get("deltagerRelation")
    if not isinstance(relations, list):
        return None
    owners = []
    for relation in relations:
        relation = mapping(relation)
        participant = mapping(relation.get("deltager"))
        identifier = company_cvr(participant.get("forretningsnoegle"))
        if participant.get("enhedstype") != "VIRKSOMHED" or identifier is None:
            continue
        try:
            name = current_name(participant, today)
            for organisation in records(relation.get("organisationer")):
                if organisation.get("hovedtype") != "REGISTER" or not any(item.get("navn") == "EJERREGISTER" for item in records(organisation.get("organisationsNavn", []))):
                    continue
                ranges = {"EJERANDEL_PROCENT": set(), "EJERANDEL_STEMMERET_PROCENT": set()}
                for member in records(organisation.get("medlemsData")):
                    for attribute in records(member.get("attributter")):
                        kind = attribute.get("type")
                        if not isinstance(kind, str) or kind not in ranges:
                            continue
                        for value in records(attribute.get("vaerdier")):
                            if active(value, today) is not True or isinstance(value.get("vaerdi"), bool):
                                continue
                            fraction = positive_ownership_fraction(value.get("vaerdi"))
                            if fraction is not None:
                                ranges[kind].add(range_for_fraction(fraction))
                if not any(ranges.values()) or any(len(values) > 1 for values in ranges.values()):
                    continue
                owners.append({"type": "VIRKSOMHED", "cvr": identifier, "name": name,
                               "ownership_range": next(iter(ranges["EJERANDEL_PROCENT"]), None),
                               "voting_rights_range": next(iter(ranges["EJERANDEL_STEMMERET_PROCENT"]), None)})
        except IncompleteEvidence:
            continue
    return sanitize_owners(owners, today)


def official_profile(company, formatted, today, range_for_fraction):
    profile = hosted_profile(formatted, today=today)
    metadata = mapping(company.get("virksomhedMetadata"))
    status = mapping(metadata.get("nyesteStatus"))
    credit = bounded_text(status.get("kreditoplysningtekst"), 200)
    profile["bankrupt"] = credit == "Konkurs" if credit is not None else None
    employment = mapping(metadata.get("nyesteErstMaanedsbeskaeftigelse"))
    profile["employeePeriod"] = employee_period({"year": employment.get("aar"), "month": employment.get("maaned")}, today)
    profile["companyOwners"] = official_owners(company, today, range_for_fraction)
    # Reopened businesses must not retain an older lifecycle's closure date.
    lifecycle = company.get("livsforloeb")
    profile["closedOn"] = None
    if isinstance(lifecycle, list):
        valid = []
        for item in lifecycle:
            item_period = mapping(mapping(item).get("periode"))
            first = calendar_date(item_period.get("gyldigFra"), today)
            raw_end = item_period.get("gyldigTil")
            last = calendar_date(raw_end, today) if raw_end is not None else None
            if first:
                # An unknown/future end on the latest lifecycle cannot revive an
                # older closure. Reversed dates likewise do not prove closure.
                valid.append((first, last if last and first <= last else None))
        if valid:
            latest_start = max(first for first, _ in valid)
            latest_ends = {last for first, last in valid if first == latest_start}
            last = next(iter(latest_ends)) if len(latest_ends) == 1 else None
            if last and (not profile["foundedOn"] or profile["foundedOn"] <= last):
                profile["closedOn"] = last
    return profile
