# Schema reference
These files document historical schema evolution. Do not run every file alphabetically:
the historical numbering contains duplicates, and several scripts assume an existing database.
For a NEW community installation use python -m scripts.community_init init.
It creates the current ORM schema plus the durable stream claim table, FULLTEXT and sibling-order indexes.
It refuses any nonempty database and never upgrades or drops existing data.
Existing installations must review migrations individually against their schema and take backups.
