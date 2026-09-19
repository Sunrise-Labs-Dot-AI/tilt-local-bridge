// jest-expo's generated expo-crypto mock returns empty output. Back it with
// node's crypto so key generation and hashing behave like the native module.
jest.mock('expo-crypto', () => {
  const { randomBytes, randomUUID } = require('node:crypto');
  return {
    getRandomBytes: (byteCount) => new Uint8Array(randomBytes(byteCount)),
    getRandomBytesAsync: async (byteCount) => new Uint8Array(randomBytes(byteCount)),
    randomUUID,
  };
});

jest.mock('expo-secure-store', () => {
  const values = new Map();
  return {
    getItemAsync: async (key) => (values.has(key) ? values.get(key) : null),
    setItemAsync: async (key, value) => {
      values.set(key, value);
    },
    deleteItemAsync: async (key) => {
      values.delete(key);
    },
    __reset: () => values.clear(),
  };
});

jest.mock('expo-device', () => ({ modelName: 'Test Phone', deviceName: null }));
