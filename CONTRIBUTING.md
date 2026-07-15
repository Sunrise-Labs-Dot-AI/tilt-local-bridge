# Contributing

Issues and focused pull requests are welcome.

Before submitting a change:

```bash
python -m unittest discover -s tests
python tools/check_public_tree.py
bash -n scripts/install.sh
```

Protocol changes need offline fixtures, strict length and command validation,
and a clear owned-device use case. Never commit real pairing keys, credentials,
device addresses, private hostnames, private IPs, account emails, or unredacted
logs.

Do not add reset, calibration, firmware, or arbitrary-command features without
a separate safety design and explicit maintainer review.
