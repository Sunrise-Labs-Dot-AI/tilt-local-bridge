// An in-process pretend bridge for the simulator and the test suite. It runs
// the real wire protocol, chunk framing included, so the app above the seam
// cannot tell the difference.

import nacl from 'tweetnacl';

import { bytesToHex, hexToBytes, utf8Decode, utf8Encode } from '../protocol/bytes';
import { canonicalBytes, type Json } from '../protocol/canonical';
import { MessageAssembler, chunkMessage } from '../protocol/framing';
import { SIGNING_PREFIX } from '../protocol/signing';
import { concatBytes } from '../protocol/bytes';
import type { BridgeLink, BridgeTransport, DiscoveredBridge, RadioState } from './types';

interface MockShade {
  id: string;
  name: string;
  position: number;
  battery: number;
  available: boolean;
  target: number | null;
}

export interface MockBridgeOptions {
  name?: string;
  bridgeId?: string;
  approveAfterMs?: number;
  writes?: boolean;
  chunkSize?: number;
  /** Percent of travel per tick while a shade moves. */
  stepPerTick?: number;
  tickMs?: number;
}

function randomHex(bytes: number): string {
  const out = new Uint8Array(bytes);
  for (let i = 0; i < bytes; i += 1) out[i] = Math.floor(Math.random() * 256);
  return bytesToHex(out);
}

export class MockBridge {
  readonly name: string;
  readonly bridgeId: string;
  readonly paired = new Set<string>();
  pending: { publicKey: string; name: string; code: string } | null = null;
  readonly shades: MockShade[] = [
    { id: 'door', name: 'Door', position: 100, battery: 91, available: true, target: null },
    { id: 'window', name: 'Window', position: 40, battery: 68, available: true, target: null },
  ];
  private readonly options: Required<MockBridgeOptions>;
  private readonly sessions = new Map<string, { publicKey: string | null; nonce: string | null; send: (payload: Uint8Array) => void }>();
  private ticker: ReturnType<typeof setInterval> | null = null;

  constructor(options: MockBridgeOptions = {}) {
    this.options = {
      name: options.name ?? 'Pretend Bridge',
      bridgeId: options.bridgeId ?? '0123456789abcdef0123456789abcdef',
      approveAfterMs: options.approveAfterMs ?? 6000,
      writes: options.writes ?? true,
      chunkSize: options.chunkSize ?? 20,
      stepPerTick: options.stepPerTick ?? 4,
      tickMs: options.tickMs ?? 1000,
    };
    this.name = this.options.name;
    this.bridgeId = this.options.bridgeId;
  }

  info(): string {
    return utf8Decode(canonicalBytes({ v: 1, bridge_id: this.bridgeId, name: this.name }));
  }

  attach(connection: string, send: (payload: Uint8Array) => void): void {
    this.sessions.set(connection, { publicKey: null, nonce: null, send });
  }

  detach(connection: string): void {
    this.sessions.delete(connection);
  }

  approve(): void {
    const pending = this.pending;
    if (!pending) return;
    this.pending = null;
    this.paired.add(pending.publicKey);
    for (const session of this.sessions.values()) {
      if (session.publicKey === pending.publicKey) {
        this.reply(session, { t: 'pair', status: 'approved' });
      }
    }
  }

  receive(connection: string, payload: Uint8Array): void {
    const session = this.sessions.get(connection);
    if (!session) return;
    let request: Record<string, Json>;
    try {
      request = JSON.parse(utf8Decode(payload)) as Record<string, Json>;
    } catch {
      this.reply(session, { t: 'error', code: 'bad_request', message: 'not json' });
      return;
    }
    const publicKey = typeof request.cpk === 'string' ? request.cpk : null;
    if (!publicKey) {
      this.reply(session, { t: 'error', code: 'bad_request', message: 'missing key' });
      return;
    }
    if (request.t === 'nonce') {
      session.publicKey = publicKey;
      session.nonce = randomHex(16);
      this.reply(session, {
        t: 'nonce', v: 1, bridge_id: this.bridgeId, name: this.name, n: session.nonce, paired: this.paired.has(publicKey),
      });
      return;
    }
    if (session.publicKey !== publicKey || request.n !== session.nonce || !session.nonce) {
      session.nonce = randomHex(16);
      this.reply(session, { t: 'error', code: 'nonce', message: 'stale nonce', n: session.nonce });
      return;
    }
    const { sig, ...unsigned } = request;
    const message = concatBytes([SIGNING_PREFIX, canonicalBytes(unsigned)]);
    if (typeof sig !== 'string' || !nacl.sign.detached.verify(message, hexToBytes(sig), hexToBytes(publicKey))) {
      this.reply(session, { t: 'error', code: 'signature', message: 'bad signature' });
      return;
    }
    session.nonce = randomHex(16);
    const n = session.nonce;
    const paired = this.paired.has(publicKey);
    switch (request.t) {
      case 'pair': {
        if (paired) {
          this.reply(session, { t: 'pair', status: 'approved', n });
        } else if (this.pending && this.pending.publicKey !== publicKey) {
          this.reply(session, { t: 'error', code: 'busy', message: 'Another phone is waiting for approval.', retry_in: 30, n });
        } else {
          if (!this.pending) {
            this.pending = { publicKey, name: String(request.name ?? 'Phone'), code: String(Math.floor(Math.random() * 1e6)).padStart(6, '0') };
            setTimeout(() => this.approve(), this.options.approveAfterMs);
          }
          this.reply(session, { t: 'pair', status: 'pending', code: this.pending.code, expires_in: 120, n });
        }
        return;
      }
      case 'pair_status':
        this.reply(session, { t: 'pair', status: paired ? 'approved' : this.pending?.publicKey === publicKey ? 'pending' : 'none', n });
        return;
      default:
        break;
    }
    if (!paired) {
      this.reply(session, { t: 'error', code: 'unauthorized', message: 'This phone is not paired.', paired: false, n });
      return;
    }
    switch (request.t) {
      case 'status':
        this.reply(session, { ...this.statusMessage(), n });
        return;
      case 'refresh':
        this.reply(session, { t: 'refresh', accepted: true, n });
        setTimeout(() => this.pushStatus(), 300);
        return;
      case 'set': {
        const shade = this.shades.find((item) => item.id === request.shade);
        const position = request.position;
        if (!shade) {
          this.reply(session, { t: 'error', code: 'unknown_shade', message: 'No configured shade has that id.', n });
        } else if (typeof position !== 'number' || !Number.isInteger(position) || position < 0 || position > 100) {
          this.reply(session, { t: 'error', code: 'invalid_position', message: 'Position must be 0 to 100.', n });
        } else if (!this.options.writes) {
          this.reply(session, { t: 'error', code: 'writes_disabled', message: 'Position writes are disabled on this bridge.', n });
        } else if (!shade.available) {
          this.reply(session, { t: 'error', code: 'not_available', message: 'The shade is not reachable right now.', n });
        } else {
          shade.target = position === shade.position ? null : position;
          this.reply(session, { t: 'set', shade: shade.id, position, outcome: 'accepted', n });
          this.ensureTicker();
          setTimeout(() => this.pushStatus(), 200);
        }
        return;
      }
      default:
        this.reply(session, { t: 'error', code: 'bad_request', message: 'Unknown request type.', n });
    }
  }

  private ensureTicker(): void {
    if (this.ticker) return;
    this.ticker = setInterval(() => {
      let moving = false;
      for (const shade of this.shades) {
        if (shade.target === null) continue;
        const delta = shade.target - shade.position;
        const step = Math.sign(delta) * Math.min(Math.abs(delta), this.options.stepPerTick);
        shade.position += step;
        if (shade.position === shade.target) shade.target = null;
        else moving = true;
      }
      this.pushStatus();
      if (!moving && this.ticker) {
        clearInterval(this.ticker);
        this.ticker = null;
      }
    }, this.options.tickMs);
  }

  private statusMessage(): Record<string, Json> {
    return {
      t: 'status',
      writes: this.options.writes,
      shades: this.shades.map((shade) => ({
        id: shade.id,
        name: shade.name,
        position: shade.position,
        battery: shade.battery,
        available: shade.available,
        target: shade.target,
        age: 5,
      })),
    };
  }

  private pushStatus(): void {
    for (const session of this.sessions.values()) {
      if (session.publicKey && this.paired.has(session.publicKey)) {
        this.reply(session, this.statusMessage());
      }
    }
  }

  private reply(session: { publicKey: string | null; send: (payload: Uint8Array) => void }, message: Record<string, Json>): void {
    const withTo = session.publicKey ? { ...message, to: session.publicKey.slice(0, 8) } : message;
    const payload = utf8Encode(JSON.stringify(withTo));
    const chunks = chunkMessage(payload, this.options.chunkSize);
    // Deliver asynchronously, the way notifications arrive, but all of one
    // message's chunks inside a single timer callback. iOS does not keep
    // zero-delay timers in scheduling order, and a reordered chunk would be
    // dropped by the assembler the way a lost notification would.
    setTimeout(() => {
      for (const chunk of chunks) session.send(chunk);
    }, 0);
  }
}

class MockLink implements BridgeLink {
  private readonly assembler = new MessageAssembler();
  private readonly listeners = new Set<(payload: Uint8Array) => void>();
  private readonly disconnectListeners = new Set<(error: Error | null) => void>();
  private readonly connection = `mock-${Math.random().toString(36).slice(2)}`;

  constructor(private readonly mock: MockBridge, readonly bridge: DiscoveredBridge) {
    mock.attach(this.connection, (chunk) => {
      let message: Uint8Array | null;
      try {
        message = this.assembler.add(chunk);
      } catch {
        return;
      }
      if (message) for (const listener of this.listeners) listener(message);
    });
  }

  async readInfo(): Promise<string> {
    return this.mock.info();
  }

  async writeMessage(payload: Uint8Array): Promise<void> {
    // Round trip through chunking so the framing code is exercised both ways.
    const assembler = new MessageAssembler();
    let whole: Uint8Array | null = null;
    for (const chunk of chunkMessage(payload, 20)) whole = assembler.add(chunk);
    if (whole) this.mock.receive(this.connection, whole);
  }

  onMessage(listener: (payload: Uint8Array) => void): () => void {
    this.listeners.add(listener);
    return () => this.listeners.delete(listener);
  }

  onDisconnect(listener: (error: Error | null) => void): () => void {
    this.disconnectListeners.add(listener);
    return () => this.disconnectListeners.delete(listener);
  }

  async disconnect(): Promise<void> {
    this.mock.detach(this.connection);
    for (const listener of this.disconnectListeners) listener(null);
  }
}

export class MockTransport implements BridgeTransport {
  readonly bridge: MockBridge;
  private state: RadioState = 'ready';
  private readonly stateListeners = new Set<(state: RadioState) => void>();

  constructor(bridge?: MockBridge, private readonly scanDelayMs = 800) {
    this.bridge = bridge ?? new MockBridge();
  }

  setRadioState(state: RadioState): void {
    this.state = state;
    for (const listener of this.stateListeners) listener(state);
  }

  async radioState(): Promise<RadioState> {
    return this.state;
  }

  onRadioState(listener: (state: RadioState) => void): () => void {
    this.stateListeners.add(listener);
    listener(this.state);
    return () => this.stateListeners.delete(listener);
  }

  async requestPermissions(): Promise<boolean> {
    return true;
  }

  startScan(onFound: (bridge: DiscoveredBridge) => void): () => void {
    const handle = setTimeout(() => onFound({ id: 'mock-bridge', name: this.bridge.name }), this.scanDelayMs);
    return () => clearTimeout(handle);
  }

  async connect(bridge: DiscoveredBridge): Promise<BridgeLink> {
    return new MockLink(this.bridge, bridge);
  }
}
