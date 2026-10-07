import { mkdtempSync, rmSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { describe, expect, it } from "vitest";
import { SessionStore } from "../src/store.js";

describe("SessionStore", () => {
  it("authenticates only with the random session secret and persists a thread mapping", () => {
    const dir = mkdtempSync(join(tmpdir(), "voice-gateway-test-"));
    try {
      const store = new SessionStore(dir);
      const { session, secret } = store.create();
      expect(store.authenticate(session.id, "not-the-secret")).toBeNull();
      expect(store.authenticate(session.id, secret)?.id).toBe(session.id);
      store.setThread(session.id, "thr_123");
      store.setStatus(session.id, "active");
      expect(store.getPublic(session.id)).toMatchObject({ id: session.id, threadId: "thr_123", status: "active" });
    } finally {
      rmSync(dir, { recursive: true, force: true });
    }
  });
});
