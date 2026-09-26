# The mapping language

This is the document to read if you only read one. Everything that happens to a
record on its way from Arcadia to Atlas Cloud is described in
`mappings/*.yaml`, and this page is its reference.

## Why the mapping is data

A field-by-field mapping is a **business document**. Which country codes are
acceptable, whether a missing opt-out flag means "yes" or "no", whether a
company whose name is too long should be truncated or refused -- none of those
are engineering decisions, and all of them end up in code in the average
migration project, where the person who should be deciding cannot read them.

So they live in YAML, and three things follow:

1. **`git diff` is the change record.** "Why did 412 more records reject this
   week?" is answered by a diff of a lookup table, not by an archaeology
   session in the commit history of a transformation module.
2. **The mapping cannot execute anything.** A file names a transform from a
   closed registry and passes declared arguments. There is no expression
   language, no `eval`, no import. Someone who must not be able to run code can
   still own the rules.
3. **The whole set has a fingerprint.** Every mapping file and every lookup
   table is hashed into one version string, and that is what the dry-run gate
   compares. Approving "version 1.3.0" is meaningless if 1.3.0 can be edited
   afterwards; approving `1.3.0+b54beeedcf36` is not.

## Anatomy of a mapping file

```yaml
version: "1.3.0"           # semantic, and owned by whoever changes the file
entity: account            # must match the file name
depends_on: []             # decides the migration order

source:
  kind: sql                # sql | csv
  schema: legacy
  table: cust
  key: custno              # the column that identifies a source record
  filter: "COALESCE(del_flag, 'N') <> 'Y'"

target:
  entity_set: accounts
  alternate_key: arcadia_id   # what the target upserts on

fields:
  - target: country_code
    source: ctry
    transform: [trim]
    lookup:
      table: country
      on_miss: reject
    required: true

validations:
  - rule: required_together
    fields: [postal_code, city]
    severity: reject         # reject | warn
```

A field is produced by four steps, always in this order:

```
source column -> transform chain -> lookup -> reference -> required check
```

Skipping a step is not possible and reordering it is not configurable, because
the order is what makes the result predictable: a lookup always sees trimmed,
normalised input, and a reference is always resolved on an identifier that has
already been cleaned.

## Transformations

Nineteen functions, listed by `keystone registry`. The ones that carry a real
decision:

| Transform | What it does | The decision inside it |
| --- | --- | --- |
| `parse_date` | Parses the five formats Arcadia allowed | `day_first`: `03/04/2021` is 3 April in the source and 4 March to a US reader. **Nothing in the data settles this** -- the mapping states it, so a reviewer can see the answer |
| `parse_amount` | `12 500,00`, `$ 12,500.00`, `€12 500,00 EUR` | When both separators appear, the **last one wins**. True for every sample here, and stated rather than discovered |
| `parse_percentage` | `70`, `70%`, `0.70` → `70` | A value at or below 1 written with a decimal point is a fraction |
| `fix_mojibake` | `SociÃ©tÃ©` → `Société` | Only attempted when the markers are present; correct text is returned untouched, because re-encoding it destroys the accents a second time |
| `normalise_phone` | Best-effort E.164 | `+33 (0)1 …` carries both a country code and a national trunk prefix. Keeping the `0` produces a number one digit too long that still dials -- the worst kind of wrong |
| `truncate` | Cuts to the target's width | `on_overflow: reject` exists, and `name` uses it: a company name silently losing its last eight characters is worse than a record someone has to look at |
| `map_boolean` | `Y`/`N`/`O`/`1` → boolean | `default` answers "what does NULL mean here?". For an opt-out flag, guessing wrong is a GDPR problem, not a data-quality one |
| `constant` | A fixed value | Used for `source_system: arcadia`, so every migrated record says where it came from |

Every transform accepts `None` and returns `None`, so a chain applied to an
empty optional field cannot crash -- there is a test that asserts this for the
whole registry, because the alternative is a bug that only appears on the rows
nobody thought about.

## Lookups

A lookup is a two-column CSV under `mappings/lookups/`:

```csv
source;target
FR;FR
France;FR
FRANCE;FR
Fr.;FR
Suisse;CH
```

Matching is **forgiving on the way in** -- trimmed, accent-free, upper-cased --
because the source holds `FR`, ` fr ` and `France` for the same country. It is
**unforgiving on a miss**, and that is the interesting half:

| `on_miss` | Behaviour | Used for |
| --- | --- | --- |
| `reject` | The record is parked with the value that failed | `country_code`, `stage` |
| `default` | A declared substitute | `industry` → `unclassified` |
| `null` | The field is emptied | `contact_role` |
| `passthrough` | The value is kept as-is | nothing here; it exists for codes the target validates itself |

`Frnace` is a typo. Guessing that it means France turns one bad record into a
country that quietly does not exist in the target's reporting; rejecting it
turns the same record into a line in a report that somebody can fix in ten
seconds. The choice between those two is per-field, declared, and visible.

## References

```yaml
  - target: account_id
    source: custno
    reference:
      entity: account
      on_missing: reject
```

A reference is resolved through the **crosswalk**: the legacy `custno` becomes
whatever id Atlas Cloud gave that company when it was migrated. This is also
the check that catches contacts pointing at companies that no longer exist.

`on_missing` is a business decision, and the mappings use both values
deliberately:

- a **contact** with no account is rejected -- it is unusable in the target,
  and inventing a placeholder company to hold it would invent data;
- an **opportunity** with no contact keeps going with `contact_id: null` --
  the deal belongs to the company, and the named person may simply have left.

### The cascade, and why the dry-run shows it

27 companies are rejected for an unmappable country. Those 27 take 374
contacts, opportunities and activities with them, because their references no
longer resolve. That is correct behaviour, it is invisible in the source, and
it is exactly what a migration discovers on the morning of the cutover if
nothing projected it in advance.

The dry-run projects it. References resolve against what earlier entities in
the same run have *decided to create*, so the report shows the cascade before
anyone has written anything -- and shows that fixing one lookup entry recovers
fourteen times its own number of records.

## Validations

Ten rules, applied after every field is mapped, because they need more than one
field. Two severities:

- **`reject`** parks the record;
- **`warn`** migrates it and counts the warning in the impact report.

`warn` is not a softer `reject`; it answers a different question. "This
opportunity is marked won with a probability of 70" is not a reason to lose the
opportunity -- it is a reason for someone in sales to look at 287 records
before the target becomes the system of record.

## Changing a mapping

```bash
# 1. Edit the file or the lookup table.
vim mappings/lookups/country.csv

# 2. The fingerprint has changed, so the gate is closed again.
keystone gate            # exit code 3

# 3. See what the change would do -- before doing it.
keystone dry-run

# 4. Compare against the previous report: data/reports/dry_run_*.md
keystone load
```

Step 2 is the one that is easy to underestimate. A mapping edited after its
approval is not the mapping that was approved, and the only reliable way to
enforce that is to make the approval refer to the content rather than to a
version number somebody remembered to bump.

## What this design does not do

- **No expression language.** A rule that needs arithmetic across three fields
  cannot be expressed; it needs a new registered function, which is a code
  change with a test. That is a deliberate floor on what a mapping can do.
- **No per-record overrides.** There is no "except for customer C-000412".
  Exceptions belong in the source data or in a rule, not in a mapping file that
  slowly becomes a list of special cases.
- **No automatic inference.** keystone will not guess that `ctry` maps to
  `country_code`. Someone writes it down, once.
