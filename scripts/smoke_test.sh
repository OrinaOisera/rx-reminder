#!/usr/bin/env bash
# Live check: create a prescription, upload a photo, poll, confirm, send one
# test reminder, then fetch the calendar. API_URL and API_KEY come from the
# environment. Put your own JPEG at tests/fixtures/sample_prescription.jpg.
# Confirm creates real schedules and asks AWS to email the destination.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

: "${API_URL:?Set API_URL to the deployed API, without a trailing path beyond the stage}"
: "${API_KEY:?Set API_KEY to the demo key}"
: "${DESTINATION:?Set DESTINATION to the caregiver email, or an E.164 phone when CHANNEL=sms}"
CHANNEL="${CHANNEL:-email}"
API_URL="${API_URL%/}"
PHOTO="tests/fixtures/sample_prescription.jpg"

if [[ ! -f "$PHOTO" ]]; then
  echo "Add a JPEG prescription photo at tests/fixtures/sample_prescription.jpg, then run this script again." >&2
  exit 1
fi

auth=(-H "x-api-key: ${API_KEY}" -H "content-type: application/json")

call() {
  local method="$1"
  local path="$2"
  local body="${3:-}"
  if [[ -n "$body" ]]; then
    curl -sS -X "$method" "${API_URL}${path}" "${auth[@]}" -d "$body"
  else
    curl -sS -X "$method" "${API_URL}${path}" "${auth[@]}"
  fi
}

python3 - "$CHANNEL" "$DESTINATION" <<'PY'
import json, sys
channel, destination = sys.argv[1], sys.argv[2]
if channel not in {"email", "sms"}:
    sys.exit("CHANNEL must be email or sms")
if channel == "email" and "@" not in destination:
    sys.exit("DESTINATION must be an email address when CHANNEL is email")
if channel == "sms" and not (destination.startswith("+") and destination[1:].isdigit()):
    sys.exit("DESTINATION must look like +254712345678 when CHANNEL is sms")
PY

echo "Creating or reusing a family member..."
patients="$(call GET /patients)"
patient_id="$(python3 -c 'import json,sys; rows=json.load(sys.stdin).get("patients") or []; print(rows[0]["patientId"] if rows else "")' <<<"$patients")"
if [[ -z "$patient_id" ]]; then
  created="$(call POST /patients '{"name":"Me"}')"
  patient_id="$(python3 -c 'import json,sys; print(json.load(sys.stdin)["patientId"])' <<<"$created")"
fi
echo "Patient ${patient_id}"

echo "Creating a prescription..."
created="$(call POST /prescriptions "$(python3 -c 'import json,sys; print(json.dumps({"patientId": sys.argv[1], "contentType": "image/jpeg"}))' "$patient_id")")"
prescription_id="$(python3 -c 'import json,sys; data=json.load(sys.stdin); print(data.get("prescriptionId",""))' <<<"$created")"
upload_url="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("uploadUrl",""))' <<<"$created")"
if [[ -z "$prescription_id" || -z "$upload_url" ]]; then
  echo "$created" >&2
  exit 1
fi

echo "Uploading ${PHOTO}..."
curl -sS -o /tmp/rx-smoke-put.txt -w "%{http_code}" -X PUT "$upload_url" -H "Content-Type: image/jpeg" --data-binary @"$PHOTO" | grep -qx "200"

echo "Waiting for the reading..."
status=""
for _ in $(seq 1 45); do
  item="$(call GET "/prescriptions/${prescription_id}")"
  status="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("status",""))' <<<"$item")"
  if [[ "$status" == "PENDING_REVIEW" || "$status" == "FAILED" ]]; then
    break
  fi
  sleep 2
done
if [[ "$status" != "PENDING_REVIEW" ]]; then
  echo "$item" >&2
  exit 1
fi

echo "Previewing and confirming..."
payload="$(python3 -c 'import json,sys; from datetime import datetime, timedelta; from zoneinfo import ZoneInfo
item=json.loads(sys.argv[1]); meds=(item.get("extracted") or {}).get("medications") or []
if not meds: sys.exit("No medicines were read")
start=(datetime.now(ZoneInfo("Africa/Nairobi"))+timedelta(days=1)).replace(hour=8, minute=0, second=0, microsecond=0)
print(json.dumps({"medications": meds, "settings": {"timezone":"Africa/Nairobi","startDateTime": start.strftime("%Y-%m-%dT%H:%M"), "wakeTime":"07:00","sleepTime":"22:00"}}))' "$item")"
preview="$(call POST "/prescriptions/${prescription_id}/preview" "$payload")"
confirm_body="$(PAYLOAD="$payload" CHANNEL="$CHANNEL" DESTINATION="$DESTINATION" python3 -c 'import json,os,sys
preview=json.load(sys.stdin)
payload=json.loads(os.environ["PAYLOAD"])
settings=dict(payload["settings"])
settings["channel"]=os.environ["CHANNEL"]
settings["destination"]=os.environ["DESTINATION"]
if "error" in preview:
    sys.stderr.write(preview["error"] + "\n")
    sys.exit(1)
print(json.dumps({"acknowledged": True, "scheduleHash": preview["scheduleHash"], "medications": payload["medications"], "settings": settings}))' <<<"$preview")"
confirmed="$(call POST "/prescriptions/${prescription_id}/confirm" "$confirm_body")"
python3 -c 'import json,sys; data=json.load(sys.stdin); sys.exit(0 if data.get("doseCount") is not None else 1)' <<<"$confirmed"

echo "Sending one test reminder..."
call POST "/prescriptions/${prescription_id}/test-reminder" "{}" >/tmp/rx-smoke-test.json
python3 -c 'import json; data=json.load(open("/tmp/rx-smoke-test.json"));
import sys
if "error" in data: sys.stderr.write(data["error"]+"\n"); sys.exit(1)
print(data.get("message") or "sent")'

echo "Fetching the calendar..."
calendar="$(call GET "/prescriptions/${prescription_id}/calendar")"
ics_url="$(python3 -c 'import json,sys; print(json.load(sys.stdin).get("icsUrl",""))' <<<"$calendar")"
if [[ -z "$ics_url" ]]; then
  echo "$calendar" >&2
  exit 1
fi
curl -sS "$ics_url" | grep -q "BEGIN:VCALENDAR"
echo "Prescription ${prescription_id} is active."
echo "Cancel it with: curl -sS -X DELETE \"${API_URL}/prescriptions/${prescription_id}\" -H \"x-api-key: \$API_KEY\""
