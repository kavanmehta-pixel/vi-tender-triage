# VI Tender Research — Weekly Cowork Task

Run this **Tuesday**, after John's CSVs are uploaded, so findings are in the app
before the Wednesday session with Philip.

The app never calls an AI API itself. All research happens here, in Cowork, and
is written back over HTTP. No API key lives on Railway.

---

## The prompt

Paste this into Cowork:

> Pull the research queue from
> `https://web-production-59089.up.railway.app/api/research-queue?limit=15`
>
> For each tender, use web search to establish:
> 1. **The real closing date.** Most rows have none — find it, or state that ICN
>    EOIs stay open and are drawn from a registered pool.
> 2. **The actual security scope.** Is there genuinely CCTV/surveillance work, or
>    was it inferred from the project type? Quote the published capability list
>    where one exists.
> 3. **Is it fixed or temporary infrastructure?** This is the decisive question.
>    Permanent gantry/pole/tunnel CCTV on mains power is a PASS however well it
>    scored. Construction-phase, relocatable or off-grid is a fit.
> 4. **How VI actually enters** — portal URL, whether registration is needed, the
>    proponent and any contractor appointments.
>
> Then POST the findings to
> `https://web-production-59089.up.railway.app/api/research` as:
>
> ```json
> {"results":[{
>   "project_hash":"<copy exactly from the queue>",
>   "research_status":"confirmed | not_relevant | needs_registration",
>   "verified_closing_date":"YYYY-MM-DD",
>   "verified_scope":"what the security scope actually is, with specifics",
>   "portal_url":"where to register or submit",
>   "contact_info":"proponent, contractor, how to enter",
>   "research_notes":"assessment and recommended action. If the score looks wrong, say so and why.",
>   "ai_summary":"3 sentences for Robin: what it is, why it scored what it did, what to do next"
> }]}
> ```
>
> Be willing to contradict the score. A tender scoring 80 that turns out to be
> permanent tunnel ITS is a PASS, and saying so is the most valuable output.

---

## Why it is built this way

`project_hash` is the join key. Copy it verbatim from the queue — the writeback
matches on it and silently counts anything else as `unmatched`.

Writeback only touches research fields. Triage decisions, comments and pipeline
status are never modified, so a research run can never overwrite the team's work.

A verified closing date also refreshes the open/closed state, which is how
long-expired tenders drop out of the active list.

`?unresearched=1` (the default) excludes anything already researched, so the
queue naturally drains week to week. Use `?unresearched=0` to re-research.

## What the first run found

Four tenders researched. Three confirmed as genuine fits — Donald Rare Earth,
Pottinger Energy Park, Sunny Corner Wind Farm, all ICN Gateway EOIs where the
published capability list names construction CCTV explicitly.

One correction: **Managed Motorway Crafers to Glen Osmond scored 80 GO but is a
PASS.** The CCTV is permanent gantry- and tunnel-mounted ITS on a powered freeway
corridor. The traffic moat (M6) fired without checking whether the infrastructure
was fixed. That is a scorer improvement worth making.

It also surfaced that Sunny Corner Wind Farm is in the database twice under two
hashes, both pointing at ICN project 17593 — one for the Duplicates tab.
