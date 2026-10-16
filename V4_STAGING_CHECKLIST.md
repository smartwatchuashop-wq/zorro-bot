# Zorro AI v4: staging verification and safe rollout

**Status: NOT FOR PRODUCTION.** Work only on `zorro-hybrid-search-v4`. The Render production service follows `main` and must not be changed before verification.

## 1. Prepare isolated test infrastructure
1. Duplicate the Render web service as a **new staging service**, selecting branch `zorro-hybrid-search-v4`. Do not redeploy or change the existing `fastapi-qie2` service.
2. Use a **separate PostgreSQL database and DATABASE_URL**. Never connect staging to the production PostgreSQL: `main.py` has a synchronization worker that WRITES catalog rows and availability.
3. Configure a Gemini key in the staging environment only. Do not commit secrets to GitHub.
4. Disable real customer notifications/callbacks in staging or route them only to a controlled test recipient.
5. Let the staging catalog sync from the public feed; verify `GET /api/catalog-status` reports a sensible product count and latest sync.

## 2. Automated CI
- Workflow `.github/workflows/v4-tests.yml` starts a disposable PostgreSQL 16 container and runs mocked conversation tests, real SQL/JSONB tests, API contract tests, and Node.js syntax checks for `zorro_widget_v4.html`.
- GitHub Actions test data is synthetic. CI does NOT validate the production PostgreSQL contents, actual Gemini quality, or any live Horoshop browser session.

## 3. Manual staging conversation test
POST `<STAGING_URL>/api/chat`, JSON:
```json
{"message":"Порадь чоловічий годинник до 2000 грн","history":[],"state":{}}
```
Retain the returned `state`, then POST:
```json
{"message":"А без шкіряного ремінця?","history":[{"role":"user","content":"Порадь чоловічий годинник до 2000 грн"}],"state":{"filters":{"max_price":2000,"audience":"men"},"product_ids":[]}}
```
Real flow must use the **actual first response state** rather than this example placeholder. Check that every recommendation is <= 2000 UAH, available, and that the non-leather claim is confirmed by the supplier's own attributes. Also test `Порівняй перший і третій`, `Розкажи детальніше про <exact shown model>`, questions about warranty, and an unavailable/unknown specification.

## 4. Widget
- `zorro_widget_v4.html` preserves the original Horoshop UI and callback form.
- Before testing, set `window.ZORRO_AI_API_BASE = '<STAGING_URL>';` in the page **before** the widget snippet.
- Test on an internal preview/staging page; **do not paste into live Horoshop yet**.
- Inspect browser Network: POST `/api/chat` must include `history` and `state`, and subsequent calls must carry the returned values.
- Test mobile dimensions, close/reopen, page navigation persistence, reset, callback failure, slow requests and requests containing HTML text.

## 5. Go/no-go
Do NOT merge to `main` until all are independently verified:
- No budget violations, no false material/function claims, no out-of-stock card sold as in-stock.
- Gemini keeps a multi-turn shopping context; only named selected cards are described/compared.
- Staging Render logs show no errors; the actual supplier feed is synchronized.
- Browser widget works on Horoshop preview for mobile and desktop.
- Callback forms cannot silently report success on failed delivery.

The user has expressly asked not to alter their live site before staging is validated.
