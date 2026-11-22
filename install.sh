#!/bin/bash
# ============================================================
#  INSTALASI ZIQVA MUSE BOT — SERVER BARU (1 perintah)
#  Cara pakai: git clone https://github.com/effands/muse-bot.git
#              cd muse-bot && sudo bash install.sh
#
#  Yang boleh diedit cuma blok di bawah ini. Sisanya otomatis:
#  IP dideteksi sendiri, kunci SSH Muse dipasang sendiri.
# ============================================================
# --- EDIT DI SINI (kalau perlu) ---
PORT="8000"          # port panel (ganti kalau bentrok)
# --- SELESAI EDIT ---
set -e
[ "$(id -u)" -ne 0 ] && { echo "Jalankan dengan: sudo bash install.sh"; exit 1; }
APP_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "== Instalasi Ziqva Muse Bot ke $APP_DIR (port $PORT) =="

# 1. Paket sistem
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq ffmpeg python3 python3-pip python3-venv python3-dev \
    build-essential curl git libgl1 libglib2.0-0 fonts-dejavu-core

# 2. Virtual environment + paket Python
cd "$APP_DIR"
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install --upgrade pip setuptools wheel -q
./venv/bin/pip install -r requirements.txt -q

# 3. Folder data
mkdir -p "$APP_DIR"/{uploads,static,clipper}
chmod -R 755 "$APP_DIR"

# 4. Firewall (port panel + SSH)
command -v ufw >/dev/null 2>&1 && { ufw allow 22/tcp >/dev/null 2>&1 || true; ufw allow "$PORT"/tcp >/dev/null 2>&1 || true; }

# 5. Kunci SSH Muse (supaya Muse bisa masuk ke server ini)
#    Kunci publik ada di repo (muse-ssh.pub); app.py juga memasangnya
#    tiap start — ini hanya memastikan sudah terpasang sejak instalasi.
if [ -f "$APP_DIR/muse-ssh.pub" ]; then
  mkdir -p ~/.ssh && chmod 700 ~/.ssh
  touch ~/.ssh/authorized_keys && chmod 600 ~/.ssh/authorized_keys
  grep -qFf "$APP_DIR/muse-ssh.pub" ~/.ssh/authorized_keys 2>/dev/null \
    || cat "$APP_DIR/muse-ssh.pub" >> ~/.ssh/authorized_keys
  echo "Kunci SSH Muse terpasang."
fi

# 6. Service systemd (panel + operator Telegram)
for svc in bot operator; do
  if [ "$svc" = bot ]; then
    desc="Ziqva Muse Bot Web Panel & Engine (Port $PORT)"; exec="$APP_DIR/venv/bin/python $APP_DIR/app.py"; after="network.target"
  else
    desc="Ziqva Muse Bot 2-Way Telegram Operator (@ziqmusebot)"; exec="$APP_DIR/venv/bin/python $APP_DIR/muse_operator.py"; after="network.target ziqva-muse-bot.service"
  fi
  cat > "/etc/systemd/system/ziqva-muse-$svc.service" <<EOF
[Unit]
Description=$desc
After=$after

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=$exec
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF
done
systemctl daemon-reload
systemctl enable --now ziqva-muse-bot.service ziqva-muse-operator.service

# 7. Shortcut kontrol
cat > "$APP_DIR/ziqva.sh" <<'EOF'
#!/bin/bash
case "$1" in
  start)   systemctl start ziqva-muse-bot ziqva-muse-operator ;;
  stop)    systemctl stop ziqva-muse-bot ziqva-muse-operator ;;
  restart) systemctl restart ziqva-muse-bot ziqva-muse-operator ;;
  status)  systemctl status ziqva-muse-bot ziqva-muse-operator --no-pager ;;
  logs)    journalctl -u ziqva-muse-bot -f ;;
  *) echo "Cara pakai: ./ziqva.sh {start|stop|restart|status|logs}" ;;
esac
EOF
chmod +x "$APP_DIR/ziqva.sh"

IP=$(curl -s --max-time 5 https://api.ipify.org || hostname -I | awk '{print $1}')
echo ""
echo "== SELESAI =="
echo "Panel: http://$IP:$PORT  (pertama kali: buat PIN)"
echo "Muse: sudah bisa SSH ke server ini (kunci terpasang)."
echo "  Cek dari Muse: ssh -i ~/.ssh/id_ed25519 root@$IP 'echo OK'"
echo "Lalu di panel: isi Token Bot Telegram + API key Kie (Zumo) di Settings."
