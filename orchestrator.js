/**
 * Process supervisor for the 4 merged bots.
 * Renders exposes exactly one PORT to the outside world; this process is the
 * only one allowed to bind it. Each child bot gets its own internal PORT
 * (never exposed directly) plus any env-var renames it needs so that two
 * bots sharing a variable name (e.g. BOT_TOKEN) don't collide.
 *
 * Routes on the public port:
 *   GET /            -> aggregate JSON status of all 4 bots (Render health check)
 *   GET /whatsapp     -> reverse-proxied to the order-status bot's QR/login page
 */

const http = require("http");
const { spawn } = require("child_process");
const path = require("path");

const PUBLIC_PORT = process.env.PORT || 10000;
const PYTHON_BIN = process.env.PYTHON_BIN || "python3";

const BOTS = [
  {
    name: "wholesale",
    cmd: "node",
    args: ["bots/wholesale/index.js"],
    internalPort: 10001,
    env: {
      BOT_TOKEN: process.env.WHOLESALE_BOT_TOKEN || "",
      GEMINI_API_KEY: process.env.WHOLESALE_GEMINI_API_KEY || process.env.GEMINI_API_KEY || "",
    },
  },
  {
    name: "price",
    cmd: PYTHON_BIN,
    args: ["bots/price/bot.py"],
    internalPort: 10002,
    env: {
      BOT_TOKEN: process.env.PRICE_BOT_TOKEN || "",
    },
  },
  {
    name: "promo",
    cmd: PYTHON_BIN,
    args: ["bots/promo/bot.py"],
    internalPort: 10003,
    env: {
      TELEGRAM_BOT_TOKEN: process.env.PROMO_TELEGRAM_BOT_TOKEN || process.env.TELEGRAM_BOT_TOKEN || "",
      META_ACCESS_TOKEN: process.env.META_ACCESS_TOKEN || "",
      GEMINI_API_KEY: process.env.PROMO_GEMINI_API_KEY || process.env.GEMINI_API_KEY || "",
      META_AD_ACCOUNT_ID: process.env.META_AD_ACCOUNT_ID || "",
      TARGET_CHAT_ID: process.env.TARGET_CHAT_ID || "",
      REPORT_INTERVAL_HOURS: process.env.REPORT_INTERVAL_HOURS || "",
      GEMINI_MODEL: process.env.GEMINI_MODEL || "",
      ACTIONS_LOG_FILE: process.env.PROMO_ACTIONS_LOG_FILE || "",
    },
  },
  {
    name: "order-status",
    cmd: "node",
    args: ["bots/order-status/bot.js"],
    internalPort: 10004,
    env: {
      AUTH_STATE_DIR: process.env.AUTH_STATE_DIR || "auth_state",
    },
    proxyPath: "/whatsapp",
  },
];

const state = new Map(); // name -> { proc, restarts, alive, lastExit }

function startBot(bot) {
  const childEnv = { ...process.env, PORT: String(bot.internalPort), ...bot.env };
  const proc = spawn(bot.cmd, bot.args, {
    cwd: __dirname,
    env: childEnv,
    stdio: ["ignore", "pipe", "pipe"],
  });

  const entry = state.get(bot.name) || { restarts: 0 };
  entry.proc = proc;
  entry.alive = true;
  entry.lastExit = null;
  state.set(bot.name, entry);

  const prefix = `[${bot.name}]`;
  proc.stdout.on("data", (d) => process.stdout.write(prefixLines(d, prefix)));
  proc.stderr.on("data", (d) => process.stderr.write(prefixLines(d, prefix)));

  proc.on("exit", (code, signal) => {
    entry.alive = false;
    entry.lastExit = { code, signal, at: new Date().toISOString() };
    if (shuttingDown) return;

    entry.restarts += 1;
    const delayMs = Math.min(30000, 2000 * entry.restarts);
    console.error(
      `${prefix} exited (code=${code}, signal=${signal}). Restarting in ${delayMs / 1000}s (attempt ${entry.restarts})...`
    );
    setTimeout(() => startBot(bot), delayMs);
  });

  console.log(`${prefix} started (pid=${proc.pid}, internal port=${bot.internalPort})`);
}

function prefixLines(buf, prefix) {
  return buf
    .toString()
    .split("\n")
    .filter((_, i, arr) => i < arr.length - 1 || arr[i] !== "")
    .map((line) => `${prefix} ${line}`)
    .join("\n") + "\n";
}

/* ══════════════ public health server + reverse proxy ══════════════ */
const server = http.createServer((req, res) => {
  const proxyTarget = BOTS.find((b) => b.proxyPath && req.url.startsWith(b.proxyPath));
  if (proxyTarget) {
    return proxyRequest(req, res, proxyTarget.internalPort, proxyTarget.proxyPath);
  }

  const statusPayload = {};
  for (const bot of BOTS) {
    const entry = state.get(bot.name);
    statusPayload[bot.name] = {
      alive: !!entry?.alive,
      restarts: entry?.restarts || 0,
      lastExit: entry?.lastExit || null,
    };
  }
  res.writeHead(200, { "Content-Type": "application/json; charset=utf-8" });
  res.end(JSON.stringify({ ok: true, bots: statusPayload }, null, 2));
});

function proxyRequest(req, res, targetPort, stripPrefix) {
  const subPath = req.url.slice(stripPrefix.length) || "/";
  const proxyReq = http.request(
    { host: "127.0.0.1", port: targetPort, path: subPath, method: req.method, headers: req.headers },
    (proxyRes) => {
      res.writeHead(proxyRes.statusCode, proxyRes.headers);
      proxyRes.pipe(res);
    }
  );
  proxyReq.on("error", (err) => {
    res.writeHead(502, { "Content-Type": "text/plain; charset=utf-8" });
    res.end(`Bad gateway: ${targetPort} unreachable (${err.message})`);
  });
  req.pipe(proxyReq);
}

server.listen(PUBLIC_PORT, () => {
  console.log(`[orchestrator] public health/proxy server listening on ${PUBLIC_PORT}`);
});

/* ══════════════ startup + graceful shutdown ══════════════ */
for (const bot of BOTS) startBot(bot);

let shuttingDown = false;
function shutdown(signal) {
  if (shuttingDown) return;
  shuttingDown = true;
  console.log(`[orchestrator] received ${signal}, stopping all bots...`);
  for (const bot of BOTS) {
    const entry = state.get(bot.name);
    entry?.proc?.kill("SIGTERM");
  }
  server.close(() => process.exit(0));
  setTimeout(() => process.exit(0), 8000).unref();
}
process.on("SIGTERM", () => shutdown("SIGTERM"));
process.on("SIGINT", () => shutdown("SIGINT"));
