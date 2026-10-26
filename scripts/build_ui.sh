#!/usr/bin/env bash
# Build the Redline web UI into packages/redline-core/src/redline_core/ui/static.
set -euo pipefail
cd "$(dirname "$0")/../frontend"
npm ci
npm run build
