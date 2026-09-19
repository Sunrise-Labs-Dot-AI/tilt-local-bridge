// The narrow seam between the app and Bluetooth. The BLE implementation and
// the in-process pretend bridge both satisfy it, so everything above this
// line is testable without a radio.

export interface DiscoveredBridge {
  /** Platform device identifier, stable for this phone only. */
  id: string;
  name: string | null;
}

export type RadioState = 'unknown' | 'off' | 'unauthorized' | 'unsupported' | 'ready';

export interface BridgeLink {
  readonly bridge: DiscoveredBridge;
  /** Raw text of the info characteristic. */
  readInfo(): Promise<string>;
  /** Write one whole message; the link chunks it for the negotiated MTU. */
  writeMessage(payload: Uint8Array): Promise<void>;
  onMessage(listener: (payload: Uint8Array) => void): () => void;
  onDisconnect(listener: (error: Error | null) => void): () => void;
  disconnect(): Promise<void>;
}

export interface BridgeTransport {
  radioState(): Promise<RadioState>;
  onRadioState(listener: (state: RadioState) => void): () => void;
  /** Ask the platform for scan and connect permission. Resolves false if refused. */
  requestPermissions(): Promise<boolean>;
  /** Start scanning; returns a function that stops the scan. */
  startScan(onFound: (bridge: DiscoveredBridge) => void, onError: (error: Error) => void): () => void;
  connect(bridge: DiscoveredBridge): Promise<BridgeLink>;
}
