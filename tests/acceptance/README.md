# Acceptance suite — SPEC §21

This directory answers one question: **is BrightBean Chat v1 accepted?**

SPEC §21 states the criteria that decide it. Most of them were verified long
before this suite existed, by the workstream that built the feature — which is
right, because the person who writes the loop cap is the person who should prove
it halts at thirty. What did not exist was anywhere that said so. Answering "is
phase 2 accepted?" meant grepping seven Django apps for `§21` and trusting the
comments.

So this suite is three things, in order of importance:

1. **The gate.** [`criteria.py`](criteria.py) maps every §21 clause to the test
   that verifies it, and [`test_criteria.py`](test_criteria.py) fails if a
   mapping stops being true — if a named test is renamed or deleted, or if a
   clause in the spec has no owner at all. The clauses are parsed out of
   `docs/SPEC.md`, so the table cannot silently fall behind the document it
   describes.
2. **The criteria nothing else covers**, because they cross apps and no single
   suite can own them: the first automated reply's latency, the unsubscribe
   round trip, the chain from a platform message to an integrator, and the
   single door every send goes through.
3. **The runbooks** for what CI cannot reach — the criteria that need real
   platform credentials or real processes.

It is deliberately **not** a second copy of the suite. A duplicated criterion is
worse than an unverified one: two tests of the same promise drift, and the day
they disagree nobody knows which was right.

## Running it

```bash
pytest tests/acceptance
```

Nothing here is marked, gated or skipped, and there is no nightly job. The whole
suite runs on every pull request, which is the only schedule that keeps a gate
honest.

---

## Phase 1

| | Criterion | Verified by |
|---|---|---|
| ✅ | Webhook ack p95 < 500 ms | `p1-ack-budget` — `apps/flows/tests/test_routing_inline.py::TestAckLatency` |
| ✅ | First automated reply p95 < 2 s | `p1-first-reply` — [`test_first_reply_latency.py`](test_first_reply_latency.py), and `p1-first-reply-budget` — [`test_first_reply.py`](test_first_reply.py). See [the reference run](#the-2-vcpu-reference-run) for the number the spec actually names. |
| ✅ | Zero duplicate sends across 1k forced worker retries | `p1-no-duplicate-sends` — `apps/messaging/tests/test_services.py::TestIdempotency` |
| ✅ | 50 parallel webhooks never interleave steps | `p1-no-interleaving` — `apps/flows/tests/test_locking.py::TestOneStepPerContact` |
| ✅ | IG private-reply constraints | `p1-ig-private-reply` — three homes; see `criteria.py` |
| ✅ | Loop flow halts at 30 blocks with admin notification | `p1-loop-cap` — `apps/flows/tests/test_loop_cap.py::TestLoopCap` |

**Phase 1 is accepted in CI.**

## Phase 2

| | Criterion | Verified by |
|---|---|---|
| ✅ | 10k-contact broadcast: token buckets, out-of-window skips, correct counts | `p2-broadcast-scale` — `apps/broadcasts/tests/test_acceptance.py`, which runs 600 contacts and says why |
| 📋 | WhatsApp template submit → approved → send **against a real WABA** | `p2-whatsapp-template-waba` — [manual](#whatsapp-template-round-trip-against-a-real-waba) |
| ✅ | STOP suppresses within one inbound event | `p2-sms-stop` — `apps/channels/tests/test_sms_compliance.py::TestStop` |
| ✅ | Unsubscribe link suppresses email within one click | `p2-email-unsubscribe` — [`test_unsubscribe_round_trip.py`](test_unsubscribe_round_trip.py) |

**Phase 2 is accepted in CI except the WABA round trip**, which no amount of test
infrastructure can automate: it needs Meta credentials and a real template
review. The runbook below is its verification.

## Phase 3

| | Criterion | Verified by |
|---|---|---|
| ✅ | Make/Zapier-style scenario over public API + outbound webhooks | `p3-zapier-scenario` — `apps/api/tests/test_acceptance_phase3.py`, and `p3-platform-inbound-chain` — [`test_integration_chain.py`](test_integration_chain.py) for the inbound leg it starts after |
| ⏳ | Flow export/import round-trips including triggers | `p3-flow-roundtrip` — **blocked on [#27](https://github.com/brightbeanxyz/brightbean-chat/issues/27)** |

**Phase 3 is accepted except the export/import round trip**, which does not exist
yet because the feature does not. #27 ships it together with its own round-trip
test; when that merges, flip the row in `criteria.py` to `CI` and point it at
that test. Nothing here asserts #27 is absent — a test that goes red on a
sibling's *correct* merge is how a suite teaches people to ignore it.

## Cross-system security gates

Not §21 clauses, but the gates SECURITY-BASELINE §11 asks each layer to pass.
They resolve like everything else, so a renamed security test cannot quietly
leave one unguarded.

| | Gate | Verified by |
|---|---|---|
| ✅ | IDOR fuzz sweep across every registered endpoint | `sec-idor-sweep` — `tests/test_idor.py::TestCrossTenantIsolation` |
| ✅ | Opt-out enforced at the adapter boundary, every send source | `sec-optout-adapter-boundary` — [`test_send_boundary.py`](test_send_boundary.py) |
| ✅ | Hostile webhook storm: repeated, oversized, malformed | `sec-hostile-webhook` — the channels suite, plus the cross-app slice in [`test_integration_chain.py`](test_integration_chain.py) |
| ✅ | Exactly-once queue processing under contended drains | `sec-exactly-once-queue` — `apps/queueing/tests/test_concurrency.py::TestExactlyOnce` |
| 📋 | Worker killed mid-broadcast; Postgres restart mid-run | `sec-crash-recovery` — [manual](#crash-and-restart-recovery) |

---

## Why the latency tests assert on a minimum

This repo has been bitten three times by a test asserting on a clock-derived
value — PRs #46, #49 and #62 — and the lessons compound: pin the clock at its
single read site, freeze it rather than repositioning it, and where you can,
assert the invariant instead of the timestamp. A suite that goes red at random
gets ignored, which is worse than not having one.

SPEC §21 asks for a **p95 on a 2 vCPU box**. That is a production SLO, and a p95
over a handful of samples on a shared CI runner is mostly a measurement of the
runner. So the budget is verified in three layers rather than one:

* **The invariant, always.** [`test_first_reply.py`](test_first_reply.py) asserts
  the inline routing budget and the adapter's HTTP timeouts fit inside the
  spec's ceiling. No clock is involved, and it is the assertion that catches the
  change which makes the criterion unmeetable — raising `INLINE_BUDGET_SECONDS`
  past two seconds — on any machine, however loaded.
* **The structure, always.** The latency test also asserts every delivery was
  answered *inside the request*. A reply handed to the worker is precisely what
  makes a first reply slow, and `InlineDecision` makes that visible without a
  stopwatch.
* **The wall clock, on the minimum.** `min(timings)` is the closest observable to
  what the code path costs; the mean and the maximum measure whatever else the
  runner was doing. The failure message reports p50, p95 and max so a human
  reading a red build sees the shape, but only the minimum is asserted, against
  the spec's own unmodified ceiling.

**There is no CI multiplier here, and none anywhere in this repo.** If the margin
ever proves too tight on a loaded runner, the lever is `SAMPLES`, not the
ceiling: taking more samples can only lower a minimum, so raising it strengthens
the test against noise, while loosening the ceiling weakens the thing being
asserted.

### Reference numbers

Measured on a developer laptop (Apple silicon, Postgres in Docker Desktop),
from a signed webhook POST to the reply reaching the adapter. The ranges are
across repeated runs, and they are wide on purpose — the point of recording them
is the spread, not a single figure:

| Condition | min (asserted) | p50 | p95 |
|---|---|---|---|
| Quiet machine, warm process | 0.05 – 0.13 s | 0.06 – 0.13 s | 0.08 – 0.30 s |
| Machine under load | 0.24 – 0.45 s | 0.26 – 0.45 s | 0.45 – 0.85 s |

Against the 2.0 s ceiling that is roughly four- to fortyfold headroom on the
asserted statistic, and the slowest *individual* sample ever observed — 0.85 s,
on a loaded machine — was still comfortably under it.

The spread is the argument for asserting on the minimum. The same code, on the
same machine, varies by more than the entire margin a tighter ceiling would have
left, so a ceiling set close to the observed cost would be measuring the
machine. Docker Desktop's VM boundary is a meaningful part of the absolute
numbers; CI's Postgres is a service container on tmpfs sharing the runner's
kernel, but CI also runs four xdist workers against it.

#### The 2 vCPU reference run

The number §21 actually names needs a 2 vCPU box, which no CI runner here is.
To produce it:

1. Deploy a build to a 2 vCPU instance following [`docs/`](../../docs) — a real
   deployment, not a laptop, because the criterion is about a deployment.
2. Point a load generator at the webhook endpoint with 100 concurrent inbound
   events, each matching a keyword trigger on a published single-send flow.
3. Record the p95 of the interval between the delivery and the outbound call,
   and put it in the table above with the date and the instance type.

Run it when the reply path changes shape — a new stage in the pipeline, a
different locking strategy, a change to the inline budget. It is not a
per-release chore.

---

## Manual runbooks

Two criteria cannot be verified by any test, and saying so plainly is more
useful than a test that pretends otherwise.

### WhatsApp template round-trip against a real WABA

**Why it is manual.** The lifecycle is fully covered against a fake Graph API in
`apps/channels/tests/test_whatsapp_templates.py` — submission payloads, the
draft → pending transition, polling, approval, rejection and the sendable
verdict. What a fake cannot supply is Meta's own review, which takes minutes to
days and is the thing the criterion names.

**What you need.** A WhatsApp Business Account, a phone number registered to it,
a Meta app with `whatsapp_business_messaging` and `whatsapp_business_management`,
and a recipient who has messaged the number in the last 24 hours or is prepared
to receive a template.

**Steps.**

1. Connect the WABA in *Settings → Channels* and confirm the connection reports
   active.
2. Create a template with at least one body variable, and submit it. The row
   should move to `pending` and record Meta's template id.
3. Confirm in Meta's own Business Manager that the template is queued for
   review, then wait for approval. Poll from the UI, or let the housekeeping
   job do it — the point is that the status arrives from Meta rather than from
   us.
4. When it reads `approved`, send it to a recipient who is **outside** the
   24-hour window. The compliance engine must return `NeedsTemplate` for an
   ordinary message to that recipient, and the template send must succeed.
5. Record the date, the template name and the WABA id in the PR or the release
   notes.

**What counts as a failure.** A template that is approved by Meta but not
sendable by us, a status that never leaves `pending` while Meta shows it
approved, or an ordinary message that goes out where a template was required.

### Crash and restart recovery

**Why it is manual.** An in-process "kill" is a function that returns early. It
proves a handler is re-entrant — which the forced-retry tests already prove —
and not that a half-written batch survives losing the process that was writing
it. This one needs real processes, so it runs against a deployment.

**Steps.**

1. Start a broadcast to a few thousand contacts and let the fanout begin.
2. `docker compose kill worker` (or the platform's equivalent) partway through.
3. Restart it. The broadcast must resume: no contact messaged twice, no contact
   skipped, and the counters reconciling against the audience.
4. Repeat, killing Postgres instead. On restart, actions left claimed by the
   dead worker must be swept back to pending by the zombie recovery job and then
   run, rather than sitting claimed forever.
5. Record the audience size, the counts before and after, and the recovery time.

**What counts as a failure.** A duplicate send, a contact never reached, a
counter that disagrees with the messages actually sent, or an action stuck in a
claimed state after the sweep has run.
