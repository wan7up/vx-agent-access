import { describe, expect, it } from "vitest";
import { TicketRegistry } from "../src/security.js";

describe("TicketRegistry", () => {
  it("accepts a ticket exactly once for its owning session", () => {
    const tickets = new TicketRegistry();
    tickets.issue("ticket-a", "session-a", 1_000);
    expect(tickets.consume("ticket-a", "session-a", 1_001)).toBe(true);
    expect(tickets.consume("ticket-a", "session-a", 1_002)).toBe(false);
  });

  it("rejects a ticket for another session or after expiry", () => {
    const tickets = new TicketRegistry();
    tickets.issue("ticket-b", "session-a", 1_000, 50);
    expect(tickets.consume("ticket-b", "session-b", 1_010)).toBe(false);
    tickets.issue("ticket-c", "session-a", 1_000, 50);
    expect(tickets.consume("ticket-c", "session-a", 1_050)).toBe(false);
  });
});
