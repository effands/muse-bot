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
    systemctl restart ziqva-muse-bot ziqva-muse-operator
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
