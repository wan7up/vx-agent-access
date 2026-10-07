import { createHash } from "node:crypto";
import { existsSync, mkdirSync, readdirSync, readFileSync, statSync, writeFileSync } from "node:fs";
import { spawnSync } from "node:child_process";
import { join } from "node:path";
import { loadConfig, codexEnvironment } from "./config.js";

const config = loadConfig();
mkdirSync(config.dataDir, { recursive: true, mode: 0o700 });
const environment = codexEnvironment(config);

function run(args: string[]) {
  return spawnSync(config.codexBin, args, { encoding: "utf8", env: environment });
}

const version = run(["--version"]);
const login = run(["login", "status"]);
const features = run(["features", "list"]);
const schemaDir = join(config.dataDir, "app-server-schema");
const schema = run(["app-server", "generate-json-schema", "--experimental", "--enable", "realtime_conversation", "--out", schemaDir]);
function hashDirectory(directory: string): string | null {
  if (!existsSync(directory)) return null;
  const digest = createHash("sha256");
  const visit = (path: string) => {
    for (const name of readdirSync(path).sort()) {
      const child = join(path, name);
      const info = statSync(child);
      if (info.isDirectory()) visit(child);
      else {
        digest.update(child.slice(directory.length));
        digest.update(readFileSync(child));
      }
    }
  };
  visit(directory);
  return digest.digest("hex");
}
const report = {
  generatedAt: new Date().toISOString(),
  codexHome: config.codexHome,
  codexHomeExists: existsSync(config.codexHome),
  version: version.stdout.trim(),
  versionExitCode: version.status,
  login: (login.stdout || login.stderr).trim(),
  loginExitCode: login.status,
  realtimeFeature: features.stdout.split(/\r?\n/).find(line => line.startsWith("realtime_conversation"))?.trim() ?? null,
  schemaExitCode: schema.status,
  schemaHash: hashDirectory(schemaDir),
};
writeFileSync(join(config.dataDir, "compatibility-report.json"), `${JSON.stringify(report, null, 2)}\n`, { mode: 0o600 });
process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
if (version.status !== 0 || login.status !== 0 || schema.status !== 0) process.exitCode = 1;
