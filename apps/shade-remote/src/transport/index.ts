import type { BridgeTransport } from './types';

/**
 * The simulator has no Bluetooth, so EXPO_PUBLIC_MOCK_BRIDGE=1 swaps in the
 * pretend bridge. The BLE module is required lazily so the mock path never
 * loads the native module.
 */
export function createTransport(): BridgeTransport {
  if (process.env.EXPO_PUBLIC_MOCK_BRIDGE === '1') {
    const { MockTransport } = require('./mockTransport') as typeof import('./mockTransport');
    return new MockTransport();
  }
  const { BleTransport } = require('./bleTransport') as typeof import('./bleTransport');
  return new BleTransport();
}
