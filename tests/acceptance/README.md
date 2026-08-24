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
| ✅ | First automated reply < 2 s, in CI | `p1-first-reply` — [`test_first_reply_latency.py`](test_first_reply_latency.py), and `p1-first-reply-budget` — [`test_first_reply.py`](test_first_reply.py) |
| 📋 | …and the **p95** the clause actually names | `p1-first-reply-p95` — [manual](#the-2-vcpu-reference-run). CI asserts the floor and the median; a p95 needs a 2 vCPU box and more samples than a unit test should take. |
| ✅ | Zero duplicate sends across 1k forced worker retries | `p1-no-duplicate-sends` — `apps/messaging/tests/test_services.py::TestIdempotency` |
| ✅ | 50 parallel webhooks never interleave steps | `p1-no-interleaving` — `apps/flows/tests/test_locking.py::TestOneStepPerContact` |
| ✅ | IG private-reply constraints | `p1-ig-private-reply` — three homes; see `criteria.py` |
| ✅ | Loop flow halts at 30 blocks with admin notification | `p1-loop-cap` — `apps/flows/tests/test_loop_cap.py::TestLoopCap` |

**Phase 1 is accepted in CI, except the p95 figure itself**, which needs the
reference run below. CI proves the reply path meets the budget and stays inside
the request; it does not produce the statistic the spec names, and the table
says so rather than letting a green tick imply it.

## Phase 2

| | Criterion | Verified by |
|---|---|---|
| ✅ | Broadcast mechanics: token buckets, out-of-window skips, correct counts, clean cancellation | `p2-broadcast-scale` — `apps/broadcasts/tests/test_acceptance.py`, at 600 contacts (one full chunk plus a partial) |
| 📋 | …at the **10k** scale the clause names | `p2-broadcast-ten-thousand` — [manual](#the-10k-contact-broadcast-run). A regression capping fanout near a thousand would not fail the CI row. |
| 📋 | WhatsApp template submit → approved → send **against a real WABA** | `p2-whatsapp-template-waba` — [manual](#whatsapp-template-round-trip-against-a-real-waba) |
| ✅ | STOP suppresses within one inbound event | `p2-sms-stop` — `apps/channels/tests/test_sms_compliance.py::TestStop` |
| ✅ | Unsubscribe link suppresses email within one click | `p2-email-unsubscribe` — [`test_unsubscribe_round_trip.py`](test_unsubscribe_round_trip.py) |

**Phase 2 is accepted in CI except two figures**: the WABA round trip, which no
test infrastructure can automate because it needs Meta credentials and a real
template review, and the 10k broadcast scale, which is a documented run rather
than ninety-plus seconds on every pull request. Both have runbooks below.

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

## Why the latency tests assert on a floor and a median

This repo has been bitten three times by a test asserting on a clock-derived
value — PRs #46, #49 and #62 — and the lessons compound: pin the clock at its
single read site, freeze it rather than repositioning it, and where you can,
assert the invariant instead of the timestamp. A suite that goes red at random
gets ignored, which is worse than not having one.

SPEC §21 asks for a **p95 on a 2 vCPU box**. That is a production SLO, and a p95
over a handful of samples on a shared CI runner is mostly a measurement of the
runner. So the budget is verified in layers rather than by one number:

* **The invariant, always.** [`test_first_reply.py`](test_first_reply.py) asserts
  the inline routing budget fits inside the spec's ceiling, that the read timeout
  is the one SPEC §7.1 names, and that a connection which overruns stays flagged
  longer than the budget that flagged it. No clock is involved, and this is what
  catches the change that makes the criterion unmeetable — raising
  `INLINE_BUDGET_SECONDS` past two seconds — on any machine, however loaded.
  Note what it does *not* claim: the HTTP timeouts do not bound the call's total
  wall clock, and [the gap below](#known-gap-the-http-timeouts-compose) says so.
* **The structure, always.** The latency test also asserts every delivery was
  answered *inside the request*. A reply handed to the worker is precisely what
  makes a first reply slow, and `InlineDecision` makes that visible without a
  stopwatch.
* **The wall clock, on two statistics.** `min(timings)` is the floor — the
  closest observable to what the code path costs — and catches a regression that
  slows every reply. `median(timings)` catches the one a minimum cannot see: a
  run like `[0.1, 3, 3, 3, 3]` has a fast minimum and is plainly broken. Both are
  asserted against the spec's own unmodified ceiling.
* **`p95` and `max` are reported, not asserted**, and that is arithmetic rather
  than squeamishness. At these sample counts the nearest-rank p95 *is* the
  maximum, and the maximum is the single statistic that measures the runner
  instead of the code. `TestTheStatisticsCatchWhatTheyClaimTo` pins all of this,
  so the discrimination is a test rather than a claim in a README.

### What this still cannot survive

A machine that cannot deliver a webhook inside the 1.5 s inline budget at all.
Every delivery is then handed to the worker, none of them is a first reply, and
there is nothing to take a statistic over — so the test fails, and it fails for
the environment rather than for the code.

Observed twice while writing this, on a laptop running four other test suites:
single deliveries at 2.49 s and 6.12 s against a quiet-machine cost of
0.05-0.13 s. That is two orders of magnitude, so the failure message says to
check the load and re-run the file alone before believing the reading.

This is the honest limit of a wall-clock assertion and it is not fixable by
choosing a better statistic: when no sample is good, no statistic helps. What
was fixed is everything upstream of it — the breaker is cleared per sample so
one slow delivery no longer invalidates the rest, and the inline requirement is
one sample rather than a majority so a run that meets SPEC §21's 2 s ceiling is
not failed by the stricter 1.5 s budget that governs deferral.

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

**No p95 has been recorded yet.** The table above is CI-scale sampling on a
laptop, not the statistic §21 asks for; `p1-first-reply-p95` stays 📋 until
somebody runs the procedure below and fills a number in.

---

## Known gap: the HTTP timeouts compose

`request_json` builds `httpx.Timeout(READ_TIMEOUT, connect=CONNECT_TIMEOUT)`, and
httpx applies those to **separate phases** — there is no total deadline. A
platform that stalls the handshake and then stalls the read can hold a single
inline call for `CONNECT_TIMEOUT + READ_TIMEOUT` = 4 s, which is more than §21's
2 s ceiling for the whole reply.

This is survivable, and it is why the criterion is a p95 rather than a maximum:
the first overrun flags the connection, and every later event on it enqueues
before doing any I/O. So the *tail* is bounded by the breaker, not by the
timeout.

It is recorded here rather than fixed because a total deadline is a change to
the outbound path's error policy, which belongs to whoever owns that policy, not
to a test suite. `test_first_reply.py` asserts what is true today — the read
timeout matches SPEC §7.1's number, the inline path is bounded far below the
background one, and the breaker outlives the budget — and deliberately does not
pin the 4 s composition in either direction, so adding a total deadline later
will not turn this suite red.

---

## Manual runbooks

Four criteria cannot be verified by a per-PR test, and saying so plainly is more
useful than a test that pretends otherwise.

### The 2 vCPU reference run

**Why it is manual.** The clause says "on a 2 vCPU box", and no CI runner here is
one — the pytest job runs four xdist workers on a shared runner. A p95 also needs
more samples than a unit test should spend.

**Steps.**

1. Deploy a build to a 2 vCPU instance following
   [`docs/self-hosting.md`](../../docs/self-hosting.md) — a real deployment, not
   a laptop, because the criterion is about a deployment.
2. Point a load generator at the webhook endpoint with 100 concurrent inbound
   events, each matching a keyword trigger on a published single-send flow.
3. Record the p95 of the interval between the delivery and the outbound call,
   and put it in the table above with the date and the instance type.

**What counts as a failure.** A p95 at or above 2 s, or a run where a meaningful
share of events were handed to the queue rather than answered inline — the
second makes the first look better than it is.

Run it when the reply path changes shape: a new stage in the pipeline, a
different locking strategy, a change to the inline budget. It is not a
per-release chore.

### The 10k-contact broadcast run

**Why it is manual.** `apps/broadcasts/tests/test_acceptance.py` runs 600
contacts — one full 500-row chunk plus a partial, which is what exercises the
chunking arithmetic. A third chunk adds about ninety seconds to every CI run, and
10k adds far more, for assertions the 600-contact run already makes. But 600 does
not prove the *scale*: a regression capping fanout near a thousand, or failing to
schedule successors past the second chunk, would leave the CI row green.

**Steps.**

1. Seed a workspace with 10,000 contacts carrying a mix of in-window and
   out-of-window identities, and a known count of each.
2. Send a broadcast to all of them and let the fanout run to completion.
3. Reconcile: messages actually sent plus skipped equals the audience, the
   skipped count matches the out-of-window count exactly, and the send rate over
   the run stays within the connection's token bucket.
4. Repeat once, cancelling mid-fanout, and confirm nothing is scheduled or sent
   after the cancellation and no message is left `queued` with nothing to move it.
5. Record the audience size, the counts and the elapsed time here.

**What counts as a failure.** A total that does not reconcile, a skipped count
that disagrees with the eligibility filter, a send rate above the bucket, or a
message stuck in `queued` after cancellation.

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

**Steps.** Against a deployment built from
[`docs/self-hosting.md`](../../docs/self-hosting.md), whose `docker-compose.prod.yml`
runs `app`, `worker`, `postgres` and `caddy` as separate services — which is what
makes "kill the worker" something you can actually do.

1. Start a broadcast to a few thousand contacts and let the fanout begin.
2. `docker compose -f docker-compose.prod.yml kill worker` (or the platform's
   equivalent) partway through.
3. Restart it. The broadcast must resume: no contact messaged twice, no contact
   skipped, and the counters reconciling against the audience.
4. Repeat, killing `postgres` instead. On restart, actions left claimed by the
   dead worker must be swept back to pending by the zombie recovery job and then
   run, rather than sitting claimed forever.
5. Record the audience size, the counts before and after, and the recovery time.

**What counts as a failure.** A duplicate send, a contact never reached, a
counter that disagrees with the messages actually sent, or an action stuck in a
claimed state after the sweep has run.
