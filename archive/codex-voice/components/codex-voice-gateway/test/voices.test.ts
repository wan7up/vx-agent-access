import { describe, expect, it } from "vitest";
import { isRealtimeVoice, REALTIME_VOICES } from "../src/types.js";

describe("realtime voice validation", () => {
  it("accepts the voices reported by Codex 0.146.0", () => {
    expect(REALTIME_VOICES).toHaveLength(19);
    expect(REALTIME_VOICES).toContain("ember");
    expect(REALTIME_VOICES).toContain("spruce");
    expect(REALTIME_VOICES.every(isRealtimeVoice)).toBe(true);
  });

  it("rejects arbitrary bridge input", () => {
    expect(isRealtimeVoice("unknown")).toBe(false);
    expect(isRealtimeVoice(7)).toBe(false);
  });
});
