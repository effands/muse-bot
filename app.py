import os, time, queue, threading, subprocess, random, json, shutil, math, re
import numpy as np
import cv2, librosa, imageio
import datetime as dt
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import secrets
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session
import requests

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from core.resource_gate import acquire_resource, release_resource

# ==========================================
# 🛡️ SETUP & MONITORING
# ==========================================
def auto_setup_dependencies():
    ffmpeg_found = shutil.which("ffmpeg") or os.path.exists("/usr/bin/ffmpeg")
    if not ffmpeg_found:
        print("⚙️ ZIQVA MUSE BOT: ffmpeg tidak ditemukan, mencoba install otomatis...")
        ret = os.system("apt-get update -qq && apt-get install -y ffmpeg")
        if ret == 0: print("✅ ffmpeg berhasil diinstall!")
        else: print("❌ Gagal install ffmpeg otomatis. Jalankan manual: apt-get install -y ffmpeg")
    else:
        path = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
        print(f"✅ ffmpeg ditemukan: {path}")

auto_setup_dependencies()

last_cpu_idle = 0
last_cpu_total = 0

def get_system_stats():
    global last_cpu_idle, last_cpu_total
    cpu_pct = 0.0
    try:
        with open('/proc/stat', 'r') as f:
            parts = [int(i) for i in f.readline().split()[1:8]]
        idle = parts[3] + parts[4]
        total = sum(parts)
        if last_cpu_total > 0:
            diff_idle = idle - last_cpu_idle
            diff_total = total - last_cpu_total
            if diff_total > 0:
                cpu_pct = round(100.0 * (1.0 - diff_idle / diff_total), 1)
        last_cpu_idle = idle
        last_cpu_total = total
        if cpu_pct < 0.0: cpu_pct = 0.0
        if cpu_pct > 100.0: cpu_pct = 100.0
    except: pass

    try:
        import psutil
        cpu_pct = psutil.cpu_percent(interval=None)
        mem = psutil.virtual_memory()
        return {"cpu": cpu_pct, "ram_pct": mem.percent, "ram_used": round(mem.used / (1024**3), 2), "ram_total": round(mem.total / (1024**3), 2)}
    except: pass

    return {"cpu": cpu_pct, "ram_pct": 0.0, "ram_used": 0.0, "ram_total": 0.0}

# ==========================================
# 💾 DATABASE & FOLDER SYSTEM
# ==========================================
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'))
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

from clipper.routes import clipper_bp
app.register_blueprint(clipper_bp, url_prefix='/api/clip')

def is_configured(): return os.path.exists(CONFIG_FILE)
def load_bot_config():
    if is_configured():
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    return {}

bot_config = load_bot_config()

# --- Sesi login yang stabil di semua browser ---
# Penyebab login berputar-putar: kalau config.json tidak punya secret_key,
# secret_key di-generate acak di setiap start proses, sehingga cookie sesi
# yang ditandatangani sebelum restart/reload tidak lagi valid dan pengguna
# dilempar kembali ke halaman login tanpa pesan error. Karena itu secret_key
# harus menetap di config.json (dibuat sekali, disimpan, dipakai selamanya).
# /setup tetap yang membuat config.json pertama kali; di sini kita hanya
# melengkapi secret_key yang hilang, tanpa mengubah PIN atau kunci lain.
if is_configured() and not bot_config.get('secret_key'):
    bot_config['secret_key'] = secrets.token_hex(32)
    try:
        with open(CONFIG_FILE, 'w') as _f:
            json.dump(bot_config, _f, indent=4)
    except Exception:
        pass
# 🔥 FASE 1 MONITORING: kunci bersama untuk operator Telegram (proses terpisah)
# agar bisa memanggil endpoint retry di bawah. Disimpan sekali di config.json,
# tidak pernah dikirim ke chat / dicatat di log. Pola sama seperti secret_key.
if is_configured() and not bot_config.get('operator_key'):
    bot_config['operator_key'] = secrets.token_hex(24)
    try:
        with open(CONFIG_FILE, 'w') as _f:
            json.dump(bot_config, _f, indent=4)
    except Exception:
        pass
app.secret_key = bot_config.get('secret_key', secrets.token_hex(32))
# Cookie sesi eksplisit: situs ini HTTP biasa di IP, jadi Secure harus False
# (cookie Secure tidak akan dikirim lewat http). SameSite=Lax agar cookie
# tetap terkirim saat navigasi top-level dari browser lain, dan sesi dibuat
# permanen 7 hari agar login tidak hilang saat browser ditutup.
app.config['SESSION_COOKIE_SAMESITE'] = 'Lax'
app.config['SESSION_COOKIE_HTTPONLY'] = True
app.config['SESSION_COOKIE_SECURE'] = False
app.config['PERMANENT_SESSION_LIFETIME'] = timedelta(days=7)

@app.before_request
def check_security():
    allowed_routes = ['login', 'setup', 'static', 'serve_uploads', 'device_login', 'poll_device_token', 'operator_retry_task', 'operator_approve_task', 'operator_reject_task', 'operator_approve_batch']
    if request.endpoint in allowed_routes: return
    if not is_configured(): return redirect(url_for('setup'))
    if 'logged_in' not in session: return redirect(url_for('login'))

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    if is_configured(): return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        pin = request.form.get('new_pin'); pin2 = request.form.get('confirm_pin')
        if not pin or len(pin) < 3: error = "PIN minimal 3 karakter."
        elif pin != pin2: error = "PIN tidak cocok!"
        else:
            new_secret = secrets.token_hex(24)
            with open(CONFIG_FILE, 'w') as f: json.dump({"admin_pin": pin, "secret_key": new_secret}, f, indent=4)
            app.secret_key = new_secret; session.permanent = True; session['logged_in'] = True
            return redirect(url_for('hub'))
    return render_template('setup.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_configured(): return redirect(url_for('setup'))
    error = None
    if request.method == 'POST':
        if request.form.get('password') == load_bot_config().get('admin_pin'):
            session.permanent = True; session['logged_in'] = True; return redirect(url_for('hub'))
        else: error = 'Akses Ditolak! PIN Salah.'
    return render_template('login.html', error=error)

@app.route('/logout')
def logout():
    session.pop('logged_in', None); return redirect(url_for('login'))

@app.route('/hub')
def hub():
    # Halaman untuk memilih masuk ke Factory atau Clipper
    return render_template('hub.html')

@app.route('/clipper')
def clipper_dashboard():
    # Halaman UI utama untuk Clipper
    return render_template('clipper.html')

BASE_UPLOAD = os.path.join(BASE_DIR, "uploads")
DB_FILE = os.path.join(BASE_DIR, 'channels_db.json')
TASKS_FILE = os.path.join(BASE_DIR, 'tasks_db.json')
PRESETS_FILE = os.path.join(BASE_DIR, 'presets.json')
CLIENT_SECRETS_FILE = os.path.join(BASE_DIR, 'client_secret.json')
SCOPES = ['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.upload']

os.makedirs(BASE_UPLOAD, exist_ok=True)
os.makedirs(os.path.join(BASE_DIR, 'static'), exist_ok=True)

db_lock = threading.RLock()  # 🔥 FIX: RLock (bukan Lock biasa) supaya tidak deadlock saat
                              # ada 'with db_lock:' bersarang (mis. save_channels() dipanggil
                              # dari dalam blok 'with db_lock:' lain, seperti di batch_create()).

GALLERY_FOLDER_MAP = {
    'audio':      'audios',
    'audios':     'audios',
    'background': 'backgrounds',
    'backgrounds':'backgrounds',
    'thumbnail':  'thumbnails',
    'thumbnails': 'thumbnails',
    'base_video': 'base_videos',  # <--- TAMBAHAN FOLDER BARU
    'overlay':    'overlays',     # 🔥 BARU: file overlay visualizer (cepat, tanpa render frame)
    'overlays':   'overlays',
    'special_song':  'special_songs',  # 🔥 BARU: lagu khusus yang ditempel di belakang playlist
    'special_songs': 'special_songs',
    'special':       'special_songs',
}

def resolve_folder(g_type: str) -> str:
    return GALLERY_FOLDER_MAP.get(str(g_type).strip().lower(), 'audios')

def load_tasks_db():
    if os.path.exists(TASKS_FILE):
        try:
            with open(TASKS_FILE, 'r') as f: return json.load(f)
        except: return {"active": [], "history": []}
    return {"active": [], "history": []}

def save_tasks_db():
    with db_lock:
        data = {"active": active_tasks, "history": history_tasks}
        temp_file = TASKS_FILE + '.tmp'
        with open(temp_file, 'w') as f: json.dump(data, f, indent=4)
        os.replace(temp_file, TASKS_FILE)

# ==========================================
# 🛡️ SISTEM KETAHANAN (ANTI-STUCK & ANTI-LOST)
# ==========================================
def send_tg_message(msg, reply_markup=None):
    """Kirim pesan ke Telegram kalau token & chat_id tersimpan. Aman gagal silent.
    reply_markup opsional — dipakai untuk tombol inline (mis. Retry) dari operator."""
    try:
        bot_config = load_bot_config()
        token, chat_id = bot_config.get('tg_token'), bot_config.get('tg_chat_id')
        if not token or not chat_id: return
        payload = {"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
        if reply_markup is not None:
            payload["reply_markup"] = reply_markup
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json=payload,
            timeout=15
        )
    except Exception:
        pass

def startup_sanitizer():
    """🔥 BARU: Bersihkan 'sisa-sisa' setelah VPS mati mendadak.
    Dipanggil sekali saat aplikasi mulai, sebelum worker jalan."""
    import subprocess as _sub
    notifs = []
    try:
        # 1. Bunuh ffmpeg zombie yang tertinggal dari proses mati mendadak
        proc = _sub.run(["pkill", "-9", "-f", "ffmpeg"], capture_output=True)
        if proc.returncode == 0:
            notifs.append("🧹 ffmpeg zombie ditemukan & dibersihkan")
    except Exception:
        pass
    # 2. Bersihkan resource gate yang menggantung (proses resource_gate punya state file-nya sendiri)
    try:
        release_resource("Factory Worker")
    except Exception:
        pass
    # 3. Bersihkan file sementara task yang tertinggal agar disk tidak penuh & tidak tertukar
    try:
        import glob
        for f in glob.glob(os.path.join(BASE_UPLOAD, "temp_a_*.mp3")) + \
                 glob.glob(os.path.join(BASE_UPLOAD, "temp_c_*.txt")) + \
                 glob.glob(os.path.join(BASE_UPLOAD, "temp_v_*.mp4")) + \
                 glob.glob(os.path.join(BASE_UPLOAD, "temp_mv_*.mp4")) + \
                 glob.glob(os.path.join(BASE_UPLOAD, "temp_sv_*.txt")) + \
                 glob.glob(os.path.join(BASE_UPLOAD, "loop_*.txt")):
            os.remove(f)
    except Exception:
        pass
    if notifs:
        send_tg_message("🧹 *Bos, saya udah beres-beres pas start nih* 😎\n" + "\n".join(notifs) + "\n\n✅ Aplikasi siap, antrean saya lanjutkan otomatis 💪")

def queue_watchdog_worker():
    """🔥 BARU: Watchdog antrean — task stuck 'Rendering...' > 30 menit otomatis digagalkan
    & lanjut ke task berikutnya, plus notifikasi Telegram."""
    STUCK_LIMIT = 60 * 60  # 60 menit (1 jam)
    while True:
        time.sleep(60)
        now = time.time()
        stuck = []
        with db_lock:
            for d in active_tasks:
                st = d['status']
                # 🔥 BARU: semua stage dimonitor — termasuk "In Factory Queue"
                # (task bisa macet di antrean kalau worker error sebelum masuk render).
                # Pengecualian: status cooldown/dijadwalkan yang memang HARUS menunggu.
                # 🔥 FIX 2: Tambahkan "Menunggu" ke daftar pengecualian
                waiting_kind = "Ditunda" in st or "Dijadwalkan" in st or "Menunggu" in st or "Dijeda" in st
                
                stuck_kind = not waiting_kind and ("Queue" in st or "Rendering" in st or "Meracik" in st
                              or "Mengunggah" in st or "Upload" in st or "YouTube" in st
                              or "Antrean" in st or "Extract" in st)
                if stuck_kind and "updated_at" not in d:
                    d['updated_at'] = now
                if stuck_kind and d.get('updated_at', 0) and (now - d['updated_at']) > STUCK_LIMIT:
                    stuck.append(d['id'])
            if stuck:
                save_tasks_db()
        for sid in stuck:
            # 🔥 FIX: hentikan worker yang masih memproses task stuck
            # (tanpa ini, worker tetap jalan sampai selesai → notif SUKSES
            # terkirim padahal task sudah digagalkan watchdog → notif ganda).
            stop_flags[sid] = True
            proc = active_processes.pop(sid, None)
            if proc is not None:
                try: proc.terminate(); proc.kill()
                except Exception: pass
            with db_lock:
                for d in active_tasks:
                    if d['id'] == sid:
                        move_to_history(sid, f"Gagal ❌ (Stuck >1 jam, otomatis dilanjutkan)")
                        break
            send_tg_message(f"⚠️ *Bos, ada task yang macet nih* 😅\nTask #{sid} stuck lebih dari 1 jam, nggak ada kemajuan. Saya gagalkan saja ya biar antrean jalan terus 💪")

threading.Thread(target=queue_watchdog_worker, daemon=True).start()

def telegram_status_report_worker():
    """🔥 UPDATE: Event-based notification — TIDAK lagi per jam.
    Notifikasi Telegram HANYA dikirim saat ada kejadian penting:
    • Task berhasil tayang / render selesai ✅
    • Task gagal ❌ (limit, error upload, dll)
    • Task dibatalkan/digagalkan watchdog 🛑
    Laporan digabung tiap menit (buffer) agar tidak spam.
    """
    global tg_event_queue     # pakai buffer modul (dideklarasikan di bawah worker)
    last_flush = time.time()
    FLUSH_INTERVAL = 60           # kirim kumpulan event maksimal tiap 1 menit
    MAX_FLUSH = 8                 # kalau event menumpuk >8, kirim segera
    while True:
        time.sleep(30)
        bot_config = load_bot_config()
        token, chat_id = bot_config.get('tg_token'), bot_config.get('tg_chat_id')
        if not token or not chat_id: continue
        # 🔥 Gabung event menjadi 1 pesan (anti-spam)
        if len(tg_event_queue) >= MAX_FLUSH or (tg_event_queue and time.time() - last_flush >= FLUSH_INTERVAL):
            events = tg_event_queue[:MAX_FLUSH]
            tg_event_queue = tg_event_queue[MAX_FLUSH:]
            last_flush = time.time()
            msg = "🔔 *Ziqva Muse Bot — Laporan Kejadian*\n\n" + "\n\n".join(events)
            try: send_tg_message(msg)
            except Exception: pass

def notify_event(emoji, title, status, reply_markup=None):
    """🔥 BARU: masukkan event notifikasi (selesai/gagal) ke buffer Telegram.
    Event digabung & dikirim maksimal tiap 1 menit agar tidak spam chat.
    Kalau reply_markup diisi (mis. tombol Retry untuk task gagal), event TIDAK
    masuk buffer — langsung dikirim saat itu juga, karena satu pesan gabungan
    tidak bisa membawa tombol untuk task tertentu."""
    try:
        if reply_markup is not None:
            send_tg_message(f"{emoji} *{title[:70]}*\nStatus: {status}", reply_markup=reply_markup)
            return
        tg_event_queue.append(f"{emoji} *{title[:70]}*\nStatus: {status}")
    except Exception:
        pass

tg_event_queue = []  # 🔥 buffer event notifikasi Telegram (dibaca worker di atas)

telegram_status_report_worker_ref = threading.Thread(target=telegram_status_report_worker, daemon=True)
telegram_status_report_worker_ref.start()

def load_channels():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, 'r') as f: return json.load(f)
        except: return []
    return []

def save_channels(channels):
    with db_lock:
        temp_file = DB_FILE + '.tmp'
        with open(temp_file, 'w') as f: json.dump(channels, f, indent=4)
        os.replace(temp_file, DB_FILE)

task_data = load_tasks_db()
active_tasks = task_data.get("active", [])
history_tasks = task_data.get("history", [])
database_channel = load_channels()

import itertools
render_queue = queue.PriorityQueue()
_queue_seq = itertools.count()  # tie-breaker biar item dgn jadwal sama tidak dibanding langsung sbg dict

def _queue_priority(blueprint):
    """Ambil timestamp dari publish_date blueprint agar antrean diproses
    berdasarkan tanggal & jam paling awal duluan, bukan sekadar urutan masuk.
    🔥 BARU: 'jaga jadwal' — task yang publish_date-nya sudah lewat diberi boost
    (didahulukan), biar antrean tidak makin tertinggal dari jadwal."""
    try:
        ts = datetime.strptime(blueprint['publish_date'], "%Y-%m-%d %H:%M").timestamp()
    except Exception:
        return float('inf')  # kalau tanggal tidak valid/kosong, taruh paling belakang
    # 🔥 BARU: task terlambat dari jadwal diproses duluan (boost 7 hari)
    overdue_boost = max(0.0, datetime.now().timestamp() - ts)
    if overdue_boost > 60:  # baru di-boost kalau lewat > 1 menit
        ts = max(0.0, ts - min(overdue_boost, 7 * 24 * 3600))
    return ts

def queue_put(blueprint):
    render_queue.put((_queue_priority(blueprint), next(_queue_seq), blueprint))

def queue_get():
    _priority, _seq, blueprint = render_queue.get()
    return blueprint

def _active_task_yt_id(t):
    """🔥 FIX v1.8: ambil yt_id task antre — bisa di field atas ATAU di dalam blueprint.
    Task dari Batch Creator menyimpan yt_id HANYA di dalam blueprint, jadi filter lama
    (t.get('yt_id')) tidak pernah cocok → Stop Antrean selalu 0 task."""
    if t.get('yt_id'):
        return t.get('yt_id')
    bp = t.get('blueprint')
    if isinstance(bp, dict):
        return bp.get('yt_id')
    return None
stop_flags = {}
active_processes = {}  # 🔥 task_id -> subprocess.Popen aktif, dipakai untuk force-stop instan
channel_cooldowns = {} 

def load_cooldowns():
    # 🔥 BARU: muat cooldown rate-limit dari channels_db.json agar TIDAK HILANG saat restart.
    # Sebelumnya cooldown hanya di memori → restart = cooldown hilang → task langsung
    # diproses lagi → kena rate-limit YouTube lagi → task "nyangkut" & rawan hilang.
    global channel_cooldowns
    for c in database_channel:
        cd = c.get('cooldown_until')
        if cd and cd > time.time():
            channel_cooldowns[c['yt_id']] = cd

def persist_cooldown(yt_id):
    # 🔥 BARU: simpan cooldown channel permanen ke database setiap kali cooldown dipasang.
    for c in database_channel:
        if c['yt_id'] == yt_id:
            c['cooldown_until'] = channel_cooldowns.get(yt_id, 0)
            try: save_channels(database_channel)
            except Exception: pass
            return

def get_quota_reset_ts():
    """🔥 BARU: timestamp reset kuota harian YouTube = tengah malam waktu Pacific
    (jam 14:00/15:00 WIB). Lebih tepat daripada cooldown 24 jam mentah — sesuai FAQ."""
    try:
        from zoneinfo import ZoneInfo
        now_pt = datetime.now(ZoneInfo("America/Los_Angeles"))
        reset_pt = (now_pt + timedelta(days=1)).replace(hour=0, minute=5, second=0, microsecond=0)
        return reset_pt.timestamp()
    except Exception:
        return time.time() + (3600 * 24)

def clear_channel_cooldown(yt_id):
    """🔥 BARU: hapus cooldown channel (mis. setelah taut ulang / tambah key baru)."""
    if yt_id in channel_cooldowns:
        del channel_cooldowns[yt_id]
    for c in database_channel:
        if c['yt_id'] == yt_id and c.get('cooldown_until'):
            c['cooldown_until'] = 0
    try: save_channels(database_channel)
    except Exception: pass

load_cooldowns()

# 🔥 BARU v1.4: batas upload harian per channel — 6 video/hari, lanjut otomatis besok.
# Counter disimpan di channels_db.json per channel → tahan restart.
MAX_UPLOADS_PER_CHANNEL_PER_DAY = 6

# 🔥 FASE 5: QC gate pra-upload + self-healing + bersih disk (audit F9a/F8)
QC_MIN_FILE_BYTES = 100_000          # file render < 100 KB dianggap gagal
QC_DURATION_TOLERANCE_SEC = 120.0    # toleransi durasi vs target (atau 5%)
QC_SILENCE_MAX_DB = -50.0            # mean_volume di bawah ini = audio hening
QC_AUDIO_PROBE_TIMEOUT = 180         # detik untuk volumedetect (decode audio)
DISK_MIN_FREE_BYTES = 5 * 1024 ** 3  # 5 GB ambang aman sebelum render
DISK_BYTES_PER_SEC = 500_000         # perkiraan ukuran hasil render (500 KB/detik)
UPLOAD_MAX_ATTEMPTS = 3              # 1 percobaan + maks 2x retry (Fase 5)
UPLOAD_RETRY_BACKOFF_BASE = 10       # detik; jeda 10 lalu 20 (eksponensial x2)
FAIL_STREAK_AUTO_PAUSE = 3           # gagal beruntun → jeda channel otomatis
# Aturan umur bersih-bersih disk (lihat cleanup_old_files):
CLEANUP_TEMP_MAX_AGE = 6 * 3600      # temp render > 6 jam
CLEANUP_FINAL_MAX_AGE = 24 * 3600    # final render > 1 hari & task tidak aktif
CLEANUP_CLIP_MAX_AGE = 7 * 24 * 3600      # output clipper > 7 hari & job tidak aktif
CLEANUP_DOWNLOAD_MAX_AGE = 7 * 24 * 3600  # download sumber clipper > 7 hari

def _wib_now():
    return datetime.now(ZoneInfo("Asia/Jakarta"))

def _next_midnight_wib_ts():
    now_wib = _wib_now()
    nxt = (now_wib + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return nxt.timestamp()

def track_daily_upload(yt_id):
    """Catat 1 upload sukses untuk channel hari ini (WIB).
    Kembalikan (jumlah_hari_ini, sudah_kena_batas)."""
    today = _wib_now().strftime("%Y-%m-%d")
    count = 0
    with db_lock:
        for c in database_channel:
            if c.get('yt_id') == yt_id:
                du = c.get('daily_uploads') or {}
                count = int(du.get('count', 0)) + 1 if du.get('date') == today else 1
                c['daily_uploads'] = {"date": today, "count": count}
                break
        try: save_channels(database_channel)
        except Exception: pass
    return count, count >= MAX_UPLOADS_PER_CHANNEL_PER_DAY

# 🔥 FASE 6: KUOTA PER PROJECT GOOGLE + AUTOPILOT
# Kuota YouTube dihitung PER PROJECT Google (~6 upload/hari), bukan per
# channel. Project diidentifikasi dari prefix client_id kredensial utama
# channel (angka sebelum '-' — BUKAN secret, hanya nomor project).
def _project_id_of_channel(channel_data):
    """Nomor project Google dari kredensial utama channel, atau None kalau
    channel belum punya kredensial / client_id tidak terbaca."""
    try:
        if not channel_data:
            return None
        creds_list = channel_data.get('creds_list') or [channel_data.get('creds_json')]
        cred_str = next((s for s in creds_list if s), '')
        if not cred_str:
            return None
        client_id = (json.loads(cred_str) or {}).get('client_id', '')
        if not client_id:
            return None
        return str(client_id).split('-')[0] or None
    except Exception:
        return None

def _project_uploads_today(project_id):
    """Total upload hari ini (WIB) dari semua channel di project ini."""
    if not project_id:
        return 0
    today = _wib_now().strftime("%Y-%m-%d")
    total = 0
    with db_lock:
        for c in database_channel:
            if _project_id_of_channel(c) == project_id:
                du = c.get('daily_uploads') or {}
                if du.get('date') == today:
                    total += int(du.get('count', 0))
    return total

def get_project_quotas():
    """Ringkasan kuota per project Google untuk panel: project_id, daftar
    channel, upload hari ini, batas (6), dan sisa. Channel tanpa kredensial
    dikelompokkan sebagai 'tidak-diketahui' (kuota tidak terlacak)."""
    today = _wib_now().strftime("%Y-%m-%d")
    projects = {}
    with db_lock:
        for c in database_channel:
            pid = _project_id_of_channel(c) or 'tidak-diketahui'
            entry = projects.setdefault(pid, {"project_id": pid, "channels": [], "uploads_today": 0})
            entry["channels"].append({"yt_id": c.get('yt_id'), "name": c.get('name', c.get('yt_id', ''))})
            du = c.get('daily_uploads') or {}
            if du.get('date') == today:
                entry["uploads_today"] += int(du.get('count', 0))
    out = []
    for pid in sorted(projects):
        e = projects[pid]
        e["limit"] = MAX_UPLOADS_PER_CHANNEL_PER_DAY
        e["remaining"] = max(0, MAX_UPLOADS_PER_CHANNEL_PER_DAY - e["uploads_today"])
        out.append(e)
    return out

def _cooldown_project_channels(project_id):
    """Pasang cooldown sampai tengah malam WIB untuk SEMUA channel di project
    ini (dipakai saat kuota project habis). Mengembalikan nama channel yang
    ikut dijeda."""
    until = _next_midnight_wib_ts()
    names = []
    with db_lock:
        for c in database_channel:
            if _project_id_of_channel(c) == project_id:
                channel_cooldowns[c.get('yt_id')] = until
                names.append(c.get('name', c.get('yt_id')))
    for c in database_channel:
        if _project_id_of_channel(c) == project_id:
            try: persist_cooldown(c.get('yt_id'))
            except Exception: pass
    return names

def _autopilot_of(channel_data):
    """Mode autopilot channel: 'auto' (full-auto, lewati approval) atau
    'approval' (default — channel lama/barau tetap lewat approval Fase 2)."""
    if not channel_data:
        return 'approval'
    mode = str(channel_data.get('autopilot') or 'approval').lower()
    return mode if mode in ('auto', 'approval') else 'approval'

# 🔥 SISTEM NOTIFIKASI LONCENG 🔥
system_notifications = []

def get_ffmpeg_path():
    local_exe = os.path.join(BASE_DIR, "ffmpeg.exe")
    if os.path.exists(local_exe): return local_exe
    found = shutil.which("ffmpeg")
    if found: return found
    for p in ["/usr/bin/ffmpeg", "/usr/local/bin/ffmpeg", "/bin/ffmpeg"]:
        if os.path.exists(p): return p
    raise FileNotFoundError("ffmpeg tidak ditemukan! Jalankan: apt-get install -y ffmpeg")

def get_ffprobe_path():
    found = shutil.which("ffprobe")
    if found: return found
    for p in ["/usr/bin/ffprobe", "/usr/local/bin/ffprobe", "/bin/ffprobe"]:
        if os.path.exists(p): return p
    return "ffprobe"

def wait_for_resources(task_id, max_ram_pct=85.0):
    while True:
        if stop_flags.get(task_id): return False
        stats = get_system_stats()
        if stats['ram_pct'] < max_ram_pct: return True
        with db_lock:
            for d in active_tasks:
                if d['id'] == task_id: d['status'] = f"Menunggu RAM Turun ({stats['ram_pct']}%) ⏳"
        save_tasks_db()
        time.sleep(10)

def move_to_history(task_id, final_status):
    global active_tasks, history_tasks
    with db_lock:
        for t in active_tasks:
            if t['id'] == task_id:
                start_t = t.get('started_at')
                if start_t:
                    import time as sys_time, datetime as sys_dt
                    dur_sec = int(sys_time.time() - start_t)
                    m, s = divmod(dur_sec, 60)
                    h, m = divmod(m, 60)
                    dur_str = f"{h}h {m}m" if h > 0 else f"{m}m {s}s"
                    st_str = sys_dt.datetime.fromtimestamp(start_t).strftime("%H:%M")
                    ed_str = sys_dt.datetime.now().strftime("%H:%M")
                    t['render_info'] = f"⏱ {st_str} - {ed_str} ({dur_str})"
                
                t['status'] = final_status
                # 🔥 FASE 1 MONITORING: cap tanggal selesai (WIB) agar operator bisa
                # menghitung upload sukses/gagal "hari ini" dan "kemarin" untuk
                # /status & laporan harian. Field tambahan, tidak mengubah field lama.
                try:
                    t['finished_date'] = _wib_now().strftime("%Y-%m-%d")
                    t['finished_at'] = time.time()
                except Exception:
                    pass
                history_tasks.insert(0, t)
                active_tasks.remove(t)
                if len(history_tasks) > 2000: history_tasks.pop() # Limit di perbesar
                break
    save_tasks_db()

def get_fresh_credentials(channel_data):
    creds_str = channel_data.get('creds_list', [channel_data.get('creds_json')])[0]
    creds = Credentials.from_authorized_user_info(json.loads(creds_str))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

# ==========================================
# 🚨 SATPAM API KEY (AUTO-CHECKER)
# ==========================================
def api_key_checker_worker():
    global system_notifications, database_channel
    while True:
        time.sleep(10)
        new_notifs = []
        changed = False
        for c in database_channel:
            cl = c.get('creds_list')
            if not cl:
                # 🔥 format lama (cuma creds_json): perilaku lama — notifikasi saja, tanpa hapus
                cred_str = c.get('creds_json', '')
                if cred_str:
                    try:
                        creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                        if creds.expired and creds.refresh_token:
                            creds.refresh(Request())
                    except Exception:
                        msg = f"⚠️ API Key #1 untuk Channel '{c.get('name','Unknown')}' EXPIRED! Silakan hapus dan tautkan ulang."
                        if not any(n['msg'] == msg for n in system_notifications):
                            new_notifs.append({"msg": msg, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
                continue
            # 🔥 FIX v1.3: pisahkan key hidup vs key mati (refresh token invalid_grant).
            # Key mati dibuang otomatis HANYA bila masih ada key hidup lain;
            # kalau semua mati, jangan hapus — minta taut ulang seperti dulu.
            live_keys = []
            dead_count = 0
            for idx, cred_str in enumerate(cl):
                if not cred_str:
                    continue
                try:
                    creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                    live_keys.append(cred_str)
                except Exception as e:
                    if 'invalid_grant' in str(e).lower():
                        dead_count += 1
                    else:
                        # gagal non-invalid_grant (mis. jaringan/timeout): jangan hapus
                        live_keys.append(cred_str)
                        msg = f"⚠️ API Key #{idx+1} untuk Channel '{c.get('name','Unknown')}' gagal dicek, dicoba lagi 12 jam berikutnya."
                        if not any(n['msg'] == msg for n in system_notifications):
                            new_notifs.append({"msg": msg, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
            if dead_count and live_keys:
                with db_lock:
                    c['creds_list'] = live_keys
                    c['status'] = f"Connected 🟢 ({len(live_keys)} Keys)"
                    changed = True
                new_notifs.append({"msg": f"🧹 {dead_count} API Key mati untuk Channel '{c.get('name','Unknown')}' dibersihkan otomatis.",
                                   "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
            elif dead_count and not live_keys:
                msg = f"⚠️ Semua API Key untuk Channel '{c.get('name','Unknown')}' EXPIRED! Silakan hapus dan tautkan ulang."
                if not any(n['msg'] == msg for n in system_notifications):
                    new_notifs.append({"msg": msg, "time": datetime.now().strftime("%Y-%m-%d %H:%M")})

        if new_notifs:
            with db_lock:
                system_notifications.extend(new_notifs)
        if changed:
            with db_lock:
                save_channels(database_channel)

        time.sleep(43200)

threading.Thread(target=api_key_checker_worker, daemon=True).start()

# ==========================================
# 🏭 GALLERY & ASSET MANAGER
# ==========================================
def get_channel_folder(yt_id, sub):
    path = os.path.join(BASE_UPLOAD, yt_id, sub)
    os.makedirs(path, exist_ok=True)
    return path

def get_multi_backgrounds(yt_id, count=1):
    path = get_channel_folder(yt_id, "backgrounds")
    files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(('.mp4', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.mov'))]
    if not files: return []
    random.shuffle(files)
    
    selected = []
    while len(selected) < count and files:
        for f in files:
            selected.append(f)
            if len(selected) == count: break
    return selected

def get_all_audios(yt_id):
    path = get_channel_folder(yt_id, "audios")
    files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(('.mp3', '.wav'))]
    random.shuffle(files)
    return files

def get_overlay_file(yt_id, filename=None):
    """🔥 BARU: ambil file overlay visualizer dari folder overlays channel.
    Kalau filename diberikan & cocok, pakai itu; kalau tidak, pakai file pertama (abjad)."""
    folder = get_channel_folder(yt_id, "overlays")
    if not os.path.exists(folder): return None
    files = [f for f in os.listdir(folder) if f.lower().endswith(('.mp4', '.mov', '.webm', '.mkv'))]
    if not files: return None
    if filename:
        for f in files:
            if f == filename:
                return os.path.join(folder, f)
    return os.path.join(folder, sorted(files)[0])

def get_special_songs(yt_id, count=2):
    """🔥 UPDATE 6 Nov 2026: ambil maks `count` lagu khusus pertama (urut abjad)
    dari folder special_songs channel — diputar berurutan di ujung video.
    🔥 FASE 6: dipilih ACAK (bukan selalu 2 file pertama abjad) supaya tiap
    video beda variasinya dan channel tidak terdengar monoton."""
    folder = get_channel_folder(yt_id, "special_songs")
    if not os.path.exists(folder): return []
    files = [f for f in os.listdir(folder) if f.lower().endswith(('.mp3', '.wav'))]
    if not files: return []
    picked = random.sample(sorted(files), min(count, len(files)))
    return [os.path.join(folder, f) for f in picked]

def get_special_song(yt_id):
    songs = get_special_songs(yt_id, count=1)
    return songs[0] if songs else None

def get_and_consume_thumbnail(yt_id):
    """🔥 UPDATE: pilih thumbnail acak (bukan selalu file pertama),
    Tanpa syarat ukuran minimum — gambar dipakai apa adanya;
    batas 2MB YouTube tetap dijaga lewat kompres otomatis di bawah."""
    path = get_channel_folder(yt_id, "thumbnails")
    files = [f for f in os.listdir(path) if f.lower().endswith(('.jpg', '.png', '.jpeg'))]
    if not files: return None
    # Pilih acak dari folder (lebih bervariasi tiap video, file tidak dihapus/dipakai ulang)
    chosen = random.choice(files)
    thumb_path = os.path.join(path, chosen)
    # 🔥 BARU: validasi ukuran — YouTube mewajibkan >= 1280x720 DAN <= 2MB (2097152 byte)
    try:
        from PIL import Image
        img = Image.open(thumb_path)
        img.load()
        # (Syarat ukuran minimum 1280x720 dihapus — permintaan Bams, 6 Nov 2026)
    except Exception:
        return None
    # 🔥 BARU: kalau file > 2MB, kompres otomatis menjadi JPG (batas YouTube: 2097152 byte)
    max_bytes = 2097152
    try:
        if os.path.getsize(thumb_path) > max_bytes:
            from PIL import Image as PILImage
            img2 = PILImage.open(thumb_path).convert('RGB')
            # YouTube hanya butuh max 1920 lebar untuk thumbnail; downscale cukup
            scale = min(1.0, 1920 / img2.width, 1080 / img2.height)
            if scale < 1.0:
                img2 = img2.resize((max(1, int(img2.width * scale)), max(1, int(img2.height * scale))), PILImage.LANCZOS)
            # Tulis ke file sementara .jpg lalu pakai itu
            base, _ = os.path.splitext(thumb_path)
            tmp_path = base + '_yt_compressed.jpg'
            quality = 92
            while quality >= 40:
                img2.save(tmp_path, 'JPEG', quality=quality, optimize=True)
                if os.path.getsize(tmp_path) <= max_bytes:
                    break
                quality -= 8
            else:
                # Fallback terakhir: turunkan skala lagi
                img3 = img2.resize((max(1, int(img2.width * 0.75)), max(1, int(img2.height * 0.75))), PILImage.LANCZOS)
                img3.save(tmp_path, 'JPEG', quality=40, optimize=True)
            return tmp_path
    except Exception as e:
        print(f"Thumbnail compress error: {e}")
    return thumb_path

def get_random_preset(allowed_names=None):
    if not os.path.exists(PRESETS_FILE): return None
    try:
        with open(PRESETS_FILE, 'r') as f: presets = json.load(f)
        if not presets: return None
        if allowed_names:
            filtered = {k: v for k, v in presets.items() if k in allowed_names}
            if filtered: return random.choice(list(filtered.values()))
        return random.choice(list(presets.values()))
    except: return None

def safe_num(val, default):
    try: return float(val) if val != "" and val is not None else default
    except: return default

# ==========================================
# ⚙️ CORE ENGINE (VISUALIZER DENGAN 7 MODE)
# ==========================================
class AudioBrain:
    def __init__(self):
        import numpy as np
        self.y = None; self.sr = None; self.has_audio = False
        self.duration = 0.0

    def load(self, path, max_duration=None):
        try:
            import librosa
            # Hanya memuat audio standar, fitur onset_strength (biang kerok RAM jebol) dimatikan total
            self.y, self.sr = librosa.load(path, sr=22050, mono=True, duration=max_duration)
            self.duration = len(self.y) / self.sr
            self.has_audio = True
        except Exception as e: print(f"Audio Error: {e}")

    def get_data(self, t, n_bars=64): 
        import numpy as np
        n_bars = int(n_bars) 
        if not self.has_audio: return 0.0, False, np.zeros(n_bars)
        
        idx = int(t * self.sr)
        if idx >= len(self.y): return 0.0, False, np.zeros(n_bars)

        try: 
            chunk = self.y[idx:idx+1024]
            vol = np.sqrt(np.mean(chunk**2)) * 10 if len(chunk) > 0 else 0
        except: vol = 0
        
        # 🔥 Deteksi Hit/Beat Ringan on-the-fly (Sangat hemat RAM)
        hit = True if vol > 1.5 else False

        final_bars = np.zeros(n_bars)
        try:
            n_fft = 2048
            fft_data = self.y[idx:idx+n_fft]
            if len(fft_data) == n_fft:
                windowed_data = fft_data * np.hanning(n_fft)
                spec = np.abs(np.fft.rfft(windowed_data))
                usable = spec[2:200] 
                ls = len(usable)
                
                if ls > 0:
                    half_n = max(1, n_bars // 2) 
                    raw_bars = np.zeros(half_n)
                    for i in range(half_n):
                        s = int((i / half_n) * ls)
                        e = int(((i + 1) / half_n) * ls)
                        if e <= s: e = s + 1
                        if e > ls: e = ls
                        raw_bars[i] = np.mean(usable[s:e]) / 15.0 if e > s else 0
                    
                    smooth_half = np.convolve(raw_bars, np.ones(3)/3, mode='same')
                    final_bars = np.concatenate((smooth_half[::-1], smooth_half))
                    
                    if len(final_bars) < n_bars: final_bars = np.pad(final_bars, (0, n_bars - len(final_bars)), 'constant')
                    elif len(final_bars) > n_bars: final_bars = final_bars[:n_bars]
        except Exception: pass 
        return vol, hit, final_bars

# Durasi crossfade visual tetap; tidak ditampilkan sebagai opsi UI.
BACKGROUND_CROSSFADE_SECONDS = 1.0


class BackgroundManager:
    def __init__(self, bg_paths, w, h, fps=30, crossfade_seconds=BACKGROUND_CROSSFADE_SECONDS):
        self.bg_paths = bg_paths
        self.w = w
        self.h = h
        self.fps = fps
        self.idx = 0
        self.reader = None
        self.static_bg = None
        self.last_frame = None
        self.transition_old = None
        self.transition_remaining = 0
        self.crossfade_frames = max(1, int(round(fps * crossfade_seconds)))
        self.load_current()

    def load_current(self):
        if self.reader:
            self.reader.close()
            self.reader = None
        self.static_bg = None
        path = self.bg_paths[self.idx]
        if path.lower().endswith(('.png', '.jpg', '.jpeg', '.webp')):
            img = cv2.imread(path)
            if img is not None:
                self.static_bg = cv2.resize(img, (self.w, self.h))
            else:
                self.static_bg = np.zeros((self.h, self.w, 3), dtype=np.uint8)
        else:
            self.reader = imageio.get_reader(path, 'ffmpeg')

    def _read_current_frame(self):
        if self.static_bg is not None:
            return self.static_bg.copy()
        try:
            frame = self.reader.get_next_data()
            return cv2.resize(cv2.cvtColor(frame, cv2.COLOR_RGB2BGR), (self.w, self.h))
        except Exception:
            return None

    def _read_next_asset_frame(self):
        # Lewati aset yang tidak dapat dibaca, tetapi tetap mempertahankan urutan loop.
        for _ in range(len(self.bg_paths)):
            self.idx = (self.idx + 1) % len(self.bg_paths)
            self.load_current()
            frame = self._read_current_frame()
            if frame is not None:
                return frame
        return None

    def _blend_transition_frame(self, new_frame):
        if self.transition_old is None or self.transition_remaining <= 0:
            return new_frame
        completed = self.crossfade_frames - self.transition_remaining + 1
        alpha = min(1.0, completed / self.crossfade_frames)
        output = cv2.addWeighted(self.transition_old, 1.0 - alpha, new_frame, alpha, 0.0)
        self.transition_remaining -= 1
        if self.transition_remaining <= 0:
            self.transition_old = None
        return output

    def get_frame(self):
        frame = self._read_current_frame()
        if frame is not None:
            if self.transition_remaining > 0:
                frame = self._blend_transition_frame(frame)
            self.last_frame = frame.copy()
            return frame

        # Background selesai. Mulai aset berikutnya dengan crossfade dari frame terakhir.
        old_frame = self.last_frame.copy() if self.last_frame is not None else None
        new_frame = self._read_next_asset_frame()
        if new_frame is None:
            return old_frame if old_frame is not None else np.zeros((self.h, self.w, 3), dtype=np.uint8)

        if old_frame is not None and self.crossfade_frames > 0:
            self.transition_old = old_frame
            self.transition_remaining = self.crossfade_frames
            frame = self._blend_transition_frame(new_frame)
        else:
            frame = new_frame
        self.last_frame = frame.copy()
        return frame

    def close(self):
        if self.reader:
            self.reader.close()
            self.reader = None

class VisualEngine:
    def __init__(self, c_bot, c_top, c_part):
        import numpy as np
        self.col_bot = (c_bot[2], c_bot[1], c_bot[0])
        self.col_top = (c_top[2], c_top[1], c_top[0])
        self.col_part = (c_part[2], c_part[1], c_part[0])
        self.bar_h = None
        self.particles = []
        
        # Generator Gradien Kustom Asli Bos K
        self.grad = np.zeros((1000, 1, 3), dtype=np.uint8)
        for c in range(3): 
            self.grad[:, 0, c] = np.linspace(self.col_top[c], self.col_bot[c], 1000)
            
        self.bar_h_h = None
        self.radial_h = None
        self.wave_points = None
        self.line_h = None
        self.band_h = None
        self.frame_count = 0
        self.rainbow_palette = []

    def _init_rainbow(self, n):
        import numpy as np
        import cv2
        if len(self.rainbow_palette) != n:
            self.rainbow_palette = []
            for i in range(n):
                hue = int((i / n) * 179) # Rentang Hue OpenCV 0-179
                color_hsv = np.uint8([[[hue, 255, 255]]])
                bgr = cv2.cvtColor(color_hsv, cv2.COLOR_HSV2BGR)[0][0]
                self.rainbow_palette.append((int(bgr[0]), int(bgr[1]), int(bgr[2])))

    def get_color(self, i, n, mode):
        if mode == 'rainbow':
            if not self.rainbow_palette: return (255,255,255)
            # 🔥 SUNTIK KEBAL: Sesuaikan rasio agar tidak pernah Out of Range
            ratio = max(0.0, min(1.0, i / max(1, n)))
            idx = int(ratio * (len(self.rainbow_palette) - 1))
            idx = max(0, min(len(self.rainbow_palette) - 1, idx))
            return self.rainbow_palette[idx]
        else:
            ratio = max(0.0, min(1.0, i / max(1, n)))
            idx = max(0, min(999, int(ratio * 999)))
            c = self.grad[idx, 0]
            return (int(c[0]), int(c[1]), int(c[2]))

    # ========== VERTICAL BARS (BOTTOM & CENTER) ==========
    def draw_vertical_bars(self, frame, glow_canvas, bars, cfg, current_px, current_py, current_wp):
        import cv2, numpy as np
        h, w = frame.shape[:2]
        n = len(bars)
        max_h = h * (cfg.get('max_height', 40) / 100)
        idle, space = int(cfg.get('idle_height', 5)), int(cfg.get('spacing', 3))
        bar_style = cfg.get('bar_style', 'bottom')
        color_mode = cfg.get('color_mode', 'gradient')
        
        tot_w = w * current_wp
        bar_w = int(cfg.get('bar_width', 0) or 0)
        if not bar_w or bar_w <= 0:
            used_gap = min(space * (n - 1), int(tot_w * 0.4))
            bar_w = max(1, int((tot_w - used_gap) / n))
        start_x = int(w * current_px) - int(tot_w / 2)
        base_y = int(h * current_py)
        
        if getattr(self, 'bar_h', None) is None or len(self.bar_h) != n: self.bar_h = np.zeros(n)
        
        # 🌈 CACHE WARNA CERAH: Merah -> Kuning -> Hijau -> Biru Muda -> Biru
        if getattr(self, 'rainbow_v', None) is None:
            self.rainbow_v = []
            for d in range(1000):
                hue = int((d / 999.0) * 120) # 0=Merah, 30=Kuning, 60=Hijau, 90=Cyan, 120=Biru
                bgr = cv2.cvtColor(np.uint8([[[hue, 255, 255]]]), cv2.COLOR_HSV2BGR)[0][0]
                self.rainbow_v.append((int(bgr[0]), int(bgr[1]), int(bgr[2])))
        
        for i in range(n):
            target = bars[i] * cfg.get('reactivity', 0.66)
            self.bar_h[i] = (self.bar_h[i] * 0.85) + (target * 0.15)
            height = max(2, int(max(idle, self.bar_h[i] * max_h)))
            
            x1 = start_x + int((tot_w / n) * i)
            x2 = x1 + bar_w
            y1 = (base_y - (height // 2)) if bar_style == 'center' else base_y - height
            y2 = (base_y + (height // 2)) if bar_style == 'center' else base_y
            
            x1_safe, x2_safe = max(0, min(w, x1)), max(0, min(w, x2))
            y1_safe, y2_safe = max(0, min(h, y1)), max(0, min(h, y2))
            
            if x2_safe > x1_safe and y2_safe > y1_safe:
                # 🌈 GRADASI VERTIKAL SOLID BERSKALA (Tarik warna puncak ke setiap tiang)
                real_h = max(1, (height // 2) if bar_style == 'center' else height)
                
                for y in range(y1_safe, y2_safe):
                    dist = abs(base_y - y) if bar_style == 'center' else (base_y - y)
                    if color_mode == 'rainbow':
                        idx = int(max(0.0, min(1.0, dist / real_h)) * 999)
                        color = self.rainbow_v[idx]
                    else:
                        color = self.get_color(dist, real_h, color_mode)
                    
                    # Layer bayangan (glow) & Layer utama (tiang solid)
                    cv2.line(glow_canvas, (x1_safe, y), (x2_safe - 1, y), color, 1)
                    cv2.line(frame, (x1_safe, y), (x2_safe - 1, y), color, 1)

    
    # ========== CIRCULAR (MELINGKAR - GAYA LED) ==========
    def draw_circle_bars(self, frame, glow_canvas, bars, cfg, rotation, current_px, current_py, current_wp, vol=0):
        import cv2, math, numpy as np
        h, w = frame.shape[:2]
        n = len(bars)
        if n == 0: return

        max_h = h * (cfg.get('max_height', 40) / 100)
        idle = int(cfg.get('idle_height', 5))
        idle = min(idle, max_h)
        color_mode = cfg.get('color_mode', 'gradient')

        tot_w = w * current_wp
        # 🔥 BARU: tebal segmen lingkaran mengikuti cfg.bar_width
        bar_w = int(cfg.get('bar_width', 0) or 0)
        if not bar_w or bar_w <= 0:
            used_gap = min(cfg.get('spacing', 3) * (n - 1), int(tot_w * 0.4))
            bar_w = max(1, int((tot_w - used_gap) / n))
        center_x, center_y = int(w * current_px), int(h * current_py)
        base_radius = min(tot_w, max_h) * 0.35

        if self.bar_h is None or len(self.bar_h) != n:
            self.bar_h = np.zeros(n)

        beat_scale = 1.0 + (min(vol, 3.0) * 0.15) if cfg.get('enable_logo_pulse', True) else 1.0
        dynamic_radius = base_radius * beat_scale

        ring_color = self.get_color(int(n / 2), n, color_mode)
        cv2.circle(glow_canvas, (center_x, center_y), int(dynamic_radius), ring_color, 4, cv2.LINE_AA)
        cv2.circle(frame, (center_x, center_y), int(dynamic_radius), (255, 255, 255), 2, cv2.LINE_AA)

        # 🔥 PERBAIKAN: RUMUS SKALA 1080p AGAR LED TIDAK MENGGUMPAL 🔥
        scale_f = h / 1080.0
        seg_h = max(2, int(18 * scale_f))     # Diperpanjang agar proporsional di 1080p
        seg_gap = max(1, int(10 * scale_f))   # Jarak direnggangkan agar tidak nempel
        max_segs = max(1, int(max_h / (seg_h + seg_gap)))
        
        # Ketebalan ditipiskan agar tidak tabrakan menjadi gumpalan
        thick_glow = max(2, int(bar_w * 0.4))
        thick_core = max(1, int(bar_w * 0.2))

        for i in range(n):
            target = bars[i] * cfg.get('reactivity', 0.66)
            self.bar_h[i] = (self.bar_h[i] * 0.85) + (target * 0.15)
            height = max(int(15 * scale_f), int(max(idle, self.bar_h[i] * max_h)))
            
            num_segs = int(height / (seg_h + seg_gap))
            if num_segs < 1: num_segs = 1

            angle = (i / n) * math.pi * 2 + (math.pi / 2) + rotation

            for s in range(num_segs):
                r1 = dynamic_radius + (s * (seg_h + seg_gap))
                r2 = r1 + seg_h
                
                x1 = int(center_x + math.cos(angle) * r1)
                y1 = int(center_y + math.sin(angle) * r1)
                x2 = int(center_x + math.cos(angle) * r2)
                y2 = int(center_y + math.sin(angle) * r2)

                color = self.get_color(s, max_segs, color_mode)

                cv2.line(glow_canvas, (x1, y1), (x2, y2), color, thick_glow, cv2.LINE_AA)
                cv2.line(frame, (x1, y1), (x2, y2), color, thick_core, cv2.LINE_AA)

        print(">> Circle bars DIPASTIKAN muncul. Jumlah bar:", n)

    
    
    # ========== LED EQUALIZER (BALOK PUTUS-PUTUS) ==========
    def draw_led_eq(self, frame, glow_canvas, bars, cfg, current_px, current_py, current_wp):
        import cv2, numpy as np
        h, w = frame.shape[:2]
        n = len(bars)
        max_h = h * (cfg.get('max_height', 40) / 100)
        idle, space = int(cfg.get('idle_height', 5)), int(cfg.get('spacing', 3))
        color_mode = cfg.get('color_mode', 'gradient')
        
        tot_w = w * current_wp
        # 🔥 BARU: lebar segmen LED EQ mengikuti cfg.bar_width
        bar_w = int(cfg.get('bar_width', 0) or 0)
        if not bar_w or bar_w <= 0:
            used_gap = min(space * (n - 1), int(tot_w * 0.4))
            bar_w = max(1, int((tot_w - used_gap) / n))
        start_x = int(w * current_px) - int(tot_w / 2)
        base_y = int(h * current_py)
        # 🔥 1. BALOK PUTUS-PUTUS DIBUAT LEBIH KECIL 🔥
        seg_h = 3     # Tinggi per balok (sebelumnya 6 atau 4)
        seg_gap = 2   # Jarak antar balok
        max_segs = max(1, int(max_h / (seg_h + seg_gap)))
        
        if self.bar_h is None or len(self.bar_h) != n: self.bar_h = np.zeros(n)
        
        for i in range(n):
            target = bars[i] * cfg.get('reactivity', 0.66)
            self.bar_h[i] = (self.bar_h[i] * 0.85) + (target * 0.15)
            height = max(10, int(max(idle, self.bar_h[i] * max_h)))
            
            num_segs = int(height / (seg_h + seg_gap))
            if num_segs < 1: num_segs = 1
            
            x1 = start_x + int((tot_w / n) * i)
            x2 = x1 + bar_w
            x1_s, x2_s = max(0, min(w, x1)), max(0, min(w, x2))
            
            # 🔥 2. GRADASI WARNA KE SAMPING (Per Bar 'i'), BUKAN KE ATAS 🔥
            color = self.get_color(i, n, color_mode)
            
            for s in range(num_segs):
                y2 = base_y - (s * (seg_h + seg_gap))
                y1 = y2 - seg_h
                
                y1_s, y2_s = max(0, min(h, y1)), max(0, min(h, y2))
                
                if x2_s > x1_s and y2_s > y1_s:
                    cv2.rectangle(glow_canvas, (x1_s, y1_s), (x2_s, y2_s), color, -1)
                    cv2.rectangle(frame, (x1_s, y1_s), (x2_s, y2_s), color, -1)

    # ========== SPECTRUM BAR DI SEMUA SISI LAYAR (ATAS/BAWAH/KIRI/KANAN) ==========
    def draw_spectrum_edge(self, frame, glow_canvas, bars, cfg):
        import cv2, numpy as np
        h, w = frame.shape[:2]
        n = len(bars)
        if n == 0:
            return

        # Batasi maksimal max_height ke 45% agar tidak mengambil seluruh ruang layar 
        # (jika mencapai 50%, margin tengah akan habis dan error).
        pct = min(45, cfg.get('max_height', 40)) / 100
        max_h = int(h * pct)   # panjang maksimal bar atas & bawah
        max_w = int(w * pct)   # panjang maksimal bar kiri & kanan
        idle = int(cfg.get('idle_height', 5))
        space = int(cfg.get('spacing', 3))
        color_mode = cfg.get('color_mode', 'gradient')
        reactivity = cfg.get('reactivity', 0.66)

        # 1. PERBAIKAN: Mencegah tabrakan antar sisi dengan memberikan margin di sudut.
        margin_x = max_w  # Ruang kosong di kiri & kanan untuk bar horizontal
        margin_y = max_h  # Ruang kosong di atas & bawah untuk bar vertikal
        avail_w = w - (2 * margin_x)
        avail_h = h - (2 * margin_y)

        # 2. PERBAIKAN: Mengurutkan bars agar High Beat (frekuensi rendah/index awal) 
        #    berada di TENGAH, dan sisa frekuensinya menyebar ke pinggir.
        centered_bars = np.zeros(n)
        mid = n // 2
        for i in range(n):
            if i % 2 == 0:
                centered_bars[mid + (i // 2)] = bars[i]
            else:
                centered_bars[mid - (i // 2) - 1] = bars[i]

        if getattr(self, 'bar_h_edge_v', None) is None or len(self.bar_h_edge_v) != n:
            self.bar_h_edge_v = np.zeros(n)
        if getattr(self, 'bar_h_edge_h', None) is None or len(self.bar_h_edge_h) != n:
            self.bar_h_edge_h = np.zeros(n)

        # ---- ATAS & BAWAH (bar tersebar di area tengah layar secara horizontal) ----
        # 🔥 BARU: lebar bar spectrum pinggir layar mengikuti cfg.bar_width
        bar_w = int(cfg.get('bar_width', 0) or 0)
        if not bar_w or bar_w <= 0:
            used_gap = min(space * (n - 1), int(avail_w * 0.4))
            bar_w = max(1, int((avail_w - used_gap) / n))
        for i in range(n):
            target = centered_bars[i] * reactivity  # Menggunakan centered_bars
            self.bar_h_edge_v[i] = (self.bar_h_edge_v[i] * 0.85) + (target * 0.15)
            height = max(2, int(max(idle, self.bar_h_edge_v[i] * max_h)))

            # Start X sekarang ditambahkan dengan margin_x
            x1 = margin_x + i * (bar_w + space)
            x2 = x1 + bar_w
            x1s, x2s = max(0, min(w, x1)), max(0, min(w, x2))
            if x2s <= x1s:
                continue

            # BAWAH: dari tepi bawah nongol ke atas
            y1b, y2b = max(0, h - height), h
            for y in range(y1b, y2b):
                color = self.get_color(y2b - y, max_h, color_mode)
                cv2.line(glow_canvas, (x1s, y), (x2s - 1, y), color, 1)
            core_w = max(1, int((x2s - x1s) * 0.5))
            off_x = ((x2s - x1s) - core_w) // 2
            cv2.rectangle(frame, (x1s + off_x, y1b), (x1s + off_x + core_w, y2b), (255, 255, 255), -1)

            # ATAS: dari tepi atas nongol ke bawah
            y1t, y2t = 0, min(h, height)
            for y in range(y1t, y2t):
                color = self.get_color(y - y1t, max_h, color_mode)
                cv2.line(glow_canvas, (x1s, y), (x2s - 1, y), color, 1)
            cv2.rectangle(frame, (x1s + off_x, y1t), (x1s + off_x + core_w, y2t), (255, 255, 255), -1)

        # ---- KIRI & KANAN (bar tersebar di area tengah layar secara vertikal) ----
        bar_h_seg = max(1, int((avail_h - (space * (n - 1))) / n))
        for i in range(n):
            target = centered_bars[i] * reactivity  # Menggunakan centered_bars
            self.bar_h_edge_h[i] = (self.bar_h_edge_h[i] * 0.85) + (target * 0.15)
            length = max(2, int(max(idle, self.bar_h_edge_h[i] * max_w)))

            # Start Y sekarang ditambahkan dengan margin_y
            y1 = margin_y + i * (bar_h_seg + space)
            y2 = y1 + bar_h_seg
            y1s, y2s = max(0, min(h, y1)), max(0, min(h, y2))
            if y2s <= y1s:
                continue

            # KIRI: dari tepi kiri nongol ke kanan
            x1l, x2l = 0, min(w, length)
            for x in range(x1l, x2l):
                color = self.get_color(x - x1l, max_w, color_mode)
                cv2.line(glow_canvas, (x, y1s), (x, y2s - 1), color, 1)
            core_h = max(1, int((y2s - y1s) * 0.5))
            off_y = ((y2s - y1s) - core_h) // 2
            cv2.rectangle(frame, (x1l, y1s + off_y), (x2l, y1s + off_y + core_h), (255, 255, 255), -1)

            # KANAN: dari tepi kanan nongol ke kiri
            x1r, x2r = max(0, w - length), w
            for x in range(x1r, x2r):
                color = self.get_color(x2r - x, max_w, color_mode)
                cv2.line(glow_canvas, (x, y1s), (x, y2s - 1), color, 1)
            cv2.rectangle(frame, (x1r, y1s + off_y), (x2r, y1s + off_y + core_h), (255, 255, 255), -1)

    # ========== MAIN PROCESS ENGINE ==========
    def process(self, frame, vol, is_hit, bars, cfg):
        import numpy as np
        import math
        import cv2
        import urllib.request
        import os
        
        def safe_num(val, default):
            try: return float(val) if val != "" and val is not None else default
            except: return default

        self.frame_count += 1
        rot_speed = safe_num(cfg.get('rotation_speed', 2), 2) * 0.01
        rotation_offset = self.frame_count * rot_speed

        # 🔥 FITUR BARU: BACKGROUND ZOOM BASS (JEDAG-JEDUG BERDASARKAN SLIDER) 🔥
        enable_bg_pulse = cfg.get('enable_bg_pulse', True)
        bg_zoom_pct = float(cfg.get('bg_zoom', 5.0)) # <-- Mengambil nilai slider dari web
        
        if enable_bg_pulse and vol > 0 and bg_zoom_pct > 0:
            bg_scale = 1.0 + (min(vol, 3.0) * (bg_zoom_pct / 100.0))
            if bg_scale > 1.001:
                h, w = frame.shape[:2]
                new_w, new_h = int(w * bg_scale), int(h * bg_scale)
                resized_bg = cv2.resize(frame, (new_w, new_h), interpolation=cv2.INTER_LINEAR)
                # Crop kembali ke ukuran asli (tengah)
                y_offset = (new_h - h) // 2
                x_offset = (new_w - w) // 2
                frame = resized_bg[y_offset:y_offset+h, x_offset:x_offset+w]

        bar_style = cfg.get('bar_style', 'bottom')
        n = len(bars)
        self._init_rainbow(n)
        
        # ACAKAN GOLDEN RATIO AMAN (TIDAK DISENTUH)
        scattered_bars = np.zeros(n)
        if bar_style in ['bottom', 'center', 'circle', 'radial', 'horizontal', 'frequency_bands', 'led_eq']:
            for i in range(n):
                fraction = (i * 0.618033988749895) % 1.0
                source_idx = int(fraction * n)
                source_idx = max(0, min(n - 1, source_idx))
                scattered_bars[i] = bars[source_idx]
        else:
            scattered_bars = np.copy(bars)

        glow_canvas = np.zeros_like(frame)

        is_double = cfg.get('vis_instance', 'single') == 'double'
        instances = 2 if is_double else 1

        for inst in range(instances):
            px = safe_num(cfg.get('pos_x' if inst == 0 else 'pos_x2', 25 if inst == 0 else 75), 50)/100
            py = safe_num(cfg.get('pos_y' if inst == 0 else 'pos_y2', 85), 85)/100
            wp = (cfg.get('width_pct', 60) * 0.45 / 100) if is_double else (cfg.get('width_pct', 60)/100)

            if bar_style == 'horizontal':
                self.draw_horizontal_bars(frame, glow_canvas, scattered_bars, cfg, px, py, wp)
            elif bar_style == 'led_eq':
                self.draw_led_eq(frame, glow_canvas, scattered_bars, cfg, px, py, wp)
            elif bar_style == 'circle':
                self.draw_circle_bars(frame, glow_canvas, scattered_bars, cfg, rotation_offset, px, py, wp, vol)
            elif bar_style == 'spectrum_edge':
                pass  # digambar sekali di luar loop instance (posisi fix di pinggir layar)
            else:
                self.draw_vertical_bars(frame, glow_canvas, scattered_bars, cfg, px, py, wp)

        if bar_style == 'spectrum_edge':
            self.draw_spectrum_edge(frame, glow_canvas, scattered_bars, cfg)

        if cfg.get('enable_glow', False):
            g_int = int(safe_num(cfg.get('glow_intensity', 15), 15))
            if g_int % 2 == 0: g_int = max(1, g_int - 1)
            glow_canvas = cv2.GaussianBlur(glow_canvas, (g_int, g_int), 0)
            frame = cv2.addWeighted(frame, 1.0, glow_canvas, 0.9, 0)
        
        # Style berbasis lingkaran (cuma 'circle' sekarang) pakai mask lingkaran paksa
        if bar_style in ['circle']:
            if getattr(self, 'logo', None) is None and not getattr(self, 'logo_checked', False):
                self.logo_checked = True
                logo_url = cfg.get('logo_url', '')
                img_data = None
                if logo_url and logo_url.startswith('http'):
                    try:
                        req = urllib.request.urlopen(logo_url)
                        arr = np.asarray(bytearray(req.read()), dtype=np.uint8)
                        img_data = cv2.imdecode(arr, cv2.IMREAD_UNCHANGED)
                    except: pass
                if img_data is None and os.path.exists('static/logo.png'):
                    img_data = cv2.imread('static/logo.png', cv2.IMREAD_UNCHANGED)
                if img_data is not None:
                    if len(img_data.shape) == 3 and img_data.shape[2] == 3:
                        img_data = cv2.cvtColor(img_data, cv2.COLOR_BGR2BGRA)
                    h_img, w_img = img_data.shape[:2]
                    mask = np.zeros((h_img, w_img), dtype=np.uint8)
                    cv2.circle(mask, (w_img//2, h_img//2), min(w_img, h_img)//2, 255, -1)
                    img_data[:, :, 3] = mask
                    self.logo = img_data

            if getattr(self, 'logo', None) is not None:
                h, w = frame.shape[:2]
                for inst in range(instances):
                    px = safe_num(cfg.get('pos_x' if inst == 0 else 'pos_x2', 25 if inst == 0 else 75), 50)/100
                    py = safe_num(cfg.get('pos_y' if inst == 0 else 'pos_y2', 85), 85)/100
                    center_x, center_y = int(w * px), int(h * py)
                    max_h = h * (cfg.get('max_height', 40)/100)
                    wp = (cfg.get('width_pct', 60) * 0.45 / 100) if is_double else (cfg.get('width_pct', 60) / 100)
                    
                    # PENTING: pakai `vol` asli (parameter process()), sama seperti ring & style 'circle'
                    beat_scale = 1.0 + (min(vol, 3.0) * 0.15) if cfg.get('enable_logo_pulse', True) else 1.0
                    dynamic_radius = min(w * wp, max_h) * 0.35 * beat_scale
                    logo_size = int(dynamic_radius * 2) 
                    
                    if logo_size > 0:
                        logo_resized = cv2.resize(self.logo, (logo_size, logo_size))
                        y1_l, x1_l = int(center_y - logo_size // 2), int(center_x - logo_size // 2)
                        y2_l, x2_l = y1_l + logo_size, x1_l + logo_size
                        if y1_l >= 0 and y2_l <= h and x1_l >= 0 and x2_l <= w:
                            alpha_s = logo_resized[:, :, 3] / 255.0
                            alpha_l = 1.0 - alpha_s
                            for c in range(3):
                                frame[y1_l:y2_l, x1_l:x2_l, c] = (alpha_s * logo_resized[:, :, c] + alpha_l * frame[y1_l:y2_l, x1_l:x2_l, c])
                                
        part_amount = int(safe_num(cfg.get('part_amount', 3), 3))
        if part_amount > 0:
            h, w = frame.shape[:2]
            part_size_mult = safe_num(cfg.get('part_size', 1.0), 1.0)
            part_color_mode = cfg.get('part_color_mode', 'solid')
            part_effect = cfg.get('part_effect', 'circle')
            part_opacity = max(0.0, min(1.0, safe_num(cfg.get('part_opacity', 100), 100) / 100.0))

            if is_hit and vol > 1.5:
                for _ in range(part_amount):
                    if part_color_mode == 'rgb':
                        hue = np.random.randint(0, 180)
                        hsv = np.uint8([[[hue, 255, 255]]])
                        bgr = cv2.cvtColor(hsv, cv2.COLOR_HSV2BGR)[0][0]
                        p_color = (int(bgr[0]), int(bgr[1]), int(bgr[2]))
                    else:
                        p_color = self.col_part
                    size0 = np.random.uniform(2, 6) * part_size_mult
                    self.particles.append([
                        np.random.randint(0, w), np.random.randint(0, h),
                        np.random.uniform(-3, 3), np.random.uniform(-3, 3),
                        size0, p_color
                    ])

            alive = []
            spd = 1.0 + (vol * 0.1 * safe_num(cfg.get('part_speed', 1.0), 1.0))
            overlay = frame.copy()
            any_alive = False
            for p in self.particles:
                p[0] += p[2] * spd; p[1] += p[3] * spd; p[4] -= 0.1
                if p[4] > 0:
                    any_alive = True
                    x, y, size, color = int(p[0]), int(p[1]), max(1, int(p[4])), p[5]
                    if part_effect == 'spark':
                        pts = np.array([
                            [x, y - size], [x + size, y], [x, y + size], [x - size, y]
                        ], dtype=np.int32)
                        cv2.fillConvexPoly(overlay, pts, color)
                    elif part_effect == 'glow':
                        cv2.circle(overlay, (x, y), max(1, int(size * 1.8)), tuple(int(c * 0.35) for c in color), -1)
                        cv2.circle(overlay, (x, y), max(1, int(size * 1.1)), tuple(int(c * 0.7) for c in color), -1)
                        cv2.circle(overlay, (x, y), size, color, -1)
                    else:
                        cv2.circle(overlay, (x, y), size, color, -1)
                    alive.append(p)
            self.particles = alive
            if any_alive and part_opacity > 0:
                frame[:] = cv2.addWeighted(frame, 1 - part_opacity, overlay, part_opacity, 0)
        
        # 🔥 GAMBAR FLOATING CARD DI RENDER FINAL
        if cfg.get('use_floating_card', False):
            current_sec = self.frame_count / 30.0
            current_title = ""
            for track in cfg.get('track_schedule', []):
                if track['start'] <= current_sec < track['end']:
                    current_title = track['title']
                    break
            
            if current_title:
                text = f"NOW PLAYING: {current_title}"
                font = cv2.FONT_HERSHEY_DUPLEX
                font_scale = 0.65
                thickness = 2
                (tw, th), baseline = cv2.getTextSize(text, font, font_scale, thickness)
                pad_x, pad_y = 18, 12
                x, y = 40, 60  
                
                overlay = frame.copy()
                cv2.rectangle(overlay, (x, y - th - pad_y), (x + tw + pad_x*2, y + pad_y), (15, 15, 15), -1)
                cv2.rectangle(overlay, (x, y - th - pad_y), (x + tw + pad_x*2, y + pad_y), self.col_top, 2)
                cv2.addWeighted(overlay, 0.85, frame, 0.15, 0, frame)
                cv2.putText(frame, text, (x + pad_x, y), font, font_scale, (255, 255, 255), thickness, cv2.LINE_AA)

        return frame

def hex_to_rgb(h): return tuple(int(str(h).lstrip('#')[i:i+2], 16) for i in (0, 2, 4))

# 🔥 BARU: daftar preset kecepatan render x264 yang diizinkan
RENDER_PRESETS_ALLOWED = ('ultrafast', 'superfast', 'veryfast', 'faster', 'fast', 'medium')

def _run_ffmpeg_zoom(task_id, cmd, err_tag):
    """🔥 BARU v1.5: jalankan ffmpeg untuk mode Zoom — dukung force-stop + ping watchdog.
    stderr ditulis ke file log sementara agar pipe tidak penuh (deadlock) saat encode panjang."""
    log_path = os.path.join(BASE_UPLOAD, f"zoomlog_{task_id}.txt")
    log_f = open(log_path, 'w', encoding='utf-8', errors='ignore')
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_f)
    active_processes[task_id] = proc
    last_ping = time.time()
    try:
        while proc.poll() is None:
            if stop_flags.get(task_id):
                proc.terminate()
                raise Exception("Dibatalkan")
            now_t = time.time()
            if now_t - last_ping > 60:
                last_ping = now_t
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['updated_at'] = now_t
            time.sleep(1)
    finally:
        try: log_f.close()
        except Exception: pass
        active_processes.pop(task_id, None)
    if proc.returncode != 0:
        err_lines = []
        try:
            with open(log_path, 'r', encoding='utf-8', errors='ignore') as f:
                err_lines = f.read().strip().split('\n')
        except Exception: pass
        try: os.remove(log_path)
        except Exception: pass
        raise Exception(f"{err_tag}: " + " | ".join(err_lines[-6:]))
    try: os.remove(log_path)
    except Exception: pass

def render_zoom_core(task_id, audio_path, bg_paths, output_path, duration, cfg, render_preset='ultrafast'):
    """🔥 BARU v1.5: mode Zoom Saja — background + zoom pelan, 100% FFmpeg (tanpa render
    frame Python, tanpa file overlay, tanpa floating card). Setara overlay dari sisi kecepatan.
    Durasi dibagi rata ke semua background. 🔥 v1.6: zoom BOLAK-BALIK halus (siklus 60 detik:
    30 dtk zoom-in ke 1+zoom_pct%, 30 dtk zoom-out kembali) agar terasa hidup."""
    w, h, fps = 1280, 720, 30
    preset_enc = str(render_preset or 'ultrafast').lower()
    if preset_enc not in RENDER_PRESETS_ALLOWED: preset_enc = 'veryfast'
    try: zoom_pct = float(cfg.get('zoom_pct', 10) or 10)
    except Exception: zoom_pct = 10.0
    zoom_pct = max(2.0, min(30.0, zoom_pct))
    zoom_frac = zoom_pct / 100.0

    if not bg_paths: raise Exception("Gallery Background Kosong!")
    duration = float(duration)
    if duration <= 0: raise Exception("Durasi sumber media tidak valid!")

    with db_lock:
        for d in active_tasks:
            if d['id'] == task_id:
                d['status'] = "Rendering Zoom 720p... 🔍"
                d['updated_at'] = time.time()
                d['started_at'] = time.time()
    save_tasks_db()

    seg_dur = duration / len(bg_paths)
    cycle_frames = 60 * fps  # 🔥 v1.6: siklus zoom bolak-balik 60 detik (30 zoom-in + 30 zoom-out), berulang
    seg_files = []
    join_txt = os.path.join(BASE_UPLOAD, f"temp_zj_{task_id}.txt")
    try:
        for i, bg in enumerate(bg_paths):
            if stop_flags.get(task_id): raise Exception("Dibatalkan")
            seg_out = os.path.join(BASE_UPLOAD, f"temp_zs_{task_id}_{i}.mp4")
            seg_files.append(seg_out)
            is_image = bg.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
            vf = (f"scale=2560:1440:force_original_aspect_ratio=increase,crop=2560:1440,"
                  f"zoompan=z='1+{zoom_frac:.4f}*(0.5-0.5*cos(2*PI*on/{cycle_frames}))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s={w}x{h}:fps={fps},"
                  f"format=yuv420p")
            cmd = [get_ffmpeg_path(), '-y']
            if is_image: cmd += ['-loop', '1', '-framerate', str(fps), '-i', bg]
            else: cmd += ['-stream_loop', '-1', '-i', bg]
            cmd += ['-vf', vf, '-an', '-t', f"{seg_dur:.3f}",
                    '-c:v', 'libx264', '-preset', preset_enc, '-crf', '18', seg_out]
            _run_ffmpeg_zoom(task_id, cmd, "FFmpeg Zoom Error")

        # Gabung segmen + tempel audio (video copy; audio AAC default — sama seperti jalur lain)
        with open(join_txt, 'w', encoding='utf-8') as f:
            for sf in seg_files:
                safe_sf = os.path.abspath(sf).replace('\\', '/')
                f.write(f"file '{safe_sf}'\n")
        cmd = [get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0', '-i', join_txt,
               '-i', audio_path, '-map', '0:v', '-map', '1:a',
               '-c:v', 'copy', '-t', str(duration), output_path]
        _run_ffmpeg_zoom(task_id, cmd, "FFmpeg Zoom Join Error")
    finally:
        try: os.remove(join_txt)
        except Exception: pass
        for sf in seg_files:
            try: os.remove(sf)
            except Exception: pass

def render_video_core(task_id, audio_path, bg_paths, output_path, duration, cfg, render_preset='ultrafast'):
    # 🔥 BARU v1.5: mode Zoom Saja → render 100% FFmpeg (paling ringan, setara overlay)
    if cfg.get('zoom_mode'):
        render_zoom_core(task_id, audio_path, bg_paths, output_path, duration, cfg, render_preset)
        return
    # 🔥 1. RESOLUSI FHD 1080p (Lebih Tajam & Jernih) 🔥
    # 🔥 PERINTAH 3: RESOLUSI 720p 🔥
    w, h = 1280, 720; fps = 30; total_f = int(duration * fps)

    # 🔥 BARU: preset kecepatan render (ultrafast = tercepat, file lebih besar)
    preset_enc = str(render_preset or 'ultrafast').lower()
    if preset_enc not in RENDER_PRESETS_ALLOWED: preset_enc = 'veryfast'

    # 🔥 BARU: mode overlay file — lewati render visualizer frame-by-frame (jauh lebih cepat).
    # Cukup tempel background; file overlay visualizer ditempel belakangan via ffmpeg.
    overlay_mode = bool(cfg.get('overlay_mode', False))

    if not overlay_mode:
        c_bot = hex_to_rgb(cfg.get('color_bot', '#10b981'))
        c_top = hex_to_rgb(cfg.get('color_top', '#0ea5e9'))
        c_part = hex_to_rgb(cfg.get('color_part', '#ffffff'))
        bar_c = int(cfg.get('bar_count', 64))

        vis = VisualEngine(c_bot, c_top, c_part)
        audio = AudioBrain(); audio.load(audio_path)
    else:
        vis = None
        audio = None
        bar_c = 0

    bg = BackgroundManager(bg_paths, w, h, fps=fps)
    
    with db_lock:
        for d in active_tasks:
            if d['id'] == task_id:
                d['status'] = "Rendering 720p... ⚡"
                d['updated_at'] = time.time()  # 🔥 BARU: timestamp untuk watchdog stuck
                d['started_at'] = time.time() # 🔥 CATAT WAKTU START
    save_tasks_db()

    cmd = [
        get_ffmpeg_path(), '-y',
        '-f', 'rawvideo', '-vcodec', 'rawvideo', '-s', f'{w}x{h}', '-pix_fmt', 'bgr24', '-r', str(fps),
        '-i', '-',
        '-i', audio_path,
        '-t', str(duration),
        '-c:v', 'libx264', '-preset', preset_enc, '-crf', '18', '-threads', '0', '-pix_fmt', 'yuv420p', output_path
    ]
    
    proc = subprocess.Popen(cmd, stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    active_processes[task_id] = proc
    
    try:
        last_ping = time.time()
        for f in range(total_f):
            if stop_flags.get(task_id):
                raise Exception("Dibatalkan")

            if overlay_mode:
                # 🔥 Mode overlay: tanpa visualizer Python — langsung frame background saja
                frame = bg.get_frame()
            else:
                v, is_hit, bars = audio.get_data(f/fps, bar_c)
                frame = vis.process(bg.get_frame(), v, is_hit, bars, cfg)
            proc.stdin.write(frame.tobytes())
            
            # 🔥 PING WATCHDOG: Lapor ke pos satpam setiap 60 detik agar tidak ditebas
            now_t = time.time()
            if now_t - last_ping > 60:
                last_ping = now_t
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['updated_at'] = now_t
                            
    except Exception as e:
        proc.stdin.close()
        proc.terminate()
        bg.close()
        raise e
        
    proc.stdin.close(); proc.wait(); bg.close()
    active_processes.pop(task_id, None)


# 🔥 BARU: tempel file overlay visualizer di atas video final via ffmpeg (cepat, tanpa render frame).
# overlay_path : file video visualizer (mp4/mov/webm/mkv), idealnya sudah transparan (alpha).
# size_pct     : lebar overlay sebagai % dari lebar video (10-100).
# key_color    : 'none' (file sudah transparan) | 'green' (buang hijau) | 'black' (buang hitam).
def apply_visual_overlay(task_id, video_path, overlay_path, size_pct=60, key_color='none', render_preset='ultrafast'):
    try:
        pct = max(10, min(100, int(size_pct or 60)))
    except:
        pct = 60
    preset_enc = str(render_preset or 'ultrafast').lower()
    if preset_enc not in RENDER_PRESETS_ALLOWED: preset_enc = 'veryfast'

    # Lebar overlay = pct% dari 1280 (resolusi render), tinggi mengikuti aspek (genap).
    ov_w = max(64, int(1280 * pct / 100))
    ov_w = ov_w - (ov_w % 2)

    key_f = ''
    kc = str(key_color or 'none').lower()
    if kc == 'green':
        key_f = ',chromakey=0x00FF00:0.15:0.1'
    elif kc == 'black':
        key_f = ',colorkey=0x000000:0.35:0.2'

    # Overlay di-loop selama durasi video, posisi tengah-bawah.
    fc = (f"[1:v]scale={ov_w}:-2,format=yuva420p{key_f}[ov];"
          f"[0:v][ov]overlay=(W-w)/2:H-h-24:format=yuv420[vout]")

    tmp_out = video_path + ".ovtmp.mp4"
    cmd = [
        get_ffmpeg_path(), '-y',
        '-i', video_path,
        '-stream_loop', '-1', '-i', overlay_path,
        '-filter_complex', fc,
        '-map', '[vout]', '-map', '0:a?',
        '-c:v', 'libx264', '-preset', preset_enc, '-crf', '18',
        '-c:a', 'copy', '-movflags', '+faststart',
        '-shortest', tmp_out,
    ]
    try:
        subprocess.run(cmd, check=True, capture_output=True, text=True)
    except subprocess.CalledProcessError as e:
        err_lines = (e.stderr or '').strip().split('\n')
        real_err = " | ".join(err_lines[-6:])
        raise Exception(f"FFmpeg Overlay Error: {real_err}")
    os.replace(tmp_out, video_path)

# ==========================================
# 🚀 BACKGROUND WORKER (SUDAH DI-TUNE UP NGEBUT)
# ==========================================
def background_worker():
    global channel_cooldowns
    while True:
        task = None
        temp_cooldowns = []
        
        # 🔥 1. GALI ANTREAN: Cari task pertama yang AMAN dari cooldown
        while not render_queue.empty():
            ptask = queue_get()
            pyt_id = ptask['yt_id']

            # 🔥 FASE 3: channel dijeda → keluarkan dari antrean (TIDAK di-render),
            # tandai Dijeda supaya bisa masuk antrean lagi saat dilanjutkan.
            # (Berbeda dengan cooldown yang dikembalikan ke antrean — task jeda
            # sengaja dikeluarkan supaya resume bisa mengantre ulang dengan bersih.)
            if _channel_paused(pyt_id):
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == ptask.get('id'):
                            d['status'] = CHANNEL_PAUSED_STATUS
                            d['updated_at'] = time.time()
                try: save_tasks_db()
                except Exception: pass
                try: render_queue.task_done()
                except Exception: pass
                continue

            # 🔥 FASE 6: kuota PROJECT Google sudah habis (6 upload/hari per
            # project, bukan per channel) → pasang cooldown untuk SEMUA
            # channel di project ini sampai tengah malam WIB, lalu task
            # diperlakukan seperti cooldown biasa (ditunda, bukan dibuang)
            # supaya tidak ada render yang sia-sia.
            _pid = _project_id_of_channel(next((c for c in database_channel if c.get('yt_id') == pyt_id), None) or {})
            if _pid and _project_uploads_today(_pid) >= MAX_UPLOADS_PER_CHANNEL_PER_DAY:
                _cooldown_project_channels(_pid)

            if pyt_id in channel_cooldowns and time.time() < channel_cooldowns[pyt_id]:
                # Jika channel ini masih cooldown, simpan task ke wadah sementara
                sisa_menit = max(1, int((channel_cooldowns[pyt_id] - time.time()) / 60))
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == ptask['id']:
                            d['status'] = f"Antrean Ditunda (Cooldown YT {sisa_menit} mnt) ⏳"
                            d['updated_at'] = time.time()
                temp_cooldowns.append(ptask)
            else:
                # Nemu task yang channel-nya AMAN! Langsung kerjakan.
                task = ptask
                break
        
        # 🔥 2. Kembalikan task yang kena cooldown tadi ke dalam antrean (tetap prioritas)
        if temp_cooldowns:
            save_tasks_db()
            for t in temp_cooldowns:
                queue_put(t)
                render_queue.task_done()
                
        # 🔥 3. Jika semua antrean ternyata sedang cooldown, istirahat sejenak
        if task is None:
            time.sleep(10)
            continue
            
        # --- MULAI PROSES TASK YANG AMAN ---
        task_id = task['id']
        yt_id = task['yt_id']
        
        # Bersihkan status cooldown jika sudah waktunya kedaluwarsa
        if yt_id in channel_cooldowns:
            del channel_cooldowns[yt_id]
            try: persist_cooldown(yt_id)
            except Exception: pass
        
        with db_lock:
            for d in active_tasks:
                if d['id'] == task_id:
                    d['status'] = "Mulai Diproses... ⚙️"
                    d['updated_at'] = time.time()
        save_tasks_db()
        
        temp_files = [
            os.path.join(BASE_UPLOAD, f"temp_a_{task_id}.mp3"),
            os.path.join(BASE_UPLOAD, f"temp_c_{task_id}.txt"),
            os.path.join(BASE_UPLOAD, f"temp_v_{task_id}.mp4"),
            os.path.join(BASE_UPLOAD, f"temp_mv_{task_id}.mp4"),
            os.path.join(BASE_UPLOAD, f"temp_sv_{task_id}.txt"),
            os.path.join(BASE_UPLOAD, f"loop_{task_id}.txt"),
            os.path.join(BASE_UPLOAD, f"temp_sp_{task_id}.mp4"),   # 🔥 v1.2: segmen lagu khusus
            os.path.join(BASE_UPLOAD, f"temp_fm_{task_id}.mp4"),   # 🔥 v1.2: bagian utama final
            os.path.join(BASE_UPLOAD, f"temp_join_{task_id}.txt"), # 🔥 v1.2: daftar gabung main+special
            os.path.join(BASE_DIR, f"static/final_{task_id}.mp4"),
        ]
        try:
            # 🔥 FASE 5 + audit F9a (REVISI): cek disk sebelum render — kalau
            # menipis, bersih-bersih dulu otomatis; baru tolak kalau masih kurang.
            disk_err = _ensure_disk_for_render(task)
            if disk_err:
                raise Exception(disk_err)
            # Pasang antrean resource gate di sini
            acquire_resource("Factory Worker")
            if stop_flags.get(task_id): 
                raise Exception("Dibatalkan")
                
            with db_lock:
                for d in active_tasks:
                    if d['id'] == task_id:
                        d['status'] = "Meracik Aset Gallery... ⚙️"
                        d['updated_at'] = time.time()  # 🔥 BARU: timestamp untuk watchdog stuck
            save_tasks_db()

            # ============== AWAL BLOK SUMBER KONTEN (UPDATE) ==============
            source_mode = task.get('source_mode', 'mix')
            base_audio = os.path.join(BASE_UPLOAD, f"temp_a_{task_id}.mp3")
            track_schedule = []
            # 🔥 v1.2: lagu khusus dirender terpisah (bukan ikut playlist utama)
            special_audio_path = None
            special_duration_sec = 0.0
            special_schedule = []

            if source_mode == 'single_video':
                # --- JALUR BARU: MODE VIDEO UTUH ---
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = "Mencari & Mengekstrak Audio dari Video... ⚙️"
                save_tasks_db()

                # Ambil dari folder khusus base_videos yang baru
                base_videos_dir = get_channel_folder(yt_id, "base_videos")
                video_paths = [os.path.join(base_videos_dir, f) for f in os.listdir(base_videos_dir) if f.lower().endswith(('.mp4', '.mov', '.webm', '.mkv'))]
                if not video_paths: raise Exception("Gallery Video+Audio kosong! Upload MP4 sumber ke tab tersebut.")
                
                # 🔥 BARU: Jumlah video acak per final video (default 1 = perilaku lama)
                vid_req = int(task.get('vid_per_video', 1))
                vid_req = max(1, min(vid_req, len(video_paths)))
                
                # 🔥 BARU: Pilih N video acak BEDA untuk task ini (diacak ulang tiap render).
                # Jadi kalau Final Video 30, masing-masing mendapat 5 video acak berbeda.
                selected_videos = random.sample(video_paths, vid_req)
                source_video = selected_videos[0]
                bg_paths = [source_video] # Video pertama sebagai background utama
                
                # 🔥 BARU: Concat video utuh (video + audio + subtitle burned jadi satu).
                # Karena subtitle sudah di-render permanen di dalam MP4, video digabung
                # tanpa re-encode (copy) agar kualitas & subtitle tidak berubah.
                if vid_req > 1:
                    concat_txt = os.path.join(BASE_UPLOAD, f"temp_sv_{task_id}.txt")
                    with open(concat_txt, 'w', encoding='utf-8') as f:
                        for vp in selected_videos:
                            safe_path = os.path.abspath(vp).replace('\\', '/').replace("'", "'\\''")
                            f.write(f"file '{safe_path}'\n")
                    merged_video = os.path.join(BASE_UPLOAD, f"temp_mv_{task_id}.mp4")
                    try:
                        subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0',
                                        '-i', concat_txt, '-c', 'copy', merged_video],
                                       check=True, capture_output=True)
                    except subprocess.CalledProcessError:
                        raise Exception("Gagal menggabungkan video acak! Pastikan semua video MP4 punya format/kodek sejenis (misal resolusi & fps sama).")
                    try: os.remove(concat_txt)
                    except: pass
                else:
                    merged_video = source_video # 1 video: pakai langsung tanpa concat
                
                # Ekstrak audio dari video gabungan ke temp_a.mp3 (untuk visualizer)
                try:
                    subprocess.run([get_ffmpeg_path(), '-y', '-i', merged_video, '-vn', '-q:a', '0', base_audio], check=True, capture_output=True)
                except subprocess.CalledProcessError:
                    raise Exception(f"Gagal ekstrak audio! Video gabungan mungkin tidak bersuara.")

                probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', base_audio], capture_output=True, text=True, check=True)
                try: base_duration_sec = float(probe.stdout.strip())
                except: raise Exception("Gagal membaca durasi audio dari video utama!")

                # 🔥 BARU: track_schedule berisi SEMUA video acak (urutan concat),
                # supaya Floating Card berganti judul mengikuti nama file tiap segmen.
                # Judul dibersihkan dari nomor depan, misal '06. Langkah Sabar.mp4' → 'Langkah Sabar'
                current_sec = 0.0
                for vp in selected_videos:
                    probe_vp = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', vp], capture_output=True, text=True)
                    try: dur_vp = float(probe_vp.stdout.strip())
                    except: dur_vp = 0.0
                    raw_title = os.path.splitext(os.path.basename(vp))[0]
                    title_vid = re.sub(r'^\d+[\.\)\-\s]*', '', raw_title).strip() if raw_title else raw_title
                    if not title_vid: title_vid = raw_title
                    track_schedule.append({'title': title_vid, 'path': os.path.abspath(vp), 'start': current_sec, 'end': current_sec + dur_vp, 'duration': dur_vp})
                    current_sec += dur_vp

            else:
                # --- JALUR LAMA: MODE KLASIK (MIX MP3) ---
                audio_paths = get_all_audios(yt_id)
                if not audio_paths: raise Exception("Gallery Audio Kosong!")
                
                mp3_req = int(task.get('mp3_per_video', 5))
                mp3_count = min(mp3_req, len(audio_paths))
                selected_audios = audio_paths[:mp3_count]

                current_sec = 0.0
                c_txt = os.path.join(BASE_UPLOAD, f"temp_c_{task_id}.txt")
                
                with open(c_txt, 'w', encoding='utf-8') as f:
                    for ap in selected_audios:
                        safe_path = os.path.abspath(ap).replace('\\', '/').replace("'", "'\\''")
                        f.write(f"file '{safe_path}'\n")
                        probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', ap], capture_output=True, text=True)
                        try: dur = float(probe.stdout.strip())
                        except: dur = 0.0
                        
                        # 🔥 PEMBERSIH JUDUL OTOMATIS (Audio MP3)
                        raw_title = os.path.splitext(os.path.basename(ap))[0]
                        title = re.sub(r'^\d+[\.\)\-\s]+', '', raw_title)
                        title = re.sub(r'\(.*?\)|\[.*?\]', '', title).strip(' -_')
                        if not title: title = raw_title
                        
                        track_schedule.append({'title': title, 'path': safe_path, 'start': current_sec, 'end': current_sec + dur, 'duration': dur})
                        current_sec += dur

                # 🔥 FIX v1.2: lagu khusus TIDAK ikut playlist utama lagi — dirender
                # terpisah & ditempel PALING AKHIR (sebelumnya ikut playlist lalu kepotong
                # -t target_sec / loop, makanya tidak pernah terdengar di akhir video).
                if task.get('use_special_song'):
                    # 🔥 UPDATE 6 Nov 2026: lagu khusus memakai MAKS 2 file pertama
                    # (urut abjad), diputar berurutan di ujung video.
                    sp_paths = get_special_songs(yt_id, count=2)
                    if not sp_paths:
                        raise Exception("Lagu khusus aktif tapi Gallery 'Lagu Khusus' kosong! Upload file MP3 dulu.")
                    special_schedule = []
                    _sp_offset = 0.0
                    for _sp in sp_paths:
                        probe_sp = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', _sp], capture_output=True, text=True)
                        try: _dur_sp = float(probe_sp.stdout.strip())
                        except: _dur_sp = 0.0
                        if _dur_sp <= 0:
                            raise Exception("Gagal membaca durasi lagu khusus!")
                        raw_sp = os.path.splitext(os.path.basename(_sp))[0]
                        title_sp = re.sub(r'^\d+[\.\)\-\s]+', '', raw_sp)
                        title_sp = re.sub(r'\(.*?\)|\[.*?\]', '', title_sp).strip(' -_')
                        if not title_sp: title_sp = raw_sp
                        special_schedule.append({'title': title_sp, 'path': os.path.abspath(_sp), 'start': _sp_offset, 'end': _sp_offset + _dur_sp, 'duration': _dur_sp, 'special': True})
                        _sp_offset += _dur_sp
                    special_duration_sec = _sp_offset
                    if len(sp_paths) == 1:
                        special_audio_path = sp_paths[0]
                    else:
                        # Gabung lagu-lagu khusus jadi 1 file audio untuk segmen penutup
                        sp_txt = os.path.join(BASE_UPLOAD, f"temp_spjoin_{task_id}.txt")
                        with open(sp_txt, 'w') as _spf:
                            for _sp in sp_paths:
                                _spf.write(f"file '{os.path.abspath(_sp).replace(chr(92), '/')}'\n")
                        special_audio_path = os.path.join(BASE_UPLOAD, f"temp_spaudio_{task_id}.mp3")
                        subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0', '-i', sp_txt, '-c:a', 'libmp3lame', '-ar', '44100', special_audio_path], check=True)

                subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0', '-i', c_txt, '-c:a', 'libmp3lame', '-ar', '44100', base_audio], check=True)

                probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', base_audio], capture_output=True, text=True, check=True)
                base_duration_sec = float(probe.stdout.strip())
                
                bg_count = int(task.get('bg_count', 1))
                bg_paths = get_multi_backgrounds(yt_id, count=bg_count)
                if not bg_paths: raise Exception("Gallery Background Kosong!")

            if base_duration_sec <= 0: raise Exception("Durasi sumber media tidak valid!")

            channel_data = next((c for c in database_channel if c['yt_id'] == yt_id), None)
            ch_name = channel_data['name'] if channel_data else "Ziqva Muse Bot FM"
            # ============== AKHIR BLOK SUMBER KONTEN (UPDATE) ==============raise Exception("Gallery Background Kosong!")

            preset = task.get('vis_preset')
            allowed_presets = task.get('vis_presets_allowed', [])
            if task.get('vis_mode') == 'random' or preset == 'random':
                preset = get_random_preset(allowed_presets)
            if not isinstance(preset, dict):
                preset = {"color_bot": "#00d4ff", "color_top": "#7c5cfc", "color_part": "#ffffff", 
                         "pos_x": 50, "pos_y": 85, "width_pct": 60, "max_height": 40, "idle_height": 5, 
                         "bar_count": 64, "reactivity": 0.66, "gravity": 0.08, "spacing": 3, 
                         "part_amount": 3, "part_speed": 1.0, "bar_style": "bottom"}

            preset['yt_id'] = yt_id
            preset['use_floating_card'] = task.get('use_floating_card', False)
            preset['track_schedule'] = track_schedule
            preset['channel_name'] = ch_name
            preset['source_mode'] = source_mode
            # 🔥 BARU: mode overlay file — render Python dilewati, overlay ditempel via ffmpeg
            preset['overlay_mode'] = (task.get('vis_mode') == 'overlay')
            # 🔥 BARU v1.5: mode Zoom Saja — render 100% FFmpeg, tanpa visualizer Python
            preset['zoom_mode'] = (task.get('vis_mode') == 'zoom')
            try: preset['zoom_pct'] = float(task.get('vis_zoom_pct', 10) or 10)
            except Exception: preset['zoom_pct'] = 10.0

            base_video = os.path.join(BASE_UPLOAD, f"temp_v_{task_id}.mp4")
            final_video = os.path.join(BASE_DIR, f"static/final_{task_id}.mp4")

            if stop_flags.get(task_id): raise Exception("Dibatalkan")
            with db_lock:
                for d in active_tasks:
                    if d['id'] == task_id: d['status'] = "Rendering Base FFmpeg... ⚡"
            save_tasks_db()

            render_video_core(task_id, base_audio, bg_paths, base_video, base_duration_sec, preset,
                            render_preset=task.get('render_preset', 'ultrafast'))
            if stop_flags.get(task_id): raise Exception("Dibatalkan")

            target_hours = float(task.get('target_duration_hours', 1))
            target_sec = target_hours * 3600

            # 🔥 FIX v1.2: lagu khusus ditempel PALING AKHIR, di luar loop/potong.
            # Bagian utama dipotong ke (target - durasi lagu khusus), lalu segmen
            # lagu khusus digabung di belakangnya — dijamin terdengar sampai habis.
            has_special = bool(special_audio_path and special_duration_sec > 0)
            main_target = target_sec - special_duration_sec if has_special else target_sec
            if main_target < 0: main_target = 0.0

            # --- render segmen lagu khusus (durasi pendek) ---
            special_video = None
            if has_special:
                if stop_flags.get(task_id): raise Exception("Dibatalkan")
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = "Rendering Lagu Khusus... 🎵"
                save_tasks_db()
                special_video = os.path.join(BASE_UPLOAD, f"temp_sp_{task_id}.mp4")
                preset_sp = dict(preset); preset_sp['track_schedule'] = special_schedule
                render_video_core(task_id, special_audio_path, bg_paths, special_video, special_duration_sec, preset_sp,
                                render_preset=task.get('render_preset', 'ultrafast'))
                if stop_flags.get(task_id): raise Exception("Dibatalkan")

            # --- loop/potong bagian utama ke main_target ---
            loop_out = os.path.join(BASE_UPLOAD, f"temp_fm_{task_id}.mp4") if has_special else final_video
            loop_count = math.ceil(main_target / base_duration_sec) if main_target > 0 else 0

            if loop_count > 1:
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = f"Auto-Looping {loop_count}x ke {target_hours} Jam... 🚀"
                save_tasks_db()

                loop_txt = os.path.join(BASE_UPLOAD, f"loop_{task_id}.txt")
                with open(loop_txt, 'w', encoding='utf-8') as f:
                    for _ in range(loop_count):
                        safe_path_vid = os.path.abspath(base_video).replace('\\', '/')
                        f.write(f"file '{safe_path_vid}'\n")

                if stop_flags.get(task_id): raise Exception("Dibatalkan")

                try:
                    # Kembali ke concat tapi dengan pengaman timestamp (+genpts)
                    subprocess.run([get_ffmpeg_path(), '-y', '-fflags', '+genpts', '-f', 'concat', '-safe', '0', '-i', loop_txt, '-c', 'copy', '-t', str(main_target), loop_out], check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as e:
                    # Ambil 6 baris terakhir agar error aslinya tidak tertutup statistik ukuran
                    err_lines = e.stderr.strip().split('\n')
                    real_err = " | ".join(err_lines[-6:])
                    raise Exception(f"FFmpeg Concat Error: {real_err}")
            elif main_target > 0:
                if stop_flags.get(task_id): raise Exception("Dibatalkan")

                try:
                    subprocess.run([get_ffmpeg_path(), '-y', '-i', base_video, '-c', 'copy', '-t', str(main_target), loop_out], check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as e:
                    err_lines = e.stderr.strip().split('\n')
                    real_err = " | ".join(err_lines[-6:])
                    raise Exception(f"FFmpeg Final Error: {real_err}")

            # --- gabung bagian utama + lagu khusus (lagu khusus PALING AKHIR) ---
            if has_special:
                if stop_flags.get(task_id): raise Exception("Dibatalkan")
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = "Menempel Lagu Khusus di Akhir... 🎵"
                save_tasks_db()
                try:
                    if main_target > 0:
                        join_txt = os.path.join(BASE_UPLOAD, f"temp_join_{task_id}.txt")
                        with open(join_txt, 'w', encoding='utf-8') as f:
                            for vp in [loop_out, special_video]:
                                safe_vp = os.path.abspath(vp).replace('\\', '/')
                                f.write(f"file '{safe_vp}'\n")
                        subprocess.run([get_ffmpeg_path(), '-y', '-fflags', '+genpts', '-f', 'concat', '-safe', '0', '-i', join_txt, '-c', 'copy', final_video], check=True, capture_output=True, text=True)
                    else:
                        # Target lebih pendek dari lagu khusus: pakai lagu khusus saja (dipotong target)
                        subprocess.run([get_ffmpeg_path(), '-y', '-i', special_video, '-c', 'copy', '-t', str(target_sec), final_video], check=True, capture_output=True, text=True)
                except subprocess.CalledProcessError as e:
                    err_lines = e.stderr.strip().split('\n')
                    real_err = " | ".join(err_lines[-6:])
                    raise Exception(f"FFmpeg Special-Song Error: {real_err}")

            # 🔥 BARU: mode overlay file — tempel visualizer via ffmpeg (cepat, tanpa render frame Python)
            if task.get('vis_mode') == 'overlay':
                ov_path = get_overlay_file(yt_id, task.get('vis_overlay_file'))
                if not ov_path:
                    raise Exception("Gallery Overlay kosong! Upload file visualizer (mp4/mov/webm) ke tab '✨ Overlay' dulu.")
                if stop_flags.get(task_id): raise Exception("Dibatalkan")
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['status'] = "Menempel Overlay Visualizer... ✨"
                            d['updated_at'] = time.time()
                save_tasks_db()
                apply_visual_overlay(task_id, final_video, ov_path,
                                     size_pct=task.get('vis_overlay_size', 60),
                                     key_color=task.get('vis_overlay_key', 'none'),
                                     render_preset=task.get('render_preset', 'ultrafast'))

            # 🔥 FASE 3: channel dijeda saat render berjalan → jangan upload.
            # Task tetap aktif berstatus Dijeda; saat dilanjutkan ia akan
            # di-render ulang dari awal (biaya render ulang, tapi tidak ada
            # video yang tayang selagi channel dijeda).
            if _channel_paused(yt_id):
                raise _PausedAbort()

            # 🔥 FASE 5: QC gate pra-upload — periksa hasil render SEBELUM
            # di-upload. Gagal QC → _QCFailure → riwayat "Gagal QC" + notif
            # Telegram + tombol Retry (retry = render ulang dari awal).
            with db_lock:
                for d in active_tasks:
                    if d['id'] == task_id:
                        d['status'] = "Memeriksa Kualitas Video (QC)... 🔍"
                        d['updated_at'] = time.time()
            save_tasks_db()
            qc_failures = run_qc_checks(task, final_video, yt_id)
            if qc_failures:
                raise _QCFailure(" | ".join(qc_failures))

            if channel_data:
                creds_list = channel_data.get('creds_list', [channel_data.get('creds_json')])
                upload_berhasil = False
                pesan_error = "Token API Tidak Ditemukan/Kosong!" 
                
                # 🔥 FASE 5 + audit F8: retry upload dengan backoff eksponensial.
                # 1 percobaan + maks 2x retry (jeda 10 dtk lalu 20 dtk), TANPA
                # render ulang — file final_video dipakai lagi tiap percobaan.
                # Hanya error SEMENTARA yang di-retry; permanen langsung berhenti.
                for upload_attempt in range(1, UPLOAD_MAX_ATTEMPTS + 1):
                    if upload_attempt > 1:
                        with db_lock:
                            for d in active_tasks:
                                if d['id'] == task_id:
                                    d['status'] = f"Upload Gagal Sementara, Auto-Retry {upload_attempt}/{UPLOAD_MAX_ATTEMPTS}... 🔌"
                                    d['updated_at'] = time.time()
                        save_tasks_db()
                    for index_kunci, cred_str in enumerate(creds_list):
                        if not cred_str: continue
                        try:
                            creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                            if creds.expired and creds.refresh_token: 
                                creds.refresh(Request())
                            
                            youtube = build('youtube', 'v3', credentials=creds)
                            try: sch_obj = datetime.strptime(task['publish_date'], "%Y-%m-%d %H:%M")
                            except: raise Exception("Format tanggal salah")
                        
                            raw_tags = task.get('tags', '')
                            clean_tags = raw_tags.replace('#', '').replace('<', '').replace('>', '').replace('"', '')
                            temp_tags = [t.strip() for t in clean_tags.split(',') if t.strip()]
                        
                            tags_list = []
                            char_count = 0
                            for t in temp_tags:
                                if char_count + len(t) <= 400:
                                    tags_list.append(t)
                                    char_count += len(t) + 1
                        
                            if not tags_list: tags_list = ['wavepush']
                        
                            body = {
                                'snippet': {'title': task['title'], 'description': task.get('description', ''), 'tags': tags_list, 'categoryId': '10'},
                                'status': {'privacyStatus': task.get('privacy', 'public')}
                            }
                            if sch_obj > datetime.now():
                                wib = ZoneInfo("Asia/Jakarta")
                                sch_aware = sch_obj.replace(tzinfo=wib)
                                sch_utc = sch_aware.astimezone(timezone.utc)
                                body['status']['publishAt'] = sch_utc.strftime("%Y-%m-%dT%H:%M:%S.000Z")
                                body['status']['privacyStatus'] = 'private'
                            
                            media = MediaFileUpload(final_video, chunksize=1024*1024*5, resumable=True)
                            req = youtube.videos().insert(part=','.join(body.keys()), body=body, media_body=media)
                            resp = None
                        
                            max_retries = 5
                            retry_count = 0
                        
                            while resp is None:
                                if stop_flags.get(task_id): raise Exception("Dibatalkan")
                                try:
                                    status, resp = req.next_chunk()
                                    if status:
                                        with db_lock:
                                            for d in active_tasks:
                                                if d['id'] == task_id:
                                                    d['status'] = f"Mengunggah (Key {index_kunci+1})... {int(status.progress()*100)}% 🚀"
                                                    d['updated_at'] = time.time()
                                        save_tasks_db()
                                    retry_count = 0 
                                except HttpError as e:
                                    if e.resp.status < 500:
                                        raise e
                                    else:
                                        retry_count += 1
                                        if retry_count > max_retries: 
                                            raise Exception("Server YouTube Down/Timeout setelah 5x percobaan.")
                                        with db_lock:
                                            for d in active_tasks:
                                                if d['id'] == task_id: d['status'] = f"Koneksi Sinyal Lemah, Auto-Retry ({retry_count}/{max_retries})... 🔌"
                                        save_tasks_db()
                                        time.sleep(10)
                                except Exception as e:
                                    retry_count += 1
                                    if retry_count > max_retries: 
                                        raise Exception("Koneksi VPS Putus setelah dicoba 5x berturut-turut.")
                                    with db_lock:
                                        for d in active_tasks:
                                            if d['id'] == task_id: d['status'] = f"Koneksi VPS Putus, Auto-Retry ({retry_count}/{max_retries})... 🔌"
                                    save_tasks_db()
                                    time.sleep(10)
                        
                            video_id = resp.get('id')
                            # 🔥 FASE 4: simpan video_id di task supaya statistik per
                            # video bisa diambil (sebelumnya hanya ada di link status).
                            try:
                                with db_lock:
                                    for d in active_tasks:
                                        if d['id'] == task_id:
                                            d['video_id'] = video_id
                                            d['video_url'] = f"https://youtu.be/{video_id}"
                                save_tasks_db()
                            except Exception:
                                pass

                            thumb_path = get_and_consume_thumbnail(yt_id)
                            if thumb_path and os.path.exists(thumb_path):
                                try:
                                    with db_lock:
                                        for d in active_tasks:
                                            if d['id'] == task_id: d['status'] = "Memasang Thumbnail... 🖼️"
                                    save_tasks_db()
                                    youtube.thumbnails().set(videoId=video_id, media_body=MediaFileUpload(thumb_path)).execute()
                                    with db_lock:
                                        for d in active_tasks:
                                            if d['id'] == task_id: d['thumb_status'] = "Terpasang ✓"
                                    # 🔥 UPDATE: file TIDAK dihapus lagi — thumbnail dipilih acak tiap video,
                                    # jadi file bisa dipakai ulang oleh video berikutnya
                                except Exception as thumb_err:
                                    # 🔥 BARU: error thumbnail kini tercatat di Task Monitor, tidak lagi diam
                                    with db_lock:
                                        for d in active_tasks:
                                            if d['id'] == task_id: d['thumb_status'] = f"Gagal: {str(thumb_err)[:80]}"
                                    save_tasks_db()
                            else:
                                # Kalau folder kosong / tidak ada gambar yang bisa dibuka, catat status
                                with db_lock:
                                    for d in active_tasks:
                                        if d['id'] == task_id: d['thumb_status'] = "Tidak ada thumbnail valid"
                                save_tasks_db()
                                
                            try:
                                if task.get('playlist_id'):
                                    youtube.playlistItems().insert(part='snippet', body={'snippet': {'playlistId': task['playlist_id'], 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}}).execute()
                            except: pass
                            move_to_history(task_id, f"Tayang! ✅ <a href='https://youtu.be/{video_id}' target='_blank'>[Lihat]</a>")
                            notify_event("✅", task['title'], f"udah tayang di YouTube {ch_name} 😎\nJadwal tayang: {task['publish_date']}")
                            upload_berhasil = True
                            # 🔥 FASE 5: 1 sukses → reset hitungan gagal beruntun.
                            _record_channel_success(yt_id)
                            # 🔥 BARU v1.4: batas 6 upload/hari/channel → cooldown sampai tengah malam WIB,
                            # besok lanjut otomatis. Task antre tetap aman menunggu.
                            try:
                                cnt, capped = track_daily_upload(yt_id)
                                if capped:
                                    channel_cooldowns[yt_id] = _next_midnight_wib_ts()
                                    try: persist_cooldown(yt_id)
                                    except Exception: pass
                                    notify_event("⏸️", f"Cooldown harian {ch_name}",
                                                 f"Udah {cnt} video tayang hari ini (batas {MAX_UPLOADS_PER_CHANNEL_PER_DAY}/hari) 😌 Saya jeda dulu, besok lanjut otomatis.")
                                elif cnt == MAX_UPLOADS_PER_CHANNEL_PER_DAY - 1:
                                    # 🔥 FASE 1 MONITORING: peringatan kuota menipis — hanya
                                    # sekali, tepat saat counter menjadi 5/6 (tinggal 1 video).
                                    notify_event("⚠️", f"Kuota hampir habis {ch_name}",
                                                 f"Udah {cnt}/{MAX_UPLOADS_PER_CHANNEL_PER_DAY} video hari ini, sisa 1 lagi 😌 Saya irit-irit dulu ya, reset tengah malam WIB.")
                                # 🔥 FASE 6: kuota dihitung PER PROJECT Google.
                                # Kalau total upload project ini sudah 6 hari ini,
                                # cooldown semua channel di project yang sama
                                # (channel yang barusan upload sudah di-cooldown
                                # di atas; saudara satu project ikut dijeda).
                                _pid = _project_id_of_channel(channel_data) if channel_data else None
                                if _pid:
                                    ptotal = _project_uploads_today(_pid)
                                    if ptotal >= MAX_UPLOADS_PER_CHANNEL_PER_DAY:
                                        _cooldown_project_channels(_pid)
                                        if not capped:
                                            notify_event("⏸️", "Kuota project Google habis",
                                                         f"Project {_pid} udah {ptotal}/{MAX_UPLOADS_PER_CHANNEL_PER_DAY} upload hari ini 😌 Channel-channel di project ini saya jeda dulu, besok lanjut otomatis.")
                                    elif ptotal == MAX_UPLOADS_PER_CHANNEL_PER_DAY - 1:
                                        notify_event("⚠️", "Kuota project Google hampir habis",
                                                     f"Project {_pid} udah {ptotal}/{MAX_UPLOADS_PER_CHANNEL_PER_DAY} hari ini, sisa 1 lagi 😌 Saya irit-irit dulu ya.")
                            except Exception:
                                pass
                            break
                        
                        except HttpError as e:
                            try:
                                err_info = json.loads(e.content.decode('utf-8'))
                                reason = err_info['error']['errors'][0]['reason']
                            except:
                                reason = str(e)
                            
                            if "quotaExceeded" in reason:
                                pesan_error = f"Limit Kuota Harian API Habis!"
                                continue 
                            elif "uploadLimitExceeded" in reason:
                                pesan_error = "Limit Upload Harian Channel Tercapai!"
                                # 🔥 FIX: cooldown s.d. reset kuota (14:00/15:00 WIB), bukan 24 jam mentah
                                channel_cooldowns[yt_id] = get_quota_reset_ts()
                                try: persist_cooldown(yt_id)
                                except Exception: pass
                                break
                            elif "rateLimitExceeded" in reason:
                                pesan_error = "Rate Limit (Terlalu Cepat) - Auto Cooldown 30 Menit"
                                channel_cooldowns[yt_id] = time.time() + 1800
                                try: persist_cooldown(yt_id)
                                except Exception: pass
                                break
                            else:
                                pesan_error = f"Ditolak YT: {reason}"
                                break
                        except Exception as e:
                            err_str = str(e).lower()
                            if "invalid_grant" in err_str or "expired" in err_str or "revoked" in err_str:
                                # 🔥 FIX: token dicabut/kedaluwarsa TIDAK sembuh dengan menunggu —
                                # jangan pasang cooldown 24 jam, cukup minta taut ulang.
                                pesan_error = "Sesi Kedaluwarsa (Tautkan Ulang!)"
                            elif "timeout" in err_str or "connection" in err_str or "broken" in err_str:
                                pesan_error = "Koneksi VPS Putus/Timeout"
                            else:
                                pesan_error = f"Error: {str(e)[:40]}"
                            break
                        
                    if upload_berhasil:
                        break
                    # Gagal pada percobaan ini — hanya error sementara yang
                    # di-retry (backoff eksponensial); permanen langsung berhenti.
                    if not _upload_error_transient(pesan_error) or upload_attempt >= UPLOAD_MAX_ATTEMPTS:
                        break
                    time.sleep(UPLOAD_RETRY_BACKOFF_BASE * (2 ** (upload_attempt - 1)))
                if not upload_berhasil:
                    if "API Habis" in pesan_error:
                        # 🔥 FIX: cooldown s.d. reset kuota (14:00/15:00 WIB), bukan 24 jam mentah
                        channel_cooldowns[yt_id] = get_quota_reset_ts()
                        try: persist_cooldown(yt_id)
                        except Exception: pass
                    raise Exception(pesan_error)
            else:
                move_to_history(task_id, f"Render Selesai ✅ <a href='/static/final_{task_id}.mp4' target='_blank'>[Download]</a>")
                notify_event("✅", task['title'], f"Render beres, Bos! Saya upload ke YouTube {ch_name} ya 😎")
                # 🔥 FASE 5: render sukses juga mereset hitungan gagal beruntun.
                _record_channel_success(yt_id)

        except _QCFailure as e:
            # 🔥 FASE 5: gagal QC — JANGAN upload. Task ke riwayat dengan alasan
            # yang jelas + notif Telegram + tombol Retry (retry = render ulang).
            reason = str(e)[:200]
            move_to_history(task_id, f"Gagal QC 🛑 ({reason[:100]})")
            _record_channel_failure(yt_id, task['title'], f"QC: {reason}")
            if not stop_flags.get(task_id):
                retry_markup = {"inline_keyboard": [[
                    {"text": "🔁 Coba Lagi", "callback_data": f"retry:{task_id}"}
                ]]}
                notify_event("🛑", task['title'],
                             f"Bos, video ini nggak lolos QC saya 😅\nAlasan: {reason[:120]}\nSaya tahan dulu biar nggak tayang yang jelek-jelek — pencet Coba Lagi kalau mau saya render ulang 💪",
                             reply_markup=retry_markup)

        except _PausedAbort:
            # 🔥 FASE 3: dibatalkan karena channel dijeda — BUKAN gagal.
            # Task tetap di antrean aktif berstatus Dijeda (tidak masuk riwayat,
            # tidak kirim notif gagal) supaya bisa lanjut saat resume.
            with db_lock:
                for d in active_tasks:
                    if d['id'] == task_id:
                        d['status'] = CHANNEL_PAUSED_STATUS
                        d['updated_at'] = time.time()
            save_tasks_db()
        except Exception as e:
            err_msg = str(e)
            if "Limit" in err_msg or "Cooldown" in err_msg or "Habis" in err_msg:
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['status'] = f"Gagal Upload, Antre Ulang ({err_msg}) ⏳"
                save_tasks_db()
                queue_put(task)
            else:
                move_to_history(task_id, f"Gagal ❌ ({err_msg})")
                # 🔥 FASE 5: catat kegagalan beruntun untuk auto-pause channel.
                # Pembatalan manual (stop flag / "Dibatalkan") BUKAN kegagalan
                # channel — jangan dihitung.
                if not stop_flags.get(task_id) and "Dibatalkan" not in err_msg:
                    _record_channel_failure(yt_id, task['title'], err_msg)
                # 🔥 FIX: kalau digagalkan watchdog (stop flag), JANGAN kirim
                # notif gagal lagi — watchdog sudah mengirim notifikasinya sendiri.
                if not stop_flags.get(task_id):
                    # 🔥 FASE 1 MONITORING: alert gagal + tombol inline Retry.
                    # Dikirim langsung (bypass buffer) supaya tombolnya menempel
                    # di pesan task ini, bukan di pesan gabungan.
                    retry_markup = {"inline_keyboard": [[
                        {"text": "🔁 Coba Lagi", "callback_data": f"retry:{task_id}"}
                    ]]}
                    notify_event("❌", task['title'],
                                 f"Bos, ada yang gagal nih 😅 Video ini gagal di channel {ch_name} — penyebab: {err_msg[:120]}. Tenang, saya pantau terus — tinggal pencet Coba Lagi ya 💪",
                                 reply_markup=retry_markup)
        finally:
            # Lepas antrean agar Clipper bisa jalan
            release_resource("Factory Worker")
            
            for path in temp_files:
                try: os.remove(path)
                except: pass
            stop_flags.pop(task_id, None)
            active_processes.pop(task_id, None)
            render_queue.task_done()

threading.Thread(target=background_worker, daemon=True).start()

# ==========================================
# 📊 API ENDPOINTS (LANJUTAN)
# ==========================================
@app.route('/')
def main_hub_landing():
    # Halaman pertama yang terbuka saat IP diakses
    return render_template('hub.html')

@app.route('/factory')
def factory_main_dashboard():
    # Dashboard Factory
    return render_template('index.html')

@app.route('/clipper')
def clipper_main_dashboard():
    # Dashboard Clipper
    return render_template('clipper.html')

@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    return jsonify(system_notifications)

@app.route('/api/notifications/clear', methods=['POST'])
def clear_notifications():
    global system_notifications
    with db_lock:
        system_notifications.clear()
    return jsonify({"status": "success"})

@app.route('/api/get_dashboard_stats')
def get_dashboard_stats():
    sys = get_system_stats()
    return jsonify({
        "channels": len(database_channel), "active_tasks": len(active_tasks), "history_tasks": len(history_tasks),
        "sys_cpu": sys["cpu"], "sys_ram_pct": sys["ram_pct"], "sys_ram_text": f"{sys['ram_used']}GB / {sys['ram_total']}GB"
    })

@app.route('/api/get_youtube_analytics')
def get_youtube_analytics():
    data = []
    for c in database_channel:
        views, subs, videos = 0, 0, 0
        # 🔥 FIX v1.3: failover antar key seperti upload — jangan cuma pakai Key #1
        # (Key #1 yang mati bikin grafik analitik kosong walau key lain hidup).
        creds_list = c.get('creds_list', [c.get('creds_json')])
        for cred_str in creds_list:
            if not cred_str:
                continue
            try:
                creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                if creds.expired and creds.refresh_token: creds.refresh(Request())
                youtube = build('youtube', 'v3', credentials=creds)
                res = youtube.channels().list(part="statistics", id=c['yt_id']).execute()
                if res.get('items'):
                    stats = res['items'][0]['statistics']
                    views = int(stats.get('viewCount', 0))
                    subs = int(stats.get('subscriberCount', 0))
                    videos = int(stats.get('videoCount', 0))
                break
            except Exception:
                continue
        data.append({"yt_id": c["yt_id"], "name": c["name"], "views": views, "subs": subs, "watch_hours": 0, "videos": videos})
    return jsonify(data)

# ==========================================
# 📊 FASE 4 STATISTIK PER VIDEO
# ==========================================
# Per-video: views/likes/komentar via YouTube Data API v3 videos().list
# (biaya 1 unit kuota per panggilan, maks 50 video/panggilan).
# Jujur soal batas API: Data API TIDAK menyediakan subscriber per video —
# itu hanya ada di YouTube Analytics API yang butuh scope
# 'youtubeAnalytics.readonly' (token lama tidak punya scope itu, jadi
# tidak dipanggil). Subs channel tetap dari channels().list (endpoint lama).
# video_id disimpan di task saat upload sukses (field baru); riwayat lama
# di-backfill dari link youtu.be di status. Cache 1 jam/channel di
# video_stats_cache.json (di-gitignore) + snapshot harian {tanggal,
# video_id, views} sebagai fondasi lingkaran analitik Fase 5/6.
VIDEO_STATS_CACHE_FILE = os.path.join(BASE_DIR, 'video_stats_cache.json')
VIDEO_STATS_CACHE_TTL = 3600  # detik
_VIDEO_ID_RE = re.compile(r'(?:youtu\.be/|youtube\.com/watch\?v=)([A-Za-z0-9_-]{11})')

def _extract_video_id_from_status(status):
    if not status:
        return None
    m = _VIDEO_ID_RE.search(str(status))
    return m.group(1) if m else None

def _history_task_video_id(t):
    return t.get('video_id') or _extract_video_id_from_status(t.get('status', ''))

def _history_task_yt_id(t):
    return t.get('yt_id') or (t.get('blueprint') or {}).get('yt_id')

def _load_video_stats_cache():
    try:
        if os.path.exists(VIDEO_STATS_CACHE_FILE):
            with open(VIDEO_STATS_CACHE_FILE, 'r') as f:
                d = json.load(f)
            if isinstance(d, dict) and isinstance(d.get('videos'), dict):
                return d
    except Exception:
        pass
    return {"videos": {}, "snapshots": []}

def _save_video_stats_cache(cache):
    try:
        tmp = VIDEO_STATS_CACHE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(cache, f)
        os.replace(tmp, VIDEO_STATS_CACHE_FILE)
    except Exception:
        pass

def _record_video_snapshots(cache, stats_by_id, today_str):
    """Satu snapshot {tanggal, video_id, views} per video per hari.
    Fondasi tren untuk lingkaran analitik (formula menang vs sepi)."""
    try:
        seen = {(s.get('date'), s.get('video_id')) for s in cache.get('snapshots', [])}
        for vid, st in stats_by_id.items():
            key = (today_str, vid)
            if key not in seen:
                cache.setdefault('snapshots', []).append(
                    {"date": today_str, "video_id": vid, "views": int(st.get('views', 0))})
                seen.add(key)
        # Batasi 90 hari terakhir supaya file tidak membengkak.
        cutoff = (_wib_now() - timedelta(days=90)).strftime("%Y-%m-%d")
        cache['snapshots'] = [s for s in cache.get('snapshots', []) if s.get('date', '') >= cutoff]
    except Exception:
        pass

def _build_youtube_client_for_channel(channel):
    """Kembalikan (youtube_client, None) atau (None, pesan_error).
    Failover antar kredensial seperti endpoint analytics lama."""
    creds_list = channel.get('creds_list', [channel.get('creds_json')])
    tried = False
    last_err = ''
    for cred_str in creds_list:
        if not cred_str:
            continue
        tried = True
        try:
            creds = Credentials.from_authorized_user_info(json.loads(cred_str))
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            return build('youtube', 'v3', credentials=creds), None
        except Exception as e:
            last_err = str(e)
            continue
    if not tried:
        return None, "Tidak ada kredensial tersimpan — taut ulang channel."
    return None, f"Token kedaluwarsa / tidak bisa refresh ({last_err[:80]}). Taut ulang channel."

def _fetch_channel_video_stats(youtube, video_ids):
    """videos().list per 50 id. Kembalikan {video_id: {views, likes, comments, published_at}}."""
    out = {}
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        try:
            res = youtube.videos().list(part="statistics,snippet", id=",".join(chunk)).execute()
        except Exception:
            continue
        for item in res.get('items', []):
            st = item.get('statistics', {})
            sn = item.get('snippet', {})
            out[item.get('id')] = {
                "views": int(st.get('viewCount', 0)),
                "likes": int(st.get('likeCount', 0)),
                "comments": int(st.get('commentCount', 0)),
                "published_at": sn.get('publishedAt', ''),
            }
    return out

@app.route('/api/get_video_stats')
def get_video_stats():
    force = request.args.get('force') == '1'
    now_wib = _wib_now()
    today_str = now_wib.strftime("%Y-%m-%d")
    cache = _load_video_stats_cache()
    cached_videos = cache.get('videos', {})

    # Kumpulkan video yang tayang dari riwayat (video_id baru / backfill link lama).
    by_channel = {}
    for t in history_tasks:
        vid = _history_task_video_id(t)
        if not vid:
            continue
        if not t.get('video_id') and "Tayang" not in str(t.get('status', '')):
            continue
        yt_id = _history_task_yt_id(t)
        if not yt_id:
            continue
        by_channel.setdefault(yt_id, {})[vid] = t

    name_by_yt = {c.get('yt_id'): c.get('name', c.get('yt_id')) for c in database_channel}
    videos_out = []
    channels_out = []
    cache_used = True
    for yt_id, tasks in by_channel.items():
        ch_name = name_by_yt.get(yt_id, yt_id)
        entry = {"yt_id": yt_id, "name": ch_name, "status": "ok", "error": None}
        ids = list(tasks.keys())
        fresh = {}
        stale = []
        for vid in ids:
            cv = cached_videos.get(vid)
            if (not force and cv and (time.time() - float(cv.get('fetched_at', 0)) < VIDEO_STATS_CACHE_TTL)):
                fresh[vid] = cv
            else:
                stale.append(vid)
        fetched = {}
        if stale:
            # Negative-cache: channel yang fetch-nya gagal tidak dicoba lagi
            # dalam 1 jam (TTL sama) supaya dashboard tidak menghantam API
            # yang rusak di setiap buka halaman.
            errs = cache.setdefault('channel_errors', {})
            prev_err = errs.get(yt_id)
            if prev_err and (time.time() - float(prev_err.get('at', 0)) < VIDEO_STATS_CACHE_TTL):
                entry["status"] = "error"
                entry["error"] = prev_err.get('error')
            else:
                channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
                if channel is None:
                    entry["status"] = "error"
                    entry["error"] = "Channel sudah tidak terhubung — statistik tidak bisa diambil."
                    errs[yt_id] = {"error": entry["error"], "at": time.time()}
                    _save_video_stats_cache(cache)
                else:
                    youtube, err = _build_youtube_client_for_channel(channel)
                    if err:
                        entry["status"] = "error"
                        entry["error"] = err
                        errs[yt_id] = {"error": err, "at": time.time()}
                        _save_video_stats_cache(cache)
                    else:
                        fetched = _fetch_channel_video_stats(youtube, stale)
                        if fetched:
                            cache_used = False
                            errs.pop(yt_id, None)
                            for vid, st in fetched.items():
                                cached_videos[vid] = dict(st, fetched_at=time.time())
                            _record_video_snapshots(cache, fetched, today_str)
                        # Video yang API tidak kembalikan (dihapus/private):
                        # tombstone supaya tidak di-fetch ulang tiap buka halaman.
                        for vid in stale:
                            if vid not in fetched:
                                cached_videos[vid] = {"views": 0, "likes": 0, "comments": 0,
                                                      "published_at": "", "fetched_at": time.time(),
                                                      "missing": True}
                        _save_video_stats_cache(cache)
        stats = dict(fresh)
        stats.update(fetched)
        for vid, t in tasks.items():
            st = stats.get(vid)
            pub = (st or {}).get('published_at', '')[:10] or t.get('finished_date') or (t.get('publish_date') or '')[:10]
            videos_out.append({
                "video_id": vid, "title": t.get('title', '?'), "channel": ch_name, "yt_id": yt_id,
                "publish_date": pub, "views": int((st or {}).get('views', 0)),
                "likes": int((st or {}).get('likes', 0)), "comments": int((st or {}).get('comments', 0)),
                "url": f"https://youtu.be/{vid}", "has_stats": bool(st) and not st.get('missing'),
            })
        channels_out.append(entry)

    # Ringkasan: views 7/30 hari dihitung dari tanggal tayang (WIB).
    def _in_days(pub, days):
        try:
            d = datetime.strptime(pub, "%Y-%m-%d").date()
            return 0 <= (now_wib.date() - d).days < days
        except Exception:
            return False
    views_7d = sum(v['views'] for v in videos_out if _in_days(v['publish_date'], 7))
    views_30d = sum(v['views'] for v in videos_out if _in_days(v['publish_date'], 30))
    best_worst = {}
    for yt_id in by_channel:
        vs = [v for v in videos_out if v['yt_id'] == yt_id and v['has_stats']]
        if vs:
            best_worst[yt_id] = {"best": max(vs, key=lambda x: x['views']), "worst": min(vs, key=lambda x: x['views'])}
    return jsonify({
        "videos": sorted(videos_out, key=lambda x: x['publish_date'], reverse=True),
        "summary": {"total_videos": len(videos_out), "total_views": sum(v['views'] for v in videos_out),
                    "views_7d": views_7d, "views_30d": views_30d, "best_worst": best_worst},
        "channels": channels_out,
        "cache_used": cache_used,
        "subs_note": "Subscriber per video tidak tersedia via YouTube Data API — butuh scope YouTube Analytics (token saat ini tidak memilikinya). Subs channel ada di statistik channel di atas.",
    })

@app.route('/api/get_schedule')
def get_schedule(): return jsonify({"active": active_tasks, "history": history_tasks})

@app.route('/api/recover_failed_tasks', methods=['GET', 'POST'])
def recover_failed_tasks():
    # 🔥 BARU: memulihkan task yang digagalkan watchdog (belum diproses)
    # dari history kembali ke antrean aktif — jadwal tayang & judul tetap utuh.
    global active_tasks, history_tasks
    recovered = []
    with db_lock:
        still_needed = []
        for t in history_tasks:
            bp = t.get('blueprint')
            if bp and "Stuck" in t.get('status', ''):
                # 🔥 FIX: Pakaikan pelindung "Menunggu" saat dipulihkan
                t['status'] = "Menunggu Giliran (Queue) ⏳ (Dipulihkan)"
                t['updated_at'] = time.time()
                active_tasks.append(t)
                queue_put(bp)   # masuk mesin render beneran
                recovered.append(t.get('title', '?')[:60])
            else:
                still_needed.append(t)
        history_tasks = still_needed
        active_tasks.sort(key=lambda x: x.get('blueprint', {}).get('publish_date', ''))
        save_tasks_db()
    for r in recovered:
        pass
    if recovered:
        send_tg_message("🔄 *Bos, saya pulihkan task yang sempat macet nih* 💪\n" + "\n".join("• " + r for r in recovered[:10]) +
                        ("\n…dst" if len(recovered) > 10 else "") + "\nUdah masuk antrean render lagi, saya lanjutkan.")
    return jsonify({"status": "success", "recovered": len(recovered),
                    "titles": recovered})

# ==========================================
# 🔁 FASE 1 MONITORING — RETRY TASK VIA TELEGRAM
# ==========================================
# ==========================================
# 🔥 FASE 2 APPROVAL PREVIEW — GATE BATCH
# ==========================================
# Aturan: task batch BARU lahir dengan approval='pending' dan TIDAK masuk
# antrean render (tidak ada queue_put) sampai owner menyetujui lewat
# Telegram atau panel. Task LAMA (tanpa field 'approval') dianggap
# 'approved' supaya batch yang sudah berjalan sebelum Fase 2 tidak tiba-tiba
# terkunci. Status pending mengandung kata "Menunggu" supaya watchdog
# stuck dan scheduled_task_activator menganggapnya menunggu, bukan macet.
APPROVAL_PENDING_STATUS = "⏳ Menunggu Approval"

# 🔥 FASE 3: status task yang ditahan karena channel dijeda. Sengaja tidak
# mengandung kata "Queue"/"Rendering"/"Mengunggah" supaya watchdog stuck
# tidak menganggapnya macet (lihat juga pengecualian "Dijeda" di watchdog).
CHANNEL_PAUSED_STATUS = "⏸️ Dijeda (Channel Pause)"

class _PausedAbort(Exception):
    """Dilempar saat channel dijeda di tengah render — batalkan sebelum upload."""
    pass

def _channel_paused(yt_id):
    """True kalau channel ini sedang dijeda (field 'paused' di channels_db.json).
    Channel lama tanpa field dianggap TIDAK dijeda."""
    if not yt_id:
        return False
    for c in database_channel:
        if c.get('yt_id') == yt_id:
            return bool(c.get('paused', False))
    return False


# ============================================================
# 🔥 FASE 5: QC GATE PRA-UPLOAD + SELF-HEALING + BERSIH DISK
# ============================================================

class _QCFailure(Exception):
    """Render selesai tapi tidak layak tayang — jangan upload."""
    pass


def _probe_video(path):
    """Jalankan ffprobe, kembalikan dict JSON (format + streams) atau None
    kalau probe gagal. Dipakai QC gate untuk durasi/resolusi/stream audio."""
    try:
        out = subprocess.run(
            [get_ffprobe_path(), '-v', 'error',
             '-show_entries', 'format=duration,size',
             '-show_entries', 'stream=codec_type,codec_name,width,height',
             '-of', 'json', path],
            capture_output=True, text=True, timeout=60)
        if out.returncode != 0:
            return None
        return json.loads(out.stdout or '{}')
    except Exception:
        return None


def _audio_mean_volume_db(path):
    """Mean volume (dB) seluruh audio via ffmpeg volumedetect.
    -inf = hening total. None = tidak bisa diukur (mis. timeout) — pemanggil
    memperlakukannya sebagai 'lewati cek hening', bukan gagal."""
    try:
        out = subprocess.run(
            [get_ffmpeg_path(), '-i', path,
             '-af', 'volumedetect', '-vn', '-sn', '-dn', '-f', 'null', '-'],
            capture_output=True, text=True, timeout=QC_AUDIO_PROBE_TIMEOUT)
        m = re.search(r'mean_volume:\s*(-?[\d.]+|-\s*inf)', out.stderr or '')
        if not m:
            return None
        v = m.group(1).replace(' ', '')
        if v == '-inf':
            return float('-inf')
        return float(v)
    except Exception:
        return None


def run_qc_checks(task, video_path, yt_id):
    """QC gate pra-upload. Kembalikan list alasan gagal (kosong = LOLOS).
    (a) file ada & ukuran wajar; (b) durasi ≈ target; (c) ada stream audio
    & tidak hening; (d) judul/deskripsi tidak kosong & judul belum tayang
    di channel yang sama. Dijalankan SETELAH render, SEBELUM upload."""
    failures = []
    title = (task.get('title') or '').strip()
    # --- (d) metadata (tidak butuh file) ---
    if not title:
        failures.append("judul kosong")
    else:
        norm = ' '.join(title.lower().split())
        for h in history_tasks:
            if h.get('yt_id') != yt_id:
                continue
            if 'Tayang' not in h.get('status', ''):
                continue
            if ' '.join((h.get('title') or '').lower().split()) == norm:
                failures.append(f"judul sudah tayang di channel ini ('{title[:40]}')")
                break
    if not (task.get('description') or '').strip():
        failures.append("deskripsi kosong")
    # --- (a) file hasil render ---
    if not video_path or not os.path.exists(video_path):
        failures.append("file video hasil render tidak ditemukan")
        return failures
    try:
        size = os.path.getsize(video_path)
    except Exception:
        size = 0
    if size <= 0:
        failures.append("ukuran file video 0 byte")
        return failures
    if size < QC_MIN_FILE_BYTES:
        failures.append(f"ukuran file terlalu kecil ({size // 1024} KB)")
    probe = _probe_video(video_path)
    if probe is None:
        # ffprobe sudah terbukti jalan saat render (dipakai di tiap probe
        # durasi audio), jadi gagal di sini = file rusak, bukan alat rusak.
        failures.append("tidak bisa memeriksa video (ffprobe gagal)")
        return failures
    streams = probe.get('streams') or []
    vstreams = [s for s in streams if s.get('codec_type') == 'video']
    astreams = [s for s in streams if s.get('codec_type') == 'audio']
    if not vstreams:
        failures.append("tidak ada stream video")
    else:
        try:
            w = int(vstreams[0].get('width') or 0)
        except Exception:
            w = 0
        if w and w < 640:
            failures.append(f"resolusi terlalu kecil ({w}px)")
    # --- (b) durasi vs target ---
    try:
        dur = float((probe.get('format') or {}).get('duration') or 0)
    except Exception:
        dur = 0.0
    try:
        target = float(task.get('target_duration_hours', 1)) * 3600
    except Exception:
        target = 0.0
    if dur <= 0:
        failures.append("durasi video tidak terbaca (0 detik)")
    elif target > 0:
        tol = max(QC_DURATION_TOLERANCE_SEC, 0.05 * target)
        if abs(dur - target) > tol:
            failures.append(
                f"durasi {int(dur // 60)} mnt, target {int(target // 60)} mnt "
                f"(selisih > {int(tol // 60)} mnt)")
    # --- (c) audio ada & tidak hening ---
    if not astreams:
        failures.append("tidak ada stream audio")
    else:
        mean_db = _audio_mean_volume_db(video_path)
        if mean_db is not None and (mean_db == float('-inf')
                                    or mean_db < QC_SILENCE_MAX_DB):
            failures.append(f"audio hening (mean volume {mean_db} dB)")
    return failures


# --- Klasifikasi error upload: sementara (boleh retry) vs permanen ---
_UPLOAD_PERMANENT_MARKERS = (
    "API Habis",              # quotaExceeded — habis hari ini, retry sia-sia
    "Limit Upload",           # uploadLimitExceeded — batas channel tercapai
    "Sesi Kedaluwarsa",       # token invalid/expired — butuh taut ulang
    "Ditolak YT",             # penolakan YouTube (bukan 5xx)
    "Token API Tidak Ditemukan",
    "Rate Limit",             # sudah di-cooldown 30 menit oleh handler lama
    "Format tanggal salah",   # data task salah — retry tidak menyembuhkan
)


def _upload_error_transient(pesan_error):
    """True kalau error upload layak dicoba ulang (sementara: jaringan,
    timeout, server YouTube 5xx). Permanen → False. Tidak dikenal → True
    (tetap dibatasi maks percobaan, jadi tidak bisa loop tanpa henti)."""
    err = pesan_error or ""
    for m in _UPLOAD_PERMANENT_MARKERS:
        if m in err:
            return False
    return True


def _record_channel_failure(yt_id, title, reason):
    """Tambah 1 ke fail_streak channel. Kalau mencapai 3x berturut-turut,
    jeda channel OTOMATIS (mekanisme paused Fase 3 — gate worker sudah
    menahan task channel dijeda) + alert Telegram. Dipanggil dari jalur
    gagal worker (render/upload/QC/disk). BUKAN untuk pembatalan manual."""
    if not yt_id:
        return
    try:
        with db_lock:
            target = next((c for c in database_channel
                           if c.get('yt_id') == yt_id), None)
            if target is None:
                return
            streak = int(target.get('fail_streak') or 0) + 1
            target['fail_streak'] = streak
            name = target.get('name', yt_id)
            auto_paused = False
            if streak >= FAIL_STREAK_AUTO_PAUSE and not target.get('paused'):
                target['paused'] = True
                target['auto_paused'] = True
                auto_paused = True
            save_channels(database_channel)
    except Exception:
        return
    if auto_paused:
        send_tg_message(
            f"⏸️ *Bos, channel {name} saya jeda dulu ya* 😅\n"
            f"Udah gagal {streak}x berturut-turut "
            f"(terakhir: {(reason or '')[:80]}).\n"
            f"Biar nggak makin parah, saya tahan dulu — cek penyebabnya, "
            f"terus pencet Lanjutkan di panel ya 💪")


def _record_channel_success(yt_id):
    """Reset fail_streak saat 1 upload/render sukses — kegagalan lama
    tidak lagi dihitung beruntun."""
    if not yt_id:
        return
    try:
        with db_lock:
            target = next((c for c in database_channel
                           if c.get('yt_id') == yt_id), None)
            if target is None:
                return
            if target.get('fail_streak'):
                target['fail_streak'] = 0
                save_channels(database_channel)
    except Exception:
        pass


def cleanup_old_files():
    """Hapus file lama yang AMAN dihapus. Kembalikan (jumlah_file, byte).
    TIDAK PERNAH menyentuh: galeri pengguna (uploads/<yt_id>/...),
    config/database (config.json, channels_db.json, tasks_db.json,
    client_secret.json), file task yang sedang aktif, atau file statis
    panel (logo, font). Aturan umur:
      - temp render (uploads/temp_*, loop_*) ............ > 6 jam & task tidak aktif
      - final render (static/final_*.mp4) .............. > 1 hari & task tidak aktif
      - output clipper (static/clip_*.mp4) ............. > 7 hari & job clipper tidak aktif
      - download sumber clipper (downloads/* media) .... > 7 hari
    File temp/final task aktif tidak pernah dihapus — render yang sedang
    berjalan selalu aman dari pembersih ini."""
    now = time.time()
    deleted = 0
    freed = 0

    def _try_delete(path, min_age):
        nonlocal deleted, freed
        try:
            st = os.stat(path)
        except Exception:
            return
        if now - st.st_mtime < min_age:
            return
        try:
            freed += st.st_size
            os.remove(path)
            deleted += 1
        except Exception:
            pass

    def _active_task_ids():
        try:
            with db_lock:
                return {str(t.get('id')) for t in active_tasks}
        except Exception:
            return set()

    def _task_id_from_name(name):
        m = re.search(r'(\d+)\.[^.]+$', name)
        return m.group(1) if m else None

    # 1. Temp render di uploads/ — top-level SAJA, jangan subfolder galeri.
    try:
        active_ids = _active_task_ids()
        for name in os.listdir(BASE_UPLOAD):
            if not name.startswith(('temp_', 'loop_')):
                continue
            tid = _task_id_from_name(name)
            if tid and tid in active_ids:
                continue
            _try_delete(os.path.join(BASE_UPLOAD, name), CLEANUP_TEMP_MAX_AGE)
    except Exception:
        pass
    # 2. Final render di static/ — hanya kalau task-nya tidak aktif.
    try:
        active_ids = _active_task_ids()
        static_dir = os.path.join(BASE_DIR, 'static')
        for name in os.listdir(static_dir):
            if not (name.startswith('final_') and name.endswith('.mp4')):
                continue
            tid = name[len('final_'):-len('.mp4')]
            if tid in active_ids:
                continue
            _try_delete(os.path.join(static_dir, name), CLEANUP_FINAL_MAX_AGE)
    except Exception:
        pass
    # 3. Output clipper di static/clip_*.mp4 — job selesai/gagal & > 7 hari.
    try:
        try:
            from clipper.queue_worker import read_db as _clipper_read_db
            jobs = _clipper_read_db()
        except Exception:
            jobs = []
        active_clip = {str(j.get('id')) for j in jobs
                       if str(j.get('status', '')).lower()
                       not in ('done', 'error', 'failed', 'cancelled')}
        static_dir = os.path.join(BASE_DIR, 'static')
        for name in os.listdir(static_dir):
            if not (name.startswith('clip_') and name.endswith('.mp4')):
                continue
            jid = name[len('clip_'):-len('.mp4')]
            if jid in active_clip:
                continue
            _try_delete(os.path.join(static_dir, name), CLEANUP_CLIP_MAX_AGE)
    except Exception:
        pass
    # 4. Download sumber clipper di downloads/ — file media > 7 hari.
    # (Sumber bisa di-download ulang dari tautan YouTube, jadi aman.)
    try:
        dl_dir = os.path.join(BASE_DIR, 'downloads')
        for name in os.listdir(dl_dir):
            if not name.lower().endswith(
                    ('.mp4', '.webm', '.mkv', '.mp3', '.m4a', '.opus', '.wav')):
                continue
            _try_delete(os.path.join(dl_dir, name), CLEANUP_DOWNLOAD_MAX_AGE)
    except Exception:
        pass
    return deleted, freed


def _ensure_disk_for_render(task):
    """Audit F9a (REVISI): cek disk sebelum render. Kalau di bawah ambang,
    BERSIHKAN dulu file lama yang aman (cleanup_old_files), lalu cek ulang.
    Kembalikan None kalau ruang cukup (render lanjut seperti biasa);
    kalau masih kurang, kembalikan pesan error 'Disk Penuh' — sengaja TANPA
    kata Limit/Cooldown/Habis supaya handler umum tidak mengantre ulang
    task ini (disk penuh tidak sembuh sendiri). Pesan menyebut berapa yang
    berhasil dibersihkan dan berapa yang masih kurang."""
    try:
        target_hours = float(task.get('target_duration_hours', 1))
    except Exception:
        target_hours = 1.0
    need = max(DISK_MIN_FREE_BYTES,
               int(target_hours * 3600 * DISK_BYTES_PER_SEC))
    try:
        free = shutil.disk_usage(BASE_DIR).free
    except Exception:
        return None  # tidak bisa memeriksa → jangan blokir render
    if free >= need:
        return None
    cleaned_n, cleaned_b = cleanup_old_files()
    try:
        free = shutil.disk_usage(BASE_DIR).free
    except Exception:
        return None
    if free >= need:
        print(f"🧹 [Fase 5] Disk menipis, bersih-bersih {cleaned_n} file "
              f"({cleaned_b / 1024 ** 2:.0f} MB) — ruang cukup, render lanjut.",
              flush=True)
        return None
    return (f"Disk Penuh — sisa {free / 1024 ** 3:.1f} GB, "
            f"butuh ≥ {need / 1024 ** 3:.1f} GB untuk render {target_hours:g} jam. "
            f"Saya udah bersih-bersih {cleaned_n} file ({cleaned_b / 1024 ** 2:.0f} MB) "
            f"tapi masih kurang {(need - free) / 1024 ** 3:.1f} GB 😅 "
            f"Hapus file lama / tambah disk dulu ya Bos, terus pencet Retry 💪")


def disk_cleanup_loop():
    """Pembersih berkala ringan (Fase 5): sekali sehari hapus file lama yang
    aman (aturan sama seperti cleanup_old_files) supaya disk tidak penuh
    diam-diam. Gagal silent; jalan di thread daemon."""
    last_run = 0.0
    while True:
        try:
            time.sleep(3600)
            if time.time() - last_run >= 24 * 3600:
                n, b = cleanup_old_files()
                last_run = time.time()
                if n:
                    print(f"🧹 [Fase 5] Pembersih harian: {n} file lama dihapus "
                          f"({b / 1024 ** 2:.0f} MB dibebaskan).", flush=True)
        except Exception:
            time.sleep(3600)


threading.Thread(target=disk_cleanup_loop, daemon=True).start()

def _blueprint_in_queue(task_id):
    """True kalau blueprint task ini masih ada di render_queue.
    Dipakai resume supaya task tidak masuk antrean dua kali (video ganda)."""
    try:
        with render_queue.mutex:
            items = list(render_queue.queue)
    except Exception:
        return False
    for item in items:
        bp = item[2] if isinstance(item, tuple) and len(item) == 3 else item
        if isinstance(bp, dict) and bp.get('id') == task_id:
            return True
    return False

def _resume_channel_tasks(yt_id):
    """Masukkan lagi task channel yang tertahan jeda ke antrean render.
    Hanya task approved, non-final, punya blueprint, dan berstatus Dijeda.
    Mengembalikan jumlah task yang masuk antrean. Harus dipanggil dengan
    db_lock TIDAK sedang dipegang (fungsi ini mengambil lock sendiri)."""
    resumed = 0
    with db_lock:
        targets = [t for t in active_tasks
                   if _active_task_yt_id(t) == yt_id
                   and _approval_of(t) != 'pending'
                   and 'blueprint' in t
                   and CHANNEL_PAUSED_STATUS in t.get('status', '')]
        for t in targets:
            t['status'] = "Menunggu Giliran (Queue) ⏳"
            t['updated_at'] = time.time()
        if targets:
            active_tasks.sort(key=lambda x: x.get('blueprint', {}).get('publish_date', ''))
            save_tasks_db()
    for t in targets:
        try:
            if not _blueprint_in_queue(t['id']):
                queue_put(t["blueprint"])
                resumed += 1
        except Exception as e:
            print(f"Gagal queue resume task {t.get('id')}: {e}", flush=True)
    return resumed

def _approval_of(t):
    """Status approval sebuah task. Legacy (tanpa field) = approved."""
    return t.get('approval') or 'approved'

def _validate_operator_key(data):
    """Cek `key` == operator_key (HMAC, pola sama seperti retry_task).
    Mengembalikan (True, '') kalau valid, kalau tidak (False, pesan)."""
    import hmac as _hmac
    key = data.get('key', '')
    expected = load_bot_config().get('operator_key', '')
    if not expected or not _hmac.compare_digest(str(key), str(expected)):
        return False, "Kunci operator tidak valid."
    return True, ''

def _apply_approval(task_id, decision):
    """Terapkan keputusan approval ke SATU task pending. Dipakai bersama
    oleh endpoint operator (Telegram) dan endpoint panel (login).
    decision: 'approved' atau 'rejected'.
    Mengembalikan (ok, pesan, judul). Harus dipanggil dengan db_lock
    TIDAK sedang dipegang (fungsi ini mengambil lock sendiri)."""
    global active_tasks, history_tasks
    try:
        task_id = int(task_id)
    except Exception:
        return False, "task_id tidak valid.", ""
    with db_lock:
        target = None
        for t in active_tasks:
            if t.get('id') == task_id:
                target = t
                break
        if target is None:
            return False, "Task tidak ditemukan di antrean aktif.", ""
        if _approval_of(target) != 'pending':
            return False, "Task ini sudah diputuskan (bukan pending lagi).", target.get('title', '')[:60]
        if not target.get('blueprint'):
            return False, "Task ini tidak punya blueprint.", target.get('title', '')[:60]
        title = target.get('title', '?')[:60]
        if decision == 'approved':
            target['approval'] = 'approved'
            # 🔥 FASE 3: channel dijeda → disetujui tapi DITAHAN (tidak masuk
            # antrean render). Task berstatus Dijeda sampai channel dilanjutkan.
            if _channel_paused(_active_task_yt_id(target)):
                target['status'] = CHANNEL_PAUSED_STATUS
                target['updated_at'] = time.time()
                active_tasks.sort(key=lambda x: x.get('blueprint', {}).get('publish_date', ''))
                save_tasks_db()
                return True, "Task disetujui, tapi channel sedang dijeda — akan masuk antrean saat channel dilanjutkan.", title
            target['status'] = "Menunggu Giliran (Queue) ⏳"
            target['updated_at'] = time.time()
            active_tasks.sort(key=lambda x: x.get('blueprint', {}).get('publish_date', ''))
            save_tasks_db()
            try:
                queue_put(target["blueprint"])
            except Exception as e:
                return False, f"Gagal masuk antrean: {e}", title
            return True, "Task disetujui dan masuk antrean render.", title
        # decision == 'rejected': pindah ke riwayat supaya terlihat di panel,
        # bukan hilang diam-diam — dan TIDAK pernah dieksekusi.
        target['approval'] = 'rejected'
        target['status'] = "Ditolak (Approval) ❌"
        active_tasks.remove(target)
        history_tasks.insert(0, target)
        save_tasks_db()
        return True, "Task ditolak dan tidak akan dieksekusi.", title

def _apply_approve_batch(batch_id):
    """Setujui SEMUA task pending dalam satu batch. Mengembalikan
    (jumlah_disetujui, [judul...]). Task yang sudah diputuskan dilewati."""
    approved_titles = []
    with db_lock:
        pending = [t for t in active_tasks
                   if t.get('batch_id') == batch_id and _approval_of(t) == 'pending'
                   and t.get('blueprint')]
        for t in pending:
            t['approval'] = 'approved'
            # 🔥 FASE 3: task dari channel yang dijeda disetujui tapi ditahan.
            if _channel_paused(_active_task_yt_id(t)):
                t['status'] = CHANNEL_PAUSED_STATUS
            else:
                t['status'] = "Menunggu Giliran (Queue) ⏳"
            t['updated_at'] = time.time()
            approved_titles.append(t.get('title', '?')[:60])
        if approved_titles:
            active_tasks.sort(key=lambda x: x.get('blueprint', {}).get('publish_date', ''))
            save_tasks_db()
        blueprints = [t["blueprint"] for t in pending
                      if CHANNEL_PAUSED_STATUS not in t.get('status', '')]
    for bp in blueprints:
        try:
            queue_put(bp)
        except Exception as e:
            print(f"Gagal queue batch approve: {e}", flush=True)
    return len(approved_titles), approved_titles

def _operator_approval_endpoint(decision):
    """Kerangka endpoint operator approve/reject satu task. Mengembalikan
    respons JSON Flask (dipakai kedua route di bawah)."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), 403
    if data.get('task_id') is None:
        return jsonify({"status": "error", "message": "task_id wajib diisi."}), 400
    ok, msg, title = _apply_approval(data.get('task_id'), decision)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400
    word = "disetujui" if decision == 'approved' else "ditolak"
    send_tg_message(f"{'✅' if decision == 'approved' else '❌'} *Sip Bos, task {word}* 😎\n`{title}`\n_{msg}_")
    return jsonify({"status": "success", "message": msg, "title": title})

@app.route('/api/operator/approve_task', methods=['POST'])
def operator_approve_task():
    """Setujui SATU task pending dari tombol inline Telegram.
    Keamanan: sama seperti retry_task — wajib operator_key, route ini
    masuk allowed_routes di check_security."""
    try:
        return _operator_approval_endpoint('approved')
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal approve: {e}"}), 500

@app.route('/api/operator/reject_task', methods=['POST'])
def operator_reject_task():
    """Tolak SATU task pending dari tombol inline Telegram. Task pindah
    ke riwayat berstatus 'Ditolak (Approval)' dan tidak dieksekusi."""
    try:
        return _operator_approval_endpoint('rejected')
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal reject: {e}"}), 500

@app.route('/api/operator/approve_batch', methods=['POST'])
def operator_approve_batch():
    """Setujui SEMUA task pending dalam satu batch (tombol 'Setujui Semua').
    Keamanan: wajib operator_key seperti endpoint operator lain."""
    try:
        data = request.get_json(silent=True) or {}
        ok_key, msg_key = _validate_operator_key(data)
        if not ok_key:
            return jsonify({"status": "error", "message": msg_key}), 403
        batch_id = data.get('batch_id')
        if not batch_id:
            return jsonify({"status": "error", "message": "batch_id wajib diisi."}), 400
        count, titles = _apply_approve_batch(batch_id)
        if count == 0:
            return jsonify({"status": "error", "message": "Tidak ada task pending di batch ini (mungkin sudah diputuskan)."}), 400
        send_tg_message(f"✅ *Sip Bos, {count} task udah saya setujui semua!* 💪\nUdah masuk antrean render, saya kerjakan satu-satu.\n_" + "; ".join(titles[:5]) + ("…" if len(titles) > 5 else "") + "_")
        return jsonify({"status": "success", "message": f"{count} task disetujui dan masuk antrean.", "count": count})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal approve batch: {e}"}), 500

# --- Approval dari panel (login) — cadangan kalau operator Telegram mati. ---
@app.route('/api/approve_task', methods=['POST'])
def panel_approve_task():
    """Setujui satu task pending dari panel web (butuh login panel)."""
    data = request.get_json(silent=True) or {}
    if data.get('task_id') is None:
        return jsonify({"status": "error", "message": "task_id wajib diisi."}), 400
    ok, msg, title = _apply_approval(data.get('task_id'), 'approved')
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400
    return jsonify({"status": "success", "message": msg, "title": title})

@app.route('/api/reject_task', methods=['POST'])
def panel_reject_task():
    """Tolak satu task pending dari panel web (butuh login panel)."""
    data = request.get_json(silent=True) or {}
    if data.get('task_id') is None:
        return jsonify({"status": "error", "message": "task_id wajib diisi."}), 400
    ok, msg, title = _apply_approval(data.get('task_id'), 'rejected')
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400
    return jsonify({"status": "success", "message": msg, "title": title})

@app.route('/api/operator/retry_task', methods=['POST'])
def operator_retry_task():
    """Retry SATU task gagal dari tombol inline Telegram.
    Dipanggil operator (proses terpisah) — operator tidak bisa menyentuh
    antrean render in-memory app.py, jadi lewat endpoint ini.
    Keamanan: tanpa login panel, tapi wajib membawa `key` == operator_key
    di config.json (dibuat otomatis saat startup, tidak pernah tampil di
    chat/log). Route ini masuk allowed_routes di check_security.
    Satu klik = satu percobaan ulang; kalau task sudah kembali ke antrean,
    klik berikutnya ditolak supaya tidak dobel."""
    global active_tasks, history_tasks
    try:
        data = request.get_json(silent=True) or {}
        task_id = data.get('task_id')
        ok_key, msg_key = _validate_operator_key(data)
        if not ok_key:
            return jsonify({"status": "error", "message": msg_key}), 403
        if task_id is None:
            return jsonify({"status": "error", "message": "task_id wajib diisi."}), 400
        try:
            task_id = int(task_id)
        except Exception:
            return jsonify({"status": "error", "message": "task_id tidak valid."}), 400

        with db_lock:
            if any(t.get('id') == task_id for t in active_tasks):
                return jsonify({"status": "error", "message": "Task sudah ada di antrean aktif."})
            target = None
            for t in history_tasks:
                if t.get('id') == task_id:
                    target = t
                    break
            if target is None:
                return jsonify({"status": "error", "message": "Task tidak ditemukan di riwayat."})
            # 🔥 FASE 2: task yang DITOLAK saat approval tidak boleh hidup lagi
            # lewat retry — keputusan tolak itu final. Task pending tidak ada
            # di riwayat (masih di antrean aktif), jadi tidak sampai ke sini.
            if _approval_of(target) == 'rejected':
                return jsonify({"status": "error", "message": "Task ini ditolak saat approval, tidak bisa di-retry. Buat batch baru."}), 400
            if not target.get('blueprint'):
                return jsonify({"status": "error", "message": "Task ini tidak punya blueprint, tidak bisa diulang."})
            if "Selesai" in target.get('status', '') or "Tayang" in target.get('status', '') or "Sukses" in target.get('status', ''):
                return jsonify({"status": "error", "message": "Task sudah berhasil, tidak perlu diulang."})
            history_tasks.remove(target)
            target['status'] = "Menunggu Giliran (Queue) ⏳ (Retry Telegram)"
            target['updated_at'] = time.time()
            active_tasks.append(target)
            active_tasks.sort(key=lambda x: x.get('blueprint', {}).get('publish_date', ''))
            save_tasks_db()
            try:
                queue_put(target["blueprint"])
            except Exception as e:
                return jsonify({"status": "error", "message": f"Gagal masuk antrean: {e}"}), 500
        title = target.get('title', '?')[:60]
        send_tg_message(f"🔁 *Siap Bos, retry jalan!* 💪\nTask `{title}` udah masuk antrean render lagi.")
        return jsonify({"status": "success", "message": "Task kembali ke antrean render.", "title": title})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal retry: {e}"}), 500

@app.route('/api/clear_history', methods=['POST'])
def clear_history():
    global history_tasks
    with db_lock: history_tasks.clear()
    save_tasks_db()
    return jsonify({"status": "success", "message": "Riwayat dibersihkan!"})

@app.route('/api/get_channels')
def get_channels():
    safe_c = [{"id": c["id"], "name": c["name"], "yt_id": c["yt_id"], "thumbnail": c["thumbnail"], "status": c["status"], "title_bank": c.get("title_bank", []), "paused": c.get("paused", False), "autopilot": c.get("autopilot", "approval")} for c in database_channel]
    return jsonify(safe_c)

@app.route('/api/get_project_quotas')
def get_project_quotas_api():
    """🔥 FASE 6: sisa kuota upload per project Google (butuh login panel —
    route ini TIDAK masuk allowed_routes, jadi check_security mewajibkan
    login). Kuota dihitung dari upload hari ini (WIB) semua channel di
    project yang sama; batas 6/hari/project."""
    return jsonify({"status": "success", "projects": get_project_quotas()})

from flask import send_file # Pastikan send_file di-import di baris paling atas jika belum ada

@app.route('/api/export_channels', methods=['GET'])
def export_channels():
    """Mengunduh database channel saat ini sebagai file JSON."""
    if not os.path.exists(DB_FILE):
        return jsonify({"status": "error", "message": "Database belum terbentuk."}), 404
    
    timestamp = int(time.time())
    return send_file(DB_FILE, as_attachment=True, download_name=f"pabrik_konten_channels_backup_{timestamp}.json")

@app.route('/api/import_channels', methods=['POST'])
def import_channels():
    """Mengunggah dan menimpa database channel dengan file JSON cadangan."""
    global database_channel
    file = request.files.get('file')
    
    if not file or not file.filename.endswith('.json'):
        return jsonify({"status": "error", "message": "File tidak valid! Harus berformat .json"}), 400
    
    try:
        new_data = json.load(file)
        if not isinstance(new_data, list):
            return jsonify({"status": "error", "message": "Format file rusak. Data channel harus berupa List/Array."}), 400
            
        with db_lock:
            database_channel = new_data
            # Langsung simpan secara permanen menimpa channels_db.json lama
            save_channels(database_channel)
            
        return jsonify({"status": "success", "message": f"{len(database_channel)} channel berhasil diimpor!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal membaca file: {str(e)}"}), 500

@app.route('/api/delete_channel', methods=['POST'])
def delete_channel():
    yt_id = request.form.get('yt_id')
    global database_channel, active_tasks, history_tasks
    
    # 1. Batalkan semua antreannya di background
    with db_lock:
        tasks_to_remove = [t for t in active_tasks if _active_task_yt_id(t) == yt_id]
        for t in tasks_to_remove:
            task_id = t['id']
            if task_id in active_processes:
                proc = active_processes[task_id]
                if proc and proc.poll() is None:
                    kill_process_tree(proc.pid)
            stop_flags[task_id] = True
            t['status'] = "Dibatalkan (Channel Dihapus) 🗑️"
            active_tasks.remove(t)
            history_tasks.insert(0, t)
            
        # 🔥 FIX v1.7: item antrean adalah tuple (prioritas, seq, blueprint) — versi lama
        # membaca item.get('yt_id') → error di item pertama, purge berhenti, blueprint
        # channel ini tertinggal di antrean → task hidup lagi setelah channel dihapus.
        temp_queue = []
        while not render_queue.empty():
            try:
                item = render_queue.get_nowait()
            except Exception:
                break
            bp = item[2] if isinstance(item, tuple) and len(item) == 3 else item
            if not (isinstance(bp, dict) and bp.get('yt_id') == yt_id):
                temp_queue.append(item)
        for item in temp_queue:
            render_queue.put(item)
    save_tasks_db()

    # 2. Sapu bersih folder & fisik dari Gallery VPS
    gallery_path = os.path.join(BASE_UPLOAD, yt_id)
    if os.path.exists(gallery_path):
        import shutil
        shutil.rmtree(gallery_path, ignore_errors=True)

    # 3. Hapus profil channel dari Database
    database_channel = [c for c in database_channel if c['yt_id'] != yt_id]
    save_channels(database_channel)
    
    return jsonify({"status": "success", "message": "Channel, aset Gallery, dan antrean berhasil disapu bersih!"})

# --- PRESET API ---
@app.route('/api/save_preset', methods=['POST'])
def save_preset():
    data = request.json
    try:
        presets = {}
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                try: presets = json.load(f)
                except: pass
        presets.update(data)
        with open(PRESETS_FILE, 'w') as f: json.dump(presets, f, indent=4)
        return jsonify({"status": "success"})
    except Exception as e: return jsonify({"status": "error", "message": str(e)})

@app.route('/api/get_presets', methods=['GET'])
def get_presets():
    if os.path.exists(PRESETS_FILE):
        with open(PRESETS_FILE, 'r') as f:
            try: 
                return jsonify(json.load(f))
            except: 
                pass
    return jsonify({})

@app.route('/api/delete_preset', methods=['POST'])
def delete_preset():
    data = request.json
    preset_name = data.get('name')
    try:
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                presets = json.load(f)
                
            if preset_name in presets:
                del presets[preset_name]
                
                with open(PRESETS_FILE, 'w') as f: 
                    json.dump(presets, f, indent=4)
                    
                return jsonify({"status": "success"})
                
        return jsonify({"status": "error", "message": "Preset tidak ditemukan"})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)})

# ============================================================
# 🖼️ GALLERY ENDPOINTS
# ============================================================
@app.route('/api/get_asset_counts')
def get_asset_counts():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify({"audios": 0, "backgrounds": 0, "thumbnails": 0, "base_videos": 0, "overlays": 0, "special_songs": 0})
    def count_files(sub):
        path = get_channel_folder(yt_id, sub)
        return len([f for f in os.listdir(path) if os.path.isfile(os.path.join(path, f))])
    return jsonify({
        "audios": count_files("audios"),
        "backgrounds": count_files("backgrounds"),
        "thumbnails": count_files("thumbnails"),
        "base_videos": count_files("base_videos"), # <--- TAMBAHAN
        "overlays": count_files("overlays"),         # 🔥 BARU
        "special_songs": count_files("special_songs") # 🔥 BARU
    })

@app.route('/api/get_gallery', methods=['GET'])
def get_gallery():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify({"audio": [], "background": [], "thumbnails": [], "base_video": [], "overlay": [], "special_song": []})
    def get_files_data(sub):
        path = get_channel_folder(yt_id, sub)
        res = []
        if os.path.exists(path):
            for f in os.listdir(path):
                fp = os.path.join(path, f)
                if os.path.isfile(fp):
                    size_mb = round(os.path.getsize(fp) / (1024*1024), 2)
                    item = {"name": f, "size": f"{size_mb} MB"}
                    # 🔥 FIX: sertakan URL preview supaya gallery bisa tampilkan
                    # thumbnail/background asli, bukan ikon gambar rusak.
                    ext = f.rsplit('.', 1)[-1].lower() if '.' in f else ''
                    if ext in ('jpg', 'jpeg', 'png', 'gif', 'webp'):
                        item['url'] = f"/uploads/{yt_id}/{sub}/{f}"
                    elif ext in ('mp4', 'webm', 'mov'):
                        item['url'] = f"/uploads/{yt_id}/{sub}/{f}"
                    res.append(item)
        return res
    return jsonify({
        "audio":      get_files_data("audios"),
        "background": get_files_data("backgrounds"),
        "thumbnails": get_files_data("thumbnails"),
        "base_video": get_files_data("base_videos"), # <--- TAMBAHAN
        "overlay":    get_files_data("overlays"),      # 🔥 BARU: file overlay visualizer
        "special_song": get_files_data("special_songs") # 🔥 BARU: lagu khusus penutup
    })

@app.route('/api/upload_gallery', methods=['POST'])
def upload_gallery():
    yt_id  = request.form.get('yt_id', '').strip()
    g_type = request.form.get('type',  '').strip()

    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id tidak boleh kosong!"}), 400
    if not g_type:
        return jsonify({"status": "error", "message": "type tidak boleh kosong!"}), 400

    folder_name = resolve_folder(g_type)
    folder      = get_channel_folder(yt_id, folder_name)

    files = (request.files.getlist('files[]')
             or request.files.getlist('files')
             or request.files.getlist('file')
             or list(request.files.values()))

    if not files:
        return jsonify({"status": "error", "message": "Tidak ada file yang diterima!"}), 400

    saved, errors = 0, []
    for f in files:
        if not f or not f.filename:
            continue
        try:
            safe_name = os.path.basename(f.filename)
            dest = os.path.join(folder, safe_name)
            f.save(dest)
            saved += 1
        except Exception as e:
            errors.append(f"{f.filename}: {str(e)}")

    if saved == 0:
        return jsonify({"status": "error", "message": "Tidak ada file yang berhasil disimpan. " + "; ".join(errors)}), 500

    msg = f"{saved} file berhasil diupload ke '{folder_name}'"
    if errors:
        msg += f" ({len(errors)} gagal: {'; '.join(errors[:3])})"
    return jsonify({"status": "success", "message": msg})

@app.route('/api/delete_gallery_file', methods=['POST'])
def delete_gallery_file():
    yt_id  = request.form.get('yt_id', '').strip()
    g_type = request.form.get('type',  '').strip()
    name   = request.form.get('name',  '').strip()

    folder_name = resolve_folder(g_type)
    path = os.path.join(get_channel_folder(yt_id, folder_name), os.path.basename(name))

    if os.path.exists(path):
        os.remove(path)
        return jsonify({"status": "success", "message": "File dihapus!"})
    return jsonify({"status": "error", "message": f"File tidak ditemukan: {path}"})

@app.route('/api/clear_gallery_folder', methods=['POST'])
def clear_gallery_folder():
    yt_id  = request.form.get('yt_id', '').strip()
    g_type = request.form.get('type',  '').strip()

    folder_name = resolve_folder(g_type)
    path = get_channel_folder(yt_id, folder_name)

    deleted = 0
    if os.path.exists(path):
        for f in os.listdir(path):
            fp = os.path.join(path, f)
            if os.path.isfile(fp):
                try:
                    os.remove(fp)
                    deleted += 1
                except: pass

    return jsonify({"status": "success", "message": f"{deleted} file berhasil disapu bersih!"})

# ============================================================
# 📝 TITLE BANK ENDPOINT
# ============================================================
@app.route('/api/upload_title_bank', methods=['POST'])
def upload_title_bank():
    yt_id = (request.form.get('yt_id') or request.args.get('yt_id') or '').strip()
    txt_file = request.files.get('txt_file') or request.files.get('file')

    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id tidak ditemukan. Pastikan channel sudah dipilih."}), 400
    if not txt_file:
        return jsonify({"status": "error", "message": "File .txt tidak ditemukan dalam request."}), 400

    try:
        raw_bytes = txt_file.read()
        try:   content = raw_bytes.decode('utf-8')
        except: content = raw_bytes.decode('latin-1', errors='ignore')

        lines = [line.strip() for line in content.split('\n') if line.strip()]
        global database_channel

        # 🔥 FIX: file .txt kosong = perintah CLEAR Title Bank (hapus semua judul),
        # bukan error. Kalau di-reject sebagai error, bank tidak pernah dihapus
        # dari database -> judul muncul lagi setelah refresh.
        if not lines:
            for c in database_channel:
                if c['yt_id'] == yt_id:
                    c['title_bank'] = []
                    save_channels(database_channel)
                    return jsonify({"status": "success", "message": "Title Bank dikosongkan!", "total": 0})
            return jsonify({"status": "error", "message": f"Channel dengan yt_id '{yt_id}' tidak ditemukan di database."}), 404

        channel_found = False
        for c in database_channel:
            if c['yt_id'] == yt_id:
                existing = c.get('title_bank', [])
                merged   = list(dict.fromkeys(existing + lines))
                c['title_bank'] = merged
                channel_found = True
                save_channels(database_channel)
                return jsonify({
                    "status":  "success",
                    "message": f"{len(lines)} judul diimport! Total bank: {len(merged)} judul.",
                    "total":   len(merged),
                })

        if not channel_found:
            return jsonify({"status": "error", "message": f"Channel dengan yt_id '{yt_id}' tidak ditemukan di database."}), 404

    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal memproses file: {str(e)}"}), 500

@app.route('/api/get_playlists', methods=['GET'])
def get_playlists():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify([])
    channel = next((c for c in database_channel if c['yt_id'] == yt_id), None)
    if not channel: return jsonify([])
    try:
        creds = get_fresh_credentials(channel)
        youtube = build('youtube', 'v3', credentials=creds)
        res = youtube.playlists().list(part="snippet", mine=True, maxResults=50).execute()
        return jsonify([{"id": p['id'], "title": p['snippet']['title']} for p in res.get('items', [])])
    except: return jsonify([])

# 🛠️ FUNGSI BANTUAN: Membunuh proses FFmpeg/Worker sampai ke akar
def kill_process_tree(pid):
    try:
        import psutil
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            child.kill()
        parent.kill()
    except Exception:
        pass

@app.route('/api/stop_task/<int:task_id>', methods=['POST'])
def stop_task(task_id):
    stop_flags[task_id] = True
    proc = active_processes.pop(task_id, None)
    if proc and proc.poll() is None:
        kill_process_tree(proc.pid)

    NOT_YET_RUNNING_MARKERS = ("In Factory Queue", "Antrean Ditunda")
    with db_lock:
        target = next((t for t in active_tasks if t['id'] == task_id), None)
    if target and any(m in target['status'] for m in NOT_YET_RUNNING_MARKERS):
        move_to_history(task_id, "Dibatalkan (Manual) 🛑")
    else:
        save_tasks_db()

    return jsonify({"status": "success", "message": "Dihentikan paksa!"})

@app.route('/api/stop_channel/<yt_id>', methods=['POST'])
def stop_channel(yt_id):
    global active_tasks, history_tasks
    with db_lock:
        tasks_to_remove = [t for t in active_tasks if _active_task_yt_id(t) == yt_id]
        for t in tasks_to_remove:
            task_id = t['id']
            if task_id in active_processes:
                proc = active_processes[task_id]
                if proc and proc.poll() is None:
                    kill_process_tree(proc.pid)
            stop_flags[task_id] = True
            t['status'] = "Dibatalkan (Stop Channel) 🛑"
            active_tasks.remove(t)
            history_tasks.insert(0, t)
            
        # 🔥 FIX v1.7: item antrean adalah tuple (prioritas, seq, blueprint) — versi lama
        # membaca item.get('yt_id') → error di item pertama, purge berhenti, blueprint
        # channel ini tertinggal di antrean → task hidup lagi setelah di-stop.
        temp_queue = []
        while not render_queue.empty():
            try:
                item = render_queue.get_nowait()
            except Exception:
                break
            bp = item[2] if isinstance(item, tuple) and len(item) == 3 else item
            if not (isinstance(bp, dict) and bp.get('yt_id') == yt_id):
                temp_queue.append(item)
        for item in temp_queue:
            render_queue.put(item)
    save_tasks_db()
    return jsonify({"status": "success", "message": f"{len(tasks_to_remove)} antrean channel dihentikan!"})

@app.route('/api/set_channel_paused', methods=['POST'])
def set_channel_paused():
    """🔥 FASE 3: Jeda / lanjutkan satu channel (butuh login panel — route ini
    TIDAK masuk allowed_routes, jadi check_security mewajibkan login).
    Dijeda → task terjadwal channel ini tidak dieksekusi/di-upload sampai
    dilanjutkan (worker mengeluarkan tasknya dari antrean; approval yang
    masuk selagi dijeda ditahan). Dilanjutkan → task yang tertahan masuk
    antrean render lagi. State 'paused' disimpan di channels_db.json
    (field baru, channel lama tanpa field = tidak dijeda) → tahan restart."""
    data = request.get_json(silent=True) or {}
    yt_id = data.get('yt_id')
    paused = bool(data.get('paused', False))
    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id wajib diisi."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        target['paused'] = paused
        if not paused:
            # 🔥 FASE 5: lanjut manual → reset hitungan gagal beruntun supaya
            # channel tidak langsung dijeda lagi di kegagalan berikutnya.
            target['fail_streak'] = 0
            target.pop('auto_paused', None)
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Gagal menyimpan: {e}"}), 500
    if paused:
        send_tg_message(f"⏸️ *Siap Bos, channel {name} saya jeda dulu.* 😌\nTask-nya saya tahan semua, nggak ada yang jalan sampai Bos lanjutkan lagi.")
        return jsonify({"status": "success", "paused": True, "resumed_count": 0,
                        "message": f"Channel {name} dijeda. Task terjadwal ditahan."})
    resumed = _resume_channel_tasks(yt_id)
    send_tg_message(f"▶️ *Siap Bos, channel {name} jalan lagi!* 💪\n{resumed} task udah masuk antrean render.")
    return jsonify({"status": "success", "paused": False, "resumed_count": resumed,
                    "message": f"Channel {name} dilanjutkan. {resumed} task masuk antrean."})

@app.route('/api/set_channel_autopilot', methods=['POST'])
def set_channel_autopilot():
    """🔥 FASE 6: ubah mode autopilot satu channel (butuh login panel — route
    ini TIDAK masuk allowed_routes, jadi check_security mewajibkan login).
    'auto' = channel terpercaya: batch baru langsung masuk antrean tanpa
    approval. 'approval' = default: batch baru tetap lewat approval Fase 2.
    Channel lama tanpa field dianggap 'approval' (aman)."""
    data = request.get_json(silent=True) or {}
    yt_id = data.get('yt_id')
    mode = str(data.get('autopilot', '') or '').lower()
    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id wajib diisi."}), 400
    if mode not in ('auto', 'approval'):
        return jsonify({"status": "error", "message": "Mode autopilot harus 'auto' atau 'approval'."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        target['autopilot'] = mode
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            return jsonify({"status": "error", "message": f"Gagal menyimpan: {e}"}), 500
    if mode == 'auto':
        send_tg_message(f"🤖 *Siap Bos, channel {name} saya set Full-Auto!* 😎\nBatch baru langsung jalan tanpa approval — saya yang urus sampai tayang 💪")
        return jsonify({"status": "success", "autopilot": "auto",
                        "message": f"Channel {name} mode Full-Auto. Batch baru langsung masuk antrean."})
    send_tg_message(f"👀 *Siap Bos, channel {name} balik ke mode Approval.* 😌\nBatch baru tetap saya kirim preview dulu buat Bos cek.")
    return jsonify({"status": "success", "autopilot": "approval",
                    "message": f"Channel {name} mode Approval. Batch baru menunggu persetujuan."})

@app.route('/api/check_secret')
def check_secret():
    # Mengembalikan info file yang terupload agar Settings bisa menampilkannya
    # (nama file, client_id, tipe client, ukuran, waktu upload). client_secret
    # TIDAK PERNAH dikembalikan ke browser.
    if not os.path.exists(CLIENT_SECRETS_FILE):
        return jsonify({"exists": False})
    try:
        size = os.path.getsize(CLIENT_SECRETS_FILE)
        modified = datetime.fromtimestamp(os.path.getmtime(CLIENT_SECRETS_FILE), tz=timezone.utc).isoformat()
        with open(CLIENT_SECRETS_FILE, 'r') as f:
            secret_data = json.load(f)
        client_type = None
        client_config = {}
        for _key in ('installed', 'web'):
            if isinstance(secret_data.get(_key), dict):
                client_type = _key
                client_config = secret_data[_key]
                break
        return jsonify({
            "exists": True,
            "filename": "client_secret.json",
            "client_id": client_config.get('client_id'),
            "client_type": client_type or 'other',
            "project_id": client_config.get('project_id'),
            "size_bytes": size,
            "modified_iso": modified,
        })
    except Exception:
        return jsonify({"exists": True, "parse_error": True})

@app.route('/api/delete_secret', methods=['POST'])
def delete_secret():
    # Hapus file client_secret.json yang terupload (CRUD: delete).
    try:
        if not os.path.exists(CLIENT_SECRETS_FILE):
            return jsonify({"status": "error", "message": "Tidak ada API key yang tersimpan."})
        os.remove(CLIENT_SECRETS_FILE)
        return jsonify({"status": "success", "message": "API key dihapus!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal menghapus API key: {str(e)}"})

@app.route('/api/upload_secret', methods=['POST'])
def upload_secret():
    try:
        file = request.files.get('secret_file')
        if file and file.filename.endswith('.json'):
            file.save(CLIENT_SECRETS_FILE)
            return jsonify({"status": "success", "message": "API Key diunggah!"})
        return jsonify({"status": "error", "message": "Harus .json!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Izin ditolak server: {str(e)}"})

@app.route('/api/generate_tv_link')
def generate_tv_link():
    if not os.path.exists(CLIENT_SECRETS_FILE): return jsonify({"auth_url": "", "error": "File client_secret.json belum ada!"})
    return jsonify({"auth_url": f"http://{request.host}/device_login"})

@app.route('/device_login')
def device_login():
    if not os.path.exists(CLIENT_SECRETS_FILE): return "File rahasia tidak ditemukan!"
    with open(CLIENT_SECRETS_FILE, 'r') as f:
        secret_data = json.load(f); client_config = secret_data.get('installed', secret_data.get('web', {})); client_id = client_config.get('client_id')
    res = requests.post('https://oauth2.googleapis.com/device/code', data={'client_id': client_id, 'scope': ' '.join(SCOPES)}).json()
    if 'error' in res: return f"Error Google: {res['error']}"
    html = f"""
    <html><head><title>Aktivasi YouTube</title>
    <style>
        body {{ font-family: 'Segoe UI', Arial; text-align: center; background: #eef2f6; color: #1e293b; padding-top: 10vh; }}
        .box {{ background: #ffffff; width: 550px; max-width: calc(100vw - 32px); margin: auto; padding: 40px; border-radius: 16px; box-shadow: 0 10px 25px rgba(0,0,0,0.05); border: 1px solid #e2e8f0; }}
        .step {{ text-align: left; margin-bottom: 25px; font-size: 14px; color: #64748b; font-weight:600; }}
        .input-group {{ display: flex; margin-top: 10px; }}
        .input-group input {{ flex: 1; padding: 15px; font-size: 16px; font-weight: bold; background: #f8fafc; color: #10b981; border: 1px solid #e2e8f0; border-radius: 8px 0 0 8px; text-align: center; outline:none; }}
        .input-group button {{ padding: 15px 25px; font-size: 14px; font-weight: bold; background: #10b981; color: white; border: none; border-radius: 0 8px 8px 0; cursor: pointer; transition: 0.3s; }}
    </style></head><body>
        <div class="box">
            <h2 style="margin-top:0;">🔗 Tautkan Channel Baru</h2>
            <div class="step"><b>Langkah 1:</b> Copy link ini dan Paste di browser target:
                <div class="input-group"><input type="text" id="glink" value="{res['verification_url']}" readonly><button onclick="document.getElementById('glink').select();document.execCommand('copy');">Copy Link</button></div>
            </div>
            <div class="step"><b>Langkah 2:</b> Masukkan Kode Rahasia ini:
                <div class="input-group"><input type="text" id="gcode" value="{res['user_code']}" readonly><button onclick="document.getElementById('gcode').select();document.execCommand('copy');">Copy Kode</button></div>
            </div>
            <div id="status" style="margin-top:30px; font-weight:bold;">⏳ Menunggu Anda memasukkan kode...</div>
        </div>
        <script>
            function poll() {{ fetch('/api/poll_device_token', {{ method: 'POST', headers: {{'Content-Type': 'application/json'}}, body: JSON.stringify({{device_code: '{res['device_code']}'}}) }}).then(r => r.json()).then(data => {{ if(data.status === 'success') {{ document.getElementById('status').innerHTML = "🎉 Berhasil! Mengalihkan..."; setTimeout(() => {{ window.location.href = '/'; }}, 2000); }} else if(data.status === 'pending') {{ setTimeout(poll, data.interval || 5000); }} else if(data.status === 'error') {{ document.getElementById('status').innerHTML = "❌ " + (data.error || 'Gagal menautkan channel.'); }} }}); }}
            setTimeout(poll, 5000);
        </script>
    </body></html>
    """
    return html

@app.route('/api/poll_device_token', methods=['POST'])
def poll_device_token():
    device_code = request.json.get('device_code')
    with open(CLIENT_SECRETS_FILE, 'r') as f:
        s_data = json.load(f); conf = s_data.get('installed', s_data.get('web', {})); c_id = conf.get('client_id'); c_sec = conf.get('client_secret')
    res = requests.post('https://oauth2.googleapis.com/token', data={'client_id': c_id, 'client_secret': c_sec, 'device_code': device_code, 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'}).json()
    if 'error' in res:
        err = res['error']
        if err == 'authorization_pending': return jsonify({"status": "pending", "interval": 5000})
        elif err == 'slow_down': return jsonify({"status": "pending", "interval": 10000})
        else: return jsonify({"status": "error", "error": err})
    creds = Credentials(token=res['access_token'], refresh_token=res.get('refresh_token'), token_uri='https://oauth2.googleapis.com/token', client_id=c_id, client_secret=c_sec, scopes=SCOPES)
    youtube = build('youtube', 'v3', credentials=creds); chan_res = youtube.channels().list(part="snippet", mine=True).execute()
    if chan_res['items']:
        item = chan_res['items'][0]; global database_channel
        c_idx = next((i for i, c in enumerate(database_channel) if c['yt_id'] == item['id']), None)
        if c_idx is None:
            new_c = {"id": len(database_channel)+1, "name": item['snippet']['title'], "yt_id": item['id'], "thumbnail": item['snippet']['thumbnails']['default']['url'], "status": "Connected 🟢 (1 Key)", "creds_list": [creds.to_json()]}
            database_channel.append(new_c)
        else:
            if 'creds_list' not in database_channel[c_idx]:
                database_channel[c_idx]['creds_list'] = [database_channel[c_idx].get('creds_json', '')]
            # 🔥 FIX v1.3: buang key yang sudah mati (invalid_grant) dulu,
            # baru tumpuk key baru — daftar key tidak menumpuk key mati lagi.
            live_keys = []
            for ks in database_channel[c_idx]['creds_list']:
                if not ks:
                    continue
                try:
                    kc = Credentials.from_authorized_user_info(json.loads(ks))
                    if kc.expired and kc.refresh_token:
                        kc.refresh(Request())
                    live_keys.append(ks)
                except Exception as e:
                    if 'invalid_grant' not in str(e).lower():
                        live_keys.append(ks)  # gagal non-invalid_grant: jangan hapus
            database_channel[c_idx]['creds_list'] = live_keys
            if creds.to_json() not in database_channel[c_idx]['creds_list']:
                database_channel[c_idx]['creds_list'].append(creds.to_json())
            database_channel[c_idx]['status'] = f"Connected 🟢 ({len(database_channel[c_idx]['creds_list'])} Keys)"
        # 🔥 FIX: taut ulang = token fresh → hapus cooldown lama channel ini
        try: clear_channel_cooldown(item['id'])
        except Exception: pass
        save_channels(database_channel)
        return jsonify({"status": "success"})
    # 🔥 FIX v1.3: jangan klaim "Berhasil" kalau akun Google-nya tidak punya channel
    # (sebelumnya halaman tetap bilang Berhasil padahal tidak ada yang ketaut).
    return jsonify({"status": "error", "error": "Akun Google ini tidak memiliki channel YouTube. Ulangi dengan akun yang punya channel."})

# --- BATCH CREATOR ---
def _tg_escape(text):
    """Escape karakter Markdown Telegram supaya judul/deskripsi video
    (yang bisa mengandung *, _, [, ] dll.) tidak merusak format pesan."""
    for ch in ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(ch, '\\' + ch)
    return text

def _send_approval_previews(batch_id, channel_name, previews):
    """Kirim SATU pesan preview per video ke owner Telegram (dijalankan di
    thread background supaya respons batch_create tidak menunggu Telegram).
    Setiap pesan membawa tombol ✅ Setujui / ❌ Tolak + tombol Setujui
    Semua di pesan pertama. Kalau token Telegram belum diset / operator
    mati, pengiriman gagal silent — task tetap aman berstatus pending
    dan terlihat di panel. Thumbnail TIDAK dikirim: blueprint tidak
    menyimpan file thumbnail, jadi tidak ada gambar yang dikarang."""
    total = len(previews)
    for idx, pv in enumerate(previews):
        desc = (pv.get('description') or '').strip()
        desc_txt = _tg_escape(desc[:200]) + ("…" if len(desc) > 200 else "") if desc else "_tidak ada deskripsi_"
        text = (
            f"📋 *Bos, batch baru nih, dicek dulu ya* 👀 ({idx + 1}/{total})\n"
            f"📺 Channel: *{_tg_escape(channel_name)}*\n"
            f"🎬 Judul: *{_tg_escape(pv.get('title', '?'))}*\n"
            f"🗓️ Jadwal publish: `{_tg_escape(pv.get('publish_date', '-'))}`\n"
            f"⏱️ Durasi target: `{pv.get('duration', '-')} jam` • 🔒 `{_tg_escape(pv.get('privacy', 'public'))}`\n"
            f"📝 Deskripsi: {desc_txt}"
        )
        if idx == 0:
            text += "\n\n_Kalau oke tinggal pencet Setujui, saya yang eksekusi 💪_"
        markup = {"inline_keyboard": [
            [{"text": "✅ Setujui", "callback_data": f"approve:{pv['id']}"},
             {"text": "❌ Tolak", "callback_data": f"reject:{pv['id']}"}],
        ]}
        if idx == 0 and total > 1:
            markup["inline_keyboard"].append(
                [{"text": f"✅ Setujui Semua ({total} video)", "callback_data": f"approve_all:{batch_id}"}])
        send_tg_message(text, reply_markup=markup)
        time.sleep(0.3)  # jeda kecil supaya tidak kena rate-limit Telegram

def _stagger_publish_date(v_date_str, yt_id):
    """🔥 FASE 6: geser jam tayang +1 jam bila bentrok persis dengan task
    aktif channel LAIN di project Google yang sama (maks 5 geseran).
    Tujuannya: channel-channel satu project tidak upload berbarengan
    (kuota project 6/hari, hindari burst/rate-limit). Tanpa bentrok,
    tanggal dikembalikan apa adanya."""
    try:
        dt = datetime.strptime(v_date_str, '%Y-%m-%d %H:%M')
    except Exception:
        return v_date_str
    my_pid = _project_id_of_channel(next((c for c in database_channel if c.get('yt_id') == yt_id), None) or {})
    if not my_pid:
        return v_date_str
    for _ in range(5):
        collision = False
        with db_lock:
            for t in active_tasks:
                other_yt = t.get('yt_id') or (t.get('blueprint') or {}).get('yt_id')
                if not other_yt or other_yt == yt_id:
                    continue
                other_date = t.get('time') or (t.get('blueprint') or {}).get('publish_date')
                if other_date != v_date_str:
                    continue
                opid = _project_id_of_channel(next((c for c in database_channel if c.get('yt_id') == other_yt), None) or {})
                if opid and opid == my_pid:
                    collision = True
                    break
        if not collision:
            return v_date_str
        dt = dt + timedelta(hours=1)
        v_date_str = dt.strftime('%Y-%m-%d %H:%M')
    return v_date_str

@app.route('/api/batch_create', methods=['POST'])
def batch_create():
    data = request.json
    yt_id = data.get('yt_id')
    count = data.get('count', 1)
    
    titles = data.get('generated_titles', [])
    # 🔥 TAMBAHKAN BARIS INI UNTUK MENANGKAP DESKRIPSI ACAK 🔥
    descs = data.get('generated_descriptions', []) 
    
    durations_array = data.get('target_durations_array', [])
    
    try:
        base_date = datetime.strptime(data['start_date'], '%Y-%m-%dT%H:%M')
    except:
        return jsonify({"status": "error", "message": "Format tanggal salah"}), 400
        
    interval_days = float(data.get('interval_days', 1))
    publish_dates = data.get('generated_publish_dates', [])

    if publish_dates and len(publish_dates) > 0:
        last_date_str = publish_dates[-1]
    else:
        final_v_date = base_date + timedelta(days=(count - 1) * interval_days)
        last_date_str = final_v_date.strftime('%Y-%m-%d %H:%M')
        
    with db_lock:
        for c in database_channel:
            if c['yt_id'] == yt_id:
                c['last_scheduled_date'] = last_date_str
                c['reminder_sent'] = False  
                break
        save_channels(database_channel)

    # 🔥 FASE 2: satu batch = satu batch_id (untuk tombol "Setujui Semua").
    batch_id = f"b{int(time.time())}"
    channel_name = next((c.get('name', yt_id) for c in database_channel if c.get('yt_id') == yt_id), yt_id or 'Tanpa Channel')
    previews = []
    # 🔥 FASE 6: mode autopilot per channel — 'auto' (channel terpercaya:
    # batch langsung masuk antrean tanpa approval) vs 'approval' (default,
    # channel baru/lama tetap lewat approval Fase 2).
    channel_rec = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    auto_mode = _autopilot_of(channel_rec) == 'auto'
    channel_paused = _channel_paused(yt_id)
    auto_blueprints = []
    adjusted_dates = []
    shifted_count = 0

    for i in range(count):
        t_id = int(time.time()) + i
        
        if publish_dates and i < len(publish_dates):
            v_date_str = publish_dates[i]
        else:
            v_date = base_date + timedelta(days=i * interval_days)
            v_date_str = v_date.strftime('%Y-%m-%d %H:%M')

        # 🔥 FASE 6: stagger jam tayang — geser +1 jam bila bentrok persis
        # dengan task aktif channel lain di project Google yang sama.
        _orig_date = v_date_str
        v_date_str = _stagger_publish_date(v_date_str, yt_id)
        if v_date_str != _orig_date:
            shifted_count += 1
        adjusted_dates.append(v_date_str)
        
        if i < len(durations_array):
            vid_duration = durations_array[i]
        else:
            vid_duration = data.get('target_duration_hours', 1)
            
        blueprint = {
            "id": t_id, "yt_id": yt_id, "title": titles[i] if i < len(titles) else f"Auto Video #{i+1}",
            "publish_date": v_date_str,
            "mp3_per_video": data.get('mp3_per_video', 5), 
            "vid_per_video": data.get('vid_per_video', 1), 
            "bg_count": data.get('bg_count', 1), 
            "target_duration_hours": vid_duration,
            "vis_mode": data.get('vis_mode'), "vis_preset": data.get('vis_preset'),
            "vis_presets_allowed": data.get('vis_presets_allowed', []),
            # 🔥 BARU: overlay visualizer + preset kecepatan render + lagu khusus
            "vis_overlay_file": data.get('vis_overlay_file', ''),
            "vis_overlay_size": data.get('vis_overlay_size', 60),
            "vis_overlay_key": data.get('vis_overlay_key', 'none'),
            "vis_zoom_pct": data.get('vis_zoom_pct', 10),
            "render_preset": data.get('render_preset', 'ultrafast'),
            "use_special_song": data.get('use_special_song', False),
            
            # 🔥 UBAH BARIS DESKRIPSI INI 🔥
            "description": descs[i] if i < len(descs) else data.get('description', ''), 
            
            "tags": data.get('tags', ''), "privacy": data.get('privacy', 'public'), "playlist_id": data.get('playlist_id', ''),
            "use_floating_card": data.get('use_floating_card', False),
            "source_mode": data.get('source_mode', 'mix'),  # 🔥 BARU: simpan jalur render engine di blueprint
            "batch_id": batch_id  # 🔥 FASE 2: batch pemilik task (untuk "Setujui Semua")
        }
        with db_lock:
            active_tasks.append({
                "id": t_id,
                "yt_id": yt_id,  # 🔥 FIX v1.8: simpan yt_id di level atas juga (sebelumnya cuma di blueprint)
                "title": blueprint['title'],
                "time": blueprint['publish_date'],
                # 🔥 FASE 2: task baru lahir PENDING — tidak masuk antrean render
                # (tidak ada queue_put di sini) sampai owner menyetujui.
                # 🔥 FASE 6: mode Full-Auto lahir langsung APPROVED (+ masuk
                # antrean di bawah); channel dijeda lahir Dijeda, tidak antre.
                "status": (CHANNEL_PAUSED_STATUS if channel_paused else "Menunggu Giliran (Queue) ⏳") if auto_mode else APPROVAL_PENDING_STATUS,
                "approval": "approved" if auto_mode else "pending",
                "batch_id": batch_id,
                "type": "📺 VOD",
                "blueprint": blueprint
            })
        if auto_mode and not channel_paused:
            auto_blueprints.append(blueprint)
        previews.append({
            "id": t_id, "title": blueprint['title'],
            "publish_date": blueprint['publish_date'],
            "duration": blueprint['target_duration_hours'],
            "description": blueprint.get('description', ''),
            "privacy": blueprint.get('privacy', 'public'),
        })

    save_tasks_db()

    # 🔥 FASE 6: last_scheduled_date mengikuti tanggal AKHIR yang sudah
    # di-stagger (bukan tanggal mentah dari klien).
    if adjusted_dates:
        with db_lock:
            for c in database_channel:
                if c['yt_id'] == yt_id:
                    c['last_scheduled_date'] = adjusted_dates[-1]
                    break
            save_channels(database_channel)

    _shift_note = (f" {shifted_count} video digeser jam tayangnya biar tidak bentrok dengan channel lain di project yang sama."
                   if shifted_count else "")

    if auto_mode:
        # 🔥 FASE 6: Full-Auto — langsung masuk antrean render, tanpa
        # preview approval (task lahir approved; yang dijeda tetap ditahan).
        for bp in auto_blueprints:
            try:
                queue_put(bp)
            except Exception as e:
                print(f"Gagal queue auto task {bp.get('id')}: {e}", flush=True)
        return jsonify({"status": "success", "message": f"{count} Video dibuat dan langsung masuk antrean (mode Full-Auto)!{_shift_note}"})

    # 🔥 FASE 2: kirim preview ke Telegram di thread background (panel
    # langsung merespons; kalau Telegram mati, task tetap pending di panel).
    threading.Thread(target=_send_approval_previews,
                     args=(batch_id, channel_name, previews), daemon=True).start()

    return jsonify({"status": "success", "message": f"{count} Video dibuat dan menunggu approval di Telegram!{_shift_note}"})
    
@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    return send_from_directory(BASE_UPLOAD, filename)

# ==========================================
# 📱 TELEGRAM REMINDER WORKER
# ==========================================
def telegram_reminder_worker():
    global database_channel
    while True:
        time.sleep(3600)  # Mengecek jadwal setiap 1 jam
        
        bot_config = load_bot_config()
        token = bot_config.get('tg_token')
        chat_id = bot_config.get('tg_chat_id')
        
        if not token or not chat_id: 
            continue
            
        now = datetime.now()
        changed = False
        
        with db_lock:
            for c in database_channel:
                last_date_str = c.get('last_scheduled_date')
                # Lewati jika belum ada jadwal atau pengingat sudah dikirim
                if not last_date_str or c.get('reminder_sent', False):
                    continue
                    
                try:
                    last_date = datetime.strptime(last_date_str, '%Y-%m-%d %H:%M')
                    time_diff = last_date - now
                    
                    # Jika sisa waktu <= 24 Jam (dan belum kelewat jauh)
                    if timedelta(hours=0) < time_diff <= timedelta(hours=24):
                        msg = f"⚠️ Notifikasi Ziqva Muse Bot\n\nChannel: *{c['name']}*\nStok jadwal video hampir habis!\nJadwal terakhir: {last_date_str}\n\nWaktunya buat video baru, Bos K!"
                        try:
                            # Kirim pesan ke Telegram
                            requests.post(
                                f"https://api.telegram.org/bot{token}/sendMessage", 
                                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"}
                            )
                            c['reminder_sent'] = True
                            changed = True
                        except Exception as e:
                            print(f"Gagal kirim Telegram: {e}")
                except Exception as e:
                    pass
                    
            if changed:
                save_channels(database_channel)

# Jalankan worker di background
threading.Thread(target=telegram_reminder_worker, daemon=True).start()

def scheduled_task_activator():
    # 🔥 BARU: mengaktifkan task "Dijadwalkan ... ⏳ (Tahan Restart ✓)" saat waktunya tiba.
    # Task berjadwal jauh sengaja TIDAK dirender saat startup (biar aman dari restart).
    # Worker ini cek tiap 2 menit: begitu publish_date masuk dalam ≤60 menit,
    # task dikembalikan ke mesin render.
    while True:
        time.sleep(120)
        now = datetime.now()
        activated = []
        with db_lock:
            for t in active_tasks:
                if "blueprint" not in t or "Dijadwalkan" not in t.get('status', ''):
                    continue
                try:
                    sch = datetime.strptime(t['blueprint']['publish_date'], "%Y-%m-%d %H:%M")
                except Exception:
                    continue
                # 🔥 FASE 3: channel dijeda → jangan aktifkan walau waktunya tiba.
                if _channel_paused(_active_task_yt_id(t)):
                    continue
                diff = sch - now
                if diff <= timedelta(hours=1):
                    t['status'] = "In Factory Queue ⚙️ (Dilanjutkan)"
                    try:
                        queue_put(t["blueprint"])
                        activated.append(t['title'][:50])
                    except Exception:
                        pass
        if activated:
            save_tasks_db()
            msg = "⏳ *Bos, jadwalnya tiba nih!* 😎\n" + "\n".join("• " + a for a in activated)
            send_tg_message(msg)

threading.Thread(target=scheduled_task_activator, daemon=True).start()

# --- Endpoint Telegram Settings ---
@app.route('/api/save_telegram', methods=['POST'])
def save_telegram():
    data = request.json
    bot_config = load_bot_config()
    bot_config['tg_token'] = data.get('token', '')
    bot_config['tg_chat_id'] = data.get('chat_id', '')
    
    with open(CONFIG_FILE, 'w') as f: 
        json.dump(bot_config, f, indent=4)
    
    # Tes kirim pesan saat disimpan
    if bot_config['tg_token'] and bot_config['tg_chat_id']:
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_config['tg_token']}/sendMessage", 
                json={"chat_id": bot_config['tg_chat_id'], "text": "✅ Ziqva Muse Bot: Notifikasi Telegram berhasil dihubungkan!"}
            )
        except: 
            pass
            
    return jsonify({"status": "success", "message": "Konfigurasi Telegram disimpan!"})

@app.route('/api/get_telegram', methods=['GET'])
def get_telegram():
    bot_config = load_bot_config()
    return jsonify({
        "token": bot_config.get('tg_token', ''), 
        "chat_id": bot_config.get('tg_chat_id', '')
    })

@app.route('/api/delete_telegram', methods=['POST'])
def delete_telegram():
    # Hapus konfigurasi Telegram yang tersimpan (CRUD: delete), kunci lain
    # di config.json (admin_pin, secret_key, dsb.) tetap dipertahankan.
    try:
        bot_config = load_bot_config()
        bot_config['tg_token'] = ''
        bot_config['tg_chat_id'] = ''
        with open(CONFIG_FILE, 'w') as f:
            json.dump(bot_config, f, indent=4)
        return jsonify({"status": "success", "message": "Konfigurasi Telegram dihapus!"})
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal menghapus konfigurasi Telegram: {str(e)}"})

@app.route('/api/hub_status')
def get_hub_status():
    try:
        # Hitung task Factory yang masih aktif
        factory_active = len([t for t in active_tasks if 'Selesai' not in t['status'] and 'Gagal' not in t['status'] and 'Dibatalkan' not in t['status']])

        # Hitung task Clipper yang masih aktif
        clipper_active = 0
        clipper_db_path = os.path.join(BASE_DIR, 'clipper', 'clips_db.json')
        if os.path.exists(clipper_db_path):
            with open(clipper_db_path, 'r') as f:
                c_jobs = json.load(f)
                clipper_active = len([j for j in c_jobs if j.get('status') not in ['done', 'error']])

        sys_stats = get_system_stats()

        return jsonify({
            "factory": factory_active,
            "clipper": clipper_active,
            "cpu": f"{sys_stats['cpu']}%"
        })
    except Exception as e:
        return jsonify({"factory": 0, "clipper": 0, "cpu": "0%"})

# ==========================================
# 🚀 STARTUP & RECOVERY SCRIPT
# ==========================================
def _reschedule_task(t):
    # 🔥 FASE 2: task yang masih menunggu approval TIDAK boleh masuk antrean
    # render saat startup — gate approval harus bertahan melewati restart.
    if _approval_of(t) == 'pending':
        t['status'] = APPROVAL_PENDING_STATUS
        return
    # 🔥 FASE 3: channel dijeda → jangan masuk antrean saat startup.
    if _channel_paused(_active_task_yt_id(t)):
        t['status'] = CHANNEL_PAUSED_STATUS
        return
    t['status'] = "Menunggu Giliran (Queue) ⏳ (Dilanjutkan)"
    t['updated_at'] = time.time()
    queue_put(t["blueprint"])

# 🔥 SISTEM PENYAPU OTOMATIS: Bersihkan hantu sebelum masuk antrean
valid_yt_ids = {c['yt_id'] for c in database_channel}
tasks_to_keep = []

for t in active_tasks:
    is_final = ("Selesai" in t['status'] or "Sukses" in t['status']
                or "Gagal" in t['status'] or "Dibatalkan" in t['status'])
    
    # Cek apakah channel induknya masih ada
    yt_id = t.get('yt_id') or (t.get('blueprint', {}).get('yt_id'))
    
    if yt_id and yt_id not in valid_yt_ids and not is_final:
        # Langsung batalkan dan buang ke history saat itu juga
        t['status'] = "Dibatalkan (Channel Dihapus) 🛑"
        history_tasks.insert(0, t)
    else:
        tasks_to_keep.append(t)
        if not is_final:
            if "blueprint" in t:
                _reschedule_task(t)
            else:
                t['status'] = "Menunggu (Data Incomplete) ⚠️"

active_tasks = [t for t in tasks_to_keep if "Dibatalkan" not in t['status']]
save_tasks_db()
    
startup_sanitizer()
# 🔥 FASE 5: bersih-bersih file lama saat start (aturan umur di cleanup_old_files).
try:
    _n, _b = cleanup_old_files()
    if _n:
        print(f"🧹 [Fase 5] Startup: {_n} file lama dibersihkan ({_b / 1024 ** 2:.0f} MB).", flush=True)
except Exception:
    pass
send_tg_message("🚀 *Bos, saya balik lagi!* 😎\n"
                f"Antrean: {len(active_tasks)} task | Riwayat: {len(history_tasks)} task — saya lanjutkan semua 💪")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False, use_reloader=False, threaded=True)
