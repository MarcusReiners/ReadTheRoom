#!/usr/bin/env bash
cd "$(dirname "$0")/.." || exit 1
KEY=$(sed -n 's/^[[:space:]]*GEMINI_API_KEY[[:space:]]*=[[:space:]]*//p' .env | tr -d "\"'[:space:]")
if [ -z "$KEY" ]; then
  echo "No GEMINI_API_KEY found in .env"
  exit 1
fi
API="https://generativelanguage.googleapis.com/v1beta"

if [ "$1" = "--list" ]; then
  curl -s --max-time 20 -H "x-goog-api-key: $KEY" "$API/models?pageSize=200" \
    | grep -o '"name": *"models/[^"]*"' | sed 's/.*models\///; s/"$//'
  exit 0
fi

MODEL=${1:-gemini-3.5-flash-lite}
TRIES=${2:-5}
BODY='{"contents":[{"role":"user","parts":[{"text":"How is it going?"}]}]}'
echo "Model $MODEL, $TRIES tries per address family, 45 s limit each"
for fam in 4 6; do
  for i in $(seq "$TRIES"); do
    curl -"$fam" -s -o /dev/null --max-time 45 \
      -H "x-goog-api-key: $KEY" -H "Content-Type: application/json" -d "$BODY" \
      -w "IPv$fam try $i: HTTP %{http_code}  connected %{time_connect}s  first byte %{time_starttransfer}s  done %{time_total}s  %{errormsg}\n" \
      "$API/models/$MODEL:streamGenerateContent?alt=sse"
  done
done
