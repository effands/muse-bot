import os
import re
import sys
import math
import time
import json
import socket
import shutil
import psutil
import requests
import threading
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
# ---- Log file aktivitas (v1.3.11): salin semua print ke logs/operator.log ----
# Sama seperti panel (lihat _LogTee di app.py): operator selama ini hanya
# menulis ke stdout (journal systemd). Tee ini menulis ke stream asli
# SEKALIGUS ke file log, supaya panel bisa menampilkan log operator juga.
_LOG_LOCK = threading.RLock()
_LOG_PATH = os.path.join(BASE_DIR, 'logs', 'operator.log')
_LOG_MAX_BYTES = 5 * 1024 * 1024


class _LogTee:
    """Pembungkus sys.stdout/stderr: teruskan ke stream asli + salin ke file."""
    def __init__(self, stream, path):
        self._stream = stream
        self._path = path
        self._fh = None
        self._bytes = 0
    def _open(self):
        try:
            os.makedirs(os.path.dirname(self._path), exist_ok=True)
            self._fh = open(self._path, 'a', encoding='utf-8', errors='replace')
            self._bytes = os.path.getsize(self._path)
        except Exception:
            self._fh = None
    def write(self, s):
        try:
            self._stream.write(s)
        except Exception:
            pass
        if not s:
            return 0
        try:
            with _LOG_LOCK:
                if self._fh is None:
                    self._open()
                if self._fh is not None:
                    try:
                        cur = os.path.getsize(self._path)
                    except Exception:
                        cur = self._bytes
                    if cur < self._bytes or cur > _LOG_MAX_BYTES:
                        try:
                            self._fh.close()
                        except Exception:
                            pass
                        if cur > _LOG_MAX_BYTES:
                            try:
                                os.replace(self._path, self._path + '.1')
                            except Exception:
                                pass
                        self._fh = None
                        self._open()
                    if self._fh is not None:
                        self._fh.write(s)
                        self._fh.flush()
                        self._bytes += len(s.encode('utf-8', 'replace'))
        except Exception:
            pass
        return len(s)
    def flush(self):
        try:
            self._stream.flush()
        except Exception:
            pass
        try:
            with _LOG_LOCK:
                if self._fh is not None:
                    self._fh.flush()
        except Exception:
            pass
    def isatty(self):
        try:
            return self._stream.isatty()
        except Exception:
            return False
    def fileno(self):
        return self._stream.fileno()


if not isinstance(sys.stdout, _LogTee):
    sys.stdout = _LogTee(sys.stdout, _LOG_PATH)
if not isinstance(sys.stderr, _LogTee):
    sys.stderr = _LogTee(sys.stderr, _LOG_PATH)


CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
CHANNELS_FILE = os.path.join(BASE_DIR, 'channels_db.json')
TASKS_FILE = os.path.join(BASE_DIR, 'tasks_db.json')
# 🔥 v1.2.25: registry multi-client OAuth (metadata saja — dibaca untuk
# menampilkan project cadangan di /kuota; secret tidak pernah dibaca di sini).
OAUTH_CLIENTS_FILE = os.path.join(BASE_DIR, 'oauth_clients.json')
PLAYLIST_JOBS_FILE = os.path.join(BASE_DIR, 'playlist_jobs.json')


def _load_playlist_jobs():
    """Daftar job Musik Playlist (baca file panel; gagal baca → [])."""
    if not os.path.exists(PLAYLIST_JOBS_FILE):
        return []
    try:
        with open(PLAYLIST_JOBS_FILE, 'r', encoding='utf-8') as f:
            data = json.load(f)
        if isinstance(data, dict) and isinstance(data.get('jobs'), list):
            return data['jobs']
    except Exception:
        pass
    return []
# 🔥 FASE 1 MONITORING: state dedupe (laporan harian & alert) — file runtime,
# JANGAN di-commit (sudah masuk .gitignore). Isinya hanya tanggal + kunci alert.
MONITOR_STATE_FILE = os.path.join(BASE_DIR, 'monitor_state.json')
# 🔥 v1.2.17 JEMBATAN TELEGRAM ↔ MUSE: kotak masuk chat bebas yang tidak
# dikenali NLU (file runtime di VPS — JANGAN di-commit, lihat .gitignore).
# Operator menulis record ke inbox; cron Muse membacanya lewat SSH dan
# membalas langsung via Bot API. Token bot TIDAK PERNAH ditulis ke file ini.
BRIDGE_INBOX_FILE = os.path.join(BASE_DIR, 'telegram_inbox.jsonl')
BRIDGE_STATE_FILE = os.path.join(BASE_DIR, 'telegram_inbox_state.json')
BRIDGE_STALE_SECONDS = 600  # 10 menit — record 'new' lebih tua dari ini dianggap basi
_BRIDGE_FORWARDED_CAP = 200  # dedupe: simpan 200 message_id terakhir
# Batas upload harian per channel — cerminan MAX_UPLOADS_PER_CHANNEL_PER_DAY di app.py.
MAX_UPLOADS_PER_DAY = 100  # 🔥 v1.2.20: batas upload per project Google — 100/hari (kuota "Video Uploads per day" di Cloud Console, dicek live 2026-11-19). Dulu 6 (asumsi lama 10.000 ÷ 1.600).
DAILY_REPORT_HOUR_WIB = 7  # laporan harian otomatis tiap pagi 07:00 WIB
EVENING_REPORT_HOUR_WIB = 17  # laporan sore otomatis tiap 17:00 WIB
# TTL cache statistik channel (cerminan VIDEO_STATS_CACHE_TTL di app.py):
# cache panel & cache operator sendiri dianggap segar selama 1 jam.
CHANNEL_STATS_CACHE_TTL = 3600
PANEL_URL = "http://127.0.0.1:8000"

# 🔥 SINGLE INSTANCE LOCK (Port 8089)
lock_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
try:
    lock_socket.bind(('127.0.0.1', 8089))
except socket.error:
    print("⚠️ [MUSE OPERATOR] Bot sudah aktif di proses lain.", flush=True)
    sys.exit(0)

def load_config():
    if not os.path.exists(CONFIG_FILE):
        return {}
    with open(CONFIG_FILE, 'r') as f:
        return json.load(f)

def load_channels():
    if not os.path.exists(CHANNELS_FILE):
        return []
    try:
        with open(CHANNELS_FILE, 'r') as f:
            return json.load(f)
    except Exception:
        return []

def load_tasks():
    if not os.path.exists(TASKS_FILE):
        return {"active": [], "history": []}
    # 🔥 v1.3.6: baca gagal (file sedang ditulis / JSON rusak sesaat) JANGAN
    # langsung dianggap kosong — coba lagi 3x. Pernah: /jadwal jawab 0 task
    # padahal 8 task aktif ada di file.
    for _attempt in range(3):
        try:
            with open(TASKS_FILE, 'r') as f:
                data = json.load(f)
                if isinstance(data, list):
                    return {"active": data, "history": []}
                return data
        except Exception:
            time.sleep(0.2)
    return {"active": [], "history": []}

# ==========================================
# 🔥 FASE 1 MONITORING — STATE DEDUPE & HELPER DATA
# ==========================================
def load_monitor_state():
    """Muat state dedupe laporan/alert. Hilang/rusak = mulai dari kosong."""
    if not os.path.exists(MONITOR_STATE_FILE):
        return {"daily_report_date": "", "alerts": {}}
    try:
        with open(MONITOR_STATE_FILE, 'r') as f:
            data = json.load(f)
        if not isinstance(data, dict):
            return {"daily_report_date": "", "alerts": {}}
        data.setdefault("daily_report_date", "")
        data.setdefault("evening_report_date", "")
        data.setdefault("alerts", {})
        data.setdefault("make_drafts", {})
        data.setdefault("channel_report_snapshot", {})
        data.setdefault("channel_stats_cache", {})
        data.setdefault("approval_reminders", {})
        data.setdefault("bridge_forwarded_ids", [])
        return data
    except Exception:
        return {"daily_report_date": "", "evening_report_date": "", "alerts": {},
                "make_drafts": {}, "channel_report_snapshot": {}, "channel_stats_cache": {},
                "approval_reminders": {}, "bridge_forwarded_ids": {}}

def save_monitor_state(state):
    """Simpan state secara atomik (temp + replace) agar tidak rusak saat restart."""
    try:
        tmp = MONITOR_STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, MONITOR_STATE_FILE)
    except Exception as e:
        print(f"Gagal simpan monitor_state: {e}", flush=True)

# ==========================================
# 🌉 JEMBATAN TELEGRAM ↔ MUSE (v1.2.17)
# Chat bebas yang tidak dikenali NLU diteruskan ke kotak masuk di VPS;
# cron Muse mengambilnya lewat SSH, menafsirkan, dan membalas via Bot API.
# Token bot TIDAK PERNAH ditulis ke inbox/state/log/chat.
# ==========================================
_LAST_BOT_MESSAGE = {}  # chat_id (str) -> teks pesan bot terakhir (konteks "yang tadi")


def _bridge_forward(token, chat_id, message_id, text, source='nlu_fallback', context_extra=None):
    """Teruskan pesan ke kotak masuk Muse (telegram_inbox.jsonl).
    Tulis 1 baris JSONL (status 'new') + catat message_id di monitor_state
    (dedupe, cap 200). Kembalikan True bila baru diteruskan, False bila
    message_id sudah pernah diteruskan / gagal tulis. source default
    'nlu_fallback'; pemanggil eskalasi (v1.3.5) bisa mengisi source lain
    (mis. 'playlist_no_candidate') + context_extra (dict) supaya cron
    bridge tahu konteks kegagalannya. message_id boleh None (tanpa
    dedupe) untuk eskalasi yang dibuat operator sendiri — pemanggil
    WAJIB memastikan hanya forward SEKALI per order gagal."""
    try:
        state = load_monitor_state()
        forwarded = state.get('bridge_forwarded_ids') or []
        if message_id is not None and message_id in forwarded:
            return False
        now = time.time()
        draft = _load_make_draft(chat_id)
        pending = _load_nlu_pending(chat_id)
        rec = {
            "message_id": message_id,
            "chat_id": str(chat_id),
            "from_id": str(chat_id),
            "text": text,
            "unix_time": int(now),
            "timestamp_wib": _wib_now().strftime('%Y-%m-%d %H:%M:%S'),
            "status": "new",
            "source": source,
            "context": {
                "draft_step": (draft or {}).get('step') if isinstance(draft, dict) else None,
                "pending_nlu": (pending or {}).get('intent') if isinstance(pending, dict) else None,
                "last_bot_message": _LAST_BOT_MESSAGE.get(str(chat_id), '')[:200],
                **((context_extra or {}) if isinstance(context_extra, dict) else {}),
            },
            "attempts": 0,
            "muse_reply_message_id": None,
            "replied_at_wib": None,
        }
        is_new = not os.path.exists(BRIDGE_INBOX_FILE)
        with open(BRIDGE_INBOX_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        if is_new:
            try:
                os.chmod(BRIDGE_INBOX_FILE, 0o600)
            except Exception:
                pass
        if message_id is not None:
            forwarded.append(message_id)
            state['bridge_forwarded_ids'] = forwarded[-_BRIDGE_FORWARDED_CAP:]
            save_monitor_state(state)
        return True
    except Exception as e:
        print(f"Gagal bridge_forward: {e}", flush=True)
        return False


def _bridge_stale_check(token, chat_id):
    """Fallback jujur (v1.2.17): record inbox berstatus 'new' yang sudah
    >10 menit belum dibalas Muse → kirim SATU pesan + tandai 'failed' di
    state file (maks 5 per tick). Record yang sudah diklaim ('processing')
    adalah tanggung jawab Muse — tidak disentuh. Dipanggil dari watchdog
    (tiap 5 menit)."""
    try:
        if not os.path.exists(BRIDGE_INBOX_FILE):
            return
        state = {}
        if os.path.exists(BRIDGE_STATE_FILE):
            try:
                with open(BRIDGE_STATE_FILE, 'r', encoding='utf-8') as f:
                    state = json.load(f) or {}
            except Exception:
                state = {}
        now = time.time()
        stale = []
        with open(BRIDGE_INBOX_FILE, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except Exception:
                    continue
                if rec.get('status') != 'new':
                    continue
                st = state.get(str(rec.get('message_id'))) or {}
                if st.get('status') in ('failed', 'replied', 'processing', 'skipped'):
                    continue
                if now - float(rec.get('unix_time') or 0) > BRIDGE_STALE_SECONDS:
                    stale.append(rec)
        if not stale:
            return
        changed = False
        for rec in stale[:5]:
            mid = str(rec.get('message_id'))
            res = send_message(
                token, chat_id,
                "Bos, Muse belum sempat membalas chat tadi 😅 — pesannya sudah saya teruskan, "
                "coba kirim lagi ya, atau ketik /start buat perintah cepat.")
            if res and res.get('ok'):
                state[mid] = {"status": "failed", "reason": "stale_10min",
                              "replied_at_wib": _wib_now().strftime('%Y-%m-%d %H:%M:%S')}
                changed = True
        if changed:
            tmp = BRIDGE_STATE_FILE + '.tmp'
            with open(tmp, 'w', encoding='utf-8') as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, BRIDGE_STATE_FILE)
            try:
                os.chmod(BRIDGE_STATE_FILE, 0o600)
            except Exception:
                pass
    except Exception as e:
        print(f"Bridge stale-check exception: {e}", flush=True)

# ==========================================
# 🎬 BUAT VIDEO LEWAT TELEGRAM — DRAFT PERCAKAPAN
# ==========================================
# Draft alur /buatshort & /buatlong disimpan di monitor_state.json
# (kunci "make_drafts", per chat_id) supaya tahan restart. Draft yang
# tidak ada kabar > 30 menit dianggap hangus.
_MAKE_DRAFT_TTL = 1800  # 30 menit (detik)
_BG_EXTS = ('.mp4', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.mov', '.webm')
_AUDIO_EXTS = ('.mp3', '.wav')

def _load_make_draft(chat_id):
    """Ambil draft aktif chat ini. Kembalikan None (tidak ada), dict draft,
    atau {"expired": True} bila draft sudah hangus (sekaligus dihapus)."""
    state = load_monitor_state()
    drafts = state.get("make_drafts") or {}
    d = drafts.get(str(chat_id))
    if not d:
        return None
    if time.time() - float(d.get('at', 0)) > _MAKE_DRAFT_TTL:
        drafts.pop(str(chat_id), None)
        state["make_drafts"] = drafts
        save_monitor_state(state)
        return {"expired": True}
    return d

def _save_make_draft(chat_id, draft):
    state = load_monitor_state()
    drafts = state.get("make_drafts") or {}
    draft['at'] = time.time()
    drafts[str(chat_id)] = draft
    state["make_drafts"] = drafts
    save_monitor_state(state)

def _clear_make_draft(chat_id):
    state = load_monitor_state()
    drafts = state.get("make_drafts") or {}
    if str(chat_id) in drafts:
        drafts.pop(str(chat_id))
        state["make_drafts"] = drafts
        save_monitor_state(state)

def _gallery_files(yt_id, sub, exts):
    """Daftar NAMA FILE galeri channel (basename saja, urut abjad).
    yt_id divalidasi ketat (allowlist) supaya tidak bisa keluar folder."""
    if not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', yt_id or ''):
        return []
    folder = os.path.join(BASE_DIR, 'uploads', yt_id, sub)
    try:
        if not os.path.isdir(folder):
            return []
        return sorted(f for f in os.listdir(folder)
                      if f.lower().endswith(exts))
    except Exception:
        return []

def _friendly_gallery_label(fname, kind, num=None):
    """Label ramah untuk daftar galeri (v1.2.17): 'Video anak 1',
    'Gambar gradien anak', 'Lagu tidur nyenyak' — tanpa ekstensi, tanpa
    jargon. Angka urut TAMPILAN tetap kunci balasan (logika parsing tidak
    berubah); num = nomor urut di antara file sejenis (khusus video)."""
    stem = os.path.splitext(os.path.basename(fname or ''))[0]
    words = stem.replace('_', ' ').replace('-', ' ').split()
    if kind == 'image':
        if stem.lower().startswith('auto-gradient-'):
            theme = ' '.join(words[2:]) or 'gradien'
            return f"🖼️ Gambar gradien {theme}"
        return "🖼️ Gambar " + (' '.join(words) or 'tanpa nama')
    if kind == 'video':
        theme_words = [w for w in words
                       if w.lower() != 'clip' and not w.isdigit()]
        theme = ' '.join(theme_words)
        label = "🎬 Video" + (f" {theme}" if theme else "")
        if num is not None:
            label += f" {num}"
        return label
    return "🎵 " + (' '.join(words) or 'tanpa nama')


def _next_slot_for_channel(ch):
    """Slot jadwal berikutnya untuk channel: sehari setelah jadwal terakhir
    (jam yang sama); bila belum ada jadwal, besok jam 19:00 WIB."""
    last = (ch or {}).get('last_scheduled_date') or ''
    try:
        dt = datetime.strptime(last, '%Y-%m-%d %H:%M').replace(
            tzinfo=ZoneInfo("Asia/Jakarta"))
        return (dt + timedelta(days=1)).strftime('%Y-%m-%d %H:%M')
    except Exception:
        return (_wib_now() + timedelta(days=1)).strftime('%Y-%m-%d 19:00')

def _parse_make_date(raw):
    """Parse tanggal kiriman user: 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DDTHH:MM'
    / 'YYYY-MM-DD' (→ jam 19:00). Kembalikan string '%Y-%m-%d %H:%M'
    atau None bila tidak bisa diparse."""
    raw = (raw or '').strip()
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
        try:
            dt = datetime.strptime(raw, fmt)
            if len(raw) == 10:
                dt = dt.replace(hour=19, minute=0)
            return dt.strftime('%Y-%m-%d %H:%M')
        except Exception:
            continue
    return None

def _wib_now():
    return datetime.now(ZoneInfo("Asia/Jakarta"))

# Nama bulan Indonesia (lengkap + singkatan) untuk parse tanggal alami.
_ID_MONTH_NAMES = {
    'januari': 1, 'jan': 1, 'februari': 2, 'feb': 2, 'maret': 3, 'mar': 3,
    'april': 4, 'apr': 4, 'mei': 5, 'juni': 6, 'jun': 6, 'juli': 7, 'jul': 7,
    'agustus': 8, 'agu': 8, 'september': 9, 'sep': 9, 'oktober': 10, 'okt': 10,
    'november': 11, 'nov': 11, 'desember': 12, 'des': 12,
}
_ID_MONTH_DISPLAY = ['', 'Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
                     'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']


def _parse_natural_datetime(text, now=None):
    """Bahasa Indonesia sehari-hari → ('YYYY-MM-DD HH:MM', catatan) atau
    (None, None) bila tidak dikenali. Paham: 'hari ini jam 9' → hari ini
    09:00; 'jam 9' → hari ini 09:00 bila masih di depan, besok 09:00 bila
    sudah lewat; 'jam 9 malam' → 21:00; 'jam 7 sore' → 19:00; 'jam 12
    siang' → 12:00; 'jam 12 malam' → 00:00 (hari berikut); 'jam 9.30' /
    'jam 9:30' / 'jam 9 lewat 30' → 09:30; 'besok jam 9' / 'lusa jam 9';
    'tanggal 20 jam 19' / '20 Nov jam 19:00' / '20 November jam 7 malam';
    format kaku 'YYYY-MM-DD HH:MM' tetap jalan (via _parse_make_date).
    'jam 9' tanpa keterangan = pagi (09:00) — catatan selalu menyebut
    tafsiran ini supaya Bos bisa mengoreksi di ringkasan konfirmasi.
    Jam yang sudah lewat hari ini digeser ke besok (catatan menyebutnya);
    tanggal eksplisit yang sudah lewat digeser ke bulan/tahun berikut.
    Nama hari (Senin, Selasa, ...) BELUM didukung — bila disebut,
    kembalikan (None, None) supaya pemanggil bertanya ulang (jangan
    tebak jadi hari ini)."""
    now = now or _wib_now()
    raw = (text or '').strip()
    if not raw:
        return None, None
    # 1. Format kaku dulu (tetap jalan, tanpa catatan tafsiran).
    strict = _parse_make_date(raw)
    if strict:
        return strict, None
    low = raw.lower()
    # Nama hari (Senin, Selasa, ...) BELUM didukung — jangan pura-pura
    # paham: kembalikan None supaya pemanggil bertanya ulang.
    if re.search(r'\b(senin|selasa|rabu|kamis|jumat|jum\'at|sabtu|minggu|ahad)\b', low):
        return None, None
    # 2. Tanggal: eksplisit ("tanggal 20", "20 November") atau relatif
    # ("hari ini" / "besok" / "lusa" — yang disebut paling awal menang).
    exp_day = exp_month = None
    m = re.search(r'(?:tanggal|tgl)\s+(\d{1,2})(?:\s+([a-z]+))?', low)
    if m:
        exp_day = int(m.group(1))
        if m.group(2) and m.group(2) in _ID_MONTH_NAMES:
            exp_month = _ID_MONTH_NAMES[m.group(2)]
    else:
        m = re.search(
            r'\b(\d{1,2})\s+(januari|februari|maret|april|mei|juni|juli|agustus|'
            r'september|oktober|november|desember|jan|feb|mar|apr|jun|jul|agu|'
            r'sep|okt|nov|des)\b', low)
        if m:
            exp_day = int(m.group(1))
            exp_month = _ID_MONTH_NAMES[m.group(2)]
    offset = 0
    day_word = None
    if exp_day is None:
        hits = []
        for w, off in (('hari ini', 0), ('besok', 1), ('lusa', 2)):
            idx = low.find(w)
            if idx >= 0:
                hits.append((idx, w, off))
        if hits:
            hits.sort()
            _, day_word, offset = hits[0]
    # 3. Jam: "jam 9", "jam 9.30", "jam 9:30", "jam 9 lewat 30",
    # "jam 7 malam", "pukul 9".
    hour = minute = None
    mer = ''
    lewat = False
    minute_given = False
    orig_hour = None
    m = re.search(r'(?:jam|pukul)\s+(\d{1,2})\s+lewat\s+(\d{1,2})\b', low)
    if m:
        orig_hour = int(m.group(1))
        hour, minute, lewat = orig_hour, int(m.group(2)), True
    else:
        m = re.search(
            r'(?:jam|pukul)\s+(\d{1,2})(?:[:.](\d{2}))?'
            r'(?:\s*(pagi|siang|sore|malam|petang))?', low)
        if m:
            orig_hour = int(m.group(1))
            hour = orig_hour
            minute_given = m.group(2) is not None
            minute = int(m.group(2) or 0)
            mer = m.group(3) or ''
    if hour is None:
        return None, None
    if hour > 23 or minute > 59:
        return None, None
    if not mer:
        # 🔥 v1.3.30: penanda waktu bisa berdiri SEBELUM jamnya
        # ("nanti sore ... jam 4") — tapi HANYA sesudah kata 'nanti'.
        # v1.3.33: scan seluruh teks salah membaca tema sebagai jam
        # ("lagu sore jam 9" jadi 21:00). Tanpa 'nanti', jam polos
        # berarti pagi (09:00) sesuai docstring; Bos bisa mengoreksi
        # di ringkasan konfirmasi bila tafsirannya salah.
        _mm = re.search(r'\bnanti\s+(pagi|siang|sore|petang|malam)\b', low)
        if _mm:
            mer = _mm.group(1)
    # Konversi meridiem. 'jam 12 malam' → 00:00; 'jam 12 siang' → 12:00.
    if mer == 'pagi':
        if hour == 12:
            hour = 0
    elif mer == 'siang':
        if hour < 12:
            hour += 12
    elif mer in ('sore', 'petang'):
        if hour < 12:
            hour += 12
    elif mer == 'malam':
        if hour == 12:
            hour = 0
        elif hour < 12:
            hour += 12
    # 4. Tanggal final + geser bila sudah lewat.
    bumped = ''
    if exp_day is not None:
        month = exp_month if exp_month is not None else now.month
        try:
            dt = now.replace(year=now.year, month=month, day=exp_day, hour=hour,
                             minute=minute, second=0, microsecond=0)
        except ValueError:
            return None, None
        if dt <= now:
            if exp_month is not None:
                try:
                    dt = dt.replace(year=dt.year + 1)
                except ValueError:
                    return None, None
                bumped = ' — sudah lewat, saya pakai tahun depan'
            else:
                nm = dt.month + 1
                ny = dt.year + (1 if nm > 12 else 0)
                nm = nm if nm <= 12 else 1
                try:
                    dt = dt.replace(year=ny, month=nm)
                except ValueError:
                    return None, None
                bumped = ' — sudah lewat, saya pakai bulan depan'
    else:
        dt = (now + timedelta(days=offset)).replace(
            hour=hour, minute=minute, second=0, microsecond=0)
        if dt <= now:
            dt = dt + timedelta(days=1)
            bumped = ' — hari ini sudah lewat, jadi besok'
    # 5. Catatan tafsiran untuk ringkasan konfirmasi.
    if exp_day is not None:
        day_txt = f"{dt.day} {_ID_MONTH_DISPLAY[dt.month]}"
        if dt.year != now.year:
            day_txt += f" {dt.year}"
    elif offset == 2:
        day_txt = 'lusa'
    elif offset == 1:
        day_txt = 'besok'
    else:
        day_txt = 'hari ini'
    if lewat:
        time_txt = f"jam {orig_hour} lewat {minute}"
    elif minute_given:
        time_txt = f"jam {orig_hour}:{minute:02d}" + (f" {mer}" if mer else '')
    else:
        time_txt = f"jam {orig_hour}" + (f" {mer}" if mer else '')
    note = f"{day_txt} {time_txt}"
    if not mer and not lewat and hour < 12:
        note += f" — saya artikan jam {orig_hour} pagi"
    note += bumped
    return dt.strftime('%Y-%m-%d %H:%M'), note

def _wib_today_str():
    return _wib_now().strftime("%Y-%m-%d")

def _wib_yesterday_str():
    return (_wib_now() - timedelta(days=1)).strftime("%Y-%m-%d")

def _alerted_today(state, key):
    """True kalau alert `key` sudah dikirim hari ini (WIB)."""
    return state.get("alerts", {}).get(key) == _wib_today_str()

def _mark_alerted(state, key):
    state.setdefault("alerts", {})[key] = _wib_today_str()
    save_monitor_state(state)

def _channel_display_name(ch):
    return ch.get('name') or ch.get('channel_name') or 'Tanpa Nama'

def _channel_connected(ch):
    return bool(ch.get('creds_list') or ch.get('creds_json')
                or ch.get('access_token') or ch.get('refresh_token'))

def get_channel_quota(ch):
    """(terpakai_hari_ini, sisa) upload hari ini (WIB) untuk satu channel.
    Counter daily_uploads ditulis app.py setiap upload sukses."""
    today = _wib_today_str()
    du = ch.get('daily_uploads') or {}
    used = int(du.get('count', 0)) if du.get('date') == today else 0
    return used, max(0, MAX_UPLOADS_PER_DAY - used)


def _oauth_registry_clients():
    """Daftar client OAuth dari registry (metadata saja)."""
    try:
        if not os.path.exists(OAUTH_CLIENTS_FILE):
            return []
        with open(OAUTH_CLIENTS_FILE, 'r') as f:
            store = json.load(f) or {}
        clients = store.get('clients') or []
        return [c for c in clients if isinstance(c, dict)]
    except Exception:
        return []


def _project_uploads_today_op(channels, project_id):
    """Total upload hari ini (WIB) di satu project — cerminan
    _project_uploads_today() di app.py: pakai daily_uploads_by_project
    (project yang benar-benar dipakai, termasuk setelah auto-switch);
    channel lama tanpa catatan per-project dihitung dari counter legacy
    dikurangi upload di project lain hari ini."""
    if not project_id:
        return 0
    today = _wib_today_str()
    total = 0
    for c in channels or []:
        byp = c.get('daily_uploads_by_project') or {}
        ent = byp.get(str(project_id))
        if ent and ent.get('date') == today:
            total += int(ent.get('count', 0))
        elif _project_id_of_channel(c) == project_id:
            du = c.get('daily_uploads') or {}
            if du.get('date') == today:
                other = sum(int(v.get('count', 0)) for k, v in byp.items()
                            if k != str(project_id) and v.get('date') == today)
                total += max(0, int(du.get('count', 0)) - other)
    return total

def get_stock_days(ch):
    """Sisa stok jadwal channel dalam hari (bisa negatif = sudah habis).
    None kalau channel belum punya jadwal sama sekali."""
    last_date_str = ch.get('last_scheduled_date')
    if not last_date_str:
        return None
    try:
        last_date = datetime.strptime(last_date_str, '%Y-%m-%d %H:%M')
        # last_scheduled_date disimpan app.py sebagai waktu lokal WIB tanpa
        # info zona — tempelkan Asia/Jakarta supaya bisa dikurangi dengan
        # _wib_now() yang aware (tanpa ini selalu TypeError → None).
        last_date = last_date.replace(tzinfo=ZoneInfo("Asia/Jakarta"))
        return (last_date - _wib_now()).total_seconds() / 86400.0
    except Exception:
        return None

def _count_history_for_date(history, date_str):
    """Hitung (sukses_tayang, gagal) di riwayat untuk satu tanggal WIB.
    Mengandalkan field finished_date yang ditulis app.py saat move_to_history
    (task yang selesai sebelum deploy Fase 1 tidak punya field ini → tidak
    terhitung, dan itu disengaja supaya tidak salah lapor)."""
    ok = fail = 0
    ok_titles, fail_titles = [], []
    for t in history:
        if t.get('finished_date') != date_str:
            continue
        st = t.get('status', '')
        if 'Tayang' in st:
            ok += 1
            ok_titles.append(t.get('title', '?')[:45])
        elif 'Gagal' in st:
            fail += 1
            fail_titles.append(t.get('title', '?')[:45])
    return ok, fail, ok_titles, fail_titles

def _build_today_summary_text(channels, history):
    """🔥 v1.3.29: teks ringkasan upload HARI INI per channel — sama
    seperti kartu HARI INI di Task Monitor. Hitung dengan aturan yang
    sama seperti /status: finished_date == tanggal WIB hari ini,
    status mengandung 'Tayang' (sukses) / 'Gagal'."""
    today = _wib_today_str()
    ok, fail, _, _ = _count_history_for_date(history, today)
    per = {}
    for ch in channels:
        per[ch.get('yt_id')] = 0
    for t in history:
        if t.get('finished_date') != today:
            continue
        if 'Tayang' in t.get('status', '') and t.get('yt_id') in per:
            per[t['yt_id']] += 1
    lines = [
        f"📅 *Upload Hari Ini* — {_wib_now().strftime('%d %b %Y')}",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for ch in channels:
        n = per.get(ch.get('yt_id'), 0)
        lines.append(f"• *{_tg_escape(_channel_display_name(ch))}* — {n} video")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"✅ Total: {ok} video • ❌ Gagal: {fail}")
    return "\n".join(lines)

def check_panel_online():
    """Cek proses panel web (port 8000) dari dalam VPS. Redirect ke /login
    (302) tetap dihitung online — yang penting prosesnya menjawab."""
    try:
        res = requests.get(PANEL_URL + '/', timeout=5)
        return res.status_code in (200, 302)
    except Exception:
        return False

def _panel_version():
    """Versi panel dari file VERSION (+ short hash git) — '—' bila tak ada.
    Dipakai /status supaya pemilik selalu tahu versi apa yang sedang jalan."""
    ver = None
    try:
        with open(os.path.join(BASE_DIR, 'VERSION'), encoding='utf-8') as f:
            parts = (f.read() or '').strip().split()
            ver = parts[0] if parts else None
    except Exception:
        ver = None
    commit = None
    try:
        import subprocess
        p = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                           cwd=BASE_DIR, capture_output=True, text=True, timeout=10)
        commit = (p.stdout or '').strip() or None
    except Exception:
        commit = None
    if ver and commit:
        return f"v{ver} ({commit})"
    if ver:
        return f"v{ver}"
    return "—"


def _version_number():
    """Nomor versi polos dari file VERSION (mis. '1.3.14') — None bila tak ada."""
    try:
        with open(os.path.join(BASE_DIR, 'VERSION'), encoding='utf-8') as f:
            parts = (f.read() or '').strip().split()
            return parts[0] if parts else None
    except Exception:
        return None


def _changelog_lines(version, max_lines=5, path=None):
    """Baca CHANGELOG.md, kembalikan list bullet untuk `version` (maks 5).

    v1.3.14: pesan restart di Telegram menampilkan isi update supaya Bos
    langsung tahu apa yang berubah. File hilang / versi tak ada -> [] dan
    pesan restart tetap seperti biasa (fail-silent)."""
    if path is None:
        path = os.path.join(BASE_DIR, 'CHANGELOG.md')
    try:
        with open(path, encoding='utf-8') as f:
            lines = f.read().splitlines()
    except Exception:
        return []
    ver = (version or '').strip().lstrip('v')
    if not ver:
        return []
    in_section = False
    out = []
    for ln in lines:
        s = ln.strip()
        if s.startswith('## '):
            if in_section:
                break
            if s[3:].strip().lstrip('v') == ver:
                in_section = True
            continue
        if in_section and s.startswith('- '):
            out.append(s[2:].strip())
            if len(out) >= max_lines:
                break
    return out


def _autopilot_of(channel_data):
    """Mode autopilot channel: 'auto' atau 'approval' (default — sama
    seperti _autopilot_of di app.py; operator tidak mengimpor app)."""
    if not channel_data:
        return 'approval'
    mode = str(channel_data.get('autopilot') or 'approval').lower()
    return mode if mode in ('auto', 'approval') else 'approval'


def _task_progress_line(t):
    """🔥 v1.2.18: baris progres untuk /antrean + /status.
    '⏳ Merender 45%' / '🚀 Mengunggah 80%' — '' bila task tidak sedang
    dirender/di-upload (tanpa progress_pct atau tahap lain, mis. 'qc')."""
    try:
        pct = (t or {}).get('progress_pct')
        stage = str((t or {}).get('progress_stage') or '')
        if not isinstance(pct, (int, float)) or stage not in ('render', 'upload'):
            return ''
        pct = max(0, min(100, int(pct)))
        return f"⏳ Merender {pct}%" if stage == 'render' else f"🚀 Mengunggah {pct}%"
    except Exception:
        return ''

def build_status_report():
    """Ringkasan /status: channel + kuota, antrean, upload hari ini, service."""
    stats = get_system_telemetry()
    tasks_data = load_tasks()
    active = tasks_data.get('active', [])
    history = tasks_data.get('history', [])
    channels = load_channels()
    today = _wib_today_str()
    ok_today, fail_today, _, _ = _count_history_for_date(history, today)
    in_progress = len([t for t in active if 'Selesai' not in t.get('status', '')
                       and 'Gagal' not in t.get('status', '')
                       and 'Dibatalkan' not in t.get('status', '')])
    # 🔥 FASE 2: task batch baru yang menunggu keputusan owner.
    pending = [t for t in active if t.get('approval') == 'pending']
    panel_ok = check_panel_online()

    lines = [
        "📊 *Laporan buat Bos* 😎",
        f"_{_wib_now().strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🖥️ *Panel Web (8000):* {'🟢 Online' if panel_ok else '🔴 Mati/tidak menjawab'}",
        f"📌 *Versi Panel:* `{_panel_version()}`",
        "🤖 *Operator Telegram:* 🟢 Online",
        f"⚡ *CPU:* `{stats['cpu']}%`  🧠 *RAM:* `{stats['ram_used_gb']} GB / {stats['ram_total_gb']} GB ({stats['ram_pct']}%)`",
        f"💾 *Sisa Disk:* `{stats['disk_free_gb']} GB`",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    # 🔥 v1.2.16: pending approval PALING ATAS — yang paling butuh keputusan Bos.
    _pend_lines = _pending_approval_lines(active, channels, limit=5)
    if _pend_lines:
        lines += _pend_lines + ["━━━━━━━━━━━━━━━━━━━━━━"]
    # --- Channel + kuota ---
    if not channels:
        lines.append("📺 *Channel:* Belum ada yang terhubung nih Bos 😅")
        lines.append("Tautkan dulu dari panel (/factory → Connect), baru kuota & antrean bisa dipantau.")
    else:
        total_left = 0
        lines.append(f"📺 *Channel ({len(channels)}):*")
        for ch in channels:
            name = _channel_display_name(ch)
            used, left = get_channel_quota(ch)
            total_left += left
            conn = "🟢 Terhubung" if _channel_connected(ch) else "🔴 Putus"
            stock = get_stock_days(ch)
            if stock is None:
                stock_txt = "belum ada jadwal"
            elif stock < 0:
                stock_txt = "⚠️ stok habis"
            elif stock <= 1:
                stock_txt = f"⚠️ sisa ~{max(0, int(stock))} hari"
            else:
                stock_txt = f"sisa ~{int(stock)} hari"
            cd = ch.get('cooldown_until', 0)
            cd_txt = " ⏸️ cooldown" if cd and cd > time.time() else ""
            # 🔥 FASE 3: tandai channel yang sedang dijeda.
            pause_txt = " ⏸️ Dijeda" if ch.get('paused') else ""
            mode_txt = ' 🤖 Full-Auto' if _autopilot_of(ch) == 'auto' else ' 👀 Approval'
            lines.append(f"• *{_tg_escape(name)}* — {conn}{cd_txt}{pause_txt}{mode_txt}")
            # 🔥 v1.2.25: tampilkan project utama channel di /status.
            _spid = _project_id_of_channel(ch) or '—'
            lines.append(f"  Upload hari ini: `{used}/{MAX_UPLOADS_PER_DAY}` (sisa `{left}`) • Project `{_tg_escape(_spid)}` • Stok: {stock_txt}")
        lines.append(f"📈 *Total sisa kuota hari ini:* `{total_left} video`")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏭 *Antrean aktif:* `{len(active)} task` (`{in_progress}` sedang berjalan/menunggu)",
        f"⏳ *Menunggu approval:* `{len(pending)} task`" + (" — cek pesan preview di atas, atau setujui dari panel." if pending else ""),
        f"✅ *Upload sukses hari ini:* `{ok_today}`",
        f"❌ *Gagal hari ini:* `{fail_today}`",
        f"📁 *Riwayat:* `{len(history)} task`",
    ]
    # 🔥 v1.2.18: task yang sedang dirender/di-upload + persennya.
    _prog_tasks = [t for t in active if _task_progress_line(t)]
    if _prog_tasks:
        _pt = _prog_tasks[0]
        lines.append(f"🎬 *Sedang berjalan:* `{_tg_escape(str(_pt.get('title', 'Video'))[:40])}` — {_task_progress_line(_pt)}")
    # Penutup berbasis data: jangan klaim aman kalau ada yang gagal/mati/pending.
    if fail_today > 0 or not panel_ok or pending:
        lines.append("Bos, ada yang perlu dicek/diputuskan, lihat di atas ya 👀")
    else:
        lines.append("Aman Bos, saya yang urus 💪")
    return "\n".join(lines)

# ==========================================
# 📊 LAPORAN PAGI/SORE — STATISTIK CHANNEL + KESIAPAN MONETISASI
# ==========================================
# KEMBARAN helper panel (app.py): _channel_monetization_readiness,
# _monetization_rule_for, _snapshot_delta, _monetization_task_hours dan
# _shorts_views_90d — dihitung ulang di sini karena operator adalah proses
# terpisah dan TIDAK BOLEH mengimpor app.py. Bila logika panel berubah,
# samakan di sini; semua ambang & tanggal peralihan di SATU tempat di bawah.
MONETIZE_SUBS_REQUIRED = 1000
MONETIZE_RULE_CHANGE_DATE = "2027-02-01"
MONETIZE_WATCH_HOURS_BEFORE = 4000
MONETIZE_WATCH_HOURS_FROM = 8000
MONETIZE_SHORTS_VIEWS_BEFORE = 10_000_000
MONETIZE_SHORTS_VIEWS_FROM = 20_000_000
_SHORTS_MUSE_KIND = 'shorts_muse'


def _monetization_rule_for(date_str):
    """Ambang monetisasi YouTube yang berlaku pada tanggal WIB tertentu.
    s.d. 31 Jan 2027: 1.000 subs + 4.000 jam (12 bln) atau 10 jt views
    Shorts (90 hari). Mulai 1 Feb 2027: 8.000 jam atau 20 jt views Shorts.
    Subs tetap 1.000; channel yang sudah monetisasi tidak terpengaruh."""
    if date_str >= MONETIZE_RULE_CHANGE_DATE:
        return {"subs": MONETIZE_SUBS_REQUIRED, "watch_hours": MONETIZE_WATCH_HOURS_FROM,
                "shorts_views_90d": MONETIZE_SHORTS_VIEWS_FROM,
                "label": "1.000 subs + 8.000 jam tayang (365 hari) atau 20 jt views Shorts (90 hari)",
                "rule_next": None}
    return {"subs": MONETIZE_SUBS_REQUIRED, "watch_hours": MONETIZE_WATCH_HOURS_BEFORE,
            "shorts_views_90d": MONETIZE_SHORTS_VIEWS_BEFORE,
            "label": "1.000 subs + 4.000 jam tayang (12 bln) atau 10 jt views Shorts (90 hari)",
            "rule_next": "Mulai 1 Feb 2027: 1.000 subs + 8.000 jam tayang (365 hari) atau 20 jt views Shorts (90 hari)"}


def _snapshot_delta(totals, days, ref_date=None):
    """Selisih nilai snapshot: total(terbaru) − total(terdekat pada/
    sebelum ref−days). totals: {tanggal 'YYYY-MM-DD': nilai}.
    Kembalikan (delta, tanggal_dasar, tanggal_ref) atau (None, None,
    tanggal_ref) bila riwayat belum cukup — pemanggil menampilkan '—',
    jangan karang angka."""
    if not totals:
        return None, None, None
    ref = ref_date or max(totals)
    try:
        ref_d = datetime.strptime(ref, "%Y-%m-%d").date()
    except Exception:
        return None, None, ref
    target = (ref_d - timedelta(days=days)).strftime("%Y-%m-%d")
    cands = [d for d in totals if d <= target]
    if not cands:
        return None, None, ref
    base_d = max(cands)
    if base_d == ref:
        return None, None, ref
    try:
        return int(totals[ref]) - int(totals[base_d]), base_d, ref
    except Exception:
        return None, None, ref


def _task_hours(t):
    """Durasi video tayang dalam jam: kompilasi dari blueprint
    target_duration_hours; Shorts Muse dari shorts_duration_sec."""
    bp = t.get('blueprint') or {}
    try:
        kind = t.get('kind') or bp.get('kind')
        if kind == _SHORTS_MUSE_KIND:
            return float(t.get('shorts_duration_sec') or bp.get('shorts_duration_sec') or 0) / 3600.0
        return float(bp.get('target_duration_hours') or t.get('target_duration_hours') or 0)
    except Exception:
        return 0.0


def _avg_long_duration_hours(history, yt_id):
    """Durasi rata-rata blueprint video LONG per channel (jam) — dasar
    estimasi jam tayang (metode sama seperti kartu Monetisasi panel)."""
    total, cnt = 0.0, 0
    for t in history or []:
        if _history_task_yt_id(t) != yt_id:
            continue
        if (t.get('kind') or (t.get('blueprint') or {}).get('kind')) == _SHORTS_MUSE_KIND:
            continue
        h = _task_hours(t)
        if h and h > 0:
            total += h
            cnt += 1
    return (total / cnt) if cnt else None


def _shorts_views_90d(yt_id, history, cached_videos, vid_snaps, ref_date):
    """Views Shorts (90 hari) dari snapshot per-video — HANYA video Shorts
    Muse (kind='shorts_muse'). Klip Clipper TIDAK termasuk (tidak tercatat
    di snapshot pabrik) — angka bisa terlalu rendah bila channel punya klip
    Clipper tayang. Kembalikan (total_views, complete): complete=False bila
    ada video Shorts yang baseline 90 hari lalu-nya tidak bisa ditentukan —
    jalur Shorts lalu tidak dipakai untuk badge. Tanpa video Shorts sama
    sekali → (0, True), terhitung jujur."""
    vid_info = {}
    for t in history or []:
        vid = _history_task_video_id(t)
        yt = _history_task_yt_id(t)
        if vid and yt:
            vid_info[vid] = (yt, t.get('kind') or (t.get('blueprint') or {}).get('kind'))
    total = 0
    complete = True
    found = False
    for vid, (yt, kind) in (vid_info or {}).items():
        if yt != yt_id or kind != _SHORTS_MUSE_KIND:
            continue
        found = True
        cv = cached_videos.get(vid) or {}
        if cv.get('missing'):
            continue
        cur = cv.get('views')
        if cur is None:
            continue  # belum pernah di-fetch — tidak bisa dihitung
        snaps = vid_snaps.get(vid, {})
        ref_v = max(snaps) if snaps else (ref_date or _wib_now().strftime("%Y-%m-%d"))
        try:
            target = (datetime.strptime(ref_v, "%Y-%m-%d").date() - timedelta(days=90)).strftime("%Y-%m-%d")
        except Exception:
            complete = False
            continue
        cands = [d for d in snaps if d <= target]
        if cands:
            base = max(cands)
            total += int(cur) - int(snaps[base])
        else:
            pub = (cv.get('published_at', '') or '')[:10]
            if pub and pub > target:
                total += int(cur)  # video belum ada 90 hari lalu → semua views masuk
            else:
                complete = False
    if not found:
        return 0, True
    return total, complete


def _channel_monetization_readiness(*, subs, watch_90d_est_hours, shorts_views_90d,
                                   shorts_complete, monetized, today_str, watch_real_hours=None):
    """SATU tempat perhitungan kesiapan monetisasi di operator (kembaran
    helper panel). PENTING: jam tayang di sini HANYA ESTIMASI (snapshot
    views 90 hari terakhir × durasi rata-rata blueprint video long channel).
    Jam tayang asli butuh YouTube Analytics API yang tidak diizinkan di
    device flow; keputusan akhir monetisasi ada di YouTube Studio. Jalur
    Shorts hanya dipakai bila shorts_complete=True; bila tidak, badge tidak
    pernah 'ready' lewat jalur Shorts."""
    rule = _monetization_rule_for(today_str)
    _watch = watch_real_hours if watch_real_hours is not None else watch_90d_est_hours
    base = {"subs": subs or 0, "subs_required": rule["subs"],
            "watch_est": _watch, "watch_real": watch_real_hours is not None,
            "watch_required": rule["watch_hours"],
            "shorts_views": shorts_views_90d, "shorts_required": rule["shorts_views_90d"],
            "shorts_complete": bool(shorts_complete),
            "rule_label": rule["label"], "rule_next": rule["rule_next"]}
    if monetized:
        return dict(base, status="monetized", badge="💰 Monetisasi Aktif", via=None)
    subs_ok = (subs or 0) >= rule["subs"]
    hours_ok = _watch is not None and _watch >= rule["watch_hours"]
    shorts_ok = bool(shorts_complete) and (shorts_views_90d or 0) >= rule["shorts_views_90d"]
    ready = subs_ok and (hours_ok or shorts_ok)
    via = None
    if ready:
        via = "shorts" if (shorts_ok and not hours_ok) else "hours"
    return dict(base, status="ready" if ready else "progress",
                badge=("✅ Siap Monet" if watch_real_hours is not None else "✅ Siap Monet (est.)") if ready else "⏳ Belum Siap", via=via)


# Definisi "segera siap" (dipakai ringkasan laporan): channel belum siap dan
# belum monetisasi, dan progres syarat yang PALING KURANG sudah ≥50%
# (progres = nilai / ambang; untuk performa dipakai yang terbesar antara jam
# tayang dan views Shorts). Ini mencakup "1 syarat terpenuhi & sisanya
# ≥50%" maupun "keduanya sudah ≥50% tapi belum ada yang terpenuhi".
def _is_almost_ready(ready):
    """True bila channel masuk kategori 'segera siap' (lihat definisi di
    atas). ready = dict hasil _channel_monetization_readiness."""
    if ready.get("status") != "progress":
        return False
    subs_prog = (ready.get("subs") or 0) / ready["subs_required"] if ready.get("subs_required") else 0
    perf_prog = 0.0
    if ready.get("watch_est") is not None:
        perf_prog = max(perf_prog, ready["watch_est"] / ready["watch_required"])
    if ready.get("shorts_complete"):
        perf_prog = max(perf_prog, (ready.get("shorts_views") or 0) / ready["shorts_required"])
    return min(subs_prog, perf_prog) >= 0.5


def _fetch_channel_stats_api(ch):
    """channels().list part=snippet,statistics untuk SATU channel (1 unit
    kuota). Dipakai hanya bila cache panel & cache operator sudah basi.
    Import googleapiclient malas (lazy) supaya operator tetap jalan bila
    library belum terpasang. Kembalikan (views, subs, videos) atau None."""
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from googleapiclient.discovery import build
    except Exception:
        return None
    creds_list = ch.get('creds_list') or [ch.get('creds_json')]
    for cred_str in creds_list:
        if not cred_str:
            continue
        try:
            creds = Credentials.from_authorized_user_info(json.loads(cred_str))
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            youtube = build('youtube', 'v3', credentials=creds)
            res = youtube.channels().list(part="snippet,statistics",
                                          id=ch.get('yt_id')).execute()
            if res.get('items'):
                st = res['items'][0].get('statistics', {})
                return (int(st.get('viewCount', 0)),
                        int(st.get('subscriberCount', 0)),
                        int(st.get('videoCount', 0)))
        except Exception:
            continue
    return None


def _get_report_channel_stats(channels):
    """Statistik channel untuk laporan: {yt_id, name, views, subs, videos,
    ok, source, monetized}. Sumber berlapis (yang pertama berhasil dipakai):
    1) cache panel video_stats_cache.json channel_stats (TTL 1 jam);
    2) cache operator sendiri di monitor_state.json (TTL 1 jam);
    3) YouTube Data API channels().list (1 unit kuota/channel) — hasilnya
    disimpan ke cache operator. Bila ketiganya gagal, ok=False dan angka
    None (laporan menampilkan '—' untuk channel itu saja; channel lain
    tetap jalan). Cache panel yang basi tapi ada tetap dipakai (ditandai)
    daripada mengosongkan laporan."""
    now = time.time()
    cache = _load_video_stats_cache()
    panel_stats = cache.get('channel_stats') or {}
    state = load_monitor_state()
    op_cache = state.get('channel_stats_cache') or {}
    op_dirty = False
    out = []
    for ch in channels:
        yt_id = ch.get('yt_id') or ''
        name = _channel_display_name(ch)
        views = subs = videos = None
        source = None
        ent = panel_stats.get(yt_id) or {}
        if (ent.get('views') is not None and ent.get('fetched_at')
                and (now - float(ent.get('fetched_at', 0)) < CHANNEL_STATS_CACHE_TTL)):
            views, subs, videos = ent.get('views'), ent.get('subs'), ent.get('videos')
            source = 'cache panel'
        else:
            op = op_cache.get(yt_id) or {}
            if (op.get('views') is not None and op.get('fetched_at')
                    and (now - float(op.get('fetched_at', 0)) < CHANNEL_STATS_CACHE_TTL)):
                views, subs, videos = op.get('views'), op.get('subs'), op.get('videos')
                source = 'cache operator'
            else:
                fetched = _fetch_channel_stats_api(ch)
                if fetched is not None:
                    views, subs, videos = fetched
                    source = 'API YouTube'
                    op_cache[yt_id] = {"views": views, "subs": subs, "videos": videos,
                                       "fetched_at": now}
                    op_dirty = True
        if views is None and ent.get('views') is not None:
            views, subs, videos = ent.get('views'), ent.get('subs'), ent.get('videos')
            source = 'cache panel (basi)'
        out.append({"yt_id": yt_id, "name": name, "views": views, "subs": subs,
                    "videos": videos, "ok": views is not None, "source": source,
                    "monetized": bool(ch.get('monetized'))})
    if op_dirty:
        state["channel_stats_cache"] = op_cache
        save_monitor_state(state)
    return out


def _fmt_snapshot_time(at_str):
    """'2026-11-18 07:05' → '18 Nov 07:05'. None bila tidak bisa diparse."""
    try:
        dt = datetime.strptime(str(at_str), "%Y-%m-%d %H:%M")
        return dt.strftime("%d %b %H:%M")
    except Exception:
        return None


# 🔥 v1.3.1: pemisah antar channel di laporan Telegram. Garis tipis box
# drawing (U+2500) — dipertahankan sanitizer v1.2.21 (bukan emoji), jadi
# tampil utuh di Telegram. Panjang 22 = sejajar pemisah tebal "━━━"
# antar seksi. Dipakai _build_channel_stats_section (laporan harian),
# build_detail_report (/laporan detail) dan build_channel_list_report
# (/channel) supaya blok tiap channel tidak menempel jadi satu tembok teks.
TG_CHANNEL_DIVIDER = "──────────────────────"


def _build_channel_stats_section(channels):
    """Bagian 'Views & Subscriber per Channel' untuk laporan pagi/sore/
    on-demand. Kembalikan (lines, new_snapshot): new_snapshot =
    {yt_id: {views, subs, videos, at}} untuk disimpan SETELAH laporan
    terkirim (pemanggil yang menyimpan — jangan simpan di sini supaya
    laporan yang gagal kirim tidak menggeser baseline delta)."""
    today_str = _wib_today_str()
    now_label = _wib_now().strftime("%Y-%m-%d %H:%M")
    stats = _get_report_channel_stats(channels)
    cache = _load_video_stats_cache()
    history = load_tasks().get('history', [])
    # Snapshot harian channel panel (90 hari) → views 90 hari & jam tayang.
    ch_snaps = cache.get('channel_snapshots') or []
    ch_totals = {}
    for s in ch_snaps:
        if s.get('yt_id'):
            ch_totals.setdefault(s['yt_id'], {})[s.get('date')] = int(s.get('views', 0) or 0)
    # Snapshot per-video panel → jalur Shorts 90 hari.
    cached_videos = cache.get('videos', {}) or {}
    vid_snaps = {}
    for s in cache.get('snapshots', []) or []:
        vid_snaps.setdefault(s.get('video_id'), {})[s.get('date')] = int(s.get('views', 0) or 0)
    prev = load_monitor_state().get("channel_report_snapshot") or {}
    new_snapshot = {}
    any_missing_baseline = False
    rule = _monetization_rule_for(today_str)
    blocks = []   # baris-baris per channel (ditampilkan setelah ringkasan)
    records = []  # status kesiapan per channel untuk hitungan ringkasan
    for st in stats[:8]:
        name = st["name"]
        if not st["ok"]:
            blocks.append([f"*{_tg_escape(name)}*",
                           "  Data belum bisa diambil (coba lagi di laporan berikutnya)"])
            continue
        views, subs, videos = int(st["views"]), int(st["subs"] or 0), int(st["videos"] or 0)
        new_snapshot[st["yt_id"]] = {"views": views, "subs": subs, "videos": videos, "at": now_label}
        block = [f"*{_tg_escape(name)}*",
                 f"  Views: {_fmt_num(views)} • Subs: {_fmt_num(subs)} • Video: {_fmt_num(videos)}"]
        base = prev.get(st["yt_id"]) or {}
        subs_per_day = hours_per_day = None
        eta_hint = None
        if base.get('views') is None or base.get('subs') is None:
            any_missing_baseline = True
            eta_hint = "Belum bisa dihitung — butuh 2 laporan."
        else:
            try:
                dv = views - int(base['views'])
                ds = subs - int(base['subs'])
                when = _fmt_snapshot_time(base.get('at', ''))
                when_txt = f" ({when})" if when else ""
                block.append(f"  📈 Sejak laporan terakhir{when_txt}: 👁️ {'+' if dv >= 0 else ''}{_fmt_num(dv)} • 👥 {'+' if ds >= 0 else ''}{_fmt_num(ds)}")
                try:
                    base_dt = datetime.strptime(str(base.get('at', '')), "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Jakarta"))
                except Exception:
                    base_dt = None
                if base_dt is None:
                    eta_hint = "Belum bisa dihitung — butuh 2 laporan."
                else:
                    elapsed_h = (_wib_now() - base_dt).total_seconds() / 3600.0
                    if elapsed_h < 6:
                        eta_hint = "Belum bisa dihitung — jeda laporan terlalu pendek."
                    else:
                        days = elapsed_h / 24.0
                        subs_per_day = ds / days
                        views_per_day = dv / days  # jam/hari dihitung setelah avg_h diketahui
            except Exception:
                any_missing_baseline = True
                eta_hint = "Belum bisa dihitung — butuh 2 laporan."
        # --- kesiapan monetisasi (kembaran helper panel) ---
        v90, _, _ = _snapshot_delta(ch_totals.get(st["yt_id"]) or {}, 90)
        avg_h = _avg_long_duration_hours(history, st["yt_id"])
        watch90 = round(v90 * avg_h, 1) if (v90 is not None and avg_h) else None
        ref_date = max(ch_totals.get(st["yt_id"])) if ch_totals.get(st["yt_id"]) else today_str
        shorts_v, shorts_ok = _shorts_views_90d(st["yt_id"], history, cached_videos, vid_snaps, ref_date)
        ready = _channel_monetization_readiness(subs=subs, watch_90d_est_hours=watch90,
                                                shorts_views_90d=shorts_v, shorts_complete=shorts_ok,
                                                monetized=st["monetized"], today_str=today_str)
        if subs_per_day is not None:
            try:
                hours_per_day = (views_per_day * avg_h) if avg_h else None
            except Exception:
                hours_per_day = None
        if ready["status"] == "monetized":
            block.append("  💰 Monetisasi Aktif")
        elif ready["status"] == "ready":
            via = "jam tayang" if ready.get("via") == "hours" else "views Shorts"
            block.append(f"  💰 ✅ Siap Monet (est.) — lewat {via} 🎉")
            block.append(f"     Subs {_fmt_num(subs)}/{_fmt_num(rule['subs'])} • ~{_fmt_num(round(watch90)) if watch90 is not None else '—'} jam/{_fmt_num(rule['watch_hours'])} jam (est.)")
        else:
            subs_missing = max(0, rule['subs'] - subs)
            if watch90 is not None:
                hours_missing = max(0, rule['watch_hours'] - watch90)
                watch_txt = f"~{_fmt_num(round(watch90))} jam (kurang ~{_fmt_num(round(hours_missing))} jam)"
            else:
                watch_txt = "jam tayang: —"
            block.append(f"  💰 Subs kurang {_fmt_num(subs_missing)} • {watch_txt}")
        records.append({"status": ready["status"], "soon": _is_almost_ready(ready)})
        blocks.append(block)
    lines = ["📺 *Views & Subscriber per Channel:*"]
    if records:
        n_monet = sum(1 for r in records if r["status"] == "monetized")
        n_ready = sum(1 for r in records if r["status"] == "ready")
        n_soon = sum(1 for r in records if r.get("soon"))
        n_far = sum(1 for r in records if r["status"] == "progress" and not r.get("soon"))
        parts = [f"{n_monet} channel sudah monet", f"{n_soon} channel segera siap", f"{n_far} channel masih jauh"]
        if n_ready:
            parts.insert(1, f"{n_ready} channel siap diajukan")
        lines.append("💰 *Monetisasi:* " + " • ".join(parts))
    if blocks:
        lines.append("")
        for i, block in enumerate(blocks):
            if i > 0:
                lines.append(TG_CHANNEL_DIVIDER)
                lines.append("")
            lines.extend(block)
    if len(stats) > 8:
        lines.append(f"_...dan {len(stats) - 8} channel lainnya (cek di panel)._")
    return lines, new_snapshot


def _update_report_snapshot(new_snapshot):
    """Simpan baseline delta untuk laporan berikutnya (gabung dengan yang
    lama: channel yang gagal diambil datanya di laporan ini tetap memakai
    baseline lamanya)."""
    if not new_snapshot:
        return
    state = load_monitor_state()
    snap = state.get("channel_report_snapshot") or {}
    snap.update(new_snapshot)
    state["channel_report_snapshot"] = snap
    save_monitor_state(state)


def build_daily_report(kind="pagi"):
    """Satu pembangun laporan untuk ketiga jalur: pagi (07:00), sore (17:00),
    dan on-demand (/laporan). Kembalikan (teks, snapshot_baru): snapshot
    berisi views/subs/video per channel yang dipakai laporan ini — pemanggil
    menyimpannya ke monitor_state.json SETELAH pesan terkirim (supaya delta
    laporan berikutnya dihitung dari laporan terakhir yang benar-benar
    terkirim). kind 'pagi' = rekap hasil KEMARIN; 'sore' = hasil HARI INI;
    'ondemand' = sama seperti pagi tapi tanpa sapaan."""
    tasks_data = load_tasks()
    active = tasks_data.get('active', [])
    history = tasks_data.get('history', [])
    channels = load_channels()
    now = _wib_now()
    if kind == "sore":
        yday = _wib_today_str()
        recap_label = "hasil hari ini"
        greeting = "Sore Bos! 🌇 Gimana harinya, lancar kan? 😊"
        title = "🌆 *LAPORAN SORE BUAT BOS* 🌆"
        ok_label, fail_label = "Upload sukses hari ini", "Gagal hari ini"
    elif kind == "ondemand":
        yday = _wib_yesterday_str()
        recap_label = "hasil kemarin"
        greeting = None
        title = "📋 *LAPORAN BUAT BOS* 📋"
        ok_label, fail_label = "Upload sukses kemarin", "Gagal kemarin"
    else:
        kind = "pagi"
        yday = _wib_yesterday_str()
        recap_label = "hasil kemarin"
        greeting = "Pagi Bos! ☀️ Gimana kabarnya hari ini, sehat kan? 😊"
        title = "🌅 *LAPORAN HARIAN BUAT BOS* ☀️"
        ok_label, fail_label = "Upload sukses kemarin", "Gagal kemarin"
    ok, fail, ok_titles, fail_titles = _count_history_for_date(history, yday)

    # Sapaan pagi/sore 1x sehari (persona asisten pribadi) — di depan
    # laporan, info penting tetap di bawahnya, tidak tertunda.
    lines = []
    if greeting:
        lines += [greeting, ""]
    lines += [
        title,
        f"Rekap {now.strftime('%d %b %Y')} ({recap_label}, {yday})",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"✅ *{ok_label}:* `{ok}`",
        f"❌ *{fail_label}:* `{fail}`",
    ]
    if fail_titles:
        lines.append("Gagal: " + "; ".join("• " + _tg_escape(t) for t in fail_titles[:5]))
    if ok_titles:
        lines.append("Sukses: " + "; ".join("• " + _tg_escape(t) for t in ok_titles[:5]))
    # --- views & subs per channel + kesiapan monetisasi ---
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if not channels:
        lines.append("📺 *Channel:* Belum ada channel terhubung.")
        new_snapshot = {}
    else:
        ch_lines, new_snapshot = _build_channel_stats_section(channels)
        lines.extend(ch_lines)
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏭 *Antrean tersisa:* `{len(active)} task`",
    ]
    for t in active[:5]:
        lines.append(f"• {_tg_escape(t.get('title', '?')[:45])} — {_tg_escape(t.get('status', '-')[:40])}")
    if len(active) > 5:
        lines.append(f"_...dan {len(active) - 5} task lainnya._")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    # 🔥 v1.2.16: bagian Menunggu Approval di laporan pagi/sore/ondemand.
    _pend_lines = _pending_approval_lines(active, channels, limit=5)
    if _pend_lines:
        lines += _pend_lines
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if not channels:
        lines.append("📺 *Channel:* Belum ada channel terhubung.")
    else:
        lines.append("📺 *Stok jadwal channel:*")
        for ch in channels:
            name = _channel_display_name(ch)
            stock = get_stock_days(ch)
            if stock is None:
                lines.append(f"• *{_tg_escape(name)}*: belum ada jadwal")
            elif stock < 0:
                lines.append(f"• *{_tg_escape(name)}*: ⚠️ stok HABIS — Bos, buat batch baru ya!")
            elif stock <= 1:
                lines.append(f"• *{_tg_escape(name)}*: ⚠️ sisa ~{max(0, int(stock))} hari — Bos, segera buat batch baru ya!")
            else:
                lines.append(f"• *{_tg_escape(name)}*: sisa ~{int(stock)} hari")
    if fail > 0:
        lines.append("\nYang gagal saya coba lagi ya Bos 💪")
    elif ok > 0:
        lines.append("\nSemua mulus Bos, saya lanjutkan 💪 Yang udah tayang jangan lupa ditonton ya, jangan lupa istirahat juga 😌")
    elif kind == "sore":
        lines.append("\nHari ini lagi sepi Bos, nggak ada yang tayang 😌")
    else:
        lines.append("\nKemarin lagi sepi Bos, nggak ada yang tayang 😌")
    lines.append("Bos, ketik /status kapan aja kalau mau cek kondisi terkini 😎")
    return "\n".join(lines), new_snapshot

# ==========================================
# 🔥 PERLUASAN PERINTAH — LAPORAN DETAIL (baca saja)
# Semua builder di bawah ini READ-ONLY: hanya membaca file JSON lokal
# (channels_db, tasks_db, video_stats_cache) dan folder galeri di disk.
# Tidak ada yang mengubah data, memanggil endpoint tulis, atau mencetak
# token/kredensial. Daftar dibatasi maks 8 item supaya pesan < 4096 karakter.
# ==========================================
_START_TIME = time.time()  # uptime operator sejak proses ini jalan
_VIDEO_ID_RE = re.compile(r'(?:youtu\.be/|v=|/shorts/)([A-Za-z0-9_-]{11})')
_VIDEO_STATS_CACHE_FILE = os.path.join(BASE_DIR, 'video_stats_cache.json')
_GALLERY_FOLDERS = ['audios', 'backgrounds', 'thumbnails', 'base_videos', 'overlays', 'special_songs']
_GALLERY_LABELS = {'audios': '🎵 Musik', 'backgrounds': '🖼️ Background',
                   'thumbnails': '🖼️ Thumbnail', 'base_videos': '🎬 Video dasar',
                   'overlays': '✨ Overlay', 'special_songs': '🎶 Lagu khusus'}


def _load_video_stats_cache():
    """Cache statistik Fase 4 (views/likes/komentar per video). Kosong kalau
    belum ada video tayang / cache belum terisi — pemanggil harus jujur
    bilang 'belum ada data', jangan mengarang angka."""
    try:
        if os.path.exists(_VIDEO_STATS_CACHE_FILE):
            with open(_VIDEO_STATS_CACHE_FILE, 'r') as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get('videos'), dict):
                return d
    except Exception:
        pass
    return {"videos": {}}


def _project_id_of_channel(ch):
    """Nomor project Google dari prefix client_id kredensial utama channel
    (angka sebelum '-', cerminan app.py). Hanya nomor project — BUKAN secret,
    dan kredensial tidak pernah dicetak/dikirim ke mana pun."""
    try:
        if not ch:
            return None
        creds_list = ch.get('creds_list') or [ch.get('creds_json')]
        cred_str = next((s for s in creds_list if s), '')
        if not cred_str:
            return None
        client_id = (json.loads(cred_str) or {}).get('client_id', '')
        if not client_id:
            return None
        return str(client_id).split('-')[0] or None
    except Exception:
        return None


def _extract_video_id_from_status(status):
    if not status:
        return None
    m = _VIDEO_ID_RE.search(str(status))
    return m.group(1) if m else None


def _history_task_video_id(t):
    return t.get('video_id') or _extract_video_id_from_status(t.get('status', ''))


def _history_task_yt_id(t):
    return t.get('yt_id') or (t.get('blueprint') or {}).get('yt_id')


def _fmt_num(n):
    """12.345 (pemisah ribuan gaya Indonesia) supaya angka enak dibaca."""
    try:
        return f"{int(n):,}".replace(",", ".")
    except Exception:
        return str(n)


# ==========================================
# 🔥 v1.2.22 — FORMAT SATU BARIS VIDEO (judul + views + tanggal + durasi)
# User 14:05–14:06 WIB: daftar video di Telegram harus menampilkan views,
# tanggal upload/tayang DAN durasi — sesimpel & serapi mungkin, satu baris
# ringkas per video dengan pemisah yang sama di SEMUA daftar (riwayat,
# jadwal, jadwalbulan, laporan detail, statistik, channel, antrean, gagal).
# Data HANYA dari sumber yang sudah ada (video_stats_cache, tasks_db,
# metadata task) — bila tidak tersedia tampilkan "—", JANGAN karang angka.
# ==========================================
def _fmt_dur_sec(sec):
    """Durasi singkat konsisten: '1:17:24' (jam) atau '0:30' (di bawah 1 jam);
    '—' bila tidak ada / tidak valid. Satu format di semua daftar.
    Dibulatkan (target_duration_hours float bisa 4619.9998, bukan 4620)."""
    try:
        s = int(round(float(sec)))
    except Exception:
        return "—"
    if s < 0:
        return "—"
    h, m, r = s // 3600, (s % 3600) // 60, s % 60
    if h:
        return f"{h}:{m:02d}:{r:02d}"
    return f"{m}:{r:02d}"


def _task_duration_sec(t):
    """Durasi video (detik) dari metadata task yang SUDAH ADA: Shorts
    (shorts_duration_sec), playlist/long (target_duration_hours di
    blueprint), audio_duration. None bila tidak tersedia."""
    bp = (t or {}).get('blueprint') or {}
    for v in ((t or {}).get('shorts_duration_sec'), bp.get('shorts_duration_sec'),
              (t or {}).get('audio_duration'), bp.get('audio_duration')):
        try:
            f = float(v)
            if f > 0:
                return f
        except Exception:
            pass
    for v in (bp.get('target_duration_hours'), (t or {}).get('target_duration_hours')):
        try:
            f = float(v)
            if f > 0:
                return f * 3600.0
        except Exception:
            pass
    return None


def _video_views_of(t, cache_videos):
    """Views dari video_stats_cache via video_id; None bila belum ada data
    (video belum tayang / belum sempat di-fetch / missing) — jangan karang."""
    vid = _history_task_video_id(t)
    if not vid:
        return None
    st = (cache_videos or {}).get(vid) or {}
    if not st or st.get('missing'):
        return None
    try:
        return int(st.get('views', 0))
    except Exception:
        return None


def _fmt_date_short(s, with_time=False):
    """'2026-11-19' -> '19 Nov'; '2026-11-19 19:00' + with_time -> '19 Nov 19:00'.
    '—' bila kosong/rusak."""
    s = (s or '').strip()
    if not s:
        return "—"
    try:
        d = datetime.strptime(s[:10], "%Y-%m-%d")
        out = f"{d.day} {_MONTHS_ID[d.month - 1]}"
        if with_time and len(s) >= 16:
            out += f" {s[11:16]}"
        return out
    except Exception:
        return s[:10] or "—"


def _fmt_dt_short(dt):
    """datetime WIB -> '19 Nov 19:00'; '—' bila None."""
    if dt is None:
        return "—"
    return f"{dt.day} {_MONTHS_ID[dt.month - 1]} {dt.strftime('%H:%M')}"


def _video_meta(title, views, date_str, dur_sec):
    """SATU format baris video untuk semua daftar Telegram (v1.2.22):
    'Judul | 1.234 views | 19 Nov | 1:17:24' — '—' bila data tak ada.
    Judul & tanggal di-escape Markdown; pemanggil menambahkan bullet/ikon
    atau nomor sendiri di depan."""
    v = _fmt_num(views) if views is not None else "—"
    d = (date_str or '').strip() or "—"
    return (f"{_tg_escape(str(title or '?')[:45])} | {v} views | "
            f"{_tg_escape(d)} | {_fmt_dur_sec(dur_sec)}")


def _uptime_str(seconds):
    """'2 hari 3 jam 15 mnt' dari detik."""
    try:
        seconds = int(seconds)
    except Exception:
        return "-"
    days, rem = divmod(max(0, seconds), 86400)
    hours, rem = divmod(rem, 3600)
    mins, _ = divmod(rem, 60)
    parts = []
    if days:
        parts.append(f"{days} hari")
    if hours:
        parts.append(f"{hours} jam")
    parts.append(f"{mins} mnt")
    return " ".join(parts)


def _clip(text, limit=4000):
    """Pengaman batas 4096 karakter Telegram: potong di baris terakhir yang
    utuh sebelum batas, jangan di tengah kata/baris."""
    if len(text) <= limit:
        return text
    cut = text[:limit].rsplit("\n", 1)[0]
    return cut + "\n_(pesan dipotong — kepanjangan)_"


def _count_gallery(yt_id):
    """Hitung isi galeri channel: {folder: (jumlah_file, byte)} + total.
    Folder tidak ada = 0. Hanya hitung file, bukan subfolder."""
    out = {}
    total_files = 0
    total_bytes = 0
    base = os.path.join(BASE_DIR, 'uploads', str(yt_id or ''))
    for folder in _GALLERY_FOLDERS:
        count = 0
        size = 0
        try:
            path = os.path.join(base, folder)
            if os.path.isdir(path):
                for name in os.listdir(path):
                    fp = os.path.join(path, name)
                    if os.path.isfile(fp):
                        count += 1
                        try:
                            size += os.path.getsize(fp)
                        except Exception:
                            pass
        except Exception:
            pass
        out[folder] = (count, size)
        total_files += count
        total_bytes += size
    return out, total_files, total_bytes


def _stats_by_channel():
    """Gabungkan riwayat tayang + cache statistik Fase 4 per channel.
    Kembalikan {yt_id: {name, videos: [...], views, likes, comments,
    views_7d, views_30d, hours}}. Video tanpa statistik di cache dilewati
    (belum sempat di-fetch API) — totalnya jadi jujur 'yang tercatat'."""
    channels = load_channels()
    name_by_yt = {c.get('yt_id'): _channel_display_name(c) for c in channels}
    cached = _load_video_stats_cache().get('videos', {})
    history = load_tasks().get('history', [])
    now = _wib_now().date()
    per_channel = {}

    def _in_days(pub, days):
        try:
            d = datetime.strptime(pub, "%Y-%m-%d").date()
            return 0 <= (now - d).days < days
        except Exception:
            return False

    for t in history:
        vid = _history_task_video_id(t)
        if not vid:
            continue
        yt = _history_task_yt_id(t)
        if not yt:
            continue
        st = cached.get(vid) or {}
        if not st or st.get('missing'):
            continue
        pub = (st.get('published_at', '') or '')[:10] or (t.get('finished_date') or '')[:10] or ''
        views = int(st.get('views', 0) or 0)
        likes = int(st.get('likes', 0) or 0)
        comments = int(st.get('comments', 0) or 0)
        e = per_channel.setdefault(yt, {"name": name_by_yt.get(yt, yt), "videos": [],
                                        "views": 0, "likes": 0, "comments": 0,
                                        "views_7d": 0, "views_30d": 0, "hours": 0.0})
        e["videos"].append({"title": t.get('title', '?'), "views": views,
                            "likes": likes, "comments": comments, "pub": pub,
                            "video_id": vid, "dur": _task_duration_sec(t)})
        e["views"] += views
        e["likes"] += likes
        e["comments"] += comments
        if _in_days(pub, 7):
            e["views_7d"] += views
        if _in_days(pub, 30):
            e["views_30d"] += views
        bp = t.get('blueprint') or {}
        try:
            kind = t.get('kind') or bp.get('kind')
            if kind == 'shorts_muse':
                e["hours"] += float(t.get('shorts_duration_sec') or bp.get('shorts_duration_sec') or 0) / 3600.0
            else:
                e["hours"] += float(bp.get('target_duration_hours') or t.get('target_duration_hours') or 0)
        except Exception:
            pass
    return per_channel


def build_quota_report():
    """Kuota upload: per project Google (batas 100/hari/project) + per channel."""
    channels = load_channels()
    today = _wib_today_str()
    lines = [
        "🎯 *Kuota Upload Buat Bos* 😎",
        f"_{_wib_now().strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not channels:
        lines.append("📺 Belum ada channel terhubung nih Bos 😅")
        lines.append("Tautkan dulu dari panel, baru kuota bisa saya hitung.")
        return "\n".join(lines)
    projects = {}
    for ch in channels:
        pid = _project_id_of_channel(ch) or 'tidak-diketahui'
        e = projects.setdefault(pid, {"channels": [], "used": 0})
        e["channels"].append(ch)
    # 🔥 v1.2.25: hitungan per project dari daily_uploads_by_project
    # (benar setelah auto-switch); project cadangan di registry yang
    # belum punya channel tetap ditampilkan (0 upload).
    for pid in list(projects):
        if pid != 'tidak-diketahui':
            projects[pid]["used"] = _project_uploads_today_op(channels, pid)
    for _c in _oauth_registry_clients():
        _pid = str(_c.get('project_id') or '')
        if _pid and _pid not in projects:
            projects[_pid] = {"channels": [], "used": 0, "backup": True,
                              "client_name": _c.get('name') or _pid}
    lines.append("☁️ *Per Project Google* (batas 100 upload/hari/project):")
    for pid in sorted(projects):
        e = projects[pid]
        used = e["used"]
        left = max(0, MAX_UPLOADS_PER_DAY - used)
        names = ", ".join(_channel_display_name(c) for c in e["channels"][:4])
        cd = any((c.get('cooldown_until') or 0) > time.time() for c in e["channels"])
        _tag = " (cadangan)" if e.get("backup") else ""
        _cname = f" — {e.get('client_name')}" if e.get("client_name") else ""
        lines.append(f"• Project `{_tg_escape(pid)}`{_tag}{_tg_escape(_cname)}: `{used}/{MAX_UPLOADS_PER_DAY}` (sisa `{left}`)"
                     + (" ⏸️ cooldown sampai tengah malam" if cd else ""))
        if names:
            lines.append(f"  Channel: {_tg_escape(names)}")
        if len(e["channels"]) > 4:
            lines.append(f"  _...dan {len(e['channels']) - 4} channel lainnya._")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📺 *Per channel hari ini:*")
    for ch in channels[:8]:
        name = _channel_display_name(ch)
        used, left = get_channel_quota(ch)
        pause = " ⏸️ Dijeda" if ch.get('paused') else ""
        _pid = _project_id_of_channel(ch) or '—'
        lines.append(f"• *{_tg_escape(name)}* (project `{_tg_escape(_pid)}`): `{used}/{MAX_UPLOADS_PER_DAY}` (sisa `{left}`){pause}")
    if len(channels) > 8:
        lines.append(f"_...dan {len(channels) - 8} channel lainnya._")
    lines.append("Bos, kalau kuota project udah 100/100, upload baru nunggu reset tengah malam WIB ya 😌")
    return "\n".join(lines)


def build_stock_report():
    """Sisa stok jadwal + isi galeri per channel."""
    channels = load_channels()
    lines = [
        "📦 *Stok Jadwal & Galeri Buat Bos* 😎",
        f"_{_wib_now().strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not channels:
        lines.append("📺 Belum ada channel terhubung nih Bos 😅")
        return "\n".join(lines)
    for ch in channels[:8]:
        name = _channel_display_name(ch)
        stock = get_stock_days(ch)
        last = ch.get('last_scheduled_date', '-') or '-'
        counts, total_files, total_bytes = _count_gallery(ch.get('yt_id', ''))
        if stock is None:
            stock_txt = "belum ada jadwal"
        elif stock < 0:
            stock_txt = "⚠️ stok HABIS"
        elif stock <= 2:
            stock_txt = f"⚠️ sisa ~{max(0, int(stock))} hari"
        else:
            stock_txt = f"sisa ~{int(stock)} hari"
        lines.append(f"📺 *{_tg_escape(name)}* — stok: {stock_txt}")
        lines.append(f"  Jadwal terakhir: `{_tg_escape(last)}`")
        if total_files:
            parts = [f"{_GALLERY_LABELS[f]} {c}" for f, (c, _s) in counts.items() if c]
            lines.append(f"  Galeri: {', '.join(parts)} (total {total_files} file, {round(total_bytes / (1024 * 1024), 1)} MB)")
        else:
            lines.append("  Galeri: masih kosong — upload dulu dari panel ya Bos 😌")
        lines.append("")
    if len(channels) > 8:
        lines.append(f"_...dan {len(channels) - 8} channel lainnya._")
    lines.append("Bos, stok ≤2 hari itu tanda waktunya bikin batch baru 💪")
    return "\n".join(lines)


def build_history_report():
    """8 upload terakhir dari riwayat (tanggal, judul, status, channel)."""
    tasks_data = load_tasks()
    history = tasks_data.get('history', [])
    channels = load_channels()
    name_by_yt = {c.get('yt_id'): _channel_display_name(c) for c in channels}
    lines = [
        "🕘 *Riwayat Upload Terakhir Buat Bos* 😎",
        f"_{_wib_now().strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not history:
        lines.append("Belum ada riwayat Bos 😌 Video pertama yang tayang bakal muncul di sini.")
        return "\n".join(lines)
    ok = sum(1 for t in history if 'Tayang' in str(t.get('status', '')) or 'Selesai' in str(t.get('status', '')) or 'Sukses' in str(t.get('status', '')))
    fail = sum(1 for t in history if 'Gagal' in str(t.get('status', '')))
    lines.append(f"Total riwayat: `{len(history)} task` (✅ tayang/selesai `{ok}` • ❌ gagal `{fail}`)")
    lines.append("")
    cache = _load_video_stats_cache().get('videos', {})
    for t in history[:8]:
        date = _fmt_date_short(t.get('finished_date') or (t.get('publish_date') or '')[:10])
        title = (t.get('title') or '?')
        st = str(t.get('status', '-'))
        if 'Tayang' in st or 'Selesai' in st or 'Sukses' in st:
            icon = "✅"
        elif 'Gagal' in st:
            icon = "❌"
        elif 'Ditolak' in st:
            icon = "🚫"
        else:
            icon = "•"
        ch_name = name_by_yt.get(_history_task_yt_id(t), '')
        ch_txt = f" ({_tg_escape(ch_name)})" if ch_name else ""
        # 🔥 v1.2.22: satu baris ringkas — judul | views | tanggal | durasi.
        lines.append(f"{icon} {_video_meta(title, _video_views_of(t, cache), date, _task_duration_sec(t))}{ch_txt}")
    if len(history) > 8:
        lines.append(f"_...dan {len(history) - 8} riwayat lainnya (cek di panel)._")
    return "\n".join(lines)


def build_stats_report():
    """Views/likes/komentar 7/30 hari + video terbaik & terburuk per channel."""
    per_channel = _stats_by_channel()
    lines = [
        "📈 *Statistik Video Buat Bos* 😎",
        f"_{_wib_now().strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not per_channel:
        lines.append("Belum ada data statistik Bos 😌")
        lines.append("Statistik muncul setelah ada video yang tayang dan datanya sempat di-fetch (maks 1 jam sekali). Coba lagi nanti ya!")
        return "\n".join(lines)
    total_views = sum(e["views"] for e in per_channel.values())
    total_likes = sum(e["likes"] for e in per_channel.values())
    total_comments = sum(e["comments"] for e in per_channel.values())
    total_videos = sum(len(e["videos"]) for e in per_channel.values())
    lines.append(f"🎬 Total video tercatat: `{total_videos}` • 👁️ Views: `{_fmt_num(total_views)}` • 👍 Likes: `{_fmt_num(total_likes)}` • 💬 Komentar: `{_fmt_num(total_comments)}`")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    for yt, e in list(per_channel.items())[:8]:
        lines.append(f"📺 *{_tg_escape(e['name'])}* — `{len(e['videos'])} video`")
        lines.append(f"  👁️ {_fmt_num(e['views'])} views • 7 hari: {_fmt_num(e['views_7d'])} • 30 hari: {_fmt_num(e['views_30d'])}")
        lines.append(f"  👍 {_fmt_num(e['likes'])} • 💬 {_fmt_num(e['comments'])}")
        vids = sorted(e["videos"], key=lambda v: v["views"], reverse=True)
        if vids:
            best = vids[0]
            lines.append(f"  🏆 Terbaik: {_video_meta(best['title'], best['views'], _fmt_date_short(best.get('pub')), best.get('dur'))}")
            if len(vids) > 1:
                worst = vids[-1]
                lines.append(f"  📉 Terendah: {_video_meta(worst['title'], worst['views'], _fmt_date_short(worst.get('pub')), worst.get('dur'))}")
        lines.append("")
    if len(per_channel) > 8:
        lines.append(f"_...dan {len(per_channel) - 8} channel lainnya._")
    lines.append("Data dari cache statistik panel (diperbarui maks 1 jam sekali) 😌")
    return "\n".join(lines)


def build_monetization_report():
    """Estimasi penghasilan dari RPM MANUAL yang diisi owner di Settings.
    Ditulis jelas: ini BUKAN data penghasilan YouTube — YouTube Data API
    tidak memberi data uang. Tanpa RPM, tidak ada estimasi sama sekali."""
    per_channel = _stats_by_channel()
    channels = load_channels()
    rpm_by_yt = {}
    for c in channels:
        try:
            rpm = float(c.get('rpm_usd') or 0)
        except Exception:
            rpm = 0.0
        if rpm > 0:
            rpm_by_yt[c.get('yt_id')] = rpm
    lines = [
        "💰 *Estimasi Penghasilan Buat Bos* 😎",
        f"_{_wib_now().strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not per_channel:
        lines.append("Belum ada video tayang yang tercatat Bos 😌")
        lines.append("Estimasi muncul setelah ada video tayang + RPM diisi di Settings.")
        return "\n".join(lines)
    if not rpm_by_yt:
        lines.append("⚠️ *Belum ada RPM yang diisi Bos* 😅")
        lines.append("Isi dulu RPM (USD per 1000 views) tiap channel di Settings panel, baru saya bisa hitung estimasinya.")
        total_views = sum(e["views"] for e in per_channel.values())
        lines.append(f"Total views tercatat sejauh ini: `{_fmt_num(total_views)}` — lumayan Bos 💪")
        return "\n".join(lines)
    grand_total = 0.0
    grand_30d = 0.0
    for yt, e in list(per_channel.items())[:8]:
        rpm = rpm_by_yt.get(yt)
        lines.append(f"📺 *{_tg_escape(e['name'])}* — `{len(e['videos'])} video`, {_fmt_num(e['views'])} views")
        if not rpm:
            lines.append("  RPM belum diisi — isi di Settings biar ada estimasinya 😌")
            continue
        est_total = e["views"] / 1000.0 * rpm
        est_30d = e["views_30d"] / 1000.0 * rpm
        grand_total += est_total
        grand_30d += est_30d
        lines.append(f"  RPM: `${rpm}` /1000 views • Jam tayang: `{round(e['hours'], 1)} jam`")
        lines.append(f"  💵 Estimasi total: `${est_total:,.2f}` • 30 hari terakhir: `${est_30d:,.2f}`")
        lines.append("")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"💵 *Total estimasi semua channel: `${grand_total:,.2f}`* (30 hari: `${grand_30d:,.2f}`)")
    lines.append("_Estimasi dari RPM MANUAL yang Bos isi di Settings — bukan data penghasilan dari YouTube (API YouTube tidak memberi data uang)._")
    return "\n".join(lines)


def build_server_report():
    """Detail mesin: CPU/RAM/disk/uptime + status panel + beban kerja."""
    stats = get_system_telemetry()
    panel_ok = check_panel_online()
    tasks_data = load_tasks()
    active = tasks_data.get('active', [])
    history = tasks_data.get('history', [])
    channels = load_channels()
    try:
        total, used, free = shutil.disk_usage(BASE_DIR)
        disk_total = round(total / (1024 ** 3), 1)
        disk_used = round(used / (1024 ** 3), 1)
    except Exception:
        disk_total = disk_used = 0
    try:
        uptime_sys = _uptime_str(time.time() - psutil.boot_time())
    except Exception:
        uptime_sys = "-"
    uptime_op = _uptime_str(time.time() - _START_TIME)
    in_progress = len([t for t in active if 'Selesai' not in t.get('status', '')
                       and 'Gagal' not in t.get('status', '')
                       and 'Dibatalkan' not in t.get('status', '')])
    lines = [
        "🖥️ *Detail Server Buat Bos* 😎",
        f"_{_wib_now().strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🖥️ *Panel Web (8000):* {'🟢 Online' if panel_ok else '🔴 Mati/tidak menjawab'}",
        f"📌 *Versi Panel:* `{_panel_version()}`",
        "🤖 *Operator Telegram:* 🟢 Online",
        f"⏱️ *Uptime server:* `{uptime_sys}` • Operator jalan: `{uptime_op}`",
        f"⚡ *CPU:* `{stats['cpu']}%`",
        f"🧠 *RAM:* `{stats['ram_used_gb']} GB / {stats['ram_total_gb']} GB ({stats['ram_pct']}%)`",
        f"💾 *Disk:* `{disk_used} GB / {disk_total} GB` terpakai (sisa `{stats['disk_free_gb']} GB`)",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"📺 *Channel terhubung:* `{len(channels)}`",
        f"🏭 *Antrean aktif:* `{len(active)} task` (`{in_progress}` sedang berjalan/menunggu)",
        f"📁 *Riwayat:* `{len(history)} task`",
    ]
    if stats['ram_pct'] >= 90 or stats['disk_free_gb'] < 5:
        lines.append("⚠️ Bos, resource udah mepet nih — kabari saya kalau mau dibersihin 😅")
    else:
        lines.append("Mesin sehat Bos, saya yang jagain 💪")
    return "\n".join(lines)


def build_failed_report():
    """Task gagal terbaru dari riwayat + tombol inline 🔁 Retry per task.
    Retry memakai callback 'retry:<id>' yang sudah ditangani
    handle_callback_query — endpoint panel menolak task yang ditolak saat
    approval / sudah sukses / tanpa blueprint. Kembalikan (pesan, markup)."""
    tasks_data = load_tasks()
    history = tasks_data.get('history', [])
    active = tasks_data.get('active', [])
    failed_hist = [t for t in history if 'Gagal' in str(t.get('status', ''))]
    failed_active = [t for t in active if 'Gagal' in str(t.get('status', ''))]
    lines = [
        "❌ *Task Gagal Buat Bos* 😅",
        f"_{_wib_now().strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    retryable = []
    if not failed_hist and not failed_active:
        lines.append("Nggak ada yang gagal Bos, semua mulus 💪")
        return "\n".join(lines), None
    cache = _load_video_stats_cache().get('videos', {})
    if failed_hist:
        lines.append(f"Di riwayat: `{len(failed_hist)} task` gagal.")
        for t in failed_hist[:5]:
            title = (t.get('title') or '?')
            st = str(t.get('status', ''))[:60]
            date = _fmt_date_short(t.get('finished_date') or '')
            lines.append(f"• {_video_meta(title, _video_views_of(t, cache), date, _task_duration_sec(t))}")
            lines.append(f"  Status: {_tg_escape(st)}")
            if t.get('blueprint') and t.get('approval') != 'rejected' and isinstance(t.get('id'), int):
                retryable.append(t)
        if len(failed_hist) > 5:
            lines.append(f"_...dan {len(failed_hist) - 5} task gagal lainnya._")
    if failed_active:
        lines.append(f"Di antrean aktif: `{len(failed_active)} task` berstatus gagal (masih diproses ulang sistem).")
    lines.append("")
    if retryable:
        lines.append("Pencet tombol 🔁 di bawah buat coba lagi task-nya ya Bos 👇")
    else:
        lines.append("Tidak ada task yang bisa di-retry dari sini Bos 😌")
    markup = None
    if retryable:
        markup = {"inline_keyboard": [
            [{"text": f"🔁 Retry #{t['id']} — {(t.get('title') or '?')[:18]}",
              "callback_data": f"retry:{t['id']}"}] for t in retryable[:5]
        ]}
    return "\n".join(lines), markup


# ==========================================
# 🔥 FASE 3 — JADWAL PUBLISH (/jadwal)
# ==========================================
_DAYS_ID = ['Senin', 'Selasa', 'Rabu', 'Kamis', 'Jumat', 'Sabtu', 'Minggu']
_MONTHS_ID = ['Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu', 'Sep', 'Okt', 'Nov', 'Des']

def _parse_schedule_dt(s):
    """Parse 'YYYY-MM-DD HH:MM' (waktu WIB naive) jadi datetime aware WIB.
    None kalau kosong/rusak."""
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:16], "%Y-%m-%d %H:%M").replace(tzinfo=ZoneInfo("Asia/Jakarta"))
    except Exception:
        return None

def _task_schedule_label(t, ch_paused=False):
    """Label status satu baris jadwal: approval dulu, lalu Dijeda, lalu status."""
    if t.get('approval') == 'pending':
        return "⏳ Menunggu Approval"
    st = str(t.get('status', ''))
    if 'Dijeda' in st or ch_paused:
        return "⏸️ Dijeda"
    if 'Ditolak' in st:
        return "❌ Ditolak"
    if 'Gagal' in st:
        return "❌ Gagal"
    if 'Selesai' in st or 'Sukses' in st or 'Tayang' in st:
        return "✅ Selesai"
    return "🗓️ Terjadwal"

# ==========================================
# 🔥 v1.2.16 — PENGINGAT APPROVAL (user sering lupa tekan ✅)
# ==========================================
def _pending_approval_tasks(active):
    """Task aktif yang masih menunggu approval Bos, urut jam tayang
    (yang tanpa jadwal di belakang). Kembalikan [(dt, task), ...]."""
    far = datetime(2999, 1, 1, tzinfo=ZoneInfo("Asia/Jakarta"))
    out = []
    for t in active or []:
        if t.get('approval') != 'pending':
            continue
        if 'Ditolak' in str(t.get('status', '')):
            continue
        dt = _parse_schedule_dt(t.get('time') or (t.get('blueprint') or {}).get('publish_date') or '')
        out.append((dt, t))
    out.sort(key=lambda x: (x[0] is None, x[0] or far))
    return out

def _task_channel_name(t, channels):
    """Nama channel pemilik task (cocokkan yt_id); 'Channel tidak dikenal'
    bila tidak ketemu — jangan ditebak."""
    yt = t.get('yt_id') or (t.get('blueprint') or {}).get('yt_id') or ''
    for ch in channels or []:
        if ch.get('yt_id') == yt:
            return _channel_display_name(ch)
    return 'Channel tidak dikenal'

def _fmt_approval_delta(dt, now):
    """Jarak jam tayang dari sekarang, bahasa sehari-hari:
    'tinggal 30 menit lagi' / 'sudah lewat 2 jam 15 menit'."""
    secs = int((dt - now).total_seconds())
    if secs >= 0:
        mins = secs // 60
        if mins < 1:
            return "tayang sebentar lagi"
        if mins < 60:
            return f"tinggal {mins} menit lagi"
        h, m = divmod(mins, 60)
        return f"tinggal {h} jam {m} menit lagi" if m else f"tinggal {h} jam lagi"
    mins = (-secs) // 60
    if mins < 1:
        return "baru saja lewat"
    if mins < 60:
        return f"sudah lewat {mins} menit"
    h, m = divmod(mins, 60)
    return f"sudah lewat {h} jam {m} menit" if m else f"sudah lewat {h} jam"

def _fmt_schedule_id(dt):
    """'Rabu, 19 Nov 2026 09:00' (WIB)."""
    return f"{_DAYS_ID[dt.weekday()]}, {dt.day} {_MONTHS_ID[dt.month - 1]} {dt.year} {dt.strftime('%H:%M')}"

def _pending_approval_lines(active, channels, limit=5):
    """Baris '⏳ Menunggu Approval (N)' untuk /status & laporan harian.
    [] bila tidak ada yang pending — pemanggil tidak menampilkan bagian."""
    pend = _pending_approval_tasks(active)
    if not pend:
        return []
    now = _wib_now()
    lines = [f"⏳ *Menunggu Approval ({len(pend)}):*"]
    for dt, t in pend[:limit]:
        name = _task_channel_name(t, channels)
        title = (t.get('title') or '?')[:40]
        if dt is None:
            sched = "jadwal: —"
        else:
            sched = f"tayang {dt.strftime('%d %b %H:%M')} ({_fmt_approval_delta(dt, now)})"
        lines.append(f"• *{_tg_escape(name)}* — {_tg_escape(title)} — {sched} | Durasi {_fmt_dur_sec(_task_duration_sec(t))}")
    if len(pend) > limit:
        lines.append(f"_...dan {len(pend) - limit} video lainnya — ketuk ✅ di pesan preview-nya ya Bos._")
    return lines

def _approval_reminder_check(token, chat_id):
    """Ingatkan Bos bila ada task yang jam tayangnya sudah ≤60 menit lagi
    ('soon') atau SUDAH LEWAT ('late') tapi approval-nya masih pending.
    Tiap ambang dikirim SEKALI per task (maks 2 pengingat per task) —
    dicatat di monitor_state['approval_reminders'] dan dicatat HANYA
    setelah pesan benar-benar terkirim (res 'ok'), supaya bila Telegram
    down pengingat diulang di tick berikutnya, bukan hilang. Tombol ✅/❌
    memakai callback_data yang SAMA PERSIS dengan pesan preview
    (approve:<id> / reject:<id>) sehingga ketukannya bekerja identik.
    Dipanggil dari watchdog_background_loop tiap 5 menit."""
    tasks_data = load_tasks()
    active = tasks_data.get('active', [])
    channels = load_channels()
    pend = _pending_approval_tasks(active)
    if not pend:
        return
    state = load_monitor_state()
    reminded = state.setdefault('approval_reminders', {})
    # Bersihkan catatan task yang sudah tidak pending lagi (disetujui /
    # ditolak / render) supaya state tidak membesar tanpa batas.
    pend_ids = set()
    for _dt, t in pend:
        try:
            pend_ids.add(str(int(t.get('id'))))
        except Exception:
            pass
    for key in list(reminded):
        if key not in pend_ids:
            reminded.pop(key, None)
    now = _wib_now()
    sent = 0
    for dt, t in pend:
        if sent >= 5:  # pengaman: maks 5 pesan pengingat per tick
            break
        if dt is None:
            continue
        try:
            tid = int(t.get('id'))
        except Exception:
            continue
        mins_left = (dt - now).total_seconds() / 60.0
        if mins_left > 60:
            continue
        threshold = 'late' if mins_left <= 0 else 'soon'
        done = reminded.setdefault(str(tid), [])
        if threshold in done:
            continue
        name = _task_channel_name(t, channels)
        title = (t.get('title') or '?')[:60]
        when = f"{_fmt_schedule_id(dt)} — {_fmt_approval_delta(dt, now)}"
        if threshold == 'late':
            head = "⏰ *Bos, video ini kelewat jam tayangnya tapi masih nunggu approval nih* 😅"
        else:
            head = "⏰ *Bos, video ini bentar lagi tayang tapi masih nunggu approval nih* 😅"
        text = (
            f"{head}\n\n"
            f"📺 Channel: *{_tg_escape(name)}*\n"
            f"🎬 Judul: *{_tg_escape(title)}*\n"
            f"📅 Jadwal tayang: *{when}*\n\n"
            "Ketuk ✅ di pesan ini untuk menyetujui, atau ❌ kalau mau ditolak — "
            "videonya belum jalan sebelum Bos setujui 🙏"
        )
        res = send_message(token, chat_id, text, reply_markup={"inline_keyboard": [[
            {"text": "✅ Setujui", "callback_data": f"approve:{tid}"},
            {"text": "❌ Tolak", "callback_data": f"reject:{tid}"},
        ]]})
        if res and res.get('ok'):
            done.append(threshold)
            save_monitor_state(state)
            sent += 1
            print(f"⏰ Pengingat approval terkirim: task #{tid} ({threshold}).", flush=True)
        else:
            print(f"⏰ Pengingat approval GAGAL terkirim: task #{tid} ({threshold}) — coba lagi tick berikutnya.", flush=True)

# v1.3.16: tombol tawaran saat jadwal 7 hari KOSONG (total 0) — Bos minta
# ada opsi, bukan cuma "cek /status".
_EMPTY_SCHEDULE_KB = {
    "inline_keyboard": [
        [{"text": "🎵 Buat Playlist", "callback_data": "jadwal:buat_playlist"}],
        [{"text": "📅 Perpanjang Jadwal 30 Hari", "callback_data": "jadwal:perpanjang30"}],
    ]
}


def build_schedule_report(days=7):
    """Jadwal publish N hari ke depan per channel (untuk /jadwal).
    Sumber: tasks_db.json yang sudah ada — tidak ada jadwal yang dikarang.
    Task aktif yang tanggal tayangnya sudah lewat ikut tampil sebagai Terlambat."""
    tasks_data = load_tasks()
    active = tasks_data.get('active', [])
    channels = load_channels()
    now = _wib_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)

    def _task_yt(t):
        return t.get('yt_id') or (t.get('blueprint') or {}).get('yt_id') or ''

    def _task_dt(t):
        return _parse_schedule_dt(t.get('time') or (t.get('blueprint') or {}).get('publish_date') or '')

    lines = [
        "🗓️ *Jadwal Bos 7 hari ke depan* 👀",
        f"_{now.strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not channels:
        lines.append("📺 Belum ada channel terhubung nih Bos 😅")
        lines.append("Tautkan dulu dari panel, baru jadwal bisa ditampilkan.")
        return "\n".join(lines)

    upcoming, overdue = [], []
    for t in active:
        dt = _task_dt(t)
        if dt is None:
            continue
        if dt < now:
            overdue.append((dt, t))
        elif dt <= end:
            upcoming.append((dt, t))
    upcoming.sort(key=lambda x: x[0])
    overdue.sort(key=lambda x: x[0])

    known_ids = {c.get('yt_id') for c in channels}
    total = 0
    for ch in channels:
        name = _channel_display_name(ch)
        yt = ch.get('yt_id', '')
        paused = bool(ch.get('paused'))
        items = [(dt, t) for dt, t in upcoming if _task_yt(t) == yt]
        over = [(dt, t) for dt, t in overdue if _task_yt(t) == yt]
        total += len(items) + len(over)
        lines.append(f"📺 *{_tg_escape(name)}*" + (" — ⏸️ *DIJEDA*" if paused else ""))
        if over:
            lines.append(f"  ⚠️ Terlambat: `{len(over)} task` belum tayang")
            for dt, t in over[:3]:
                lines.append(f"  • {_video_meta((t.get('title') or '?'), None, _fmt_dt_short(dt), _task_duration_sec(t))}")
        if not items and not over:
            lines.append("  _Nggak ada jadwal 7 hari ke depan Bos 😌_")
        by_day = {}
        for dt, t in items:
            by_day.setdefault(dt.strftime('%Y-%m-%d'), []).append((dt, t))
        for day in sorted(by_day):
            day_dt = datetime.strptime(day, '%Y-%m-%d')
            day_lbl = f"{_DAYS_ID[day_dt.weekday()]}, {day_dt.day} {_MONTHS_ID[day_dt.month - 1]} {day_dt.year}"
            lines.append(f"  📅 *{day_lbl}*")
            for dt, t in sorted(by_day[day], key=lambda x: x[0]):
                label = _task_schedule_label(t, paused)
                lines.append(f"  • {_video_meta((t.get('title') or '?'), None, _fmt_dt_short(dt), _task_duration_sec(t))} — {label}")
        lines.append("")
    # Task yang channel-nya tidak dikenal (mis. channel baru dihapus).
    unknown = [(dt, t) for dt, t in upcoming + overdue if _task_yt(t) not in known_ids]
    if unknown:
        total += len(unknown)
        lines.append("📺 *Channel Tidak Dikenal*")
        for dt, t in sorted(unknown, key=lambda x: x[0])[:5]:
            label = _task_schedule_label(t)
            lines.append(f"  • {_video_meta((t.get('title') or '?'), None, _fmt_dt_short(dt), _task_duration_sec(t))} — {label}")
        lines.append("")
    lines.append(f"Total: `{total} task` dalam 7 hari ke depan (termasuk yang terlambat).")
    if total == 0:
        # v1.3.16: jadwal kosong -> tawarkan isi (tombol inline di call site),
        # bukan cuma "cek /status".
        lines.append("Jadwal masih kosong semua nih Bos — mau saya isikan? Pencet di bawah ya 👇")
    else:
        lines.append("Bos, ketik /status buat cek kuota & antrean terkini 😎")
    return "\n".join(lines), total


# ==========================================
# 🔥 v1.2.20 — FITUR PENDUKUNG TELEGRAM
# /jadwalbulan, /laporan detail, /channel (baca saja, tidak mengubah data).
# FORMAT (sesuai permintaan user 13:31 WIB — "tampilan database yang rapi"):
# semua builder di sini memakai format yang SAMA dengan laporan lama:
# judul bagian tebal + stempel waktu, pemisah ━━━, blok per channel
# "📺 *Nama*" + baris berindentasi 3 spasi, angka lewat _fmt_num,
# tanggal "Senin, 19 Nov" / "19 Nov 19:00". Data dari database yang SUDAH
# ADA (channels_db.json, tasks_db.json, cache channel_stats) — TIDAK ADA
# file database baru.
# ==========================================
def _split_long_message(text, limit=3800):
    """Pecah pesan panjang per baris supaya tiap kiriman ≤ limit karakter
    (batas Telegram 4096 — sisakan ruang untuk aman)."""
    lines = str(text or '').split("\n")
    chunks, cur = [], ""
    for ln in lines:
        if cur and len(cur) + 1 + len(ln) > limit:
            chunks.append(cur)
            cur = ln
        else:
            cur = (cur + "\n" + ln) if cur else ln
    if cur:
        chunks.append(cur)
    return chunks or [""]


def build_month_schedule_blocks(channel_arg=None, days=30):
    """Jadwal 30 hari ke depan — kembalikan LIST pesan (1 pesan per channel).
    Format hari SAMA dengan /jadwal: `  📅 *Hari, Tgl*` + `  • `HH:MM`
    Judul — label`. Hari terisi tampil judul + jam; hari kosong digabung
    jadi rentang ('21–24 Nov — kosong'). Hari yang sudah lewat (terlambat)
    ditandai ⚠️. Stok jadwal tersisa di akhir tiap channel."""
    tasks_data = load_tasks()
    active = tasks_data.get('active', [])
    channels = load_channels()
    now = _wib_now()
    start = now.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=days)

    def _task_yt(t):
        return t.get('yt_id') or (t.get('blueprint') or {}).get('yt_id') or ''

    def _task_dt(t):
        return _parse_schedule_dt(t.get('time') or (t.get('blueprint') or {}).get('publish_date') or '')

    def _day_label(dt):
        return f"{_DAYS_ID[dt.weekday()]}, {dt.day} {_MONTHS_ID[dt.month - 1]}"

    if channel_arg:
        matched, _all = _nlu_match_channels(_nlu_clean(channel_arg), channels)
        if matched:
            channels = matched
        else:
            return [f"😅 *Saya nggak nemu channel '{_tg_escape(channel_arg)}' Bos* — yang terdaftar: "
                    + ", ".join(_channel_display_name(c) for c in load_channels()) + "."]
    if not channels:
        return ["📺 Belum ada channel terhubung nih Bos 😅\nTautkan dulu dari panel, baru jadwal bisa ditampilkan."]
    blocks = []
    for ch in channels:
        name = _channel_display_name(ch)
        yt = ch.get('yt_id', '')
        paused = bool(ch.get('paused'))
        by_day = {}
        overdue = []
        for t in active:
            if _task_yt(t) != yt:
                continue
            dt = _task_dt(t)
            if dt is None:
                continue
            if dt < now:
                overdue.append((dt, t))
            elif dt <= end:
                by_day.setdefault(dt.strftime('%Y-%m-%d'), []).append((dt, t))
        lines = [
            f"🗓️ *Jadwal 30 Hari — {_tg_escape(name)}* 👀" + (" — ⏸️ *DIJEDA*" if paused else ""),
            f"_{start.strftime('%d %b')} – {end.strftime('%d %b %Y')} WIB_",
            "━━━━━━━━━━━━━━━━━━━━━━",
        ]
        if overdue:
            lines.append(f"⚠️ *Terlambat: `{len(overdue)} task` belum tayang*")
            for dt, t in sorted(overdue, key=lambda x: x[0])[:3]:
                lines.append(f"  • {_video_meta((t.get('title') or '?'), None, _fmt_dt_short(dt), _task_duration_sec(t))}")
        filled = 0
        gap_start = None
        gap_len = 0

        def _flush_gap(last_gap_day):
            nonlocal gap_start, gap_len
            if gap_start is None:
                return
            if gap_len == 1:
                lines.append(f"  📅 *{_day_label(gap_start)}* — _kosong_")
            else:
                lines.append(f"  📅 *{_day_label(gap_start)} – {_day_label(last_gap_day)}* — _kosong ({gap_len} hari)_")
            gap_start = None
            gap_len = 0

        for i in range(days):
            day = start + timedelta(days=i)
            items = by_day.get(day.strftime('%Y-%m-%d'))
            if items:
                _flush_gap(day - timedelta(days=1))
                filled += 1
                lines.append(f"  📅 *{_day_label(day)}*")
                for dt, t in sorted(items, key=lambda x: x[0]):
                    label = _task_schedule_label(t, paused)
                    lines.append(f"  • {_video_meta((t.get('title') or '?'), None, _fmt_dt_short(dt), _task_duration_sec(t))} — {label}")
            else:
                if gap_start is None:
                    gap_start = day
                gap_len += 1
        _flush_gap(end - timedelta(days=1))
        stock = get_stock_days(ch)
        last = ch.get('last_scheduled_date', '-') or '-'
        if stock is None:
            stock_txt = "belum ada jadwal"
        elif stock < 0:
            stock_txt = "⚠️ stok HABIS"
        else:
            stock_txt = f"sisa ~{int(stock)} hari"
        lines.append("━━━━━━━━━━━━━━━━━━━━━━")
        lines.append(f"📦 Stok jadwal: {stock_txt} (terakhir: `{_tg_escape(last)}`) • `{filled}/{days} hari terisi`")
        blocks.append("\n".join(lines))
    return blocks


def _channel_monet_compact(st, cache, history, today_str):
    """Satu baris progres monetisasi: 'Subs 12/1.000 • ~123 jam/8.000 jam
    (est.)'. '—' bila data belum bisa dihitung. Memakai helper yang sama
    dengan _build_channel_stats_section (snapshot 90 hari + durasi
    rata-rata video long)."""
    try:
        rule = _monetization_rule_for(today_str)
        if st.get("monetized"):
            return "💰 Monetisasi Aktif"
        subs = int(st.get("subs") or 0)
        yt_id = st.get("yt_id")
        ch_snaps = cache.get('channel_snapshots') or []
        ch_totals = {}
        for s in ch_snaps:
            if s.get('yt_id') == yt_id:
                ch_totals[s.get('date')] = int(s.get('views', 0) or 0)
        v90, _, _ = _snapshot_delta(ch_totals, 90)
        avg_h = _avg_long_duration_hours(history, yt_id)
        watch90 = round(v90 * avg_h, 1) if (v90 is not None and avg_h) else None
        cached_videos = cache.get('videos', {}) or {}
        vid_snaps = {}
        for s in cache.get('snapshots', []) or []:
            vid_snaps.setdefault(s.get('video_id'), {})[s.get('date')] = int(s.get('views', 0) or 0)
        ref_date = max(ch_totals) if ch_totals else today_str
        shorts_v, shorts_ok = _shorts_views_90d(yt_id, history, cached_videos, vid_snaps, ref_date)
        ready = _channel_monetization_readiness(subs=subs, watch_90d_est_hours=watch90,
                                                shorts_views_90d=shorts_v, shorts_complete=shorts_ok,
                                                monetized=False, today_str=today_str)
        if ready["status"] == "ready":
            via = "jam tayang" if ready.get("via") == "hours" else "views Shorts"
            return f"💰 ✅ Siap Monet (est.) — lewat {via}"
        watch_txt = f"~{_fmt_num(round(watch90))} jam" if watch90 is not None else "jam tayang —"
        return (f"💰 Subs {_fmt_num(subs)}/{_fmt_num(rule['subs'])} • "
                f"{watch_txt}/{_fmt_num(rule['watch_hours'])} jam (est.)")
    except Exception:
        return "💰 Monetisasi: —"


def build_channel_list_report():
    """Daftar semua channel tertaut — 'tampilan database channel' yang rapi:
    nama, subs, videos, mode, dijeda, stok jadwal, progres monetisasi.
    Format blok per channel SAMA dengan laporan lain: '📺 *Nama*' +
    baris berindentasi 3 spasi. Untuk /channel (dan NLU 'list channel')."""
    channels = load_channels()
    now = _wib_now()
    lines = [
        "📺 *Channel Buat Bos* 😎",
        f"_{now.strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    if not channels:
        lines.append("Belum ada channel yang tertaut, Bos 😌")
        lines.append("Tautkan dulu dari panel (Settings → ➕ Tambah Channel).")
        return "\n".join(lines)
    stats = _get_report_channel_stats(channels)
    cache = _load_video_stats_cache()
    cache_videos = cache.get('videos', {})
    history = load_tasks().get('history', [])
    today_str = _wib_today_str()
    # 🔥 v1.2.22: video terbaru per channel (dari riwayat) untuk baris
    # 'Terbaru' — judul | views | tanggal | durasi.
    latest_by_yt = {}
    for t in history:
        yt = _history_task_yt_id(t)
        if not yt:
            continue
        key = (t.get('finished_date') or (t.get('publish_date') or '')[:10] or '')
        if yt not in latest_by_yt or key > latest_by_yt[yt][0]:
            latest_by_yt[yt] = (key, t)
    for i, (ch, st) in enumerate(zip(channels, stats)):
        name = _channel_display_name(ch)
        mode = 'Full-Auto 🤖' if _autopilot_of(ch) == 'auto' else 'Approval 👀'
        paused = " ⏸️ *DIJEDA*" if ch.get('paused') else ""
        lines.append(f"📺 *{_tg_escape(name)}*{paused}")
        if st["ok"]:
            lines.append(f"   👥 {_fmt_num(int(st['subs'] or 0))} subs • 📹 {_fmt_num(int(st['videos'] or 0))} video • 👁️ {_fmt_num(int(st['views']))} views")
        else:
            lines.append("   👥 subs — • 📹 video — • 👁️ views — (data belum bisa diambil)")
        lines.append(f"   Mode: {mode}")
        lt = latest_by_yt.get(ch.get('yt_id'))
        if lt:
            _lt = lt[1]
            lines.append(f"   🎬 Terbaru: {_video_meta(_lt.get('title'), _video_views_of(_lt, cache_videos), _fmt_date_short(_lt.get('finished_date') or (_lt.get('publish_date') or '')[:10]), _task_duration_sec(_lt))}")
        else:
            lines.append("   🎬 Terbaru: —")
        stock = get_stock_days(ch)
        last = ch.get('last_scheduled_date', '-') or '-'
        if stock is None:
            stock_txt = "belum ada jadwal"
        elif stock < 0:
            stock_txt = "⚠️ stok HABIS"
        elif stock <= 2:
            stock_txt = f"⚠️ sisa ~{max(0, int(stock))} hari"
        else:
            stock_txt = f"sisa ~{int(stock)} hari"
        lines.append(f"   📦 Stok: {stock_txt} (terakhir: `{_tg_escape(last)}`)")
        lines.append(f"   {_channel_monet_compact(st, cache, history, today_str)}")
        if i < len(channels) - 1:
            lines.append(TG_CHANNEL_DIVIDER)
        lines.append("")
    lines.append(f"Total: `{len(channels)} channel` tertaut.")
    return "\n".join(lines)


def build_detail_report():
    """Laporan terperinci (/laporan detail): per channel views/subs/videos +
    delta, 3 video terbaik, upload hari ini, kuota project x/100, gagal hari
    ini, progres monetisasi. Format blok per channel SAMA dengan /channel.
    /laporan biasa TIDAK berubah. Kembalikan (teks, new_snapshot) seperti
    build_daily_report — pemanggil menyimpan snapshot SETELAH pesan terkirim."""
    tasks_data = load_tasks()
    active = tasks_data.get('active', [])
    history = tasks_data.get('history', [])
    channels = load_channels()
    now = _wib_now()
    today = _wib_today_str()
    ok_today, fail_today, _ok_t, fail_titles = _count_history_for_date(history, today)
    cache = _load_video_stats_cache()
    stats = _get_report_channel_stats(channels)
    prev = load_monitor_state().get("channel_report_snapshot") or {}
    new_snapshot = {}
    per_ch = _stats_by_channel()
    lines = [
        "🔍 *LAPORAN DETAIL BUAT BOS* 🔍",
        f"_{now.strftime('%d %b %Y, %H:%M')} WIB_",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"✅ *Upload sukses hari ini:* `{ok_today}`",
        f"❌ *Gagal hari ini:* `{fail_today}`",
    ]
    if fail_titles:
        lines.append("Gagal: " + "; ".join("• " + _tg_escape(t) for t in fail_titles[:5]))
    projects = {}
    for ch in channels:
        pid = _project_id_of_channel(ch) or 'tidak-diketahui'
        e = projects.setdefault(pid, {"used": 0})
        du = ch.get('daily_uploads') or {}
        if du.get('date') == today:
            e["used"] += int(du.get('count', 0))
    for pid in sorted(projects):
        used = projects[pid]["used"]
        lines.append(f"🎯 Kuota project `{_tg_escape(pid)}`: `{used}/{MAX_UPLOADS_PER_DAY}` (sisa `{max(0, MAX_UPLOADS_PER_DAY - used)}`)")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    if not channels:
        lines.append("📺 Belum ada channel terhubung.")
    for i, st in enumerate(stats[:8]):
        name = st["name"]
        lines.append(f"📺 *{_tg_escape(name)}*")
        if not st["ok"]:
            lines.append("   Data belum bisa diambil 😌 (coba lagi nanti)")
            lines.append("")
            continue
        views, subs, videos = int(st["views"]), int(st["subs"] or 0), int(st["videos"] or 0)
        new_snapshot[st["yt_id"]] = {"views": views, "subs": subs, "videos": videos,
                                     "at": now.strftime("%Y-%m-%d %H:%M")}
        lines.append(f"   👁️ {_fmt_num(views)} views • 👥 {_fmt_num(subs)} subs • 📹 {_fmt_num(videos)} video")
        base = prev.get(st["yt_id"]) or {}
        if base.get('views') is not None and base.get('subs') is not None:
            dv = views - int(base['views'])
            ds = subs - int(base['subs'])
            when = _fmt_snapshot_time(base.get('at', ''))
            when_txt = f" ({when})" if when else ""
            lines.append(f"   📈 Sejak laporan terakhir{when_txt}: 👁️ {'+' if dv >= 0 else ''}{_fmt_num(dv)} • 👥 {'+' if ds >= 0 else ''}{_fmt_num(ds)}")
        lines.append(f"   {_channel_monet_compact(st, cache, history, today)}")
        top = sorted((per_ch.get(st["yt_id"]) or {}).get("videos", []),
                     key=lambda v: v.get("views", 0), reverse=True)[:3]
        if top:
            lines.append("   🏆 *3 video terbaik:*")
            for j, v in enumerate(top, 1):
                lines.append(f"      {j}. {_video_meta(v.get('title'), v.get('views'), _fmt_date_short(v.get('pub')), v.get('dur'))}")
        else:
            lines.append("   🏆 Video terbaik: belum ada data (video tayang belum tercatat di statistik).")
        if i < len(stats[:8]) - 1:
            lines.append(TG_CHANNEL_DIVIDER)
        lines.append("")
    if len(stats) > 8:
        lines.append(f"_...dan {len(stats) - 8} channel lainnya (cek di panel)._")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append(f"🏭 *Antrean tersisa:* `{len(active)} task`")
    _pend_lines = _pending_approval_lines(active, channels, limit=5)
    if _pend_lines:
        lines += _pend_lines
    lines.append("Bos, ketik /laporan buat ringkasan harian biasa 😎")
    return "\n".join(lines), new_snapshot


def _tg_escape(text):
    """Escape karakter Markdown Telegram supaya nama channel / judul video
    (yang bisa mengandung *, _, `, [ dll.) tidak merusak format pesan.
    Hanya bagian DATA yang di-escape — template pesan yang memang memakai
    *tebal* / `kode` tetap ditulis langsung. (#23)
    PENTING: pesan dikirim dengan parse_mode 'Markdown' (LEGACY), BUKAN
    'MarkdownV2' — di mode legacy HANYA \\, *, _, `, [ yang butuh
    backslash. Dulu memakai set MarkdownV2 (termasuk -, ., !) sehingga
    backslash justru TAMPIL di pesan, mis. 'jelek\\-jelek' (glitch
    2026-11-19). Karakter lain (-, ., !, dsb.) dibiarkan apa adanya."""
    text = str(text or '')
    text = text.replace('\\', '\\\\')
    for ch in ['*', '_', '`', '[']:
        text = text.replace(ch, '\\' + ch)
    return text


def _log_safe_text(text):
    """Teks aman untuk log stdout: API key Pixabay yang diketik user
    (perintah /pixabaykey) dan API key Kie.ai (perintah /kiekey) TIDAK
    PERNAH tercatat utuh di log."""
    text = str(text or '')
    if text.strip().lower().startswith('/pixabaykey'):
        return '/pixabaykey ****'
    if text.strip().lower().startswith('/kiekey'):
        return '/kiekey ****'
    return text


def get_main_keyboard():
    """Keyboard menu utama (🔥 v1.3.3): 14 tombol BERIKON — ikon adalah
    chrome UI menu, bukan isi pesan (hapus emoji itu aturan untuk JUDUL
    VIDEO YOUTUBE v1.2.13; teks pesan Telegram tetap polos — keputusan
    terpisah yang dipertahankan). one_time_keyboard supaya keyboard
    minimize setelah dipakai (tidak memenuhi setengah layar terus);
    TANPA is_persistent."""
    return {
        "keyboard": [
            [{"text": "🎵 Buat Playlist"}, {"text": "🎬 Ide Shorts"}],
            [{"text": "📱 Buat Short"}, {"text": "🎥 Buat Video Long"}],
            [{"text": "💾 Backup"}, {"text": "📋 Laporan"}],
            [{"text": "📊 Status Server"}, {"text": "📤 Kuota Upload"}],
            [{"text": "📺 Cek Channel"}, {"text": "📂 Antrean Render"}],
            [{"text": "📅 Jadwal 7 Hari"}, {"text": "📈 Statistik Video"}],
            [{"text": "💰 Monetisasi"}, {"text": "❌ Task Gagal"}]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": True
    }

def send_message(token, chat_id, text, parse_mode="Markdown", reply_markup=None):
    # Emoji BOLEH di chat Telegram (aturan user 20 Nov 2026) — yang dilarang
    # hanya emoji di judul/deskripsi video YouTube (sanitasi upload v1.2.13).
    # Pengaman batas 4096 karakter Telegram — pesan kepanjangan dipotong rapi.
    text = _clip(str(text or ''))
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    # 🔥 v1.3.3: TANPA reply_markup → kirim TANPA keyboard (minimize
    # default). Keyboard menu hanya dipasang eksplisit di /start & /menu.
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
        
    try:
        res = requests.post(url, json=payload, timeout=15)
        # 🌉 v1.2.17: catat pesan bot terakhir (konteks jembatan: "yang tadi").
        _LAST_BOT_MESSAGE[str(chat_id)] = text
        return res.json()
    except Exception as e:
        print(f"Error kirim pesan TG: {e}", flush=True)
        return None

# ==========================================
# 🔥 FASE 1 MONITORING — TOMBOL INLINE RETRY
# 🔥 FASE 2 APPROVAL — TOMBOL ✅ SETUJUI / ❌ TOLAK
# ==========================================
_cb_in_flight = set()  # data callback yang sedang diproses (anti dobel-klik)

def answer_callback(token, callback_query_id, text):
    """Jawab callback query supaya tombol berhenti berputar di Telegram."""
    try:
        requests.post(
            f"https://api.telegram.org/bot{token}/answerCallbackQuery",
            json={"callback_query_id": callback_query_id, "text": text[:200]},
            timeout=10
        )
    except Exception as e:
        print(f"Error jawab callback TG: {e}", flush=True)

def request_retry(task_id):
    """Panggil endpoint retry di panel (proses app.py). Kunci operator_key
    dibaca fresh dari config.json — TIDAK pernah dikirim ke chat.
    Kembalikan (ok, info, data) — data = JSON panel (judul + judul baru
    bila diganti; dipakai pesan konfirmasi tunggal v1.2.17)."""
    config = load_config()
    key = config.get('operator_key', '')
    if not key:
        return False, "Kunci operator belum siap (panel mungkin baru restart). Coba lagi sebentar lagi.", {}
    try:
        res = requests.post(
            PANEL_URL + '/api/operator/retry_task',
            json={"task_id": task_id, "key": key},
            timeout=20
        )
        data = res.json()
        if res.status_code == 200 and data.get('status') == 'success':
            return True, data.get('message', 'Task kembali ke antrean render.'), data
        return False, data.get('message', f"Panel menjawab HTTP {res.status_code}."), data
    except Exception as e:
        return False, f"Tidak bisa menghubungi panel: {e}", {}

def request_approval(action, task_id=None, batch_id=None):
    """Panggil endpoint approval di panel (approve_task / reject_task /
    approve_batch). Pola sama seperti request_retry: operator_key dibaca
    fresh dari config.json — TIDAK pernah dikirim ke chat.
    action: 'approve_task' | 'reject_task' | 'approve_batch'."""
    config = load_config()
    key = config.get('operator_key', '')
    if not key:
        return False, "Kunci operator belum siap (panel mungkin baru restart). Coba lagi sebentar lagi."
    payload = {"key": key}
    if task_id is not None:
        payload["task_id"] = task_id
    if batch_id is not None:
        payload["batch_id"] = batch_id
    try:
        res = requests.post(
            PANEL_URL + f'/api/operator/{action}',
            json=payload,
            timeout=20
        )
        data = res.json()
        if res.status_code == 200 and data.get('status') == 'success':
            return True, data.get('message', 'Berhasil.')
        return False, data.get('message', f"Panel menjawab HTTP {res.status_code}.")
    except Exception as e:
        return False, f"Tidak bisa menghubungi panel: {e}"

def request_create_shorts(payload):
    """Buat Shorts Muse lewat endpoint operator panel
    (/api/operator/create_shorts). Pola sama seperti request_retry:
    operator_key dibaca fresh dari config.json — TIDAK pernah dikirim
    ke chat. Kembalikan (ok, pesan, data_respons)."""
    config = load_config()
    key = config.get('operator_key', '')
    if not key:
        return False, "Kunci operator belum siap (panel mungkin baru restart). Coba lagi sebentar lagi.", {}
    body = dict(payload or {})
    body["key"] = key
    try:
        res = requests.post(
            PANEL_URL + '/api/operator/create_shorts',
            json=body,
            timeout=25
        )
        data = res.json()
        if res.status_code == 200 and data.get('status') == 'success':
            return True, data.get('message', 'Berhasil dibuat.'), data
        return False, data.get('message', f"Panel menjawab HTTP {res.status_code}."), data
    except Exception as e:
        return False, f"Tidak bisa menghubungi panel: {e}", {}

def request_create_long(payload):
    """Buat batch video long lewat endpoint operator panel
    (/api/operator/create_long). Pola sama seperti request_create_shorts."""
    config = load_config()
    key = config.get('operator_key', '')
    if not key:
        return False, "Kunci operator belum siap (panel mungkin baru restart). Coba lagi sebentar lagi.", {}
    body = dict(payload or {})
    body["key"] = key
    try:
        res = requests.post(
            PANEL_URL + '/api/operator/create_long',
            json=body,
            timeout=25
        )
        data = res.json()
        if res.status_code == 200 and data.get('status') == 'success':
            return True, data.get('message', 'Berhasil dibuat.'), data
        return False, data.get('message', f"Panel menjawab HTTP {res.status_code}."), data
    except Exception as e:
        return False, f"Tidak bisa menghubungi panel: {e}", {}

def _operator_post(endpoint, payload, timeout=25):
    """POST generik ke endpoint operator panel (pola sama seperti
    request_create_shorts: operator_key dibaca fresh dari config.json,
    TIDAK pernah dikirim ke chat)."""
    config = load_config()
    key = config.get('operator_key', '')
    if not key:
        return False, "Kunci operator belum siap (panel mungkin baru restart). Coba lagi sebentar lagi.", {}
    body = dict(payload or {})
    body["key"] = key
    try:
        res = requests.post(
            PANEL_URL + endpoint,
            json=body,
            timeout=timeout
        )
        data = res.json()
        if res.status_code == 200 and data.get('status') == 'success':
            return True, data.get('message', 'Berhasil.'), data
        return False, data.get('message', f"Panel menjawab HTTP {res.status_code}."), data
    except Exception as e:
        return False, f"Tidak bisa menghubungi panel: {e}", {}


def request_extend_schedule(yt_id, days, start=None):
    """Perpanjang jadwal channel lewat /api/operator/extend_schedule."""
    payload = {"yt_id": yt_id, "days": days}
    if start:
        payload["start"] = start
    return _operator_post('/api/operator/extend_schedule', payload)


def request_set_autopilot(yt_id, mode):
    """Ubah mode autopilot channel lewat /api/operator/set_autopilot."""
    return _operator_post('/api/operator/set_autopilot', {"yt_id": yt_id, "mode": mode})


def request_set_daily_playlist(yt_id, enabled):
    """Nyalakan/matikan jadwal Playlist harian channel (v1.3.18)."""
    return _operator_post('/api/operator/set_daily_playlist', {"yt_id": yt_id, "enabled": bool(enabled)})


def request_set_paused(yt_id, paused):
    """Jeda / lanjutkan channel lewat /api/operator/set_paused."""
    return _operator_post('/api/operator/set_paused', {"yt_id": yt_id, "paused": paused})


def handle_callback_query(token, chat_id, callback_query):
    """Proses tombol inline (Fase 1: 🔁 Coba Lagi; Fase 2: ✅/❌ approval).
    Hanya owner yang sampai ke sini — pengecekan owner dilakukan di loop utama."""
    cq_id = callback_query.get('id', '')
    data = (callback_query.get('data') or '').strip()
    if data.startswith('retry:'):
        try:
            task_id = int(data.split(':', 1)[1])
        except Exception:
            answer_callback(token, cq_id, "Eh, data tombolnya nggak valid nih 😅")
            return
        if data in _cb_in_flight:
            answer_callback(token, cq_id, "Sabar Bos, retry-nya masih saya proses 😅")
            return
        _cb_in_flight.add(data)
        try:
            answer_callback(token, cq_id, "Siap Bos, saya kirim retry-nya... 🔁")
            ok, info, data = request_retry(task_id)
            if ok:
                # 🔥 v1.2.17: SATU pesan konfirmasi saja (app.py tidak lagi
                # mengirim pesan retry — dulu ada dua: "Siap Bos, retry
                # jalan!" + "Berhasil Bos, retry jalan!").
                renamed = (data or {}).get('renamed_title')
                title = (data or {}).get('title') or f"Task #{task_id}"
                if renamed:
                    send_message(token, chat_id,
                                 f"✅ *Sip Bos, retry jalan dengan judul baru!* 💪\n"
                                 f"Judul lama sudah pernah tayang, jadi saya ganti jadi `{_tg_escape(str(renamed)[:60])}` — sudah masuk antrean render lagi.")
                else:
                    send_message(token, chat_id,
                                 f"✅ *Sip Bos, retry jalan!* 💪\n"
                                 f"Video `{_tg_escape(str(title)[:60])}` sudah masuk antrean render lagi.")
            else:
                send_message(token, chat_id,
                             f"⚠️ *Yah, retry-nya gagal Bos* 😅\nTask #{task_id}: {info}")
        finally:
            _cb_in_flight.discard(data)
        return
    # --- FASE 2: approval satu task ---
    if data.startswith('approve:') or data.startswith('reject:'):
        action, _, _raw_id = data.partition(':')
        try:
            task_id = int(_raw_id)
        except Exception:
            answer_callback(token, cq_id, "Eh, data tombolnya nggak valid nih 😅")
            return
        if data in _cb_in_flight:
            answer_callback(token, cq_id, "Sabar Bos, keputusannya masih saya proses 😅")
            return
        _cb_in_flight.add(data)
        try:
            word = "Siap Bos, saya setujui" if action == 'approve' else "Siap Bos, saya tolak"
            answer_callback(token, cq_id, f"{word} task...")
            ok, info = request_approval('approve_task' if action == 'approve' else 'reject_task',
                                        task_id=task_id)
            if ok:
                icon = "✅" if action == 'approve' else "❌"
                verb = "disetujui dan masuk antrean render" if action == 'approve' else "ditolak dan tidak akan dieksekusi"
                send_message(token, chat_id,
                             f"{icon} *Sip Bos, task #{task_id} {verb}.*\n_{info}_")
            else:
                send_message(token, chat_id,
                             f"⚠️ *Yah gagal Bos* 😅\nTask #{task_id}: {info}")
        finally:
            _cb_in_flight.discard(data)
        return
    # --- FASE 2: approval satu batch sekaligus ---
    if data.startswith('approve_all:'):
        batch_id = data.split(':', 1)[1].strip()
        if not batch_id:
            answer_callback(token, cq_id, "Eh, data tombolnya nggak valid nih 😅")
            return
        if data in _cb_in_flight:
            answer_callback(token, cq_id, "Sabar Bos, batch-nya masih saya proses 😅")
            return
        _cb_in_flight.add(data)
        try:
            answer_callback(token, cq_id, "Siap Bos, saya setujui semua... ✅")
            ok, info = request_approval('approve_batch', batch_id=batch_id)
            if ok:
                send_message(token, chat_id,
                             f"✅ *Sip Bos, batch-nya udah saya setujui semua!* 💪\n{info}")
            else:
                send_message(token, chat_id,
                             f"⚠️ *Yah, approve batch-nya gagal Bos* 😅\nBatch `{batch_id}`: {info}")
        finally:
            _cb_in_flight.discard(data)
        return
    # --- 🔄 UPDATE / RESTART PANEL (v1.2.17): konfirmasi /update & /restart ---
    if data in ('upd:yes', 'upd:no', 'rst:yes', 'rst:no'):
        kind = 'update' if data.startswith('upd') else 'restart'
        confirm = data.endswith(':yes')
        if data in _cb_in_flight:
            answer_callback(token, cq_id, "Sabar Bos, masih saya proses 😅")
            return
        _cb_in_flight.add(data)
        try:
            if not confirm:
                answer_callback(token, cq_id, "Dibatalkan Bos 😌")
                send_message(token, chat_id,
                             "😌 *Sip Bos, dibatalkan.*\n"
                             "Panel tetap jalan seperti biasa. "
                             "Ketik /update atau /restart kalau mau mulai lagi ya Bos!")
                return
            if kind == 'update':
                answer_callback(token, cq_id, "Siap Bos, saya mulai update-nya... 🔄")
                ok, info, _d = _operator_post('/api/operator/update_panel', {}, timeout=30)
                if ok:
                    send_message(token, chat_id,
                                 "🔄 *Update jalan, Bos* 😎\n\n"
                                 "Panel sedang mengambil kode terbaru dari GitHub lalu restart sendiri. "
                                 "Saya kabari lagi kalau sudah selesai — lengkap dengan versi barunya.")
                else:
                    send_message(token, chat_id,
                                 f"⚠️ *Yah, update-nya belum bisa jalan Bos* 😅\n{_tg_escape(info)}")
                return
            # --- restart: pesan HARUS terkirim dulu, baru restart dieksekusi.
            answer_callback(token, cq_id, "Siap Bos, saya restart... 🔄")
            send_message(token, chat_id,
                         "🔄 *Bot + panel sedang restart, Bos* 💪\n\n"
                         "Saya kembali sebentar lagi (~10–30 detik) dan menyapa lagi kalau sudah hidup.")
            ok, info, _d = _operator_post('/api/operator/restart_services', {}, timeout=10)
            if not ok:
                # Koneksi putus = kemungkinan besar restart SUDAH jalan
                # (panel mati sebelum sempat membalas) — JANGAN kirim pesan
                # gagal palsu; sapaan startup operator adalah konfirmasinya.
                if info.startswith("Tidak bisa menghubungi panel"):
                    print("⚠️ Restart: panel tidak menjawab setelah perintah (kemungkinan restart sedang jalan).", flush=True)
                else:
                    send_message(token, chat_id,
                                 f"⚠️ *Yah, restart-nya gagal Bos* 😅\n{_tg_escape(info)}")
            return
        finally:
            _cb_in_flight.discard(data)
    # --- v1.3.18: toggle jadwal Playlist harian (per channel / semua) ---
    if data.startswith('dailypl:'):
        if data in ('dailypl:all:on', 'dailypl:all:off'):
            enable = data.endswith(':on')
            if data in _cb_in_flight:
                answer_callback(token, cq_id, "Sabar Bos, masih saya proses 😅")
                return
            _cb_in_flight.add(data)
            try:
                answer_callback(token, cq_id, "Siap Bos, saya ubah semua... 📅")
                done = []
                for ch in load_channels():
                    ok, _info, _d = request_set_daily_playlist(ch.get('yt_id'), enable)
                    if ok:
                        done.append(_channel_display_name(ch))
                send_message(token, chat_id,
                             f"{'▶️' if enable else '⏹️'} *Jadwal harian {'nyala' if enable else 'berhenti'} buat semua channel, Bos*\n" +
                             ("✅ " + ", ".join(_tg_escape(n) for n in done) if done else "⚠️ Tidak ada channel yang berubah.") +
                             "\nCek status: /jadwalplaylist")
            finally:
                _cb_in_flight.discard(data)
            return
        parts = data.split(':')
        if len(parts) == 3 and parts[1] == 'toggle':
            yt_id = parts[2]
            if data in _cb_in_flight:
                answer_callback(token, cq_id, "Sabar Bos, masih saya proses 😅")
                return
            _cb_in_flight.add(data)
            try:
                ch = next((c for c in load_channels() if c.get('yt_id') == yt_id), None)
                if not ch:
                    answer_callback(token, cq_id, "Channel tidak ketemu 😅")
                    return
                enable = not bool(ch.get('daily_playlist'))
                answer_callback(token, cq_id, "Siap Bos, saya ubah... 📅")
                ok, info, _d = request_set_daily_playlist(yt_id, enable)
                if ok:
                    _send_daily_playlist_report(token, chat_id)
                else:
                    send_message(token, chat_id, f"⚠️ *Yah gagal Bos* 😅\n{_tg_escape(info)}")
            finally:
                _cb_in_flight.discard(data)
            return
    # --- v1.3.16: tawaran isi jadwal saat /jadwal kosong (total 0) ---
    if data in ('jadwal:buat_playlist', 'jadwal:perpanjang30'):
        if data.endswith('buat_playlist'):
            answer_callback(token, cq_id, "Siap Bos, buat playlist ya 🎵")
            _start_make_playlist(token, chat_id, '/playlist')
        else:
            # Sama seperti /perpanjang semua 30: simpan pending + minta
            # konfirmasi dulu lewat _nlu_advance (tidak langsung membuat).
            answer_callback(token, cq_id, "Siap Bos, saya siapkan perpanjangnya 📅")
            _chs = load_channels()
            pending = {"intent": "perpanjang_jadwal",
                       "slots": {"days": 30,
                                 "channels": [c.get('yt_id') for c in _chs]},
                       "awaiting": None,
                       "candidates": [c.get('yt_id') for c in _chs],
                       "created_at": time.time()}
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
        return
    # --- v1.3.56: tombol durasi Langkah 3 /playlist ---
    if data.startswith('mkplaylist:dur:'):
        draft = _load_make_draft(chat_id)
        if not draft or draft.get('expired') or draft.get('kind') != 'playlist':
            answer_callback(token, cq_id, "Draft-nya sudah tidak ada 😅")
            send_message(token, chat_id,
                         "🤖 *Eh Bos, draft-nya sudah tidak ada* 😅\nKetik /playlist untuk mulai lagi ya Bos!")
            return
        # 🔥 v1.3.33 pattern: tap kedua ditolak — step sudah berubah jadi
        # 'research' saat tap pertama diproses, jadi riset tidak jalan dua
        # kali (kuota + job yatim).
        if draft.get('step') != 'duration':
            answer_callback(token, cq_id, "Sudah dijawab Bos 😅")
            return
        choice = data.rsplit(':', 1)[-1]
        d = draft.get('data') or {}
        if choice == 'custom':
            answer_callback(token, cq_id, "Siap Bos, ketik durasinya ya ✏️")
            send_message(token, chat_id,
                         "✏️ *Durasi custom*\n\nKetik durasinya Bos — mis. `40-70`, `1 jam`, angka `60`, atau `lewati` untuk semua durasi.")
            return
        if choice == 'all':
            answer_callback(token, cq_id, "Siap Bos, semua durasi 😎")
            d['duration_min_sec'] = None
            d['duration_max_sec'] = None
            d['duration_note'] = ''
        else:
            # Nilai tombol dipakai ulang parser teks yang sama ('40-70',
            # '1jam', '60') — jadi arti tombol = arti ketikan yang sama.
            parsed = _parse_playlist_duration(choice)
            if not parsed:
                answer_callback(token, cq_id, "Hmm, pilihan itu tidak dikenal 😅")
                return
            answer_callback(token, cq_id, "Siap Bos 😎")
            d['duration_min_sec'], d['duration_max_sec'], d['duration_note'] = parsed
        draft['step'] = 'research'
        draft['data'] = d
        _save_make_draft(chat_id, draft)
        _pl_run_research(token, chat_id, draft)
        return
    # --- v1.3.13: tawaran "yang paling dekat" di /playlist ---
    if data in ('mkplaylist:closest:yes', 'mkplaylist:closest:no'):
        draft = _load_make_draft(chat_id)
        if not draft or draft.get('expired') or draft.get('kind') != 'playlist':
            answer_callback(token, cq_id, "Draft-nya sudah tidak ada 😅")
            send_message(token, chat_id,
                         "🤖 *Eh Bos, draft-nya sudah tidak ada* 😅\nKetik /playlist untuk mulai lagi ya Bos!")
            return
        # 🔥 v1.3.33: tap kedua ditolak — step sudah berubah jadi
        # 'research'/'duration' saat tap pertama diproses, jadi riset
        # tidak jalan dua kali (kuota + job yatim).
        if draft.get('step') != 'closest':
            answer_callback(token, cq_id, "Sudah dijawab Bos 😅")
            return
        d = draft.get('data') or {}
        if data.endswith(':no'):
            answer_callback(token, cq_id, "Siap Bos, ganti durasi ya")
            d.pop('closest', None)
            draft['step'] = 'duration'
            draft['data'] = d
            _save_make_draft(chat_id, draft)
            _make_ask_step(token, chat_id, draft)
            return
        # yes: pakai yang paling dekat — riset ulang TANPA filter durasi
        # (job baru berisi video itu; create menolak video di luar job).
        answer_callback(token, cq_id, "Siap Bos, pakai yang paling dekat 😎")
        d['duration_min_sec'] = None
        d['duration_max_sec'] = None
        d['duration_note'] = ''
        d.pop('closest', None)
        draft['step'] = 'research'
        draft['data'] = d
        _save_make_draft(chat_id, draft)
        _pl_run_research(token, chat_id, draft)
        return
    # --- BUAT VIDEO/PLAYLIST/ZUMO: konfirmasi ✅ / batal ❌ dari ringkasan
    # /buatshort & /buatlong & /buatplaylist & /zumo ---
    if data in ('mkshort:yes', 'mkshort:no', 'mklong:yes', 'mklong:no',
                'mkplaylist:yes', 'mkplaylist:no',
                'mkzumo:yes', 'mkzumo:no',
                'mkzumbank:yes', 'mkzumbank:no',
                'mkzumrakit:yes', 'mkzumrakit:no'):
        # 'mkzumbank'/'mkzumrakit' dicek DULU — keduanya berawalan
        # 'mkzumo', jadi urutan startswith di bawah penting.
        kind = ('zumobank' if data.startswith('mkzumbank')
                else 'zumorakit' if data.startswith('mkzumrakit')
                else 'zumo' if data.startswith('mkzumo')
                else 'playlist' if data.startswith('mkplaylist')
                else 'short' if data.startswith('mkshort') else 'long')
        retry_cmd = {'short': '/buatshort', 'long': '/buatlong',
                     'playlist': '/playlist', 'zumo': '/zumo',
                     'zumobank': '/zumobank',
                     'zumorakit': '/zumorakit'}[kind]
        confirm = data.endswith(':yes')
        if data in _cb_in_flight:
            answer_callback(token, cq_id, "Sabar Bos, masih saya proses 😅")
            return
        _cb_in_flight.add(data)
        try:
            draft = _load_make_draft(chat_id)
            if not draft or draft.get('expired'):
                answer_callback(token, cq_id, "Draft-nya sudah tidak ada 😅")
                send_message(token, chat_id,
                             "🤖 *Eh Bos, draft-nya sudah tidak ada* 😅\n"
                             "Mungkin sudah dibuat, dibatalkan, atau hangus (>30 menit). "
                             f"Ketik {retry_cmd} untuk mulai lagi ya Bos!")
                return
            if not confirm:
                _clear_make_draft(chat_id)
                answer_callback(token, cq_id, "Dibatalkan Bos 😌")
                cancel_msg = {'zumobank': "Tidak ada lagu bank yang dibuat.",
                              'zumorakit': "Tidak ada video rakitan yang dibuat."}.get(
                    kind, "Tidak ada video yang dibuat.")
                send_message(token, chat_id,
                             f"😌 *Sip Bos, dibatalkan.*\n{cancel_msg} "
                             f"Ketik {retry_cmd} kalau mau mulai lagi ya Bos!")
                return
            answer_callback(token, cq_id, "Siap Bos, saya buatkan... 🎬")
            d = draft.get('data') or {}
            if kind == 'short':
                ok, info, resp = request_create_shorts({
                    "yt_id": d.get('yt_id'), "title": d.get('title'),
                    "hook": d.get('hook', ''), "script": d.get('script', ''),
                    "cta": d.get('cta', 'Subscribe'),
                    "duration_sec": d.get('duration_sec', 30),
                    "publish_date": d.get('publish_date'),
                    "text_color": "#FFFFFF",
                    "bg_file": d.get('bg_file', ''),
                    "bgm_file": d.get('bgm_file', 'none'),
                })
            elif kind == 'playlist':
                ok, info, resp = _operator_post('/api/operator/playlist_create', {
                    "job_id": d.get('job_id'), "video_id": d.get('video_id'),
                    "publish_date": d.get('publish_date'), "privacy": "public",
                    "pixabay_theme": d.get('pixabay_theme') or '',
                })
                resp = resp or {}
            elif kind in ('zumo', 'zumobank', 'zumorakit'):
                ok, info, resp = _operator_post('/api/operator/zumo_create',
                                                _zumo_create_payload(d))
                resp = resp or {}
            else:
                ok, info, resp = request_create_long({
                    "yt_id": d.get('yt_id'), "count": d.get('count', 1),
                    "titles": d.get('titles') or [],
                    "start_date": d.get('start_date'),
                    "interval_days": d.get('interval_days', 1),
                })
            _clear_make_draft(chat_id)
            if not ok:
                send_message(token, chat_id,
                             f"⚠️ *Yah Bos, gagal dibuat* 😅\n{info}\n"
                             f"Coba lagi dengan {retry_cmd} ya Bos!")
                return
            if kind == 'zumo':
                _send_zumo_created_message(token, chat_id, d, resp)
            elif kind == 'zumobank':
                _send_zumo_bank_created_message(token, chat_id, d, resp)
            elif kind == 'zumorakit':
                _send_zumo_assemble_created_message(token, chat_id, d, resp)
            elif kind == 'playlist':
                _send_playlist_created_message(token, chat_id, d, resp)
            else:
                _send_make_created_message(token, chat_id, kind, d, resp)
        finally:
            _cb_in_flight.discard(data)
        return
    # --- 🎵 ZUMO: menu /zumo + ganti model ---
    if data in ('zm:new', 'zm:jobs'):
        if data == 'zm:new':
            answer_callback(token, cq_id, "Siap, kita buat Playlist Zumo baru!")
            _start_make_zumo(token, chat_id, '')
            return
        answer_callback(token, cq_id, "Siap, ini status Zumo-nya.")
        _send_zumo_jobs_message(token, chat_id)
        return
    if data.startswith('zmmodel:'):
        model = data.split(':', 1)[1].upper()
        if model not in _ZUMO_MODELS:
            answer_callback(token, cq_id, "Model tidak dikenal.")
            return
        ok, msg, _dd = _operator_post('/api/operator/zumo_model', {'model': model})
        answer_callback(token, cq_id, "Siap, model diganti." if ok else "Gagal ganti model.")
        send_message(token, chat_id, "Zumo — Model\n" + _tg_escape(msg))
        return
    # --- 🎵 MUSIK PLAYLIST: menu /playlist + pilih kandidat ---
    if data in ('pl:new', 'pl:jobs'):
        if data == 'pl:new':
            answer_callback(token, cq_id, "Siap, kita buat Playlist baru!")
            _start_make_playlist(token, chat_id, '')
            return
        answer_callback(token, cq_id, "Siap, ini job terakhirnya.")
        _send_playlist_jobs_message(token, chat_id)
        return
    if data.startswith('plpick:'):
        arg = data.split(':', 1)[1]
        draft = _load_make_draft(chat_id)
        if not draft or draft.get('kind') != 'playlist' or draft.get('step') != 'pick':
            answer_callback(token, cq_id, "Pilihannya sudah tidak berlaku 😅")
            send_message(token, chat_id,
                         "😅 *Pilihan kandidatnya sudah tidak berlaku* — ketik /playlist untuk mulai lagi ya Bos!")
            return
        if arg == 'auto':
            answer_callback(token, cq_id, "Siap, saya pilihkan yang terbaik!")
            _pl_auto_pick(token, chat_id, draft)
            return
        try:
            idx = int(arg)
        except Exception:
            answer_callback(token, cq_id, "Pilihan tidak dikenal.")
            return
        cands = (draft.get('data') or {}).get('candidates') or []
        if not (0 <= idx < len(cands)):
            answer_callback(token, cq_id, "Pilihan tidak dikenal.")
            return
        cand = cands[idx]
        d = draft.get('data') or {}
        d['video_id'] = cand.get('video_id')
        d['ref_title'] = cand.get('title', '')
        draft['data'] = d
        draft['step'] = _playlist_next_step(d)
        _save_make_draft(chat_id, draft)
        answer_callback(token, cq_id, "Sip, kandidat dipilih!")
        _make_ask_step(token, chat_id, draft)
        return
    # --- ⚙️ ATUR JADWAL (v1.2.20): tombol alur /aturjadwal ---
    # aj:ch:<idx> pilih channel → aj:field:<key> pilih yang diubah →
    # aj:theme:<niche> pilih tema → aj:yes / aj:no konfirmasi simpan.
    if data.startswith('aj:'):
        draft = _load_aturjadwal_draft(chat_id)
        if not draft or draft.get('expired'):
            answer_callback(token, cq_id,
                            "Pengaturannya sudah hangus / selesai — ketik /aturjadwal lagi ya Bos 😌")
            return
        if data.startswith('aj:ch:'):
            try:
                idx = int(data[len('aj:ch:'):])
                ch = load_channels()[idx]
            except Exception:
                answer_callback(token, cq_id, "Eh, channel-nya nggak valid nih 😅")
                return
            draft['yt_id'] = ch.get('yt_id')
            draft['step'] = 'field'
            _save_aturjadwal_draft(chat_id, draft)
            answer_callback(token, cq_id, "Sip!")
            _aturjadwal_ask_field(token, chat_id, draft)
            return
        if data.startswith('aj:field:'):
            key = data[len('aj:field:'):]
            if key not in _AJ_FIELD_LABEL:
                answer_callback(token, cq_id, "Eh, pilihannya nggak valid nih 😅")
                return
            draft['field'] = key
            draft['step'] = 'value'
            _save_aturjadwal_draft(chat_id, draft)
            answer_callback(token, cq_id, "Sip!")
            _aturjadwal_ask_value(token, chat_id, draft)
            return
        if data.startswith('aj:theme:'):
            key = data[len('aj:theme:'):]
            if key not in _NICHES:
                answer_callback(token, cq_id, "Eh, temanya nggak valid nih 😅")
                return
            draft['value'] = key
            draft['step'] = 'confirm'
            _save_aturjadwal_draft(chat_id, draft)
            answer_callback(token, cq_id, "Sip!")
            _aturjadwal_ask_confirm(token, chat_id, draft)
            return
        if data == 'aj:yes':
            answer_callback(token, cq_id, "Siap, saya simpan!")
            _aturjadwal_save(token, chat_id, draft)
            return
        if data == 'aj:no':
            _clear_aturjadwal_draft(chat_id)
            answer_callback(token, cq_id, "Dibatalkan.")
            send_message(token, chat_id, "😌 *Sip Bos, dibatalkan* — tidak ada yang diubah.")
            return
        return
    # --- 🧠 NLU: jawaban tombol klarifikasi (channel / niche / mode /
    # Short-vs-Long / Ya-Bukan). ---
    if data.startswith('nlu:'):
        pending = _load_nlu_pending(chat_id)
        if not pending:
            answer_callback(token, cq_id,
                            "Klarifikasinya sudah hangus / selesai — ketik lagi perintahnya ya Bos 😌")
            return
        if data == 'nlu:yes':
            pending['confirmed'] = True
            _nlu_audit_log('', {"intent": pending.get('intent'), "confidence": None,
                                "slots": pending.get('slots') or {}}, 'confirmed')
            _save_nlu_pending(chat_id, pending)
            answer_callback(token, cq_id, "Siap, saya jalankan!")
            _nlu_advance(token, chat_id, pending)
            return
        if data == 'nlu:no':
            _clear_nlu_pending(chat_id)
            answer_callback(token, cq_id, "Dibatalkan.")
            send_message(token, chat_id,
                         "Siap Bos, dibatalkan 😌 — coba ketik lagi perintahnya dengan bahasa yang lain ya.")
            return
        if data.startswith('nlu:ch:'):
            arg = data[len('nlu:ch:'):]
            cands = pending.get('candidates') or [c.get('yt_id') for c in load_channels()]
            if arg == 'all':
                pending.setdefault('slots', {})['channels'] = list(cands)
            else:
                try:
                    idx = int(arg)
                    if 0 <= idx < len(cands):
                        # Bila diminta N channel, tombol ditekan satu per
                        # satu (akumulasi, tanpa duplikat); bila tidak, satu
                        # pilihan menggantikan yang lama.
                        cur = pending.setdefault('slots', {}).get('channels') or []
                        if pending.get('slots', {}).get('channel_count'):
                            if cands[idx] not in cur:
                                cur.append(cands[idx])
                            pending['slots']['channels'] = cur
                        else:
                            pending['slots']['channels'] = [cands[idx]]
                    else:
                        answer_callback(token, cq_id, "Pilihan tidak dikenal.")
                        return
                except ValueError:
                    answer_callback(token, cq_id, "Pilihan tidak dikenal.")
                    return
            answer_callback(token, cq_id, "Siap!")
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return
        if data.startswith('nlu:niche:'):
            niche = data[len('nlu:niche:'):]
            if niche in _NICHES:
                pending.setdefault('slots', {})['niche'] = niche
                pending['slots'].pop('unknown_niche', None)
                answer_callback(token, cq_id, f"Tema {_NICHES[niche]}.")
                _save_nlu_pending(chat_id, pending)
                _nlu_advance(token, chat_id, pending)
                return
            answer_callback(token, cq_id, "Tema tidak dikenal.")
            return
        if data.startswith('nlu:mode:'):
            mode = data[len('nlu:mode:'):]
            if mode in ('auto', 'approval'):
                pending.setdefault('slots', {})['mode'] = mode
                answer_callback(token, cq_id,
                                "Full-Auto." if mode == 'auto' else "Approval.")
                _save_nlu_pending(chat_id, pending)
                _nlu_advance(token, chat_id, pending)
                return
            answer_callback(token, cq_id, "Mode tidak dikenal.")
            return
        if data.startswith('nlu:kind:'):
            kind = data[len('nlu:kind:'):]
            if kind == 'short':
                pending['intent'] = 'buat_short'
                pending['slots'].pop('kind_ambiguous', None)
            elif kind == 'long':
                pending['intent'] = 'buat_long'
                pending['slots'].pop('kind_ambiguous', None)
            else:
                answer_callback(token, cq_id, "Pilihan tidak dikenal.")
                return
            answer_callback(token, cq_id, "Siap!")
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return
        answer_callback(token, cq_id, "Tombol ini saya nggak kenal, Bos 😅")
        return

    answer_callback(token, cq_id, "Eh, tombol ini saya nggak kenal 😅")

def get_system_telemetry():
    cpu = psutil.cpu_percent(interval=0.2)
    mem = psutil.virtual_memory()
    total, used, free = shutil.disk_usage(BASE_DIR)
    return {
        "cpu": cpu,
        "ram_pct": mem.percent,
        "ram_used_gb": round(mem.used / (1024**3), 1),
        "ram_total_gb": round(mem.total / (1024**3), 1),
        "disk_free_gb": round(free / (1024**3), 1)
    }

def generate_seo_titles(niche, count=10):
    """Judul SEO /judul — v1.2.24: kata kunci niche PALING DEPAN,
    tanpa emoji, maks ~100 karakter (rumus yang sama dengan judul
    playlist otomatis)."""
    niche_lower = niche.lower()
    templates = []

    if "tidur" in niche_lower or "sleep" in niche_lower or "insomnia" in niche_lower or "night" in niche_lower:
        templates = [
            "Deep Sleep Music: Fall Asleep in 3 Minutes with Relaxing Piano & Soft Rain",
            "Deep Sleep Music for Insomnia: Delta Waves to Stop Overthinking & Calm Your Mind",
            "Sleep Music for Deep Sleep: Bedtime Piano Melodies to Relieve Anxiety & Stress",
            "Deep Sleep Music 24/7: Cozy Bedroom Ambience with Gentle Rain Sounds",
            "Sleep Music for Insomnia: 60 Minutes Relaxing Piano for a Peaceful Night",
            "Deep Sleep Music 432Hz: Miracle Tone & Healing Night Ambience",
            "Sleep Music to Fall Asleep Fast: Peaceful Piano & Cozy Rain Sounds",
            "Deep Sleep Music for Anxiety: Calming Piano for Instant Sleep & Recovery",
            "Sleep Music for Stress Relief: Soothing Night Lullaby for Restless Minds",
            "Deep Sleep Music with Rain: Fall Asleep Fast with Soothing Piano & Thunder",
            "Sleep Music 528Hz: Peaceful Deep Delta Wave Healing Frequencies",
            "Sleep Music: Cozy Night Rain in a Cabin with Relaxing Instrumental Melodies",
            "Deep Sleep Music for Overthinkers: Gentle Piano Melodies to Release Stress",
            "Sleep Music for Babies: Soothing Piano & Soft White Noise for Deep Rest",
            "Sleep Music: Midnight Rest with 80 Minutes of Pure Calming Soundscapes"
        ]
    elif "anak" in niche_lower or "kids" in niche_lower or "balita" in niche_lower:
        templates = [
            "Lagu Anak Ceria: Belajar Berhitung 1-10 Sambil Bernyanyi (Edukatif & Menyenangkan)",
            "Lagu Anak-Anak Ceria: Ayo Bernyanyi Bersama untuk Balita & PAUD",
            "Lagu Anak Edukatif: Belajar Huruf A-Z Sambil Bernyanyi untuk Usia Dini",
            "Lagu Anak: Warna-Warni Pelangi, Belajar Warna Sambil Bernyanyi",
            "Lagu Anak Ceria: Gerak dan Lagu Senam Pagi Seru untuk Anak-Anak",
            "Lagu Anak Tidur: Lullaby Lembut untuk Anak Tidur Nyenyak",
            "Lagu Anak Edukatif: Belajar Nama Hewan Sambil Bernyanyi yang Seru",
            "Lagu Anak Indonesia: Kompilasi Lagu Ceria untuk Anak Usia Dini",
            "Lagu Anak: Ayo Belajar Berhitung Angka 1-20 untuk Anak PAUD & TK",
            "Lagu Anak Ceria: Hari-Hari Ceria, Lagu Semangat Pagi untuk Anak-Anak",
            "Lagu Anak Edukatif: Belajar Bentuk & Warna yang Mudah Diingat",
            "Lagu Anak Ceria: Sahabatku, Lagu Persahabatan untuk Anak-Anak",
        ]
    elif "asmr" in niche_lower:
        templates = [
            "ASMR for Deep Sleep: Gentle Whispers & Soft Tapping to Fall Asleep Fast",
            "ASMR Whispers for Sleep: Relaxing Soft Sounds for Stress Relief & Calm Nights",
            "ASMR Tapping & Brushing Sounds for Instant Relaxation (No Talking)",
            "ASMR for Insomnia: Fall Asleep in Minutes with Soothing Whispers",
            "ASMR for Deep Sleep: Cozy Night with Gentle Tapping, Whispers & Rain",
            "ASMR for Anxiety Relief: Calming Whispers to Quiet Your Mind",
            "ASMR Soft Spoken: Gentle Whispers to Help You Sleep Peacefully",
            "ASMR Ear-to-Ear Whispers: Deep Relaxation & Sleep Sounds (Headphones On)",
            "ASMR Tingly Sounds: Brushing, Tapping & Whispering for Deep Sleep",
            "ASMR Sleep Aid: Whispered Affirmations & Calm Sounds for Insomnia",
            "ASMR for Study & Focus: Soft Tapping Without Distraction",
            "ASMR Rain & Whispers: The Perfect Soundscape for a Good Night's Sleep",
        ]
    elif "piano" in niche_lower:
        templates = [
            "Piano Music for Deep Sleep: Relaxing Melodies to Calm Your Mind Instantly",
            "Piano Music for Sleep: Peaceful Piano & Soft Rain to Fall Asleep Fast",
            "Piano Music for Anxiety Relief: Soothing Instrumental for Stress-Free Nights",
            "Piano Music for Sleep & Meditation: Beautiful Melodies for Deep Relaxation (60 Mins)",
            "Piano Music at Midnight: Gentle Keys to Release Stress and Drift into Sweet Dreams",
            "Piano Lullaby for Insomnia: Soft, Slow Melodies for Peaceful Rest",
            "Piano Music 432Hz: Healing Tones for Deep Relaxation of Mind and Body",
            "Piano Music for a Cozy Evening: Warm Melodies for Reading, Resting & Unwinding",
            "Piano Music Collection: Emotional Melodies for Quiet Nights Alone",
            "Sleep Piano Music: Continuous Calming Melodies All Night Long"
        ]
    elif "hujan" in niche_lower or "rain" in niche_lower:
        templates = [
            "Rain Sounds for Sleeping: Gentle Rain on Roof & Distant Thunder for Deep Sleep",
            "Rain Sounds for Relaxation: Cozy Rainy Night Ambience on Window",
            "Rain Sounds for Deep Sleep: Heavy Rain & Thunderstorm (No Ads, All Night)",
            "Rain Sounds in a Cabin: Peaceful Night Ambience in the Woods for Insomnia",
            "Rain Sounds & Piano: The Perfect Combination to Fall Asleep in Minutes",
            "Rain Sounds for Focus & Study: Calm Rainy Day Ambience for Deep Work",
            "Rain Sounds at Midnight: Soothing Rainstorm to Block Noise & Sleep Better",
            "Rain Sounds on Leaves: Gentle Nature Sounds for Meditation & Stress Relief",
            "Rain Sounds in Tokyo: Cozy Rain Ambience with Soft City Sounds",
            "Rain Sounds for Sleep: 8 Hours of Pure Relaxing Rain"
        ]
    elif "meditasi" in niche_lower or "meditation" in niche_lower:
        templates = [
            "Meditation Music for Deep Relaxation: Guided Meditation & Inner Peace (60 Mins)",
            "Meditation Music for the Morning: Calm Your Mind, Breathe Slowly & Start Fresh",
            "Meditation Music 432Hz: Healing Sounds to Release Anxiety & Find Stillness",
            "Meditation Music: Zen Garden Ambience for Peaceful Mindfulness",
            "Meditation Music for Breathing: Deep Breathing & Slow Tones for Stress Relief",
            "Meditation Music for Chakra Healing: Soothing Sounds for Balance & Energy",
            "Meditation Music for Sleep: Drift into Deep Rest with Gentle Ambient Tones",
            "Meditation Music for Focus: Mindfulness Sounds for Calm & Emotional Healing",
            "Meditation Music at Sunrise: Soft Ambient Music for a Peaceful Morning",
            "Meditation Music for Stress Relief: Guided Relaxation for Body & Mind"
        ]
    elif "fokus" in niche_lower or "focus" in niche_lower:
        templates = [
            "Focus Music for Deep Focus: Concentration Sounds for Studying, Working & Reading",
            "Focus Music for Flow State: Steady Rhythms to Keep Your Brain in the Zone (60 Mins)",
            "Focus Music for Deep Work: Maximum Productivity with No Distractions",
            "Focus Music for Study: Calm Instrumental for Concentration, Exams & Deadlines",
            "Focus Music for Pomodoro: 25 Minutes of Pure Concentration",
            "Focus Music for Deep Thinking: Brain Food Backed by Science",
            "Focus Music for Coding & Writing: Ambient Sounds for a Concentration Boost",
            "Focus Music for Flow State: Enter Deep Focus in 5 Minutes (Study & Work)",
            "Focus Music for the Morning: Energizing Yet Calm Music for a Productive Day",
            "Focus Music for Deep Work: Silent Mind, Sharp Focus with Minimal Ambient Music"
        ]
    elif "lofi" in niche_lower or "study" in niche_lower or "chill" in niche_lower:
        templates = [
            "Lofi Hip Hop Radio: Beats to Relax & Study to with Cozy Rain Ambience",
            "Lofi Beats for Study: Late Night Session with Chill Beats for Focus",
            "Lofi Beats for Reading: Cozy Bedroom Beats with Smooth Instrumental",
            "Lofi Beats for the Morning: Peaceful Beats to Start Your Productive Day",
            "Lofi Chill Beats: Alone in Tokyo Rain, Nostalgic Beats for Overthinkers",
            "Lofi Beats for Deep Work: Instrumental Beats to Maximize Productivity",
            "Lofi Beats & Coffee Shop Ambience: Chill Beats for Rainy Afternoons",
            "Lofi Beats for Sunday Evening: Soft Beats for Stress Relief & Unwinding",
            "Lofi Beats for Coding: Midnight Coding & Deep Focus with Minimal Chillhop (2026)",
            "Lofi Beats for Autumn: Warm Relaxing Beats to Study, Chill, and Sleep to"
        ]
    else:
        templates = [
            f"{niche.title()} Music: Relaxing Melodies for Stress Relief & Peace",
            f"{niche.title()} Instrumental: Deep Focus & Calm Collection",
            f"{niche.title()} Sounds: Peaceful & Calming for Instant Relaxation",
            f"{niche.title()} Ambience: Healing & Recharging for Restful Days",
            f"{niche.title()} Melodies: Serenity Now, Gentle Sounds to Refresh Your Soul",
            f"{niche.title()} Music for the Morning: Uplifting Harmony for Positive Energy",
            f"{niche.title()} Instrumental: End of Day Reflection with Soft Relaxing Sounds",
            f"{niche.title()} Soundscapes: Quiet Moments to Melt Stress Away",
            f"{niche.title()} Compilation 2026: Deep Relaxation & Peaceful Music",
            f"{niche.title()} for Study & Work: Comfort & Calm Acoustic Sounds"
        ]

    while len(templates) < count:
        templates.append(f"{niche.title()} Relaxation Vol. {len(templates)+1} (60 Mins)")
    return templates[:count]

# ==========================================
# 🔥 PERLUASAN KONTEN — NICHE, HASHTAG, CTA, IDE SHORTS
# Bank konten statis (bukan data live): judul/deskripsi/tag/CTA/ide Shorts
# per niche. Niche yang dikenal: sleep, lofi, piano, hujan, meditasi, fokus,
# anak, asmr.
# ==========================================
_NICHES = {
    'sleep': 'Deep Sleep Music',
    'lofi': 'Lofi Study',
    'piano': 'Piano',
    'hujan': 'Hujan',
    'meditasi': 'Meditasi',
    'fokus': 'Fokus',
    'anak': 'Anak-Anak',
    'asmr': 'ASMR',
}


def _parse_niche_arg(clean_cmd, parts, default='sleep'):
    """(kunci_niche, label) dari kata kunci di pesan. Urutan penting: yang
    spesifik dulu supaya 'fokus' tidak ketangkap cabang 'lofi'."""
    if 'asmr' in clean_cmd:
        return 'asmr', 'ASMR'
    if 'anak' in clean_cmd or 'kids' in clean_cmd or 'balita' in clean_cmd:
        return 'anak', 'Anak-Anak'
    if 'piano' in clean_cmd:
        return 'piano', 'Piano'
    if 'hujan' in clean_cmd or 'rain' in clean_cmd:
        return 'hujan', 'Hujan'
    if 'meditasi' in clean_cmd or 'meditation' in clean_cmd:
        return 'meditasi', 'Meditasi'
    if 'fokus' in clean_cmd or 'focus' in clean_cmd:
        return 'fokus', 'Fokus'
    if 'lofi' in clean_cmd or 'chill' in clean_cmd or 'study' in clean_cmd:
        return 'lofi', 'Lofi Study'
    return default, _NICHES.get(default, 'Deep Sleep Music')


def _parse_count(parts, default=10, max_count=20):
    """Jumlah dari angka di akhir pesan ('/judul lofi 15' -> 15), dibatasi
    1..20 supaya pesan tidak kepanjangan."""
    if parts and parts[-1].isdigit():
        return max(1, min(max_count, int(parts[-1])))
    return default


_HASHTAG_BANK = {
    'sleep': ['#deepsleep', '#sleepmusic', '#sleepaid', '#relaxingmusic', '#insomniarelief',
              '#calmmusic', '#ambientmusic', '#sleepsounds', '#nightmusic', '#peacefulsleep',
              '#stressrelief', '#meditationmusic', '#calm', '#sleepbetter', '#lullaby'],
    'lofi': ['#lofi', '#lofimusic', '#lofihiphop', '#chillbeats', '#studybeats',
             '#chillmusic', '#lofivibes', '#cozybeats', '#studymusic', '#relaxingbeats',
             '#nightdrive', '#aesthetic', '#chillvibes', '#beats', '#focusmusic'],
    'piano': ['#piano', '#pianomusic', '#relaxingpiano', '#calmpiano', '#pianocover',
              '#instrumental', '#peacefulpiano', '#classicalpiano', '#pianorelaxation', '#softpiano',
              '#pianolove', '#musicforsleep', '#pianoambient', '#solopiano', '#beautifulpiano'],
    'hujan': ['#rain', '#rainsounds', '#rainyday', '#rainambience', '#sleeprain',
              '#rainymood', '#relaxingrain', '#rainydays', '#asmrrain', '#cozyrain',
              '#rainmusic', '#thunderstorm', '#rainfall', '#peacefulrain', '#nature'],
    'meditasi': ['#meditation', '#meditationmusic', '#mindfulness', '#yoga', '#healing',
                 '#zen', '#calm', '#innerpeace', '#relaxation', '#spiritual',
                 '#breathe', '#chakra', '#peace', '#wellness', '#mindful'],
    'fokus': ['#focus', '#focusmusic', '#deepfocus', '#studymusic', '#concentration',
              '#productivity', '#workmusic', '#studysession', '#deepwork', '#brainfood',
              '#instrumental', '#ambient', '#studymotivation', '#focusbeats', '#flowstate'],
    'anak': ['#laguanak', '#anakkecil', '#laguanakindonesia', '#belajarberhitung',
             '#laguceria', '#anakcerdas', '#dunianak', '#laguanakcerdas',
             '#belajarhuruf', '#anakkreatif', '#lagubalita', '#anakhebat',
             '#ceritabergambar', '#laguedukasi', '#kidsongs'],
    'asmr': ['#asmr', '#asmrsounds', '#asmrwhisper', '#asmrtingles',
             '#asmrsleep', '#asmrrelax', '#asmrcommunity', '#asmrvideo',
             '#asmrtapping', '#whisperasmr', '#asmrforsleep', '#relaxingasmr',
             '#asmrindonesia', '#asmrtriggers', '#sleepasmr'],
}


def build_hashtag_report(niche_key):
    """Bank hashtag per niche — tinggal salin ke deskripsi video."""
    label = _NICHES.get(niche_key, 'Deep Sleep Music')
    tags = _HASHTAG_BANK.get(niche_key, _HASHTAG_BANK['sleep'])
    lines = [
        f"🏷️ *Bank Hashtag Buat Bos* 😎",
        f"🎯 *Niche:* `{label}`",
        "━━━━━━━━━━━━━━━━━━━━━━",
        " ".join(tags),
        "━━━━━━━━━━━━━━━━━━━━━━",
        "Tips dari saya Bos: pakai maks 15 hashtag, taruh di AKHIR deskripsi, dan jangan diulang-ulang di semua video ya — campur dikit-dikit biar nggak dikira spam 😌",
    ]
    return "\n".join(lines)


_CTA_IDEAS = [
    ("🔔 Subscribe", "Suka videonya? Pencet Subscribe — gratis, dan bikin kami makin semangat upload tiap hari!"),
    ("💬 Komen", "Komen 'TIDUR' kalau kamu ketiduran pas dengerin ini 😴"),
    ("💾 Save", "Save video ini buat nanti malam — badan capek butuh ini."),
    ("↗️ Share", "Share ke temanmu yang susah tidur — siapa tahu dia butuh ini."),
    ("🔔 Lonceng", "Tekan loncengnya 🔔 supaya nggak ketinggalan upload jam 7 malam."),
    ("▶️ Lanjut nonton", "Part 2 ada di video berikutnya — tonton sampai habis ya!"),
    ("❤️ Follow", "Follow biar tiap malam ada teman tidur baru."),
    ("🕘 Komen jam", "Komen jam berapa kamu nonton ini — yang paling malam menang 😄"),
]


def build_cta_report():
    """Ide teks CTA buat kartu akhir Shorts (5 detik terakhir)."""
    lines = [
        "📣 *Ide CTA Buat Shorts Bos* 😎",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for icon_title, text in _CTA_IDEAS:
        lines.append(f"{icon_title}\n“{text}”\n")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("Tips dari saya Bos: 1 Shorts cukup 1 CTA, taruh di kartu akhir (5 detik terakhir), teksnya BESAR dan gampang dibaca sambil scroll 😌")
    return "\n".join(lines)


_SHORTS_IDEAS = {
    'sleep': [
        {"hook": "Susah tidur? Coba dengerin ini 60 detik.",
         "naskah": ["0–4 dtk: teks hook + piano lembut masuk", "5–40 dtk: hujan rintik + melodi pelan, teks 'tarik napas... hembuskan...'", "41–55 dtk: suara makin pelan, layar gelap perlahan"],
         "cta": "Follow buat tidur nyenyak tiap malam."},
        {"hook": "Otak nggak bisa berhenti mikir?",
         "naskah": ["0–4 dtk: teks hook, zoom pelan ke gambar hujan", "5–40 dtk: piano + suara hujan, teks 'biarkan pikiran lewat seperti awan'", "41–55 dtk: nada terakhir, jeda hening 2 detik"],
         "cta": "Save video ini buat nanti malam."},
        {"hook": "3 menit lagi kamu ketiduran.",
         "naskah": ["0–4 dtk: teks hook + jam dinding berdetak pelan", "5–40 dtk: melodi tidur 432Hz, teks 'mata makin berat...'", "41–55 dtk: fade out, teks 'selamat tidur'"],
         "cta": "Komen 'TIDUR' kalau kamu ketiduran."},
        {"hook": "Ini suara yang dicari 2 juta orang sebelum tidur.",
         "naskah": ["0–4 dtk: teks hook, gambar kabin hujan", "5–40 dtk: hujan di atap + piano jauh, teks 'tidak ada iklan, tidak ada suara tiba-tiba'", "41–55 dtk: hujan mereda"],
         "cta": "Share ke temanmu yang susah tidur."},
        {"hook": "Jangan skip — 10 detik pertama bikin ngantuk.",
         "naskah": ["0–4 dtk: teks hook + hitung mundur 3-2-1", "5–40 dtk: delta wave + napas pelan, teks 'satu... dua...'", "41–55 dtk: layar hampir gelap"],
         "cta": "Subscribe biar tiap malam ada teman tidur baru."},
    ],
    'lofi': [
        {"hook": "Lagi belajar tapi nggak fokus?",
         "naskah": ["0–4 dtk: teks hook + beat lofi masuk", "5–40 dtk: beat pelan + hujan, teks 'satu lagu, satu bab, selesai'", "41–55 dtk: beat mereda, teks 'lanjutkan...'"],
         "cta": "Save buat sesi belajar berikutnya."},
        {"hook": "Suasana kafe hujan, tanpa harus keluar rumah.",
         "naskah": ["0–4 dtk: teks hook, gambar jendela hujan", "5–40 dtk: lofi + suara hujan, teks 'kopi hangat, tugas beres'", "41–55 dtk: fade ke logo channel"],
         "cta": "Follow buat teman begadang tiap malam."},
        {"hook": "Beat ini bikin tugas 2 jam kerasa 20 menit.",
         "naskah": ["0–4 dtk: teks hook + vinyl berputar", "5–40 dtk: chill beat, teks 'fokus mode: ON'", "41–55 dtk: beat terakhir + jeda"],
         "cta": "Komen mata kuliah tersulitmu di bawah."},
        {"hook": "Tengah malam, lampu kamar, dan satu lagu ini.",
         "naskah": ["0–4 dtk: teks hook, lampu kuning temaram", "5–40 dtk: lofi malam + jangkrik jauh, teks 'tenang... kerjakan pelan-pelan'", "41–55 dtk: redup perlahan"],
         "cta": "Share ke teman begadangmu."},
        {"hook": "Coba belajar pakai ini — nilai ikut naik.",
         "naskah": ["0–4 dtk: teks hook + buku terbuka", "5–40 dtk: beat fokus, teks 'pomodoro: 25 menit, gas!'", "41–55 dtk: bel kecil, teks 'istirahat 5 menit'"],
         "cta": "Subscribe buat playlist belajar tiap minggu."},
    ],
    'anak': [
        {"hook": "Halo teman-teman! Ayo bernyanyi bersama!",
         "naskah": ["0–4 dtk: teks hook + karakter ceria melambai", "5–40 dtk: lagu ceria, teks lirik besar, ajak tepuk tangan", "41–55 dtk: semua karakter melambai, teks 'sampai jumpa!'"],
         "cta": "Subscribe ya!"},
        {"hook": "Satu, dua, tiga — ayo berhitung!",
         "naskah": ["0–4 dtk: teks hook + angka 1-2-3 muncul besar", "5–40 dtk: lagu berhitung, angka melompat satu-satu", "41–55 dtk: angka 10 besar, teks 'kamu hebat!'"],
         "cta": "Ajak temanmu subscribe!"},
        {"hook": "Tahu nggak warna pelangi ada berapa?",
         "naskah": ["0–4 dtk: teks hook + pelangi muncul", "5–40 dtk: lagu warna, tiap warna muncul dengan benda contohnya", "41–55 dtk: pelangi lengkap, teks 'sebutkan warnanya!'"],
         "cta": "Subscribe buat lagu belajar baru tiap minggu."},
        {"hook": "Ayo bergerak! Ikuti gerakanku ya!",
         "naskah": ["0–4 dtk: teks hook + karakter menari", "5–40 dtk: lagu gerak, teks 'lompat! putar! tepuk tangan!'", "41–55 dtk: gerakan pelan, teks 'istirahat dulu ya'"],
         "cta": "Subscribe ya!"},
        {"hook": "Hari ini kita belajar huruf A sampai E!",
         "naskah": ["0–4 dtk: teks hook + huruf A besar", "5–40 dtk: lagu huruf, tiap huruf muncul dengan gambar (A = Apel)", "41–55 dtk: semua huruf berjajar, teks 'kamu sudah bisa!'"],
         "cta": "Ajak temanmu subscribe!"},
    ],
    'asmr': [
        {"hook": "Bisikan lembut… dengarkan baik-baik.",
         "naskah": ["0–4 dtk: teks hook + visual gelap lembut", "5–40 dtk: bisikan pelan + ketukan halus, teks bergerak perlahan", "41–55 dtk: suara meredup, teks 'selamat tidur'"],
         "cta": "Subscribe untuk ASMR baru setiap malam."},
        {"hook": "Suara ketukan halus ini bikin rileks.",
         "naskah": ["0–4 dtk: teks hook + tangan mengetuk kayu pelan", "5–40 dtk: ketukan berirama + bisikan 'tenang…'", "41–55 dtk: ketukan melambat, teks 'tarik napas dalam'"],
         "cta": "Subscribe ya!"},
        {"hook": "Pasang headphone-mu… sekarang dengarkan.",
         "naskah": ["0–4 dtk: teks hook + ikon headphone", "5–40 dtk: suara lembut kiri-kanan, teks 'pejamkan mata…'", "41–55 dtk: suara meredup, teks 'tidur nyenyak ya'"],
         "cta": "Subscribe untuk ASMR baru setiap malam."},
        {"hook": "ASMR hujan + bisikan untuk tidur nyenyak.",
         "naskah": ["0–4 dtk: teks hook + hujan rintik", "5–40 dtk: hujan lembut + bisikan pelan, teks 'semua baik-baik saja'", "41–55 dtk: hujan meredup, teks 'sampai jumpa besok'"],
         "cta": "Subscribe ya!"},
        {"hook": "Satu menit tenang untuk pikiranmu.",
         "naskah": ["0–4 dtk: teks hook + lilin menyala", "5–40 dtk: suara lembut + teks 'tarik napas… hembuskan perlahan'", "41–55 dtk: api lilin meredup, teks 'kamu sudah tenang'"],
         "cta": "Subscribe untuk ASMR baru setiap malam."},
    ],
}


def build_shorts_ideas_report(niche_key):
    """Ide Shorts: hook 4 detik pertama + alur naskah + CTA. Niche yang
    punya bank khusus: sleep & lofi; niche lain memakai bank sleep."""
    label = _NICHES.get(niche_key, 'Deep Sleep Music')
    ideas = _SHORTS_IDEAS.get(niche_key) or _SHORTS_IDEAS['sleep']
    lines = [
        f"🎬 *Ide Shorts Buat Bos* 😎",
        f"🎯 *Niche:* `{label}`",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
    for i, idea in enumerate(ideas, 1):
        lines.append(f"*{i}. Hook:* “{idea['hook']}”")
        for beat in idea["naskah"]:
            lines.append(f"   • {beat}")
        lines.append(f"   📣 CTA: _{idea['cta']}_")
        lines.append("")
    lines.append("Tips dari saya Bos: hook harus bikin orang BERHENTI scroll dalam 4 detik pertama — pertanyaan atau janji paling ampuh 😌")
    return "\n".join(lines)


_DESC_TEMPLATES = {
    'sleep': ("Welcome to your ultimate sanctuary for relaxation and deep sleep. 🌙✨\n\n"
              "This deep sleep music compilation is carefully crafted to help ease insomnia, relieve mental fatigue, and release stress after a demanding day.\n\n"
              "🎧 Listening Advice:\n"
              "• Use low-medium volume (30-40%)\n"
              "• Dim the lights in your bedroom\n"
              "• Close your eyes and breathe gently\n\n"
              "Tracklist & Audio Engineering by Ziqva Muse Bot Cloud System.\n\n"
              "#deepsleep #relaxingmusic #sleepaid #ambientmusic #insomniarelief #calmmind"),
    'lofi': ("Welcome to your cozy corner for focus and chill. ☕🌧️\n\n"
             "This lofi beats compilation is made for late-night study sessions, deep work, and slow rainy afternoons — smooth instrumentals, warm vinyl crackle, and gentle rain in the background.\n\n"
             "🎧 Listening Advice:\n"
             "• Put on your headphones for the full vibe\n"
             "• One song, one chapter — keep going\n"
             "• Perfect for studying, reading, and working\n\n"
             "Beats & Audio Engineering by Ziqva Muse Bot Cloud System.\n\n"
             "#lofi #lofimusic #chillbeats #studybeats #cozybeats #focusmusic"),
    'piano': ("Welcome to a quiet room full of piano. 🎹🌙\n\n"
              "This relaxing piano compilation is carefully played to calm your mind, ease anxiety, and help you fall asleep or simply breathe slower after a long day.\n\n"
              "🎧 Listening Advice:\n"
              "• Use low-medium volume (30-40%)\n"
              "• Dim the lights and close your eyes\n"
              "• Let every note melt the tension away\n\n"
              "Performance & Audio Engineering by Ziqva Muse Bot Cloud System.\n\n"
              "#piano #pianomusic #relaxingpiano #calmpiano #peacefulpiano #instrumental"),
    'hujan': ("Welcome to the sound of rain. 🌧️😌\n\n"
              "This rain sounds compilation captures gentle rain on the roof, distant thunder, and soft night ambience — perfect for deep sleep, focus, and quiet evenings at home.\n\n"
              "🎧 Listening Advice:\n"
              "• Use low-medium volume (30-40%)\n"
              "• Play it all night — no sudden sounds, promise\n"
              "• Great as a background for reading and working too\n\n"
              "Recording & Audio Engineering by Ziqva Muse Bot Cloud System.\n\n"
              "#rain #rainsounds #rainyday #rainambience #sleeprain #relaxingrain"),
    'meditasi': ("Welcome to your moment of stillness. 🧘✨\n\n"
                 "This meditation music compilation blends soft tones, slow breathing cues, and gentle ambient pads to help you relax, refocus, and reconnect with yourself.\n\n"
                 "🎧 Listening Advice:\n"
                 "• Sit comfortably, back straight, shoulders relaxed\n"
                 "• Breathe in for 4 counts, out for 6 counts\n"
                 "• Use headphones for the deepest effect\n\n"
                 "Composition & Audio Engineering by Ziqva Muse Bot Cloud System.\n\n"
                 "#meditation #meditationmusic #mindfulness #healing #innerpeace #calm"),
    'fokus': ("Welcome to your deep focus zone. 🎯🎧\n\n"
              "This deep focus music compilation is designed for concentration — steady rhythms, minimal distractions, and a calm pulse that keeps your brain in flow state for hours.\n\n"
              "🎧 Listening Advice:\n"
              "• Use headphones and set a 25-minute timer\n"
              "• One task at a time — no multitasking\n"
              "• Perfect for studying, coding, and writing\n\n"
              "Composition & Audio Engineering by Ziqva Muse Bot Cloud System.\n\n"
              "#focus #focusmusic #deepfocus #studymusic #concentration #productivity"),
    'anak': ("Selamat datang di dunia lagu anak yang ceria! 🎵🌈\n\n"
             "Kompilasi lagu anak ini dibuat khusus untuk balita, PAUD, dan TK — lirik mudah diingat, melodi ceria, dan pesan yang baik untuk anak-anak. Cocok untuk belajar sambil bermain di rumah maupun di sekolah.\n\n"
             "🎧 Untuk Ayah & Bunda:\n"
             "• Putar bersama anak dan ikut bernyanyi ya\n"
             "• Ajak anak menirukan gerakan di lagu ini\n"
             "• Cocok untuk waktu bermain, belajar, dan sebelum tidur\n\n"
             "Lagu & Produksi oleh Ziqva Muse Bot Cloud System.\n\n"
             "#laguanak #anakkecil #belajarberhitung #laguceria #anakcerdas #laguedukasi"),
    'asmr': ("Selamat datang di dunia ASMR yang menenangkan! 🎧✨\n\n"
             "Kompilasi ASMR ini berisi bisikan lembut, ketukan halus, dan suara menenangkan lainnya — dibuat untuk membantumu rileks, mengurangi stres, dan tidur lebih nyenyak. Gunakan headphone untuk pengalaman terbaik.\n\n"
             "🎧 Saran Mendengarkan:\n"
             "• Pasang headphone dan cari posisi paling nyaman\n"
             "• Pejamkan mata dan fokus pada suara lembutnya\n"
             "• Cocok untuk sebelum tidur, relaksasi, dan menenangkan pikiran\n\n"
             "Produksi oleh Ziqva Muse Bot Cloud System.\n\n"
             "#asmr #asmrwhisper #asmrsleep #asmrrelax #relaxingasmr #sleepasmr"),
}


def build_description_report(niche_key, niche_label):
    """Template deskripsi SEO per niche — tinggal salin ke panel."""
    body = _DESC_TEMPLATES.get(niche_key, _DESC_TEMPLATES['sleep'])
    return (
        f"📝 *Template Deskripsi SEO buat Bos* 😎\n"
        f"🎯 *Niche:* `{niche_label}`\n"
        "━━━━━━━━━━━━━━━━━━━━━━\n"
        f"{body}"
    )

# ==========================================
# 🎬 BUAT VIDEO LEWAT TELEGRAM — ALUR TERPANDU /buatshort & /buatlong
# ==========================================
# Bahasa dibuat sederhana (Bos masih awam): tiap langkah hanya minta
# SATU jawaban. /batal membatalkan kapan saja. Draft tersimpan di
# monitor_state.json (tahan restart), hangus setelah 30 menit diam.
_MAIN_BUTTON_TEXTS = {
    "status server", "kuota upload", "cek channel", "antrean render",
    "jadwal 7 hari", "statistik video", "monetisasi", "task gagal",
    "10 judul sleep", "ide shorts", "template deskripsi", "blueprint resep",
    "buat short", "buat video long", "buat playlist",
    "backup", "laporan",
}

def _make_short_summary(d):
    hook = (d.get('hook') or '—')
    script = (d.get('script') or '—')
    bg_label = d.get('bg_file') or ''
    if not bg_label:
        n_vid = d.get('bg_video_count') or 0
        if n_vid:
            bg_label = f"semua klip video ({n_vid} klip, disambung)"
        elif d.get('bg_options'):
            bg_label = "acak dari galeri"
        else:
            bg_label = "gradien otomatis"
    sched_txt = d.get('publish_date', '')
    if d.get('publish_note'):
        sched_txt += f" ({d['publish_note']})"
    return (
        "📋 *Cek dulu ya Bos, ini Shorts yang mau dibuat:* 🎬\n"
        f"📺 Channel: *{_tg_escape(d.get('channel_name', ''))}*\n"
        f"🎬 Judul: *{_tg_escape(d.get('title', ''))}*\n"
        f"🪝 Hook (4 detik): {_tg_escape(hook[:150])}{'…' if len(hook) > 150 else ''}\n"
        f"📝 Naskah: {_tg_escape(script[:200])}{'…' if len(script) > 200 else ''}\n"
        f"📣 CTA: {_tg_escape(d.get('cta', 'Subscribe'))}\n"
        f"⏱️ Durasi: `{d.get('duration_sec', 30)} detik`\n"
        f"🗓️ Jadwal: `{_tg_escape(sched_txt)}`\n"
        f"🖼️ Background: `{_tg_escape(bg_label)}`\n"
        f"🎵 Musik: `{_tg_escape(d.get('bgm_file') if d.get('bgm_file') not in (None, '', 'none') else 'musik bawaan')}`\n"
        "🎙️ Suara: `voiceover + musik` (suara membacakan naskah)\n\n"
        "Kalau sudah oke, pencet ✅ Buat. Kalau mau ubah, pencet ❌ Batal lalu mulai lagi dari /buatshort ya Bos!"
    )

def _make_long_summary(d):
    start_txt = d.get('start_date', '')
    if d.get('start_note'):
        start_txt += f" ({d['start_note']})"
    lines = [
        "📋 *Cek dulu ya Bos, ini batch video long yang mau dibuat:* 🎥",
        f"📺 Channel: *{_tg_escape(d.get('channel_name', ''))}*",
        f"🎬 Jumlah: `{d.get('count', 1)} video` (durasi tiap video diacak 60–80 menit)",
        f"🗓️ Mulai: `{_tg_escape(start_txt)}` • Jeda antar video: `{d.get('interval_days', 1)} hari`",
        "📝 Judul:",
    ]
    titles = d.get('titles') or []
    if titles:
        for i, t in enumerate(titles[:7], 1):
            lines.append(f"  {i}. {_tg_escape(t)}")
    else:
        lines.append("  _diambil dari Bank Judul channel_")
    lines.append("\nKalau sudah oke, pencet ✅ Buat. Kalau mau ubah, pencet ❌ Batal lalu mulai lagi dari /buatlong ya Bos!")
    return "\n".join(lines)

def _send_make_created_message(token, chat_id, kind, d, resp):
    """Pesan sukses setelah task dibuat — jelaskan apa yang terjadi
    selanjutnya (menunggu approval vs langsung antre)."""
    approval = (resp or {}).get('approval', 'pending')
    if kind == 'short':
        lines = [
            "✅ *Sip Bos, Shorts-nya udah saya buatkan!* 🎬",
            f"🎬 Judul: *{_tg_escape(d.get('title', ''))}*",
            f"📺 Channel: *{_tg_escape(d.get('channel_name', ''))}*",
            f"🗓️ Jadwal: `{_tg_escape((resp or {}).get('publish_date') or d.get('publish_date', ''))}`",
            f"⏱️ Durasi: `{d.get('duration_sec', 30)} detik`",
        ]
        if (resp or {}).get('bg_video'):
            lines.append(f"🎬 Background: video ({(resp or {}).get('bg_video_count', 0)} klip, disambung)")
        elif (resp or {}).get('bg_auto'):
            lines.append("🖼️ Background: gradien otomatis (galeri masih kosong — upload gambar sendiri di panel bila mau yang lebih bagus)")
    else:
        tasks = (resp or {}).get('tasks') or []
        lines = [
            f"✅ *Sip Bos, {resp.get('count', d.get('count', 1))} video long udah saya buatkan!* 🎥",
            f"📺 Channel: *{_tg_escape(d.get('channel_name', ''))}*",
        ]
        for t in tasks[:7]:
            lines.append(f"• {_tg_escape(t.get('title', ''))} — `{_tg_escape(t.get('publish_date', ''))}`")
    if approval == 'pending':
        lines += ["",
                  "⏳ *Video menunggu persetujuan Bos dulu ya* — preview-nya bakal masuk ke chat ini, "
                  "tinggal pencet ✅ Setujui biar langsung dirender & upload."]
    else:
        lines += ["",
                  "🚀 *Langsung masuk antrean render (mode Full-Auto)* — ditunggu kabar selesainya ya Bos! 💪"]
    send_message(token, chat_id, "\n".join(lines))

def _make_pick_channel(channels, multi=False):
    """Kembalikan (channel_terpilih_atau_None, pesan_pilih_atau_None).
    1 channel → langsung pakai; >1 → minta pilih nomor.
    multi=True → boleh jawab 'semua channel' (Short & Playlist)."""
    if not channels:
        return None, None
    if len(channels) == 1:
        return channels[0], None
    lines = ["📺 *Bos punya lebih dari 1 channel nih, pilih dulu ya:*\n"]
    for i, c in enumerate(channels, 1):
        mark = " ⏸️ Dijeda" if c.get('paused') else ""
        lines.append(f"{i}. {_tg_escape(_channel_display_name(c))}{mark}")
    if multi:
        lines.append("\nBalas dengan angkanya saja (mis. `1`), ketik nama channelnya, atau `semua channel` untuk semua channel.")
    else:
        lines.append("\nBalas dengan angkanya saja (mis. `1`) atau ketik nama channelnya.")
    return None, "\n".join(lines)

def _make_check_bg_or_cancel(token, chat_id, draft):
    """Cek galeri background channel untuk Shorts. Galeri kosong TIDAK
    lagi membatalkan — panel membuat gradien otomatis sekali saat Short
    dibuat (helper _create_shorts_muse_task). Kembalikan True bila boleh
    lanjut (selalu True selama channel ada)."""
    d = draft.get('data') or {}
    bgs = _gallery_files(d.get('yt_id', ''), 'backgrounds', _BG_EXTS)
    d['bg_options'] = bgs
    d['bg_video_count'] = sum(1 for f in bgs
                              if f.lower().endswith(('.mp4', '.mov', '.webm')))
    draft['data'] = d
    return True

def _make_ask_step(token, chat_id, draft):
    """Kirim pertanyaan untuk langkah draft saat ini."""
    d = draft.get('data') or {}
    step = draft.get('step')
    kind = draft.get('kind')
    if step == 'channel':
        if kind in ('zumo', 'zumobank', 'zumorakit'):
            channels = load_channels()
            if kind == 'zumobank':
                head = "Bank Audio Zumo — langkah 1 dari 5: Channel\n\nLagu bank mau disimpan untuk channel mana Bos?"
            elif kind == 'zumorakit':
                head = "Rakit Video dari Bank — langkah 1 dari 3: Channel\n\nVideo rakitan mau di-upload ke channel mana Bos?"
            else:
                head = "Buat Playlist Zumo — langkah 1 dari 8: Channel\n\nLagu mau di-upload ke channel mana Bos?"
            lines = [head]
            for i, c in enumerate(channels, 1):
                lines.append(f"{i}. {_tg_escape(_channel_display_name(c))}")
            lines.append("\nBalas angkanya atau ketik namanya. Ketik `lewati` untuk pakai Cozy Sax Relaxing (default).")
            send_message(token, chat_id, "\n".join(lines))
            return
        _ch, msg = _make_pick_channel(load_channels(), multi=(kind in ('short', 'playlist')))
        if msg:
            send_message(token, chat_id, msg)
        return
    if kind == 'zumo':
        if step == 'theme':
            send_message(token, chat_id,
                         "Buat Playlist Zumo — langkah 2 dari 8: Tema lagu\n\nTema lagunya apa Bos? (mis. `saxophone romantis untuk dinner`, `reggae santai`)")
        elif step == 'mode':
            send_message(token, chat_id, _zumo_mode_ask_text("Langkah 3 dari 8"))
        elif step == 'style':
            default_note = (" (Cozy Sax: smooth jazz saxophone, warm, relaxing, romantic, cozy cafe)"
                            if d.get('yt_id') == _ZUMO_YT_ID else "")
            send_message(token, chat_id,
                         _zumo_style_ask_text("Langkah 4 dari 8", default_note))
        elif step == 'lyrics':
            send_message(token, chat_id,
                         "Langkah 5 dari 8: Lirik\n\nTempel lirik lengkap gaya Suno — tiap bagian diawali [Bagian: arahan], mis. [Verse 1: ...], [Chorus: ...]. Tag [..] itu instruksi cara menyanyi/memainkan, bukan dinyanyikan, dan saya kirim utuh apa adanya.\n\nAtau ketik `auto` supaya lirik dibuatkan otomatis (bahasa & vokal ikut profil channel).")
        elif step == 'count':
            if d.get('lyrics_mode') == 'pasted':
                send_message(token, chat_id,
                             "Langkah 6 dari 8: Jumlah judul lagu\n\nSudah ada 1 judul + lirik tempel. Mau total berapa judul Bos? Balas `1` (hanya lagu itu), `2`, atau `3` — judul sisanya dibuatkan otomatis oleh server (judul + lirik).\nKetik `lewati` untuk 1 judul.")
            else:
                send_message(token, chat_id,
                             "Langkah 6 dari 8: Jumlah judul lagu\n\nMau berapa judul lagu Bos? Balas `2` atau `3` (tiap judul jadi 2 varian, jadi total 4 atau 6 lagu).\nKetik `lewati` untuk 3 judul.")
        elif step == 'particle':
            send_message(token, chat_id,
                         "Langkah 7 dari 8: Partikel\n\nPartikelnya salju atau sakura Bos? Balas `salju` atau `sakura`.\nBoleh tambah warna spectrum: `putih` (default), `cyan`, atau `viridis` — mis. `sakura cyan`.\nKetik `lewati` untuk salju + spectrum putih.")
        elif step == 'publish':
            nxt = _next_slot_for_channel(next((c for c in load_channels()
                                              if c.get('yt_id') == d.get('yt_id')), None))
            send_message(token, chat_id,
                         "Langkah 8 dari 8: Jadwal tayang\n\nTulis jamnya — mis. `hari ini jam 9`, `besok jam 7 malam`, atau `2026-11-20 19:00`.\n"
                         f"Ketik `lewati` untuk jadwal berikutnya (`{nxt}`, server yang atur) atau `publish` untuk langsung tayang setelah render.")
        elif step == 'confirm':
            send_message(token, chat_id, _make_zumo_summary(d),
                         reply_markup={"inline_keyboard": [
                             [{"text": "✅ Buat", "callback_data": "mkzumo:yes"},
                              {"text": "❌ Batal", "callback_data": "mkzumo:no"}]]})
        return
    if kind == 'zumobank':
        if step == 'theme':
            send_message(token, chat_id,
                         "Bank Audio Zumo — langkah 2 dari 5: Tema lagu\n\nTema lagunya apa Bos? (mis. `jazz instrumental santai`, `reggae akustik`)")
        elif step == 'mode':
            send_message(token, chat_id, _zumo_mode_ask_text("Langkah 3 dari 5"))
        elif step == 'style':
            default_note = (" (Cozy Sax: smooth jazz saxophone, warm, relaxing, romantic, cozy cafe)"
                            if d.get('yt_id') == _ZUMO_YT_ID else "")
            send_message(token, chat_id,
                         _zumo_style_ask_text("Langkah 4 dari 5", default_note))
        elif step == 'bank_count':
            send_message(token, chat_id,
                         "Langkah 5 dari 5: Jumlah lagu\n\nMau berapa lagu disimpan ke bank Bos? Balas angkanya `1`-`30` (mis. `30`).\nKetik `lewati` untuk 10 lagu.")
        elif step == 'confirm':
            send_message(token, chat_id, _make_zumo_bank_summary(d),
                         reply_markup={"inline_keyboard": [
                             [{"text": "✅ Buat", "callback_data": "mkzumbank:yes"},
                              {"text": "❌ Batal", "callback_data": "mkzumbank:no"}]]})
        return
    if kind == 'zumorakit':
        if step == 'bank_count':
            send_message(token, chat_id,
                         "Rakit Video dari Bank — langkah 2 dari 3: Jumlah lagu\n\nMau berapa lagu dari bank dipakai Bos? Balas angkanya `1`-`30`.\nKetik `lewati` untuk 8 lagu.")
        elif step == 'publish':
            nxt = _next_slot_for_channel(next((c for c in load_channels()
                                              if c.get('yt_id') == d.get('yt_id')), None))
            send_message(token, chat_id,
                         "Langkah 3 dari 3: Jadwal tayang\n\nTulis jamnya — mis. `hari ini jam 9`, `besok jam 7 malam`, atau `2026-11-20 19:00`.\n"
                         f"Ketik `lewati` untuk jadwal berikutnya (`{nxt}`, server yang atur) atau `publish` untuk langsung tayang setelah render.")
        elif step == 'confirm':
            send_message(token, chat_id, _make_zumo_assemble_summary(d),
                         reply_markup={"inline_keyboard": [
                             [{"text": "✅ Buat", "callback_data": "mkzumrakit:yes"},
                              {"text": "❌ Batal", "callback_data": "mkzumrakit:no"}]]})
        return
    if kind == 'playlist':
        if step == 'theme':
            send_message(token, chat_id,
                         "🎵 *Buat Playlist — langkah 1: Tema musik*\n\nTema musiknya apa Bos? (mis. `lagu malaysia lawas`, `lofi`, `piano santai`)")
        elif step == 'pixabay':
            send_message(token, chat_id,
                         "🖼️ *Langkah 2: Tema footage Pixabay*\n\nTema footage-nya apa Bos? (mis. `autumn`, `pantai`, `kota malam`)\nKetik `lewati` untuk auto (pakai tema musik).")
        elif step == 'duration':
            # 🔥 v1.3.56: tombol durasi tinggal klik + Custom (ketik sendiri).
            # Nilai tombol dipakai ulang oleh parser yang sama
            # (_parse_playlist_duration) di callback mkplaylist:dur:*.
            send_message(token, chat_id,
                         "⏱️ *Langkah 3: Durasi video*\n\nDurasi videonya berapa menit, Bos? Pencet salah satu di bawah, atau ketik sendiri.",
                         reply_markup={"inline_keyboard": [
                             [{"text": "40–70 menit", "callback_data": "mkplaylist:dur:40-70"},
                              {"text": "1 jam", "callback_data": "mkplaylist:dur:1jam"}],
                             [{"text": "60 menit", "callback_data": "mkplaylist:dur:60"},
                              {"text": "Semua durasi", "callback_data": "mkplaylist:dur:all"}],
                             [{"text": "✏️ Custom (ketik sendiri)", "callback_data": "mkplaylist:dur:custom"}]]})
        elif step == 'research':
            _pl_run_research(token, chat_id, draft)
        elif step == 'pick':
            _pl_send_candidates(token, chat_id, draft)
        elif step == 'publish':
            nxt = _next_slot_for_channel(next((c for c in load_channels()
                                              if c.get('yt_id') == d.get('yt_id')), None))
            send_message(token, chat_id,
                         "🗓️ *Langkah 5: Jadwal tayang*\n\nKetik `publish` untuk langsung tayang setelah render (tanpa jadwal), "
                         "atau tulis jamnya — mis. `hari ini jam 9`, `besok jam 7 malam`, atau `2026-11-20 19:00`.\n"
                         f"Ketik `lewati` atau `default` untuk jadwal berikutnya (`{nxt}`).")
        elif step == 'confirm':
            send_message(token, chat_id, _make_playlist_summary(d),
                         reply_markup={"inline_keyboard": [
                             [{"text": "✅ Buat", "callback_data": "mkplaylist:yes"},
                              {"text": "❌ Batal", "callback_data": "mkplaylist:no"}]]})
        return
    if kind == 'short':
        if step == 'theme':
            send_message(token, chat_id,
                         "🎬 *Buat Short — langkah 2 dari 3: Tema atau judul*\n\n"
                         "Tema atau judulnya apa, Bos? (mis. `anak-anak`, `tidur`, atau ketik judul sendiri)")
        elif step == 'publish':
            nxt = _next_slot_for_channel(next((c for c in load_channels()
                                              if c.get('yt_id') == d.get('yt_id')), None))
            send_message(token, chat_id,
                         "🗓️ *Langkah 3 dari 3: Jadwal tayang*\n\nTulis jamnya — mis. `hari ini jam 9`, `besok jam 7 malam`, atau `2026-11-20 19:00`.\n"
                         f"Ketik `lewati` untuk jadwal berikutnya (`{nxt}`).")
        elif step == 'confirm':
            send_message(token, chat_id, _make_short_summary(d),
                         reply_markup={"inline_keyboard": [
                             [{"text": "✅ Buat", "callback_data": "mkshort:yes"},
                              {"text": "❌ Batal", "callback_data": "mkshort:no"}]]})
        else:
            # Langkah lama dari draft sebelum v1.2.19 — mulai ulang alur pendek.
            _short_restart_legacy(token, chat_id)
    else:  # long
        if step == 'count':
            send_message(token, chat_id,
                         "🎥 *Buat Video Long — langkah 1 dari 4: Jumlah*\n\nMau buat berapa video? Balas angkanya (`1`–`7`).")
        elif step == 'titles':
            send_message(token, chat_id,
                         "📝 *Langkah 2 dari 4: Judul*\n\nKirim judul videonya, pisahkan dengan `|` kalau lebih dari 1.\n"
                         "Ketik `lewati` untuk pakai judul dari Bank Judul channel.")
        elif step == 'start':
            send_message(token, chat_id,
                         "🗓️ *Langkah 3 dari 4: Tanggal mulai*\n\nTulis tanggal & jam video pertama — mis. `hari ini jam 9`, `besok jam 7 malam`, atau `2026-11-20 19:00`.\n"
                         "Tanggal saja juga boleh (jam jadi 19:00). Ketik `lewati` untuk besok jam 19:00.")
        elif step == 'interval':
            send_message(token, chat_id,
                         "⏳ *Langkah 4 dari 4: Jeda antar video*\n\nBerapa hari jeda tiap video? Balas angkanya (mis. `1`).\nKetik `lewati` untuk 1 hari.")
        elif step == 'confirm':
            send_message(token, chat_id, _make_long_summary(d),
                         reply_markup={"inline_keyboard": [
                             [{"text": "✅ Buat", "callback_data": "mklong:yes"},
                              {"text": "❌ Batal", "callback_data": "mklong:no"}]]})

# --- v1.2.19: alur /buatshort dipersingkat jadi 3 pertanyaan ---
# Langkah lama (title/hook/script/cta/duration/bg/bgm) — draft yang masih
# menyimpan salah satunya (dibuat sebelum v1.2.19) dimulai ulang ramah.
_SHORT_LEGACY_STEPS = ('title', 'hook', 'script', 'cta', 'duration', 'bg', 'bgm')
_SHORT_THEME_FILLERS = ('tema', 'temanya', 'buat', 'buatkan', 'bikin', 'short',
                        'video', 'yang', 'tentang', 'niche', 'tolong', 'dong')


def _short_detect_theme(ans):
    """Kunci niche dari jawaban tema, atau None bila jawabannya judul custom.
    Hanya jawaban PENDEK (≤2 kata setelah buang kata pengisi) yang dianggap
    tema — judul custom seperti 'Musik Tidur Nyenyak' (3 kata) tidak
    tertangkap jadi tema. Urutan _NLU_NICHE_ORDER supaya 'asmr buat tidur'
    jadi asmr, bukan sleep."""
    words = [re.sub(r'[^\w]', '', w)
             for w in (ans or '').lower().strip().split()]
    words = [w for w in words if w and w not in _SHORT_THEME_FILLERS]
    if not words or len(words) > 2:
        return None
    joined = ' '.join(words)
    for niche in _NLU_NICHE_ORDER:
        for w in _NLU_NICHE_WORDS.get(niche, ()):
            if w in joined:
                return niche
    return None


def _short_fallback_bank(d):
    """(bank, kunci_niche) untuk hook/naskah/CTA bila judulnya custom:
    tema auto-shorts channel bila diset di panel, sonst 'sleep' — tema
    default di semua tempat (panel, NLU, _parse_niche_arg). Bukan 'anak':
    bank anak (lagu ceria + ajakan subscribe anak) hanya dipakai bila Bos
    memang meminta tema anak."""
    ch_theme = None
    try:
        ch = next((c for c in load_channels()
                   if c.get('yt_id') == d.get('yt_id')), None)
        raw = (ch or {}).get('auto_shorts') or {}
        if isinstance(raw, dict):
            ch_theme = str(raw.get('theme') or '').strip().lower()
    except Exception:
        ch_theme = None
    niche = ch_theme if ch_theme in _NLU_SHORTS_BANK else 'sleep'
    return _NLU_SHORTS_BANK[niche], niche


def _short_fill_from_bank(d):
    """Isi hook/naskah/CTA yang masih kosong dari bank tema — dipakai bila
    judul datang dari one-shot /buatshort tanpa hook/naskah, dan sebagai
    jaring pengaman sebelum task dibuat. Tidak menimpa yang sudah ada."""
    niche = d.get('theme') if d.get('theme') in _NLU_SHORTS_BANK else None
    if niche:
        bank = _NLU_SHORTS_BANK[niche]
    else:
        bank, niche = _short_fallback_bank(d)
    day_idx = _wib_now().timetuple().tm_yday
    if not d.get('theme'):
        d['theme'] = niche
    if not d.get('hook'):
        d['hook'] = bank['hooks'][day_idx % len(bank['hooks'])]
    if not d.get('script'):
        d['script'] = bank['scripts'][day_idx % len(bank['scripts'])]
    if not d.get('cta'):
        d['cta'] = bank['ctas'][day_idx % len(bank['ctas'])]
    return d


def _short_restart_legacy(token, chat_id):
    """Draft lama (langkah 7+ dari sebelum v1.2.19) → mulai ulang alur
    pendek dengan pesan sopan, tanpa crash."""
    _clear_make_draft(chat_id)
    send_message(token, chat_id,
                 "😌 *Eh Bos, alur buat Short sekarang sudah lebih singkat — cuma 3 pertanyaan aja* 🎬\n"
                 "Draft lamanya saya mulai ulang ya, biar nggak bingung.")
    _start_make_short(token, chat_id, '')


def _short_next_step(d):
    """Langkah pertama yang masih kosong untuk Short — alur pendek v1.2.19:
    channel → tema/judul → jadwal → konfirmasi. Judul dari one-shot
    sekali kirim langsung lompat ke jadwal."""
    if not d.get('title'):
        return 'theme'
    if not d.get('publish_date'):
        return 'publish'
    return 'confirm'

def _long_next_step(d):
    """Langkah pertama yang masih kosong untuk Long. 'titles' kosong
    (lewati = pakai bank judul) tetap dihitung terisi bila kuncinya ada."""
    if not d.get('count'):
        return 'count'
    if 'titles' not in d:
        return 'titles'
    if not d.get('start_date'):
        return 'start'
    if not d.get('interval_days'):
        return 'interval'
    return 'confirm'

def _start_make_short(token, chat_id, raw_cmd):
    """Mulai alur /buatshort. Dukung sekali kirim:
    /buatshort Judul | Hook | Naskah | CTA — bagian yang sudah diisi
    tidak ditanya lagi."""
    channels = load_channels()
    if not channels:
        send_message(token, chat_id,
                     "😅 *Bos, belum ada channel yang terhubung nih*\n\n"
                     "Tautkan channel dulu di panel (Settings → Tautkan Channel Baru), "
                     "lalu ketik /buatshort lagi ya Bos!")
        return
    pre = {}
    rest = raw_cmd[len('/buatshort'):].strip() if raw_cmd.strip().lower().startswith('/buatshort') else ''
    if rest:
        parts = [p.strip() for p in rest.split('|')]
        if len(parts) > 0 and parts[0]:
            pre['title'] = parts[0][:200]
        if len(parts) > 1 and parts[1]:
            pre['hook'] = parts[1][:500]
        if len(parts) > 2 and parts[2]:
            pre['script'] = parts[2][:2000]
        if len(parts) > 3 and parts[3]:
            pre['cta'] = parts[3][:100]
    ch, ask_msg = _make_pick_channel(channels, multi=True)
    draft = {"kind": "short", "step": "theme", "data": dict(pre)}
    if ch is None:
        draft['step'] = 'channel'
        _save_make_draft(chat_id, draft)
        send_message(token, chat_id,
                     "🎬 *Siap Bos, kita buat Short bareng-bareng ya!* 😎\n"
                     "Cuma 3 pertanyaan aja — sisanya saya atur otomatis 😎\nKetik /batal kapan saja untuk berhenti.\n\n" + ask_msg)
        return
    draft['data']['yt_id'] = ch.get('yt_id')
    draft['data']['channel_name'] = _channel_display_name(ch)
    if not _make_check_bg_or_cancel(token, chat_id, draft):
        return
    # Lompati langkah yang sudah terisi dari sekali kirim.
    draft['step'] = _short_next_step(draft['data'])
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 f"🎬 *Siap Bos, kita buat Short buat channel {_tg_escape(draft['data']['channel_name'])} ya!* 😎\n"
                 "Cuma 3 pertanyaan aja — sisanya saya atur otomatis 😎\nKetik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)

def _start_make_long(token, chat_id, raw_cmd):
    """Mulai alur /buatlong. Dukung sekali kirim:
    /buatlong 3 | 2026-11-20 19:00 | 1 (jumlah | tanggal | jeda hari)."""
    channels = load_channels()
    if not channels:
        send_message(token, chat_id,
                     "😅 *Bos, belum ada channel yang terhubung nih*\n\n"
                     "Tautkan channel dulu di panel (Settings → Tautkan Channel Baru), "
                     "lalu ketik /buatlong lagi ya Bos!")
        return
    pre = {}
    rest = raw_cmd[len('/buatlong'):].strip() if raw_cmd.strip().lower().startswith('/buatlong') else ''
    if rest:
        parts = [p.strip() for p in rest.split('|')]
        try:
            if len(parts) > 0 and parts[0]:
                pre['count'] = max(1, min(7, int(parts[0])))
        except Exception:
            pass
        if len(parts) > 1 and parts[1]:
            parsed, note = _parse_natural_datetime(parts[1])
            if parsed:
                pre['start_date'] = parsed
                if note:
                    pre['start_note'] = note
        try:
            if len(parts) > 2 and parts[2]:
                pre['interval_days'] = max(0.5, min(30.0, float(parts[2])))
        except Exception:
            pass
    ch, ask_msg = _make_pick_channel(channels)
    draft = {"kind": "long", "step": "count", "data": dict(pre)}
    if ch is None:
        draft['step'] = 'channel'
        _save_make_draft(chat_id, draft)
        send_message(token, chat_id,
                     "🎥 *Siap Bos, kita buat batch video long bareng-bareng ya!* 😎\n"
                     "Jawab tiap langkah satu per satu — ketik /batal kapan saja untuk berhenti.\n\n" + ask_msg)
        return
    draft['data']['yt_id'] = ch.get('yt_id')
    draft['data']['channel_name'] = _channel_display_name(ch)
    draft['step'] = _long_next_step(draft['data'])
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 f"🎥 *Siap Bos, kita buat batch video long buat channel {_tg_escape(draft['data']['channel_name'])} ya!* 😎\n"
                 "Jawab tiap langkah satu per satu — ketik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)


# 🎵 MUSIK PLAYLIST LEWAT TELEGRAM — ALUR TANYA-JAWAB /playlist & /buatplaylist
# ==========================================
# /playlist polos → menu (Buat Baru / Lihat Job). "Buat Baru" atau
# /playlist <teks bebas> atau /buatplaylist → bot bertanya satu per satu:
# channel → tema musik → tema footage (lewati = auto) → durasi (lewati =
# semua durasi; '40-70', '1 jam', '60') → riset → pilih kandidat
# (tombol / angka / 🤖 Auto) → jadwal (lewati/default = slot berikutnya)
# → ringkasan + ✅ Buat / ❌ Batal. Draft per-chat seperti /buatshort
# (tahan restart, hangus >30 menit, batal via /batal). /buatplaylist
# adalah alias + one-shot (tema | channel | tema pixabay) yang memanggil
# alur yang SAMA; slot yang belum lengkap ditanyakan, bukan digagalkan.
# Mode AUTO: tombol '🤖 Auto — pilih yang terbaik' di langkah pilih,
# ketik 'auto' di langkah pilih, segmen 'auto' di one-shot, atau kata
# 'auto'/'otomatis' di NLU → _playlist_pick_best memilih bertingkat
# (v1.3.5 koreksi: tier berbasis UMUR saja — 30 hari, 90 hari, 1 tahun,
# evergreen, lalu 'link'; TANPA filter views, views berapa pun boleh).
# Di tiap tier pemenangnya views tertinggi (seri → durasi terpanjang;
# tier 'link': durasi terpanjang). Durasi filter tetap berlaku bila
# user mengisi rentang, KECUALI tier 'link'. Bila tidak ada yang
# lolos → pesan jelas + pilihan manual, tidak asal pilih.
def _parse_playlist_duration(text):
    """Parse jawaban durasi playlist → (min_sec, max_sec, note) atau None.
    '40-70' / '40 - 70 menit' / '40 sampai 70 menit' → (2400, 4200).
    'durasi minimal 40 menit' → (2400, None, 'minimal 40 menit').
    '1 jam' → sekitar 60 menit: (3000, 4200) + note menyebutkannya
    (TIDAK ditebak diam-diam jadi 40-70). Angka tunggal '60' / '60 menit'
    → sekitar 60 menit: (3300, 3900). 'lewati'/'semua' → (None, None, '')
    = tanpa filter. Tidak dikenali → None (pemanggil bertanya ulang)."""
    t = (text or '').strip().lower()
    t = re.sub(r'^durasi\s+', '', t).strip()
    t = re.sub(r'\s+', ' ', t)
    if not t or t in ('lewati', '-', 'skip', 'semua', 'bebas', 'auto', 'default'):
        return (None, None, '')
    # v1.3.5 koreksi: 'minimal 40 menit' -> min saja (2400, None);
    # 'maksimal 40 menit' -> max saja (None, 2400).
    m = re.match(r'^(minimal|minimum|paling tidak|setidaknya)\s+(\d+(?:[.,]\d+)?)\s*(jam|menit|mnt)?\b', t)
    if m:
        x = float(m.group(2).replace(',', '.'))
        mult = 3600 if m.group(3) == 'jam' else 60
        return (int(x * mult), None, f"minimal {m.group(2).replace(',', '.')} {'jam' if m.group(3) == 'jam' else 'menit'}")
    m = re.match(r'^(maksimal|maksimum|paling banyak)\s+(\d+(?:[.,]\d+)?)\s*(jam|menit|mnt)?\b', t)
    if m:
        x = float(m.group(2).replace(',', '.'))
        mult = 3600 if m.group(3) == 'jam' else 60
        return (None, int(x * mult), f"maksimal {m.group(2).replace(',', '.')} {'jam' if m.group(3) == 'jam' else 'menit'}")
    # Rentang: 40-70 / 40 - 70 / 40 sampai 70 / 40 s/d 70 (+ satuan opsional).
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:-|–|—|sampai|s/d|sd|hingga)\s*(\d+(?:[.,]\d+)?)\s*(jam|menit|mnt)?\b', t)
    if m:
        a = float(m.group(1).replace(',', '.'))
        b = float(m.group(2).replace(',', '.'))
        unit = m.group(3) or 'menit'
        mult = 3600 if unit == 'jam' else 60
        lo, hi = int(a * mult), int(b * mult)
        if lo > hi:
            lo, hi = hi, lo
        return (lo, hi, f"{int(a)}–{int(b)} {'jam' if unit == 'jam' else 'menit'}")
    # 'X jam' (mis. 1 jam, 1,5 jam) → sekitar X jam: ±10 menit.
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*jam\b', t)
    if m:
        x = float(m.group(1).replace(',', '.'))
        c = int(x * 3600)
        lo, hi = max(0, c - 600), c + 600
        return (lo, hi, f"sekitar {m.group(1).replace(',', '.')} jam ({lo // 60}–{hi // 60} menit)")
    # Angka tunggal menit: '60' / '60 menit' / '60 mnt' → ±5 menit.
    m = re.fullmatch(r'(\d+)\s*(menit|mnt)?', t)
    if m:
        n = int(m.group(1))
        lo, hi = max(0, (n - 5) * 60), (n + 5) * 60
        return (lo, hi, f"sekitar {n} menit ({lo // 60}–{hi // 60} menit)")
    return None


def _playlist_next_step(d):
    """Langkah pertama yang masih kosong untuk Playlist."""
    if not d.get('yt_id'):
        return 'channel'
    if not d.get('theme') and not d.get('source_url'):
        return 'theme'
    if 'pixabay_theme' not in d:
        return 'pixabay'
    if 'duration_min_sec' not in d:
        return 'duration'
    if not d.get('job_id') or not d.get('candidates'):
        return 'research'
    if not d.get('video_id'):
        return 'pick'
    if not d.get('publish_date'):
        return 'publish'
    return 'confirm'


# Kata jawaban "langsung tayang" di langkah jadwal Playlist (Langkah 5):
# selesai render → upload langsung (publik, tanpa publishAt). Mekanisme:
# publish_date diisi jam SAAT INI (WIB) — backend menganggap jadwal yang
# sudah lewat = tayang langsung (tidak memasang publishAt, privacy tetap
# 'public'), sedangkan publish_date kosong justru diam-diam dijadikan
# besok 19:00 oleh backend (_playlist_do_create di app.py). Karena
# render butuh waktu, jam ini PASTI sudah lewat saat upload tiba.
_PLAYLIST_PUBLISH_NOW_WORDS = ('publish', 'langsung', 'tayang sekarang',
                               'tanpa jadwal')


def _playlist_publish_now_date():
    """Tanggal tayang untuk mode 'langsung': jam sekarang (WIB)."""
    return _wib_now().strftime('%Y-%m-%d %H:%M')


def _stagger_publish_date(base, index, step_min=15):
    """🔥 v1.3.30: 'YYYY-MM-DD HH:MM' + index*step menit — jadwal
    beruntun per channel (channel ke-0 tayang jam dasar, ke-1 +15 mnt,
    dst.). Gagal parse → kembalikan base apa adanya."""
    try:
        # 🔥 v1.3.31: modul ini memakai `from datetime import datetime,
        # timedelta` (datetime = KELAS) — datetime.datetime/timedelta
        # melempar AttributeError diam-diam sehingga semua channel
        # dapat jam dasar yang sama (10:15 WIB: 7 channel semua 16:00).
        dt = datetime.strptime(base, '%Y-%m-%d %H:%M') + timedelta(minutes=step_min * int(index))
        return dt.strftime('%Y-%m-%d %H:%M')
    except Exception:
        return base


def _playlist_schedule_text(d):
    """Teks jadwal untuk ringkasan/pesan sukses: kalimat 'Langsung tayang'
    bila publish_now, else tanggal terjadwal + catatan tafsiran waktu
    alami bila ada (mis. '2026-11-19 09:00 (hari ini jam 9)')."""
    if d.get('publish_now'):
        return "Langsung tayang setelah render (tanpa jadwal)"
    pd = d.get('publish_date') or ''
    note = d.get('publish_note')
    if pd and note:
        return f"{pd} ({note})"
    return pd


def _make_playlist_summary(d):
    cand = next((c for c in (d.get('candidates') or [])
                 if c.get('video_id') == d.get('video_id')), None) or {}
    views = cand.get('views') or 0
    views_txt = f"{views:,}".replace(',', '.')
    dur_mnt = round((cand.get('duration_sec') or 0) / 60)
    lines = [
        "📋 *Cek dulu ya Bos, ini playlist yang mau dibuat:* 🎵",
        f"📺 Channel: *{_tg_escape(d.get('channel_name', ''))}*",
        f"🎵 Tema musik: *{_tg_escape(d.get('theme', ''))}*",
        f"🎬 Referensi: *{_tg_escape((cand.get('title') or '')[:80])}* "
        f"({_tg_escape(cand.get('channel', ''))} • {views_txt} views)",
        f"🖼️ Tema footage: `{_tg_escape(d.get('pixabay_theme') or 'auto (dari tema musik)')}`",
        f"⏱️ Durasi: `{_tg_escape(d.get('duration_note') or 'semua durasi')}`",
    ]
    if d.get('auto_picked'):
        lines.append(f"🤖 Dipilih otomatis — views tertinggi ({views_txt} views, {dur_mnt} mnt, sesuai kriteria)")
    lines += [
        f"📅 Jadwal: {_tg_escape(_playlist_schedule_text(d))}",
        "",
        "Kalau sudah oke, pencet ✅ Buat. Kalau mau ubah, pencet ❌ Batal lalu mulai lagi dari /playlist ya Bos!",
    ]
    return "\n".join(lines)


def _playlist_should_escalate(made):
    """Eskalasi ke Muse (v1.3.5): True bila order playlist tidak
    menghasilkan SATU PUN job (semua channel 0 kandidat / gagal).
    `made` = list bool per channel (True = job berhasil dibuat).
    Bila >=1 berhasil, JANGAN forward — operator sudah bekerja."""
    return not any(made or [])


def _playlist_escalation_text(theme, channel_names, publish_date, tier_label, source_url=None):
    """Teks konteks kegagalan playlist untuk _bridge_forward: tema, channel,
    jadwal, tier terakhir yang dicoba. Dibaca Muse dari telegram_inbox."""
    lines = [
        "[ESKALASI PLAYLIST — 0 kandidat di semua channel]",
        f"Tema: {theme or '(tidak ada)'}",
        f"Channel: {', '.join(channel_names) if channel_names else '(tidak ada)'}",
        f"Jadwal diminta: {publish_date or '(slot berikutnya)'}",
        f"Tier terakhir dicoba: {tier_label or 'evergreen (tier 4, semua umur video)'}",
    ]
    if source_url:
        lines.append(f"Link kiriman Bos: {source_url} (mode jiplak, juga gagal dibaca/dibuat)")
    lines.append("Tidak ada kandidat sama sekali setelah filter bertingkat — tolong bantu cari solusi: longgarkan lagi, ganti tema, atau minta link YouTube untuk dijiplak.")
    return "\n".join(lines)


def _extract_youtube_link(text):
    """Ambil URL YouTube pertama dari teks (atau None)."""
    m = re.search(r'https?://(?:www\.|music\.)?(?:youtube\.com|youtu\.be)\S+', (text or ''))
    if not m:
        return None
    return m.group(0).rstrip('.,);]>"\'')


def _extract_youtube_ids_from_url(url):
    """Ekstrak (video_id, playlist_id) dari link YouTube (cermin app.py)."""
    text = (url or '').strip()
    if not text:
        return None, None
    m_list = re.search(r'[?&]list=([A-Za-z0-9_-]+)', text)
    playlist_id = m_list.group(1) if m_list else None
    video_id = None
    for pat in (r'youtu\.be/([A-Za-z0-9_-]{11})',
                r'[?&]v=([A-Za-z0-9_-]{11})',
                r'/shorts/([A-Za-z0-9_-]{11})',
                r'/embed/([A-Za-z0-9_-]{11})',
                r'/live/([A-Za-z0-9_-]{11})',
                r'/v/([A-Za-z0-9_-]{11})'):
        m = re.search(pat, text)
        if m:
            video_id = m.group(1)
            break
    return video_id, playlist_id


_PLAYLIST_TIER_LABELS = {1: "30 hari", 2: "90 hari",
                        3: "1 tahun", 4: "evergreen",
                        "link": "link kiriman Bos"}


def _playlist_tier_key(c):
    """Tier kandidat (1/2/3/4/'link'), atau None bila tidak ada video_id /
    tidak ada data sama sekali. v1.3.5 koreksi: TANPA filter views —
    tier hanya dari UMUR terbit; kandidat lama tanpa field 'tier' ditebak
    dari umur saja. Views 0 pun lolos."""
    if not c or not c.get('video_id'):
        return None
    t = c.get('tier')
    if t in (1, 2, 3, 4, 'link'):
        return t
    pub = (c.get('published_at') or '')[:10]
    age = None
    if pub:
        try:
            age = (_wib_now().date() - datetime.strptime(pub, '%Y-%m-%d').date()).days
        except Exception:
            age = None
    if age is not None and age <= 30:
        return 1
    if age is not None and age <= 90:
        return 2
    if age is not None and age <= 365:
        return 3
    return 4


def _playlist_pick_best(cands, min_sec=None, max_sec=None):
    """Pilih kandidat terbaik, BERTINGKAT (v1.3.5) — kembalikan
    (best, tier_label). Tier 1 dulu, lalu 2, 3, 4, lalu 'link'; di tiap
    tier pemenang = views tertinggi, seri → durasi terpanjang (bila
    semua 0 views, yang terpilih = pertama / durasi terpanjang).
    v1.3.5 koreksi: TIDAK ADA filter views — hanya kandidat tanpa
    video_id / tanpa data yang ditolak. Filter durasi (min/max_sec)
    TETAP berlaku bila user mengisi rentang, KECUALI untuk tier 'link'
    (link kiriman Bos dikecualikan — link itu sumbernya, bukan hasil
    riset). (None, None) bila tidak ada kandidat sama sekali."""
    if not cands:
        return None, None
    groups = {}
    for c in (cands or []):
        key = _playlist_tier_key(c)
        if key is None:
            continue
        if key != 'link' and (min_sec is not None or max_sec is not None):
            dur = c.get('duration_sec') or 0
            if dur <= 0:
                continue
            if min_sec is not None and dur < min_sec:
                continue
            if max_sec is not None and dur > max_sec:
                continue
        groups.setdefault(key, []).append(c)
    for key in (1, 2, 3, 4, 'link'):
        lst = groups.get(key)
        if not lst:
            continue
        if key == 'link':
            best = max(lst, key=lambda c: ((c.get('duration_sec') or 0), (c.get('views') or 0)))
        else:
            best = max(lst, key=lambda c: ((c.get('views') or 0), (c.get('duration_sec') or 0)))
        label = best.get('tier_label') or _PLAYLIST_TIER_LABELS.get(key, 'evergreen')
        return best, label
    return None, None


def _pl_send_candidates(token, chat_id, draft):
    """Tampilkan kandidat hasil riset sebagai tombol inline (max 8) +
    tombol Auto. Setiap baris: judul, channel, views, durasi."""
    d = draft.get('data') or {}
    cands = d.get('candidates') or []
    _tier_lbl = d.get('tier_label') or 'semua umur video'
    _src_txt = (f"dari link Bos: {_tg_escape(d.get('source_title') or '')} — " if d.get('source_url') else '')
    lines = ["🎵 *Langkah 4: Pilih referensi*\n\nKandidat playlist (" + _src_txt + "tier " + _tier_lbl + ") — pencet salah satu ya Bos:\n"]
    rows = []
    for i, c in enumerate(cands[:8]):
        views = c.get('views') or 0
        views_txt = f"{views:,}".replace(',', '.')
        dur_mnt = round((c.get('duration_sec') or 0) / 60)
        lines.append(f"{i + 1}. *{_tg_escape((c.get('title') or '')[:60])}* — "
                     f"{_tg_escape(c.get('channel') or '')} ({views_txt} views · {dur_mnt} mnt)")
        rows.append([{"text": f"{i + 1}. {(c.get('title') or '')[:40]}",
                      "callback_data": f"plpick:{i}"}])
    rows.append([{"text": "🤖 Auto — pilih yang terbaik", "callback_data": "plpick:auto"}])
    lines.append("\nAtau balas angkanya (mis. `1`), ketik `auto`, atau pencet 🤖 Auto — saya pilihkan yang terbaik (views tertinggi, sesuai kriteria).")
    send_message(token, chat_id, "\n".join(lines),
                 reply_markup={"inline_keyboard": rows})


def _pl_auto_pick(token, chat_id, draft):
    """Mode Auto di langkah pilih kandidat: pilih yang terbaik lalu
    lanjut ke jadwal. Bila tidak ada yang lolos kriteria → pesan jelas
    + tampilkan pilihan manual (tidak pernah asal pilih di luar
    kriteria)."""
    d = draft.get('data') or {}
    cands = d.get('candidates') or []
    best, _tier_lbl = _playlist_pick_best(cands, d.get('duration_min_sec'), d.get('duration_max_sec'))
    if not best:
        dur_txt = d.get('duration_note') or 'semua durasi'
        draft['step'] = 'pick'
        _save_make_draft(chat_id, draft)
        send_message(token, chat_id,
                     f"😅 *Tidak ada kandidat yang lolos kriteria Auto Bos* — "
                     f"tidak ada video {dur_txt} yang lolos, padahal sudah dicari sampai semua umur video (evergreen).\n\n"
                     "Silakan pilih manual dari daftar di bawah ya, atau kirim link YouTube-nya dengan 'jiplak playlist ini'.")
        _pl_send_candidates(token, chat_id, draft)
        return
    d['video_id'] = best.get('video_id')
    d['ref_title'] = best.get('title', '')
    d['auto_picked'] = True
    draft['data'] = d
    # Jadwal sudah diputuskan duluan (mis. 'publish' dari one-shot/NLU)
    # → langsung konfirmasi; kalau belum → tanya jadwal seperti biasa.
    draft['step'] = _playlist_next_step(d)
    _save_make_draft(chat_id, draft)
    views_txt = f"{best.get('views') or 0:,}".replace(',', '.')
    dur_mnt = round((best.get('duration_sec') or 0) / 60)
    dur_txt = d.get('duration_note') or 'semua durasi'
    send_message(token, chat_id,
                 f"🤖 *Saya pilihkan yang terbaik Bos!* 😎\n"
                 f"🎬 *{_tg_escape((best.get('title') or '')[:80])}* — "
                 f"{_tg_escape(best.get('channel', ''))} ({views_txt} views · {dur_mnt} mnt)\n"
                 f"Alasan: views tertinggi di antara kandidat {dur_txt} (tier {_tier_lbl or 'evergreen'}).")
    _make_ask_step(token, chat_id, draft)


def _pl_closest_offer_text(closest, dur_txt):
    """Pesan tawaran saat filter durasi mengosongkan hasil (v1.3.13):
    tunjukkan video yang PALING DEKAT supaya Bos tinggal pencet ✅."""
    lines = [f"😅 *Tidak ada video {dur_txt} untuk tema ini Bos* — tapi ada yang paling dekat:"]
    for i, c in enumerate(closest, 1):
        v = c.get('views') or 0
        vtxt = f"{v:,}".replace(',', '.') + " views"
        lines.append(f"{i}. *{_tg_escape((c.get('title') or '')[:80])}* — {c.get('duration_text') or ''} · {vtxt}")
    lines.append("Pencet ✅ kalau mau pakai yang paling dekat, atau ❌ untuk ganti durasi.")
    return "\n".join(lines)


def _pl_run_research(token, chat_id, draft):
    """Jalankan riset kandidat playlist (YouTube API, ~101 unit kuota;
    cache panel 1 jam). Dipanggil otomatis saat draft sampai langkah
    'research' (setelah tema + footage + durasi terisi). Gagal / kosong →
    kembali ke langkah tema supaya Bos bisa coba tema lain; bila kosong
    karena filter durasi, kembali ke langkah durasi."""
    d = draft.get('data') or {}
    if d.get('source_url'):
        # v1.3.5 jiplak: link Bos = sumbernya, tanpa riset/filter.
        send_message(token, chat_id, "🔍 *Mengambil daftar lagu dari link Bos...* 😎")
        ok, msg, data = _operator_post('/api/operator/playlist_from_url',
                                      {'yt_id': d.get('yt_id'), 'url': d.get('source_url')})
        if not ok:
            draft['step'] = 'theme'
            _save_make_draft(chat_id, draft)
            send_message(token, chat_id,
                         f"⚠️ *Link-nya gagal dibaca Bos* 😅\n{_tg_escape(msg)}\nCoba kirim link YouTube lain, atau ketik tema musiknya ya.")
            _make_ask_step(token, chat_id, draft)
            return
        _cands = (data or {}).get('candidates') or []
        if not _cands:
            draft['step'] = 'theme'
            _save_make_draft(chat_id, draft)
            send_message(token, chat_id,
                         "😅 *Link itu tidak ada lagunya Bos* — playlist-nya kosong atau videonya tidak bisa dibaca. Coba link lain atau ketik tema musiknya ya.")
            _make_ask_step(token, chat_id, draft)
            return
        d['job_id'] = (data or {}).get('job_id', '')
        d['candidates'] = _cands
        d['source_title'] = (data or {}).get('source_title') or ''
        d['source_kind'] = (data or {}).get('source_kind') or ''
        d['tier_label'] = 'link kiriman Bos'
        draft['data'] = d
        if d.get('auto_pick'):
            _pl_auto_pick(token, chat_id, draft)
            return
        draft['step'] = 'pick'
        _save_make_draft(chat_id, draft)
        _pl_send_candidates(token, chat_id, draft)
        return
    send_message(token, chat_id, "🔍 *Mencari kandidat playlist...* 😎")
    ok, msg, data = _operator_post('/api/operator/playlist_research',
                                  {'yt_id': d.get('yt_id'), 'theme': d.get('theme', ''),
                                   'min_duration_sec': d.get('duration_min_sec'),
                                   'max_duration_sec': d.get('duration_max_sec')})
    if not ok:
        draft['step'] = 'theme'
        _save_make_draft(chat_id, draft)
        send_message(token, chat_id,
                     f"⚠️ *Risetnya gagal Bos* 😅\n{_tg_escape(msg)}\nCoba tema musik yang lain ya.")
        _make_ask_step(token, chat_id, draft)
        return
    cands = (data or {}).get('candidates') or []
    d['research_tier'] = (data or {}).get('research_tier')
    d['tier_label'] = (data or {}).get('tier_label') or ''
    if not cands:
        filter_note = (data or {}).get('filter_note') or ''
        if filter_note and (d.get('duration_min_sec') is not None
                            or d.get('duration_max_sec') is not None):
            closest = (data or {}).get('closest') or []
            if closest:
                # v1.3.13: TAWARKAN yang paling dekat (tombol ✅/❌) — jangan
                # tanya ulang Langkah 3 begitu saja; itulah yang terasa bodoh.
                d['closest'] = closest
                draft['step'] = 'closest'
                draft['data'] = d
                _save_make_draft(chat_id, draft)
                send_message(token, chat_id,
                             _pl_closest_offer_text(closest, d.get('duration_note') or ''),
                             reply_markup={"inline_keyboard": [
                                 [{"text": "✅ Pakai yang paling dekat", "callback_data": "mkplaylist:closest:yes"},
                                  {"text": "❌ Ganti durasi", "callback_data": "mkplaylist:closest:no"}]]})
                return
            draft['step'] = 'duration'
            _save_make_draft(chat_id, draft)
            send_message(token, chat_id,
                         f"😅 *{_tg_escape(filter_note)}*\n\n"
                         "Balas rentang durasi yang lain (mis. `30-90`), atau ketik `lewati` untuk semua durasi.")
            # TANPA _make_ask_step: pesan di atas SUDAH jadi prompt-nya.
            # Dulu Langkah 3 dikirim ulang persis setelah pesan ini.
            return
        draft['step'] = 'theme'
        _save_make_draft(chat_id, draft)
        # v1.3.5 eskalasi: riset 0 kandidat total (bukan filter durasi)
        # -> teruskan SEKALI ke Muse lewat jembatan, lalu SATU pesan
        # gabungan (hasil 0 + sudah diteruskan), bukan pesan menyerah.
        _esc = _playlist_escalation_text(
            d.get('theme'), [d.get('channel_name') or d.get('yt_id') or ''],
            d.get('publish_date'), d.get('tier_label'))
        _bridge_forward(token, chat_id, None, _esc,
                        source='playlist_no_candidate',
                        context_extra={'theme': d.get('theme'), 'channels': [d.get('yt_id')],
                                       'publish_date': d.get('publish_date'),
                                       'tier_label': d.get('tier_label')})
        send_message(token, chat_id,
                     "😅 *Tidak ada kandidat untuk tema itu Bos* — sudah dicari sampai semua umur video (evergreen).\n\n"
                     "Saya teruskan ke Muse ya Bos, biar dicarikan solusinya — mis. longgarkan lagi, ganti tema, atau kirim link YouTube untuk dijiplak.")
        _make_ask_step(token, chat_id, draft)
        return
    d['job_id'] = (data or {}).get('job_id', '')
    d['candidates'] = cands
    draft['data'] = d
    if d.get('auto_pick'):
        # Mode Auto (dari NLU/one-shot): langsung pilih yang terbaik,
        # lewati tampilan pilihan manual.
        _pl_auto_pick(token, chat_id, draft)
        return
    draft['step'] = 'pick'
    _save_make_draft(chat_id, draft)
    _pl_send_candidates(token, chat_id, draft)


def _start_make_playlist(token, chat_id, raw_cmd):
    """Mulai alur /playlist & /buatplaylist. Dukung sekali kirim:
    /buatplaylist <tema> | <channel> | <tema pixabay> | <durasi> — slot
    yang sudah diisi tidak ditanya lagi; slot yang kurang ditanyakan
    satu per satu (alur yang sama seperti /playlist polos). Segmen 'auto'
    (di posisi mana pun) → pilih kandidat terbaik otomatis; segmen
    'publish' (di posisi mana pun) → langsung tayang setelah render
    (tanpa jadwal)."""
    channels = load_channels()
    if not channels:
        send_message(token, chat_id,
                     "😅 *Bos, belum ada channel yang terhubung nih*\n\n"
                     "Tautkan channel dulu di panel (Settings → Tautkan Channel Baru), "
                     "lalu ketik /playlist lagi ya Bos!")
        return
    pre = {}
    rest = raw_cmd[len('/buatplaylist'):].strip() if raw_cmd.strip().lower().startswith('/buatplaylist') else ''
    if rest:
        parts = [p.strip() for p in rest.split('|')]
        # Segmen 'auto' di posisi mana pun → mode auto (lewati pilih
        # manual, langsung pilih kandidat terbaik setelah riset).
        if any(p.lower() in ('auto', 'otomatis') for p in parts):
            pre['auto_pick'] = True
            parts = [p for p in parts if p.lower() not in ('auto', 'otomatis')]
        # Segmen 'publish' di posisi mana pun → langsung tayang setelah
        # render (tanpa jadwal); publish_date = jam sekarang (WIB).
        if any(p.lower() in ('publish',) for p in parts):
            pre['publish_now'] = True
            pre['publish_date'] = _playlist_publish_now_date()
            parts = [p for p in parts if p.lower() not in ('publish',)]
        if len(parts) > 0 and parts[0]:
            pre['theme'] = parts[0][:100]
        if len(parts) > 1 and parts[1]:
            matched, _all = _nlu_match_channels(_nlu_clean(parts[1]), channels)
            if len(matched) == 1:
                pre['yt_id'] = matched[0].get('yt_id')
                pre['channel_name'] = _channel_display_name(matched[0])
        if len(parts) > 2 and parts[2]:
            pre['pixabay_theme'] = parts[2][:100]
        if len(parts) > 3 and parts[3]:
            parsed = _parse_playlist_duration(parts[3])
            if parsed and parsed[0] is not None:
                pre['duration_min_sec'], pre['duration_max_sec'], pre['duration_note'] = parsed
    draft = {"kind": "playlist", "step": "theme", "data": dict(pre)}
    if pre.get('yt_id'):
        draft['step'] = _playlist_next_step(draft['data'])
        _save_make_draft(chat_id, draft)
        send_message(token, chat_id,
                     "🎵 *Siap Bos, kita buat Playlist bareng-bareng ya!* 😎\n"
                     "Jawab tiap langkah satu per satu — ketik /batal kapan saja untuk berhenti.")
        _make_ask_step(token, chat_id, draft)
        return
    ch, ask_msg = _make_pick_channel(channels, multi=True)
    if ch is None:
        draft['step'] = 'channel'
        _save_make_draft(chat_id, draft)
        send_message(token, chat_id,
                     "🎵 *Siap Bos, kita buat Playlist bareng-bareng ya!* 😎\n"
                     "Jawab tiap langkah satu per satu — ketik /batal kapan saja untuk berhenti.\n\n" + ask_msg)
        return
    draft['data']['yt_id'] = ch.get('yt_id')
    draft['data']['channel_name'] = _channel_display_name(ch)
    draft['step'] = _playlist_next_step(draft['data'])
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 f"🎵 *Siap Bos, kita buat Playlist buat channel {_tg_escape(draft['data']['channel_name'])} ya!* 😎\n"
                 "Jawab tiap langkah satu per satu — ketik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)


def _send_playlist_created_message(token, chat_id, d, resp):
    """Pesan sukses setelah job playlist dibuat — jelaskan apa yang terjadi
    selanjutnya (menunggu approval vs langsung antre, sesuai mode channel)."""
    ch = next((c for c in load_channels() if c.get('yt_id') == d.get('yt_id')), None)
    auto = _autopilot_of(ch) == 'auto' if ch else False
    cand = next((c for c in (d.get('candidates') or [])
                 if c.get('video_id') == d.get('video_id')), None) or {}
    views_txt = f"{cand.get('views') or 0:,}".replace(',', '.')
    dur_mnt = round((cand.get('duration_sec') or 0) / 60)
    lines = [
        "✅ *Sip Bos, playlist-nya udah saya buatkan!* 🎵",
        f"🎬 Referensi: *{_tg_escape((cand.get('title') or d.get('theme', ''))[:80])}*",
        f"📺 Channel: *{_tg_escape(d.get('channel_name', ''))}*",
        f"📅 Jadwal: {_tg_escape(_playlist_schedule_text(d))}",
        f"🖼️ Footage: `{_tg_escape(d.get('pixabay_theme') or 'auto (dari tema musik)')}`",
        f"⏱️ Durasi: `{_tg_escape(d.get('duration_note') or 'semua durasi')}`",
    ]
    if d.get('auto_picked'):
        lines.append(f"🤖 Dipilih otomatis: views tertinggi ({views_txt} views, {dur_mnt} mnt, sesuai kriteria)")
    if auto:
        lines += ["",
                  "🚀 *Langsung masuk antrean (mode Full-Auto)* — audio & footage sedang diunduh, ditunggu kabar selesainya ya Bos! 💪"]
    else:
        lines += ["",
                  "⏳ *Video menunggu persetujuan Bos dulu ya* — setelah render selesai, preview-nya bakal masuk ke chat ini, "
                  "tinggal pencet ✅ Setujui biar langsung di-upload."]
    lines.append("\nPantau progresnya dengan `/playlist status` ya Bos! 🎵")
    send_message(token, chat_id, "\n".join(lines))


def _send_playlist_jobs_message(token, chat_id):
    """Daftar 5 job playlist terakhir (dipakai /playlist status dan tombol
    📋 Lihat Job Terakhir)."""
    jobs = _load_playlist_jobs()
    if not jobs:
        send_message(token, chat_id,
                     "🎵 *Musik Playlist*\nBelum ada job playlist. Buat baru dengan /playlist ya Bos! 😎")
        return
    lines = ["🎵 *Musik Playlist — Job Terakhir* 😎", "━━━━━━━━━━━━━━━━━━━━━━"]
    for j in jobs[:5]:
        st = {'researched': '🔍 Riset selesai', 'downloading': '⬇️ Mengunduh',
              'preparing': '🛠️ Menyiapkan', 'queued': '⏳ Menunggu approval/antrean',
              'done': '✅ Tayang', 'failed': '❌ Gagal'}.get(j.get('status'), j.get('status', '?'))
        lines.append(f"• *{_tg_escape((j.get('new_title') or j.get('ref_title') or j.get('theme') or '?')[:45])}*")
        lines.append(f"  {_tg_escape(j.get('channel_name', ''))} • {st}")
        if j.get('progress'):
            lines.append(f"  {_tg_escape(j['progress'][:90])}")
        if j.get('error'):
            lines.append(f"  Alasan: {_tg_escape(j['error'][:90])}")
        if j.get('video_url'):
            lines.append(f"  ▶️ {j['video_url']}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("Buat playlist baru: /playlist (bot akan bertanya) atau /buatplaylist tema | channel.")
    send_message(token, chat_id, "\n".join(lines))


# 🎵 ZUMO / KIE.AI — PLAYLIST LAGU ORISINAL LEWAT TELEGRAM
# ==========================================
# /zumo polos → menu (Buat Baru / Lihat Job). "Buat Baru" atau
# /zumo <teks bebas> atau one-shot '/zumo tema | jumlah | jadwal' →
# bot bertanya satu per satu: channel (lewati = Cozy Sax Relaxing) →
# tema → mode vokal/instrumental (lewati = vokal; instrumental =
# lirik dilewati total) → style/genre bebas (lewati = profil channel;
# boleh sebut bahasa & vokal, mis. 'reggae Indonesia, vokal pria') →
# lirik (tempel lirik lengkap gaya Suno ATAU 'auto'; dilewati bila
# instrumental) → jumlah judul 2/3 (1 bila sudah ada lirik tempel;
# judul sisa dibuatkan otomatis server) → partikel salju/sakura +
# warna spectrum → jadwal (lewati = slot berikutnya, server yang
# atur; 'publish' = langsung tayang) → ringkasan + ✅ Buat / ❌ Batal.
# Draft per-chat sama seperti /buatshort & /playlist
# (monitor_state.json "make_drafts", tahan restart, hangus >30 menit,
# batal via /batal). Profil (style/bahasa/vokal) tersimpan PER CHANNEL
# di server; field yang dikirim di order disimpan sebagai profil
# channel itu — jadi JANGAN hardcode sax/Inggris/f di sini. Lirik tempel
# dikirim UTUH apa adanya (tag [Bagian: arahan] adalah instruksi, bukan
# dinyanyikan; jangan dipotong, jangan dibersihkan tag-nya).
# Channel default Cozy Sax Relaxing — yt_id dikirim eksplisit di order
# (kontrak /api/operator/zumo_create diperbarui 20:44 WIB).
# /zumobank & /zumorakit (v1.3.0): alur terpandu yang sama polanya,
# kind 'zumobank'/'zumorakit' — bank: channel → tema → mode → style →
# jumlah lagu 1-30 → konfirmasi (POST mode='bank', tanpa upload);
# rakit: channel → jumlah lagu 1-30 → jadwal → konfirmasi (POST
# mode='assemble', lagu dipilih acak dari bank).
_ZUMO_YT_ID = 'UCg8be5L4_CieGjXA8ejB1_Q'
_ZUMO_CHANNEL_NAME = 'Cozy Sax Relaxing'
_ZUMO_DEFAULT_STYLE = ("smooth jazz saxophone, warm, relaxing, romantic, "
                       "cozy cafe dinner late night")
_ZUMO_DEFAULT_MODEL = 'V5.5'
_ZUMO_MODELS = ('V5.5', 'V6', 'V6_MINI', 'V6_WILD')
_ZUMO_PARTICLE_LABEL = {'snow': 'salju', 'sakura': 'sakura'}
_ZUMO_SPECTRUM_LABEL = {'white': 'putih', 'cyan': 'cyan', 'viridis': 'viridis'}
# Contoh BENTUK penulisan style (v1.3.0, koreksi user 21:00 WIB):
# HANYA teks referensi di pesan langkah style — BUKAN preset, BUKAN
# tombol pilihan, BUKAN daftar tetap. User mengetik style bebas apa
# pun; bila mengetik salah satu contoh ini utuh, diterima sebagai
# style biasa. Pola: daftar instrumen + mood + BPM di akhir (BPM
# dipakai server sebagai hint, tidak diparse operator).
_ZUMO_STYLE_EXAMPLES = (
    "Indonesian Roots Reggae, Tropical Archipelago Groove, Acoustic & Clean Electric Skank Guitar, Traditional Kendang & Ketipung Percussion, Bamboo Suling Flute, Warm Dub Bassline, Melodica, Laid-Back Soulful Indonesian Male Vocals, Warung Kopi Beach Vibe, 82 BPM",
    "Indonesian Acoustic Roots Reggae, Islamic Nusantara Fusion, Warm Nylon Guitar Skank, Rebana & Marawis Percussion, Oud Melody, Deep Warm Dub Bassline, Bamboo Suling, Soulful Humble Indonesian Male Vocals, Peaceful Ramadan Atmosphere, 80 BPM",
    "Indonesian Pop Religi, Warm Acoustic Ballad, Fingerpicked Acoustic Guitar, Emotional Grand Piano, Lush Cello & String Section, Subtle Oud & Darbuka Percussion, Soulful Heartfelt Indonesian Male Vocals, Peaceful Spiritual Ramadan Vibe, 76 BPM",
)
_ZUMO_MODE_VOCAL_WORDS = ('vokal', 'vocal', 'dengan vokal', 'bervokal',
                          'pakai vokal', 'ada vokal', 'penyanyi', 'bernyanyi',
                          'nyanyi')
_ZUMO_MODE_INSTR_WORDS = ('instrumental', 'instrument', 'piano',
                          'tanpa vokal', 'tanpa lirik', 'tanpa penyanyi',
                          'tanpa suara', 'musik saja', 'instrumental saja',
                          'no vocal', 'tanpa nyanyi')


def _zumo_next_step(d):
    """Langkah pertama yang masih kosong untuk Zumo: channel → tema →
    mode → style → lirik → jumlah → partikel → jadwal → konfirmasi.
    Bila instrumental, langkah lirik dilewati total."""
    if not d.get('yt_id'):
        return 'channel'
    if not d.get('theme'):
        return 'theme'
    if 'mode' not in d:
        return 'mode'
    if 'style' not in d:
        return 'style'
    if d.get('mode') != 'instrumental' and 'lyrics_mode' not in d:
        return 'lyrics'
    if 'song_count' not in d:
        return 'count'
    if 'particle' not in d:
        return 'particle'
    if 'publish_date' not in d:
        return 'publish'
    return 'confirm'


def _zumobank_next_step(d):
    """Langkah pertama yang masih kosong untuk /zumobank: channel →
    tema → mode → style → jumlah lagu → konfirmasi. Bank TIDAK
    menanyakan lirik (lirik dibuatkan server bila vokal) dan TIDAK
    menanyakan partikel/jadwal (tanpa upload)."""
    if not d.get('yt_id'):
        return 'channel'
    if not d.get('theme'):
        return 'theme'
    if 'mode' not in d:
        return 'mode'
    if 'style' not in d:
        return 'style'
    if 'bank_count' not in d:
        return 'bank_count'
    return 'confirm'


def _zumorakit_next_step(d):
    """Langkah pertama yang masih kosong untuk /zumorakit: channel →
    jumlah lagu → jadwal → konfirmasi. Tema tidak ditanyakan
    (payload memakai 'Bank' sebagai judul fallback)."""
    if not d.get('yt_id'):
        return 'channel'
    if 'bank_count' not in d:
        return 'bank_count'
    if 'publish_date' not in d:
        return 'publish'
    return 'confirm'


def _zumo_parse_mode(text):
    """Jawaban langkah mode → 'vokal' / 'instrumental' / None.
    Hanya jawaban PENDEK yang dikenali ('vokal', 'vokal pria',
    'instrumental', 'instrumental piano', 'piano', 'tanpa vokal',
    ...). Deskripsi style yang panjang / ber koma ('reggae Indonesia,
    vokal pria') → None — pemanggil memakainya sebagai style +
    mode vokal, supaya jawaban style lama tetap jalan."""
    t = (text or '').strip().lower()
    if t in _ZUMO_MODE_VOCAL_WORDS:
        return 'vokal'
    if t in _ZUMO_MODE_INSTR_WORDS:
        return 'instrumental'
    words = t.split()
    if len(words) <= 2 and ',' not in t and words:
        if words[0] in ('vokal', 'vocal'):
            return 'vokal'
        if words[0] in ('instrumental', 'instrument', 'piano'):
            return 'instrumental'
    return None


def _zumo_parse_bank_count(text):
    """Parse jawaban jumlah lagu bank/rakit → 1–30, atau None.
    '30' / '30 lagu' → 30; angka di luar 1–30 / tanpa angka → None
    (pemanggil bertanya ulang)."""
    m = re.search(r'(\d+)', str(text or ''))
    if not m:
        return None
    n = int(m.group(1))
    return n if 1 <= n <= 30 else None


def _zumo_looks_like_lyrics(text):
    """Lirik tempel gaya Suno yang nyasar ke langkah lain: mengandung
    tag [Verse / [Chorus, atau >=3 tag [..] dalam teks multi-baris."""
    t = text or ''
    tl = t.lower()
    if '[verse' in tl or '[chorus' in tl:
        return True
    return t.count('[') >= 3 and '\n' in t


def _zumo_set_style(d, ans):
    """Terapkan jawaban style (bebas, apa pun isinya). Bila jawabannya
    ternyata lirik tempel (tag [Verse]/[Chorus]) — disimpan sebagai
    lirik, BUKAN style (style jadi '' = profil channel). Bila
    instrumental, petunjuk bahasa/vokal TIDAK diparse (style hanya
    mendeskripsikan musik). Kembalikan True bila dipakai sebagai
    lirik tempel."""
    if d.get('mode') != 'instrumental' and _zumo_looks_like_lyrics(ans):
        title = (_zumo_title_from_lyrics(ans)
                 or (d.get('theme') or 'Lagu Zumo')[:80])
        d['lyrics_mode'] = 'pasted'
        d['titles'] = [{'title': title, 'lyrics': ans}]
        d['style'] = ''
        return True
    d['style'] = ans[:500]
    if d.get('mode') != 'instrumental':
        lang, vocal = _zumo_parse_style_hints(ans)
        if lang:
            d['lyrics_language'] = lang
        if vocal:
            d['vocal_gender'] = vocal
    return False


def _zumo_apply_mode_answer(d, ans):
    """Terapkan jawaban langkah mode (dipakai /zumo & /zumobank).
    'lewati' = vokal. Jawaban mode dikenali → mode itu; bila
    instrumental, lirik ditandai 'none' (dilewati total). Jawaban
    berupa deskripsi style / lirik tempel → dipakai sebagai style /
    lirik + mode vokal (kompatibilitas jawaban lama)."""
    low = (ans or '').strip().lower()
    if low in ('lewati', '-', 'skip', 'default'):
        d['mode'] = 'vokal'
        return
    mode = _zumo_parse_mode(ans)
    if mode is None:
        d['mode'] = 'vokal'
        _zumo_set_style(d, ans)
        return
    d['mode'] = mode
    if mode == 'instrumental':
        d['lyrics_mode'] = 'none'
        d.pop('titles', None)


def _zumo_apply_publish_answer(d, ans):
    """Terapkan jawaban jadwal (dipakai /zumo & /zumorakit).
    Kembalikan None bila OK, atau teks pesan ulang bila jawaban
    tidak dipahami."""
    low = (ans or '').strip().lower()
    lown = ' '.join(low.split())
    if lown in _PLAYLIST_PUBLISH_NOW_WORDS:
        d['publish_date'] = _playlist_publish_now_date()
        d['publish_now'] = True
        d.pop('publish_note', None)
        return None
    if low in ('lewati', '-', 'skip', 'default'):
        d['publish_date'] = ''
        d.pop('publish_now', None)
        d.pop('publish_note', None)
        return None
    parsed, note = _parse_natural_datetime(ans)
    if not parsed:
        return ("Waktu tayangnya belum saya pahami Bos — coba `hari ini jam 9`, `besok jam 7 malam`, "
                "`2026-11-20 19:00`, ketik `publish` untuk langsung tayang, atau `lewati`.")
    d['publish_date'] = parsed
    if note:
        d['publish_note'] = note
    else:
        d.pop('publish_note', None)
    d.pop('publish_now', None)
    return None


def _zumo_confirm_answer(token, chat_id, draft, low):
    """Jawaban teks di langkah konfirmasi Zumo/bank/rakit ('ya' /
    'batal' / lain). Selalu ditangani — pemanggil return True."""
    kind = draft.get('kind')
    if set(re.findall(r'[a-z]+', low)) & {'ya', 'buat', 'ok', 'oke', 'lanjut'}:
        _confirm_make_create(token, chat_id, draft)
        return True
    if set(re.findall(r'[a-z]+', low)) & {'batal', 'tidak', 'cancel'}:
        _clear_make_draft(chat_id)
        msg = {'zumobank': "Sip Bos, dibatalkan. Tidak ada lagu bank yang dibuat.",
               'zumorakit': "Sip Bos, dibatalkan. Tidak ada video rakitan yang dibuat."}.get(
            kind, "Sip Bos, dibatalkan. Tidak ada playlist Zumo yang dibuat.")
        send_message(token, chat_id, msg)
        return True
    send_message(token, chat_id,
                 "Tinggal pencet tombol di atas ya Bos — Buat atau Batal. Atau balas `ya` / `batal`.")
    return True


def _zumo_style_ask_text(step_label, default_note=''):
    """Pesan langkah style (dipakai /zumo & /zumobank): style bebas +
    3 contoh HANYA sebagai referensi BENTUK penulisan (bukan preset,
    bukan tombol pilihan)."""
    ex = _ZUMO_STYLE_EXAMPLES
    return (
        f"{step_label}: Style / genre\n\n"
        "Style musiknya bagaimana Bos? Tulis bebas saja — sebut daftar instrumen, mood, dan BPM di akhir "
        "(mis. `..., 82 BPM`).\n\n"
        "Contoh bentuk penulisan (bukan pilihan tetap, boleh tulis sendiri):\n"
        f"1. {ex[0]}\n"
        f"2. {ex[1]}\n"
        f"3. {ex[2]}\n\n"
        f"Ketik `lewati` untuk pakai style profil channel ini{default_note}.")


def _zumo_mode_ask_text(step_label):
    """Pesan langkah mode (dipakai /zumo & /zumobank)."""
    return (
        f"{step_label}: Vokal atau instrumental?\n\n"
        "Lagunya pakai vokal atau instrumental saja Bos? Balas `vokal` atau `instrumental` "
        "(mis. `piano` untuk instrumental piano).\n"
        "Ketik `lewati` untuk vokal.")


def _zumo_active_model():
    """Model Zumo aktif menurut server (best effort; gagal → default)."""
    try:
        ok, _msg, data = _operator_post('/api/operator/zumo_status', {}, timeout=10)
        if ok and isinstance(data, dict) and (data.get('model') or '') in _ZUMO_MODELS:
            return data['model']
    except Exception:
        pass
    return _ZUMO_DEFAULT_MODEL


def _zumo_schedule_text(d):
    """Teks jadwal untuk ringkasan/pesan sukses Zumo."""
    if d.get('publish_now'):
        return "Langsung tayang setelah render (tanpa jadwal)"
    pd = d.get('publish_date') or ''
    if not pd:
        return "Slot berikutnya (diatur server)"
    note = d.get('publish_note')
    if note:
        return f"{pd} ({note})"
    return pd


def _zumo_parse_count(text, allow_one=False):
    """Parse jawaban jumlah judul Zumo → 2 atau 3 (atau 1 bila allow_one —
    hanya bila sudah ada lirik tempel), atau None. '2' / '3 judul' /
    '3 lagu' → angka itu; angka lain / tanpa angka → None (pemanggil
    bertanya ulang — 1 playlist hanya 1–3 judul)."""
    m = re.search(r'(\d+)', str(text or ''))
    if not m:
        return None
    n = int(m.group(1))
    return n if n in ((1, 2, 3) if allow_one else (2, 3)) else None


def _zumo_parse_style_hints(text):
    """Bahasa lirik + gender vokal dari jawaban style (opsional — TIDAK
    wajib disebut). 'indonesia' → 'id', 'inggris'/'english' → 'en';
    'pria'/'cowok'/'bariton'/'male' → 'm', 'wanita'/'cewek'/'perempuan'/
    'female' → 'f'. Tidak disebut → (None, None) = ikut profil channel
    di server (tidak dikirim di order)."""
    t = (text or '').lower()
    lang = None
    if 'indonesia' in t:
        lang = 'id'
    elif 'inggris' in t or 'english' in t:
        lang = 'en'
    vocal = None
    if re.search(r'\b(pria|cowok|bariton|male)\b', t):
        vocal = 'm'
    elif re.search(r'\b(wanita|cewek|perempuan|female)\b', t):
        vocal = 'f'
    return lang, vocal


def _zumo_title_from_lyrics(text):
    """Judul untuk lirik tempel: baris 'Judul: ...' / 'Title: ...' di
    awal teks bila ada, sonst None (pemanggil memakai tema sebagai judul).
    Liriknya sendiri TIDAK diubah — hanya dibaca baris judulnya."""
    for ln in (text or '').split('\n')[:5]:
        m = re.match(r'\s*(judul|title)\s*[:\-–]\s*(.+)', ln, flags=re.I)
        if m and m.group(2).strip():
            return m.group(2).strip()[:80]
    return None


def _zumo_parse_particle(text):
    """Parse jawaban partikel Zumo → (particle, spectrum_color) atau
    (None, None). 'salju'/'snow' → snow; 'sakura' → sakura; warna spectrum
    'putih'/'white'/'cyan'/'viridis' boleh digabung (mis. 'sakura cyan').
    'lewati'/'default' → ('snow', 'white'). Tidak dikenali → (None, None)."""
    t = (text or '').strip().lower()
    if not t or t in ('lewati', '-', 'skip', 'default'):
        return ('snow', 'white')
    particle = None
    if 'sakura' in t:
        particle = 'sakura'
    elif 'salju' in t or 'snow' in t:
        particle = 'snow'
    color = None
    if 'cyan' in t:
        color = 'cyan'
    elif 'viridis' in t:
        color = 'viridis'
    elif 'putih' in t or 'white' in t:
        color = 'white'
    if particle is None and color is None:
        return (None, None)
    return (particle or 'snow', color or 'white')


def _make_zumo_summary(d):
    """Ringkasan konfirmasi Zumo (teks biasa, tanpa emoji/ikon). Bila
    song_count > jumlah titles tempel, disebut eksplisit: judul sisanya
    dibuatkan otomatis oleh server."""
    n = d.get('song_count') or 3
    titles = d.get('titles') or []
    lang_label = {'id': 'Indonesia', 'en': 'Inggris'}.get(d.get('lyrics_language') or '')
    vocal_label = {'m': 'pria', 'f': 'wanita'}.get(d.get('vocal_gender') or '')
    instrumental = d.get('mode') == 'instrumental'
    mode_label = 'Instrumental (tanpa lirik)' if instrumental else 'Vokal'
    lines = [
        "Cek dulu ya Bos, ini playlist Zumo yang mau dibuat:",
        f"Channel: {_tg_escape(d.get('channel_name') or _ZUMO_CHANNEL_NAME)}",
        f"Tema: {_tg_escape(d.get('theme', ''))}",
        f"Mode: {mode_label}",
        f"Style: {_tg_escape(d.get('style') or 'ikut profil channel')}",
        f"Jumlah judul: {n} (jadi {n * 2} varian lagu)",
        f"Model: {d.get('model') or _ZUMO_DEFAULT_MODEL}",
        f"Partikel: {_ZUMO_PARTICLE_LABEL.get(d.get('particle') or 'snow', 'salju')} | Spectrum: {_ZUMO_SPECTRUM_LABEL.get(d.get('spectrum_color') or 'white', 'putih')}",
        f"Jadwal: {_tg_escape(_zumo_schedule_text(d))}",
    ]
    if instrumental:
        lines.append(f"Biaya musik kira-kira: {n * 12} kredit ({n} generate x 12 kredit; tanpa biaya lirik).")
    else:
        lines.append(f"Biaya musik kira-kira: {n * 12} kredit ({n} generate x 12 kredit; biaya lirik ikut tagihan Kie).")
    if instrumental:
        lines.append("Lirik: tanpa lirik (instrumental).")
    elif d.get('lyrics_mode') == 'pasted' and titles:
        t0 = titles[0]
        lines.append(f"Lirik: tempel 1 judul ({_tg_escape((t0.get('title') or '')[:60])}) — dikirim utuh apa adanya.")
        if n > len(titles):
            lines.append(f"Judul lain ({n - len(titles)}) dibuatkan otomatis oleh server (judul + lirik).")
    else:
        lines.append("Lirik: otomatis (dibuatkan server).")
    if instrumental:
        pass  # bahasa & vokal tidak dipakai (instrumental)
    elif lang_label or vocal_label:
        prof = "Bahasa lirik: " + (lang_label or "ikut profil")
        prof += " | Vokal: " + (vocal_label or "ikut profil")
        lines.append(prof)
        lines.append("Bahasa & vokal ini disimpan sebagai profil channel ini.")
    else:
        lines.append("Bahasa & vokal lirik: ikut profil channel.")
    lines += [
        "",
        "Kalau sudah oke, pencet Buat. Kalau mau ubah, pencet Batal lalu mulai lagi dari /zumo ya Bos!",
    ]
    return "\n".join(lines)


def _make_zumo_bank_summary(d):
    """Ringkasan konfirmasi /zumobank (teks biasa, tanpa emoji/ikon).
    Biaya lirik disebut apa adanya bila vokal — tanpa angka pasti
    (biaya lirik ikut tagihan Kie)."""
    n = d.get('bank_count') or 10
    instrumental = d.get('mode') == 'instrumental'
    mode_label = 'Instrumental (tanpa lirik)' if instrumental else 'Vokal'
    lines = [
        "Cek dulu ya Bos, ini order Bank Audio Zumo yang mau dibuat:",
        f"Channel: {_tg_escape(d.get('channel_name') or _ZUMO_CHANNEL_NAME)}",
        f"Tema: {_tg_escape(d.get('theme', ''))}",
        f"Mode: {mode_label}",
        f"Style: {_tg_escape(d.get('style') or 'ikut profil channel')}",
        f"Jumlah lagu: {n} (disimpan di bank audio, tanpa upload)",
        f"Model: {d.get('model') or _ZUMO_DEFAULT_MODEL}",
    ]
    if instrumental:
        lines.append(f"Biaya musik kira-kira: {n * 12} kredit ({n} generate x 12 kredit; tanpa biaya lirik).")
    else:
        lines.append(f"Biaya musik kira-kira: {n * 12} kredit ({n} generate x 12 kredit; biaya lirik ikut tagihan Kie).")
    lines += [
        "",
        "Kalau sudah oke, pencet Buat. Kalau mau ubah, pencet Batal lalu mulai lagi dari /zumobank ya Bos!",
    ]
    return "\n".join(lines)


def _make_zumo_assemble_summary(d):
    """Ringkasan konfirmasi /zumorakit (teks biasa, tanpa emoji/ikon)."""
    n = d.get('bank_count') or 8
    lines = [
        "Cek dulu ya Bos, ini video rakitan dari Bank Audio yang mau dibuat:",
        f"Channel: {_tg_escape(d.get('channel_name') or _ZUMO_CHANNEL_NAME)}",
        f"Jumlah lagu: {n} (dipilih acak dari bank audio channel ini)",
        f"Jadwal: {_tg_escape(_zumo_schedule_text(d))}",
        "",
        "Kalau sudah oke, pencet Buat. Kalau mau ubah, pencet Batal lalu mulai lagi dari /zumorakit ya Bos!",
    ]
    return "\n".join(lines)


def _zumo_create_payload(d):
    """Payload /api/operator/zumo_create dari draft (kontrak diperbarui
    21:01 WIB: mode 'upload'|'bank'|'assemble', instrumental bool,
    bank_count untuk assemble). yt_id selalu dikirim (pilihan user,
    default Cozy Sax); style/bahasa/vokal hanya dikirim bila user
    menyebutnya — kosong = pakai profil channel yang tersimpan di
    server (dan field yang dikirim disimpan server sebagai profil
    channel itu). titles hanya bila ada lirik tempel (1 entri, lirik
    utuh apa adanya). Untuk mode='assemble' hanya bank_count + yt_id
    + publish_date (+ theme 'Bank' sebagai judul fallback)."""
    mode = d.get('zumo_mode') or 'upload'
    if mode == 'assemble':
        return {
            "yt_id": d.get('yt_id') or _ZUMO_YT_ID,
            "theme": d.get('theme') or 'Bank',
            "mode": "assemble",
            "bank_count": d.get('bank_count') or 8,
            "publish_date": d.get('publish_date') or '',
            "privacy": "public",
        }
    if mode == 'bank':
        payload = {
            "yt_id": d.get('yt_id') or _ZUMO_YT_ID,
            "theme": d.get('theme', ''),
            "mode": "bank",
            "song_count": d.get('bank_count') or 10,
            "instrumental": d.get('mode') == 'instrumental',
            "model": d.get('model') or _ZUMO_DEFAULT_MODEL,
        }
        if d.get('style'):
            payload["style"] = d['style']
        if d.get('mode') != 'instrumental':
            if d.get('lyrics_language'):
                payload["lyrics_language"] = d['lyrics_language']
            if d.get('vocal_gender'):
                payload["vocal_gender"] = d['vocal_gender']
        return payload
    payload = {
        "yt_id": d.get('yt_id') or _ZUMO_YT_ID,
        "theme": d.get('theme', ''),
        "mode": "upload",
        "instrumental": d.get('mode') == 'instrumental',
        "song_count": d.get('song_count') or 3,
        "publish_date": d.get('publish_date') or '',
        "privacy": "public",
        "model": d.get('model') or _ZUMO_DEFAULT_MODEL,
        "spectrum_color": d.get('spectrum_color') or 'white',
        "particle": d.get('particle') or 'snow',
    }
    if d.get('style'):
        payload["style"] = d['style']
    if d.get('lyrics_language'):
        payload["lyrics_language"] = d['lyrics_language']
    if d.get('vocal_gender'):
        payload["vocal_gender"] = d['vocal_gender']
    if d.get('titles'):
        payload["titles"] = d['titles']
    return payload


def _start_make_zumo(token, chat_id, raw_cmd):
    """Mulai alur /zumo. Dukung sekali kirim:
    /zumo <tema> | <jumlah judul 2/3> | <jadwal> — slot yang sudah diisi
    tidak ditanya lagi; slot yang kurang ditanyakan satu per satu (alur
    yang sama seperti /zumo polos). Segmen jadwal 'lewati' = slot
    berikutnya (server yang atur); 'publish' = langsung tayang."""
    pre = {}
    rest = raw_cmd[len('/zumo'):].strip() if raw_cmd.strip().lower().startswith('/zumo') else ''
    if rest:
        # Buang awalan perintah alami bila ada ("buatkan playlist zumo ...").
        rest = re.sub(r'^(buatkan|bikin|buat)\s+(playlist\s+)?(lagu\s+)?(zumo\s*)?',
                      '', rest, flags=re.I).strip()
    if rest:
        parts = [p.strip() for p in rest.split('|')]
        if len(parts) > 0 and parts[0]:
            pre['theme'] = parts[0][:100]
        if len(parts) > 1 and parts[1]:
            n = _zumo_parse_count(parts[1])
            if n:
                pre['song_count'] = n
        if len(parts) > 2 and parts[2]:
            low2 = parts[2].strip().lower()
            if low2 in ('lewati', '-', 'skip', 'default'):
                pre['publish_date'] = ''
            elif low2 in ('publish', 'langsung', 'tayang sekarang'):
                pre['publish_date'] = _playlist_publish_now_date()
                pre['publish_now'] = True
            else:
                parsed, note = _parse_natural_datetime(parts[2])
                if parsed:
                    pre['publish_date'] = parsed
                    if note:
                        pre['publish_note'] = note
    pre['model'] = _zumo_active_model()
    draft = {"kind": "zumo", "step": _zumo_next_step(pre), "data": dict(pre)}
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 "Siap Bos, kita buat Playlist Zumo bareng-bareng ya!\n"
                 "Lagu orisinal (Kie.ai) — jawab tiap langkah satu per satu, ketik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)


def _start_make_zumobank(token, chat_id, raw_cmd):
    """Mulai alur /zumobank. Dukung sekali kirim:
    /zumobank <tema> | <jumlah lagu 1-30> — slot yang sudah diisi
    tidak ditanya lagi. Generate massal ke bank audio (tanpa upload):
    channel → tema → mode → style → jumlah → konfirmasi."""
    pre = {'zumo_mode': 'bank'}
    rest = raw_cmd[len('/zumobank'):].strip() if raw_cmd.strip().lower().startswith('/zumobank') else ''
    if rest:
        parts = [p.strip() for p in rest.split('|')]
        if parts and parts[0]:
            # Segmen pertama berupa angka saja (mis. '/zumobank 30')
            # dibaca sebagai jumlah, bukan tema.
            n0 = _zumo_parse_bank_count(parts[0])
            if n0 and len(parts) == 1 and not re.search(r'[A-Za-z]', parts[0]):
                pre['bank_count'] = n0
            else:
                pre['theme'] = parts[0][:100]
        if len(parts) > 1 and parts[1]:
            n = _zumo_parse_bank_count(parts[1])
            if n:
                pre['bank_count'] = n
    pre['model'] = _zumo_active_model()
    draft = {"kind": "zumobank", "step": _zumobank_next_step(pre), "data": dict(pre)}
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 "Siap Bos, kita isi Bank Audio Zumo bareng-bareng ya!\n"
                 "Lagu di-generate massal dan disimpan di bank (tanpa upload) — jawab tiap langkah satu per satu, ketik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)


def _start_make_zumorakit(token, chat_id, raw_cmd):
    """Mulai alur /zumorakit. Dukung sekali kirim:
    /zumorakit <jumlah lagu 1-30> | <jadwal> — slot yang sudah diisi
    tidak ditanya lagi. Rakit video dari bank audio: channel →
    jumlah lagu → jadwal → konfirmasi. Lagu dipilih acak dari bank
    oleh server; bila bank kosong server menolak dengan pesan jelas."""
    pre = {'zumo_mode': 'assemble', 'theme': 'Bank'}
    rest = raw_cmd[len('/zumorakit'):].strip() if raw_cmd.strip().lower().startswith('/zumorakit') else ''
    if rest:
        parts = [p.strip() for p in rest.split('|')]
        if parts and parts[0]:
            n = _zumo_parse_bank_count(parts[0])
            if n:
                pre['bank_count'] = n
        if len(parts) > 1 and parts[1]:
            low2 = parts[1].strip().lower()
            if low2 in ('lewati', '-', 'skip', 'default'):
                pre['publish_date'] = ''
            elif low2 in ('publish', 'langsung', 'tayang sekarang'):
                pre['publish_date'] = _playlist_publish_now_date()
                pre['publish_now'] = True
            else:
                parsed, note = _parse_natural_datetime(parts[1])
                if parsed:
                    pre['publish_date'] = parsed
                    if note:
                        pre['publish_note'] = note
    pre['model'] = _zumo_active_model()
    draft = {"kind": "zumorakit", "step": _zumorakit_next_step(pre), "data": dict(pre)}
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 "Siap Bos, kita rakit video dari Bank Audio Zumo ya!\n"
                 "Lagu dipilih acak dari bank channel ini — jawab tiap langkah satu per satu, ketik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)


def _send_zumo_jobs_message(token, chat_id):
    """Status Zumo: saldo Kie + model aktif + 5 job terakhir
    (dipakai /zumo status dan tombol Lihat Job Zumo)."""
    ok, msg, data = _operator_post('/api/operator/zumo_status', {})
    if not ok or not isinstance(data, dict):
        send_message(token, chat_id, "Zumo — Status\n" + _tg_escape(msg))
        return
    lines = ["Zumo — Status", "━━━━━━━━━━━━━━━━━━━━━━"]
    bal = data.get('balance')
    bal_txt = f"{bal} kredit" if bal is not None else "tidak diketahui"
    if data.get('balance_note'):
        bal_txt += f" ({data['balance_note']})"
    lines.append(f"Saldo Kie: {bal_txt}")
    lines.append(f"Model aktif: {_tg_escape(data.get('model') or _ZUMO_DEFAULT_MODEL)}")
    models = data.get('models') or list(_ZUMO_MODELS)
    lines.append("Pilihan model: " + ", ".join(str(m) for m in models))
    bank = data.get('bank') or []
    if bank:
        parts = []
        for b in bank:
            if isinstance(b, dict):
                parts.append(f"{b.get('channel') or '?'} ({b.get('count') or 0} lagu)")
        if parts:
            lines.append("Bank: " + ", ".join(parts))
    jobs = data.get('jobs') or []
    if not jobs:
        lines.append("Belum ada job Zumo.")
    else:
        for j in jobs[:5]:
            title = (j.get('new_title') or j.get('theme') or '?')[:45]
            lines.append(f"• {_tg_escape(title)}")
            ch_txt = j.get('channel_name') or j.get('channel') or ''
            st = f"  Status: {_tg_escape(j.get('status') or '?')}"
            if ch_txt:
                st += f" • {_tg_escape(str(ch_txt)[:40])}"
            if j.get('progress'):
                st += f" • {_tg_escape(j['progress'][:90])}"
            lines.append(st)
            if j.get('error'):
                lines.append(f"  Alasan: {_tg_escape(j['error'][:90])}")
            if j.get('created_at'):
                lines.append(f"  Dibuat: {_tg_escape(str(j['created_at'])[:16])}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("Buat playlist baru: /zumo (bot akan bertanya) atau /zumo tema | jumlah | jadwal.")
    lines.append("Isi bank audio: /zumobank (bot akan bertanya) atau /zumobank tema | jumlah.")
    lines.append("Rakit video dari bank: /zumorakit (bot akan bertanya) atau /zumorakit jumlah | jadwal.")
    lines.append("Ganti model: /zumomodel V6")
    send_message(token, chat_id, "\n".join(lines))


def _send_zumo_created_message(token, chat_id, d, resp):
    """Pesan sukses setelah job Zumo dibuat — jelaskan apa yang terjadi
    selanjutnya (generate → render → preview approval)."""
    n = d.get('song_count') or 3
    titles = d.get('titles') or []
    lines = [
        "Sip Bos, playlist Zumo-nya udah saya buatkan!",
        f"Tema: {_tg_escape(d.get('theme', ''))}",
        f"Channel: {_tg_escape(d.get('channel_name') or _ZUMO_CHANNEL_NAME)}",
        f"Jumlah judul: {n} (jadi {n * 2} varian lagu)",
        f"Mode: {'Instrumental (tanpa lirik)' if d.get('mode') == 'instrumental' else 'Vokal'}",
        f"Jadwal: {_tg_escape(_zumo_schedule_text(d))}",
    ]
    if d.get('mode') == 'instrumental':
        lines.append("Lirik: tanpa lirik (instrumental).")
    elif d.get('lyrics_mode') == 'pasted' and titles:
        lines.append(f"Lirik: tempel 1 judul ({_tg_escape((titles[0].get('title') or '')[:60])}) — dikirim utuh apa adanya.")
        if n > len(titles):
            lines.append(f"Judul lain ({n - len(titles)}) dibuatkan otomatis oleh server (judul + lirik).")
    else:
        lines.append("Lirik: otomatis (dibuatkan server).")
    lines += [
        "",
        "Lagu sedang di-generate (Kie.ai), lalu di-render dan masuk antrean. "
        "Setelah render selesai, preview-nya masuk ke chat ini untuk persetujuan Bos.",
        "",
        "Pantau progresnya dengan `/zumo status` ya Bos!",
    ]
    send_message(token, chat_id, "\n".join(lines))


def _send_zumo_bank_created_message(token, chat_id, d, resp):
    """Pesan sukses setelah order bank audio dibuat — lagu di-generate
    lalu disimpan di bank, tanpa upload."""
    n = d.get('bank_count') or 10
    instrumental = d.get('mode') == 'instrumental'
    lines = [
        "Sip Bos, order Bank Audio Zumo-nya udah saya buatkan!",
        f"Tema: {_tg_escape(d.get('theme', ''))}",
        f"Channel: {_tg_escape(d.get('channel_name') or _ZUMO_CHANNEL_NAME)}",
        f"Jumlah lagu: {n}",
        f"Mode: {'Instrumental (tanpa lirik)' if instrumental else 'Vokal'}",
    ]
    if instrumental:
        lines.append(f"Biaya musik kira-kira: {n * 12} kredit ({n} generate x 12 kredit; tanpa biaya lirik).")
    else:
        lines.append(f"Biaya musik kira-kira: {n * 12} kredit ({n} generate x 12 kredit; biaya lirik ikut tagihan Kie).")
    lines += [
        "",
        "Lagu sedang di-generate (Kie.ai) lalu disimpan di bank audio channel ini, tanpa upload.",
        "",
        "Cek isi bank dengan `/zumo status` ya Bos!",
    ]
    send_message(token, chat_id, "\n".join(lines))


def _send_zumo_assemble_created_message(token, chat_id, d, resp):
    """Pesan sukses setelah video rakitan dari bank dibuat — judul
    video diambil dari respons job bila ada."""
    n = d.get('bank_count') or 8
    resp = resp or {}
    title = (resp.get('new_title') or resp.get('title') or '')[:80]
    lines = [
        "Sip Bos, video rakitan dari Bank Audio-nya udah saya buatkan!",
        f"Channel: {_tg_escape(d.get('channel_name') or _ZUMO_CHANNEL_NAME)}",
        f"Jumlah lagu: {n} (dipilih acak dari bank audio channel ini)",
    ]
    if title:
        lines.append(f"Judul video: {_tg_escape(title)}")
    lines += [
        f"Jadwal: {_tg_escape(_zumo_schedule_text(d))}",
        "",
        "Video sedang dirakit lalu masuk antrean. "
        "Setelah render selesai, preview-nya masuk ke chat ini untuk persetujuan Bos.",
        "",
        "Pantau progresnya dengan `/zumo status` ya Bos!",
    ]
    send_message(token, chat_id, "\n".join(lines))


def _handle_make_answer(token, chat_id, draft, raw_cmd):
    """Proses SATU jawaban dalam alur terpandu. Kembalikan True bila
    pesan ditangani sebagai jawaban (bukan perintah lain)."""
    d = draft.get('data') or {}
    step = draft.get('step')
    kind = draft.get('kind')
    ans = (raw_cmd or '').strip()
    low = ans.lower()
    if step == 'channel':
        if kind in ('zumo', 'zumobank', 'zumorakit') and low in ('lewati', '-', 'skip', 'default'):
            # Zumo/bank/rakit: 'lewati' = channel default Cozy Sax Relaxing.
            d['yt_id'] = _ZUMO_YT_ID
            d['channel_name'] = _ZUMO_CHANNEL_NAME
            draft['data'] = d
            draft['step'] = (_zumo_next_step(d) if kind == 'zumo'
                             else _zumobank_next_step(d) if kind == 'zumobank'
                             else _zumorakit_next_step(d))
            _save_make_draft(chat_id, draft)
            _make_ask_step(token, chat_id, draft)
            return True
        channels = load_channels()
        try:
            idx = int(ans) - 1
            ch = channels[idx] if 0 <= idx < len(channels) else None
        except Exception:
            ch = None
        if ch is None and kind in ('playlist', 'zumo', 'zumobank', 'zumorakit', 'short', 'long'):
            # Playlist/Zumo/bank/rakit/Short/Long: jawaban bebas diterima — ketik nama channel juga boleh.
            matched, all_flag = _nlu_match_channels(_nlu_clean(ans), channels)
            if all_flag:
                if kind == 'zumo':
                    send_message(token, chat_id,
                                 "Playlist Zumo dibuat 1 channel sekali proses Bos — pilih 1 channel ya: balas angkanya atau ketik namanya.")
                elif kind == 'zumobank':
                    send_message(token, chat_id,
                                 "Bank audio dibuat 1 channel sekali proses Bos — pilih 1 channel ya: balas angkanya atau ketik namanya.")
                elif kind == 'zumorakit':
                    send_message(token, chat_id,
                                 "Video rakitan dibuat 1 channel sekali proses Bos — pilih 1 channel ya: balas angkanya atau ketik namanya.")
                elif kind == 'long':
                    send_message(token, chat_id,
                                 "Video long dibuat 1 channel sekali proses Bos — pilih 1 channel ya: balas angkanya atau ketik namanya.")
                elif kind == 'short':
                    # 🔥 v1.3.65: 'semua channel' untuk Short TIDAK ditolak lagi.
                    # Wizard memang 1 channel, tapi jalur NLU bisa
                    # multi-channel (jadwal + konfirmasi sekali, lalu
                    # eksekusi per channel). Draft wizard dibersihkan,
                    # order dilempar ke NLU. Catatan: judul/hook/naskah/
                    # CTA custom dari one-shot /buatshort tidak dibawa —
                    # NLU Short memakai bank konten per niche; konfirmasi
                    # NLU menampilkan rencananya sebelum dieksekusi.
                    _clear_make_draft(chat_id)
                    _slots = {'channels': 'all'}
                    if d.get('theme'):
                        _slots['niche'] = d.get('theme')
                    _pending = {'intent': 'buat_short', 'slots': _slots,
                                'awaiting': None, 'confirmed': False,
                                'created_at': time.time()}
                    _save_nlu_pending(chat_id, _pending)
                    _nlu_advance(token, chat_id, _pending)
                else:
                    # 🔥 v1.3.21: 'semua' untuk Playlist TIDAK ditolak lagi.
                    # Wizard memang 1 channel, tapi jalur NLU bisa
                    # multi-channel (riset + auto-pick per channel).
                    # Draft wizard dibersihkan, order dilempar ke NLU:
                    # login dicek, tema diambil dari jawaban ini, lalu
                    # konfirmasi sekali sebelum dieksekusi.
                    _clear_make_draft(chat_id)
                    # 🔥 v1.3.33: bawa data draft (tema/footage/durasi/
                    # auto/jadwal) — sebelumnya slot dibangun dari nol
                    # sehingga tema & durasi yang sudah diisi hilang.
                    _th = _nlu_extract_playlist_theme(_nlu_clean(ans), channels) or d.get('theme')
                    _slots = {'channels': 'all',
                              'playlist_theme': _th}
                    if d.get('pixabay_theme'):
                        _slots['pixabay_theme'] = d['pixabay_theme']
                    if d.get('duration_min_sec') is not None:
                        _slots['duration_min_sec'] = d['duration_min_sec']
                        _slots['duration_max_sec'] = d.get('duration_max_sec')
                        _slots['duration_note'] = d.get('duration_note') or ''
                    if d.get('auto_pick'):
                        _slots['auto_pick'] = True
                    if d.get('publish_date'):
                        _slots['publish_date'] = d['publish_date']
                        if d.get('publish_note'):
                            _slots['publish_note'] = d['publish_note']
                    else:
                        _slots['publish_now'] = True
                    _pending = {'intent': 'buat_playlist', 'slots': _slots,
                                'awaiting': None, 'confirmed': False,
                                'created_at': time.time()}
                    _save_nlu_pending(chat_id, _pending)
                    _nlu_advance(token, chat_id, _pending)
                return True
            if len(matched) == 1:
                ch = matched[0]
            elif len(matched) > 1:
                send_message(token, chat_id,
                             "🤔 *Ada beberapa channel yang cocok Bos* — balas angkanya ya:\n" +
                             "\n".join(f"{i}. {_tg_escape(_channel_display_name(c))}" for i, c in enumerate(channels, 1)))
                return True
            else:
                send_message(token, chat_id,
                             "😅 *Saya nggak nemu channel itu Bos* — yang terdaftar: "
                             + ", ".join(_channel_display_name(c) for c in channels)
                             + ". Balas angkanya atau ketik namanya ya.")
                return True
        if ch is None:
            send_message(token, chat_id,
                         f"😅 *Eh Bos, pilihannya nggak ada* — balas angkanya 1–{len(channels)} ya.")
            return True
        d['yt_id'] = ch.get('yt_id')
        d['channel_name'] = _channel_display_name(ch)
        draft['data'] = d
        if kind == 'short':
            if not _make_check_bg_or_cancel(token, chat_id, draft):
                return True
            draft['step'] = _short_next_step(d)
        elif kind == 'playlist':
            draft['step'] = _playlist_next_step(d)
        elif kind == 'zumo':
            draft['step'] = _zumo_next_step(d)
        elif kind == 'zumobank':
            draft['step'] = _zumobank_next_step(d)
        elif kind == 'zumorakit':
            draft['step'] = _zumorakit_next_step(d)
        else:
            draft['step'] = _long_next_step(d)
        _save_make_draft(chat_id, draft)
        _make_ask_step(token, chat_id, draft)
        return True
    if kind == 'short':
        if step in _SHORT_LEGACY_STEPS:
            # Draft dari sebelum v1.2.19 (alur 7 langkah) — mulai ulang alur pendek.
            _short_restart_legacy(token, chat_id)
            return True
        if step == 'theme':
            if not ans:
                send_message(token, chat_id,
                             "😅 *Temanya masih kosong Bos* — kirim temanya (mis. `anak-anak`, `tidur`) atau judulnya ya.")
                return True
            day_idx = _wib_now().timetuple().tm_yday
            if low in ('lewati', '-', 'skip'):
                # Lewati = pakai bank tema channel (atau 'sleep') untuk semuanya.
                bank, niche = _short_fallback_bank(d)
                d['theme'] = niche
                d['title'] = bank['titles'][day_idx % len(bank['titles'])]
                d['hook'] = bank['hooks'][day_idx % len(bank['hooks'])]
                d['script'] = bank['scripts'][day_idx % len(bank['scripts'])]
                d['cta'] = bank['ctas'][day_idx % len(bank['ctas'])]
            else:
                niche = _short_detect_theme(ans)
                if niche:
                    bank = _NLU_SHORTS_BANK[niche]
                    d['theme'] = niche
                    d['title'] = bank['titles'][day_idx % len(bank['titles'])]
                    d['hook'] = bank['hooks'][day_idx % len(bank['hooks'])]
                    d['script'] = bank['scripts'][day_idx % len(bank['scripts'])]
                    d['cta'] = bank['ctas'][day_idx % len(bank['ctas'])]
                else:
                    # Judul custom — hook/naskah/CTA dari bank tema channel (atau 'sleep').
                    d['title'] = ans[:200]
                    bank, niche = _short_fallback_bank(d)
                    d['theme'] = niche
                    d['hook'] = bank['hooks'][day_idx % len(bank['hooks'])]
                    d['script'] = bank['scripts'][day_idx % len(bank['scripts'])]
                    d['cta'] = bank['ctas'][day_idx % len(bank['ctas'])]
            # Otomatis, tanpa ditanya: durasi 30 detik, background semua
            # klip video (atau gradien bila galeri kosong), musik bawaan.
            d['duration_sec'] = 30
            d['bg_file'] = ''
            d['bgm_file'] = 'none'
            draft['step'] = 'publish'
        elif step == 'publish':
            if low in ('lewati', '-', 'skip'):
                ch = next((c for c in load_channels() if c.get('yt_id') == d.get('yt_id')), None)
                d['publish_date'] = _next_slot_for_channel(ch)
                d.pop('publish_note', None)
            else:
                parsed, note = _parse_natural_datetime(ans)
                if not parsed:
                    send_message(token, chat_id,
                                 "😅 *Waktu tayangnya belum saya pahami Bos* — coba `hari ini jam 9`, `besok jam 7 malam`, "
                                 "atau `2026-11-20 19:00` (atau `lewati` untuk jadwal berikutnya).")
                    return True
                d['publish_date'] = parsed
                if note:
                    d['publish_note'] = note
                else:
                    d.pop('publish_note', None)
            draft['step'] = 'confirm'
        elif step == 'confirm':
            if set(re.findall(r'[a-z]+', low)) & {'ya', 'buat', 'ok', 'oke', 'lanjut'} or '✅' in low:
                _confirm_make_create(token, chat_id, draft)
                return True
            if set(re.findall(r'[a-z]+', low)) & {'batal', 'tidak', 'cancel'} or '❌' in low:
                _clear_make_draft(chat_id)
                send_message(token, chat_id, "😌 *Sip Bos, dibatalkan.* Tidak ada video yang dibuat.")
                return True
            send_message(token, chat_id,
                         "👆 *Tinggal pencet tombol di atas ya Bos* — ✅ Buat atau ❌ Batal. "
                         "Atau balas `ya` / `batal`.")
            return True
        else:
            # Langkah tidak dikenal — mulai ulang alur pendek.
            _short_restart_legacy(token, chat_id)
            return True
        draft['data'] = d
        _save_make_draft(chat_id, draft)
        _make_ask_step(token, chat_id, draft)
        return True
    if kind == 'playlist':
        if step == 'research':
            # Riset sedang jalan (langkah otomatis) — jangan ditimpa jawaban.
            send_message(token, chat_id, "Sabar Bos, risetnya masih jalan 😅")
            return True
        if step == 'theme':
            if not ans:
                send_message(token, chat_id,
                             "😅 *Temanya masih kosong Bos* — kirim tema musiknya ya (mis. `lagu malaysia lawas`), atau kirim link YouTube + 'jiplak playlist ini'.")
                return True
            _link = _extract_youtube_link(ans)
            if _link:
                # v1.3.5 jiplak: link = sumbernya, bukan tema teks.
                d['source_url'] = _link
                _vid, _plid = _extract_youtube_ids_from_url(_link)
                d['source_kind'] = 'playlist' if _plid else 'video'
                d['theme'] = 'Link YouTube'
            else:
                d['theme'] = ans[:100]
            draft['step'] = _playlist_next_step(d)
        elif step == 'pixabay':
            d['pixabay_theme'] = '' if low in ('lewati', '-', 'skip', 'auto') else ans[:100]
            draft['step'] = _playlist_next_step(d)
        elif step == 'closest':
            # v1.3.13: jawaban atas tawaran "yang paling dekat".
            if low in ('lewati', '-', 'skip', 'semua', 'bebas'):
                d['duration_min_sec'] = None
                d['duration_max_sec'] = None
                d['duration_note'] = ''
                d.pop('closest', None)
                draft['step'] = 'research'
                draft['data'] = d
                _save_make_draft(chat_id, draft)
                _pl_run_research(token, chat_id, draft)
                return True
            # 🔥 v1.3.33: digit 1/2/3 dihapus — dulu diperlakukan sama
            # seperti 'ya' padahal nomor yang dipilih tidak dipakai.
            if low in ('ya', 'pakai', 'oke', 'ok', '✅', 'lanjut', 'mau'):
                d['duration_min_sec'] = None
                d['duration_max_sec'] = None
                d['duration_note'] = ''
                d.pop('closest', None)
                draft['step'] = 'research'
                draft['data'] = d
                _save_make_draft(chat_id, draft)
                _pl_run_research(token, chat_id, draft)
                return True
            if low in ('tidak', 'batal', '❌', 'ganti', 'cancel'):
                d.pop('closest', None)
                draft['step'] = 'duration'
                draft['data'] = d
                _save_make_draft(chat_id, draft)
                _make_ask_step(token, chat_id, draft)
                return True
            parsed = _parse_playlist_duration(ans)
            if parsed:
                d['duration_min_sec'], d['duration_max_sec'], d['duration_note'] = parsed
                d.pop('closest', None)
                draft['step'] = 'research'
                draft['data'] = d
                _save_make_draft(chat_id, draft)
                _pl_run_research(token, chat_id, draft)
                return True
            send_message(token, chat_id,
                         "👆 *Pencet tombol di atas ya Bos* — ✅ Pakai yang paling dekat, atau ❌ Ganti durasi. "
                         "Atau balas rentang durasi baru / `lewati`.")
            return True
        elif step == 'duration':
            if low in ('lewati', '-', 'skip', 'semua', 'bebas'):
                d['duration_min_sec'] = None
                d['duration_max_sec'] = None
                d['duration_note'] = ''
            else:
                parsed = _parse_playlist_duration(ans)
                if not parsed:
                    send_message(token, chat_id,
                                 "😅 *Format durasinya belum saya pahami Bos* — pencet tombol di atas, atau coba `40-70`, `1 jam`, angka `60`, atau ketik `lewati` untuk semua durasi.")
                    return True
                d['duration_min_sec'], d['duration_max_sec'], d['duration_note'] = parsed
            draft['step'] = _playlist_next_step(d)
        elif step == 'pick':
            if low in ('auto', 'otomatis'):
                _pl_auto_pick(token, chat_id, draft)
                return True
            try:
                idx = int(ans) - 1
                cands = d.get('candidates') or []
                cand = cands[idx] if 0 <= idx < len(cands) else None
            except Exception:
                cand = None
            if not cand:
                send_message(token, chat_id,
                             "😅 *Pilihannya nggak ada Bos* — pencet tombol kandidat di atas atau balas angkanya ya.")
                return True
            d['video_id'] = cand.get('video_id')
            d['ref_title'] = cand.get('title', '')
            draft['step'] = _playlist_next_step(d)
        elif step == 'publish':
            lown = ' '.join(low.split())
            if lown in _PLAYLIST_PUBLISH_NOW_WORDS:
                # Langsung tayang: publish_date = jam sekarang (WIB).
                # Saat upload tiba jam ini sudah lewat → backend tidak
                # memasang publishAt, video langsung publik. (Bila
                # publish_date dikosongkan, backend justru menjadwalkan
                # besok 19:00 — jadi JANGAN dikosongkan.)
                d['publish_date'] = _playlist_publish_now_date()
                d['publish_now'] = True
                d.pop('publish_note', None)
            elif low in ('lewati', '-', 'skip', 'default'):
                ch = next((c for c in load_channels() if c.get('yt_id') == d.get('yt_id')), None)
                d['publish_date'] = _next_slot_for_channel(ch)
                d.pop('publish_now', None)
                d.pop('publish_note', None)
            else:
                parsed, note = _parse_natural_datetime(ans)
                if not parsed:
                    send_message(token, chat_id,
                                 "😅 *Waktu tayangnya belum saya pahami Bos* — coba `hari ini jam 9`, `besok jam 7 malam`, "
                                 "`2026-11-20 19:00`, ketik `publish` untuk langsung tayang, atau `default`.")
                    return True
                d['publish_date'] = parsed
                if note:
                    d['publish_note'] = note
                else:
                    d.pop('publish_note', None)
                d.pop('publish_now', None)
            draft['step'] = _playlist_next_step(d)
        elif step == 'confirm':
            if set(re.findall(r'[a-z]+', low)) & {'ya', 'buat', 'ok', 'oke', 'lanjut'} or '✅' in low:
                _confirm_make_create(token, chat_id, draft)
                return True
            if set(re.findall(r'[a-z]+', low)) & {'batal', 'tidak', 'cancel'} or '❌' in low:
                _clear_make_draft(chat_id)
                send_message(token, chat_id, "😌 *Sip Bos, dibatalkan.* Tidak ada playlist yang dibuat.")
                return True
            send_message(token, chat_id,
                         "👆 *Tinggal pencet tombol di atas ya Bos* — ✅ Buat atau ❌ Batal. "
                         "Atau balas `ya` / `batal`.")
            return True
        draft['data'] = d
        _save_make_draft(chat_id, draft)
        _make_ask_step(token, chat_id, draft)
        return True
    if kind == 'zumobank':
        if step == 'theme':
            if not ans:
                send_message(token, chat_id,
                             "Temanya masih kosong Bos — kirim tema lagunya ya (mis. `jazz instrumental santai`).")
                return True
            d['theme'] = ans[:100]
            draft['step'] = _zumobank_next_step(d)
        elif step == 'mode':
            _zumo_apply_mode_answer(d, ans)
            draft['step'] = _zumobank_next_step(d)
        elif step == 'style':
            if low in ('lewati', '-', 'skip', 'default'):
                # 'lewati' = pakai profil channel di server (style TIDAK
                # dikirim di order) — bukan default sax hardcode.
                d['style'] = ''
            else:
                _zumo_set_style(d, ans)
            draft['step'] = _zumobank_next_step(d)
        elif step == 'bank_count':
            if low in ('lewati', '-', 'skip', 'default'):
                d['bank_count'] = 10
            else:
                n = _zumo_parse_bank_count(ans)
                if not n:
                    send_message(token, chat_id,
                                 "Jumlah lagunya 1-30 Bos — balas angkanya (mis. `30`). Atau ketik `lewati` untuk 10 lagu.")
                    return True
                d['bank_count'] = n
            draft['step'] = _zumobank_next_step(d)
        elif step == 'confirm':
            _zumo_confirm_answer(token, chat_id, draft, low)
            return True
        draft['data'] = d
        _save_make_draft(chat_id, draft)
        _make_ask_step(token, chat_id, draft)
        return True
    if kind == 'zumorakit':
        if step == 'bank_count':
            if low in ('lewati', '-', 'skip', 'default'):
                d['bank_count'] = 8
            else:
                n = _zumo_parse_bank_count(ans)
                if not n:
                    send_message(token, chat_id,
                                 "Jumlah lagunya 1-30 Bos — balas angkanya (mis. `8`). Atau ketik `lewati` untuk 8 lagu.")
                    return True
                d['bank_count'] = n
            draft['step'] = _zumorakit_next_step(d)
        elif step == 'publish':
            err = _zumo_apply_publish_answer(d, ans)
            if err:
                send_message(token, chat_id, err)
                return True
            draft['step'] = _zumorakit_next_step(d)
        elif step == 'confirm':
            _zumo_confirm_answer(token, chat_id, draft, low)
            return True
        draft['data'] = d
        _save_make_draft(chat_id, draft)
        _make_ask_step(token, chat_id, draft)
        return True
    if kind == 'zumo':
        if step == 'theme':
            if not ans:
                send_message(token, chat_id,
                             "Temanya masih kosong Bos — kirim tema lagunya ya (mis. `saxophone romantis untuk dinner`).")
                return True
            d['theme'] = ans[:100]
            draft['step'] = _zumo_next_step(d)
        elif step == 'mode':
            _zumo_apply_mode_answer(d, ans)
            draft['step'] = _zumo_next_step(d)
        elif step == 'style':
            if low in ('lewati', '-', 'skip', 'default'):
                # 'lewati' = pakai profil channel di server (style TIDAK
                # dikirim di order) — bukan default sax hardcode.
                d['style'] = ''
            else:
                _zumo_set_style(d, ans)
            draft['step'] = _zumo_next_step(d)
        elif step == 'lyrics':
            if d.get('mode') == 'instrumental':
                # Jaring pengaman: instrumental tidak pernah menanyakan
                # lirik (langkah dilewati _zumo_next_step).
                d['lyrics_mode'] = 'none'
                d.pop('titles', None)
            elif low in ('auto', 'otomatis', 'lewati', '-', 'skip', 'default'):
                d['lyrics_mode'] = 'auto'
                d.pop('titles', None)
            else:
                # Lirik tempel: kirim APA ADANYA (tag [Bagian: arahan]
                # utuh, jangan dipotong, jangan dibersihkan tag-nya).
                # Judul dari baris 'Judul: ...' bila ada, sonst tema.
                title = (_zumo_title_from_lyrics(ans)
                         or (d.get('theme') or 'Lagu Zumo')[:80])
                d['lyrics_mode'] = 'pasted'
                d['titles'] = [{'title': title, 'lyrics': ans}]
            draft['step'] = _zumo_next_step(d)
        elif step == 'count':
            allow_one = bool(d.get('titles'))
            if low in ('lewati', '-', 'skip', 'default'):
                d['song_count'] = 1 if allow_one else 3
            else:
                n = _zumo_parse_count(ans, allow_one=allow_one)
                if not n:
                    rng = "1, 2, atau 3" if allow_one else "2 atau 3"
                    send_message(token, chat_id,
                                 f"Jumlah judulnya {rng} Bos — balas angkanya (tiap judul jadi 2 varian lagu). Atau ketik `lewati`.")
                    return True
                d['song_count'] = n
            draft['step'] = _zumo_next_step(d)
        elif step == 'particle':
            p, c = _zumo_parse_particle(ans)
            if p is None:
                send_message(token, chat_id,
                             "Partikelnya salju atau sakura Bos — balas `salju` atau `sakura` (boleh tambah warna spectrum: putih/cyan/viridis). Atau ketik `lewati`.")
                return True
            d['particle'] = p
            d['spectrum_color'] = c
            draft['step'] = _zumo_next_step(d)
        elif step == 'publish':
            err = _zumo_apply_publish_answer(d, ans)
            if err:
                send_message(token, chat_id, err)
                return True
            draft['step'] = _zumo_next_step(d)
        elif step == 'confirm':
            _zumo_confirm_answer(token, chat_id, draft, low)
            return True
        draft['data'] = d
        _save_make_draft(chat_id, draft)
        _make_ask_step(token, chat_id, draft)
        return True
    # --- long ---
    if step == 'count':
        try:
            n = int(ans)
            if n < 1 or n > 7:
                raise ValueError
            d['count'] = n
        except Exception:
            send_message(token, chat_id, "😅 *Jumlahnya harus angka 1–7 Bos* — balas angkanya ya.")
            return True
        draft['step'] = _long_next_step(d)
    elif step == 'titles':
        if low in ('lewati', '-', 'skip', 'kosong'):
            d['titles'] = []
        else:
            parts = [p.strip() for p in ans.replace('\n', '|').split('|') if p.strip()]
            d['titles'] = parts[:50]
        draft['step'] = _long_next_step(d)
    elif step == 'start':
        if low in ('lewati', '-', 'skip'):
            # 'lewati' = pakai yang sudah diisi sekali kirim; kalau belum
            # ada, default besok jam 19:00.
            if not d.get('start_date'):
                d['start_date'] = (_wib_now() + timedelta(days=1)).strftime('%Y-%m-%d 19:00')
        else:
            parsed, note = _parse_natural_datetime(ans)
            if not parsed:
                send_message(token, chat_id,
                             "😅 *Tanggal mulainya belum saya pahami Bos* — coba `hari ini jam 9`, `besok jam 7 malam`, "
                             "atau `2026-11-20 19:00` (atau `lewati` untuk besok jam 19:00).")
                return True
            d['start_date'] = parsed
            if note:
                d['start_note'] = note
            else:
                d.pop('start_note', None)
        draft['step'] = _long_next_step(d)
    elif step == 'interval':
        if low in ('lewati', '-', 'skip'):
            if not d.get('interval_days'):
                d['interval_days'] = 1
        else:
            try:
                v = float(ans)
                if v < 0.5 or v > 30:
                    raise ValueError
                d['interval_days'] = v
            except Exception:
                send_message(token, chat_id,
                             "😅 *Jeda harus angka 0,5–30 hari Bos* — balas angkanya ya (mis. `1`).")
                return True
        draft['step'] = _long_next_step(d)
    elif step == 'confirm':
        if set(re.findall(r'[a-z]+', low)) & {'ya', 'buat', 'ok', 'oke', 'lanjut'} or '✅' in low:
            _confirm_make_create(token, chat_id, draft)
            return True
        if set(re.findall(r'[a-z]+', low)) & {'batal', 'tidak', 'cancel'} or '❌' in low:
            _clear_make_draft(chat_id)
            send_message(token, chat_id, "😌 *Sip Bos, dibatalkan.* Tidak ada video yang dibuat.")
            return True
        send_message(token, chat_id,
                     "👆 *Tinggal pencet tombol di atas ya Bos* — ✅ Buat atau ❌ Batal. "
                     "Atau balas `ya` / `batal`.")
        return True
    draft['data'] = d
    _save_make_draft(chat_id, draft)
    _make_ask_step(token, chat_id, draft)
    return True

def _confirm_make_create(token, chat_id, draft):
    """Eksekusi pembuatan dari jawaban teks 'ya' di langkah konfirmasi
    (jalur yang sama dengan tombol inline ✅)."""
    d = draft.get('data') or {}
    kind = draft.get('kind')
    retry_cmd = {'short': '/buatshort', 'long': '/buatlong',
                 'playlist': '/playlist', 'zumo': '/zumo',
                 'zumobank': '/zumobank', 'zumorakit': '/zumorakit'}.get(kind, '/buatshort')
    if kind == 'short':
        # Jaring pengaman (alur pendek v1.2.19): judul dari one-shot tanpa
        # hook/naskah → isi dari bank tema; tema ikut dikirim supaya panel
        # memilih suara voiceover + gradien sesuai tema.
        _short_fill_from_bank(d)
        ok, info, resp = request_create_shorts({
            "yt_id": d.get('yt_id'), "title": d.get('title'),
            "hook": d.get('hook', ''), "script": d.get('script', ''),
            "cta": d.get('cta', 'Subscribe'),
            "duration_sec": d.get('duration_sec', 30),
            "publish_date": d.get('publish_date'),
            "text_color": "#FFFFFF",
            "bg_file": d.get('bg_file', ''),
            "bgm_file": d.get('bgm_file', 'none'),
            "theme": d.get('theme', ''),
        })
    elif kind == 'playlist':
        ok, info, resp = _operator_post('/api/operator/playlist_create', {
            "job_id": d.get('job_id'), "video_id": d.get('video_id'),
            "publish_date": d.get('publish_date'), "privacy": "public",
            "pixabay_theme": d.get('pixabay_theme') or '',
        })
        resp = resp or {}
    elif kind in ('zumo', 'zumobank', 'zumorakit'):
        ok, info, resp = _operator_post('/api/operator/zumo_create',
                                        _zumo_create_payload(d))
        resp = resp or {}
    else:
        ok, info, resp = request_create_long({
            "yt_id": d.get('yt_id'), "count": d.get('count', 1),
            "titles": d.get('titles') or [],
            "start_date": d.get('start_date'),
            "interval_days": d.get('interval_days', 1),
        })
    _clear_make_draft(chat_id)
    if not ok:
        send_message(token, chat_id,
                     f"⚠️ *Yah Bos, gagal dibuat* 😅\n{info}\n"
                     f"Coba lagi dengan {retry_cmd} ya Bos!")
        return
    if kind == 'zumo':
        _send_zumo_created_message(token, chat_id, d, resp)
        return
    if kind == 'zumobank':
        _send_zumo_bank_created_message(token, chat_id, d, resp)
        return
    if kind == 'zumorakit':
        _send_zumo_assemble_created_message(token, chat_id, d, resp)
        return
    if kind == 'playlist':
        _send_playlist_created_message(token, chat_id, d, resp)
        return
    _send_make_created_message(token, chat_id, kind, d, resp)

# ==========================================
# 🧠 NLU BAHASA MANUSIA — perintah bebas tanpa LLM
# Rule-based: pencocokan pola + slot di pesan Indonesia sehari-hari.
# TIDAK ADA pemanggilan AI/LLM — semua penafsiran tercatat di
# nlu_audit.jsonl supaya Bos bisa mengaudit & menulis revisi.
# ==========================================
NLU_AUDIT_FILE = os.path.join(BASE_DIR, 'nlu_audit.jsonl')
_NLU_PENDING_TTL = 600  # 10 menit — klarifikasi yang tidak dijawab hangus

_NLU_NICHE_WORDS = {
    'anak': ('anak', 'kids', 'balita', 'bocah', 'paud', 'lagu anak'),
    'asmr': ('asmr',),
    'sleep': ('tidur', 'sleep', 'insomnia', 'nyenyak', 'lullaby'),
    'lofi': ('lofi', 'chill', 'study'),
    'piano': ('piano',),
    'hujan': ('hujan', 'rain'),
    'meditasi': ('meditasi', 'meditation', 'zen'),
    'fokus': ('fokus', 'focus', 'konsentrasi'),
}
# 'asmr' di depan 'sleep': 'suara asmr buat tidur' harus jadi asmr,
# bukan tidur (kata 'tidur' ada di kalimat itu juga).
_NLU_NICHE_ORDER = ('asmr', 'anak', 'piano', 'hujan', 'meditasi', 'fokus', 'lofi', 'sleep')

# Bank konten pembuatan Short via NLU — kalimat polos (bukan beat bernomor
# seperti _SHORTS_IDEAS). Isi hook/naskah/CTA niche lama sama dengan
# AUTO_SHORTS_BANK di app.py supaya panel & Telegram konsisten.
_NLU_SHORTS_BANK = {
    'sleep': {
        'titles': ['Musik Tidur Nyenyak 30 Detik', 'Tidur Nyenyak Malam Ini',
                   'Piano Tidur yang Menenangkan', 'Hujan Rintik untuk Tidur'],
        'hooks': ['Tidur nyenyak malam ini', 'Pejamkan mata, tenangkan pikiran',
                  'Musik tidur untuk malam yang tenang', 'Saatnya istirahat yang nyenyak'],
        'scripts': ['Musik lembut ini menemani tidurmu malam ini.',
                    'Tarik napas dalam-dalam… hembuskan perlahan…',
                    'Biarkan nada-nada tenang ini membawamu ke tidur yang nyenyak.',
                    'Selamat beristirahat, sampai jumpa di pagi yang segar.'],
        'ctas': ['Subscribe untuk musik tidur setiap malam', 'Subscribe'],
    },
    'lofi': {
        'titles': ['Lofi Santai 30 Detik', 'Beat Lofi untuk Fokus',
                   'Lofi Hujan yang Menenangkan', 'Santai Dulu, Dengerin Lofi Ini'],
        'hooks': ['Lofi santai untuk harimu', 'Nada chill untuk fokus belajar',
                  'Beat lembut untuk menemani kerja', 'Santai dulu, dengerin lofi ini'],
        'scripts': ['Lofi lembut ini cocok untuk belajar, kerja, atau sekadar bersantai.',
                    'Pasang headphone-mu dan nikmati alunannya.',
                    'Satu lagu lofi untuk menemani harimu.',
                    'Biarkan beat-nya mengalir pelan-pelan.'],
        'ctas': ['Subscribe untuk lofi baru setiap hari', 'Subscribe'],
    },
    'piano': {
        'titles': ['Piano Lembut 30 Detik', 'Melodi Piano yang Menenangkan',
                   'Piano untuk Sore yang Tenang', 'Dengarkan Piano Ini Sejenak'],
        'hooks': ['Piano lembut untuk hatimu', 'Melodi piano yang menenangkan',
                  'Dengarkan piano ini sejenak', 'Nada piano untuk sore yang tenang'],
        'scripts': ['Melodi piano ini menemani harimu yang tenang.',
                    'Tutup mata sejenak dan dengarkan nadanya.',
                    'Piano lembut untuk istirahat dan refleksi.',
                    'Biarkan setiap nada menenangkan pikiranmu.'],
        'ctas': ['Subscribe untuk piano baru setiap hari', 'Subscribe'],
    },
    'hujan': {
        'titles': ['Suara Hujan 30 Detik', 'Hujan Rintik yang Menenangkan',
                   'Suara Hujan untuk Relaksasi', 'Dengarkan Hujan Turun'],
        'hooks': ['Suara hujan yang menenangkan', 'Dengarkan hujan turun pelan-pelan',
                  'Hujan rintik untuk tidur nyenyak', 'Suara hujan untuk relaksasi'],
        'scripts': ['Suara hujan ini membantu pikiranmu rileks.',
                    'Bayangkan hujan turun di luar jendela…',
                    'Rintik hujan untuk tidur yang lebih nyenyak.',
                    'Biarkan suara hujan menenangkan harimu.'],
        'ctas': ['Subscribe untuk suara hujan setiap hari', 'Subscribe'],
    },
    'meditasi': {
        'titles': ['Meditasi Sejenak 30 Detik', 'Tarik Napas, Tenangkan Pikiran',
                   'Hening Sejenak', 'Meditasi untuk Harimu'],
        'hooks': ['Tarik napas… tenangkan pikiran', 'Meditasi sejenak untuk harimu',
                  'Hening sejenak, dengarkan dirimu', 'Waktu untuk bermeditasi'],
        'scripts': ['Tarik napas dalam… dan hembuskan perlahan.',
                    'Fokus pada napasmu, lepaskan semua beban.',
                    'Satu menit tenang untuk memulai harimu.',
                    'Biarkan pikiranmu mengalir seperti air.'],
        'ctas': ['Subscribe untuk meditasi harian', 'Subscribe'],
    },
    'fokus': {
        'titles': ['Musik Fokus 30 Detik', 'Fokus Penuh untuk Kerjamu',
                   'Musik Fokus untuk Produktivitas', 'Konsentrasi Penuh'],
        'hooks': ['Fokus penuh untuk kerjamu', 'Musik fokus untuk produktivitas',
                  'Konsentrasi penuh, tanpa distraksi', 'Musik untuk fokus belajar dan kerja'],
        'scripts': ['Musik ini membantumu fokus tanpa distraksi.',
                    'Pasang headphone dan mulai kerjamu.',
                    'Fokus sebentar, istirahat sejenak, lalu lanjut lagi.',
                    'Biarkan musik ini menemani jam fokusmu.'],
        'ctas': ['Subscribe untuk musik fokus harian', 'Subscribe'],
    },
    'anak': {
        'titles': ['Lagu Anak Ceria — Ayo Bernyanyi!', 'Belajar Berhitung 1-10 Sambil Bernyanyi',
                   'Lagu Warna Pelangi untuk Anak', 'Gerak dan Lagu Anak Ceria'],
        'hooks': ['Lagu ceria untuk anak-anak!', 'Ayo bernyanyi bersama!',
                  'Belajar sambil bernyanyi, seru!', 'Lagu anak yang bikin semangat'],
        'scripts': ['Ayo bernyanyi bersama, tepuk tangan yuk!',
                    'Lagu ceria ini untuk menemani harimu yang seru.',
                    'Belajar berhitung sambil bernyanyi, menyenangkan!',
                    'Ikuti gerakannya, lompat dan bernyanyi bersama!'],
        'ctas': ['Subscribe ya!', 'Ajak temanmu subscribe!'],
    },
    'asmr': {
        'titles': ['ASMR Bisikan Lembut untuk Tidur', 'Suara Ketukan Halus ASMR',
                   'ASMR Menenangkan 30 Detik', 'Bisikan ASMR untuk Relaksasi'],
        'hooks': ['Bisikan lembut untuk tidur nyenyak', 'Suara ketukan halus yang menenangkan',
                  'ASMR lembut untuk relaksasi malam ini', 'Dengarkan suara tenang ini… rileks'],
        'scripts': ['Bisikan lembut ini menemani tidurmu malam ini.',
                    'Dengarkan ketukan halus ini… tarik napas perlahan.',
                    'Suara lembut untuk menenangkan pikiran dan tubuhmu.',
                    'Pasang headphone-mu dan biarkan suara ini membawamu tidur.'],
        'ctas': ['Subscribe untuk ASMR baru setiap malam', 'Subscribe'],
    },
}


def _nlu_clean(text):
    """Normalisasi seperti handle_user_command: huruf kecil, buang tanda
    baca (tanda hubung ikut hilang: 'anak-anak' → 'anakanak')."""
    return re.sub(r'[^\w\s/]', '', (text or '').strip().lower())


def _nlu_audit_log(text, parsed, action, detail=''):
    """Satu baris JSON per penafsiran — file audit untuk Bos."""
    try:
        rec = {"ts_wib": _wib_now().strftime('%Y-%m-%d %H:%M:%S'),
               "text": text, "intent": parsed.get('intent'),
               "confidence": parsed.get('confidence'),
               "slots": parsed.get('slots') or {},
               "action": action, "detail": detail}
        with open(NLU_AUDIT_FILE, 'a', encoding='utf-8') as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"Gagal tulis nlu_audit: {e}", flush=True)


def _nlu_norm_name(name):
    return re.sub(r'[^a-z0-9 ]', '', (name or '').lower()).strip()


def _nlu_match_channels(clean, channels):
    """(matched, all_flag): cocokkan nama channel di teks. matched = channel
    yang namanya (atau kata pertamanya, min 4 huruf) disebut. all_flag True
    bila teks menyebut 'semua'/'ke-3'/'ketiga' channel."""
    all_flag = ('semua channel' in clean or 'ketiga' in clean
                or 'tiga channel' in clean
                or bool(re.search(r'\bke\s*3\b', clean))
                or bool(re.search(r'\bsemua\b', clean))
                or ('semua' in clean and 'channel' in clean))
    matched = []
    for ch in channels or []:
        nm = _nlu_norm_name(_channel_display_name(ch))
        if not nm:
            continue
        if nm in clean:
            matched.append(ch)
            continue
        first = nm.split()[0] if nm.split() else ''
        # Kata generik 'channel' bukan nama — jangan sampai teks apa pun
        # yang menyebut 'channel' cocok dengan channel bernama 'Channel ...'.
        if first not in ('channel', 'chanel') and len(first) >= 4 \
                and re.search(r'\b' + re.escape(first) + r'\b', clean):
            matched.append(ch)
    return matched, all_flag


def _nlu_detect_niche(clean):
    """(kunci_niche|None, kata_tema_tidak_dikenal|None)."""
    for key in _NLU_NICHE_ORDER:
        for w in _NLU_NICHE_WORDS[key]:
            if w in clean:
                return key, None
    m = re.search(r'tema\s+([a-z]+)', clean)
    if m:
        return None, m.group(1)
    return None, None


def _nlu_extract_time(clean):
    """'jam 7 malam' → '19:00'; 'jam 19:00' → '19:00'; 'jam 7' → '07:00'."""
    m = re.search(r'jam\s+(\d{1,2})(?:[:.](\d{2}))?\s*(pagi|siang|sore|malam|petang)?', clean)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    mer = m.group(3) or ''
    if mer in ('sore', 'malam', 'petang') and hour < 12:
        hour += 12
    if mer == 'pagi' and hour == 12:
        hour = 0
    if hour > 23 or minute > 59:
        return None
    return f"{hour:02d}:{minute:02d}"


# Jangkar waktu harian untuk video Short PERTAMA (aturan jeda 6 jam,
# 2026-11-19): pagi 07:00, siang 12:00, sore/petang 17:00, malam 19:00.
_NLU_DAYPART_HOURS = {
    'pagi': '07:00', 'siang': '12:00', 'sore': '17:00',
    'malam': '19:00', 'petang': '17:00',
}
_NLU_MONTHS_ID = ['Jan', 'Feb', 'Mar', 'Apr', 'Mei', 'Jun', 'Jul', 'Agu',
                  'Sep', 'Okt', 'Nov', 'Des']


def _nlu_extract_dayparts(clean):
    """(anchor_time, dayparts) dari perintah. Jam eksplisit MENANG
    ('jam 8 pagi' → 08:00, 'jam 8 pagi dan jam 5 sore' → 08:00);
    kalau tidak ada jam eksplisit, kata hari jadi jangkar ('pagi sore' →
    07:00, ['pagi', 'sore']). Tanpa keduanya → (None, [])."""
    explicit = re.findall(
        r'jam\s+(\d{1,2})(?:[:.](\d{2}))?\s*(pagi|siang|sore|malam|petang)?',
        clean or '')
    times = []
    for h, m, mer in explicit:
        hour = int(h)
        minute = int(m or 0)
        if mer in ('sore', 'malam', 'petang') and hour < 12:
            hour += 12
        if mer == 'pagi' and hour == 12:
            hour = 0
        if hour <= 23 and minute <= 59:
            times.append(f"{hour:02d}:{minute:02d}")
    if times:
        return times[0], []
    # Kata hari dicari sebagai substring (teks sudah dibersihkan — 'pagi-sore'
    # jadi 'pagisore'), urut kemunculan. 'semalam' dikecualikan (itu semalam,
    # bukan permintaan jam malam).
    scan = (clean or '').replace('semalam', ' ')
    positions = []
    for word in _NLU_DAYPART_HOURS:
        idx = scan.find(word)
        if idx >= 0:
            positions.append((idx, word))
    positions.sort()
    found = [w for _, w in positions]
    if found:
        return _NLU_DAYPART_HOURS[found[0]], found
    return None, []


def _nlu_channel_occupied(yt_id):
    """Tanggal+jam yang sudah terisi untuk channel ini: task AKTIF
    (menunggu approval/antrean) + video yang sudah TAYANG di riwayat.
    Entri Gagal/Ditolak tidak dihitung — slotnya bebas. Dipakai aturan
    jeda 6 jam supaya Short baru tidak menimpa slot lama."""
    occ = set()
    try:
        tasks = load_tasks() or {}
    except Exception:
        return occ
    for t in (tasks.get('active') or []):
        if (t.get('yt_id') or (t.get('blueprint') or {}).get('yt_id')) == yt_id:
            d = t.get('time') or (t.get('blueprint') or {}).get('publish_date')
            if d:
                occ.add(str(d)[:16])
    for h in (tasks.get('history') or []):
        if h.get('yt_id') != yt_id:
            continue
        if 'Tayang' not in (h.get('status') or ''):
            continue
        d = h.get('time') or (h.get('blueprint') or {}).get('publish_date')
        if d:
            occ.add(str(d)[:16])
    return occ


def _nlu_compute_shorts_schedule(ch, count, anchor_time, publish_time, first_dt=None):
    """Hitung jadwal Shorts aturan jeda 6 jam (2026-11-19): video pertama
    di slot kosong berikutnya (jam eksplisit/jangkar hari/jam tetap
    channel), video berikutnya = sebelumnya + 6 jam — boleh lewat tengah
    malam ke hari berikutnya. first_dt ('YYYY-MM-DD HH:MM' lengkap dari
    perintah alami, mis. 'besok jam 9') MENANG atas anchor/publish_time.
    Bila slot hasil hitungan sudah terisi, geser +6 jam sampai kosong.
    Kembalikan list 'YYYY-MM-DD HH:MM'."""
    first = first_dt or anchor_time or publish_time
    dt = None
    if first:
        try:
            if len(str(first)) > 5:  # 'YYYY-MM-DD HH:MM' lengkap — pakai apa adanya
                dt = datetime.strptime(str(first), '%Y-%m-%d %H:%M').replace(
                    tzinfo=ZoneInfo("Asia/Jakarta"))
            else:
                hh, mm = str(first).split(':')
                dt = _wib_now().replace(hour=int(hh), minute=int(mm),
                                        second=0, microsecond=0)
                if dt <= _wib_now():
                    dt = dt + timedelta(days=1)
        except Exception:
            dt = None
    if dt is None:
        try:
            dt = datetime.strptime(
                _next_slot_for_channel(ch), '%Y-%m-%d %H:%M').replace(
                    tzinfo=ZoneInfo("Asia/Jakarta"))
        except Exception:
            dt = (_wib_now() + timedelta(days=1)).replace(
                hour=19, minute=0, second=0, microsecond=0)
    yt_id = (ch or {}).get('yt_id')
    occ = _nlu_channel_occupied(yt_id) if yt_id else set()
    out = []
    prev = None
    for _ in range(max(1, count or 1)):
        cand = dt if prev is None else prev + timedelta(hours=6)
        guard = 0
        while cand.strftime('%Y-%m-%d %H:%M') in occ and guard < 200:
            cand = cand + timedelta(hours=6)
            guard += 1
        out.append(cand.strftime('%Y-%m-%d %H:%M'))
        occ.add(cand.strftime('%Y-%m-%d %H:%M'))
        prev = cand
    return out


def _nlu_build_shorts_schedules(slots):
    """{yt_id: [jadwal]} per channel dari slots (anchor/publish_time/count).
    Dihitung sekali sebelum konfirmasi supaya pesan konfirmasi menyebut
    semua jam hasil hitungan."""
    count = slots.get('count') or 1
    sched = {}
    for ch in load_channels():
        if ch.get('yt_id') in (slots.get('channels') or []):
            sched[ch.get('yt_id')] = _nlu_compute_shorts_schedule(
                ch, count, slots.get('anchor_time'), slots.get('publish_time'),
                slots.get('publish_date'))
    return sched


def _nlu_format_schedule(dts):
    """['2026-11-20 07:00', '2026-11-20 13:00', '2026-11-21 01:00'] →
    '20 Nov 07:00, 13:00, 21 Nov 01:00' (tanggal ditulis ulang hanya
    bila harinya berganti)."""
    parts = []
    last_date = None
    for s in dts or []:
        try:
            dt = datetime.strptime(str(s), '%Y-%m-%d %H:%M')
        except Exception:
            continue
        dkey = dt.strftime('%Y-%m-%d')
        if dkey != last_date:
            parts.append(f"{dt.day} {_NLU_MONTHS_ID[dt.month - 1]} "
                         f"{dt.strftime('%H:%M')}")
            last_date = dkey
        else:
            parts.append(dt.strftime('%H:%M'))
    return ', '.join(parts)


def _nlu_extract_duration(clean):
    """'30 detik' → 30 (dijepit ke 15/30/60); '1 menit' → 60."""
    m = re.search(r'(\d+)\s*(detik|dtk|det)\b', clean)
    if m:
        v = int(m.group(1))
        return 15 if v <= 20 else (30 if v <= 45 else 60)
    m = re.search(r'(\d+)\s*menit\b', clean)
    if m:
        return 60 if int(m.group(1)) >= 1 else 30
    return None


def _nlu_extract_count(clean, intent):
    """Jumlah dari angka + kata benda ('3 short', '2 video long')."""
    if intent == 'buat_long':
        m = re.search(r'(\d+)\s*(video|vid|long)\b', clean)
    else:
        m = re.search(r'(\d+)\s*(short|video|vid)\b', clean)
    if m:
        return max(1, min(7, int(m.group(1))))
    m = re.search(r'\b(buat|bikin|buatkan)\s+(\d+)\b', clean)
    if m:
        return max(1, min(7, int(m.group(2))))
    return None


def _nlu_extract_cta(raw):
    """Teks CTA dari perintah ('cta subscribe', 'ajakan cta untuk
    subscribe', 'cta: Subscribe ya!'). Diambil dari teks ASLI (sebelum
    dibersihkan) supaya kapitalisasi & tanda baca ikut kebawa."""
    low = (raw or '').lower()
    tail = ''
    idx = low.find('cta')
    if idx >= 0:
        tail = raw[idx + 3:]
    else:
        m = re.search(r'ajakan\s+(.+)', low)
        if m:
            tail = raw[m.start(1):]
    if not tail:
        return None
    tail = re.sub(r'^[\s:,\-"\']+', '', tail)
    tail = re.sub(r'^(untuk|ajakan|ajak|yang|buat|supaya|agar|biar|nya)\s+', '', tail, flags=re.I)
    tail = tail.strip().strip('"').strip("'")
    words = tail.split()
    if not words:
        return None
    if any(w.lower().startswith('subscrib') for w in words[:3]):
        return 'Subscribe'
    cta = ' '.join(words[:6])[:60].strip()
    return cta[:1].upper() + cta[1:] if cta else None


def _nlu_extract_days(clean):
    """'30 hari' → 30; 'sebulan' → 30; 'seminggu' → 7; 'dua minggu' → 14."""
    m = re.search(r'(\d+)\s*hari\b', clean)
    if m:
        return max(1, min(90, int(m.group(1))))
    if 'sebulan' in clean or 'satu bulan' in clean or '1 bulan' in clean:
        return 30
    if 'tiga bulan' in clean or '3 bulan' in clean:
        return 90
    if 'dua minggu' in clean or '2 minggu' in clean:
        return 14
    if 'seminggu' in clean or 'satu minggu' in clean:
        return 7
    return None


def _nlu_extract_mode(clean):
    """'auto' dari kata kunci auto; 'approval' dari kata kunci approval.
    'matikan autopilot' = matikan autopilotnya → approval (bukan auto);
    dicek dulu sebelum 'autopilot' generik."""
    if re.search(r'matikan autopilot|nonaktifkan autopilot|autopilot off|matikan otomatis', clean):
        return 'approval'
    if re.search(r'\bauto\b|otomatis|langsung tayang|tanpa approval|tanpa persetujuan|full.?auto|autopilot', clean):
        return 'auto'
    if re.search(r'approval|persetujuan|minta izin|manual|izin dulu', clean):
        return 'approval'
    return None


# Segmen durasi playlist dalam kalimat NLU ('durasi 40-70 menit' /
# 'durasi 40 sampai 70 menit' / 'durasi 1 jam' / 'durasi 60 menit') —
# dipakai untuk mengekstrak slot durasi DAN membuang segmen itu dari
# tema musik / tema footage supaya tidak nyangkut jadi tema.
_NLU_PLAYLIST_DURATION_SEG = (
    r'\bdurasi\s+(?:(?:minimal|minimum|maksimal|maksimum|paling\s+tidak|setidaknya)\s+)?'
    r'\d+(?:[.,]\d+)?\s*'
    r'(?:(?:-|–|—|sampai|s/d|sd|hingga)\s*\d+(?:[.,]\d+)?\s*)?'
    r'(?:jam|menit|mnt)?')


def _nlu_extract_playlist_duration(raw):
    """Slot durasi NLU: 'durasi 40-70 menit' → (2400, 4200, '40–70 menit');
    'durasi 1 jam' → (3000, 4200, 'sekitar 1 jam (50–70 menit)');
    'durasi 60 menit' → (3300, 3900, 'sekitar 60 menit (55–65 menit)').
    PENTING: dibaca dari teks ASLI (bukan clean) karena _nlu_clean
    membuang tanda hubung — '40-70' jadi '4070' di clean dan akan
    disalahartikan sebagai 4070 menit. Tidak disebut → None (nanti
    ditanyakan, bukan ditebak)."""
    t = (raw or '').lower()
    m = re.search(_NLU_PLAYLIST_DURATION_SEG, t)
    if not m:
        return None
    seg = re.sub(r'^\bdurasi\s+', '', m.group(0)).strip()
    return _parse_playlist_duration(seg)


def _nlu_trailing_footage_theme(text):
    """'tema X' di ujung perintah playlist = footage Pixabay (v1.3.5
    koreksi), BILA tema musik sudah disebut di awal ('lagu ...') atau
    setelah klausa durasi. Kembalikan X (1-2 kata) atau None.
    Contoh: '... durasi minimal 40 menit tema pantai' -> 'pantai'.
    'buatkan playlist tema lofi' -> None (tidak ada tema musik di awal,
    jadi 'lofi' tetap tema musik, bukan footage)."""
    m = re.search(r'\btema\s+([a-z][a-z0-9]*(?:\s+[a-z][a-z0-9]*)?)\s*(?:\s+(?:saja|aja|dong))?\s*$', (text or ''))
    if not m:
        return None
    pre = text[:m.start()]
    if not (re.search(r'\blagu\b', pre) or re.search(_NLU_PLAYLIST_DURATION_SEG, pre)):
        return None
    cap = re.sub(r'\s+(saja|aja|dong)$', '', m.group(1).strip())
    return cap or None


def _nlu_extract_pixabay_theme(clean):
    """Tema footage Pixabay dari kalimat ('... footage autumn' /
    '... tema pixabay autumn' / '... tema footage autumn'). Segmen
    durasi dibuang dulu supaya 'durasi 40-70 menit' tidak nyangkut."""
    # v1.3.5 koreksi: 'tema X' di ujung = footage (lihat helper di atas).
    _trail = _nlu_trailing_footage_theme(clean)
    if _trail:
        return _trail
    clean = re.sub(_NLU_PLAYLIST_DURATION_SEG, ' ', clean)
    m = re.search(r'(?:tema\s+pixabay|tema\s+footage|footage|pixabay)\s+([a-z0-9][a-z0-9 ]*?)(?:\s+(?:di|untuk|ke|pada|channel|tema)\b|$)', clean)
    if not m:
        return None
    return re.sub(r'\s+', ' ', m.group(1)).strip()[:50] or None


# v1.3.5 koreksi: typo umum di tema musik dinormalisasi (malaysa -> malaysia).
_NLU_THEME_TYPO_FIX = {'malaysa': 'malaysia', 'malaysya': 'malaysia',
                      'malayasia': 'malaysia'}


def _nlu_extract_playlist_theme(clean, channels):
    """Tema musik playlist: sisa kalimat setelah buang kata kerja, kata
    'playlist', nama channel, segmen Pixabay, klausa cek-login, jumlah
    channel, dan kata sambung.
    'buatkan playlist lagu malaysia lawas di aruya footage autumn'
    → 'lagu malaysia lawas'.
    'buatkan video playlist lagu nostalgia tahun 2000 an untuk ke 4
    channel ku yang ada saat ini, cek dulu sebelumnya apakah semua
    channel masih terlogin semua' → 'lagu nostalgia tahun 2000an'.
    Kosong bila tema tidak disebut."""
    t = clean
    # v1.3.5 koreksi: buang tanda baca dulu — "saya, cari" tanpa ini
    # menyisakan koma menggantung (", lagu malaysia").
    t = re.sub(r'[,;:.!?]+', ' ', t)
    # v1.3.5: buang link YouTube (ditangani jalur jiplak, bukan tema)
    # dan awalan instruksi ganti tema — "coba ganti tema X" artinya
    # temanya diganti jadi X, bukan seluruh kalimat jadi tema.
    t = re.sub(r'https?://\S+', ' ', t)
    t = re.sub(r'^(coba\s+)?(ganti|ubah|gantiin)\s+(tema|temanya|lagu|lagunya)\s+', ' ', t)
    t = re.sub(r'^(coba\s+)?(ganti|ubah|gantiin)\s+', ' ', t)
    t = re.sub(r'^coba\s+', ' ', t)
    # v1.3.5 koreksi: 'tema X' di ujung = footage Pixabay (lihat
    # _nlu_trailing_footage_theme) — buang dari tema musik.
    if _nlu_trailing_footage_theme(t):
        t = re.sub(r'\btema\s+[a-z][a-z0-9]*(?:\s+[a-z][a-z0-9]*)?\s*(?:\s+(?:saja|aja|dong))?\s*$', ' ', t)
    # Segmen durasi dibuang dulu supaya 'durasi 40-70 menit' tidak
    # nyangkut jadi tema.
    t = re.sub(_NLU_PLAYLIST_DURATION_SEG, ' ', t)
    m = re.search(r'(?:tema\s+pixabay|tema\s+footage|footage|pixabay)\s+[a-z0-9 ]*?(?:\s+(?:di|untuk|ke|pada|channel|tema)\b|$)', t)
    if m:
        t = (t[:m.start()] + ' ' + t[m.end():]).strip()
    # 🔥 v1.3.2: klausa cek-login dibuang dulu (semua teks mulai dari
    # 'cek'/'periksa'/'pastikan' bila mengandung 'login'/'terlogin'/
    # 'masuk') — kalau tidak dibuang, seluruh kalimat perintah nyangkut
    # jadi tema ("...cek dulu sebelumnya apakah masih terlogin semua").
    _m_login = re.search(r'\b(cek|periksa|pastikan)\b', t)
    if _m_login and re.search(r'\b(login|terlogin|masuk)\b', t[_m_login.start():]):
        t = t[:_m_login.start()].strip()
    # Jumlah channel ("ke 4 channel", "4 channel", "keempat") + sisa
    # "ku yang ada saat ini" bukan tema — buang. 'video' juga bukan tema
    # (itu jenis artefak: "buatkan video playlist ...").
    t = re.sub(r'\bke\s*\d+\s*channel\b', ' ', t)
    t = re.sub(r'\b\d+\s*channel\b', ' ', t)
    t = re.sub(r'\b(keempat|ketiga|kedua|kelima|keenam|ketujuh|kedelapan|kesembilan|kesepuluh)\b', ' ', t)
    t = re.sub(r'\byang ada saat ini\b', ' ', t)
    t = re.sub(r'\bku\b', ' ', t)
    # 🔥 v1.3.30: segmen jadwal-waktu bukan tema — 'jadwalkan
    # masing-masing 1 video untuk nanti sore di mulai jam 4 sampai
    # semua akun selesai' temanya KOSONG, bukan 'masingmasing 1 nanti
    # sore mulai jam 4 ...'. Polanya spesifik ('nanti sore', 'jam 4',
    # '1 video') supaya 'lagu sore' tanpa 'nanti'/'jam' tetap aman.
    t = re.sub(r'\bnanti\s+(pagi|siang|sore|petang|malam)\b', ' ', t)
    t = re.sub(r'\b(jam|pukul)\s+\d{1,2}(?::\d{2})?\s*(pagi|siang|sore|petang|malam)?\b', ' ', t)
    t = re.sub(r'\b\d+\s*video\b', ' ', t)
    t = re.sub(r'\b(masingmasing|masing-masing|masing|mulai|sampai|akun|selesai)\b', ' ', t)
    # 🔥 v1.3.33: kata hari relatif bukan tema — 'lagu malaysia besok'
    # temanya 'lagu malaysia', bukan 'lagu malaysia besok'.
    t = re.sub(r'\b(hari ini|besok|lusa)\b', ' ', t)
    t = re.sub(r'\bvideo\b', ' ', t)
    t = re.sub(r'\b(buatkan|bikin|buat|tolong|buatlah)\b', ' ', t)
    t = re.sub(r'\bmusik\s+playlist\b', ' ', t)
    t = re.sub(r'\bplaylist\b', ' ', t)
    # v1.3.5 koreksi: kata perintah/pengisi bukan tema — 'jadwalkan ke 4
    # channel saya, cari lagu ... saja' -> sisanya cuma tema musiknya.
    t = re.sub(r'\b(jadwalkan|jadwal|jadwalin|saya|aku|cari|carikan|saja|aja|dong|nih|deh)\b', ' ', t)
    # v1.3.13: kata jadwal/bukan-tema — "jadwal sebulan konten lagu
    # malaysia" temanya 'lagu malaysia', bukan 'sebulan konten lagu
    # malaysia' (pernah: 'sebulan' malah cocok ke video 'Gaji Sebulan').
    t = re.sub(r'\b(sebulan|seminggu|sehari|selama|konten)\b', ' ', t)
    # 🔥 v1.3.33: 'bulan'/'minggu' telanjang bisa jadi isi tema
    # ('lagu bulan ramadhan') — buang hanya sebagai jadwal
    # ('bulan depan', 'bulan ini', 'minggu depan', 'minggu ini').
    t = re.sub(r'\b(bulan|minggu)\s+(depan|ini)\b', ' ', t)
    # 🔥 v1.3.21: hitungan hari bukan tema — '7 hari kedepan musik
    # malaysia' temanya 'musik malaysia', bukan '7 hari kedepan musik
    # malaysia'. ('hari raya' aman: yang dibuang hanya angka+hari.)
    t = re.sub(r'\b\d+\s*hari\b', ' ', t)
    t = re.sub(r'\b(kedepan|ke\s+depan)\b', ' ', t)
    # Kata sisa durasi (bila segmen durasi lolos karena format aneh).
    t = re.sub(r'\b(minimal|maksimal|minimum|maksimum|durasi|menit|mnt)\b', ' ', t)
    for c in (channels or []):
        nm = _nlu_norm_name(_channel_display_name(c))
        if nm:
            t = t.replace(nm, ' ')
            # Nama channel sering disebut sebagian saja ('Aruya', tanpa
            # '0089') — buang juga tiap kata namanya (≥3 huruf) supaya
            # tidak nyangkut jadi 'tema' (mis. 'lagu malaysia lawas aruya').
            for w in nm.split():
                if len(w) >= 3:
                    t = re.sub(r'\b' + re.escape(w) + r'\b', ' ', t)
    t = re.sub(r'\b(untuk|di|ke|pada|dari|channel|semua|tema)\b', ' ', t)
    # v1.3.5 koreksi: typo umum dinormalisasi (malaysa -> malaysia).
    for _bad, _good in _NLU_THEME_TYPO_FIX.items():
        t = re.sub(r'\b' + _bad + r'\b', _good, t)
    # "tahun 2000 an" → "tahun 2000an" (tahun + 'an' menyatu).
    t = re.sub(r'\b(\d{2,4})\s+an\b', r'\1an', t)
    t = re.sub(r'\s+', ' ', t).strip()
    return t[:100] or None


def _nlu_extract_zumo_theme(clean, channels):
    """Tema lagu Zumo: sisa kalimat setelah buang kata kerja, kata 'zumo',
    'playlist', 'lagu', 'kie', 'baru', dan nama channel.
    'buatkan playlist zumo saxophone romantis untuk dinner' → 'saxophone
    romantis dinner' — kata 'sax'/'saxophone' TIDAK dibuang (bisa jadi isi
    tema); hanya bila sisanya tinggal 'sax'/'saxophone' saja (mis.
    'buat lagu sax') dianggap tidak ada tema → None. Kosong bila tema
    tidak disebut."""
    t = clean
    t = re.sub(r'\b(buatkan|bikin|buat|tolong|buatlah)\b', ' ', t)
    t = re.sub(r'\bzumo\b', ' ', t)
    t = re.sub(r'\bplaylist\b', ' ', t)
    t = re.sub(r'\b(lagu|lagu-lagu)\b', ' ', t)
    t = re.sub(r'\bkie\b', ' ', t)
    t = re.sub(r'\bbaru\b', ' ', t)
    for c in (channels or []):
        nm = _nlu_norm_name(_channel_display_name(c))
        if nm:
            t = t.replace(nm, ' ')
            for w in nm.split():
                if len(w) >= 3:
                    t = re.sub(r'\b' + re.escape(w) + r'\b', ' ', t)
    t = re.sub(r'\b(untuk|di|ke|pada|dari|channel|semua|tema)\b', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip()
    if t in ('sax', 'saxophone'):
        return None
    return t[:100] or None


# ==========================================
# 🔥 v1.2.20 — SINONIM + TOLERANSI TYPO perintah baru Bagian B
# Satu helper bersama untuk kata-kata perintah BARU saja (NLU lama tidak
# dirombak). Sinonim alami per perintah + toleransi salah ketik ringan:
# kata cocok bila sama persis, substring salah satu arah (kata tergabung
# "jadwalbuln", kependekan "jadwa"), atau kemiripan difflib ≥ 0.8.
# Bila cocoknya lewat typo (fuzzy), confidence lebih rendah (55) supaya
# bot menanyakan "Maksud Bos ...?" dulu — tidak pernah langsung
# "nggak ngerti" tanpa tawaran.
# ==========================================
_CMD_SYNONYMS = {
    # intent: (daftar frasa sinonim, confidence bila cocok persis)
    'jadwal_bulan': (["jadwal bulan", "jadwal sebulan", "kalender",
                      "jadwal 30 hari", "jadwal bulanan"], 85),
    'atur_jadwal': (["atur jadwal", "ubah jadwal", "ganti jam tayang",
                     "setting jadwal", "pengaturan jadwal", "ganti jadwal"], 85),
    'laporan_detail': (["laporan lengkap", "laporan rinci", "report detail",
                        "laporan detail", "laporan terperinci"], 85),
    'channel_list': (["list channel", "daftar channel", "channel saya",
                      "info channel", "channelku"], 85),
}
# Kata tunggal yang bila salah ketik ringan tetap dikenali (teks hanya
# 1 kata): "chnnel" → channel_list, "jadwaal" → jadwal, dst. Kata yang
# ejaannya sudah benar TIDAK lewat sini (biar jatuh ke cabang lama).
_FUZZY_SINGLE_WORDS = {
    'kalender': 'jadwal_bulan',
    'channel': 'channel_list',
    'jadwal': 'jadwal',
    'laporan': 'laporan',
}


def _fuzzy_word_match(word, target, cutoff=0.8):
    """Satu kata cocok dengan toleransi typo ringan? Sama persis, salah
    satu mengandung yang lain (min. 2 huruf — supaya 'a' tidak cocok
    ke mana-mana), atau kemiripan difflib ≥ cutoff."""
    import difflib
    w, t = (word or '').lower(), (target or '').lower()
    if not w or not t:
        return False
    if w == t:
        return True
    short = w if len(w) <= len(t) else t
    if len(short) >= 2 and (w in t or t in w):
        return True
    return difflib.SequenceMatcher(None, w, t).ratio() >= cutoff


def _fuzzy_phrase_in(clean, phrase, cutoff=0.8):
    """Apakah frasa (1+ kata) ada di teks dengan toleransi typo per kata?
    Kata-katanya harus berurutan (boleh ada 1 kata lain di antaranya).
    Kata tergabung ('jadwalbuln' ≈ 'jadwal bulan') ditangkap lewat
    perbandingan frasa-utuh vs satu token."""
    import difflib
    words = (clean or '').split()
    pwords = (phrase or '').split()
    if not pwords or not words:
        return False
    joined = ''.join(pwords)
    for w in words:
        # Arah "frasa di dalam token" (token lebih panjang) bebas; arah
        # "token di dalam frasa" hanya bila token hampir sepanjang frasa
        # (kata tergabung utuh) — supaya "channel" saja tidak ikut cocok
        # ke frasa "channel saya" (itu urusan cabang lama).
        if len(w) >= 4 and (joined in w
                            or (len(w) >= len(joined) - 2 and w in joined)
                            or difflib.SequenceMatcher(None, w, joined).ratio() >= 0.85):
            return True
    for start in range(len(words)):
        if not _fuzzy_word_match(words[start], pwords[0], cutoff):
            continue
        j = start
        ok = True
        for pw in pwords[1:]:
            if _fuzzy_word_match(words[j], pw, cutoff):
                continue
            found = False
            for k in range(j + 1, min(j + 3, len(words))):
                if _fuzzy_word_match(words[k], pw, cutoff):
                    j = k
                    found = True
                    break
            if not found:
                ok = False
                break
        if ok:
            return True
    return False


def _match_new_command(clean):
    """Cocokkan teks bebas ke perintah baru Bagian B (sinonim + typo).
    Kembalikan (intent, confidence, fuzzy_bool) atau None. Dicek SEBELUM
    intent baca-saja lama supaya 'laporan lengkap' tidak ditangkap
    /laporan biasa dan 'jadwal bulan' tidak ditangkap /jadwal.
    Exact = confidence 85 (langsung jalan, seperti intent baca-saja lain);
    fuzzy = 55 + penanda slots['fuzzy_match'] (bot bertanya
    'Maksud Bos ...?' dulu lewat alur konfirmasi NLU)."""
    for intent, (phrases, conf) in _CMD_SYNONYMS.items():
        for ph in phrases:
            if ph in (clean or ''):
                return (intent, conf, False)
    for intent, (phrases, _c) in _CMD_SYNONYMS.items():
        for ph in phrases:
            if _fuzzy_phrase_in(clean, ph):
                return (intent, 55, True)
    words = (clean or '').split()
    if len(words) == 1:
        for kw, intent in _FUZZY_SINGLE_WORDS.items():
            if words[0] != kw and _fuzzy_word_match(words[0], kw):
                return (intent, 55, True)
    return None


def _is_publish_now_order(clean):
    """True bila pesan memerintahkan video yang SUDAH dijadwalkan tayang
    langsung (bukan order baru). 'tidak usah dijadwalkan' = negasi jadwal,
    bukan perintah LIHAT jadwal — itu yang salah dibaca v1.3.5."""
    if 'tayang sekarang' in clean or 'langsung tayang' in clean:
        return True
    if 'publish' in clean and ('langsung' in clean or 'sekarang' in clean or 'saja' in clean):
        return True
    # 🔥 v1.3.33: negasi HARUS menempel ke 'jadwalkan' ("tidak usah
    # dijadwalkan") — 'jadwal' + 'jangan' yang longgar salah membaca
    # "jadwal besok jangan lupa dicek" sebagai perintah publish.
    if re.search(r'\b(tidak usah|tidak perlu|nggak usah|gak usah|jangan)\s+(di)?jadwalkan\b', clean):
        return True
    return False


def parse_natural_command(text, channels):
    """Terjemahkan pesan bebas → {intent, confidence 0-100, slots}.
    Intent: buat_short, buat_long, buat_playlist, buat_zumo, buat_zumobank, buat_zumorakit, status, laporan, kuota, stok, statistik,
    monetisasi, jadwal, gagal, channel_list, bantuan, jeda_channel,
    lanjutkan_channel, perpanjang_jadwal, ubah_mode, hapus,
    hentikan_antrean, tidak_dikenali, jadwal_bulan, laporan_detail,
    atur_jadwal (🔥 v1.2.20 — perintah baru Bagian B + sinonim/typo),
    publish_now (🔥 v1.3.6 — tayangkan langsung video yang sudah dijadwalkan),
    jadwalkan_playlist (🔥 v1.3.30 — jadwalkan 1 video playlist per
    channel mulai jam tertentu, mis. 'jadwalkan 1 video untuk nanti
    sore mulai jam 4 semua akun')."""
    clean = _nlu_clean(text)
    slots = {}
    conf = 0
    intent = 'tidak_dikenali'
    matched, all_flag = _nlu_match_channels(clean, channels)
    if all_flag:
        slots['channels'] = 'all'
    elif matched:
        slots['channels'] = [c.get('yt_id') for c in matched]
    if 'channel' in clean and not matched and not all_flag:
        slots['channel_unknown'] = True
    # Jumlah channel ("buatkan untuk 2 channel") — BUKAN angka tetap:
    # hanya bila channel tidak disebut namanya dan bukan 'semua'/'ke-3'.
    # 'ke-3 channel' / 'ketiga' sudah ditangkap all_flag di atas.
    if not all_flag and not matched:
        _mch = re.search(r'(\d+)\s*channel\b', clean)
        if _mch:
            slots['channel_count'] = max(1, min(10, int(_mch.group(1))))

    # Permintaan konten (judul/deskripsi/hashtag/ide) tetap ditangani cabang
    # lama — jangan sampai ketangkap intent buat_short.
    if (re.search(r'\b(judul|deskripsi|hashtag|tagar)\b', clean)
            or 'ide shorts' in clean or 'ideashorts' in clean) \
            and not re.search(r'\b(buat|bikin|buatkan)\s+short\b', clean):
        return {"intent": "tidak_dikenali", "confidence": 0, "slots": slots}

    create_verb = bool(re.search(r'\b(buat|bikin|buatkan|tolong buat|tolong bikin)\b', clean))

    # 🔥 v1.2.20: perintah baru Bagian B (sinonim + toleransi typo) —
    # dihitung sekali di sini, dipakai cabang elif di bawah (sebelum
    # intent baca-saja lama). fuzzy=True → slot penanda supaya
    # _handle_nlu_message menanyakan "Maksud Bos ...?" dulu.
    _new_cmd = _match_new_command(clean)

    # --- Destruktif: tidak pernah dieksekusi langsung (selalu konfirmasi). ---
    if re.search(r'\b(hapus|delete|buang)\b', clean):
        intent = 'hapus'
        conf = 55
        if 'channel' in clean:
            slots['target'] = 'channel'
        elif 'video' in clean:
            slots['target'] = 'video'
        elif 'antrean' in clean or 'antrian' in clean:
            slots['target'] = 'antrean'
        else:
            slots['target'] = 'tidak_jelas'
    elif ('hentikan antrean' in clean or 'stop antrean' in clean
            or 'batalkan antrean' in clean or 'bersihkan antrean' in clean
            or 'hentikan antrian' in clean):
        intent = 'hentikan_antrean'
        conf = 60
    # --- Jeda / lanjutkan channel. ---
    elif ('jeda' in clean or 'pause' in clean
            or ('hentikan' in clean and 'antrean' not in clean and 'antrian' not in clean
                and ('channel' in clean or matched))
            or ('matikan' in clean and ('channel' in clean or matched))):
        intent = 'jeda_channel'
        conf = 60
    elif ('lanjutkan' in clean or 'resume' in clean or 'jalan lagi' in clean
            or 'nyalakan lagi' in clean or 'batalkan jeda' in clean
            or 'aktifin lagi' in clean):
        intent = 'lanjutkan_channel'
        conf = 60
    # --- Perpanjang jadwal. ---
    elif 'perpanjang' in clean or 'tambah jadwal' in clean or 'tambah hari' in clean:
        intent = 'perpanjang_jadwal'
        conf = 60
        d = _nlu_extract_days(clean)
        if d:
            slots['days'] = d
    # --- Bank Audio Zumo (v1.3.0): 'buatkan lagu untuk bank' / 'isi
    # bank' (generate massal, tanpa upload) dan 'rakit video dari
    # bank' (video dari lagu bank). Dicek SEBELUM buat_zumo /
    # buat_playlist — HANYA bila kata 'bank' / 'rakit' disebut
    # eksplisit, jadi 'buatkan playlist' biasa dan 'buatkan lagu
    # zumo' tidak direbut. 'cek isi bank' (baca saja) bukan perintah
    # buat — tidak ketangkap cabang ini. ---
    elif 'rakit' in clean and ('bank' in clean or 'video' in clean
                               or 'playlist' in clean):
        intent = 'buat_zumorakit'
        conf = 75
    elif ('bank' in clean and (create_verb or 'isi' in clean)
            and ('lagu' in clean or 'isi' in clean or 'zumo' in clean
                 or 'audio' in clean)
            and not re.search(r'\b(cek|lihat|tampilkan|status|berapa)\b', clean)):
        intent = 'buat_zumobank'
        conf = 75
    # --- Buat lagu Zumo (Kie.ai) — dicek SEBELUM buat_short/buat_long/
    # buat_playlist supaya 'buatkan playlist zumo' tidak ketangkap
    # buat_playlist dan 'buatkan lagu zumo untuk Cozy Sax' tidak ketangkap
    # buat_short (cabang 'buatkan untuk <channel>'). Frasa zumo eksplisit
    # confidence 80; 'buatkan lagu' (tanpa 'playlist') 60; 'lagu baru'
    # tanpa kata kerja 40 — selalu DI BAWAH frasa zumo eksplisit dan
    # tidak pernah merebut 'buatkan playlist ...' (ada kata 'playlist'
    # → jatuh ke buat_playlist di bawah).
    elif 'zumo' in clean or (create_verb and 'kie' in clean and 'lagu' in clean):
        intent = 'buat_zumo'
        conf = 80
    elif create_verb and 'lagu' in clean and 'playlist' not in clean \
            and ('sax' in clean or 'saxophone' in clean) \
            and 'short' not in clean and 'video pendek' not in clean \
            and 'video long' not in clean and 'video panjang' not in clean \
            and not re.search(r'\blong\b', clean):
        intent = 'buat_zumo'
        conf = 80
    elif create_verb and 'lagu' in clean and 'playlist' not in clean \
            and 'short' not in clean and 'video pendek' not in clean \
            and 'video long' not in clean and 'video panjang' not in clean \
            and not re.search(r'\blong\b', clean):
        intent = 'buat_zumo'
        conf = 60
    elif 'lagu baru' in clean and 'playlist' not in clean \
            and 'short' not in clean and 'video pendek' not in clean \
            and 'video long' not in clean and 'video panjang' not in clean \
            and not re.search(r'\blong\b', clean):
        intent = 'buat_zumo'
        conf = 40
    # --- Buat Short / Long. ---
    elif create_verb and ('short' in clean or 'video pendek' in clean):
        intent = 'buat_short'
        conf = 50
    elif create_verb and ('video long' in clean or 'video panjang' in clean
                          or 'kompilasi' in clean or re.search(r'\blong\b', clean)):
        intent = 'buat_long'
        conf = 50
    elif create_verb and 'playlist' in clean:
        intent = 'buat_playlist'
        conf = 50
    elif create_verb and 'video' in clean:
        # "buatkan video" tanpa penanda short/long → tanyakan dulu.
        intent = 'buat_short'
        conf = 40
        slots['kind_ambiguous'] = True
    elif create_verb and (matched or all_flag or slots.get('channel_count')):
        # "buatkan untuk 2 channel" / "buatkan untuk semua channel" —
        # objek tidak disebut, default Short (konfirmasi akan menyatakan
        # tafsiran ini, Bos bisa menolak bila maksudnya video long).
        intent = 'buat_short'
        conf = 45
    elif ('short' in clean and (matched or all_flag or 'tema' in clean
                                or 'semua' in clean)):
        # Tanpa kata kerja tapi jelas ("short tema anak untuk semua channel").
        intent = 'buat_short'
        conf = 40
    elif ('playlist' in clean and (matched or all_flag or 'tema' in clean
                                   or 'semua' in clean)):
        # Tanpa kata kerja tapi jelas ("playlist lofi untuk Aruya").
        intent = 'buat_playlist'
        conf = 40
    # --- Ubah mode channel. ---
    elif ('mode' in clean or 'autopilot' in clean or 'approval' in clean
            or 'persetujuan' in clean
            or 'jadikan auto' in clean or 'jadi auto' in clean
            or 'aktifkan otomatis' in clean or 'matikan otomatis' in clean):
        intent = 'ubah_mode'
        conf = 60
    # --- Tambah channel (hanya bisa dari panel/browser — dijawab, bukan
    # dieksekusi dari Telegram). ---
    elif ('tambah channel' in clean or 'channel baru' in clean
            or (create_verb and 'channel' in clean and not matched and not all_flag)):
        intent = 'tambah_channel'
        conf = 65
    # --- 🔥 v1.2.20: perintah baru Bagian B (sinonim + toleransi typo). ---
    # Dicek SEBELUM intent baca-saja lama supaya 'laporan lengkap' tidak
    # ditangkap /laporan biasa, 'jadwal bulan' tidak ditangkap /jadwal,
    # dan 'channel saya' tidak ditangkap cabang channel lama.
    # --- 🔥 v1.3.30: jadwalkan playlist — 'jadwalkan 1 video ... jam 4
    # ... semua akun'. Dicek SEBELUM _new_cmd (Bagian B fuzzy
    # 'channel_list' mencuri kalimat yang mengandung kata 'channel')
    # dan SEBELUM publish_now/'jadwal' supaya tidak dibaca sebagai
    # laporan jadwal (dulu: intent 'jadwal', conf 70). Butuh kata
    # 'video' supaya 'jadwalkan laporan' tidak ketangkap; publish_now
    # tetap menang bila frasanya memang perintah tayang langsung.
    elif (re.search(r'\b(jadwalkan|jadwalin)\b', clean) and 'video' in clean
            and (re.search(r'\blong\b', clean) or 'video panjang' in clean)
            and not _is_publish_now_order(clean)):
        intent = 'buat_long'
        conf = 75
    elif (re.search(r'\b(jadwalkan|jadwalin)\b', clean) and 'video' in clean
            and not _is_publish_now_order(clean)):
        intent = 'jadwalkan_playlist'
        conf = 75
    elif _new_cmd:
        intent, conf = _new_cmd[0], _new_cmd[1]
        if _new_cmd[2]:
            slots['fuzzy_match'] = True
    # --- 🔥 v1.3.6: publish langsung — video yang SUDAH dijadwalkan tayang
    # sekarang (bukan order baru). Dicek SEBELUM 'jadwal' supaya
    # "tidak usah dijadwalkan" tidak dibaca sebagai perintah LIHAT jadwal.
    elif _is_publish_now_order(clean):
        intent = 'publish_now'
        conf = 75
    # --- Baca saja. ---
    elif 'status' in clean:
        intent = 'status'
        conf = 70
    elif 'laporan' in clean or 'report' in clean:
        intent = 'laporan'
        conf = 70
    elif 'kuota' in clean:
        intent = 'kuota'
        conf = 70
    elif 'stok' in clean:
        intent = 'stok'
        conf = 70
    elif 'statistik' in clean or 'stats' in clean:
        intent = 'statistik'
        conf = 70
    elif 'monetisasi' in clean or 'penghasilan' in clean or 'uang' in clean or 'rpm' in clean:
        intent = 'monetisasi'
        conf = 70
    elif 'jadwal' in clean or 'schedule' in clean:
        intent = 'jadwal'
        conf = 70
    elif 'gagal' in clean:
        intent = 'gagal'
        conf = 70
    elif 'channel' in clean:
        intent = 'channel_list'
        conf = 60
    elif 'bantuan' in clean or 'help' in clean or 'perintah apa' in clean:
        intent = 'bantuan'
        conf = 70

    if intent == 'tidak_dikenali':
        return {"intent": intent, "confidence": 0, "slots": slots}

    # --- Slot tambahan sesuai intent. ---
    niche, unknown_niche = _nlu_detect_niche(clean)
    if intent in ('buat_short', 'buat_long'):
        if niche:
            slots['niche'] = niche
            conf += 10
        elif unknown_niche:
            slots['unknown_niche'] = unknown_niche
        cnt = _nlu_extract_count(clean, intent)
        if cnt:
            slots['count'] = cnt
            conf += 10
        dur = _nlu_extract_duration(clean)
        if dur:
            slots['duration_sec'] = dur
            conf += 5
        cta = _nlu_extract_cta(text)
        if cta:
            slots['cta'] = cta
            conf += 5
        # 🔥 v1.3.33: pakai teks mentah (lowercase) — _nlu_clean
        # membuang ':' sehingga 'jam 9:30' jadi 'jam 930' dan menit
        # tidak pernah terbaca.
        tm = _nlu_extract_time(text.lower())
        if tm:
            slots['publish_time'] = tm
            conf += 5
        # Waktu lengkap alami ("hari ini jam 9", "besok jam 7 malam",
        # "20 November jam 19") → slot publish_date + catatan tafsiran.
        # Dipakai sebagai jadwal pertama (menang atas jam saja).
        ndt, nnote = _parse_natural_datetime(text)
        if ndt:
            slots['publish_date'] = ndt
            if nnote:
                slots['publish_note'] = nnote
            conf += 5
        # Jangkar waktu harian (buat_short saja): jam eksplisit menang,
        # kalau tidak ada → kata hari (pagi 07:00, sore 17:00, dst.).
        # Jadwal 6 jam dihitung di _nlu_advance sebelum konfirmasi.
        if intent == 'buat_short':
            anchor, dayparts = _nlu_extract_dayparts(clean)
            if anchor:
                slots['anchor_time'] = anchor
                conf += 5
            if dayparts:
                slots['dayparts'] = dayparts
        if slots.get('channels'):
            conf += 15
        if create_verb:
            conf += 5
    elif intent == 'buat_playlist':
        # v1.3.5 jiplak: link YouTube + kata jiplak/salin/copy/tiru
        # ATAU kata playlist/lagu -> link = sumbernya, bukan tema.
        _link = _extract_youtube_link(text)
        if _link and re.search(r'\b(jiplak|salin|copy|tiru|playlist|lagu)\b', clean):
            slots['playlist_source_url'] = _link
            _vid, _plid = _extract_youtube_ids_from_url(_link)
            slots['playlist_source_kind'] = 'playlist' if _plid else 'video'
            conf += 10
            clean = re.sub(r'https?://\S+', ' ', clean)
            text = re.sub(r'https?://\S+', ' ', text)
        # Tema musik + tema footage Pixabay + durasi (bila disebut).
        # Yang belum disebut TIDAK ditebak — ditanyakan lewat alur
        # tanya-jawab.
        # Kata 'auto' / 'otomatis' → mode auto (pilih kandidat terbaik
        # setelah riset); hapus dari teks dulu supaya tidak bocor ke tema.
        if re.search(r'\b(auto|otomatis)\b', clean):
            slots['auto_pick'] = True
            conf += 5
            clean = re.sub(r'\b(auto|otomatis)\b', ' ', clean)
        # Kata 'publish' → langsung tayang setelah render (tanpa jadwal);
        # hapus dari teks dulu supaya tidak bocor ke tema.
        # 🔥 v1.3.33: tanggal masa depan MENANG atas 'publish' —
        # "publish besok jam 7 malam" dijadwalkan besok 19:00, bukan
        # langsung tayang; 'publish' saja (tanpa tanggal) baru langsung.
        _has_publish = bool(re.search(r'\bpublish\b', clean))
        clean = re.sub(r'\bpublish\b', ' ', clean)
        # Waktu lengkap alami ("hari ini jam 9", "besok jam 7 malam") →
        # slot publish_date + catatan tafsiran.
        ndt, nnote = _parse_natural_datetime(text)
        if ndt:
            slots['publish_date'] = ndt
            if nnote:
                slots['publish_note'] = nnote
            conf += 5
        elif _has_publish:
            slots['publish_now'] = True
            conf += 5
        dur = _nlu_extract_playlist_duration(text)
        if dur and dur[0] is not None:
            slots['duration_min_sec'], slots['duration_max_sec'], slots['duration_note'] = dur
            conf += 5
        px = _nlu_extract_pixabay_theme(clean)
        if px:
            slots['pixabay_theme'] = px
            conf += 5
        th = _nlu_extract_playlist_theme(clean, channels)
        if th:
            slots['playlist_theme'] = th
            conf += 10
        if slots.get('channels'):
            conf += 15
        if create_verb:
            conf += 5
    elif intent == 'jadwalkan_playlist':
        # 🔥 v1.3.30: sama seperti buat_playlist (tema + footage +
        # durasi + jam mulai) + jeda beruntun 15 menit per channel.
        # Yang belum disebut TIDAK ditebak — ditanyakan lewat _nlu_advance.
        px = _nlu_extract_pixabay_theme(clean)
        if px:
            slots['pixabay_theme'] = px
            conf += 5
        ndt, nnote = _parse_natural_datetime(text)
        if ndt:
            slots['publish_date'] = ndt
            if nnote:
                slots['publish_note'] = nnote
            conf += 10
        dur = _nlu_extract_playlist_duration(text)
        if dur and dur[0] is not None:
            slots['duration_min_sec'], slots['duration_max_sec'], slots['duration_note'] = dur
            conf += 5
        th = _nlu_extract_playlist_theme(clean, channels)
        if th:
            slots['playlist_theme'] = th
            conf += 10
        slots['schedule_stagger_min'] = 15
        if slots.get('channels'):
            conf += 15
    elif intent == 'buat_zumo':
        # Tema lagu + jumlah judul + jadwal (bila disebut). Channel ikut
        # slot 'channels' (diisi di atas bila disebut namanya — bila tidak,
        # alur /zumo menanyakan channel, default Cozy Sax). Bahasa lirik &
        # vokal dari kata kunci ('indonesia'/'inggris', 'pria'/'wanita');
        # tidak disebut = ikut profil channel di server. Yang belum
        # disebut TIDAK ditebak — ditanyakan lewat alur tanya-jawab /zumo.
        th = _nlu_extract_zumo_theme(clean, channels)
        if th:
            slots['zumo_theme'] = th
            conf += 10
        mcount = re.search(r'\b([23])\s*(judul|lagu)\b', clean)
        if mcount:
            slots['song_count'] = int(mcount.group(1))
            conf += 5
        lang, vocal = _zumo_parse_style_hints(clean)
        if lang:
            slots['lyrics_language'] = lang
            conf += 5
        if vocal:
            slots['vocal_gender'] = vocal
            conf += 5
        if 'instrumental' in clean or ('piano' in clean and 'vokal' not in clean):
            slots['instrumental'] = True
            conf += 5
        elif 'vokal' in clean:
            slots['instrumental'] = False
        ndt, nnote = _parse_natural_datetime(text)
        if ndt:
            slots['publish_date'] = ndt
            if nnote:
                slots['publish_note'] = nnote
            conf += 5
    elif intent in ('buat_zumobank', 'buat_zumorakit'):
        # Tema + jumlah lagu bank + mode vokal/instrumental + jadwal
        # (rakit). Kata 'bank'/'rakit'/'isi' dibuang dari tema.
        th = _nlu_extract_zumo_theme(clean, channels)
        if th:
            th = re.sub(r'\b(bank|rakit|audio|isi)\b', ' ', th)
            th = re.sub(r'^\d+\s*', '', th)
            th = re.sub(r'\s+', ' ', th).strip()[:100] or None
        if th:
            slots['zumo_theme'] = th
            conf += 10
        mbank = re.search(r'\b(\d{1,2})\s*(lagu|judul)\b', clean)
        if mbank:
            slots['bank_count'] = max(1, min(30, int(mbank.group(1))))
            conf += 5
        if 'instrumental' in clean or ('piano' in clean and 'vokal' not in clean):
            slots['instrumental'] = True
            conf += 5
        elif 'vokal' in clean:
            slots['instrumental'] = False
        lang, vocal = _zumo_parse_style_hints(clean)
        if lang:
            slots['lyrics_language'] = lang
        if vocal:
            slots['vocal_gender'] = vocal
        ndt, nnote = _parse_natural_datetime(text)
        if ndt:
            slots['publish_date'] = ndt
            if nnote:
                slots['publish_note'] = nnote
            conf += 5
    elif intent == 'perpanjang_jadwal':
        if slots.get('channels'):
            conf += 15
    elif intent in ('jeda_channel', 'lanjutkan_channel', 'ubah_mode', 'hapus'):
        if slots.get('channels'):
            conf += 15
        if intent == 'ubah_mode':
            mode = _nlu_extract_mode(clean)
            if mode:
                slots['mode'] = mode
                conf += 10
    elif intent in ('status', 'laporan', 'kuota', 'stok', 'statistik',
                    'monetisasi', 'jadwal', 'gagal', 'channel_list', 'bantuan',
                    'jadwal_bulan', 'laporan_detail'):
        pass  # baca saja: skor dasar sudah cukup
    return {"intent": intent, "confidence": min(95, conf), "slots": slots}


# --- Klarifikasi tertunda (NLU): disimpan di monitor_state.json, TTL 10 mnt. ---
def _load_nlu_pending(chat_id):
    try:
        state = load_monitor_state()
        p = (state.get('nlu_pending') or {}).get(str(chat_id))
        if not p:
            return None
        if time.time() - float(p.get('created_at', 0)) > _NLU_PENDING_TTL:
            _clear_nlu_pending(chat_id)
            return None
        return p
    except Exception:
        return None


def _save_nlu_pending(chat_id, pending):
    try:
        state = load_monitor_state()
        state.setdefault('nlu_pending', {})[str(chat_id)] = pending
        save_monitor_state(state)
    except Exception as e:
        print(f"Gagal simpan nlu_pending: {e}", flush=True)


def _clear_nlu_pending(chat_id):
    try:
        state = load_monitor_state()
        (state.get('nlu_pending') or {}).pop(str(chat_id), None)
        save_monitor_state(state)
    except Exception:
        pass


def _nlu_channel_names(yt_ids):
    names = []
    for c in load_channels():
        if c.get('yt_id') in (yt_ids or []):
            names.append(_channel_display_name(c))
    return names


def _nlu_describe(pending):
    """Kalimat penafsiran: 'Maksud Bos: buatkan 1 Short tema Anak-Anak,
    CTA Subscribe, durasi 30 detik, untuk Aruya 0089, Barang Unick,
    Cozy Sax Relaxing.'"""
    slots = pending.get('slots') or {}
    intent = pending.get('intent')
    # 🔥 v1.3.2: 'all' (dari "semua channel" / tombol Semua channel)
    # diuraikan ke daftar nama supaya konfirmasi menyebut semua channel,
    # bukan sekadar "semua channel".
    _ch_ids = slots.get('channels')
    if _ch_ids == 'all':
        _ch_ids = [c.get('yt_id') for c in load_channels()]
    names = _nlu_channel_names(_ch_ids or [])
    if names:
        ch = ', '.join(names)
        if slots.get('channel_count') and len(names) < slots['channel_count']:
            ch += f" ({len(names)} dari {slots['channel_count']})"
    elif slots.get('channel_count'):
        ch = f"{slots['channel_count']} channel (pilih dulu)"
    else:
        ch = 'semua channel'
    if intent == 'buat_short':
        niche = _NICHES.get(slots.get('niche') or 'sleep', 'Deep Sleep Music')
        base = (f"buatkan {slots.get('count') or 1} Short tema {niche}, "
                f"CTA {slots.get('cta') or 'Subscribe'}, "
                f"durasi {slots.get('duration_sec') or 30} detik, untuk {ch}")
        # Jadwal hasil hitungan (aturan jeda 6 jam) WAJIB disebut supaya
        # Bos bisa mengoreksi sebelum dieksekusi — plus catatan tafsiran
        # waktu alami bila ada (mis. 'hari ini jam 9').
        sched = slots.get('schedule') or {}
        note_suffix = f" ({slots['publish_note']})" if slots.get('publish_note') else ''
        if sched:
            if len(sched) == 1:
                return base + f" — jadwal: {_nlu_format_schedule(next(iter(sched.values())))}" + note_suffix
            lines = []
            for yt in (slots.get('channels') or []):
                if yt in sched:
                    nm = _nlu_channel_names([yt])
                    lines.append(f"• {nm[0] if nm else yt}: {_nlu_format_schedule(sched[yt])}")
            if lines:
                return base + " — jadwal:\n" + "\n".join(lines) + note_suffix
            return base
        if slots.get('publish_time'):
            return base + f", jam tayang {slots['publish_time']}"
        return base
    if intent == 'buat_long':
        niche = _NICHES.get(slots.get('niche') or 'sleep', 'Deep Sleep Music')
        base = (f"buatkan {slots.get('count') or 1} video long tema {niche} untuk {ch}")
        if slots.get('publish_date'):
            base += f" — mulai {slots['publish_date']}"
            if slots.get('publish_note'):
                base += f" ({slots['publish_note']})"
        return base
    if intent == 'buat_playlist':
        th = slots.get('playlist_theme') or 'musik (tanya dulu)'
        px = slots.get('pixabay_theme') or 'auto (dari tema musik)'
        if slots.get('playlist_source_url'):
            base = f"buatkan playlist dari link YouTube ({slots.get('playlist_source_kind') or 'video'}) untuk {ch} (footage: {px})"
        else:
            base = f"buatkan playlist tema {th} untuk {ch} (footage: {px})"
        if slots.get('duration_note'):
            base += f", durasi {slots['duration_note']}"
        if slots.get('auto_pick'):
            base += ", pilih otomatis (views tertinggi)"
        if slots.get('publish_now'):
            base += ", langsung tayang (publish, tanpa jadwal)"
        elif slots.get('publish_date'):
            base += f", jadwal {slots['publish_date']}"
            if slots.get('publish_note'):
                base += f" ({slots['publish_note']})"
        return base
    if intent == 'jadwalkan_playlist':
        th = slots.get('playlist_theme') or 'musik (tanya dulu)'
        px = slots.get('pixabay_theme') or 'auto (dari tema musik)'
        base = f"jadwalkan 1 video playlist tema {th} per channel untuk {ch} (footage: {px})"
        if slots.get('duration_note'):
            base += f", durasi {slots['duration_note']}"
        if slots.get('publish_date'):
            base += f", mulai {slots['publish_date']}"
            if slots.get('publish_note'):
                base += f" ({slots['publish_note']})"
            _chs = slots.get('channels') or []
            if len(_chs) > 1:
                lines = []
                for _i, _yt in enumerate(_chs):
                    _nm = _nlu_channel_names([_yt])
                    lines.append(f"• {_nm[0] if _nm else _yt}: {_stagger_publish_date(slots['publish_date'], _i, slots.get('schedule_stagger_min') or 15)}")
                base += " — jadwal:\n" + "\n".join(lines)
        return base
    if intent == 'buat_zumobank':
        th = slots.get('zumo_theme') or 'lagu (tanya dulu)'
        ch_zumo = ', '.join(names) if names else 'Cozy Sax Relaxing (default, bisa diubah)'
        base = f"buatkan {slots.get('bank_count') or 10} lagu tema {th} untuk bank audio {ch_zumo}"
        if slots.get('instrumental'):
            base += " (instrumental, tanpa lirik)"
        return base
    if intent == 'buat_zumorakit':
        ch_zumo = ', '.join(names) if names else 'Cozy Sax Relaxing (default, bisa diubah)'
        base = f"rakit video dari bank audio ({slots.get('bank_count') or 8} lagu acak) untuk {ch_zumo}"
        if slots.get('publish_date'):
            base += f", jadwal {slots['publish_date']}"
            if slots.get('publish_note'):
                base += f" ({slots['publish_note']})"
        return base
    if intent == 'buat_zumo':
        th = slots.get('zumo_theme') or 'lagu (tanya dulu)'
        ch_zumo = ', '.join(names) if names else 'Cozy Sax Relaxing (default, bisa diubah)'
        base = f"buatkan playlist Zumo tema {th} untuk {ch_zumo}"
        if slots.get('song_count'):
            base += f", {slots['song_count']} judul"
        lang_label = {'id': 'bahasa Indonesia', 'en': 'bahasa Inggris'}.get(slots.get('lyrics_language') or '')
        vocal_label = {'m': 'vokal pria', 'f': 'vokal wanita'}.get(slots.get('vocal_gender') or '')
        if lang_label:
            base += f", {lang_label}"
        if vocal_label:
            base += f", {vocal_label}"
        if slots.get('publish_date'):
            base += f", jadwal {slots['publish_date']}"
            if slots.get('publish_note'):
                base += f" ({slots['publish_note']})"
        return base
    if intent == 'perpanjang_jadwal':
        return f"perpanjang jadwal {slots.get('days') or 30} hari untuk {ch}"
    if intent == 'ubah_mode':
        mode = 'Full-Auto' if slots.get('mode') == 'auto' else 'Approval'
        return f"ubah mode {ch} jadi {mode}"
    if intent == 'jeda_channel':
        return f"jeda channel {ch}"
    if intent == 'lanjutkan_channel':
        return f"lanjutkan channel {ch}"
    if intent == 'hapus':
        return f"hapus {slots.get('target') or 'sesuatu'} {ch}"
    if intent == 'hentikan_antrean':
        return "hentikan antrean"
    if intent == 'jadwal_bulan':
        return f"tampilkan jadwal 30 hari untuk {ch}"
    if intent == 'laporan_detail':
        return "tampilkan laporan detail (views, video terbaik, kuota, gagal hari ini, monetisasi)"
    if intent == 'atur_jadwal':
        return f"atur jadwal {ch}"
    if intent == 'channel_list':
        return "tampilkan daftar channel"
    if intent == 'jadwal':
        return "tampilkan jadwal 7 hari"
    if intent == 'publish_now':
        return "publish langsung video yang masih dijadwalkan" + (f" untuk {ch}" if ch else "")
    if intent == 'laporan':
        return "tampilkan laporan harian"
    return intent or 'sesuatu'


def _nlu_check_channels_login(channels):
    """Cek login per channel via channels().list (1 unit kuota/channel,
    refresh token bila perlu — _fetch_channel_stats_api yang sama seperti
    laporan). Kembalikan {yt_id: True/False}: True = token masih jalan
    (login hidup), False = token mati / tidak bisa dipakai. 🔥 v1.3.2."""
    out = {}
    for ch in (channels or []):
        yt = ch.get('yt_id')
        if not yt:
            continue
        try:
            out[yt] = _fetch_channel_stats_api(ch) is not None
        except Exception:
            out[yt] = False
    return out


def _nlu_ask_channel(token, chat_id, pending, multi=True):
    """Tanya channel mana lewat tombol inline. Bila diminta N channel,
    tombol ditekan satu per satu sampai N terkumpul (progres ditampilkan)."""
    chans = load_channels()
    rows = []
    for i, c in enumerate(chans):
        rows.append([{"text": _channel_display_name(c),
                      "callback_data": f"nlu:ch:{i}"}])
    if multi:
        rows.append([{"text": "🌐 Semua channel", "callback_data": "nlu:ch:all"}])
    slots = pending.get('slots') or {}
    picked = slots.get('channels') or []
    need = slots.get('channel_count') or 0
    if need and picked:
        head = (f"✅ Sudah dipilih: {', '.join(_nlu_channel_names(picked))} "
                f"({len(picked)} dari {need}) — pilih {need - len(picked)} lagi ya Bos 🤔\n")
    else:
        head = "Siap Bos, channel yang mana? 🤔\n"
    send_message(token, chat_id,
                 head + f"({_nlu_describe(pending)})",
                 reply_markup={"inline_keyboard": rows})


def _nlu_ask_niche(token, chat_id, pending):
    unknown = (pending.get('slots') or {}).get('unknown_niche')
    rows = []
    keys = list(_NICHES.keys())
    for i in range(0, len(keys), 2):
        row = [{"text": _NICHES[k], "callback_data": f"nlu:niche:{k}"} for k in keys[i:i + 2]]
        rows.append(row)
    head = (f"Tema '{unknown}' belum saya kenal Bos 😅 — yang saya tahu "
            if unknown else "Tema yang mana Bos? 🤔\n")
    send_message(token, chat_id,
                 head + "Pilih salah satu ya:",
                 reply_markup={"inline_keyboard": rows})


def _nlu_ask_mode(token, chat_id, pending):
    names = _nlu_channel_names((pending.get('slots') or {}).get('channels') or [])
    who = ', '.join(names) if names else 'channel itu'
    send_message(token, chat_id,
                 f"Mode apa untuk {who} Bos? 🤔",
                 reply_markup={"inline_keyboard": [[
                     {"text": "🤖 Full-Auto (langsung tayang)", "callback_data": "nlu:mode:auto"},
                     {"text": "👀 Approval (minta izin dulu)", "callback_data": "nlu:mode:approval"},
                 ]]})


def _nlu_ask_confirm(token, chat_id, pending):
    """Konfirmasi penafsiran sebelum bertindak (wajib bila ragu / bila
    aksinya berbahaya atau membuat konten)."""
    pending['awaiting'] = 'confirm'
    _save_nlu_pending(chat_id, pending)
    extra = ''
    if pending.get('intent') == 'ubah_mode' and (pending.get('slots') or {}).get('mode') == 'auto':
        extra = ("\n⚠️ Beralih ke Full-Auto berarti video langsung tayang "
                 "tanpa persetujuan Bos. Lanjut?")
    elif pending.get('intent') == 'hapus':
        extra = ("\n⚠️ Ini perintah destruktif — saya TIDAK akan menghapus "
                 "apa-apa sendiri; setelah Bos setuju saya kasih langkah "
                 "hapusnya di panel.")
    elif pending.get('intent') == 'hentikan_antrean':
        extra = "\n⚠️ Antrean yang dihentikan tidak bisa dibatalkan."
    send_message(token, chat_id,
                 f"Maksud Bos: {_nlu_describe(pending)}?{extra}\n\n"
                 "Kalau mau diubah, ketik saja yang mau diubah ya Bos "
                 "(mis. `jam 5 sore` / `tema lofi` / `cuma Aruya`) — tidak perlu batal.",
                 reply_markup={"inline_keyboard": [[
                     {"text": "✅ Ya, benar", "callback_data": "nlu:yes"},
                     {"text": "❌ Bukan, batal", "callback_data": "nlu:no"},
                 ]]})


def _nlu_advance(token, chat_id, pending):
    """Majukan klarifikasi: tanya slot yang masih hilang, konfirmasi bila
    perlu, atau eksekusi bila semua sudah lengkap."""
    slots = pending.setdefault('slots', {})
    intent = pending.get('intent')
    chans = load_channels()
    # --- Channel: isi dari 'all' / cek apakah sudah ada. ---
    if intent in ('buat_short', 'buat_long', 'buat_playlist', 'jadwalkan_playlist', 'perpanjang_jadwal', 'ubah_mode',
                  'jeda_channel', 'lanjutkan_channel', 'hapus', 'hentikan_antrean',
                  'atur_jadwal'):
        if slots.get('channels') == 'all':
            slots['channels'] = [c.get('yt_id') for c in chans]
        if not slots.get('channels') and slots.get('channel_unknown') \
                and not slots.get('channel_count'):
            # 🔥 v1.3.2: 'channel_unknown' JANGAN menahan bila channel
            # disebut lewat JUMLAH ("ke 4 channel" → channel_count) —
            # nama memang tidak disebut, tapi maksudnya jelas.
            send_message(token, chat_id,
                         "Saya nggak nemu channel yang Bos sebut 😅 — channel yang "
                         "terdaftar: " + ", ".join(_channel_display_name(c) for c in chans)
                         + ". Coba sebut namanya lagi ya Bos.")
            _clear_nlu_pending(chat_id)
            return
        if intent == 'buat_long':
            # Video long hanya 1 channel sekali proses (sesuai /buatlong):
            # bila disebut lebih dari 1 channel, minta pilih satu saja.
            slots.pop('channel_count', None)
            if len(slots.get('channels') or []) > 1:
                slots['channels'] = []
            if not slots.get('channels'):
                pending['awaiting'] = 'channel'
                _save_nlu_pending(chat_id, pending)
                _nlu_ask_channel(token, chat_id, pending, multi=False)
                return
        elif intent in ('buat_playlist', 'jadwalkan_playlist'):
            # 🔥 v1.3.2: playlist BOLEH multi-channel (riset + auto-pick
            # per channel di eksekusi). Angka yang sama dengan jumlah
            # channel yang ada ("ke 4 channel" / "4 channel") = semua.
            if slots.get('channel_count') and slots['channel_count'] >= len(chans):
                slots['channels'] = [c.get('yt_id') for c in chans]
                slots.pop('channel_count', None)
            if not slots.get('channels'):
                pending['awaiting'] = 'channel'
                _save_nlu_pending(chat_id, pending)
                _nlu_ask_channel(token, chat_id, pending, multi=True)
                return
            need = slots.get('channel_count') or 0
            if need and len(slots.get('channels') or []) < need:
                pending['awaiting'] = 'channel'
                _save_nlu_pending(chat_id, pending)
                _nlu_ask_channel(token, chat_id, pending, multi=True)
                return
            slots.pop('channel_count', None)
            # Cek login semua channel SEBELUM konfirmasi (hanya bila
            # multi-channel; 1 channel tetap ikut alur terpandu lama).
            if len(slots.get('channels') or []) > 1 and not slots.get('login_checked'):
                slots['login_checked'] = True
                _save_nlu_pending(chat_id, pending)
                _target = [c for c in chans
                           if c.get('yt_id') in (slots.get('channels') or [])]
                _res = _nlu_check_channels_login(_target)
                _live = [c for c in _target if _res.get(c.get('yt_id'))]
                _dead = [c for c in _target if not _res.get(c.get('yt_id'))]
                if not _live:
                    send_message(token, chat_id,
                                 "Semua channel loginnya mati Bos — tidak ada playlist yang dibuat.\n"
                                 "Tautkan ulang dari panel (Settings → Tambah Channel, device login), lalu coba lagi ya.")
                    _clear_nlu_pending(chat_id)
                    return
                if _dead:
                    slots['login_dead'] = [_channel_display_name(c) for c in _dead]
                    slots['channels'] = [c.get('yt_id') for c in _live]
                    _save_nlu_pending(chat_id, pending)
                    send_message(token, chat_id,
                                 "Cek login: "
                                 + ", ".join(_channel_display_name(c) for c in _live)
                                 + " masih terlogin. "
                                 + ", ".join(_channel_display_name(c) for c in _dead)
                                 + " loginnya mati — saya keluarkan dulu dari daftar ya. "
                                 "Tautkan ulang dari panel (Settings → Tambah Channel, device login) supaya bisa dipakai lagi.")
                else:
                    send_message(token, chat_id,
                                 "Cek login: "
                                 + ", ".join(_channel_display_name(c) for c in _live)
                                 + " — masih terlogin semua.")
        else:
            need = slots.get('channel_count') or 1
            have = len(slots.get('channels') or [])
            if have < need:
                pending['awaiting'] = 'channel'
                _save_nlu_pending(chat_id, pending)
                _nlu_ask_channel(token, chat_id, pending,
                                 multi=(intent in ('buat_short', 'buat_long', 'perpanjang_jadwal',
                                                  'jeda_channel', 'lanjutkan_channel', 'ubah_mode')
                                        and not slots.get('channel_count')))
                return
    # --- 🔥 v1.2.20: perintah baru Bagian B. ---
    # atur_jadwal butuh 1 channel (sudah ditanyakan di blok channel di
    # atas bila belum ada). Bila cocoknya lewat typo (fuzzy) → konfirmasi
    # "Maksud Bos ...?" dulu; bila sudah dikonfirmasi / cocok persis →
    # langsung jalankan (baca saja / mulai alur atur jadwal).
    if intent == 'atur_jadwal':
        # Atur jadwal itu per-channel: bila disebut lebih dari 1 channel
        # ("untuk semua channel"), minta pilih satu saja dulu.
        if len(slots.get('channels') or []) > 1:
            slots['channels'] = []
            pending['awaiting'] = 'channel'
            _save_nlu_pending(chat_id, pending)
            _nlu_ask_channel(token, chat_id, pending, multi=False)
            return
        if slots.get('fuzzy_match') and not pending.get('confirmed'):
            _nlu_ask_confirm(token, chat_id, pending)
            return
        _clear_nlu_pending(chat_id)
        _aturjadwal_start(token, chat_id, (slots.get('channels') or [None])[0])
        return
    if intent in ('jadwal_bulan', 'laporan_detail', 'channel_list', 'jadwal', 'laporan'):
        if slots.get('fuzzy_match') and not pending.get('confirmed'):
            _nlu_ask_confirm(token, chat_id, pending)
            return
        _clear_nlu_pending(chat_id)
        _nlu_run_readonly(token, chat_id, intent, slots)
        return
    # --- Tema musik (buat playlist): bila belum disebut, tanyakan dulu
    # (tidak ditebak) — jawaban bebas ditangkap cabang 'theme' di
    # _nlu_try_complete_pending. ---
    if intent in ('buat_playlist', 'jadwalkan_playlist') and not slots.get('playlist_theme') and not slots.get('playlist_source_url'):
        pending['awaiting'] = 'theme'
        _save_nlu_pending(chat_id, pending)
        send_message(token, chat_id,
                     "Tema musik playlist-nya apa Bos? 🤔\n(mis. `lagu malaysia lawas`, `lofi`, `piano santai`)")
        return
    # --- Jam mulai (jadwalkan playlist): bila belum disebut, tanyakan dulu. ---
    if intent == 'jadwalkan_playlist' and not slots.get('publish_date'):
        pending['awaiting'] = 'publish_time'
        _save_nlu_pending(chat_id, pending)
        send_message(token, chat_id,
                     "Mulai jam berapa Bos? 🕓\n(mis. `jam 4 sore` atau `jam 16:00`)")
        return
    # --- Tema lagu (buat zumo / bank): bila belum disebut, tanyakan
    # dulu (tidak ditebak) — jawaban bebas ditangkap cabang 'theme' di
    # _nlu_try_complete_pending. Channel tidak ditanya (selalu Cozy Sax).
    if intent in ('buat_zumo', 'buat_zumobank') and not slots.get('zumo_theme'):
        pending['awaiting'] = 'theme'
        _save_nlu_pending(chat_id, pending)
        send_message(token, chat_id,
                     "Tema lagu Zumo-nya apa Bos?\n(mis. `saxophone romantis untuk dinner`, `jazz santai malam hari`)")
        return
    # --- Niche (buat short/long). ---
    if intent in ('buat_short', 'buat_long'):
        if not slots.get('niche'):
            pending['awaiting'] = 'niche'
            _save_nlu_pending(chat_id, pending)
            _nlu_ask_niche(token, chat_id, pending)
            return
        if slots.get('kind_ambiguous'):
            pending['awaiting'] = 'confirm'
            _save_nlu_pending(chat_id, pending)
            send_message(token, chat_id,
                         f"Maksud Bos: buatkan Short atau video long? "
                         f"({_nlu_describe(pending)})",
                         reply_markup={"inline_keyboard": [[
                             {"text": "🎬 Short", "callback_data": "nlu:kind:short"},
                             {"text": "🎥 Video Long", "callback_data": "nlu:kind:long"},
                         ]]})
            return
    # --- Mode (ubah mode). ---
    if intent == 'ubah_mode' and not slots.get('mode'):
        pending['awaiting'] = 'mode'
        _save_nlu_pending(chat_id, pending)
        _nlu_ask_mode(token, chat_id, pending)
        return
    # --- Hari (perpanjang): default 30 bila tidak disebut. ---
    if intent == 'perpanjang_jadwal' and not slots.get('days'):
        slots['days'] = 30
    # --- Jadwal Short (aturan jeda 6 jam): hitung SEBELUM konfirmasi
    # supaya pesan konfirmasi menyebut semua jam hasil hitungan. Hitung
    # ulang hanya bila set channel berubah (setelah konfirmasi, jadwal
    # yang sudah disetujui dipakai apa adanya — tidak dihitung ulang).
    if intent == 'buat_short' and slots.get('channels'):
        _sched_chs = sorted((slots.get('schedule') or {}).keys())
        if _sched_chs != sorted(slots.get('channels') or []):
            try:
                slots['schedule'] = _nlu_build_shorts_schedules(slots)
            except Exception:
                # 🔥 v1.3.33: jangan pakai jadwal LAMA (channel/jam
                # sudah berubah) — buang supaya eksekusi memakai
                # publish_date sebagai fallback.
                slots.pop('schedule', None)
    # --- Konfirmasi bila aksi membuat konten / berbahaya / matikan pengaman. ---
    needs_confirm = (
        intent in ('buat_short', 'buat_long', 'buat_playlist', 'jadwalkan_playlist', 'buat_zumo',
                   'buat_zumobank', 'buat_zumorakit', 'hapus', 'hentikan_antrean', 'publish_now')
        or (intent == 'ubah_mode' and slots.get('mode') == 'auto')
    )
    if needs_confirm and not pending.get('confirmed'):
        _nlu_ask_confirm(token, chat_id, pending)
        return
    # --- Semua lengkap → eksekusi. ---
    _clear_nlu_pending(chat_id)
    _nlu_execute(token, chat_id, pending)


def _nlu_publish_slot(publish_time, ch):
    """Slot tayang: jam dari perintah (hari ini bila masih di depan, besok
    bila sudah lewat), atau slot berikutnya dari _next_slot_for_channel."""
    if publish_time:
        try:
            hh, mm = publish_time.split(':')
            dt = _wib_now().replace(hour=int(hh), minute=int(mm), second=0, microsecond=0)
            if dt <= _wib_now():
                dt = dt + timedelta(days=1)
            return dt.strftime('%Y-%m-%d %H:%M')
        except Exception:
            pass
    return _next_slot_for_channel(ch)


def _execute_nlu_create_shorts(token, chat_id, pending):
    slots = pending.get('slots') or {}
    niche = slots.get('niche') or 'sleep'
    bank = _NLU_SHORTS_BANK.get(niche) or _NLU_SHORTS_BANK['sleep']
    count = slots.get('count') or 1
    duration = slots.get('duration_sec') or 30
    day_idx = _wib_now().timetuple().tm_yday
    chans = [c for c in load_channels()
             if c.get('yt_id') in (slots.get('channels') or [])]
    lines = ["🎬 *Hasil Buat Short Bos:*"]
    for ch in chans:
        yt_id = ch.get('yt_id')
        name = _channel_display_name(ch)
        # Galeri kosong TIDAK lagi menolak: panel membuat gradien otomatis
        # sekali (helper _create_shorts_muse_task); pesan "galeri kosong"
        # hanya muncul bila pembuatan gradien itu sendiri gagal (dari panel).
        base_slot = slots.get('publish_date') or _nlu_publish_slot(slots.get('publish_time'), ch)
        # Jadwal 6 jam yang sudah dihitung sebelum konfirmasi (slots['schedule'])
        # dipakai apa adanya; bila tidak ada (pending lama), fallback ke
        # perilaku lama (base_slot + 1 hari per video).
        sched = (slots.get('schedule') or {}).get(yt_id)
        made = 0
        last_resp = {}
        err = ''
        used_auto_bg = False
        used_video = 0
        made_dates = []
        for k in range(count):
            idx = (day_idx + k) % len(bank['hooks'])
            pub = base_slot
            if sched and k < len(sched):
                pub = sched[k]
            elif k:
                try:
                    dt = datetime.strptime(base_slot, '%Y-%m-%d %H:%M') + timedelta(days=k)
                    pub = dt.strftime('%Y-%m-%d %H:%M')
                except Exception:
                    pass
            ok, info, resp = request_create_shorts({
                "yt_id": yt_id, "title": bank['titles'][(day_idx + k) % len(bank['titles'])],
                "hook": bank['hooks'][idx], "script": bank['scripts'][idx],
                "cta": slots.get('cta') or bank['ctas'][(day_idx + k) % len(bank['ctas'])],
                "duration_sec": duration, "publish_date": pub,
                "text_color": "#FFFFFF", "bg_file": "", "bgm_file": "none",
                "theme": niche,
            })
            if ok:
                made += 1
                last_resp = resp
                # Tanggal final dari panel (panel boleh menggeser slot bila
                # bentrok — tanggal inilah yang benar-benar dipakai).
                made_dates.append((resp or {}).get('publish_date') or pub)
                if (resp or {}).get('bg_auto'):
                    used_auto_bg = True
                if (resp or {}).get('bg_video'):
                    try:
                        used_video = max(used_video, int((resp or {}).get('bg_video_count') or 0))
                    except Exception:
                        used_video = max(used_video, 1)
            else:
                err = info
                break
        if made:
            appr = (last_resp or {}).get('approval')
            stat = 'menunggu approval Bos' if appr == 'pending' else 'masuk antrean'
            if used_video:
                note = f" 🎬 Background video ({used_video} klip, disambung)."
            elif used_auto_bg:
                note = (" 🖼️ Background otomatis (gradien) karena galeri masih kosong — "
                        "upload gambar sendiri di panel bila mau yang lebih bagus.")
            else:
                note = ""
            sched_note = (f" Jadwal: {_nlu_format_schedule(made_dates)}."
                          if made_dates else "")
            lines.append(f"✅ *{name}* — {made} Short dibuat, {stat}.{sched_note}{note}")
        else:
            lines.append(f"❌ *{name}* — {err or 'gagal dibuat'}.")
    send_message(token, chat_id, "\n".join(lines))


def _execute_nlu_create_long(token, chat_id, pending):
    slots = pending.get('slots') or {}
    chans = [c for c in load_channels()
             if c.get('yt_id') in (slots.get('channels') or [])]
    ch = chans[0] if chans else None
    if ch is None:
        send_message(token, chat_id, "Channelnya nggak ketemu Bos 😅 — coba sebut nama channelnya lagi.")
        return
    count = slots.get('count') or 1
    start_date = slots.get('publish_date') or _nlu_publish_slot(slots.get('publish_time'), ch)
    ok, info, resp = request_create_long({
        "yt_id": ch.get('yt_id'), "count": count, "titles": [],
        "start_date": start_date, "interval_days": 1,
    })
    if not ok:
        send_message(token, chat_id, f"⚠️ *Yah Bos, gagal dibuat* 😅\n{info}")
        return
    _send_make_created_message(token, chat_id, 'long',
                               {'channel_name': _channel_display_name(ch), 'count': count},
                               resp)


def _execute_nlu_publish_now(token, chat_id, pending):
    """Publish langsung (v1.3.6): ubah task yang masih dijadwalkan jadi
    tayang sekarang, lewat endpoint operator panel (tanpa edit file
    langsung — panel yang pegang state in-memory)."""
    slots = pending.get('slots') or {}
    chans = slots.get('channels')
    if chans == 'all' or not chans:
        yt_ids = [c.get('yt_id') for c in load_channels() if c.get('yt_id')]
    else:
        yt_ids = [y for y in chans if y]
    ok, msg, data = _operator_post('/api/operator/publish_now', {'yt_ids': yt_ids})
    if not ok:
        send_message(token, chat_id,
                     "Gagal publish langsung Bos — " + (msg or 'panel tidak merespons'))
        return
    updated = (data or {}).get('updated') or []
    if not updated:
        send_message(token, chat_id,
                     "Tidak ada video yang masih dijadwalkan Bos — semua sudah tayang atau antrean kosong.")
        return
    _all_ch = load_channels()
    _names = []
    for _u in updated:
        _c = next((c for c in _all_ch if c.get('yt_id') == _u.get('yt_id')), None)
        _names.append(_channel_display_name(_c) if _c else (_u.get('yt_id') or '?'))
    _uniq = sorted(set(_names))
    _extra = ""
    if (data or {}).get('skipped_approval'):
        _extra += f"\n{(data or {}).get('skipped_approval')} video masih menunggu approval — tidak ikut dipublish."
    if (data or {}).get('skipped_paused'):
        _extra += f"\n{(data or {}).get('skipped_paused')} video di channel yang dijeda — tidak ikut dipublish."
    send_message(token, chat_id,
                 f"Siap Bos, {len(updated)} video langsung tayang: {', '.join(_uniq)}. "
                 "Begitu render selesai langsung tayang di YouTube, tidak menunggu jadwal." + _extra)


def _execute_nlu_extend(token, chat_id, pending):
    slots = pending.get('slots') or {}
    days = slots.get('days') or 30
    chans = [c for c in load_channels()
             if c.get('yt_id') in (slots.get('channels') or [])]
    lines = [f"📅 *Hasil Perpanjang Jadwal ({days} hari) Bos:*"]
    for ch in chans:
        name = _channel_display_name(ch)
        ok, info, resp = request_extend_schedule(ch.get('yt_id'), days)
        if not ok:
            lines.append(f"❌ *{name}* — {info}")
            continue
        appr = resp.get('approval')
        stat = 'menunggu approval Bos' if appr == 'pending' else 'masuk antrean'
        try:
            first = datetime.strptime(resp.get('first_date', ''), '%Y-%m-%d %H:%M').strftime('%d %b %Y')
            last = datetime.strptime(resp.get('last_date', ''), '%Y-%m-%d %H:%M').strftime('%d %b %Y')
            rng = f"{first} – {last}"
        except Exception:
            rng = '—'
        lines.append(f"✅ *{name}* — {resp.get('count', days)} hari ({rng}), {stat}.")
    send_message(token, chat_id, "\n".join(lines))


def _execute_nlu_set_mode(token, chat_id, pending):
    slots = pending.get('slots') or {}
    mode = slots.get('mode')
    chans = [c for c in load_channels()
             if c.get('yt_id') in (slots.get('channels') or [])]
    lines = []
    for ch in chans:
        name = _channel_display_name(ch)
        ok, info, resp = request_set_autopilot(ch.get('yt_id'), mode)
        if not ok:
            lines.append(f"❌ *{name}* — {info}")
            continue
        if mode == 'auto':
            lines.append(f"🤖 *{name}* — mode Full-Auto. Video baru langsung tayang tanpa persetujuan Bos.")
        else:
            lines.append(f"👀 *{name}* — mode Approval. Mulai sekarang video channel ini menunggu persetujuan Bos sebelum tayang.")
    send_message(token, chat_id, "\n".join(lines) or "Channelnya nggak ketemu Bos 😅")


def _execute_nlu_set_paused(token, chat_id, pending):
    slots = pending.get('slots') or {}
    paused = pending.get('intent') == 'jeda_channel'
    chans = [c for c in load_channels()
             if c.get('yt_id') in (slots.get('channels') or [])]
    lines = []
    for ch in chans:
        name = _channel_display_name(ch)
        ok, info, resp = request_set_paused(ch.get('yt_id'), paused)
        if not ok:
            lines.append(f"❌ *{name}* — {info}")
            continue
        if paused:
            lines.append(f"⏸️ *{name}* dijeda — semua jadwal & antreannya berhenti sampai dilanjutkan.")
        else:
            lines.append(f"▶️ *{name}* jalan lagi — {resp.get('resumed_count', 0)} task masuk antrean render.")
    send_message(token, chat_id, "\n".join(lines) or "Channelnya nggak ketemu Bos 😅")


def _execute_nlu_destructive(token, chat_id, pending):
    slots = pending.get('slots') or {}
    target = slots.get('target') or 'tidak_jelas'
    names = _nlu_channel_names(slots.get('channels') or [])
    ch = ', '.join(names) if names else ''
    if target == 'channel':
        send_message(token, chat_id,
                     f"Siap Bos, saya TIDAK hapus sendiri dari Telegram (terlalu berisiko). "
                     f"Untuk hapus channel {ch or 'itu'}: buka panel → Settings → daftar channel → "
                     f"Hapus. Saya tunggu di sini kalau Bos berubah pikiran 😌")
    elif target == 'video':
        send_message(token, chat_id,
                     "Siap Bos, hapus video saya tidak lakukan dari Telegram — video yang sudah "
                     "tayang hanya bisa dihapus dari YouTube Studio. Video yang masih di antrean "
                     "bisa dibatalkan dari panel → Task Monitor.")
    elif target == 'antrean':
        send_message(token, chat_id,
                     "Siap Bos, antrean tidak saya hentikan dari sini — buka panel → Task Monitor, "
                     "batalkan task-nya satu per satu, atau jeda channelnya dari Telegram "
                     "dengan perintah 'jeda channel <nama>'.")
    else:
        send_message(token, chat_id,
                     "Siap Bos, hapus apa maksudnya? Sebut yang mau dihapus ya — channel, video, "
                     "atau antrean. Saya tidak hapus apa-apa sebelum Bos konfirmasi 😌")


def _execute_nlu_create_playlist(token, chat_id, pending):
    """NLU 'buatkan playlist ...' — 1 channel: masuk ke alur tanya-jawab
    yang SAMA seperti /playlist (draft + pilih kandidat manual).
    Multi-channel (🔥 v1.3.2): riset + auto-pick + buat langsung per
    channel, tanpa alur terpandu."""
    slots = pending.get('slots') or {}
    chs = [c for c in load_channels()
           if c.get('yt_id') in (slots.get('channels') or [])]
    if len(chs) > 1:
        _execute_nlu_create_playlist_multi(token, chat_id, pending, chs)
        return
    ch = chs[0] if chs else None
    if not ch:
        send_message(token, chat_id,
                     "😅 *Channel-nya belum ketemu Bos* — ketik /playlist lalu pilih channelnya ya.")
        return
    data = {'yt_id': ch.get('yt_id'),
            'channel_name': _channel_display_name(ch),
            'theme': slots.get('playlist_theme') or ''}
    if slots.get('playlist_source_url'):
        data['source_url'] = slots['playlist_source_url']
        data['source_kind'] = slots.get('playlist_source_kind') or 'video'
        if not data.get('theme'):
            data['theme'] = 'Link YouTube'
    if slots.get('pixabay_theme'):
        data['pixabay_theme'] = slots['pixabay_theme']
    if slots.get('duration_min_sec') is not None:
        data['duration_min_sec'] = slots['duration_min_sec']
        data['duration_max_sec'] = slots.get('duration_max_sec')
        data['duration_note'] = slots.get('duration_note') or ''
    if slots.get('auto_pick'):
        data['auto_pick'] = True
    if slots.get('publish_now'):
        data['publish_now'] = True
        data['publish_date'] = _playlist_publish_now_date()
    elif slots.get('publish_date'):
        # Jadwal lengkap dari perintah alami — masuk ke draft supaya
        # langkah jadwal dilewati dan ringkasan menyebut tafsirannya.
        data['publish_date'] = slots['publish_date']
        if slots.get('publish_note'):
            data['publish_note'] = slots['publish_note']
    if not data.get('theme'):
        data.pop('theme', None)
    draft = {"kind": "playlist", "step": _playlist_next_step(data), "data": data}
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 f"🎵 *Siap Bos, kita buat Playlist buat channel {_tg_escape(data['channel_name'])} ya!* 😎\n"
                 "Jawab tiap langkah satu per satu — ketik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)


def _execute_nlu_create_playlist_multi(token, chat_id, pending, chs):
    """NLU playlist multi-channel (🔥 v1.3.2, v1.3.5): untuk SETIAP channel —
    riset tema yang sama (bertingkat berbasis umur: 30 hari → 90 hari →
    1 tahun → evergreen; TANPA filter views) atau, bila Bos mengirim link YouTube
    ("jiplak playlist ini"), ambil daftar lagu dari link itu langsung
    tanpa riset/filter; lalu auto-pick + buat job di slot berikutnya.
    Channel yang riset / kandidat / create-nya gagal dilewati +
    dilaporkan jujur; channel lain tetap jalan. Bila SEMUA channel 0 job
    (v1.3.5): eskalasi SEKALI ke Muse lewat jembatan (_bridge_forward,
    source playlist_no_candidate) — jangan berhenti begitu saja.
    Login dicek dulu; yang mati dikeluarkan (bila semua mati,
    tidak ada yang dibuat)."""
    slots = pending.get('slots') or {}
    theme = slots.get('playlist_theme') or ''
    # Login — pengaman bila _nlu_advance belum sempat cek.
    if not slots.get('login_checked'):
        _res = _nlu_check_channels_login(chs)
        _live = [c for c in chs if _res.get(c.get('yt_id'))]
        _dead = [c for c in chs if not _res.get(c.get('yt_id'))]
        slots['login_checked'] = True
        if _dead:
            slots['login_dead'] = [_channel_display_name(c) for c in _dead]
        chs = _live
        if not chs:
            send_message(token, chat_id,
                         "Semua channel loginnya mati Bos — tidak ada playlist yang dibuat.\n"
                         "Tautkan ulang dari panel (Settings → Tambah Channel, device login), lalu coba lagi ya.")
            return
    dead_names = slots.get('login_dead') or []
    _src_url = slots.get('playlist_source_url') or ''
    if _src_url:
        send_message(token, chat_id,
                     f"Siap Bos, saya buatkan playlist dari link Bos untuk {len(chs)} channel ya — ambil daftar lagunya langsung, yang berhasil langsung dibuat.")
    else:
        send_message(token, chat_id,
                     f"Siap Bos, saya buatkan playlist tema *{_tg_escape(theme)}* untuk {len(chs)} channel ya — riset satu per satu, yang lolos langsung dibuat.")
    lines = []
    made = []
    _last_tier_label = None
    # 🔥 v1.3.31: referensi yang sudah dipakai di batch ini (supaya tiap
    # channel dapat video sumber yang beda — judul laporan tidak sama semua).
    used_refs = set()
    for _ci, ch in enumerate(chs):
        name = _channel_display_name(ch)
        yt = ch.get('yt_id')
        if _src_url:
            ok, msg, data = _operator_post('/api/operator/playlist_from_url',
                                           {'yt_id': yt, 'url': _src_url})
            if not ok:
                lines.append(f"• *{_tg_escape(name)}*: link gagal dibaca — {_tg_escape(msg)} (dilewati)")
                made.append(False)
                continue
            cands = (data or {}).get('candidates') or []
            best, _tier_lbl = _playlist_pick_best(cands, None, None)
            _last_tier_label = _tier_lbl
            if not best:
                lines.append(f"• *{_tg_escape(name)}*: link itu tidak ada lagunya — dilewati")
                made.append(False)
                continue
        else:
            ok, msg, data = _operator_post('/api/operator/playlist_research',
                                           {'yt_id': yt, 'theme': theme,
                                            'min_duration_sec': slots.get('duration_min_sec'),
                                            'max_duration_sec': slots.get('duration_max_sec')})
            if not ok:
                lines.append(f"• *{_tg_escape(name)}*: riset gagal — {_tg_escape(msg)} (dilewati)")
                made.append(False)
                continue
            cands = (data or {}).get('candidates') or []
            # 🔥 v1.3.31: pilih kandidat terbaik yang BELUM dipakai channel
            # lain di batch ini; bila semua sudah dipakai, pakai daftar penuh.
            _avail0 = [c for c in cands if c.get('video_id') not in used_refs]
            best, _tier_lbl = _playlist_pick_best(_avail0 or cands, slots.get('duration_min_sec'),
                                                  slots.get('duration_max_sec'))
            _last_tier_label = _tier_lbl
            if not best:
                lines.append(f"• *{_tg_escape(name)}*: tidak ada kandidat yang lolos — sudah dicari sampai semua umur video (evergreen), coba kirim link YouTube-nya dengan 'jiplak playlist ini'")
                made.append(False)
                continue
        if slots.get('publish_date'):
            pub = slots['publish_date']
            if pending.get('intent') == 'jadwalkan_playlist':
                pub = _stagger_publish_date(pub, _ci, slots.get('schedule_stagger_min') or 15)
        elif slots.get('publish_now'):
            pub = _playlist_publish_now_date()
        else:
            pub = _next_slot_for_channel(ch)
        # 🔥 v1.3.31: bila create ditolak dedupe (referensi sudah dipakai
        # 24 jam terakhir), coba kandidat terbaik berikutnya (maks 3x).
        ok2 = False
        info2 = ''
        _resp2 = None
        _tried = set()
        for _attempt in range(3):
            ok2, info2, _resp2 = _operator_post('/api/operator/playlist_create',
                                                {'job_id': (data or {}).get('job_id', ''),
                                                 'video_id': best.get('video_id'),
                                                 'publish_date': pub, 'privacy': 'public',
                                                 'pixabay_theme': slots.get('pixabay_theme') or ''})
            if ok2:
                break
            if 'sudah dibuat' not in (info2 or '') and 'sudah dipakai' not in (info2 or ''):
                break
            _tried.add(best.get('video_id'))
            _next, _tl = _playlist_pick_best(
                [c for c in (cands if not _src_url else [])
                 if c.get('video_id') not in _tried and c.get('video_id') not in used_refs],
                slots.get('duration_min_sec'), slots.get('duration_max_sec'))
            if not _next:
                break
            best = _next
        if not ok2:
            lines.append(f"• *{_tg_escape(name)}*: gagal dibuat — {_tg_escape(info2)} (dilewati)")
            made.append(False)
            continue
        if not _src_url:
            used_refs.add(best.get('video_id'))
        views_txt = f"{best.get('views') or 0:,}".replace(',', '.')
        made.append(True)
        if _src_url:
            lines.append(f"• *{_tg_escape(name)}*: dibuat dari link Bos — *{_tg_escape((best.get('title') or '')[:60])}* ({views_txt} views) — tayang {pub}")
        else:
            lines.append(f"• *{_tg_escape(name)}*: dibuat — *{_tg_escape((best.get('title') or '')[:60])}* ({views_txt} views, tier {_tier_lbl or 'evergreen'}) — tayang {pub}")
    # v1.3.5 eskalasi (SEKALI per order, bukan per channel): bila 0 job
    # dibuat di semua channel, teruskan kegagalan ke Muse lewat jembatan
    # (cron bridge 2 menit) — jangan biarkan order berhenti begitu saja.
    if _playlist_should_escalate(made):
        _esc_text = _playlist_escalation_text(
            theme or 'Link YouTube (jiplak)',
            [_channel_display_name(c) for c in chs],
            slots.get('publish_date'), _last_tier_label, _src_url or None)
        _bridge_forward(token, chat_id, None, _esc_text,
                        source='playlist_no_candidate',
                        context_extra={'theme': theme, 'channels': [c.get('yt_id') for c in chs],
                                       'publish_date': slots.get('publish_date'),
                                       'tier_label': _last_tier_label,
                                       'source_url': _src_url or None})
        lines.append("")
        lines.append("Saya teruskan ke Muse ya Bos, biar dicarikan solusinya — mis. longgarkan lagi, ganti tema, atau kirim link YouTube untuk dijiplak.")
    if dead_names:
        lines.append("")
        lines.append("Yang loginnya mati (tidak dibuatkan): " + ", ".join(_tg_escape(n) for n in dead_names)
                     + " — tautkan ulang dari panel (Settings → Tambah Channel, device login).")
    if any(made):
        lines.append("")
        lines.append("Judul di atas itu judul video SUMBER-nya ya Bos — judul final tiap channel beda-beda "
                     "(ditulis ulang pakai rumus SEO), bisa dicek di panel Task Monitor begitu task-nya muncul.")
    send_message(token, chat_id, "\n".join(lines) or "Tidak ada playlist yang dibuat Bos.")


def _execute_nlu_create_zumo(token, chat_id, pending):
    """NLU 'buatkan lagu zumo ...' — masuk ke alur tanya-jawab yang SAMA
    seperti /zumo: slot yang sudah diketahui (channel, tema, bahasa,
    vokal, jumlah, jadwal) diisi ke draft, sisanya ditanyakan satu per
    satu (tidak ditebak). Channel yang disebut di perintah dipakai;
    bila tidak disebut, langkah channel menanyakan (default Cozy Sax)."""
    slots = pending.get('slots') or {}
    data = {}
    chs = [c for c in load_channels()
           if c.get('yt_id') in (slots.get('channels') or [])]
    ch = chs[0] if chs else None
    if ch:
        data['yt_id'] = ch.get('yt_id')
        data['channel_name'] = _channel_display_name(ch)
    if slots.get('zumo_theme'):
        data['theme'] = slots['zumo_theme']
    if slots.get('instrumental') is True:
        data['mode'] = 'instrumental'
        data['lyrics_mode'] = 'none'
    elif slots.get('instrumental') is False:
        data['mode'] = 'vokal'
    if slots.get('lyrics_language'):
        data['lyrics_language'] = slots['lyrics_language']
    if slots.get('vocal_gender'):
        data['vocal_gender'] = slots['vocal_gender']
    if slots.get('song_count'):
        data['song_count'] = slots['song_count']
    if slots.get('publish_date'):
        data['publish_date'] = slots['publish_date']
        if slots.get('publish_note'):
            data['publish_note'] = slots['publish_note']
    data['model'] = _zumo_active_model()
    draft = {"kind": "zumo", "step": _zumo_next_step(data), "data": data}
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 "Siap Bos, kita buat Playlist Zumo bareng-bareng ya!\n"
                 "Lagu orisinal (Kie.ai) — jawab tiap langkah satu per satu, ketik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)


def _execute_nlu_create_zumobank(token, chat_id, pending):
    """NLU 'buatkan lagu untuk bank ...' — masuk ke alur tanya-jawab
    yang SAMA seperti /zumobank: slot yang sudah diketahui (channel,
    tema, mode, jumlah) diisi ke draft, sisanya ditanyakan satu per
    satu (tidak ditebak)."""
    slots = pending.get('slots') or {}
    data = {'zumo_mode': 'bank'}
    chs = [c for c in load_channels()
           if c.get('yt_id') in (slots.get('channels') or [])]
    ch = chs[0] if chs else None
    if ch:
        data['yt_id'] = ch.get('yt_id')
        data['channel_name'] = _channel_display_name(ch)
    if slots.get('zumo_theme'):
        data['theme'] = slots['zumo_theme']
    if slots.get('instrumental') is True:
        data['mode'] = 'instrumental'
        data['lyrics_mode'] = 'none'
    elif slots.get('instrumental') is False:
        data['mode'] = 'vokal'
    if data.get('mode') != 'instrumental':
        if slots.get('lyrics_language'):
            data['lyrics_language'] = slots['lyrics_language']
        if slots.get('vocal_gender'):
            data['vocal_gender'] = slots['vocal_gender']
    if slots.get('bank_count'):
        data['bank_count'] = slots['bank_count']
    data['model'] = _zumo_active_model()
    draft = {"kind": "zumobank", "step": _zumobank_next_step(data), "data": data}
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 "Siap Bos, kita isi Bank Audio Zumo bareng-bareng ya!\n"
                 "Lagu di-generate massal dan disimpan di bank (tanpa upload) — jawab tiap langkah satu per satu, ketik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)


def _execute_nlu_create_zumorakit(token, chat_id, pending):
    """NLU 'rakit video dari bank ...' — masuk ke alur tanya-jawab
    yang SAMA seperti /zumorakit: slot yang sudah diketahui (channel,
    jumlah, jadwal) diisi ke draft, sisanya ditanyakan satu per satu."""
    slots = pending.get('slots') or {}
    data = {'zumo_mode': 'assemble', 'theme': 'Bank'}
    chs = [c for c in load_channels()
           if c.get('yt_id') in (slots.get('channels') or [])]
    ch = chs[0] if chs else None
    if ch:
        data['yt_id'] = ch.get('yt_id')
        data['channel_name'] = _channel_display_name(ch)
    if slots.get('bank_count'):
        data['bank_count'] = slots['bank_count']
    if slots.get('publish_date'):
        data['publish_date'] = slots['publish_date']
        if slots.get('publish_note'):
            data['publish_note'] = slots['publish_note']
    data['model'] = _zumo_active_model()
    draft = {"kind": "zumorakit", "step": _zumorakit_next_step(data), "data": data}
    _save_make_draft(chat_id, draft)
    send_message(token, chat_id,
                 "Siap Bos, kita rakit video dari Bank Audio Zumo ya!\n"
                 "Lagu dipilih acak dari bank channel ini — jawab tiap langkah satu per satu, ketik /batal kapan saja untuk berhenti.")
    _make_ask_step(token, chat_id, draft)


def _nlu_execute(token, chat_id, pending):
    kind = pending.get('intent')
    if kind == 'buat_short':
        _execute_nlu_create_shorts(token, chat_id, pending)
    elif kind == 'buat_long':
        _execute_nlu_create_long(token, chat_id, pending)
    elif kind == 'buat_playlist':
        _execute_nlu_create_playlist(token, chat_id, pending)
    elif kind == 'jadwalkan_playlist':
        _chs = [c for c in load_channels()
                if c.get('yt_id') in ((pending.get('slots') or {}).get('channels') or [])]
        _execute_nlu_create_playlist_multi(token, chat_id, pending, _chs)
    elif kind == 'buat_zumo':
        _execute_nlu_create_zumo(token, chat_id, pending)
    elif kind == 'buat_zumobank':
        _execute_nlu_create_zumobank(token, chat_id, pending)
    elif kind == 'buat_zumorakit':
        _execute_nlu_create_zumorakit(token, chat_id, pending)
    elif kind == 'perpanjang_jadwal':
        _execute_nlu_extend(token, chat_id, pending)
    elif kind == 'ubah_mode':
        _execute_nlu_set_mode(token, chat_id, pending)
    elif kind == 'jeda_channel' or kind == 'lanjutkan_channel':
        _execute_nlu_set_paused(token, chat_id, pending)
    elif kind in ('hapus', 'hentikan_antrean'):
        _execute_nlu_destructive(token, chat_id, pending)
    elif kind == 'publish_now':
        _execute_nlu_publish_now(token, chat_id, pending)


def _nlu_run_readonly(token, chat_id, intent, slots=None):
    """Perintah baca saja dari NLU — memakai builder yang sama seperti
    perintah baku."""
    if intent == 'status':
        send_message(token, chat_id, build_status_report())
    elif intent == 'laporan':
        kind = 'morning' if _wib_now().hour < 12 else 'evening'
        report = build_daily_report(kind)
        send_message(token, chat_id, report if report else "Belum ada data laporan hari ini, Bos.")
    elif intent == 'laporan_detail':
        # 🔥 v1.2.20: versi terperinci — snapshot disimpan SETELAH terkirim
        # (sama seperti /laporan di handle_user_command).
        text, snap = build_detail_report()
        res = send_message(token, chat_id, text)
        if res and res.get('ok'):
            _update_report_snapshot(snap)
    elif intent == 'kuota':
        send_message(token, chat_id, build_quota_report())
    elif intent == 'stok':
        send_message(token, chat_id, build_stock_report())
    elif intent == 'statistik':
        send_message(token, chat_id, build_stats_report())
    elif intent == 'monetisasi':
        send_message(token, chat_id, build_monetization_report())
    elif intent == 'jadwal':
        _jadwal_txt, _jadwal_total = build_schedule_report()
        send_message(token, chat_id, _jadwal_txt,
                     reply_markup=_EMPTY_SCHEDULE_KB if _jadwal_total == 0 else None)
    elif intent == 'jadwal_bulan':
        # 🔥 v1.2.20: 1 pesan per channel (sudah ringkas per channel).
        names = _nlu_channel_names((slots or {}).get('channels') or [])
        blocks = []
        if names:
            for nm in names:
                blocks += build_month_schedule_blocks(channel_arg=nm)
        else:
            blocks = build_month_schedule_blocks()
        for blk in blocks:
            for chunk in _split_long_message(blk):
                send_message(token, chat_id, chunk)
    elif intent == 'gagal':
        send_message(token, chat_id, build_failed_report()[0])
    elif intent == 'channel_list':
        # 🔥 v1.2.20: tampilan database channel yang rapi (nama, subs,
        # videos, mode, dijeda, stok jadwal, progres monetisasi) — builder
        # yang sama dengan perintah /channel.
        send_message(token, chat_id, build_channel_list_report())
    elif intent == 'bantuan':
        # 🔥 v1.3.33: build_help_message() tidak pernah didefinisikan
        # (NameError → pesan bantuan hangus diam-diam). Pakai teks
        # yang sama dengan /start.
        send_message(token, chat_id, _build_start_text())
    elif intent == 'tambah_channel':
        send_message(token, chat_id,
                     "Tambah channel hanya bisa dari panel/browser Bos — butuh login Google di browser 😌\n"
                     "Caranya: buka panel → Settings → ➕ Tambah Channel, lalu ikuti device login seperti biasa.\n"
                     "Saya tidak bisa menambahkan channel dari Telegram.")


def _nlu_suggest(clean):
    """Did-you-mean: perintah baku terdekat berdasarkan kata yang sama."""
    _KW = {
        '/status': ('status', 'server', 'cek', 'kabar', 'jalan'),
        '/buatshort': ('short', 'pendek', 'buat', 'bikin', 'video'),
        '/buatlong': ('long', 'panjang', 'kompilasi', 'video', 'buat', 'bikin'),
        '/buatplaylist': ('playlist', 'musik', 'buat', 'bikin'),
        '/playlist': ('playlist', 'musik', 'buat', 'bikin'),
        '/zumo': ('zumo', 'lagu', 'sax', 'saxophone', 'buat', 'bikin'),
        '/zumobank': ('zumo', 'bank', 'lagu', 'buat', 'bikin'),
        '/zumorakit': ('zumo', 'bank', 'rakit', 'video', 'buat'),
        '/zumomodel': ('zumo', 'model', 'versi'),
        '/kiekey': ('kie', 'key', 'api'),
        '/kiekeys': ('kie', 'key', 'api'),
        '/kiedel': ('kie', 'key', 'hapus'),
        '/laporan': ('laporan', 'report', 'pagi', 'sore'),
        '/kuota': ('kuota', 'limit', 'upload'),
        '/stok': ('stok', 'sisa', 'jadwal'),
        '/statistik': ('statistik', 'stats', 'views'),
        '/monetisasi': ('monetisasi', 'penghasilan', 'uang', 'rpm'),
        '/jadwal': ('jadwal', 'schedule', 'kalender'),
        '/jadwalbulan': ('jadwal', 'bulan', 'sebulan', 'kalender', '30'),
        '/aturjadwal': ('atur', 'ubah', 'jadwal', 'setting', 'ganti'),
        '/channel': ('channel', 'daftar', 'list', 'info'),
        '/gagal': ('gagal', 'error', 'gagal'),
        '/perpanjang': ('perpanjang', 'tambah', 'jadwal'),
        '/mode': ('mode', 'auto', 'approval', 'autopilot'),
        '/jeda': ('jeda', 'pause', 'berhenti'),
        '/lanjutkan': ('lanjutkan', 'resume', 'jalan'),
        '/judul': ('judul', 'title'),
        '/ideashorts': ('ide', 'shorts', 'ideashorts'),
        '/cta': ('cta', 'ajakan'),
    }
    words = set(clean.split())
    scored = []
    for cmd, kws in _KW.items():
        score = len(words & set(kws))
        if score:
            scored.append((score, cmd))
    scored.sort(reverse=True)
    return [cmd for _s, cmd in scored[:2]]


def _nlu_try_revise_pending(pending, raw_cmd, clean_cmd):
    """🔥 v1.3.31: koreksi slot pending yang sedang dikonfirmasi
    ('jam 5 sore' / 'tema lofi' / 'cuma Aruya'). True bila ada slot
    yang berubah (pemanggil menyimpan + advance ulang untuk konfirmasi
    baru). Satu koreksi sekali balas; konfirmasi baru yang muncul bisa
    dikoreksi lagi. Jawaban murni ya/tidak BUKAN revisi."""
    if set((clean_cmd or '').split()) <= {'ya', 'iya', 'iyes', 'ok', 'oke', 'setuju', 'lanjut', 'benar',
                                          'tidak', 'nggak', 'gak', 'bukan', 'batal', 'ngga', 'salah'}:
        return False
    slots = pending.setdefault('slots', {})
    intent = pending.get('intent')
    # 1. Jam/waktu ("jam 5", "jam 5 sore", "besok jam 9").
    ndt, nnote = _parse_natural_datetime(raw_cmd)
    if ndt:
        slots['publish_date'] = ndt
        if nnote:
            slots['publish_note'] = nnote
        else:
            slots.pop('publish_note', None)
        # 🔥 v1.3.33: jadwal Short lama (slots['schedule']) dihitung
        # dari jam lama — buang supaya _nlu_advance menghitung ulang.
        slots.pop('schedule', None)
        return True
    # 2. Channel ("Aruya aja", "cuma Cozy Sax", "semua") — dicek
    # SEBELUM tema supaya 'cuma Aruya' tidak dibaca jadi tema 'cuma'.
    matched, all_flag = _nlu_match_channels(clean_cmd, load_channels())
    if all_flag:
        slots['channels'] = [c.get('yt_id') for c in load_channels()]
        slots.pop('login_checked', None)
        return True
    if matched:
        slots['channels'] = [c.get('yt_id') for c in matched]
        slots.pop('login_checked', None)
        return True
    # 3. Tema playlist ("tema lofi", "temanya nostalgia").
    if intent in ('buat_playlist', 'jadwalkan_playlist'):
        th = _nlu_extract_playlist_theme(clean_cmd, load_channels())
        if th:
            slots['playlist_theme'] = th
            slots.pop('playlist_source_url', None)
            return True
    return False


def _nlu_try_complete_pending(token, chat_id, raw_cmd, clean_cmd):
    """Jawaban singkat atas klarifikasi yang sedang menunggu (nama channel /
    angka / niche / mode / ya-tidak). Kembalikan True bila pending
    diselesaikan atau dimajukan."""
    pending = _load_nlu_pending(chat_id)
    if not pending:
        return False
    awaiting = pending.get('awaiting')
    slots = pending.setdefault('slots', {})
    if awaiting == 'confirm':
        words = set(clean_cmd.split())
        # 🔥 v1.3.33: REVISI dicek SEBELUM kata ya — "ya, jam 5 sore"
        # itu koreksi jam, bukan konfirmasi jam lama. Jawaban murni
        # ya/tidak ditolak guard di dalam revise, jadi tetap konfirmasi.
        if _nlu_try_revise_pending(pending, raw_cmd, clean_cmd):
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return True
        if words & {'ya', 'iya', 'iyes', 'ok', 'oke', 'setuju', 'lanjut', 'benar'}:
            pending['confirmed'] = True
            _nlu_audit_log(raw_cmd, {"intent": pending.get('intent'), "confidence": None, "slots": slots},
                           'confirmed')
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return True
        if words & {'tidak', 'nggak', 'gak', 'bukan', 'batal', 'ngga', 'salah'}:
            _clear_nlu_pending(chat_id)
            send_message(token, chat_id, "Siap Bos, dibatalkan 😌 — coba ketik lagi perintahnya dengan bahasa yang lain ya.")
            return True
        return False
    if awaiting == 'channel':
        chans = load_channels()
        cands = pending.get('candidates') or [c.get('yt_id') for c in chans]
        need = slots.get('channel_count') or 1
        cur = list(slots.get('channels') or [])

        def _accumulate(new_ids):
            if need == 1:
                # Satu channel saja: pilihan baru menggantikan yang lama.
                slots['channels'] = list(new_ids)
            else:
                added = [y for y in new_ids if y not in cur]
                cur.extend(added)
                slots['channels'] = cur
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return True

        if clean_cmd in ('semua', 'semua channel'):
            return _accumulate(list(cands))
        m = re.match(r'^(\d+)$', clean_cmd)
        if m:
            idx = int(m.group(1)) - 1
            if 0 <= idx < len(cands):
                return _accumulate([cands[idx]])
            return False
        matched, all_flag = _nlu_match_channels(clean_cmd, chans)
        if all_flag:
            return _accumulate(list(cands))
        if matched:
            return _accumulate([c.get('yt_id') for c in matched])
        # Bukan jawaban atas klarifikasi (mis. perintah baru "cek status")
        # → biarkan di-parse ulang sebagai perintah segar. Hanya bila teks
        # menyebut 'channel' tapi tidak cocok, beri petunjuk.
        if 'channel' in clean_cmd:
            send_message(token, chat_id,
                         "Saya nggak nemu channel yang Bos sebut 😅 — yang terdaftar: "
                         + ", ".join(_channel_display_name(c) for c in chans)
                         + ". Coba sebut namanya lagi ya Bos.")
            return True
        return False
    if awaiting == 'niche':
        niche, _unk = _nlu_detect_niche(clean_cmd)
        if niche:
            slots['niche'] = niche
            slots.pop('unknown_niche', None)
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return True
        return False
    if awaiting == 'theme':
        # v1.3.5 jiplak: jawaban berupa link YouTube -> sumber playlist.
        _link = _extract_youtube_link(raw_cmd)
        if _link and pending.get('intent') == 'buat_playlist':
            _vid, _plid = _extract_youtube_ids_from_url(_link)
            slots['playlist_source_url'] = _link
            slots['playlist_source_kind'] = 'playlist' if _plid else 'video'
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return True
        # Jawaban tema playlist (bebas) — tapi jangan telan perintah
        # baca/aksi lain; biarkan di-parse ulang sebagai perintah segar.
        _READONLY_WORDS = {'status', 'laporan', 'kuota', 'stok', 'statistik',
                           'monetisasi', 'jadwal', 'gagal', 'bantuan', 'channel',
                           'judul', 'deskripsi', 'tag', 'cta', 'ide', 'backup',
                           'mode', 'jeda', 'lanjutkan', 'perpanjang'}
        if (set(clean_cmd.split()) & _READONLY_WORDS
                or clean_cmd.startswith(('buat', 'bikin', 'buatkan'))):
            return False
        th = raw_cmd.strip()
        th = re.sub(r'^(tema|temanya)\s+', '', th, flags=re.I).strip()
        if not th:
            return False
        if pending.get('intent') in ('buat_zumo', 'buat_zumobank'):
            slots['zumo_theme'] = th[:100]
        else:
            slots['playlist_theme'] = th[:100]
        _save_nlu_pending(chat_id, pending)
        _nlu_advance(token, chat_id, pending)
        return True
    if awaiting == 'mode':
        mode = _nlu_extract_mode(clean_cmd)
        if mode:
            slots['mode'] = mode
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return True
        return False
    if awaiting == 'publish_time':
        # 🔥 v1.3.30: jawaban jam mulai ("jam 4 sore" / "jam 16:00").
        # Tidak bisa diparse → biarkan di-parse ulang sebagai perintah segar.
        ndt, _nnote = _parse_natural_datetime(raw_cmd)
        if ndt:
            slots['publish_date'] = ndt
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return True
        return False
    return False


def _handle_nlu_message(token, chat_id, raw_cmd, clean_cmd):
    """Terjemahkan pesan bebas. True = ditangani (dieksekusi / dikonfirmasi /
    diklarifikasi); False = tidak dikenali (biarkan cabang lama)."""
    if _nlu_try_complete_pending(token, chat_id, raw_cmd, clean_cmd):
        return True
    channels = load_channels()
    parsed = parse_natural_command(raw_cmd, channels)
    intent = parsed.get('intent')
    if intent == 'tidak_dikenali':
        _nlu_audit_log(raw_cmd, parsed, 'fallback')
        return False
    # 🔥 v1.2.20: perintah baru Bagian B ikut jalur baca-saja — KECUALI
    # bila cocoknya lewat typo (fuzzy): jangan langsung jalan, tanyakan
    # "Maksud Bos ...?" dulu lewat alur konfirmasi (confidence <60).
    _fuzzy_new = (parsed.get('slots') or {}).get('fuzzy_match')
    if intent in ('status', 'laporan', 'kuota', 'stok', 'statistik',
                  'monetisasi', 'jadwal', 'gagal', 'channel_list', 'bantuan',
                  'tambah_channel', 'jadwal_bulan', 'laporan_detail') and not _fuzzy_new:
        _nlu_audit_log(raw_cmd, parsed, 'executed')
        _nlu_run_readonly(token, chat_id, intent, parsed.get('slots') or {})
        return True
    if (parsed.get('confidence') or 0) < 60:
        # Ragu — konfirmasi tafsiran dulu, jangan bertindak.
        pending = {"intent": intent, "slots": parsed.get('slots') or {},
                   "awaiting": None, "candidates": [c.get('yt_id') for c in channels],
                   "created_at": time.time()}
        _nlu_audit_log(raw_cmd, parsed, 'clarified')
        _save_nlu_pending(chat_id, pending)
        _nlu_advance(token, chat_id, pending)
        return True
    pending = {"intent": intent, "slots": parsed.get('slots') or {},
               "awaiting": None, "candidates": [c.get('yt_id') for c in channels],
               "created_at": time.time()}
    _nlu_audit_log(raw_cmd, parsed, 'dispatched')
    _save_nlu_pending(chat_id, pending)
    _nlu_advance(token, chat_id, pending)
    return True


# ==========================================
# ⌨️ AUTO-SUGGEST PERINTAH TELEGRAM (setMyCommands)
# SATU sumber kebenaran untuk daftar perintah: teks /start DIBANGUN dari
# _BOT_COMMANDS (lihat _build_start_text) dan setMyCommands memakai daftar
# yang sama — jadi tidak ada perintah yang ketinggalan dan tidak ada
# perintah mati yang terdaftar. (2026-11-19: mengetik '/' di chat bot
# langsung memunculkan saran perintah + deskripsi singkatnya.)
# Batas Bot API: nama 1–32 karakter (huruf kecil/angka/_), deskripsi
# 3–256 karakter, maks 100 perintah.
# ==========================================
_BOT_COMMANDS = [
    # (perintah, deskripsi singkat, seksi di /start, contoh pemakaian)
    # Teks /start dibangun dari daftar ini: barisnya '👉 /{cmd} {pemakaian}
    # — {deskripsi}' — jadi daftar di pesan & di Telegram selalu sama.
    ('start', 'Buka menu utama bot', 'menu', ''),
    ('help', 'Buka menu bantuan', 'menu', ''),
    ('status', 'Channel, antrean, kuota & kesehatan server', 'pantau', ''),
    ('server', 'Detail mesin: CPU, RAM, disk, uptime, panel', 'pantau', ''),
    ('kuota', 'Kuota upload per project Google & per channel', 'pantau', ''),
    ('stok', 'Sisa stok jadwal & isi galeri per channel', 'pantau', ''),
    ('channel', 'Daftar channel YouTube terhubung', 'pantau', ''),
    ('antrean', 'Video yang sedang diproses', 'pantau', ''),
    ('jadwal', 'Jadwal publish 7 hari ke depan', 'pantau', ''),
    ('jadwalbulan', 'Jadwal 30 hari ke depan per channel', 'pantau', '[channel]'),
    ('riwayat', '8 upload terakhir', 'pantau', ''),
    ('gagal', 'Task gagal + tombol Retry', 'pantau', ''),
    ('laporan', 'Laporan harian (otomatis 07:00 & 17:00 WIB); tambah "detail" buat versi lengkap', 'pantau', ''),
    ('statistik', 'Views/likes/komentar + video terbaik & terburuk', 'hasil', ''),
    ('monetisasi', 'Estimasi penghasilan dari RPM manual', 'hasil', ''),
    ('judul', 'Judul SEO, mis. /judul lofi 15', 'konten', '[niche] [jumlah]'),
    ('deskripsi', 'Template deskripsi SEO', 'konten', '[niche]'),
    ('tag', 'Bank hashtag, mis. /tag piano', 'konten', '[niche]'),
    ('cta', 'Ide teks CTA buat Shorts', 'konten', ''),
    ('ideashorts', 'Hook + naskah singkat Shorts', 'konten', '[niche]'),
    ('blueprint', 'Resep settingan video', 'konten', ''),
    ('buatshort', 'Buat Shorts Muse terpandu — cuma 3 pertanyaan', 'buat', ''),
    ('buatlong', 'Buat batch video long 60–80 menit terpandu', 'buat', ''),
    ('playlist', 'Buat Playlist baru (bot bertanya satu per satu) atau /playlist status', 'buat', '[status]'),
    ('buatplaylist', 'Buat Musik Playlist (alias /playlist, sekali kirim pakai | )', 'buat', '<tema> | [channel] | [tema pixabay]'),
    ('zumo', 'Buat playlist lagu Zumo/Kie terpandu atau /zumo status', 'buat', '[status]'),
    ('zumobank', 'Generate massal lagu ke bank audio (tanpa upload)', 'buat', '[tema] | [jumlah]'),
    ('zumorakit', 'Rakit video dari lagu bank audio (acak)', 'buat', '[jumlah]'),
    ('batal', 'Batalkan draft yang sedang dibuat', 'buat', ''),
    ('perpanjang', 'Tambah jadwal, mis. /perpanjang Aruya 0089 30 atau /perpanjang semua 30', 'atur', '[channel] [hari]'),
    ('aturjadwal', 'Ubah jam tayang / jadwal auto-shorts channel (cuma 3 pertanyaan)', 'atur', '[channel]'),
    ('mode', 'Ganti mode, mis. /mode Aruya 0089 auto', 'atur', '[channel] [auto|approval]'),
    ('jadwalplaylist', 'Jadwal Playlist harian per channel (tombol on/off)', 'atur', ''),
    ('stopjadwal', 'Stop jadwal harian, mis. /stopjadwal Cozy Sax atau /stopjadwal semua', 'atur', '[channel|semua]'),
    ('mulaijadwal', 'Mulai jadwal harian, mis. /mulaijadwal Cozy Sax atau /mulaijadwal semua', 'atur', '[channel|semua]'),
    ('hariini', 'Upload hari ini per channel (berapa video di akun mana saja)', 'atur', ''),
    ('jeda', 'Jeda channel (jadwal & antrean berhenti)', 'atur', '[channel]'),
    ('lanjutkan', 'Lanjutkan channel yang dijeda', 'atur', '[channel]'),
    ('pixabaykey', 'Tambah API key Pixabay', 'atur', '<key>'),
    ('pixabaykeys', 'Daftar API key Pixabay (masked)', 'atur', ''),
    ('pixabaydel', 'Hapus API key Pixabay', 'atur', '<4-karakter-terakhir|nomor>'),
    ('zumomodel', 'Lihat/ganti model Zumo (V5.5/V6/V6_MINI/V6_WILD)', 'atur', '[model]'),
    ('kiekey', 'Tambah API key Kie.ai', 'atur', '<key>'),
    ('kiekeys', 'Daftar API key Kie.ai (masked)', 'atur', ''),
    ('kiedel', 'Hapus API key Kie.ai', 'atur', '<4-karakter-terakhir|nomor>'),
    ('backup', 'Backup sekarang + status backup terakhir', 'atur', ''),
    ('update', 'Update panel dari GitHub + restart', 'atur', ''),
    ('restart', 'Restart panel + bot Telegram', 'atur', ''),
]
_BOT_COMMAND_SECTIONS = [
    ('pantau', '📊 Pantau:'),
    ('hasil', '📈 Hasil:'),
    ('konten', '💡 Konten:'),
    ('buat', '🎬 Buat Video:'),
    ('atur', '⚙️ Atur Channel:'),
]
# Baris contoh pemakaian sekali-kirim (bukan perintah baru — hanya cara
# pakai /buatshort & /buatlong yang sama), disisipkan setelah baris
# perintahnya di /start.
_BOT_COMMAND_EXTRAS = {
    'buatshort': '👉 /buatshort Judul | Hook | Naskah | CTA — Sekali kirim',
    'buatlong': '👉 /buatlong 3 | 2026-11-20 19:00 | 1 — Sekali kirim (jumlah | tanggal | jeda hari)',
    'buatplaylist': '👉 /buatplaylist lagu malaysia lawas | Aruya 0089 | autumn — Sekali kirim',
    'zumo': '/zumo tema | jumlah | jadwal — Sekali kirim',
}


def _build_start_text():
    """Teks /start — dibangun dari _BOT_COMMANDS supaya daftar perintah di
    pesan ini SELALU sama dengan yang didaftarkan ke Telegram."""
    lines = [
        "🤖 *ZIQVA MUSE BOT — CLOUD OPERATOR* 😎",
        "",
        "Halo Bos! Saya siap kerja, tinggal perintah aja. Sentuh tombol di bawah atau klik perintah berikut:",
        "",
    ]
    for key, title in _BOT_COMMAND_SECTIONS:
        lines.append(f"*{title}*")
        for cmd, desc, section, usage in _BOT_COMMANDS:
            if section != key:
                continue
            lines.append(f"👉 /{cmd}" + (f" {usage}" if usage else "") + f" — {desc}")
            if cmd in _BOT_COMMAND_EXTRAS:
                lines.append(_BOT_COMMAND_EXTRAS[cmd])
        lines.append("")
    lines.append("Niche: " + ", ".join(_NICHES.keys()) + ".")
    lines.append("")
    lines.append("💬 *Bicara bebas juga bisa Bos!* Ketik perintah sehari-hari, mis. "
                 "'buatkan short tema anak untuk semua channel' atau 'jeda channel Barang Unick' — "
                 "saya terjemahkan maksud Bos, dan kalau ragu saya konfirmasi dulu 😌")
    return "\n".join(lines)


def register_bot_commands(token):
    """Daftarkan daftar perintah ke Telegram (setMyCommands) supaya mengetik
    '/' di chat bot langsung memunculkan auto-suggest. Idempotent —
    dipanggil tiap start operator. Gagal (token salah / network mati)
    TIDAK BOLEH menggagalkan startup: fail-silent, log saja."""
    try:
        res = requests.post(
            f"https://api.telegram.org/bot{token}/setMyCommands",
            json={"commands": [{"command": c, "description": d}
                              for c, d, _s, _u in _BOT_COMMANDS]},
            timeout=10,
        )
        data = res.json() if res is not None else {}
        if res.status_code == 200 and data.get('ok'):
            print(f"✅ Perintah Telegram terdaftar ({len(_BOT_COMMANDS)} perintah).", flush=True)
            return True
        print(f"⚠️ setMyCommands gagal (HTTP {res.status_code}): {data}", flush=True)
        return False
    except Exception as e:
        print(f"⚠️ setMyCommands gagal (network/token): {e}", flush=True)
        return False


# ==========================================
# 🔥 v1.2.20 — /aturjadwal: ubah pengaturan jadwal channel (MAKS 3 pertanyaan)
# Q1 channel → Q2 yang diubah → Q3 nilai baru → konfirmasi ✅/❌.
# Draft di monitor_state.json key 'aturjadwal_drafts' (TTL 30 menit, sama
# seperti draft buat video). TIDAK ADA file database baru — perubahan
# disimpan ke channels_db.json lewat endpoint operator yang SAMA dengan
# panel: /api/operator/set_channel_publish_time (jam tayang blueprint)
# dan /api/operator/set_channel_auto_shorts (jam/tema auto-shorts).
# ==========================================
_AJ_FIELDS = [
    ('publish_time', '🕖 Jam tayang blueprint'),
    ('as_create', '🌅 Jam buat auto-shorts'),
    ('as_publish', '🌇 Jam tayang auto-shorts'),
    ('as_theme', '🎨 Tema auto-shorts'),
]
_AJ_FIELD_LABEL = dict(_AJ_FIELDS)
_AJ_DRAFT_TTL = 1800  # 30 menit, sama seperti draft buat video


def _load_aturjadwal_draft(chat_id):
    try:
        state = load_monitor_state()
        d = (state.get('aturjadwal_drafts') or {}).get(str(chat_id))
        if not d:
            return None
        if time.time() - float(d.get('created_at', 0)) > _AJ_DRAFT_TTL:
            _clear_aturjadwal_draft(chat_id)
            return {"expired": True}
        return d
    except Exception:
        return None


def _save_aturjadwal_draft(chat_id, draft):
    try:
        state = load_monitor_state()
        state.setdefault('aturjadwal_drafts', {})[str(chat_id)] = draft
        save_monitor_state(state)
    except Exception as e:
        print(f"Gagal simpan aturjadwal_draft: {e}", flush=True)


def _clear_aturjadwal_draft(chat_id):
    try:
        state = load_monitor_state()
        (state.get('aturjadwal_drafts') or {}).pop(str(chat_id), None)
        save_monitor_state(state)
    except Exception:
        pass


def _aturjadwal_current(draft):
    """Nilai yang tersimpan saat ini untuk field yang sedang diubah
    (dipakai di Q3 + pesan konfirmasi)."""
    ch = next((c for c in load_channels() if c.get('yt_id') == draft.get('yt_id')), None)
    if not ch:
        return None
    f = draft.get('field')
    if f == 'publish_time':
        return ((ch.get('blueprint') or {}).get('publish_time')
                or ch.get('default_publish_time') or '19:00')
    cfg = ch.get('auto_shorts') or {}
    if f == 'as_create':
        return cfg.get('create_time')
    if f == 'as_publish':
        return cfg.get('publish_time')
    if f == 'as_theme':
        return cfg.get('theme')
    return None


def _aturjadwal_start(token, chat_id, yt_id=None):
    """Mulai alur /aturjadwal. Bila channel sudah disebut (argumen / NLU),
    langsung ke Q2 (pilih yang diubah) — tetap maks 3 pertanyaan."""
    chans = load_channels()
    if not chans:
        send_message(token, chat_id, "📺 Belum ada channel terhubung nih Bos 😌")
        return
    draft = {"step": "channel", "yt_id": yt_id, "field": None, "value": None,
             "created_at": time.time()}
    if yt_id and any(c.get('yt_id') == yt_id for c in chans):
        draft["step"] = "field"
        _save_aturjadwal_draft(chat_id, draft)
        _aturjadwal_ask_field(token, chat_id, draft)
        return
    _save_aturjadwal_draft(chat_id, draft)
    rows = [[{"text": _channel_display_name(c), "callback_data": f"aj:ch:{i}"}]
            for i, c in enumerate(chans)]
    send_message(token, chat_id, "⚙️ *Atur Jadwal* 😎\n\nChannel yang mana Bos?",
                 reply_markup={"inline_keyboard": rows})


def _aturjadwal_ask_field(token, chat_id, draft):
    ch = next((c for c in load_channels() if c.get('yt_id') == draft.get('yt_id')), None)
    name = _channel_display_name(ch) if ch else '?'
    rows = [[{"text": label, "callback_data": f"aj:field:{key}"}]
            for key, label in _AJ_FIELDS]
    send_message(token, chat_id,
                 f"⚙️ *Atur Jadwal — {_tg_escape(name)}*\n\nMau diubah yang mana Bos?",
                 reply_markup={"inline_keyboard": rows})


def _aturjadwal_ask_value(token, chat_id, draft):
    field = draft.get('field')
    if field == 'as_theme':
        rows = []
        keys = list(_NICHES.keys())
        for i in range(0, len(keys), 2):
            rows.append([{"text": _NICHES[k], "callback_data": f"aj:theme:{k}"}
                         for k in keys[i:i + 2]])
        send_message(token, chat_id, "🎨 *Tema auto-shorts yang baru?*",
                     reply_markup={"inline_keyboard": rows})
        return
    label = _AJ_FIELD_LABEL.get(field, 'Jam')
    cur = _aturjadwal_current(draft)
    send_message(token, chat_id,
                 f"🕖 *{label}* — jam barunya berapa Bos?\n"
                 f"(saat ini: `{_tg_escape(cur) if cur else '-'}` — balas mis. `19:00`)")
    if field == 'publish_time':
        send_message(token, chat_id,
                     "ℹ️ Jam ini dipakai /perpanjang untuk jam video-video berikutnya.")


def _aturjadwal_ask_confirm(token, chat_id, draft):
    ch = next((c for c in load_channels() if c.get('yt_id') == draft.get('yt_id')), None)
    name = _channel_display_name(ch) if ch else '?'
    label = _AJ_FIELD_LABEL.get(draft.get('field'), '?')
    value = draft.get('value')
    if draft.get('field') == 'as_theme':
        val_txt = f"*{_tg_escape(_NICHES.get(value, value))}*"
    else:
        val_txt = f"`{_tg_escape(value)}` WIB"
    cur = _aturjadwal_current(draft)
    send_message(token, chat_id,
                 f"⚙️ *Atur Jadwal — {_tg_escape(name)}*\n\n{label} diubah jadi {val_txt}?\n"
                 f"(sebelumnya: `{_tg_escape(cur) if cur else '-'}`)",
                 reply_markup={"inline_keyboard": [[
                     {"text": "✅ Ya, simpan", "callback_data": "aj:yes"},
                     {"text": "❌ Batal", "callback_data": "aj:no"},
                 ]]})


def _handle_aturjadwal_answer(token, chat_id, draft, raw_cmd):
    """Jawaban teks untuk alur /aturjadwal (langkah channel/field/nilai).
    Kembalikan True bila jawaban dipakai (draft maju / selesai)."""
    clean = _nlu_clean(raw_cmd)
    step = draft.get('step')
    if step == 'channel':
        matched, _all = _nlu_match_channels(clean, load_channels())
        if matched:
            draft['yt_id'] = matched[0].get('yt_id')
            draft['step'] = 'field'
            _save_aturjadwal_draft(chat_id, draft)
            _aturjadwal_ask_field(token, chat_id, draft)
            return True
        return False
    if step == 'field':
        keys = [k for k, _l in _AJ_FIELDS]
        m = re.match(r'^(\d+)$', clean)
        if m and 1 <= int(m.group(1)) <= len(keys):
            draft['field'] = keys[int(m.group(1)) - 1]
        elif 'tema' in clean:
            draft['field'] = 'as_theme'
        elif 'buat' in clean:
            draft['field'] = 'as_create'
        elif 'tayang' in clean:
            draft['field'] = 'as_publish' if ('auto' in clean or 'short' in clean) else 'publish_time'
        else:
            return False
        draft['step'] = 'value'
        _save_aturjadwal_draft(chat_id, draft)
        _aturjadwal_ask_value(token, chat_id, draft)
        return True
    if step == 'value':
        if draft.get('field') == 'as_theme':
            niche, _unk = _nlu_detect_niche(clean)
            if not niche:
                return False
            draft['value'] = niche
        else:
            m = re.search(r'(\d{1,2})[:.](\d{2})', raw_cmd)
            if not m:
                send_message(token, chat_id, "😅 Jamnya pakai format `HH:MM` ya Bos — mis. `19:00`.")
                return True
            hh, mm = int(m.group(1)), int(m.group(2))
            if not (0 <= hh <= 23 and 0 <= mm <= 59):
                send_message(token, chat_id, "😅 Jam tidak valid Bos — pakai 00:00 sampai 23:59 ya.")
                return True
            draft['value'] = f"{hh:02d}:{mm:02d}"
        draft['step'] = 'confirm'
        _save_aturjadwal_draft(chat_id, draft)
        _aturjadwal_ask_confirm(token, chat_id, draft)
        return True
    return False


def _aturjadwal_save(token, chat_id, draft):
    """Simpan perubahan via endpoint operator yang SAMA dengan panel."""
    yt_id = draft.get('yt_id')
    field, value = draft.get('field'), draft.get('value')
    if field == 'publish_time':
        ok, msg, _d = _operator_post('/api/operator/set_channel_publish_time',
                                    {'yt_id': yt_id, 'publish_time': value})
    else:
        key = {'as_create': 'create_time', 'as_publish': 'publish_time',
               'as_theme': 'theme'}.get(field)
        ok, msg, _d = _operator_post('/api/operator/set_channel_auto_shorts',
                                    {'yt_id': yt_id, 'auto_shorts': {key: value}})
    _clear_aturjadwal_draft(chat_id)
    ch = next((c for c in load_channels() if c.get('yt_id') == yt_id), None)
    name = _channel_display_name(ch) if ch else '?'
    if not ok:
        send_message(token, chat_id,
                     f"😅 *Gagal menyimpan Bos* — {_tg_escape(msg or 'panel tidak merespons')}")
        return
    if field == 'as_theme':
        val_txt = f"tema auto-shorts jadi *{_tg_escape(_NICHES.get(value, value))}*"
    else:
        val_txt = f"{_AJ_FIELD_LABEL.get(field, '?')} jadi `{_tg_escape(value)}` WIB"
    extra = ""
    if field in ('as_create', 'as_publish', 'as_theme'):
        cfg = (ch.get('auto_shorts') or {}) if ch else {}
        if not cfg.get('enabled'):
            extra = "\nℹ️ Auto-shorts channel ini masih *nonaktif* — aktifkan dari panel (Settings → JADWAL OTOMATIS SHORT) bila mau jalan otomatis."
    send_message(token, chat_id,
                 f"✅ *Berhasil Bos!* 😎\n{val_txt} untuk channel *{_tg_escape(name)}*.{extra}")


def _daily_playlist_keyboard(channels):
    """Keyboard toggle jadwal harian (v1.3.18): 2 channel per baris
    + baris Mulai Semua / Stop Semua."""
    rows = []
    row = []
    for ch in channels:
        name = _channel_display_name(ch)
        on = bool(ch.get('daily_playlist'))
        row.append({"text": f"{'✅' if on else '⏸️'} {name}",
                    "callback_data": f"dailypl:toggle:{ch.get('yt_id')}"})
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    rows.append([{"text": "▶️ Mulai Semua", "callback_data": "dailypl:all:on"},
                 {"text": "⏹️ Stop Semua", "callback_data": "dailypl:all:off"}])
    return {"inline_keyboard": rows}


def _daily_playlist_text(channels):
    lines = ["📅 *Jadwal Playlist Harian*",
             "━━━━━━━━━━━━━━━━━━━━━━"]
    for ch in channels:
        name = _channel_display_name(ch)
        on = bool(ch.get('daily_playlist'))
        lines.append(f"{'✅' if on else '⏸️'} *{_tg_escape(name)}* — {'AKTIF' if on else 'MATI'}")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("AKTIF = tiap pagi saya riset video yang lagi trend, buatkan 1 Playlist, langsung tayang.")
    lines.append("Channel jalur Zumo jangan diaktifkan — polanya beda.")
    lines.append("Pencet tombol buat nyalakan/matikan per channel, atau /stopjadwal semua buat berhentiin semua.")
    return "\n".join(lines)


def _send_daily_playlist_report(token, chat_id):
    channels = load_channels()
    if not channels:
        send_message(token, chat_id, "📅 *Jadwal Playlist Harian*\n\nBelum ada channel.")
        return
    send_message(token, chat_id, _daily_playlist_text(channels),
                 reply_markup=_daily_playlist_keyboard(channels))


def handle_user_command(token, chat_id, text, message_id=None):
    try:
        raw_cmd = text.strip()
        # Bersihkan emoji agar teks tombol tombol seperti "📊 Status Server" cocok ke "status server"
        clean_cmd = re.sub(r'[^\w\s/]', '', raw_cmd).strip().lower()
        parts = clean_cmd.split()
        first_word = parts[0] if parts else ""

        print(f"Perintah diproses: '{_log_safe_text(raw_cmd)}' -> clean: '{clean_cmd}'", flush=True)

        # --- 🎬 ALUR BUAT VIDEO (/buatshort & /buatlong): dicek PALING DULU
        # supaya jawaban langkah tidak ketangkap cabang lain (mis. kata
        # "sleep" di jawaban akan ketangkap /judul). /batal membatalkan
        # kapan saja; /start membuka menu (draft ikut dibatalkan); tombol
        # keyboard & perintah "/" lain tetap jalan, draft tersimpan.
        if first_word in ("/batal",) or clean_cmd in ("batal", "cancel"):
            _d0 = _load_make_draft(chat_id)
            _aj0 = _load_aturjadwal_draft(chat_id)
            if _d0:
                _clear_make_draft(chat_id)
                _retry = {'short': '/buatshort', 'long': '/buatlong',
                          'playlist': '/playlist', 'zumo': '/zumo',
                          'zumobank': '/zumobank',
                          'zumorakit': '/zumorakit'}.get(_d0.get('kind'), '/buatshort')
                _cancel_msg = {'zumobank': "Tidak ada lagu bank yang dibuat.",
                               'zumorakit': "Tidak ada video rakitan yang dibuat."}.get(
                    _d0.get('kind'), "Tidak ada video yang dibuat.")
                send_message(token, chat_id,
                             f"😌 *Sip Bos, dibatalkan.* {_cancel_msg}\nKetik {_retry} kalau mau mulai lagi ya Bos!")
            elif _aj0:
                # 🔥 v1.2.20: /batal juga membatalkan alur /aturjadwal.
                _clear_aturjadwal_draft(chat_id)
                send_message(token, chat_id,
                             "😌 *Sip Bos, dibatalkan.* Tidak ada pengaturan jadwal yang diubah.\nKetik /aturjadwal kalau mau mulai lagi ya Bos!")
            else:
                send_message(token, chat_id,
                             "😅 *Tidak ada yang sedang dibuat Bos* — ketik /buatshort, /buatlong, atau /playlist untuk mulai.")
            return
        _draft = _load_make_draft(chat_id)
        if isinstance(_draft, dict) and _draft.get("expired"):
            # 🔥 v1.3.21: draft basi HANYA dijawab bila pesan ini memang
            # jawaban untuk draft itu (teks bebas). Perintah "/" dan tombol
            # keyboard tetap jalan — draft basi sudah dihapus
            # _load_make_draft, jadi diam-diam saja. Sebelum ini, tap
            # 'Jadwal 7 Hari' malah dijawab pesan draft hangus.
            # 🔥 v1.3.30: TAPI pesan bebas bisa jadi PERINTAH BARU
            # ("jadwalkan ... jam 4"), bukan jawaban draft — coba NLU
            # dulu; bila NLU mengenalinya, jalankan; bila tidak, baru
            # beri tahu draft hangus (10:04 WIB 2026-11-20: perintah
            # jadwal sore Bos ditelan pesan hangus ini).
            if not first_word.startswith("/") and clean_cmd not in _MAIN_BUTTON_TEXTS:
                if _handle_nlu_message(token, chat_id, raw_cmd, clean_cmd):
                    return
                send_message(token, chat_id,
                             "⌛ *Eh Bos, draft buat videonya sudah hangus (>30 menit tidak ada kabar)* 😅\n"
                             "Ketik /buatshort atau /buatlong untuk mulai lagi ya Bos!")
                return
        if _draft:
            if first_word in ("/start", "/help"):
                _clear_make_draft(chat_id)
            elif first_word in ("/buatshort", "/buatlong", "/buatplaylist", "/zumobank", "/zumorakit") or clean_cmd in ("buat short", "buat video long", "buat playlist"):
                _clear_make_draft(chat_id)
                # jatuh ke cabang /buatshort|/buatlong|/buatplaylist|/zumobank|/zumorakit di bawah (mulai baru)
            elif first_word in ("/playlist",):
                # /playlist status tidak mengganggu draft; /playlist polos /
                # berteks memulai alur baru (draft lama dibersihkan).
                if raw_cmd[len('/playlist'):].strip().lower() != 'status':
                    _clear_make_draft(chat_id)
                # jatuh ke cabang /playlist di bawah (menu / alur baru)
            elif first_word in ("/zumo",):
                # /zumo status tidak mengganggu draft; /zumo polos /
                # berteks memulai alur baru (draft lama dibersihkan).
                if raw_cmd[len('/zumo'):].strip().lower() != 'status':
                    _clear_make_draft(chat_id)
                # jatuh ke cabang /zumo di bawah (menu / alur baru)
            elif first_word.startswith("/"):
                pass  # perintah lain tetap jalan, draft tersimpan
            elif clean_cmd in _MAIN_BUTTON_TEXTS:
                pass  # tombol keyboard tetap jalan, draft tersimpan
            else:
                _handle_make_answer(token, chat_id, _draft, raw_cmd)
                return

        # 🔥 v1.2.20 — DRAFT /aturjadwal: jawaban langkah ditangkap di sini
        # (pola yang sama seperti draft buat video di atas). Perintah "/"
        # lain & tombol keyboard tetap jalan, draft tersimpan.
        _aj = _load_aturjadwal_draft(chat_id)
        if isinstance(_aj, dict) and _aj.get("expired"):
            # 🔥 v1.3.21: sama seperti draft buat video — perintah "/"
            # dan tombol keyboard tetap jalan, draft basi dihapus diam-diam.
            # 🔥 v1.3.30: sama seperti di atas — perintah baru didahulukan.
            if not first_word.startswith("/") and clean_cmd not in _MAIN_BUTTON_TEXTS:
                if _handle_nlu_message(token, chat_id, raw_cmd, clean_cmd):
                    return
                send_message(token, chat_id,
                             "⌛ *Eh Bos, pengaturan jadwalnya sudah hangus (>30 menit tidak ada kabar)* 😅\n"
                             "Ketik /aturjadwal untuk mulai lagi ya Bos!")
                return
        if _aj:
            if first_word in ("/start", "/help"):
                _clear_aturjadwal_draft(chat_id)
            elif first_word in ("/aturjadwal",):
                _clear_aturjadwal_draft(chat_id)
                # jatuh ke cabang /aturjadwal di bawah (mulai baru)
            elif first_word.startswith("/"):
                pass  # perintah lain tetap jalan, draft tersimpan
            elif clean_cmd in _MAIN_BUTTON_TEXTS:
                pass  # tombol keyboard tetap jalan, draft tersimpan
            else:
                if _handle_aturjadwal_answer(token, chat_id, _aj, raw_cmd):
                    return
                # Bukan jawaban atas langkah ini → biarkan cabang lain
                # (NLU / perintah baca) yang menangani pesannya.

        # 1. MENU / START — teks dibangun dari _BOT_COMMANDS (satu sumber
        # dengan setMyCommands, supaya daftarnya tidak pernah beda).
        if first_word in ["/start", "/menu", "/help", "halo", "menu", "hai"]:
            # 🔥 v1.3.3: keyboard menu dipasang eksplisit di sini saja
            # (lengkap + berikon); pesan lain tidak membawa keyboard.
            send_message(token, chat_id, _build_start_text(),
                         reply_markup=get_main_keyboard())
            return

        # --- 🎬 BUAT VIDEO (alur terpandu) — dicek sebelum cabang lama
        # supaya "buat short" tidak ketangkap cabang /ideashorts ("shorts").
        if first_word in ("/buatshort",) or clean_cmd in ("buat short",):
            _start_make_short(token, chat_id, raw_cmd)
            return
        if first_word in ("/buatlong",) or clean_cmd in ("buat video long",):
            _start_make_long(token, chat_id, raw_cmd)
            return
        if first_word in ("/buatplaylist",) or clean_cmd in ("buat playlist",):
            _start_make_playlist(token, chat_id, raw_cmd)
            return

        # --- 📅 PERPANJANG / MODE / JEDA / LANJUTKAN (aksi, bukan baca saja).
        # Memakai mesin pending + klarifikasi NLU yang sama (tanya channel /
        # mode bila tidak disebut; konfirmasi bila beralih ke Full-Auto).
        if first_word in ("/perpanjang",):
            rest = raw_cmd[len('/perpanjang'):].strip()
            clean_rest = _nlu_clean(rest)
            matched, all_flag = _nlu_match_channels(clean_rest, load_channels())
            days = _nlu_extract_days(clean_rest)
            if not days:
                _m = re.search(r'(\d+)\s*$', clean_rest)
                if _m:
                    days = max(1, min(90, int(_m.group(1))))
            slots = {"days": days or 30}
            if all_flag:
                slots['channels'] = [c.get('yt_id') for c in load_channels()]
            elif matched:
                slots['channels'] = [c.get('yt_id') for c in matched]
            pending = {"intent": "perpanjang_jadwal", "slots": slots, "awaiting": None,
                       "candidates": [c.get('yt_id') for c in load_channels()],
                       "created_at": time.time()}
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return
        if first_word in ("/mode",):
            rest = raw_cmd[len('/mode'):].strip()
            clean_rest = _nlu_clean(rest)
            matched, all_flag = _nlu_match_channels(clean_rest, load_channels())
            slots = {}
            mode = _nlu_extract_mode(clean_rest)
            if mode:
                slots['mode'] = mode
            if all_flag:
                slots['channels'] = [c.get('yt_id') for c in load_channels()]
            elif matched:
                slots['channels'] = [c.get('yt_id') for c in matched]
            pending = {"intent": "ubah_mode", "slots": slots, "awaiting": None,
                       "candidates": [c.get('yt_id') for c in load_channels()],
                       "created_at": time.time()}
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return
        if first_word in ("/jeda", "/lanjutkan"):
            rest = raw_cmd[len(first_word):].strip()
            clean_rest = _nlu_clean(rest)
            matched, all_flag = _nlu_match_channels(clean_rest, load_channels())
            slots = {}
            if all_flag:
                slots['channels'] = [c.get('yt_id') for c in load_channels()]
            elif matched:
                slots['channels'] = [c.get('yt_id') for c in matched]
            pending = {"intent": "jeda_channel" if first_word == "/jeda" else "lanjutkan_channel",
                       "slots": slots, "awaiting": None,
                       "candidates": [c.get('yt_id') for c in load_channels()],
                       "created_at": time.time()}
            _save_nlu_pending(chat_id, pending)
            _nlu_advance(token, chat_id, pending)
            return

        # --- 📅 HARI INI (v1.3.29): ringkasan upload hari ini per channel. ---
        if first_word in ("/hariini",):
            send_message(token, chat_id, _build_today_summary_text(load_channels(), load_tasks().get('history', [])))
            return

        # --- 📅 JADWAL PLAYLIST HARIAN (v1.3.18): toggle per channel + stop/mulai. ---
        # /jadwalplaylist = status + tombol; /stopjadwal [channel|semua] =
        # matikan LANGSUNG tanpa konfirmasi (ini tombol darurat, misal
        # channel sudah monet); /mulaijadwal [channel|semua] = nyalakan.
        # Tanpa argumen channel → tampilkan laporan + tombol.
        if first_word in ("/jadwalplaylist",):
            _send_daily_playlist_report(token, chat_id)
            return
        if first_word in ("/stopjadwal", "/mulaijadwal"):
            enable = first_word == "/mulaijadwal"
            rest = raw_cmd[len(first_word):].strip()
            clean_rest = _nlu_clean(rest)
            matched, all_flag = _nlu_match_channels(clean_rest, load_channels())
            if all_flag:
                targets = load_channels()
            elif matched:
                targets = matched
            else:
                _send_daily_playlist_report(token, chat_id)
                return
            done = []
            failed = []
            for ch in targets:
                ok, info, _d = request_set_daily_playlist(ch.get('yt_id'), enable)
                if ok:
                    done.append(_channel_display_name(ch))
                else:
                    failed.append(f"{_channel_display_name(ch)}: {info}")
            verb = "nyala" if enable else "berhenti"
            lines = [f"{'▶️' if enable else '⏹️'} *Jadwal harian {verb}, Bos*"]
            if done:
                lines.append("✅ " + ", ".join(_tg_escape(n) for n in done))
            if failed:
                lines.append("⚠️ Gagal: " + "; ".join(_tg_escape(f) for f in failed))
            lines.append("Cek status: /jadwalplaylist")
            send_message(token, chat_id, "\n".join(lines))
            return

        # --- ⚙️ ATUR JADWAL (v1.2.20): ubah jam tayang / auto-shorts,
        # maks 3 pertanyaan + konfirmasi. Argumen channel opsional.
        if first_word in ("/aturjadwal",):
            rest = raw_cmd[len('/aturjadwal'):].strip()
            clean_rest = _nlu_clean(rest)
            matched, _all = _nlu_match_channels(clean_rest, load_channels())
            yt = matched[0].get('yt_id') if matched else None
            _aturjadwal_start(token, chat_id, yt)
            return

        # --- 🎵 MUSIK PLAYLIST: /playlist (pintu masuk utama, konversasional) ---
        # /playlist polos → menu (Buat Baru / Lihat Job). /playlist status →
        # daftar job (perilaku lama). /playlist <teks bebas> → mulai alur
        # tanya-jawab, teksnya dipakai sebagai tema awal.
        if first_word in ("/playlist",):
            rest = raw_cmd[len('/playlist'):].strip()
            if rest.lower() == 'status':
                _send_playlist_jobs_message(token, chat_id)
                return
            if rest:
                # Buang awalan perintah bila ada ("buatkan playlist ...").
                rest2 = re.sub(r'^(buatkan|bikin|buat)\s+(musik\s+)?playlist\s*',
                               '', rest, flags=re.I).strip()
                _start_make_playlist(token, chat_id,
                                     '/buatplaylist ' + rest2 if rest2 else '/buatplaylist')
                return
            send_message(token, chat_id,
                         "🎵 *Musik Playlist* 😎\n\nMau apa Bos?",
                         reply_markup={"inline_keyboard": [
                             [{"text": "🎵 Buat Playlist Baru", "callback_data": "pl:new"}],
                             [{"text": "📋 Lihat Job Terakhir", "callback_data": "pl:jobs"}],
                         ]})
            return

        # --- 🎵 ZUMO / KIE.AI: /zumo (menu / status / alur terpandu) ---
        # /zumo polos → menu (Buat Baru / Lihat Job). /zumo status →
        # saldo + model + job. /zumo <teks bebas> → mulai alur
        # tanya-jawab, teksnya dipakai sebagai tema awal.
        if first_word in ("/zumo",):
            rest = raw_cmd[len('/zumo'):].strip()
            if rest.lower() == 'status':
                _send_zumo_jobs_message(token, chat_id)
                return
            if rest:
                _start_make_zumo(token, chat_id, raw_cmd)
                return
            send_message(token, chat_id,
                         "Zumo — Lagu Orisinal (Kie.ai)\n\nBuat playlist lagu orisinal. Mau apa Bos?",
                         reply_markup={"inline_keyboard": [
                             [{"text": "Buat Playlist Zumo Baru", "callback_data": "zm:new"}],
                             [{"text": "Lihat Job Zumo", "callback_data": "zm:jobs"}],
                         ]})
            return

        # --- 🎵 ZUMO: /zumobank (generate massal ke bank, tanpa upload) ---
        if first_word in ("/zumobank",):
            _start_make_zumobank(token, chat_id, raw_cmd)
            return

        # --- 🎵 ZUMO: /zumorakit (rakit video dari lagu bank, acak) ---
        if first_word in ("/zumorakit",):
            _start_make_zumorakit(token, chat_id, raw_cmd)
            return

        # --- 🎵 ZUMO: /zumomodel (lihat / ganti model generate) ---
        if first_word in ("/zumomodel",):
            arg = raw_cmd[len('/zumomodel'):].strip().upper()
            if not arg:
                ok, msg, data = _operator_post('/api/operator/zumo_status', {})
                if not ok or not isinstance(data, dict):
                    send_message(token, chat_id, "Zumo — Model\n" + _tg_escape(msg))
                    return
                models = data.get('models') or list(_ZUMO_MODELS)
                rows = [[{"text": m, "callback_data": f"zmmodel:{m}"}] for m in models]
                send_message(token, chat_id,
                             f"Zumo — Model\n\nModel aktif: {_tg_escape(data.get('model') or _ZUMO_DEFAULT_MODEL)}\nPilih model baru di bawah, atau ketik `/zumomodel V6`.",
                             reply_markup={"inline_keyboard": rows})
                return
            if arg not in _ZUMO_MODELS:
                send_message(token, chat_id,
                             "Zumo — Model\n\nModel tidak dikenal Bos. Pilihan: " + ", ".join(_ZUMO_MODELS) + ".\nContoh: `/zumomodel V6`")
                return
            ok, msg, data = _operator_post('/api/operator/zumo_model', {'model': arg})
            send_message(token, chat_id, "Zumo — Model\n" + _tg_escape(msg))
            return

        # --- 💾 BACKUP OTOMATIS: /backup (buat + status) ---
        # Backup TIDAK PERNAH dikirim ke Telegram (berisi token) — hanya status.
        if first_word in ("/backup",) or clean_cmd == "backup":
            ok, msg, _d = _operator_post('/api/operator/backup_create', {}, timeout=10)
            if not ok:
                ok2, _m2, st = _operator_post('/api/operator/backup_status', {}, timeout=10)
                if ok2:
                    last = (st or {}).get('last') or {}
                    lines = ["💾 *Backup Otomatis*", "━━━━━━━━━━━━━━━━━━━━━━"]
                    if last:
                        lines.append(f"Terakhir: `{_tg_escape(last.get('filename', '?'))}`")
                        lines.append(f"{_tg_escape(last.get('created_at', '?'))} • {_tg_escape(last.get('size_human', '?'))} • {'terenkripsi ✅' if last.get('encrypted') else '⚠️ TANPA enkripsi'}")
                    else:
                        lines.append("Belum pernah backup.")
                    lines.append(f"Jadwal harian: {'aktif' if (st or {}).get('enabled') else 'nonaktif'} jam {(st or {}).get('hour', 3):02d}:00 WIB.")
                    lines.append(f"Catatan: {_tg_escape(msg)}")
                    send_message(token, chat_id, "\n".join(lines))
                else:
                    send_message(token, chat_id, f"💾 *Backup*\n{_tg_escape(msg)}")
                return
            # Backup jalan di latar — polling status sampai ~20 detik.
            done = None
            for _ in range(10):
                time.sleep(2)
                ok2, _m2, st = _operator_post('/api/operator/backup_status', {}, timeout=10)
                if ok2 and not (st or {}).get('running'):
                    done = (st or {}).get('last') or {}
                    break
            if done:
                send_message(token, chat_id,
                             f"💾 *Backup selesai, Bos* 😎\n\n`{_tg_escape(done.get('filename', '?'))}` ({_tg_escape(done.get('size_human', '?'))}, {done.get('files_count', '?')} file, {'terenkripsi ✅' if done.get('encrypted') else '⚠️ TANPA enkripsi'})\nIsi: DB + config + galeri{' (video di-skip: galeri >2 GB)' if done.get('media_skipped') else ''}.")
            else:
                send_message(token, chat_id,
                             "💾 *Backup sedang dibuat, Bos* ⏳\n\nKetik /backup lagi sebentar lagi buat lihat hasilnya.")
            return

        # --- 🔄 UPDATE + RESTART LEWAT TELEGRAM (v1.2.17): /update & /restart ---
        # Konfirmasi inline dulu (aksi restart/update); eksekusi di
        # callback 'upd:yes' / 'rst:yes'. Panel yang menjalankan
        # update/restart (endpoint operator di app.py) — operator hanya
        # memanggil, supaya urutan restart tetap benar (v1.2.11).
        if first_word in ("/update",):
            send_message(token, chat_id,
                         "🔄 *Update Panel* 😎\n\n"
                         "Update mengambil kode terbaru dari GitHub lalu restart panel + bot Telegram.\n"
                         "⚠️ Kalau ada video yang sedang dirender/di-upload, update ditolak dulu sampai render selesai.\n"
                         "Setelah selesai saya kabari lagi dengan versi barunya.\n\n"
                         "Lanjut update, Bos?",
                         reply_markup={"inline_keyboard": [[
                             {"text": "✅ Ya, Update", "callback_data": "upd:yes"},
                             {"text": "❌ Batal", "callback_data": "upd:no"},
                         ]]})
            return
        if first_word in ("/restart",):
            send_message(token, chat_id,
                         "🔄 *Restart Panel + Bot* 😎\n\n"
                         "Panel dan bot Telegram akan mati sebentar lalu hidup lagi (~10–30 detik).\n"
                         "⚠️ Kalau ada video yang sedang dirender/di-upload, restart ditolak dulu sampai render selesai.\n\n"
                         "Lanjut restart, Bos?",
                         reply_markup={"inline_keyboard": [[
                             {"text": "✅ Ya, Restart", "callback_data": "rst:yes"},
                             {"text": "❌ Batal", "callback_data": "rst:no"},
                         ]]})
            return

        # --- 🔑 API KEY PIXABAY: /pixabaykey, /pixabaykeys, /pixabaydel ---
        # Key TIDAK PERNAH ditampilkan utuh kembali (di chat, log, status).
        if first_word in ("/pixabaykey",):
            new_key = (raw_cmd[len(first_word):].strip() if raw_cmd else '')
            if not new_key:
                send_message(token, chat_id,
                             "🔑 *Tambah API Key Pixabay*\nPakai: `/pixabaykey <key>`\nKey gratis — daftar di pixabay.com/api. Key tidak akan ditampilkan lagi setelah disimpan.")
                return
            ok, msg, data = _operator_post('/api/operator/pixabay_key_add', {'pixabay_key': new_key})
            send_message(token, chat_id, f"🔑 *API Key Pixabay*\n{_tg_escape(msg)}")
            return
        if first_word in ("/pixabaykeys",):
            ok, msg, data = _operator_post('/api/operator/pixabay_key_list', {})
            if not ok or not isinstance(data, dict):
                send_message(token, chat_id, f"🔑 *API Key Pixabay*\n{_tg_escape(msg)}")
                return
            keys = data.get('keys') or []
            if not keys:
                send_message(token, chat_id,
                             "🔑 *API Key Pixabay*\nBelum ada key. Tambah dengan `/pixabaykey <key>` (gratis, daftar di pixabay.com/api).")
                return
            lines = ["🔑 *API Key Pixabay* 😎", "━━━━━━━━━━━━━━━━━━━━━━"]
            for k in keys:
                flag = " (nonaktif — pernah ditolak)" if k.get('disabled') else ""
                # masked dari panel ('••••' + 4 karakter terakhir, hanya
                # bullet + alfanumerik) — aman di dalam backtick, tanpa escape
                # (escape di dalam backtick justru menampilkan backslash).
                lines.append(f"• `{k.get('index', '?')}. {k.get('masked', '')}`{flag}")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("Hapus: `/pixabaydel <4-karakter-terakhir>` atau `/pixabaydel <nomor>`.")
            send_message(token, chat_id, "\n".join(lines))
            return
        if first_word in ("/pixabaydel",):
            ident = (raw_cmd[len(first_word):].strip() if raw_cmd else '')
            if not ident:
                send_message(token, chat_id,
                             "🔑 *Hapus API Key Pixabay*\nPakai: `/pixabaydel <4-karakter-terakhir>` atau `/pixabaydel <nomor>` — lihat daftar di `/pixabaykeys`.")
                return
            ok, msg, data = _operator_post('/api/operator/pixabay_key_del', {'ident': ident})
            send_message(token, chat_id, f"🔑 *API Key Pixabay*\n{_tg_escape(msg)}")
            return

        # --- 🔑 API KEY KIE.AI: /kiekey, /kiekeys, /kiedel ---
        # Pola sama persis seperti Pixabay di atas. Key TIDAK PERNAH
        # ditampilkan utuh kembali (di chat, log, status).
        if first_word in ("/kiekey",):
            new_key = (raw_cmd[len(first_word):].strip() if raw_cmd else '')
            if not new_key:
                send_message(token, chat_id,
                             "Tambah API Key Kie.ai\nPakai: `/kiekey <key>`\nKey Kie.ai — daftar di kie.ai (menu API Keys). Key tidak akan ditampilkan lagi setelah disimpan.")
                return
            ok, msg, data = _operator_post('/api/operator/kie_key_add', {'kie_key': new_key})
            send_message(token, chat_id, f"API Key Kie.ai\n{_tg_escape(msg)}")
            return
        if first_word in ("/kiekeys",):
            ok, msg, data = _operator_post('/api/operator/kie_key_list', {})
            if not ok or not isinstance(data, dict):
                send_message(token, chat_id, f"API Key Kie.ai\n{_tg_escape(msg)}")
                return
            keys = data.get('keys') or []
            if not keys:
                send_message(token, chat_id,
                             "API Key Kie.ai\nBelum ada key. Tambah dengan `/kiekey <key>` (daftar di kie.ai, menu API Keys).")
                return
            lines = ["API Key Kie.ai", "━━━━━━━━━━━━━━━━━━━━━━"]
            for k in keys:
                flag = " (nonaktif — pernah ditolak)" if k.get('disabled') else ""
                bal = k.get('balance')
                if isinstance(bal, (int, float)):
                    bal_txt = f" — {bal:g} kredit"
                elif not k.get('disabled'):
                    bal_txt = " — saldo tidak terbaca"
                else:
                    bal_txt = ""
                # masked dari panel ('••••' + 4 karakter terakhir, hanya
                # bullet + alfanumerik) — aman di dalam backtick, tanpa escape
                # (escape di dalam backtick justru menampilkan backslash).
                lines.append(f"• `{k.get('index', '?')}. {k.get('masked', '')}`{flag}{bal_txt}")
            if any(k.get('disabled') for k in keys):
                lines.append("Key nonaktif aktif lagi otomatis 24 jam setelah ditolak.")
            lines.append("━━━━━━━━━━━━━━━━━━━━━━")
            lines.append("Hapus: `/kiedel <4-karakter-terakhir>` atau `/kiedel <nomor>`.")
            send_message(token, chat_id, "\n".join(lines))
            return
        if first_word in ("/kiedel",):
            ident = (raw_cmd[len(first_word):].strip() if raw_cmd else '')
            if not ident:
                send_message(token, chat_id,
                             "Hapus API Key Kie.ai\nPakai: `/kiedel <4-karakter-terakhir>` atau `/kiedel <nomor>` — lihat daftar di `/kiekeys`.")
                return
            ok, msg, data = _operator_post('/api/operator/kie_key_del', {'ident': ident})
            send_message(token, chat_id, f"API Key Kie.ai\n{_tg_escape(msg)}")
            return

        # --- 🧠 NLU BAHASA MANUSIA: pesan bebas (bukan perintah "/" dan
        # bukan tombol keyboard) diterjemahkan dulu; bila dikenali →
        # ditangani di sini. Bila tidak dikenali → jatuh ke cabang lama
        # di bawah (kompatibilitas perintah lama tetap utuh).
        if not first_word.startswith('/') and clean_cmd not in _MAIN_BUTTON_TEXTS:
            if _handle_nlu_message(token, chat_id, raw_cmd, clean_cmd):
                return

        # --- PERINTAH BARU (baca saja) — dicek dulu supaya tidak ketangkap
        # cabang lama: "server" ketangkap /status, "ide shorts" ketangkap /judul.
        # 8. SERVER DETAIL (laporan mesin lengkap, bukan /status)
        if first_word in ("/server",) or clean_cmd in ("server", "server detail", "detail server"):
            send_message(token, chat_id, build_server_report())
            return

        # 9. KUOTA UPLOAD
        if "kuota" in clean_cmd:
            send_message(token, chat_id, build_quota_report())
            return

        # 10. STOK JADWAL + GALERI
        if "stok" in clean_cmd:
            send_message(token, chat_id, build_stock_report())
            return

        # 11. RIWAYAT UPLOAD
        if "riwayat" in clean_cmd or "history" in clean_cmd:
            send_message(token, chat_id, build_history_report())
            return

        # 12. STATISTIK VIDEO
        if "statistik" in clean_cmd or "stats" in clean_cmd:
            send_message(token, chat_id, build_stats_report())
            return

        # 13. MONETISASI (estimasi RPM manual)
        if ("monetisasi" in clean_cmd or "penghasilan" in clean_cmd
                or "rpm" in clean_cmd or first_word in ("/uang",)):
            send_message(token, chat_id, build_monetization_report())
            return

        # 13b. LAPORAN DETAIL (v1.2.20): /laporan detail — dicek SEBELUM
        # laporan biasa (14) karena mengandung kata "laporan".
        if (("laporan" in clean_cmd or "report" in clean_cmd)
                and ("detail" in clean_cmd or "lengkap" in clean_cmd
                     or "rinci" in clean_cmd or "terperinci" in clean_cmd)):
            _text, _snap = build_detail_report()
            _res = send_message(token, chat_id, _text)
            if _res and _res.get('ok'):
                _update_report_snapshot(_snap)
            return

        # 14. LAPORAN HARIAN (on-demand, pakai builder yang sudah ada)
        if "laporan" in clean_cmd or "report" in clean_cmd:
            _text, _snap = build_daily_report("ondemand")
            _res = send_message(token, chat_id, _text)
            if _res and _res.get('ok'):
                _update_report_snapshot(_snap)
            return

        # 15. TASK GAGAL + tombol inline Retry
        if "gagal" in clean_cmd:
            fail_msg, fail_markup = build_failed_report()
            send_message(token, chat_id, fail_msg, reply_markup=fail_markup)
            return

        # 16. BANK HASHTAG
        if ("hashtag" in clean_cmd or first_word in ("/tag", "/hashtag", "tagar")
                or clean_cmd.startswith("tag ") or clean_cmd.startswith("tagar ")):
            niche_key, _label = _parse_niche_arg(clean_cmd, parts)
            send_message(token, chat_id, build_hashtag_report(niche_key))
            return

        # 17. IDE CTA SHORTS
        if first_word in ("/cta",) or clean_cmd.startswith("cta"):
            send_message(token, chat_id, build_cta_report())
            return

        # 18. IDE SHORTS (hook + naskah singkat)
        if ("ideashorts" in clean_cmd or "ide shorts" in clean_cmd
                or "shorts" in clean_cmd):
            niche_key, _label = _parse_niche_arg(clean_cmd, parts)
            send_message(token, chat_id, build_shorts_ideas_report(niche_key))
            return

        # 2. STATUS SERVER (FASE 1 MONITORING: channel + kuota + upload hari ini)
        if "status" in clean_cmd or "server" in clean_cmd:
            send_message(token, chat_id, build_status_report())
            return

        # 3. CHANNEL LIST (🔥 v1.2.20: tampilan database channel yang rapi —
        # nama, subs, videos, mode, dijeda, stok jadwal, progres monetisasi;
        # builder yang sama dengan NLU 'list channel').
        if "channel" in clean_cmd:
            send_message(token, chat_id, build_channel_list_report())
            return

        # 4. ANTREAN RENDER
        if "antrean" in clean_cmd or "queue" in clean_cmd:
            tasks_data = load_tasks()
            active = tasks_data.get('active', [])
            if not active:
                msg = "📂 *Antrean lagi kosong Bos* 😌\nBelum ada tugas render yang dijadwalkan."
            else:
                msg = f"📂 *Antrean Render Bos ({len(active)})*\n━━━━━━━━━━━━━━━━━━━━━━\n"
                for idx, t in enumerate(active[:8], 1):
                    title = t.get('title', 'Video Kompilasi')
                    status = t.get('status', 'Menunggu')
                    _dt = _parse_schedule_dt(t.get('time') or (t.get('blueprint') or {}).get('publish_date') or '')
                    sched = _fmt_dt_short(_dt) if _dt else (t.get('schedule_date') or '-')[:16] or '—'
                    # 🔥 v1.2.18: tampilkan persen progres task yang sedang jalan.
                    prog = _task_progress_line(t)
                    # 🔥 v1.2.22: satu baris ringkas — judul | views | tanggal | durasi.
                    msg += f"*{idx}. {_video_meta(title, None, sched, _task_duration_sec(t))}*\nStatus: `{_tg_escape(status)}`"
                    if prog:
                        msg += f"\n{prog}"
                    msg += "\n\n"
                if len(active) > 8:
                    msg += f"_...dan {len(active)-8} video lainnya dalam antrean._"
            send_message(token, chat_id, msg)
            return

        # 4a. JADWAL 30 HARI (v1.2.20): /jadwalbulan [channel] — dicek
        # SEBELUM /jadwal (4b) karena "jadwalbulan" mengandung kata "jadwal".
        if first_word in ("/jadwalbulan",):
            rest = raw_cmd[len('/jadwalbulan'):].strip()
            for blk in build_month_schedule_blocks(channel_arg=rest or None):
                for chunk in _split_long_message(blk):
                    send_message(token, chat_id, chunk)
            return

        # 4b. JADWAL PUBLISH (FASE 3)
        if "jadwal" in clean_cmd or "schedule" in clean_cmd:
            _jadwal_txt, _jadwal_total = build_schedule_report()
            send_message(token, chat_id, _jadwal_txt,
                         reply_markup=_EMPTY_SCHEDULE_KB if _jadwal_total == 0 else None)
            return

        # 5. GENERATOR JUDUL (niche + jumlah, mis. "/judul lofi 15")
        if "judul" in clean_cmd or "ide" in clean_cmd or "sleep" in clean_cmd or "titles" in clean_cmd:
            niche_key, niche = _parse_niche_arg(clean_cmd, parts)
            count = _parse_count(parts, default=10, max_count=20)

            titles = generate_seo_titles(niche, count)
            title_list = "\n\n".join([f"*{i+1}.* {t}" for i, t in enumerate(titles)])
            
            msg = (
                f"💡 *Bos, ini {count} judul buat Bos* 😎\n"
                f"🎯 *Target Global:* `{niche}`\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{title_list}\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📋 Salin aja judulnya ke kolom *Bank Judul* di panel ya Bos!"
            )
            send_message(token, chat_id, msg)
            return

        # 6. TEMPLATE DESKRIPSI (varian per niche, mis. "/deskripsi piano")
        if "deskripsi" in clean_cmd or "description" in clean_cmd:
            niche_key, niche = _parse_niche_arg(clean_cmd, parts)
            send_message(token, chat_id, build_description_report(niche_key, niche))
            return

        # 7. BLUEPRINT RESEP
        if "blueprint" in clean_cmd or "resep" in clean_cmd:
            msg = (
                "📐 *Resep standar kita Bos:*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "• *Durasi:* Acak 60 – 80 menit (optimal untuk retensi dan algoritma)\n"
                "• *Preset Render:* ultrafast (hemat CPU server VPS)\n"
                "• *Visualizer:* Waveform di bawah (pos_y 85%, opacity 100%)\n"
                "• *Ending Special Song:* 1 lagu penutup di 2 menit terakhir\n"
                "• *Batas Deskripsi YouTube:* Maksimal 5.000 karakter\n"
                "• *Kapasitas Upload:* ~100 video per hari per project Google Cloud"
            )
            send_message(token, chat_id, msg)
            return

        # DEFAULT FALLBACK — did-you-mean (NLU sudah mencoba menerjemahkan
        # dan tidak mengenali maksudnya; sarankan perintah terdekat).
        # 🔥 v1.2.17 JEMBATAN: chat bebas (bukan '/') diteruskan ke Muse —
        # SATU ack singkat ini MENGGANTIKAN pesan "nggak ngerti" (bukan
        # menambahnya). Perintah '/' tetap balasan did-you-mean lama.
        sug = _nlu_suggest(clean_cmd)
        if sug:
            sug_txt = "Mungkin maksud Bos: " + " atau ".join(sug) + "?"
        else:
            sug_txt = "Coba sentuh tombol di bawah atau ketik /start buat lihat semua perintah ya Bos!"
        msg = (
            f"🤖 *Eh Bos, pesan \"{_tg_escape(raw_cmd)}\" saya terima tapi saya nggak ngerti maksudnya* 😅\n\n"
            f"{sug_txt}\n"
            "Perintah cepat: /status /server /kuota /stok /statistik /monetisasi /judul /ideashorts /buatshort /buatlong /perpanjang /mode /jeda /lanjutkan\n"
            "Atau bicara bebas saja, mis. 'buatkan short tema anak untuk semua channel' — saya terjemahkan maksud Bos 😌"
        )
        if raw_cmd.startswith('/'):
            send_message(token, chat_id, msg)
        elif _bridge_forward(token, chat_id, message_id, raw_cmd):
            send_message(token, chat_id,
                         "📩 *Siap Bos, diteruskan ke Muse — saya balas sebentar lagi ya* 😌")
        else:
            send_message(token, chat_id, msg)

    except Exception as e:
        print(f"Error di handle_user_command: {e}", flush=True)
        send_message(token, chat_id, f"⚠️ *Yah Bos, ada kendala nih* 😅: `{_tg_escape(e)}`")

def watchdog_background_loop(token, chat_id):
    print("🛡️ [Ziqva Watchdog] Lapis 1 pengawas aktif...", flush=True)
    last_alert_time = 0
    while True:
        try:
            time.sleep(300)
            tasks_data = load_tasks()
            active = tasks_data.get('active', [])
            failed = [t for t in active if 'Gagal' in t.get('status', '') or 'Error' in t.get('status', '')]
            now = time.time()
            if failed and (now - last_alert_time > 3600):
                msg = (
                    "🚨 *Bos, ada kendala nih* 😅\n\n"
                    f"`{len(failed)}` tugas render bermasalah!\n"
                    f"Contoh: `{_tg_escape(failed[0].get('title', 'Unknown'))}` — {_tg_escape(failed[0].get('status', ''))}\n\n"
                    "Coba cek panel ya Bos, saya juga lagi pantau 👀"
                )
                send_message(token, chat_id, msg)
                last_alert_time = now

            # 🔥 FASE 1 MONITORING: alert stok jadwal menipis/habis.
            # Peringatan H-1 (≤24 jam) sudah dikirim app.py (telegram_reminder_worker,
            # sekali per batch) — di sini TIDAK diulang supaya tidak dobel.
            # Operator hanya mengingatkan 2 kondisi yang TIDAK ditangani app.py:
            #  (a) stok sudah HABIS (jadwal terakhir lewat) — channel berhenti total;
            #  (b) stok tinggal ≤2 hari — peringatan dini sebelum masuk H-1.
            # Maksimal 1 pesan per channel per kondisi per hari (dedupe monitor_state).
            try:
                state = load_monitor_state()
                for ch in load_channels():
                    stock = get_stock_days(ch)
                    if stock is None:
                        continue
                    name = _channel_display_name(ch)
                    yt_id = ch.get('yt_id', name)
                    last_sched = ch.get('last_scheduled_date', '-')
                    if stock < 0:
                        key = f"stock_empty:{yt_id}"
                        if not _alerted_today(state, key):
                            send_message(
                                token, chat_id,
                                f"⛔ *Bos, stok jadwal {_tg_escape(name)} HABIS* 😅\n\n"
                                f"Channel: *{_tg_escape(name)}*\n"
                                f"Jadwal terakhir: `{_tg_escape(last_sched)}` (sudah lewat)\n\n"
                                "Channel ini berhenti dulu sampai batch baru dibuat — "
                                "buatin batch baru ya Bos, dari panel (/factory → Batch Creator) 🙏")
                            _mark_alerted(state, key)
                    elif stock <= 2:
                        key = f"stock_low:{yt_id}"
                        if not _alerted_today(state, key):
                            send_message(
                                token, chat_id,
                                f"⚠️ *Bos, stok {_tg_escape(name)} menipis nih* 😌\n\n"
                                f"Channel: *{_tg_escape(name)}*\n"
                                f"Jadwal terakhir: `{_tg_escape(last_sched)}` (sisa ~{int(stock)} hari)\n\n"
                                "Buatin batch baru ya Bos, biar channel-nya nggak kehabisan video 🙏")
                            _mark_alerted(state, key)
            except Exception as e:
                print(f"Watchdog stock-alert exception: {e}", flush=True)
            # 🔥 v1.2.16: pengingat approval — video yang jam tayangnya
            # sudah dekat / sudah lewat tapi masih menunggu ✅ Bos.
            try:
                _approval_reminder_check(token, chat_id)
            except Exception as e:
                print(f"Watchdog approval-reminder exception: {e}", flush=True)
            # 🔥 v1.2.17: fallback basi jembatan — chat yang diteruskan ke
            # Muse tapi belum dibalas >10 menit → 1 pesan jujur + failed.
            try:
                _bridge_stale_check(token, chat_id)
            except Exception as e:
                print(f"Watchdog bridge-stale exception: {e}", flush=True)
        except Exception as e:
            print(f"Watchdog exception: {e}", flush=True)
            time.sleep(60)

def daily_report_loop(token, chat_id):
    """🔥 FASE 1 MONITORING: laporan harian otomatis 07:00 WIB.
    Pola sama seperti watchdog (thread + sleep), tanpa cron sistem atau
    dependensi baru. Cek tiap 60 detik; kirim sekali sehari (dedupe tanggal
    di monitor_state.json — tahan restart, tidak dobel kirim). Snapshot
    delta disimpan hanya bila pesan benar-benar terkirim (res 'ok')."""
    print("🌅 [Ziqva Daily Report] Pengawas laporan harian aktif (07:00 WIB)...", flush=True)
    while True:
        try:
            time.sleep(60)
            now_wib = _wib_now()
            if now_wib.hour != DAILY_REPORT_HOUR_WIB:
                continue
            state = load_monitor_state()
            today = _wib_today_str()
            if state.get("daily_report_date") == today:
                continue
            text, snap = build_daily_report("pagi")
            res = send_message(token, chat_id, text)
            if res and res.get('ok'):
                _update_report_snapshot(snap)
                state = load_monitor_state()
                state["daily_report_date"] = today
                save_monitor_state(state)
                print(f"🌅 Laporan harian {today} terkirim.", flush=True)
            else:
                print("🌅 Laporan harian gagal terkirim — coba lagi menit berikutnya.", flush=True)
        except Exception as e:
            print(f"Daily report exception: {e}", flush=True)
            time.sleep(60)


def evening_report_loop(token, chat_id):
    """Laporan sore otomatis 17:00 WIB — pola sama persis seperti laporan
    pagi (thread + sleep 60 detik, dedupe tanggal di monitor_state.json,
    snapshot delta disimpan hanya bila pesan benar-benar terkirim)."""
    print("🌆 [Ziqva Evening Report] Pengawas laporan sore aktif (17:00 WIB)...", flush=True)
    while True:
        try:
            time.sleep(60)
            now_wib = _wib_now()
            if now_wib.hour != EVENING_REPORT_HOUR_WIB:
                continue
            state = load_monitor_state()
            today = _wib_today_str()
            if state.get("evening_report_date") == today:
                continue
            text, snap = build_daily_report("sore")
            res = send_message(token, chat_id, text)
            if res and res.get('ok'):
                _update_report_snapshot(snap)
                state = load_monitor_state()
                state["evening_report_date"] = today
                save_monitor_state(state)
                print(f"🌆 Laporan sore {today} terkirim.", flush=True)
            else:
                print("🌆 Laporan sore gagal terkirim — coba lagi menit berikutnya.", flush=True)
        except Exception as e:
            print(f"Evening report exception: {e}", flush=True)
            time.sleep(60)

def main():
    config = load_config()
    token = config.get('tg_token')
    owner_chat_id = str(config.get('tg_chat_id', ''))

    if not token or not owner_chat_id:
        print("❌ Token Telegram atau Chat ID belum ditemukan di config.json!", flush=True)
        return

    print("==================================================", flush=True)
    print("🚀 MUSE AI OPERATOR: Menghubungkan ke Telegram...", flush=True)
    # 🔒 #22: token bot TIDAK PERNAH dicetak ke log (dulu 12 karakter pertama).
    print("🤖 Bot Token: tersimpan di config.json (tidak ditampilkan di log).", flush=True)
    print(f"👤 Owner Chat ID: {owner_chat_id}", flush=True)
    print("==================================================", flush=True)

    w_thread = threading.Thread(target=watchdog_background_loop, args=(token, owner_chat_id), daemon=True)
    w_thread.start()

    # 🔥 FASE 1 MONITORING: laporan harian otomatis 07:00 WIB
    d_thread = threading.Thread(target=daily_report_loop, args=(token, owner_chat_id), daemon=True)
    d_thread.start()

    # Laporan sore otomatis 17:00 WIB (pola sama seperti laporan pagi)
    e_thread = threading.Thread(target=evening_report_loop, args=(token, owner_chat_id), daemon=True)
    e_thread.start()

    # Kirim salam & pasang keyboard menu interaktif
    welcome_msg = (
        "🟢 *Bos, saya siap!* 😎\n\n"
        "Sistem udah diperbarui, semua tombol di bawah udah bisa dipencet dan langsung saya respon! 💪"
    )
    # v1.3.14: sebutkan isi update di pesan restart (CHANGELOG.md) — Bos
    # minta ada info update-nya, bukan cuma "sistem diperbarui".
    _ver = _version_number()
    _news = _changelog_lines(_ver) if _ver else []
    if _news:
        welcome_msg += ("\n\n🆕 *Update v" + _ver + ":*\n"
                        + "\n".join("• " + b for b in _news))
    send_message(token, owner_chat_id, welcome_msg, reply_markup=get_main_keyboard())

    # Auto-suggest perintah: daftarkan daftar perintah ke Telegram supaya
    # mengetik '/' langsung memunculkan saran. Fail-silent (lihat
    # register_bot_commands) — tidak boleh menggagalkan startup.
    register_bot_commands(token)

    offset = 0
    print("Listening for commands...", flush=True)

    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=15"
            res = requests.get(url, timeout=20).json()
            if res.get('ok') and res.get('result'):
                for update in res['result']:
                    offset = update['update_id'] + 1
                    # 🔥 FASE 1 MONITORING: tombol inline (mis. 🔁 Coba Lagi).
                    # Callback query tidak punya 'message' — tangani dulu, owner-only.
                    cq = update.get('callback_query')
                    if cq:
                        cq_sender = str(cq.get('from', {}).get('id', ''))
                        cq_chat = str(cq.get('message', {}).get('chat', {}).get('id', ''))
                        if cq_sender == owner_chat_id or cq_chat == owner_chat_id:
                            print(f"🔘 Callback diterima dari Bos: {cq.get('data')}", flush=True)
                            handle_callback_query(token, owner_chat_id, cq)
                        else:
                            print(f"⚠️ Callback ditolak dari ID asing: {cq_sender}", flush=True)
                            answer_callback(token, cq.get('id', ''), "⛔ Hanya owner yang boleh memakai tombol ini.")
                        continue
                    msg = update.get('message')
                    if not msg:
                        continue
                    sender_id = str(msg.get('from', {}).get('id', ''))
                    chat_id = str(msg.get('chat', {}).get('id', ''))
                    text = msg.get('text', '')

                    if (sender_id == owner_chat_id or chat_id == owner_chat_id) and text:
                        print(f"📩 Perintah diterima dari Bos ({chat_id}): {_log_safe_text(text)}", flush=True)
                        handle_user_command(token, chat_id, text, msg.get('message_id'))
                    elif text:
                        print(f"⚠️ Akses ditolak dari ID asing: {sender_id} / chat: {chat_id}", flush=True)
            time.sleep(0.3)
        except Exception as e:
            print(f"Polling loop exception: {e}", flush=True)
            time.sleep(2)

if __name__ == '__main__':
    main()
