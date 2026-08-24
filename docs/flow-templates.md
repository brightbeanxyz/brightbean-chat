# Flow templates — the export/import format

A flow is portable. Export one from any BrightBean Chat installation, hand the
file to somebody else, and they can import it into theirs. This document is the
format specification, the list of what is stripped on the way out and why, and
the rules an importer can rely on.

Implemented by [`apps/flows/portability/`](../apps/flows/portability/); the
shipped starter templates are in [`flow-templates/`](../flow-templates/).

---

## The short version

* **Export** scrubs everything that belongs to the exporting workspace and
  replaces every workspace-local id with a synthetic reference, recording in a
  `requirements` manifest what each one has to become.
* **Import** validates the file before it touches the database, asks you to
  answer every requirement, shows you a dry run — including every URL the flow
  would call — and only then creates anything.
* An imported flow arrives as an **unpublished draft** with its **triggers
  switched off**. Publishing stays a human action.

---

## The document

```jsonc
{
  "app": "brightbean-chat",
  "format": 1,          // this envelope's own version
  "schema": 1,          // the graph format the flows are written against
  "entry": "flow-1",    // which flow in `flows` was the one exported
  "flows": [
    {
      "key": "flow-1",
      "name": "Telegram welcome and FAQ",
      "folder": "Starters",
      "graph": { "schema": 1, "nodes": [ … ], "edges": [ … ] },
      "triggers": [
        { "type": "welcome", "platform": "telegram", "config": {} }
      ]
    }
  ],
  "requirements": {
    "tag": [ { "key": "needs a person", "name": "Needs a person", "used_by": ["flow-1:tag_human"] } ],
    "custom_field": [], "sequence": [], "segment": [], "member": [], "flow": [],
    "media": [], "platform": [ { "key": "telegram", "name": "telegram", "used_by": ["flow-1:trigger-0"] } ],
    "request_header": [], "whatsapp_template": [], "link_handle": [],
    "from_override": [], "comment_posts": []
  }
}
```

**Two version stamps.** `format` is the envelope's; `schema` is
[`SCHEMA_VERSION`](../apps/flows/schema/envelope.py), the graph format each
`graph` also carries in its own `schema` key. Neither is guessed at when it does
not match — a future format wants a migration, and a migration that never ran is
worse than a refusal. An export that did not carry its schema version would be
an import nobody could validate later.

**`flows` is always a list.** A single-flow export is a list of one. Bundle
export — following `start_flow` and sequence-step references to their closure —
then needs no second shape, so there is one schema, one validator and one
importer rather than two that drift. This is a deliberate deviation from the
singular `flow: {name, graph}` sketched in issue #27.

**Triggers are part of the document.** SPEC §21 phase 3 asks for a round trip
"including triggers", and a graph on its own is not a flow — it is a flow with
no way to start. A trigger travels as its type, its config, and the *platform*
it was bound to.

**Serialisation is canonical**: sorted keys, two-space indent, no timestamps.
Two exports of the same flow are byte-identical, which is what makes the
round-trip guarantee something a test can assert rather than describe.

---

## Workspace-local ids: the decision

A tag, a custom field, a sequence, a segment, a member, a media asset, a channel
connection and another flow all mean something **only in the workspace that owns
them**. Copying such an id into a shared file produces an import that points at
nothing — and silently importing a dangling id is the failure this format exists
to prevent.

So every workspace-local id is replaced on export by a **synthetic reference**:
a UUIDv5 derived from the reference's kind and its position in walk order. Two
consequences are worth knowing:

* **An exported document is still a valid flow graph.** The references are
  UUIDs, and the fields that hold them accept UUIDs — including
  `rule.tag_id` and `rule.field_id`, which are pattern-constrained to exactly
  that shape. So an untrusted file can be validated in full, against the
  unmodified graph and trigger schemas, before anything touches the database.
* **References are numbered, not named.** The second export of an imported flow
  mints the same references in the same order whatever the importing workspace
  decided to call things, which is what makes the round trip byte-stable.

Two things already travel as **names** and stay that way: `add_tag` / `remove_tag`
carry a tag name, and `set_field` / `clear_field` / a `data_collection` target
carry a custom-field name. That is the product's existing addressing convention
(see [`apps/flows/engine/nodes/action.py`](../apps/flows/engine/nodes/action.py)),
not something this format invented. A name is portable already; the mapping step
can still rewrite one so a template naming `plan` imports into a workspace whose
field is `Plan tier`.

### What each reference becomes

| Reference | Where it appears | Travels as | Answered on import by |
|---|---|---|---|
| Tag | `add_tag`/`remove_tag` (name); condition rule, `rule.tag_id` (id) | name, plus a reference for the id sites | create it, or map to an existing tag |
| Custom field | `set_field`/`clear_field`, `data_collection` target, response mapping (name); condition rule, `rule.field_id` (id) | name, plus a reference | create it (you pick the type), or map |
| Sequence | `subscribe_sequence`/`unsubscribe_sequence`, condition rule | reference + name | create it (arrives empty), or map |
| Segment | condition rule | reference + name | **map only** — see below |
| Member | `assign_conversation`, `notify_members` | reference **and nothing else** | map to a member; defaults to you |
| Flow | `start_flow` | reference + name | resolved from the bundle when it is in the file; otherwise map, or create an empty draft under the expected name |
| Media asset | `send_message` media block, card image | reference + filename and kind | pick an asset, or paste a URL |
| Channel connection | a trigger's binding | its **platform**, or `null` | pick a connection, or leave it to every connection of that platform |

A custom field reached only by **name** carries no type, so "create it" defaults
to text and the mapping step asks you to confirm. Deriving the type from the
exporting workspace's field would be a nicer default and would break the
byte-stable round trip: a template referencing a field its own workspace does not
have would gain a type on the way back, and the second export would no longer
match the first.

A **segment** is the one thing that can only be mapped. It is a saved *filter*,
and inventing one that matches nobody would silently change what an imported
condition means — so if a template needs a segment and your workspace has none,
the import says so and stops rather than importing a condition that quietly does
the wrong thing.

### What is removed outright

| Removed | Why |
|---|---|
| `external_request` header **values** | An `Authorization: Bearer …` is the exporter's credential. Header *names* survive and each becomes a question you can answer with your own value. |
| `ref_url.link_handle` | The exporting account's public handle. |
| `send_email.from_override` | The exporting workspace's sending address. |
| `comment.post_ids` | Platform ids of the exporter's own posts. The list is **emptied**, not removed: `post_scope: specific` with no posts listed matches nothing, and keeping the key is what raises the question so you can list your own. |
| `whatsapp_template.template_id` | A row id in the exporter's workspace. `reference` (`<name>/<language>`) survives — that is the Cloud API's own key and what actually reaches the wire, so the node still works. |
| Row ids, timestamps, `created_by`, workspace and organization identifiers | Not part of a template. |
| Media **delivery URLs** | They are unguessable, long-lived signed URLs. Putting one in a shared file would hand every reader read access to the exporter's asset for as long as it exists. A library asset leaves as a reference and a filename hint instead. |

### What "zero workspace-identifying data" means, exactly

An exported document carries no database id, member identity, connection
identity, credential, signed media URL, workspace name or organization id. It
*does* carry the author's own content — the flow's name and folder, message
text, keyword lists, an asset's filename — because that content is the template.
`apps/flows/tests/test_portability_export.py` asserts the first list against the
serialised bytes.

---

## What the importer does, in order

1. **Size cap**, before the file is parsed. Then `json.loads`, then a depth cap
   measured iteratively, then a scan for NUL characters — JSON permits
   `\u0000` and Postgres `text` and `jsonb` do not, so a document carrying one
   is refused rather than allowed to fail at insert time.
2. **Envelope schema**, with unknown keys rejected (the mass-assignment guard of
   [SECURITY-BASELINE §7](SECURITY-BASELINE.md)).
3. **Version checks** on `format` and `schema`.
4. **Every graph** through `apps.flows.schema.validate_graph` — which is where
   `MAX_NODES`, `MAX_EDGES` and the size and depth caps are enforced — and
   **every trigger config** through
   `apps.flows.triggers.validation.validate_config`.
5. **Requirements**, re-derived by walking the flows themselves.
6. **Mapping**, then a **dry run**.
7. **Creation**, in one transaction, on an explicit confirm.

Nothing before step 7 creates a flow, a tag, a field, a sequence or a trigger.
The only thing an upload writes is the `FlowImport` row holding the validated
document.

### The manifest is advisory

`requirements` in the file is there for a human reading it. The importer
**re-derives** the requirement set by walking the graphs and trigger configs, so
a document whose manifest omits a reference cannot talk it into leaving one
dangling, and one that invents a requirement nothing references is simply never
asked about. What the manifest supplies is *labels* — the name behind a
reference — which are displayed and used as defaults, never as authority.

### Node and edge ids are preserved

They are graph-local: bounded by the graph schema, unique within their own
document, and used at runtime only inside execution-scoped keys
(`exec:{execution_id}:node:{node_id}`). There is nothing to rewrite, and
rewriting them would break byte-stable round-tripping for no benefit. Canvas
positions are preserved too.

### Message bodies cannot reach a template engine

An imported message body carries `{{placeholders}}`. They are substituted by
[`apps/flows/rendering.py`](../apps/flows/rendering.py), which is plain token
substitution against a fixed grammar with no template engine anywhere near it —
so imported text is data, not a program (SECURITY-BASELINE §3). The import
wizard renders every string from the file through Django's autoescaping, with no
`|safe` and no `mark_safe`.

### An `external_request` node points somewhere you did not choose

The dry run **lists every one of them** — method and URL, as text, not as a
link — before the import can be confirmed. Beyond that, the flow arrives
unpublished, so nothing runs until you publish it; and when it does run, the
request goes through
[`apps.common.outbound.guarded_request`](../apps/common/outbound.py), which
refuses loopback, link-local and private addresses, pins the resolved address so
DNS cannot rebind under it, and re-validates every redirect
(SECURITY-BASELINE §6). Importing a flow never fetches anything.

---

## Bundles

A `start_flow` node hands over to another flow, and a sequence a flow subscribes
to runs flows of its own. **Export bundle** follows both references to their
closure and puts the whole set in one file, up to 20 flows.

The sequence itself is *not* exported — it is a workspace object with a schedule
and live enrollments, so it appears in `requirements` as something to create or
map. That means the flows a sequence's steps start travel with the bundle, while
the imported sequence arrives empty, waiting for you to put those flows on its
rungs. A bundle exported from the *imported* flow is therefore legitimately
smaller than the one you imported. This asymmetry is stated rather than papered
over.

---

## Contributing a template

Templates live in [`flow-templates/`](../flow-templates/) — not in `templates/`,
which is Django's own template directory.

1. Build the flow in a workspace, then use **Export** on the flow list.
2. Drop the file into `flow-templates/` with a descriptive kebab-case name.
3. Run the validator:

   ```bash
   python manage.py validate_flow_templates
   ```

4. Open a pull request. `apps/flows/tests/test_portability_library.py` validates
   every file in the directory and imports each one into a clean workspace, so a
   template that stops working turns the build red.

Keep a template free of anything specific to your workspace: no real customer
names, no live URLs you do not control, no credentials. The export already
removes the ones it can identify, but a message body is your own prose and it
travels exactly as you wrote it.

---

## Limits

| Limit | Value |
|---|---|
| Whole document | 4 MiB |
| Flows per document | 20 |
| Triggers per flow | 50 |
| Nodes / edges per flow | `MAX_NODES` (500) / `MAX_EDGES` (2000) |
| Per-graph size / depth | `MAX_GRAPH_BYTES` (512 KiB) / `MAX_GRAPH_DEPTH` (20) |

The per-graph limits are the ones the builder already enforces
([`apps/flows/schema/envelope.py`](../apps/flows/schema/envelope.py)); an import
does not get its own, looser set.
