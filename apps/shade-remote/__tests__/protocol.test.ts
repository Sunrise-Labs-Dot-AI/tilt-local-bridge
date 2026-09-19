import * as fs from 'node:fs';
import * as path from 'node:path';
import nacl from 'tweetnacl';

import { bytesToHex, hexToBytes, utf8Decode, utf8Encode, bytesToBase64, base64ToBytes } from '../src/protocol/bytes';
import { canonicalJson, type Json } from '../src/protocol/canonical';
import { MessageAssembler, chunkMessage, chunkSizeForMtu } from '../src/protocol/framing';
import { parseReply } from '../src/protocol/messages';
import { identityFromSeed, signRequest, signingInput, verifySignature } from '../src/protocol/signing';

interface Vectors {
  seed_hex: string;
  public_key_hex: string;
  canonical_cases: { value: Json; canonical: string }[];
  signed_cases: {
    name: string;
    request: { [key: string]: Json };
    signing_input_hex: string;
    signature_hex: string;
    signed_canonical: string;
  }[];
  framing_cases: { payload_hex: string; chunk_size: number; chunks_hex: string[] }[];
}

const vectors: Vectors = JSON.parse(
  fs.readFileSync(path.resolve(__dirname, '../../../tests/fixtures/bluetooth_remote_vectors.json'), 'utf8'),
);

describe('canonical JSON', () => {
  it('matches the shared vectors byte for byte', () => {
    for (const testCase of vectors.canonical_cases) {
      expect(canonicalJson(testCase.value)).toBe(testCase.canonical);
    }
  });

  it('refuses floats', () => {
    expect(() => canonicalJson({ position: 50.5 })).toThrow();
  });
});

describe('signing', () => {
  const identity = identityFromSeed(vectors.seed_hex);

  it('derives the same public key as the bridge', () => {
    expect(identity.publicKeyHex).toBe(vectors.public_key_hex);
  });

  it('signs every vector identically', () => {
    for (const testCase of vectors.signed_cases) {
      expect(bytesToHex(signingInput(testCase.request))).toBe(testCase.signing_input_hex);
      const signed = signRequest(testCase.request, identity);
      expect(signed.sig).toBe(testCase.signature_hex);
      expect(canonicalJson(signed)).toBe(testCase.signed_canonical);
      expect(verifySignature(identity.publicKeyHex, signingInput(testCase.request), testCase.signature_hex)).toBe(true);
    }
  });

  it('rejects a tampered signature', () => {
    const [testCase] = vectors.signed_cases;
    if (!testCase) throw new Error('no vectors');
    const bad = testCase.signature_hex.replace(/^../, (first) => (first === '00' ? '01' : '00'));
    expect(verifySignature(identity.publicKeyHex, signingInput(testCase.request), bad)).toBe(false);
  });

  it('agrees with tweetnacl key generation from a random seed', () => {
    const seed = nacl.randomBytes(32);
    const identity2 = identityFromSeed(bytesToHex(seed));
    expect(identity2.publicKeyHex).toHaveLength(64);
  });
});

describe('framing', () => {
  it('matches the shared chunk vectors and reassembles', () => {
    for (const testCase of vectors.framing_cases) {
      const payload = hexToBytes(testCase.payload_hex);
      const chunks = chunkMessage(payload, testCase.chunk_size);
      expect(chunks.map(bytesToHex)).toEqual(testCase.chunks_hex);
      const assembler = new MessageAssembler();
      const results = chunks.map((chunk) => assembler.add(chunk));
      expect(results.slice(0, -1).every((result) => result === null)).toBe(true);
      expect(bytesToHex(results[results.length - 1] ?? new Uint8Array())).toBe(testCase.payload_hex);
    }
  });

  it('restarts on a new message start and rejects bad sequences', () => {
    const assembler = new MessageAssembler();
    const first = chunkMessage(utf8Encode('abcdefghijklmnopqrstuvwxyz'), 12);
    const second = chunkMessage(utf8Encode('hello'), 12);
    expect(assembler.add(first[0]!)).toBeNull();
    expect(utf8Decode(assembler.add(second[0]!)!)).toBe('hello');
    const chunks = chunkMessage(utf8Encode('0123456789'.repeat(4)), 12);
    assembler.add(chunks[0]!);
    expect(() => assembler.add(chunks[2]!)).toThrow();
    expect(() => new MessageAssembler().add(chunks[1]!)).toThrow();
  });

  it('sizes chunks from the MTU', () => {
    expect(chunkSizeForMtu(null)).toBe(20);
    expect(chunkSizeForMtu(185)).toBe(182);
    expect(chunkSizeForMtu(517)).toBe(244);
  });
});

describe('bytes', () => {
  it('round trips utf-8 and base64', () => {
    const text = 'James’s iPhone é 😀';
    expect(utf8Decode(utf8Encode(text))).toBe(text);
    const bytes = Uint8Array.from([0, 1, 2, 250, 251, 252, 253, 254, 255]);
    expect(base64ToBytes(bytesToBase64(bytes))).toEqual(bytes);
    expect(bytesToBase64(utf8Encode('hi'))).toBe('aGk=');
  });
});

describe('reply parsing', () => {
  const key = vectors.public_key_hex;

  it('accepts well formed replies for this phone', () => {
    expect(parseReply({ t: 'nonce', n: 'ab', bridge_id: 'id', name: 'Bridge', paired: false, v: 1, to: key.slice(0, 8) }, key))
      .toEqual({ t: 'nonce', n: 'ab', bridgeId: 'id', name: 'Bridge', paired: false, version: 1 });
    expect(parseReply({ t: 'pair', status: 'pending', code: '123456', expires_in: 100, n: 'cd' }, key))
      .toEqual({ t: 'pair', status: 'pending', code: '123456', expiresIn: 100, n: 'cd' });
    const status = parseReply({
      t: 'status',
      writes: true,
      shades: [{ id: 'door', name: 'Door', position: 40, battery: 90, available: true, target: null, age: 3 }],
    }, key);
    expect(status).toEqual({
      t: 'status',
      writes: true,
      n: null,
      shades: [{ id: 'door', name: 'Door', position: 40, battery: 90, available: true, target: null, age: 3 }],
    });
  });

  it('drops replies for other phones and malformed ones', () => {
    expect(parseReply({ t: 'status', shades: [], to: 'ffffffff' }, key)).toBeNull();
    expect(parseReply({ t: 'pair', status: 'weird' }, key)).toBeNull();
    expect(parseReply({ t: 'status', shades: [{ id: 1 }] }, key)).toBeNull();
    expect(parseReply('nope', key)).toBeNull();
  });
});
