# FreeClaw telemetry endpoint

The collector behind the opt-in install ping. It's a Cloudflare Worker backed
by a D1 table, and it is **not deployed automatically** — the client side ships
disabled, so nothing breaks if this never goes up.

## What it stores

One row per install, and only these fields:

| Column | Example | Where it comes from |
|---|---|---|
| `install_id` | `3f2a…` (UUID4) | generated on the user's machine, saved to their `.env` |
| `version` | `0.1.0` | the `VERSION` file |
| `os` | `darwin` | `platform.system()` |
| `install_method` | `docker` | presence of `/.dockerenv` |
| `first_seen` | ISO timestamp | set by the Worker on insert |

No IP address, no User-Agent, no hostname, no chat content. The schema has no
column for them, which is the point — see [`schema.sql`](schema.sql).

## Deploy

```bash
cd telemetry
npx wrangler d1 create freeclaw-telemetry
```

Paste the printed `database_id` into `wrangler.toml`, then:

```bash
npx wrangler d1 execute freeclaw-telemetry --remote --file=schema.sql
```

Set the key that guards the stats route, then ship it:

```bash
npx wrangler secret put STATS_KEY
```

```bash
npx wrangler deploy
```

The route in `wrangler.toml` only claims `freeclaw.eedeb.dev/telemetry/*`, so
the marketing page and `install.sh` on that same hostname are untouched.

## Check the numbers

```bash
curl "https://freeclaw.eedeb.dev/telemetry/stats?key=YOUR_STATS_KEY"
```

Returns the total plus breakdowns by OS, install method, version, and day for
the last 30 days. Without a correct key the route 404s rather than 403s, so it
doesn't advertise its own existence.

## Verify the client end to end

Point a FreeClaw install at a local collector instead of production:

```bash
FC_TELEMETRY=1 FC_TELEMETRY_URL=http://localhost:8787/telemetry/install npx wrangler dev
```

The client writes `FC_INSTALL_ID` to `.env` only after a send succeeds, so
deleting that line and restarting is how you re-test a "first run".
