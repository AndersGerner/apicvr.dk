import copy
from datetime import date, datetime, timezone
import json
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient
import httpx

from apis import searchcvr
from apis.company_profile import hosted_profile, official_profile
from apis.models import SigningCompany
from apis.signing import extract_signing
from main import app
import test_hosted_fallback as hosted_tests
from test_hosted_fallback import PUBLIC_COMPANY
from test_signing_evidence import company, value


TODAY = date(2026, 10, 8)
OBSERVED = datetime(2026, 10, 8, 12, tzinfo=timezone.utc)


def owner_relation(entity_type="VIRKSOMHED", *, start="2020-01-01", end=None, name="Corporate Owner", identifier=22345672):
    return {"deltager": {"enhedstype": entity_type, "forretningsnoegle": identifier,
                           "navne": [{"navn": name, "periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}}],
                           "beliggenhedsadresse": [{"vejnavn": "PRIVATE ADDRESS"}]},
            "organisationer": [{"hovedtype": "REGISTER", "organisationsNavn": [{"navn": "EJERREGISTER"}],
                                "medlemsData": [{"attributter": [
                                    {"type": "EJERANDEL_PROCENT", "vaerdier": [value("0.5", start=start, end=end)]},
                                    {"type": "EJERANDEL_STEMMERET_PROCENT", "vaerdier": [value("1", start=start, end=end)]}]}]}]}


def source_company():
    source = company()
    source["cvrNummer"] = 12345674
    source["virksomhedMetadata"] = {
        "nyesteNavn": {"navn": "Synthetic Company"},
        "nyesteKontaktoplysninger": ["info@example.test", "12345678", "https://example.test"],
        "nyesteHovedbranche": {"branchekode": 620100, "branchetekst": "Software"},
        "sammensatStatus": "NORMAL", "nyesteStatus": {"kreditoplysningtekst": "Normal"},
        "stiftelsesDato": "2020-01-01", "nyesteErstMaanedsbeskaeftigelse": {"antalAnsatte": 0, "aar": 2026, "maaned": 8},
    }
    source["livsforloeb"] = [{"periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}}]
    source["penheder"] = []
    source["deltagerRelation"] += [owner_relation(), owner_relation("PERSON", name="Person Owner")]
    return source


def unit(number="1000000001", cvr=12345674):
    return {"pNummer": number, "virksomhedsrelation": [{"cvrNummer": cvr, "periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}}],
            "produktionsEnhedMetadata": {"nyesteNavn": {"navn": "Unit"}, "nyesteBeliggenhedsadresse": {
                "vejnavn": "Business Street", "husnummerFra": 1, "postnummer": 2800, "postdistrikt": "City"}}}


def response_hits(documents, kind="Vrvirksomhed", total=None):
    return {"hits": {"total": len(documents) if total is None else total,
                     "hits": [{"_source": {kind: document}} for document in documents]}}


class CompanyProfileTests(unittest.TestCase):
    def test_hosted_missing_empty_invalid_and_zero_remain_distinct(self):
        missing = hosted_profile({}, today=TODAY)
        self.assertTrue(all(item is None for item in missing.values()))
        raw = {"employees": 0, "bankrupt": False, "p_units": [], "ejere": [], "industrycode": 0}
        profile = hosted_profile(raw, today=TODAY)
        self.assertEqual(profile["employeeCount"], 0)
        self.assertFalse(profile["bankrupt"])
        self.assertEqual(profile["industryCode"], "0")
        self.assertEqual(profile["productionUnits"], [])
        self.assertIsNone(profile["productionUnitsComplete"])
        self.assertEqual(profile["companyOwners"], [])
        self.assertIsNone(profile["employeePeriod"])
        self.assertEqual(hosted_profile({"employees": 100000000}, today=TODAY)["employeeCount"], 100000000)
        for raw_value in [True, -1, 1.2, "2", None, 100000001]:
            with self.subTest(value=raw_value):
                self.assertIsNone(hosted_profile({"employees": raw_value}, today=TODAY)["employeeCount"])
        for field, raw_value in [("bankrupt", "false"), ("email", []), ("phone", "x" * 51), ("industrydesc", "x" * 301), ("p_units", "invalid"), ("ejere", "invalid")]:
            result = hosted_profile({field: raw_value}, today=TODAY)
            self.assertTrue(all(item is None for item in result.values()))

    def test_http_urls_reject_credentials_invalid_schemes_and_parser_ambiguity(self):
        for raw in ["ftp://example.test", "https://user:pass@example.test", "https://example.test@other.test", "https://example.test:99999", "https://", "https://bad host", "https://example.test\\@other.test", "javascript:alert(1)"]:
            with self.subTest(url=raw):
                self.assertIsNone(hosted_profile({"website": raw}, today=TODAY)["website"])
        for raw in ["http://example.test", "https://example.test/path?value=1"]:
            self.assertEqual(hosted_profile({"website": raw}, today=TODAY)["website"], raw)

    def test_dates_validate_future_leap_day_and_closure_order(self):
        for raw in ["2026-02-29", "2026-13-01", "2999-01-01", "invalid", 0]:
            with self.subTest(raw=raw):
                self.assertIsNone(hosted_profile({"startdate": raw, "enddate": raw}, today=TODAY)["foundedOn"])
                self.assertIsNone(hosted_profile({"startdate": raw, "enddate": raw}, today=TODAY)["closedOn"])
        profile = hosted_profile({"startdate": "2024-02-29T00:00:00", "enddate": "2020-01-01"}, today=TODAY)
        self.assertEqual(profile["foundedOn"], "2024-02-29")
        self.assertIsNone(profile["closedOn"])

    def test_hosted_owners_are_company_only_current_bounded_and_preserve_ranges(self):
        base = {"type": "VIRKSOMHED", "cvr": 22345672, "name": "Owner", "ownership_range": "25-33,32%", "voting_rights_range": "100%", "address": "PRIVATE", "startdate": "2020-01-01"}
        raw = {"ejere": [base, {**base, "type": "PERSON"}, {**base, "startdate": "2999-01-01"}, {**base, "enddate": "2000-01-01"}, {**base, "cvr": True}, {**base, "name": "x" * 301}]}
        profile = hosted_profile(raw, today=TODAY)
        self.assertEqual(profile["companyOwners"], [{"cvr": "22345672", "name": "Owner", "ownershipRange": "25-33,32%", "votingRightsRange": "100%"}])
        self.assertNotIn("PRIVATE", json.dumps(profile))
        identifiers = [number for number in range(10000000, 10003000) if sum(int(digit) * weight for digit, weight in zip(str(number), [2, 7, 6, 5, 4, 3, 2, 1])) % 11 == 0]
        raw["ejere"] = [{**base, "cvr": number} for number in identifiers[:201]]
        self.assertIsNone(hosted_profile(raw, today=TODAY)["companyOwners"])

    def test_hosted_supplied_ownership_must_include_a_valid_positive_relation(self):
        base = {"type": "VIRKSOMHED", "cvr": 22345672, "name": "Owner"}
        for invalid in [0, -1, 2, float("nan"), float("inf"), "malformed", True, 10 ** 400]:
            with self.subTest(invalid=invalid):
                owners = [{**base, "ownership_percent": invalid, "voting_rights_percent": 0}]
                self.assertEqual(hosted_profile({"ejere": owners}, today=TODAY)["companyOwners"], [])
        for evidence in [{}, {"ownership_percent": None}, {"ownership_percent": 0, "voting_rights_percent": 0.5}]:
            self.assertEqual(len(hosted_profile({"ejere": [{**base, **evidence}]}, today=TODAY)["companyOwners"]), 1)

    def test_optional_unit_fields_have_consumer_bounds_and_sanitize_independently(self):
        raw = {"p_units": [{"p_number": "1000000001", "name": "x" * 301,
                            "address": "x" * 251, "zipcode": "x" * 33, "city": "x" * 101}]}
        profile = hosted_profile(raw, today=TODAY)
        self.assertEqual(profile["productionUnits"], [{"pNumber": "1000000001", "name": None, "address": None, "postalCode": None, "city": None}])
        self.assertIsNone(profile["productionUnitsComplete"])

    def test_owner_cvr_is_normalized_and_check_digit_invalid_owner_is_omitted(self):
        owners = [{"type": "VIRKSOMHED", "cvr": " DK 2234 5672 ", "name": "Valid"},
                  {"type": "VIRKSOMHED", "cvr": 22345673, "name": "Invalid check digit"}]
        profile = hosted_profile({"ejere": owners}, today=TODAY)
        self.assertEqual(profile["companyOwners"], [{"cvr": "22345672", "name": "Valid", "ownershipRange": None, "votingRightsRange": None}])

    def test_hosted_units_are_whitelisted_sorted_capped_and_unknown_data_is_missing(self):
        raw = [{"p_number": 1000000000 + i, "name": "Unit", "zipcode": 2800, "private": "SECRET"} for i in range(201, 0, -1)]
        profile = hosted_profile({"p_units": raw}, today=TODAY)
        self.assertEqual(len(profile["productionUnits"]), 200)
        self.assertEqual(profile["productionUnits"][0]["pNumber"], "1000000001")
        self.assertEqual(profile["productionUnits"][0]["postalCode"], "2800")
        self.assertFalse(profile["productionUnitsComplete"])
        self.assertNotIn("SECRET", json.dumps(profile))
        profile = hosted_profile({"p_units": [{"p_number": "INVALID"}]}, today=TODAY)
        self.assertEqual(profile["productionUnits"], [])
        self.assertFalse(profile["productionUnitsComplete"])

    def test_official_profile_preserves_signing_and_current_company_owner_ranges(self):
        source = source_company()
        source["deltagerRelation"] += [owner_relation(start="2999-01-01"), owner_relation(end="2020-01-02"), owner_relation(identifier=12345674)]
        signing = extract_signing(source, observed_at=OBSERVED)
        before = copy.deepcopy(signing)
        data = searchcvr.format_company_data(source, 12345674)
        profile = official_profile(source, data, TODAY, searchcvr._ownership_range)
        self.assertEqual(profile["employeeCount"], 0)
        self.assertEqual(profile["employeePeriod"], {"year": 2026, "month": 8})
        self.assertFalse(profile["bankrupt"])
        self.assertEqual([owner["cvr"] for owner in profile["companyOwners"]], ["12345674", "22345672"])
        self.assertEqual(profile["companyOwners"][0]["ownershipRange"], "50-66,66%")
        self.assertEqual(profile["companyOwners"][0]["votingRightsRange"], "100%")
        self.assertNotIn("PRIVATE", json.dumps(profile))
        self.assertNotIn("Person Owner", json.dumps(profile))
        self.assertEqual(signing, before)

    def test_conflicting_collection_identities_are_omitted_without_order_dependent_winners(self):
        unit_one = {"p_number": "1000000001", "name": "First"}
        owner_one = {"type": "VIRKSOMHED", "cvr": 22345672, "name": "First", "ownership_range": "100%"}
        for items in [[unit_one, {**unit_one, "name": "Second"}, unit_one], [unit_one, unit_one, {**unit_one, "name": "Second"}]]:
            profile = hosted_profile({"p_units": items}, today=TODAY)
            self.assertEqual(profile["productionUnits"], [])
            self.assertFalse(profile["productionUnitsComplete"])
        for items in [[owner_one, {**owner_one, "ownership_range": "50-66,66%"}, owner_one], [owner_one, owner_one, {**owner_one, "ownership_range": "50-66,66%"}]]:
            self.assertEqual(hosted_profile({"ejere": items}, today=TODAY)["companyOwners"], [])

    def test_official_employee_periods_do_not_invent_or_coerce_reporting_dates(self):
        source = source_company()
        for year, month, expected in [(2025, None, {"year": 2025, "month": None}), (2026, 0, None), (2026, 13, None), (2026, True, None), ("2026", 8, None), (1899, 8, None), (2201, 8, None), (2027, 1, None), (2026, 11, None), (None, 8, None)]:
            with self.subTest(year=year, month=month):
                source["virksomhedMetadata"]["nyesteErstMaanedsbeskaeftigelse"] = {"aar": year, "maaned": month, "antalAnsatte": 0}
                data = searchcvr.format_company_data(source, 12345674)
                result = official_profile(source, data, TODAY, searchcvr._ownership_range)
                self.assertEqual(result["employeePeriod"], expected)
                self.assertEqual(result["employeeCount"], 0)

    def test_official_reopened_company_does_not_keep_old_closure(self):
        source = source_company()
        source["livsforloeb"] = [{"periode": {"gyldigFra": "2020-01-01", "gyldigTil": "2021-01-01"}}, {"periode": {"gyldigFra": "2022-01-01", "gyldigTil": None}}]
        profile = official_profile(source, searchcvr.format_company_data(source, 12345674), TODAY, searchcvr._ownership_range)
        self.assertIsNone(profile["closedOn"])
        for end in ["2999-01-01", "invalid", "2021-01-01"]:
            source["livsforloeb"][1]["periode"]["gyldigTil"] = end
            self.assertIsNone(official_profile(source, searchcvr.format_company_data(source, 12345674), TODAY, searchcvr._ownership_range)["closedOn"])
        source["livsforloeb"][1]["periode"]["gyldigTil"] = "2025-01-01"
        self.assertEqual(official_profile(source, searchcvr.format_company_data(source, 12345674), TODAY, searchcvr._ownership_range)["closedOn"], "2025-01-01")


class ProfileWiringTests(unittest.TestCase):
    def lookup(self, source, units=None):
        responses = [response_hits([source])]
        if units is not None:
            responses.append(units)
        with patch.object(searchcvr, "_API_TOKEN", "synthetic"), patch.object(searchcvr, "_post_search", side_effect=responses) as transport, patch.object(searchcvr, "extract_signing", side_effect=lambda source: extract_signing(source, observed_at=OBSERVED)):
            result = searchcvr.search_cvr_api(12345674, include_relations=False)
        return result, transport

    def test_official_empty_and_missing_units_need_no_extra_request(self):
        source = source_company()
        result, transport = self.lookup(source)
        self.assertEqual(result["profile"]["productionUnits"], [])
        self.assertTrue(result["profile"]["productionUnitsComplete"])
        self.assertEqual(transport.call_count, 1)
        source.pop("penheder")
        result, transport = self.lookup(source)
        self.assertIsNone(result["profile"]["productionUnits"])
        self.assertIsNone(result["profile"]["productionUnitsComplete"])
        self.assertEqual(transport.call_count, 1)

    def test_official_units_use_one_grouped_request_with_exact_completeness(self):
        source = source_company()
        source["penheder"] = [{"pNummer": "1000000001", "periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}}, {"pNummer": "1000000002", "periode": {"gyldigFra": "2999-01-01", "gyldigTil": None}}]
        result, transport = self.lookup(source, response_hits([unit()], "VrproduktionsEnhed", {"value": 1, "relation": "eq"}))
        self.assertEqual(transport.call_count, 2)
        self.assertEqual(transport.call_args.kwargs["size"], 200)
        self.assertEqual(transport.call_args.args[0]["terms"]["VrproduktionsEnhed.pNummer"], ["1000000001"])
        self.assertTrue(result["profile"]["productionUnitsComplete"])
        self.assertEqual(result["profile"]["productionUnits"][0], {"pNumber": "1000000001", "name": "Unit", "address": "Business Street 1", "postalCode": "2800", "city": "City"})
        SigningCompany.model_validate(result)

    def test_ownership_conversion_overflow_does_not_break_company_or_signing(self):
        source = source_company()
        oversized = owner_relation()
        for attribute in oversized["organisationer"][0]["medlemsData"][0]["attributter"]:
            attribute["vaerdier"][0]["vaerdi"] = 10 ** 400
        person = copy.deepcopy(oversized)
        person["deltager"]["enhedstype"] = "PERSON"
        source["deltagerRelation"] = [oversized, person]
        result, _ = self.lookup(source)
        self.assertEqual(result["name"], "Synthetic Company")
        self.assertEqual(result["profile"]["companyOwners"], [])
        self.assertIn(result["signing"]["status"], ["available", "incomplete"])
        SigningCompany.model_validate(result)

    def test_reopened_unit_uses_current_lifecycle_and_keeps_completeness_exact(self):
        source = source_company()
        source["penheder"] = [{"pNummer": "1000000001", "periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}}]
        reopened = unit()
        reopened["livsforloeb"] = [
            {"periode": {"gyldigFra": "2010-01-01", "gyldigTil": "2012-01-01"}},
            {"periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}},
        ]
        result, _ = self.lookup(source, response_hits([reopened], "VrproduktionsEnhed"))
        self.assertEqual(len(result["profile"]["productionUnits"]), 1)
        self.assertTrue(result["profile"]["productionUnitsComplete"])
        for lifecycle in [reopened["livsforloeb"][:1], "invalid", [{"periode": {"gyldigFra": "invalid", "gyldigTil": None}}],
                          [{"periode": {"gyldigFra": "invalid", "gyldigTil": None}}, {"periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}}],
                          [None, {"periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}}]]:
            with self.subTest(lifecycle=lifecycle):
                reopened["livsforloeb"] = lifecycle
                result, _ = self.lookup(source, response_hits([reopened], "VrproduktionsEnhed"))
                self.assertEqual(result["profile"]["productionUnits"], [])
                self.assertFalse(result["profile"]["productionUnitsComplete"])

    def test_official_unit_failure_never_becomes_complete_empty(self):
        source = source_company()
        source["penheder"] = [{"pNummer": "1000000001", "periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}}]
        for upstream in [{"error": "HTTP_ERROR", "status": 502, "message": None}, {}, {"hits": {"hits": "invalid"}}, response_hits([], "VrproduktionsEnhed"), response_hits([unit(cvr=87654321)], "VrproduktionsEnhed"), response_hits([unit("1000000002")], "VrproduktionsEnhed"), response_hits([unit(), unit()], "VrproduktionsEnhed"), response_hits([unit()], "VrproduktionsEnhed", {"value": 2, "relation": "gte"})]:
            with self.subTest(upstream=upstream):
                result, _ = self.lookup(source, upstream)
                self.assertFalse(result["profile"]["productionUnitsComplete"])
                self.assertEqual(result["signing"]["status"], "available")
                if "error" in upstream or "hits" not in upstream:
                    self.assertIsNone(result["profile"]["productionUnits"])

    def test_official_units_cap_request_and_mark_truncation(self):
        source = source_company()
        source["penheder"] = [{"pNummer": str(1000000000 + i), "periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}} for i in range(1, 202)]
        units = [unit(str(1000000000 + i)) for i in range(1, 201)]
        result, transport = self.lookup(source, response_hits(units, "VrproduktionsEnhed"))
        self.assertEqual(len(transport.call_args.args[0]["terms"]["VrproduktionsEnhed.pNummer"]), 200)
        self.assertEqual(len(result["profile"]["productionUnits"]), 200)
        self.assertFalse(result["profile"]["productionUnitsComplete"])

    def test_hosted_endpoint_fetches_once_and_keeps_people_in_contact_suggestions(self):
        fixture = hosted_tests.HostedFallbackTests()
        raw = {**PUBLIC_COMPANY, "email": "info@example.test", "phone": "12345678", "employees": 0, "bankrupt": False,
               "website": "https://user:password@example.test", "p_units": [], "ejere": [
                   {"type": "PERSON", "name": "Person Owner", "address": "PRIVATE", "ownership_percent": 1},
                   {"type": "VIRKSOMHED", "cvr": 22345672, "name": "Corporate Owner", "address": "PRIVATE", "ownership_range": "100%"}],
               "profile": {"employeePeriod": {"year": 2026, "month": 8}, "companyOwners": [{"name": "INJECTED"}]}}
        with fixture.upstream(lambda _: httpx.Response(200, json=raw)) as (requests, _):
            response = TestClient(app).get("/api/v1/12345674/signing-profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(requests), 1)
        result = response.json()
        self.assertEqual(result["signing"]["status"], "incomplete")
        self.assertEqual(result["profile"]["employeeCount"], 0)
        self.assertIsNone(result["profile"]["employeePeriod"])
        self.assertIsNone(result["profile"]["website"])
        self.assertEqual(result["profile"]["companyOwners"][0]["name"], "Corporate Owner")
        self.assertIn({"name": "Person Owner", "roles": ["LEGAL_OWNER"]}, result["contactSuggestions"])
        for private in ["PRIVATE", "password", "INJECTED"]:
            self.assertNotIn(private, response.text)

    def test_invalid_optional_status_does_not_invalidate_either_provider_response(self):
        fixture = hosted_tests.HostedFallbackTests()
        with fixture.upstream(lambda _: httpx.Response(200, json={**PUBLIC_COMPANY, "status": []})):
            response = TestClient(app).get("/api/v1/12345674/signing-profile")
        self.assertEqual(response.status_code, 200)
        self.assertIsNone(response.json()["profile"]["companyStatus"])
        source = source_company()
        source["virksomhedMetadata"]["sammensatStatus"] = []
        result, _ = self.lookup(source)
        self.assertIsNone(result["profile"]["companyStatus"])
        self.assertIsNone(SigningCompany.model_validate(result).status)

    def test_old_signing_response_remains_valid_without_profile(self):
        signing = extract_signing(company(), observed_at=OBSERVED)
        result = SigningCompany.model_validate({"vat": 12345674, "signing": signing})
        self.assertIsNone(result.profile)


if __name__ == "__main__":
    unittest.main()
