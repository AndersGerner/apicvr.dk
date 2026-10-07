"""FastAPI application entrypoint for apicvr.dk."""
import os
import secrets
import time
from typing import List, Optional

from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import PlainTextResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.routing import NoMatchFound

from apis.models import (
    Company,
    SigningCompany,
    CompanyFuzzy,
    CompanyRelations,
    not_found_responses,
    upstream_error_responses,
)
from apis.searchcvr import (
    get_company_relations,
    selected_provider,
    upstream_configured,
    search_cvr_api,
    search_cvr_by_address,
    search_cvr_by_email,
    search_cvr_by_email_domain,
    search_cvr_by_fuzzy_name,
    search_cvr_by_name,
    search_cvr_by_phone,
    search_cvr_combined,
)
from modules.kapitalsog import show_capital_result
from modules.stats import get_stats, init_db, log_request


if os.getenv("STATS_ENABLED") == "true":
    init_db()

security = HTTPBasic()


def verify_stats_auth(credentials: HTTPBasicCredentials = Depends(security)):
    if os.getenv("STATS_ENABLED") != "true":
        raise HTTPException(status_code=503, detail="Statistics are disabled")
    stats_password = os.getenv("STATS_PASSWORD", "")
    if not stats_password:
        raise HTTPException(status_code=503, detail="STATS_PASSWORD not configured")
    if not secrets.compare_digest(
        credentials.password.encode("utf-8"),
        stats_password.encode("utf-8"),
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Incorrect password",
            headers={"WWW-Authenticate": "Basic"},
        )


class RequestLogMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        start = time.perf_counter()
        response = await call_next(request)
        elapsed_ms = (time.perf_counter() - start) * 1000
        response.headers["Cache-Control"] = "no-store"
        # Stats are opt-in. Registry requests need no client IP or referrer storage.
        if os.getenv("STATS_ENABLED") == "true":
            log_request(request.method, request.url.path, response.status_code, elapsed_ms)
        return response


_API_DESCRIPTION = """
Free & open-source JSON API for the Danish company register (CVR).

When the operator configures `API_TOKEN`, endpoints query **distribution.virk.dk**
(ERST's official distribution API). Without it, company and search endpoints use
the hosted **apicvr.dk** API. Both modes return a stable shape.

### Quickstart

```bash
curl https://YOUR_SERVICE/api/v1/41013583/signing-profile
```

### What you can do

* **Look up a company by CVR** — full profile, production units,
  current *direktion*, *fully-liable participants*, and *legal owners*.
* **Search** by name (prefix or fuzzy), email, email domain, phone,
  or street address.
* **Just the leadership** — hit `/api/v1/{cvr}/direktion-og-ansvarlig`
  for a slim response with only the people/entities that matter.

### Auth

No client credentials. Official mode requires issued CVR distribution credentials.
Hosted fallback provides company enrichment but no registered signing rule or stable
signing-participant IDs, so its signing evidence is explicitly incomplete. A configured
token selects official mode exclusively; authentication failures do not trigger fallback.
Complete signing evidence still requires operator interpretation of the registered rule.

### Source & issues

<https://github.com/AndersGerner/apicvr.dk>
"""


_TAGS_METADATA = [
    {
        "name": "Companies",
        "description": "Look up a single company by CVR-number.",
    },
    {
        "name": "Relations",
        "description": "Current *direktion*, fully-liable participants, and legal owners.",
    },
    {
        "name": "Search",
        "description": "Find companies by name, email, phone, or address.",
    },
]


app = FastAPI(
    title="APICVR.dk",
    description=_API_DESCRIPTION,
    version="1.1",
    openapi_tags=_TAGS_METADATA,
    license_info={
        "name": "MIT License",
        "url": "https://raw.githubusercontent.com/NoahBohme/apicvr.dk/master/LICENSE",
    },
)

templates = Jinja2Templates(directory="frontend/templates")

app.add_middleware(RequestLogMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


# --- Helpers ---------------------------------------------------------------

_ERROR_STATUS = {
    "NOT_FOUND": 404,
    "NOT_CONFIGURED": 503,
    "INVALID_CVR": 400,
    "HTTP_ERROR": 502,
    "INVALID_RESPONSE": 502,
    "TRANSPORT_ERROR": 502,
}


def _unwrap(result):
    """Convert an error-dict from apis.searchcvr to an HTTPException; else pass through."""
    if isinstance(result, dict) and result.get("error"):
        code = result.get("error", "HTTP_ERROR")
        status_code = _ERROR_STATUS.get(code, 502)
        raise HTTPException(status_code=status_code, detail=result)
    return result


def _base_context(request: Request) -> dict:
    base_url = str(request.base_url).rstrip("/")
    try:
        docs_url = request.url_for("swagger_ui_html")
    except NoMatchFound:
        docs_url = f"{base_url}/docs"
    return {"request": request, "base_url": base_url, "docs_url": docs_url}


@app.get("/healthz", include_in_schema=False)
def health():
    return {"status": "ok"}


@app.get("/readyz", include_in_schema=False)
def ready():
    return {"status": "configured", "provider": selected_provider(),
            "signingEvidenceSupported": upstream_configured(), "upstreamVerified": False}


@app.get("/api/v1/{cvrNumber}/signing-profile", response_model=SigningCompany, tags=["Companies"],
         responses={**not_found_responses(), **upstream_error_responses()})
def signing_profile(cvrNumber: int):
    return _unwrap(search_cvr_api(cvrNumber, include_relations=False))


# --- Homepages -------------------------------------------------------------

@app.get("/", include_in_schema=False)
def home_da(request: Request):
    return templates.TemplateResponse(request, "/homepage.html", _base_context(request))


@app.get("/en/", include_in_schema=False)
def home_en(request: Request):
    return templates.TemplateResponse(request, "/homepage_en.html", _base_context(request))


# --- Search UI -------------------------------------------------------------

@app.get("/da/search/", include_in_schema=False)
def search_ui_da(request: Request):
    return templates.TemplateResponse(request, "/sogning.html", {"request": request})


# --- Company detail pages --------------------------------------------------

@app.get("/da/virksomhed/{cvrNumber}", include_in_schema=False)
def company_page_da(request: Request, cvrNumber: str):
    return templates.TemplateResponse(
        request, "/virksomhed.html",
        {"request": request, "cvrNumber": cvrNumber, "info": _lookup_for_template(cvrNumber, lang="da")},
    )


@app.get("/en/company/{cvrNumber}", include_in_schema=False)
def company_page_en(request: Request, cvrNumber: str):
    return templates.TemplateResponse(
        request, "/virksomhed_en.html",
        {"request": request, "cvrNumber": cvrNumber, "info": _lookup_for_template(cvrNumber, lang="en")},
    )


def _lookup_for_template(cvr_number: str, *, lang: str) -> dict:
    try:
        cvr_int = int(cvr_number)
    except ValueError:
        return {
            "error": "INVALID_CVR",
            "status": 400,
            "message": (
                "CVR-nummer skal bestå af 8 cifre." if lang == "da"
                else "CVR number must consist of 8 digits."
            ),
        }
    return search_cvr_api(cvr_int)


# --- Public JSON API -------------------------------------------------------
# Order matters: specific /api/v1/search* routes MUST come before the
# catch-all /api/v1/{cvrNumber} so "search" isn't parsed as an int CVR.

@app.get(
    "/api/v1/search",
    tags=["Search"],
    summary="Combined search by name and/or CVR",
    response_model=List[Company],
    responses=upstream_error_responses(),
)
def api_search(
    name: Optional[str] = None,
    cvr: Optional[int] = None,
    limit: int = 100,
):
    """
    Search by name (prefix), CVR number, or both. At least one is required.

    Returns a list of matching companies with the same shape as
    `GET /api/v1/{cvrNumber}` (minus production units and relations, which
    are only expanded for single-company lookups).
    """
    if not name and cvr is None:
        raise HTTPException(
            status_code=400,
            detail={"error": "MISSING_QUERY", "message": "At least one of 'name' or 'cvr' must be provided."},
        )
    return _unwrap(search_cvr_combined(name=name, cvr=cvr, limit=limit))


@app.get(
    "/api/v1/search/address",
    tags=["Search"],
    summary="Search by street address",
    response_model=List[Company],
    responses=upstream_error_responses(),
)
def api_search_by_address(address: str, postal_code: Optional[str] = None, limit: int = 100):
    """
    Find companies at a street address.

    The search runs three stages in order and returns the first non-empty
    result: exact `adressebetegnelse` match → structured street/number
    match → fuzzy multi-field match. Pass `postal_code` to narrow the
    search to a single postal district.
    """
    return _unwrap(search_cvr_by_address(address, postal_code, limit=limit))


@app.get(
    "/api/v1/search/company/{companyName}",
    tags=["Search"],
    summary="Prefix-match on company name",
    response_model=List[Company],
    responses=upstream_error_responses(),
)
def api_search_by_name(companyName: str, limit: int = 100):
    """Prefix-match on the current company name (`nyesteNavn`)."""
    return _unwrap(search_cvr_by_name(companyName, limit=limit))


@app.get(
    "/api/v1/search/fuzzy/{companyName}",
    tags=["Search"],
    summary="Fuzzy-match on company name",
    response_model=List[CompanyFuzzy],
    responses=upstream_error_responses(),
)
def api_search_fuzzy(companyName: str, limit: int = 100):
    """
    Fuzzy-match on the current company name.

    Returns a slimmer shape than the other search endpoints — useful for
    autocompletion and typo tolerance.
    """
    return _unwrap(search_cvr_by_fuzzy_name(companyName, limit=limit))


@app.get(
    "/api/v1/search/email/{email}",
    tags=["Search"],
    summary="Search by registered email",
    response_model=List[Company],
    responses=upstream_error_responses(),
)
def api_search_by_email(email: str, limit: int = 100):
    """Find companies with the given email on their public CVR record."""
    return _unwrap(search_cvr_by_email(email, limit=limit))


@app.get(
    "/api/v1/search/email-domain/{domain}",
    tags=["Search"],
    summary="Search by email domain",
    response_model=List[Company],
    responses=upstream_error_responses(),
)
def api_search_by_email_domain(domain: str, limit: int = 100):
    """Find companies whose registered email is on the given domain."""
    return _unwrap(search_cvr_by_email_domain(domain, limit=limit))


@app.get(
    "/api/v1/search/phone/{phone}",
    tags=["Search"],
    summary="Search by registered phone number",
    response_model=List[Company],
    responses=upstream_error_responses(),
)
def api_search_by_phone(phone: str, limit: int = 100):
    """Find companies with the given phone number on their public CVR record."""
    return _unwrap(search_cvr_by_phone(phone, limit=limit))


# --- Legacy aliases (kept for backwards compatibility, hidden from /docs) --

@app.get("/api/v1/fuzzy_search/company/{companyName}", include_in_schema=False)
def _legacy_fuzzy_search(companyName: str, limit: int = 100):
    return _unwrap(search_cvr_by_fuzzy_name(companyName, limit=limit))


@app.get("/api/v1/email/{email}", include_in_schema=False)
def _legacy_email(email: str, limit: int = 100):
    return _unwrap(search_cvr_by_email(email, limit=limit))


@app.get("/api/v1/email_domain/{domain}", include_in_schema=False)
def _legacy_email_domain(domain: str, limit: int = 100):
    return _unwrap(search_cvr_by_email_domain(domain, limit=limit))


@app.get("/api/v1/phone/{phone}", include_in_schema=False)
def _legacy_phone(phone: str, limit: int = 100):
    return _unwrap(search_cvr_by_phone(phone, limit=limit))


@app.get(
    "/api/v1/{cvrNumber}/direktion-og-ansvarlig",
    tags=["Relations"],
    summary="Direktion, fuldt ansvarlige & ejere",
    response_model=CompanyRelations,
    responses={**not_found_responses(), **upstream_error_responses()},
)
def api_relations(cvrNumber: int):
    """
    Return only the *currently-active* leadership and ownership of a company.

    * **direktion** — DIREKTØR / ADM. DIR.
    * **fuldt_ansvarlige** — participants with unlimited personal liability
      (typical for I/S, K/S, enkeltmandsvirksomhed).
    * **ejere** — legal owners with ≥5 % ownership or voting rights,
      including CVR display buckets (e.g. `25-33,32%`).
    """
    return _unwrap(get_company_relations(cvrNumber))


@app.get(
    "/api/v1/{cvrNumber}",
    tags=["Companies"],
    summary="Full company profile",
    response_model=Company,
    responses={**not_found_responses(), **upstream_error_responses()},
)
def api_company(cvrNumber: int):
    """
    Full company profile for a CVR-number.

    Includes: core metadata, contact info, industry, production units
    (P-numbers), plus currently-active *direktion*, *fuldt ansvarlige*
    and *legale ejere*.
    """
    return _unwrap(search_cvr_api(cvrNumber))


# --- Kapitalsøg (capital-raise search) ------------------------------------

@app.get("/da/kapitalsog/", include_in_schema=False)
def kapitalsog_ui(request: Request):
    return templates.TemplateResponse(request, "/kapitalsog.html", {"request": request})


@app.get("/da/kapitalindsigt/{cvrNumber}", include_in_schema=False)
def kapitalindsigt_page(request: Request, cvrNumber: str):
    return templates.TemplateResponse(
        request, "/kapitalresultat.html",
        {"request": request, "data": show_capital_result(cvrNumber)},
    )


# --- Stats -----------------------------------------------------------------

@app.get("/stats", include_in_schema=False)
def stats_dashboard(request: Request, _: bool = Depends(verify_stats_auth)):
    return templates.TemplateResponse(request, "/stats.html", {"request": request, "stats": get_stats()})


# --- SEO / crawler files ---------------------------------------------------

@app.get("/robots.txt", response_class=PlainTextResponse, include_in_schema=False)
def robots_txt():
    return (
        "User-agent: *\n"
        "Allow: /\n"
        "Disallow: /stats\n"
        "Disallow: /da/search/\n"
        "Disallow: /da/virksomhed/\n"
        "Disallow: /en/company/\n"
        "\n"
        "Sitemap: https://apicvr.dk/sitemap.xml\n"
    )


_SITEMAP_URLS = [
    # (loc, hreflang_pairs, changefreq, priority)
    ("https://apicvr.dk/", [("da", "https://apicvr.dk/"), ("en", "https://apicvr.dk/en/")], "weekly", "1.0"),
    ("https://apicvr.dk/en/", [("da", "https://apicvr.dk/"), ("en", "https://apicvr.dk/en/")], "weekly", "1.0"),
    ("https://apicvr.dk/docs", [], "monthly", "0.8"),
    ("https://apicvr.dk/da/kapitalsog/", [], "monthly", "0.5"),
]


@app.get("/sitemap.xml", include_in_schema=False)
def sitemap():
    def _url(loc, hreflangs, freq, prio):
        lines = [f"  <url>", f"    <loc>{loc}</loc>"]
        for lang, href in hreflangs:
            lines.append(f'    <xhtml:link rel="alternate" hreflang="{lang}" href="{href}"/>')
        lines.append(f"    <changefreq>{freq}</changefreq>")
        lines.append(f"    <priority>{prio}</priority>")
        lines.append("  </url>")
        return "\n".join(lines)

    body = "\n".join(_url(*entry) for entry in _SITEMAP_URLS)
    content = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9"\n'
        '        xmlns:xhtml="http://www.w3.org/1999/xhtml">\n'
        f"{body}\n"
        "</urlset>"
    )
    return Response(content=content, media_type="application/xml")
