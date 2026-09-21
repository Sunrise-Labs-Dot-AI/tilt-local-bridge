// One authenticated conversation with one connected bridge.
//
// Replies always carry the next nonce as `n`; pushes never do. That single
// rule is how a message is matched to the request in flight, so a status push
// arriving while a set is outstanding is routed to listeners rather than
// mistaken for the reply.

import { utf8Decode, utf8Encode } from '../protocol/bytes';
import { canonicalJson, type Json } from '../protocol/canonical';
import {
  parseInfo,
  parseReply,
  requestToJson,
  type BridgeInfo,
  type PairingStatus,
  type Reply,
  type RequestBody,
  type ShadeState,
} from '../protocol/messages';
import { signRequest, type Identity } from '../protocol/signing';
import type { BridgeLink } from '../transport/types';

export const REQUEST_TIMEOUT_MS = 12000;

export class BridgeError extends Error {
  constructor(readonly code: string, message: string, readonly retryIn: number | null = null) {
    super(message);
    this.name = 'BridgeError';
  }
}

export interface StatusUpdate {
  shades: ShadeState[];
  writes: boolean;
}

type Extract<T extends Reply['t']> = globalThis.Extract<Reply, { t: T }>;

export class BridgeSession {
  private nonce: string | null = null;
  private bridgeId: string | null = null;
  private bridgeName: string | null = null;
  private inFlight: { resolve: (reply: Reply) => void; reject: (error: Error) => void; timer: ReturnType<typeof setTimeout> } | null = null;
  private queue: Promise<unknown> = Promise.resolve();
  private readonly statusListeners = new Set<(update: StatusUpdate) => void>();
  private readonly pairingListeners = new Set<(status: PairingStatus) => void>();
  private readonly unsubscribe: () => void;
  private closed = false;

  constructor(private readonly link: BridgeLink, private readonly identity: Identity) {
    this.unsubscribe = link.onMessage((payload) => this.receive(payload));
  }

  get id(): string | null {
    return this.bridgeId;
  }

  get name(): string | null {
    return this.bridgeName;
  }

  onStatus(listener: (update: StatusUpdate) => void): () => void {
    this.statusListeners.add(listener);
    return () => this.statusListeners.delete(listener);
  }

  onPairing(listener: (status: PairingStatus) => void): () => void {
    this.pairingListeners.add(listener);
    return () => this.pairingListeners.delete(listener);
  }

  async readInfo(): Promise<BridgeInfo> {
    const info = parseInfo(await this.link.readInfo());
    this.bridgeId = info.bridgeId;
    this.bridgeName = info.name;
    return info;
  }

  /** Ask for a nonce; learns the bridge identity and whether we are paired. */
  async handshake(): Promise<Extract<'nonce'>> {
    const reply = await this.exchange({ t: 'nonce', cpk: this.identity.publicKeyHex });
    if (reply.t !== 'nonce') throw this.unexpected(reply);
    this.bridgeId = reply.bridgeId;
    this.bridgeName = reply.name;
    return reply;
  }

  async pair(deviceName: string): Promise<Extract<'pair'>> {
    const reply = await this.signed({ t: 'pair', name: deviceName });
    if (reply.t !== 'pair') throw this.unexpected(reply);
    return reply;
  }

  async pairStatus(): Promise<PairingStatus> {
    const reply = await this.signed({ t: 'pair_status' });
    if (reply.t !== 'pair') throw this.unexpected(reply);
    return reply.status;
  }

  async status(): Promise<StatusUpdate> {
    const reply = await this.signed({ t: 'status' });
    if (reply.t !== 'status') throw this.unexpected(reply);
    return { shades: reply.shades, writes: reply.writes };
  }

  async setPosition(shade: string, position: number): Promise<Extract<'set'>> {
    const reply = await this.signed({ t: 'set', shade, position });
    if (reply.t !== 'set') throw this.unexpected(reply);
    return reply;
  }

  async refresh(): Promise<void> {
    const reply = await this.signed({ t: 'refresh' });
    if (reply.t !== 'refresh') throw this.unexpected(reply);
  }

  async close(): Promise<void> {
    this.closed = true;
    this.unsubscribe();
    this.failInFlight(new BridgeError('closed', 'The connection was closed.'));
    await this.link.disconnect();
  }

  private async signed(body: RequestBody, retried = false): Promise<Reply> {
    if (!this.nonce || !this.bridgeId) await this.handshake();
    const request: { [key: string]: Json } = {
      ...requestToJson(body),
      cpk: this.identity.publicKeyHex,
      n: this.nonce as string,
      bridge_id: this.bridgeId as string,
    };
    const reply = await this.exchange(signRequest(request, this.identity));
    if (reply.t === 'error' && reply.code === 'nonce' && !retried) {
      // The bridge re-issued a nonce; one transparent retry covers a race
      // with a push or a reconnect on its side.
      return this.signed(body, true);
    }
    return reply;
  }

  private exchange(message: { [key: string]: Json }): Promise<Reply> {
    const run = () => new Promise<Reply>((resolve, reject) => {
      if (this.closed) {
        reject(new BridgeError('closed', 'The connection was closed.'));
        return;
      }
      const timer = setTimeout(() => {
        this.inFlight = null;
        reject(new BridgeError('timeout', 'The bridge did not answer in time.'));
      }, REQUEST_TIMEOUT_MS);
      this.inFlight = { resolve, reject, timer };
      this.link.writeMessage(utf8Encode(canonicalJson(message))).catch((error: unknown) => {
        clearTimeout(timer);
        this.inFlight = null;
        reject(error instanceof Error ? error : new Error(String(error)));
      });
    });
    const next = this.queue.then(run, run);
    this.queue = next.catch(() => undefined);
    return next;
  }

  private receive(payload: Uint8Array): void {
    let raw: unknown;
    try {
      raw = JSON.parse(utf8Decode(payload));
    } catch {
      return;
    }
    const reply = parseReply(raw, this.identity.publicKeyHex);
    if (!reply) return;
    if (reply.n) this.nonce = reply.n;
    const isReply = reply.n !== null || (reply.t === 'error' && this.inFlight !== null);
    if (isReply && this.inFlight) {
      const pending = this.inFlight;
      this.inFlight = null;
      clearTimeout(pending.timer);
      pending.resolve(reply);
      if (reply.t === 'status') this.emitStatus(reply);
      return;
    }
    if (reply.t === 'status') this.emitStatus(reply);
    if (reply.t === 'pair') for (const listener of this.pairingListeners) listener(reply.status);
  }

  private emitStatus(reply: Extract<'status'>): void {
    for (const listener of this.statusListeners) listener({ shades: reply.shades, writes: reply.writes });
  }

  private failInFlight(error: Error): void {
    const pending = this.inFlight;
    if (!pending) return;
    this.inFlight = null;
    clearTimeout(pending.timer);
    pending.reject(error);
  }

  private unexpected(reply: Reply): BridgeError {
    if (reply.t === 'error') return new BridgeError(reply.code, reply.message, reply.retryIn);
    return new BridgeError('unexpected', `The bridge answered with ${reply.t}.`);
  }
}
