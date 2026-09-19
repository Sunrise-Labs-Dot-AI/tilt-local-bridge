// The phone's key and name, kept in the platform keychain.

import * as Crypto from 'expo-crypto';
import * as Device from 'expo-device';
import * as SecureStore from 'expo-secure-store';
import { Platform } from 'react-native';
import nacl from 'tweetnacl';

import { bytesToHex } from '../protocol/bytes';
import { identityFromSeed, type Identity } from '../protocol/signing';

const SEED_KEY = 'shade-remote.seed';
const NAME_KEY = 'shade-remote.name';
const BRIDGE_KEY = 'shade-remote.bridge';

nacl.setPRNG((target, length) => {
  const random = Crypto.getRandomBytes(length);
  for (let i = 0; i < length; i += 1) target[i] = random[i] ?? 0;
});

export async function loadIdentity(): Promise<Identity> {
  const existing = await SecureStore.getItemAsync(SEED_KEY);
  if (existing && /^[0-9a-f]{64}$/.test(existing)) return identityFromSeed(existing);
  const seed = bytesToHex(Crypto.getRandomBytes(32));
  await SecureStore.setItemAsync(SEED_KEY, seed);
  return identityFromSeed(seed);
}

export function defaultDeviceName(): string {
  const model = Device.modelName;
  if (model) return model;
  return Platform.OS === 'ios' ? 'iPhone' : 'Android phone';
}

export async function loadDeviceName(): Promise<string> {
  const stored = await SecureStore.getItemAsync(NAME_KEY);
  return stored && stored.trim() ? stored.trim().slice(0, 40) : defaultDeviceName();
}

export async function saveDeviceName(name: string): Promise<void> {
  const clean = name.trim().slice(0, 40);
  if (clean) await SecureStore.setItemAsync(NAME_KEY, clean);
  else await SecureStore.deleteItemAsync(NAME_KEY);
}

export interface KnownBridge {
  bridgeId: string;
  name: string;
}

export async function loadKnownBridge(): Promise<KnownBridge | null> {
  const stored = await SecureStore.getItemAsync(BRIDGE_KEY);
  if (!stored) return null;
  try {
    const parsed: unknown = JSON.parse(stored);
    if (
      typeof parsed === 'object' && parsed !== null
      && typeof (parsed as KnownBridge).bridgeId === 'string'
      && typeof (parsed as KnownBridge).name === 'string'
    ) {
      return parsed as KnownBridge;
    }
  } catch {
    // Fall through and forget the unreadable value.
  }
  await SecureStore.deleteItemAsync(BRIDGE_KEY);
  return null;
}

export async function saveKnownBridge(bridge: KnownBridge | null): Promise<void> {
  if (bridge) await SecureStore.setItemAsync(BRIDGE_KEY, JSON.stringify(bridge));
  else await SecureStore.deleteItemAsync(BRIDGE_KEY);
}
