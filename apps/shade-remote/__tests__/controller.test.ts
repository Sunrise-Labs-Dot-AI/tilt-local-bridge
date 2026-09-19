import { identityFromSeed } from '../src/protocol/signing';
import { RemoteController, type RemoteSnapshot } from '../src/session/remoteController';
import { MockBridge, MockTransport } from '../src/transport/mockTransport';

const SEED = '000102030405060708090a0b0c0d0e0f101112131415161718191a1b1c1d1e1f';

function waitFor(controller: RemoteController, predicate: (snapshot: RemoteSnapshot) => boolean, timeoutMs = 8000): Promise<RemoteSnapshot> {
  if (predicate(controller.state)) return Promise.resolve(controller.state);
  return new Promise((resolve, reject) => {
    let unsubscribe: (() => void) | null = null;
    let settled = false;
    const finish = () => {
      settled = true;
      clearTimeout(timer);
      // subscribe() calls the listener synchronously, so the unsubscribe
      // handle may not exist yet on the very first notification.
      if (unsubscribe) unsubscribe();
      else queueMicrotask(() => unsubscribe?.());
    };
    const timer = setTimeout(() => {
      finish();
      reject(new Error(`timed out waiting; phase=${controller.state.phase} message=${controller.state.message}`));
    }, timeoutMs);
    unsubscribe = controller.subscribe((snapshot) => {
      if (settled) return;
      if (predicate(snapshot)) {
        finish();
        resolve(snapshot);
      }
    });
  });
}

describe('RemoteController against the pretend bridge', () => {
  beforeEach(() => {
    // The keychain mock is module-wide; each flow starts from a fresh phone.
    (require('expo-secure-store') as { __reset: () => void }).__reset();
  });

  it('scans, connects, pairs after approval, and moves a shade', async () => {
    const bridge = new MockBridge({ approveAfterMs: 400, tickMs: 50, stepPerTick: 20 });
    const transport = new MockTransport(bridge, 10);
    const controller = new RemoteController({
      transport,
      identity: identityFromSeed(SEED),
      deviceName: 'Test Phone',
    });
    await controller.start();
    const unpaired = await waitFor(controller, (s) => s.phase === 'unpaired');
    expect(unpaired.bridgeName).toBe('Pretend Bridge');
    expect(unpaired.bridgeId).toBe(bridge.bridgeId);

    await controller.pair();
    const pairing = await waitFor(controller, (s) => s.phase === 'pairing' && s.pairingCode !== null);
    expect(pairing.pairingCode).toMatch(/^\d{6}$/);
    expect(bridge.pending?.name).toBe('Test Phone');

    const ready = await waitFor(controller, (s) => s.phase === 'ready');
    expect(ready.shades.map((shade) => shade.id)).toEqual(['door', 'window']);
    expect(ready.writes).toBe(true);
    expect(ready.knownBridge?.bridgeId).toBe(bridge.bridgeId);

    await controller.setPosition('door', 60);
    expect(controller.state.requested.door).toBe(60);
    const arrived = await waitFor(controller, (s) => s.shades.find((shade) => shade.id === 'door')?.position === 60);
    expect(arrived.requested.door).toBeUndefined();

    await controller.stop();
  });

  it('reports a read-only bridge without moving anything', async () => {
    const bridge = new MockBridge({ approveAfterMs: 100, writes: false });
    const controller = new RemoteController({
      transport: new MockTransport(bridge, 10),
      identity: identityFromSeed(SEED),
      deviceName: 'Test Phone',
    });
    bridge.paired.add(identityFromSeed(SEED).publicKeyHex);
    await controller.start();
    const ready = await waitFor(controller, (s) => s.phase === 'ready');
    expect(ready.writes).toBe(false);
    await controller.setPosition('door', 10);
    expect(controller.state.shadeErrors.door).toMatch(/read-only/);
    expect(bridge.shades[0]?.target).toBeNull();
    await controller.stop();
  });

  it('notices a different bridge than the one it paired with', async () => {
    const first = new MockBridge({ bridgeId: 'aa'.repeat(16), approveAfterMs: 50 });
    const identity = identityFromSeed(SEED);
    first.paired.add(identity.publicKeyHex);
    const controller = new RemoteController({ transport: new MockTransport(first, 10), identity, deviceName: 'P' });
    await controller.start();
    await waitFor(controller, (s) => s.phase === 'ready');
    await controller.stop();

    const second = new MockBridge({ bridgeId: 'bb'.repeat(16), approveAfterMs: 50 });
    second.paired.add(identity.publicKeyHex);
    const again = new RemoteController({ transport: new MockTransport(second, 10), identity, deviceName: 'P' });
    await again.start();
    const ready = await waitFor(again, (s) => s.phase === 'ready');
    expect(ready.differentBridge).toBe(true);
    expect(ready.knownBridge?.bridgeId).toBe('aa'.repeat(16));
    await again.connectToDifferentBridge();
    expect(again.state.knownBridge?.bridgeId).toBe('bb'.repeat(16));
    await again.stop();
  });

  it('goes back to scanning when the radio turns off', async () => {
    const transport = new MockTransport(new MockBridge(), 10);
    const controller = new RemoteController({ transport, identity: identityFromSeed(SEED), deviceName: 'P' });
    await controller.start();
    await waitFor(controller, (s) => s.phase === 'unpaired');
    transport.setRadioState('off');
    expect(controller.state.phase).toBe('radio');
    transport.setRadioState('ready');
    await waitFor(controller, (s) => s.phase === 'unpaired');
    await controller.stop();
  });
});
