# BrightBean Chat Platform API v1

A bearer-authenticated JSON API for the system that **runs** a BrightBean Chat
installation (a control plane, a billing back end) rather than for a customer
inside it. It provisions tenants: an organization with its owner and first
workspace, the people in it, more workspaces, and workspace API keys. Everything
a tenant then does — contacts, messages, flows — happens through the
[workspace API](v1.md) using the keys issued here.

Invitations are **not** part of this API. They stay in the organization's own
member management; this API adds people directly (see
[Users](#add-a-user-to-an-organization)).

Base URL below is `https://chat.example.com`; substitute your own. There is no
interactive page and no public OpenAPI document: this API provisions tenants and
its shape is not for anonymous readers.

---

## Authentication

A platform key is installation-level: it names no workspace and no
organization, and can reach all of them. Treat it like a database password.

```bash
python manage.py create_platform_key --name zirek-prod   # prints bbp_… once
python manage.py revoke_platform_key <id>                # id is printed on stderr at creation
```

Send it as a bearer token:

```bash
curl https://chat.example.com/api/platform/v1/organizations/<id> \
  -H "Authorization: Bearer bbp_..."
```

* **The key is shown once.** Only a keyed digest is stored.
* **Revocation is immediate.**
* **HTTPS only** outside development.
* **`bbp_` and `bb_` keys are not interchangeable.** A workspace key gets `401`
  here and a platform key gets `401` on `/api/v1/`.
* Same limits as the workspace API: 10 requests per second per key (a separate
  bucket from workspace keys; raise `API_RATE_LIMIT_PER_SECOND` if you provision
  in bursts), bodies up to `API_MAX_BODY_BYTES`, `429` with `Retry-After` when
  over.
* **Failed authentications are counted per client address, shared with the
  workspace API.** A misconfigured tenant integration behind the same address can
  use up the budget a platform client needs; a revoked key does not count, only
  unrecognised tokens do.

## Errors

The same envelope as the workspace API:

```json
{"error": {"code": "other_organization", "message": "…", "detail": {}}}
```

| Code | Status | Meaning |
|---|---|---|
| `unauthenticated` | 401 | Missing, malformed, unknown or revoked key. |
| `not_found` | 404 | No such organization, workspace or key — **also** the answer when the id exists but belongs to a different organization than the one in the path. |
| `invalid_request` | 422 | A field is missing, unknown, malformed, or the service refused it (unknown role or scope, bad email, blank name). |
| `duplicate` | 409 | A workspace with that name already exists in the organization. |
| `other_organization` | 409 | The account already belongs to a different organization (one per account). |
| `external_id_conflict` | 409 | The `external_id` exists but for an organization with a different name or owner. |
| `conflict` | 409 | A concurrent change blocked the request and no result could be found; retry. |
| `deleted` | 409 | The `external_id` belongs to an organization that was deleted; ids are not recycled. |
| `single_workspace` | 409 | The deployment runs with `SINGLE_WORKSPACE_MODE`, so a workspace is only created with its organization. |
| `plan_workspaces`, `plan_seats`, `plan_api` | 409 | The organization's plan does not include another workspace, user or API key. |
| `key_limit` | 409 | The workspace already has the maximum number of active keys (50). |
| `rate_limited` | 429 | Over the per-key limit. |

Unknown request fields are rejected (`422`), not ignored, and so is input longer
than the column it lands in (organization and workspace names 100, `external_id`
128, person names 255, email 254): nothing is silently truncated.

---

## Organizations

### Create a tenant

`POST /api/platform/v1/organizations`

```bash
curl -X POST https://chat.example.com/api/platform/v1/organizations \
  -H "Authorization: Bearer bbp_..." -H "Content-Type: application/json" \
  -d '{"name": "Acme", "external_id": "tenant-42",
       "owner_email": "boss@acme.test", "owner_name": "Boss",
       "workspace_name": "Main"}'
```

Creates the organization, the owner's account if it does not exist (no
password; they sign in through SSO or a password reset), an owner membership and
a first workspace in which the owner is admin. `external_id` and
`workspace_name` are optional; the workspace is called "My Workspace" if you
give none.

```json
{"id": "0192f…", "name": "Acme", "external_id": "tenant-42",
 "workspaces": [{"id": "0192f…", "name": "Main", "is_archived": false}],
 "member_count": 1}
```

**Idempotent by `external_id`.** Repeating the request returns the existing
organization with `200` instead of `201`, and creates nothing — safe to retry
after a timeout. The replay must describe the same tenant: a different `name` or `owner_email` is
`409 external_id_conflict`, and an organization deleted since is `409 deleted`. The owner must not already belong to another organization
(`409 other_organization`).

### Read one

`GET /api/platform/v1/organizations/{org_id}` returns the same document.

## Workspaces

`POST /api/platform/v1/organizations/{org_id}/workspaces` with `{"name": "Support"}`
→ `201 {"id", "name", "is_archived"}`. Names are unique per organization
(`409 duplicate`) and the plan's workspace limit applies (`409 plan_workspaces`).
The workspace starts with no members; add people with the next call.

## Add a user to an organization

`POST /api/platform/v1/organizations/{org_id}/users`

```json
{"email": "dev@acme.test", "name": "Dev", "org_role": "member",
 "workspaces": [{"workspace_id": "0192f…", "role": "editor"}]}
```

→ `201 {"id", "email", "name", "org_role", "user_created"}`.

* The account is created if the address is new. Addresses are matched
  case-insensitively and stored in lower case.
* `org_role` is `admin` or `member`; an owner cannot be added this way.
* Every `workspace_id` must belong to this organization.
* Adding someone already in the organization returns `200` and changes nothing:
  the `org_role` and `workspaces` in the request are **not** applied to an
  existing member.
* It is all or nothing. A refused add (no free seat, an unknown role, a
  workspace of another organization) does not leave the new account behind.
* A person in a different organization gets `409 other_organization`.
* The plan's seat limit applies (`409 plan_seats`).

**Signing in.** These accounts have no invitation, so they reach the product
through single sign-on. That needs `OIDC_LINK_BY_EMAIL` left on (the default):
the provider's verified email is matched to the account and linked on first
sign-in. With it off and `OIDC_SIGNUP=invite`, they are refused.

## Workspace API keys

### Issue

`POST /api/platform/v1/organizations/{org_id}/workspaces/{ws_id}/api-keys`

```json
{"name": "zirek", "scopes": ["read", "write"]}
```

```json
{"id": "0192f…", "name": "zirek", "scopes": ["read", "write"],
 "lookup_prefix": "a1b2c3d4", "token": "bb_…"}
```

`token` is a regular workspace key for [`/api/v1/`](v1.md) and **appears only in
this response**. Scopes are `read`, `write`, `erase`; any combination may be
granted because no member is issuing it. The plan's API access and the
50-active-keys-per-workspace cap apply. The key has no creator, and survives
changes to anyone's membership until it is revoked.

### Revoke

`DELETE /api/platform/v1/organizations/{org_id}/workspaces/{ws_id}/api-keys/{key_id}`
→ `204`. Takes effect on the next request; repeating it is harmless.

---

## A whole tenant, end to end

```bash
H='-H "Authorization: Bearer bbp_..." -H "Content-Type: application/json"'
B=https://chat.example.com/api/platform/v1

# 1. Tenant, owner and first workspace
curl -X POST $B/organizations $H \
  -d '{"name":"Acme","external_id":"tenant-42","owner_email":"boss@acme.test"}'

# 2. A colleague, straight into the workspace
curl -X POST $B/organizations/$ORG/users $H \
  -d '{"email":"dev@acme.test","org_role":"member","workspaces":[{"workspace_id":"'$WS'","role":"editor"}]}'

# 3. A key for the integration that will talk to the workspace API
curl -X POST $B/organizations/$ORG/workspaces/$WS/api-keys $H \
  -d '{"name":"zirek","scopes":["read","write"]}'

# 4. The tenant's key at work
curl https://chat.example.com/api/v1/contacts -H "Authorization: Bearer bb_..."
```
