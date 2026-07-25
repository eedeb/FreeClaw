/**
 * FreeClaw install-ping collector.
 *
 * Receives the single anonymous ping src/telemetry.py sends on first start
 * when a user has opted in, and records it in D1. Dedupe is the table's job:
 * install_id is the primary key and every write is INSERT OR IGNORE, so a
 * client that retries — or a user who re-runs the installer — is counted once.
 *
 * Two routes:
 *   POST /telemetry/install   public, takes the ping
 *   GET  /telemetry/stats     needs ?key=<STATS_KEY>, returns the counts
 *
 * Deliberately not stored: IP address, User-Agent, or anything else the edge
 * happens to see. The published promise is "a random ID, version, OS, install
 * method" and the schema physically cannot hold more than that.
 */

const UUID_RE = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const VERSION_RE = /^[\w.\-+]{1,32}$/;
const OS_RE = /^[a-z0-9_\-]{1,32}$/;
const INSTALL_METHODS = new Set(["docker", "native"]);

// Generous for a four-field JSON object, small enough that nobody can use the
// endpoint as free storage.
const MAX_BODY_BYTES = 1024;

const json = (obj, status = 200) =>
  new Response(JSON.stringify(obj), {
    status,
    headers: { "content-type": "application/json" },
  });

/** Length-independent-ish compare, so the stats key can't be probed byte by byte. */
function secretsMatch(a, b) {
  if (typeof a !== "string" || typeof b !== "string" || a.length !== b.length) return false;
  let diff = 0;
  for (let i = 0; i < a.length; i++) diff |= a.charCodeAt(i) ^ b.charCodeAt(i);
  return diff === 0;
}

function validate(body) {
  if (typeof body !== "object" || body === null) return "malformed body";
  if (!UUID_RE.test(body.install_id ?? "")) return "bad install_id";
  if (!VERSION_RE.test(body.version ?? "")) return "bad version";
  if (!OS_RE.test(body.os ?? "")) return "bad os";
  if (!INSTALL_METHODS.has(body.install_method)) return "bad install_method";
  return null;
}

async function handleInstall(request, env) {
  const declared = Number(request.headers.get("content-length") ?? 0);
  if (declared > MAX_BODY_BYTES) return json({ error: "payload too large" }, 413);

  let body;
  try {
    body = await request.json();
  } catch {
    return json({ error: "invalid json" }, 400);
  }

  const problem = validate(body);
  if (problem) return json({ error: problem }, 400);

  // Only the four validated fields are ever bound — extra keys in the payload
  // are dropped on the floor rather than persisted.
  await env.DB.prepare(
    `INSERT OR IGNORE INTO installs
       (install_id, version, os, install_method, first_seen)
     VALUES (?, ?, ?, ?, ?)`
  )
    .bind(
      body.install_id,
      body.version,
      body.os,
      body.install_method,
      new Date().toISOString()
    )
    .run();

  // Same 204 whether this was new or a duplicate: the client has nothing to
  // do differently, and it keeps the endpoint from confirming which ids exist.
  return new Response(null, { status: 204 });
}

async function handleStats(request, env) {
  const key = new URL(request.url).searchParams.get("key") ?? "";
  if (!env.STATS_KEY || !secretsMatch(key, env.STATS_KEY)) {
    return json({ error: "not found" }, 404);
  }

  const [total, byOs, byMethod, byVersion, recent] = await Promise.all([
    env.DB.prepare(`SELECT COUNT(*) AS n FROM installs`).first(),
    env.DB.prepare(`SELECT os, COUNT(*) AS n FROM installs GROUP BY os ORDER BY n DESC`).all(),
    env.DB.prepare(`SELECT install_method, COUNT(*) AS n FROM installs GROUP BY install_method ORDER BY n DESC`).all(),
    env.DB.prepare(`SELECT version, COUNT(*) AS n FROM installs GROUP BY version ORDER BY n DESC`).all(),
    env.DB.prepare(
      `SELECT substr(first_seen, 1, 10) AS day, COUNT(*) AS n
         FROM installs GROUP BY day ORDER BY day DESC LIMIT 30`
    ).all(),
  ]);

  return json({
    total_installs: total?.n ?? 0,
    by_os: byOs.results,
    by_install_method: byMethod.results,
    by_version: byVersion.results,
    last_30_days: recent.results,
  });
}

export default {
  async fetch(request, env) {
    const { pathname } = new URL(request.url);

    if (request.method === "POST" && pathname === "/telemetry/install") {
      return handleInstall(request, env);
    }
    if (request.method === "GET" && pathname === "/telemetry/stats") {
      return handleStats(request, env);
    }
    return json({ error: "not found" }, 404);
  },
};
