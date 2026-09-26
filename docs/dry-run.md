# The dry-run

## The problem it solves

A data migration has one property that makes it different from almost anything
else a developer writes: **you find out whether it worked after it is too late
to decide not to do it.** The target is the new system of record from Monday
morning; the old one is switched off; and the 412 records that quietly became
`NULL` are discovered in March.

So the question a migration tool has to answer is not "did it work?" but "what
would happen if I ran this?" -- asked and answered before anything is written.

## What makes a dry-run believable

Three properties, and the first is the one usually missing.

### 1. It runs the same code the load runs

`load/planner.py` is used by both. The dry-run is not a mode, a flag inside the
loader, or a parallel implementation: it is the same extraction, the same
mapping engine, the same crosswalk comparison, with the last stage not called.

A dry-run with its own logic predicts the behaviour of a program that does not
exist. That failure is silent and it is the reason nobody trusts dry-runs.

### 2. It cannot write

`run_dry_run` never constructs an `AtlasClient`. There is no code path from the
dry-run to the target, so a mistake in it cannot become a write. CI asserts the
consequence rather than the intention:

```python
# after `keystone dry-run`, before any load
assert stats["total"] == 0, "the dry-run wrote records to the target"
```

### 3. It projects forward

The subtlety that a naive implementation gets wrong, and the one worth
understanding.

On a first run the crosswalk is empty. A contact whose company is about to be
created *in the same run* has no crosswalk entry, so a plain resolver reports
"account C-000123 has not been migrated" -- and the dry-run predicts that every
contact, opportunity and activity will be rejected. The report is alarming,
useless, and wrong.

So during a dry-run, references resolve against two things: what the crosswalk
already holds, and what earlier entities in this run have decided to create.
Entities are planned in dependency order, so by the time contacts are planned,
the accounts are known. The projected ids are deliberately fake and obviously
so (`planned:account:C-000123`); nothing stores them, and their only job is to
let a reference resolve so the rest of the record can be evaluated.

## What the report contains

`data/reports/dry_run_latest.md`, regenerated on every run.

### What would happen

| Entity | Read | Create | Update | Skip | Reject | Reject rate |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| `account` | 1,249 | 1,222 | 0 | 0 | 27 | 2.16% |
| `contact` | 3,823 | 3,689 | 0 | 0 | 134 | 3.51% |
| `opportunity` | 2,548 | 2,369 | 0 | 0 | 179 | 7.03% |
| `activity` | 8,728 | 8,535 | 0 | 0 | 193 | 2.21% |
| **total** | **16,348** | | | | **533** | **3.26%** |

Four actions, not two. `skip` is what makes the second run's report read
`create: 0, skip: 15 815`, which is how a re-run proves it is a re-run.

### Why records would be rejected

Ordered by how many records each cause accounts for, because the fix for the
first row is usually worth more than the fix for all the others:

| Entity | Field | Rule | Records | Example |
| --- | --- | --- | ---: | --- |
| `activity` | `account_id` | `reference:account` | 193 | account 'C-000067' has not been migrated |
| `contact` | `account_id` | `reference:account` | 134 | account 'C-000789' has not been migrated |
| `opportunity` | `stage` | `lookup:deal_stage` | 79 | 'PEND' has no entry in lookup 'deal_stage' |
| `opportunity` | `account_id` | `reference:account` | 47 | account 'C-000115' has not been migrated |
| `opportunity` | `stage` | `required` | 27 | required field 'stage' is empty |
| `opportunity` | `amount` | `parse_amount` | 26 | no number in '-' |
| `account` | `country_code` | `required` | 15 | required field 'country_code' is empty |
| `account` | `country_code` | `lookup:country` | 12 | 'Frnace' has no entry in lookup 'country' |

Read that table from the bottom up and it tells a story the counts alone do
not: **27 rejected companies cost 374 other records.** Twelve of those
companies are one typo. Fixing a single lookup entry recovers more records than
fixing everything else on the list.

That cascade is invisible in the source system, invisible in a row count, and
obvious here. It is the entire argument for the dry-run existing.

### Source profile

Every column the mapping reads, as it actually is: row count, how empty, how
many distinct values, and examples. A high empty percentage on a required field
is a rejection waiting to happen, and seeing `ctry` with six distinct spellings
is what tells you the lookup needs to be forgiving.

## The gate

```
keystone load
  -> is there a dry-run for mapping version 1.3.0+b54beeedcf36?   no -> exit 3
  -> did it project a rejection rate below the ceiling?           no -> exit 3
  -> proceed
```

Two conditions, both read from the database rather than from something an
operator typed.

**The version includes a fingerprint of every mapping file and lookup table.**
Editing a mapping after its dry-run invalidates the approval automatically.
This is the condition that a version number cannot enforce, because bumping it
is a habit and forgetting to is normal.

**The ceiling is a decision, not a default.** `KEYSTONE_MAX_REJECT_RATE_PCT`
ships at 5%, and that number is not round for comfort: the first dry-run
against this dataset projected 3.26%, most of it the cascade above. The ceiling
sits above what is expected and below the level at which the migration would be
losing something structural.

`--force` exists. A controlled partial load is sometimes the right call --
migrate the 96% now, fix the rest by hand on Tuesday. What `--force` cannot be
is accidental: it is a named flag, it is logged as a warning, and the run
record says the gate was overridden.

Exit code **3** means "blocked by a gate", and it is deliberately distinct from
1. A blocked migration is not a crash, and a scheduler should be able to tell
the difference without parsing text.

## The loop this is meant to produce

```bash
keystone dry-run                 # 3.26%, gate open, but look at the causes
vim mappings/lookups/country.csv # add the spellings that are real
keystone dry-run                 # fewer rejections, and 14x that in the cascade
keystone load                    # now do it
keystone reconcile               # and prove it
```

Two or three iterations of that loop, before anyone touches the target, is what
the whole project is for.
