# Zorro v2 — staging only

The existing `main.py` is unchanged. The staging branch has been created for a safer upgrade.

An offline prototype has been prepared separately with `catalog_v2.py`, `import_v2.py`, `server_v2.py`, and 9 passing offline tests. Those source files are provided to the store owner as `zorro_consultant_v2_staging.zip`; they have **not yet been committed to this branch**.

The prototype imports into a separate PostgreSQL table `zorro_products_v2`, preserves Zorro image URLs and uses Best Time item attributes. This should be reviewed before connecting real database credentials or deploying a separate Render staging service.

Do not deploy this branch to production yet. Further work: code review and commit staging files, test actual PostgreSQL and Gemini integration, cover ambiguous customer requests, then staged deployment with owner approval.