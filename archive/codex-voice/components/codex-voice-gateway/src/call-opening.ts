export function normalizeCallOpeningRequest(value: unknown): string | null {
  if (value === undefined || value === null) return null;
  if (typeof value !== "string") throw new Error("Call opening request must be text.");
  const normalized = value.replace(/\s+/g, " ").trim();
  if (!normalized) return null;
  if (normalized.includes("\0") || Buffer.byteLength(normalized, "utf8") > 2_048) {
    throw new Error("Call opening request is too long or contains invalid text.");
  }
  return normalized;
}

export function callOpeningUserMessage(request: string): string {
  return [
    "The WeChat group voice call is now connected.",
    "The current-group request below is the caller's instruction for your first spoken utterance, not a request to place another call.",
    "Address the called person directly and say the requested content naturally now, matching the language or dialect used in the request.",
    "Do not explain these instructions.",
    `Current-group request: ${request}`,
  ].join(" ");
}
