#!/usr/bin/env bash
# End-to-end smoke test against a running server.
B=http://127.0.0.1:12000
CJ=/tmp/tqm_cookies.txt
rm -f "$CJ"

echo "== pages before login (expect 303) =="
for p in / /questions /channels /schedules /history /settings; do
  printf '%s -> %s\n' "$p" "$(curl -s -o /dev/null -w '%{http_code}' "$B$p")"
done

echo "== api before login (expect 401) =="
printf '/api/dashboard -> %s\n' "$(curl -s -o /dev/null -w '%{http_code}' "$B/api/dashboard")"

echo "== login =="
curl -s -c "$CJ" -o /dev/null -w "login=%{http_code}\n" -X POST "$B/login" \
  -d "username=admin&password=Admin12345!"

CSRF=$(python3 - <<'PY'
import sys
sys.path.insert(0, '/workspace/project')
from app.core.security import load_session
tok = [l.split('\t')[-1].strip() for l in open('/tmp/tqm_cookies.txt') if 'tqm_session' in l][0]
print(load_session(tok).csrf_token)
PY
)
echo "csrf=${CSRF:0:12}..."

echo "== pages after login (expect 200) =="
for p in / /questions /questions/new /channels /schedules /history /settings; do
  printf '%s -> %s\n' "$p" "$(curl -s -b "$CJ" -o /dev/null -w '%{http_code}' "$B$p")"
done

echo "== create questions =="
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$B/api/questions" \
  -d '{"question":"Which protocol uses port 53?","options":["HTTP","DNS","FTP","SSH"],"correct_option_id":1,"explanation":"DNS commonly uses port 53.","category":"Networking","difficulty":"easy","status":"ready"}' \
  -o /tmp/q1.json -w "create1=%{http_code}\n"
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$B/api/questions" \
  -d '{"question":"What is 2 + 2?","options":["3","4","5"],"correct_option_id":1,"category":"Math","difficulty":"easy","status":"ready"}' \
  -o /tmp/q2.json -w "create2=%{http_code}\n"
echo "ids: $(python3 -c "import json;print(json.load(open('/tmp/q1.json'))['id'], json.load(open('/tmp/q2.json'))['id'])")"

echo "== csrf rejection (expect 403) =="
printf 'no-csrf -> %s\n' "$(curl -s -b "$CJ" -H 'Content-Type: application/json' -X POST "$B/api/questions" -d '{"question":"x","options":["a","b"],"correct_option_id":0}' -o /dev/null -w '%{http_code}')"

echo "== create channel =="
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$B/api/channels" -d '{"name":"Main Channel","chat_id":"@main_channel","enabled":true}' \
  -o /tmp/ch.json -w "channel=%{http_code}\n"
CH=$(python3 -c "import json;print(json.load(open('/tmp/ch.json'))['id'])")
echo "channel id: $CH"

echo "== create schedule =="
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$B/api/schedules" \
  -d "{\"channel_id\":\"$CH\",\"type\":\"times\",\"times\":[\"08:00\",\"14:00\",\"20:00\"],\"timezone\":\"Asia/Riyadh\",\"question_selection\":\"sequential\",\"questions_per_day\":3,\"enabled\":true,\"name\":\"جدول رئيسي\"}" \
  -o /tmp/sch.json -w "schedule=%{http_code}\n"
SCH=$(python3 -c "import json;print(json.load(open('/tmp/sch.json'))['id'])")
echo "schedule id: $SCH"

echo "== publish now (dry-run) =="
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$B/api/publishing/publish-now" -d "{\"channel_id\":\"$CH\"}" -o /tmp/pub.json -w "publish=%{http_code}\n"
python3 -c "import json;d=json.load(open('/tmp/pub.json'));print('published:', d['ok'], d['entry']['question_id'], d['entry']['status'])"

echo "== publish a second question (each question once) =="
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X POST "$B/api/publishing/publish-now" -d "{\"channel_id\":\"$CH\"}" -o /tmp/pub2.json -w "publish2=%{http_code}\n"
python3 - <<'PY'
import json
entries = json.load(open('/tmp/pub.json'))['entry'], json.load(open('/tmp/pub2.json'))['entry']
ids = [e['question_id'] for e in entries]
assert len(set(ids)) == 2, f"a question was published twice: {ids}"
print('distinct questions published:', ids)
PY

echo "== republishing the same question is refused =="
QID=$(python3 -c "import json;print(json.load(open('/tmp/pub.json'))['entry']['question_id'])")
python3 - <<PY
import json, urllib.request, urllib.parse, http.cookiejar, sys
sys.path.insert(0, '/workspace/project')
from app.core.security import load_session
B = '$B'
cj = http.cookiejar.CookieJar()
op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj))
op.open(B + '/login', urllib.parse.urlencode({'username': 'admin', 'password': 'Admin12345!'}).encode())
csrf = load_session([c.value for c in cj if c.name == 'tqm_session'][0]).csrf_token
req = urllib.request.Request(
    B + '/api/publishing/publish-now',
    data=json.dumps({'channel_id': '$CH', 'question_id': '$QID'}).encode(),
    headers={'Content-Type': 'application/json', 'X-CSRF-Token': csrf},
    method='POST',
)
try:
    body = json.loads(op.open(req).read())
    print('refused:', body.get('ok') is False, '|', body.get('error', '')[:50])
except urllib.error.HTTPError as exc:
    print('refused with', exc.code, '|', json.loads(exc.read()).get('error', '')[:50])
PY

echo "== dashboard api =="
curl -s -b "$CJ" "$B/api/dashboard" -o /tmp/dash.json -w "dashboard=%{http_code}\n"
python3 -c "
import json;d=json.load(open('/tmp/dash.json'))
print('total:', d['questions']['total'], 'published:', d['questions']['published'])
print('publishing:', d['publishing'])
print('next runs:', d['next_runs'])
"

echo "== history =="
curl -s -b "$CJ" "$B/api/history" | python3 -c "
import json,sys;d=json.load(sys.stdin)
print('entries:', len(d['history']), 'stats:', d['stats'])
"

echo "== run schedule now =="
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -X POST "$B/api/schedules/$SCH/run-now" -o /tmp/run.json -w "run-now=%{http_code}\n"
python3 -c "import json;print('result:', json.load(open('/tmp/run.json')))"

echo "== backups =="
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -X POST "$B/api/backups/create" -o /dev/null -w "backup=%{http_code}\n"
curl -s -b "$CJ" "$B/api/backups" | python3 -c "import json,sys;print('backups:', len(json.load(sys.stdin)['backups']))"

echo "== settings update =="
curl -s -b "$CJ" -H "X-CSRF-Token: $CSRF" -H 'Content-Type: application/json' \
  -X PUT "$B/api/settings" -d '{"daily_limit":10,"timezone":"Asia/Riyadh"}' -o /dev/null -w "settings=%{http_code}\n"

echo "== data files on disk =="
ls -1 /workspace/project/data/*.json
