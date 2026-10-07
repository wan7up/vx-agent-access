import { randomBytes, timingSafeEqual } from "node:crypto";
import Fastify, { type FastifyInstance } from "fastify";
import fastifyStatic from "@fastify/static";
import { WebSocket, WebSocketServer, type RawData } from "ws";
import { join } from "node:path";
import { fileURLToPath } from "node:url";
import type { GatewayConfig } from "./config.js";
import { CodexAppServer } from "./codex-app-server.js";
import { HeadlessWebRtcPeer } from "./headless-webrtc.js";
import { SessionStore } from "./store.js";
import { isRealtimeVoice, type BrowserMessage, type GatewayEvent, type VoiceBridgeMessage } from "./types.js";
import { TicketRegistry } from "./security.js";
import { callOpeningUserMessage, normalizeCallOpeningRequest } from "./call-opening.js";

const TICKET_TTL_MS = 60_000;
const MAX_MESSAGE_BYTES = 256 * 1024;
const MAX_SDP_BYTES = 192 * 1024;
const MAX_TEXT_BYTES = 32 * 1024;

interface ActiveConnection {
  socket: WebSocket;
  adapter: CodexAppServer | null;
  audioBridge: HeadlessWebRtcPeer | null;
  openingSent: boolean;
}

function rawLength(data: RawData): number {
  return Array.isArray(data) ? data.reduce((total, part) => total + part.byteLength, 0) : data.byteLength;
}

function browserSecret(headers: Record<string, unknown>): string | undefined {
  const value = headers["x-voice-session-token"];
  return typeof value === "string" && value.length >= 32 ? value : undefined;
}

function parseMessage(data: RawData): BrowserMessage | null {
  try {
    const parsed = JSON.parse(data.toString());
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as BrowserMessage : null;
  } catch {
    return null;
  }
}

function parseBridgeMessage(data: RawData): VoiceBridgeMessage | null {
  try {
    const parsed = JSON.parse(data.toString());
    return parsed && typeof parsed === "object" && !Array.isArray(parsed) ? parsed as VoiceBridgeMessage : null;
  } catch {
    return null;
  }
}

function bridgeAuthorized(headers: Record<string, unknown>, expected: string | null | undefined): boolean {
  if (!expected) return false;
  const value = headers["authorization"];
  if (typeof value !== "string" || !value.startsWith("Bearer ")) return false;
  const candidate = Buffer.from(value.slice("Bearer ".length));
  const secret = Buffer.from(expected);
  return candidate.length === secret.length && timingSafeEqual(candidate, secret);
}

function isConversationKey(value: string): boolean {
  return /^wechat:(group|direct):[A-Za-z0-9_@.-]{1,160}$/.test(value);
}

export class VoiceGateway {
  readonly app: FastifyInstance;
  readonly store: SessionStore;
  private readonly wss = new WebSocketServer({ noServer: true, maxPayload: MAX_MESSAGE_BYTES });
  private readonly tickets = new TicketRegistry();
  private readonly active = new Map<string, ActiveConnection>();

  constructor(readonly config: GatewayConfig) {
    this.store = new SessionStore(config.dataDir);
    this.app = Fastify({ logger: true, trustProxy: false });
    this.app.register(fastifyStatic, { root: join(fileURLToPath(new URL(".", import.meta.url)), "..", "public"), prefix: "/" });
    this.installRoutes();
    this.installWebSocket();
  }

  async listen(): Promise<void> {
    await this.app.listen({ host: this.config.host, port: this.config.port });
  }

  async close(): Promise<void> {
    for (const active of this.active.values()) await this.stopActive(active);
    this.active.clear();
    this.wss.close();
    await this.app.close();
  }

  private installRoutes(): void {
    this.app.addHook("onSend", async (_request, reply) => {
      reply.header("Cache-Control", "no-store");
      reply.header("X-Content-Type-Options", "nosniff");
      reply.header("Referrer-Policy", "no-referrer");
    });
    this.app.get("/healthz", async () => ({ ok: true }));
    this.app.post("/api/sessions", async () => this.store.create());
    this.app.get("/api/sessions/:id", async (request, reply) => {
      const session = this.authenticateHttp(request.params as { id: string }, request.headers);
      if (!session) return reply.code(401).send({ error: "Invalid session token." });
      return this.store.getPublic(session.id);
    });
    this.app.post("/api/sessions/:id/ticket", async (request, reply) => {
      const session = this.authenticateHttp(request.params as { id: string }, request.headers);
      if (!session) return reply.code(401).send({ error: "Invalid session token." });
      const ticket = randomBytes(32).toString("base64url");
      this.tickets.issue(ticket, session.id, Date.now(), TICKET_TTL_MS);
      return { ticket, expiresInMs: TICKET_TTL_MS };
    });
    this.app.post("/api/approvals/:id/decision", async (request, reply) => {
      const body = request.body as { sessionId?: string; decision?: "accept" | "decline" } | undefined;
      const session = body?.sessionId ? this.authenticateHttp({ id: body.sessionId }, request.headers) : null;
      if (!session) return reply.code(401).send({ error: "Invalid session token." });
      if (body?.decision !== "accept" && body?.decision !== "decline") return reply.code(400).send({ error: "Invalid approval decision." });
      const accepted = await this.decideApproval(session.id, String((request.params as { id: string }).id), body.decision);
      return accepted ? { ok: true } : reply.code(404).send({ error: "Approval is not active." });
    });
  }

  private installWebSocket(): void {
    this.app.server.on("upgrade", (request, socket, head) => {
      const url = new URL(request.url ?? "/", "http://localhost");
      if (url.pathname === "/api/bridges/wechat/live") {
        if (!bridgeAuthorized(request.headers, this.config.bridgeToken)) {
          socket.destroy();
          return;
        }
        this.wss.handleUpgrade(request, socket, head, ws => this.handleBridgeConnection(ws));
        return;
      }
      const match = url.pathname.match(/^\/api\/sessions\/([^/]+)\/live$/);
      if (!match || !this.config.allowedOrigins.includes(String(request.headers.origin ?? ""))) {
        socket.destroy();
        return;
      }
      this.wss.handleUpgrade(request, socket, head, ws => this.handleConnection(ws, decodeURIComponent(match[1])));
    });
  }

  private handleBridgeConnection(socket: WebSocket): void {
    let sessionId: string | null = null;
    let sequence = Promise.resolve();
    const timer = setTimeout(() => { if (!sessionId) socket.close(4401, "Start timed out"); }, 10_000);
    socket.on("message", (raw, binary) => {
      sequence = sequence.then(async () => {
        if (binary || rawLength(raw) > MAX_MESSAGE_BYTES) return socket.close(4400, "Invalid message");
        const message = parseBridgeMessage(raw);
        if (!message) return this.send(socket, { type: "session.error", message: "Invalid bridge message.", recoverable: true });
        if (!sessionId) {
          if (message.type !== "start" || !isConversationKey(message.conversationKey)) {
            return socket.close(4401, "Invalid bridge start");
          }
          if (message.voice !== undefined && !isRealtimeVoice(message.voice)) {
            return socket.close(4401, "Invalid bridge voice");
          }
          const voice = message.voice ?? "ember";
          const session = this.store.getOrCreateExternal(message.conversationKey);
          if (this.active.has(session.id)) return socket.close(4409, "Conversation already active");
          sessionId = session.id;
          clearTimeout(timer);
          const active: ActiveConnection = { socket, adapter: null, audioBridge: null, openingSent: false };
          this.active.set(sessionId, active);
          this.store.setStatus(sessionId, "connecting");
          active.audioBridge = new HeadlessWebRtcPeer(event => this.handleAdapterEvent(sessionId!, event));
          active.adapter = new CodexAppServer(this.config, sessionId, session.threadId, event => {
            if (!active.audioBridge?.handleCodexEvent(event)) this.handleAdapterEvent(sessionId!, event);
          }, voice);
          try {
            await active.audioBridge.start(active.adapter);
            const threadId = active.adapter.currentThreadId;
            if (threadId) this.store.setThread(sessionId, threadId);
            this.store.setStatus(sessionId, "active");
            this.store.audit(sessionId, "wechat-voice.started", `Realtime bridge started (${voice})`);
          } catch (error) {
            this.active.delete(sessionId);
            this.store.setStatus(sessionId, "error");
            await this.stopActive(active);
            throw error;
          }
          return;
        }
        await this.handleBridgeMessage(socket, sessionId, message);
      }).catch(error => this.send(socket, { type: "session.error", message: error instanceof Error ? error.message : String(error) }));
    });
    socket.once("close", () => {
      clearTimeout(timer);
      if (!sessionId) return;
      const active = this.active.get(sessionId);
      if (active?.socket === socket) {
        this.active.delete(sessionId);
        void this.stopActive(active);
        this.store.setStatus(sessionId, "idle");
      }
    });
  }

  private handleConnection(socket: WebSocket, sessionId: string): void {
    let authenticated = false;
    let sequence = Promise.resolve();
    const timer = setTimeout(() => { if (!authenticated) socket.close(4401, "Authentication timed out"); }, 5_000);
    socket.on("message", (raw, binary) => {
      sequence = sequence.then(async () => {
        if (binary || rawLength(raw) > MAX_MESSAGE_BYTES) return socket.close(4400, "Invalid message");
        const message = parseMessage(raw);
        if (!message) return this.send(socket, { type: "session.error", message: "Invalid message.", recoverable: true });
        if (!authenticated) {
          if (message.type !== "authenticate" || !this.consumeTicket(sessionId, message.ticket)) return socket.close(4401, "Authentication failed");
          authenticated = true;
          clearTimeout(timer);
          this.send(socket, { type: "ready", sessionId });
          return;
        }
        await this.handleBrowserMessage(socket, sessionId, message);
      }).catch(error => this.send(socket, { type: "session.error", message: error instanceof Error ? error.message : String(error) }));
    });
    socket.once("close", () => {
      clearTimeout(timer);
      const active = this.active.get(sessionId);
      if (active?.socket === socket) {
        this.active.delete(sessionId);
        void this.stopActive(active);
        this.store.setStatus(sessionId, "idle");
      }
    });
  }

  private async handleBrowserMessage(socket: WebSocket, sessionId: string, message: BrowserMessage): Promise<void> {
    if (message.type === "start") {
      if (Buffer.byteLength(message.sdp, "utf8") > MAX_SDP_BYTES || !message.sdp.trim()) throw new Error("Invalid WebRTC SDP offer.");
      if (this.active.has(sessionId)) throw new Error("Another browser owns this voice session.");
      const session = this.store.get(sessionId);
      if (!session) throw new Error("Voice session no longer exists.");
      const active: ActiveConnection = { socket, adapter: null, audioBridge: null, openingSent: false };
      this.active.set(sessionId, active);
      this.store.setStatus(sessionId, "connecting");
      active.adapter = new CodexAppServer(this.config, sessionId, session.threadId, event => this.handleAdapterEvent(sessionId, event));
      try {
        await active.adapter.start(message.sdp);
        const threadId = active.adapter.currentThreadId;
        if (threadId) this.store.setThread(sessionId, threadId);
        this.store.setStatus(sessionId, "active");
        this.store.audit(sessionId, "voice.started", "Realtime session started");
      } catch (error) {
        this.active.delete(sessionId);
        this.store.setStatus(sessionId, "error");
        await this.stopActive(active);
        throw error;
      }
      return;
    }
    const active = this.active.get(sessionId);
    if (message.type === "stop") {
      if (active) await this.stopActive(active);
      this.active.delete(sessionId);
      this.store.setStatus(sessionId, "idle");
      this.send(socket, { type: "session.closed", reason: "requested" });
      socket.close(1000, "Stopped");
      return;
    }
    if (!active?.adapter) throw new Error("Live Voice is not started.");
    if (message.type === "text") {
      if (!message.text.trim() || Buffer.byteLength(message.text, "utf8") > MAX_TEXT_BYTES) throw new Error("Invalid text input.");
      await active.adapter.appendText(message.text);
    } else if (message.type === "interrupt") {
      await active.adapter.interrupt();
    } else if (message.type === "approval.decision") {
      await this.decideApproval(sessionId, message.approvalId, message.decision);
    }
  }

  private async handleBridgeMessage(socket: WebSocket, sessionId: string, message: VoiceBridgeMessage): Promise<void> {
    const active = this.active.get(sessionId);
    if (message.type === "stop") {
      if (active) await this.stopActive(active);
      this.active.delete(sessionId);
      this.store.setStatus(sessionId, "idle");
      this.send(socket, { type: "session.closed", reason: "requested" });
      socket.close(1000, "Stopped");
      return;
    }
    if (!active?.adapter) throw new Error("Realtime bridge is not started.");
    if (message.type === "audio") {
      active.audioBridge?.appendAudio(message.audio);
    } else if (message.type === "opening") {
      if (active.openingSent) throw new Error("The call opening was already sent.");
      const openingRequest = normalizeCallOpeningRequest(message.text);
      if (!openingRequest) throw new Error("The call opening is empty.");
      active.openingSent = true;
      try {
        await active.adapter.appendText(callOpeningUserMessage(openingRequest));
        this.store.audit(sessionId, "wechat-voice.opening", "queued");
      } catch (error) {
        active.openingSent = false;
        this.store.audit(sessionId, "wechat-voice.opening", "failed");
        throw error;
      }
    } else if (message.type === "interrupt") {
      await active.adapter.interrupt();
    }
  }

  private handleAdapterEvent(sessionId: string, event: GatewayEvent): void {
    const active = this.active.get(sessionId);
    if (!active) return;
    if (event.type === "approval.requested") this.store.audit(sessionId, `approval.${event.approval.kind}`, "requested");
    if (event.type === "session.error") this.store.audit(sessionId, "voice.error", event.message);
    this.send(active.socket, event);
  }

  private async decideApproval(sessionId: string, approvalId: string, decision: "accept" | "decline"): Promise<boolean> {
    const accepted = await this.active.get(sessionId)?.adapter?.decideApproval(approvalId, decision) ?? false;
    if (accepted) this.store.approvalAudit(sessionId, "command", decision);
    return accepted;
  }

  private async stopActive(active: ActiveConnection): Promise<void> {
    active.audioBridge?.stop();
    await active.adapter?.stop();
  }

  private authenticateHttp(params: { id: string }, headers: Record<string, unknown>) {
    return this.store.authenticate(params.id, browserSecret(headers));
  }

  private consumeTicket(sessionId: string, ticket: string): boolean {
    return this.tickets.consume(ticket, sessionId);
  }

  private send(socket: WebSocket, event: GatewayEvent): void {
    if (socket.readyState === WebSocket.OPEN) socket.send(JSON.stringify(event));
  }
}
