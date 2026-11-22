## 1.4.12
- Kartu statistik Task Monitor kini bisa diklik (permintaan user 22 Nov 2026 — "buat ini nge link bisa gak sih?"): CHANNELS → Dashboard (tabel channel), ACTIVE → scroll ke antrean RENDER & UPLOAD QUEUE, DONE → Riwayat Channel, DIJADWALKAN → Jadwal Rutin. Class baru `.stat-link` (cursor pointer + hover: border accent + glow + naik sedikit) + tooltip "Lihat …" di tiap kartu; kartu antrean diberi id `queue-card` sebagai target scroll.

## 1.4.11
- Rapikan header Bank Lagu (komplain user 22 Nov 2026 — subjudul terpotong 2 baris, jelek): subjudul jadi "Stok lagu mentah per channel." (kata "untuk video harian" dibuang) + `white-space:nowrap` supaya sebaris. Tombol "➕ Upload Audio" kini warna mencolok (btn-p, gradasi accent + glow, sebelumnya btn-g polos) + `white-space:nowrap` supaya teksnya sebaris.

## 1.4.10
- Edit judul lagu bank MANUAL (permintaan user 22 Nov 2026 — 4 lagu Anto Panie judulnya sama semua, mau dibedakan): tombol ✏️ di kolom AKSI tabel Bank Lagu — klik, judul jadi kolom input (pola edit inline yang sama seperti edit email channel), Enter/✓ simpan, Esc/✕ batal. Backend POST /api/zumo_bank/rename {yt_id, id, title} + helper baru zumo_bank_rename (hanya indeks yang berubah, file audio tidak disentuh; judul kosong ditolak, maks 120 karakter). Judul baru langsung dipakai di deskripsi/timestamp video harian berikutnya. Subjudul Bank Lagu dipersingkat (permintaan user 22 Nov 2026): "Stok lagu mentah per channel — untuk video harian." (sebelumnya "Stok lagu mentah Zumo per channel — dipakai video harian (rakit acak, 0 kredit).").
- Tes lokal 21/21 lolos (rename 2 lagu berjudul sama, file tak tersentuh, judul kosong/id tak ada ditolak, potong 120 karakter, sintaks JS via node --check, marker endpoint + UI).

## 1.4.9
- Upload audio MANUAL ke Bank Lagu (permintaan user 22 Nov 2026 — audio sendiri, bukan cuma hasil generate Zumo): tombol "➕ Upload Audio" di halaman Bank Lagu (form: pilih file MP3/WAV maks 100 MB + judul wajib + style opsional + ☑ Instrumental). Backend POST /api/zumo_bank/upload: validasi channel + ekstensi + magic-byte (HTML berkedok .mp3 ditolak, pola sama seperti galeri) + durasi dibaca via ffprobe (file tak terbaca ditolak); lagu tersimpan lewat zumo_bank_add dengan 0 kredit, job_id 'manual', model 'Manual' — langsung bisa dipakai rakit video harian (assemble acak, 0 kredit). File temp dibersihkan sesudah simpan.
- Tes lokal 32/32 lolos (alur simpan manual MP3 asli 2 detik + meta manual, lagu manual dipilih zumo_bank_pick, magic-byte MP3/WAV, sintaks JS via node --check, marker endpoint + UI).

## 1.4.8
- Thumbnail Zumo SENADA dengan video (aturan user 22 Nov 2026): `_zumo_finish_video` kini mengambil thumbnail dari FRAME loop video channel (`_zumo_loop_frame`, baru — ekstrak frame t=2s dari `backgrounds/zumo_loop.mp4`), bukan lagi dari bg statis channel yang sama tiap hari; fallback ke bg statis bila loop tidak ada / ekstraksi gagal. Penyebab thumbnail "Slow Moonlight Drive" kemarin sama persis dengan "Midnight Rain": thumbnail memakai bg statis malam yang sama, padahal loop videonya sudah baru.
- Loop 30 detik + seamless (aturan user 22 Nov 2026 16:45 WIB, usulan user: 30 detik biar tidak terlihat + efek seamless looping transisi): loop 30 detik, gerakan berkelanjutan penuh (bukan 3x video 10 detik yang sama), crossfade 2 detik akhir ke 2 detik awal, tanpa ping-pong. Skrip baru `~/workspace/zumo/make_seamless_loop.py` (zoom pelan + xfade + verifikasi seam diff); executor cron step 5 diperbarui (10 → 20 → 30 detik). Thumbnail video hari ini (pyCJxjWEJBk) sudah diperbaiki dari frame loop baru (hash maxres berubah, tampil beda dan senada).
- Tes: loop 20 detik seamless LOLOS (seam diff 5,26 < 8, gerak tengah 17,74; 1920x1080 24fps); tes thumbnail di VPS (lihat verify_v148).

## 1.4.7
- README: panduan pasang server baru ditulis ulang (permintaan user 22 Nov 2026) — 1 perintah clone+install, blok edit cuma 1 baris (PORT), cara hubungkan ke Muse (kunci SSH dipasang installer + app.py tiap start, tanpa setting), perintah cek SSH dari Muse, dan langkah setelah instalasi (buat PIN, isi Token Bot + API key Kie di Settings).

## 1.4.6
- install.sh disederhanakan (168 → 96 baris, permintaan user 22 Nov 2026): blok edit cuma 1 baris (PORT) di atas, sisanya otomatis (IP dideteksi sendiri, folder dari lokasi script). Service systemd dibuat lewat loop (bot + operator), shortcut ziqva.sh dipadatkan. Cara hubungkan ke Muse: installer kini memasang kunci SSH Muse (muse-ssh.pub dari repo) ke ~/.ssh/authorized_keys saat instalasi (idempotent, chmod 600) + mencetak perintah cek SSH-nya — app.py tetap memasangnya tiap start sebagai cadangan. Cara pakai server baru: `git clone https://github.com/effands/muse-bot.git && cd muse-bot && sudo bash install.sh`, lalu buka panel dan isi Token Bot + API key Kie di Settings.
- Tes: bash -n OK; cuplikan pasang kunci (2x jalan = 1 baris kunci, mode 600) + unit systemd (bot/operator) terverifikasi.

## 1.4.5
- Tiru Style di tombol 🎵 saran trending (permintaan user 22 Nov 2026 — lagu populer rawan strike karena milik perorangan, jadi tiru style-nya saja): tombol 🎵 sekarang menanyakan dulu "🎨 Tiru Style" atau "📋 Jiplak (mp3 referensi)". Tiru Style = generate 3 lagu baru se-style referensi via Zumo (upload, ~20 mnt, 36 kredit) + visual baru dari tema, bukan mp3 referensi. Style prompt + 3 judul lagu diturunkan dari saran itu sendiri (kata kunci tema + komponen judul hasil riset, bukan karangan) via helper baru `_tiru_style_brief`; instrumental otomatis untuk genre instrumental (lofi/jazz/piano/sax), vokal untuk lagu anak/pop/dangdut. Jiplak = alur lama (playlist_from_url → wizard), dipindah utuh ke `_research_playlist_jiplak`. Order Malaysia tidak berubah.
- Tes lokal 30/30 lolos (brief lofi + anak-anak, payload Zumo, routing tombol rs:pl/rs:tiru/rs:jiplak).

## 1.4.4
- Bank Lagu kini menampilkan SIZE + MODEL per lagu (permintaan user 22 Nov 2026): kolom SIZE (ukuran file, "3,2 MB") + kolom MODEL (model generate Zumo, mis. "V5.5") — backend GET /api/zumo_bank kini mengembalikan `size_bytes` per trek (os.path.getsize, file hilang → 0) + `total_size` per channel + `model` dari indeks bank. Footer baru di bawah tabel: "Total: 30 lagu • 2:00:50 • 360 kredit • 245,3 MB (0,24 GB)".
- Kolom VARIAN / DURASI / SIZE / MODEL / DIBUAT / KREDIT / PUTAR / AKSI di Bank Lagu kini rata tengah (permintaan user 22 Nov 2026 — isi kolom pendek lebih enak dibaca di tengah); JUDUL tetap rata kiri.
- Tes lokal 24/24 lolos (format MB/GB, field size+model di endpoint, sintaks JS via node --check, struktur tabel + footer).

## 1.4.3
- Perbaiki kolom STATUS di \"Riwayat Channel\": baris Shorts menampilkan teks HTML mentah (`Tayang! ✅ <a href=...> [Lihat]</a>`) karena status di history_tasks berisi anchor HTML (untuk Task Monitor). Backend kini membersihkan status lewat helper baru `_clean_history_status` (buang anchor + tag apa pun) — judul sudah jadi link sendiri di Riwayat.
- Rapikan header \"Riwayat Channel\" + \"Bank Lagu\" (komplain user 22 Nov 2026 — \"penataan ini jelek banget gak profesional\", teks kepanjangan): subjudul dipersingkat jadi satu baris (\"Riwayat upload lewat pabrik per channel (90 hari terakhir).\" / \"Stok lagu mentah Zumo per channel — dipakai video harian (rakit acak, 0 kredit).\"), kontrol kanan (Channel + pill + Refresh) satu baris tanpa wrap, select 180px.
- Desain ulang bagian analisa judul (lingkaran analitik) di Dashboard (permintaan user 22 Nov 2026 — data per channel susah dibaca): tiap channel kini kartu terpisah (`.card`, pembatas jelas antar channel) dengan nama + jumlah video ternilai + terakhir disesuaikan + rentang bobot di kepala kartu; TERBAIK/TERENDAH tampil sebagai tabel 2 baris dengan label pill hijau/merah dan judul UTUH (bukan terpotong tengah); tabel formula pakai judul utuh + tanda 🏆 pada formula terbaik; durasi target jadi chip pill; \"Riwayat penyesuaian\" yang tadinya satu baris panjang kini tabel ringkas TANGGAL | JUDUL | BOBOT (lama → baru).
- Tes lokal 30/30 lolos (pembersih status, sintaks JS via node --check, struktur kartu formula + header + endpoint).

## 1.4.2
- Menu baru "Bank Lagu" di panel (permintaan user 22 Nov 2026 — bank lagu Zumo tidak terlihat di panel: file hanya ada di server uploads/zumo_bank/…). Tabel per channel: JUDUL | VARIAN | DURASI | DIBUAT | KREDIT + tombol ▶️ dengar (audio player) + 🗑️ hapus (themedConfirm, file + indeks). Backend: GET /api/zumo_bank (semua channel + total durasi/kredit + URL putar), POST /api/zumo_bank/delete; helper baru zumo_core.zumo_bank_remove (hapus file + entri indeks, tulis atomik).
- Menu baru "Riwayat Channel" di panel (permintaan user 22 Nov 2026 — pantau sejarah tiap channel: apa yang pernah di-upload, kapan, judul, jenis konten): tabel per channel (atau semua channel): TANGGAL/JAM | JUDUL (link YouTube) | KONTEN | DURASI | STATUS | VIEWS. Backend: GET /api/channel_history (?yt_id=, maks 200, terbaru dulu) — history_tasks (90 hari terakhir) + views dari video_stats_cache.json.
- Tes lokal 39/39 lolos (zumo_bank_remove, _bank_variant, sintaks JS via node --check, struktur endpoint + menu).

## 1.4.1
- Menu baru "Jadwal Rutin" di panel (permintaan user 22 Nov 2026 — jadwal rutin inti selama ini tidak punya menu: order Malaysia hanya ada di cron Muse, Shorts otomatis hanya di Settings per channel). Tabel CHANNEL | JENIS | JAM | TEMA | SAMPAI | STATUS | AKSI + tombol Tambah/Ubah/Jeda/Aktif/Hapus, nav + PAGE_META + hook go().
- Backend: schedules_db.json (atomic write, chmod 600) + helpers _schedules_load/_save/_schedules_seed_if_empty (seed SEKALI saat file belum ada dari channel daily_playlist=true: 05:00, tema 'lagu malaysia', sampai 2026-11-26; file kosong yang sudah ada TIDAK di-seed ulang) + _schedule_status (Aktif/Nonaktif/Selesai) + _validate_store_row + _sync_daily_playlist (samakan flag daily_playlist channel dengan baris playlist aktif) + _set_shorts_enabled. Endpoint GET /api/schedules (baris store + baris shorts diturunkan dari auto_shorts channel), POST /api/schedules/save (shorts -> konfigurasi auto_shorts channel via _validate_auto_shorts_cfg; playlist/zumo_bank -> upsert store), POST /api/schedules/delete (shorts hanya dinonaktifkan), POST /api/schedules/toggle.
- Modal tambah/ubah: channel, jenis (Playlist Harian / Shorts Otomatis / Bank Zumo Harian), jam, tema (teks bebas untuk playlist/bank; select niche untuk shorts), sampai tanggal, lagu/hari (bank), jam tayang + durasi (shorts), aktif. Channel + jenis dikunci saat mengubah. Hapus pakai themedConfirm. Eksekutor cron (tiap 15 menit, 04:00-23:00 WIB) membaca file ini — jam yang diubah berlaku paling lambat 15 menit kemudian; pergantian cron Malaysia ke eksekutor dilakukan sesudah deploy.

## 1.4.0
- DETAIL CHANNEL: kolom GROUP kini hanya menampilkan nama group + tombol "…" untuk ganti group (permintaan user 22 Nov 2026 — dropdown di tiap baris boros tempat). Klik "…" -> select pemilih muncul di sel itu (groupSelectHtml yang sudah ada); pilih -> simpan lewat onGroupSelect -> sel menutup jadi teks lagi; klik di luar tanpa memilih -> kembali ke teks (blur + timeout 150 mdtk, change jalan dulu). Guard render ulang poll 5 menit diperluas ke '.grp-picker' supaya pemilih yang sedang dibuka tidak terhapus; sesudah simpan sukses pemilih ditutup eksplisit di saveChannelGroup (tanpa ini guard menahan render ulang sehingga select tidak pernah kembali ke teks); batal dari input group baru yang dibuka lewat pemilih kembali ke teks (cancelEditGroup). Tabel Dashboard dan popup Kelola Group tetap pakai dropdown.
- DETAIL CHANNEL: kolom VIEW 28D dihapus (permintaan user 22 Nov 2026 — semua baris "—" karena snapshot harian baru jalan sejak 18 Nov, butuh 28 hari riwayat; kolom baru terisi ~16 Des). Header + sel baris + colspan (12 -> 11) dihapus; perhitungan snapshot di backend dibiarkan (bisa dihidupkan lagi bila diminta).

## 1.3.101
- Metadata Zumo kini INGGRIS SEO untuk tema musik (komplain user 22 Nov 2026 atas video Midnight Rain 3IdP-DUmkyE yang tayang berbahasa Indonesia: "judul nya kok bhs indonesia bukanya kmren sudah aku minta sesuai target audience luar negeri, terus judul nya kok gak seo deskripsi juga gak niat"): generator `_zumo_rewrite_title` / `_zumo_build_description` / `_zumo_build_tags` selama ini hardcode Bahasa Indonesia — perbaikan video pertama saja (fix_metadata.py, 21 Nov) tidak menyentuh generator, jadi video baru tetap Indonesia. Kini: (1) judul musik = lead track + tema + ekor kata kunci Inggris (mood + instrumen + kegunaan, dari riset tema — jazz/sax: "Relaxing Saxophone & Soulful R&B for a Quiet Evening"; piano/lofi/sleep/rain/default masing-masing ada ekornya), maks 100 karakter, aturan v1.3.82 (jangan ulang tema) tetap berlaku; (2) deskripsi musik = judul + pembuka Inggris + "Tracklist:" + timestamp + CTA subscribe Inggris + nama channel + 12 hashtag Inggris per tema; (3) tag = frasa utuh per koma dari style ("smooth jazz", "late night") + judul lagu utuh + "relaxing music"/"background music" + nama channel — dulu style dipecah per kata sehingga tag jadi sampah kata tunggal ('City', 'Dancing', 'in', 'the', 'musik santai' — terlihat di 3IdP-DUmkyE). Tema AGAMA (dzikir/sholawat/Islam) tetap template Indonesia lama (audiensnya Indonesia) — deteksi via `_zumo_is_agama_theme`. Video Midnight Rain sendiri sudah diperbaiki manual via API sebelum deploy ini (judul SEO Inggris + deskripsi Inggris + 12 tag frasa). Tes lokal 24/24 lolos (judul/deskripsi/tag jazz + piano + default + regresi agama + aturan v1.3.82).

## 1.3.99
- Render Zumo kini punya jalur cepat LOOP+COPY (keputusan user 22 Nov 2026 — "spectrum gak penting juga, yg penting cepet aja hahaha.. asal ada efek gerak2" + "Pakai loop + copy (cepat, spectrum generik)", setelah render Smooth Jazz 90s 20,5 menit makan ~76 menit): bila channel punya visual loop `uploads/<yt_id>/backgrounds/zumo_loop.mp4` (video 10 detik 1920x1080 seamless — adegan STATIS + gerakan lembut sesuai genre: asap kopi, daun, air danau, lilin, lampu kafe, bass speaker — dibuat Muse per tema, spectrum generik sudah ditanam di dalam loop), `_zumo_render` merakit video dengan CONCAT + STREAM COPY (tanpa FFT spectrum, tanpa encode 1080p) — TERBUKTI di VPS 27 detik untuk video 20,5 menit (vs ~76 menit render penuh), output 1232 dtk / 306 MB, lalu upload seperti biasa. Loop harus 1920x1080 (dicek via `_probe_video`); loop tidak ada / tidak valid / rakitan gagal -> jatuh ke render penuh (bukan gagal task), "Dibatalkan" tetap langsung naik; method tercatat 'loop-copy'. Helper baru `_zumo_visual_loop_path(yt_id)`. Render penuh lama tetap ada sebagai fallback (dan tetap dipakai video 90s + dzikir yang sudah antre). Tes lokal: proof rakitan di VPS 27 detik + tes unit jalur cepat.

## 1.3.98
- Label info penting di pesan Zumo kini DITEBALKAN (permintaan user 22 Nov 2026 — "tandai bold untuk info penting ya.. biar enak bacanya", atas screenshot ringkasan order dzikir yang dinding teks polos semua): 6 fungsi pesan Zumo — `_make_zumo_summary`, `_make_zumo_bank_summary`, `_make_zumo_assemble_summary`, `_send_zumo_created_message`, `_send_zumo_bank_created_message`, `_send_zumo_assemble_created_message` — kini memakai `*Label:*` untuk Tema/Channel/Mode/Style/Jumlah judul/Jumlah lagu/Model/Partikel/Spectrum/Jadwal/Biaya musik kira-kira/Lirik/Bahasa lirik/Vokal/Judul video, dan judul pesan sukses jadi "✅ *Sip Bos, ... udah saya buatkan!* 🎵" (menyamai gaya pesan Shorts/Long). Nilai (judul, nama channel, dsb.) tetap lewat `_tg_escape` seperti dulu. Tes lokal 27/27 lolos (semua label tebal, nilai utuh, tanpa label polos tersisa). CATATAN: deploy DITUNDA — render video 90s masih jalan (deploy = restart panel = render mati); berlaku untuk pesan Zumo berikutnya setelah deploy, pesan dzikir yang sudah terkirim tidak berubah.

## 1.3.97
- Bot kini paham tema AGAMA ISLAM (laporan user 22 Nov 2026 07:48 WIB — "harusnya bisa memahami kata ini ya"): order "buatkan video dzikir Lailahaillalloh 100x tema agama islam tanpa spectrum hanya partikel saja dan backgrund video islami looping" tadinya jatuh ke Short tema Deep Sleep Music 30 detik + "Tema 'agama' belum saya kenal". Penyebab: daftar niche bot hanya berisi sleep/lofi/piano/hujan/meditasi/fokus/anak/asmr, dan "buatkan video" tanpa penanda short/long/lagu default-nya Short. Perbaikan: (1) niche baru 'agama' (kata kunci: agama/islam/islami/dzikir/zikir/sholawat/murottal/lailahaillallah/adzan/nasheed; tombol "Agama Islam" di pilihan tema; bank hashtag islami) — dikenali di _NICHES, _parse_niche_arg, _NLU_NICHE_WORDS (urutan pertama supaya "dzikir sebelum tidur" jadi agama, bukan sleep), dan _nlu_detect_niche (tidak lagi "belum saya kenal"); (2) order dzikir/sholawat/murottal — dan order yang menyebut 'spectrum'/'partikel' (konsep render Zumo; Short tidak punya keduanya) — kini rute ke buat_zumo (video panjang), bukan buat_short; (3) spectrum 'none' (tanpa spectrum) kini opsi resmi: parser jawaban partikel menerima "tanpa spectrum" → ('snow', 'none'), label "tanpa spectrum", slot NLU mengisinya dari order ("tanpa spectrum hanya partikel saja"), _zumo_render melewati layer showfreqs (audio anull, method 'static'/'video' tanpa +spectrum) dan validasi profil menerimanya. Tes lokal 28/28 lolos (niche, routing, parser, tombol tema, render none vs white).

## 1.3.96
- Thumbnail playlist kini DIGENERATE BARU (keputusan user 22 Nov 2026 — sebelumnya foto asli video referensi dipakai apa adanya sejak v1.3.3): foto referensi dijadikan latar full-bleed yang di-blur kuat + digelapkan, supaya semua teks bawaan foto — termasuk watermark/signature channel lain di sudut mana pun — hancur jadi noda warna yang tidak terbaca; di atasnya digambar ulang judul video gaya Zumo (serif, tengah-atas, baris terakhir emas) + daftar lagu + nama channel kita di kanan bawah (emas). Daftar lagu diambil dari deskripsi video referensi (deskripsi penuh dari videos().list) dengan URUTAN YANG SAMA PERSIS seperti referensi (tidak diurutkan ulang, tidak dihapus duplikatnya; nomor ditulis ulang 01..N sesuai urutan tampil) — diuji pada 13 deskripsi referensi asli: 9 berisi daftar lagu (10–30 lagu, format "1. Judul", "01.Judul", dan chapter "00:00 Judul") dan 4 tidak berisi daftar lagu. Bila deskripsi tidak berisi daftar lagu, thumbnail dibuat TANPA tracklist (hanya foto blur + judul) — daftar lagu tidak pernah dikarang. Bila foto referensi gagal diunduh, judul + tracklist tetap digambar di atas gradien lokal. Tes lokal 24/24 lolos (parser + generator, dicek visual pada foto asli: watermark tidak terbaca, urutan lagu utuh).

## 1.3.95
- Batas timeout render Zumo kini mengikuti durasi video (gagal 22 Nov 2026 03:16 WIB — video Smooth Jazz 90s "Midnight Rain" gagal "FFmpeg Zumo Render Error: timeout (3600 dtk)"): `_run_ffmpeg_zoom` dulu pakai deadline tetap 3600 dtk, sementara render Zumo 27 menit butuh ~90 menit (0,3x realtime) — auto-retry 3x (v1.3.93) pun mentok di batas yang sama, 3x60 menit lalu gagal. Kini deadline = 6x durasi target, minimal 3600 dtk, maksimal 4 jam (video 27 menit dapat 9720 dtk / 2,7 jam). Shorts 60 dtk tidak berubah (tetap 3600 dtk). Pesan error kini menyebut batas sebenarnya, mis. "timeout (9720 dtk)".

## 1.3.94
- Perbaiki pesan Telegram yang "ditelan" pesan draft hangus (laporan user 22 Nov 2026 00:23 WIB — "kenapa gak langsung di handle muse ya"): bila ada draft buat-video / aturjadwal yang sudah basi (>30 menit), pesan bebas yang NLU-nya tidak kenal kini DITERUSKAN ke Muse lewat jembatan (seperti fallback default) — bukan hanya dibalas "draft sudah hangus" lalu berhenti. Bot tetap memberitahu draftnya hangus, tapi menambahkan "pesan Bos sudah saya teruskan ke Muse". Bila bridge gagal tulis, pesan hangus lama yang dipakai. Perintah yang NLU kenal tetap jalan lewat NLU (tidak ikut diteruskan); perintah "/" dan tombol keyboard tidak berubah. Tes lokal 9/9 kasus lolos (teruskan + kabari, bridge gagal, NLU kenal, tanpa draft).

## 1.3.93
- Auto-retry render Zumo 2-3x (permintaan user 22 Nov 2026 — "buat auto retry 2-3x dong", setelah render video Smooth Jazz 90s gagal): `_zumo_render` kini mencoba ffmpeg sampai 3x (1 + maks 2x retry, jeda 60 dtk antar percobaan, status panel "Render Zumo gagal, Auto-Retry (1/2)... 🔄"), pola sama seperti retry upload (UPLOAD_MAX_ATTEMPTS). "Dibatalkan" tidak pernah di-retry. Cek pra-render (audio/bg/durasi hilang) tetap gagal cepat tanpa retry. Kegagalan akhir (setelah 3x) tetap masuk riwayat + notif Telegram + tombol Coba Lagi seperti dulu.
- Hapus fallback video latar "mp4 apa pun" (insiden 22 Nov 2026 00:07 WIB): `_zumo_background_video_path` kini HANYA memakai file eksplisit `zumo_cozy_sax_bg.mp4`. Sebelumnya fallback itu memilih `clip-cozy-sax-01.mp4` (kereta kartun anak siang terang, sisa Shorts anak Nov 18 yang tersimpan di folder backgrounds/ Cozy Sax) sebagai latar video Smooth Jazz 90s (lounge malam gelap) — render harus dihentikan manual di menit ke-3. 3 klip anak itu sudah dipindah keluar dari backgrounds/ (ke uploads/zumo_tmp/old_bg_clips/). Tes lokal 14/14 kasus lolos (fallback ditolak, retry 2x-lalu-sukses, gagal-terus-berhenti-di-3x, Dibatalkan tanpa retry, sukses langsung 1x).

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
