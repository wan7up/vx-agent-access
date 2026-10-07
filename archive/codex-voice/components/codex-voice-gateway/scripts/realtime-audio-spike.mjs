import { spawn } from "node:child_process";
import readline from "node:readline";
import { loadConfig, codexEnvironment } from "../dist/config.js";

const config = loadConfig();
const child = spawn(config.codexBin, ["app-server", "--enable", "realtime_conversation"], {
  env: codexEnvironment(config),
  stdio: ["pipe", "pipe", "pipe"],
});

let nextId = 1;
const pending = new Map();

function summarize(value) {
  const json = JSON.stringify(value);
  return json
    .replace(/(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}/g, "[redacted credential]")
    .replace(/(?:Bearer\s+)[^\s"]+/gi, "Bearer [redacted]")
    .replace(/https?:\/\/[^\s"]+/gi, "[URL]")
    .slice(0, 1_000);
}

function write(message) {
  child.stdin.write(`${JSON.stringify(message)}\n`);
}

function request(method, params, timeoutMs = 30_000) {
  const id = nextId++;
  return new Promise((resolve, reject) => {
    const timer = setTimeout(() => {
      pending.delete(id);
      reject(new Error(`Timed out waiting for ${method}`));
    }, timeoutMs);
    pending.set(id, { resolve, reject, timer, method });
    write({ id, method, params });
  });
}

function notification(method, params) {
  write({ method, params });
}

function threadId(result) {
  return typeof result.threadId === "string" ? result.threadId : result.thread?.id;
}

readline.createInterface({ input: child.stdout }).on("line", line => {
  let message;
  try { message = JSON.parse(line); } catch { return; }
  if (typeof message.id === "number" && pending.has(message.id)) {
    const entry = pending.get(message.id);
    clearTimeout(entry.timer);
    pending.delete(message.id);
    if (message.error) entry.reject(new Error(`${entry.method}: ${summarize(message.error)}`));
    else entry.resolve(message.result ?? {});
    return;
  }
  if (message.method === "thread/realtime/outputAudio/delta") {
    const audio = message.params?.audio ?? {};
    console.log(JSON.stringify({ event: message.method, sampleRate: audio.sampleRate, numChannels: audio.numChannels, bytes: typeof audio.data === "string" ? Buffer.from(audio.data, "base64").length : 0 }));
    return;
  }
  console.log(JSON.stringify({ event: message.method ?? "response", params: message.params ? summarize(message.params) : undefined }));
});

readline.createInterface({ input: child.stderr }).on("line", line => {
  console.error(`[app-server] ${summarize(line)}`);
});

try {
  await request("initialize", {
    clientInfo: { name: "voice_gateway_audio_spike", title: "Voice Gateway Audio Spike", version: "0.1.0" },
    capabilities: { experimentalApi: true },
  });
  notification("initialized", {});
  const started = await request("thread/start", {
    cwd: config.projectDir,
    approvalPolicy: "on-request",
    sandbox: "read-only",
    serviceName: "codex_voice_gateway_audio_spike",
  });
  const id = threadId(started);
  if (typeof id !== "string") throw new Error("App Server did not return a thread id.");
  await request("thread/realtime/start", {
    threadId: id,
    version: "v3",
    outputModality: "audio",
    transport: { type: "websocket" },
    includeStartupContext: true,
    clientManagedHandoffs: false,
    codexResponseHandoffMode: "bemTags",
  }, 65_000);
  console.log(JSON.stringify({ started: true, threadId: id }));
  await request("thread/realtime/appendText", { threadId: id, role: "user", text: "Please say exactly: audio transport check." });
  await new Promise(resolve => setTimeout(resolve, 5_000));
  const pcm16le = Buffer.alloc(4_800); // 100 ms, 24 kHz mono PCM16 silence.
  await request("thread/realtime/appendAudio", {
    threadId: id,
    audio: {
      data: pcm16le.toString("base64"),
      sampleRate: 24_000,
      numChannels: 1,
      samplesPerChannel: 2_400,
    },
  });
  console.log(JSON.stringify({ appendAudio: "accepted" }));
  await new Promise(resolve => setTimeout(resolve, 2_000));
} catch (error) {
  console.error(error instanceof Error ? error.message : String(error));
  process.exitCode = 1;
} finally {
  child.kill();
}
