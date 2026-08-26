#!/bin/bash
# Nạp YT_CLIENTS vào Cloudflare Worker từ yt_clients.json
# Chạy SAU KHI đã dán 2 secret vào file đó.
cd "$(dirname "$0")" || exit 1
F=yt_clients.json
grep -q "DAN_SECRET" "$F" && { echo "❌ Còn chỗ chưa dán secret trong $F — mở ra dán rồi lưu đã."; exit 1; }
python3 -c "
import json,sys
d=json.load(open('$F'))
for x in d: x.pop('_ghi_chu', None)
assert all(x.get('id') and x.get('secret') for x in d), 'thiếu id hoặc secret'
print(json.dumps(d, separators=(',',':')))
" > /tmp/_ytc.txt || exit 1
echo "→ nạp $(python3 -c "import json;print(len(json.load(open('$F'))))") app vào YT_CLIENTS…"
npx wrangler secret put YT_CLIENTS < /tmp/_ytc.txt
rm -f /tmp/_ytc.txt
echo "✅ xong. Kiểm: npx wrangler secret list | grep YT_CLIENTS"
