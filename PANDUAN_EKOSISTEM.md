# 📖 PANDUAN EKOSISTEM — ZIQVA MUSE BOT

> 📋 **INSTRUKSI UNTUK MUSE — BACA DULU:** File ini bisa di-paste ke Muse mana pun sebagai konteks awal. Setelah membaca file ini, Muse tersebut memahami proyek ini, arsitekturnya, status terkininya, dan aturan mainnya — tanpa perlu bertanya ulang dari nol. File ini juga bisa dibaca manusia biasa sebagai panduan langkah demi langkah (bahasa dibuat sesederhana mungkin, istilah teknis dijelaskan saat pertama muncul).

---

## 1. Proyek ini apa

**ZIQVA MUSE BOT** adalah **pabrik konten YouTube otomatis**: sebuah sistem yang membuat video kompilasi musik panjang (60–80 menit), memotong Shorts, menjadwalkan upload, lalu meng-upload-nya ke YouTube — semuanya berjalan sendiri di server, 24 jam nonstop.

- **Repo GitHub:** https://github.com/effands/muse-bot
- **Pemilik proyek:** Effands (memanggil dirinya "Bos"; Muse memanggilnya "Bos"). Berkomunikasi dalam Bahasa Indonesia, santai. Masih awam soal sistem ini — jadi jelaskan dengan bahasa sederhana, hindari jargon, dan beri kepastian soal apa yang jalan sendiri vs apa yang butuh dia.
- **Tujuan:** mengelola beberapa channel YouTube (musik tidur, lofi, piano, hujan, meditasi, fokus, dan lainnya) dengan sedikit campur tangan manusia: pemilik memberi tema dan keputusan, sistem yang bekerja.

---

## 2. Tiga bagian ekosistem (yang saling terhubung)

```
                  ┌──────────────────────────────────┐
                  │  MUSE (Asisten AI)               │
                  │  Membangun fitur, memperbaiki    │
                  │  bug, menganalisis channel,      │
                  │  membuat Short & video long,     │
                  │  menjadwalkan konten             │
                  └──────────────┬───────────────────┘
                                 │ edit kode → push ke GitHub
                                 ▼
  ┌──────────────────┐   ┌──────────────────────────────────┐
  │  PEMILIK (HP /    │   │  VPS — PABRIK (nyala 24/7)       │
  │  laptop)          │◄─►│  • Panel web di port 8000        │
  │  • Perintah lewat │   │  • Render video (FFmpeg)         │
  │    Telegram       │   │  • Upload terjadwal ke YouTube   │
  │  • Cek panel di   │   │  • Bot Telegram (@ziqmusebot)    │
  │    browser        │   │    yang melapor & menerima       │
  │  • Deploy: git    │   │    perintah dari HP              │
  │    pull + restart │   └──────────────────────────────────┘
  └──────────────────┘
```

| Bagian | Apa itu | Perannya |
|---|---|---|
| **VPS** (Virtual Private Server) | Komputer sewaan di internet yang nyala 24 jam | Pabriknya. Merender video, meng-upload ke YouTube, dan menjalankan bot Telegram. **Laptop dan browser boleh dimatikan — VPS tetap bekerja.** |
| **Bot Telegram** (@ziqmusebot) | Program yang tinggal di VPS dan berbicara lewat Telegram | Ruang kontrol dari HP. Melapor tiap pagi, memberi peringatan kalau ada yang gagal, dan menerima perintah (buat Short, cek status, minta judul, dan lain-lain). |
| **Muse** (asisten AI, di chat ini) | Asisten yang membantu pemilik | Membangun fitur baru, memperbaiki bug, menganalisis data channel, membuatkan Short/video dengan tema & CTA, dan menyiapkan jadwal. Muse mengedit kode → push ke GitHub → pemilik menjalankan `git pull` + restart di VPS. |

**Alur kerja sama standar (ini yang selalu dipakai):**

1. Pemilik meminta sesuatu ke Muse (di chat).
2. Muse mengedit kode di repo, menguji, lalu **push ke GitHub**.
3. Pemilik menjalankan di konsol VPS (noVNC): `cd ~/muse-bot && git pull origin main && bash ziqva.sh restart`.
4. Bot Telegram memberi tahu kalau sistem sudah diperbarui.
5. Pemilik menguji lewat Telegram atau panel.

---

## 3. Aturan main (berlaku untuk Muse mana pun yang mengerjakan proyek ini)

1. **Rute gratis saja.** Jangan mengusulkan opsi berbayar kecuali pemilik menyetujuinya secara eksplisit.
2. **Jangan install Tailscale.** Pemilik sudah memutuskan untuk tidak memakainya.
3. **HTTPS/TLS ditunda.** Jangan dikerjakan sekarang; fokus ke hal lain.
4. **Jangan pernah memaksa `git pull`.** Bila `git pull` menolak karena ada perubahan lokal di VPS, BERHENTI dan laporkan ke pemilik — jangan pakai `--force`.
5. **Muse tidak men-deploy sendiri.** Deploy selalu dilakukan pemilik lewat konsol noVNC (`git pull` + restart).
6. **Mode Approval adalah default per channel.** Video baru lahir sebagai "Menunggu Approval" dan butuh persetujuan pemilik di Telegram sebelum dirender/di-upload — kecuali channel diubah ke mode Full-Auto.
7. **Kuota upload: 6 video per hari per Google Project** (bukan per channel — semua channel dalam satu project berbagi kuota yang sama).
8. **Scope Analytics TIDAK bisa dipakai di device flow.** Google menolak scope `yt-analytics.readonly` dan `yt-analytics-monetary.readonly` pada login perangkat (device-code flow). Akibatnya: **subs per video dan RPM otomatis NONAKTIF**. Subs per video tampil "—", RPM hanya dari isian manual di Settings. Jangan mencoba menambahkan scope itu lagi — sudah terbukti gagal lewat tes langsung.
9. **PIN panel minimal 8 karakter** (campuran huruf dan angka). PIN disimpan dalam bentuk hash, bukan teks biasa.
10. **Kredensial tidak pernah ditulis di chat.** Jangan pernah meminta atau menampilkan token bot Telegram, PIN, password, atau isi file `client_secret.json` di chat.
11. **Jangan mengarang data.** Bila suatu angka tidak tersedia dari API (mis. penghasilan asli, jam tayang 1 tahun), tampilkan "—" atau beri label estimasi yang jelas — jangan mengisi angka karangan.
12. **Verifikasi sebelum menyatakan selesai.** Uji di sandbox (data palsu) dan, bila memungkinkan, uji live di VPS sebelum menyatakan fitur selesai.

---

## 4. Arsitektur (untuk Muse yang baru pertama kali melihat repo ini)

- **Server:** VPS Ubuntu 22.04, IP `141.11.190.90`, 4 vCPU / 10 GB RAM / 100 GB disk. Panel web: `http://141.11.190.90:8000`.
- **2 service systemd** (otomatis nyala saat boot, otomatis restart bila mati — `Restart=always`):
  - `ziqva-muse-bot` — panel web + mesin render/upload (port 8000, file utama `app.py`).
  - `ziqva-muse-operator` — bot Telegram (file utama `muse_operator.py`).
- **File kunci di repo:**
  - `app.py` — panel web, antrian, worker render/upload, approval, kalender, statistik, monetisasi.
  - `muse_operator.py` — semua perintah & tombol Telegram, laporan harian, watchdog (pemantau kegagalan, kuota, stok).
  - `clipper/` — Shorts AI / Clipper (potong klip dari video YouTube, subtitle Whisper).
  - `core/` — pembatas sumber daya (resource gate) dan utilitas.
  - `templates/` — halaman panel (`index.html`, `hub.html`, `login.html`, `setup.html`, `clipper.html`).
  - `install.sh` — installer VPS. `ziqva.sh` — shortcut `start/stop/restart/status/logs`.
- **File yang TIDAK ada di Git (di-`.gitignore`, hanya hidup di VPS):** `config.json` (PIN hash, token Telegram, kunci operator), `client_secret.json` (kredensial OAuth Google), `channels_db.json` (daftar channel + token-nya), `tasks_db.json` (antrean & riwayat), `monitor_state.json` (status laporan/alert bot), `video_stats_cache.json` (cache statistik video), `nodes.json`.
- **Kunci OAuth Google:** project `project-musik-1`, consent screen "Ziqva Musik 1" (status **In Production**), client "Ziqva Musik 1 - TV" (tipe **TV and Limited Input Devices**). Client ini yang dipakai panel (device-code flow). Ada juga client tipe Desktop ("Ziqva Musik 1 - Desktop") — **tidak dipakai panel**, jangan dihapus sembarangan, jangan dipakai untuk device login.
- **YouTube API yang dipakai:** YouTube Data API v3 (upload, statistik channel & video). YouTube Analytics API pernah diaktifkan di project, tapi scope-nya tidak bisa dipakai di device flow (lihat aturan 8).

---

## 5. Status terkini proyek (per 18 November 2026)

> Bagian ini adalah **snapshot** keadaan saat file ini ditulis. Bila membaca file ini jauh setelah tanggal tersebut, angka dan daftar fitur bisa sudah berubah — cek repo & panel untuk keadaan terbaru.

- **Channel tertaut: 2** — "Aruya 0089" dan "Barang Unick". Keduanya mode **Approval**. Total views 1.310, subscribers 7 (mayoritas views dari Barang Unick).
- **Sudah ada (Fase 1–10):** monitoring Telegram (`/status`, alert gagal + tombol Retry, laporan harian 07:00 WIB, alert kuota & stok); approval preview (video menunggu persetujuan di Telegram); kalender jadwal 30 hari + pause/resume per channel; statistik per video; QC gate pra-upload + self-healing (retry, auto-pause channel setelah 3 gagal); kuota per Google project + autopilot bertingkat + stagger jadwal; lingkaran analitik (bobot judul otomatis); Shorts Muse (Shorts buatan panel dengan hook + naskah + CTA); dashboard monetisasi (estimasi dari RPM manual) + persiapan multi-node (belum aktif — butuh VPS kedua); perintah Telegram lengkap termasuk `/buatshort` & `/buatlong`.
- **Perbaikan keamanan sudah diterapkan:** PIN hash + lockout, device login wajib login panel, validasi upload, proteksi XSS/CSRF/SSRF, dan lain-lain (25 temuan, 24 diperbaiki; HTTPS ditunda — lihat aturan 3).
- **Yang masih butuh data/aset:** galeri channel (background, MP3, visualizer) untuk render; blueprint per channel; uji batch kecil sebelum batch 30 hari.

---

## 6. Google Cloud — langkah demi langkah

> Tujuannya: mendapatkan file `client_secret.json` (tipe TV) yang nanti di-upload ke panel. Sekali saja — file ini tinggal di VPS dan tidak perlu di-upload ulang tiap update.

### 6.1 Buat project & aktifkan API

1. Buka [Google Cloud Console](https://console.cloud.google.com) (login dengan akun Google pemilik project — di proyek ini akun developernya memakai email dukungan `alvi851@gmail.com`).
2. Buat project baru (di proyek ini namanya `project-musik-1`) atau pakai project yang sudah ada.
3. Menu **APIs & Services → Library**, cari dan aktifkan **YouTube Data API v3**.
   - (YouTube Analytics API boleh diaktifkan juga, tapi ingat aturan 8: scope-nya tidak bisa dipakai untuk login perangkat.)

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

### 6.4 Buat OAuth Client ID — TIPE "TV and Limited Input Devices" (PENTING!)

1. Menu **APIs & Services → Credentials → CREATE CREDENTIALS → OAuth client ID**.
2. **Application type: pilih "TV and Limited Input Devices"** (di proyek ini nama client-nya "Ziqva Musik 1 - TV").
3. Klik **Create**, lalu **Download JSON** — file bernama seperti `client_secret_....apps.googleusercontent.com.json`. Simpan baik-baik.

> ⚠️ **JANGAN pilih tipe "Desktop App".** Panel memakai device-code flow (login lewat kode di `google.com/device`), dan flow itu **hanya menerima client tipe TV**. Client tipe Desktop menyebabkan error **`invalid_client`** saat device login — ini sudah terbukti lewat pengalaman nyata di proyek ini. Bila sudah terlanjur membuat client Desktop, jangan dihapus dulu; buat client TV baru dan pakai file JSON dari client TV itu.
>
> ⚠️ **JANGAN menambahkan scope Analytics** (`yt-analytics.readonly` / `yt-analytics-monetary.readonly`) ke daftar scope atau ke consent screen dengan harapan subs per video & RPM otomatis bisa jalan — Google menolak scope itu di device flow dengan error **`invalid_scope`**. Sudah terbukti lewat tes langsung ke endpoint Google. Scope yang dipakai panel hanya `youtube` dan `youtube.upload`.

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
3. Login memakai PIN itu untuk masuk ke panel.

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

1. Di panel, buka **Settings → 🔄 UPDATE & VERSI**. Kartu ini menampilkan **versi yang sedang jalan** (mis. `v1.0.0`) dan **commit** yang sedang jalan.
2. Tekan **🔄 Periksa Update** — panel membandingkan commit lokal vs GitHub; bila ada update, tampil "ada update: `<hash>`".
3. Tekan **⬆️ Update & Restart** — update berjalan di latar, panel restart sendiri dalam 1–2 menit. Hasilnya juga dikirim ke Telegram.
   - Bila ada video sedang dirender/di-upload, update **ditolak** (coba lagi setelah selesai).
   - Bila `git pull` gagal (ada perubahan lokal/konflik), update **dibatalkan tanpa restart** dan alasannya ditampilkan di kartu.
   - Versi juga tampil di Telegram lewat perintah `/status` (baris "Versi Panel").

**Cara cadangan — konsol VPS** (bila tombol Update tidak bisa dipakai):

```bash
cd ~/muse-bot && git pull origin main && bash ziqva.sh restart
```

- Perintah ini menarik kode terbaru dari GitHub lalu me-restart kedua service.
- `client_secret.json`, `config.json`, dan database channel/task **tidak ikut tertimpa** `git pull` (sudah di-`.gitignore`) — jadi tidak perlu upload ulang.
- ⚠️ **Bila `git pull` menolak karena ada perubahan lokal di VPS, JANGAN dipaksa** (jangan `--force`, jangan `reset --hard`). Berhenti dan tanyakan ke Muse — biasanya cukup disimpan dulu dengan `git stash`, lalu pull lagi.
- Bila lupa PIN: hapus file `config.json` di VPS secara manual, lalu buka panel lagi untuk setup PIN dari awal (lihat Troubleshooting).

**Skema versi:** file `VERSION` di root repo berisi `major.minor.patch` (mis. `1.0.0`) — **minor naik untuk fitur baru, patch untuk perbaikan**. Setiap commit/push punya versi; versi + short commit hash tampil di panel (kartu UPDATE & VERSI) dan di Telegram `/status`.

---

## 8. Hubungkan channel YouTube ke panel

1. Di panel, buka **Settings → GOOGLE OAUTH 2.0 API**, upload file `client_secret.json` (yang tipe TV dari langkah 6.4), klik **Upload API Key**.
2. Klik **Connect via Device Login** (tombol di halaman Settings / hubungkan channel). Panel menampilkan **kode perangkat** (mis. `XXXX-XXXX`).
3. Buka `https://www.google.com/device` di browser (boleh dari HP), masukkan kode itu, lalu pilih **akun YouTube** yang mau ditautkan.
   - Akun Google pemilik channel **boleh berbeda** dari akun developer Google Cloud (di proyek ini channel "Aruya 0089" memakai akun `aruya0089@gmail.com`, sementara akun developernya `alvi851@gmail.com`).
4. Kembali ke panel — channel muncul sebagai **Connected**. Kode device berlaku **15 menit**; bila kedaluwarsa, ulangi dari langkah 2.
5. **Mode channel** (diatur per channel di Settings):
   - **Approval (default):** setiap video baru lahir sebagai "Menunggu Approval" — bot Telegram mengirim preview dan pemilik menekan ✅ Setujui / ❌ Tolak sebelum video dirender/di-upload.
   - **Full-Auto:** video langsung masuk antrean render & upload tanpa persetujuan.
6. **Tambah channel kedua/ketiga:** ulangi langkah 2–4 dengan akun YouTube lain. Semua channel dalam satu Google Project berbagi kuota 6 upload/hari (lihat aturan 7).
7. `client_secret.json` tinggal di VPS dan **tidak perlu di-upload ulang** tiap update sistem.

---

## 9. Telegram — ruang kontrol dari HP

### 9.1 Pasang bot

1. Di Telegram, cari **@BotFather**, kirim `/newbot`, ikuti langkahnya sampai mendapat **Bot Token**.
2. Cari **@userinfobot** (atau kirim pesan ke bot lalu cek) untuk mendapatkan **Chat ID** angka milik pemilik.
3. Di panel, buka **Settings → TELEGRAM NOTIFICATION**, isi **Bot Token** dan **Chat ID**, klik **Simpan & Test Pesan**. Bila berhasil, bot mengirim pesan tes ke HP pemilik.
4. Bot hanya melayani **satu pemilik** (Chat ID yang terdaftar) — pesan dari orang lain diabaikan.

### 9.2 Laporan otomatis

- **Laporan harian jam 07:00 WIB** — dikirim otomatis tiap pagi (channel, kuota, antrean, upload kemarin/hari ini). Bila tidak ada laporan masuk di pagi hari, itu tanda VPS/bot sedang bermasalah.
- **Alert langsung** bila: ada video gagal (dengan tombol 🔁 Coba Lagi), kuota hampir habis (5/6), atau stok jadwal video hampir habis.
- **Minta laporan kapan saja** dengan perintah `/laporan`.

### 9.3 Daftar perintah (sesuai kode `muse_operator.py` — jangan mengarang di luar daftar ini)

**Pantau:**

| Perintah | Artinya |
|---|---|
| `/status` | Ringkasan: channel, kuota x/6, antrean, upload hari ini |
| `/server` | Detail mesin: CPU, RAM, disk, uptime, status panel |
| `/kuota` | Kuota upload per Google project & per channel |
| `/stok` | Sisa stok jadwal & isi galeri per channel |
| `/channel` | Daftar channel YouTube yang terhubung |
| `/antrean` | Video yang sedang antre / diproses |
| `/jadwal` | Jadwal publish 7 hari ke depan |
| `/riwayat` | 8 upload terakhir + total |
| `/gagal` | Task gagal + tombol inline 🔁 Retry |
| `/laporan` | Laporan harian, kapan saja (sama seperti laporan 07:00) |

**Hasil:**

| Perintah | Artinya |
|---|---|
| `/statistik` | Views/likes/komentar total + 7/30 hari, video terbaik & terburuk |
| `/monetisasi` | Estimasi penghasilan dari RPM manual (bukan data penghasilan YouTube — bila RPM belum diisi, tidak ada estimasi sama sekali) |

**Konten (ide & tulisan):**

| Perintah | Artinya |
|---|---|
| `/judul [niche] [jumlah]` | Judul SEO, mis. `/judul lofi 15` (niche: sleep, lofi, piano, hujan, meditasi, fokus; jumlah 1–20) |
| `/deskripsi [niche]` | Template deskripsi SEO per niche |
| `/tag [niche]` | Bank hashtag (15 hashtag per niche) |
| `/cta` | 8 ide teks CTA untuk kartu akhir Shorts |
| `/ideashorts [niche]` | 5 ide Shorts: hook 4 detik + alur naskah + CTA |
| `/blueprint` | Resep settingan video kompilasi (durasi, preset, dsb.) |

**Buat video (baru):**

| Perintah | Artinya |
|---|---|
| `/buatshort` | Buat Shorts Muse terpandu, langkah demi langkah: judul → hook → naskah → CTA (boleh lewati) → durasi 15/30/60 detik → jadwal → background dari galeri → musik (opsional). Sekali kirim juga bisa: `/buatshort Judul \| Hook \| Naskah \| CTA` |
| `/buatlong` | Buat batch video long 60–80 menit terpandu: jumlah 1–7 → judul (boleh kosong, diambil dari bank judul) → tanggal mulai → jeda hari. Sekali kirim: `/buatlong 3 \| 2026-11-20 19:00 \| 1` |
| `/batal` | Batalkan draft pembuatan video yang sedang berjalan (draft hangus sendiri setelah 30 menit tidak ada kabar) |

**Catatan penting soal `/buatshort` & `/buatlong`:**
- Video yang dibuat lewat Telegram mengikuti semua gerbang yang sama seperti panel (cek disk, channel dijeda, kuota project, stagger jadwal).
- Bila channel mode **Approval**, video lahir sebagai pending — preview persetujuan masuk ke Telegram seperti biasa, dan video **tidak** langsung dirender.
- Shorts butuh **minimal 1 gambar background di galeri channel** untuk bisa dirender. Bila galeri kosong, bot membatalkan dengan pesan yang jelas.

**Tombol keyboard (14 tombol, 7 baris):** Status Server · Kuota Upload · Cek Channel · Antrean Render · Jadwal 7 Hari · Statistik Video · Monetisasi · Task Gagal · 10 Judul Sleep · Ide Shorts · Template Deskripsi · Blueprint Resep · Buat Short · Buat Video Long.

---

## 10. Peran Muse (apa yang bisa diminta)

Pemilik bisa meminta ke Muse, antara lain:

- **Bangun fitur baru** (mis. perintah Telegram baru, kolom baru di Dashboard, laporan baru).
- **Perbaiki bug** dan menjelaskan penyebabnya dengan bahasa sederhana.
- **Analisis channel** (video mana yang paling laku, judul apa yang sebaiknya dipakai lebih sering).
- **Buatkan Short / video long** dengan tema & CTA tertentu — Muse membuatkan kontennya, lalu sistem yang merender & meng-upload.
- **Jadwalkan konten** (mis. short otomatis tiap jam 6 pagi dengan tema tertentu di channel tertentu).
- **Menjawab pertanyaan** soal cara kerja sistem, kuota, monetisasi, dan troubleshooting.

**Yang Muse TIDAK bisa lakukan sendiri:** masuk langsung ke VPS tanpa browser pemilik (tidak pegang kunci SSH/PIN — memang sengaja demi keamanan). Yang memantau 24 jam adalah **bot Telegram di VPS**; Muse membantu analisis & perbaikan setiap kali pemilik menghubunginya (dari HP lewat aplikasi Muse, atau dari komputer).

---

## 11. Contoh alur harian

- **Pagi (07:00):** baca laporan harian di Telegram — channel mana naik views/subs, kuota tersisa berapa, ada video gagal atau tidak.
- **Siang (dari HP):** kirim `/buatshort` ke bot, jawab langkah-langkahnya, konfirmasi ✅ — video masuk antrean.
- **Sore/malam:** cek `/jadwal` (publish 7 hari ke depan) dan `/stok` (apakah stok video & galeri masih cukup). Bila ada video gagal, tekan 🔁 Coba Lagi dari `/gagal`.
- **Bila Muse mengirim update:** tekan **⬆️ Update & Restart** di panel (Settings → UPDATE & VERSI) — konsol VPS hanya cadangan, lihat §7.5. Setelah itu uji perintah baru di Telegram.

---

## 12. Troubleshooting (dari masalah nyata yang pernah terjadi)

| Masalah | Penyebab & solusi |
|---|---|
| **Device login error `invalid_client`** | File JSON yang di-upload **bukan tipe TV**. Buat OAuth Client ID baru tipe **"TV and Limited Input Devices"** (lihat 6.4), download JSON-nya, upload ulang ke Settings. Client Desktop tidak bisa dipakai untuk device login. |
| **Device login error `invalid_scope`** | Ada scope Analytics di daftar scope. **Jangan** menambahkan scope Analytics — Google menolaknya di device flow. Scope panel hanya `youtube` dan `youtube.upload`. (Subs per video & RPM otomatis memang nonaktif — lihat aturan 8.) |
| **Lupa PIN panel** | Hapus file `config.json` di VPS secara manual (mis. lewat konsol: `rm ~/muse-bot/config.json`), lalu buka panel lagi — halaman setup PIN muncul dari awal. Setelah ganti PIN, semua sesi login lama diminta login ulang. |
| **`git pull` ditolak (ada perubahan lokal)** | Jangan paksa. Simpan dulu perubahannya (`git stash`), lalu `git pull` lagi. Bila ragu, tanyakan ke Muse sebelum menjalankan apa pun. |
| **Panel tidak bisa login di satu browser** (PIN benar tapi kembali ke halaman login) | Coba browser lain (pernah terbukti: gagal di satu Chrome, berhasil di Microsoft Edge). Penyebabnya cookie sesi yang tidak terkirim browser tersebut. |
| **Statistik per video menampilkan 0** padahal total views channel ribuan | Statistik per video hanya melacak **video yang di-upload pabrik** (setelah Fase 4). Views dari video lama channel (yang di-upload sebelum pabrik ada) tidak terlacak di tabel itu — itu normal, bukan error. |
| **Tidak ada laporan pagi di Telegram** | Cek apakah VPS & service operator masih jalan (`./ziqva.sh status`). Bila service mati, restart (`./ziqva.sh restart`). |

---

## 13. Keamanan — yang wajib diingat

- **Jangan pernah menaruh token, PIN, password, atau isi `client_secret.json` di chat** (Telegram, Muse, atau di mana pun).
- File sensitif sudah di-`.gitignore` dan tidak akan ter-commit ke GitHub: `config.json`, `client_secret.json`, `channels_db.json`, `tasks_db.json`, `monitor_state.json`, `video_stats_cache.json`.
- PIN minimal 8 karakter (huruf + angka). Setelah 5 kali salah, login dikunci sementara.
- Semua endpoint penting di panel wajib login + token CSRF; endpoint operator Telegram memakai kunci HMAC (`operator_key`) yang hanya dibaca dari `config.json` di VPS — tidak pernah muncul di chat/log.
- Upload ke galeri hanya menerima jenis file yang diizinkan, dan URL unduhan Clipper hanya menerima host YouTube.

---

## 14. Istilah singkat (untuk pemula)

| Istilah | Artinya |
|---|---|
| **VPS** | Komputer sewaan di internet yang nyala 24 jam — "pabrik"-nya. |
| **Panel** | Halaman web untuk mengatur pabrik (di `http://IP_VPS:8000`). |
| **API** | Pintu resmi untuk meminta data ke YouTube/Google dari program. |
| **OAuth / Device Login** | Cara menghubungkan channel YouTube tanpa mengetik password di panel: panel menampilkan kode, pemilik memasukkan kode itu di `google.com/device`. |
| **Scope** | Izin yang diminta ke Google (mis. izin upload video). |
| **Kuota** | Batas upload harian dari Google: 6 video/hari per project. |
| **Blueprint** | Resep per channel: tema, durasi acak 60–80 menit, preset render, deskripsi SEO, tag, jam tayang. |
| **Galeri** | Kumpulan bahan baku per channel: gambar background, MP3, visualizer, lagu penutup. |
| **Approval** | Video menunggu persetujuan pemilik di Telegram sebelum diproses. |
| **RPM** | Perkiraan penghasilan per 1.000 views (USD) — diisi manual per channel di Settings, karena API tidak memberikannya. |
| **YPP** | YouTube Partner Program — program monetisasi YouTube. Syarat (per 2026): 1.000 subscriber + 4.000 jam tayang (12 bulan) ATAU 10 jt views Shorts (90 hari). **Mulai 1 Feb 2027 syarat naik: 8.000 jam tayang ATAU 20 jt views Shorts** (subscriber tetap 1.000; channel yang sudah monetisasi tidak terpengaruh). |

---

## 15. Segera hadir (direncanakan, BELUM ada saat file ini ditulis)

- **Laporan sore (17:00 WIB)** di Telegram — selain laporan pagi 07:00, berisi update views & subscriber per channel.
- **Short otomatis tiap jam 6 pagi** — jadwal harian: bot membuat 1 Short bertema tertentu di channel tertentu, mode Full-Auto.
- **Tabel detail channel di Dashboard** — kolom subscriber, jumlah video, views 28 hari & 48 jam, rata-rata views, umur channel, tren, dan tombol buka channel; plus badge kesiapan monetisasi (subs & jam tayang vs syarat yang berlaku) dan badge "Monetisasi Aktif" (ditandai manual di Settings).
- **Tombol "Tambah Channel" di Dashboard** — pop-up dengan alur device login yang sama, tanpa harus membuka Settings.
- **Perapian tampilan Dashboard & Settings** — susunan kartu lebih ringkas, daftar channel pindah ke atas, penjelasan panjang dipendekkan.

> Bila membaca file ini setelah fitur-fitur di atas selesai, pindahkan dari bagian ini ke bagian 5 (Status terkini).

---

*Dokumen ini ditulis 18 November 2026 untuk proyek ZIQVA MUSE BOT (https://github.com/effands/muse-bot). Bila ada yang tidak sesuai dengan kode terbaru, kode-lah yang benar — laporkan ke Muse agar dokumen diperbarui.*
