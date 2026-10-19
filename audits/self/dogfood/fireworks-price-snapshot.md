# Fireworks serverless price snapshot

**Captured:** 2026-10-08, from public pages only. No API calls were made and the Fireworks key was not used.

**Sources:**
- https://app.fireworks.ai/models?capability=serverless (live serverless view, read 2026-10-08, 15 results). The source for live-confirmed Standard rates and model names. It shows no `accounts/...` paths and no Priority prices.
- https://docs.fireworks.ai/serverless/pricing (Priority figures, US and Fast variants, size-based tiers). Not on the live page, so unverified for per-model rows.
- https://fireworks.ai/models/fireworks/nemotron-lightning-3p5-30b-a3b (model path and price, confirmed on its model page).
- User paste of the same live list. It matches the live view row for row on names and rates. It is not checked beyond that comparison.

**Units:** USD per 1M tokens, written as input / cached input / output. Reranker and embedding rates are per 1M input tokens.

**Status:**
- `live-confirmed`: on the public serverless page on 2026-10-08. Standard rate only.
- `unverified`: a US or Fast variant not on the live page. Shown for reference. Not current.

**Priority figures are unverified on every row.** They come from the docs page and are not on the live page. The docs page says its pricing table is the source of truth for Priority availability.

## Models

| Model | Standard in / cached / out | Status | Model path (`accounts/fireworks/models/` + slug) | Priority in / cached / out (unverified) |
|---|---|---|---|---|
| Nemotron Lightning 3.5 30B A3B | 0.05 / 0.01 / 0.20 | live-confirmed | confirmed: `nemotron-lightning-3p5-30b-a3b` | 0.0625 / 0.0125 / 0.25 |
| Ember-1 | 3.00 / 0.30 / 15.00 | live-confirmed | unconfirmed: `ember-1` | 3.75 / 0.375 / 18.75 |
| DeepSeek V4.1 Flash | 0.30 / 0.006 / 1.20 | live-confirmed | unconfirmed: `deepseek-v4p1-flash` | 0.375 / 0.0075 / 1.50 |
| GLM 5.3 | 1.40 / 0.26 / 4.40 | live-confirmed | unconfirmed: `glm-5p3` | 1.75 / 0.325 / 5.50 |
| GLM 5.3 Flash | 0.15 / 0.03 / 0.50 | live-confirmed | unconfirmed: `glm-5p3-flash` | 0.1875 / 0.0375 / 0.625 |
| GLM 5.2 | 1.40 / 0.14 / 4.40 | live-confirmed | unconfirmed: `glm-5p2` | not in docs table |
| Qwen 3.8 Max | 2.00 / 0.25 / 6.00 | live-confirmed | unconfirmed: `qwen3p8-max` | 3.00 / 0.375 / 9.00 |
| Kimi K3 | 3.00 / 0.30 / 15.00 | live-confirmed | unconfirmed: `kimi-k3` | 3.75 / 0.375 / 18.75 |
| MiniMax M3 | 0.30 / 0.06 / 1.20 | live-confirmed | unconfirmed: `minimax-m3` | 0.45 / 0.09 / 1.80 |
| OpenAI gpt-oss-120b | 0.15 / 0.015 / 0.60 | live-confirmed | unconfirmed: `gpt-oss-120b` | 0.18 / 0.018 / 0.72 |
| NVIDIA Nemotron 3 Ultra NVFP4 | 0.60 / 0.12 / 2.40 | live-confirmed | unconfirmed: `nemotron-3-ultra-nvfp4` | 0.75 / 0.15 / 3.00 (see note 1) |
| Inkling | 1.00 / 0.17 / 4.05 | live-confirmed | unconfirmed: `inkling` | not in docs table |
| Qwen3 Reranker 8B | 0.20 per 1M tokens | live-confirmed | unconfirmed: `qwen3-reranker-8b` | not in docs table |
| Qwen3 Embedding 8B | 0.10 per 1M input tokens | live-confirmed | unconfirmed: `qwen3-embedding-8b` | not in docs table |
| Kimi K3 Fast | 4.50 / 0.45 / 22.50 | unverified | unconfirmed, no slug on live page | — |
| Kimi K3 (US) | 4.50 / 0.45 / 22.50 | unverified | unconfirmed, no slug on live page | 5.625 / 0.5625 / 28.125 |
| DeepSeek V4.1 Flash (US) | 0.45 / 0.009 / 1.80 | unverified | unconfirmed, no slug on live page | 0.5625 / 0.01125 / 2.25 |
| GLM 5.3 Flash (US) | 0.225 / 0.045 / 0.75 | unverified | unconfirmed, no slug on live page | 0.28125 / 0.05625 / 0.9375 |
| GLM 5.3 (US) | 2.10 / 0.39 / 6.60 | unverified | unconfirmed, no slug on live page | 2.625 / 0.4875 / 8.25 |
| GLM 5.3 Fast | 2.10 / 0.39 / 6.60 | unverified | unconfirmed, no slug on live page | — |

**Notes**
1. NVIDIA Nemotron 3 Ultra NVFP4 on the live page has the same Standard rate as the docs row "NVIDIA Nemotron 3 Ultra (Preview)". Whether they are the same model is unconfirmed. The Priority figures come from the docs row.
2. Nemotron Lightning model page: serverless supported, context 262k, reasoning model yes, image input no, fine-tuning not supported.
3. "FireRouter with Opus" is a routing product with no per-token price on the page. It is excluded.

## Size-based tiers (docs-only, not per-model)

Input and output are priced the same, with no separate cached rate.

| Parameters or architecture | $ / 1M tokens |
|---|---|
| Less than 4B | 0.10 |
| 4B–16B | 0.20 |
| More than 16B | 0.90 |
| MoE up to 56B | 0.50 |
| MoE 56.1B–176B | 1.20 |

Embeddings, docs-only (per 1M input tokens): up to 150M $0.008; 150M–350M $0.016.

## Gaps (not captured)

1. **The full model catalog.** The docs list headline models only. The live page is one filtered view of 15 results. The model library at https://fireworks.ai/models is over 2 MB and could not be parsed. The sitemap at https://fireworks.ai/sitemap.xml is truncated and includes many older models. Every other model is priced by size tier, and its ID is unknown.
2. **Model paths.** Only Nemotron Lightning is confirmed (see the Path column).
3. **Batch pricing.** The docs say batch inference is billed at 50% of serverless pricing. That is not applied to any row.
4. **Serverless Training API and on-demand GPU prices.** Out of scope for routing and not captured.

## Keeping this accurate

A one-off snapshot goes stale. Keeping the catalog current needs either a reviewed refresh each time this file is updated, or a scheduled pull from Fireworks' authenticated model-listing endpoint. The second needs the key, which is a decision for the user.

**Using these numbers:** the canon (`docs/jev-roadmap.md`, rows `EV-6` to `EV-8`) owns the decisions. Only rows with status `live-confirmed` enter the generated `packs/fireworks.endpoints.json`. A model is routable only with a confirmed path and the confirmation list in the canon. Today that is the Nemotron row alone.
