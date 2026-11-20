## 1.3.56
- Langkah 3 /playlist (Durasi) kini pakai tombol tinggal klik: 40–70 menit, 1 jam, 60 menit, Semua durasi, + ✏️ Custom (ketik sendiri). Nilai tombol dipakai ulang parser teks yang sama, jadi artinya identik dengan ketikan. Tap kedua ditolak (anti riset ganda).

## 1.3.55
- Kolom EMAIL di tabel Channel Terhubung Dashboard (home) disembunyikan — email tetap tampil + bisa diisi manual di Task Monitor, dan tetap tercatat otomatis saat login Analytics.

## 1.3.54
- Kolom EMAIL di tabel channel bisa diisi manual: klik emailnya, ketik, lalu ✓ (Enter juga bisa, Esc batal). Kosongkan untuk menghapus. Email juga tetap terisi otomatis saat login Analytics.

## 1.3.53
- Tombol Tambah Channel di Dashboard kembali ke device login (TV) — login lebih cepat. Jalur 1x login browser tetap tersedia di Settings → GOOGLE OAUTH 2.0 API → Tambah Channel — 1x Login (dipakai untuk Analytics + email).

## 1.3.52
- Tambah channel kini konfirmasi dulu: sesudah login, panel menampilkan channel yang terdeteksi (nama + email) sebelum disimpan — 1 akun Google bisa punya beberapa channel, yang terdeteksi adalah channel yang sedang AKTIF di akun itu. Tombol Tambah Channel di Dashboard kini membuka jalur 1x login browser (device login tetap tersedia sebagai cadangan).

## 1.3.51
- Link google.com/device di modal Tambah Channel kini klik-untuk-menyalin (bukan buka tab baru) — tempel sendiri di browser profil channel.

## 1.3.50
- Perbaikan darurat: file client OAuth kini unik per client — upload JSON Desktop sempat menimpa file client TV satu project sehingga device login ditolak Google (invalid_client). File TV sudah dipulihkan; device login jalan lagi.

## 1.3.49
- Tambah channel cukup 1x login browser — token langsung punya semua izin (upload + Analytics + email); tidak perlu device login lagi. Blok baru di Settings → GOOGLE OAUTH 2.0 API → Tambah Channel — 1x Login (device login tetap tersedia sebagai cadangan).

## 1.3.48
- Kolom EMAIL di tabel Channel Terhubung (Dashboard + Task Monitor) — email pemilik channel, kosong tampil strip; email ditangkap otomatis saat login Analytics (izin baca email) dan disimpan per channel.

## 1.3.47
- Aktivasi Analytics: jam tayang asli 365 hari untuk syarat 4.000 jam — login browser sekali per channel di Settings → OAuth → Analytics (device login tetap tidak bisa, Google menolak). WATCH 30D + KESIAPAN MONETISASI pakai angka asli (tanda ✓) bila sudah aktif.

## 1.3.46
- Halaman Hub (home) tidak scroll — muat satu layar; konten dipadatkan, layar pendek makin dipadatkan, HP tetap boleh scroll.

## 1.3.45
- Saldo Kie.ai yang selalu "tidak terbaca" diperbaiki (API mengembalikan angka, bukan objek); saldo tampil per key + total + perkiraan generate; tombol Refresh Saldo.

## 1.3.44
- Kalender Jadwal kini menampilkan video yang sudah di-upload tapi belum tayang (pill "Sudah di-upload") — sebelumnya selalu 0 karena hanya membaca antrean aktif.

## 1.3.43
- Judul JADWAL BERIKUTNYA + UPDATE TERAKHIR dipendekkan (maks 40 karakter, potong di batas kata, sufiks tetap kelihatan); jarak kolom STATUS diperlebar; kolom STATUS rata tengah.

## 1.3.42
- HISTORY tiap baris kini tampilkan jam tayang + ikon jenis video (Short/Playlist/Long), tetap satu baris.

# Changelog Zimuse Bot

Catatan perubahan per versi. Dibaca operator saat start dan ditampilkan di
pesan restart Telegram, supaya Bos langsung tahu apa yang berubah.

## 1.3.41
- HISTORY dibuat rapat: satu video satu baris — judul + durasi render sebaris, status + thumbnail sebaris, padding baris dipangkas separuh, tombol hapus dan header grup diperkecil. Jauh lebih banyak video muat di satu layar.

## 1.3.40
- Pill di kartu HARI INI bisa diklik: klik pill channel untuk melihat detail video hari ini channel itu saja di HISTORY (yang gagal dan kemarin ikut tersaring), halaman otomatis turun ke HISTORY. Klik lagi untuk melepas filter, atau tombol "Hapus filter".

## 1.3.39
- Nama channel di tabel tidak lagi terpotong (batas lebar + ellipsis dihapus).
- Scroll tabel ke samping tidak lagi kembali ke awal: ternyata tabel di-render ulang tiap 2 detik (poll) dan posisi scroll ikut terhapus — sekarang posisi scroll disimpan sebelum render ulang lalu dipulihkan.

## 1.3.38
- Pill monetisasi yang subs-nya sudah terpenuhi (≥1.000) sekarang kaca emas transparan + glow dan ikon ✅ — yang belum tetap abu-abu ⏳. Yang sudah "Siap Monet" / "Monetisasi Aktif" tetap hijau.

## 1.3.37
- Judul di tabel channel dipendekkan dari tengah (awal + ujung tetap kelihatan, ujung itu sudut yang membedakan tiap channel); judul lengkap muncul saat kursor diarahkan.
- Pill channel di kartu "Hari Ini" sekarang beda warna tiap channel (channel baru otomatis dapat warna baru yang beda); channel yang 0 video dibuat redup.

## 1.3.36
- Task Monitor punya kartu ke-4 "Dijadwalkan": jumlah video yang sudah dijadwalkan tapi belum tayang + jam tayang terdekat (video yang gagal tidak dihitung).

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
