export type ApprovalKind = "command" | "file-change" | "permissions" | "mcp";

export interface VoiceSession {
  id: string;
  secretHash: string;
  threadId: string | null;
  createdAt: number;
  updatedAt: number;
  closedAt: number | null;
}

export interface PublicSession {
  id: string;
  threadId: string | null;
  status: "idle" | "connecting" | "active" | "closed" | "error";
  createdAt: number;
  updatedAt: number;
}

export interface PendingApproval {
  id: string;
  sessionId: string;
  kind: ApprovalKind;
  summary: string;
  expiresAt: number;
}

export type BrowserMessage =
  | { type: "authenticate"; ticket: string }
  | { type: "start"; sdp: string }
  | { type: "text"; text: string }
  | { type: "interrupt" }
  | { type: "stop" }
  | { type: "approval.decision"; approvalId: string; decision: "accept" | "decline" };

export type AudioChunk = {
  data: string;
  sampleRate: number;
  numChannels: number;
  samplesPerChannel?: number;
};

export const REALTIME_VOICES = [
  "juniper", "maple", "spruce", "ember", "vale", "breeze", "arbor", "sol", "cove",
  "alloy", "ash", "ballad", "coral", "echo", "sage", "shimmer", "verse", "marin", "cedar",
] as const;

export type RealtimeVoice = typeof REALTIME_VOICES[number];

export function isRealtimeVoice(value: unknown): value is RealtimeVoice {
  return typeof value === "string" && (REALTIME_VOICES as readonly string[]).includes(value);
}

export type VoiceBridgeMessage =
  | { type: "start"; conversationKey: string; voice?: string }
  | { type: "opening"; text: string }
  | { type: "audio"; audio: AudioChunk }
  | { type: "interrupt" }
  | { type: "stop" };

export type GatewayEvent =
  | { type: "ready"; sessionId: string }
  | { type: "session.sdp"; sdp: string }
  | { type: "session.started"; threadId: string }
  | { type: "session.closed"; reason?: string }
  | { type: "audio.delta"; audio: AudioChunk }
  | { type: "session.error"; message: string; recoverable?: boolean }
  | { type: "transcript.delta"; role: string; delta: string }
  | { type: "transcript.done"; role: string; text: string }
  | { type: "manager.status"; status: "thinking" | "working" | "done" }
  | { type: "message.final"; text: string }
  | { type: "approval.requested"; approval: PendingApproval }
  | { type: "approval.resolved"; approvalId: string; decision: "accept" | "decline" | "expired" };
