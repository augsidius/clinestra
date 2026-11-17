#!/usr/bin/env bash
# Walk the Clinical API with dev headers. Start the server first: clinestra serve --dev
set -euo pipefail
API=${API:-http://127.0.0.1:8000}
DR=(-H 'X-Actor-Id: dr_rao' -H 'X-Actor-Role: clinician' -H 'X-Site: clinic_1' -H 'Content-Type: application/json')
NURSE=(-H 'X-Actor-Id: nurse_meena' -H 'X-Actor-Role: nurse' -H 'X-Site: clinic_1' -H 'Content-Type: application/json')
j() { python3 -c "import sys,json; d=json.load(sys.stdin); print($1)"; }

PID=$(curl -s "$API/v1/patients" "${DR[@]}" -d '{"name":"Asha K","country":"IN","gender":"female","age_years":34}' | j 'd["id"]')
echo "patient: $PID"
curl -s "$API/v1/conditions" "${DR[@]}" -d "{\"patient_id\":\"$PID\",\"code\":\"htn\"}" > /dev/null
PLAN=$(curl -s "$API/v1/careplans" "${DR[@]}" -d "{\"patient_id\":\"$PID\",\"definition_key\":\"hypertension_basic\"}" | j 'd["id"]')
echo "careplan: $PLAN"
curl -s "$API/v1/careplans/$PLAN/transition" "${DR[@]}" -d '{"to":"active","reason":"Agreed with patient"}' > /dev/null
echo "nurse tries to record a diagnosis:"
curl -s -o /dev/null -w '  HTTP %{http_code}\n' "$API/v1/conditions" "${NURSE[@]}" -d "{\"patient_id\":\"$PID\",\"code\":\"ckd\"}"
curl -s "$API/v1/assessments" "${DR[@]}" -d "{\"patient_id\":\"$PID\",\"definition_key\":\"bmi\",\"answers\":{\"weight\":58,\"height\":160}}" | j '"BMI: "+str(d["score"])'
curl -s "$API/v1/patients/$PID/pending-actions" "${DR[@]}" | j '"pending actions: "+str(len(d))'
curl -s "$API/v1/patients/$PID/timeline/verify" "${DR[@]}" | j '"timeline valid: "+str(d["valid"])'
