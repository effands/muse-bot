# 🏭 MUSE BOT & PABRIK KONTEN

> **Autonomous Video Factory & Multi-Channel Content Automation Platform**  
> Sistem otomasi produksi video massal, audio visualizer kompilasi panjang (60–80 menit), pemotong klip AI (Clipper Pro), dan manajemen jadwal upload multi-channel YouTube dengan pendampingan Operator AI (Muse).

---

## 🌟 Fitur Utama

### 1. 🏭 Pabrik Konten (Factory Batch Engine)
* **Video Compilation Generator**: Otomatis menggabungkan background bertema, playlist lagu MP3, audio visualizer waveform transparan (`visualizer.mov`), dan ending track khusus.
* **YouTube Data API v3 OAuth (Device Flow)**: Menghubungkan puluhan channel YouTube tanpa perlu login ketik password di panel (alur kode perangkat seperti YouTube TV).
* **Smart Blueprint & Preset**: Menentukan resep durasi video (acak 60–80 menit), preset render FFmpeg ultrafast, template deskripsi SEO, dan tag per channel.
* **Batch Scheduler**: Menjadwalkan kalender konten harian (misal 30 hari ke depan) dalam satu klik.

### 2. ✂️ Clipper Pro (AI Video Slicer)
* **Direct yt-dlp Stream**: Potong video langsung dari tautan YouTube tanpa download manual.
* **Whisper AI Subtitles**: Pembuatan subtitle dan auto-caption otomatis dengan model speech-to-text.
* **Format Shorts (9:16)**: Otomatis cropping dan optimasi tata letak untuk YouTube Shorts, TikTok, dan Instagram Reels.

### 3. 📱 Quiet Watchdog & Telegram Notification
* **Notifikasi Hening**: Hanya melapor jika ada kegagalan render atau stok jadwal channel tersisa kurang dari 48 jam.
* **Laporan Kenaikan Views**: Pemantauan metrik publik setiap pagi untuk melihat channel yang sedang bertumbuh.

---

## 🛠️ Arsitektur & Teknologi

* **Backend**: Python 3.12, Flask, Werkzeug
* **Video & Audio Processing**: FFmpeg, Librosa, OpenCV, ImageIO
* **AI & Subtitling**: Faster-Whisper, PyTorch
* **Downloader**: yt-dlp
* **Frontend**: HTML5, Tailwind CSS, FontAwesome, Google Fonts (Plus Jakarta Sans & JetBrains Mono), Dark Glassmorphism Theme

---

## 🚀 Panduan Memulai (Quick Start)

### A. Komputer Lokal (Windows)
1. Pastikan **Python 3.10+** dan **FFmpeg** sudah terpasang.
2. Clone repositori ini:
   ```bash
   git clone https://github.com/effands/muse-bot.git
   cd muse-bot
   ```
3. Install dependensi:
   ```bash
   pip install -r requirements.txt
   ```
4. Jalankan sistem:
   * Cukup **klik dua kali** file `JALANKAN_PABRIK_KONTEN.bat`
   * Atau jalankan via terminal:
     ```bash
     python app.py
     ```
5. Buka dashboard di browser Anda: `http://localhost:8000`

---

### B. Server VPS (Ubuntu / Debian 24/7)
1. Unggah source code ke server VPS.
2. Jalankan script instalasi otomatis:
   ```bash
   sudo bash install.sh
   ```
3. Panel akan otomatis terpasang sebagai service `systemd` di port `8000` dan menyala 24/7 nonstop.

---

## 🛡️ Catatan Keamanan
* File sensitif (`config.json`, `client_secret.json`, `channels_db.json`, `tasks_db.json`) telah diabaikan oleh `.gitignore` dan tidak akan terunggah ke repositori publik.
* Pastikan status OAuth Consent Screen di Google Cloud Console diatur ke **In Production** agar sesi koneksi channel tidak kedaluwarsa dalam 7 hari.

---

## 📄 Lisensi
Hak Cipta © 2026. Dikembangkan untuk efisiensi produksi konten otomatis skala besar.
