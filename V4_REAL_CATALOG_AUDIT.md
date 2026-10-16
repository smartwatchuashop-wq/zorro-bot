# Zorro v4 catalog audit (read-only production observations)

Snapshot observed: **2026-10-16** via Render's **read-only** Postgres query capability; no credentials, customer data or production mutations.

Production PostgreSQL: `zorro_db` (existing production database); **NOT** a staging database.

- 1,035 total catalog products
- 752 marked available
- 1,035 rows with non-empty JSONB attributes
- Main product categories:
  - Чоловічі годинники: 560
  - Смарт годинники: 134
  - Жіночі годинники: 129
  - Ремінці: 119
  - Аксесуари: 51
  - Дитячі годинники: 24
  - Унісекс годинники: 15
  - Спортивні годинники: 2
  - Скриньки і коробочки: 1
- Watch strap value examples (source product attributes): `Сталевий`, `Каучуковий`, `Шкіряний`, `Тканевий`, `Нейлон`, `Стальной`, `Шкіра`.
- Mechanism data appears in `Тип механізму` (718 catalog products); sample values include `Quartz` and `Механічний`.
- `Можливість розмовляти` key occurs in 132 products, therefore **do not assume all smart watches can make calls**.

Engineering implications:
1. The assistant's recommendation SQL should exclude straps, accessories and boxes before ranking, even if their names contain "годинник". `search_v4.py` now requires `category` to contain "годинник" in the verified Zorro taxonomy.
2. Material filters should normalize confirmed Ukrainian/Russian synonyms without guessing when a material is missing or broader (generic metal ≠ steel).
3. Calling support must be confirmed by explicit positive supplier attributes; missing information is not confirmation.
4. The existing production data may change at any time due to the six-hour refresh worker. These counts are a snapshot, not a permanent guarantee.

## Hosting constraint

Render refused creation of a second **free** PostgreSQL with error: `cannot have more than one active free tier database`. Thus **no second database was created**. A paid database must not be created without the user's explicit budget approval.

Staging verification currently uses GitHub Actions' disposable isolated PostgreSQL 16 container, alongside mocked conversation, API, and widget tests. These CI tests are separate from production, involve no Render compute charges, and cannot substitute for real Gemini conversations on a dedicated staging service.

The production database's Render free-tier expiry is shown as **2026-11-11**. Plan a migration/upgrade before that deadline.

As of the latest successful GitHub Actions run, **69 tests passed**. Deployment on a new Render service remains pending because the separate persistent database is not available on the free plan.
