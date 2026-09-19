# Shade Remote

The phone side of the Tilt Local Bridge [Bluetooth phone remote](../../docs/BLUETOOTH_REMOTE.md):
an Expo app that finds the bridge over Bluetooth Low Energy, pairs once after a
person approves it, and sets shade positions without the network.

```bash
npm ci
npm run typecheck
npm test
```

Bluetooth needs a development build rather than Expo Go:

```bash
npx expo prebuild --platform ios
npx expo run:ios --device
```

The simulator has no Bluetooth, so `EXPO_PUBLIC_MOCK_BRIDGE=1 npx expo start`
runs the app against an in-process pretend bridge that speaks the real wire
protocol. Cloud builds go through EAS with the account and project supplied
from the environment, never from this repository:

```bash
eas init
EXPO_OWNER=<account> EAS_PROJECT_ID=<id> eas build --profile preview --platform android
```

The protocol, the pairing flow, and the security model are documented with the
bridge in `docs/BLUETOOTH_REMOTE.md`. Both sides test against
`tests/fixtures/bluetooth_remote_vectors.json`.
