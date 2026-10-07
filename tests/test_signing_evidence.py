import copy
import unittest
from datetime import datetime, timezone

from apis.signing import extract_signing

NOW = datetime(2026, 10, 6, 12, tzinfo=timezone.utc)


def value(text, start="2020-01-01", end=None):
    return {"vaerdi": text, "periode": {"gyldigFra": start, "gyldigTil": end}}


def relation(kind="LEDELSESORGAN", role="DIREKTØR", **dates):
    return {"deltager": {"enhedsNummer": 4000000001, "enhedstype": "PERSON",
            "navne": [{"navn": "Test Person", "periode": {"gyldigFra": "2020-01-01", "gyldigTil": None}}],
            "beliggenhedsadresse": [{"vejnavn": "Private address"}], "cpr": "never expose"},
            "organisationer": [{"hovedtype": kind, "medlemsData": [{"attributter": [
                {"type": "FUNKTION", "vaerdier": [value(role, **dates)]}]}]}]}


def company():
    # Synthetic fixture uses the official CVR mapping; it is not a live capture.
    return {"attributter": [{"type": "TEGNINGSREGEL", "vaerdier": [value("Selskabet tegnes af to direktører i forening.")]}],
            "deltagerRelation": [relation()]}


class SigningTests(unittest.TestCase):
    def extract(self, source):
        return extract_signing(source, observed_at=NOW)

    def test_verbatim_rule_and_candidate_not_independent_authority(self):
        result = self.extract(company())
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["rules"][0]["text"], "Selskabet tegnes af to direktører i forening.")
        self.assertFalse(result["participants"][0]["registeredRepresentative"])
        self.assertNotIn("Private address", str(result))
        self.assertNotIn("never expose", str(result))

    def test_registered_representative_and_board_roles_deduplicated_by_identity(self):
        source = company()
        source["deltagerRelation"] += [relation("TEGNINGSBERETTIGEDE", "TEGNINGSBERETTIGET"), relation(role="BESTYRELSESFORMAND"), relation()]
        result = self.extract(source)
        self.assertEqual(len(result["participants"]), 1)
        self.assertEqual(len(result["participants"][0]["roles"]), 3)
        self.assertTrue(result["participants"][0]["registeredRepresentative"])

    def test_expired_and_future_roles_excluded_including_future_names(self):
        source = company()
        source["deltagerRelation"] = [relation(end="2026-10-05"), relation(start="2026-10-07")]
        self.assertEqual(self.extract(source)["participants"], [])
        source = company()
        source["deltagerRelation"][0]["deltager"]["navne"].append({"navn": "Future name", "periode": {"gyldigFra": "2026-10-07", "gyldigTil": None}})
        self.assertEqual(self.extract(source)["participants"][0]["name"], "Test Person")

    def test_period_end_inclusive_and_danish_calendar(self):
        source = company()
        source["deltagerRelation"] = [relation(start="2026-10-07")]
        result = extract_signing(source, observed_at=datetime(2026, 10, 6, 23, tzinfo=timezone.utc))
        self.assertEqual(len(result["participants"]), 1)
        source["deltagerRelation"] = [relation(end="2026-10-06")]
        self.assertEqual(len(self.extract(source)["participants"]), 1)

    def test_optional_time_validity_forms_normalize_to_danish_dates(self):
        source = company()
        rule = source["attributter"][0]["vaerdier"][0]
        rule["periode"]["gyldigFra"] = "2020-01-01T00:00:00"
        rule["periode"]["gyldigTil"] = "2026-10-06T00:00:00Z"
        result = self.extract(source)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["rules"][0]["validFrom"], "2020-01-01")
        self.assertEqual(result["rules"][0]["validTo"], "2026-10-06")
        source["deltagerRelation"] = [relation(start="2026-10-06T23:00:00Z")]
        self.assertEqual(self.extract(source)["participants"], [])

    def test_optional_time_update_forms_preserve_precision(self):
        import json
        from apis.models import Company
        source = company()
        source["sidstOpdateret"] = "2026-10-05"
        source["attributter"][0]["vaerdier"][0]["sidstOpdateret"] = "2026-10-05T12:30:00"
        result = self.extract(source)
        self.assertEqual(result["status"], "available")
        self.assertEqual(result["source"]["companyUpdatedAt"], "2026-10-05")
        self.assertEqual(result["rules"][0]["updatedAt"], "2026-10-05T12:30:00")
        serialized = json.loads(Company.model_validate({"vat": 12345674, "signing": result}).model_dump_json(by_alias=True))
        self.assertEqual(serialized["signing"]["source"]["companyUpdatedAt"], "2026-10-05")
        self.assertEqual(serialized["signing"]["rules"][0]["updatedAt"], "2026-10-05T12:30:00")

    def test_missing_conflicting_and_malformed_rules(self):
        source = company()
        source["attributter"] = []
        self.assertEqual(self.extract(source)["status"], "missing_rule")
        source["attributter"] = [{"type": "TEGNINGSREGEL", "vaerdier": [value("Rule one"), value("Rule two")]}]
        self.assertEqual(self.extract(source)["status"], "conflicting_rules")
        source["attributter"][0]["vaerdier"][0]["periode"]["gyldigFra"] = "bad"
        self.assertEqual(self.extract(source)["status"], "incomplete")

    def test_malformed_participants_never_produce_complete_evidence(self):
        for mutation in [lambda p: p.pop("enhedsNummer"), lambda p: p.update(enhedstype="UNKNOWN"), lambda p: p.update(navne=[]), lambda p: p.update(navne="bad")]:
            source = company()
            mutation(source["deltagerRelation"][0]["deltager"])
            self.assertEqual(self.extract(source)["status"], "incomplete")
        source = company()
        source["deltagerRelation"].append(copy.deepcopy(source["deltagerRelation"][0]))
        source["deltagerRelation"][1]["deltager"]["navne"][0]["navn"] = "Conflicting name"
        self.assertEqual(self.extract(source)["status"], "incomplete")

    def test_empty_or_missing_function_per_membership_is_incomplete(self):
        source = company()
        member = source["deltagerRelation"][0]["organisationer"][0]["medlemsData"][0]
        member["attributter"][0]["vaerdier"] = []
        self.assertEqual(self.extract(source)["status"], "incomplete")
        source = company()
        source["deltagerRelation"][0]["organisationer"][0]["medlemsData"].append({"attributter": []})
        self.assertEqual(self.extract(source)["status"], "incomplete")

    def test_empty_memberships_are_incomplete(self):
        source = company()
        source["deltagerRelation"][0]["organisationer"][0]["medlemsData"] = []
        self.assertEqual(self.extract(source)["status"], "incomplete")

    def test_missing_membership_function_is_incomplete(self):
        source = company()
        source["deltagerRelation"][0]["organisationer"][0]["medlemsData"] = [{"attributter": []}]
        self.assertEqual(self.extract(source)["status"], "incomplete")


class UpstreamWiringTests(unittest.TestCase):
    def setUp(self):
        from unittest.mock import patch
        token = patch("apis.searchcvr._API_TOKEN", "synthetic")
        token.start()
        self.addCleanup(token.stop)

    def test_exact_lookup_exposes_typed_signing_without_changing_existing_fields(self):
        from unittest.mock import patch
        from fastapi.encoders import jsonable_encoder
        from apis.searchcvr import search_cvr_api
        from apis.models import Company
        source = company()
        source["cvrNummer"] = 12345674
        source["virksomhedMetadata"] = {"nyesteNavn": {"navn": "Synthetic Company"}}
        response = {"hits": {"hits": [{"_source": {"Vrvirksomhed": source}}]}}
        with patch("apis.searchcvr._post_search", return_value=response) as lookup, patch("apis.searchcvr.fetch_p_units", return_value=[]):
            result = search_cvr_api(12345674)
        self.assertEqual(lookup.call_args.kwargs["size"], 2)
        serialized = jsonable_encoder(Company.model_validate(result), by_alias=True)
        self.assertEqual(serialized["vat"], 12345674)
        self.assertEqual(serialized["name"], "Synthetic Company")
        self.assertEqual(serialized["signing"]["schemaVersion"], 1)
        self.assertEqual(serialized["signing"]["source"]["register"], "CVR")
        self.assertNotIn("register_name", serialized["signing"]["source"])
        self.assertEqual(serialized["signing"]["rules"][0]["text"], source["attributter"][0]["vaerdier"][0]["vaerdi"])

    def test_identity_mismatch_and_duplicate_hits_reject_signing_evidence(self):
        from unittest.mock import patch
        from apis.searchcvr import search_cvr_api
        hit = {"_source": {"Vrvirksomhed": {"cvrNummer": 87654321}}}
        for hits in [[hit], [hit, hit]]:
            with patch("apis.searchcvr._post_search", return_value={"hits": {"hits": hits}}):
                self.assertEqual(search_cvr_api(12345674)["error"], "INVALID_RESPONSE")

    def test_documented_string_cvr_is_accepted_without_coercing_malformed_identity(self):
        from unittest.mock import patch
        from apis.searchcvr import search_cvr_api
        source = company()
        source["cvrNummer"] = "12345674"
        response = {"hits": {"hits": [{"_source": {"Vrvirksomhed": source}}]}}
        with patch("apis.searchcvr._post_search", return_value=response), patch("apis.searchcvr.fetch_p_units", return_value=[]):
            result = search_cvr_api(12345674)
        self.assertEqual(result["vat"], 12345674)
        self.assertEqual(result["signing"]["status"], "available")

    def test_malformed_source_cvr_is_rejected(self):
        from unittest.mock import patch
        from apis.searchcvr import search_cvr_api
        for value in [None, True, 12345674.0, "12345674 ", "012345674", "１２３４５６７４"]:
            with self.subTest(value=value), patch("apis.searchcvr._post_search", return_value={"hits": {"hits": [{"_source": {"Vrvirksomhed": {"cvrNummer": value}}}]}}):
                self.assertEqual(search_cvr_api(12345674)["error"], "INVALID_RESPONSE")


if __name__ == "__main__":
    unittest.main()
