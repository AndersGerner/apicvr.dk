"""
Pydantic response models for the public JSON API.

These are used with FastAPI's ``response_model`` so the OpenAPI schema
(and /docs) shows a real contract. ``extra = "allow"`` keeps any future
fields flowing through without needing a schema bump on the caller side.
"""
from datetime import date, datetime
from typing import Annotated, Any, List, Optional, Union
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class _Loose(BaseModel):
    model_config = ConfigDict(extra="allow")


# --- Building blocks -------------------------------------------------------

class ErrorDetail(_Loose):
    error: str = Field(..., description="Machine-readable error code.", example="NOT_FOUND")
    status: Optional[int] = Field(None, description="Upstream HTTP status, if applicable.", example=404)
    message: Optional[str] = Field(None, description="Human-readable error message.")


class ErrorResponse(_Loose):
    detail: ErrorDetail


class PUnit(_Loose):
    p_number: Optional[int] = Field(None, description="Production-unit (P-)number.")
    name: Optional[str] = None
    address: Optional[str] = None
    zipcode: Optional[int] = None
    city: Optional[str] = None
    cityname: Optional[str] = None
    addressco: Optional[str] = None
    phone: Optional[str] = None
    email: Optional[str] = None
    website: Optional[str] = None
    fax: Optional[Any] = None
    startdate: Optional[str] = None
    enddate: Optional[str] = None
    industrycode: Optional[int] = None
    industrydesc: Optional[str] = None
    employees: Optional[int] = None
    protected: Optional[bool] = None


class _Person(_Loose):
    name: Optional[str] = None
    role: str = Field(..., description="Role code as stored in CVR (e.g. DIREKTØR, INTERESSENTER).")
    type: str = Field(..., description="'PERSON' or 'VIRKSOMHED'.")
    cvr: Optional[int] = Field(None, description="Company CVR-number (only for VIRKSOMHED).")
    address: Optional[str] = None
    zipcode: Optional[int] = None
    city: Optional[str] = None
    country: Optional[str] = Field(None, description="ISO 3166-1 alpha-2 country code.")
    startdate: Optional[str] = Field(None, description="Role start date (YYYY-MM-DD).")


class Direktor(_Person):
    """A current director (DIREKTØR / ADM. DIR.)."""


class FuldtAnsvarlig(_Person):
    """A currently-registered fully-liable participant."""


class Ejer(_Loose):
    """A currently-registered legal owner (ejerregister)."""
    name: Optional[str] = None
    type: str = Field(..., description="'PERSON' or 'VIRKSOMHED'.")
    cvr: Optional[int] = None
    address: Optional[str] = None
    zipcode: Optional[int] = None
    city: Optional[str] = None
    country: Optional[str] = None
    ownership_percent: Optional[float] = Field(
        None, description="Raw ownership fraction (0-1)."
    )
    ownership_range: Optional[str] = Field(
        None, description="Display bucket, e.g. '25-33,32%'.", example="25-33,32%"
    )
    voting_rights_percent: Optional[float] = None
    voting_rights_range: Optional[str] = None
    startdate: Optional[str] = None


# --- Top-level responses --------------------------------------------------

class SigningValidity(BaseModel):
    validFrom: Optional[date]
    validTo: Optional[date]
    updatedAt: Optional[Union[date, datetime]]


class SigningRule(SigningValidity):
    text: str = Field(..., min_length=1, max_length=10000)


class SigningRole(SigningValidity):
    organisationType: Literal["LEDELSESORGAN", "TEGNINGSBERETTIGEDE", "FULDT_ANSVARLIG_DELTAGERE"]
    role: str = Field(..., min_length=1, max_length=200)


class SigningParticipant(BaseModel):
    unitId: str = Field(..., pattern=r"^[0-9]{10}$")
    name: str = Field(..., min_length=1, max_length=300)
    entityType: Literal["PERSON", "VIRKSOMHED", "ANDEN DELTAGER"]
    registeredRepresentative: bool
    roles: List[SigningRole] = Field(..., min_length=1, max_length=100)


class SigningSource(BaseModel):
    register_name: Literal["CVR"] = Field(..., alias="register")
    observedAt: datetime
    companyUpdatedAt: Optional[Union[date, datetime]]


class SigningEvidence(BaseModel):
    schemaVersion: Literal[1]
    status: Literal["available", "missing_rule", "conflicting_rules", "incomplete"]
    rules: List[SigningRule] = Field(..., max_length=10)
    participants: List[SigningParticipant] = Field(..., max_length=200)
    source: SigningSource


class Company(_Loose):
    """Full company profile returned by ``/api/v1/{cvr}``."""
    vat: int = Field(..., description="CVR-number (8 digits).", example=41013583)
    name: Optional[str] = None
    address: Optional[str] = None
    zipcode: Optional[int] = None
    city: Optional[str] = None
    cityname: Optional[str] = None
    protected: Optional[bool] = Field(None, description="True if the company opted out of marketing use.")
    phone: Optional[str] = None
    email: Optional[str] = None
    fax: Optional[Any] = None
    startdate: Optional[str] = Field(None, description="Founding date (YYYY-MM-DD).")
    enddate: Optional[str] = None
    employees: Optional[int] = None
    addressco: Optional[str] = None
    industrycode: Optional[int] = None
    industrydesc: Optional[str] = None
    companycode: Optional[int] = None
    companydesc: Optional[str] = None
    bankrupt: Optional[bool] = None
    status: Optional[str] = Field(None, description="e.g. NORMAL, OPHØRT, UNDERKONKURS.")
    companytypeshort: Optional[str] = None
    website: Optional[str] = None
    version: int = 1
    signing: Optional[SigningEvidence] = Field(None, description="Registered rule and current signing-role evidence; not a determination that one person may sign alone.")
    p_units: List[PUnit] = []
    direktion: List[Direktor] = []
    fuldt_ansvarlige: List[FuldtAnsvarlig] = []
    ejere: List[Ejer] = []


class CompanyRelations(_Loose):
    """Slim response returned by ``/api/v1/{cvr}/direktion-og-ansvarlig``."""
    vat: int
    direktion: List[Direktor]
    fuldt_ansvarlige: List[FuldtAnsvarlig]
    ejere: List[Ejer]


class CompanyFuzzy(_Loose):
    """Slim shape returned by the fuzzy-search endpoint."""
    name: str
    cvr_number: Optional[int] = None
    industrycode: Union[int, str, None] = None
    industrytext: Union[str, None] = None


# --- Response docs helpers -------------------------------------------------

def not_found_responses() -> dict:
    """Attach a 404 shape to routes that can miss."""
    return {404: {"model": ErrorResponse, "description": "Company not found"}}


def upstream_error_responses() -> dict:
    """Attach a 502 shape for upstream failures."""
    return {502: {"model": ErrorResponse, "description": "CVR distribution API error"}}


class ContactSuggestion(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(..., min_length=1, max_length=300)
    roles: List[Annotated[str, Field(min_length=1, max_length=200)]] = Field(..., min_length=1, max_length=20)


class EmployeePeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")
    year: int = Field(..., strict=True, ge=1900, le=2200)
    month: Optional[int] = Field(None, strict=True, ge=1, le=12)


class ProfileProductionUnit(BaseModel):
    model_config = ConfigDict(extra="forbid")
    pNumber: str = Field(..., pattern=r"^[0-9]{10}$")
    name: Optional[str] = Field(None, max_length=300)
    address: Optional[str] = Field(None, max_length=250)
    postalCode: Optional[str] = Field(None, max_length=32)
    city: Optional[str] = Field(None, max_length=100)


class ProfileCompanyOwner(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cvr: str = Field(..., pattern=r"^[1-9][0-9]{7}$")
    name: str = Field(..., min_length=1, max_length=300)
    ownershipRange: Optional[str] = Field(None, max_length=100)
    votingRightsRange: Optional[str] = Field(None, max_length=100)


class CompanyProfile(BaseModel):
    """Auxiliary company information, separate from signing evidence."""
    model_config = ConfigDict(extra="forbid")
    companyEmail: Optional[str] = Field(None, max_length=320)
    companyPhone: Optional[str] = Field(None, max_length=50)
    website: Optional[str] = Field(None, max_length=2000)
    industryCode: Optional[str] = Field(None, max_length=20)
    industryDescription: Optional[str] = Field(None, max_length=300)
    companyStatus: Optional[str] = Field(None, max_length=200)
    bankrupt: Optional[bool] = Field(None, strict=True)
    foundedOn: Optional[date] = None
    closedOn: Optional[date] = None
    employeeCount: Optional[int] = Field(None, strict=True, ge=0, le=100000000)
    employeePeriod: Optional[EmployeePeriod] = None
    productionUnits: Optional[List[ProfileProductionUnit]] = Field(None, max_length=200)
    productionUnitsComplete: Optional[bool] = Field(None, strict=True)
    companyOwners: Optional[List[ProfileCompanyOwner]] = Field(None, max_length=200)


class SigningCompany(BaseModel):
    """Bounded Lead response; excludes personal addresses and personal owner data."""
    vat: int
    name: Optional[str] = None
    address: Optional[str] = None
    zipcode: Optional[int] = None
    city: Optional[str] = None
    companydesc: Optional[str] = None
    status: Optional[str] = None
    protected: Optional[bool] = None
    signing: SigningEvidence
    contactSuggestions: List[ContactSuggestion] = Field(default_factory=list, max_length=200)
    profile: Optional[CompanyProfile] = None
