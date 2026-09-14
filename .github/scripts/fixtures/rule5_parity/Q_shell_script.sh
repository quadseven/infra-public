# parity-case: Q - a standalone shell script, one bounded curl and one not
# canonical-rule5-lines: 6
#!/usr/bin/env bash
set -euo pipefail
curl -fsS --max-time 10 --connect-timeout 5 https://example.invalid/ok
curl -fsS https://example.invalid/unbounded
