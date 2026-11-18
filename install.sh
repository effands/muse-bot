#!/bin/bash
# ============================================================
#  INSTALASI PABRIK KONTEN — FRESH INSTALL (dari awal)
#  Cara pakai: upload & extract zip ke VPS, lalu jalankan:
#      sudo bash install.sh
#  (jalankan dari dalam folder hasil extract)
# ============================================================
set -e

echo "========================================"
echo "🚀 INSTALASI PABRIK KONTEN 🚀"
echo "========================================"

if [ "$(id -u)" -ne 0 ]; then
  echo "❌ Jalankan sebagai root: sudo bash install.sh"
  exit 1
fi

# 1. Dependensi sistem (FFMPEG wajib untuk render & overlay)
echo "⚙️ Menginstal FFMPEG dan dependensi sistem..."
apt-get update -y
apt-get install -y ffmpeg python3-pip python3-venv curl

# 2. Salin file ke folder instalasi
SRC_DIR="$(cd "$(dirname "$0")" && pwd)"
INSTALL_DIR="/root/pabrik-konten"

echo "📁 Menyalin file ke $INSTALL_DIR ..."
rm -rf "$INSTALL_DIR"
mkdir -p "$INSTALL_DIR"
cp -r "$SRC_DIR"/. "$INSTALL_DIR"/
rm -f "$INSTALL_DIR"/*.zip

cd "$INSTALL_DIR"

# 3. Virtual environment + library Python
echo "📦 Menginstal library Python (bisa beberapa menit)..."
python3 -m venv venv
./venv/bin/pip install --upgrade pip
./venv/bin/pip install -r requirements.txt

# 4. Systemd service agar jalan 24 jam nonstop (port 8000)
echo "⚙️ Menyiapkan service 24/7 (port 8000)..."
cat > /etc/systemd/system/pabrik-konten.service <<'EOF'
[Unit]
Description=Pabrik Konten Web Panel
After=network.target

[Service]
User=root
WorkingDirectory=/root/pabrik-konten
ExecStart=/root/pabrik-konten/venv/bin/python /root/pabrik-konten/app.py
Restart=always

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable pabrik-konten
systemctl restart pabrik-konten

IP_ADDRESS=$(curl -s --max-time 10 ifconfig.me || hostname -I | awk '{print $1}')

echo "========================================"
echo "🎉 INSTALASI SELESAI! 🎉"
echo "Pabrik Konten sudah menyala 24/7."
echo "Buka di browser: 👉 http://$IP_ADDRESS:8000"
echo "Buat PIN keamanan saat pertama kali dibuka."
echo "========================================"
