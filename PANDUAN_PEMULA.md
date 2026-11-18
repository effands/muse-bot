# 🏭 PANDUAN PENGGUNAAN PABRIK KONTEN & MUSEBOT (LOKAL WINDOWS)

Panduan ini dibuat khusus agar mudah dipahami bagi pemula yang menjalankan sistem di komputer lokal Windows.

---

## ⚡ Cara Menjalankan (Satu Kali Klik)

1. Buka folder: `D:\PROJECT 10000 CHANNEL\MUSEBOT`
2. **Klik dua kali (Double-click)** file:
   👉 **`JALANKAN_PABRIK_KONTEN.bat`**
3. Jendela hitam (command prompt) akan terbuka dan otomatis membuka browser Anda ke alamat:
   👉 **`http://localhost:8000`**
4. **PENTING**: Biarkan jendela hitam tetap terbuka selama Anda menggunakan aplikasi. Jika ingin mematikan, cukup tutup jendela hitam tersebut atau jalankan `HENTIKAN_PABRIK_KONTEN.bat`.

---

## 🧭 Langkah Pertama di Layar Tampilan (GUI)

### 1. Buat PIN Keamanan Pertama Kali
Saat browser terbuka ke `http://localhost:8000`, Anda akan melihat halaman **Setup PIN**.
* Masukkan PIN angka/karakter yang Anda inginkan (misal: `123456`).
* Ulangi PIN yang sama di kolom konfirmasi.
* Klik tombol Simpan.

### 2. Memilih Modul di "Pabrik Konten Hub"
Setelah login dengan PIN, Anda akan melihat 2 menu utama:
* 🏭 **Factory**: Mesin utama untuk merender video musik/niche panjang (60-80 menit), audio visualizer, dan penjadwalan batch ke banyak channel YouTube.
* ✂️ **Clipper**: Modul AI pemotong klip video pendek/shorts dengan subtitle otomatis.

---

## 🎬 Cara Mengisi Channel & Bahan Konten (Di Menu Factory)

Sesuai panduan di Ebook Bab 3:
1. **Tambah Channel**: Masukkan nama dan target niche channel Anda.
2. **Galeri Media Per Channel**:
   * **Backgrounds**: Masukkan gambar latar video (resolusi 1920x1080).
   * **Audios**: Masukkan file lagu MP3 yang ingin dikompilasi jadi video panjang.
   * **Overlays**: Masukkan file gelombang musik transparan (`visualizer.mov`).
   * **Special Songs**: Masukkan 1-2 lagu penutup khusus di menit terakhir.
3. **Bank Judul**: Masukkan daftar judul siap pakai (1 baris = 1 judul).
4. **Buat Batch Jadwal**: Tentukan mau bikin berapa video (misal 30 video untuk 30 hari), set durasi 60-80 menit, dan sistem akan mengantre render secara otomatis!

---

## 🔑 Menghubungkan Akun YouTube (Google OAuth)

1. Buka [Google Cloud Console](https://console.cloud.google.com).
2. Buat Project baru dan aktifkan **YouTube Data API v3**.
3. Di OAuth Consent Screen, pilih **External** dan pastikan klik tombol **PUBLISH APP** agar statusnya **In Production** (agar token login tidak kedaluwarsa tiap 7 hari).
4. Buat Credentials -> OAuth Client ID -> Tipe **Desktop App** -> Download file JSON-nya.
5. Di panel Factory, unggah file JSON tersebut, lalu klik **Connect**.
6. Panel akan memunculkan kode perangkat (Device Code). Masukkan kode tersebut melalui link yang diberikan dan login dengan akun YouTube Anda. Status akan berubah menjadi **Connected**!

---

## 📱 Notifikasi Telegram (Operator AI / Watchdog)
Di bagian pengaturan panel:
* Anda bisa memasukkan **Telegram Bot Token** dan **Telegram Chat ID**.
* Panel akan otomatis mengirimkan notifikasi ke Telegram Anda hanya jika ada render yang error atau jika jadwal tayang channel Anda tersisa kurang dari 48 jam.
