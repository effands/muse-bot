"""Zumo / Kie.ai pipeline — core murni (v1.3.0).

Modul ini TIDAK mengimpor Flask dan TIDAK membaca config panel: semua
yang butuh secret (API key Kie) diterima sebagai argumen dari app.py.
Dipakai app.py (build thread) + harness (mock).

Sumber kebenaran API: docs.kie.ai (dibaca 2026-11-19) + ZUMO_DESIGN.md.
Ketidakpastian yang MASIH TERBUKA dan harus dijawab 1x generate tes live:
  1. Bentuk hasil polling (resultJson vs sunoData) — parser di sini
     menangani keduanya, tapi mana yang benar-benar keluar belum terlihat.
  2. Model V5.5 ("V5_5" di API) ditandai Discontinued di docs — mungkin
     masih jalan, mungkin ditolak. Kode mencoba model yang diminta dulu,
     lalu fallback V6 bila endpoint menolak (422 / fail).
  3. Biaya lyrics endpoint tidak tertulis di docs — cek creditsConsumed.
  4. Durasi aktual lagu vs target 240-360 dtk.

Aturan keamanan: key TIDAK PERNAH ditulis ke log/pesan — hanya mask
('....' + 4 karakter terakhir) yang boleh keluar dari modul ini.
"""

import json
import math
import os
import random
import re
import shutil
import subprocess
import time

import requests

KIE_BASE = 'https://api.kie.ai'
KIE_CREATE_URL = KIE_BASE + '/api/v1/jobs/createTask'
KIE_RECORD_URL = KIE_BASE + '/api/v1/jobs/recordInfo'
KIE_CREDIT_URL = KIE_BASE + '/api/v1/chat/credit'

# Model yang bisa dipilih user (nama tampilan) -> nama di API Kie.
# V5.5 tampil sebagai "V5.5" di Telegram/panel, tapi string API-nya
# "V5_5" (lihat docs.kie.ai: tabel limit memakai V5_5; contoh node
# ComfyUI "Add Vocals V5.5" memakai inner model "V5_5").
ZUMO_MODELS = ['V5.5', 'V6', 'V6_MINI', 'V6_WILD']
ZUMO_MODEL_API = {'V5.5': 'V5_5', 'V6': 'V6', 'V6_MINI': 'V6_MINI',
                  'V6_WILD': 'V6_WILD'}
ZUMO_DEFAULT_MODEL = 'V5.5'

ZUMO_COZY_YT_ID = 'UCg8be5L4_CieGjXA8ejB1_Q'
ZUMO_BG_FILENAME = 'zumo_cozy_sax_bg.jpg'

# Profil racik — PER CHANNEL / PER ORDER, bukan konstanta tunggal.
# Default di bawah ini adalah profil COZY SAX (sax romantis, lirik
# Inggris) — dipakai bila channel belum punya profil tersimpan. Profil
# tiap channel tersimpan di config panel (`zumo_profiles`, key = yt_id)
# dan bisa diubah user via Telegram per order; field yang dikirim di
# order disimpan sebagai profil channel itu. JANGAN hardcode satu genre:
# contoh user 20:44 WIB (reggae Indonesia, bariton pria) membuktikan
# satu channel bisa punya gaya yang sama sekali berbeda.
ZUMO_DEFAULT_PROFILE = {
    'style': 'smooth jazz saxophone, warm, relaxing, romantic, cozy cafe dinner late night',
    'lyrics_language': 'en',
    'vocal_gender': 'f',
    'model': ZUMO_DEFAULT_MODEL,
    'duration_min': 240,
    'duration_max': 360,
    'particle': 'snow',          # 'snow' | 'sakura'
    'spectrum_color': 'white',   # 'white' | 'cyan' | 'viridis'
    'song_titles': 3,            # judul per playlist -> 6 varian
    'songs_keep': 5,             # varian yang dipakai dari 6
}

# Profil contoh dari user (20:44 WIB, file
# ~/workspace/zumo-assets/contoh-lirik-zumo.md): reggae Indonesia,
# vokal bariton pria — BUKAN default, hanya referensi format + contoh
# profil lain. Target channel lagu reggae ini BELUM dikonfirmasi user.
ZUMO_REGGAE_PROFILE = {
    'style': 'Indonesian acoustic reggae, warm, relaxing, soulful, rebana, bamboo suling, laid-back groove',
    'lyrics_language': 'id',
    'vocal_gender': 'm',
    'model': ZUMO_DEFAULT_MODEL,
    'duration_min': 240,
    'duration_max': 360,
    'particle': 'snow',
    'spectrum_color': 'white',
    'song_titles': 3,
    'songs_keep': 5,
}

# Format lirik gaya Suno (dari user 20:44 WIB): tiap bagian diawali tag
# [Bagian: arahan] berisi PERINTAH cara bagian itu dinyanyikan/dimainkan
# (instrumen, groove, cara vokal, mood). Tag [..] adalah instruksi,
# BUKAN dinyanyikan. Urutan bagian baku (lihat contoh-lirik-zumo.md):
ZUMO_LYRIC_SECTIONS = [
    'Intro', 'Vocal Style', 'Verse 1', 'Pre-Chorus', 'Chorus - Hook',
    'Instrumental Break', 'Verse 2', 'Pre-Chorus', 'Chorus - Hook',
    'Bridge', 'Chorus - Final Hook', 'Outro', 'End',
]

# EQ preset PIANO Zumo (terverifikasi tersimpan di panel Zumo):
# (frekuensi Hz, gain dB, lebar oktaf). Lebar = setengah jarak geometris
# ke tetangga terdekat — strategi terbaik dari uji empiris (§3.1 desain:
# error terburuk +0,7 dB vs +12 dB bila 1 oktaf semua band).
# STATUS JUJUR: replikasi ini "mendekati, belum terbukti sama" dengan
# Mastered WAV Zumo sampai kalibrasi vs WAV (§3.1) — JANGAN klaim identik.
ZUMO_PIANO_EQ = [
    (60, 0.7, 0.5),
    (120, -0.2, 0.5),
    (250, -0.7, 0.5),
    (500, -0.2, 0.5),
    (1000, 0.2, 0.5),
    (2000, 1.7, 0.5),
    (4000, 2.6, 0.5),
    (8000, 3.8, 0.292),
    (12000, 4.6, 0.208),
    (16000, 5.8, 0.085),
    (18000, 5.8, 0.076),
    (20000, 4.1, 0.076),
]

ZUMO_CROSSFADE_SEC = 1.5
ZUMO_POLL_INTERVAL_SEC = 15
ZUMO_POLL_TIMEOUT_SEC = 600


class KieError(Exception):
    """Gagal di API Kie. `code` = kode body Kie ('401','402','422','429',
    '455','500','501','fail','timeout','network', ...)."""

    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = str(code) if code is not None else None


def zumo_mask_key(key):
    """Mask key untuk log/pesan: '....' + 4 karakter terakhir (pola Pixabay)."""
    k = (key or '').strip()
    if len(k) <= 4:
        return '....'
    return '....' + k[-4:]


def zumo_lyrics_has_tags(text):
    """Apakah lirik memakai format Suno (ada tag [Bagian: arahan])?
    Tag dikenali dari '[...]' di awal baris — mis. [Verse 1: ...],
    [Chorus - Hook: ...], [End]."""
    import re
    t = text or ''
    return bool(re.search(r'(?m)^\s*\[(Intro|Vocal Style|Verse|Pre-Chorus|Chorus|Instrumental|Bridge|Outro|End)\b', t))


def zumo_normalize_lyrics(text):
    """Rapikan lirik tempel/hasil generate TANPA PERNAH membuang tag [..].
    Tag adalah instruksi, bukan dinyanyikan — dikirim utuh sebagai field
    `lyrics` di custom mode Kie. Hanya: rapikan spasi kosong berlebih,
    dan tambahkan '[End]' bila tidak ada (penanda akhir baku Suno)."""
    t = (text or '').strip()
    if not t:
        return ''
    lines = [ln.rstrip() for ln in t.splitlines()]
    # Buang maksimal 2 baris kosong beruntun -> 1 baris kosong.
    out, blank = [], 0
    for ln in lines:
        if ln.strip() == '':
            blank += 1
            if blank > 1:
                continue
        else:
            blank = 0
        out.append(ln)
    t = '\n'.join(out).strip()
    if '[End]' not in t:
        t += '\n[End]'
    return t


def zumo_lyrics_prompt(theme, profile):
    """Prompt untuk lyrics endpoint (jalur B) — maks 200 karakter.
    Meminta lirik format Suno: tag [Bagian: arahan] per bagian + lirik
    dalam bahasa profil. Struktur diminta singkat supaya muat di batas
    200 karakter endpoint."""
    lang = (profile or {}).get('lyrics_language') or 'en'
    lang_txt = 'Bahasa Indonesia' if lang == 'id' else 'English'
    vg = (profile or {}).get('vocal_gender') or 'f'
    voice = 'male baritone' if vg == 'm' else 'female vocal'
    style = ((profile or {}).get('style') or '')[:60]
    prompt = (f'{theme}: {style}. Lyrics in {lang_txt}, {voice}, '
              f'Suno format with [Verse]/[Chorus]/[Bridge] direction tags, '
              f'structured song with hook')
    return prompt[:200]


def _kie_headers(api_key):
    return {'Authorization': 'Bearer ' + (api_key or '').strip(),
            'Content-Type': 'application/json'}


def _kie_check_body(body, what):
    """HTTP Kie selalu 200 — kegagalan dibaca dari field `code` di body."""
    if not isinstance(body, dict):
        raise KieError(f'{what}: respons Kie tidak dikenali', code='bad-response')
    code = body.get('code')
    if code != 200:
        msg = (body.get('msg') or body.get('message') or '').strip()
        raise KieError(f'{what}: Kie menolak (code {code})'
                       + (f' — {msg}' if msg else ''), code=code)
    return body.get('data') or {}


def _kie_post(api_key, url, payload, timeout=60):
    try:
        r = requests.post(url, headers=_kie_headers(api_key),
                          json=payload, timeout=timeout)
    except requests.RequestException as e:
        raise KieError(f'Jaringan Kie gagal: {e}', code='network')
    try:
        body = r.json()
    except Exception:
        raise KieError(f'Kie mengembalikan bukan-JSON (HTTP {r.status_code})',
                       code='bad-response')
    return _kie_check_body(body, 'createTask' if 'createTask' in url else 'Kie')


def _kie_get(api_key, url, timeout=60):
    try:
        r = requests.get(url, headers=_kie_headers(api_key), timeout=timeout)
    except requests.RequestException as e:
        raise KieError(f'Jaringan Kie gagal: {e}', code='network')
    try:
        body = r.json()
    except Exception:
        raise KieError(f'Kie mengembalikan bukan-JSON (HTTP {r.status_code})',
                       code='bad-response')
    return _kie_check_body(body, 'Kie')


def kie_create_music(api_key, model, title, lyrics, style, duration=None,
                     vocal_gender='f', variety=2, custom_mode=True,
                     instrumental=False):
    """Buat task generate musik. Kembalikan taskId (str).
    `model` = nama tampilan ('V5.5'/'V6'/...) — dipetakan ke string API.
    `lyrics` dikirim UTUH apa adanya sebagai field `lyrics` custom mode
    — termasuk tag [Bagian: arahan] gaya Suno (instruksi, bukan dinyanyikan).
    JANGAN buang tag-nya dan JANGAN kirim lirik sebagai `prompt` biasa.
    `duration` hanya dikirim untuk keluarga V6 (docs: V5_5 dicoret dari
    daftar model yang menghormati duration) — untuk V5.5 durasi aktual
    diterima apa adanya.
    Instrumental (user 20:58 WIB): kirim instrumental=true, TANPA
    lyrics/prompt/vocal_gender (Kie menolak campuran). Mastering EQ PIANO
    tetap diterapkan per lagu sesudah download — mastering wajib untuk
    vokal MAUPUN instrumental, tidak ada jalur yang melewatkannya."""
    api_model = ZUMO_MODEL_API.get(model, model)
    inp = {'custom_mode': bool(custom_mode),
           'model': api_model,
           'title': (title or '')[:80],
           'style': (style or '')[:1000],
           'variety': int(variety)}
    if instrumental:
        inp['instrumental'] = True
    else:
        inp['vocal_gender'] = vocal_gender
    if custom_mode:
        if not instrumental and lyrics:
            inp['lyrics'] = lyrics[:5000]
        if duration and api_model in ('V6', 'V6_MINI', 'V6_WILD'):
            inp['duration'] = int(max(10, min(360, duration)))
    payload = {'model': 'ai-music-api/generate', 'input': inp}
    data = _kie_post(api_key, KIE_CREATE_URL, payload, timeout=60)
    task_id = (data.get('taskId') or '').strip()
    if not task_id:
        raise KieError('Kie tidak mengembalikan taskId', code='bad-response')
    return task_id


def kie_create_music_prompt(api_key, model, prompt, title=''):
    """Jalur A (fallback): non-custom — Kie membuat lirik sendiri dari tema.
    Tidak bisa atur duration / vocal_gender."""
    api_model = ZUMO_MODEL_API.get(model, model)
    inp = {'custom_mode': False,
           'model': api_model,
           'prompt': (prompt or '')[:3000]}
    if title:
        inp['title'] = title[:80]
    payload = {'model': 'ai-music-api/generate', 'input': inp}
    data = _kie_post(api_key, KIE_CREATE_URL, payload, timeout=60)
    task_id = (data.get('taskId') or '').strip()
    if not task_id:
        raise KieError('Kie tidak mengembalikan taskId', code='bad-response')
    return task_id


def kie_create_lyrics(api_key, prompt):
    """Jalur B: generate lirik dari tema (maks 200 karakter). Kembalikan taskId."""
    payload = {'model': 'ai-music-api/generate-lyrics',
               'input': {'prompt': (prompt or '')[:200]}}
    data = _kie_post(api_key, KIE_CREATE_URL, payload, timeout=60)
    task_id = (data.get('taskId') or '').strip()
    if not task_id:
        raise KieError('Kie tidak mengembalikan taskId', code='bad-response')
    return task_id


def kie_poll(api_key, task_id, timeout=ZUMO_POLL_TIMEOUT_SEC,
             interval=ZUMO_POLL_INTERVAL_SEC, sleep_fn=None):
    """Poll recordInfo sampai success/fail/timeout. Kembalikan dict `data`
    mentah (berisi state, resultJson/sunoData, costTime, creditsConsumed).
    `sleep_fn` bisa diinjeksi harness (mock) supaya tes tidak menunggu."""
    sleep = sleep_fn or time.sleep
    deadline = time.time() + timeout
    last = {}
    while True:
        data = _kie_get(api_key, KIE_RECORD_URL + '?taskId=' + task_id,
                        timeout=30)
        last = data
        state = (data.get('state') or '').strip().lower()
        if state == 'success':
            return data
        if state == 'fail':
            raise KieError(
                'Generate Kie gagal: ' + (data.get('failMsg') or data.get('failCode') or 'tanpa pesan'),
                code=data.get('failCode') or 'fail')
        if time.time() >= deadline:
            raise KieError(f'Polling Kie timeout {timeout} dtk (task {task_id})',
                           code='timeout')
        sleep(interval)


def _parse_result_json(raw):
    """resultJson bisa string JSON atau dict. Kembalikan dict/list atau None."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, list):
        return raw
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except Exception:
            return None
    return None


def parse_kie_tracks(record):
    """Ambil daftar track audio dari hasil polling. Menangani DUA bentuk
    yang disebut di §1.2 desain: `resultJson` (string/dict, berisi
    `data[]` ala callback Suno atau `resultUrls[]` ala skema generik)
    dan `sunoData`. Kembalikan list dict {id, audio_url, stream_audio_url,
    title, duration, image_url} — bisa kosong bila bentuk tak dikenali."""
    data = record or {}
    candidates = []
    for key in ('resultJson', 'sunoData', 'result'):
        v = _parse_result_json(data.get(key))
        if v is not None:
            candidates.append(v)
    tracks = []
    for cand in candidates:
        items = None
        if isinstance(cand, list):
            items = cand
        elif isinstance(cand, dict):
            if isinstance(cand.get('data'), list):
                items = cand['data']
            elif isinstance(cand.get('resultUrls'), list):
                items = [{'audio_url': u} for u in cand['resultUrls']]
            elif isinstance(cand.get('tracks'), list):
                items = cand['tracks']
        if not items:
            continue
        for it in items:
            if not isinstance(it, dict):
                continue
            url = (it.get('audio_url') or it.get('audioUrl') or '').strip()
            if not url:
                continue
            try:
                dur = float(it.get('duration') or 0)
            except Exception:
                dur = 0.0
            tracks.append({
                'id': str(it.get('id') or ''),
                'audio_url': url,
                'stream_audio_url': (it.get('stream_audio_url') or it.get('streamAudioUrl') or '').strip(),
                'title': (it.get('title') or '').strip(),
                'duration': dur,
                'image_url': (it.get('image_url') or it.get('imageUrl') or '').strip(),
            })
        if tracks:
            return tracks
    return tracks


def parse_kie_lyrics(record):
    """Ambil variasi lirik dari hasil generate-lyrics:
    resultJson.resultObject.lyricsData[] ({text, title, status, tags}).
    Kembalikan list dict {title, text, tags} (hanya yang status-nya
    sukses / tanpa status)."""
    data = record or {}
    parsed = _parse_result_json(data.get('resultJson'))
    out = []
    if isinstance(parsed, dict):
        robj = parsed.get('resultObject') or parsed
        items = robj.get('lyricsData') if isinstance(robj, dict) else None
        if isinstance(items, list):
            for it in items:
                if not isinstance(it, dict):
                    continue
                text = (it.get('text') or '').strip()
                if not text:
                    continue
                st = (it.get('status') or '').strip().lower()
                if st and st not in ('success', 'completed', 'complete'):
                    continue
                out.append({'title': (it.get('title') or '').strip(),
                            'text': text,
                            'tags': (it.get('tags') or '').strip()})
    return out


def kie_download(url, dest, timeout=300):
    """Unduh file hasil (MP3) — streaming supaya file besar aman."""
    try:
        with requests.get(url, timeout=timeout, stream=True) as r:
            r.raise_for_status()
            os.makedirs(os.path.dirname(dest) or '.', exist_ok=True)
            with open(dest, 'wb') as f:
                for chunk in r.iter_content(chunk_size=1 << 20):
                    if chunk:
                        f.write(chunk)
    except requests.RequestException as e:
        raise KieError(f'Unduh audio Kie gagal: {e}', code='download')
    if not os.path.exists(dest) or os.path.getsize(dest) <= 0:
        raise KieError('Unduh audio Kie menghasilkan file kosong', code='download')
    return dest


def kie_get_balance(api_key):
    """Saldo kredit (float) atau None bila tidak terbaca. Tidak pernah
    melempar untuk kesalahan jaringan — pemanggil menampilkan 'tidak tahu'."""
    try:
        data = _kie_get(api_key, KIE_CREDIT_URL, timeout=30)
    except Exception:
        return None
    # 🔥 v1.3.45: API Kie mengembalikan `data` sebagai ANGKA (mis. 43.5),
    # bukan dict — versi lama menolaknya sehingga saldo selalu 'tidak
    # terbaca' padahal key valid (terbukti live: 43.5 / 80.0 / 9990.0).
    if isinstance(data, (int, float)):
        return float(data)
    if not isinstance(data, dict):
        return None
    for k in ('credits', 'credit', 'balance', 'remaining', 'remainingCredits'):
        try:
            if data.get(k) is not None:
                return float(data[k])
        except Exception:
            continue
    return None


def kie_call_with_failover(keys, fn, disable_cb=None):
    """Jalankan fn(key) dengan failover multi-key berurutan (pola Pixabay):
    401/402 -> tandai key nonaktif (via disable_cb) lalu key berikut;
    429 -> key berikut TANPA menonaktifkan; error lain -> gagal langsung.
    Kembalikan (hasil, index_key_yang_dipakai). Key TIDAK PERNAH muncul
    di pesan error."""
    last_err = None
    for idx, key in enumerate(keys or []):
        try:
            return fn(key), idx
        except KieError as e:
            last_err = e
            code = (e.code or '')
            if code in ('401', '402'):
                try:
                    if disable_cb:
                        disable_cb(key)
                except Exception:
                    pass
                continue
            if code == '429':
                continue
            raise
        except Exception:
            raise
    if last_err is not None:
        raise last_err
    raise KieError('Tidak ada API key Kie yang bisa dipakai', code='no-key')


def shuffle_zumo_tracks(pairs, keep=5, rng=None):
    """Acak varian lagu supaya judul yang sama TIDAK PERNAH berdampingan
    (§4.2 desain). `pairs` = list dict dengan kunci 'title' (judul lagu).
    Kembalikan list sepanjang min(keep, len(pairs)) — bila varian < 4,
    semua dipakai apa adanya (pemanggil melapor ke user)."""
    rng = rng or random
    items = list(pairs or [])
    rng.shuffle(items)
    if keep is not None:
        keep = max(0, min(int(keep), len(items)))
        items = items[:keep]

    def _has_adjacent(lst):
        return any(lst[i].get('title') == lst[i + 1].get('title')
                   for i in range(len(lst) - 1))

    # Perbaiki berdampingan: tukar dengan lagu terjauh yang judulnya beda.
    guard = 0
    while _has_adjacent(items) and guard < 100:
        guard += 1
        fixed = False
        for i in range(len(items) - 1):
            if items[i].get('title') == items[i + 1].get('title'):
                for j in range(len(items) - 1, -1, -1):
                    if (items[j].get('title') != items[i].get('title')
                            and items[j].get('title') != items[i + 1].get('title')):
                        items[i + 1], items[j] = items[j], items[i + 1]
                        fixed = True
                        break
                if fixed:
                    break
        if not fixed:
            break
    return items


def zumo_piano_eq_filter():
    """Rantai equalizer PIANO (12 band) sebagai SATU string filter FFmpeg.
    Lihat ZUMO_PIANO_EQ — status 'mendekati, belum terbukti sama'."""
    parts = []
    for freq, gain, width in ZUMO_PIANO_EQ:
        parts.append(f'equalizer=f={freq}:width_type=o:width={width}:g={gain}')
    return ','.join(parts)


def zumo_media_duration(path, ffprobe_path='ffprobe'):
    """Durasi detik (float) via ffprobe, 0.0 bila gagal."""
    try:
        out = subprocess.run(
            [ffprobe_path, '-v', 'error', '-show_entries', 'format=duration',
             '-of', 'default=noprint_wrappers=1:nokey=1', path],
            capture_output=True, text=True, timeout=120)
        return float((out.stdout or '').strip())
    except Exception:
        return 0.0


def zumo_detect_bpm(audio_path, ffmpeg_path='ffmpeg'):
    """Deteksi tempo (BPM) dari envelope energi + autokorelasi (numpy).
    Mengembalikan (bpm_float, confidence_0_1, method_str).
    Musik sax santai ketukannya lembut — bila tidak ada puncak tempo yang
    jelas, fallback (60.0, 0.0, 'fallback-60') dan pemanggil MELAPORKAN
    itu di job (bukan pura-pura yakin)."""
    try:
        import numpy as np
    except Exception:
        return 60.0, 0.0, 'fallback-60'
    try:
        raw = subprocess.run(
            [ffmpeg_path, '-v', 'error', '-i', audio_path,
             '-ac', '1', '-ar', '11025', '-f', 'f32le', '-'],
            capture_output=True, timeout=300)
        if raw.returncode != 0 or not raw.stdout:
            return 60.0, 0.0, 'fallback-60'
        y = np.frombuffer(raw.stdout, dtype=np.float32)
        if y.size < 11025 * 5:
            return 60.0, 0.0, 'fallback-60'
        # Envelope RMS per jendela 20 ms -> onset (naiknya energi).
        win = 220  # 11025 * 0.02
        n = y.size // win
        y = y[:n * win].reshape(n, win)
        env = np.sqrt(np.mean(y * y, axis=1) + 1e-12)
        # Gate rentang dinamis: musik tanpa ketukan yang jelas (nada murni,
        # drone, ambient) punya envelope HAMPIR KONSTAN — autokorelasi
        # ripple kecilnya bisa menipu (sine 440 Hz lolos sebagai 120 BPM
        # di harness v1.3.0). Syarat: puncak envelope minimal 30% di atas
        # median; kalau tidak, fallback 60 BPM (jujur, bukan pura-pura).
        _dyn = (float(np.max(env)) - float(np.median(env))) / (float(np.median(env)) + 1e-9)
        if _dyn < 0.3:
            return 60.0, 0.0, 'fallback-60'
        onset = np.diff(env, prepend=env[0])
        onset = np.clip(onset, 0, None)
        onset = onset - np.mean(onset)
        if np.std(onset) < 1e-9:
            return 60.0, 0.0, 'fallback-60'
        # Autokorelasi pada lag 60-180 BPM (0.333-1.0 dtk = 17-50 jendela).
        ac = np.correlate(onset, onset, mode='full')[len(onset) - 1:]
        lo, hi = 17, 50
        seg = ac[lo:hi + 1]
        if seg.size == 0 or ac[0] <= 0:
            return 60.0, 0.0, 'fallback-60'
        # Confidence = puncak autokorelasi vs energi total (ac[0]).
        # (Bukan vs median segmen — median bisa NEGATIF karena onset sudah
        # dikurangi mean-nya, sehingga confidence jatuh ke 0 walau beat
        # jelas. Bug ini ketahuan di harness v1.3.0.)
        ratio = float(np.max(seg) / ac[0])
        lag = int(np.argmax(seg)) + lo
        bpm = 60.0 / (lag * 0.02)
        if ratio < 0.25:
            return 60.0, 0.0, 'fallback-60'
        conf = max(0.0, min(1.0, (ratio - 0.25) / 0.5))
        return round(float(bpm), 1), round(conf, 2), 'autocorr'
    except Exception:
        return 60.0, 0.0, 'fallback-60'


def zumo_build_audio(track_files, out_path, ffmpeg_path='ffmpeg',
                     crossfade=ZUMO_CROSSFADE_SEC, ffprobe_path='ffprobe'):
    """Gabung lagu: EQ PIANO PER LAGU -> crossfade 1,5 dtk -> limiter ->
    1 file audio. Kembalikan (out_path, offsets, durations) dengan
    offsets = detik mulai tiap lagu di hasil gabungan (untuk timestamp
    deskripsi: offset = durasi kumulatif - crossfade).
    QC: volumedetect — max_volume harus < -0,5 dB; bila tidak, turunkan
    gain global 3 dB dan cek sekali lagi. Audio senyap (max < -60 dB)
    -> raise (QC gagal, jangan upload bisu)."""
    if not track_files:
        raise ValueError('Tidak ada file lagu untuk digabung.')
    durations = []
    for p in track_files:
        d = zumo_media_duration(p, ffprobe_path)
        if d <= 0:
            raise ValueError(f'Durasi tidak terbaca: {p}')
        durations.append(d)
    eq = zumo_piano_eq_filter()
    n = len(track_files)
    inputs = []
    for p in track_files:
        inputs += ['-i', p]
    # Filtergraph: EQ per input -> acrossfade berantai -> limiter.
    filt = []
    for i in range(n):
        filt.append(f'[{i}:a]{eq},aresample=44100,aformat=channel_layouts=stereo[a{i}]')
    cur = 'a0'
    for i in range(1, n):
        filt.append(f'[{cur}][a{i}]acrossfade=d={crossfade}:c1=tri:c2=tri[x{i}]')
        cur = f'x{i}'
    filt.append(f'[{cur}]alimiter=limit=0.9,aresample=44100[aout]')
    cmd = [ffmpeg_path, '-y'] + inputs + ['-filter_complex', ';'.join(filt),
           '-map', '[aout]', '-c:a', 'libmp3lame', '-q:a', '2', out_path]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=3600)
    if r.returncode != 0 or not os.path.exists(out_path):
        tail = (r.stderr or '')[-400:]
        raise RuntimeError(f'Gabung audio Zumo gagal (FFmpeg): {tail}')

    def _max_volume(path):
        rr = subprocess.run(
            [ffmpeg_path, '-v', 'info', '-i', path, '-af', 'volumedetect',
             '-f', 'null', '-'],
            capture_output=True, text=True, timeout=600)
        m = None
        for line in (rr.stderr or '').splitlines():
            if 'max_volume' in line:
                try:
                    m = float(line.split('max_volume:')[1].split('dB')[0].strip())
                except Exception:
                    pass
        return m

    mv = _max_volume(out_path)
    if mv is None:
        raise RuntimeError('QC audio Zumo gagal (volumedetect tidak terbaca).')
    if mv < -60:
        raise RuntimeError(f'QC audio Zumo gagal: hasil gabungan senyap (max {mv} dB).')
    if mv >= -0.5:
        # Turunkan gain global 3 dB lalu cek sekali lagi.
        tmp = out_path + '.quieter.mp3'
        r2 = subprocess.run(
            [ffmpeg_path, '-y', '-i', out_path, '-af', 'volume=-3dB',
             '-c:a', 'libmp3lame', '-q:a', '2', tmp],
            capture_output=True, text=True, timeout=1800)
        if r2.returncode == 0 and os.path.exists(tmp):
            os.replace(tmp, out_path)
            mv = _max_volume(out_path)
            if mv is None or mv >= -0.5:
                raise RuntimeError(f'QC audio Zumo gagal: max volume {mv} dB setelah diturunkan.')
        else:
            raise RuntimeError('QC audio Zumo gagal: tidak bisa menurunkan gain.')
    offsets = []
    acc = 0.0
    for i, d in enumerate(durations):
        offsets.append(round(acc, 2))
        acc += d - (crossfade if i < n - 1 else 0.0)
    return out_path, offsets, durations


def make_particle_loop(kind, out_path, duration=8.0, fps=30,
                       width=1920, height=1080, ffmpeg_path='ffmpeg',
                       seed=7):
    """Render SATU loop partikel (dipakai ulang semua playlist):
    partikel putih/berwarna di atas latar HITAM (untuk screen blend).
    kind 'snow' = salju putih jatuh + goyang; 'sakura' = kelopak pink
    muda jatuh pelan + goyang. Durasi 8 dtk @30fps = 240 frame; partikel
    membungkus (wrap) supaya loop mulus. Kembalikan out_path."""
    from PIL import Image, ImageDraw
    import tempfile
    kind = 'sakura' if kind == 'sakura' else 'snow'
    rng = random.Random(seed + (1 if kind == 'sakura' else 0))
    n_frames = int(round(duration * fps))
    if kind == 'snow':
        n_part = 170
        # Radius 1.5-4.5 (was 2.0-6.0): user 21 Nov 2026 bilang partikel
        # salju "udah lumayan oke, hanya size-nya kurang kecil dikit" —
        # dikecilkan ~25%.
        parts = [{'x': rng.uniform(0, width), 'y': rng.uniform(0, height),
                  'r': rng.uniform(1.5, 4.5), 'vy': rng.uniform(55, 130),
                  'sway': rng.uniform(0.4, 1.4), 'ph': rng.uniform(0, 6.28),
                  'a': rng.randint(150, 255)} for _ in range(n_part)]
    else:
        n_part = 90
        parts = [{'x': rng.uniform(0, width), 'y': rng.uniform(0, height),
                  'r': rng.uniform(5.0, 11.0), 'vy': rng.uniform(28, 60),
                  'sway': rng.uniform(0.8, 2.0), 'ph': rng.uniform(0, 6.28),
                  'a': rng.randint(140, 230)} for _ in range(n_part)]
    tmpdir = tempfile.mkdtemp(prefix='zumo_loop_')
    try:
        for f in range(n_frames):
            t = f / fps
            img = Image.new('RGB', (width, height), (0, 0, 0))
            dr = ImageDraw.Draw(img, 'RGBA')
            for p in parts:
                x = (p['x'] + math.sin(t * p['sway'] + p['ph']) * 36) % width
                y = (p['y'] + p['vy'] * t) % height
                r = p['r']
                if kind == 'snow':
                    col = (255, 255, 255, p['a'])
                    dr.ellipse([x - r, y - r, x + r, y + r], fill=col)
                else:
                    # Kelopak: elips pink dengan aspek berubah (purapura rotasi).
                    wob = 0.6 + 0.4 * math.sin(t * 1.7 + p['ph'])
                    col = (255, 183, 197, p['a'])
                    dr.ellipse([x - r, y - r * wob, x + r, y + r * wob], fill=col)
            img.save(os.path.join(tmpdir, f'f{f:04d}.png'))
        cmd = [ffmpeg_path, '-y', '-framerate', str(fps), '-i',
               os.path.join(tmpdir, 'f%04d.png'),
               '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
               '-pix_fmt', 'yuv420p', '-an', out_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=1800)
        if r.returncode != 0 or not os.path.exists(out_path):
            tail = (r.stderr or '')[-300:]
            raise RuntimeError(f'Render loop partikel {kind} gagal: {tail}')
    finally:
        import shutil
        shutil.rmtree(tmpdir, ignore_errors=True)
    return out_path


def zumo_bpm_hint_from_style(style):
    """Ambil BPM dari teks style ('... 82 BPM') -> int 40-240 atau None.

    BPM di style adalah HINT generate + seed denyut partikel bila deteksi
    tempo audio lemah (user 20:58 WIB). Deteksi tempo audio tetap utama;
    hint dipakai HANYA bila zumo_detect_bpm mengembalikan fallback-60.
    """
    m = re.search(r'(\d{2,3})\s*BPM', style or '', re.I)
    if not m:
        return None
    bpm = int(m.group(1))
    return bpm if 40 <= bpm <= 240 else None


# CONTOH FORMAT penulisan style (referensi BENTUK saja — BUKAN preset
# resmi; user 21:00 WIB menegaskan ketiga contoh di bawah ini HANYA
# contoh cara menulis prompt genre: daftar instrumen + mood + BPM di
# akhir. Style bebas per order; user yang tentukan tiap order. Jangan
# jadikan ketiganya daftar preset tetap / hardcode pilihan.
ZUMO_STYLE_FORMAT_EXAMPLES = [
    ('Indonesian Roots Reggae, Tropical Archipelago Groove, Acoustic & '
     'Clean Electric Skank Guitar, Traditional Kendang & Ketipung '
     'Percussion, Bamboo Suling Flute, Warm Dub Bassline, Melodica, '
     'Laid-Back Soulful Indonesian Male Vocals, Warung Kopi Beach Vibe, '
     '82 BPM'),
    ('Indonesian Acoustic Roots Reggae, Islamic Nusantara Fusion, Warm '
     'Nylon Guitar Skank, Rebana & Marawis Percussion, Oud Melody, Deep '
     'Warm Dub Bassline, Bamboo Suling, Soulful Humble Indonesian Male '
     'Vocals, Peaceful Ramadan Atmosphere, 80 BPM'),
    ('Indonesian Pop Religi, Warm Acoustic Ballad, Fingerpicked Acoustic '
     'Guitar, Emotional Grand Piano, Lush Cello & String Section, Subtle '
     'Oud & Darbuka Percussion, Soulful Heartfelt Indonesian Male Vocals, '
     'Peaceful Spiritual Ramadan Vibe, 76 BPM'),
]


# --- BANK AUDIO (user 20:58 WIB) ------------------------------------
# Semua lagu hasil generate yang BERHASIL disimpan di bank per channel
# (1 folder per nama channel). File disimpan MENTAH hasil download;
# EQ PIANO diterapkan saat perakitan (zumo_build_audio), supaya satu
# file bank bisa dipakai ulang. Metadata di bank_index.json per folder:
# {channel, tracks: [{id, filename, title, style, bpm, duration,
# created_at, credits, job_id, instrumental, model}]}.
# Menu browse/kelola bank adalah FASE 2 — struktur ini sudah mendukungnya
# (list/add/pick + hapus per id bisa ditambah tanpa ubah format).

def zumo_bank_dirname(channel_name):
    """Nama folder bank dari nama channel: lowercase, non-alnum -> '-'.
    'Cozy Sax Relaxing' -> 'cozy-sax-relaxing'."""
    s = re.sub(r'[^a-z0-9]+', '-', (channel_name or '').strip().lower())
    return s.strip('-') or 'channel'


def zumo_bank_index_path(bank_root, channel_name):
    return os.path.join(bank_root, zumo_bank_dirname(channel_name),
                        'bank_index.json')


def zumo_bank_list(bank_root, channel_name):
    """Daftar entri bank (metadata) untuk satu channel — [] bila belum ada
    atau indeks rusak. Entri yang filenya hilang ikut dilewati."""
    path = zumo_bank_index_path(bank_root, channel_name)
    try:
        with open(path, 'r', encoding='utf-8') as fh:
            idx = json.load(fh)
        tracks = idx.get('tracks') or []
    except Exception:
        return []
    folder = os.path.dirname(path)
    out = []
    for t in tracks:
        if isinstance(t, dict) and t.get('filename') and \
                os.path.exists(os.path.join(folder, t['filename'])):
            out.append(t)
    return out


def zumo_bank_add(bank_root, channel_name, src_path, meta):
    """Simpan SATU lagu mentah ke bank + catat metadata. `meta` berisi
    title, style, bpm, duration, credits, job_id, instrumental, model.
    File DISALIN (bukan dipindah) supaya job_dir masih bisa dibersihkan
    sesudahnya. Kembalikan entri yang tersimpan, atau None bila gagal
    (kegagalan bank TIDAK BOLEH menggagalkan job upload — pemanggil
    mencatatnya dan lanjut)."""
    try:
        folder = os.path.dirname(zumo_bank_index_path(bank_root, channel_name))
        os.makedirs(folder, exist_ok=True)
        base = os.path.basename(src_path or '')
        if not base or not os.path.exists(src_path):
            return None
        stamp = time.strftime('%Y%m%d%H%M%S')
        safe = re.sub(r'[^A-Za-z0-9_.\-]+', '_', base)
        filename = f'{stamp}_{safe}'
        dest = os.path.join(folder, filename)
        shutil.copyfile(src_path, dest)
        entry = {
            'id': f'{stamp}-{os.urandom(3).hex()}',
            'filename': filename,
            'title': (meta.get('title') or 'Tanpa Judul')[:120],
            'style': (meta.get('style') or '')[:1000],
            'bpm': meta.get('bpm'),
            'duration': meta.get('duration'),
            'created_at': time.strftime('%Y-%m-%d %H:%M:%S'),
            'credits': meta.get('credits'),
            'job_id': meta.get('job_id'),
            'instrumental': bool(meta.get('instrumental')),
            'model': meta.get('model') or '',
        }
        tracks = zumo_bank_list(bank_root, channel_name)
        tracks.append(entry)
        with open(zumo_bank_index_path(bank_root, channel_name), 'w',
                  encoding='utf-8') as fh:
            json.dump({'channel': channel_name, 'tracks': tracks}, fh,
                      ensure_ascii=False, indent=1)
        return entry
    except Exception:
        return None


def zumo_bank_remove(bank_root, channel_name, track_id):
    """Hapus 1 lagu dari bank (file + entri indeks) berdasarkan id entri.
    Kembalikan (True, '') bila terhapus, (False, pesan) bila tidak ketemu
    atau gagal. Dipakai halaman Bank Lagu (v1.4.2)."""
    try:
        path = zumo_bank_index_path(bank_root, channel_name)
        with open(path, 'r', encoding='utf-8') as fh:
            idx = json.load(fh)
        tracks = idx.get('tracks') or []
        keep = []
        removed = None
        for t in tracks:
            if isinstance(t, dict) and str(t.get('id')) == str(track_id):
                removed = t
            else:
                keep.append(t)
        if removed is None:
            return False, 'Lagu tidak ditemukan di bank.'
        folder = os.path.dirname(path)
        fn = removed.get('filename') or ''
        if fn:
            try:
                os.remove(os.path.join(folder, fn))
            except FileNotFoundError:
                pass
        tmp = path + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump({'channel': idx.get('channel', channel_name),
                       'tracks': keep}, fh, ensure_ascii=False, indent=1)
        os.replace(tmp, path)
        return True, ''
    except Exception as e:
        return False, str(e)


def zumo_bank_pick(bank_root, channel_name, n, rng=None):
    """Pilih acak n entri dari bank (tanpa judul sama berdampingan bila
    memungkinkan — reuse shuffle_zumo_tracks). n dibatasi isi bank.
    Kembalikan list entri (boleh < n bila bank lebih kecil)."""
    tracks = zumo_bank_list(bank_root, channel_name)
    if not tracks:
        return []
    keep = max(1, min(int(n or 1), len(tracks)))
    # Entri bank sudah dict dengan kunci 'title' — langsung masuk shuffle.
    return list(shuffle_zumo_tracks(tracks, keep=keep, rng=rng))
