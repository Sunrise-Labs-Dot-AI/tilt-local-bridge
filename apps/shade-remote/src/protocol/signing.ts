// Ed25519 identity for the phone, on tweetnacl so it needs no native module.

import nacl from 'tweetnacl';

import { canonicalBytes, type Json } from './canonical';
import { concatBytes, hexToBytes, bytesToHex, utf8Encode } from './bytes';

export const SIGNING_PREFIX = utf8Encode('tilt-remote/1\n');

export interface Identity {
  readonly seedHex: string;
  readonly publicKeyHex: string;
  readonly secretKey: Uint8Array;
}

export function identityFromSeed(seedHex: string): Identity {
  const seed = hexToBytes(seedHex);
  if (seed.length !== 32) throw new Error('Ed25519 seeds must be exactly 32 bytes.');
  const pair = nacl.sign.keyPair.fromSeed(seed);
  return { seedHex, publicKeyHex: bytesToHex(pair.publicKey), secretKey: pair.secretKey };
}

export function signingInput(request: { [key: string]: Json }): Uint8Array {
  const unsigned: { [key: string]: Json } = {};
  for (const [key, value] of Object.entries(request)) {
    if (key !== 'sig') unsigned[key] = value;
  }
  return concatBytes([SIGNING_PREFIX, canonicalBytes(unsigned)]);
}

export function signRequest(request: { [key: string]: Json }, identity: Identity): { [key: string]: Json } {
  const signature = nacl.sign.detached(signingInput(request), identity.secretKey);
  return { ...request, sig: bytesToHex(signature) };
}

export function verifySignature(publicKeyHex: string, message: Uint8Array, signatureHex: string): boolean {
  try {
    return nacl.sign.detached.verify(message, hexToBytes(signatureHex), hexToBytes(publicKeyHex));
  } catch {
    return false;
  }
}
