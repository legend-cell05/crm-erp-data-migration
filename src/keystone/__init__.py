"""keystone -- a declarative CRM data-migration toolkit.

Migrates a legacy on-premise CRM (Arcadia) into a Dataverse-style cloud CRM
(Atlas Cloud). Both systems are simulated and every record is synthetic.

The design rests on three ideas:

1. **The mapping is data, not code.** Field-by-field rules live in versioned
   YAML, so a business analyst can read them and a diff shows what changed
   between two migration attempts.
2. **A dry-run is mandatory.** Nothing may be written to the target before an
   impact report for the same mapping version says what would happen.
3. **Identity is persisted.** A crosswalk table records legacy id -> target
   id, which is what makes a re-run an update rather than a duplicate.
"""

__version__ = "1.0.0"
__all__ = ["__version__"]
