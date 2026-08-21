# Contributing to BrightBean Chat

Conventions that are load-bearing across issues. `docs/SPEC.md` is the product
specification, `docs/ROADMAP.md` the plan, and `docs/SECURITY-BASELINE.md` the
per-PR security checklist — this file covers the things a reviewer will ask you
about that are not in any of those.

## Tenant scoping is enforced, not remembered

Every model holding tenant data inherits `apps.common.scoping.WorkspaceScopedModel`.
It brings a `workspace` foreign key and, more importantly, a manager whose
querysets **refuse to run** until they have been scoped:

```python
Contact.objects.filter(status="active")  # UnscopedQueryError
Contact.objects.for_workspace(request.workspace)  # fine
```

The guard fires at execution, not at `.filter()`, and covers every terminal
operation — iteration, `count()`, `exists()`, `aggregate()`, `update()`,
`delete()`, `iterator()`, `in_bulk()`. An unscoped `update()` is the most
damaging of the set, which is why `_fetch_all` alone is not enough.

Three rules:

1. **Never use `.objects.all()` on a tenant model** in a view, service or API.
   If you find yourself reaching for it, you want `.for_workspace(...)`.
2. **`.unscoped()` needs a comment saying why.** It exists for code that
   genuinely operates across tenants — housekeeping sweeps, superuser admin
   actions, migrations. It is greppable on purpose.
3. **`all_objects` is Django's, not yours.** It is declared before `objects` so
   the admin, cascade deletes and reverse related access (`workspace.contacts.all()`,
   already scoped by construction) keep working. `apps.common.checks.check_workspace_scoped_models`
   fails the build if that ordering is ever reversed.

There is exactly one `WorkspaceScopedManager`, in `apps.common.scoping`. Studio's
non-enforcing namesake is deliberately not ported: two classes of the same name,
one of which quietly enforces nothing, is a trap rather than a convenience.

### Cross-tenant access answers 404, never 403

A 403 confirms the id names something real. Over a UUID space, that confirmation
is the only thing an attacker was missing. Use
`apps.common.shortcuts.get_scoped_object_or_404`, or scope the lookup and let
`get_object_or_404` do it:

```python
membership = get_object_or_404(OrgMembership, pk=membership_id, organization=request.org)
```

`RBACMiddleware` applies the same rule to `/w/<uuid:workspace_id>/` routes.

**403 is still correct for "you are in this workspace but lack the permission"** —
that reveals nothing the caller did not already know.

## Every PR that adds an endpoint extends the IDOR suite

`tests/idor.py` walks the registered URL patterns and hits every route that
names a tenant object with **another tenant's** ids, as a fully privileged member
of a different organization, asserting 404.

It is not opt-in, and it has no silent skips:

* A route carrying an id the suite cannot build raises
  `UnregisteredRouteKwargError`, so adding
  `/w/<uuid:workspace_id>/contacts/<uuid:contact_id>/` without registering a
  `contact_id` resolver turns the suite red. Register it in
  `TENANT_KWARG_RESOLVERS` (ids that identify a tenant's object) or
  `NEUTRAL_KWARG_VALUES` (values that need to exist but identify nothing).
* A tenant route with no `name=` raises `UnnamedTenantRouteError`, because the
  suite reverses by name. Endpoints nothing reverses are exactly the ones that
  get registered nameless; give it a name.
* A route that genuinely must not be swept gets a `WAIVED_ROUTES` entry with a
  reason. That is the only exemption, and it is a reviewed line of code.

## URLs

* Workspace-scoped routes live under `/w/<uuid:workspace_id>/`. The kwarg name
  `workspace_id` is `RBACMiddleware`'s entire resolution contract — a route that
  spells it differently silently loses the membership check and the 404.
* Org-scoped management lives under `/organization/`. v1 is one organization per
  user, so there is no id in the URL.
* No slugs. A slug is a second, mutable identifier for a tenant boundary.

## Roles and permissions

`apps/members/roles.py` is the only place role levels and the permission matrix
are written down. `decorators.py` and `services.py` import from it; do not
re-declare either. Adding a permission key means adding it to `PERMISSION_KEYS`
and to every role row — `apps/members/tests/test_roles.py` asserts that every
role answers every key, so a half-added key fails rather than defaulting to
denied-by-accident.

### Decorator stacking

Outermost first, at every call site:

```python
@login_required
@require_permission("manage_members")
@require_POST
def some_view(request, workspace_id): ...
```

The order matters beyond tidiness. `require_POST` innermost means the tenancy
and permission checks run before the method check, so a GET from another tenant
answers 404 rather than 405 — and a 405 would confirm the route and the object
exist.

Prefer `require_permission` over `require_workspace_role`: it reads
`effective_permissions` and nothing else, which is the protocol the public API
(#25) duck-types a bearer-token membership against.

## Secrets

Credentials and tokens go in `EncryptedTextField` / `EncryptedJSONField` from
`apps.common.encryption`, never a plain column (SECURITY-BASELINE §5).

**Encrypted fields cannot be filtered.** Every write uses a fresh nonce, so
`.filter(secret=value)` compares two unrelated ciphertexts and silently matches
nothing — no exception, just an empty result that reads like "no such row". To
look a row up *by* a secret, store a deterministic HMAC of it in a separate
column and query that. To look one up by tenant and kind, use plaintext columns
(which is what the credential tables do).

Never render a stored secret. `masked_credentials` exists for that.

## Dependencies

`requirements.in` and `requirements-dev.in` are the files humans edit; the `.txt`
files are compiled from them. After changing either:

```bash
make lock
```

Commit both compiled files. CI recompiles and diffs them, so a stale lock fails
the build, and installs use `--require-hashes`.

## Before opening a PR

```bash
make lint typecheck test
```

```bash
make audit
```
