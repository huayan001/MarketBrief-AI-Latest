import assert from "node:assert/strict";
import test from "node:test";

import {
  cancelSubscription,
  configureTestWebhook,
  createAuthenticatedCheckout,
  findManagedSubscriptionProduct,
  lookupSubscriptionOrder,
  PRO_MONTHLY_PRODUCT,
  selectSingleStore,
} from "./client.mjs";
import { parseEnvText } from "./config.mjs";
import { createPaymentServer } from "./service.mjs";

test("parseEnvText preserves escaped PEM newlines", () => {
  const values = parseEnvText(`
    # comment
    WAFFO_MERCHANT_ID=MER_test
    WAFFO_PRIVATE_KEY="-----BEGIN PRIVATE KEY-----\\nabc\\n-----END PRIVATE KEY-----"
  `);
  assert.equal(values.WAFFO_MERCHANT_ID, "MER_test");
  assert.match(values.WAFFO_PRIVATE_KEY, /\\nabc\\n/);
});

test("selectSingleStore refuses zero or multiple stores", () => {
  assert.throws(() => selectSingleStore([]), /没有 Store/);
  assert.throws(
    () =>
      selectSingleStore([
        { id: "STO_one", name: "One" },
        { id: "STO_two", name: "Two" },
      ]),
    /多个 Waffo Store/,
  );
  assert.equal(selectSingleStore([{ id: "STO_one", name: "One" }]).id, "STO_one");
});

test("findManagedSubscriptionProduct supports GraphQL JSON metadata", () => {
  const product = findManagedSubscriptionProduct([
    { id: "PROD_other", metadata: "{}" },
    {
      id: "PROD_marketbrief",
      metadata: JSON.stringify({
        marketbriefIntegration: PRO_MONTHLY_PRODUCT.marker,
      }),
    },
  ]);
  assert.equal(product.id, "PROD_marketbrief");
});

test("authenticated checkout binds the app user and internal order", async () => {
  const calls = [];
  const client = {
    graphql: {
      async query() {
        const call = calls.length;
        calls.push("graphql");
        if (call === 0) {
          return { data: { stores: [{ id: "STO_test", name: "Test Store" }] } };
        }
        return {
          data: {
            store: {
              subscriptionProducts: [
                {
                  id: "PROD_test",
                  name: PRO_MONTHLY_PRODUCT.name,
                  status: "active",
                  metadata: {
                    marketbriefIntegration: PRO_MONTHLY_PRODUCT.marker,
                  },
                },
              ],
            },
          },
        };
      },
    },
    subscriptionProducts: {
      async update(params) {
        return {
          product: {
            id: params.id,
            name: PRO_MONTHLY_PRODUCT.name,
            status: "active",
          },
        };
      },
    },
    checkout: {
      authenticated: {
        async create(params) {
          calls.push(params);
          return {
            sessionId: "cs_test",
            checkoutUrl: "https://checkout.example/test",
            expiresAt: "2026-08-07T08:00:00.000Z",
          };
        },
      },
    },
  };

  const result = await createAuthenticatedCheckout(
    {
      userId: 42,
      buyerEmail: "USER@example.com",
      internalOrderId: "order-local-1",
      successUrl: "http://127.0.0.1:8787/?payment=order-local-1",
      language: "zh-CN",
    },
    client,
  );

  const checkout = calls.at(-1);
  assert.equal(checkout.buyerIdentity, "marketbrief-user:42");
  assert.equal(checkout.buyerEmail, "user@example.com");
  assert.equal(checkout.orderMerchantExternalId, "order-local-1");
  assert.equal(checkout.metadata.internalOrderId, "order-local-1");
  assert.equal(checkout.metadata.planCode, "pro_monthly");
  assert.equal(checkout.withTrial, false);
  assert.equal(checkout.language, "zh-Hans");
  assert.equal(result.productId, "PROD_test");
  assert.equal(result.amount, "19.99");
});

test("cancelSubscription scopes the request to the stored Waffo order", async () => {
  const calls = [];
  const client = {
    orders: {
      async cancelSubscription(params) {
        calls.push(params);
        return { orderId: params.orderId, status: "canceling" };
      },
    },
  };
  const result = await cancelSubscription({ orderId: "ORD_subscription" }, client);
  assert.deepEqual(calls, [{ orderId: "ORD_subscription" }]);
  assert.equal(result.status, "canceling");
});

test("lookupSubscriptionOrder reconciles by merchant external id", async () => {
  const queries = [];
  const client = {
    graphql: {
      async query(input) {
        queries.push(input);
        if (queries.length === 1) {
          return { data: { stores: [{ id: "STO_test", name: "Test Store" }] } };
        }
        return {
          data: {
            subscriptionOrders: [
              {
                id: "ORD_test",
                testMode: true,
                status: "active",
                metadata: JSON.stringify({ internalOrderId: "local-1" }),
                productVersion: {
                  productId: "PROD_test",
                  metadata: JSON.stringify({
                    marketbriefIntegration: PRO_MONTHLY_PRODUCT.marker,
                  }),
                },
                priceSnapshot: {
                  regularPhase: { subtotal: "19.99" },
                },
              },
            ],
          },
        };
      },
    },
  };

  const result = await lookupSubscriptionOrder(
    { externalId: "local-1" },
    client,
  );

  assert.equal(
    queries.at(-1).variables.externalId,
    "local-1",
  );
  assert.equal(result.order.id, "ORD_test");
  assert.equal(result.order.productId, "PROD_test");
  assert.equal(result.order.amount, "19.99");
  assert.equal(result.order.metadata.internalOrderId, "local-1");
});

test("webhook configuration subscribes to the full subscription lifecycle", async () => {
  const calls = [];
  const client = {
    graphql: {
      async query() {
        const index = calls.filter((item) => item === "graphql").length;
        calls.push("graphql");
        if (index === 0) {
          return { data: { stores: [{ id: "STO_test", name: "Test Store" }] } };
        }
        if (index === 1) {
          return {
            data: {
              store: {
                subscriptionProducts: [
                  {
                    id: "PROD_test",
                    name: PRO_MONTHLY_PRODUCT.name,
                    status: "active",
                    metadata: {
                      marketbriefIntegration: PRO_MONTHLY_PRODUCT.marker,
                    },
                  },
                ],
              },
            },
          };
        }
        return {
          data: {
            store: {
              storeWebhooks: [
                {
                  id: "hook-1",
                  channel: "http",
                  url: "https://example.com/api/payments/webhook",
                  events: ["order.completed"],
                  testMode: true,
                },
              ],
            },
          },
        };
      },
    },
    subscriptionProducts: {
      async update(params) {
        return {
          product: {
            id: params.id,
            name: PRO_MONTHLY_PRODUCT.name,
            status: "active",
          },
        };
      },
    },
    webhooks: {
      async update(params) {
        calls.push(params);
        return { webhook: params };
      },
    },
  };

  await configureTestWebhook(
    "https://example.com/api/payments/webhook",
    client,
  );
  const update = calls.at(-1);
  assert.deepEqual(new Set(update.events), new Set([
    "subscription.activated",
    "subscription.payment_succeeded",
    "subscription.canceling",
    "subscription.uncanceled",
    "subscription.canceled",
    "subscription.past_due",
  ]));
});

test("webhook endpoint rejects an invalid signature without parsing JSON first", async (context) => {
  const server = createPaymentServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  context.after(() => new Promise((resolve) => server.close(resolve)));
  const address = server.address();

  const response = await fetch(
    `http://127.0.0.1:${address.port}/internal/verify-webhook`,
    {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-Waffo-Signature": "t=1,v1=invalid",
      },
      body: '{ "spacing": "must stay unchanged" }\n',
    },
  );
  assert.equal(response.status, 401);
  assert.match((await response.json()).error, /签名无效/);
});
