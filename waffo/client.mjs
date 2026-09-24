import {
  WaffoPancake,
  WaffoPancakeError,
  WebhookEventType,
  verifyWebhook,
} from "@waffo/pancake-ts";

import { getWaffoCredentials } from "./config.mjs";

export const PRO_MONTHLY_PRODUCT = Object.freeze({
  marker: "marketbrief-pro-monthly-v1",
  planCode: "pro_monthly",
  name: "MarketBrief AI Pro Monthly",
  currency: "USD",
  amount: "19.99",
  billingPeriod: "monthly",
});

const PRO_MONTHLY_DESCRIPTION =
  "Subscription stock market information and research software with charts, watchlists, news summaries, and analytical reports. No brokerage, trade execution, custody, or personalized investment advice.";

let sharedClient = null;
let catalogPromise = null;

export function createWaffoClient() {
  return new WaffoPancake(getWaffoCredentials());
}

export function getWaffoClient() {
  if (!sharedClient) sharedClient = createWaffoClient();
  return sharedClient;
}

export function parseMetadata(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) return value;
  if (typeof value !== "string" || !value.trim()) return {};
  try {
    const parsed = JSON.parse(value);
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed : {};
  } catch {
    return {};
  }
}

export function selectSingleStore(stores) {
  if (!Array.isArray(stores) || stores.length === 0) {
    throw new Error("当前 Waffo 商户没有 Store，请先在 Dashboard 创建 Store");
  }
  if (stores.length > 1) {
    const labels = stores.map((store) => `${store.name || "未命名"} (${store.id})`).join(", ");
    throw new Error(`检测到多个 Waffo Store，不能自动选择：${labels}`);
  }
  return stores[0];
}

export function findManagedSubscriptionProduct(products) {
  return (
    (Array.isArray(products) ? products : []).find(
      (product) =>
        parseMetadata(product.metadata).marketbriefIntegration ===
        PRO_MONTHLY_PRODUCT.marker,
    ) || null
  );
}

function graphqlData(result, operation) {
  if (result?.data) return result.data;
  const detail = (result?.errors || []).map((item) => item.message).filter(Boolean).join("; ");
  throw new Error(`${operation}失败${detail ? `：${detail}` : ""}`);
}

async function discoverOrCreateCatalog(client) {
  const storesResult = await client.graphql.query({
    query: `query MarketBriefStores {
      stores {
        id
        name
        status
      }
    }`,
  });
  const store = selectSingleStore(graphqlData(storesResult, "查询 Waffo Store").stores);

  const productsResult = await client.graphql.query({
    query: `query MarketBriefProducts($storeId: String!) {
      store(id: $storeId) {
        subscriptionProducts {
          id
          name
          status
          metadata
        }
      }
    }`,
    variables: { storeId: store.id },
  });
  const productsData = graphqlData(productsResult, "查询 Waffo Pro 月度订阅商品");
  let product = findManagedSubscriptionProduct(
    productsData.store?.subscriptionProducts,
  );

  if (!product) {
    const created = await client.subscriptionProducts.create({
      storeId: store.id,
      name: PRO_MONTHLY_PRODUCT.name,
      description: PRO_MONTHLY_DESCRIPTION,
      billingPeriod: PRO_MONTHLY_PRODUCT.billingPeriod,
      prices: {
        USD: {
          amount: PRO_MONTHLY_PRODUCT.amount,
          taxCategory: "saas",
        },
      },
      metadata: {
        marketbriefIntegration: PRO_MONTHLY_PRODUCT.marker,
        planCode: PRO_MONTHLY_PRODUCT.planCode,
      },
    });
    product = created.product;
  } else {
    const updated = await client.subscriptionProducts.update({
      id: product.id,
      name: PRO_MONTHLY_PRODUCT.name,
      description: PRO_MONTHLY_DESCRIPTION,
      billingPeriod: PRO_MONTHLY_PRODUCT.billingPeriod,
      prices: {
        USD: {
          amount: PRO_MONTHLY_PRODUCT.amount,
          taxCategory: "saas",
        },
      },
      metadata: {
        marketbriefIntegration: PRO_MONTHLY_PRODUCT.marker,
        planCode: PRO_MONTHLY_PRODUCT.planCode,
      },
    });
    product = updated.product;
  }
  if (product.status === "inactive") {
    const activated = await client.subscriptionProducts.updateStatus({
      id: product.id,
      status: "active",
    });
    product = activated.product;
  }

  return {
    store: { id: store.id, name: store.name },
    product: { id: product.id, name: product.name || PRO_MONTHLY_PRODUCT.name },
  };
}

export async function ensureTestCatalog(client = getWaffoClient()) {
  if (client !== sharedClient) return discoverOrCreateCatalog(client);
  if (!catalogPromise) {
    catalogPromise = discoverOrCreateCatalog(client).catch((error) => {
      catalogPromise = null;
      throw error;
    });
  }
  return catalogPromise;
}

function validatedSuccessUrl(value) {
  const url = new URL(String(value || ""));
  if (!["http:", "https:"].includes(url.protocol)) {
    throw new Error("successUrl 必须使用 http 或 https");
  }
  return url.toString();
}

export async function createAuthenticatedCheckout(input, client = getWaffoClient()) {
  const userId = String(input?.userId || "").trim();
  const buyerEmail = String(input?.buyerEmail || "").trim().toLowerCase();
  const internalOrderId = String(input?.internalOrderId || "").trim();
  if (!userId || !buyerEmail || !internalOrderId) {
    throw new Error("checkout 缺少 userId、buyerEmail 或 internalOrderId");
  }

  const catalog = await ensureTestCatalog(client);
  const result = await client.checkout.authenticated.create({
    productId: catalog.product.id,
    currency: PRO_MONTHLY_PRODUCT.currency,
    buyerIdentity: `marketbrief-user:${userId}`,
    buyerEmail,
    withTrial: false,
    successUrl: validatedSuccessUrl(input.successUrl),
    orderMerchantExternalId: internalOrderId,
    metadata: {
      internalOrderId,
      marketbriefUserId: userId,
      planCode: PRO_MONTHLY_PRODUCT.planCode,
      source: "marketbrief-ai",
    },
    language: input.language === "en" ? "en" : "zh-Hans",
  });

  return {
    storeId: catalog.store.id,
    productId: catalog.product.id,
    planCode: PRO_MONTHLY_PRODUCT.planCode,
    sessionId: result.sessionId,
    checkoutUrl: result.checkoutUrl,
    expiresAt: result.expiresAt,
    amount: PRO_MONTHLY_PRODUCT.amount,
    currency: PRO_MONTHLY_PRODUCT.currency,
  };
}

export async function cancelSubscription(input, client = getWaffoClient()) {
  const orderId = String(input?.orderId || "").trim();
  if (!orderId) throw new Error("取消订阅缺少 orderId");
  const result = await client.orders.cancelSubscription({ orderId });
  return {
    orderId: result.orderId,
    status: result.status,
  };
}

export async function lookupSubscriptionOrder(input, client = getWaffoClient()) {
  const externalId = String(input?.externalId || "").trim();
  if (!externalId) throw new Error("订阅对账缺少 externalId");
  const storesResult = await client.graphql.query({
    query: `query MarketBriefReconciliationStores {
      stores {
        id
        name
        status
      }
    }`,
  });
  const store = selectSingleStore(
    graphqlData(storesResult, "查询 Waffo Store").stores,
  );
  const result = await client.graphql.query({
    query: `query MarketBriefSubscriptionByReference(
      $storeId: String!
      $externalId: String!
    ) {
      subscriptionOrders(
        storeId: $storeId
        filter: { orderMerchantExternalId: { eq: $externalId } }
      ) {
        id
        storeId
        status
        testMode
        buyerEmail
        merchantProvidedBuyerIdentity
        orderMerchantExternalId
        metadata
        billingPeriod
        currency
        currentPeriodStart
        currentPeriodEnd
        canceledAt
        createdAt
        updatedAt
        priceSnapshot {
          currency
          regularPhase {
            subtotal
            taxAmount
            total
            taxCategory
          }
        }
        productVersion {
          id
          productId
          billingPeriod
          metadata
        }
      }
    }`,
    variables: {
      storeId: store.id,
      externalId,
    },
  });
  const orders = graphqlData(result, "查询 Waffo 订阅订单").subscriptionOrders || [];
  const order = orders[0] || null;
  if (order && order.testMode !== true) {
    throw new Error("订阅对账返回了非 test 订单");
  }
  if (!order) return { order: null };
  return {
    order: {
      ...order,
      metadata: parseMetadata(order.metadata),
      productMetadata: parseMetadata(order.productVersion?.metadata),
      productId: order.productVersion?.productId || null,
      amount:
        order.priceSnapshot?.regularPhase?.subtotal ||
        order.priceSnapshot?.regularPhase?.total ||
        null,
    },
  };
}

export function verifyTestWebhook(rawBody, signature) {
  const event = verifyWebhook(String(rawBody), signature, { environment: "test" });
  if (event.mode !== "test") throw new Error("测试 webhook 收到了非 test 事件");
  return event;
}

function endpointPathMatches(value) {
  try {
    return new URL(value).pathname.endsWith("/api/payments/webhook");
  } catch {
    return false;
  }
}

export async function configureTestWebhook(endpoint, client = getWaffoClient()) {
  const url = new URL(String(endpoint || ""));
  if (url.protocol !== "https:") throw new Error("Webhook endpoint 必须是公开 HTTPS URL");
  if (!url.pathname.endsWith("/api/payments/webhook")) {
    throw new Error("Webhook endpoint 必须以 /api/payments/webhook 结尾");
  }

  const catalog = await ensureTestCatalog(client);
  const result = await client.graphql.query({
    query: `query MarketBriefWebhooks($storeId: String!) {
      store(id: $storeId) {
        storeWebhooks {
          id
          channel
          url
          events
          testMode
        }
      }
    }`,
    variables: { storeId: catalog.store.id },
  });
  const webhooks = graphqlData(result, "查询 Waffo Webhook").store?.storeWebhooks || [];
  const managed = webhooks.filter(
    (item) => item.channel === "http" && item.testMode === true && endpointPathMatches(item.url),
  );
  const existing = managed.find((item) => item.url === url.toString()) || managed[0] || null;
  if (!webhooks.find((item) => item.url === url.toString()) && managed.length > 1) {
    throw new Error("检测到多个 MarketBrief 测试 webhook，请先在 Dashboard 清理重复配置");
  }

  const requiredEvents = [
    WebhookEventType.SubscriptionActivated,
    WebhookEventType.SubscriptionPaymentSucceeded,
    WebhookEventType.SubscriptionCanceling,
    WebhookEventType.SubscriptionUncanceled,
    WebhookEventType.SubscriptionCanceled,
    WebhookEventType.SubscriptionPastDue,
  ];
  if (existing) {
    const updated = await client.webhooks.update({
      id: existing.id,
      url: url.toString(),
      events: requiredEvents,
    });
    return { action: "updated", store: catalog.store, webhook: updated.webhook };
  }

  const created = await client.webhooks.add({
    storeId: catalog.store.id,
    channel: "http",
    url: url.toString(),
    events: requiredEvents,
    testMode: true,
  });
  return { action: "created", store: catalog.store, webhook: created.webhook };
}

export function publicError(error) {
  if (error instanceof WaffoPancakeError) {
    const messages = error.errors?.map((item) => item.message).filter(Boolean).join("; ");
    return {
      status: error.status || 502,
      message: messages || "Waffo Pancake API 请求失败",
    };
  }
  return {
    status: 500,
    message: error instanceof Error ? error.message : "Waffo 支付服务发生未知错误",
  };
}

export function resetClientForTests() {
  sharedClient = null;
  catalogPromise = null;
}
