// Message shapes on the wire, with parsing that refuses anything malformed.

import type { Json } from './canonical';

export const PROTOCOL_VERSION = 1;
export const REMOTE_SERVICE_UUID = '4e9a0001-3b7c-4f2e-9d61-5c8a2f7b0e10';
export const REMOTE_REQUEST_UUID = '4e9a0002-3b7c-4f2e-9d61-5c8a2f7b0e10';
export const REMOTE_RESPONSE_UUID = '4e9a0003-3b7c-4f2e-9d61-5c8a2f7b0e10';
export const REMOTE_INFO_UUID = '4e9a0004-3b7c-4f2e-9d61-5c8a2f7b0e10';

export interface BridgeInfo {
  version: number;
  bridgeId: string;
  name: string;
}

export interface ShadeState {
  id: string;
  name: string;
  position: number | null;
  battery: number | null;
  available: boolean;
  target: number | null;
  age: number | null;
}

export type PairingStatus = 'pending' | 'approved' | 'denied' | 'expired' | 'none';

/** The one physical press that approves a request: a shade and a direction. */
export interface PairingChallenge {
  shade: string;
  name: string;
  direction: 'up' | 'down';
}

export type Reply =
  | { t: 'nonce'; n: string; bridgeId: string; name: string; paired: boolean; version: number }
  | { t: 'pair'; status: PairingStatus; code: string | null; expiresIn: number | null; challenge: PairingChallenge | null; n: string | null }
  | { t: 'status'; shades: ShadeState[]; writes: boolean; n: string | null }
  | { t: 'set'; shade: string; position: number; outcome: string; n: string | null }
  | { t: 'refresh'; accepted: boolean; n: string | null }
  | { t: 'error'; code: string; message: string; n: string | null; paired: boolean | null; retryIn: number | null };

function str(value: unknown): string | null {
  return typeof value === 'string' ? value : null;
}

function int(value: unknown): number | null {
  return typeof value === 'number' && Number.isInteger(value) ? value : null;
}

function bool(value: unknown): boolean | null {
  return typeof value === 'boolean' ? value : null;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

export function parseInfo(text: string): BridgeInfo {
  const raw: unknown = JSON.parse(text);
  if (!isRecord(raw)) throw new Error('Bridge info is not an object.');
  const version = int(raw.v);
  const bridgeId = str(raw.bridge_id);
  const name = str(raw.name);
  if (version === null || bridgeId === null || name === null) {
    throw new Error('Bridge info is incomplete.');
  }
  return { version, bridgeId, name };
}

function parseChallenge(raw: unknown): PairingChallenge | null {
  if (!isRecord(raw)) return null;
  const shade = str(raw.shade);
  const name = str(raw.name);
  const direction = str(raw.direction);
  if (shade === null || name === null || (direction !== 'up' && direction !== 'down')) return null;
  return { shade, name, direction };
}

function parseShade(raw: unknown): ShadeState | null {
  if (!isRecord(raw)) return null;
  const id = str(raw.id);
  const name = str(raw.name);
  const available = bool(raw.available);
  if (id === null || name === null || available === null) return null;
  return {
    id,
    name,
    position: int(raw.position),
    battery: int(raw.battery),
    available,
    target: int(raw.target),
    age: int(raw.age),
  };
}

/** Parse one reply or push. Returns null for anything that is not for us. */
export function parseReply(raw: unknown, publicKeyHex: string): Reply | null {
  if (!isRecord(raw)) return null;
  const to = str(raw.to);
  if (to !== null && to !== publicKeyHex.slice(0, 8)) return null;
  const n = str(raw.n);
  switch (raw.t) {
    case 'nonce': {
      const bridgeId = str(raw.bridge_id);
      const name = str(raw.name);
      const paired = bool(raw.paired);
      const version = int(raw.v);
      if (n === null || bridgeId === null || name === null || paired === null || version === null) return null;
      return { t: 'nonce', n, bridgeId, name, paired, version };
    }
    case 'pair': {
      const status = str(raw.status);
      if (status !== 'pending' && status !== 'approved' && status !== 'denied' && status !== 'expired' && status !== 'none') {
        return null;
      }
      return { t: 'pair', status, code: str(raw.code), expiresIn: int(raw.expires_in), challenge: parseChallenge(raw.challenge), n };
    }
    case 'status': {
      if (!Array.isArray(raw.shades)) return null;
      const shades = raw.shades.map(parseShade);
      if (shades.some((shade) => shade === null)) return null;
      return { t: 'status', shades: shades as ShadeState[], writes: bool(raw.writes) ?? false, n };
    }
    case 'set': {
      const shade = str(raw.shade);
      const position = int(raw.position);
      const outcome = str(raw.outcome);
      if (shade === null || position === null || outcome === null) return null;
      return { t: 'set', shade, position, outcome, n };
    }
    case 'refresh':
      return { t: 'refresh', accepted: bool(raw.accepted) ?? false, n };
    case 'error': {
      const code = str(raw.code);
      if (code === null) return null;
      return {
        t: 'error',
        code,
        message: str(raw.message) ?? 'The bridge refused the request.',
        n,
        paired: bool(raw.paired),
        retryIn: int(raw.retry_in),
      };
    }
    default:
      return null;
  }
}

export type RequestBody =
  | { t: 'pair'; name: string }
  | { t: 'pair_status' }
  | { t: 'status' }
  | { t: 'set'; shade: string; position: number }
  | { t: 'refresh' };

export function requestToJson(body: RequestBody): { [key: string]: Json } {
  return { ...body };
}
