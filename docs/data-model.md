# Data model

Three schemas, owned by three different parties.

| Schema | Owner | keystone's access |
| --- | --- | --- |
| `legacy` | Arcadia CRM (simulated) | **read-only** |
| `migration` | keystone | read/write |
| Atlas Cloud | the target CRM (simulated) | write through its API only |

---

## 1. `legacy` -- the source, as it is

This schema is deliberately bad, and the difficulty of the migration **is**
this schema. Everything below is modelled on what actually turns up in a
twenty-year-old CRM.

| Table | Rows | What it holds |
| --- | ---: | --- |
| `cust` | 1,284 | Companies |
| `person` | 4,000 | People |
| `deal` | 2,600 | Opportunities |
| `act` | 9,000 | Calls, meetings, emails, notes |
| `usr` | 19 | Users of the old system |
| `ref_stage` / `ref_industry` | 7 / 8 | Code tables, deliberately incomplete |

### What is wrong with it, on purpose

Generated deterministically from `KEYSTONE_RANDOM_SEED`, counted, and written
to the report directory so the dry-run's findings can be checked against the
truth. A data-quality report nobody can verify is a report nobody should trust.

| Defect | Records | Why it is realistic |
| --- | ---: | --- |
| `contact.case_or_whitespace` | 734 | ALL CAPS, lower case, doubled spaces |
| `activity.mojibake` | 458 | A Latin-1 export read as UTF-8: `SociÃ©tÃ©` |
| `deal.amount_or_date_format` | 449 | `12 500,00`, `$ 12,500.00`, `24-07-19` |
| `contact.unusable_email` | 415 | `N/A`, `jean(at)example`, two addresses in one field |
| `activity.soft_deleted` | 272 | `del_flag = 'Y'`, which half the old reports ignored |
| `contact.soft_deleted` | 177 | idem |
| `deal.unregistered_stage_code` | 116 | Codes users typed that were never in `ref_stage` |
| `account.duplicate_spelling` | 84 | The same company as `Argos SA`, `ARGOS S.A.`, `Argos SA (ex-Argos)` |
| `account.case_or_whitespace` | 60 | idem |
| `account.unregistered_industry_code` | 60 | idem |
| `account.country_unmappable` | 53 | `Frnace`, `F`, empty, `  FR` |
| `deal.soft_deleted` | 52 | idem |
| `contact.orphan_account_reference` | 45 | Points at a company that does not exist |
| `deal.unparseable_amount` | 34 | `n/c`, `à définir`, `-` |
| `account.soft_deleted` | 32 | idem |
| `account.mojibake` | 22 | idem |
| **total** | **3,063** | |

Two structural properties matter as much as the list:

- **Every column is text**, including amounts, dates and flags. There are no
  constraints, so nothing was ever prevented.
- **There are no foreign keys**, so orphans exist and the migration is the
  first thing that has ever checked.

### The activity export

Activities are *not* read from the database. Arcadia archived its activity log
to files, and those are what keystone reads: `ACT_EXPORT_*.csv`,
semicolon-separated, encoded in **cp1252**, with an uppercase header.

That is what the export job produced and what the sales team opens in Excel. A
migration tool that assumes UTF-8 and commas fails on the first accented
subject line, which is roughly row four.

---

## 2. `migration` -- keystone's own memory

Five tables, each answering a question that cannot be answered from the source
or the target alone.

### `crosswalk` -- the one that cannot be lost

```sql
PRIMARY KEY (entity, source_id)
UNIQUE      (entity, target_id)
```

| Column | Why |
| --- | --- |
| `source_id` → `target_id` | Which legacy record became which target record |
| `content_hash` | SHA-256 of the **mapped payload**, canonical JSON |
| `load_count`, `last_run_id` | How many times it has been written, and by which run |

`content_hash` is what turns two outcomes into three:

| Situation | Action |
| --- | --- |
| No crosswalk row | `create` |
| Row exists, hash differs | `update` |
| Row exists, hash matches | `skip` -- nothing is sent at all |

The hash is computed **after** mapping, not on the source row. Two source rows
differing only in whitespace, date format or country spelling produce the same
target payload, the same hash, and therefore no write. A re-run touches only
what genuinely changed, and an integration test asserts exactly that.

The reverse index exists because "this record in the target looks wrong --
where did it come from?" is the first question anyone asks after go-live.

### `reject` -- what did not make it, and why

```sql
PRIMARY KEY (entity, source_id)
```

One row per record, not one per attempt: a permanently broken record would
otherwise fill the table with copies of the same problem. `attempts` counts,
and `status` ends it (`PENDING` → `ABANDONED` after
`KEYSTONE_MAX_RETRY_ATTEMPTS`).

`stage` (`extract` / `map` / `validate` / `load`), `field` and `rule` are what
make the report actionable. "533 records failed" is a number; "79 opportunities
have a stage code that is not in the lookup, for example `PEND`" is a task.

### `run`, `impact`, `profile`

`run` holds one row per operation, written on the way in so that a process
killed halfway leaves a `RUNNING` row with a start time rather than no trace at
all. `impact` holds what a dry-run projected -- and is what the load's gate
reads. `profile` holds the source profile per run, so two dry-runs can be
compared.

---

## 3. Atlas Cloud -- the target's contract

Published by the target itself at `/api/v1/$metadata`, rather than documented
in a PDF that drifts:

| Entity set | Required fields | References |
| --- | --- | --- |
| `accounts` | `arcadia_id`, `name`, `country_code`, `status`, `source_system` | — |
| `contacts` | `arcadia_id`, `account_id`, `last_name`, `source_system` | `accounts` |
| `opportunities` | `arcadia_id`, `account_id`, `name`, `stage`, `source_system` | `accounts`, `contacts` |
| `activities` | `arcadia_id`, `account_id`, `activity_type`, `subject`, `activity_date`, `source_system` | `accounts`, `contacts`, `opportunities` |

What it enforces, and what each rule costs a careless client:

| Rule | Code | Consequence of ignoring it |
| --- | --- | --- |
| Upsert on the alternate key | — | Every re-run duplicates every record |
| Unknown fields are refused | `UNKNOWN_FIELD` | A silently dropped column, discovered in a sales meeting |
| References must exist | `REFERENCE_NOT_FOUND` | Orphans in the new system |
| Enum values are checked | `INVALID_VALUE` | A pipeline report with invented stages |
| Lengths are checked | `VALUE_TOO_LONG` | Truncation nobody decided on |
| Batches ≤ 250 | `413` | A load that fails at a size the client never tested |
| Rate limits, outages | `429` + `Retry-After`, `503` | A client that gets blocked instead of throttled |

CI checks, as its own job, that every mapped target field exists in this
contract and that every required target field is mapped. That is the check
which catches a mapping drifting away from the system it writes to -- the
failure that otherwise surfaces as thousands of `UNKNOWN_FIELD` rejections
halfway through a load.

---

## Volumes, measured

| | |
| --- | --- |
| Source rows | 16,903 (+ 3 export files) |
| Records read by the migration | 16,348 (after soft-delete filters) |
| Migrated | 15,815 |
| Rejected | 533 (3.26%) |
| `legacy` on disk | 3.5 MB |
| `migration` on disk | 6.2 MB |

The migration's own state is larger than the data it migrated. That is the
correct shape: a crosswalk row carries two identifiers and a 64-character hash
for every record that has ever moved, and a reject row carries the whole
payload that failed.
