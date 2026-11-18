#!/bin/bash
# ============================================================
#  ZIQVA MUSE BOT — updater latar (dipanggil POST /api/update_panel)
#  Dijalankan detached (nohup, sesi baru) supaya selamat dari
#  restart service panel.
#
#  1) git pull TANPA --force / reset --hard.
#     Bila pull gagal (perubahan lokal/konflik/jaringan) -> tulis
#     status gagal ke update_status.json dan BERHENTI (tidak restart).
#  2) Bila pull sukses -> bash ziqva.sh restart (panel + operator),
#     lalu kirim notifikasi Telegram (token dibaca dari config.json).
# ============================================================
cd "$(dirname "$0")" || exit 1

STATUS_FILE="update_status.json"
STARTED=$(date '+%Y-%m-%d %H:%M')

# Beri waktu request HTTP "update dimulai" kembali dulu sebelum
# service panel dimatikan oleh restart.
sleep 2

write_status() {
  # $1 = status (success|failed), $2 = output pull (boleh kosong)
  python3 - "$1" "$2" "$STARTED" <<'PYEOF'
import json, subprocess, sys
from datetime import datetime
st, out, started = sys.argv[1], sys.argv[2], sys.argv[3]
now = datetime.now().strftime('%Y-%m-%d %H:%M')
commit = None
try:
    commit = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                            capture_output=True, text=True, timeout=10).stdout.strip() or None
except Exception:
    pass
ver = None
try:
    with open('VERSION', encoding='utf-8') as f:
        ver = f.read().strip().split()[0]
except Exception:
    pass
data = {"status": st, "started_at": started, "finished_at": now,
        "version": ver, "commit": commit, "output": (out or '')[:2000]}
tmp = 'update_status.json.tmp'
with open(tmp, 'w', encoding='utf-8') as f:
    json.dump(data, f)
import os
os.replace(tmp, 'update_status.json')
PYEOF
}

notify_tg() {
  # $1 = pesan (Markdown). Gagal silent — jangan gagalkan update.
  python3 - "$1" <<'PYEOF'
import json, sys, urllib.request
msg = sys.argv[1]
try:
    with open('config.json', encoding='utf-8') as f:
        cfg = json.load(f)
    token = cfg.get('tg_token')
    chat = cfg.get('tg_chat_id')
    if not token or not chat:
        sys.exit(0)
    body = json.dumps({"chat_id": chat, "text": msg,
                       "parse_mode": "Markdown"}).encode('utf-8')
    req = urllib.request.Request(
        "https://api.telegram.org/bot%s/sendMessage" % token,
        data=body, headers={"Content-Type": "application/json"})
    urllib.request.urlopen(req, timeout=15).read()
except Exception:
    pass
PYEOF
}

# 🔒 BACKUP PRA-UPDATE: snapshot sebelum git pull (backup_core.py, stdlib).
# Gagal backup TIDAK menghentikan update — pesan OK/SKIP/GAGAL tercetak
# di update.log. Bila panel sudah membuat backup <10 menit lalu (hook
# update_panel_api), backup_core langsung skip (dedupe).
if [ -f backup_core.py ]; then
  python3 backup_core.py backup --trigger pre_update 2>&1 | tail -2
fi

PULL_OUT=$(git pull 2>&1)PULL_RC=$?
if [ $PULL_RC -ne 0 ]; then
  write_status "failed" "$PULL_OUT"
  notify_tg "❌ *Update panel gagal, Bos* 😅

\`git pull\` ditolak (kemungkinan ada perubahan lokal di VPS atau jaringan putus). Panel *TIDAK* di-restart — versi lama tetap jalan.

Cek output lengkap di Settings → UPDATE & VERSI, atau tanya Muse."
  exit 1
fi

# Restart kedua service. Urutan diatur DI DALAM ziqva.sh (operator dulu,
# panel terakhir, dua perintah systemctl terpisah) — JANGAN digabung:
# me-restart panel membunuh skrip ini (anak service panel) sebelum
# operator sempat di-restart. Status "running" yang tertinggal dipulihkan
# self-heal boot-time (v1.2.1) saat panel kembali naik.
bash ziqva.sh restart
RESTART_RC=$?
if [ $RESTART_RC -ne 0 ]; then
  write_status "failed" "$PULL_OUT

ziqva.sh restart gagal (exit $RESTART_RC)."
  notify_tg "⚠️ *Update panel: pull sukses, restart gagal, Bos* 😅

Kode baru sudah ditarik tapi service gagal restart. Cek konsol VPS (\`bash ziqva.sh status\`)."
  exit 1
fi

VER=$(head -n 1 VERSION 2>/dev/null | tr -d '[:space:]')
HASH=$(git rev-parse --short HEAD 2>/dev/null)
write_status "success" "$PULL_OUT"
notify_tg "✅ *Update panel selesai, Bos* 😎

Versi sekarang: \`v${VER:-?} (${HASH:-?})\`
Panel + operator sudah restart dan kembali online."
exit 0
