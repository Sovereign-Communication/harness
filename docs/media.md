# Media generation (`harness media`)

Harness integrates with **sovereign-media** — a sibling service
(`Sovereign-Communication/sovereign-media`, local checkout
`Documents/GitHub/sovereign-media`) that fronts six providers for fast
image/video generation:

| Provider | Best for |
|---|---|
| **openai** | gpt-image (image), Sora 2 (video) |
| **google** | Imagen 4 / Nano Banana (image — free tier via AI Studio key), Veo 3.1 (video) |
| **higgsfield** | 50+ model catalog: Soul 2 stills ($0.003!), Kling 3.0 / Seedance / Wan video |
| **fal** | cheapest Flux variants, broad open-model catalog |
| **replicate** | per-second billing, huge community model list |
| **luma** | Photon (image), Ray 2 (video) |

Same discipline as the rest of harness, applied to media spend:

- **Pre-flight ceilings** — worst-case cost computed *before* any provider
  call; over ceiling or daily budget → refused with the math, $0 spent.
- **Hash-chained spend ledger** — every transition and cost is append-only,
  tamper-evident, attributed **per project**.
- **Honest failures** — service down / unsigned-in / refusing →
  `[defer] media: <reason>` and nothing spent.
- **Credentials stay service-side** — harness never sees provider keys.

## One-time setup

```bash
cd ../sovereign-media        # sibling checkout
pip install -e .
media auth login google-key  # free AI Studio key ($0, image gen included)
# and/or: media auth login openai | higgsfield | fal | replicate | luma
media serve                  # REST on http://127.0.0.1:8765
```

Remote endpoint (optional): `~/.config/harness/media.json`
→ `{"base_url": "...", "token": "..."}`, or environment variables
`MEDIA_BASE_URL`, `MEDIA_TOKEN`, `MEDIA_CONFIG_PATH` (a custom config file
location). Precedence: constructor args > config file > env vars > loopback
default (`http://127.0.0.1:8765`). No service URL or provider name is
hardcoded in `harness/media_client.py`; the provider catalog above lives
service-side in sovereign-media.

## CLI

```bash
harness media image "a cabin in snowy woods" --project scmessenger --quality low
harness media image "neon product shot" --provider higgsfield --model soul-2 --project scmessenger
harness media video "ocean waves" --provider higgsfield --model kling-3.0 --seconds 4 --project scmessenger
harness media jobs --project scmessenger
harness media balance --project scmessenger
```

Note the governor: a 4s Kling 3.0 clip bills ~$0.56 (padded bucket), which
the default $0.50 ceiling refuses — raise `per_call_ceiling` in
`~/.sovereign-media/config.json` (max $5) for premium video models.

## Python API

```python
from harness.media_client import MediaAdapter

media = MediaAdapter()
result = media.image("a cabin in snowy woods", project="scmessenger")
result = media.video("ocean waves", provider="higgsfield",
                     model="seedance-2.5", seconds=4, project="scmessenger")
# {"job_id", "status", "provider", "model", "cost_estimate",
#  "cost", "artifacts", "error", "project"}
```

`status` ∈ `succeeded | failed | refused | service_unavailable | timeout`
(`queued | running` only with `wait=False`). Budget refusals carry `math`.
