# Google Home

Google Home should receive the Home Assistant cover entity, not the Position
number or Battery sensor. Home Assistant's Google Assistant integration supports
cover open, close, set position, and state reporting.

## Option A: Home Assistant Cloud

This is the shorter route. It requires a paid Home Assistant Cloud subscription
after the trial.

1. In Home Assistant, open **Settings**, **Home Assistant Cloud**.
2. Connect Google Assistant.
3. Open **Settings**, **Voice assistants**, then **Expose**.
4. Expose only each shade's cover entity to Google Assistant.
5. In Google Home, link the Home Assistant service and assign the shades to rooms.

The current [Home Assistant Google Assistant documentation](https://www.home-assistant.io/integrations/google_assistant/)
describes subscription status and setup.

## Option B: Manual Google Assistant integration

This route does not require a Home Assistant Cloud subscription. It does require:

- a public HTTPS Home Assistant URL with a valid certificate
- a project in the Google Home Developer Console
- Google Cloud HomeGraph API configuration
- Home Assistant YAML and service-account configuration

Follow Home Assistant's official
[manual Google Assistant setup](https://www.home-assistant.io/integrations/google_assistant/#manual-setup-if-you-dont-have-home-assistant-cloud)
from start to finish. Google changes its console flow, so duplicating those
screens here would age badly.

Use an allowlist so only the covers are exposed:

```yaml
google_assistant:
  project_id: YOUR_PROJECT_ID
  service_account: !include SERVICE_ACCOUNT.json
  report_state: true
  expose_by_default: false
  entity_config:
    cover.office_shade:
      name: Office Shade
      expose: true
      room: Office
```

Keep the service-account JSON out of git and backups that are not encrypted.

After linking the test service in Google Home, say "Hey Google, sync my devices"
or use the app's sync flow. Confirm percentage control with a small movement.
