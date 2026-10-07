import { createHash } from "node:crypto";

interface TicketRecord {
  sessionId: string;
  expiresAt: number;
}

export function digest(value: string): string {
  return createHash("sha256").update(value).digest("hex");
}

export class TicketRegistry {
  private readonly tickets = new Map<string, TicketRecord>();

  issue(ticket: string, sessionId: string, now = Date.now(), ttlMs = 60_000): void {
    this.prune(now);
    this.tickets.set(digest(ticket), { sessionId, expiresAt: now + ttlMs });
  }

  consume(ticket: string, sessionId: string, now = Date.now()): boolean {
    this.prune(now);
    const key = digest(ticket);
    const record = this.tickets.get(key);
    this.tickets.delete(key);
    return Boolean(record && record.sessionId === sessionId && record.expiresAt > now);
  }

  prune(now = Date.now()): void {
    for (const [key, ticket] of this.tickets) if (ticket.expiresAt <= now) this.tickets.delete(key);
  }
}
