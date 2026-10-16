# Zorro AI v4 — staging integration

**Branch:** `zorro-hybrid-search-v4`. Production `main` is unchanged.

Files:
- `search_v4.py`: SQL hard-filtered retrieval, positive/negative attribute conditions, ranking and exact-ID comparison.
- `conversation_v4.py`: Gemini JSON intent extraction, retrieval action routing, persistent dialogue state and concise consultation response.
- `main.py`: staged `/api/chat` endpoint delegates to v4. Existing import, callback and catalog routes are retained.
- `test_search_v4.py`: six offline regression tests **written, not yet executed**.

**Widget integration:** Use the separately supplied `zorro_widget_v4.html` from this conversation. The widget now sends `history` and `state`, then retains server-returned `state` between turns. Reset clears state.

**Not yet validated:** actual Render PostgreSQL connection, Gemini responses, deployment, real product recommendations and end-to-end widget ↔ API tests. No live credentials have been accessed.

**Known caveats to resolve before production:**
- Need broad conversational tests, including exclusion and negation and comparison of exact previous items.
- `ai_json` relies on Gemini extraction; when Gemini is unavailable, intent parsing degrades. Do not promise good matching in that state.
- The current sales logic still needs verified store policy information for shipping/payment/returns.
- Widget change must be installed in Horoshop, separately from GitHub. Do not deploy until evaluated.

**How to test safely:**
1. Make staging Render service using this branch and the same dependency file, and staging DB or backed-up copy. Do not point production widget to staging.
2. Run `python -m unittest test_search_v4.py`.
3. Call `/api/catalog-status` and `/api/chat` on the staging service; verify returned `state.product_ids`.
4. Test multi-turn conversation: search under 2000; compare; exclude leather; prefer steel; request two previously shown models.
5. Run a broader regression set before deploying to customers.
