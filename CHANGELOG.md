## 1.3.92
- Perintah /riset + riset tema dari Telegram (aturan user 22 Nov 2026 — jadikan gaya riset "Smooth Jazz 90s" sebagai DEFAULT, ada data, berlaku juga lewat Telegram): perintah baru `/riset <tema>` (terdaftar di /start, seksi Konten) dan kalimat bebas "Risetkan saya tentang <tema>" / "riset <tema>" — keduanya diteruskan ke Muse lewat jembatan (source='riset', tema di context) supaya risetnya mengikuti playbook data di ~/workspace/zumo/riset-template.md (top 20 video via yt-dlp, 8 thumbnail divisual, 3 deskripsi, arah background + thumbnail + palet hex + contoh judul). Operator menjawab "riset dimulai, hasilnya ~10 menit lagi" (riset dikerjakan Muse karena operator tidak bisa melihat thumbnail). CATATAN: tema telanjang tanpa kata "riset" (mis. cuma tulis "Musik Afrobeats 90s") BUKAN perintah riset — bot akan membacanya sebagai perintah buat lagu/playlist; harus pakai /riset atau kata "riset". Jembatan (cron telegram-bridge) diperbarui: record 'processing' source='riset' tidak pernah diproses ulang walau klaimnya basi (riset ~10 menit, klaim 10 menit akan basi di tengah jalan dan memicu riset ganda) — penyelesaiannya dimiliki agen utama yang mengirim ringkasan ke Telegram + menandai 'replied'. Tes lokal 27/27 kasus lolos (parse tema incl. "R&B" utuh, forward source='riset', /start memuat /riset).

## 1.3.91
- Tombol perbaiki judul/deskripsi video tayang (pilihan user 21 Nov 2026 dari inventaris API TV): tiap baris HISTORY di Task Monitor yang punya video kini punya tombol ✏️ (di samping 🗑️) — modal memuat judul/deskripsi/tags asli dari YouTube (GET /api/video_snippet), lalu menyimpan via POST /api/video_update (videos.update, 51 unit kuota). PENTING: snippet diambil ulang dulu dari YouTube — categoryId + defaultLanguage dipertahankan, kalau tidak tags & kategori video ikut terhapus; privasi tidak disentuh. Validasi: judul wajib 1–100 karakter, deskripsi maks 5000, tags dipecah koma (tiap tag maks 30 karakter, total maks 500). Kepemilikan dicek (snippet.channelId harus sama dengan channel yang dipilih) — video channel lain ditolak 403. Judul di riwayat panel ikut diperbarui supaya tidak tampil judul lama. Tes lokal: parse tags + sync judul riwayat + struktur endpoint + JS modal.

## 1.3.90
- Timer hitung mundur di modal Tambah Channel (permintaan user 21 Nov 2026 — "kasih timer aja klo gitu"): status modal kini menampilkan "sisa waktu 14:59" yang berdetak tiap detik (batas server 900 dtk, bukan expires_in Google 1800 dtk); habis → polling berhenti + pesan "⌛ Kode kedaluwarsa (15 menit)" + tombol "🔁 Coba Lagi". Timer dimatikan di semua jalan keluar (sukses/konfirmasi/error/tutup modal) supaya tidak bocor.
- Sesi device login kini juga disimpan di server (bug laporan user 21 Nov 2026 — teks merah "Sesi device login tidak ditemukan" muncul 6 detik setelah kode dikeluarkan, sebelum Bos sempat memasukkan kode): penyebabnya Flask mengirim ulang cookie sesi di setiap respons (default SESSION_REFRESH_EACH_REQUEST=True), sehingga request auto-refresh dashboard yang berangkat sebelum respons /api/device_code tiba akan membalas dengan cookie lama dan menimpa cookie baru — device_code terhapus dari sesi. Perbaikan: device_code + pending_add juga dicatat di `device_sessions.json` (chmod 600, tulis atomik, ikut backup; entri kedaluwarsa 900 dtk, di-prune tiap tulis). `/api/poll_device_token` memakai simpanan server sebagai fallback bila sesi kosong/tidak cocok (lalu memulihkan sesinya); `/api/channel_add_confirm` dan `/api/channel_add_cancel` memakai simpanan pending_add yang sama. Berlaku juga untuk halaman /device_login. Pesan error bila kode benar-benar tidak ada di mana pun tetap sama seperti dulu. Tes lokal: store + fallback + timer JS.

## 1.3.89
- Contoh acak di wizard + satu ketuk (permintaan user 21 Nov 2026 — contoh "autumn, pantai, kota malam" itu template tetap): langkah tema playlist, tema footage Pixabay, dan tema Short kini menampilkan 3 contoh yang diambil ACAK tiap kali ditanya (random.sample dari pool 18–20 pilihan, tanpa seed tetap — 10 undian berurutan memberi 10 trio berbeda di tes). Contoh bisa dipencet — tombol `ex:<teks>` diproses persis seperti jawaban ketikan (tanpa logika ganda); ketik sendiri tetap bisa, `lewati` di langkah Pixabay tetap ada. Catatan: tombol yang langsung mengisi kolom chat butuh mode inline bot (saat ini NONAKTIF di @ziqmusebot, dicek via getMe) — jadi dipakai satu ketuk langsung, lebih cepat daripada isi-kolom-lalu-Enter; bila Bos tetap mau versi isi-kolom, perlu aktifkan inline di BotFather + handler inline. Tes lokal 22/22 kasus lolos.

## 1.3.88
- Tombol jadwal digabung (koreksi user 21 Nov 2026 — "kalau sama kenapa ada 2?"): "Lewati" dan "Default" artinya sama (pakai jadwal berikutnya), jadi kini SATU tombol "⏭️ Lewati / Default (jadwal berikutnya)" + "🚀 Publish langsung". v1.3.87 sempat membuat dua tombol terpisah karena perintah "sama lewati dan default" dibaca harfiah — itu membingungkan, diperbaiki di sini. Tes lokal 15/15 kasus lolos.

## 1.3.87
- Tombol jadwal tayang di wizard (permintaan user 21 Nov 2026 — "yang publish itu buat button aja biar tinggal klik aja", sama untuk lewati & default): langkah jadwal di /playlist (Langkah 5), /buatshort (Langkah 3), /zumo (Langkah 8) dan /zumorakit (Langkah 3) kini menampilkan 3 tombol — "🚀 Publish langsung", "⏭️ Lewati", "🕐 Default (jadwal berikutnya)" — plus penjelasan di pesan: Publish = langsung tayang setelah render (tanpa jadwal); Lewati / Default artinya SAMA = pakai jadwal berikutnya (server yang atur). Callback baru `plsch:publish` / `plsch:skip` di operator: publish set publish_date = jam sekarang + publish_now; skip set jadwal berikutnya (playlist/short) atau kosongkan publish_date (zumo — server yang atur); draft yang sudah tidak di langkah publish dijawab "tidak berlaku". Ketik jam manual tetap bisa. Tes lokal 15/15 kasus lolos.

## 1.3.86
- Filter video official dari riset playlist pabrik (strategi user 21 Nov 2026 — video rilisan official dari penyanyi/artis tidak masuk strategi): helper baru `_playlist_is_official_reference` — buang kandidat bila judul mengandung frasa official ("official music video", "official live music video", "official audio", "official lyric video", "official video/mv/music", "(official", "[official") ATAU nama artis di judul sama dengan channel yang meng-upload (normalisasi: huruf kecil, buang kata official/channel/music/tv + angka; segmen deskripsi seperti kompilasi/kumpulan/playlist/remix/dj tidak dihitung sebagai nama artis). Diterapkan di `_playlist_search_one_tier` sebelum tier dinyatakan berhasil — tier yang hanya berisi video official dianggap kosong, riset lanjut ke tier berikutnya yang lebih longgar. Link kiriman user (jiplak) tidak lewat jalur ini, jadi tidak ikut tersaring. Tes lokal 15/15 kasus lolos (dua video official batch 18:13 tersaring; cover, DJ remix, kompilasi, dan channel "RITA OFFICIAL 9" tetap lolos). Bodi cron riset trending juga diperbarui ke v2 dengan filter yang sama (jadwal tetap 2 jam sekali).

## 1.3.85
- Perbaiki klik kolom MODE di tabel CHANNEL TERHUBUNG yang bikin panel blank (laporan user 21 Nov 2026): pill Auto/Approval memanggil `go('panel-tasks')`, padahal `go()` mengharapkan id pendek dan menambahkan awalan `panel-` sendiri — jadinya mencari elemen `panel-panel-tasks` (tidak ada), lalu error setelah semua panel kehilangan status aktif → halaman kosong. Sekarang memanggil `go('tasks')` seperti semua tombol lain, plus `go()` diperkeras: id dinormalisasi (awalan `panel-` dibuang bila ada) dan target dipastikan ada SEBELUM panel lain disembunyikan, jadi id yang salah tidak akan pernah mengosongkan halaman lagi.

## 1.3.84
- Playlist ditolak karena referensi sudah dipakai 24 jam terakhir kini menawarkan tukar lagu lain, bukan pesan buntu (laporan user 21 Nov 2026, MelodiMUsic / lagu malaysia lawas: bot cuma bilang "coba lagi dengan /playlist"): bot menampilkan sampai 4 kandidat lain dari riset yang sama sebagai tombol — satu ketuk langsung dibuat dengan setting yang sama (channel, tema, footage, jadwal) — plus tombol "🔄 Riset ulang tema ini" dan "❌ Batal". Bila kandidat sudah habis, baru pesan gagal seperti dulu. Berlaku di kedua jalur konfirmasi (tombol ✅ dan jawaban "ya"), dan bila referensi pengganti juga ditolak, tawaran diulang dengan sisa kandidat (yang sudah dicoba tidak ditawarkan lagi). Mengikuti prinsip user 20 Nov 2026: tiap pesan bot harus diakhiri shortcut yang menebak langkah berikutnya.

## 1.3.83
- Spectrum Zumo jadi EQ BARS vertikal (komplain user 21 Nov 2026: spectrum lama "malah jadi video geser kanan ke kiri, harusnya spectrum equalizer"): `showspectrum slide=scroll` diganti `showfreqs mode=bar` (batang naik-turun mengikuti musik, jangkar di bawah, ruang hitam di atasnya; win_size=512 supaya batang chunky; ascale=lin dicoba — batang hampir tak terlihat). Warna putih/cyan/viridis tetap didukung lewat rantai gray → lutrgb yang sama.
- Partikel salju diperkecil ~25% (radius 2.0-6.0 → 1.5-4.5): user bilang partikel "udah lumayan oke, hanya size-nya kurang kecil dikit".
- Judul/deskripsi video Zumo pertama (lj3bfh-4FWc) diperbaiki langsung via API (tanpa re-upload): judul SEO Bahasa Inggris 89 karakter, deskripsi Inggris ~1000 karakter + CTA subscribe + hashtag.

## 1.3.82
- Thumbnail Zumo dirombak total (komplain user 21 Nov 2026 + analisa thumbnail YouTube niche relaxing jazz / coffee: 'RELAX JAZZ CAFE', 'November Morning Bossa Nova Jazz', 'Cozy November Coffee Jazz'): foto FULL-BLEED tanpa panel split, tanpa badge ZUMO, tanpa nama channel; teks pendek maks 3 kata di atas gambar (3 kata terakhir segmen pertama judul), 2 baris serif elegan tengah-atas — baris 1 putih, baris 2 krem keemasan — + scrim gelap halus di atas supaya terbaca. Contoh: 'Cozy European Morning Coffee Jazz' → 'Morning Coffee' / 'Jazz'.
- Judul video Zumo dibersihkan: tema tidak diulang bila kata-katanya sudah ada di judul lagu, suffix '(N Lagu)' dibuang (bukan gaya judul YouTube; jumlah lagu tetap ada di deskripsi/tracklist).
- Latar video Zumo: bila ada file video di galeri backgrounds channel (`zumo_cozy_sax_bg.mp4`), render memakai video itu (loop sampai durasi lagu) sebagai latar, bukan gambar statis — asap kopi + daun/lampu bergerak, kamera statis (permintaan user 21 Nov 2026). Gambar statis tetap jadi fallback.

## 1.3.81
- Hapus kolom TREND dan AVG dari tabel DETAIL CHANNEL (permintaan user 21 Nov 2026) — termasuk kalimat penjelasnya di catatan "Tentang data ini"; colspan baris kosong/muat disesuaikan (14 → 12).
- Perbaiki tampilan dropdown GROUP yang aneh (laporan user 21 Nov 2026): dropdown tertutup hanya menampilkan chevron kecil tanpa nama group. Penyebab: aturan global `.content select { width:100% }` bikin select di sel tabel auto-layout menciut sampai tinggal chevron-nya. Sekarang select punya lebar tetap 130px, jadi nama group yang aktif ("Playlist M" / "— Tanpa Group —") selalu kelihatan tanpa harus dibuka.

## 1.3.80
- Perbaiki pesan "Gagal menautkan channel" yang bohong (laporan user 21 Nov 2026): klik pertama "Ya, Tambah Channel Ini" sebenarnya BERHASIL di server (channel Wiji Fiko Teren tertambah jam 12:30:52, terverifikasi di log + channels_db.json), tapi panel menampilkan "Gagal". Penyebab: 6 fungsi (confirmDeviceAddChannel, channelAddConfirm, channelAddMakeUrl, channelAddSaveCode, analyticsMakeUrl, analyticsSaveCode) membaca jawaban server tanpa `r.json()` dulu — yang dibaca adalah objek Response mentah yang status-nya angka HTTP (200), bukan string 'success', sehingga selalu masuk cabang error. Sekarang semua membaca JSON dulu, jadi sukses tampil sebagai sukses dan pesan error asli server tampil bila memang gagal. Efek samping yang ikut teratasi: klik kedua dst dulu selalu 400 karena channel sudah tertambah (pending_add sudah dihapus) — user tidak lagi disesatkan untuk mengulang klik.

## 1.3.79
- Perbaiki tombol "Ya, Tambah Channel Ini" / "Batal" di modal Tambah Channel yang mati total saat diklik (laporan Bos 21 Nov 2026, channel NadaKoplo): penyebabnya `confirmDeviceAddChannel()`/`cancelDeviceAddChannel()` memanggil `_fetch(...)`, padahal `_fetch` hanya dideklarasikan di dalam IIFE pembungkus CSRF (scope lokal) — dari fungsi top-level, namanya tidak dikenal sehingga klik langsung error tanpa pesan apa pun. Perbaikan: semua pemanggil top-level kini memakai `fetch(...)` (global yang sudah dibungkus header CSRF). Satu akar masalah yang sama juga memperbaiki 5 tombol lain yang diam-diam mati: simpan group channel, simpan email channel, buat link Analytics, konfirmasi/batal tambah channel via 1x login browser, dan kirim kode Analytics.
- Termasuk perbaikan v1.3.78 (salin link google.com/device) dalam deploy yang sama.

## 1.3.78
- Perbaiki klik link google.com/device di modal Tambah Channel (laporan Bos 21 Nov 2026): klik link tidak menyalin apa-apa, malah muncul "Browser ini tidak mendukung salin otomatis". Penyebab: panel jalan lewat HTTP (bukan HTTPS) sehingga `navigator.clipboard` tidak tersedia di browser (butuh secure context) dan kode langsung menyerah. Sekarang pakai fallback textarea + `execCommand('copy')` seperti tombol salin lain di panel — jalan di HTTP. Bila itu pun gagal, URL ditampilkan utuh di tempat link supaya bisa diseleksi & disalin manual. Perilaku tetap: klik = salin, bukan buka tab baru.

## 1.3.77
- Perbaikan salah-baca NLU (3 yang paling rawan, permintaan Bos 21 Nov 2026): (1) 'ketiga' / 'tiga channel' / 'ke-3' TIDAK lagi berarti semua channel — warisan saat Bos cuma punya 3 channel; sekarang berarti minta 3 channel (bot menanyakan pilih 3). 'Semua channel' tetap berarti semua 8. Berlaku di perintah bebas, jawaban wizard, dan jawaban klarifikasi channel. (2) Pertanyaan tentang Zumo ('zumo itu apa?', 'apa itu zumo?') BUKAN order — tidak lagi memicu buat lagu Zumo; dijawab Muse lewat jembatan. Order Zumo ('buatkan lagu zumo') tidak berubah. (3) 'tampilkan playlist group X' BUKAN 'lihat group' — bila disebut konten (playlist/short/video/lagu) di luar nama group-nya, tidak lagi jadi laporan group; dijawab Muse. Nama group yang mengandung kata konten ('Playlist M') dibuang dulu sebelum dicek supaya 'lihat group Playlist M' tetap jalan.
- Catatan: perintah berbahaya (hapus, publish langsung, buat konten) tetap selalu minta konfirmasi 'Maksud Bos: ...?' dulu sebelum jalan.

## 1.3.76
- Upload per group (baru): perintah upload kini bisa menyasar satu group — semua channel di dalamnya ikut dibuatkan video. Bisa lewat ketik bebas (`buatkan playlist untuk group Playlist M`, `buatkan short untuk group Playlist M`, `jadwalkan playlist untuk group Playlist M jam 4 sore`), lewat jawaban di langkah channel wizard (`group Playlist M`), lewat one-shot (`/buatplaylist tema | group Playlist M | ...`), atau lewat tombol 📁 Group di pertanyaan channel NLU. Target group direpresentasikan 'group:<nama>' (sejajar 'all') dan diuraikan ke anggotanya sebelum konfirmasi — konfirmasi menyebut semua nama channelnya, cek login per channel tetap jalan, tiap channel diriset + dipilih referensinya sendiri (rotasi v1.3.73 tetap berlaku). Group yang disebut tapi belum ada / kosong dijawab dengan daftar group yang ada, bukan diam-diam gagal. Perintah atur-group (`Aruya masuk ke Group X`, `lihat group`) tidak terganggu; bonus: jeda/lanjutkan/ubah mode/perpanjang juga ikut bisa per group lewat NLU. Perbaikan kecil di jalan: guard atur-group dulu salah menangkap 'jadwalkan playlist untuk group X' sebagai 'lihat group' karena kata 'list' di dalam 'playlist'.
- Footer /group kini menyebut cara upload per group.

## 1.3.75
- Pixabay: klip unduhan WAJIB video bergerak — tolak codec gambar (mjpeg/png/dll), durasi <2 dtk, dan klip statis (3 frame sampel identik = foto); klip bermasalah dilewati, tidak gugurkan job; pencarian lewati klip <3 dtk. (Video Aruya + Mahewa 21 Nov 2026 freeze 20-33 dtk.)
- Render playlist: klip tidak seragam (beda codec/resolusi/fps) langsung normalisasi ke 720p30, tidak coba copy; validasi hasil copy + cek celah PTS (gap >2 dtk = freeze → jatuh ke normalisasi).
- Notifikasi WAJIB bawa tombol saran: pesan stok jadwal (🎵 Buat video baru → wizard playlist channel terisi, 📅 Lihat jadwal) + Lingkaran Analitik (🎵 Buat Playlist, 📊 Statistik); handler baru `stok:buat:<yt_id>` di operator.

## 1.3.74
- Analisa SEO: buang serpihan kata <3 huruf dari sisa kata ('th' dari judul referensi "...Hati th 90an" sempat lolos jadi "...2026 th" di video Mahewa 07:42 WIB 21 Nov 2026).

## 1.3.73
- ANALISA SEO judul playlist (janji 20 Nov 2026, komplain Bos 21 Nov): judul TIDAK lagi menyalin judul referensi. Referensi diurai jadi komponen (genre, asal, mood, era, jenis, tahun, sisa kata) lalu judul BARU disusun dari komponen itu dengan 4 susunan kata berbeda per channel + sudut per channel — satu referensi yang sama menghasilkan 8 judul berbeda struktur (tes: 8/8 unik untuk ref slow rock hari ini, kata kunci SEO tetap ada). Sudut yang kata intinya sudah ada di judul ikut diganti sudut cadangan (tidak lagi "... Menyentuh Hati Paling Menyentuh").
- ROTASI REFERENSI (order_playlist_nostalgia.py, dipakai cron harian): tiap channel dapat video referensi BERBEDA hari itu — auto-pick views tertinggi yang BELUM dipakai channel lain hari ini (seed dari task Tayang hari ini); bila semua kandidat sudah dipakai, pakai yang teratas.

## 1.3.72
- Popup "👥 Kelola Group" — tombol "Buat Group" di header DETAIL CHANNEL (tempat yang ditunjuk user). Isinya: form buat group baru (nama + channel), daftar group + anggotanya, dan channel tanpa group — tiap channel punya dropdown untuk pindah group / keluar group. Dropdown di popup me-render ulang isinya setelah simpan.

## 1.3.71
- LOG AKTIVITAS jadi SATU log (logs/activity.log): task dibuat (Shorts/Playlist/Zumo/VOD), render mulai, task selesai (Tayang ✅ / Gagal ❌ / Ditolak 🚫) — semua dengan jam WIB. Dropdown sumber Panel/Operator dihapus; log polling GET tidak ada lagi di tampilan. Riwayat 100 task terakhir di-backfill sekali saat deploy.

## 1.3.70
- Dashboard: kolom GROUP disembunyikan dari tabel CHANNEL TERHUBUNG (tetap ada di Task Monitor + DETAIL CHANNEL, di sanalah group diatur); TREN CHANNEL dikecilkan (grid 2fr → 1.5fr) supaya tabel muat tanpa scroll menyamping; label mode "Full-Auto" disingkat jadi "Auto" (panel saja).

## 1.3.69
- Kolom GROUP di tabel DETAIL CHANNEL kini dropdown juga (sama seperti tabel CHANNEL TERHUBUNG) — sebelumnya cuma tampil "—" tanpa tombol; buat/pindah/keluar group bisa langsung dari tabel ini. Render ulang tiap 5 menit dijaga supaya tidak menghapus input group baru yang sedang dibuka.

## 1.3.68
- /jadwal kini menampilkan order Playlist harian: channel daily_playlist yang belum punya task 7 hari ke depan tampil "🔁 Playlist harian aktif — video dibuat tiap pagi ±06:10, riset fresh, langsung tayang" (bukan "Nggak ada jadwal"), plus ringkasan "🔁 Order harian: N channel ..." di bawah; teks penutup bila total 0 task tapi order harian aktif ikut dijelaskan.
- Kalender Jadwal: slot rutin hari ini tampil — channel daily_playlist yang belum punya video hari ini dapat baris "🔁 Playlist harian (rutin)" jam 06:10 (status "Rutin — dibuat ±06:10", atau "belum dibuat hari ini" bila jam sudah lewat, "Dijeda" bila channel dijeda); bila videonya sudah dibuat, baris rutin hilang dan video aslinya yang tampil. Ringkasan kalender menyebut jumlah slot rutin hari ini.
- Dashboard: angka awal kartu statistik (TOTAL VIEWS / WATCH TIME / SUBSCRIBERS / ACTIVE TASKS / CHANNEL) kini "…" selama data dimuat, bukan 0 — angka 0 kemarin pagi hanya angka awal sebelum fetch /api/get_youtube_analytics selesai (log: HTTP 200, cache fresh).
- Group channel (baru): 1 channel = 1 group; group disimpan sebagai field `group` per channel (group baru lahir saat channel pertama dimasukkan). Panel: kolom GROUP di tabel channel (Dashboard + Task Monitor) berupa dropdown ganti group + "Buat group baru…" (input inline); kolom GROUP di DETAIL CHANNEL; filter per group di dropdown analytics (kartu + grafik + tabel ikut terfilter). Telegram: perintah /group (daftar / masukkan / keluar), NLU "Aruya masuk ke Group Playlist A", "Akun H pindah ke Group X", "Aruya keluar group", "lihat group" (group baru dibuat otomatis; ejaan disamakan dengan group yang sudah ada); pesan atur-group diakhiri tombol shortcut 👥 Lihat Group + 📅 Lihat Jadwal.
- Hapus channel dari Telegram (baru): NLU "hapus channel X" dan /hapuschannel kini benar-benar menghapus (dulu ditolak dan diarahkan ke panel) — lewat endpoint operator baru yang memakai logika yang sama dengan tombol 🗑 di panel (batalkan antrean + hapus channel + hapus folder galeri; video YouTube tidak ikut terhapus); tetap lewat konfirmasi NLU dulu. Panel: hapus channel sudah ada sejak lama (Settings → 🗑), tidak berubah.
- Endpoint baru: /api/operator/set_channel_group, /api/operator/delete_channel (HMAC, masuk _OPERATOR_ENDPOINTS), /api/set_channel_group (panel, login+CSRF); /api/get_channels kini menyertakan field group + daily_playlist.

## 1.3.67
- Shorts Muse gagal render diperbaiki: FFmpeg 4.4.2 di VPS gagal negosiasi format di `sidechaincompress` ("could not choose their formats") padahal input sudah sama-sama s16/44,1 kHz/stereo — rantai lama terverifikasi gagal, rantai baru jalan di ffmpeg VPS. Rantai audio kini menyeragamkan kedua input dulu (`aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo`) + `asplit` karena voice dipakai 2x. Tombol 🔁 Coba Lagi pada Short yang gagal kini akan berhasil.
- Wizard /buatshort: langkah jadwal kini menerima `publish` / `langsung` / `sekarang` / `publish langsung` / `langsung tayang` (juga `tanpa jadwal`, `tayang sekarang`) = langsung tayang setelah render — publish_date diisi jam sekarang, mekanisme sama seperti Playlist (jadwal yang sudah lewat = publik tanpa publishAt). Pesan langkah 3 & pesan error kini menyebut opsi `publish`; ringkasan menampilkan "Langsung tayang setelah render" bila mode ini dipilih. Kata-kata yang sama juga diterima di langkah jadwal Playlist & NLU.
- Tombol 🎵 di pesan riset trending: video saran kini langsung jadi referensi playlist — alur membuat job riset asli dari link-nya (mode jiplak, metadata asli: durasi/views/channel), lalu langkah durasi, riset & pilih kandidat dilewati (durasi mengikuti video referensi, tampil di ringkasan mis. "mengikuti video referensi (91 mnt)"); tinggal jawab tema footage → jadwal → konfirmasi. Bila link gagal dibaca panel, alur kembali ke NLU lama. Alur /buatplaylist biasa tidak berubah (durasi tetap ditanya karena dipakai menyaring kandidat).

## 1.3.66
- Telegram riset trending: pesan saran kini format v3 — judul jadi link biru tebal (klik langsung nonton), baris 👁 views • 📅 tanggal upload per saran (diambil via yt-dlp), alasan + channel tetap ada. Tombol shortcut lengkap di bawah pesan: 🎵/🎬 per nomor saran, ✏️ Custom, 💡 Saran lain, 🔄 Analisa lagi. Callback baru `rs:pl:`/`rs:sh:` (pesan dari `research_suggestions.json` di VPS — playlist lewat NLU, Short lewat wizard), `rs:more` (kirim saran cadangan), `rs:again` (kirim analisa genre), `rs:custom` (minta ketik bebas). Catatan: Telegram TIDAK support warna teks — judul link biru adalah warna yang tersedia.
- Telegram /status dirapikan: Web+versi 1 baris, CPU/RAM/Disk 1 baris, status & mode channel yang sama diringkas di kepala ("🟢 semua terhubung • 🤖 semua Full-Auto"), tiap channel 1 baris (↑upload/hari), stok habis diringkas 1 baris bila semua habis, Project ID hanya 1 baris bila ada >1 project, "Hari ini" diringkas 2 baris. Tombol shortcut kontekstual di bawah laporan: ❌ Gagal (N) bila ada yang gagal, 📅 Jadwal, 📊 Statistik, 🔄 Refresh (callback `rep:` memakai builder yang sama seperti perintah baku).
- Prinsip baru (permintaan Bos): setiap pesan bot yang menawarkan sesuatu diakhiri shortcut yang menebak langkah Bos berikutnya.

## 1.3.65
- Telegram /buatshort: jawaban "semua channel" kini diterima — tidak ditolak lagi. Wizard 1 channel dialihkan ke jalur NLU multi-channel (tanya tema, konfirmasi sekali, lalu Short dibuat untuk semua 8 channel dengan jadwal per channel). Ketik nama channel ("Aruya", "cozy sax") juga kini diterima sebagai jawaban, bukan cuma angka 1–8. Prompt pilih channel sudah menyebut kedua opsi ini.
- Telegram /buatlong: "semua channel" tetap ditolak dengan penjelasan (video long 1 channel sekali proses) — sama seperti aturan NLU yang sudah ada; ketik nama channel kini diterima.

## 1.3.64
- Ganti PIN Panel: tombol "Ganti PIN" kini sejajar 1 baris dengan ketiga field (PIN Lama, PIN Baru, Ulangi PIN Baru) — sama seperti tombol Riset Playlist.
- Input jam (Auto Shorts "Buat/Tayang", Batch Creator) yang tadinya putih kini mengikuti tema: `input[type=time]` masuk ke aturan input gelap + `color-scheme` mengikuti tema (gelap/terang) supaya ikon jam & pemilih waktunya juga gelap.
- Riwayat Job Playlist dirapatkan: baris lebih tipis (padding 6px, jeda 4px), ikon & judul sedikit lebih kecil — muat lebih banyak job dalam 1 layar.

## 1.3.63
- Semua menu: judul besar di dalam halaman dihapus (Dashboard, Task Monitor, Kalender Jadwal, Gallery, Title Generator, Batch Creator, Shorts Muse, Settings) — judulnya sudah tertulis jelas di bar atas, jadi tidak dobel lagi. Deskripsi kecil di bawahnya tetap ada.

## 1.3.62
- Musik Playlist: judul besar "♫ Musik Playlist" dihapus (tinggal deskripsi singkatnya); tombol "🔍 Riset Playlist" kini sejajar dalam 1 baris dengan keempat field — tepat di samping Durasi Max (grid 5 kolom; 2 kolom di layar sempit, 1 kolom di HP).
- Riwayat Job Playlist: judul job kini berwarna sesuai temanya (1 tema = 1 warna tetap, dari palet 8 warna) + titik warna di depan judul; arahkan kursor ke judul untuk melihat nama temanya.

## 1.3.61
- Kalender Jadwal kini TABEL beneran (TANGGAL | JAM | JUDUL | CHANNEL | STATUS): 1 baris per video, semua sebaris, judul dipotong "…" + tooltip; tanggal pakai rowspan bila 1 hari ada >1 video; yang terlambat jadi baris pemisah merah di atas. Muat jauh lebih banyak dalam 1 layar.
- Pemetaan Channel → Client dirapikan: label client tidak dobel lagi ("project-musik-1" bukan "project-musik-1 (project project-musik-1)"; client Desktop tampil "desktop (project-musik-1)") — dua client di project yang sama kini bisa dibedakan; badge Terhubung yang jadi client utama ditandai ★; kolom tabel dikunci lebarnya + ada keterangan di bawah tabel.

## 1.3.60
- Kalender Jadwal didesain ulang jadi daftar datar ala tabel: tanpa kartu bersarang — tiap hari hanya kepala tanggal bergaris bawah + baris video polos dipisah garis tipis (ikon kalender dihapus). Jauh lebih bersih & mudah dibaca; Task Monitor tidak ikut berubah.
- Musik Playlist: keempat field (Channel, Tema, Durasi Min, Durasi Max) kini sejajar dalam 1 baris (4 kolom; 2 kolom di layar sempit).
- Tabel video STATISTIK PER VIDEO: kolom CHANNEL kini tidak membungkus (nama channel selalu sebaris) — tabel tetap muat 100% tanpa scroll horizontal.
- Tabel video: judul kini selalu sebaris (dipotong "…" bila kepanjangan, judul lengkap di tooltip) dan tanpa garis bawah — semua kolom rapi sejajar.

## 1.3.59
- Kalender Jadwal dirapatkan: kartu per hari kini jauh lebih hemat tempat (header lebih tipis, baris video lebih rapat — padding & ikon diperkecil, judul 12px), satu hari dengan 1 video tidak lagi memakan satu layar penuh. Hanya kalender yang berubah; baris task di Task Monitor tetap seperti semula.
- Shorts Muse: form Buat Short Baru dan Daftar Shorts Muse kini tersusun atas-bawah (1 kolom) — dulu bersebelahan, kolom daftar sempit sampai tombol "Tayang!" terpotong di tepi.
- Musik Playlist dibuat rapat-padat: penjelasan riset dilipat jadi "ⓘ Tentang riset ini" (klik untuk buka), field/kartu/header dirapatkan — halaman yang tadinya setengah layar kosong kini jauh lebih pendek.
- Tabel video STATISTIK PER VIDEO: kolom TAYANG kini tidak membungkus (tanggal selalu sebaris "2026-11-20"), judul video menyesuaikan lebar sisanya.
- LINGKARAN ANALITIK didesain ulang biar lebih pro: penjelasan dilipat jadi "ⓘ Cara kerja", Terbaik/Terendah jadi tabel ringkas (label | judul | views/hari | bobot) dengan angka sejajar, judul formula dipendekkan + tooltip, tiap channel dipisah garis bawah.

## 1.3.58
- Tombol ↻ Refresh kini menunjukkan tanda loading: ikon berputar + teks "Memuat…", tombol nonaktif selama data diambil, lalu notifikasi "Data channel diperbarui." / "Statistik video diperbarui." (atau pesan gagal) — berlaku untuk tombol Refresh DETAIL CHANNEL dan STATISTIK PER VIDEO, termasuk saat auto-update 5 menit jalan.
- Daftar video di STATISTIK PER VIDEO dibatasi tinggi 360px dengan scroll di dalam kartu (header kolom tetap terlihat saat scroll) — halaman tidak lagi memanjang sampai 31 video.
- LINGKARAN ANALITIK: Terbaik dan Terendah kini dipisah atas-bawah (dulu satu baris panjang yang menumpuk).
- KUOTA PROJECT GOOGLE dan MONETISASI & BEBAN kini tersusun atas-bawah (1 kolom), seperti STATISTIK + LINGKARAN ANALITIK.

## 1.3.57
- Status key tanpa kurung: pill kini `1 Key` / `2 Keys` (dulu `(1 Key)` / `(2 Keys)`); status lama di database ikut dimigrasi saat deploy.
- DETAIL CHANNEL: tombol ↻ Refresh (paksa ambil data terbaru dari YouTube, lewati cache 1 jam — endpoint kini dukung `?force=1`) + tabel auto-update tiap 5 menit (posisi scroll horizontal dijaga saat render ulang).
- STATISTIK PER VIDEO: Terbaik/Terendah per channel kini tabel rapi (CHANNEL | TERBAIK | VIEWS | TERENDAH | VIEWS), judul dipendekkan dari tengah + judul lengkap di tooltip (dulu satu paragraf panjang per channel).
- STATISTIK PER VIDEO dan LINGKARAN ANALITIK kini tersusun atas-bawah (1 kolom), tidak lagi bersebelahan.

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
