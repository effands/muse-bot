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

## 🚀 Panduan Deploy ke VPS (Ubuntu / Debian 24/7)

> 📖 **Panduan ekosistem lengkap** (cara dapat JSON TV, install VPS, Telegram, dan cara kerja sama dengan Muse — sekaligus briefing yang bisa di-paste ke Muse mana pun): **[PANDUAN_EKOSISTEM.md](PANDUAN_EKOSISTEM.md)**.

1. **Login ke VPS via SSH**:
   ```bash
   ssh root@ip-vps-anda
   ```

2. **Clone repositori**:
   ```bash
   git clone https://github.com/effands/muse-bot.git
   cd muse-bot
   ```

3. **Jalankan Installer Otomatis**:
   ```bash
   sudo bash install.sh
   ```
   Script installer akan otomatis:
   * Menginstall FFMPEG, Python venv, dan seluruh dependencies.
   * Mendaftarkan service `systemd` `ziqva-muse-bot` (Port 8000) dan `ziqva-muse-operator` (@ziqmusebot).
   * Menjalankan kedua service secara 24/7 nonstop di VPS.

4. **Buka di Browser**:
   ```
   http://IP_VPS_ANDA:8000
   ```

---

## 🛡️ Catatan Keamanan
* File sensitif (`config.json`, `client_secret.json`, `channels_db.json`, `tasks_db.json`) telah diabaikan oleh `.gitignore`.
* Pastikan status OAuth Consent Screen di Google Cloud Console diatur ke **In Production** agar sesi koneksi channel tidak kedaluwarsa tiap 7 hari.

---

## 📄 Lisensi
Hak Cipta © 2026 **Ziqva Muse Bot**. Dikembangkan untuk efisiensi produksi konten skala besar.
