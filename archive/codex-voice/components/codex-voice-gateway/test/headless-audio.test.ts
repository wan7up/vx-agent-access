import { describe, expect, it } from "vitest";
import { tcpOnlyIceCandidates } from "../src/headless-webrtc.js";
import { Pcm16Mono48kNormalizer } from "../src/pcm-normalizer.js";

function frame(sampleRate: number, samples: number[]): {
  samples: Int16Array;
  sampleRate: number;
  channelCount: number;
  numberOfFrames: number;
} {
  return {
    samples: Int16Array.from(samples),
    sampleRate,
    channelCount: 1,
    numberOfFrames: samples.length,
  };
}

describe("headless WebRTC transport", () => {
  it("keeps only TCP ICE candidates", () => {
    const sdp = [
      "v=0",
      "a=candidate:1 1 UDP 1 192.0.2.1 3478 typ host",
      "a=candidate:2 1 TCP 1 192.0.2.1 443 typ host tcptype passive",
      "a=end-of-candidates",
      "",
    ].join("\r\n");

    const filtered = tcpOnlyIceCandidates(sdp);
    expect(filtered).not.toContain(" UDP ");
    expect(filtered).toContain(" TCP ");
    expect(filtered).toContain("a=end-of-candidates");
  });
});

describe("Pcm16Mono48kNormalizer", () => {
  it("passes 48 kHz PCM through in exact 10 ms frames", () => {
    const normalizer = new Pcm16Mono48kNormalizer();
    const input = Array.from({ length: 480 }, (_, index) => index - 240);
    const output = normalizer.normalize(frame(48_000, input));
    expect(output).toHaveLength(1);
    expect(Array.from(output[0])).toEqual(input);
  });

  it("converts 16 kHz input to one continuous 48 kHz frame", () => {
    const normalizer = new Pcm16Mono48kNormalizer();
    const first = Array.from({ length: 160 }, (_, index) => index * 10);
    const second = Array.from({ length: 160 }, (_, index) => (index + 160) * 10);
    const firstOutput = normalizer.normalize(frame(16_000, first));
    const secondOutput = normalizer.normalize(frame(16_000, second));

    expect(firstOutput).toHaveLength(1);
    expect(secondOutput).toHaveLength(1);
    expect(firstOutput[0]).toHaveLength(480);
    expect(Array.from(secondOutput[0].slice(0, 6))).toEqual([1590, 1593, 1597, 1600, 1603, 1607]);
  });

  it("converts 24 kHz input to one 48 kHz frame", () => {
    const normalizer = new Pcm16Mono48kNormalizer();
    const input = Array.from({ length: 240 }, (_, index) => index * 8);
    const output = normalizer.normalize(frame(24_000, input));
    expect(output).toHaveLength(1);
    expect(output[0]).toHaveLength(480);
    expect(Array.from(output[0].slice(0, 8))).toEqual([0, 0, 0, 4, 8, 12, 16, 20]);
  });

  it("rejects unsupported formats without retaining stale output", () => {
    const normalizer = new Pcm16Mono48kNormalizer();
    expect(() => normalizer.normalize(frame(44_100, [1, 2, 3]))).toThrow("Unsupported remote audio sample rate");
    normalizer.reset();
    expect(normalizer.normalize(frame(48_000, Array(480).fill(7)))[0][0]).toBe(7);
  });
});
