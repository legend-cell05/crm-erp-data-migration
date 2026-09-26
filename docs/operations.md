# Operations

## The routine

| Task | Command |
| --- | --- |
| Check everything is reachable | `keystone doctor` |
| Generate the simulated source | `keystone seed` |
| Create the schemas | `keystone init-db` |
| See what a migration would do | `keystone dry-run` |
| Ask whether a load is allowed | `keystone gate` |
| Migrate | `keystone load` |
| Everything, in order | `keystone migrate` |
| Prove it worked | `keystone reconcile` |
| See what did not migrate | `keystone rejects --show-examples` |
| Trace one record | `keystone crosswalk --entity account --source-id C-000001` |
| Run history | `keystone runs` |

Exit codes: **0** success · **1** handled failure · **2** misuse ·
**3** blocked by a gate. The last is separate so a scheduler can tell a refusal
from a crash without parsing output.

## First run, from nothing

```bash
cp .env.example .env
keystone doctor            # configuration and connectivity
keystone seed              # the simulated Arcadia CRM
keystone serve &           # the simulated Atlas Cloud, on :8080
keystone dry-run           # read the report before going further
keystone load
keystone reconcile
```

or `docker compose up --build`, which does the same thing in three containers.

## Diagnostics

### `keystone load` exits 3

Not a failure. The gate refused, and the message says which condition:

```
BLOCKED no dry-run has been performed for mapping version 1.3.0+b54beeedcf36
BLOCKED the dry-run projects a 7.80% rejection rate, above the 5.00% ceiling
```

**If it is the first:** either no dry-run was run, or a mapping file or lookup
table has been edited since. Editing changes the fingerprint, which is the
point. Run `keystone dry-run` again.

**If it is the second:** look at the causes before touching the ceiling.

```bash
keystone dry-run
head -60 data/reports/dry_run_latest.md
```

The rejection table is ordered by record count. Ask whether the top row is a
data problem (fix the source), a rules problem (fix the lookup), or a genuine
change of scope (raise the ceiling deliberately, in `.env`, with a comment).

### The rejection rate jumped between two runs

Compare the two reports; they are kept per run.

```bash
ls -t data/reports/dry_run_*.md | head -2
diff <(sed -n '/Why records/,/^$/p' data/reports/dry_run_A.md) \
     <(sed -n '/Why records/,/^$/p' data/reports/dry_run_B.md)
```

The usual causes, in order of likelihood:

1. **A cascade.** A handful of extra account rejections costs many times their
   number in contacts, opportunities and activities. Check whether the account
   row moved first.
2. **A new code in the source.** Somebody added a sales stage last week.
   `keystone profile --entity opportunity` shows the distinct values.
3. **A mapping change.** `git diff mappings/` is the whole answer.

### A load ended `PARTIAL`

Expected whenever anything was rejected -- and deliberately distinct from
`FAILED`. A load in which the target refused 12 records out of 16 000 did work,
and reporting it as a failure teaches everyone to ignore the status.

```bash
keystone rejects --show-examples
```

`stage` says where it went wrong: `map` and `validate` are keystone's rules,
`load` is the target refusing it. A `load`-stage rejection means the mapping
and the target's contract disagree, which is a bug on this side.

### The target refuses records with `UNKNOWN_FIELD`

A mapping produces a field the target does not have. This should never reach
production, because CI checks every mapped field against the published contract
on every push:

```bash
curl -s localhost:8080/api/v1/\$metadata | python -m json.tool | head -40
```

### `RetryBudgetExhausted`

The target was unavailable for longer than the retry budget. The run failed;
nothing is half-written, because the crosswalk is written per batch and
whatever was written is recorded. Re-running continues where it stopped.

If it happens repeatedly, the budget is too small for the target's actual
behaviour rather than the target being broken:

```
KEYSTONE_RETRY_MAX_ATTEMPTS=8
KEYSTONE_RETRY_MAX_DELAY_SECONDS=60
```

### A run is stuck in `RUNNING`

The process was killed between its start and its end. The data is consistent --
the crosswalk is written per batch -- but the row stays open.

```sql
UPDATE migration.run SET status = 'FAILED', finished_at = now(),
       notes = 'process killed; closed by hand'
WHERE status = 'RUNNING' AND started_at < now() - INTERVAL '1 hour';
```

### Reconciliation reports a mismatch

`crosswalk_matches_target` failing means the crosswalk claims records the
target does not have. Two causes, and they need opposite responses:

- **The target was reset and the crosswalk was not.** The crosswalk is now
  lying; the next load would skip everything and migrate nothing. Clear the
  migration state (`keystone reset --keep-legacy`) and start again.
- **Records were deleted in the target by someone.** The crosswalk is right and
  the target is missing data. Do not clear anything; find out who.

The reconciliation report says which entity, which is where to start.

## Monitoring

Nothing here is deployed, so this is what *would* be watched rather than what
is. The signals that matter are not "the job failed":

| Signal | Source | Why |
| --- | --- | --- |
| Rejection rate per run | `migration.run` | A migration can succeed while losing 8% of the records |
| `ABANDONED` rejections | `migration.reject` | Records nobody is going to come back to |
| Gate refusals | exit code 3 | Somebody is trying to load without a plan |
| Crosswalk growth vs target count | `keystone reconcile` | The two must move together |
| Retries per batch | run logs | A rising rate means the target is degrading before it fails |

```yaml
# The alert worth having, if this ran on a schedule
- alert: MigrationRejectionRateJumped
  expr: keystone_reject_rate_pct > 5
  for: 1 run
  annotations:
    summary: "the rejection rate is above the ceiling the last dry-run approved"
```

Logs are JSON when `KEYSTONE_LOG_FORMAT=json`, one object per line, with
`entity`, `run_id` and the counts as real fields rather than interpolated into
a message -- so a run can be queried rather than grepped.

## Starting over

```bash
keystone reset --keep-legacy   # migration state only; the source survives
keystone reset                 # everything, including the simulated source
```

`--keep-legacy` exists because "run the migration again" and "throw away the
source system too" are different intentions, and a command that only offers the
second will eventually be run when the first was meant.

Both destroy the crosswalk, which is the one thing in this project that cannot
be rebuilt from anything else. In a real migration that command would not
exist.
