# Case study: Callvault

*A call-recording pipeline for a failing world.*

## The problem

A call center records its calls. The business wants two things out of them: customers who asked
for a meeting should get a booking, and customers who complained should have a complaint filed
in the CRM. The system also has to serve several companies at once, and the audio contains
people's names, phone numbers, IBANs and national ID numbers.

That sounds like a straightforward pipeline until you look at what it depends on. The
speech-to-text service rate-limits and fails. The CRM answers 429, 500, stalls for eight seconds,
and sometimes commits a write and then drops the connection before replying. Workers get
killed. And the requirement is blunt: **no recording may be lost or silently skipped, and no
customer may be booked twice.**

## Why it matters

Each of those failures is cheap in isolation and expensive in combination:

- A lost call is a customer whose complaint never reached anyone.
- A duplicate booking double-books a salesperson and tells the customer the company is careless.
- One missed `WHERE tenant_id = ?` leaks a customer's call to a competitor.
- A transcript that keeps an IBAN in clear text is a data-protection incident waiting for a
  copy-paste.

## The approach

I treated the three hard guarantees as database problems rather than application problems,
because the database is the one component that stays correct when everything else crashes.

**1. State lives in PostgreSQL, and every step is one transaction.** The queue is a table, claimed
with `FOR UPDATE SKIP LOCKED`, with a lease, a heartbeat and a fencing token. A step writes its
output and the job's next state in the same transaction, so a crash between steps leaves a
well-defined state. A worker that is killed simply stops heartbeating, its lease expires, and
another worker takes the job.

**2. Idempotency is designed, not hoped for.** Before sending anything to the CRM, the worker
writes an outbox row whose idempotency key is derived from tenant, call and slot. Before every
new attempt it asks the CRM what it already holds for that call, and adopts an existing booking
instead of creating a second. A new key is used only after an explicit `409` conflict. Retries
resend the same key and the same body.

**3. Isolation is enforced where the data is.** `FORCE ROW LEVEL SECURITY` on every table, with a
tenant context that is mandatory: a query without it is an SQL error, not an empty result. Three
database roles, none with `BYPASSRLS` or `DELETE`. The tenant is derived from a hashed API key. The
raw transcript is AES-256-GCM encrypted with the tenant and recording as associated data, and the
tenant-facing role has no grant on that column at all.

**4. Personal data is masked before anything leaves.** Turkish needs its own rules: spoken numbers
("sıfır beş yüz otuz iki...") have to become digits before a checksum can be applied, and a
checksum is what separates an ID number from eleven random digits. TCKN and IBAN are validated
(mod-97 for IBAN), cards by Luhn. An invalid number is deliberately *not* masked, because masking
everything that looks like a number destroys the transcript's value.

## Decisions worth defending

| Decision | Chosen | Rejected | Why |
|---|---|---|---|
| Queue | PostgreSQL table | Celery/Redis, a broker | Job state and business state must commit together. A broker adds a second source of truth that can disagree after a crash. |
| Tenancy | Shared tables + RLS | Schema or database per tenant | Fewer moving parts to migrate; the guarantee is checked by the database on every query. Cost: noisy neighbours share a pool. |
| Action extraction | Deterministic rules | An LLM | Only masked text reaches it, results must be reproducible, and no third party sees the call. Cost: lower recall on unusual phrasing. |
| Name masking | Lists + context rules | Mask every capitalised word | Masking every capitalised word destroys company and day names. Cost: a name with no context can pass (documented). |
| Duplicate audio | SHA-256 per tenant | Treat every id as a new call | Byte-identical audio is the same call; processing it twice would double-book the same person. |

Every one of these has a full record in [`DECISIONS.md`](DECISIONS.md) with the alternatives and
the conditions under which I would change my mind.

## What went wrong, and what it taught me

The first full run on real call audio found problems the unit tests had not:

- **Spoken e-mail addresses were not masked.** Whisper writes them without an `@` in several
  shapes, and the first rule only knew the written form. Fixed with new rules, then the stored
  data was repaired by a new `remask` command, which is how the command came to exist.
- **"Yüz yüze" (face to face) became 100 100'e.** The number normaliser read the idiom as
  numbers. It now keeps idioms and the pronoun forms of "o" as words.
- **Whisper invented subtitle text on silence** ("Altyazı M.K."). Those strings are removed from
  the derived text; the encrypted raw transcript keeps them as evidence.
- **A 429 consumed a retry attempt** that should have been spent waiting. A rate limit now waits
  for `Retry-After` and gives the attempt back.
- **A leaked e-mail reached the CRM once**, before the fix, and the CRM offered no way to update
  a record. That is why the design sends only masked text, and why the README documents the
  remediation path for a real CRM instead of pretending the problem does not exist.

While preparing this repository for publication I also found that running the live demo twice
in a row classified the whole second run as duplicates (the audio hashes matched the previous
run's database). The demo now starts from an empty database each time.

## Result

- 387 automated tests, including 43 penetration tests, a real worker `SIGKILL`, and a pipeline
  run against a CRM simulator that injects 429s, 500s, stalls and lost replies.
- On a local set of 21 Turkish call recordings (not included in the repository) the
  first full run transcribed all 19 valid recordings, rejected one truncated file, recognised one
  byte-identical copy, and created 11 bookings and 7 complaints in the CRM with 0 duplicates.
- A one-command demo that kills a worker mid-job under CRM chaos and verifies the outcome against
  the CRM itself.

What I did **not** measure: throughput, latency under load, or transcription accuracy. No numbers
for those are claimed anywhere in this repository.

## What I would do next

1. A Turkish NER model as a second masking layer, evaluated against the rule-based layer on a
   labelled set (the current gap, ADR-36).
2. A thin HTTP API in front of the tenant read side. The CLI proves the model; an API is what a
   customer would integrate.
3. Deterministic slot assignment when two calls want the same slot, by call time rather than
   worker timing.
4. The CRM correction path (`PATCH` with a derived idempotency key) once there is a CRM that
   accepts updates.
5. A load test, so that the claim "moderate volume" can be replaced by a number.

## Skills this project shows

Distributed-systems failure handling (leases, fencing, idempotency, outbox), PostgreSQL security
(row-level security, roles, column grants), applied cryptography (AEAD with associated data),
privacy engineering for a morphologically rich language, adversarial testing, test design for
nondeterministic failures, and writing decisions down so someone else can challenge them.
