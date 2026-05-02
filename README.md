# BirdNET Analyzer — Field Edition

A Flask dashboard for visualizing bird detections from a Raspberry Pi running BirdNET, deployed on Railway.

## What's new in this redesign

- **Darkgreen naturalist aesthetic** — forest palette, Fraunces serif display, JetBrains Mono labels, field-journal layout.
- **AI-generated bird imagery** — uses OpenAI `gpt-image-1` to render an Audubon-style illustration of every species detected.
- **Per-species image cache** — once a bird is generated, it's stored as base64 PNG in SQLite. The same `species` key is **never regenerated** unless you explicitly clear the cache.

## Environment variables (set in Railway)

| Var | Purpose |
|---|---|
| `BIRDNET_API_KEY`   | Auth for the Pi → server `POST /api/detect` and `/api/upload` |
| `OPENAI_API_KEY`    | Used for bird-image generation |
| `OPENAI_IMAGE_MODEL` | optional, defaults to `gpt-image-1` |
| `DB_PATH`           | optional, defaults to `birdnet.db` |
| `PORT`              | injected by Railway |

## Endpoints

- `GET  /`                    — dashboard
- `POST /api/detect`          — Pi pushes one or many detections
- `POST /api/upload`          — Pi pushes a BirdNET CombinedTable CSV
- `GET  /api/detections`      — recent rows (filterable)
- `GET  /api/stats`           — aggregates for the dashboard
- `GET  /api/live`            — last 20 detections
- `GET  /api/bird-image?species=Northern+Cardinal[&scientific=Cardinalis+cardinalis][&format=json]`
        — returns PNG (default) or `{url:"data:image/png;base64,..."}` if `format=json`. Cached forever after first call.
- `POST /api/bird-images/clear[?species=...]` — admin: drop image cache (auth: `X-API-Key`)
- `GET  /api/health`          — Railway healthcheck

## Caching behaviour

The first request for a species hits OpenAI (~5–15 s). Every request after that is served instantly from SQLite. The dashboard hydrates thumbnails progressively in the background, so the page never blocks waiting for images.
