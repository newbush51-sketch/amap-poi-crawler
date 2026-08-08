# Security

## API keys

Pass keys only through `AMAP_KEYS` or `AMAP_KEY`. Never put a real key in
source code, examples, state files, command history screenshots, or issues.
The crawler does not write keys to its output files.

If a key is accidentally committed, revoke it in the AMap console immediately
and remove it from Git history before publishing.

## Reporting a vulnerability

Please open a GitHub security advisory instead of a public issue when the
report contains credentials or a reproducible security impact.

