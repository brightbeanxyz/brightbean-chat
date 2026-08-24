# Security baseline — traceability audit

Every bullet in [`SECURITY-BASELINE.md`](SECURITY-BASELINE.md), and every bullet
in [SPEC §19](SPEC.md#19-security), mapped to the test that enforces it. Produced
by issue #29, whose job is to verify the baseline the earlier layers built in
rather than to introduce security at the end.

**This document is enforced.** `tests/test_security_audit.py` parses the tables
below, asserts their ids are exactly the baseline's bullets, and resolves every
test id named here to a real class or function. Adding a bullet to the baseline
without mapping it turns the suite red; so does renaming a test this file cites.
What the test cannot prove is that a mapped test *passes* — that is what the
`test` CI job is for. The pair is the claim.

## How to read the Status column

Four values, and no fifth. There is deliberately no "trusted" or "reviewed":

| Status | Meaning |
|---|---|
| `COVERED` | Every clause of the bullet has a linked test. |
| `PARTIAL` | Some clause is not covered. The cell says which, and carries a filed issue. |
| `DEVIATION` | Implemented differently on purpose. The cell links the argument, and the baseline names it too. |
| `NOT YET BUILT` | The subject does not exist in this tree. The cell carries the issue that builds it. |

A row cannot be `PARTIAL`, `DEVIATION` or `NOT YET BUILT` without saying why in
the same cell, which is the mechanism behind the issue's "zero baseline items
without a linked passing test": an unmapped item cannot be given a legal status.

---

## §1 Tenancy isolation

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §1.1 | Every queryset on tenant data goes through the workspace-scoped base manager | `apps/common/tests/test_scoping.py::TestUnscopedAccessRaises`, `apps/common/tests/test_scoping.py::TestTheInvariantIsChecked` | COVERED — the second class sweeps every first-party model and fails on one that is neither scoped nor recorded as non-tenant data. It did **not** before this issue: it built the list of scoped models and then asserted only that the test model was in it, which cannot fail for anything real. Fixed here. |
| §1.2 | Cross-workspace access returns 404, never 403 | `tests/test_idor.py::TestCrossTenantIsolation`, `apps/api/tests/test_isolation.py::TestApiV1CrossWorkspaceIsolation` | COVERED |
| §1.3 | Any PR adding views or API endpoints extends the IDOR fuzz suite | `tests/test_idor.py::TestTheSuiteActuallyCatchesLeaks`, `apps/api/tests/test_isolation.py::TestApiV1CrossWorkspaceIsolation` | COVERED — not opt-in: an unregistered route kwarg raises, an unnamed tenant route raises, and the API sweep asserts its waived set equals its swept set in both directions. |

## §2 Untrusted inbound content

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §2.1 | Platform content is escaped on render; never `mark_safe`, never raw HTML | `apps/inbox/tests/test_hostile_content.py::TestTheAppCannotBypassEscaping`, `apps/inbox/tests/test_hostile_content.py::TestHostileText`, `apps/contacts/tests/test_hostile_content.py::TestEscaping` | COVERED — the first is a source scan for `mark_safe` and `autoescape off`, so it covers templates nobody thought to test. |
| §2.2 | Webhook parsing is defensive; fixtures include malformed and hostile payloads | `apps/messaging/tests/test_hostile_payloads.py::TestWrongTypes`, `apps/channels/tests/test_webhooks.py::TestHostilePayloads`, `apps/channels/tests/test_messenger_hostile.py::TestShapesThatAreNotShapes` | COVERED |

## §3 Templating (SSTI ban)

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §3.1 | `{{placeholder}}` is plain token substitution through one renderer; never a template engine | `apps/flows/tests/test_rendering.py::TestSstiBan`, `apps/flows/tests/test_rendering.py::TestFuzz` | COVERED |
| §3.2 | HTML contexts escape substituted values; External Request responses are untrusted like contact input | `apps/flows/tests/test_rendering.py::TestUrlMode`, `apps/channels/tests/test_send_email_node.py` | COVERED |

## §4 Public token routes

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §4.1 | Unauthenticated token routes use the one shared signing utility | `apps/common/tests/test_signing.py`, `apps/channels/tests/test_unsubscribe.py`, `apps/media_library/tests/test_delivery.py` | DEVIATION — two, both argued in code and now named in the baseline: `/internal/tick`'s bare env token and Telegram's 64-character preview handles. `/c/` and `/o/` do not exist yet (issue #26). |
| §4.2 | Verification is constant-time; every failure is a generic 404 | `tests/test_token_routes.py::TestEveryTokenRouteAnswersABare404`, `tests/test_token_routes.py::TestNoCredentialIsComparedWithEquality`, `apps/common/tests/test_signing.py` | COVERED |
| §4.3 | The two divergences keep constant-time compare and a bare 404 | `apps/queueing/tests/test_views.py`, `apps/channels/tests/test_telegram_preview.py` | COVERED |
| §4.4 | A token route's prefix is scrubbed from request lines | `tests/test_token_routes.py::TestEveryTokenRouteIsScrubbedFromLogs` | COVERED — new in #29, and it found `/m/` missing. Media delivery tokens are bearer capabilities and every asset request had been logging one in full. |

## §5 Secrets

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §5.1 | Credentials live only in encrypted fields; never plain columns, fixtures or logs | `apps/common/tests/test_encryption.py`, `tests/test_gitleaks_config.py` | COVERED |
| §5.2 | The scrubbing filter is installed everywhere; tokens never reach logs, error reports, admin displays or API responses | `apps/common/tests/test_logging.py::TestInstallation`, `apps/common/tests/test_logging.py::TestEncryptedFieldPlaintextNeverReachesLogs`, `apps/common/tests/test_sentry.py::TestScrubEvent`, `apps/channels/tests/test_telegram_scrubbing.py::TestNothingLogsTheToken`, `apps/channels/tests/test_whatsapp_scrubbing.py::TestNothingLogsTheToken`, `apps/api/tests/test_keys.py` | PARTIAL — logs, Sentry and per-adapter shapes are covered thoroughly. "Admin list displays" and "API responses" have no dedicated sweep; the admin registers only `Segment` and `PlatformCredential` (superuser-gated), and no serializer emits an encrypted column, but neither is asserted by a test that would fail if one started to. Filed as [#94](https://github.com/brightbeanxyz/brightbean-chat/issues/94). |

## §6 Outbound HTTP (SSRF)

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §6.1 | Every server-initiated request to an influenceable URL goes through the guard. No exceptions | `tests/test_ssrf_call_sites.py::TestEveryRequestLeavesThroughAKnownDoor`, `apps/flows/tests/test_node_external_request.py`, `apps/api/tests/test_delivery.py`, `apps/channels/tests/test_media.py` | PARTIAL — every httpx request leaves through one of two doors, asserted structurally. Two paths reach the network without passing either, because neither is httpx: Django's SMTP backend (address rules pre-flighted, connection not pinned) and boto3 to SES (own transport). Filed as [#92](https://github.com/brightbeanxyz/brightbean-chat/issues/92) and [#91](https://github.com/brightbeanxyz/brightbean-chat/issues/91). |
| §6.2 | "Proving" means `guard_required()`, not a patched `guarded_request` | `tests/test_ssrf_helper.py::TestGuardRequired`, `tests/test_ssrf_call_sites.py::TestEveryGuardedCallSiteIsProven` | COVERED — new in #29. `apps/channels/providers/email_signatures.py` was the one call site whose tests replaced the symbol; it now has `apps/channels/tests/test_email_signature_fetch.py`. |
| §6.3 | The guard denies private ranges, pins the resolved address, re-validates redirects, caps the body | `apps/common/tests/test_outbound.py` | COVERED |
| §6.4 | `request_json` is the sibling for adapter-built URLs | `apps/channels/tests/test_providers_base.py`, `tests/test_ssrf_call_sites.py::TestEveryRequestLeavesThroughAKnownDoor` | COVERED |
| §6.5 | The two non-HTTP egress paths are named and bounded | `tests/test_ssrf_call_sites.py::TestEveryRequestLeavesThroughAKnownDoor` | PARTIAL — pinned so a third cannot appear quietly, and the SMTP pre-flight is asserted. The gaps themselves are filed ([#91](https://github.com/brightbeanxyz/brightbean-chat/issues/91), [#92](https://github.com/brightbeanxyz/brightbean-chat/issues/92)), not closed. |

## §7 Input limits

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §7.1 | Body-size caps on every webhook and public endpoint, before signature work and before DB writes | `apps/channels/tests/test_security.py::TestBodySizeCap`, `apps/channels/tests/test_webhooks.py::TestBodyLimits`, `apps/api/tests/test_auth.py` | COVERED |
| §7.2 | User-authored JSON gets size and depth caps and schema validation rejecting unknown keys | `apps/flows/tests/test_validation.py`, `apps/contacts/tests/test_conditions_fuzz.py::TestTheSizeCapAppliesToParsedDocumentsToo`, `apps/api/tests/test_limits.py` | COVERED |
| §7.3 | Condition evaluation compiles through the ORM only, with an allowlist. No string-built SQL | `apps/contacts/tests/test_conditions_fuzz.py::TestHostileDocumentsNeverReachTheOrm`, `apps/contacts/tests/test_conditions.py` | COVERED |

## §8 Web platform hardening

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §8.1 | CSP with per-request nonces on every page, builder island included | `apps/common/tests/test_csp.py::TestContentSecurityPolicy`, `apps/inbox/tests/test_hostile_content.py::TestContentSecurityPolicy` | COVERED |
| §8.2 | Session/CSRF cookie flags; CSRF on all session-authenticated endpoints | `apps/accounts/tests/test_auth_hardening.py::TestSessionSettingsAreNotRedeclared` | COVERED |
| §8.3 | Auth endpoints rate-limited; responses enumeration-safe | `apps/accounts/tests/test_auth_hardening.py::TestAuthRateLimiting`, `apps/accounts/tests/test_auth_hardening.py::TestEnumerationSafety`, `apps/common/tests/test_ratelimit.py::TestConcurrentAttemptsAreNotLost` | COVERED |
| §8.4 | Production refuses to boot without secrets; `DEBUG` off; security headers set and verified | `apps/common/tests/test_checks.py::TestProductionSecrets`, `apps/common/tests/test_settings_boot.py` | COVERED — the baseline previously attributed the headers to a Caddy config that does not exist in this repository. They are set by the application in `config/settings/production.py`; the wording is corrected. Duplicating them at the proxy belongs with issue #28. |

## §9 File uploads (media library)

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §9.1 | Content type by sniffing; unsafe types served as attachments with `nosniff` | `apps/media_library/tests/test_mimes.py`, `apps/media_library/tests/test_delivery.py` | COVERED |
| §9.2 | Per-file and per-workspace quotas; delivery URLs signed and unguessable | `apps/media_library/tests/test_quota_concurrency.py`, `apps/media_library/tests/test_delivery.py` | COVERED |

## §10 Supply chain

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §10.1 | Lockfiles pinned; `pip-audit` and `npm audit` in CI; Dependabot; Bandit or ruff security rules | `tests/test_gitleaks_config.py`, CI job `audit` (`.github/workflows/ci.yml`), CI job `lint` | COVERED — `pip-audit --strict` over both requirement files, a lockfile-staleness diff, `npm audit --audit-level=low`, and a **gate self-test** that runs pip-audit against a deliberately vulnerable fixture and asserts it exits 1. Bandit is substituted by ruff's `S` ruleset, which §10 permits. No waivers exist. |

## §11 Gate policy

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| §11.1 | Per-PR checklist plus tests; security review for security-critical issues | `tests/test_security_audit.py` | COVERED — process, enforced here only as traceability. |
| §11.2 | Per-layer security review, IDOR suite green, dependency audits clean | `tests/test_idor.py::TestCrossTenantIsolation` | COVERED — process. |
| §11.3 | This document maps every bullet, and a test keeps it honest | `tests/test_security_audit.py::TestTheAuditIsComplete` | COVERED |

---

## SPEC §19

| ID | Requirement | Enforcing tests | Status |
|---|---|---|---|
| SPEC §19.1 | Credentials and tokens encrypted at rest, never logged | `apps/common/tests/test_encryption.py`, `apps/common/tests/test_logging.py::TestEncryptedFieldPlaintextNeverReachesLogs` | COVERED |
| SPEC §19.2 | Webhook signature verification mandatory; raw-body HMAC before parsing; constant-time compares | `apps/channels/tests/test_security.py::TestSignatures`, `apps/channels/tests/test_webhooks.py::TestSignatureFailures`, `tests/test_token_routes.py::TestNoCredentialIsComparedWithEquality` | DEVIATION — one, and it is inherent to the provider. SES/SNS signs a canonicalised set of fields with RSA rather than the raw body, so `apps/channels/providers/email.py` verifies a parsed payload. Size and depth caps still run first, so the parse is bounded. Recorded rather than hidden. |
| SPEC §19.3 | SSRF guard on external_request and any URL fetch | `tests/test_ssrf_call_sites.py::TestEveryRequestLeavesThroughAKnownDoor`, `apps/flows/tests/test_node_external_request.py`, `apps/channels/tests/test_media.py` | PARTIAL — the same two non-HTTP paths as §6.1 and §6.5. |
| SPEC §19.4 | Placeholders auto-escape in email HTML, plain text elsewhere | `apps/flows/tests/test_rendering.py::TestSstiBan`, `apps/channels/tests/test_send_email_node.py` | COVERED |
| SPEC §19.5 | Instagram message-deletion webhooks redact the stored body | `apps/channels/tests/test_instagram_deletions.py::TestRedaction`, `apps/channels/tests/test_instagram_deletions.py::TestTheTombstone` | COVERED — end to end: a signed `message_deletions` delivery to `/webhooks/instagram/` through the real pipeline, asserting the body is replaced and the row kept. |
| SPEC §19.6 | GDPR: contact hard delete of contact, identities, field values and message bodies, keeping anonymized counters; JSON export on the contact view | `apps/contacts/tests/test_erasure.py::TestItRemovesWhatItClaims`, `apps/contacts/tests/test_erasure.py::TestAndNothingElse`, `apps/contacts/tests/test_erasure.py::TestNoPiiSurvivesAnywhere`, `apps/contacts/tests/test_erasure.py::TestEveryContactReferenceHasBeenClassified`, `apps/contacts/tests/test_subject_export.py::TestConsentIsIncluded`, `apps/api/tests/test_erasure.py::TestScope` | COVERED — new in #29. |
| SPEC §19.7 | Opt-out enforced in the compliance engine so it cannot be bypassed | `tests/test_opt_out_unbypassable.py::TestTheChokepointIsStructural`, `tests/test_opt_out_unbypassable.py::TestNoSourceReachesAnAdapter`, `apps/messaging/tests/test_compliance.py::TestOptOutBeatsEverything` | COVERED — new in #29. The structural half is what makes it a property: `adapter.send()` has exactly one call site, so "the chokepoint refuses" is a claim about every path rather than the ones somebody thought to test. |

---

## What erasure cannot reach

Three categories of personal data survive a contact erasure. Each is a decision,
and `apps/contacts/tests/test_erasure.py::TestNoPiiSurvivesAnywhere` pins the
list so a fourth cannot join it quietly.

| Category | Why it survives | Bound |
|---|---|---|
| `channels.EmailSuppression` | A bounce or spam report is a fact about a **mailbox**, not about a contact row. The list is keyed on the address with no foreign key precisely so deleting and re-importing a contact cannot undo it — `apps/channels/models.py` argues it, and a test has asserted it since Layer 5. | Kept indefinitely, by design. Disclosed in the subject export's `retained` section. |
| `channels.WebhookEventLog.raw` | Raw inbound deliveries, kept for replay protection and debugging. Keyed on `(connection, provider_event_id)` with no contact reference, so it cannot be searched by person. | Pruned 30 days after receipt by housekeeping. Filed as [#95](https://github.com/brightbeanxyz/brightbean-chat/issues/95). |
| `contacts.ContactImport` | An uploaded spreadsheet quotes whatever cells it contained, and nothing links a row of it to the contact it created. | The file is pruned after the retention window; the row errors are not. Filed as [#95](https://github.com/brightbeanxyz/brightbean-chat/issues/95). |

The subject export names all three in its `not_included` and `retained` sections
rather than presenting itself as complete.

---

## Findings

Fifteen, from a sweep of the merged tree against the baseline. Fixed inline
where the change was a test, a document or a single line; filed where it needed
another app's production code changed, which a hardening PR is the wrong place
to do.

### Fixed in this PR

| # | Finding | Baseline | What changed |
|---|---|---|---|
| 1 | No `SECURITY.md` anywhere in the repository — no disclosure policy, no safe harbour, nothing in GitHub's Security tab | §11 | Added, with `docs/pentest-runbook.md` beside it |
| 2 | `/m/` media-delivery tokens were logged in full. `apps/media_library/views.py` reads the token and then queries `unscoped()`, so the token is the authorisation | §5.2, §4.4 | Prefix added; `tests/test_token_routes.py` derives the list from the URL conf so the next route cannot be missed |
| 3 | `apps/channels/providers/email_signatures.py` was the only `guarded_request` call site with no `guard_required()` proof — its tests replaced the symbol | §6.2 | `apps/channels/tests/test_email_signature_fetch.py`, driving the real guard including a metadata-address resolution |
| 4 | The tenant-model sweep could not fail. It built the list of scoped models and asserted only that the test model was in it | §1.1 | Rewritten to sweep every first-party model against a reasoned `NOT_TENANT_DATA` table |
| 5 | The Meta `EAA…` scrubber regex was compiled twice, once per issue that added it | §5.2 | Folded into one, keeping both rationales |
| 6 | No `makemigrations --check` in CI, despite a migration docstring claiming since Layer 4 that it runs there | §10.1 | Added to the `test` job |
| 7 | Baseline §8 attributed the security headers to a Caddy config that does not exist here; they are set by the application | §8.4 | Wording corrected |
| 8 | Baseline §4 did not name the two deliberate divergences, so the checklist read as unmet against code that was right | §4.1 | Both recorded, with the argument each makes |
| 9 | SPEC §19's "raw-body HMAC" has an SES/SNS exception nobody had written down | SPEC §19.2 | Recorded above |
| 10 | `templates/contacts/_activity.html` reversed `{% url 'inbox' %}`, but the app is namespaced — a 500 on the contact detail page for any contact with messages | — | One-line fix. Not a security finding; found by the first test to render that page with messages present |

### Filed

| Issue | Finding | Baseline | Why not fixed here |
|---|---|---|---|
| [#91](https://github.com/brightbeanxyz/brightbean-chat/issues/91) | boto3/SES reaches the network outside both the guard and `request_json`; botocore owns its transport | §6.1, §6.5 | Closing it means a botocore event hook that validates addresses — design work in another app's provider |
| [#92](https://github.com/brightbeanxyz/brightbean-chat/issues/92) | Workspace SMTP applies the guard's address rules as a pre-flight but does not pin the connection, leaving a check-then-connect window | §6.1, §6.5 | Needs a custom `smtplib` socket factory |
| [#93](https://github.com/brightbeanxyz/brightbean-chat/issues/93) | The SES `region` is unvalidated free text and becomes part of the outbound host, while the sibling `CERT_URL_RE` does pin its region label | §6.1 | Belongs with 11; a regex on the form alone would be a half-measure |
| [#94](https://github.com/brightbeanxyz/brightbean-chat/issues/94) | No secrets sweep over admin displays and API responses, which §5.2 names alongside logs | §5.2 | Wants a canary-based sweep across every registered ModelAdmin and every API route — worth doing properly, not squeezed in |
| [#95](https://github.com/brightbeanxyz/brightbean-chat/issues/95) | `WebhookEventLog.raw` and `ContactImport.errors` hold personal data erasure cannot target | SPEC §19.6 | Retention is the control today. Shortening the webhook window would weaken replay protection, so it needs a decision rather than a patch |

Two more worth knowing about and not filed: `apps/api/tests/support.py` keeps a
private `FakeInternet` that duplicates `tests/ssrf.py`'s, contradicting the
latter's own consolidation rationale — left alone because editing another
workstream's suite for zero behaviour change is what this PR is meant not to do.
And `/c/` and `/o/` (§4.1) do not exist: they are issue #26's, and
`tests/test_token_routes.py` will require their prefixes the moment they land.
