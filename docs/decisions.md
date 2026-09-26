# Decisions

Architecture decision records: the choice, the reason, what it costs, and **the
condition under which it becomes the wrong choice** -- the last being the part
that makes an ADR worth writing.

Status: all `Accepted` unless noted.

---

## ADR-001 -- The mapping is data, not code

**Decision.** Field-by-field rules live in versioned YAML under `mappings/`,
interpreted by a closed registry of transformations and validation rules.

**Why.** A mapping is a business document: which country codes are acceptable,
whether a missing opt-out means yes or no, whether an over-long company name is
truncated or refused. In the average migration those decisions end up inside
Python, where the person who should be making them cannot read them, and where
a change is a deployment.

**Cost.** Two layers to debug instead of one, a schema to maintain, and a hard
ceiling on what a rule can express -- anything needing arithmetic across three
fields requires a new registered function, which is a code change with a test.

**Wrong when.** The transformations become so specific that every field needs
its own function. At that point the YAML is a worse way of writing Python, and
the honest move is to write Python.

**Rejected alternative.** An expression language in the YAML (`amount * 1.2 if
currency == 'EUR'`). It would remove the ceiling and remove the security
property with it: the whole point is that a mapping cannot execute anything.

---

## ADR-002 -- A closed registry, not arbitrary callables

**Decision.** A mapping names a transform; it cannot define one. `get_transform`
raises on an unknown name, and every name is resolved when the mapping loads.

**Why.** Two properties fall out. **Security**: a YAML file that could name any
importable callable is remote code execution with extra steps, and these files
are meant to be editable by people who must not be able to run code.
**Failure timing**: a mapping that mentions a function nobody wrote fails at
load, not on record 40 000 of a twenty-minute migration.

**Cost.** Adding a transformation is a pull request rather than a config
change.

**Wrong when.** Never, for this threat model. If mappings were ever generated
by a trusted build step rather than edited by hand, the argument would weaken.

---

## ADR-003 -- The fingerprint, not the version number, is what the gate compares

**Decision.** The mapping set's version is `<highest file version>+<sha256 of
every mapping file and lookup table>`. The dry-run gate matches on the whole
string.

**Why.** "We ran a dry-run for 1.3.0" is worthless if 1.3.0 was edited
afterwards, and bumping a version number is a habit that is normal to forget.
The fingerprint also covers the **lookup tables**, which a file version cannot:
adding a country changes what the migration does and changes no file version at
all.

**Cost.** The version string is ugly, and any edit -- including a comment --
closes the gate and forces another dry-run. That is the intended behaviour and
it is mildly annoying, which is the correct amount of annoying.

**Wrong when.** Mappings are generated rather than authored, and the generator
is itself under version control. Then the generator's commit is the identity.

---

## ADR-004 -- The dry-run shares the load's code path

**Decision.** `load/planner.py` is used by both. The dry-run is the same
pipeline with the final stage not called, not a separate implementation.

**Why.** A dry-run with its own logic predicts the behaviour of a program that
does not exist. The failure is silent, it appears only when the two drift, and
it is the reason experienced people do not trust dry-runs.

**Cost.** The planner cannot take shortcuts that would only be safe in one of
the two modes.

**Enforcement.** CI asserts the target is still empty after a dry-run, rather
than trusting that no client is constructed.

---

## ADR-005 -- The dry-run projects forward

**Decision.** During a dry-run, references resolve against the crosswalk **and**
against records that earlier entities in the same run have decided to create.

**Why.** Without it, the first dry-run reports that every contact, opportunity
and activity will be rejected, because their companies do not exist yet. The
report would be alarming, useless and wrong, and the feature would be switched
off within a day.

**Cost.** A second resolver implementation (`ProjectedCrosswalk`) and a set of
projected ids per entity in memory.

**Honest limitation.** The projection assumes every mapped record will be
accepted by the target. A record that maps cleanly and is then refused by a
server-side rule appears as a create in the report and as a rejection in the
load. The reconciliation catches it; the dry-run cannot.

---

## ADR-006 -- Four actions, not two, and a content hash to tell them apart

**Decision.** Every planned record is `create`, `update`, `skip` or `reject`.
The crosswalk stores a SHA-256 of the **mapped payload**, and a matching hash
means `skip` -- nothing is sent at all.

**Why.** Without the hash, a re-run rewrites every record. The target's audit
log fills with changes that changed nothing, and the handful of records that
genuinely moved become impossible to find. Hashing the mapped payload rather
than the source row means cosmetic source churn -- whitespace, a date re-typed,
a country spelled differently -- produces no write.

**Cost.** A hash per record, and a subtle trap: **changing a mapping changes
every hash**, so the first load after a mapping edit rewrites everything. That
is correct, and it is why the gate forces a dry-run first, where the report
shows 15 815 updates and somebody can ask whether that was intended.

**Wrong when.** The target has no meaningful audit log and writes are free. The
hash would then be optimisation rather than hygiene.

---

## ADR-007 -- Rejections are recorded per record, with a field and a rule

**Decision.** One row per rejected record in `migration.reject`, carrying the
stage, the field, the rule, the message and the whole source payload.
`attempts` counts; `ABANDONED` ends it.

**Why.** "533 records failed" is a number. "79 opportunities have a stage code
that is not in the lookup, for example `PEND`" is a conversation with the sales
operations team that ends in a one-line change. The difference between those
two reports is the `field` and `rule` columns.

**Cost.** The payload makes the table large -- larger than the data migrated,
in this project.

**Consequence.** Failing the whole batch on the first bad record was rejected
outright: one malformed record in sixteen thousand would block the other
15 999, at 3 a.m., and the migration would be run with the checks disabled
within a week.

---

## ADR-008 -- Load in dependency order, derived rather than declared

**Decision.** `depends_on` in each mapping; the order is a topological sort,
and a cycle is an error rather than an infinite loop.

**Why.** Atlas Cloud enforces referential integrity, so contacts cannot precede
accounts. Hard-coding the order in Python means adding an entity is a code
change in two places, and one of them will be forgotten.

**Cost.** A sort and a cycle check nobody will ever see fail.

**Detail.** `keystone load --entity contact` still respects the order: the
caller chooses the subset, never the sequence.

---

## ADR-009 -- The target is a separate HTTP service, with its faults on in CI

**Decision.** Atlas Cloud is a FastAPI application keystone talks to over HTTP,
with an API key, a batch limit of 250, injected 429s and 503s. Fault injection
stays **on** in CI.

**Why.** A migration demonstrated against a function call never exercises what
actually breaks: a batch limit lower than anyone guessed, a rate limiter that
expects to be obeyed, a partial failure inside a good batch, referential
integrity enforced on the other side. A retry policy that has never retried
anything in CI is a retry policy nobody has tested.

**Cost.** CI timing is non-deterministic and occasionally slower. The *outcome*
stays deterministic, because the retries succeed -- which is the property under
test.

**Wrong when.** Flakiness becomes indistinguishable from a real failure. The
seed exists so the fault pattern can be pinned if that day comes.

---

## ADR-010 -- Only transient failures are retried, with full jitter

**Decision.** Retry `TransientTargetError` only -- timeouts, connection resets,
408, 425, 429, 5xx. Delay is `Retry-After` when the server sends one, otherwise
`uniform(0, min(base · 2ⁿ, cap))`.

**Why.** A 400, a 401 or a 413 will be identical on the tenth attempt; retrying
them spends the budget the next genuine outage needs and delays the error
report. Full jitter -- a draw over the whole interval rather than its endpoint
-- is what stops every client that failed together from retrying together; it
is the variant AWS measured as best in *Exponential Backoff and Jitter* (2015).

**Testability.** `sleeper` and `rng` are injected, so the tests assert the
delay sequence against the formula without waiting for it.

**Specific consequence.** An oversized batch is a **413, not a 429**: waiting
will never make 300 records acceptable, and the client must fail immediately
rather than retry five times first.

---

## ADR-011 -- Upsert on an alternate key, with per-record batch results

**Decision.** The target upserts on `arcadia_id` and answers a batch with one
result per record and a 207 when any of them failed.

**Why.** This is how Dataverse and most real CRMs behave, and it forces the
client into the right shape: a migration that infers success from a status code
will report a clean run in which a quarter of the records were refused. The
loader has to read the array, and the tests assert that it does.

**Cost.** The client is more complicated than `response.raise_for_status()`.

**Detail worth keeping.** The target distinguishes `unchanged` from `updated`.
That is what lets a re-run *prove* it changed nothing, instead of asking
everyone to take its word for it -- and it is what the idempotency test
asserts.

---

## ADR-012 -- Soft-deleted records are excluded in the mapping, not in code

**Decision.** `filter: "COALESCE(del_flag, 'N') <> 'Y'"` in each source
mapping.

**Why.** Whether to migrate records the old system considered deleted is a
business decision, and it belongs where the business can see it. Arcadia's own
reports never filtered on `del_flag`, so the old dashboards counted them -- the
first thing anyone will ask after go-live is why the new totals are lower.

**Cost.** The filter is raw SQL from a file, which is the one place in this
codebase where SQL does not come from a literal. It is mitigated by the
validator described in ADR-013.

---

## ADR-013 -- The source filter is validated before it reaches the database

**Decision.** `SourceSpec` rejects a filter containing `;`, `--`, `/*`, `UNION`,
`INSERT`, `UPDATE`, `DELETE` or `DROP`, and schema names must match
`[a-z_][a-z0-9_]*`.

**Why.** Mapping files are repository content, reviewed like code -- but
"reviewed like code" is a process control, and a technical one should exist as
well. A filter is interpolated into a `WHERE` clause because a clause cannot be
a bound parameter.

**Cost.** A legitimate filter containing the word `update` in a column name
would be refused. No such column exists here, and the trade is worth it.

**Honest scope.** This is defence in depth, not a sandbox. Someone who can edit
`mappings/` can already change what the migration does to the data, which is a
larger problem than SQL injection. The validator exists so that an
*accidentally* dangerous filter cannot reach the database.

---

## ADR-014 -- Activities come from CSV, and the CSV is cp1252

**Decision.** One entity is read from `ACT_EXPORT_*.csv`, semicolon-separated,
encoded cp1252, with the encoding and delimiter declared in the mapping.

**Why.** Every migration has at least one source that is a file somebody
exports, and every one of those files is in the encoding the exporting system
used rather than the one the receiving code assumes. Making three of the four
entities relational and one a file is what a real migration looks like; making
them all relational would be a comfortable lie.

**Cost.** A second extractor, a second profiler, and `errors="replace"` --
which means one unreadable byte costs that value rather than the file. The
record still goes through validation, so a mangled required field is rejected
and counted rather than imported silently.

---

## ADR-015 -- Exit code 3 for a blocked migration

**Decision.** 0 success, 1 handled failure, 2 misuse, **3 blocked by a gate**.

**Why.** A blocked migration is not a crash. A scheduler, a CI job or an
operator needs to tell "you have not planned this yet" from "something broke",
and the only reliable way is an exit code -- parsing log text for the word
"blocked" is how that breaks silently six months later.

**Enforcement.** CI runs `keystone load` before any dry-run and asserts the
exit code is exactly 3. If the gate ever stopped working, that job fails.

---

## ADR-016 -- `init-db` may never destroy the source

**Decision.** The legacy schema file uses `CREATE TABLE IF NOT EXISTS` and
contains no `DROP`. Re-initialising creates what is missing and touches
nothing else.

**Why this is an ADR and not a commit.** It was wrong. The file began with
seven `DROP TABLE ... CASCADE` statements, which made `keystone init-db` --
documented as "safe to re-run" and run by `seed`, by the tests and by the
compose stack -- silently empty the source system. The failure was invisible
in every normal sequence, because `seed` always ran straight afterwards and
refilled the tables. It only surfaced when the commands were run in the other
order, from a fresh checkout:

```
keystone reset --keep-legacy    # keep the source
keystone init-db                # ... and destroy it
keystone dry-run                # 8 728 records read, 100% rejected
```

The activities still appeared, because they come from CSV files on disk rather
than from the database -- which is the detail that made the report confusing
rather than obviously wrong.

**The general rule it cost.** A command whose documentation says "safe to
re-run" needs a test that re-runs it and compares the counts, not a docstring
saying so. There is now one.

**Cost.** Changing a legacy column now requires a migration script rather than
a re-run. That is the correct trade, and it is what a real source system would
force anyway.

---

## ADR-017 -- Logging can never be what breaks a migration

**Decision.** A `Logger` subclass renames `extra` keys that collide with
`LogRecord`'s own attributes (`created` becomes `ctx_created`) instead of
letting `logging` raise.

**Why.** This is in the list because it happened: a load report naturally has a
field called `created`, which is also the timestamp every `LogRecord` carries,
and `logging` answers that with a `KeyError` at the log call -- in the middle
of a migration, after the batch was sent.

**Cost.** A log field is silently renamed, which is surprising the first time.
That is a much better surprise than a crash between a write and the crosswalk
entry that records it.
