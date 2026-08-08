import { readFileSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = dirname(fileURLToPath(import.meta.url));
export const PROJECT_ROOT = resolve(HERE, "..");

export function parseEnvText(text) {
  const values = {};
  for (const rawLine of String(text).split(/\r?\n/)) {
    const line = rawLine.trim();
    if (!line || line.startsWith("#") || !line.includes("=")) continue;

    const separator = line.indexOf("=");
    const key = line.slice(0, separator).trim();
    let value = line.slice(separator + 1).trim();
    if (!key) continue;

    const quote = value[0];
    if ((quote === '"' || quote === "'") && value.at(-1) === quote) {
      value = value.slice(1, -1);
    }
    if (value) values[key] = value;
  }
  return values;
}

export function loadProjectEnv(baseEnv = process.env) {
  const env = { ...baseEnv };
  for (const filename of ["api_keys.env", ".env"]) {
    try {
      const parsed = parseEnvText(readFileSync(resolve(PROJECT_ROOT, filename), "utf8"));
      for (const [key, value] of Object.entries(parsed)) {
        if (env[key] === undefined || env[key] === "") env[key] = value;
      }
    } catch (error) {
      if (error?.code !== "ENOENT") throw error;
    }
  }
  return env;
}

export function getWaffoCredentials(baseEnv = process.env) {
  const env = loadProjectEnv(baseEnv);
  const merchantId = String(env.WAFFO_MERCHANT_ID || "").trim();
  const privateKey = String(env.WAFFO_PRIVATE_KEY || "").trim();
  if (!merchantId || !privateKey) {
    throw new Error(
      "Waffo 未配置：请在 api_keys.env 中填写 WAFFO_MERCHANT_ID 和 WAFFO_PRIVATE_KEY",
    );
  }
  return { merchantId, privateKey };
}

export function hasWaffoCredentials(baseEnv = process.env) {
  try {
    getWaffoCredentials(baseEnv);
    return true;
  } catch {
    return false;
  }
}
