import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";

export interface GatewayConfig {
  projectDir: string;
  host: string;
  port: number;
  allowedOrigins: string[];
  codexHome: string;
  dataDir: string;
  codexBin: string;
  bridgeToken?: string | null;
}

function readDotEnv(path: string): Record<string, string> {
  if (!existsSync(path)) return {};
  const values: Record<string, string> = {};
  for (const line of readFileSync(path, "utf8").split(/\r?\n/)) {
    const trimmed = line.trim();
    if (!trimmed || trimmed.startsWith("#")) continue;
    const separator = trimmed.indexOf("=");
    if (separator < 1) continue;
    const key = trimmed.slice(0, separator).trim();
    const raw = trimmed.slice(separator + 1).trim();
    values[key] = raw.replace(/^(["'])(.*)\1$/, "$2");
  }
  return values;
}

function required(value: string | undefined, name: string): string {
  if (!value?.trim()) throw new Error(`${name} must be configured. Copy .env.example to .env first.`);
  return value.trim();
}

function defaultCodexBin(): string {
  const bundledCandidates = [
    "/Applications/Codex.app/Contents/Resources/codex",
    "/Applications/ChatGPT.app/Contents/Resources/codex",
  ];
  return bundledCandidates.find(existsSync) ?? "codex";
}

export function loadConfig(cwd = process.env.VOICE_GATEWAY_PROJECT_DIR ?? process.cwd()): GatewayConfig {
  const projectDir = resolve(cwd);
  const fileValues = readDotEnv(resolve(projectDir, ".env"));
  const value = (name: string) => process.env[name] ?? fileValues[name];
  const port = Number(value("VOICE_GATEWAY_PORT") ?? "4317");
  if (!Number.isInteger(port) || port < 1 || port > 65535) {
    throw new Error("VOICE_GATEWAY_PORT must be a valid TCP port.");
  }
  const originSource = value("VOICE_GATEWAY_ALLOWED_ORIGINS") ?? value("VOICE_GATEWAY_ALLOWED_ORIGIN");
  const allowedOrigins = required(originSource, "VOICE_GATEWAY_ALLOWED_ORIGINS").split(",").map(item => item.trim()).filter(Boolean);
  for (const allowedOrigin of allowedOrigins) {
    let origin: URL;
    try {
      origin = new URL(allowedOrigin);
    } catch {
      throw new Error("VOICE_GATEWAY_ALLOWED_ORIGINS must contain absolute HTTP(S) origins.");
    }
    if (!["http:", "https:"].includes(origin.protocol) || origin.origin !== allowedOrigin) {
      throw new Error("VOICE_GATEWAY_ALLOWED_ORIGINS entries must not contain paths, queries, or fragments.");
    }
  }

  return {
    projectDir,
    host: value("VOICE_GATEWAY_HOST")?.trim() || "127.0.0.1",
    port,
    allowedOrigins,
    codexHome: resolve(required(value("VOICE_GATEWAY_CODEX_HOME"), "VOICE_GATEWAY_CODEX_HOME")),
    dataDir: resolve(required(value("VOICE_GATEWAY_DATA_DIR"), "VOICE_GATEWAY_DATA_DIR")),
    codexBin: value("VOICE_GATEWAY_CODEX_BIN")?.trim() || defaultCodexBin(),
    bridgeToken: value("VOICE_GATEWAY_BRIDGE_TOKEN")?.trim() || null,
  };
}

export function codexEnvironment(config: GatewayConfig): NodeJS.ProcessEnv {
  const passthrough = ["PATH", "HOME", "TMPDIR", "LANG", "LC_ALL", "TERM", "USER", "SHELL"];
  const environment: NodeJS.ProcessEnv = {};
  for (const key of passthrough) {
    if (process.env[key]) environment[key] = process.env[key];
  }
  environment.CODEX_HOME = config.codexHome;
  environment.NO_COLOR = "1";
  return environment;
}
