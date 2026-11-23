# BRIEF — LOOP B v5 (Rumus Resmi Loop Video Zumo)

> **Tujuan dokumen ini:** supaya Muse mana pun yang baru memasang bot ini di VPS
> lain langsung paham aturan loop video yang benar, tanpa perlu tanya-tanya.
> Aturan di sini adalah KESEPAKATAN dengan user (Pak Effand) — jangan diubah
> sepihak, jangan diganti dengan cara lain yang "kelihatan sama".

---

## 1. Nama resmi

**LOOP B v5 — rumus tukar-paruh (swap halves).**

Disepakati user **22 Nov 2026 17:49 WIB** ("LOOP B v5 — RUMUS BARU ini baru
mantab"). Inilah SATU-SATUNYA rumus loop yang berlaku. Semua rumus
sebelumnya (v1–v4) sudah ditolak dan TIDAK BOLEH dipakai lagi.

---

## 2. Rumus (baca sampai paham sebelum membuat loop)

Bahan: **satu video 10 detik, 1920x1080, 24fps**.

1. **Belah** klip 10 detik tepat di tengah (t=5):
   - A = [0, 5] (paruh pertama)
   - B = [5, 10] (paruh kedua)
2. **Tukar** paruh: klip baru **S = B + A**.
3. **Titik loop S (ujung → awal) adalah frame yang SAMA** — ujung S adalah
   frame V pada t=5, awal S juga frame V pada t=5. Jadi loop-nya
   **seamless BY CONSTRUCTION** (sempurna karena konstruksi), bukan karena
   dihaluskan belakangan.
4. **Satu-satunya sambungan** kini ada **DI TENGAH klip S** (ujung B = V t=10
   bertemu awal A = V t=0). Di sinilah **dissolve** dipasang — mata membaca
   dissolve di tengah sebagai "gerak berlanjut", bukan sebagai patahan di
   titik ulang.
5. **Fade (dissolve tengah) yang disepakati v5 = 2,5 detik.**
   Maka klip seamless S = 10 − 2,5 = **7,5 detik**.
6. **Ulangi S 4x = 30 detik** (30 = 4 × 7,5). Semua sambungan (t=7,5 / 15 /
   22,5 / 30) adalah titik loop S yang sempurna.

```
video 10 dtk (1920x1080, 24fps)
   → belah t=5 → A=[0,5], B=[5,10]
   → S = B + A, dissolve 2,5 dtk DI TENGAH S
   → S = 7,5 dtk → ulang 4x → loop 30 dtk
```

---

## 3. Skrip (dipakai apa adanya, jangan ubah angkanya)

Skrip ada di workspace Muse (bukan di repo ini):

```bash
python3 ~/workspace/zumo/make_loop_swap.py <video10s.mp4> <out30s.mp4> --fade 2.5
```

- `--fade 2.5` **WAJIB** — itu angka v5 yang disetujui. Jangan pakai default
  skrip (1.0) dan jangan pakai angka lain: fade 1.0 menghasilkan klip S 9
  detik (3x + sisa 3 detik), sehingga titik loop 30 detik jatuh di tengah
  klip S dan tidak lagi sempurna.

---

## 4. Verifikasi (angka v5 yang sudah disetujui user 22 Nov 2026)

Skrip mencetak 4 angka saat selesai. Patokan v5:

| Ukuran | Angka v5 (disetujui) | Arti |
|---|---|---|
| titik loop (ujung vs awal) | **1.26** | frame ujung & awal nyaris identik |
| sambung t=10 | **1.66–1.90** | sambungan antar salinan bersih |
| sambung t=20 | **1.66–1.90** | sambungan antar salinan bersih |
| gerak tengah | **> 1.0** | ada gerakan alami di dalam loop |

**PENTING — jangan tolak loop hanya karena angka titik loop 1.26.**
Ambang ideal di skrip adalah < 1.0, tapi **1.26 adalah angka v5 yang
disetujui user** (loop kemarin yang dibilang "mantab" persis 1.26).
Yang dinilai: adegan diam + kamera terkunci + gerakan alami, dan TIDAK
ADA zoom. Kalau ketiga frame cek (awal / tengah / akhir) menunjukkan
adegan dan skala yang sama, loop DITERIMA.

---

## 5. Sumber video 10 detik (wajib baru tiap hari)

- **Generate BARU setiap hari** dari Muse / AI image-to-video, dari
  **gambar latar hari itu**.
- Gerakan harus **ALAMI dari adegannya sendiri**: hujan di jendela, lilin
  menyala, asap kopi naik, dsb.
- **Adegan statis, kamera terkunci**, 1920x1080, 24fps.
- Gerakan lembut sesuai genre; tidak ada gerakan kamera sama sekali.

---

## 6. YANG DILARANG (sudah ditolak user — JANGAN dipakai)

1. **`make_seamless_loop.py` — DILARANG.** Skrip ini men-zoom pelan gambar
   (zoompan) + crossfade di titik loop. Hasilnya video jadi **zoom in /
   zoom out terus-menerus**. User komplain 23 Nov 2026 ("ini kok foto cuma
   di zoom in zoom out"). Jangan pernah dipakai lagi, untuk channel mana
   pun.
2. **Crossfade di titik loop (v3/v4) — DILARANG.** Crossfade biasa di titik
   ulang selalu menyisakan dissolve yang terlihat ("ada gak ada") —
   patahannya tetap kelihatan. Itulah alasan v5 memakai tukar-paruh.
3. **Ping-pong / reverse — DILARANG.** Gerakan harus selalu maju
   (forward-only). Jangan pernah membalik arah gerakan.
4. **Jangan pakai gambar statis yang di-zoom** sebagai pengganti video
   10 detik. Zoom sintetis BUKAN gerakan alami dan langsung terlihat.

---

## 7. Penempatan di server

1. Loop 30 detik hasil **DIPASANG SEBELUM assemble** sebagai:
   ```
   uploads/<yt_id>/backgrounds/zumo_loop.mp4
   ```
   (wajib 1920x1080; bila file hilang/rusak, assemble jatuh ke fallback
   dan loop tidak terpakai.)
2. **Thumbnail diambil dari frame loop itu sendiri** (kode v1.4.8,
   `_zumo_loop_frame`) — bukan dari gambar latar statis — supaya thumbnail
   senada dengan isi video.
3. Loop lama diarsipkan dulu (mis. `zumo_loop_20261122.mp4`) sebelum
   diganti yang baru, jangan langsung ditimpa tanpa arsip.

---

## 8. Catatan insiden 23 Nov 2026 (pelajaran)

- Body cron executor (`jadwal-rutin-executor`) sempat **masih berisi resep
  lama** (`make_seamless_loop.py`), sementara kesepakatan sudah Loop B v5.
  Akibatnya video bank Cozy Sax jam 16:00 tanggal 23 Nov 2026 ke-render
  dengan loop zoom — video tayang, judul + deskripsi sudah oke, tapi
  visualnya salah.
- Diperbaiki hari yang sama (23 Nov 2026): body cron diganti ke
  `make_loop_swap.py --fade 2.5`, video di-render ulang dengan loop yang
  benar (6 lagu, judul, dan deskripsi yang SAMA — hanya visual diganti),
  video zoom lama dihapus.
- **Pelajaran: resep loop di cron HARUS sama dengan brief ini.** Setiap
  kali rumus loop berubah, ubah brief ini DAN body cron dalam satu
  pekerjaan yang sama — jangan pernah hanya satu sisi.

---

## 9. Ringkasan satu baris (untuk ditempel di instruksi cron)

> Loop video Zumo HANYA Loop B v5: video 10 dtk → tukar paruh (swap
> halves) → dissolve 2,5 dtk di tengah → klip 7,5 dtk ×4 = 30 dtk,
> `make_loop_swap.py --fade 2.5`. DILARANG: zoom (make_seamless_loop.py),
> crossfade di titik loop, ping-pong/reverse.
