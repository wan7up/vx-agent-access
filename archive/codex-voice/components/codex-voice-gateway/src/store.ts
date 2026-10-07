import { randomBytes, randomUUID } from "node:crypto";
import { mkdirSync } from "node:fs";
import { join } from "node:path";
import { DatabaseSync } from "node:sqlite";
import type { ApprovalKind, PublicSession, VoiceSession } from "./types.js";
import { digest } from "./security.js";

export class SessionStore {
  private readonly db: DatabaseSync;

  constructor(dataDir: string) {
    mkdirSync(dataDir, { recursive: true, mode: 0o700 });
    this.db = new DatabaseSync(join(dataDir, "gateway.sqlite"));
    this.db.exec(`
      PRAGMA journal_mode = WAL;
      CREATE TABLE IF NOT EXISTS sessions (
        id TEXT PRIMARY KEY,
        secret_hash TEXT NOT NULL,
        thread_id TEXT,
        status TEXT NOT NULL,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL,
        closed_at INTEGER
      ) STRICT;
      CREATE TABLE IF NOT EXISTS audit_events (
        id TEXT PRIMARY KEY,
        session_id TEXT NOT NULL,
        type TEXT NOT NULL,
        summary TEXT NOT NULL,
        created_at INTEGER NOT NULL
      ) STRICT;
      CREATE TABLE IF NOT EXISTS external_sessions (
        external_key TEXT PRIMARY KEY,
        session_id TEXT NOT NULL UNIQUE,
        created_at INTEGER NOT NULL,
        updated_at INTEGER NOT NULL
      ) STRICT;
    `);
  }

  create(): { session: PublicSession; secret: string } {
    const id = randomUUID();
    const secret = randomBytes(32).toString("base64url");
    const now = Date.now();
    this.db.prepare(
      "INSERT INTO sessions (id, secret_hash, thread_id, status, created_at, updated_at, closed_at) VALUES (?, ?, NULL, 'idle', ?, ?, NULL)",
    ).run(id, digest(secret), now, now);
    return { session: { id, threadId: null, status: "idle", createdAt: now, updatedAt: now }, secret };
  }

  getOrCreateExternal(externalKey: string): VoiceSession {
    const existing = this.db.prepare(
      "SELECT s.* FROM external_sessions e JOIN sessions s ON s.id = e.session_id WHERE e.external_key = ?",
    ).get(externalKey) as Record<string, unknown> | undefined;
    if (existing) {
      this.db.prepare("UPDATE external_sessions SET updated_at = ? WHERE external_key = ?").run(Date.now(), externalKey);
      return this.asSession(existing);
    }
    const created = this.create();
    const now = Date.now();
    this.db.prepare("INSERT INTO external_sessions (external_key, session_id, created_at, updated_at) VALUES (?, ?, ?, ?)")
      .run(externalKey, created.session.id, now, now);
    return this.get(created.session.id)!;
  }

  authenticate(id: string, secret: string | undefined): VoiceSession | null {
    if (!secret) return null;
    const row = this.db.prepare("SELECT * FROM sessions WHERE id = ? AND secret_hash = ?").get(id, digest(secret)) as Record<string, unknown> | undefined;
    return row ? this.asSession(row) : null;
  }

  get(id: string): VoiceSession | null {
    const row = this.db.prepare("SELECT * FROM sessions WHERE id = ?").get(id) as Record<string, unknown> | undefined;
    return row ? this.asSession(row) : null;
  }

  getPublic(id: string): PublicSession | null {
    const row = this.db.prepare("SELECT id, thread_id, status, created_at, updated_at FROM sessions WHERE id = ?").get(id) as Record<string, unknown> | undefined;
    if (!row) return null;
    const status = String(row.status);
    if (!["idle", "connecting", "active", "closed", "error"].includes(status)) return null;
    return {
      id: String(row.id),
      threadId: typeof row.thread_id === "string" ? row.thread_id : null,
      status: status as PublicSession["status"],
      createdAt: Number(row.created_at),
      updatedAt: Number(row.updated_at),
    };
  }

  setThread(id: string, threadId: string): void {
    this.db.prepare("UPDATE sessions SET thread_id = ?, updated_at = ? WHERE id = ?").run(threadId, Date.now(), id);
  }

  setStatus(id: string, status: PublicSession["status"]): void {
    this.db.prepare("UPDATE sessions SET status = ?, updated_at = ? WHERE id = ?").run(status, Date.now(), id);
  }

  close(id: string): void {
    const now = Date.now();
    this.db.prepare("UPDATE sessions SET status = 'closed', updated_at = ?, closed_at = ? WHERE id = ?").run(now, now, id);
  }

  audit(sessionId: string, type: string, summary: string): void {
    this.db.prepare("INSERT INTO audit_events (id, session_id, type, summary, created_at) VALUES (?, ?, ?, ?, ?)")
      .run(randomUUID(), sessionId, type, summary.slice(0, 400), Date.now());
  }

  approvalAudit(sessionId: string, kind: ApprovalKind, decision: string): void {
    this.audit(sessionId, `approval.${kind}`, decision);
  }

  private asSession(row: Record<string, unknown>): VoiceSession {
    return {
      id: String(row.id),
      secretHash: String(row.secret_hash),
      threadId: typeof row.thread_id === "string" ? row.thread_id : null,
      createdAt: Number(row.created_at),
      updatedAt: Number(row.updated_at),
      closedAt: typeof row.closed_at === "number" ? row.closed_at : null,
    };
  }
}
