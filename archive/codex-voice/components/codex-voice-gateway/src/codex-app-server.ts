import { spawn, type ChildProcessWithoutNullStreams } from "node:child_process";
import { randomUUID } from "node:crypto";
import readline from "node:readline";
import type { GatewayConfig } from "./config.js";
import { codexEnvironment } from "./config.js";
import type { ApprovalKind, GatewayEvent, PendingApproval, RealtimeVoice } from "./types.js";

type JsonRecord = Record<string, unknown>;

const APP_SERVER_INITIALIZE_TIMEOUT_MS = 12_000;
const APP_SERVER_BOOT_ATTEMPTS = 2;
const APP_SERVER_BOOT_RETRY_DELAY_MS = 250;

interface PendingRequest {
  resolve: (value: JsonRecord) => void;
  reject: (error: Error) => void;
  timer: NodeJS.Timeout;
}

interface StoredApproval {
  approval: PendingApproval;
  method: string;
  requestId: number;
  params: JsonRecord;
  timer: NodeJS.Timeout;
}

function record(value: unknown): JsonRecord {
  return value && typeof value === "object" && !Array.isArray(value) ? value as JsonRecord : {};
}

function resultThreadId(result: JsonRecord): string | null {
  if (typeof result.threadId === "string") return result.threadId;
  const thread = record(result.thread);
  return typeof thread.id === "string" ? thread.id : null;
}

function redact(value: string): string {
  return value
    .replace(/(?:sk|rk|pk)-[A-Za-z0-9_-]{12,}/g, "[redacted credential]")
    .replace(/(?:Bearer\s+)[^\s]+/gi, "Bearer [redacted]")
    .replace(/https?:\/\/[^\s]+/gi, "[URL]")
    .slice(0, 500);
}

function delay(milliseconds: number): Promise<void> {
  return new Promise(resolve => setTimeout(resolve, milliseconds));
}

export function retryableAppServerBootError(error: unknown): boolean {
  return error instanceof Error
    && error.message === "Timed out waiting for Codex App Server response to initialize.";
}

function approvalKind(method: string): ApprovalKind | null {
  if (method === "item/commandExecution/requestApproval" || method === "execCommandApproval") return "command";
  if (method === "item/fileChange/requestApproval" || method === "applyPatchApproval") return "file-change";
  if (method === "item/permissions/requestApproval") return "permissions";
  if (method === "mcpServer/elicitation/request") return "mcp";
  return null;
}

function approvalSummary(kind: ApprovalKind, params: JsonRecord): string {
  switch (kind) {
    case "command": {
      const command = typeof params.command === "string" ? redact(params.command) : "A command requires approval";
      const cwd = typeof params.cwd === "string" ? ` in ${params.cwd}` : "";
      return `${command}${cwd}`;
    }
    case "file-change":
      return redact(typeof params.reason === "string" ? params.reason : "Codex requests permission to change files.");
    case "permissions":
      return redact(typeof params.reason === "string" ? params.reason : "Codex requests additional permissions.");
    case "mcp":
      return redact(typeof params.message === "string" ? params.message : "An MCP server requests input or confirmation.");
  }
}

export class CodexAppServer {
  private process: ChildProcessWithoutNullStreams | null = null;
  private nextId = 1;
  private readonly requests = new Map<number, PendingRequest>();
  private readonly approvals = new Map<string, StoredApproval>();
  private threadId: string | null = null;
  private activeTurnId: string | null = null;
  private answerSdp: { resolve: (sdp: string) => void; reject: (error: Error) => void; timer: NodeJS.Timeout } | null = null;
  private onEvent: (event: GatewayEvent) => void;

  constructor(
    private readonly config: GatewayConfig,
    private readonly sessionId: string,
    private readonly previousThreadId: string | null,
    onEvent: (event: GatewayEvent) => void,
    private readonly voice: RealtimeVoice = "ember",
  ) {
    this.onEvent = onEvent;
  }

  async start(offerSdp: string): Promise<string> {
    await this.boot();
    const threadId = this.requireThreadId();
    const answer = new Promise<string>((resolve, reject) => {
      const timer = setTimeout(() => {
        this.answerSdp = null;
        reject(new Error("Timed out waiting for the WebRTC SDP answer."));
      }, 60_000);
      this.answerSdp = { resolve, reject, timer };
    });
    await this.request("thread/realtime/start", {
      threadId,
      version: "v3",
      outputModality: "audio",
      voice: this.voice,
      transport: { type: "webrtc", sdp: offerSdp },
      includeStartupContext: true,
      clientManagedHandoffs: false,
      codexResponseHandoffMode: "bemTags",
    }, 65_000);
    this.onEvent({ type: "session.started", threadId });
    return answer;
  }

  async appendText(text: string): Promise<void> {
    const value = text.trim();
    if (!value) return;
    await this.request("thread/realtime/appendText", { threadId: this.requireThreadId(), role: "user", text: value });
  }

  async interrupt(): Promise<void> {
    if (!this.threadId || !this.activeTurnId) return;
    await this.request("turn/interrupt", { threadId: this.threadId, turnId: this.activeTurnId }, 10_000);
  }

  async decideApproval(id: string, decision: "accept" | "decline"): Promise<boolean> {
    const stored = this.approvals.get(id);
    if (!stored) return false;
    clearTimeout(stored.timer);
    this.approvals.delete(id);
    if (decision === "decline") {
      this.respondError(stored.requestId, -32000, "User declined this action.");
    } else if (stored.method === "item/permissions/requestApproval") {
      this.respond(stored.requestId, { permissions: stored.params.permissions ?? {}, scope: "turn", reviewAllSubsequentCommands: false });
    } else if (stored.method === "mcpServer/elicitation/request") {
      this.respondError(stored.requestId, -32000, "MCP input requires a dedicated form and is not available in Voice Gateway yet.");
    } else {
      this.respond(stored.requestId, { decision: "accept" });
    }
    this.onEvent({ type: "approval.resolved", approvalId: id, decision });
    return true;
  }

  async stop(): Promise<void> {
    const process = this.process;
    for (const pending of this.requests.values()) {
      clearTimeout(pending.timer);
      pending.reject(new Error("Codex App Server stopped."));
    }
    this.requests.clear();
    if (this.answerSdp) {
      clearTimeout(this.answerSdp.timer);
      this.answerSdp.reject(new Error("Codex App Server stopped before WebRTC completed."));
      this.answerSdp = null;
    }
    for (const approval of this.approvals.values()) clearTimeout(approval.timer);
    this.approvals.clear();
    if (!process) return;
    if (this.threadId) {
      try { await this.request("thread/realtime/stop", { threadId: this.threadId }, 5_000); } catch { /* best effort */ }
      try { await this.request("thread/unsubscribe", { threadId: this.threadId }, 5_000); } catch { /* best effort */ }
    }
    this.process = null;
    process.kill();
  }

  get currentThreadId(): string | null {
    return this.threadId;
  }

  private async boot(): Promise<void> {
    let lastError: unknown;
    for (let attempt = 1; attempt <= APP_SERVER_BOOT_ATTEMPTS; attempt += 1) {
      try {
        await this.bootOnce();
        return;
      } catch (error) {
        lastError = error;
        if (!retryableAppServerBootError(error) || attempt === APP_SERVER_BOOT_ATTEMPTS) throw error;
        console.warn(`Codex App Server initialize timed out; retrying startup (${attempt + 1}/${APP_SERVER_BOOT_ATTEMPTS}).`);
        await this.stop();
        await delay(APP_SERVER_BOOT_RETRY_DELAY_MS);
      }
    }
    throw lastError;
  }

  private async bootOnce(): Promise<void> {
    if (this.process) throw new Error("Codex App Server is already running for this session.");
    const process = spawn(this.config.codexBin, ["app-server", "--enable", "realtime_conversation"], {
      env: codexEnvironment(this.config),
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.process = process;
    process.once("error", error => {
      if (this.process === process) this.fail(error);
    });
    process.once("exit", (code, signal) => {
      if (this.process === process) this.fail(new Error(`Codex App Server exited (${code ?? signal ?? "unknown"}).`));
    });
    readline.createInterface({ input: process.stdout }).on("line", line => this.receive(line));
    readline.createInterface({ input: process.stderr }).on("line", line => {
      if (/error|failed/i.test(line)) {
        console.warn(`Codex App Server stderr: ${redact(line)}`);
        this.onEvent({ type: "session.error", message: "Codex App Server reported an error.", recoverable: true });
      }
    });
    await this.request("initialize", {
      clientInfo: { name: "codex_voice_gateway", title: "Codex Voice Gateway", version: "0.1.0" },
      capabilities: { experimentalApi: true },
    }, APP_SERVER_INITIALIZE_TIMEOUT_MS);
    this.notify("initialized", {});
    await this.resumeOrStartThread();
  }

  private async resumeOrStartThread(): Promise<void> {
    if (this.previousThreadId) {
      try {
        const resumed = await this.request("thread/resume", this.threadParams({ threadId: this.previousThreadId }), 20_000);
        this.threadId = resultThreadId(resumed) ?? this.previousThreadId;
        return;
      } catch {
        // A stored thread may be unavailable after a Codex reset. Start a new isolated thread.
      }
    }
    const started = await this.request("thread/start", this.threadParams({}), 20_000);
    this.threadId = resultThreadId(started);
    if (!this.threadId) throw new Error("Codex App Server did not return a thread id.");
  }

  private threadParams(extra: JsonRecord): JsonRecord {
    return {
      ...extra,
      cwd: this.config.projectDir,
      approvalPolicy: "on-request",
      // App Server's current schema represents this as a mode string. The
      // dedicated config.toml applies the same read-only default process-wide.
      sandbox: "read-only",
      serviceName: "codex_voice_gateway",
      developerInstructions: "You are speaking with the user. Keep spoken progress brief. Never read commands, paths, URLs, source code, tokens, or raw tool output aloud.",
    };
  }

  private requireThreadId(): string {
    if (!this.threadId) throw new Error("No Codex thread is active.");
    return this.threadId;
  }

  private request(method: string, params: JsonRecord, timeoutMs = 30_000): Promise<JsonRecord> {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.requests.delete(id);
        reject(new Error(`Timed out waiting for Codex App Server response to ${method}.`));
      }, timeoutMs);
      this.requests.set(id, { resolve, reject, timer });
      this.write({ method, id, params });
    });
  }

  private notify(method: string, params: JsonRecord): void {
    this.write({ method, params });
  }

  private respond(id: number, result: JsonRecord): void {
    this.write({ id, result });
  }

  private respondError(id: number, code: number, message: string): void {
    this.write({ id, error: { code, message } });
  }

  private write(message: JsonRecord): void {
    if (!this.process) throw new Error("Codex App Server is not running.");
    this.process.stdin.write(`${JSON.stringify(message)}\n`);
  }

  private receive(line: string): void {
    let message: JsonRecord;
    try { message = JSON.parse(line) as JsonRecord; } catch { return; }
    if (typeof message.id === "number" && this.requests.has(message.id)) {
      const pending = this.requests.get(message.id)!;
      clearTimeout(pending.timer);
      this.requests.delete(message.id);
      if (message.error) pending.reject(new Error(String(record(message.error).message ?? "Codex App Server request failed.")));
      else pending.resolve(record(message.result));
      return;
    }
    if (typeof message.id === "number" && typeof message.method === "string") {
      this.handleServerRequest(message.id, message.method, record(message.params));
      return;
    }
    if (typeof message.method === "string") this.handleNotification(message.method, record(message.params));
  }

  private handleServerRequest(requestId: number, method: string, params: JsonRecord): void {
    const kind = approvalKind(method);
    if (!kind) {
      this.respondError(requestId, -32601, `Unsupported App Server request: ${method}`);
      return;
    }
    const id = randomUUID();
    const approval: PendingApproval = { id, sessionId: this.sessionId, kind, summary: approvalSummary(kind, params), expiresAt: Date.now() + 60_000 };
    const timer = setTimeout(() => {
      if (!this.approvals.delete(id)) return;
      this.respondError(requestId, -32000, "Approval timed out.");
      this.onEvent({ type: "approval.resolved", approvalId: id, decision: "expired" });
    }, 60_000);
    this.approvals.set(id, { approval, method, requestId, params, timer });
    this.onEvent({ type: "approval.requested", approval });
  }

  private handleNotification(method: string, params: JsonRecord): void {
    switch (method) {
      case "thread/realtime/sdp":
        if (typeof params.sdp === "string") {
          if (this.answerSdp) {
            clearTimeout(this.answerSdp.timer);
            this.answerSdp.resolve(params.sdp);
            this.answerSdp = null;
          }
          this.onEvent({ type: "session.sdp", sdp: params.sdp });
        }
        break;
      case "thread/realtime/transcript/delta":
        this.onEvent({ type: "transcript.delta", role: String(params.role ?? ""), delta: String(params.delta ?? "") });
        break;
      case "thread/realtime/transcript/done":
        this.onEvent({ type: "transcript.done", role: String(params.role ?? ""), text: String(params.text ?? "") });
        break;
      case "thread/realtime/outputAudio/delta": {
        const audio = record(params.audio);
        if (typeof audio.data === "string" && typeof audio.sampleRate === "number" && typeof audio.numChannels === "number") {
          this.onEvent({
            type: "audio.delta",
            audio: {
              data: audio.data,
              sampleRate: audio.sampleRate,
              numChannels: audio.numChannels,
              samplesPerChannel: typeof audio.samplesPerChannel === "number" ? audio.samplesPerChannel : undefined,
            },
          });
        }
        break;
      }
      case "turn/started": {
        const turn = record(params.turn);
        this.activeTurnId = typeof turn.id === "string" ? turn.id : null;
        this.onEvent({ type: "manager.status", status: "thinking" });
        break;
      }
      case "turn/completed":
        this.activeTurnId = null;
        this.onEvent({ type: "manager.status", status: "done" });
        break;
      case "item/completed": {
        const item = record(params.item);
        const root = record(item.root);
        const value = Object.keys(root).length ? root : item;
        if (value.type === "agentMessage" && typeof value.text === "string") {
          this.onEvent({ type: "message.final", text: value.text });
        }
        break;
      }
      case "thread/realtime/error":
        this.onEvent({ type: "session.error", message: String(params.message ?? "Realtime session failed.") });
        break;
      case "thread/realtime/closed":
        this.onEvent({ type: "session.closed", reason: typeof params.reason === "string" ? params.reason : undefined });
        break;
    }
  }

  private fail(error: Error): void {
    this.onEvent({ type: "session.error", message: error.message });
  }
}
