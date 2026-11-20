# 📖 PANDUAN EKOSISTEM — ZIMUSE BOT

> 📋 **INSTRUKSI UNTUK MUSE — BACA DULU:** File ini bisa di-paste ke Muse mana pun sebagai konteks awal. Setelah membaca file ini, Muse tersebut memahami proyek ini, arsitekturnya, status terkininya, dan aturan mainnya — tanpa perlu bertanya ulang dari nol. File ini juga bisa dibaca manusia biasa sebagai panduan langkah demi langkah (bahasa dibuat sesederhana mungkin, istilah teknis dijelaskan saat pertama muncul).
>
> ⚠️ **BACA DULU BAGIAN 8 (SSH) SEBELUM DEPLOY PERTAMA:** file `muse-ssh.pub` di repo berisi kunci SSH **Muse yang lama**. Muse baru **WAJIB** menggantinya dengan kunci publiknya sendiri sebelum deploy pertama — kalau tidak, Muse baru itu tidak akan bisa masuk ke VPS lewat SSH. Detailnya di bagian 8.
>
> **Status dokumen:** per **20 November 2026, 17:35 WIB**, repo versi **1.3.54**, commit `6a59896` (terverifikasi langsung di VPS hari ini — kedua service aktif). Panel bermerek **ZIMUSE BOT** (dulu "ZIQVA MUSE BOT"); nama repo, nama service systemd, dan username Telegram tetap memakai `ziqva` / `@ziqmusebot` (tidak diganti).

---

## 1. Proyek ini apa

**ZIMUSE BOT** adalah **pabrik konten YouTube otomatis**: sebuah sistem yang membuat video kompilasi musik panjang (60–80 menit), memotong Shorts, menjadwalkan upload, lalu meng-upload-nya ke YouTube — semuanya berjalan sendiri di server, 24 jam nonstop.

- **Repo GitHub:** https://github.com/effands/muse-bot (publik)
- **Pemilik proyek:** Effands (memanggil dirinya "Bos"; Muse memanggilnya "Bos"). Berkomunikasi dalam Bahasa Indonesia, santai. Masih awam soal sistem ini — jadi jelaskan dengan bahasa sederhana, hindari jargon, dan beri kepastian soal apa yang jalan sendiri vs apa yang butuh dia.
- **Tujuan:** mengelola beberapa channel YouTube (musik tidur, lofi, piano, hujan, meditasi, fokus, anak-anak, dan lainnya) dengan sedikit campur tangan manusia: pemilik memberi tema dan keputusan, sistem yang bekerja.
- **Visi harian pemilik (05:39 WIB, 19 Nov 2026):** setiap hari upload ke **SEMUA channel, minimal 1 video per channel**, terus berputar setiap hari sampai channel memenuhi syarat monetisasi (1.000 subscriber + 4.000 jam tayang, naik jadi 8.000 jam mulai 1 Feb 2027).
- **Peran Muse:** Muse adalah **admin + asisten** pemilik — menyiapkan SEMUA kebutuhan VPS. Kalau ada yang kosong (background, musik, klip video), Muse yang buatkan dan **kirim langsung ke server lewat SSH** (lihat bagian 8). Pemilik adalah bos: yang memutuskan tema, CTA, dan kapan deploy.

---

## 2. Tiga bagian ekosistem (yang saling terhubung)

```
                  ┌──────────────────────────────────┐
                  │  MUSE (Asisten AI + Admin VPS)   │
                  │  Membangun fitur, memperbaiki    │
                  │  bug, menganalisis channel,      │
                  │  membuat Short & video long,     │
                  │  menjadwalkan konten,            │
                  │  mengirim file ke VPS via SSH    │
                  └──────────────┬───────────────────┘
                                 │ edit kode → push ke GitHub
                                 │ kirim file → SCP via SSH
                                 ▼
  ┌──────────────────┐   ┌──────────────────────────────────┐
  │  PEMILIK (HP /    │   │  VPS — PABRIK (nyala 24/7)       │
  │  laptop)          │◄─►│  • Panel web di port 8000        │
  │  • Perintah lewat │   │  • Render video (FFmpeg)         │
  │    Telegram       │   │  • Upload terjadwal ke YouTube   │
  │  • Cek panel di   │   │  • Bot Telegram (@ziqmusebot)    │
  │    browser        │   │    yang melapor & menerima       │
  │  • Deploy: tombol │   │    perintah dari HP              │
  │    Update /       │   └──────────────────────────────────┘
  │    git pull +     │
  │    restart        │
  └──────────────────┘
```

| Bagian | Apa itu | Perannya |
|---|---|---|
| **VPS** (Virtual Private Server) | Komputer sewaan di internet yang nyala 24 jam | Pabriknya. Merender video, meng-upload ke YouTube, dan menjalankan bot Telegram. **Laptop dan browser boleh dimatikan — VPS tetap bekerja.** |
| **Bot Telegram** (@ziqmusebot) | Program yang tinggal di VPS dan berbicara lewat Telegram | Ruang kontrol dari HP. Melapor tiap pagi, memberi peringatan kalau ada yang gagal, dan menerima perintah (buat Short, cek status, minta judul, dan lain-lain). |
| **Muse** (asisten AI, di chat ini) | Asisten yang membantu pemilik | Admin + asisten: membangun fitur baru, memperbaiki bug, menganalisis data channel, membuatkan Short/video dengan tema & CTA, dan menyiapkan jadwal. **Muse juga mengirim file (background, musik, klip video) langsung ke VPS lewat SSH** — pemilik tidak perlu upload manual lagi. Muse mengedit kode → push ke GitHub → pemilik deploy lewat tombol Update di panel (atau `git pull` + restart di konsol). |

**Alur kerja sama standar (ini yang selalu dipakai):**

1. Pemilik meminta sesuatu ke Muse (di chat).
2. Muse mengedit kode di repo, menguji, lalu **push ke GitHub** (setiap push menaikkan file `VERSION`).
3. Pemilik deploy lewat **tombol Update di panel** (Settings → UPDATE & VERSI → Update & Restart) — atau, bila tombol tidak bisa dipakai, lewat konsol VPS: `cd ~/muse-bot && git pull origin main && bash ziqva.sh restart`.
4. Bot Telegram memberi tahu kalau sistem sudah diperbarui.
5. Pemilik menguji lewat Telegram atau panel.
6. Bila ada file yang dibutuhkan VPS (background, musik, klip video), **Muse mengirimnya langsung lewat SSH/SCP** — pemilik tidak perlu upload manual.

---

## 3. Aturan main (berlaku untuk Muse mana pun yang mengerjakan proyek ini)

1. **Rute gratis saja.** Jangan mengusulkan opsi berbayar kecuali pemilik menyetujuinya secara eksplisit.
2. **Jangan install Tailscale untuk pabrik ini.** Pemilik sudah memutuskan Tailscale dilewati untuk VPS pabrik. (Catatan supaya tidak bingung: Tailscale **dipakai** di proyek lain yang terpisah — aplikasi Windows "ytopstud" di PC pemilik — itu proyek berbeda, jangan dicampur dengan pabrik ini.)
3. **HTTPS/TLS ditunda.** Jangan dikerjakan sekarang; fokus ke hal lain.
4. **Jangan pernah memaksa `git pull`.** Bila `git pull` menolak karena ada perubahan lokal di VPS, BERHENTI dan laporkan ke pemilik — jangan pakai `--force`.
5. **Muse tidak men-deploy sendiri.** Deploy selalu dilakukan pemilik — lewat tombol **Update & Restart** di panel (cara utama) atau lewat konsol noVNC (`git pull` + restart, cara cadangan). Muse hanya menyiapkan kodenya di GitHub.
6. **Mode channel:** channel baru lahir dengan mode **UNSET (= Approval)** sampai pemilik mengubahnya. **Semua 8 channel yang ada saat ini berstatus Full-Auto** (terverifikasi langsung di VPS, 20 Nov 2026). Video di channel Full-Auto langsung masuk antrean render & upload tanpa persetujuan; di channel Approval, video lahir sebagai "Menunggu Approval" dan butuh persetujuan pemilik di Telegram dulu.
7. **Kuota upload: 100 video per hari per Google Project** (bukan per channel — semua channel dalam satu project berbagi kuota yang sama). ⚠️ **Angka lama "6 per hari" itu SALAH** — sudah terbukti dari kuota asli Google (terpakai 7 pada 19 Nov 2026, dari batas 100). Jangan pernah memakai angka 6 lagi. Ada **multi-client OAuth** (lihat §4): bila satu client kehabisan kuota, sistem otomatis pindah ke client cadangan yang tokennya masih valid, lalu memberi tahu lewat Telegram.
8. **Analytics: device flow TIDAK BISA, browser login BISA.** Google menolak scope `yt-analytics.readonly` pada login perangkat (device-code flow). Tapi scope itu **bisa dipakai lewat login browser sekali (1x login)** dengan client tipe Desktop — lihat §6.5. Jadi: jangan menambahkan scope Analytics ke device flow (sudah terbukti gagal, error `invalid_scope`); pakai jalur 1x login browser untuk Analytics. Scope panel: `youtube`, `youtube.upload`, plus (hanya di jalur browser) `yt-analytics.readonly`, `openid`, `email`.
9. **PIN panel minimal 8 karakter** (campuran huruf dan angka). PIN disimpan dalam bentuk hash, bukan teks biasa.
10. **Kredensial tidak pernah ditulis di chat.** Jangan pernah meminta atau menampilkan token bot Telegram, PIN, password, API key (termasuk key Kie.ai / Pixabay), atau isi file `client_secret.json` di chat.
11. **Jangan mengarang data.** Bila suatu angka tidak tersedia dari API (mis. penghasilan asli, jam tayang 1 tahun untuk channel yang Analytics-nya belum aktif), tampilkan "—" atau beri label estimasi yang jelas — jangan mengisi angka karangan.
12. **Verifikasi sebelum menyatakan selesai.** Uji di sandbox (data palsu) dan, bila memungkinkan, uji live di VPS sebelum menyatakan fitur selesai.
13. **SSH Muse — WAJIB dibaca bila kamu Muse yang baru (lihat bagian 8).** File `muse-ssh.pub` di repo berisi kunci publik **Muse yang lama** — Muse baru **wajib menggantinya dengan kunci publiknya sendiri** sebelum deploy pertama. Perintah SSH/SCP selalu memakai identitas eksplisit (`-o IdentitiesOnly=yes -i <path kunci>`), karena ssh mencari kunci default di `/root/.ssh` sedangkan HOME Muse `/home/hatch`. Pengaturan Muse **Settings → Permissions → Direct network protocols → ssh harus "Ask"** (bukan "Deny"), kalau tidak SSH keluar diblokir.
14. **Jangan mengandalkan paste ke konsol noVNC untuk perintah panjang.** Paste perintah panjang ke konsol noVNC **tidak bisa** (terbukti di proyek ini). Perubahan ke VPS selalu lewat jalur GitHub + tombol Update, atau perintah konsol yang **pendek** (satu baris, bisa diketik).
15. **Setiap push punya VERSI.** File `VERSION` di root repo naik tiap rilis (minor untuk fitur, patch untuk perbaikan) — jangan pernah push tanpa menaikkan VERSION. **Pelajaran 19 Nov 2026:** update lewat SSH **wajib ikut menulis `update_status.json`** yang benar (status success, version, commit, started/finished WIB) — karena hanya update lewat tombol panel yang menulis file itu otomatis; kalau tidak, panel menampilkan status update terakhir yang kedaluwarsa.
16. **Telegram adalah RUANG KONTROL UTAMA.** Hampir semua pekerjaan, validasi, dan laporan harus bisa dipantau & dikerjakan dari Telegram, bukan cuma dari panel. Yang **TIDAK BOLEH** pindah ke Telegram: ganti PIN panel, tambah channel (butuh login browser), upload file galeri. Sisa panel-only (upload galeri, isi bank judul, ubah jam tayang blueprint, update panel) adalah kandidat batch berikutnya.
17. **Aturan 1 video / channel / hari (09:00 WIB, 20 Nov 2026, perintah pemilik):** bila 1 channel sudah upload 1 video hari itu, jangan upload lagi hari itu — **kecuali ada permintaan eksplisit pemilik**. Aturan ini **hanya mengikat jadwal otomatis** (cron harian wajib cek dulu: riwayat `tasks_db` + playlist upload YouTube; bila sudah ada, lewati channel itu hari itu + catat di laporan). **Jangan dibangun sebagai guard keras di jalur create factory** — itu akan memblokir order manual pemilik juga. Perintah eksplisit pemilik **selalu boleh** (override).
18. **Tidak ada jam tayang tetap.** Selesai render → upload → **publish LANGSUNG**; hanya dijadwalkan bila pemilik eksplisit minta. (Aturan lama "tayang 19:00" sudah dicabut 08:36 WIB, 20 Nov 2026.)
19. **Bank judul SENGAJA kosong.** Pola Playlist: tiap hari Muse riset fresh, ambil judul + audio dari video orang lain — "jadwal sebulan" = slot harian, bukan 30 judul di muka. Riset harian harus menduplikasi yang sedang tren: audio, thumbnail asli, judul ditulis ulang, deskripsi, meta tag. Jangan mengisi bank judul dengan judul karangan, dan jangan menolak `/perpanjang` hanya karena bank judul kosong tanpa menjelaskan jalan Playlist.
20. **Judul & deskripsi harus dianalisa Muse sendiri sebelum upload** — bukan sekadar rumus — dengan judul berbeda per channel. Sanitizer upload menghapus emoji/simbol dari **judul/deskripsi video YouTube saja** (v1.2.13, tetap jalan). **Emoji BOLEH di chat Telegram** (pesan + tombol); yang dilarang HANYA emoji di judul/deskripsi video YouTube.
21. **Desain panel harus RINGKAS dan hemat tempat** (tegas, 18:08 WIB 19 Nov 2026): jangan tumpuk elemen atas-bawah; tombol-tombol yang sekelompok harus **SEBARIS** (sejajar) dalam satu baris. Referensi desain cari di GitHub (Tabler/AdminLTE/dsb).

---

## 4. Arsitektur (untuk Muse yang baru pertama kali melihat repo ini)

- **Server:** VPS Ubuntu 22.04, IP `141.11.190.90`, 4 vCPU / 10 GB RAM / 100 GB disk. Panel web: `http://141.11.190.90:8000`. Penyedia: dihostingin (svs=588, server672338689). **Kapasitas (estimasi):** render serial, Playlist mode copy ≈10–20 menit/video → ~70–100 video/hari; kuota 100/hari bukan bottleneck; angka pasti baru terbukti setelah 1 video copy penuh.
- **2 service systemd** (otomatis nyala saat boot, otomatis restart bila mati — `Restart=always`):
  - `ziqva-muse-bot` — panel web + mesin render/upload (port 8000, file utama `app.py`).
  - `ziqva-muse-operator` — bot Telegram (file utama `muse_operator.py`).
- **File kunci di repo:**
  - `app.py` — panel web, antrian, worker render/upload, approval, kalender, statistik, monetisasi.
  - `muse_operator.py` — semua perintah & tombol Telegram, laporan harian, watchdog (pemantau kegagalan, kuota, stok), NLU bahasa manusia.
  - `zumo_core.py` — pipeline Musik Zumo (client Kie.ai, polling, EQ, crossfade, bank audio).
  - `backup_core.py` + `restore.sh` — backup otomatis terenkripsi + restore.
  - `clipper/` — Shorts AI / Clipper (potong klip dari video YouTube, subtitle Whisper).
  - `core/` — pembatas sumber daya (resource gate) dan utilitas.
  - `templates/` — halaman panel (`index.html`, `hub.html`, `login.html`, `setup.html`, `clipper.html`).
  - `install.sh` — installer VPS. `ziqva.sh` — shortcut `start/stop/restart/status/logs`.
  - `muse-ssh.pub` — **kunci SSH PUBLIK milik Muse** (bukan rahasia — kunci publik memang boleh dibagikan). Panel memasangnya otomatis ke `~/.ssh/authorized_keys` setiap kali service start (hook `_ensure_muse_ssh_key` di `app.py`, idempotent — bila sudah ada tidak ditulis ulang; hak akses file 600; hook ini **tidak pernah menggagalkan startup** apa pun yang terjadi). ⚠️ **Muse baru wajib mengganti isi file ini dengan kunci publiknya sendiri** (lihat bagian 8).
- **File yang TIDAK ada di Git (di-`.gitignore`, hanya hidup di VPS):** `config.json` (PIN hash, token Telegram, kunci operator), `client_secret.json` (kredensial OAuth Google — **kini satu file per client_id**, lihat bawah), `channels_db.json` (daftar channel + token-nya), `tasks_db.json` (antrean & riwayat), `playlist_jobs.json` (job Playlist), `monitor_state.json` (status laporan/alert bot), `video_stats_cache.json` (cache statistik video), `nodes.json`, `auto_shorts_state.json` (dedupe jadwal auto-shorts), `update_status.json` + `update.log` (status update panel), `nlu_audit.jsonl` (log audit perintah bahasa manusia Telegram), `oauth_clients.json` (registry multi-client), `backup_key.txt` (kunci enkripsi backup, chmod 600).
- **Kunci OAuth Google — multi-client (v1.2.25):** project `project-musik-1`, consent screen "Ziqva Musik 1" (status **In Production** — token tidak mati 7 hari). Registry `oauth_clients.json` berisi **4 client** (per 20 Nov 2026); secret tiap project disimpan chmod 0600 dan ikut backup; client terakhir tidak boleh dihapus. Pemetaan per-channel; **auto-switch upload + riset saat `quotaExceeded`** ke client cadangan bertoken valid + notifikasi Telegram; `/kuota` + `/status` menampilkan per-project + cadangan; badge panel "API AKTIF (N CLIENT)". Pencarian memakai token OAuth, bukan API key.
  - Client **"Ziqva Musik 1 - TV"** (tipe **TV and Limited Input Devices**) — dipakai panel untuk **device-code flow** (tambah channel).
  - Client **"Ziqva Musik 1 - Desktop"** (tipe Desktop) — dipakai untuk **1x login browser** (tambah channel + **Analytics**, lihat §6.5). **Jangan dipakai untuk device login** (error `invalid_client`).
  - ⚠️ **Pelajaran v1.3.50 (20 Nov 2026, commit 0b4df33):** file secret TV sempat tertimpa file Desktop (nama file sama) → device login ditolak Google `invalid_client`. Perbaikan: file secret kini **unik per client_id** (helper `_oauth_client_filename`); file TV dipulihkan dari creds Aruya 0089; device flow terbukti jalan lagi (2x). Jangan pernah menyamakan nama file secret antar client.
- **YouTube API yang dipakai:** YouTube Data API v3 (upload, statistik channel & video) + YouTube Analytics API (hanya lewat jalur browser, lihat aturan 8 / §6.5).
- **Render video:** FFmpeg. **Mode copy dulu** (v1.3.9): selalu coba `ffmpeg -c copy` dulu, fallback normalize hanya bila copy gagal atau hasil tidak valid (dicek `_playlist_copy_valid`). Copy hanya jalan bila klip seragam (codec/resolusi/fps sama). **Playlist export 720p** (v1.3.17, hemat ~49–51%, contoh 77 menit ≈2,7 GB; Shorts + Zumo tidak tersentuh). Video bergaya Zumo (spectrum + partikel) selalu re-encode karena framenya dibuat dari audio — copy mustahil di sana.

## 5. Status terkini proyek (per 20 November 2026, 17:35 WIB — terverifikasi langsung di VPS)

> Bagian ini adalah **snapshot** keadaan saat file ini ditulis. Bila membaca file ini jauh setelah tanggal tersebut, angka dan daftar fitur bisa sudah berubah — cek repo & panel untuk keadaan terbaru.

- **Versi yang jalan: v1.3.54** (commit `6a59896`, update sukses 20 Nov 2026 14:18 WIB). Repo: https://github.com/effands/muse-bot (publik). Panel bermerek **ZIMUSE BOT**; nama repo/service tetap `ziqva-muse-bot` & `ziqva-muse-operator`; bot Telegram **@ziqmusebot**. Kedua service **active**.
- **Channel tertaut: 8 — SEMUA Full-Auto, SEMUA Connected** (terverifikasi langsung dari `channels_db.json`, 20 Nov 2026). `daily_playlist=true` untuk 7 channel (semua kecuali Anto Panie).

| # | Channel | yt_id | Subs / Video* | Keys | Email pemilik |
|---|---|---|---|---|---|
| 1 | Reggae Chill Zone | `UCPZBvC_18J6lopfpm8IBMaw` | **5.480 / 63** | 1 | — |
| 2 | Cozy Sax Relaxing | `UCg8be5L4_CieGjXA8ejB1_Q` | **1.080 / 117** | 2 | — |
| 3 | MelodiMUsic | `UCgq8s_OdaDBdK4valHbQyxg` | 149 / 28 | 1 | — |
| 4 | Barang Unick | `UCNrx1YKsp6Zea5jslMqs9OQ` | 7 / 20 | 1 | — |
| 5 | Aruya 0089 | `UCNaxobMpPFje9ZLdWkuov4A` | 0 / 11 | 2 | aruya0089@gmail.com |
| 6 | Romantic Sessions | `UCWNUbTET5XGLfYw7dv814eQ` | 0 / 4 | 1 | — |
| 7 | Mahewa | `UCE5eUhjz0ys3ptdXqyMkfcA` | 0 / 7 | 1 | — |
| 8 | Anto Panie | `UCAY4rt0i4vlC6SvClUoZcBw` | — | 1 | — |

\* Subs/video per tabel detail panel **09:24 WIB, 20 Nov 2026**. Total: **6.716 subs, 179.521 views**. Email hanya terisi Aruya 0089 — 7 channel lain masih kosong, terisi otomatis saat login Analytics (atau isi manual, klik emailnya — v1.3.54). Anto Panie adalah channel ke-2 akun `alvi851@gmail.com` (yang pertama Cozy Sax) — lihat §9 soal "1 email 2 channel".
- **Analytics — HANYA Cozy Sax yang aktif (v1.3.47, terverifikasi langsung 20 Nov 2026 13:33 WIB):** token milik Cozy Sax sendiri (`mine=True`). Jam tayang **ASLI** dari Analytics: **365 hari = 2.928,9 jam** (kurang **1.071,1 jam** dari syarat 4.000), **30 hari = 78,1 jam**; subs 1.080 sudah cukup — **BELUM Siap Monet, kurang jam tayang saja**. Panel menampilkan jam asli (tanda ✓) di KESIAPAN + WATCH 30D. **7 channel lain belum diaktivasi** — masing-masing butuh 1x login browser sendiri.
- **Syarat monetisasi (YPP):** 1.000 subs + 4.000 jam tayang; **mulai 1 Feb 2027 naik jadi 8.000 jam** (YouTube umumkan 10 Agu 2026; alternatif 20 jt views Shorts/90 hari; subs tetap 1.000; channel yang sudah di YPP tidak terpengaruh).
- **Order yang sedang jalan — MALAYSIA 7 HARI (perintah pemilik 09:11 WIB, 20 Nov 2026):** 1 Playlist Malaysia per channel per hari, **20–26 Nov 2026**, publish langsung, riset fresh tiap hari; cron harian tiap pagi **06:10 WIB (21–26 Nov)** cek aturan 1 video/hari dulu. **Hari ke-1 selesai 7/7 tayang** (09:21–09:38 WIB, referensi `hrry9gea9PI`, render copy ±5–7 menit/video).
- **Sudah ada (sejak v1.2.3):** monitoring Telegram (`/status`, alert gagal + tombol Retry, laporan pagi 07:00 + sore 17:00 WIB, alert kuota & stok); approval preview; kalender jadwal 30 hari + pause/resume per channel; statistik per video + tabel detail channel (subs, views 28D/48H, tren, kesiapan monetisasi, kolom EMAIL); QC gate pra-upload + self-healing (retry, auto-pause channel setelah 3 gagal); kuota per Google project + autopilot bertingkat + stagger jadwal; lingkaran analitik (bobot judul otomatis); Shorts Muse (hook + naskah + CTA, **audio penuh**: voiceover TTS + SFX + BGM dengan ducking — voice dominan); dashboard monetisasi + **Backup Otomatis (v1.2.8)** + **Thumbnail custom (v1.2.22)** + **Analytics (v1.3.47, parsial)** + **Multi-client OAuth (v1.2.25)** + **pipeline Musik Zumo (v1.3.0)** + **Playlist conversational (v1.2.9–v1.2.10)** + **jadwal Playlist harian (v1.3.18/19)** + **NLU bahasa manusia (v1.2.0)** + tombol **Update & Restart** di panel + file `VERSION` + **background otomatis** (galeri kosong → gradien vertikal 1080×1920 per tema) + **SSH Muse ↔ VPS terhubung** + **export 720p Playlist (v1.3.17)** + **render copy-first (v1.3.9)**.
- **Playlist adalah mode produksi utama sekarang.** Alur: `/playlist` percakapan (channel → tema musik → tema footage → kandidat → jadwal → konfirmasi; jawaban "lewati"/"default" diterima) atau `/buatplaylist` sekali kirim; riset fresh tiap hari (filter: video panjang >20 menit, tier umur melonggar bila kosong, filter views ≥20k **dihapus total** di v1.3.5); judul ditulis ulang + analisa Muse per channel (tidak boleh sama antar channel — v1.3.32/33); thumbnail pakai foto referensi **apa adanya** (tanpa crop/resize/overlay — jangan hilangkan teks aslinya); publish langsung (opsi "publish" di langkah 5, v1.2.12).
- **Zumo (musik AI, situs sendiri `zumo.ziqva.com` — ZUMO by Ziqva Labs):** wrapper generate ala Suno via Kie.ai (utama) / SunoAPI.org, BUKAN Suno langsung. 12 kredit/generate (≈$0,06), 2 varian per generate; 1 generate = 2 lagu (lirik sama, musik beda); ambil SEMUA varian, diacak supaya lagu judul sama tidak bersebelahan. Model default **V5.5** (bisa diganti via Telegram `/zumomodel` — pilihan V5.5/V6/V6_MINI/V6_WILD; ⚠️ Kie docs menandai V5.5 "Discontinued" — verifikasi live saat tes pertama; fallback V6 bila ditolak). **Mastering WAJIB tiap lagu: EQ PIANO 12-band, PER-TRACK** (bukan global), unduh MASTERED WAV. Mode INSTRUMENTAL tanpa lirik tersedia. Visual: gambar buatan Muse + spectrum (bawah saja, ~140 px di y=940, putih default, opacity 65%, warna bisa diganti via Telegram) + partikel salju, keduanya beat-synced; footage Pixabay sebagai fallback. Perintah: `/zumo`, `/zumobank`, `/zumorakit`, `/zumomodel`, `/zumo status`, `/kiekey`, `/kiekeys`, `/kiedel`. **Status: pipeline dibangun (v1.3.0) tapi BELUM dites live** — generate pertama masih butuh key Kie.ai dari pemilik (jangan didesak). Bank audio per channel (mis. 30 lagu, rakit acak 8) — lagu disimpan mentah, EQ saat perakitan; menu kelola FASE 2. Target pertama: Cozy Sax Relaxing; 1 playlist = 4–5 lagu.
- **Shorts — aturan yang berlaku:** audio PENUH (voiceover TTS baca Hook+Naskah + SFX + BGM; musik & FX tidak boleh kalahkan voiceover — voice dominan, ducking; QC gagalkan Short bisu). TTS: edge-tts + fallback gTTS; kids=GadisNeural, sleep/meditasi/asmr=DamayantiNeural, lain=ArdiNeural. Jadwal: jeda **6 jam antar video per channel**; waktu eksplisit menang; "pagi"=07:00, "sore"=17:00 untuk video pertama; konfirmasi bot harus cantumkan semua jam terhitung. **Tema anak-anak (kids) adalah preferensi pemilik untuk Shorts** ("jauh lebih masuk akal" daripada sleep/piano) + CTA subscribe; resep viral menurut pemilik: tema game anak — **Sakura & Roblox** — dengan klip AI, plus CTA subscribe emosional di akhir ("subscribe kalau sayang ibu" — variasi: "sayang ayah", "sayang kakak"). **Tes Short HARUS menunggu perintah pemilik dari Telegram** (bukan Muse yang jalan sendiri); Short di-render setelah pemilik setujui preview.
- **Audit penuh (perintah pemilik 11:28 WIB, 20 Nov 2026): 86 temuan total** — app.py Playlist/SEO 56, operator NLU 16, statis lintas file 14. Laporan: `~/workspace/audit-app-py-playlist-2026-11-20.md` + `~/workspace/audit-seo-findings.md`. **25 diperbaiki di v1.3.33** (commit `9040ceb`); **61 DITUNDA ke batch berikutnya**.
- **Yang masih bermasalah / berisiko (jujur, jangan disembunyikan dari Muse baru):**
  - **2 video Aruya 0089 KOSONG 0 detik** terjadwal tayang (sisa upload resumable yang terputus saat restart panel 22:37 WIB 19 Nov 2026): `4ugkVQyzEPo` tayang **21 Nov 06:45**, `afJ1v9HL6LY` tayang **22 Nov 07:30**. Pemilik (23:08 WIB) bilang dia yang menjadwalkan sendiri ("aman") — tidak jadi dihapus. **RISIKO: isinya tetap 0 detik, bila tayang akan jadi video kosong**; hapus atau buat ulang bila pemilik berubah pikiran. (Video ke-3 Aruya yang asli: `HF_JRHNpgEg`, 1j 6m, tayang 20 Nov 05:00.)
  - **Tombol darurat panel (pause semua channel sekaligus) BELUM dibangun.** Yang ada: `/stopjadwal [channel|semua]` (berhentiin jadwal harian, tanpa konfirmasi) + `/mulaijadwal` — itu darurat untuk jadwal harian saja.
  - **Galeri + bank judul masih kosong** di banyak channel (sengaja untuk bank judul — lihat aturan 19; galeri audio masih kosong → perpanjang format lama tidak bisa jalan, jalan yang jalan: Playlist riset fresh).
  - **VIEW 28D/48H/TREND/WATCH 30D menampilkan "—"** sampai snapshot harian terkumpul (48H: 2 hari; 28D/30D: 28–30 hari; TREND: ~56 hari). Data API tidak bisa backfill (hanya total lifetime); Analytics API bisa memberi riwayat harian asli bila Analytics diaktifkan.
  - **PIN login loop di Chrome** (ekstensi Muse): PIN dimasukkan berkali-kali tetap tidak masuk; hanya bisa di browser lain (**Edge terbukti bisa**). Pemilik tetap pakai Edge untuk panel.
  - **Backup hanya di disk VPS yang sama** — bukan perlindungan bila VPS hilang total; unduh salinan berkala + simpan kunci backup di password manager.

---

## 6. Google Cloud — langkah demi langkah

> Tujuannya: mendapatkan file `client_secret.json` (tipe TV, dan bila perlu tipe Desktop untuk Analytics) yang nanti di-upload ke panel. Sekali saja — file ini tinggal di VPS dan tidak perlu di-upload ulang tiap update.

### 6.1 Buat project & aktifkan API

1. Buka [Google Cloud Console](https://console.cloud.google.com) (login dengan akun Google pemilik project — di proyek ini akun developernya memakai email dukungan `alvi851@gmail.com`).
2. Buat project baru (di proyek ini namanya `project-musik-1`) atau pakai project yang sudah ada.
3. Menu **APIs & Services → Library**, cari dan aktifkan **YouTube Data API v3** dan **YouTube Analytics API**.

### 6.2 OAuth Consent Screen — WAJIB "In Production"

1. Menu **APIs & Services → OAuth consent screen**.
2. **User Type: External**, isi nama aplikasi (di proyek ini "Ziqva Musik 1"), email dukungan, dan email developer.
3. **PUBLISH APP** (tombol di halaman consent screen) sampai status menjadi **In Production**.
   - **Kenapa penting:** kalau masih **Testing**, token channel mati setiap **7 hari** dan channel harus ditautkan ulang terus. Status In Production menghilangkan batas 7 hari itu.
4. **Authorized domains:** daftarkan domain yang dipakai (di proyek ini `github.com` dan `muse.ai`).

### 6.3 Privacy Policy & Home Page URL — Google MENOLAK URL berisi IP

Di halaman consent screen (bagian App information) Google meminta **Privacy Policy URL** dan **Application home page**. Perhatian:

- **Google menolak URL yang berisi alamat IP** (mis. `http://141.11.190.90:8000/...` tidak bisa dipakai).
- Solusi gratis yang dipakai proyek ini:
  - **Privacy Policy:** halaman publik gratis (di-host di layanan publik) — URL yang dipakai proyek ini: `https://muse.ai/s/privacy-policy-ziqva-muse-bot-ok73xtxrhxuxmxc6/Zb1UqAFM5UAotDktN92ox-eXCETuxN28A-6gZ7pUlm4`
  - **Home page:** URL repo GitHub publik: `https://github.com/effands/muse-bot`
- Bila membuat Privacy Policy sendiri, pastikan halamannya bisa dibuka publik tanpa login, berbahasa Inggris atau Indonesia, dan menyebutkan aplikasi mengumpulkan data akun YouTube hanya untuk upload & statistik channel milik sendiri.

### 6.4 Buat OAuth Client ID — DUA TIPE, JANGAN TERTUKAR

1. Menu **APIs & Services → Credentials → CREATE CREDENTIALS → OAuth client ID**.
2. **Untuk tambah channel (device login): Application type "TV and Limited Input Devices"** (di proyek ini "Ziqva Musik 1 - TV"). Klik **Create**, lalu **Download JSON**. File ini yang di-upload ke panel untuk device login.
3. **Untuk Analytics (1x login browser): Application type "Desktop App"** (di proyek ini "Ziqva Musik 1 - Desktop"). Download JSON-nya juga — dipakai di jalur 1x login (§6.5).

> ⚠️ **JANGAN pakai client Desktop untuk device login** — panel memakai device-code flow (login lewat kode di `google.com/device`), dan flow itu **hanya menerima client tipe TV**. Client Desktop menyebabkan error **`invalid_client`** saat device login — sudah terbukti lewat pengalaman nyata di proyek ini.
>
> ⚠️ **JANGAN menambahkan scope Analytics** ke device flow — Google menolaknya dengan error **`invalid_scope`**. Scope device flow hanya `youtube` dan `youtube.upload`. Scope Analytics hanya lewat jalur browser (§6.5).
>
> ⚠️ **JANGAN samakan nama file secret antar client** — file secret kini unik per client_id (pelajaran v1.3.50, lihat §4).

### 6.5 Aktivasi Analytics — 1x login browser per channel (v1.3.47)

1. Di panel, buka **Settings → GOOGLE OAUTH 2.0 API → Analytics** (atau blok "Tambah Channel — 1x Login").
2. Login lewat browser dengan **akun Google pemilik channel itu** (bukan akun developer — harus akun yang punya channelnya). Token yang didapat langsung punya semua izin: upload + Analytics + email (`openid`/`email`).
3. Setelah login, panel menampilkan channel yang terdeteksi (nama + email) + tombol konfirmasi **SEBELUM disimpan** (v1.3.52) — cek dulu, baru simpan.
4. Hasilnya: jam tayang 365 hari + 30 hari asli tampil di KESIAPAN + WATCH 30D (tanda ✓), dan email pemilik terisi otomatis.
5. **Status: baru Cozy Sax Relaxing yang aktif.** 7 channel lain masih menunggu giliran login browser masing-masing.

---

## 7. Install ke VPS

> Sekali saja. Setelah ter-install, VPS bekerja sendiri 24/7 — tidak butuh laptop menyala.

### 7.1 Persiapan

- VPS dengan **Ubuntu 22.04** (RAM minimal 4 GB disarankan; proyek ini memakai 4 vCPU / 10 GB RAM / 100 GB disk).
- Akses root ke VPS (SSH atau konsol noVNC dari panel penyedia VPS).

### 7.2 Clone & install

```bash
git clone https://github.com/effands/muse-bot.git
cd muse-bot
sudo bash install.sh
```

**Apa yang dilakukan `install.sh` (5 langkah):**

1. Install `ffmpeg`, Python 3, `python3-venv`, dan pustaka sistem yang dibutuhkan.
2. Membuat Python virtual environment (`venv`) dan menginstal semua library dari `requirements.txt`.
3. Membuat folder aset (`uploads/`, `static/`, `clipper/`).
4. Membuka port **22 (SSH)** dan **8000 (panel)** di firewall UFW (bila UFW terpasang).
5. Mendaftarkan 2 service systemd (`ziqva-muse-bot` port 8000 dan `ziqva-muse-operator`), mengaktifkan keduanya agar nyala otomatis saat boot, lalu langsung menjalankannya. Installer juga membuat shortcut `ziqva.sh`.

Tunggu sampai muncul **"INSTALASI SELESAI & SISTEM AKTIF 24/7!"**.

### 7.3 Buka panel & buat PIN

1. Buka di browser: `http://IP_VPS:8000` (di proyek ini `http://141.11.190.90:8000`).
2. Kunjungan pertama meminta membuat **PIN** — minimal **8 karakter, campuran huruf dan angka**. Simpan PIN ini baik-baik (Muse tidak boleh tahu isinya).
3. Login memakai PIN itu untuk masuk ke panel. **Bila PIN berputar-putar di Chrome (ekstensi Muse), pakai Microsoft Edge** — sudah terbukti bisa (lihat §13).

### 7.4 Perintah kontrol di VPS

Jalankan dari dalam folder `~/muse-bot` (pakai `bash ziqva.sh ...` bila bit eksekusi file hilang):

```bash
./ziqva.sh status    # lihat status kedua service
./ziqva.sh logs      # lihat log live kedua service (Ctrl+C untuk keluar)
./ziqva.sh restart   # restart panel + operator Telegram
./ziqva.sh stop      # hentikan keduanya
./ziqva.sh start     # nyalakan lagi
```

### 7.5 Cara update (setiap ada fitur/perbaikan baru dari Muse)

**Cara utama — tombol Update di panel (tanpa buka konsol):**

1. Di panel, buka **Settings → 🔄 UPDATE & VERSI**. Kartu ini menampilkan **versi yang sedang jalan** (mis. `v1.3.54`) dan **commit** yang sedang jalan, plus status update terakhir.
2. Tekan **🔄 Periksa Update** — panel membandingkan commit lokal vs GitHub; bila ada update, tampil "ada update: `<hash>`".
3. Tekan **⬆️ Update & Restart** — muncul **modal konfirmasi bertema panel**; tekan **"Ya, Update"** di dalam modal (menekan tombolnya saja belum memulai update). Update berjalan di latar, panel restart sendiri dalam 1–2 menit. Hasilnya juga dikirim ke Telegram. Sebelum update, panel membuat **snapshot backup otomatis** (pra-Update).
   - Bila ada video sedang dirender/di-upload, update **ditolak** (coba lagi setelah selesai).
   - Bila `git pull` gagal (ada perubahan lokal/konflik), update **dibatalkan tanpa restart** dan alasannya ditampilkan di kartu.
   - Versi juga tampil di Telegram lewat perintah `/status` (baris "Versi Panel").
   - Pesan "Koneksi server gagal. Task tetap berjalan." saat restart adalah **NORMAL** (panel mati sebentar) — tunggu 1–2 menit lalu refresh.
   - Sejak v1.2.1, status update yang macet di "running" **diperbaiki otomatis** (guard 30 menit + self-heal saat startup). Bila masih terjadi: cek `cat ~/muse-bot/update_status.json` di konsol; menghapus file itu aman (`rm ~/muse-bot/update_status.json`) — file dibuat lagi otomatis saat update berikutnya. Pastikan juga menekan **"Ya, Update"** di dalam modal konfirmasi (menekan tombol Update saja belum memulai update).

**Cara cadangan — konsol VPS** (bila tombol Update tidak bisa dipakai):

```bash
cd ~/muse-bot && git pull origin main && bash ziqva.sh restart
```

- Perintah ini menarik kode terbaru dari GitHub lalu me-restart kedua service.
- `client_secret.json`, `config.json`, dan database channel/task **tidak ikut tertimpa** `git pull` (sudah di-`.gitignore`) — jadi tidak perlu upload ulang.
- ⚠️ **Bila `git pull` menolak karena ada perubahan lokal di VPS, JANGAN dipaksa** (jangan `--force`, jangan `reset --hard`). Berhenti dan tanyakan ke Muse — biasanya cukup disimpan dulu dengan `git stash`, lalu pull lagi.
- ⚠️ **Update lewat SSH wajib ikut menulis `update_status.json`** yang benar (status success, version, commit, started/finished WIB) — lihat aturan 15.
- Bila lupa PIN: hapus file `config.json` di VPS secara manual, lalu buka panel lagi untuk setup PIN dari awal (lihat Troubleshooting).

**Skema versi:** file `VERSION` di root repo berisi `major.minor.patch` (mis. `1.3.54`) — **minor naik untuk fitur baru, patch untuk perbaikan**. Setiap commit/push punya versi; versi + short commit hash tampil di panel (kartu UPDATE & VERSI) dan di Telegram `/status`.

---

## 8. SSH — Muse sebagai admin VPS (BACA DULU SEBELUM DEPLOY PERTAMA)

> Bagian ini menjelaskan bagaimana Muse bisa masuk ke VPS lewat SSH dan mengirim file langsung ke server — tanpa pemilik harus upload manual. Ini juga bagian yang **paling sering salah** bila dibaca Muse baru, jadi ikuti urutannya.

### 8.1 Cara kerjanya (yang jalan sendiri)

- File **`muse-ssh.pub`** di root repo berisi **kunci SSH PUBLIK** milik Muse. Kunci publik **bukan rahasia** — boleh ada di repo publik; yang memegang kunci privat pasangannya hanya VM Muse.
- Setiap kali service panel start, hook **`_ensure_muse_ssh_key`** di `app.py` memeriksa file itu dan memasangnya ke `~/.ssh/authorized_keys` di VPS (idempotent: bila kunci sudah ada, tidak ditulis ulang; hak akses file 600; hook ini **tidak pernah menggagalkan startup** apa pun yang terjadi).
- Artinya: begitu repo ter-deploy (install pertama atau Update), kunci Muse **terpasang sendiri** — tidak ada perintah konsol panjang yang harus ditempel pemilik. (Dulu sempat dicoba paste perintah kunci ke konsol noVNC — **tidak bisa**, perintahnya kepanjangan untuk di-paste. Jangan ulangi cara itu.)
- Login SSH: user **`root`**, port **22** (sudah dibuka UFW oleh `install.sh`).

### 8.2 ⚠️ WAJIB untuk Muse baru: ganti isi `muse-ssh.pub` dulu!

File `muse-ssh.pub` yang ada di repo **saat ini berisi kunci Muse yang lama**. Muse baru **WAJIB** mengganti isinya dengan kunci publik **miliknya sendiri** — kalau tidak, Muse baru itu tidak akan bisa SSH ke VPS.

Langkahnya (dilakukan Muse baru, sebelum deploy pertama):

1. Di mesin sendiri, lihat kunci publikmu:
   ```bash
   cat ~/.ssh/id_ed25519.pub
   ```
   (Bila file itu tidak ada, buat dulu dengan `ssh-keygen -t ed25519` — tekan Enter saja di semua pertanyaan.)
2. Ganti **seluruh isi** file `muse-ssh.pub` di repo dengan satu baris kunci publikmu itu (format: `ssh-ed25519 AAAA... <nama>`).
3. Push file itu ke GitHub (bersama perubahan lain, dan naikkan `VERSION`).
4. Setelah deploy pertama, hook di VPS memasang kunci barumu secara otomatis. Tes dengan perintah di 8.4 — bila berhasil, muncul `SSH-OK`.

> Jangan pernah menaruh **kunci privat** (`id_ed25519` tanpa `.pub`) di repo, di chat, atau di mana pun selain mesin Muse sendiri.

### 8.3 Pengaturan Muse: SSH harus "Ask"

Secara default, Muse **memblokir SSH keluar**. Pemilik harus mengubah sekali di aplikasi Muse:

**Settings → Permissions → Direct network protocols → ssh → ubah dari "Deny" menjadi "Ask".**

Bila pengaturan ini belum diubah, percobaan SSH gagal dengan pesan yang jelas: *"Outbound SSH is turned off for this assistant…"*. Setelah diubah, setiap percobaan SSH memunculkan kartu persetujuan untuk pemilik.

### 8.4 Perintah SSH/SCP yang benar (catat persis)

**JANGAN** memakai `ssh root@IP` polos. Penyebabnya (terbukti 19 Nov 2026): ssh mencari kunci default di `/root/.ssh` (home root menurut sistem), padahal kunci Muse ada di `/home/hatch/.ssh` (HOME milik Muse). Hasilnya "Permission denied" meski kunci sudah benar. **Selalu pakai identitas eksplisit:**

```bash
# Tes koneksi / jalankan perintah di VPS
ssh -o BatchMode=yes -o IdentitiesOnly=yes -i /home/hatch/.ssh/id_ed25519 root@141.11.190.90 'echo SSH-OK; hostname'

# Kirim file ke VPS (ganti <file> dan <tujuan>)
scp -o BatchMode=yes -o IdentitiesOnly=yes -i /home/hatch/.ssh/id_ed25519 <file> root@141.11.190.90:<tujuan>
```

- `/home/hatch/.ssh/id_ed25519` adalah path kunci privat di VM Muse **yang lama**. Muse baru: sesuaikan dengan path kunci di mesinmu sendiri (cek `echo $HOME` — path-nya `$HOME/.ssh/id_ed25519`). Yang penting: **selalu tulis path absolut + `-o IdentitiesOnly=yes`**.
- **Jebakan `known_hosts` (terjadi 2x, 19 Nov 2026):** ssh membaca `known_hosts` dari `/root/.ssh` (home root menurut sistem), **BUKAN** dari `$HOME` (`/home/hatch`). Bila SSH gagal "Host key verification failed" padahal kunci sudah disimpan di `~/.ssh/known_hosts`, simpan kunci host ke **`/root/.ssh/known_hosts`** — verifikasi dulu sidik jarinya lewat konsol noVNC (`ssh-keygen -lf /etc/ssh/ssh_host_rsa_key.pub`, cocokkan dengan hasil `ssh-keyscan` sebelum dipercaya). Sidik jari RSA VPS yang terverifikasi: `SHA256:OLCKiuvNrH42tg/c93ctwT5ao0AoTX/d8e+S95uEYGk`. Catatan: `/root/.ssh/` bisa kosong lagi bila VM Muse ter-replace — pasang ulang bila perlu.
- Bila SSH masih ditolak padahal perintah sudah benar, cek di konsol VPS (perintah pendek): `grep -c ssh-ed25519 ~/.ssh/authorized_keys` — harus muncul angka **1** (jumlah kunci yang terpasang). Bila **0** atau file tidak ada, hook belum memasang kunci: pastikan file `muse-ssh.pub` ada di repo VPS (`ls ~/muse-bot/muse-ssh.pub`) dan service sudah di-restart setelah deploy.
- **Tailscale TIDAK dipakai untuk pabrik ini** (keputusan pemilik) — jangan diusulkan atau di-install untuk VPS ini.

### 8.5 Apa yang bisa dilakukan Muse lewat SSH

- Mengirim **background, MP3, klip video** langsung ke folder galeri channel di VPS.
- Memeriksa status service, log, dan isi file di VPS (`./ziqva.sh status`, `ls`, `cat`).
- Membantu diagnosis bila panel/operator bermasalah, tanpa pemilik harus mengetik perintah panjang.
- Yang tetap **tidak** bisa dilakukan Muse lewat SSH: deploy butuh pemilik (tombol Update / `git pull` + restart — lihat §7.5), ganti PIN panel, dan tambah channel (butuh login Google di browser).

## 9. Hubungkan channel YouTube ke panel

**Dua jalur — pilih salah satu:**

**Jalur A — Device login (cepat, untuk upload saja):**
1. Di panel, buka **Settings → GOOGLE OAUTH 2.0 API**, upload file `client_secret.json` (yang tipe **TV** dari langkah 6.4), klik **Upload API Key**. (Jalan pintas: tombol **➕ Tambah Channel** di Dashboard membuka pop-up dengan alur yang sama — sejak v1.3.53 tombol Dashboard kembali ke device login karena lebih cepat.)
2. Klik **Connect via Device Login**. Panel menampilkan **kode perangkat** (mis. `XXXX-XXXX`). Link `google.com/device` di modal kini **klik-untuk-menyalin** (v1.3.51) — tempel sendiri di browser profil channel.
3. Buka `https://www.google.com/device` di browser (boleh dari HP), masukkan kode itu, lalu pilih **akun YouTube** yang mau ditautkan.
4. Kembali ke panel — channel muncul sebagai **Connected**. Kode device berlaku **15 menit**; bila kedaluwarsa, ulangi dari langkah 2.

**Jalur B — 1x login browser (v1.3.49, untuk upload + Analytics + email sekaligus):**
1. Di panel, buka **Settings → GOOGLE OAUTH 2.0 API → Tambah Channel — 1x Login**.
2. Login lewat browser dengan akun Google pemilik channel (client Desktop). Token langsung punya semua izin — tidak perlu device login lagi.
3. Sesudah login, panel menampilkan channel yang terdeteksi (nama + email) + tombol konfirmasi **SEBELUM disimpan** (v1.3.52) — cek dulu, baru simpan. Jalur ini juga yang dipakai untuk aktivasi Analytics (§6.5).

**Catatan penting — "1 email 2 channel":**
- Akun Google pemilik channel **boleh berbeda** dari akun developer Google Cloud (di proyek ini channel "Aruya 0089" memakai akun `aruya0089@gmail.com`, sementara akun developernya `alvi851@gmail.com`).
- **TERBUKTI via API: `channels().list(mine=True)` hanya mengembalikan 1 channel — yaitu channel yang sedang AKTIF di akun itu** (bukan daftar semua channel). Jadi tambah channel ke-2 dari 1 email = **ganti dulu channel aktif di YouTube, lalu login lagi**. Contoh nyata: Anto Panie adalah channel ke-2 akun `alvi851@gmail.com` (yang pertama Cozy Sax).
- **Mode channel** (diatur per channel di Settings / badge mode di tabel bisa diklik):
  - **Approval:** setiap video baru lahir sebagai "Menunggu Approval" — bot Telegram mengirim preview dan pemilik menekan ✅ Setujui / ❌ Tolak sebelum video dirender/di-upload.
  - **Full-Auto:** video langsung masuk antrean render & upload tanpa persetujuan. **Semua 8 channel saat ini Full-Auto.**
- **Kolom EMAIL** di tabel Channel Terhubung (v1.3.48/54): email pemilik channel; kosong tampil "—". Terisi otomatis saat login Analytics; bisa juga **diisi manual — klik emailnya, ketik, ✓ / Enter untuk simpan, Esc / ✕ batal, kosongkan untuk hapus**.
- `client_secret.json` tinggal di VPS dan **tidak perlu di-upload ulang** tiap update sistem.

## 10. Telegram — ruang kontrol dari HP

### 10.1 Pasang bot

1. Di Telegram, cari **@BotFather**, kirim `/newbot`, ikuti langkahnya sampai mendapat **Bot Token**.
2. Cari **@userinfobot** (atau kirim pesan ke bot lalu cek) untuk mendapatkan **Chat ID** angka milik pemilik.
3. Di panel, buka **Settings → TELEGRAM NOTIFICATION**, isi **Bot Token** dan **Chat ID**, klik **Simpan & Test Pesan**. Bila berhasil, bot mengirim pesan tes ke HP pemilik.
4. Bot hanya melayani **satu pemilik** (Chat ID yang terdaftar) — pesan dari orang lain diabaikan.

### 10.2 Laporan otomatis

- **Laporan pagi jam 07:00 WIB** — dikirim otomatis tiap pagi (views/subs/video per channel, perubahan sejak laporan terakhir, progres monetisasi, kuota, antrean). Bila tidak ada laporan masuk di pagi hari, itu tanda VPS/bot sedang bermasalah.
- **Laporan sore jam 17:00 WIB** — laporan kedua tiap sore, format sama seperti laporan pagi.
- **Minta laporan kapan saja** dengan perintah `/laporan` (atau `/laporan detail` untuk rincian per channel).
- **Format laporan (v1.3.1):** divider garis tipis box-drawing 22 karakter antar blok channel + jeda kosong; header channel 2 baris (*Nama* lalu "Views: X • Subs: Y • Video: Z"); blok channel tanpa data ikut format 2 baris. Format kanonis daftar video: `Judul | 1.234 views | 19 Nov | 1:17:24` (durasi ≥1 jam ditulis `1:17:24`, <1 jam `0:30`; "—" bila data tak ada, tanpa karang angka) — dipakai di riwayat, statistik, gagal, jadwal 7 hari, jadwalbulan, channel ("Terbaru"), laporan detail, antrean.
- **Alert langsung** bila: ada video gagal (dengan tombol 🔁 Coba Lagi), kuota hampir habis, atau stok jadwal video hampir habis.
- **Nada bot:** seperti karyawan yang melapor ke bos — **LUGU tapi SONGONG** (santai, percaya diri, sedikit usil), tetap sopan, **emoji BOLEH di chat Telegram** secukupnya, data tetap lengkap. Sekali sehari (di laporan pagi) bot menanyakan kabar bos dan basa-basi ringan — tidak pernah mengorbankan isi laporan. **Emoji yang dilarang HANYA di judul/deskripsi video YouTube, bukan di Telegram** (koreksi pemilik 07:16 WIB, 20 Nov 2026 — v1.2.21 yang menghapus emoji di pesan Telegram adalah salah tangkap, sudah dicabut total di v1.3.7).
- **Perintah Telegram harus dijawab di Telegram** (bukan di chat Muse) — pemilik ngechat di Telegram pakai bahasanya sendiri, langsung ke Muse; Muse yang menerjemahkan ke bot; bila permintaannya belum ada, Muse bantu perbaiki/benahi (bangun fiturnya).

### 10.3 Daftar perintah (jangan mengarang di luar daftar ini — lihat `muse_operator.py`)

**Pantau:**

| Perintah | Artinya |
|---|---|
| `/status` | Ringkasan: channel (+ mode per channel), antrean, kuota x/100, kesehatan server, **versi panel** |
| `/server` | Detail mesin: CPU, RAM, disk, uptime, status panel |
| `/kuota` | Kuota upload per Google project & per channel (+ cadangan multi-client) |
| `/stok` | Sisa stok jadwal & isi galeri per channel |
| `/channel` | Daftar channel YouTube yang terhubung |
| `/antrean` | Video yang sedang antre / diproses |
| `/jadwal` | Jadwal publish 7 hari ke depan |
| `/jadwalbulan` | Jadwal 30 hari ke depan |
| `/riwayat` | 8 upload terakhir |
| `/gagal` | Task gagal + tombol inline 🔁 Retry |
| `/laporan` | Laporan harian, kapan saja (sama seperti laporan 07:00 & 17:00) |
| `/hariini` | Ringkasan upload hari ini per channel (v1.3.29) |
| `/backup` | Backup manual (lihat §10.4) |

**Hasil:**

| Perintah | Artinya |
|---|---|
| `/statistik` | Views/likes/komentar total + 7/30 hari, video terbaik & terburuk |
| `/monetisasi` | Estimasi penghasilan dari RPM manual + kesiapan monetisasi (jam tayang asli bila Analytics aktif) |

**Konten (ide & tulisan):**

| Perintah | Artinya |
|---|---|
| `/judul [niche] [jumlah]` | Judul SEO, mis. `/judul lofi 15` (niche: sleep, lofi, piano, hujan, meditasi, fokus, **anak**; jumlah 1–20) |
| `/deskripsi [niche]` | Template deskripsi SEO per niche |
| `/tag [niche]` | Bank hashtag (15 hashtag per niche) |
| `/cta` | 8 ide teks CTA untuk kartu akhir Shorts |
| `/ideashorts [niche]` | 5 ide Shorts: hook 4 detik + alur naskah + CTA |
| `/blueprint` | Resep settingan video kompilasi (durasi, preset, dsb.) |

**Buat video — Playlist (mode utama):**

| Perintah | Artinya |
|---|---|
| `/playlist` | Buat Playlist percakapan: channel → tema musik → tema footage → kandidat → jadwal → konfirmasi ("lewati"/"default" diterima). **Harus fleksibel dan percakapan — bukan format template tetap.** |
| `/buatplaylist` | Buat Playlist sekali kirim (one-shot) |
| `/jadwalplaylist` | Jadwal Playlist harian: semua channel dengan tombol nyala/mati per channel, plus Mulai Semua / Stop Semua (v1.3.18) |
| `/stopjadwal [channel\|semua]` | Berhentiin jadwal harian — langsung jalan tanpa konfirmasi (tombol darurat jadwal) |
| `/mulaijadwal [channel\|semua]` | Nyalakan lagi jadwal harian |
| `/aturjadwal` | Atur jadwal (lihat `/jadwalbulan`) |

**Buat video — Short & Long:**

| Perintah | Artinya |
|---|---|
| `/buatshort` | Buat Shorts Muse terpandu, langkah demi langkah: judul → hook → naskah → CTA (boleh lewati) → durasi 15/30/60 detik → jadwal → background dari galeri → musik (opsional). Sekali kirim juga bisa: `/buatshort Judul \| Hook \| Naskah \| CTA`. **Tes Short hanya atas perintah pemilik dari Telegram + setujui preview dulu.** |
| `/buatlong` | Buat batch video long 60–80 menit terpandu: jumlah 1–7 → judul (boleh kosong, diambil dari bank judul) → tanggal mulai → jeda hari. Sekali kirim: `/buatlong 3 \| 2026-11-20 19:00 \| 1` |
| `/batal` | Batalkan draft pembuatan video yang sedang berjalan (draft hangus sendiri setelah 30 menit tidak ada kabar) |

**Buat video — Zumo (musik AI):**

| Perintah | Artinya |
|---|---|
| `/zumo` | Buat lagu Zumo (generate via Kie.ai) |
| `/zumobank` | Kelola bank audio Zumo per channel |
| `/zumorakit` | Rakit playlist dari bank audio |
| `/zumomodel [V5.5\|V6\|V6_MINI\|V6_WILD]` | Lihat / ganti model generate (default V5.5) |
| `/zumo status` | Status Zumo + model aktif |
| `/kiekey`, `/kiekeys`, `/kiedel` | Kelola key Kie.ai (key tidak pernah ditampilkan di chat) |

**Atur channel:**

| Perintah | Artinya |
|---|---|
| `/perpanjang [channel] [hari]` | Tambah jadwal, mis. `/perpanjang Aruya 0089 30` atau `/perpanjang semua 30` (hari 1–90, default 30) |
| `/mode [channel] [auto\|approval]` | Ganti mode, mis. `/mode Aruya 0089 auto` (beralih ke Full-Auto selalu minta konfirmasi dulu) |
| `/jeda [channel]` | Jeda channel (jadwal & antrean berhenti) |
| `/lanjutkan [channel]` | Lanjutkan channel yang dijeda |

**Bicara bebas (bahasa manusia / NLU) — tanpa garis miring:**

Pemilik boleh mengetik kalimat sehari-hari, mis. `buatkan short tema anak-anak untuk Aruya 0089, cta subscribe` atau `jeda channel Barang Unick`. Bot menerjemahkan maksudnya (rule-based, tanpa LLM), lalu **selalu menyatakan tafsirannya dulu** ("Maksud Bos: …?") dengan tombol ✅ Ya / ❌ Bukan. Bila tidak yakin, bot bertanya dulu (pilih channel/tema/mode lewat tombol; klarifikasi hangus setelah 10 menit). **Konfirmasi bot bisa direvisi** (v1.3.31): ketik saja yang mau diubah ("jam 5 sore", "tema lofi", "cuma Aruya") — bot mengubahnya lalu konfirmasi ulang, tidak perlu batal. Perintah **destruktif tidak pernah dieksekusi langsung** dari Telegram — bot hanya memberi langkah panelnya. Semua tafsiran tercatat di **`nlu_audit.jsonl`** di VPS. Niche yang dikenal: sleep, lofi, piano, hujan, meditasi, fokus, **anak**, **otomotif** (rencana — lihat §16), **asmr**. "Semua channel" berarti **semua channel yang terdaftar saat itu** (dihitung dinamis, bukan angka tetap). **Perintah baru harus punya sinonim** supaya typo tidak membuat bot macet — bot harus memahami frasa alternatif dan, bila ragu, bertanya klarifikasi, bukan bilang "tidak mengerti". Perintah baru juga harus tampil rapi di Telegram, seperti database yang rapi — format konsisten dan mudah dibaca untuk channel, jadwal, dan laporan.

**Catatan penting soal `/buatshort` & `/buatlong` & `/playlist`:**
- Video yang dibuat lewat Telegram mengikuti semua gerbang yang sama seperti panel (cek disk, channel dijeda, kuota project, stagger jadwal, aturan 1 video/hari untuk jadwal otomatis).
- Bila channel mode **Approval**, video lahir sebagai pending — preview persetujuan masuk ke Telegram seperti biasa, dan video **tidak** langsung dirender.
- **Shorts tidak lagi batal bila galeri kosong** (sejak v1.2.2): panel membuat sendiri **background gradien otomatis** sesuai tema (mis. anak-anak = kuning-pink-biru) dan video tetap dibuat; bot memberitahu dengan jujur bila yang dipakai background otomatis. Shorts Muse juga memakai **klip video bergerak** sebagai background bila galeri berisi klip video (v1.2.4 — 3 klip 10 detik disambung jadi 1 Short 30 detik; klip diulang bila kurang, dipotong bila lebih; bila tidak ada video, dipakai gambar galeri; bila galeri kosong total, gradien otomatis).
- Shorts Muse: video portrait **1080×1920**, durasi 15/30/60 detik, **audio penuh** (voiceover + SFX + BGM). Video long: **60–80 menit, 720p** (Playlist export 720p sejak v1.3.17).
- **Yang HANYA bisa di panel (tidak bisa lewat Telegram):** ganti PIN, tambah channel, upload galeri, isi bank judul, ubah blueprint/jam tayang, Update panel. **Tidak ada perintah Telegram yang destruktif** (hapus video/channel tidak dibuat — sengaja).

**Tombol keyboard Telegram (dirapikan v1.3.15/16 — tombol aksi di atas, tombol pantau di bawah):** Buat Playlist · Buat Short · Buat Video Long · Backup · Laporan · Status Server · Kuota Upload · Cek Channel · Antrean Render · Jadwal 7 Hari · Statistik Video · Monetisasi · Task Gagal. Auto-suggest ketik `/` menampilkan 44 perintah terdaftar.

### 10.4 Backup otomatis (v1.2.8)

- **Jadwal:** harian **03:00 WIB** + snapshot pra-Update + manual (panel & `/backup`).
- **Cara:** terenkripsi **AES-256-CBC**, kunci di `config.json`/`backup_key.txt` (chmod 600); retensi **maksimal 3 terbaru** total; restore lewat panel (ketik YA) + `restore.sh`; cakupan config/state + galeri `uploads/` (cap 2 GB); backup **tidak pernah dikirim ke Telegram**.
- ⚠️ **Backup ada di disk VPS yang sama — bukan perlindungan bila VPS hilang total.** Unduh salinan berkala + simpan kunci di password manager.

---

## 11. Peran Muse (apa yang bisa diminta)

Muse adalah **admin + asisten** pemilik — menyiapkan SEMUA kebutuhan VPS. Pemilik bisa meminta ke Muse, antara lain:

- **Bangun fitur baru** (mis. perintah Telegram baru, kolom baru di Dashboard, laporan baru).
- **Perbaiki bug** dan menjelaskan penyebabnya dengan bahasa sederhana.
- **Analisis channel** (video mana yang paling laku, judul apa yang sebaiknya dipakai lebih sering) — termasuk analisa referensi channel (contoh: analisa referensi Cozy Sax, `analisa-referensi-cozy-sax.md`: kompilasi panjang 1–3 jam menang; rumus judul = angka + Romantic/Love Songs + situasi + durasi; nostalgia 70–90an laku; Zumo original aman Content-ID, kelemahan: judul lagu tak dikenal → jual mood/situasi).
- **Buatkan Short / video long / Playlist** dengan tema & CTA tertentu — Muse membuatkan kontennya (termasuk **klip video** dan background), lalu sistem yang merender & meng-upload. Short yang dibuat Muse: video portrait bergerak (bukan gambar diam + teks).
- **Jadwalkan konten** (mis. Playlist harian tiap channel — atur di `/jadwalplaylist` atau Settings).
- **Kirim file langsung ke VPS lewat SSH** (lihat bagian 8) — background, MP3, klip video — tanpa pemilik harus upload manual.
- **Menjawab pertanyaan** soal cara kerja sistem, kuota, monetisasi, dan troubleshooting.

**Yang Muse TIDAK bisa lakukan sendiri (butuh pemilik):**

- **Deploy** — pemilik yang menekan tombol Update di panel (atau `git pull` + restart di konsol). Muse hanya menyiapkan kodenya di GitHub.
- **Ganti PIN panel, tambah channel, isi bank judul, ubah blueprint/jam tayang** — semua itu hanya bisa di panel, dan butuh login pemilik (PIN / login Google di browser).
- **Masuk ke panel web** — Muse tidak tahu PIN panel (dan tidak boleh tahu). Bila panel tidak bisa dibuka, pemilik yang membukanya di browser (bila login PIN berputar-putar di Chrome yang terpasang ekstensi Muse, pakai **Microsoft Edge** — sudah terbukti bisa; lihat §13).

Yang memantau 24 jam adalah **bot Telegram di VPS**; Muse membantu analisis & perbaikan setiap kali pemilik menghubunginya (dari HP lewat aplikasi Muse, atau dari komputer).

---

## 12. Contoh alur harian (per 20 Nov 2026)

- **Pagi (06:10 WIB):** cron harian jalan — cek aturan 1 video/hari per channel, lalu buat Playlist hari itu (saat ini: tema Malaysia, 20–26 Nov 2026).
- **Pagi (07:00):** baca laporan harian di Telegram — channel mana naik views/subs, kuota tersisa berapa, ada video gagal atau tidak.
- **Siang (dari HP):** kirim `/playlist` atau `/buatshort` ke bot (atau ketik kalimat biasa, mis. `buatkan short tema anak-anak untuk Aruya 0089, cta subscribe`), jawab langkah-langkahnya, konfirmasi ✅ — video masuk antrean. Selesai render → upload → **publish langsung** (kecuali pemilik minta dijadwalkan).
- **Sore (17:00):** baca laporan sore di Telegram — update views & subscriber hari itu.
- **Sore/malam:** cek `/jadwal` (publish 7 hari ke depan) dan `/stok` (apakah stok video & galeri masih cukup). Bila ada video gagal, tekan 🔁 Coba Lagi dari `/gagal`.
- **Bila Muse mengirim update:** tekan **⬆️ Update & Restart** di panel (Settings → UPDATE & VERSI) — konsol VPS hanya cadangan, lihat §7.5. Setelah itu uji perintah baru di Telegram.

---

## 13. Troubleshooting (dari masalah nyata yang pernah terjadi)

| Masalah | Penyebab & solusi |
|---|---|
| **Device login error `invalid_client`** | (1) File JSON yang di-upload **bukan tipe TV** — buat OAuth Client ID baru tipe **"TV and Limited Input Devices"** (lihat 6.4), download JSON-nya, upload ulang ke Settings. Client Desktop tidak bisa dipakai untuk device login. (2) **File secret TV tertimpa Desktop** (insiden v1.3.50) — sudah diperbaiki dengan file unik per client_id; bila terulang, pulihkan file TV dari creds channel yang masih punya secret TV, lalu restart. |
| **Device login error `invalid_scope`** | Ada scope Analytics di daftar scope device flow. **Jangan** menambahkan scope Analytics ke device flow — Google menolaknya. Scope Analytics hanya lewat 1x login browser (§6.5). |
| **Lupa PIN panel** | Hapus file `config.json` di VPS secara manual (mis. lewat konsol: `rm ~/muse-bot/config.json`), lalu buka panel lagi — halaman setup PIN muncul dari awal. Setelah ganti PIN, semua sesi login lama diminta login ulang. |
| **`git pull` ditolak (ada perubahan lokal)** | Jangan paksa. Simpan dulu perubahannya (`git stash`), lalu `git pull` lagi. Bila ragu, tanyakan ke Muse sebelum menjalankan apa pun. |
| **Panel tidak bisa login di satu browser** (PIN benar tapi kembali ke halaman login) | Khusus **Chrome yang terpasang ekstensi Muse**: PIN dimasukkan berkali-kali tetap tidak masuk (loop). **Pakai Microsoft Edge** — sudah terbukti bisa. Penyebabnya cookie sesi yang tidak terkirim browser tersebut. |
| **Statistik per video menampilkan 0** padahal total views channel ribuan | Statistik per video hanya melacak **video yang di-upload pabrik** (setelah Fase 4). Views dari video lama channel (yang di-upload sebelum pabrik ada) tidak terlacak di tabel itu — itu normal, bukan error. |
| **Kolom VIEW 28D / VIEW 48H / TREND di tabel channel menampilkan "—"** | Kolom itu butuh snapshot harian channel (1/hari, retensi 90 hari) yang baru mulai tercatat sejak tabel di-deploy: VIEW 48H butuh 2 hari data, VIEW 28D & WATCH 30D butuh 28–30 hari, TREND butuh ~56 hari. Bukan error — tunggu datanya terkumpul. Jalan pintasnya: Aktifkan Analytics (§6.5) agar kolom terisi data asli tanpa menunggu. |
| **Tombol Update ditekan tapi versi tidak berubah / status macet "running"** | Dulu: `update_status.json` macet di "running" sehingga semua update ditolak. Sejak **v1.2.1** ini diperbaiki otomatis (guard 30 menit + self-heal saat startup). Bila masih terjadi: cek `cat ~/muse-bot/update_status.json` di konsol; menghapus file itu aman (`rm ~/muse-bot/update_status.json`) — file dibuat lagi otomatis saat update berikutnya. Pastikan juga menekan **"Ya, Update"** di dalam modal konfirmasi (menekan tombol Update saja belum memulai update). **Update via SSH wajib ikut menulis `update_status.json`** (aturan 15) — panel menampilkan versi dari `/api/app_version` (file VERSION, benar) tapi status "update terakhir" bisa kedaluwarsa bila file itu tidak ditulis. |
| **SSH ditolak "Permission denied (publickey,password)"** | (1) Kunci Muse belum terpasang di VPS — cek `grep -c ssh-ed25519 ~/.ssh/authorized_keys` di konsol (harus **1**; bila 0, pastikan `muse-ssh.pub` ada di repo dan service sudah restart setelah deploy — lihat bagian 8). (2) Pengaturan Muse **Settings → Permissions → Direct network protocols → ssh** masih "Deny" — ubah ke "Ask". (3) Perintah SSH **wajib** memakai `-o IdentitiesOnly=yes -i /home/hatch/.ssh/id_ed25519` (atau path kunci di mesinmu) — `ssh root@IP` polos selalu gagal karena ssh mencari kunci di `/root/.ssh`. (4) "Host key verification failed" — lihat jebakan `known_hosts` di §8.4 (simpan ke `/root/.ssh/known_hosts`, sidik jari RSA terverifikasi di sana). |
| **Bot menyebut "Background otomatis (gradien)"** | Itu **normal, bukan error**: galeri channel kosong, jadi panel membuat sendiri gambar gradien sesuai tema supaya Short tetap jadi. Upload gambar sendiri di panel bila mau yang lebih bagus. |
| **Video Aruya 0 detik terjadwal tayang** | Sisa upload resumable terputus saat restart panel (lihat §5). Hapus video itu di YouTube Studio, atau buat ulang — bila tayang, isinya kosong. |
| **Tidak ada laporan pagi di Telegram** | Cek apakah VPS & service operator masih jalan (`./ziqva.sh status`). Bila service mati, restart (`./ziqva.sh restart`). |

---

## 14. Keamanan — yang wajib diingat

- **Jangan pernah menaruh token, PIN, password, API key, atau isi `client_secret.json` di chat** (Telegram, Muse, atau di mana pun).
- File sensitif sudah di-`.gitignore` dan tidak akan ter-commit ke GitHub: `config.json`, `client_secret.json` (+ file secret per client), `channels_db.json`, `tasks_db.json`, `playlist_jobs.json`, `monitor_state.json`, `video_stats_cache.json`, `auto_shorts_state.json`, `update_status.json`, `nlu_audit.jsonl`, `nodes.json`, `oauth_clients.json`, `backup_key.txt`.
- **Pengecualian yang aman:** `muse-ssh.pub` berisi **kunci PUBLIK** SSH Muse — boleh ada di repo publik. Yang tidak boleh keluar dari mesin Muse: **kunci privat** (`id_ed25519` tanpa `.pub`), PIN panel, token bot, dan password / API key apa pun.
- PIN minimal 8 karakter (huruf + angka). Setelah 5 kali salah, login dikunci sementara.
- Semua endpoint penting di panel wajib login + token CSRF; endpoint operator Telegram memakai kunci HMAC (`operator_key`) yang hanya dibaca dari `config.json` di VPS — tidak pernah muncul di chat/log. Endpoint operator baru **wajib didaftarkan di `_OPERATOR_ENDPOINTS`** (pelajaran v1.3.19 — endpoint yang lupa didaftarkan tidak jalan).
- Upload ke galeri hanya menerima jenis file yang diizinkan, dan URL unduhan Clipper hanya menerima host YouTube.

---

## 15. Istilah singkat (untuk pemula)

| Istilah | Artinya |
|---|---|
| **VPS** | Komputer sewaan di internet yang nyala 24 jam — "pabrik"-nya. |
| **Panel** | Halaman web untuk mengatur pabrik (di `http://IP_VPS:8000`). |
| **API** | Pintu resmi untuk meminta data ke YouTube/Google dari program. |
| **OAuth / Device Login** | Cara menghubungkan channel YouTube tanpa mengetik password di panel: panel menampilkan kode, pemilik memasukkan kode itu di `google.com/device`. |
| **1x Login (browser)** | Cara menghubungkan channel lewat login browser sekali — token langsung punya izin upload + Analytics + email. |
| **Scope** | Izin yang diminta ke Google (mis. izin upload video, izin baca Analytics). |
| **Kuota** | Batas upload harian dari Google: **100 video/hari per project** (bukan 6 — angka 6 itu salah). |
| **Blueprint** | Resep per channel: tema, durasi acak 60–80 menit, preset render, deskripsi SEO, tag, jam tayang. |
| **Galeri** | Kumpulan bahan baku per channel: gambar background, MP3, visualizer, lagu penutup. |
| **Approval** | Video menunggu persetujuan pemilik di Telegram sebelum diproses. |
| **Full-Auto** | Video langsung dirender & di-upload tanpa persetujuan. |
| **Playlist** | Video kompilasi panjang dari riset fresh (judul + audio dari video orang lain yang sedang tren, judul ditulis ulang, thumbnail asli) — mode produksi utama saat ini. |
| **Zumo** | Jalur musik AI (situs sendiri, via Kie.ai) — generate lagu, mastering EQ PIANO, bank audio, rakit playlist. |
| **RPM** | Perkiraan penghasilan per 1.000 views (USD) — diisi manual per channel di Settings, karena API tidak memberikannya. |
| **YPP** | YouTube Partner Program — program monetisasi YouTube. Syarat: 1.000 subscriber + 4.000 jam tayang (12 bulan) ATAU 10 jt views Shorts (90 hari). **Mulai 1 Feb 2027 syarat naik: 8.000 jam tayang ATAU 20 jt views Shorts** (subscriber tetap 1.000; channel yang sudah monetisasi tidak terpengaruh). |
| **SSH** | Cara masuk & mengirim file ke VPS dari komputer lain dengan aman (pakai sepasang kunci: publik + privat). Muse memakai SSH untuk mengirim file langsung ke VPS. |
| **NLU** | Perintah bahasa manusia — pemilik mengetik kalimat sehari-hari di Telegram, bot menerjemahkan maksudnya (bukan perintah `/...`). |
| **Gradien otomatis** | Gambar background pengganti yang dibuat sendiri oleh panel (gradasi warna vertikal sesuai tema) bila galeri channel masih kosong, supaya Short tetap bisa dibuat. |
| **Shorts Muse** | Shorts yang dibuat pabrik dari judul + hook + naskah + CTA (bukan potongan video lama) — video portrait 1080×1920, 15/30/60 detik, audio penuh (voiceover + SFX + BGM). |
| **Clipper / Shorts AI** | Alat untuk memotong klip pendek dari video YouTube (pakai subtitle/transkrip untuk mencari momen viral). |
| **Daily Playlist** | Jadwal harian: 1 Playlist per channel per hari, diatur lewat `/jadwalplaylist` (channel baru tidak otomatis ikut — harus dinyalakan dulu). |

---

## 16. Segera hadir (direncanakan, BELUM ada saat file ini ditulis)

> Urutan yang disepakati pemilik (23:24 WIB, 18 Nov 2026): kerjakan dari bawah daftar celah ebook dulu, baru antrean berikutnya. Yang sudah selesai **dihapus dari daftar ini dan dipindah ke bagian 5** (laporan sore 17:00, tabel detail channel, tombol Tambah Channel, perapian Dashboard/Settings, jadwal otomatis Short, perpanjang jadwal 30 hari, **Backup otomatis v1.2.8, Thumbnail custom v1.2.22, Analytics v1.3.47 (parsial — baru Cozy Sax), jadwal Playlist harian v1.3.18/19, multi-client OAuth v1.2.25, Zumo v1.3.0** — semuanya sudah ada, lihat bagian 5).

**Celah dari ebook (urut dari bawah, sesuai keputusan pemilik) — BELUM dikerjakan:**
1. **Penjaga server dari luar** — bila VPS mati, bot Telegram ikut mati dan tidak ada yang memberi tahu pemilik. (Catatan: ada "watchdog antrean" internal yang mengawasi task macet — itu beda, bukan penjaga server dari luar.)
2. **Peta aset** — dokumen akun Google mana pemilik channel apa + silang data channel vs pabrik. (Sebagian terisi: Aruya = `aruya0089@gmail.com`; Cozy Sax + Anto Panie = `alvi851@gmail.com`; 6 channel lain pemiliknya masih belum diketahui — jangan diasumsikan.)
3. **Laporan "bintang hari ini"** — total laporan dari kenaikan positif saja, penurunan (video dihapus/di-private) dilaporkan terpisah, tampilkan channel dengan kenaikan terbesar.
4. **Duplikasi channel** — salin blueprint + galeri dari channel yang sudah terbukti ke channel baru + verifikasi jumlah file. (Dipilih pemilik 05:16 WIB 19 Nov 2026 sebagai berikutnya setelah Backup — masih antre. Desain batch duplikasi massal ada di `BATCH_DUPLIKASI_DESIGN.md` — rencana "nanti": riset playlist lagu lawas 90an Indonesia per channel, selesai 1 lanjut berikutnya.)
5. **Uji kesehatan kunci channel** ("Test Key") — panel hanya menampilkan "Connected" tanpa pernah menguji kuncinya.

**Antrean berikutnya (setelah celah ebook):**
1. **Tombol darurat panel** — jeda semua channel sekaligus (perintah Telegram `/darurat` + tombol panel). Yang sudah ada baru `/stopjadwal` untuk jadwal harian.
2. **Aktivasi Analytics untuk 7 channel tersisa** — masing-masing butuh 1x login browser sendiri (§6.5).
3. **Tes live Zumo pertama** — 1x generate (butuh key Kie.ai dari pemilik); kalibrasi EQ masih "mendekati, belum terbukti sama".
4. **Video MOTION untuk channel review OTOMOTIF** (rencana pemilik 01:27 WIB 19 Nov 2026, "nanti"): contoh Short 1 menit tentang beda mobil listrik vs hybrid. Yang dibutuhkan saat mulai: niche "otomotif" di bot (bank judul/hook/naskah/CTA + NLU paham "otomotif") + klip video mobil di galeri channel itu (Muse buatkan seperti klip anak) + voiceover. Durasi 60 detik sudah didukung.
5. **Audit batch berikutnya — 61 temuan ditunda** dari audit 20 Nov 2026 (detail di `~/workspace/audit-app-py-playlist-2026-11-20.md` + `~/workspace/audit-seo-findings.md`), termasuk data-integrity races, timezone unification, dan key-health gaps.
6. **Auto-render pasca-rekam + cleanup retensi** — ini untuk proyek ytopstud (Windows), bukan pabrik ini; jangan dicampur.

**Ditunda / belum aktif:**
- **HTTPS/TLS** — ditunda atas keputusan pemilik (jangan dikerjakan sekarang).
- **Multi-node** — persiapan kodenya ada, tapi mode ini **tidak boleh diaktifkan** sampai ada VPS kedua + database/antrean bersama yang sesungguhnya.
- **Jadwal Facebook & X** — out of scope.
- **Channel cadangan uji niche** — belum dibuat.

> Bila membaca file ini setelah fitur-fitur di atas selesai, pindahkan dari bagian ini ke bagian 5 (Status terkini).

---

*Dokumen ini ditulis 18 November 2026 dan diperbarui 20 November 2026, 17:35 WIB, untuk proyek ZIMUSE BOT (https://github.com/effands/muse-bot), versi 1.3.54, commit `6a59896` (terverifikasi langsung di VPS). Bila ada yang tidak sesuai dengan kode terbaru, kode-lah yang benar — laporkan ke Muse agar dokumen diperbarui.*
