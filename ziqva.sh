#!/bin/bash
# ============================================================
#  ZIQVA MUSE BOT — VPS SHORTCUT CONTROLLER
#  Penggunaan: ./ziqva.sh [start|stop|restart|status|logs]
# ============================================================

case "$1" in
  start)
    systemctl start ziqva-muse-bot ziqva-muse-operator
    echo "✅ Ziqva Muse Bot & Telegram Operator dinyalakan."
    ;;
  stop)
    systemctl stop ziqva-muse-bot ziqva-muse-operator
    echo "🛑 Ziqva Muse Bot & Telegram Operator dihentikan."
    ;;
  restart)
    # URUTAN PENTING — JANGAN digabung jadi satu perintah
    # `systemctl restart ziqva-muse-bot ziqva-muse-operator`!
    # update.sh berjalan sebagai ANAK service PANEL (ziqva-muse-bot);
    # me-restart panel membunuh seluruh cgroup service itu, termasuk
    # skrip ini dan proses systemctl-nya sendiri. Karena itu:
    #   1) restart OPERATOR dulu (service lain — skrip tetap hidup),
    #   2) restart PANEL terakhir (panel mati pun operator sudah restart).
    systemctl restart ziqva-muse-operator
    systemctl restart ziqva-muse-bot
    echo "🔄 Ziqva Muse Bot & Telegram Operator di-restart."
    ;;
  status)
    echo "================ STATUS PANEL WEB ================"
    systemctl status ziqva-muse-bot --no-pager
    echo -e "\n=========== STATUS TELEGRAM OPERATOR ============="
    systemctl status ziqva-muse-operator --no-pager
    ;;
  logs)
    journalctl -u ziqva-muse-bot -u ziqva-muse-operator -f
    ;;
  *)
    echo "Cara pakai: ./ziqva.sh {start|stop|restart|status|logs}"
    ;;
esac
