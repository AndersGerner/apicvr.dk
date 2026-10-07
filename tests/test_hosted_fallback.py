import asyncio
import json
import unittest
from contextlib import contextmanager
from unittest.mock import patch

import httpx
from fastapi.testclient import TestClient

from apis import searchcvr
from main import app
from test_signing_evidence import company


PUBLIC_COMPANY = {
    "vat": 12345674, "name": "Synthetic Company", "address": "Business Street 1",
    "zipcode": 2800, "city": "Synthetic City", "companydesc": "ApS", "protected": True,
    "direktion": [{"name": "Candidate", "role": "DIREKTØR", "type": "PERSON",
                    "address": "Private address", "undocumented": "SECRET"}],
    "fuldt_ansvarlige": [], "ejere": [], "undocumented": "SECRET",
}


class HostedFallbackTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    @contextmanager
    def upstream(self, responder, *, token=""):
        requests = []

        def handler(request):
            requests.append(request)
            return responder(request)

        constructor = httpx.AsyncClient
        with patch.object(searchcvr, "_API_TOKEN", token), patch.object(
            searchcvr.httpx, "AsyncClient",
            side_effect=lambda **kwargs: constructor(transport=httpx.MockTransport(handler), **kwargs),
        ) as client:
            yield requests, client

    def test_absent_or_blank_token_uses_hosted_company_without_auth_or_signing_inference(self):
        for token in ["", " \t\n"]:
            with self.subTest(token=token), self.upstream(lambda _: httpx.Response(200, json=PUBLIC_COMPANY), token=token) as (requests, client):
                response = self.client.get("/api/v1/12345674/signing-profile")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.headers["cache-control"], "no-store")
            profile = response.json()
            self.assertEqual(profile["vat"], 12345674)
            self.assertEqual(profile["name"], "Synthetic Company")
            self.assertTrue(profile["protected"])
            self.assertEqual(profile["signing"]["status"], "incomplete")
            self.assertEqual(profile["signing"]["rules"], [])
            self.assertEqual(profile["signing"]["participants"], [])
            self.assertIsNone(profile["signing"]["source"]["companyUpdatedAt"])
            self.assertNotIn("Private address", response.text)
            self.assertNotIn("SECRET", response.text)
            self.assertNotIn("direktion", profile)
            self.assertEqual(len(requests), 1)
            self.assertEqual(str(requests[0].url), "https://apicvr.dk/api/v1/12345674")
            self.assertEqual(requests[0].method, "GET")
            self.assertNotIn("authorization", requests[0].headers)
            self.assertFalse(client.call_args.kwargs["follow_redirects"])
            self.assertNotIn("verify", client.call_args.kwargs)

    def test_configured_token_uses_official_lookup_and_preserves_registered_evidence(self):
        source = company()
        source["cvrNummer"] = 12345674
        raw = {"hits": {"hits": [{"_source": {"Vrvirksomhed": source}}]}}
        with self.upstream(lambda _: httpx.Response(200, json=raw), token="synthetic-issued-payload") as (requests, _):
            response = self.client.get("/api/v1/12345674/signing-profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["signing"]["status"], "available")
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "distribution.virk.dk")
        self.assertEqual(requests[0].method, "POST")
        self.assertEqual(requests[0].headers["authorization"], "Basic synthetic-issued-payload")

    def test_configured_but_failed_official_access_never_falls_back(self):
        with self.upstream(lambda _: httpx.Response(401, content=b"SECRET"), token="invalid-synthetic") as (requests, _):
            response = self.client.get("/api/v1/12345674/signing-profile")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0].url.host, "distribution.virk.dk")
        self.assertNotIn("SECRET", response.text)

    def test_hosted_signing_fields_are_not_trusted(self):
        raw = {**PUBLIC_COMPANY, "signing": {"status": "available", "rules": ["SECRET"]}}
        with self.upstream(lambda _: httpx.Response(200, json=raw)):
            profile = self.client.get("/api/v1/12345674/signing-profile").json()
        self.assertEqual(profile["signing"]["status"], "incomplete")
        self.assertEqual(profile["signing"]["participants"], [])
        self.assertNotIn("SECRET", json.dumps(profile))

    def test_full_profile_relations_and_documented_searches_use_hosted_contract(self):
        paths = [
            ("/api/v1/12345674", PUBLIC_COMPANY),
            ("/api/v1/12345674/direktion-og-ansvarlig", PUBLIC_COMPANY),
            ("/api/v1/search?name=Synthetic&cvr=12345674&limit=5", [PUBLIC_COMPANY]),
            ("/api/v1/search/address?address=Business&postal_code=2800&limit=5", [PUBLIC_COMPANY]),
            ("/api/v1/search/company/Synthetic?limit=5", [PUBLIC_COMPANY]),
            ("/api/v1/search/fuzzy/Synthetic?limit=5", [{"name": "Synthetic Company", "cvr_number": 12345674}]),
            ("/api/v1/search/email/info%40example.test?limit=5", [PUBLIC_COMPANY]),
            ("/api/v1/search/email-domain/example.test?limit=5", [PUBLIC_COMPANY]),
            ("/api/v1/search/phone/12345678?limit=5", [PUBLIC_COMPANY]),
        ]
        for path, raw in paths:
            with self.subTest(path=path), self.upstream(lambda _: httpx.Response(200, json=raw)) as (requests, _):
                response = self.client.get(path)
            self.assertEqual(response.status_code, 200)
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].url.host, "apicvr.dk")
            self.assertNotIn("authorization", requests[0].headers)
            self.assertNotIn("SECRET", response.text)
            self.assertEqual(requests[0].url.path, httpx.URL("https://apicvr.dk" + path).path)
            self.assertEqual(dict(requests[0].url.params), dict(httpx.URL("https://apicvr.dk" + path).params))

    def test_name_path_segments_cannot_escape_the_hosted_endpoint(self):
        with self.upstream(lambda _: httpx.Response(200, json=[])) as (requests, _):
            searchcvr.search_cvr_by_name("../name?secret#fragment", limit=5)
        self.assertEqual(requests[0].url.host, "apicvr.dk")
        self.assertIn(b"%2E%2E%2Fname%3Fsecret%23fragment", requests[0].url.raw_path)
        self.assertEqual(dict(requests[0].url.params), {"limit": "5"})

    def test_hosted_not_found_failures_identity_and_shape_are_sanitized(self):
        cases = [
            (404, b"SECRET", 404, "NOT_FOUND"),
            (429, b"SECRET", 502, "HTTP_ERROR"),
            (302, b"SECRET", 502, "HTTP_ERROR"),
            (200, b"not json SECRET", 502, "INVALID_RESPONSE"),
            (200, b"x" * 8_000_001, 502, "INVALID_RESPONSE"),
            (200, b"[]", 502, "INVALID_RESPONSE"),
            (200, b'{"vat":12345675}', 502, "INVALID_RESPONSE"),
            (200, b'{"vat":true}', 502, "INVALID_RESPONSE"),
            (200, b'{"vat":12345674,"name":[]}', 502, "INVALID_RESPONSE"),
            (200, b'{"error":"NOT_FOUND","status":404,"message":"SECRET"}', 502, "INVALID_RESPONSE"),
        ]
        for status, content, expected, code in cases:
            with self.subTest(status=status, code=code), self.upstream(lambda _: httpx.Response(status, content=content)):
                response = self.client.get("/api/v1/12345674/signing-profile")
            self.assertEqual(response.status_code, expected)
            self.assertEqual(response.json()["detail"]["error"], code)
            self.assertNotIn("SECRET", response.text)

    def test_invalid_cvr_never_reaches_either_provider(self):
        with patch.object(searchcvr, "_API_TOKEN", ""), patch.object(searchcvr.httpx, "AsyncClient") as client:
            for number in ["123", "01234567", "123456789"]:
                for suffix in ["signing-profile", "direktion-og-ansvarlig"]:
                    self.assertEqual(self.client.get(f"/api/v1/{number}/{suffix}").status_code, 400)
            client.assert_not_called()

    def test_hosted_transport_errors_and_trickled_body_have_bounded_failures(self):
        def fail(_):
            raise httpx.ConnectError("SECRET")

        with self.upstream(fail):
            response = self.client.get("/api/v1/12345674/signing-profile")
        self.assertEqual(response.status_code, 502)
        self.assertNotIn("SECRET", response.text)

        class Trickle(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                for byte in b'{"vat":12345674}':
                    await asyncio.sleep(0.03)
                    yield bytes([byte])

            async def aclose(self):
                self.closed = True

        stream = Trickle()
        with patch.object(searchcvr, "_TOTAL_SECONDS", 0.08), self.upstream(lambda _: httpx.Response(200, stream=stream)):
            response = self.client.get("/api/v1/12345674/signing-profile")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(response.json()["detail"]["error"], "TRANSPORT_ERROR")
        self.assertTrue(stream.closed)


if __name__ == "__main__":
    unittest.main()
