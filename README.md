# 🤖 ZIQVA MUSE BOT (Cloud Production Engine)

> **Autonomous Video Factory, Clipper AI & Multi-Channel Content Automation Platform**  
> Sistem otomasi produksi video massal, audio visualizer kompilasi panjang (60–80 menit), pemotong klip AI (Clipper Pro), dan manajemen jadwal upload multi-channel YouTube dengan pendampingan Operator Telegram 2 Arah (@ziqmusebot).

---

## 🌟 Fitur Utama

### 1. 🏭 Pabrik Konten (Factory Batch Engine)
* **Video Compilation Generator**: Otomatis menggabungkan background gambar bertema, playlist musik MP3, audio visualizer waveform transparan (`visualizer.mov`), dan ending track khusus.
* **YouTube Data API v3 OAuth (Device Flow)**: Menghubungkan channel YouTube tanpa perlu login ketik password di panel (alur kode verifikasi perangkat).
* **Smart Blueprint & Preset**: Menentukan resep durasi video (acak 60–80 menit), preset render FFmpeg ultrafast, template deskripsi SEO, dan tag per channel.
* **Batch Scheduler**: Menjadwalkan kalender konten harian (misal 30 hari ke depan) dalam satu kali klik.

### 2. ✂️ Clipper Pro (AI Video Slicer)
* **Direct yt-dlp Stream**: Potong klip video viral langsung dari tautan YouTube tanpa download manual.
* **Whisper AI Subtitles**: Pembuatan subtitle dan auto-caption otomatis model speech-to-text.
* **Format Shorts (9:16)**: Otomatis cropping dan optimasi tata letak vertikal untuk YouTube Shorts, TikTok, dan Instagram Reels.

### 3. 🤖 2-Way Telegram Operator (@ziqmusebot) & Watchdog
* **Kendali Interaktif Dua Arah**: Tekan tombol menu untuk cek `/status` server, `/channel` aktif, `/antrean` render, serta generate `/judul` viral & `/deskripsi` SEO secara instan.
* **Quiet Watchdog**: Memantau kesehatan server 24/7 dan hanya mengirim peringatan jika terjadi kegagalan render atau stok video menipis.

---

## 🛠️ Arsitektur & Teknologi

* **Backend**: Python 3.10+, Flask, Werkzeug
* **Video & Audio Processing**: FFmpeg, Librosa, OpenCV, ImageIO
* **AI & Subtitling**: Faster-Whisper, PyTorch
* **Downloader**: yt-dlp
* **Frontend**: HTML5, Tailwind CSS, FontAwesome, Google Fonts, Responsive Glassmorphism Theme

---

## 🚀 Pasang ke Server Baru (1 perintah)

> 📖 **Panduan ekosistem lengkap** (cara dapat JSON TV, install VPS, Telegram, dan cara kerja sama dengan Muse — sekaligus briefing yang bisa di-paste ke Muse mana pun): **[PANDUAN_EKOSISTEM.md](PANDUAN_EKOSISTEM.md)**.

```bash
git clone https://github.com/effands/muse-bot.git && cd muse-bot && sudo bash install.sh
```

Installer otomatis mengurus semuanya: FFMPEG, Python venv + semua dependencies, folder data, firewall (port 8000 & SSH), service `systemd` `ziqva-muse-bot` (Port 8000) dan `ziqva-muse-operator` (@ziqmusebot) yang jalan 24/7.

**Yang perlu diedit cuma 1 baris** di paling atas `install.sh` (kalau perlu saja):

```bash
PORT="8000"          # port panel (ganti kalau bentrok)
```

Sisanya otomatis — IP server dideteksi sendiri, folder aplikasi ikut lokasi script.

### 🔑 Hubungkan ke Muse

Tidak perlu setting apa-apa. Installer memasang kunci SSH publik Muse (`muse-ssh.pub` dari repo) ke `~/.ssh/authorized_keys` saat instalasi, dan `app.py` memasangnya lagi tiap kali nyala (cadangan). Setelah instalasi selesai, Muse langsung bisa masuk ke server ini.

Cek dari Muse:

```bash
ssh -o BatchMode=yes -o IdentitiesOnly=yes -i ~/.ssh/id_ed25519 root@IP_SERVER 'echo OK'
```

### ✅ Setelah Instalasi

1. Buka panel di browser: `http://IP_SERVER:8000` — buat PIN saat pertama kali.
2. Di Settings panel, isi **Token Bot Telegram** dan **API key Kie** (untuk Zumo).
3. Tambah channel YouTube lewat panel (login browser) atau device login.

---

## 🛡️ Catatan Keamanan
* File sensitif (`config.json`, `client_secret.json`, `channels_db.json`, `tasks_db.json`) telah diabaikan oleh `.gitignore`.
* Pastikan status OAuth Consent Screen di Google Cloud Console diatur ke **In Production** agar sesi koneksi channel tidak kedaluwarsa tiap 7 hari.

---

## 📄 Lisensi
Hak Cipta © 2026 **Ziqva Muse Bot**. Dikembangkan untuk efisiensi produksi konten skala besar.
