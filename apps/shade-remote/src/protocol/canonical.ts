// Canonical JSON, byte for byte the same as the bridge's Python encoder:
// sorted keys, no whitespace, integers only, UTF-8 with non-ASCII left raw.

import { utf8Encode } from './bytes';

export type Json = null | boolean | number | string | Json[] | { [key: string]: Json };

function encodeString(value: string): string {
  // JSON.stringify escapes exactly what Python's json.dumps(ensure_ascii=False)
  // escapes: quotes, backslashes, and control characters.
  return JSON.stringify(value);
}

function encodeValue(value: Json): string {
  if (value === null) return 'null';
  if (typeof value === 'boolean') return value ? 'true' : 'false';
  if (typeof value === 'number') {
    if (!Number.isInteger(value)) {
      throw new Error('Canonical JSON does not allow floating point numbers.');
    }
    return String(value);
  }
  if (typeof value === 'string') return encodeString(value);
  if (Array.isArray(value)) return `[${value.map(encodeValue).join(',')}]`;
  const keys = Object.keys(value).sort();
  const parts = keys.map((key) => {
    const item = value[key];
    if (item === undefined) {
      throw new Error('Canonical JSON does not allow undefined values.');
    }
    return `${encodeString(key)}:${encodeValue(item)}`;
  });
  return `{${parts.join(',')}}`;
}

export function canonicalJson(value: Json): string {
  return encodeValue(value);
}

export function canonicalBytes(value: Json): Uint8Array {
  return utf8Encode(canonicalJson(value));
}
