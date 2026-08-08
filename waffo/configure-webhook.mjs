import { configureTestWebhook, publicError } from "./client.mjs";

const endpoint = process.argv[2];
if (!endpoint) {
  console.error(
    "用法: npm run waffo:webhook -- https://your-public-host/api/payments/webhook",
  );
  process.exitCode = 2;
} else {
  try {
    const result = await configureTestWebhook(endpoint);
    console.log(
      JSON.stringify(
        {
          ok: true,
          action: result.action,
          environment: "test",
          store: result.store,
          webhook: {
            id: result.webhook.id,
            url: result.webhook.url,
            events: result.webhook.events,
            testMode: result.webhook.testMode,
          },
        },
        null,
        2,
      ),
    );
  } catch (error) {
    const exposed = publicError(error);
    console.error(exposed.message);
    process.exitCode = 1;
  }
}
