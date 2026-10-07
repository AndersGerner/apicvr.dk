"""
Client for official CVR data and the hosted apicvr.dk fallback.

Wraps the Elasticsearch endpoints ERST exposes for the public register
of companies when API_TOKEN is configured. Without it, documented hosted
endpoints provide company enrichment with explicitly incomplete signing evidence.
Public functions all return plain dicts / lists so the FastAPI
routes and the Jinja templates can consume them directly.

Failures return a stable error dict:
    {"error": <machine-readable code>, "status": <int|None>, "message": <str|None>}
Route handlers convert these to HTTPException; templates render an error box.
"""
import json
import os
import re
import asyncio
from datetime import datetime, timezone
from typing import Any, Iterable, Optional
from urllib.parse import quote

import httpx
from dotenv import load_dotenv
from pydantic import ValidationError

from apis.models import Company, CompanyFuzzy, CompanyRelations, SigningCompany
from apis.signing import extract_signing


load_dotenv()

# --- Configuration ---------------------------------------------------------

_API_TOKEN = os.getenv("API_TOKEN", "").strip()
_BASE = "https://distribution.virk.dk"
_HOSTED_BASE = "https://apicvr.dk"
_COMPANY_URL = f"{_BASE}/cvr-permanent/virksomhed/_search"
_PRODUCTION_UNIT_URL = f"{_BASE}/cvr-permanent/produktionsenhed/_search"
_TIMEOUT = httpx.Timeout(3, connect=2)
_MAX_RESPONSE_BYTES = 8_000_000
_TOTAL_SECONDS = 8

# Only currently-active roles are surfaced (FUNKTION periode.gyldigTil is None).
_DIRECTOR_FUNCTIONS = {"DIREKTØR", "ADM. DIR."}

# CVR ownership display buckets: (low, high, label) — half-open [low, high).
_OWNERSHIP_BUCKETS = [
    (0.05,   0.10,   "5-9,99%"),
    (0.10,   0.15,   "10-14,99%"),
    (0.15,   0.20,   "15-19,99%"),
    (0.20,   0.25,   "20-24,99%"),
    (0.25,   0.3333, "25-33,32%"),
    (0.3333, 0.50,   "33,33-49,99%"),
    (0.50,   0.6667, "50-66,66%"),
    (0.6667, 0.90,   "66,67-89,99%"),
    (0.90,   1.00,   "90-99,99%"),
]


# --- Internal transport ----------------------------------------------------

def _post_search(
    query: dict,
    *,
    endpoint: str = _COMPANY_URL,
    size: int = 100,
    source: Optional[list] = None,
) -> Any:
    """POST an Elasticsearch query. Returns the parsed body, or an error dict."""
    payload = {
        "_source": source or ["Vrvirksomhed"],
        "query": query,
        "size": size,
    }
    if not upstream_configured():
        return {"error": "NOT_CONFIGURED", "status": 503, "message": None}
    return asyncio.run(_post_search_bounded(endpoint, payload))


async def _post_search_bounded(endpoint: str, payload: dict) -> Any:
    result = await _request_json_bounded(
        "POST", endpoint,
        headers={"Authorization": f"Basic {_API_TOKEN.strip()}", "Content-Type": "application/json"},
        json=payload,
    )
    if not isinstance(result, dict):
        return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
    return result


async def _request_json_bounded(method: str, endpoint: str, **kwargs) -> Any:
    # This deadline includes connect, headers and slow/trickled body reads.
    try:
        async with asyncio.timeout(_TOTAL_SECONDS):
            async with httpx.AsyncClient(timeout=_TIMEOUT, follow_redirects=False) as client:
                async with client.stream(method, endpoint, **kwargs) as response:
                    if response.status_code != 200:
                        return {"error": "HTTP_ERROR", "status": response.status_code, "message": None}
                    body = bytearray()
                    async for chunk in response.aiter_bytes():
                        if len(body) + len(chunk) > _MAX_RESPONSE_BYTES:
                            return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
                        body.extend(chunk)
                    result = json.loads(body)
                    if not isinstance(result, (dict, list)) or (isinstance(result, dict) and result.get("error")):
                        return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
                    return result
    except (httpx.HTTPError, TimeoutError):
        return {"error": "TRANSPORT_ERROR", "status": 502, "message": None}
    except (ValueError, UnicodeError):
        return {"error": "INVALID_RESPONSE", "status": 502, "message": None}


def upstream_configured() -> bool:
    """Presence only; successful official authentication needs a live lookup."""
    return bool(_API_TOKEN.strip())


def selected_provider() -> str:
    return "distribution.virk.dk" if upstream_configured() else "apicvr.dk"


def _valid_cvr(value) -> bool:
    return type(value) in (int, str) and re.fullmatch(r"[1-9][0-9]{7}", str(value)) is not None


def _hosted_segment(value: str) -> str:
    # Encode a single path segment, including dot-only values.
    return quote(value, safe="").replace(".", "%2E")


def _hosted_get(path: str, *, params=None, model=Company, cvr_number=None) -> Any:
    """Consume documented hosted endpoints without forwarding any credentials."""
    result = asyncio.run(_request_json_bounded(
        "GET", f"{_HOSTED_BASE}{path}", headers={"Accept": "application/json"}, params=params,
    ))
    if _is_error(result):
        if result["error"] == "HTTP_ERROR" and result["status"] == 404:
            return _not_found()
        return result

    documents = [result] if cvr_number is not None else result
    if not isinstance(documents, list) or len(documents) > 1000:
        return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
    normalized = []
    for document in documents:
        if not isinstance(document, dict):
            return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
        identity = document.get("cvr_number" if model is CompanyFuzzy else "vat")
        if not _valid_cvr(identity) or (cvr_number is not None and int(identity) != cvr_number):
            return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
        data = dict(document)
        if model in (Company, SigningCompany):
            # Hosted roles lack the registered rule and stable participant IDs.
            # They cannot establish signing authority, even if extra fields appear.
            data["signing"] = {
                "schemaVersion": 1, "status": "incomplete", "rules": [], "participants": [],
                "source": {"register": "CVR", "observedAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
                           "companyUpdatedAt": None},
            }
        try:
            normalized.append(model.model_validate(data, extra="ignore").model_dump(mode="json", by_alias=True))
        except ValidationError:
            return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
    return normalized[0] if cvr_number is not None else normalized


def _is_error(result: Any) -> bool:
    return isinstance(result, dict) and bool(result.get("error"))


def _hits(result: Any) -> list:
    if _is_error(result) or not isinstance(result, dict):
        return []
    return result.get("hits", {}).get("hits", []) or []


def _companies_from_hits(hits: list) -> list:
    out = []
    for hit in hits:
        company = hit.get("_source", {}).get("Vrvirksomhed", {})
        cvr_number = company.get("cvrNummer")
        if cvr_number is None:
            continue
        out.append(format_company_data(company, cvr_number))
    return out


def _not_found() -> dict:
    return {"error": "NOT_FOUND", "status": 404, "message": None}


# --- Public search API -----------------------------------------------------

def search_cvr_api(cvr_number: int, *, include_relations: bool = True) -> dict:
    """Look up a company by CVR number, including production units and relations."""
    if not _valid_cvr(cvr_number):
        return {"error": "INVALID_CVR", "status": 400, "message": None}
    if not upstream_configured():
        return _hosted_get(f"/api/v1/{cvr_number}", cvr_number=cvr_number,
                           model=Company if include_relations else SigningCompany)
    result = _post_search({"term": {"Vrvirksomhed.cvrNummer": cvr_number}}, size=2)
    if _is_error(result):
        return result

    envelope = result.get("hits")
    if not isinstance(envelope, dict) or not isinstance(envelope.get("hits"), list):
        return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
    hits = envelope["hits"]
    if not hits:
        return _not_found()

    hit_source = hits[0].get("_source") if isinstance(hits[0], dict) else None
    company = hit_source.get("Vrvirksomhed") if isinstance(hit_source, dict) else None
    if not isinstance(company, dict):
        return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
    source_cvr = company.get("cvrNummer")
    if (
        len(hits) != 1
        or not isinstance(source_cvr, (str, int))
        or re.fullmatch(r"[1-9][0-9]{7}", str(source_cvr)) is None
        or int(source_cvr) != cvr_number
    ):
        return {"error": "INVALID_RESPONSE", "status": 502, "message": None}
    data = format_company_data(company, cvr_number)
    data["signing"] = extract_signing(company)

    if not include_relations:
        return data

    p_numbers = [
        p["pNummer"]
        for p in company.get("penheder") or []
        if isinstance(p, dict) and p.get("pNummer")
    ]
    data["p_units"] = fetch_p_units(p_numbers)

    direktion, fuldt_ansvarlige, ejere = _extract_current_relations(company)
    data["direktion"] = direktion
    data["fuldt_ansvarlige"] = fuldt_ansvarlige
    data["ejere"] = ejere
    return data


def get_company_relations(cvr_number: int) -> dict:
    """Return only current direktion, fully-liable participants, and legal owners."""
    if not _valid_cvr(cvr_number):
        return {"error": "INVALID_CVR", "status": 400, "message": None}
    if not upstream_configured():
        return _hosted_get(f"/api/v1/{cvr_number}/direktion-og-ansvarlig",
                           cvr_number=cvr_number, model=CompanyRelations)
    result = _post_search(
        {"term": {"Vrvirksomhed.cvrNummer": cvr_number}},
        source=["Vrvirksomhed.cvrNummer", "Vrvirksomhed.deltagerRelation"],
        size=1,
    )
    if _is_error(result):
        return result

    hits = _hits(result)
    if not hits:
        return _not_found()

    company = hits[0]["_source"].get("Vrvirksomhed", {}) or {}
    direktion, fuldt_ansvarlige, ejere = _extract_current_relations(company)
    return {
        "vat": cvr_number,
        "direktion": direktion,
        "fuldt_ansvarlige": fuldt_ansvarlige,
        "ejere": ejere,
    }


def search_cvr_combined(
    name: Optional[str] = None,
    cvr: Optional[int] = None,
    limit: int = 100,
) -> Any:
    """Search by name and/or CVR number. At least one must be provided."""
    must: list = []
    if cvr is not None:
        must.append({"term": {"Vrvirksomhed.cvrNummer": cvr}})
    if name:
        must.append({
            "match_phrase_prefix": {
                "Vrvirksomhed.virksomhedMetadata.nyesteNavn.navn": name
            }
        })
    if not must:
        return []
    if not upstream_configured():
        params = {key: value for key, value in {"name": name, "cvr": cvr, "limit": limit}.items() if value is not None}
        return _hosted_get("/api/v1/search", params=params)

    result = _post_search({"bool": {"must": must}}, size=limit)
    if _is_error(result):
        return result
    return _companies_from_hits(_hits(result))


def search_cvr_by_name(company_name: str, limit: int = 100) -> Any:
    """Match companies whose current name starts with the query."""
    if not upstream_configured():
        return _hosted_get(f"/api/v1/search/company/{_hosted_segment(company_name)}", params={"limit": limit})
    result = _post_search(
        {"match_phrase_prefix": {
            "Vrvirksomhed.virksomhedMetadata.nyesteNavn.navn": company_name
        }},
        size=limit,
    )
    if _is_error(result):
        return result
    return _companies_from_hits(_hits(result))


def search_cvr_by_fuzzy_name(company_name: str, limit: int = 100) -> Any:
    """Fuzzy-match on current company name. Returns a slimmer shape."""
    if not upstream_configured():
        return _hosted_get(f"/api/v1/search/fuzzy/{_hosted_segment(company_name)}", params={"limit": limit}, model=CompanyFuzzy)
    result = _post_search(
        {"multi_match": {
            "query": company_name,
            "fields": ["Vrvirksomhed.virksomhedMetadata.nyesteNavn.navn^2"],
            "fuzziness": "AUTO",
        }},
        size=limit,
    )
    if _is_error(result):
        return result

    out = []
    for hit in _hits(result):
        company = hit.get("_source", {}).get("Vrvirksomhed", {})
        metadata = company.get("virksomhedMetadata") or {}
        nyeste_navn = metadata.get("nyesteNavn") or {}
        hovedbranche = metadata.get("nyesteHovedbranche") or {}
        out.append({
            "name": nyeste_navn.get("navn", "Unknown"),
            "cvr_number": company.get("cvrNummer"),
            "industrycode": hovedbranche.get("branchekode", "Unknown"),
            "industrytext": hovedbranche.get("branchetekst", "Unknown"),
        })
    return out


def search_cvr_by_email(email: str, limit: int = 100) -> Any:
    """Find companies registered with the given email address."""
    if not upstream_configured():
        return _hosted_get(f"/api/v1/search/email/{_hosted_segment(email)}", params={"limit": limit})
    result = _post_search(
        {"match": {"Vrvirksomhed.elektroniskPost.kontaktoplysning": email}},
        source=["*"],
        size=limit,
    )
    if _is_error(result):
        return result
    return _companies_from_hits(_hits(result))


def search_cvr_by_email_domain(email_domain: str, limit: int = 100) -> Any:
    """Find companies whose registered email is on the given domain."""
    if not upstream_configured():
        return _hosted_get(f"/api/v1/search/email-domain/{_hosted_segment(email_domain)}", params={"limit": limit})
    return search_cvr_by_email(f"@{email_domain}", limit=limit)


def search_cvr_by_phone(phone_number: str, limit: int = 100) -> Any:
    """Find companies by registered phone number."""
    if not upstream_configured():
        return _hosted_get(f"/api/v1/search/phone/{_hosted_segment(phone_number)}", params={"limit": limit})
    result = _post_search(
        {"match": {"Vrvirksomhed.telefonNummer.kontaktoplysning": phone_number}},
        source=["*"],
        size=limit,
    )
    if _is_error(result):
        return result
    return _companies_from_hits(_hits(result))


def search_cvr_by_address(
    address: str,
    postal_code: Optional[str] = None,
    limit: int = 100,
) -> Any:
    """Find companies at an address; falls back to structured then fuzzy match."""
    cleaned = address.strip()
    if not cleaned:
        return []
    if not upstream_configured():
        params = {"address": address, "limit": limit}
        if postal_code is not None:
            params["postal_code"] = postal_code
        return _hosted_get("/api/v1/search/address", params=params)

    components = _parse_address_components(cleaned)
    filters = _postal_code_filter(postal_code)

    for build in (
        lambda: _exact_address_query(cleaned, filters),
        lambda: _structured_address_query(components, filters),
        lambda: _fuzzy_address_query(cleaned, filters),
    ):
        query = build()
        if query is None:
            continue
        result = _post_search(query["query"], size=limit)
        if _is_error(result):
            return result
        companies = _companies_from_hits(_hits(result))
        if companies:
            return companies
    return []


def fetch_p_units(p_numbers: list) -> list:
    """Fetch full production-unit detail for each P-number. Returns [] on error."""
    if not p_numbers:
        return []
    result = _post_search(
        {"terms": {"VrproduktionsEnhed.pNummer": p_numbers}},
        endpoint=_PRODUCTION_UNIT_URL,
        source=["VrproduktionsEnhed"],
        size=1000,
    )
    return [
        format_p_unit_data(hit["_source"]["VrproduktionsEnhed"])
        for hit in _hits(result)
        if hit.get("_source", {}).get("VrproduktionsEnhed")
    ]


# --- Address query builders ------------------------------------------------

def _postal_code_filter(postal_code: Optional[str]) -> list:
    if not postal_code:
        return []
    trimmed = postal_code.strip()
    if not trimmed:
        return []
    value: Any = int(trimmed) if trimmed.isdigit() else trimmed
    return [{"term": {
        "Vrvirksomhed.virksomhedMetadata.nyesteBeliggenhedsadresse.postnummer": value
    }}]


def _exact_address_query(address: str, filters: list) -> Optional[dict]:
    if not address:
        return None
    bool_q: dict = {"must": [{
        "match_phrase": {
            "Vrvirksomhed.virksomhedMetadata.nyesteBeliggenhedsadresse.adressebetegnelse": address
        }
    }]}
    if filters:
        bool_q["filter"] = filters
    return {"query": {"bool": bool_q}}


def _structured_address_query(components: dict, filters: list) -> Optional[dict]:
    street = components.get("street")
    number = components.get("number")
    letter = components.get("letter")
    if not street or number is None:
        return None
    must = [
        {"match_phrase": {
            "Vrvirksomhed.virksomhedMetadata.nyesteBeliggenhedsadresse.vejnavn": street
        }},
        {"term": {
            "Vrvirksomhed.virksomhedMetadata.nyesteBeliggenhedsadresse.husnummerFra": number
        }},
    ]
    if letter:
        must.append({"term": {
            "Vrvirksomhed.virksomhedMetadata.nyesteBeliggenhedsadresse.bogstavFra": letter
        }})
    bool_q: dict = {"must": must}
    if filters:
        bool_q["filter"] = filters
    return {"query": {"bool": bool_q}}


def _fuzzy_address_query(address: str, filters: list) -> dict:
    bool_q: dict = {"must": [{
        "multi_match": {
            "query": address,
            "fields": [
                "Vrvirksomhed.virksomhedMetadata.nyesteBeliggenhedsadresse.adressebetegnelse^3",
                "Vrvirksomhed.virksomhedMetadata.nyesteBeliggenhedsadresse.fritekst^2",
                "Vrvirksomhed.virksomhedMetadata.nyesteBeliggenhedsadresse.vejnavn",
            ],
            "fuzziness": "AUTO",
            "type": "best_fields",
            "operator": "or",
        }
    }]}
    if filters:
        bool_q["filter"] = filters
    return {"query": {"bool": bool_q}}


_ADDRESS_RE = re.compile(
    r"^(?P<street>[^0-9]+?)\s+(?P<number>\d+)(?:\s*(?P<letter>[A-Za-z]))?$"
)


def _parse_address_components(address: str) -> dict:
    main = address.partition(",")[0].strip()
    if not main:
        return {"street": None, "number": None, "letter": None}
    m = _ADDRESS_RE.match(main)
    if not m:
        return {"street": None, "number": None, "letter": None}
    return {
        "street": (m.group("street") or "").strip() or None,
        "number": int(m.group("number")) if m.group("number") else None,
        "letter": (m.group("letter") or "").upper() or None,
    }


# --- Formatters ------------------------------------------------------------

def format_company_data(company: dict, cvr_number: int) -> dict:
    """Convert raw Vrvirksomhed to the public API response schema."""
    metadata = company.get("virksomhedMetadata") or {}
    hovedbranche = metadata.get("nyesteHovedbranche") or {}
    virksomhedsform = metadata.get("nyesteVirksomhedsform") or {}
    livsforloeb = company.get("livsforloeb") or []
    first_period = _first_period(livsforloeb)

    return {
        "vat": cvr_number,
        "name": _metadata_name(metadata),
        "address": _combined_address(metadata),
        "zipcode": _address_field(metadata, "postnummer"),
        "city": _address_field(metadata, "postdistrikt"),
        "cityname": _address_field(metadata, "bynavn"),
        "protected": company.get("reklamebeskyttet"),
        "phone": _contact_phone(metadata),
        "email": _contact_email(metadata),
        "fax": company.get("telefaxNummer"),
        "startdate": metadata.get("stiftelsesDato"),
        "enddate": first_period.get("gyldigTil"),
        "employees": _employees(metadata),
        "addressco": _address_field(metadata, "conavn"),
        "industrycode": hovedbranche.get("branchekode"),
        "industrydesc": hovedbranche.get("branchetekst"),
        "companycode": virksomhedsform.get("virksomhedsformkode"),
        "companydesc": virksomhedsform.get("langBeskrivelse"),
        "bankrupt": _is_bankrupt(metadata),
        "status": metadata.get("sammensatStatus"),
        "companytypeshort": virksomhedsform.get("kortBeskrivelse"),
        "website": _contact_website(metadata),
        "version": 1,
    }


def format_p_unit_data(p_unit: dict) -> dict:
    """Convert raw VrproduktionsEnhed to the public API response schema."""
    metadata = p_unit.get("produktionsEnhedMetadata") or {}
    hovedbranche = metadata.get("nyesteHovedbranche") or {}
    livsforloeb = p_unit.get("livsforloeb") or []
    first_period = _first_period(livsforloeb)

    return {
        "p_number": p_unit.get("pNummer"),
        "name": _metadata_name(metadata),
        "address": _combined_address(metadata),
        "zipcode": _address_field(metadata, "postnummer"),
        "city": _address_field(metadata, "postdistrikt"),
        "cityname": _address_field(metadata, "bynavn"),
        "addressco": _address_field(metadata, "conavn"),
        "phone": _contact_phone(metadata),
        "email": _contact_email(metadata),
        "website": _contact_website(metadata),
        "fax": p_unit.get("telefaxNummer"),
        "startdate": first_period.get("gyldigFra"),
        "enddate": first_period.get("gyldigTil"),
        "industrycode": hovedbranche.get("branchekode"),
        "industrydesc": hovedbranche.get("branchetekst"),
        "employees": _employees(metadata),
        "protected": p_unit.get("reklamebeskyttet"),
    }


# --- Metadata helpers ------------------------------------------------------

def _first_period(livsforloeb: list) -> dict:
    if livsforloeb and isinstance(livsforloeb[0], dict):
        return livsforloeb[0].get("periode") or {}
    return {}


def _metadata_name(metadata: dict) -> Optional[str]:
    return (metadata.get("nyesteNavn") or {}).get("navn")


def _combined_address(metadata: dict) -> Optional[str]:
    return _format_address_line(metadata.get("nyesteBeliggenhedsadresse") or {})


def _address_field(metadata: dict, field: str):
    return (metadata.get("nyesteBeliggenhedsadresse") or {}).get(field)


_PHONE_RE = re.compile(r"\b\d{8}\b")
_EMAIL_RE = re.compile(r"\b[\w.-]+@[\w.-]+\b")
_URL_RE = re.compile(
    r"\bhttps?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*(),]|(?:%[0-9a-fA-F]{2}))+\b"
)


def _contact_phone(metadata: dict) -> Optional[str]:
    info = metadata.get("nyesteKontaktoplysninger")
    if not info:
        return None
    match = _PHONE_RE.findall(str(info))
    return match[0] if match else None


def _contact_email(metadata: dict) -> Optional[str]:
    info = metadata.get("nyesteKontaktoplysninger")
    if not info:
        return None
    match = _EMAIL_RE.findall(str(info))
    return match[0] if match else None


def _contact_website(metadata: dict) -> Optional[str]:
    info = metadata.get("nyesteKontaktoplysninger")
    if not info:
        return None
    match = _URL_RE.findall(str(info))
    return match[0] if match else None


def _employees(metadata: dict) -> Optional[int]:
    return (metadata.get("nyesteErstMaanedsbeskaeftigelse") or {}).get("antalAnsatte")


def _is_bankrupt(metadata: dict) -> bool:
    return (metadata.get("nyesteStatus") or {}).get("kreditoplysningtekst") == "Konkurs"


# --- Address line formatting (shared by company + deltager) ---------------

def _format_address_line(address: dict) -> Optional[str]:
    vejnavn = address.get("vejnavn")
    if not vejnavn:
        return None
    line = f"{vejnavn} {address.get('husnummerFra', '') or ''}".rstrip()
    if address.get("husnummerTil"):
        line += f"-{address['husnummerTil']}"
    line += address.get("bogstavFra") or ""
    if address.get("bogstavTil"):
        line += f"-{address['bogstavTil']}"
    if address.get("etage"):
        line += f", {address['etage']}"
    return line


# --- Direktion / Fuldt ansvarlige / Ejere ---------------------------------

def _extract_current_relations(company: dict) -> tuple:
    """Return (direktion, fuldt_ansvarlige, ejere) — currently-active entries only."""
    direktion: list = []
    fuldt_ansvarlige: list = []
    ejere: list = []

    for relation in company.get("deltagerRelation") or []:
        deltager = relation.get("deltager") or {}
        for org in relation.get("organisationer") or []:
            hovedtype = org.get("hovedtype")
            if hovedtype == "LEDELSESORGAN":
                for value, periode in _iter_current_funktioner(org):
                    if value in _DIRECTOR_FUNCTIONS:
                        direktion.append(_format_deltager(deltager, value, periode.get("gyldigFra")))
            elif hovedtype == "FULDT_ANSVARLIG_DELTAGERE":
                for value, periode in _iter_current_funktioner(org):
                    fuldt_ansvarlige.append(_format_deltager(deltager, value, periode.get("gyldigFra")))
            elif hovedtype == "REGISTER" and _is_ejerregister(org):
                owner = _format_owner(deltager, org)
                if owner is not None:
                    ejere.append(owner)

    return direktion, fuldt_ansvarlige, ejere


def _iter_current_funktioner(organisation: dict) -> Iterable[tuple]:
    for member in organisation.get("medlemsData") or []:
        for attribute in member.get("attributter") or []:
            if attribute.get("type") != "FUNKTION":
                continue
            for value in attribute.get("vaerdier") or []:
                periode = value.get("periode") or {}
                if periode.get("gyldigTil") is None:
                    yield value.get("vaerdi"), periode


def _is_ejerregister(organisation: dict) -> bool:
    return any(
        isinstance(entry, dict) and entry.get("navn") == "EJERREGISTER"
        for entry in organisation.get("organisationsNavn") or []
    )


def _format_deltager(deltager: dict, role: str, startdate) -> dict:
    address = _current_deltager_address(deltager.get("beliggenhedsadresse"))
    enhedstype = deltager.get("enhedstype")
    return {
        "name": _current_deltager_name(deltager.get("navne")),
        "role": role,
        "type": enhedstype,
        "cvr": deltager.get("forretningsnoegle") if enhedstype == "VIRKSOMHED" else None,
        "address": _format_address_line(address) if address else None,
        "zipcode": address.get("postnummer") if address else None,
        "city": address.get("postdistrikt") if address else None,
        "country": address.get("landekode") if address else None,
        "startdate": startdate,
    }


def _format_owner(deltager: dict, organisation: dict) -> Optional[dict]:
    ownership = _current_owner_attribute(organisation, "EJERANDEL_PROCENT")
    voting = _current_owner_attribute(organisation, "EJERANDEL_STEMMERET_PROCENT")
    if ownership is None and voting is None:
        return None
    startdate = ownership[1] if ownership else (voting[1] if voting else None)

    address = _current_deltager_address(deltager.get("beliggenhedsadresse"))
    enhedstype = deltager.get("enhedstype")
    return {
        "name": _current_deltager_name(deltager.get("navne")),
        "type": enhedstype,
        "cvr": deltager.get("forretningsnoegle") if enhedstype == "VIRKSOMHED" else None,
        "address": _format_address_line(address) if address else None,
        "zipcode": address.get("postnummer") if address else None,
        "city": address.get("postdistrikt") if address else None,
        "country": address.get("landekode") if address else None,
        "ownership_percent": ownership[0] if ownership else None,
        "ownership_range": _ownership_range(ownership[0]) if ownership else None,
        "voting_rights_percent": voting[0] if voting else None,
        "voting_rights_range": _ownership_range(voting[0]) if voting else None,
        "startdate": startdate,
    }


def _current_owner_attribute(organisation: dict, attribute_type: str):
    for member in organisation.get("medlemsData") or []:
        for attribute in member.get("attributter") or []:
            if attribute.get("type") != attribute_type:
                continue
            for value in attribute.get("vaerdier") or []:
                periode = value.get("periode") or {}
                if periode.get("gyldigTil") is not None:
                    continue
                try:
                    return float(value.get("vaerdi")), periode.get("gyldigFra")
                except (TypeError, ValueError):
                    return None
    return None


def _ownership_range(percent: Optional[float]) -> Optional[str]:
    if percent is None:
        return None
    if percent >= 1.0:
        return "100%"
    for low, high, label in _OWNERSHIP_BUCKETS:
        if low <= percent < high:
            return label
    return None


def _current_deltager_name(navne) -> Optional[str]:
    if not navne:
        return None
    for entry in navne:
        if isinstance(entry, dict) and (entry.get("periode") or {}).get("gyldigTil") is None:
            navn = entry.get("navn")
            if navn:
                return navn
    first = navne[0]
    return first.get("navn") if isinstance(first, dict) else None


def _current_deltager_address(addresses) -> Optional[dict]:
    if not addresses:
        return None
    for entry in addresses:
        if isinstance(entry, dict) and (entry.get("periode") or {}).get("gyldigTil") is None:
            return entry
    first = addresses[0]
    return first if isinstance(first, dict) else None
