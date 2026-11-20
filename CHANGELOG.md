# Changelog Zimuse Bot

Catatan perubahan per versi. Dibaca operator saat start dan ditampilkan di
pesan restart Telegram, supaya Bos langsung tahu apa yang berubah.

## 1.3.22
- Kolom WATCH 30D di tabel Detail Channel dibuat lebih ramping.
- Tabel Kesiapan Monetisasi lebih enak dibaca: jam tayang yang belum ada datanya tampil "-" saja (bukan "-/4.000"), dan aturan syaratnya kini dua baris.
- Bar progress monetisasi sekarang berwarna gradasi (sebelumnya tidak ada warnanya sama sekali).
- Header panel kini menampilkan info DISK (persen terpakai + GB) di samping CPU dan RAM.

## 1.3.21
- Kata "semua" sekarang dipahami di langkah pilih channel — playlist untuk semua channel langsung dikerjakan lewat jalur multi-channel, tidak ditolak lagi.
- Tombol dan perintah di Telegram tidak lagi dijawab pesan "draft hangus" — pesan itu hanya muncul kalau memang sedang menjawab draft.
- Tabel Channel Terhubung sekarang bisa di-scroll (tingginya tetap), jadi tambah channel tidak mendorong layout ke bawah.
- Badge mode (Full-Auto / Approval) di tabel channel bisa diklik — langsung lompat ke Task Monitor tempat antrean approval.

## 1.3.20
- Kartu Channel Terhubung di Dashboard dan Task Monitor sekarang bentuk tabel — kolom Channel, Status, Mode, dan Jadwal Berikutnya sejajar semua, jadi rapi dibacanya.

## 1.3.19
- Perbaikan: tombol jadwal harian tidak jalan karena endpoint-nya belum didaftarkan sebagai endpoint operator — sekarang sudah diperbaiki.

## 1.3.18
- Jadwal Playlist harian sekarang ada tombolnya: /jadwalplaylist menampilkan semua channel dengan tombol nyala/mati per channel, plus tombol Mulai Semua dan Stop Semua.
- Perintah /stopjadwal [channel|semua] buat berhentiin jadwal harian — langsung jalan tanpa konfirmasi (tombol darurat, misal channel sudah monet). /mulaijadwal buat nyalakan lagi.
- Channel baru tidak otomatis ikut jadwal harian — harus dinyalakan dulu lewat tombol, supaya channel jalur Zumo tidak kecampur (pola beda).

## 1.3.17
- Video Playlist sekarang export 720p (dulu 1080p) — ukuran file jadi sekitar separuhnya (terbukti 49%), upload lebih cepat, kualitas tetap HD.
- Kalau klip 720p tidak ada, tetap pakai klip lain lalu diturunkan ke 720p — tidak gagal.

## 1.3.16
- Keyboard Telegram dibalik: tombol aksi (Buat Playlist, Buat Short, Buat Video Long, Backup, Laporan) di atas, tombol pantau (Status, Kuota, Channel, Antrean, Jadwal, Statistik, Monetisasi, Task Gagal) di bawah.
- Auto-suggest ketik "/" di Telegram sudah aktif (44 perintah terdaftar) — tinggal ketik "/" lalu pilih, tanpa buka keyboard.
- Kalau /jadwal kosong (0 task 7 hari), bot menawarkan tombol: Buat Playlist atau Perpanjang Jadwal 30 Hari (minta konfirmasi dulu).

## 1.3.15
- Keyboard Telegram dirapikan: tombol "10 Judul Sleep", "Template Deskripsi", dan "Blueprint Resep" yang sudah tidak relevan diganti "Buat Playlist", "Backup", dan "Laporan".

## 1.3.14
- Pesan restart di Telegram sekarang berisi isi update (yang ini).

## 1.3.13
- Riset playlist sekarang pakai filter YouTube "video panjang, lebih dari 20 menit" — klip 5 menit tidak bisa lolos lagi.
- Kalau durasi yang diminta tidak ada, bot menawarkan 3 video yang durasinya paling dekat, tinggal pencet ya atau ganti durasi.
- Kata jadwal seperti "sebulan konten" tidak ikut dicari lagi — yang dicari cuma temanya, misal "lagu malaysia".
- Bot tidak tanya ulang Langkah 3 kalau riset gagal.

## 1.3.12
- Task Monitor menampilkan jadwal video terdekat di tiap channel.
- Kartu Log Aktivitas di Task Monitor: lihat log panel dan operator, tombol salin dan bersihkan.
