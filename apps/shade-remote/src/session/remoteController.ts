// The app's whole state machine: radio, scan, connect, pair, control.
//
// Screens render snapshots of this and call its verbs. Nothing here touches
// React, so the flow is exercised in the test suite against the pretend
// bridge from start to a moved shade.

import type { PairingChallenge, PairingStatus, ShadeState } from '../protocol/messages';
import type { Identity } from '../protocol/signing';
import type { BridgeLink, BridgeTransport, DiscoveredBridge, RadioState } from '../transport/types';
import { BridgeError, BridgeSession } from './bridgeSession';
import { loadDeviceName, loadIdentity, loadKnownBridge, saveKnownBridge, type KnownBridge } from './identity';

export type Phase =
  | 'starting'
  | 'radio'
  | 'scanning'
  | 'connecting'
  | 'unpaired'
  | 'pairing'
  | 'ready'
  | 'disconnected';

export interface RemoteSnapshot {
  phase: Phase;
  radio: RadioState;
  bridgeName: string | null;
  bridgeId: string | null;
  knownBridge: KnownBridge | null;
  /** True when the bridge that answered is not the one this phone paired with before. */
  differentBridge: boolean;
  pairingCode: string | null;
  pairingExpiresAt: number | null;
  pairingOutcome: PairingStatus | null;
  /** Which shade button the bridge asked for; null when no shade was reachable. */
  pairingChallenge: PairingChallenge | null;
  shades: ShadeState[];
  writes: boolean;
  /** Positions this phone asked for, shown until the bridge reports arrival. */
  requested: Record<string, number>;
  shadeErrors: Record<string, string>;
  refreshing: boolean;
  message: string | null;
}

const RESCAN_DELAY_MS = 2000;
const PAIR_POLL_MS = 4000;

export interface ControllerOptions {
  transport: BridgeTransport;
  identity?: Identity;
  deviceName?: string;
  now?: () => number;
}

export class RemoteController {
  private snapshot: RemoteSnapshot = {
    phase: 'starting',
    radio: 'unknown',
    bridgeName: null,
    bridgeId: null,
    knownBridge: null,
    differentBridge: false,
    pairingCode: null,
    pairingExpiresAt: null,
    pairingOutcome: null,
    pairingChallenge: null,
    shades: [],
    writes: false,
    requested: {},
    shadeErrors: {},
    refreshing: false,
    message: null,
  };
  private readonly listeners = new Set<(snapshot: RemoteSnapshot) => void>();
  private readonly transport: BridgeTransport;
  private identity: Identity | null;
  private deviceName: string | null;
  private readonly now: () => number;
  private session: BridgeSession | null = null;
  private link: BridgeLink | null = null;
  private stopScan: (() => void) | null = null;
  private stopRadio: (() => void) | null = null;
  private timer: ReturnType<typeof setTimeout> | null = null;
  private pairPoll: ReturnType<typeof setTimeout> | null = null;
  private stopped = false;
  private generation = 0;

  constructor(options: ControllerOptions) {
    this.transport = options.transport;
    this.identity = options.identity ?? null;
    this.deviceName = options.deviceName ?? null;
    this.now = options.now ?? (() => Date.now());
  }

  subscribe(listener: (snapshot: RemoteSnapshot) => void): () => void {
    this.listeners.add(listener);
    listener(this.snapshot);
    return () => this.listeners.delete(listener);
  }

  get state(): RemoteSnapshot {
    return this.snapshot;
  }

  async start(): Promise<void> {
    this.stopped = false;
    if (!this.identity) this.identity = await loadIdentity();
    if (!this.deviceName) this.deviceName = await loadDeviceName();
    this.update({ knownBridge: await loadKnownBridge() });
    const granted = await this.transport.requestPermissions();
    if (!granted) {
      this.update({ phase: 'radio', radio: 'unauthorized' });
      return;
    }
    this.stopRadio = this.transport.onRadioState((radio) => {
      this.update({ radio });
      if (radio === 'ready') {
        if (this.snapshot.phase === 'radio' || this.snapshot.phase === 'starting') this.beginScan();
      } else if (radio !== 'unknown') {
        void this.dropConnection();
        this.update({ phase: 'radio' });
      }
    });
  }

  async stop(): Promise<void> {
    this.stopped = true;
    this.stopRadio?.();
    this.stopRadio = null;
    this.clearTimers();
    this.stopScan?.();
    this.stopScan = null;
    await this.dropConnection();
  }

  /** Called when the app comes back to the foreground. */
  resume(): void {
    if (this.stopped) return;
    if (this.snapshot.phase === 'disconnected') this.beginScan();
  }

  async pair(): Promise<void> {
    const session = this.session;
    if (!session || !this.deviceName) return;
    this.update({ phase: 'pairing', pairingOutcome: null, message: null });
    try {
      const reply = await session.pair(this.deviceName);
      this.applyPairing(reply.status, reply.code, reply.expiresIn, reply.challenge);
    } catch (error) {
      this.handlePairingError(error);
    }
  }

  async setPosition(shadeId: string, position: number): Promise<void> {
    const session = this.session;
    if (!session) return;
    this.update({
      requested: { ...this.snapshot.requested, [shadeId]: position },
      shadeErrors: withoutKey(this.snapshot.shadeErrors, shadeId),
    });
    try {
      await session.setPosition(shadeId, position);
    } catch (error) {
      this.update({
        requested: withoutKey(this.snapshot.requested, shadeId),
        shadeErrors: { ...this.snapshot.shadeErrors, [shadeId]: describeError(error) },
      });
    }
  }

  async refresh(): Promise<void> {
    const session = this.session;
    if (!session) return;
    this.update({ refreshing: true });
    try {
      await session.refresh();
      const update = await session.status();
      this.applyStatus(update.shades, update.writes);
    } catch (error) {
      this.update({ message: describeError(error) });
    } finally {
      this.update({ refreshing: false });
    }
  }

  async forgetBridge(): Promise<void> {
    await saveKnownBridge(null);
    this.update({ knownBridge: null, differentBridge: false });
  }

  async connectToDifferentBridge(): Promise<void> {
    if (!this.snapshot.bridgeId || !this.snapshot.bridgeName) return;
    await saveKnownBridge({ bridgeId: this.snapshot.bridgeId, name: this.snapshot.bridgeName });
    this.update({ knownBridge: { bridgeId: this.snapshot.bridgeId, name: this.snapshot.bridgeName }, differentBridge: false });
  }

  // ----- internals -----

  private update(patch: Partial<RemoteSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    for (const listener of this.listeners) {
      try {
        listener(this.snapshot);
      } catch (error) {
        console.warn('Remote listener failed', error);
      }
    }
  }

  private beginScan(): void {
    if (this.stopped) return;
    this.clearTimers();
    this.stopScan?.();
    this.update({ phase: 'scanning', message: null });
    const generation = ++this.generation;
    let chosen = false;
    this.stopScan = this.transport.startScan(
      (bridge) => {
        if (chosen || generation !== this.generation) return;
        chosen = true;
        this.stopScan?.();
        this.stopScan = null;
        void this.connect(bridge, generation);
      },
      (error) => {
        if (generation !== this.generation) return;
        this.update({ message: describeError(error) });
        this.scheduleRescan();
      },
    );
  }

  private async connect(bridge: DiscoveredBridge, generation: number): Promise<void> {
    const identity = this.identity;
    if (!identity) return;
    this.update({ phase: 'connecting', bridgeName: bridge.name });
    try {
      const link = await this.transport.connect(bridge);
      if (generation !== this.generation || this.stopped) {
        await link.disconnect();
        return;
      }
      this.link = link;
      link.onDisconnect(() => {
        if (generation !== this.generation) return;
        this.onLost();
      });
      const session = new BridgeSession(link, identity);
      this.session = session;
      session.onStatus((update) => this.applyStatus(update.shades, update.writes));
      session.onPairing((status) => this.applyPairing(status, this.snapshot.pairingCode, null));
      const hello = await session.handshake();
      const known = this.snapshot.knownBridge;
      this.update({
        bridgeName: hello.name,
        bridgeId: hello.bridgeId,
        differentBridge: known !== null && known.bridgeId !== hello.bridgeId,
      });
      if (hello.paired) {
        await this.enterReady();
      } else {
        this.update({ phase: 'unpaired', pairingCode: null });
      }
    } catch (error) {
      if (generation !== this.generation) return;
      this.update({ message: describeError(error) });
      this.onLost();
    }
  }

  private async enterReady(): Promise<void> {
    const session = this.session;
    if (!session) return;
    const update = await session.status();
    this.applyStatus(update.shades, update.writes);
    if (!this.snapshot.differentBridge && this.snapshot.bridgeId && this.snapshot.bridgeName) {
      const known = { bridgeId: this.snapshot.bridgeId, name: this.snapshot.bridgeName };
      await saveKnownBridge(known);
      this.update({ knownBridge: known });
    }
    this.update({ phase: 'ready', pairingCode: null, pairingChallenge: null, pairingOutcome: null, message: null });
  }

  private applyStatus(shades: ShadeState[], writes: boolean): void {
    const requested = { ...this.snapshot.requested };
    for (const shade of shades) {
      const asked = requested[shade.id];
      if (asked !== undefined && (shade.position === asked || (shade.target !== null && shade.target !== asked))) {
        delete requested[shade.id];
      }
    }
    this.update({ shades, writes, requested });
  }

  private applyPairing(
    status: PairingStatus,
    code: string | null,
    expiresIn: number | null,
    challenge: PairingChallenge | null = this.snapshot.pairingChallenge,
  ): void {
    if (status === 'pending') {
      this.update({
        phase: 'pairing',
        pairingCode: code,
        pairingChallenge: challenge,
        pairingExpiresAt: expiresIn !== null ? this.now() + expiresIn * 1000 : this.snapshot.pairingExpiresAt,
      });
      this.schedulePairPoll();
      return;
    }
    this.clearPairPoll();
    if (status === 'approved') {
      void this.enterReady().catch((error: unknown) => {
        this.update({ message: describeError(error) });
        this.onLost();
      });
      return;
    }
    this.update({ phase: 'unpaired', pairingCode: null, pairingChallenge: null, pairingOutcome: status });
  }

  private handlePairingError(error: unknown): void {
    if (error instanceof BridgeError && error.code === 'busy') {
      this.update({
        phase: 'unpaired',
        pairingCode: null,
        message: `Another phone is waiting for approval. Try again in ${error.retryIn ?? 30} seconds.`,
      });
      return;
    }
    this.update({ phase: 'unpaired', pairingCode: null, message: describeError(error) });
  }

  private schedulePairPoll(): void {
    this.clearPairPoll();
    this.pairPoll = setTimeout(async () => {
      const session = this.session;
      if (!session || this.snapshot.phase !== 'pairing') return;
      try {
        const status = await session.pairStatus();
        this.applyPairing(status, this.snapshot.pairingCode, null);
      } catch (error) {
        this.handlePairingError(error);
      }
    }, PAIR_POLL_MS);
  }

  private clearPairPoll(): void {
    if (this.pairPoll) clearTimeout(this.pairPoll);
    this.pairPoll = null;
  }

  private onLost(): void {
    void this.dropConnection();
    if (this.stopped) return;
    this.update({ phase: 'disconnected', pairingCode: null });
    this.scheduleRescan();
  }

  private scheduleRescan(): void {
    this.clearTimers();
    this.timer = setTimeout(() => this.beginScan(), RESCAN_DELAY_MS);
  }

  private clearTimers(): void {
    if (this.timer) clearTimeout(this.timer);
    this.timer = null;
    this.clearPairPoll();
  }

  private async dropConnection(): Promise<void> {
    this.generation += 1;
    const session = this.session;
    const link = this.link;
    this.session = null;
    this.link = null;
    if (session) await session.close().catch(() => undefined);
    else if (link) await link.disconnect().catch(() => undefined);
  }
}

function withoutKey<T>(record: Record<string, T>, key: string): Record<string, T> {
  const copy = { ...record };
  delete copy[key];
  return copy;
}

export function describeError(error: unknown): string {
  if (error instanceof BridgeError) {
    switch (error.code) {
      case 'writes_disabled':
        return 'This bridge is read-only, so it reports positions but will not move a shade.';
      case 'not_available':
        return 'The bridge cannot reach that shade right now.';
      case 'unauthorized':
        return 'This phone is no longer paired with the bridge.';
      case 'timeout':
        return 'The bridge did not answer. It may be busy with a shade; try again.';
      default:
        return error.message;
    }
  }
  if (error instanceof Error) return error.message;
  return String(error);
}
