#!/bin/bash
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p state
touch state/conversation.log
exec tail -n 100 -f state/conversation.log
