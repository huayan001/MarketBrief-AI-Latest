import { createServer } from "node:http";
import { pathToFileURL } from "node:url";

import { hasWaffoCredentials } from "./config.mjs";
import {
  cancelSubscription,
  createAuthenticatedCheckout,
  lookupSubscriptionOrder,
  publicError,
  verifyTestWebhook,
} from "./client.mjs";

const BODY_LIMIT = 256 * 1024;

function sendJson(response, status, payload) {
  const body = Buffer.from(JSON.stringify(payload), "utf8");
  response.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": body.length,
    "Cache-Control": "no-store",
  });
  response.end(body);
}

async function readBody(request) {
  const chunks = [];
  let size = 0;
  for await (const chunk of request) {
    size += chunk.length;
    if (size > BODY_LIMIT) throw new Error("请求体超过 256 KiB 限制");
    chunks.push(chunk);
  }
  return Buffer.concat(chunks);
}

function parseCheckoutBody(raw) {
  const value = JSON.parse(raw.toString("utf8") || "{}");
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    throw new Error("checkout 请求体必须是 JSON object");
  }
  return value;
}

export function createPaymentServer() {
  return createServer(async (request, response) => {
    try {
      const url = new URL(request.url || "/", "http://127.0.0.1");
      if (request.method === "GET" && url.pathname === "/health") {
        sendJson(response, 200, {
          ok: true,
          configured: hasWaffoCredentials(),
          environment: "test",
        });
        return;
      }

      if (request.method === "POST" && url.pathname === "/internal/checkout") {
        const result = await createAuthenticatedCheckout(parseCheckoutBody(await readBody(request)));
        sendJson(response, 200, result);
        return;
      }

      if (request.method === "POST" && url.pathname === "/internal/subscription/cancel") {
        const result = await cancelSubscription(parseCheckoutBody(await readBody(request)));
        sendJson(response, 200, result);
        return;
      }

      if (request.method === "POST" && url.pathname === "/internal/subscription/lookup") {
        const result = await lookupSubscriptionOrder(parseCheckoutBody(await readBody(request)));
        sendJson(response, 200, result);
        return;
      }

      if (request.method === "POST" && url.pathname === "/internal/verify-webhook") {
        const raw = await readBody(request);
        const signature = request.headers["x-waffo-signature"];
        try {
          const event = verifyTestWebhook(raw.toString("utf8"), signature);
          sendJson(response, 200, { event });
        } catch {
          sendJson(response, 401, { error: "Waffo webhook 签名无效或事件已过期" });
        }
        return;
      }

      sendJson(response, 404, { error: "Not found" });
    } catch (error) {
      const exposed = publicError(error);
      const status = exposed.message.startsWith("Waffo 未配置") ? 503 : exposed.status;
      console.error(`[waffo-sidecar] ${exposed.message}`);
      sendJson(response, status, { error: exposed.message });
    }
  });
}

function parsePort(argv) {
  const value = Number(argv[2] || 8790);
  if (!Number.isInteger(value) || value < 1 || value > 65535) {
    throw new Error("sidecar 端口无效");
  }
  return value;
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) {
  const port = parsePort(process.argv);
  const server = createPaymentServer();
  server.listen(port, "127.0.0.1", () => {
    console.log(`WAFFO_SIDECAR_READY ${port}`);
  });

  const stop = () => server.close(() => process.exit(0));
  process.once("SIGINT", stop);
  process.once("SIGTERM", stop);
}
