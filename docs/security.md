# Security

> Synthetic data throughout. No real company, person or opportunity exists in
> this repository, and nothing here has been deployed to a production
> environment.

This is a portfolio project, so the honest framing is: here are the controls
that **are** implemented and verifiable by reading the code, followed by the
ones that would be required before this migrated anybody's real CRM.

A data migration deserves more care than most tools, for a reason specific to
it: it reads an entire customer database, holds it in flight, and writes it
somewhere else. The blast radius of a mistake is the whole dataset.

---

## 1. Secrets

**No credential is in the repository.**

| Control | Where |
| --- | --- |
| `.env` is git-ignored; `.env.example` holds placeholders only | `.gitignore`, `.env.example` |
| The ignore rule is `.env` **and** `.env.*`, with a `!.env.example` exception | `.gitignore` |
| The database password and the target API key are Pydantic `SecretStr` | `config.py` |
| Logs and reports use `safe_dsn`, never `dsn` | `config.py`, `db/engine.py`, `cli.py` |
| Compose defaults are obviously-local placeholders | `docker-compose.yml` |

`SecretStr` is the control that does real work. A plain `str` leaks through any
`repr()` of the settings object -- which is exactly what an unhandled exception
prints, what a debugger shows, and what `logger.info("config=%s", settings)`
emits. `SecretStr.__repr__` returns `**********`, so the leak has to be
deliberate:

```python
class Settings(BaseSettings):
    db_password: SecretStr = SecretStr("change_me_local_only")
    target_api_key: SecretStr = SecretStr("local_dev_key_not_a_secret")

    @property
    def dsn(self) -> str:
        """SQLAlchemy URL including the password -- never log this."""

    @property
    def safe_dsn(self) -> str:
        """Connection string with the password masked -- safe to log."""
```

Two properties rather than one, so the safe form is the convenient one. The
`doctor` command prints `safe_dsn`, and so does every error message about the
database.

The CI workflow contains `POSTGRES_PASSWORD: ci_test_password` in plain text.
That is deliberate and commented in place: it belongs to a service container
created and destroyed with the job, reachable only from inside the runner, with
no counterpart anywhere else. Putting it in a GitHub secret would imply it
protects something.

---

## 2. SQL injection

Every **value** reaching the database is a bound parameter. There is no
f-string interpolation of user input into SQL anywhere in `src/`.

Identifiers are the harder half, because they cannot be bound, and this project
has three cases:

**Schema names** come from configuration and are validated on the way in:

```python
@field_validator("legacy_schema", "migration_schema")
# must match [a-z_][a-z0-9_]*
```

`KEYSTONE_LEGACY_SCHEMA='x; DROP SCHEMA migration CASCADE'` fails at process
start with a validation error, before a connection is opened. The SQL files use
`${LEGACY}` and `${MIGRATION}` placeholders substituted only after that
validation, and `extract/reader.py` maps a mapping file's logical schema name
onto the configured one through an allow-list rather than passing it through.

**Table and key names** in a mapping must match `[A-Za-z_][A-Za-z0-9_]*`,
enforced by `SourceSpec`.

**The source filter** is the genuinely interesting one, and it has its own
section below.

---

## 3. The mapping files as a trust boundary

Mapping files are the one place where this codebase executes something that did
not come from a Python literal: a `WHERE` clause, interpolated.

```python
_FORBIDDEN_IN_FILTER = re.compile(
    r"(;|--|/\*|\*/|\bUNION\b|\bINSERT\b|\bUPDATE\b|\bDELETE\b|\bDROP\b)", re.I
)
```

A filter containing a statement terminator, a comment marker or a
data-modifying keyword is refused when the mapping loads, with the file named.
There are tests for five specific injection shapes.

**The honest scope of that control.** It is defence in depth, not a sandbox.
Anyone who can edit `mappings/` can already change what the migration does to
the data -- silently mapping every company to the wrong owner is a worse
outcome than a dropped schema and needs no SQL at all. The validator exists so
that an *accidentally* dangerous filter cannot reach the database, and so that
a review has something mechanical backing it up. The real control on mapping
files is that they are repository content under code review.

What the mapping layer **cannot** do, by construction:

- call anything outside the transform registry -- there is no expression
  language, no `eval`, no import mechanism;
- reference a table outside the two configured schemas;
- write to the source system, which keystone only ever reads.

---

## 4. The target as untrusted input

keystone treats the target API as hostile, because in production it is merely
*unreliable*, which has the same consequences.

| Threat | Control |
| --- | --- |
| Malformed response | Every field read with `.get`, and per-record results parsed into a typed dataclass |
| An error page instead of JSON | `_detail()` falls back to truncated text rather than raising inside the error handler |
| Unbounded retries against a sick target | Retry budget, `Retry-After` obeyed, `RetryBudgetExhausted` ends the run |
| A 4xx retried forever | Transient/permanent split; 413 and 401 are never retried |
| A batch the target will always refuse | Batch size is configuration and capped at 1 000 by the settings validator |
| Credentials in flight | `X-API-Key` header, never in a URL or a query string, so it stays out of access logs |

And in the other direction, the simulated target defends itself the way a real
one does: an API key on every data endpoint, unknown fields refused rather than
dropped, enum and length validation, referential integrity, and a batch limit.
Those are tested from a client's point of view in `tests/unit/test_target_api.py`,
so that making the simulator more forgiving cannot quietly weaken the tests
above it.

---

## 5. The container

```dockerfile
RUN useradd --create-home --shell /bin/bash --uid 10001 keystone
USER keystone
```

- **Multi-stage build**: compilers, pip caches and build metadata stay in the
  builder stage.
- **Non-root, UID 10001.** CI asserts it rather than trusting the Dockerfile:
  `test "$(docker run --rm --entrypoint id …:ci -u)" != "0"`.
- **Slim base**, with only `postgresql-client` and `curl` added, apt lists
  removed in the same layer.
- **`.dockerignore`** excludes `.env`, `.git`, `data/`, caches and tests, so a
  local `.env` cannot be copied into an image by accident.
- **No secret in any layer**: `ENV` carries configuration, never credentials.
- **Healthcheck** is `keystone doctor`, which already exits non-zero when the
  database is unreachable.

---

## 6. Data protection

This is where a CRM migration differs from most projects, and the section is
here even though every record in this repository is invented.

**What a real run of this tool would be handling.** Names, job titles, direct
telephone numbers, email addresses, and notes written by salespeople about
individuals. That is personal data under the GDPR, and a migration touches all
of it at once.

What the design already does in the right direction:

- **`migration.reject` stores the payload of records that failed.** That is a
  copy of personal data, kept for as long as the row exists, and it has no
  retention policy here. In production it would need one, and the report would
  need to show the *cause* without the payload.
- **`do_not_email` has an explicit default.** `map_boolean` forces the mapping
  to answer "what does a NULL opt-out flag mean?" rather than guessing. Getting
  that wrong sends marketing email to people who asked not to receive it, which
  is a regulatory problem rather than a data-quality one. It is written down in
  `mappings/contact.yaml` so it can be argued about before the migration.
- **`source_system: arcadia` on every record**, so the target can always say
  where a piece of personal data came from -- the first question in any
  subject-access request.

What is missing, and would not be optional:

- a retention policy on `migration.reject` and on the dry-run reports, both of
  which contain personal data;
- pseudonymisation of the reject payloads in any report that leaves the team;
- a record of the lawful basis for the migration itself;
- deletion of the crosswalk and the reject store at the end of the project,
  which is exactly the data you are most tempted to keep.

---

## 7. What this does not do

Stated plainly, because a security section listing only what is present is a
marketing document.

| Gap | What production would require |
| --- | --- |
| Secrets in environment variables | A managed secret store with rotation and per-environment scoping |
| A single static API key | OAuth2 client credentials, or mTLS, with a short-lived token |
| One database role for everything | `keystone_read` on `legacy`, `keystone_write` on `migration`, and nothing else |
| No TLS enforced on the database connection | `sslmode=verify-full` |
| No audit of *who* ran a migration | `migration.run` records what ran and when, not which principal triggered it |
| No dependency or image scanning | `pip-audit` or Dependabot, plus Trivy on the image |
| No secret scanning | `gitleaks` as a pre-commit hook -- `.gitignore` is a convention, a hook is a control |
| Reject payloads unbounded | Retention, and a field-level redaction list |

None of these are hard to add. They are absent because adding them without an
environment to enforce them against produces configuration that has never been
exercised -- which reads as a control and behaves as decoration.

---

## 8. If you find something

This is a portfolio repository with no deployment and no users. Open an issue.
