import type { ExpoConfig } from 'expo/config';

import base from './app.json';

/**
 * app.json is the whole public configuration. The EAS account and project
 * that build it are not part of the project, so they come from the
 * environment of whoever runs `eas build`:
 *
 *   EXPO_OWNER=<account> EAS_PROJECT_ID=<id> eas build --profile preview
 *
 * Run `eas init` once to create a project under your own account.
 */
export default (): ExpoConfig => {
  const config = base.expo as ExpoConfig;
  const owner = process.env.EXPO_OWNER;
  const projectId = process.env.EAS_PROJECT_ID;
  return {
    ...config,
    ...(owner ? { owner } : {}),
    extra: {
      ...(config.extra ?? {}),
      ...(projectId ? { eas: { projectId } } : {}),
    },
  };
};
