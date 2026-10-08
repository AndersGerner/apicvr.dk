import copy
import unittest

from apis.contact_suggestions import official_contact_suggestions
from apis.signing import extract_signing
from test_signing_evidence import NOW, company, relation, value


class ContactSuggestionTests(unittest.TestCase):
    def suggestions(self, source):
        return official_contact_suggestions(source, extract_signing(source, observed_at=NOW))

    def test_current_leadership_is_suggested_without_signing_approval_or_private_data(self):
        source = company()
        source["deltagerRelation"] += [relation(role="BESTYRELSESFORMAND"), relation(end="2026-10-05"), relation(start="2026-10-07")]
        output = self.suggestions(source)
        self.assertEqual(output, [{"name": "Test Person", "roles": ["DIREKTØR", "BESTYRELSESFORMAND"]}])
        self.assertNotIn("Private", str(output))
        self.assertNotIn("4000000001", str(output))
        self.assertNotIn("representative", str(output))

    def test_legal_person_owner_is_included_only_for_current_positive_ownership(self):
        owner = relation()
        owner["organisationer"] = [{"hovedtype": "REGISTER", "organisationsNavn": [{"navn": "EJERREGISTER"}],
                                    "medlemsData": [{"attributter": [{"type": "EJERANDEL_PROCENT", "vaerdier": [value("0.5")]}]}]}]
        for start, end, fraction, included in [("2020-01-01", None, "0.5", True), ("2026-10-07", None, "0.5", False),
                                               ("2020-01-01", "2026-10-05", "0.5", False), ("2020-01-01", None, "0", False), ("2020-01-01", None, True, False)]:
            source = company()
            current = copy.deepcopy(owner)
            current["organisationer"][0]["medlemsData"][0]["attributter"][0]["vaerdier"] = [value(fraction, start, end)]
            source["deltagerRelation"] = [current]
            self.assertEqual(bool(self.suggestions(source)), included)
        owner["deltager"]["enhedstype"] = "VIRKSOMHED"
        source["deltagerRelation"] = [owner]
        self.assertEqual(self.suggestions(source), [])

    def test_missing_or_conflicting_person_name_and_malformed_relation_are_not_suggested(self):
        source = company()
        source["deltagerRelation"][0]["deltager"]["navne"] += [{"navn": "Conflicting", "periode": {"gyldigFra": None, "gyldigTil": None}}]
        self.assertEqual(self.suggestions(source), [])
        source["deltagerRelation"] = "malformed"
        self.assertEqual(self.suggestions(source), [])
