# Every custom field this app adds to other apps' DocTypes, keyed by DocType.
# Applied by install.ensure_custom_fields on install and on every migrate.
# Conventions: fieldnames start with `bol_`; bol identifiers are `read_only` and `no_copy`
# (and `unique` where they are a natural key).
CUSTOM_FIELDS: dict[str, list[dict]] = {}
