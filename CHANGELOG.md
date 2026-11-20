# Changelog Zimuse Bot

Catatan perubahan per versi. Dibaca operator saat start dan ditampilkan di
pesan restart Telegram, supaya Bos langsung tahu apa yang berubah.

## 1.3.35
- Kolom "Jadwal Berikutnya" sekarang juga menampilkan video yang sudah di-upload tapi belum tayang (misal yang tayang jam 19:00), dengan penanda "sudah di-upload" — sebelumnya kolom ini selalu kosong karena semua video publish langsung.

## 1.3.34
- Tombol header (Light Mode, Hub, Update) sekarang ikon saja tanpa teks, di semua lebar layar.
- Angka RAM/DISK di header ditumpuk atas-bawah (dipakai di atas, total di bawah) supaya tidak sempit.
- Tabel channel di Task Monitor punya kolom baru "Update Terakhir": jenis video terakhir (Short/Playlist/Long), judul, dan jam tayangnya; video yang gagal tidak dihitung.

## 1.3.33
- Audit penuh semua fungsi: 86 temuan, 25 diperbaiki di versi ini — sisanya dicatat untuk batch berikutnya.
- Perintah "publish langsung" sekarang hanya jalan kalau Bos benar-benar menyuruh tayang (kalimat "jadwal besok jangan lupa dicek" tidak lagi salah dibaca), dan selalu minta konfirmasi dulu.
- Bot tidak lagi salah paham "buatkan short lagu sore" jadi buat lagu Zumo, dan "jadwalkan video long" tidak lagi jadi playlist.
- Koreksi di konfirmasi sekarang jalan walaupun diawali kata "ya" ("ya, jam 5 sore" benar-benar mengubah jamnya).
- Perintah "publish besok jam 7 malam" sekarang dijadwalkan besok 19:00, bukan langsung tayang.
- Worker render sekarang punya pengawas: kalau mati karena error, restart sendiri dalam 5 detik.
- Job playlist yang terputus saat restart panel sekarang ditandai gagal supaya bisa dibuat ulang (sebelumnya macet dan retry selalu ditolak).
- Batas riwayat job naik dari 50 ke 500 supaya dedupe 24 jam tetap jalan.
- Perbaikan kecil: nama channel tidak lagi memotong kata lain di judul ("Ana" tidak lagi merusak "Manado"), judul tetap beda antar channel walau sudutnya sudah ada di judul, dan beberapa error yang dulu hilang diam-diam sekarang tercatat.

## 1.3.32
- Judul playlist tidak lagi kembar antar channel: sudut + pola judul sekarang dari urutan channel (bukan dari hash yang kebetulan sama) — Barang Unick dan Mahewa sebelumnya sama-sama dapat sudut "Spesial", sekarang dijamin beda sampai 48 channel.

## 1.3.31
- Konfirmasi bot sekarang bisa direvisi: ketik saja yang mau diubah (mis. "jam 5 sore", "tema lofi", "cuma Aruya") — bot mengubahnya lalu konfirmasi ulang, tidak perlu batal.
- Jadwal beruntun per channel diperbaiki (sebelumnya semua channel dapat jam yang sama karena bug penulisan tanggal).
- Satu batch playlist sekarang memakai video sumber yang beda-beda per channel, dan video yang sudah dipakai 24 jam terakhir tidak bisa dipakai lagi untuk channel yang sama.

## 1.3.30
- Bot Telegram sekarang paham perintah "jadwalkan 1 video ... jam 4 ... semua akun": 1 video playlist per channel, tayang beruntun tiap 15 menit mulai jam yang diminta (tema dan jam ditanyakan dulu bila belum disebut, lalu konfirmasi sebelum dibuat).
- Perintah baru di Telegram tidak lagi ditelan pesan "draft sudah hangus" — bot mencoba memahami perintahnya dulu, baru memberi tahu bila draft memang sudah kedaluwarsa.

## 1.3.29
- Perintah Telegram baru /hariini: ringkasan upload hari ini per channel, sama seperti kartu "Hari Ini" di Task Monitor.

## 1.3.28
- Task Monitor kini punya kartu "Hari Ini": total video yang sudah upload hari ini + jumlah per channel (channel yang belum upload tampil abu-abu).

## 1.3.27
- Tabel Channel Terhubung tidak lagi scroll menyamping di HP — kolom Mode kini rapat dengan Status.

## 1.3.26
- Pill status channel tidak lagi menulis "Connected" — warna saja yang bicara (hijau terhubung, merah putus), teks lengkapnya ada di tooltip saat kursor diarahkan.
- Tombol Light Mode di layar HP tinggal ikonnya saja.

## 1.3.25
- Tabel Kesiapan Monetisasi sekarang bisa di-sort: klik header kolom untuk mengurutkan, klik lagi untuk membalik arah.
- Kartu statistik bertambah 1: CHANNEL — jumlah channel yang terhubung.
- Di layar HP, subjudul header ("Overview sistem & analytics") disembunyikan — judulnya cukup "Dashboard" saja.

## 1.3.24
- Kartu Tren Channel dan Channel Terhubung sekarang presisi sama tinggi — tabel channel mengisi penuh sisa ruang kartunya.

## 1.3.23
- Baris tabel Channel Terhubung dibuat lebih rapat — 7 channel sekarang muat tanpa scroll.

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
