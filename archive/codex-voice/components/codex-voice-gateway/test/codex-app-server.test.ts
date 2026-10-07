import { describe, expect, it } from "vitest";
import { retryableAppServerBootError } from "../src/codex-app-server.js";

describe("Codex App Server startup recovery", () => {
  it("retries only the bounded initialize timeout", () => {
    expect(retryableAppServerBootError(
      new Error("Timed out waiting for Codex App Server response to initialize."),
    )).toBe(true);
    expect(retryableAppServerBootError(new Error("Authentication failed."))).toBe(false);
    expect(retryableAppServerBootError("initialize timeout")).toBe(false);
  });
});
