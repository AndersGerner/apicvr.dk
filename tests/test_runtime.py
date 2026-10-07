import asyncio
import json
import unittest
from unittest.mock import patch

import httpx

from fastapi.testclient import TestClient

from apis import searchcvr
from main import app
from test_signing_evidence import company


class RuntimeTests(unittest.TestCase):
    def setUp(self):
        token = patch.object(searchcvr, "_API_TOKEN", "synthetic")
        token.start()
        self.addCleanup(token.stop)
        self.client = TestClient(app)

    def test_readiness_reports_selected_provider_without_claiming_live_access(self):
        with patch.object(searchcvr, "_API_TOKEN", ""), patch.object(searchcvr.httpx, "AsyncClient") as post:
            response = self.client.get("/readyz")
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {"status": "configured", "provider": "apicvr.dk",
                                              "signingEvidenceSupported": False, "upstreamVerified": False})
            self.assertEqual(self.client.get("/healthz").status_code, 200)
            post.assert_not_called()
        self.assertEqual(self.client.get("/readyz").json(), {
            "status": "configured", "provider": "distribution.virk.dk",
            "signingEvidenceSupported": True, "upstreamVerified": False})

    def test_http_profile_preserves_signing_contract_and_omits_private_details(self):
        source = company()
        source["cvrNummer"] = "12345674"
        source["virksomhedMetadata"] = {"nyesteNavn": {"navn": "Synthetic Company"}}
        raw = {"hits": {"hits": [{"_source": {"Vrvirksomhed": source}}]}}
        with patch.object(searchcvr, "_post_search", return_value=raw), patch.object(searchcvr, "fetch_p_units") as units:
            response = self.client.get("/api/v1/12345674/signing-profile")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["signing"]["status"], "available")
        self.assertEqual(response.json()["signing"]["source"]["register"], "CVR")
        self.assertNotIn("Private address", response.text)
        self.assertNotIn("direktion", response.json())
        self.assertNotIn("ejere", response.json())
        self.assertEqual(response.headers["cache-control"], "no-store")
        units.assert_not_called()

    def test_invalid_cvr_and_malformed_envelope_do_not_become_not_found(self):
        for number in ["123", "01234567", "123456789"]:
            self.assertEqual(self.client.get(f"/api/v1/{number}/signing-profile").status_code, 400)
        for envelope in [{}, {"hits": None}, {"hits": {"hits": [{}]}}]:
            with patch.object(searchcvr, "_post_search", return_value=envelope):
                self.assertEqual(self.client.get("/api/v1/12345674/signing-profile").status_code, 502)

    def test_template_and_openapi_compatibility_after_runtime_upgrade(self):
        for path in ["/", "/en/", "/da/search/", "/da/kapitalsog/", "/openapi.json"]:
            with self.subTest(path=path):
                self.assertEqual(self.client.get(path).status_code, 200)


    def request(self, status=200, content=b'{"hits":{"hits":[]}}'):
        return httpx.Response(status, content=content)

    def test_transport_bounds_verified_tls_redirects_and_sanitized_failures(self):
        for status, content, expected in [
            (401, b"SECRET", "HTTP_ERROR"),
            (302, b"SECRET", "HTTP_ERROR"),
            (200, b"not json SECRET", "INVALID_RESPONSE"),
            (200, b"x" * 8_000_001, "INVALID_RESPONSE"),
            (200, b"[]", "INVALID_RESPONSE"),
        ]:
            transport = httpx.MockTransport(lambda request: self.request(status, content))
            constructor = httpx.AsyncClient
            with patch.object(searchcvr, "_API_TOKEN", "synthetic"), patch.object(searchcvr.httpx, "AsyncClient", side_effect=lambda **kwargs: constructor(transport=transport, **kwargs)) as client:
                result = searchcvr._post_search({})
            self.assertEqual(result["error"], expected)
            self.assertIsNone(result["message"])
            self.assertFalse(client.call_args.kwargs["follow_redirects"])
            self.assertNotIn("verify", client.call_args.kwargs)

    def test_slow_trickle_is_cancelled_at_total_deadline(self):
        class Trickle(httpx.AsyncByteStream):
            closed = False

            async def __aiter__(self):
                for byte in b'{"hits":{"hits":[]}}':
                    await asyncio.sleep(0.03)
                    yield bytes([byte])

            async def aclose(self):
                self.closed = True

        stream = Trickle()
        transport = httpx.MockTransport(lambda request: httpx.Response(200, stream=stream))
        constructor = httpx.AsyncClient
        with patch.object(searchcvr, "_API_TOKEN", "synthetic"), patch.object(searchcvr, "_TOTAL_SECONDS", 0.08), patch.object(searchcvr.httpx, "AsyncClient", side_effect=lambda **kwargs: constructor(transport=transport, **kwargs)):
            import time
            start = time.monotonic()
            result = searchcvr._post_search({})
            elapsed = time.monotonic() - start
        self.assertEqual(result["error"], "TRANSPORT_ERROR")
        self.assertLess(elapsed, 0.2)
        self.assertTrue(stream.closed)

    def test_transport_exception_does_not_leak_details(self):
        def fail(request):
            raise httpx.ConnectError("SECRET")
        constructor = httpx.AsyncClient
        with patch.object(searchcvr, "_API_TOKEN", "synthetic"), patch.object(searchcvr.httpx, "AsyncClient", side_effect=lambda **kwargs: constructor(transport=httpx.MockTransport(fail), **kwargs)):
            self.assertNotIn("SECRET", json.dumps(searchcvr._post_search({})))


if __name__ == "__main__":
    unittest.main()
