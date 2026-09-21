// Chunk framing shared with the bridge. Header bit 7 marks the first chunk of
// a message, bit 6 the last, the low six bits count chunks; the first chunk
// carries the payload length as two big-endian bytes.

export const MAX_MESSAGE_BYTES = 4096;
export const MIN_CHUNK_BYTES = 8;
export const DEFAULT_CHUNK_BYTES = 20;
export const MAX_CHUNK_BYTES = 244;

const START = 0x80;
const END = 0x40;
const SEQUENCE_MASK = 0x3f;

export function chunkSizeForMtu(mtu: number | null | undefined): number {
  if (!mtu || mtu < 23) return DEFAULT_CHUNK_BYTES;
  return Math.max(MIN_CHUNK_BYTES, Math.min(MAX_CHUNK_BYTES, mtu - 3));
}

export function chunkMessage(payload: Uint8Array, chunkSize: number = DEFAULT_CHUNK_BYTES): Uint8Array[] {
  if (chunkSize < MIN_CHUNK_BYTES || chunkSize > MAX_CHUNK_BYTES) {
    throw new Error(`Chunk size must be between ${MIN_CHUNK_BYTES} and ${MAX_CHUNK_BYTES} bytes.`);
  }
  if (payload.length > MAX_MESSAGE_BYTES) {
    throw new Error('Remote message exceeds the maximum size.');
  }
  const body = new Uint8Array(payload.length + 2);
  body[0] = payload.length >> 8;
  body[1] = payload.length & 0xff;
  body.set(payload, 2);
  const capacity = chunkSize - 1;
  const chunks: Uint8Array[] = [];
  let offset = 0;
  let sequence = 0;
  for (;;) {
    const piece = body.subarray(offset, offset + capacity);
    offset += piece.length;
    let header = sequence & SEQUENCE_MASK;
    if (sequence === 0) header |= START;
    if (offset >= body.length) header |= END;
    const chunk = new Uint8Array(piece.length + 1);
    chunk[0] = header;
    chunk.set(piece, 1);
    chunks.push(chunk);
    if (offset >= body.length) return chunks;
    sequence += 1;
  }
}

export class MessageAssembler {
  private expectedSequence = 0;
  private expectedLength: number | null = null;
  private buffer: number[] = [];

  reset(): void {
    this.expectedSequence = 0;
    this.expectedLength = null;
    this.buffer = [];
  }

  add(chunk: Uint8Array): Uint8Array | null {
    if (chunk.length < 1) {
      this.reset();
      throw new Error('Remote chunk is empty.');
    }
    const header = chunk[0] ?? 0;
    const sequence = header & SEQUENCE_MASK;
    let piece = chunk.subarray(1);
    if (header & START) {
      this.reset();
      if (piece.length < 2) throw new Error('Remote message start is missing its length.');
      const length = ((piece[0] ?? 0) << 8) | (piece[1] ?? 0);
      if (length > MAX_MESSAGE_BYTES) throw new Error('Remote message exceeds the maximum size.');
      this.expectedLength = length;
      piece = piece.subarray(2);
    } else if (this.expectedLength === null) {
      throw new Error('Remote chunk arrived without a message start.');
    }
    if (sequence !== this.expectedSequence) {
      this.reset();
      throw new Error('Remote chunk sequence is not contiguous.');
    }
    this.expectedSequence = (sequence + 1) & SEQUENCE_MASK;
    for (const byte of piece) this.buffer.push(byte);
    if (this.buffer.length > (this.expectedLength ?? 0)) {
      this.reset();
      throw new Error('Remote message is longer than announced.');
    }
    if (header & END) {
      if (this.buffer.length !== this.expectedLength) {
        this.reset();
        throw new Error('Remote message ended before its announced length.');
      }
      const message = Uint8Array.from(this.buffer);
      this.reset();
      return message;
    }
    return null;
  }
}
