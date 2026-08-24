# Security policy

BrightBean Chat is self-hosted, and a self-hosted deployment is exposed to the
internet from the day it accepts its first webhook. We would much rather hear
about a problem from you than from somebody's incident review.

## Supported versions

There are no tagged releases yet. **`main` is the supported version**, and a fix
lands there.

That is a real answer rather than a placeholder: publishing a support matrix for
versions that do not exist would be inventing a promise. Once releases begin,
this section becomes the newest minor plus the previous one for 90 days, and
this paragraph goes away.

## Reporting a vulnerability

**Please do not open a public issue for anything exploitable.** A public report
on a self-hosted product is a disclosure to every operator's attacker at the
same moment it reaches us, and most operators will not be reading GitHub that
day.

Use **GitHub's private vulnerability reporting** on this repository — the
*Security* tab, then *Report a vulnerability*. It gives us a private fork to fix
in and you a CVE path if the finding warrants one, and it works without either
of us publishing an email address for spam to find.

If private reporting is disabled or you cannot reach it, open a public issue
containing **only** the words "security report, requesting a private channel"
and nothing about the finding, and a maintainer will open one.

What to include: [`docs/pentest-runbook.md`](docs/pentest-runbook.md) ends with
a report template. The short version is the commit you tested, how to reproduce
it, what you saw, what you expected, and who it affects — cross-tenant,
cross-user, or only yourself.

A **failing test is worth more than a screenshot** here, and we will usually ask
for one. If you can write it, the fix ships faster and cannot regress.

## What to expect

| Stage | Target |
|---|---|
| Acknowledgement | 3 business days |
| Triage and a severity | 10 business days |
| Fix — Critical | 14 days |
| Fix — High | 30 days |
| Fix — Medium | 90 days |
| Fix — Low | The next convenient release |

These are targets for a small maintainer team, not a contractual SLA, and we
would rather write them down and occasionally miss than leave you guessing.

Disclosure is coordinated: an advisory goes out with the fix, and we will not
sit on a report indefinitely — 90 days is the backstop, sooner if the fix is
ready. You get credit by default, under whatever name you give us; tell us if
you would rather not.

## Safe harbour

Good-faith research within this policy is authorised, and we will not pursue or
report you for it. Conditions, all of which amount to "test your own instance":

- Test a deployment **you own or are authorised to test**. Not somebody else's.
- Do not access, modify or retain another person's data. If you encounter it,
  stop and tell us what you did to get there.
- No denial of service, no resource exhaustion, no spam through a deployment's
  channel integrations.
- No social engineering of maintainers, contributors or users, and no physical
  attacks.
- Do not attack infrastructure we do not control — GitHub, PyPI, npm, or the
  messaging platforms this integrates with.
- Stop at proof. You do not need to pivot to demonstrate impact.

## Threat model

From [`docs/SECURITY-BASELINE.md`](docs/SECURITY-BASELINE.md), which is the
checklist every pull request is reviewed against:

> A self-hosted deployment exposes webhook endpoints and public token routes to
> the internet from day one; message content, usernames, comment bodies, and
> media URLs arrive from **strangers** (attacker-controlled); users author
> automations that make server-side HTTP requests; multiple workspaces share one
> database; the encrypted platform credentials are the crown jewels.

Three trust levels, and most findings are about a boundary between two of them:

| | Who | What they may do |
|---|---|---|
| **Untrusted** | Webhook callers, contacts, anyone holding a public token | Reach the webhook endpoints and the signed token routes. Nothing else. |
| **Semi-trusted** | Workspace members, API keys | Bound by RBAC (`apps/members/roles.py`) and by scope (`apps/api/auth.py`), and by workspace. A member of one workspace is untrusted with respect to another. |
| **Trusted** | The operator | Controls the settings, the database and the encrypted credentials. |

What the platform defends, and where it is proven, is enumerated in
[`docs/security-audit.md`](docs/security-audit.md) — every baseline item mapped
to the test that enforces it, including the ones currently only partly covered.
Reading that first will save you time; the gaps are written down.

## Out of scope

Not because they do not matter, but because they are not ours to fix:

- **Deployment configuration the operator owns** — TLS termination, reverse
  proxy rules, firewalling, database exposure, backups, OS patching.
- **A misconfigured instance.** `DEBUG=True` in production, a placeholder
  `SECRET_KEY` or `ENCRYPTION_KEY_SALT`, an empty `ALLOWED_HOSTS`. Production
  settings already refuse to boot on these (`apps/common/checks.py`); a report
  that they are dangerous when deliberately overridden is not a vulnerability.
- **`EXTERNAL_REQUEST_ALLOW_PRIVATE` switched on.** It exists so an on-prem
  deployment can reach its own network, it relaxes the private-range rule alone,
  and turning it on is a decision the operator makes.
- **An operator or superuser doing what they can do.** The Django admin decrypts
  stored credentials by design and is superuser-gated.
- **Third-party platform vulnerabilities** — Meta, Twilio, Telegram, AWS.
  Report those to them.
- **Automated scanner output with no demonstrated impact**: a missing header on
  an endpoint where it changes nothing, a "weak" cipher your own proxy chose, a
  rate limit on an endpoint that already documents its limits.
- **Findings against `brightbean.xyz` or any instance that is not yours.**

If you are not sure which side of a line something falls on, report it. We would
rather read one that turns out to be out of scope than miss one that was not.
