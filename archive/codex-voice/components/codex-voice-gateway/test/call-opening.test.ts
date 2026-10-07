import { describe, expect, it } from "vitest";
import { callOpeningUserMessage, normalizeCallOpeningRequest } from "../src/call-opening.js";

describe("one-time WeChat call opening", () => {
  it("normalizes a bounded request without adding a channel persona", () => {
    const request = normalizeCallOpeningRequest("  Call the requested member and say dinner is ready  ");
    expect(request).toBe("Call the requested member and say dinner is ready");
    const message = callOpeningUserMessage(request!);
    expect(message).toContain("not a request to place another call");
    expect(message).toContain("matching the language or dialect");
    expect(message).toContain(request!);
    expect(message).not.toContain("ChannelBot");
    expect(message).not.toContain("persona");
  });

  it("rejects invalid or oversized requests", () => {
    expect(normalizeCallOpeningRequest(undefined)).toBeNull();
    expect(() => normalizeCallOpeningRequest({ text: "call" })).toThrow("must be text");
    expect(() => normalizeCallOpeningRequest("x".repeat(2_049))).toThrow("too long");
  });
});
