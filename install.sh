#!/bin/bash
# ============================================================
#  INSTALASI OTOMATIS ZIQVA MUSE BOT (PRODUCTION CLOUD VPS)
# ============================================================
set -e

# Warna output
GREEN='\033[0;32m'
CYAN='\033[0;36m'
YELLOW='\033[1;33m'
RED='\033[0;31m'
NC='\033[0m' # No Color

echo -e "${CYAN}==========================================================${NC}"
echo -e "${CYAN}🚀  MEMULAI INSTALASI ZIQVA MUSE BOT DI VPS CLOUD        🚀${NC}"
echo -e "${CYAN}==========================================================${NC}"

# 1. Pastikan dijalankan sebagai root / sudo
if [ "$(id -u)" -ne 0 ]; then
  echo -e "${RED}❌ Harap jalankan script ini dengan akses root:${NC}"
  echo -e "${YELLOW}   sudo bash install.sh${NC}"
  exit 1
fi

APP_DIR="$(cd "$(dirname "$0")" && pwd)"
echo -e "${GREEN}📁 Direktori Aplikasi:${NC} $APP_DIR"

# 2. Update repository & Install dependensi sistem Linux
echo -e "\n${CYAN}⚙️ [1/5] Menginstal FFMPEG, Python & pustaka visual sistem...${NC}"
export DEBIAN_FRONTEND=noninteractive
apt-get update -y -qq
apt-get install -y -qq \
    ffmpeg \
    python3 \
    python3-pip \
    python3-venv \
    python3-dev \
    build-essential \
    curl \
    git \
    libgl1 \
    libglib2.0-0 \
    fonts-dejavu-core

# 3. Setup Virtual Environment
echo -e "\n${CYAN}📦 [2/5] Menyiapkan Virtual Environment Python (venv)...${NC}"
cd "$APP_DIR"
if [ ! -d "venv" ]; then
    python3 -m venv venv
fi
./venv/bin/pip install --upgrade pip setuptools wheel -q
echo -e "${GREEN}📦 Menginstal paket Python dari requirements.txt... (mohon tunggu sebentar)${NC}"
./venv/bin/pip install -r requirements.txt -q

# 4. Inisialisasi folder data agar siap pakai
echo -e "\n${CYAN}🗂️ [3/5] Memeriksa dan membuat folder penyimpanan aset...${NC}"
mkdir -p "$APP_DIR/uploads"
mkdir -p "$APP_DIR/static"
mkdir -p "$APP_DIR/clipper"
chmod -R 755 "$APP_DIR"

# 5. Konfigurasi Firewall (UFW) jika terpasang
echo -e "\n${CYAN}🛡️ [4/5] Memastikan port 8000 dan SSH terbuka di firewall...${NC}"
if command -v ufw >/dev/null 2>&1; then
    ufw allow 22/tcp >/dev/null 2>&1 || true
    ufw allow 8000/tcp >/dev/null 2>&1 || true
    echo -e "${GREEN}✅ Port 8000 & 22 dibuka pada UFW firewall.${NC}"
fi

# 6. Mendaftarkan Service Otomatis Systemd (24/7 Auto-Start & Auto-Restart)
echo -e "\n${CYAN}⚙️ [5/5] Memasang Service Systemd 24/7...${NC}"

# Service Web Panel & Engine
cat > /etc/systemd/system/ziqva-muse-bot.service <<EOF
[Unit]
Description=Ziqva Muse Bot Web Panel & Engine (Port 8000)
After=network.target

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/app.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# Service Telegram 2-Way Operator Bot
cat > /etc/systemd/system/ziqva-muse-operator.service <<EOF
[Unit]
Description=Ziqva Muse Bot 2-Way Telegram Operator (@ziqmusebot)
After=network.target ziqva-muse-bot.service

[Service]
Type=simple
User=root
WorkingDirectory=$APP_DIR
ExecStart=$APP_DIR/venv/bin/python $APP_DIR/muse_operator.py
Restart=always
RestartSec=5
Environment=PYTHONUNBUFFERED=1

[Install]
WantedBy=multi-user.target
EOF

# Reload dan jalankan service
systemctl daemon-reload
systemctl enable ziqva-muse-bot.service
systemctl restart ziqva-muse-bot.service

systemctl enable ziqva-muse-operator.service
systemctl restart ziqva-muse-operator.service

# Buat shortcut ziqva.sh untuk memudahkan kontrol
cat > "$APP_DIR/ziqva.sh" << 'EOF'
#!/bin/bash
case "$1" in
  start)
    systemctl start ziqva-muse-bot ziqva-muse-operator
    echo "✅ Ziqva Muse Bot dinyalakan."
    ;;
  stop)
    systemctl stop ziqva-muse-bot ziqva-muse-operator
    echo "🛑 Ziqva Muse Bot dihentikan."
    ;;
  restart)
    systemctl restart ziqva-muse-bot ziqva-muse-operator
    echo "🔄 Ziqva Muse Bot di-restart."
    ;;
  status)
    echo "=== STATUS PANEL WEB ==="
    systemctl status ziqva-muse-bot --no-pager
    echo -e "\n=== STATUS TELEGRAM OPERATOR ==="
    systemctl status ziqva-muse-operator --no-pager
    ;;
  logs)
    journalctl -u ziqva-muse-bot -f
    ;;
  *)
    echo "Cara pakai: ./ziqva.sh {start|stop|restart|status|logs}"
    ;;
esac
EOF
chmod +x "$APP_DIR/ziqva.sh"

# Ambil IP Public VPS
PUBLIC_IP=$(curl -s --max-time 5 https://api.ipify.org || curl -s --max-time 5 ifconfig.me || hostname -I | awk '{print $1}')

echo -e "\n${GREEN}==========================================================${NC}"
echo -e "${GREEN}🎉  INSTALASI SELESAI & SISTEM AKTIF 24/7!               🎉${NC}"
echo -e "${GREEN}==========================================================${NC}"
echo -e "🌐 Buka Panel Ziqva Muse Bot di browser:"
echo -e "   👉 ${CYAN}http://$PUBLIC_IP:8000${NC}"
echo -e "   (Jika pertama kali, Anda akan diarahkan untuk membuat PIN)"
echo -e ""
echo -e "🤖 Operator Telegram (@ziqmusebot):"
echo -e "   Sudah menyala di background dan siap menerima perintah."
echo -e ""
echo -e "🛠️ Perintah Praktis di VPS:"
echo -e "   - Cek Status:  ${YELLOW}./ziqva.sh status${NC}"
echo -e "   - Lihat Log:   ${YELLOW}./ziqva.sh logs${NC}"
echo -e "   - Restart:     ${YELLOW}./ziqva.sh restart${NC}"
echo -e "${GREEN}==========================================================${NC}\n"
