#!/bin/bash
# ============================================================
#  INSTALASI ZIQVA MUSE BOT (AUTONOMOUS CLOUD VPS PRODUCTION)
#  Cara pakai di VPS (Ubuntu/Debian):
#      git clone https://github.com/effands/muse-bot.git
#      cd muse-bot
#      sudo bash install.sh
# ============================================================
set -e

echo "=========================================================="
echo "🚀 INSTALASI ZIQVA MUSE BOT — CLOUD PRODUCTION ENGINE 🚀"
echo "=========================================================="

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Jalankan sebagai root: sudo bash install.sh"
  exit 1
fi

# 1. Dependensi sistem (FFMPEG wajib untuk render kompilasi & visualizer)
echo "⚙️ [1/4] Menginstal FFMPEG dan dependensi sistem Linux..."
apt-get update -y
apt-get install -y ffmpeg python3-pip python3-venv curl git

# 2. Salin atau siapkan folder instalasi
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/root/ziqva-muse-bot"

echo "📁 [2/4] Menyiapkan direktori aplikasi di $INSTALL_DIR ..."
mkdir -p "$INSTALL_DIR"
cp -r "$SRC_DIR"/. "$INSTALL_DIR"/
rm -f "$INSTALL_DIR"/*.zip

cd "$INSTALL_DIR"

# 3. Virtual environment + library Python
echo "📦 [3/4] Menyiapkan virtual environment & library Python..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 4. Systemd service agar Web Panel (Port 8000) dan Operator Telegram jalan 24/7
echo "⚙️ [4/4] Mendaftarkan Service Otomatis 24/7 (systemd)..."

# Service 1: Web Panel (app.py)
cat > /etc/systemd/system/ziqva-muse-bot.service <<'EOF'
[Unit]
Description=Ziqva Muse Bot Web Panel & Engine
After=network.target

[Service]
User=root
WorkingDirectory=/root/ziqva-muse-bot
ExecStart=/root/ziqva-muse-bot/venv/bin/python /root/ziqva-muse-bot/app.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

# Service 2: Telegram Operator 2-way Bot (muse_operator.py)
cat > /etc/systemd/system/ziqva-muse-operator.service <<'EOF'
[Unit]
Description=Ziqva Muse Bot 2-Way Telegram Operator
After=network.target ziqva-muse-bot.service

[Service]
User=root
WorkingDirectory=/root/ziqva-muse-bot
ExecStart=/root/ziqva-muse-bot/venv/bin/python /root/ziqva-muse-bot/muse_operator.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable ziqva-muse-bot
systemctl restart ziqva-muse-bot

systemctl enable ziqva-muse-operator
systemctl restart ziqva-muse-operator

IP_ADDRESS=$(curl -s --max-time 10 ifconfig.me || hostname -I | awk '{print $1}')

echo "=========================================================="
echo "🎉 INSTALASI SELESAI & SISTEM AKTIF 24/7! 🎉"
echo "=========================================================="
echo "🌐 Akses Panel Web Cloud: 👉 http://$IP_ADDRESS:8000"
echo "🤖 Telegram Operator: @ziqmusebot aktif otomatis"
echo "📊 Cek status service:"
echo "   systemctl status ziqva-muse-bot"
echo "   systemctl status ziqva-muse-operator"
echo "=========================================================="
