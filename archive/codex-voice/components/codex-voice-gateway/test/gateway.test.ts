import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import WebSocket from "ws";
import { VoiceGateway } from "../src/gateway.js";

describe("VoiceGateway HTTP API", () => {
  it("requires the session secret before exposing session state or tickets", async () => {
    const dir = mkdtempSync(join(tmpdir(), "voice-gateway-test-"));
    const gateway = new VoiceGateway({
      projectDir: dir,
      host: "127.0.0.1",
      port: 4317,
      allowedOrigins: ["http://127.0.0.1:4317"],
      codexHome: dir,
      dataDir: dir,
      codexBin: "codex",
    });
    try {
      const created = await gateway.app.inject({ method: "POST", url: "/api/sessions" });
      const data = created.json() as { session: { id: string }; secret: string };
      const path = `/api/sessions/${data.session.id}`;
      expect((await gateway.app.inject({ method: "GET", url: path })).statusCode).toBe(401);
      expect((await gateway.app.inject({ method: "POST", url: `${path}/ticket` })).statusCode).toBe(401);
      const headers = { "x-voice-session-token": data.secret };
      expect((await gateway.app.inject({ method: "GET", url: path, headers })).statusCode).toBe(200);
      const ticket = await gateway.app.inject({ method: "POST", url: `${path}/ticket`, headers });
      expect(ticket.statusCode).toBe(200);
      expect(ticket.json().ticket).toEqual(expect.any(String));
      expect((await gateway.app.inject({ method: "GET", url: "/" })).statusCode).toBe(200);
    } finally {
      await gateway.close();
      rmSync(dir, { recursive: true, force: true });
    }
  });

  it("rejects a cross-origin WebSocket before it can authenticate", async () => {
    const dir = mkdtempSync(join(tmpdir(), "voice-gateway-test-"));
    const gateway = new VoiceGateway({
      projectDir: dir,
      host: "127.0.0.1",
      port: 0,
      allowedOrigins: ["http://127.0.0.1:4317"],
      codexHome: dir,
      dataDir: dir,
      codexBin: "codex",
    });
    try {
      await gateway.listen();
      const created = await gateway.app.inject({ method: "POST", url: "/api/sessions" });
      const { session } = created.json() as { session: { id: string } };
      const address = gateway.app.server.address() as { port: number };
      const closed = await new Promise<boolean>(resolve => {
        const socket = new WebSocket(`ws://127.0.0.1:${address.port}/api/sessions/${session.id}/live`, {
          origin: "https://untrusted.example",
        });
        socket.once("error", () => resolve(true));
        socket.once("close", () => resolve(true));
        socket.once("open", () => resolve(false));
      });
      expect(closed).toBe(true);
    } finally {
      await gateway.close();
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
