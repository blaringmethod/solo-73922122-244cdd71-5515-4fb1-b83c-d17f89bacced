#!/usr/bin/env bash
# Demo flow: upload volumes, query change intervals, swap/remove volumes, and
# walk a segmented response from missing -> complete -> missing again.
# Usage: scripts/demo_flow.sh [base_url]   (default http://127.0.0.1:8000)
set -euo pipefail
BASE="${1:-http://127.0.0.1:8000}"
URL="http://gazetteer.example.gov.cn/zcgg/notice.html"
SEG_URL="http://gazetteer.example.gov.cn/zcgg/segmented-notice.html"
SEG_ID="urn:uuid:5e6f7a8b-0001-4000-8000-000000000001"
DEMO_DIR="$(cd "$(dirname "$0")/.." && pwd)/demo"

step() { printf '\n\033[1m== %s ==\033[0m\n' "$*"; }
upload() { curl -sS -X PUT "$BASE/volumes/$1/warc" -F "file=@$DEMO_DIR/$2;type=application/warc"; }
show_segments() {
  curl -sS -G "$BASE/segments" --data-urlencode "record_id=$SEG_ID" \
    | jq '{status, reason, missing_segments, declared_total_length, assembled_length,
           segments: [.segments[] | {segment_number, present, volume, verification}]}'
}
show_changes() {
  curl -sS -G "$BASE/changes" --data-urlencode "url=$SEG_URL" \
    | jq '{changes, unevidenced,
           captures: [.captures[] | {ts, type, evidenced, segment_status}]}'
}

step "1. upload vol1.warc (t1 V1, t2 V2)"
upload vol1 vol1.warc | jq '{volume, records_indexed, responses_verified}'

step "2. upload vol2.warc (t3 revisit, t4 V3, t5 corrupt digest, t6 revisit)"
upload vol2 vol2.warc | jq '{volume, records_indexed, responses_verified, responses_digest_failed}'

step "3. BEFORE swap: between which captures did the bulletin body change?"
curl -sS -G "$BASE/changes" --data-urlencode "url=$URL" \
  | jq '{changes, unevidenced}'

step "4. replace vol1 with vol1b.warc (V2 body dropped)"
upload vol1 vol1b.warc | jq '{volume, records_indexed}'

step "5. AFTER swap: t3 revisit lost evidence, intervals recomputed"
curl -sS -G "$BASE/changes" --data-urlencode "url=$URL" \
  | jq '{changes, unevidenced}'

step "6. conflicting upload is rejected (409) and old data kept"
curl -sS -o /dev/null -w 'HTTP %{http_code}\n' -X PUT "$BASE/volumes/vol3/warc" \
  -F "file=@$DEMO_DIR/conflict.warc;type=application/warc"
curl -sS "$BASE/volumes" | jq '[.volumes[] | {volume, records, segments}]'

step "7. segmented bulletin: upload the FIRST segment only (missing tail)"
upload seg-head seg-head.warc | jq '{volume, segments, segmented_series_rechecked}'
show_segments
echo "-- /changes for the segmented URL (capture is unevidenced, revisit in seg-witness too):"
upload seg-witness seg-witness.warc | jq '{volume, records_indexed}'
show_changes

step "8. add the final continuation in another volume -> series verifies"
upload seg-tail seg-tail.warc | jq '{volume, segments, segmented_series_rechecked}'
show_segments
show_changes

step "9. remove the continuation volume again -> evidence withdrawn"
curl -sS -X DELETE "$BASE/volumes/seg-tail" | jq .
show_segments
show_changes
