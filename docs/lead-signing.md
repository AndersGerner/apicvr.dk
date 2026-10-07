# Lead staging CVR service

This fork belongs to AndersGerner. The original MIT source remains credited in
LICENSE; operational changes and deployments use this repository only.

`GET /api/v1/{cvr}/signing-profile` returns a bounded company profile and typed
`signing` evidence. It makes one exact official lookup, rejects duplicate or
mismatched register identities and omits personal addresses and ownership data.
The original full-profile and search endpoints remain available.

Evidence preserves the registered `TEGNINGSREGEL`, current leadership, fully
liable participants and explicitly registered representatives. Dates use the
Danish register calendar, including inclusive end dates. Conflicting rules,
malformed memberships and missing rules have explicit states. A participant is
a candidate; the complete rule determines whether a group is required. The API
does not decide that a person may sign alone.

## Access and deployment

Consumers need no API credential. The service needs issued official CVR
System-to-System access. Set `API_TOKEN` to the base64 Basic-auth payload in
Render's secret store. Never commit or paste that value into issue/PR material.
Official guidance: https://datacvr.virk.dk/artikel/system-til-system-adgang-til-cvr-data

The Docker runtime uses Python 3.12, verified TLS, no redirects, bounded responses
and sanitized failures. HTTPX requests have connect/read timeouts and an eight-second
total transport deadline. No request bodies, access logs, client IPs or referrers
are recorded. Optional aggregate SQLite statistics are disabled in staging.
The data catalogue mapping is documented in `apis/signing.py`.

The free Render web service is defined in `render.yaml`; automatic deploys are
off. Deploy a reviewed master commit explicitly. `/healthz` proves process
health. `/readyz` checks credential presence only and says `upstreamVerified:
false`; a permitted live company lookup is required to prove official access.
Without credentials, registry routes return a sanitized 503 and make no
upstream request. Never substitute fixtures for registry data in a deployed API.

Set Lead platform API `CVR_API_BASE_URL` to this service's HTTPS origin.
Free services may sleep, so the first lookup after inactivity may report
unavailable and require a retry. Do not fall back to the public upstream service.

## Validation

Install `requirements-dev.txt`, then run
`python -m unittest discover -s tests -v`. Fixtures are synthetic and prove
mapping/transport behavior. Before accepting staging, verify a permitted live
CVR identity, rule and participants against official data, then exercise Lead's
protected lookup and a contract assessment with the required complete group.
Health and deploy receipts alone do not satisfy this acceptance.
