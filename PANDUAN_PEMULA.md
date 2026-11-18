# 🤖 PANDUAN PENGGUNAAN ZIQVA MUSE BOT

Panduan lengkap instalasi dan operasional sistem produksi konten otomatis **Ziqva Muse Bot**, baik di komputer lokal Windows maupun di Server Cloud VPS (24/7).

---

## 🚀 PANDUAN DEPLOY KE VPS (Ubuntu / Debian 24 Jam Nonstop)

Jika ingin menjalankan sistem 24 jam nonstop tanpa perlu komputer rumah terus menyala:

### 1. Beli & Siapkan VPS
* Rekomendasi OS: **Ubuntu 22.04 LTS** atau **Ubuntu 24.04 LTS** (RAM min. 2GB–4GB, 2 Core CPU).
* Penyedia: DigitalOcean, Linode, Contabo, Hetzner, AWS Lightsail, IDCloudHost, dsb.

### 2. Login ke VPS via SSH
Buka Terminal / PowerShell di komputer Anda:
```bash
ssh root@IP_VPS_ANDA
```

### 3. Clone Repository & Jalankan Installer
Ketik perintah berikut:
```bash
git clone https://github.com/effands/muse-bot.git
cd muse-bot
sudo bash install.sh
```

**Script `install.sh` akan otomatis melakukan segalanya:**
* Menginstal `ffmpeg`, Python venv, font, dan dependensi sistem Linux.
* Menginstal seluruh library Python (`requirements.txt`).
* Membuka port 8000 dan SSH pada firewall UFW.
* Mendaftarkan 2 service otomatis 24/7 (systemd):
  1. `ziqva-muse-bot` (Panel Web & Batch Engine)
  2. `ziqva-muse-operator` (Operator Bot Telegram 2 Arah `@ziqmusebot`)
* Menyalakan kedua service secara otomatis saat VPS boot.

### 4. Akses Web Panel Cloud
Buka browser favorit Anda dan kunjungi:
👉 **`http://IP_VPS_ANDA:8000`**

### 5. Kontrol Praktis di VPS
Di dalam folder `muse-bot`, Anda cukup menggunakan perintah shortcut:
* Cek Status: `./ziqva.sh status`
* Lihat Log Live: `./ziqva.sh logs`
* Restart Bot: `./ziqva.sh restart`
* Matikan Bot: `./ziqva.sh stop`

---

## 💻 PANDUAN JALANKAN DI WINDOWS (LOKAL)

1. Pastikan **Python 3.10+** dan **FFmpeg** sudah terpasang.
2. Buka folder: `D:\PROJECT 10000 CHANNEL\MUSEBOT`
3. Klik dua kali (Double-click):
   👉 **`JALANKAN_PABRIK_KONTEN.bat`**
4. Browser akan otomatis terbuka ke alamat:
   👉 **`http://localhost:8000`**
5. Jika ingin menghentikan sistem, jalankan `HENTIKAN_PABRIK_KONTEN.bat`.

---

## 🧭 Panduan Alur Kerja GUI (4 Langkah SOP)

### 1. Setup PIN Keamanan
Saat pertama kali membuka port 8000:
* Masukkan PIN rahasia (contoh: `336699`).
* Klik **Simpan PIN**.

### 2. Hubungkan Akun YouTube (Google OAuth Device Flow)
* Buka [Google Cloud Console](https://console.cloud.google.com).
* Buat Project baru dan aktifkan **YouTube Data API v3**.
* Pada OAuth Consent Screen, pilih **External** dan pastikan klik tombol **PUBLISH APP** (status **In Production**) agar token upload tidak expired setiap 7 hari.
* Buat Credentials -> OAuth Client ID -> Tipe **Desktop App** -> Download file JSON-nya.
* Masuk ke menu **Factory -> Settings**, upload file JSON tersebut, lalu klik **Connect**.
* Salin kode yang muncul ke `https://www.google.com/device` dan login akun YouTube Anda.

### 3. Masukkan Bahan Baku Media (Asset Gallery)
* **Backgrounds**: Gambar latar tema (1920x1080).
* **Audios**: File audio musik MP3.
* **Overlays**: File animasi gelombang musik transparan (`visualizer.mov`).
* **Special Songs**: 1 lagu penutup di menit-menit akhir.
* **Bank Judul**: Generate 30 judul otomatis via menu *Title Generator* atau via Telegram `/judul`.

### 4. Jadwalkan Batch Render 30 Hari
* Tentukan durasi acak 60–80 menit.
* Pilih preset render `ultrafast`.
* Klik **Buat Batch**. Sistem akan merender otomatis menjelang jam tayang dan mengupload ke YouTube terjadwal!

---

## 🤖 Perintah Interaktif Telegram (@ziqmusebot)

Ketik atau sentuh tombol di chat `@ziqmusebot`:
* `📊 Status Server` atau `/status`: Cek CPU, RAM, disk, & jumlah antrean render.
* `📺 Cek Channel` atau `/channel`: Cek daftar channel YouTube terhubung.
* `📁 Antrean Render` atau `/antrean`: Cek video yang sedang antre / proses.
* `💡 10 Judul Sleep Music` atau `/judul`: Generate 10 judul video tidur viral siap pakai.
* `📝 Template Deskripsi` atau `/deskripsi`: Template deskripsi SEO YouTube.
* `📐 Blueprint Resep` atau `/blueprint`: Standar resep video kompilasi.
