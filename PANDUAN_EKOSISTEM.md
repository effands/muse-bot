# 📖 PANDUAN EKOSISTEM — ZIMUSE BOT

> 📋 **INSTRUKSI UNTUK MUSE — BACA DULU:** File ini bisa di-paste ke Muse mana pun sebagai konteks awal. Setelah membaca file ini, Muse tersebut memahami proyek ini, arsitekturnya, status terkininya, dan aturan mainnya — tanpa perlu bertanya ulang dari nol. File ini juga bisa dibaca manusia biasa sebagai panduan langkah demi langkah (bahasa dibuat sesederhana mungkin, istilah teknis dijelaskan saat pertama muncul).
>
> ⚠️ **BACA DULU BAGIAN 8 (SSH) SEBELUM DEPLOY PERTAMA:** file `muse-ssh.pub` di repo berisi kunci SSH **Muse yang lama**. Muse baru **WAJIB** menggantinya dengan kunci publiknya sendiri sebelum deploy pertama — kalau tidak, Muse baru itu tidak akan bisa masuk ke VPS lewat SSH. Detailnya di bagian 8.
>
> **Status dokumen:** per **19 November 2026**, repo versi **1.2.3**, commit `a3225a06`. Panel bermerek **ZIMUSE BOT** (dulu "ZIQVA MUSE BOT"); nama repo, nama service systemd, dan username Telegram tetap memakai `ziqva` / `@ziqmusebot` (tidak diganti).

---

## 1. Proyek ini apa

**ZIMUSE BOT** adalah **pabrik konten YouTube otomatis**: sebuah sistem yang membuat video kompilasi musik panjang (60–80 menit), memotong Shorts, menjadwalkan upload, lalu meng-upload-nya ke YouTube — semuanya berjalan sendiri di server, 24 jam nonstop.

- **Repo GitHub:** https://github.com/effands/muse-bot (publik)
- **Pemilik proyek:** Effands (memanggil dirinya "Bos"; Muse memanggilnya "Bos"). Berkomunikasi dalam Bahasa Indonesia, santai. Masih awam soal sistem ini — jadi jelaskan dengan bahasa sederhana, hindari jargon, dan beri kepastian soal apa yang jalan sendiri vs apa yang butuh dia.
- **Tujuan:** mengelola beberapa channel YouTube (musik tidur, lofi, piano, hujan, meditasi, fokus, anak-anak, dan lainnya) dengan sedikit campur tangan manusia: pemilik memberi tema dan keputusan, sistem yang bekerja.
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
2. **Jangan install Tailscale.** Pemilik sudah memutuskan untuk tidak memakainya.
3. **HTTPS/TLS ditunda.** Jangan dikerjakan sekarang; fokus ke hal lain.
4. **Jangan pernah memaksa `git pull`.** Bila `git pull` menolak karena ada perubahan lokal di VPS, BERHENTI dan laporkan ke pemilik — jangan pakai `--force`.
5. **Muse tidak men-deploy sendiri.** Deploy selalu dilakukan pemilik — lewat tombol **Update & Restart** di panel (cara utama) atau lewat konsol noVNC (`git pull` + restart, cara cadangan). Muse hanya menyiapkan kodenya di GitHub.
6. **Mode Approval adalah default per channel.** Video baru lahir sebagai "Menunggu Approval" dan butuh persetujuan pemilik di Telegram sebelum dirender/di-upload — kecuali channel diubah ke mode Full-Auto.
7. **Kuota upload: 6 video per hari per Google Project** (bukan per channel — semua channel dalam satu project berbagi kuota yang sama).
8. **Scope Analytics TIDAK bisa dipakai di device flow.** Google menolak scope `yt-analytics.readonly` dan `yt-analytics-monetary.readonly` pada login perangkat (device-code flow). Akibatnya: **subs per video dan RPM otomatis NONAKTIF**. Subs per video tampil "—", RPM hanya dari isian manual di Settings. Jangan mencoba menambahkan scope itu lagi — sudah terbukti gagal lewat tes langsung.
9. **PIN panel minimal 8 karakter** (campuran huruf dan angka). PIN disimpan dalam bentuk hash, bukan teks biasa.
10. **Kredensial tidak pernah ditulis di chat.** Jangan pernah meminta atau menampilkan token bot Telegram, PIN, password, atau isi file `client_secret.json` di chat.
11. **Jangan mengarang data.** Bila suatu angka tidak tersedia dari API (mis. penghasilan asli, jam tayang 1 tahun), tampilkan "—" atau beri label estimasi yang jelas — jangan mengisi angka karangan.
12. **Verifikasi sebelum menyatakan selesai.** Uji di sandbox (data palsu) dan, bila memungkinkan, uji live di VPS sebelum menyatakan fitur selesai.
13. **SSH Muse — WAJIB dibaca bila kamu Muse yang baru (lihat bagian 8).** File `muse-ssh.pub` di repo berisi kunci publik **Muse yang lama** — Muse baru **wajib menggantinya dengan kunci publiknya sendiri** sebelum deploy pertama. Perintah SSH/SCP selalu memakai identitas eksplisit (`-o IdentitiesOnly=yes -i <path kunci>`), karena ssh mencari kunci default di `/root/.ssh` sedangkan HOME Muse `/home/hatch`. Pengaturan Muse **Settings → Permissions → Direct network protocols → ssh harus "Ask"** (bukan "Deny"), kalau tidak SSH keluar diblokir.
14. **Jangan mengandalkan paste ke konsol noVNC untuk perintah panjang.** Paste perintah panjang ke konsol noVNC **tidak bisa** (terbukti di proyek ini). Perubahan ke VPS selalu lewat jalur GitHub + tombol Update, atau perintah konsol yang **pendek** (satu baris, bisa diketik).
15. **Setiap push punya VERSI.** File `VERSION` di root repo naik tiap rilis (minor untuk fitur, patch untuk perbaikan) — jangan pernah push tanpa menaikkan VERSION.

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
  - `muse-ssh.pub` — **kunci SSH PUBLIK milik Muse** (bukan rahasia — kunci publik memang boleh dibagikan). Panel memasangnya otomatis ke `~/.ssh/authorized_keys` setiap kali service start (hook `_ensure_muse_ssh_key` di `app.py`, idempotent — bila sudah ada tidak ditulis ulang). ⚠️ **Muse baru wajib mengganti isi file ini dengan kunci publiknya sendiri** (lihat bagian 8).
- **File yang TIDAK ada di Git (di-`.gitignore`, hanya hidup di VPS):** `config.json` (PIN hash, token Telegram, kunci operator), `client_secret.json` (kredensial OAuth Google), `channels_db.json` (daftar channel + token-nya), `tasks_db.json` (antrean & riwayat), `monitor_state.json` (status laporan/alert bot), `video_stats_cache.json` (cache statistik video), `nodes.json`, `auto_shorts_state.json` (dedupe jadwal auto-shorts), `update_status.json` + `update.log` (status update panel), `nlu_audit.jsonl` (log audit perintah bahasa manusia Telegram).
- **Kunci OAuth Google:** project `project-musik-1`, consent screen "Ziqva Musik 1" (status **In Production**), client "Ziqva Musik 1 - TV" (tipe **TV and Limited Input Devices**). Client ini yang dipakai panel (device-code flow). Ada juga client tipe Desktop ("Ziqva Musik 1 - Desktop") — **tidak dipakai panel**, jangan dihapus sembarangan, jangan dipakai untuk device login.
- **YouTube API yang dipakai:** YouTube Data API v3 (upload, statistik channel & video). YouTube Analytics API pernah diaktifkan di project, tapi scope-nya tidak bisa dipakai di device flow (lihat aturan 8).

---

## 5. Status terkini proyek (per 19 November 2026)

> Bagian ini adalah **snapshot** keadaan saat file ini ditulis. Bila membaca file ini jauh setelah tanggal tersebut, angka dan daftar fitur bisa sudah berubah — cek repo & panel untuk keadaan terbaru.

- **Versi yang jalan: v1.2.3** (commit `a3225a06`). Repo: https://github.com/effands/muse-bot (publik). Panel bermerek **ZIMUSE BOT**; nama repo/service tetap `ziqva-muse-bot` & `ziqva-muse-operator`; bot Telegram **@ziqmusebot**.
- **Channel tertaut: 3** — "Aruya 0089" (akun Google `aruya0089@gmail.com`, mode **Approval**), "Barang Unick" (mode Approval; **akun Google pemiliknya BELUM DIKETAHUI — jangan diasumsikan**), "Cozy Sax Relaxing" (**akun Google pemiliknya BELUM DIKETAHUI — jangan diasumsikan**). Total views 1.310, subscribers 7 (per 18 Nov 2026; mayoritas views dari Barang Unick).
- **Sudah ada (Fase 1–11 + paket lanjutan):** monitoring Telegram (`/status`, alert gagal + tombol Retry, laporan pagi 07:00 + sore 17:00 WIB, alert kuota & stok); approval preview (video menunggu persetujuan di Telegram); kalender jadwal 30 hari + pause/resume per channel; statistik per video + tabel detail channel (subs, views 28D/48H, tren, kesiapan monetisasi); QC gate pra-upload + self-healing (retry, auto-pause channel setelah 3 gagal); kuota per Google project + autopilot bertingkat + stagger jadwal; lingkaran analitik (bobot judul otomatis); Shorts Muse (Shorts buatan panel dengan hook + naskah + CTA); dashboard monetisasi (estimasi dari RPM manual) + persiapan multi-node (**belum aktif** — butuh VPS kedua); perintah Telegram lengkap termasuk `/buatshort`, `/buatlong`, `/perpanjang`, `/mode`, `/jeda`, `/lanjutkan` + **perintah bahasa manusia / NLU** (bot menerjemahkan kalimat sehari-hari, selalu konfirmasi dulu, semua tafsiran tercatat di `nlu_audit.jsonl`); **jadwal otomatis Short** per channel (tema + jam buat/tayang bisa diatur di Settings); **tombol Update & Restart di panel** (Settings → UPDATE & VERSI) + file `VERSION`; **background otomatis** (galeri kosong → panel membuat sendiri gradien vertikal 1080×1920 sesuai tema, mis. anak-anak = kuning-pink-biru); **SSH Muse ↔ VPS sudah terhubung** (lihat bagian 8) — Muse bisa kirim file langsung ke server.
- **Perbaikan keamanan sudah diterapkan:** PIN hash + lockout, device login wajib login panel, validasi upload, proteksi XSS/CSRF/SSRF, dan lain-lain (25 temuan, 24 diperbaiki; HTTPS ditunda — lihat aturan 3). Perbaikan bug tombol Update yang macet di status "running" juga sudah masuk (v1.2.1: guard 30 menit + self-heal saat startup).
- **Yang masih butuh data/aset:** galeri channel (background, MP3, visualizer) untuk render; blueprint per channel; bank judul channel (masih kosong — `/perpanjang` akan menolak dengan jelas bila bank judul kosong).
- **Yang BELUM PERNAH jalan: tes perdana (batch uji kecil).** Sampai 19 Nov 2026 belum ada satu video pun yang tayang dari pabrik. Tes perdana yang direncanakan: **Short anak-anak + CTA subscribe untuk Aruya 0089** (judul "Ayo Bernyanyi! Lagu Anak Ceria"). Jalankan tes ini dulu sebelum batch 30 hari.

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

1. Di panel, buka **Settings → 🔄 UPDATE & VERSI**. Kartu ini menampilkan **versi yang sedang jalan** (mis. `v1.2.3`) dan **commit** yang sedang jalan, plus status "Belum dicek" bila belum pernah diperiksa.
2. Tekan **🔄 Periksa Update** — panel membandingkan commit lokal vs GitHub; bila ada update, tampil "ada update: `<hash>`".
3. Tekan **⬆️ Update & Restart** — muncul **modal konfirmasi bertema panel**; tekan **"Ya, Update"** di dalam modal (menekan tombolnya saja belum memulai update). Update berjalan di latar, panel restart sendiri dalam 1–2 menit. Hasilnya juga dikirim ke Telegram.
   - Bila ada video sedang dirender/di-upload, update **ditolak** (coba lagi setelah selesai).
   - Bila `git pull` gagal (ada perubahan lokal/konflik), update **dibatalkan tanpa restart** dan alasannya ditampilkan di kartu.
   - Versi juga tampil di Telegram lewat perintah `/status` (baris "Versi Panel").
   - Pesan "Koneksi server gagal. Task tetap berjalan." saat restart adalah **NORMAL** (panel mati sebentar) — tunggu 1–2 menit lalu refresh.
   - Sejak v1.2.1, status update yang macet di "running" **diperbaiki otomatis**: bila status "running" sudah lebih dari 30 menit, update baru diizinkan; saat startup, status macet dipulihkan sendiri (jadi "selesai" bila versi sudah berubah, "gagal" bila tidak). Jadi tombol Update tidak akan terkunci selamanya lagi.

**Cara cadangan — konsol VPS** (bila tombol Update tidak bisa dipakai):

```bash
cd ~/muse-bot && git pull origin main && bash ziqva.sh restart
```

- Perintah ini menarik kode terbaru dari GitHub lalu me-restart kedua service.
- `client_secret.json`, `config.json`, dan database channel/task **tidak ikut tertimpa** `git pull` (sudah di-`.gitignore`) — jadi tidak perlu upload ulang.
- ⚠️ **Bila `git pull` menolak karena ada perubahan lokal di VPS, JANGAN dipaksa** (jangan `--force`, jangan `reset --hard`). Berhenti dan tanyakan ke Muse — biasanya cukup disimpan dulu dengan `git stash`, lalu pull lagi.
- Bila lupa PIN: hapus file `config.json` di VPS secara manual, lalu buka panel lagi untuk setup PIN dari awal (lihat Troubleshooting).

**Skema versi:** file `VERSION` di root repo berisi `major.minor.patch` (mis. `1.2.3`) — **minor naik untuk fitur baru, patch untuk perbaikan**. Setiap commit/push punya versi; versi + short commit hash tampil di panel (kartu UPDATE & VERSI) dan di Telegram `/status`.

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
- Bila SSH masih ditolak padahal perintah sudah benar, cek di konsol VPS (perintah pendek): `grep -c ssh-ed25519 ~/.ssh/authorized_keys` — harus muncul angka **1** (jumlah kunci yang terpasang). Bila **0** atau file tidak ada, hook belum memasang kunci: pastikan file `muse-ssh.pub` ada di repo VPS (`ls ~/muse-bot/muse-ssh.pub`) dan service sudah di-restart setelah deploy.
- **Tailscale TIDAK dipakai** (keputusan pemilik) — jangan diusulkan atau di-install.

### 8.5 Apa yang bisa dilakukan Muse lewat SSH

- Mengirim **background, MP3, klip video** langsung ke folder galeri channel di VPS.
- Memeriksa status service, log, dan isi file di VPS (`./ziqva.sh status`, `ls`, `cat`).
- Membantu diagnosis bila panel/operator bermasalah, tanpa pemilik harus mengetik perintah panjang.
- Yang tetap **tidak** bisa dilakukan Muse lewat SSH: deploy butuh pemilik (tombol Update / `git pull` + restart — lihat §7.5), ganti PIN panel, dan tambah channel (butuh login Google di browser).

## 9. Hubungkan channel YouTube ke panel

1. Di panel, buka **Settings → GOOGLE OAUTH 2.0 API**, upload file `client_secret.json` (yang tipe TV dari langkah 6.4), klik **Upload API Key**. (Jalan pintas: tombol **➕ Tambah Channel** di Dashboard membuka pop-up dengan alur yang sama.)
2. Klik **Connect via Device Login** (tombol di halaman Settings / pop-up Tambah Channel). Panel menampilkan **kode perangkat** (mis. `XXXX-XXXX`).
3. Buka `https://www.google.com/device` di browser (boleh dari HP), masukkan kode itu, lalu pilih **akun YouTube** yang mau ditautkan.
   - Akun Google pemilik channel **boleh berbeda** dari akun developer Google Cloud (di proyek ini channel "Aruya 0089" memakai akun `aruya0089@gmail.com`, sementara akun developernya `alvi851@gmail.com`).
   - **Catatan untuk Muse baru:** pemilik akun Google untuk "Barang Unick" dan "Cozy Sax Relaxing" **belum diketahui** — jangan diasumsikan; tanyakan ke pemilik bila perlu (lihat bagian "Peta aset" di §16 — peta ini belum dibuat).
4. Kembali ke panel — channel muncul sebagai **Connected**. Kode device berlaku **15 menit**; bila kedaluwarsa, ulangi dari langkah 2.
5. **Mode channel** (diatur per channel di Settings):
   - **Approval (default):** setiap video baru lahir sebagai "Menunggu Approval" — bot Telegram mengirim preview dan pemilik menekan ✅ Setujui / ❌ Tolak sebelum video dirender/di-upload.
   - **Full-Auto:** video langsung masuk antrean render & upload tanpa persetujuan.
6. **Tambah channel kedua/ketiga:** ulangi langkah 2–4 dengan akun YouTube lain. Semua channel dalam satu Google Project berbagi kuota 6 upload/hari (lihat aturan 7).
7. `client_secret.json` tinggal di VPS dan **tidak perlu di-upload ulang** tiap update sistem.

---

## 10. Telegram — ruang kontrol dari HP

### 10.1 Pasang bot

1. Di Telegram, cari **@BotFather**, kirim `/newbot`, ikuti langkahnya sampai mendapat **Bot Token**.
2. Cari **@userinfobot** (atau kirim pesan ke bot lalu cek) untuk mendapatkan **Chat ID** angka milik pemilik.
3. Di panel, buka **Settings → TELEGRAM NOTIFICATION**, isi **Bot Token** dan **Chat ID**, klik **Simpan & Test Pesan**. Bila berhasil, bot mengirim pesan tes ke HP pemilik.
4. Bot hanya melayani **satu pemilik** (Chat ID yang terdaftar) — pesan dari orang lain diabaikan.

### 10.2 Laporan otomatis

- **Laporan pagi jam 07:00 WIB** — dikirim otomatis tiap pagi (views/subs/video per channel, perubahan sejak laporan terakhir, progres monetisasi, kuota, antrean). Bila tidak ada laporan masuk di pagi hari, itu tanda VPS/bot sedang bermasalah.
- **Laporan sore jam 17:00 WIB** — laporan kedua tiap sore, format sama seperti laporan pagi.
- **Minta laporan kapan saja** dengan perintah `/laporan`.
- **Alert langsung** bila: ada video gagal (dengan tombol 🔁 Coba Lagi), kuota hampir habis (5/6), atau stok jadwal video hampir habis.
- **Nada bot:** seperti karyawan yang melapor ke bos — **LUGU tapi SONGONG** (santai, percaya diri, sedikit usil), tetap sopan, emoji 1–3 per pesan, data tetap lengkap. Sekali sehari (di laporan pagi) bot menanyakan kabar bos dan basa-basi ringan — tidak pernah mengorbankan isi laporan.

### 10.3 Daftar perintah (sesuai pesan `/start` di `muse_operator.py` — jangan mengarang di luar daftar ini)

**Pantau:**

| Perintah | Artinya |
|---|---|
| `/status` | Ringkasan: channel (+ mode Approval/Full-Auto per channel), antrean, kuota x/6, kesehatan server, **versi panel** |
| `/server` | Detail mesin: CPU, RAM, disk, uptime, status panel |
| `/kuota` | Kuota upload per Google project & per channel |
| `/stok` | Sisa stok jadwal & isi galeri per channel |
| `/channel` | Daftar channel YouTube yang terhubung |
| `/antrean` | Video yang sedang antre / diproses |
| `/jadwal` | Jadwal publish 7 hari ke depan |
| `/riwayat` | 8 upload terakhir |
| `/gagal` | Task gagal + tombol inline 🔁 Retry |
| `/laporan` | Laporan harian, kapan saja (sama seperti laporan 07:00 & 17:00) |

**Hasil:**

| Perintah | Artinya |
|---|---|
| `/statistik` | Views/likes/komentar total + 7/30 hari, video terbaik & terburuk |
| `/monetisasi` | Estimasi penghasilan dari RPM manual (bukan data penghasilan YouTube — bila RPM belum diisi, tidak ada estimasi sama sekali) |

**Konten (ide & tulisan):**

| Perintah | Artinya |
|---|---|
| `/judul [niche] [jumlah]` | Judul SEO, mis. `/judul lofi 15` (niche: sleep, lofi, piano, hujan, meditasi, fokus, **anak**; jumlah 1–20) |
| `/deskripsi [niche]` | Template deskripsi SEO per niche |
| `/tag [niche]` | Bank hashtag (15 hashtag per niche) |
| `/cta` | 8 ide teks CTA untuk kartu akhir Shorts |
| `/ideashorts [niche]` | 5 ide Shorts: hook 4 detik + alur naskah + CTA |
| `/blueprint` | Resep settingan video kompilasi (durasi, preset, dsb.) |

**Buat video:**

| Perintah | Artinya |
|---|---|
| `/buatshort` | Buat Shorts Muse terpandu, langkah demi langkah: judul → hook → naskah → CTA (boleh lewati) → durasi 15/30/60 detik → jadwal → background dari galeri → musik (opsional). Sekali kirim juga bisa: `/buatshort Judul \| Hook \| Naskah \| CTA` |
| `/buatlong` | Buat batch video long 60–80 menit terpandu: jumlah 1–7 → judul (boleh kosong, diambil dari bank judul) → tanggal mulai → jeda hari. Sekali kirim: `/buatlong 3 \| 2026-11-20 19:00 \| 1` |
| `/batal` | Batalkan draft pembuatan video yang sedang berjalan (draft hangus sendiri setelah 30 menit tidak ada kabar) |

**Atur channel:**

| Perintah | Artinya |
|---|---|
| `/perpanjang [channel] [hari]` | Tambah jadwal, mis. `/perpanjang Aruya 0089 30` atau `/perpanjang semua 30` (hari 1–90, default 30; judul lanjut berurutan dari bank judul — bila bank judul kosong, perintah ditolak dengan jelas) |
| `/mode [channel] [auto\|approval]` | Ganti mode, mis. `/mode Aruya 0089 auto` (beralih ke Full-Auto selalu minta konfirmasi dulu) |
| `/jeda [channel]` | Jeda channel (jadwal & antrean berhenti) |
| `/lanjutkan [channel]` | Lanjutkan channel yang dijeda |

**Bicara bebas (bahasa manusia / NLU) — tanpa garis miring:**

Pemilik boleh mengetik kalimat sehari-hari, mis. `buatkan short tema anak-anak untuk Aruya 0089, cta subscribe` atau `jeda channel Barang Unick`. Bot menerjemahkan maksudnya (rule-based, tanpa LLM), lalu **selalu menyatakan tafsirannya dulu** ("Maksud Bos: …?") dengan tombol ✅ Ya / ❌ Bukan. Bila tidak yakin, bot bertanya dulu (pilih channel/tema/mode lewat tombol; klarifikasi hangus setelah 10 menit). Perintah **destruktif tidak pernah dieksekusi langsung** dari Telegram — bot hanya memberi langkah panelnya. Semua tafsiran tercatat di **`nlu_audit.jsonl`** di VPS untuk diaudit & direvisi pemilik. Niche yang dikenal: sleep, lofi, piano, hujan, meditasi, fokus, **anak**. "Semua channel" / "ke-3 channel" berarti **semua channel yang terdaftar saat itu** (dihitung dinamis, bukan angka tetap).

**Catatan penting soal `/buatshort` & `/buatlong`:**
- Video yang dibuat lewat Telegram mengikuti semua gerbang yang sama seperti panel (cek disk, channel dijeda, kuota project, stagger jadwal).
- Bila channel mode **Approval**, video lahir sebagai pending — preview persetujuan masuk ke Telegram seperti biasa, dan video **tidak** langsung dirender.
- **Shorts tidak lagi batal bila galeri kosong** (sejak v1.2.2): panel membuat sendiri **background gradien otomatis** sesuai tema (mis. anak-anak = kuning-pink-biru) dan video tetap dibuat; bot memberitahu dengan jujur bila yang dipakai background otomatis. Upload gambar sendiri di panel bila mau yang lebih bagus. Alur terpandu `/buatshort`: balas `lewati` di langkah background untuk lanjut dengan gradien.
- Shorts Muse: video portrait **1080×1920**, durasi 15/30/60 detik. Video long: **60–80 menit, 720p**.
- **Yang HANYA bisa di panel (tidak bisa lewat Telegram):** ganti PIN, tambah channel, upload galeri, isi bank judul, ubah blueprint/jam tayang, Update panel. **Tidak ada perintah Telegram yang destruktif** (hapus video/channel tidak dibuat — sengaja).

**Tombol keyboard (14 tombol, 7 baris):** Status Server · Kuota Upload · Cek Channel · Antrean Render · Jadwal 7 Hari · Statistik Video · Monetisasi · Task Gagal · 10 Judul Sleep · Ide Shorts · Template Deskripsi · Blueprint Resep · Buat Short · Buat Video Long.

---

## 11. Peran Muse (apa yang bisa diminta)

Muse adalah **admin + asisten** pemilik — menyiapkan SEMUA kebutuhan VPS. Pemilik bisa meminta ke Muse, antara lain:

- **Bangun fitur baru** (mis. perintah Telegram baru, kolom baru di Dashboard, laporan baru).
- **Perbaiki bug** dan menjelaskan penyebabnya dengan bahasa sederhana.
- **Analisis channel** (video mana yang paling laku, judul apa yang sebaiknya dipakai lebih sering).
- **Buatkan Short / video long** dengan tema & CTA tertentu — Muse membuatkan kontennya (termasuk **klip video** dan background), lalu sistem yang merender & meng-upload. Short yang dibuat Muse: video portrait bergerak (bukan gambar diam + teks) — desainnya 3 klip 10 detik disambung jadi 1 Short 30 detik (lihat §16).
- **Jadwalkan konten** (mis. short otomatis tiap jam 6 pagi dengan tema tertentu di channel tertentu — atur di Settings → Jadwal Otomatis Short, per channel).
- **Kirim file langsung ke VPS lewat SSH** (lihat bagian 8) — background, MP3, klip video — tanpa pemilik harus upload manual.
- **Menjawab pertanyaan** soal cara kerja sistem, kuota, monetisasi, dan troubleshooting.

**Yang Muse TIDAK bisa lakukan sendiri (butuh pemilik):**

- **Deploy** — pemilik yang menekan tombol Update di panel (atau `git pull` + restart di konsol). Muse hanya menyiapkan kodenya di GitHub.
- **Ganti PIN panel, tambah channel, isi bank judul, ubah blueprint/jam tayang** — semua itu hanya bisa di panel, dan butuh login pemilik (PIN / login Google di browser).
- **Masuk ke panel web** — Muse tidak tahu PIN panel (dan tidak boleh tahu). Bila panel tidak bisa dibuka, pemilik yang membukanya di browser (bila login PIN berputar-putar di Chrome yang terpasang ekstensi Muse, pakai **Microsoft Edge** — sudah terbukti bisa; lihat §13).

Yang memantau 24 jam adalah **bot Telegram di VPS**; Muse membantu analisis & perbaikan setiap kali pemilik menghubunginya (dari HP lewat aplikasi Muse, atau dari komputer).

---

## 12. Contoh alur harian

- **Pagi (07:00):** baca laporan harian di Telegram — channel mana naik views/subs, kuota tersisa berapa, ada video gagal atau tidak.
- **Siang (dari HP):** kirim `/buatshort` ke bot (atau ketik kalimat biasa, mis. `buatkan short tema anak-anak untuk Aruya 0089, cta subscribe`), jawab langkah-langkahnya, konfirmasi ✅ — video masuk antrean.
- **Sore (17:00):** baca laporan sore di Telegram — update views & subscriber hari itu.
- **Sore/malam:** cek `/jadwal` (publish 7 hari ke depan) dan `/stok` (apakah stok video & galeri masih cukup). Bila ada video gagal, tekan 🔁 Coba Lagi dari `/gagal`.
- **Bila Muse mengirim update:** tekan **⬆️ Update & Restart** di panel (Settings → UPDATE & VERSI) — konsol VPS hanya cadangan, lihat §7.5. Setelah itu uji perintah baru di Telegram.

---

## 13. Troubleshooting (dari masalah nyata yang pernah terjadi)

| Masalah | Penyebab & solusi |
|---|---|
| **Device login error `invalid_client`** | File JSON yang di-upload **bukan tipe TV**. Buat OAuth Client ID baru tipe **"TV and Limited Input Devices"** (lihat 6.4), download JSON-nya, upload ulang ke Settings. Client Desktop tidak bisa dipakai untuk device login. |
| **Device login error `invalid_scope`** | Ada scope Analytics di daftar scope. **Jangan** menambahkan scope Analytics — Google menolaknya di device flow. Scope panel hanya `youtube` dan `youtube.upload`. (Subs per video & RPM otomatis memang nonaktif — lihat aturan 8.) |
| **Lupa PIN panel** | Hapus file `config.json` di VPS secara manual (mis. lewat konsol: `rm ~/muse-bot/config.json`), lalu buka panel lagi — halaman setup PIN muncul dari awal. Setelah ganti PIN, semua sesi login lama diminta login ulang. |
| **`git pull` ditolak (ada perubahan lokal)** | Jangan paksa. Simpan dulu perubahannya (`git stash`), lalu `git pull` lagi. Bila ragu, tanyakan ke Muse sebelum menjalankan apa pun. |
| **Panel tidak bisa login di satu browser** (PIN benar tapi kembali ke halaman login) | Khusus **Chrome yang terpasang ekstensi Muse**: PIN dimasukkan berkali-kali tetap tidak masuk (loop). **Pakai Microsoft Edge** — sudah terbukti bisa. Penyebabnya cookie sesi yang tidak terkirim browser tersebut. |
| **Statistik per video menampilkan 0** padahal total views channel ribuan | Statistik per video hanya melacak **video yang di-upload pabrik** (setelah Fase 4). Views dari video lama channel (yang di-upload sebelum pabrik ada) tidak terlacak di tabel itu — itu normal, bukan error. |
| **Kolom VIEW 28D / VIEW 48H / TREND di tabel channel menampilkan "—"** | Kolom itu butuh snapshot harian channel (1/hari, retensi 90 hari) yang baru mulai tercatat sejak tabel di-deploy: VIEW 48H butuh 2 hari data, VIEW 28D & WATCH 30D butuh 28–30 hari, TREND butuh ~56 hari. Bukan error — tunggu datanya terkumpul. Jalan pintasnya: Aktifkan Analytics (lihat §16) agar kolom terisi data asli tanpa menunggu. |
| **Tombol Update ditekan tapi versi tidak berubah / status macet "running"** | Dulu: `update_status.json` macet di "running" sehingga semua update ditolak. Sejak **v1.2.1** ini diperbaiki otomatis (guard 30 menit + self-heal saat startup). Bila masih terjadi: cek `cat ~/muse-bot/update_status.json` di konsol; menghapus file itu aman (`rm ~/muse-bot/update_status.json`) — file dibuat lagi otomatis saat update berikutnya. Pastikan juga menekan **"Ya, Update"** di dalam modal konfirmasi (menekan tombol Update saja belum memulai update). |
| **SSH ditolak "Permission denied (publickey,password)"** | (1) Kunci Muse belum terpasang di VPS — cek `grep -c ssh-ed25519 ~/.ssh/authorized_keys` di konsol (harus **1**; bila 0, pastikan `muse-ssh.pub` ada di repo dan service sudah restart setelah deploy — lihat bagian 8). (2) Pengaturan Muse **Settings → Permissions → Direct network protocols → ssh** masih "Deny" — ubah ke "Ask". (3) Perintah SSH **wajib** memakai `-o IdentitiesOnly=yes -i /home/hatch/.ssh/id_ed25519` (atau path kunci di mesinmu) — `ssh root@IP` polos selalu gagal karena ssh mencari kunci di `/root/.ssh`. |
| **Bot menyebut "Background otomatis (gradien)"** | Itu **normal, bukan error**: galeri channel kosong, jadi panel membuat sendiri gambar gradien sesuai tema supaya Short tetap jadi. Upload gambar sendiri di panel bila mau yang lebih bagus. |
| **Tidak ada laporan pagi di Telegram** | Cek apakah VPS & service operator masih jalan (`./ziqva.sh status`). Bila service mati, restart (`./ziqva.sh restart`). |

---

## 14. Keamanan — yang wajib diingat

- **Jangan pernah menaruh token, PIN, password, atau isi `client_secret.json` di chat** (Telegram, Muse, atau di mana pun).
- File sensitif sudah di-`.gitignore` dan tidak akan ter-commit ke GitHub: `config.json`, `client_secret.json`, `channels_db.json`, `tasks_db.json`, `monitor_state.json`, `video_stats_cache.json`, `auto_shorts_state.json`, `update_status.json`, `nlu_audit.jsonl`, `nodes.json`.
- **Pengecualian yang aman:** `muse-ssh.pub` berisi **kunci PUBLIK** SSH Muse — boleh ada di repo publik. Yang tidak boleh keluar dari mesin Muse: **kunci privat** (`id_ed25519` tanpa `.pub`), PIN panel, token bot, dan password apa pun.
- PIN minimal 8 karakter (huruf + angka). Setelah 5 kali salah, login dikunci sementara.
- Semua endpoint penting di panel wajib login + token CSRF; endpoint operator Telegram memakai kunci HMAC (`operator_key`) yang hanya dibaca dari `config.json` di VPS — tidak pernah muncul di chat/log.
- Upload ke galeri hanya menerima jenis file yang diizinkan, dan URL unduhan Clipper hanya menerima host YouTube.

---

## 15. Istilah singkat (untuk pemula)

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
| **SSH** | Cara masuk & mengirim file ke VPS dari komputer lain dengan aman (pakai sepasang kunci: publik + privat). Muse memakai SSH untuk mengirim file langsung ke VPS. |
| **NLU** | Perintah bahasa manusia — pemilik mengetik kalimat sehari-hari di Telegram, bot menerjemahkan maksudnya (bukan perintah `/...`). |
| **Gradien otomatis** | Gambar background pengganti yang dibuat sendiri oleh panel (gradasi warna vertikal sesuai tema) bila galeri channel masih kosong, supaya Short tetap bisa dibuat. |
| **Shorts Muse** | Shorts yang dibuat pabrik dari judul + hook + naskah + CTA (bukan potongan video lama) — video portrait 1080×1920, 15/30/60 detik. |
| **Clipper / Shorts AI** | Alat untuk memotong klip pendek dari video YouTube (pakai subtitle/transkrip untuk mencari momen viral). |

---

## 16. Segera hadir (direncanakan, BELUM ada saat file ini ditulis)

> Urutan yang disepakati pemilik: kerjakan dari bawah daftar celah ebook dulu, baru antrean berikutnya. Yang sudah selesai **dihapus dari daftar ini** (laporan sore 17:00, tabel detail channel, tombol Tambah Channel, perapian Dashboard/Settings, jadwal otomatis Short, perpanjang jadwal 30 hari — semuanya sudah ada, lihat bagian 5).

**Selesai di v1.2.4:**
- **Short dari klip video** — Shorts Muse memakai klip video bergerak sebagai background (3 klip 10 detik disambung jadi 1 Short 30 detik portrait), bukan gambar diam + teks. Bila galeri berisi klip video (mp4/mov/webm), semua klip dipakai berurutan (diulang bila kurang dari durasi, dipotong bila lebih); bila tidak ada video, dipakai gambar galeri; bila galeri kosong total, gradien otomatis.

**Belum dikerjakan (bukan fitur, tapi langkah penting):**
- **Tes perdana / batch uji kecil** — belum ada satu video pun yang tayang dari pabrik. Rencana: Short anak-anak + CTA subscribe untuk Aruya 0089. Jalankan ini dulu sebelum batch 30 hari.
- **Galeri, bank judul, dan blueprint channel masih kosong** — isi dulu per channel (background, MP3, judul, jam tayang) supaya pabrik bisa berproduksi.

**Celah dari ebook (urut dari bawah, sesuai keputusan pemilik):**
1. **Penjaga server dari luar** — bila VPS mati, bot Telegram ikut mati dan tidak ada yang memberi tahu pemilik. (Catatan: ada "watchdog antrean" internal yang mengawasi task macet — itu beda, bukan penjaga server dari luar.)
2. **Peta aset** — dokumen akun Google mana pemilik channel apa (pemilik Barang Unick & Cozy Sax Relaxing belum diketahui) + silang data channel vs pabrik.
3. **Laporan "bintang hari ini"** — total laporan dari kenaikan positif saja, penurunan (video dihapus/di-private) dilaporkan terpisah, tampilkan channel dengan kenaikan terbesar.
4. **Duplikasi channel** — salin blueprint + galeri dari channel yang sudah terbukti ke channel baru + verifikasi jumlah file.
5. ~~Perpanjang jadwal 30 hari~~ — **selesai** (`/perpanjang`, lihat §10.3).
6. **Uji kesehatan kunci channel** ("Test Key") — panel hanya menampilkan "Connected" tanpa pernah menguji kuncinya.

**Antrean berikutnya (setelah celah ebook):**
1. **Backup otomatis** — paling kritis (VPS rusak = semua hilang). Saat ini yang ada hanya export channel manual (unduh JSON dari panel).
2. **Aktifkan Analytics** — alur auth-code lewat browser dengan client Desktop (bukan device flow) supaya jam tayang/subs per video/RPM jadi data asli, dan kolom snapshot terisi tanpa menunggu 2–56 hari.
3. **Thumbnail custom** per video.
4. **Tombol darurat** — jeda semua channel sekaligus (perintah Telegram `/darurat` + tombol panel).

**Ditunda / belum aktif:**
- **HTTPS/TLS** — ditunda atas keputusan pemilik (jangan dikerjakan sekarang).
- **Multi-node** — persiapan kodenya ada, tapi mode ini **tidak boleh diaktifkan** sampai ada VPS kedua + database/antrean bersama yang sesungguhnya.
- **2 worker bersamaan & mode overlay/zoom sebagai default render** — masih terbuka dari audit optimasi (butuh uji live).

> Bila membaca file ini setelah fitur-fitur di atas selesai, pindahkan dari bagian ini ke bagian 5 (Status terkini).

---

*Dokumen ini ditulis 18 November 2026 dan diperbarui 19 November 2026 untuk proyek ZIMUSE BOT (https://github.com/effands/muse-bot), versi 1.2.3, commit `a3225a06`. Bila ada yang tidak sesuai dengan kode terbaru, kode-lah yang benar — laporkan ke Muse agar dokumen diperbarui.*
