# Lead staging CVR service

This fork belongs to AndersGerner. The original MIT source remains credited in
LICENSE; operational changes and deployments use this repository only.

`GET /api/v1/{cvr}/signing-profile` returns a bounded company profile and typed
`signing` evidence. It makes one exact lookup using the selected provider,
rejects mismatched identities and omits personal addresses and ownership data.
Official mode also rejects duplicate register hits.
The original full-profile and search endpoints remain available.

Evidence preserves the registered `TEGNINGSREGEL`, current leadership, fully
liable participants and explicitly registered representatives. Dates use the
Danish register calendar, including inclusive end dates. Conflicting rules,
malformed memberships and missing rules have explicit states. A participant is
a candidate; the complete rule determines whether a group is required. The API
does not decide that a person may sign alone.

## Access and deployment

Consumers need no API credential. Provider selection is automatic:

- With `API_TOKEN` unset or blank, company, relations and search endpoints call
  the documented hosted `https://apicvr.dk` API without authentication.
- With `API_TOKEN` set, the service queries the official CVR distribution API
  directly. Invalid credentials or upstream failures do not trigger fallback.

Official mode needs issued CVR System-to-System access. Set `API_TOKEN` to
Base64 of the issued `username:password`, without the `Basic ` prefix, in
Render's secret store, then redeploy. It must not be a random token. Never
commit or paste that value into issue/PR material.
Official guidance: https://datacvr.virk.dk/artikel/system-til-system-adgang-til-cvr-data

The hosted API provides company metadata and leadership/ownership roles, but its
documented contract provides no registered signing rule or stable signing-participant
IDs. In fallback mode `/signing-profile` returns company enrichment with signing
status `incomplete`, empty rules and participants, and no company-update timestamp.
It never turns director/owner roles into signing authority or invents register IDs.
Lead already supports this state and retains its manual assessment guards.
Hosted documentation: https://apicvr.dk/docs. Rate limits apply; sustained heavy
traffic requires a separate provider-capacity decision.

The Docker runtime uses Python 3.12, verified TLS, no redirects, bounded responses
and sanitized failures. HTTPX requests have connect/read timeouts and an eight-second
total transport deadline. No request bodies, access logs, client IPs or referrers
are recorded. Optional aggregate SQLite statistics are disabled in staging.
The data catalogue mapping is documented in `apis/signing.py`.

The free Render web service is defined in `render.yaml`; automatic deploys are
off. Deploy a reviewed master commit explicitly. `/healthz` proves process
health. `/readyz` reports the selected provider and whether that mode supports
registered signing evidence, always with `upstreamVerified: false`. It does not
perform a live request. A permitted company lookup must prove the selected provider
works; process health alone does not prove upstream access. Never substitute
fixtures for registry data in a deployed API.

Set Lead platform API `CVR_API_BASE_URL` to this service's HTTPS origin.
Free services may sleep, so the first lookup after inactivity may report
unavailable and require a retry. Lead always calls our own service; it never
needs the official credential or selects the upstream provider itself.

## Validation

Install `requirements-dev.txt`, then run
`python -m unittest discover -s tests -v`. Fixtures are synthetic and prove
mapping/transport behavior. Before accepting staging, verify a permitted live
CVR identity, rule and participants against official data, then exercise Lead's
protected lookup and a contract assessment with the required complete group.
Health and deploy receipts alone do not satisfy this acceptance.
