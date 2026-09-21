// react-native-ble-plx behind the transport seam.

import { PermissionsAndroid, Platform } from 'react-native';
import { BleManager, State, type Device, type Subscription } from 'react-native-ble-plx';

import { base64ToBytes, bytesToBase64, utf8Decode } from '../protocol/bytes';
import { MessageAssembler, chunkMessage, chunkSizeForMtu } from '../protocol/framing';
import {
  REMOTE_INFO_UUID,
  REMOTE_REQUEST_UUID,
  REMOTE_RESPONSE_UUID,
  REMOTE_SERVICE_UUID,
} from '../protocol/messages';
import type { BridgeLink, BridgeTransport, DiscoveredBridge, RadioState } from './types';

function radioStateFrom(state: State): RadioState {
  switch (state) {
    case State.PoweredOn:
      return 'ready';
    case State.PoweredOff:
      return 'off';
    case State.Unauthorized:
      return 'unauthorized';
    case State.Unsupported:
      return 'unsupported';
    default:
      return 'unknown';
  }
}

class BleLink implements BridgeLink {
  private readonly assembler = new MessageAssembler();
  private readonly listeners = new Set<(payload: Uint8Array) => void>();
  private readonly disconnectListeners = new Set<(error: Error | null) => void>();
  private monitor: Subscription | null = null;
  private disconnectSubscription: Subscription | null = null;
  private writeQueue: Promise<void> = Promise.resolve();

  constructor(
    private readonly manager: BleManager,
    private readonly device: Device,
    readonly bridge: DiscoveredBridge,
  ) {}

  async start(): Promise<void> {
    this.disconnectSubscription = this.manager.onDeviceDisconnected(this.device.id, (error) => {
      this.teardown();
      for (const listener of this.disconnectListeners) listener(error ?? null);
    });
    this.monitor = this.device.monitorCharacteristicForService(
      REMOTE_SERVICE_UUID,
      REMOTE_RESPONSE_UUID,
      (error, characteristic) => {
        if (error || !characteristic?.value) return;
        let message: Uint8Array | null;
        try {
          message = this.assembler.add(base64ToBytes(characteristic.value));
        } catch {
          return;
        }
        if (message) for (const listener of this.listeners) listener(message);
      },
    );
  }

  async readInfo(): Promise<string> {
    const characteristic = await this.device.readCharacteristicForService(REMOTE_SERVICE_UUID, REMOTE_INFO_UUID);
    if (!characteristic.value) throw new Error('The bridge returned no information.');
    return utf8Decode(base64ToBytes(characteristic.value));
  }

  writeMessage(payload: Uint8Array): Promise<void> {
    const run = async () => {
      const chunks = chunkMessage(payload, chunkSizeForMtu(this.device.mtu));
      for (const chunk of chunks) {
        await this.device.writeCharacteristicWithResponseForService(
          REMOTE_SERVICE_UUID,
          REMOTE_REQUEST_UUID,
          bytesToBase64(chunk),
        );
      }
    };
    const next = this.writeQueue.then(run, run);
    this.writeQueue = next.catch(() => undefined);
    return next;
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
    this.teardown();
    try {
      await this.device.cancelConnection();
    } catch {
      // Already gone; nothing to release.
    }
  }

  private teardown(): void {
    this.monitor?.remove();
    this.monitor = null;
    this.disconnectSubscription?.remove();
    this.disconnectSubscription = null;
  }
}

export class BleTransport implements BridgeTransport {
  private readonly manager = new BleManager();

  radioState(): Promise<RadioState> {
    return this.manager.state().then(radioStateFrom);
  }

  onRadioState(listener: (state: RadioState) => void): () => void {
    const subscription = this.manager.onStateChange((state) => listener(radioStateFrom(state)), true);
    return () => subscription.remove();
  }

  async requestPermissions(): Promise<boolean> {
    if (Platform.OS !== 'android') return true;
    const version = typeof Platform.Version === 'number' ? Platform.Version : parseInt(String(Platform.Version), 10);
    const wanted = version >= 31
      ? [PermissionsAndroid.PERMISSIONS.BLUETOOTH_SCAN, PermissionsAndroid.PERMISSIONS.BLUETOOTH_CONNECT]
      : [PermissionsAndroid.PERMISSIONS.ACCESS_FINE_LOCATION];
    const results = await PermissionsAndroid.requestMultiple(wanted);
    return wanted.every((permission) => results[permission] === PermissionsAndroid.RESULTS.GRANTED);
  }

  startScan(onFound: (bridge: DiscoveredBridge) => void, onError: (error: Error) => void): () => void {
    this.manager.startDeviceScan([REMOTE_SERVICE_UUID], { allowDuplicates: false }, (error, device) => {
      if (error) {
        onError(error);
        return;
      }
      if (device) onFound({ id: device.id, name: device.localName ?? device.name ?? null });
    });
    return () => this.manager.stopDeviceScan();
  }

  async connect(bridge: DiscoveredBridge): Promise<BridgeLink> {
    let device = await this.manager.connectToDevice(bridge.id, { timeout: 15000 });
    if (Platform.OS === 'android') {
      try {
        device = await device.requestMTU(247);
      } catch {
        // Some stacks refuse; the default MTU still works, just in smaller chunks.
      }
    }
    device = await device.discoverAllServicesAndCharacteristics();
    const link = new BleLink(this.manager, device, bridge);
    await link.start();
    return link;
  }
}
