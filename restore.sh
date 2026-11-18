#!/bin/bash
# ============================================================
#  ZIMUSE BOT — restore backup dari konsol
#
#  Pemakaian:
#    bash restore.sh                    -> tampilkan daftar backup
#    bash restore.sh <nama-backup>      -> restore (minta ketik YA dulu)
#    BACKUP_KEY=<kunci> bash restore.sh <nama-backup>
#                                       -> restore bila kunci tidak ada
#                                          di config.json / backup_key.txt
#
#  Restore MENIMPA config.json (PIN + token Telegram + kredensial
#  channel), channels_db.json, tasks_db.json, dan galeri. Sebelum
#  menimpa, file lama disalin dulu ke backups/rollback-<waktu>.tar.gz
#  (3 rollback terakhir disimpan). Setelah restore, kedua service
#  di-restart otomatis. TANPA konfirmasi ketik YA -> DITOLAK.
# ============================================================
cd "$(dirname "$0")" || exit 1

if [ "$1" = "" ]; then
  echo "=== Daftar backup (backups/) ==="
  python3 backup_core.py list || echo "(belum ada backup — buat dulu dari panel atau: python3 backup_core.py backup)"
  echo ""
  echo "Pemakaian: bash restore.sh <nama-backup>"
  echo "Contoh:    bash restore.sh backup-2026-11-19-0300.tar.gz.enc"
  exit 0
fi

NAME="$1"
echo "⚠️  RESTORE akan MENIMPA data panel dengan isi $NAME"
echo "    - config.json (PIN panel, token Telegram, kredensial channel)"
echo "    - channels_db.json, tasks_db.json, dan file state lainnya"
echo "    - file galeri (uploads/)"
echo "    File yang sedang berjalan akan di-restart setelah restore."
echo "    Snapshot pengaman file lama disimpan di backups/rollback-*.tar.gz"
echo ""
printf "Ketik YA (huruf besar) untuk lanjut, selain itu dibatalkan: "
read -r JAWAB
if [ "$JAWAB" != "YA" ]; then
  echo "Dibatalkan. Tidak ada yang berubah."
  exit 1
fi

if [ -n "$BACKUP_KEY" ]; then
  python3 backup_core.py restore --file "$NAME" --key "$BACKUP_KEY" --confirm
else
  python3 backup_core.py restore --file "$NAME" --confirm
fi
RC=$?
if [ $RC -ne 0 ]; then
  echo "❌ Restore gagal (kode $RC). Tidak ada file yang ditimpa."
  exit $RC
fi

echo "✅ File dipulihkan. Restart service..."
bash ziqva.sh restart
echo "✅ Selesai. Buka panel dan login kembali (PIN ikut dipulihkan dari backup)."
