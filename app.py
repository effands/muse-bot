import os, sys, time, queue, threading, subprocess, random, json, shutil, math, re, hmac, wave, zlib
import unicodedata
import html as _html
import urllib.parse
import numpy as np
import cv2, librosa, imageio
import datetime as dt
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo
import secrets
from flask import Flask, render_template, request, jsonify, redirect, url_for, send_from_directory, session, Response
import requests
from werkzeug.security import generate_password_hash, check_password_hash

from googleapiclient.discovery import build
from google.oauth2.credentials import Credentials
from google.auth.transport.requests import Request
from googleapiclient.http import MediaFileUpload
from googleapiclient.errors import HttpError
from core.resource_gate import acquire_resource, release_resource
try:
    import zumo_core as _zumo_core
    _ZUMO_CORE_OK = True
except Exception:
    # Panel harus tetap jalan bila zumo_core.py belum ikut ter-deploy.
    _zumo_core = None
    _ZUMO_CORE_OK = False

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
# ---- Log file aktivitas (v1.3.11): salin semua print ke logs/panel.log ----
# Panel selama ini hanya menulis ke stdout (journal systemd) — tidak bisa
# dilihat dari web. Tee ini menulis ke stream asli SEKALIGUS ke file log
# (rotasi sederhana di 5 MB), supaya panel bisa menampilkan progres kerja
# bot + tombol Copy/Clear. Gagal tulis file tidak boleh mematikan aplikasi.
_LOG_LOCK = threading.RLock()
_LOG_PATH = os.path.join(BASE_DIR, 'logs', 'panel.log')
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
                        # dipotong dari luar (tombol Clear) atau terlalu
                        # besar -> tutup, rotasi bila perlu, buka ulang.
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


app = Flask(__name__, static_folder=os.path.join(BASE_DIR, 'static'))
os.environ['OAUTHLIB_INSECURE_TRANSPORT'] = '1'
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')

from clipper.routes import clipper_bp
app.register_blueprint(clipper_bp, url_prefix='/api/clip')

# 🔒 KEAMANAN: batas total ukuran request (upload galeri, import, title
# bank). Flask default = tak terbatas → disk-fill. 512 MB cukup untuk
# semua upload sah (per file galeri dibatasi lagi 200 MB di upload_gallery).
app.config['MAX_CONTENT_LENGTH'] = 512 * 1024 * 1024

def is_configured(): return os.path.exists(CONFIG_FILE)
def load_bot_config():
    if is_configured():
        with open(CONFIG_FILE, 'r') as f: return json.load(f)
    return {}

bot_config = load_bot_config()

# ============================================================
# 🔒 HELPERS KEAMANAN (perbaikan bug hunt #1, #5, #10, #12, #15, #20, #22, #25)
# ============================================================
# Batas-batas keamanan (nilai disepakati di laporan perbaikan).
GALLERY_MAX_FILE_BYTES = 200 * 1024 * 1024   # per file galeri (#21)
QUEUE_MAX_SIZE = 200                          # maks task di antrean render (#15)
BATCH_MAX_COUNT = 50                          # maks video per request batch_create (#15)
LOGIN_MAX_FAILS = 5                           # gagal login → kunci (#1)
LOGIN_LOCK_BASE_MINUTES = 15                  # durasi kunci awal (#1)
LOGIN_WINDOW_SECONDS = 900                    # jendela hitung kegagalan login (#1)
OPERATOR_MAX_FAILS = 30                       # kunci operator salah / menit / IP (#22)
OPERATOR_WINDOW_SECONDS = 60
DEVICE_POLL_MAX_PER_MIN = 30                  # poll device token / menit / IP (#3)

def _save_bot_config(cfg):
    """Tulis config.json + kunci permission 0600 (#25: secret plaintext di
    satu file — minimal batasi siapa yang bisa membacanya di disk).
    Gagal chmod tidak boleh mematikan aplikasi (try/except)."""
    with open(CONFIG_FILE, 'w') as f:
        json.dump(cfg, f, indent=4)
    try:
        os.chmod(CONFIG_FILE, 0o600)
    except Exception:
        pass

def _session_version_expected():
    """Nomor versi sesi dari config (default 1 untuk config lama)."""
    try:
        return int(load_bot_config().get('session_version', 1) or 1)
    except Exception:
        return 1

def _bump_session_version():
    """Naikkan versi sesi di config → SEMUA cookie sesi lama mati (#10).
    Dipakai logout() dan ganti PIN."""
    cfg = load_bot_config()
    cfg['session_version'] = _session_version_expected() + 1
    _save_bot_config(cfg)
    return cfg['session_version']

# --- PIN: hash + migrasi plaintext lama (#1) ---
def _pin_is_hashed(stored):
    return isinstance(stored, str) and (stored.startswith('pbkdf2:') or stored.startswith('scrypt:'))

def _pin_strength_ok(pin):
    """PIN minimal 8 karakter, campur huruf + angka."""
    if not isinstance(pin, str) or len(pin) < 8:
        return False
    has_letter = any(c.isalpha() for c in pin)
    has_digit = any(c.isdigit() for c in pin)
    return has_letter and has_digit

def _check_pin(pin):
    """Cek PIN terhadap config. PIN plaintext LAMA tetap diterima sekali —
    saat cocok, penyimpanannya langsung diganti hash (migrasi otomatis,
    owner tidak terkunci). Hash TIDAK PERNAH ditulis ke log.
    Mengembalikan True/False."""
    if not isinstance(pin, str) or not pin:
        return False
    cfg = load_bot_config()
    stored = cfg.get('admin_pin')
    if not stored:
        return False
    if _pin_is_hashed(stored):
        try:
            return check_password_hash(stored, pin)
        except Exception:
            return False
    # Penyimpanan lama (plaintext): bandingkan constant-time, lalu migrasi.
    if hmac.compare_digest(str(stored), pin):
        try:
            cfg['admin_pin'] = generate_password_hash(pin)
            _save_bot_config(cfg)
            print("🔒 PIN lama (plaintext) berhasil dimigrasi ke hash.", flush=True)
        except Exception:
            pass
        return True
    return False

# --- Rate-limit login (#1) + operator (#22) + poll device (#3) ---
_login_failures = {}   # ip -> [timestamp gagal]
_login_locks = {}      # ip -> (lock_until, lock_count)
_operator_failures = {}  # ip -> [timestamp kunci salah]
_operator_locks = {}     # ip -> lock_until
_poll_attempts = {}      # ip -> [timestamp poll]

def _prune(lst, window):
    now = time.time()
    return [t for t in lst if now - t < window]

def _login_locked(ip):
    until, _n = _login_locks.get(ip, (0, 0))
    return time.time() < until

def _login_lock_remaining(ip):
    until, _n = _login_locks.get(ip, (0, 0))
    return max(0, int(until - time.time()))

def _record_login_failure(ip):
    now = time.time()
    lst = _prune(_login_failures.get(ip, []), LOGIN_WINDOW_SECONDS)
    lst.append(now)
    _login_failures[ip] = lst
    if len(lst) >= LOGIN_MAX_FAILS:
        _until, n = _login_locks.get(ip, (0, 0))
        # Jeda eksponensial: 15 → 30 → 60 menit (maks 60).
        minutes = min(LOGIN_LOCK_BASE_MINUTES * (2 ** n), 60)
        _login_locks[ip] = (now + minutes * 60, n + 1)
        _login_failures[ip] = []
        print(f"🔒 Login dikunci {minutes} menit untuk IP {ip} setelah {LOGIN_MAX_FAILS} gagal.", flush=True)

def _clear_login_failures(ip):
    _login_failures.pop(ip, None)
    _login_locks.pop(ip, None)

def _operator_locked(ip):
    return time.time() < _operator_locks.get(ip, 0)

def _record_operator_failure(ip):
    now = time.time()
    lst = _prune(_operator_failures.get(ip, []), OPERATOR_WINDOW_SECONDS)
    lst.append(now)
    _operator_failures[ip] = lst
    if len(lst) >= OPERATOR_MAX_FAILS:
        _operator_locks[ip] = now + 60
        _operator_failures[ip] = []
        print(f"🔒 Endpoint operator dikunci 60 dtk untuk IP {ip} ({OPERATOR_MAX_FAILS} kunci salah/menit).", flush=True)

def _poll_rate_limited(ip):
    now = time.time()
    lst = _prune(_poll_attempts.get(ip, []), 60)
    if len(lst) >= DEVICE_POLL_MAX_PER_MIN:
        return True
    lst.append(now)
    _poll_attempts[ip] = lst
    return False

# --- CSRF per-sesi (#12) ---
def _get_csrf_token():
    if 'csrf_token' not in session:
        session['csrf_token'] = secrets.token_hex(16)
    return session['csrf_token']

def _check_csrf():
    """Token CSRF dari header X-CSRF-Token (JS panel) atau field form."""
    sent = request.headers.get('X-CSRF-Token') or request.form.get('csrf_token') or ''
    expected = session.get('csrf_token') or ''
    return bool(expected) and hmac.compare_digest(str(sent), str(expected))

# --- Validasi yt_id anti path-traversal (#5) ---
_YT_ID_RE = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
_GALLERY_SUBS = {'audios', 'backgrounds', 'thumbnails', 'base_videos', 'overlays', 'special_songs'}

def _valid_yt_id(yt_id):
    return isinstance(yt_id, str) and bool(_YT_ID_RE.fullmatch(yt_id))

def _safe_join_under(base, *parts):
    """Gabung path + pastikan hasil tetap di bawah `base` (commonpath)."""
    base_abs = os.path.abspath(base)
    out = os.path.abspath(os.path.join(base_abs, *parts))
    if os.path.commonpath([out, base_abs]) != base_abs:
        raise ValueError("Path di luar direktori yang diizinkan")
    return out

# --- Penulis daftar concat FFmpeg tunggal (#20) ---
def _concat_list_line(path):
    """Satu baris `file '...'` untuk -f concat, dengan ' di-escape
    gaya shell ('\\''). Dipakai di SEMUA titik concat (dulu hanya 2 dari 6
    yang meng-escape)."""
    safe = os.path.abspath(path).replace('\\', '/').replace("'", "'\\''")
    return f"file '{safe}'\n"

def _write_concat_list(txt_path, paths):
    with open(txt_path, 'w', encoding='utf-8') as f:
        for p in paths:
            f.write(_concat_list_line(p))

class _QueueFullError(Exception):
    """Antrean render penuh — queue_put menolak task baru (#15)."""
    pass

def _mask_token(token):
    """Masking token untuk API/UI (#11): '••••' + 4 karakter terakhir."""
    if not token:
        return ''
    return '••••' + str(token)[-4:]

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
        _save_bot_config(bot_config)
    except Exception:
        pass
# 🔥 FASE 1 MONITORING: kunci bersama untuk operator Telegram (proses terpisah)
# agar bisa memanggil endpoint retry di bawah. Disimpan sekali di config.json,
# tidak pernah dikirim ke chat / dicatat di log. Pola sama seperti secret_key.
if is_configured() and not bot_config.get('operator_key'):
    bot_config['operator_key'] = secrets.token_hex(24)
    try:
        _save_bot_config(bot_config)
    except Exception:
        pass
# 🔥 FASE 9 MULTI-NODE: identitas node ini, dibuat sekali & menetap di
# config.json (pola sama seperti secret_key/operator_key). Default
# single-node: role 'primary', multi_node_enabled False — berperilaku
# persis seperti sebelum Fase 9. VPS kedua nanti: node_role 'worker',
# node_id berbeda, multi_node_enabled True di kedua node.
if is_configured() and not bot_config.get('node_id'):
    bot_config['node_id'] = 'node-' + secrets.token_hex(4)
    try:
        _save_bot_config(bot_config)
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

# 🔒 KEAMANAN (#3, #9, #10, #12): route yang boleh tanpa login HANYA
# login/setup/statis umum + endpoint operator (auth via operator_key
# HMAC, bukan cookie — jadi tidak butuh CSRF). device_login,
# poll_device_token dan serve_uploads SUDAH DIKELUARKAN dari daftar ini:
# keduanya wajib login panel. 'static' tetap bebas karena halaman login
# butuh CSS/JS — TAPI file hasil render di /static/final_*/clip_* tetap
# dikunci (dicek khusus di bawah). Endpoint pengubah state panel wajib
# membawa token CSRF per-sesi (header X-CSRF-Token) — termasuk blueprint
# clipper (dulu dikecualikan karena clipper.js dikelola terpisah; sejak
# perbaikan sisa #12, clipper.js mengirim token yang sama seperti panel).
_OPERATOR_ENDPOINTS = {'operator_retry_task', 'operator_approve_task',
                      'operator_reject_task', 'operator_approve_batch',
                      'operator_register_node', 'operator_create_shorts',
                      'operator_create_long', 'operator_extend_schedule',
                      'operator_set_autopilot', 'operator_set_paused',
                      'operator_set_daily_playlist',
                      'operator_set_channel_group', 'operator_delete_channel',
                      'operator_pixabay_key_add', 'operator_pixabay_key_list',
                      'operator_pixabay_key_del', 'operator_backup_create',
                      'operator_backup_status', 'operator_playlist_research',
                      'operator_playlist_create', 'operator_playlist_from_url', 'operator_update_panel', 'operator_publish_now',
                      'operator_restart_services', 'operator_set_channel_auto_shorts',
                      'operator_set_channel_publish_time', 'operator_kie_key_add',
                      'operator_kie_key_list', 'operator_kie_key_del',
                      'operator_zumo_create', 'operator_zumo_status',
                      'operator_zumo_model'}
_CSRF_EXEMPT_ENDPOINTS = {'login', 'setup', 'static'} | _OPERATOR_ENDPOINTS

@app.before_request
def check_security():
    # File hasil render/log di /static/ hanya untuk sesi login.
    if request.path.startswith('/static/final_') or request.path.startswith('/static/clip_'):
        if 'logged_in' not in session:
            return redirect(url_for('login'))
        if session.get('session_version') != _session_version_expected():
            session.clear()
            return redirect(url_for('login'))
        return
    if request.endpoint in _CSRF_EXEMPT_ENDPOINTS:
        return
    if not is_configured():
        return redirect(url_for('setup'))
    if 'logged_in' not in session:
        return redirect(url_for('login'))
    # #10: cookie lama mati begitu versi sesi di config berubah (logout /
    # ganti PIN menaikkan versi). Sesi lama tanpa versi = wajib login ulang.
    if session.get('session_version') != _session_version_expected():
        session.clear()
        return redirect(url_for('login'))
    # #12 (sisa): SEMUA metode pengubah state (POST/PUT/PATCH/DELETE)
    # wajib membawa token CSRF — termasuk blueprint clipper (start job,
    # upload font/cookie, hapus font/clip/cookie). GET yang hanya membaca
    # tidak dicek. Endpoint operator dikecualikan di atas (auth HMAC,
    # bukan cookie — tidak butuh CSRF).
    if request.method in ('POST', 'PUT', 'PATCH', 'DELETE'):
        if not _check_csrf():
            return jsonify({"status": "error",
                            "message": "Token CSRF tidak valid. Muat ulang halaman lalu coba lagi."}), 403

@app.after_request
def _security_headers(resp):
    # #7: header keamanan di semua respons. CSP memakai 'unsafe-inline'
    # untuk script/style karena panel memakai inline JS/CSS di template —
    # ini BUKAN CSP penuh (inline script tetap bisa jalan), tapi
    # object-src 'none' + base-uri 'self' + frame-ancestors 'none' tetap
    # membatasi. /uploads/ memakai CSP sandbox sendiri (lihat serve_uploads)
    # karena header CSP ganda akan saling menguatkan (AND), bukan mengganti.
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['X-Frame-Options'] = 'DENY'
    if request.endpoint != 'serve_uploads':
        # CDN yang dipakai panel: Chart.js (jsdelivr, index.html),
        # Tailwind (cdn.tailwindcss.com) + Font Awesome CSS/font
        # (cdnjs.cloudflare.com) di hub.html.
        resp.headers['Content-Security-Policy'] = (
            "default-src 'self'; script-src 'self' 'unsafe-inline' https://cdn.jsdelivr.net https://cdn.tailwindcss.com; "
            "style-src 'self' 'unsafe-inline' https://cdnjs.cloudflare.com; img-src 'self' data: https:; "
            "media-src 'self' blob:; connect-src 'self'; font-src 'self' https://cdnjs.cloudflare.com; "
            "form-action 'self'; object-src 'none'; base-uri 'self'; "
            "frame-ancestors 'none'")
    return resp

@app.route('/api/csrf_token')
def csrf_token_api():
    """Token CSRF per-sesi untuk JS panel (#12). Butuh login panel."""
    return jsonify({"csrf_token": _get_csrf_token()})

@app.route('/setup', methods=['GET', 'POST'])
def setup():
    # #25: setup ulang DILARANG bila config sudah berisi PIN owner — PIN
    # lama hanya bisa diganti lewat /api/change_pin (butuh PIN lama).
    # Kalau config.json ada tapi TIDAK punya PIN (rusak), setup diizinkan
    # sebagai jalur pemulihan. Owner yang lupa PIN harus menghapus
    # config.json manual di VPS (disengaja — tidak ada reset via web).
    if is_configured() and load_bot_config().get('admin_pin'):
        return redirect(url_for('login'))
    error = None
    if request.method == 'POST':
        pin = request.form.get('new_pin'); pin2 = request.form.get('confirm_pin')
        if not pin or not _pin_strength_ok(pin):
            error = "PIN minimal 8 karakter, campur huruf dan angka."
        elif pin != pin2: error = "PIN tidak cocok!"
        else:
            new_secret = secrets.token_hex(24)
            # #1: PIN disimpan sebagai HASH (bukan plaintext).
            # #25: config ditulis dengan permission 0600.
            _save_bot_config({"admin_pin": generate_password_hash(pin),
                              "secret_key": new_secret, "session_version": 1})
            app.secret_key = new_secret
            session.permanent = True
            session['logged_in'] = True
            session['session_version'] = 1
            _get_csrf_token()
            return redirect(url_for('hub'))
    return render_template('setup.html', error=error)

@app.route('/login', methods=['GET', 'POST'])
def login():
    if not is_configured(): return redirect(url_for('setup'))
    error = None
    if request.method == 'POST':
        ip = request.remote_addr or '?'
        if _login_locked(ip):
            error = (f"Terlalu banyak percobaan gagal. Coba lagi dalam "
                     f"{_login_lock_remaining(ip)} detik.")
        elif _check_pin(request.form.get('password') or ''):
            _clear_login_failures(ip)
            session.permanent = True
            session['logged_in'] = True
            session['session_version'] = _session_version_expected()
            _get_csrf_token()
            return redirect(url_for('hub'))
        else:
            # #1: kegagalan dicatat (IP saja — PIN TIDAK PERNAH masuk log).
            _record_login_failure(ip)
            print(f"⚠️ Percobaan login gagal dari IP {ip}.", flush=True)
            error = 'Akses Ditolak! PIN Salah.'
    return render_template('login.html', error=error)

@app.route('/api/change_pin', methods=['POST'])
def change_pin():
    """Ganti PIN panel (#1). Butuh login + PIN lama yang benar.
    Sesi lain ikut mati (versi sesi naik) — termasuk cookie lama."""
    data = request.get_json(silent=True) or {}
    old_pin = data.get('old_pin') or ''
    new_pin = data.get('new_pin') or ''
    new_pin2 = data.get('confirm_pin') or ''
    if not _check_pin(old_pin):
        return jsonify({"status": "error", "message": "PIN lama salah."}), 400
    if not _pin_strength_ok(new_pin):
        return jsonify({"status": "error",
                        "message": "PIN baru minimal 8 karakter, campur huruf dan angka."}), 400
    if new_pin != new_pin2:
        return jsonify({"status": "error", "message": "PIN baru tidak cocok."}), 400
    cfg = load_bot_config()
    cfg['admin_pin'] = generate_password_hash(new_pin)
    cfg['session_version'] = _session_version_expected() + 1
    _save_bot_config(cfg)
    session['session_version'] = cfg['session_version']
    return jsonify({"status": "success", "message": "PIN berhasil diganti. Sesi lain sudah dimatikan."})

@app.route('/logout', methods=['POST'])
def logout():
    # #10 + #12: logout via POST (bukan GET) dan menaikkan versi sesi —
    # cookie lama yang sudah diterbitkan langsung tidak berlaku lagi.
    try:
        _bump_session_version()
    except Exception:
        pass
    session.clear()
    return redirect(url_for('login'))

@app.route('/hub')
def hub():
    # Halaman untuk memilih masuk ke Factory atau Clipper
    return render_template('hub.html', csrf_token=_get_csrf_token())

@app.route('/clipper')
def clipper_dashboard():
    # Halaman UI utama untuk Clipper
    return render_template('clipper.html', csrf_token=_get_csrf_token())

BASE_UPLOAD = os.path.join(BASE_DIR, "uploads")
DB_FILE = os.path.join(BASE_DIR, 'channels_db.json')
TASKS_FILE = os.path.join(BASE_DIR, 'tasks_db.json')
PRESETS_FILE = os.path.join(BASE_DIR, 'presets.json')
CLIENT_SECRETS_FILE = os.path.join(BASE_DIR, 'client_secret.json')
# 🔥 v1.2.25: REGISTRY MULTI-CLIENT OAUTH — panel bisa menyimpan BANYAK
# client_secret.json (satu per project Google). Kuota YouTube dihitung PER
# PROJECT (100 upload/hari, 10.000 unit/hari, 100 pencarian/hari), jadi
# tiap project cadangan butuh client-nya sendiri. Registry hanya menyimpan
# METADATA (client_id BUKAN secret — nomor project & client_id aman
# ditampilkan); file secret JSON disimpan per client di oauth_clients/
# (chmod 0600, di-.gitignore, ikut backup). client_secret TIDAK PERNAH
# dikembalikan ke browser / log / Telegram.
OAUTH_CLIENTS_FILE = os.path.join(BASE_DIR, 'oauth_clients.json')
OAUTH_CLIENTS_DIR = os.path.join(BASE_DIR, 'oauth_clients')


def _oauth_client_id_of_cred_str(cred_str):
    """client_id dari satu entri token channel (string JSON), atau None.
    Hanya identifier — BUKAN secret."""
    try:
        if not cred_str:
            return None
        return (json.loads(cred_str) or {}).get('client_id') or None
    except Exception:
        return None


def _oauth_project_of_cred_str(cred_str):
    """Nomor project Google (prefix client_id sebelum '-') dari satu entri
    token channel, atau None."""
    cid = _oauth_client_id_of_cred_str(cred_str)
    return str(cid).split('-')[0] or None if cid else None


def _oauth_project_of_client_id(client_id):
    try:
        return str(client_id or '').split('-')[0] or None
    except Exception:
        return None


def _oauth_client_filename(client_id, project_id):
    """Nama file secret satu client — UNIK per client_id (bukan per
    project): dua client satu project (TV + Desktop) tidak boleh saling
    menimpa (insiden v1.3.50: upload Desktop menimpa file TV sehingga
    device login mengirim client Desktop ke Google -> invalid_client)."""
    stem = str(client_id or '').split('.apps.googleusercontent.com')[0]
    stem = re.sub(r'[^A-Za-z0-9_.-]', '_', stem) or 'client'
    return f"{stem}.json"


def _oauth_clients_load():
    """Baca registry multi-client. Migrasi SEKALI dari file tunggal lama
    (client_secret.json → entri pertama, primary) bila registry belum ada.
    File lama TIDAK dihapus (cadangan + kompatibilitas). Tidak pernah raise."""
    try:
        if os.path.exists(OAUTH_CLIENTS_FILE):
            with open(OAUTH_CLIENTS_FILE, 'r') as f:
                store = json.load(f) or {}
            clients = store.get('clients') or []
            if isinstance(clients, list):
                return {"clients": [c for c in clients if isinstance(c, dict)]}
        store = {"clients": []}
        if os.path.exists(CLIENT_SECRETS_FILE):
            try:
                with open(CLIENT_SECRETS_FILE, 'r') as f:
                    data = json.load(f)
                conf, _err = _oauth_validate_client_json(data)
                if conf:
                    os.makedirs(OAUTH_CLIENTS_DIR, exist_ok=True)
                    dest = os.path.join(OAUTH_CLIENTS_DIR, _oauth_client_filename(conf['client_id'], conf['project_id']))
                    with open(dest, 'w') as f:
                        json.dump(data, f, indent=2)
                    try:
                        os.chmod(dest, 0o600)
                    except Exception:
                        pass
                    store["clients"].append({
                        "client_id": conf['client_id'],
                        "project_id": conf['project_id'],
                        "type": conf['type'],
                        "name": conf['project_id'],
                        "file": os.path.relpath(dest, BASE_DIR),
                        "uploaded_at": datetime.fromtimestamp(
                            os.path.getmtime(CLIENT_SECRETS_FILE),
                            tz=timezone.utc).isoformat(),
                        "primary": True,
                        "migrated_from_legacy": True,
                    })
                    _oauth_clients_save(store)
            except Exception:
                pass
        return store
    except Exception:
        return {"clients": []}


def _oauth_clients_save(store):
    """Tulis registry (metadata saja) + kunci 0600. Gagal chmod tidak fatal."""
    with db_lock:
        with open(OAUTH_CLIENTS_FILE, 'w') as f:
            json.dump(store, f, indent=2)
    try:
        os.chmod(OAUTH_CLIENTS_FILE, 0o600)
    except Exception:
        pass


def _oauth_client_entries():
    return list(_oauth_clients_load().get('clients') or [])


def _oauth_client_by_id(client_id):
    for e in _oauth_client_entries():
        if e.get('client_id') == client_id:
            return e
    return None


def _oauth_client_by_project(project_id):
    for e in _oauth_client_entries():
        if str(e.get('project_id')) == str(project_id):
            return e
    return None


def _oauth_primary_client():
    entries = _oauth_client_entries()
    for e in entries:
        if e.get('primary'):
            return e
    return entries[0] if entries else None


def _oauth_desktop_client():
    """Client Desktop untuk login browser (tambah channel 1x login):
    entri bernama 'desktop' bila ada, kalau tidak client utama."""
    for e in _oauth_client_entries():
        if (e.get('name') or '').strip().lower() == 'desktop':
            return e
    return _oauth_primary_client()


def _oauth_set_primary(client_id):
    """Jadikan satu client sebagai utama (dipakai default device login).
    Kembalikan True bila client ditemukan."""
    store = _oauth_clients_load()
    found = False
    for e in store.get('clients') or []:
        if e.get('client_id') == client_id:
            e['primary'] = True
            found = True
        else:
            e['primary'] = False
    if found:
        _oauth_clients_save(store)
    return found


def _oauth_validate_client_json(data):
    """Validasi file client_secret.json. Kembalikan (conf, error):
    conf = {'client_id','client_secret','project_id','type'} — client_secret
    hanya dipakai di server, tidak pernah dikembalikan ke browser.
    project_id diambil dari JSON, atau diturunkan dari prefix client_id."""
    try:
        if not isinstance(data, dict):
            return None, "File bukan JSON object."
        conf_raw = None
        ctype = None
        for key in ('installed', 'web'):
            if isinstance(data.get(key), dict):
                conf_raw = data[key]
                ctype = key
                break
        if not conf_raw:
            return None, "File bukan client OAuth (tidak ada bagian 'installed'/'web')."
        client_id = (conf_raw.get('client_id') or '').strip()
        client_secret = (conf_raw.get('client_secret') or '').strip()
        if not client_id or not client_secret:
            return None, "File client OAuth tidak berisi client_id/client_secret."
        project_id = (conf_raw.get('project_id') or '').strip()
        if not project_id:
            project_id = _oauth_project_of_client_id(client_id)
        if not project_id:
            return None, "Project ID tidak bisa dibaca dari file."
        return {"client_id": client_id, "client_secret": client_secret,
                "project_id": project_id, "type": ctype}, None
    except Exception as e:
        return None, f"File tidak bisa dibaca: {e}"


def _oauth_register_client(file_bytes, name=None):
    """Daftarkan satu client_secret.json ke registry. Upload ulang client
    yang sama (client_id sama) = update file, BUKAN entri ganda.
    Kembalikan (entry_metadata, error). entry TIDAK berisi secret."""
    try:
        data = json.loads(file_bytes.decode('utf-8') if isinstance(file_bytes, (bytes, bytearray)) else file_bytes)
    except Exception:
        return None, "File bukan JSON yang valid."
    conf, err = _oauth_validate_client_json(data)
    if not conf:
        return None, err
    try:
        os.makedirs(OAUTH_CLIENTS_DIR, exist_ok=True)
        dest = os.path.join(OAUTH_CLIENTS_DIR, _oauth_client_filename(conf['client_id'], conf['project_id']))
        with open(dest, 'w') as f:
            json.dump(data, f, indent=2)
        try:
            os.chmod(dest, 0o600)
        except Exception:
            pass
        store = _oauth_clients_load()
        existing = None
        for e in store.get('clients') or []:
            if e.get('client_id') == conf['client_id']:
                existing = e
                break
        if existing:
            existing['file'] = os.path.relpath(dest, BASE_DIR)
            existing['project_id'] = conf['project_id']
            existing['type'] = conf['type']
            if name:
                existing['name'] = name
            existing['uploaded_at'] = _wib_now().isoformat()
            entry = existing
        else:
            entry = {
                "client_id": conf['client_id'],
                "project_id": conf['project_id'],
                "type": conf['type'],
                "name": (name or '').strip() or conf['project_id'],
                "file": os.path.relpath(dest, BASE_DIR),
                "uploaded_at": _wib_now().isoformat(),
                "primary": not store.get('clients'),
            }
            store.setdefault('clients', []).append(entry)
        _oauth_clients_save(store)
        return dict(entry), None
    except Exception as e:
        return None, f"Gagal menyimpan client: {e}"


def _oauth_client_secret_path(entry):
    """Path absolut file secret satu client (di bawah oauth_clients/).
    Tolak path yang keluar dari folder itu."""
    try:
        rel = (entry or {}).get('file') or ''
        path = _safe_join_under(OAUTH_CLIENTS_DIR, os.path.basename(rel))
        return path if os.path.isfile(path) else None
    except Exception:
        return None


def _oauth_client_conf(entry):
    """(client_id, client_secret) satu client dari file tersimpannya.
    Dipakai HANYA di server (device flow / refresh token)."""
    try:
        if not entry:
            return None, None
        path = _oauth_client_secret_path(entry)
        if not path:
            return None, None
        with open(path, 'r') as f:
            data = json.load(f)
        conf, _err = _oauth_validate_client_json(data)
        if not conf:
            return None, None
        return conf['client_id'], conf['client_secret']
    except Exception:
        return None, None


def _oauth_delete_client(client_id):
    """Hapus satu client dari registry + file secret-nya. Client terakhir
    tidak boleh dihapus (tambahkan pengganti dulu). Bila yang dihapus adalah
    utama, client pertama yang tersisa jadi utama. Token channel yang sudah
    diterbitkan client itu TIDAK ikut dihapus — channel tetap bisa memakai
    tokennya sampai kedaluwarsa, tapi tidak bisa refresh / login ulang
    dengan client itu sampai JSON-nya di-upload lagi.
    Kembalikan (ok, pesan)."""
    store = _oauth_clients_load()
    entries = store.get('clients') or []
    target = None
    for e in entries:
        if e.get('client_id') == client_id:
            target = e
            break
    if not target:
        return False, "Client tidak ditemukan."
    if len(entries) <= 1:
        return False, "Ini satu-satunya client — tambahkan client lain dulu sebelum menghapus."
    was_primary = bool(target.get('primary'))
    store['clients'] = [e for e in entries if e.get('client_id') != client_id]
    if was_primary and store['clients']:
        store['clients'][0]['primary'] = True
    _oauth_clients_save(store)
    try:
        path = _oauth_client_secret_path(target)
        if path and os.path.isfile(path):
            os.remove(path)
    except Exception:
        pass
    try:
        if os.path.exists(CLIENT_SECRETS_FILE):
            with open(CLIENT_SECRETS_FILE, 'r') as f:
                legacy = json.load(f)
            lconf, _e = _oauth_validate_client_json(legacy)
            if lconf and lconf['client_id'] == client_id:
                os.remove(CLIENT_SECRETS_FILE)
    except Exception:
        pass
    return True, f"Client {target.get('name') or target.get('project_id')} dihapus."


def _oauth_channel_cred_client_ids(channel):
    """Daftar client_id token yang dimiliki channel, sesuai urutan
    creds_list (index 0 = utama)."""
    out = []
    try:
        for s in (channel or {}).get('creds_list') or [(channel or {}).get('creds_json')]:
            cid = _oauth_client_id_of_cred_str(s)
            if cid and cid not in out:
                out.append(cid)
    except Exception:
        pass
    return out


def _oauth_channel_primary_client_id(channel):
    """Client utama channel = client_id token pertama (creds_list[0]).
    Disimpan juga di field channel 'oauth_client_id' supaya panel bisa
    menampilkannya tanpa membaca token. Lazy-persist, tidak pernah raise."""
    try:
        if not channel:
            return None
        cid = channel.get('oauth_client_id') or None
        first = _oauth_channel_cred_client_ids(channel)
        if first:
            if cid != first[0]:
                cid = first[0]
                try:
                    channel['oauth_client_id'] = cid
                except Exception:
                    pass
            return cid
        return cid
    except Exception:
        return None


def _oauth_is_quota_error(exc):
    """True bila exception adalah quotaExceeded YouTube (403) — dari
    HttpError Google API (reason) maupun teks error lain."""
    try:
        s = str(exc or '')
        if 'quotaExceeded' in s:
            return True
        err = getattr(exc, 'content', None)
        if err:
            try:
                info = json.loads(err.decode('utf-8') if isinstance(err, (bytes, bytearray)) else err)
                for e in (info.get('error') or {}).get('errors', []):
                    if e.get('reason') == 'quotaExceeded':
                        return True
            except Exception:
                pass
        return 'quotaexceeded' in s.lower()
    except Exception:
        return False


def _oauth_missing_backup_clients(channel):
    """Client di registry yang channel ini BELUM punya tokennya — calon
    cadangan yang belum bisa dipakai auto-switch sampai channel di-device-
    login dengan client itu. Kembalikan daftar entry metadata."""
    try:
        have = set(_oauth_channel_cred_client_ids(channel))
        return [e for e in _oauth_client_entries() if e.get('client_id') not in have]
    except Exception:
        return []


def _oauth_quota_failure_hint(channel, channel_name=''):
    """Pesan jujur saat SEMUA token channel kena quotaExceeded: sebut
    project cadangan yang belum dihubungkan (bila ada) + langkahnya.
    Kembalikan string tambahan ('' bila tidak ada cadangan sama sekali)."""
    try:
        missing = _oauth_missing_backup_clients(channel)
        if not missing:
            return ''
        names = ", ".join((e.get('name') or e.get('project_id') or '?') for e in missing)
        who = f"Channel '{channel_name}'" if channel_name else "Channel ini"
        return (f" {who} belum dihubungkan di project cadangan ({names}) — "
                "device login channel ini dengan client cadangan itu supaya "
                "lain kali bisa auto-switch.")
    except Exception:
        return ''


def _oauth_upsert_channel_cred(channel, cred_json):
    """Sisipkan token hasil device login ke creds_list channel.
    Dedup BERDASARKAN client_id (bukan isi JSON — access_token berubah
    tiap login): token client yang sama MENGGANTI entri lama di tempat
    yang sama (urutan/primary tidak berubah); client baru ditambahkan
    di akhir. Kembalikan True bila ada perubahan."""
    try:
        if not channel or not cred_json:
            return False
        cid = _oauth_client_id_of_cred_str(cred_json)
        creds = channel.get('creds_list')
        if not isinstance(creds, list) or not creds:
            legacy = channel.get('creds_json') or ''
            creds = [legacy] if legacy else []
            channel['creds_list'] = creds
        if cid:
            for i, s in enumerate(creds):
                if s and _oauth_client_id_of_cred_str(s) == cid:
                    if creds[i] != cred_json:
                        creds[i] = cred_json
                        return True
                    return False
        if cred_json not in creds:
            creds.append(cred_json)
            return True
        return False
    except Exception:
        return False


def _oauth_set_channel_primary_cred(channel, client_id):
    """Pindahkan token client tertentu ke index 0 creds_list channel
    (menjadikannya utama) + set field oauth_client_id.
    Kembalikan (True, None) bila berhasil, (False, pesan) bila channel
    belum punya token untuk client itu."""
    try:
        if not channel or not client_id:
            return False, "Channel/client tidak valid."
        creds = channel.get('creds_list') or ([channel.get('creds_json')] if channel.get('creds_json') else [])
        idx = None
        for i, s in enumerate(creds):
            if s and _oauth_client_id_of_cred_str(s) == client_id:
                idx = i
                break
        if idx is None:
            return False, "Channel belum punya token untuk client ini."
        if idx != 0:
            creds.insert(0, creds.pop(idx))
            channel['creds_list'] = creds
        channel['oauth_client_id'] = client_id
        return True, None
    except Exception as e:
        return False, str(e)


def _oauth_notify_quota_switch(channel_name, what, from_pid, to_pid, used=None, limit=None):
    """Laporan Telegram saat auto-switch kuota berhasil. Untuk upload,
    sertakan hitungan hari ini (used/limit); untuk riset, cukup kabar
    pindahnya."""
    try:
        if what == 'riset':
            msg = (f"Kuota pencarian project {from_pid or '?'} habis saat riset "
                   f"untuk channel {channel_name} — otomatis pindah ke project "
                   f"{to_pid or '?'} dan riset berhasil.")
        else:
            msg = (f"Kuota project {from_pid or '?'} habis saat {what} untuk "
                   f"channel {channel_name} — otomatis pindah ke project "
                   f"{to_pid or '?'} dan berhasil.")
            if used is not None and limit:
                msg += f" Upload hari ini di project {to_pid or '?'}: {used}/{limit}."
        send_tg_message(msg)
    except Exception:
        pass
SCOPES = ['https://www.googleapis.com/auth/youtube', 'https://www.googleapis.com/auth/youtube.upload']
# ⚠️ Scope Analytics (yt-analytics.readonly + yt-analytics-monetary.readonly)
# TIDAK BOLEH masuk SCOPES: endpoint device-code Google menolaknya dengan
# "invalid_scope / Invalid device flow scope" (diverifikasi langsung ke
# oauth2.googleapis.com/device/code 2026-11-18: youtube & youtube.upload
# lolos, kedua scope Analytics ditolak). Device flow (client TV) hanya
# mengizinkan scope YouTube biasa — Analytics butuh auth-code flow lewat
# browser (client Desktop), belum dibangun. Akibatnya subs per video
# tampil '—' dan RPM memakai isian manual di Settings (fallback Fase 9);
# helper Analytics di bawah tetap ada tapi _query_analytics tidak
# memanggil API selama scope-nya tidak ada di SCOPES.
# menampilkan "perlu taut ulang" (lihat blok FASE 10).

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

# 🔥 v1.2.18: PROGRES RENDER/UPLOAD — persen 0-100 di task (panel + Telegram).
# Task aktif mendapat field progress_pct (int 0-100, PER TAHAP — bukan persen
# keseluruhan video) + progress_stage ('render' / 'upload' / 'qc').
# Persen render DIUKUR dari FFmpeg (time= di stderr, atau frame ke-N dari
# total frame di render klasik); persen upload DIUKUR dari next_chunk()
# YouTube (sudah ada di teks status, di sini ditulis juga ke field
# terstruktur). Penulisan di-throttle (~5 detik / naik >=5% / ganti tahap /
# 100%) supaya tasks_db.json tidak ditulis tiap frame. Fungsi-fungsi ini
# TIDAK PERNAH melempar — progres tidak boleh merusak render.
_PROGRESS_LAST_WRITE = {}

def _ffmpeg_time_to_sec(text):
    """Parse 'time=HH:MM:SS.cc' dari stderr FFmpeg -> detik (float) atau None.
    Bila ada beberapa baris, ambil yang TERAKHIR (paling baru)."""
    try:
        all_m = re.findall(r'time=(\d+):(\d+):([\d.]+)', text or '')
        if not all_m:
            return None
        h, m, s = all_m[-1]
        return int(h) * 3600 + int(m) * 60 + float(s)
    except Exception:
        return None

def _task_set_progress(task_id, pct=None, stage=None, force=False):
    """Tulis progress_pct / progress_stage ke task aktif (throttle ~5 dtk /
    naik >=5% / ganti tahap / 100%). pct=None = jangan ubah persen;
    stage=None = jangan ubah tahap. Aman gagal silent."""
    try:
        now = time.time()
        if pct is not None:
            pct = max(0, min(100, int(pct)))
        last = _PROGRESS_LAST_WRITE.get(task_id)
        if not force and last is not None:
            lt, lp, ls = last
            stage_changed = stage is not None and stage != ls
            pct_moved = (pct is not None and lp is not None and pct - lp >= 5)
            pct_done = (pct == 100 and lp != 100)
            if not stage_changed and not pct_moved and not pct_done and (now - lt) < 5:
                return
        found = False
        with db_lock:
            for d in active_tasks:
                if d.get('id') == task_id:
                    if pct is not None:
                        d['progress_pct'] = pct
                    if stage is not None:
                        d['progress_stage'] = stage
                    d['updated_at'] = now
                    found = True
                    break
        if not found:
            return
        save_tasks_db()
        _PROGRESS_LAST_WRITE[task_id] = (
            now,
            pct if pct is not None else (last[1] if last else None),
            stage if stage is not None else (last[2] if last else None),
        )
    except Exception:
        pass

# ==========================================
# 🛡️ SISTEM KETAHANAN (ANTI-STUCK & ANTI-LOST)
# ==========================================
def send_tg_message(msg, reply_markup=None):
    """Kirim pesan ke Telegram kalau token & chat_id tersimpan. Aman gagal silent.
    reply_markup opsional — dipakai untuk tombol inline (mis. Retry) dari operator.
    Emoji BOLEH di chat Telegram (aturan user 20 Nov 2026) — yang dilarang
    hanya emoji di judul/deskripsi video YouTube (sanitasi upload v1.2.13)."""
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


def send_tg_photo(photo_path, caption='', reply_markup=None):
    """Kirim foto ke Telegram (dipakai preview approval thumbnail
    playlist v1.2.24). Aman gagal silent — preview teks tetap dikirim
    pemanggil. Caption dibatasi 1024 karakter (batas Telegram)."""
    try:
        if not photo_path or not os.path.isfile(photo_path):
            return False
        bot_config = load_bot_config()
        token, chat_id = bot_config.get('tg_token'), bot_config.get('tg_chat_id')
        if not token or not chat_id:
            return False
        with open(photo_path, 'rb') as f:
            files = {'photo': (os.path.basename(photo_path), f, 'image/jpeg')}
            data = {'chat_id': chat_id,
                    'caption': (caption or '')[:1024]}
            if reply_markup is not None:
                data['reply_markup'] = json.dumps(reply_markup)
            requests.post(f"https://api.telegram.org/bot{token}/sendPhoto",
                          files=files, data=data, timeout=30)
        return True
    except Exception:
        return False

def startup_sanitizer():
    """🔥 BARU: Bersihkan 'sisa-sisa' setelah VPS mati mendadak.
    Dipanggil sekali saat aplikasi mulai, sebelum worker jalan."""
    import subprocess as _sub
    notifs = []
    try:
        # 1. Bunuh ffmpeg zombie yang tertinggal dari proses mati mendadak
        proc = _sub.run(["pkill", "-9", "-f", "ffmpeg"], capture_output=True, timeout=10)
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
            msg = "🔔 *Update dari Ziqva Muse Bot*\n\n" + "\n\n".join(events)
            try: send_tg_message(msg)
            except Exception: pass

def _short_tayang_date(publish_date):
    """Tanggal tayang singkat untuk pesan Telegram: '2026-11-20 05:35'
    -> '20 Nov 05:35'. Bila format tidak dikenal, kembalikan apa adanya."""
    s = (publish_date or '').strip()
    if not s:
        return '—'
    try:
        return datetime.strptime(s, "%Y-%m-%d %H:%M").strftime("%d %b %H:%M")
    except Exception:
        return s


def notify_event(emoji, title, status, reply_markup=None):
    """🔥 BARU: masukkan event notifikasi (selesai/gagal) ke buffer Telegram.
    Event digabung & dikirim maksimal tiap 1 menit agar tidak spam chat.
    Kalau reply_markup diisi (mis. tombol Retry untuk task gagal), event TIDAK
    masuk buffer — langsung dikirim saat itu juga, karena satu pesan gabungan
    tidak bisa membawa tombol untuk task tertentu.
    🔒 #23: judul/status dinamis di-escape Markdown Telegram (judul video
    bisa mengandung *, _, [, ] dll. — tanpa escape, format pesan rusak /
    bisa dipakai injeksi Markdown)."""
    try:
        # _tg_escape didefinisikan di bawah (dipakai saat fungsi dipanggil,
        # bukan saat modul dimuat — jadi aman).
        title_e = _tg_escape(str(title)[:70])
        status_e = _tg_escape(str(status))
        if reply_markup is not None:
            send_tg_message(f"{emoji} *{title_e}*\nStatus: {status_e}", reply_markup=reply_markup)
            return
        tg_event_queue.append(f"{emoji} *{title_e}*\nStatus: {status_e}")
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

def queue_put(blueprint, allow_overflow=False):
    # 🔥 FASE 9: cap pemilik task. Di single-node (default) nilainya tidak
    # dipakai — worker mengambil semua task. Di multi-node, worker hanya
    # mengambil task yang assigned_node-nya == node_id sendiri (atau kosong
    # = task lama, boleh diambil siapa saja).
    # 🔒 #15: tolak task baru bila antrean sudah penuh (maks 200), kecuali
    # jalur internal yang memasukkan lagi task yang SUDAH ADA (resume /
    # aktivator / startup) — antrean penuh tidak boleh membuat task lama
    # hilang diam-diam.
    if not allow_overflow and render_queue.qsize() >= QUEUE_MAX_SIZE:
        raise _QueueFullError(f"Antrean render penuh (maks {QUEUE_MAX_SIZE} task). "
                              "Tunggu antrean berkurang lalu coba lagi.")
    if isinstance(blueprint, dict):
        blueprint.setdefault('assigned_node', _node_id())
    render_queue.put((_queue_priority(blueprint), next(_queue_seq), blueprint))

# 🔒 #15: ID task monotonik (milidetik + naik terus) supaya dua batch
# dalam milidetik yang sama tidak pernah menghasilkan ID yang sama.
_last_task_id_ms = 0

def _next_task_id():
    global _last_task_id_ms
    now_ms = int(time.time() * 1000)
    _last_task_id_ms = max(now_ms, _last_task_id_ms + 1)
    return _last_task_id_ms

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
            try:
                save_channels(database_channel)
            except Exception as e:
                # 🔥 v1.3.33 (audit M2): jangan diam-diam — cooldown
                # yang gagal tersimpan hilang saat restart dan channel
                # bisa upload lagi padahal kuota sudah habis.
                print(f"[cooldown] GAGAL simpan cooldown {yt_id}: {e}", flush=True)
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

# 🔥 v1.2.20: batas upload harian per project Google — 100 video/hari.
# Dulu 6 (asumsi lama: 10.000 unit ÷ 1.600 unit/upload). Dicek LIVE di
# Google Cloud Console 2026-11-19 (project-musik-1): kuota "Video Uploads
# per day" limit 100/hari (baru 7 terpakai), sementara "Queries per day"
# (10.000 unit) baru 136 terpakai padahal 6 video sudah naik — upload
# TIDAK memakan kuota unit seperti asumsi lama. Konstanta ini dipakai
# sebagai batas PER PROJECT (gate worker + get_project_quotas) dan juga
# sebagai batas per channel di track_daily_upload; keduanya 100.
# Counter disimpan di channels_db.json per channel → tahan restart.
MAX_UPLOADS_PER_CHANNEL_PER_DAY = 100

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
# 🔥 FASE 8: Shorts Muse — video vertikal 1080x1920 dari naskah + CTA.
# Naskah/hook/CTA dibuat oleh Muse di chat; VPS hanya me-render & upload.
SHORTS_MUSE_KIND = 'shorts_muse'      # penanda task di blueprint & antrean aktif

# 🎵 MUSIK PLAYLIST (v1.2.7): kompilasi musik dari referensi trending.
PLAYLIST_KIND = 'playlist'            # penanda task di blueprint & antrean aktif
PLAYLIST_TMP_DIR = os.path.join(BASE_UPLOAD, 'playlist_tmp')
PLAYLIST_JOBS_FILE = os.path.join(BASE_DIR, 'playlist_jobs.json')
# 🎵 ZUMO / Kie.ai (v1.3.0): generate lagu orisinal -> render visual statis
# + spectrum + salju/sakura -> upload. Task kind 'zumo_playlist' memakai
# worker + gates yang SAMA dengan playlist (disk, pause, kuota, approval, QC).
ZUMO_KIND = 'zumo_playlist'
ZUMO_TMP_DIR = os.path.join(BASE_UPLOAD, 'zumo_tmp')
ZUMO_JOBS_FILE = os.path.join(BASE_DIR, 'zumo_jobs.json')
ZUMO_ASSETS_DIR = os.path.join(BASE_UPLOAD, 'zumo_assets')
# Bank audio (user 20:58 WIB): uploads/zumo_bank/<nama-channel>/ —
# 1 folder per nama channel (disanitasi zumo_core.zumo_bank_dirname),
# file mentah hasil download + bank_index.json (metadata). Sudah di
# bawah uploads/ -> ikut ter-backup otomatis (backup mencakup uploads/).
ZUMO_BANK_DIR = os.path.join(BASE_UPLOAD, 'zumo_bank')
PLAYLIST_MAX_CLIPS = 15               # klip footage Pixabay per video
PLAYLIST_MIN_CLIPS = 5                # minimal klip agar render jalan
PLAYLIST_RESEARCH_CACHE_SEC = 3600    # cache riset 1 jam (hemat kuota)
PLAYLIST_MIN_VIEWS = 0                # v1.3.5 koreksi: BUANG filter view
# sepenuhnya — views berapa pun (termasuk 0) boleh jadi referensi; yang
# penting videonya ada dan audionya bisa diunduh. Tier berbasis UMUR saja.
# v1.3.5 — tier bertingkat (days, min_views=0, diabaikan): tier 1 video
# terbit <=30 hari, tier 2 <=90 hari, tier 3 <=365 hari, tier 4 evergreen
# (semua umur). Prinsip: jangan pulang tangan kosong cuma gara-gara
# filter — lagu lawas (mis. Panbers) hampir tidak punya video BARU
# 30 hari terakhir, padahal video lamanya jutaan views dan itu justru
# yang dicari untuk nostalgia.
PLAYLIST_TIERS = [(30, 0), (90, 0), (365, 0), (None, 0)]
PLAYLIST_TIER_LABELS = ["30 hari", "90 hari", "1 tahun", "evergreen"]
PLAYLIST_TIER_MAX_RESULTS = [10, 15, 25, 25]
PLAYLIST_RESEARCH_MAX_RESULTS = 25  # hasil terbanyak per tier (tier 3-4)
PLAYLIST_MAX_AUDIO_BYTES = 500 * 1024 * 1024   # batas audio referensi (500 MB)
PLAYLIST_MAX_CLIP_BYTES = 300 * 1024 * 1024    # batas 1 klip footage (300 MB)
SHORTS_MUSE_DURATIONS = (15, 30, 60)  # pilihan durasi target (detik)
SHORTS_MUSE_MIN_SEC = 5               # batas bawah durasi (validasi server)
SHORTS_MUSE_MAX_SEC = 180             # batas atas durasi (Shorts maks 3 menit)
SHORTS_MUSE_HOOK_SEC = 4.0            # hook tampil di awal video
SHORTS_MUSE_CTA_SEC = 5.0             # kartu CTA di akhir video
SHORTS_MUSE_DEFAULT_CTA = 'Subscribe' # CTA bila form dikosongkan
# ⏰ JADWAL OTOMATIS SHORT — 1 short per channel per hari, jam buat & jam
# tayang bisa diatur user per channel (Settings → JADWAL OTOMATIS SHORT).
# Konten (hook/naskah/CTA/judul) dibuat otomatis dari bank per niche yang
# berputar berdasarkan tanggal, supaya tidak sama terus. Bila bank judul
# channel ada, judul diambil dari sana secara bergiliran; bila kosong,
# judul dibuat dari label tema + tanggal (tanpa klaim palsu).
AUTO_SHORTS_NICHES = ('sleep', 'lofi', 'piano', 'hujan', 'meditasi', 'fokus', 'anak', 'asmr')
AUTO_SHORTS_NICHE_LABELS = {
    'sleep': 'Musik Tidur', 'lofi': 'Lofi', 'piano': 'Piano',
    'hujan': 'Suara Hujan', 'meditasi': 'Meditasi', 'fokus': 'Musik Fokus',
    'asmr': 'ASMR',
}
AUTO_SHORTS_DEFAULTS = {
    'enabled': False, 'theme': 'sleep',
    'create_time': '06:00', 'publish_time': '19:00', 'duration_sec': 30,
}
AUTO_SHORTS_STATE_FILE = os.path.join(BASE_DIR, 'auto_shorts_state.json')
AUTO_SHORTS_TITLE_LABELS = {
    'sleep': 'Musik Tidur Nyenyak', 'lofi': 'Lofi Santai untuk Fokus',
    'piano': 'Piano Menenangkan', 'hujan': 'Suara Hujan Menenangkan',
    'meditasi': 'Meditasi Tenang', 'fokus': 'Musik Fokus & Konsentrasi',
    'asmr': 'ASMR Menenangkan untuk Tidur',
}
AUTO_SHORTS_BANK = {
    'sleep': {
        'hooks': ['Tidur nyenyak malam ini', 'Pejamkan mata, tenangkan pikiran',
                  'Musik tidur untuk malam yang tenang', 'Saatnya istirahat yang nyenyak'],
        'scripts': ['Musik lembut ini menemani tidurmu malam ini.',
                    'Tarik napas dalam-dalam… hembuskan perlahan…',
                    'Biarkan nada-nada tenang ini membawamu ke tidur yang nyenyak.',
                    'Selamat beristirahat, sampai jumpa di pagi yang segar.'],
        'ctas': ['Subscribe untuk musik tidur setiap malam', 'Subscribe'],
    },
    'lofi': {
        'hooks': ['Lofi santai untuk harimu', 'Nada chill untuk fokus belajar',
                  'Beat lembut untuk menemani kerja', 'Santai dulu, dengerin lofi ini'],
        'scripts': ['Lofi lembut ini cocok untuk belajar, kerja, atau sekadar bersantai.',
                    'Pasang headphone-mu dan nikmati alunannya.',
                    'Satu lagu lofi untuk menemani harimu.',
                    'Biarkan beat-nya mengalir pelan-pelan.'],
        'ctas': ['Subscribe untuk lofi baru setiap hari', 'Subscribe'],
    },
    'piano': {
        'hooks': ['Piano lembut untuk hatimu', 'Melodi piano yang menenangkan',
                  'Dengarkan piano ini sejenak', 'Nada piano untuk sore yang tenang'],
        'scripts': ['Melodi piano ini menemani harimu yang tenang.',
                    'Tutup mata sejenak dan dengarkan nadanya.',
                    'Piano lembut untuk istirahat dan refleksi.',
                    'Biarkan setiap nada menenangkan pikiranmu.'],
        'ctas': ['Subscribe untuk piano baru setiap hari', 'Subscribe'],
    },
    'hujan': {
        'hooks': ['Suara hujan yang menenangkan', 'Dengarkan hujan turun pelan-pelan',
                  'Hujan rintik untuk tidur nyenyak', 'Suara hujan untuk relaksasi'],
        'scripts': ['Suara hujan ini membantu pikiranmu rileks.',
                    'Bayangkan hujan turun di luar jendela…',
                    'Rintik hujan untuk tidur yang lebih nyenyak.',
                    'Biarkan suara hujan menenangkan harimu.'],
        'ctas': ['Subscribe untuk suara hujan setiap hari', 'Subscribe'],
    },
    'meditasi': {
        'hooks': ['Tarik napas… tenangkan pikiran', 'Meditasi sejenak untuk harimu',
                  'Hening sejenak, dengarkan dirimu', 'Waktu untuk bermeditasi'],
        'scripts': ['Tarik napas dalam… dan hembuskan perlahan.',
                    'Fokus pada napasmu, lepaskan semua beban.',
                    'Satu menit tenang untuk memulai harimu.',
                    'Biarkan pikiranmu mengalir seperti air.'],
        'ctas': ['Subscribe untuk meditasi harian', 'Subscribe'],
    },
    'fokus': {
        'hooks': ['Fokus penuh untuk kerjamu', 'Musik fokus untuk produktivitas',
                  'Konsentrasi penuh, tanpa distraksi', 'Musik untuk fokus belajar dan kerja'],
        'scripts': ['Musik ini membantumu fokus tanpa distraksi.',
                    'Pasang headphone dan mulai kerjamu.',
                    'Fokus sebentar, istirahat sejenak, lalu lanjut lagi.',
                    'Biarkan musik ini menemani jam fokusmu.'],
        'ctas': ['Subscribe untuk musik fokus harian', 'Subscribe'],
    },
    'anak': {
        'hooks': ['Lagu ceria untuk anak-anak!', 'Ayo bernyanyi bersama!',
                  'Belajar sambil bernyanyi, seru!', 'Lagu anak yang bikin semangat'],
        'scripts': ['Ayo bernyanyi bersama, tepuk tangan yuk!',
                    'Lagu ceria ini untuk menemani harimu yang seru.',
                    'Belajar berhitung sambil bernyanyi, menyenangkan!',
                    'Ikuti gerakannya, lompat dan bernyanyi bersama!'],
        'ctas': ['Subscribe ya!', 'Ajak temanmu subscribe!'],
    },
    'asmr': {
        'hooks': ['Bisikan lembut untuk tidur nyenyak', 'Suara ketukan halus yang menenangkan',
                  'ASMR lembut untuk relaksasi malam ini', 'Dengarkan suara tenang ini… rileks'],
        'scripts': ['Bisikan lembut ini menemani tidurmu malam ini.',
                    'Dengarkan ketukan halus ini… tarik napas perlahan.',
                    'Suara lembut untuk menenangkan pikiran dan tubuhmu.',
                    'Pasang headphone-mu dan biarkan suara ini membawamu tidur.'],
        'ctas': ['Subscribe untuk ASMR baru setiap malam', 'Subscribe'],
    },
}
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

def track_daily_upload(yt_id, project_id=None):
    """Catat 1 upload sukses untuk channel hari ini (WIB).
    Kembalikan (jumlah_hari_ini, sudah_kena_batas).
    🔥 v1.2.25: bila project_id (project yang BENAR-BENAR dipakai —
    bisa project cadangan setelah auto-switch) diberikan, hitungan
    per-project dicatat juga di channel['daily_uploads_by_project']
    supaya kuota per project tetap benar setelah switch."""
    today = _wib_now().strftime("%Y-%m-%d")
    count = 0
    with db_lock:
        for c in database_channel:
            if c.get('yt_id') == yt_id:
                du = c.get('daily_uploads') or {}
                count = int(du.get('count', 0)) + 1 if du.get('date') == today else 1
                c['daily_uploads'] = {"date": today, "count": count}
                if project_id:
                    byp = c.get('daily_uploads_by_project') or {}
                    ent = byp.get(str(project_id)) or {}
                    pc = int(ent.get('count', 0)) + 1 if ent.get('date') == today else 1
                    byp[str(project_id)] = {"date": today, "count": pc}
                    c['daily_uploads_by_project'] = byp
                break
        try: save_channels(database_channel)
        except Exception: pass
    return count, count >= MAX_UPLOADS_PER_CHANNEL_PER_DAY

# 🔥 FASE 6: KUOTA PER PROJECT GOOGLE + AUTOPILOT
# Kuota YouTube dihitung PER PROJECT Google (100 upload/hari — kuota
# "Video Uploads per day" di Cloud Console, dicek live 2026-11-19),
# bukan per channel. Project diidentifikasi dari prefix client_id kredensial utama
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
    """Total upload hari ini (WIB) dari semua channel di project ini.
    🔥 v1.2.25: hitung dari daily_uploads_by_project (project yang
    benar-benar dipakai, termasuk setelah auto-switch). Channel lama
    tanpa catatan per-project untuk project ini dihitung dari counter
    legacy dikurangi upload yang tercatat di project lain hari ini,
    supaya tidak dobel."""
    if not project_id:
        return 0
    today = _wib_now().strftime("%Y-%m-%d")
    total = 0
    with db_lock:
        for c in database_channel:
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

def get_project_quotas():
    """Ringkasan kuota per project Google untuk panel: project_id, daftar
    channel, upload hari ini, batas (100), dan sisa. Channel tanpa kredensial
    dikelompokkan sebagai 'tidak-diketahui' (kuota tidak terlacak)."""
    today = _wib_now().strftime("%Y-%m-%d")
    projects = {}
    with db_lock:
        for c in database_channel:
            pid = _project_id_of_channel(c) or 'tidak-diketahui'
            entry = projects.setdefault(pid, {"project_id": pid, "channels": [], "uploads_today": 0})
            entry["channels"].append({"yt_id": c.get('yt_id'), "name": c.get('name', c.get('yt_id', ''))})
    for _e in _oauth_client_entries():
        _pid = str(_e.get('project_id') or '')
        if _pid and _pid in projects:
            projects[_pid]["client_name"] = _e.get('name') or _pid
    # 🔥 v1.2.25: hitungan per project lewat _project_uploads_today
    # (memakai daily_uploads_by_project) supaya upload via project
    # cadangan setelah auto-switch dihitung di project yang benar.
    for _pid, _entry in projects.items():
        if _pid != 'tidak-diketahui':
            _entry["uploads_today"] = _project_uploads_today(_pid)
    out = []
    # 🔥 v1.2.25: tampilkan juga project cadangan di registry yang belum
    # punya channel (0 upload) supaya cadangan terlihat di panel/Telegram.
    for e in _oauth_client_entries():
        pid = str(e.get('project_id') or '')
        if pid and pid not in projects:
            projects[pid] = {"project_id": pid, "channels": [],
                             "uploads_today": _project_uploads_today(pid),
                             "client_name": e.get('name') or pid,
                             "backup": True}
    for pid in sorted(projects):
        e = projects[pid]
        e["limit"] = MAX_UPLOADS_PER_CHANNEL_PER_DAY
        e["remaining"] = max(0, MAX_UPLOADS_PER_CHANNEL_PER_DAY - e["uploads_today"])
        out.append(e)
    return out

def _heal_stale_quota_cooldowns():
    """🔥 v1.2.20: bersihkan cooldown kuota yang sudah tidak berlaku.
    Cooldown kuota (sampai tengah malam) hanya sah bila kuota project
    memang habis. Aturan lama (batas 6/hari) memasang cooldown padahal
    kuota Google masih longgar (100/hari, dicek live 2026-11-19) —
    cooldown seperti itu harus dilepas saat startup supaya task yang
    tertahan (mis. 2 task 2026-11-19, "Cooldown YT 649 mnt") bisa jalan
    lagi setelah deploy, bukan menunggu tengah malam. Channel tanpa
    project yang bisa dihitung dibiarkan apa adanya. Tidak pernah raise."""
    try:
        now = time.time()
        cleared = []
        for c in list(database_channel):
            yt_id = c.get('yt_id')
            cd = c.get('cooldown_until') or 0
            if not yt_id or not cd or cd <= now:
                continue
            pid = _project_id_of_channel(c)
            if not pid:
                continue
            if _project_uploads_today(pid) < MAX_UPLOADS_PER_CHANNEL_PER_DAY:
                try:
                    clear_channel_cooldown(yt_id)
                except Exception:
                    continue
                cleared.append(c.get('name') or yt_id)
        if cleared:
            print(f"🔥 v1.2.20: cooldown kuota lama dibersihkan untuk: {', '.join(cleared)}", flush=True)
    except Exception as e:
        print(f"Gagal heal cooldown kuota lama: {e}", flush=True)


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


# 🔥 v1.2.20: jalankan saat startup (setelah load_cooldowns di atas) —
# cooldown kuota dari aturan lama (batas 6/hari) dilepas bila kuota
# project hari ini masih longgar (batas baru 100/hari).
_heal_stale_quota_cooldowns()

# 🔥 v1.2.26: terapkan retensi baru (maks 3 backup) sekali saat startup —
# daftar backup langsung terpangkas ke 3 tanpa menunggu backup
# berikutnya. Gagal prune tidak boleh menggagalkan startup.
try:
    import backup_core as _backup_core_startup
    _backup_core_startup.prune_backups()
except Exception:
    pass


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
                _activity_task_finished(t, final_status)  # 🔥 v1.3.71: catat ke log aktivitas
                break
    save_tasks_db()
    # Auto-cleanup: buang entri riwayat >90 hari setiap ada task baru masuk.
    try:
        _prune_old_history()
    except Exception:
        pass

def get_fresh_credentials(channel_data):
    creds_str = channel_data.get('creds_list', [channel_data.get('creds_json')])[0]
    creds = Credentials.from_authorized_user_info(json.loads(creds_str))
    if creds.expired and creds.refresh_token:
        creds.refresh(Request())
    return creds

# ==========================================
# 🚨 SATPAM API KEY (AUTO-CHECKER)
# ==========================================
def _keys_label(n):
    """Label jumlah key untuk status channel — tanpa kurung, singular
    bila 1: '1 Key' / '2 Keys' (v1.3.57, permintaan user)."""
    n = int(n or 0)
    return f"{n} Key" if n == 1 else f"{n} Keys"


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
                    c['status'] = f"Connected 🟢 {_keys_label(len(live_keys))}"
                    changed = True
                new_notifs.append({"msg": f"🧹 {dead_count} API Key mati untuk Channel '{c.get('name','Unknown')}' dibersihkan otomatis.",
                                   "time": datetime.now().strftime("%Y-%m-%d %H:%M")})
            elif dead_count and not live_keys:
                # 🔥 v1.3.26: semua key mati -> status jadi Disconnected (pill
                # merah di panel); kembali Connected saat taut ulang (8865).
                with db_lock:
                    c['status'] = f"Disconnected 🔴 {_keys_label(dead_count)}"
                    changed = True
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
    # 🔒 #5: yt_id TIDAK PERNAH dipercaya mentah — allowlist ketat +
    # pastikan hasil akhir tetap di bawah BASE_UPLOAD (commonpath).
    # sub juga harus salah satu folder galeri yang dikenal.
    if not _valid_yt_id(yt_id):
        raise ValueError("yt_id tidak valid")
    if sub not in _GALLERY_SUBS:
        raise ValueError("sub-folder galeri tidak dikenal")
    path = _safe_join_under(BASE_UPLOAD, yt_id, sub)
    os.makedirs(path, exist_ok=True)
    return path

def get_multi_backgrounds(yt_id, count=1):
    path = get_channel_folder(yt_id, "backgrounds")
    files = [os.path.join(path, f) for f in os.listdir(path) if f.lower().endswith(('.mp4', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.mov', '.webm'))]
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
                # 🔒 #13: dukungan logo REMOTE (logo_url dari preset) DIHAPUS —
                # urlopen tanpa timeout/batas ukuran/validasi host = blind
                # SSRF + DoS (thread render menahan resource_gate). Logo
                # selalu dari static/logo.png (lokal). Preset lama yang
                # punya logo_url: nilainya diabaikan (lihat save_preset).
                img_data = None
                if os.path.exists('static/logo.png'):
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

def _run_ffmpeg_zoom(task_id, cmd, err_tag, total_sec=None, pct_base=0.0, pct_span=100.0):
    """🔥 BARU v1.5: jalankan ffmpeg untuk mode Zoom — dukung force-stop + ping watchdog.
    stderr ditulis ke file log sementara agar pipe tidak penuh (deadlock) saat encode panjang.
    🔥 v1.2.18: total_sec = durasi target (detik) → persen render dihitung dari
    time= terakhir di log FFmpeg (pengukuran, bukan perkiraan); pct_base/pct_span
    memetakan panggilan ini ke slice persen (mis. segmen ke-i dari N). total_sec=None
    = tanpa pelaporan progres."""
    log_path = os.path.join(BASE_UPLOAD, f"zoomlog_{task_id}.txt")
    log_f = open(log_path, 'w', encoding='utf-8', errors='ignore')
    proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=log_f)
    active_processes[task_id] = proc
    last_ping = time.time()
    last_prog = time.time()
    # 🔒 #16: batas waktu 3600 dtk per panggilan ffmpeg Zoom (satu segmen /
    # gabung durasinya paling lama = durasi video, maks ~80 menit).
    _zoom_deadline = time.time() + 3600
    try:
        while proc.poll() is None:
            if stop_flags.get(task_id):
                proc.terminate()
                raise Exception("Dibatalkan")
            if time.time() > _zoom_deadline:
                proc.terminate()
                raise Exception(f"{err_tag}: timeout (3600 dtk)")
            now_t = time.time()
            if now_t - last_ping > 60:
                last_ping = now_t
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id:
                            d['updated_at'] = now_t
            # 🔥 v1.2.18: progres render — baca time= terakhir dari log FFmpeg.
            if total_sec and float(total_sec) > 0 and (now_t - last_prog) > 5:
                last_prog = now_t
                try:
                    with open(log_path, 'rb') as _lf:
                        _lf.seek(0, 2)
                        _sz = _lf.tell()
                        _lf.seek(max(0, _sz - 8192))
                        _tail = _lf.read().decode('utf-8', errors='ignore')
                    _el = _ffmpeg_time_to_sec(_tail)
                    if _el is not None:
                        _task_set_progress(task_id, pct_base + pct_span * _el / float(total_sec), 'render')
                except Exception:
                    pass
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
    # 🔥 v1.2.18: tutup progres segmen ini di ujung slice-nya.
    if total_sec and float(total_sec) > 0:
        _task_set_progress(task_id, pct_base + pct_span, 'render', force=True)
    try: os.remove(log_path)
    except Exception: pass

def render_zoom_core(task_id, audio_path, bg_paths, output_path, duration, cfg, render_preset='ultrafast', progress_base=0.0, progress_span=100.0):
    """🔥 BARU v1.5: mode Zoom Saja — background + zoom pelan, 100% FFmpeg (tanpa render
    frame Python, tanpa file overlay, tanpa floating card). Setara overlay dari sisi kecepatan.
    Durasi dibagi rata ke semua background. 🔥 v1.6: zoom BOLAK-BALIK halus (siklus 60 detik:
    30 dtk zoom-in ke 1+zoom_pct%, 30 dtk zoom-out kembali) agar terasa hidup.
    🔥 v1.2.18: progress_base/progress_span memetakan render ini ke slice persen task."""
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
            # 🔥 v1.2.18: segmen ke-i memetakan ke slice persennya sendiri.
            _run_ffmpeg_zoom(task_id, cmd, "FFmpeg Zoom Error", total_sec=seg_dur,
                             pct_base=progress_base + progress_span * i / max(1, len(bg_paths)),
                             pct_span=progress_span / max(1, len(bg_paths)))

        # Gabung segmen + tempel audio (video copy; audio AAC default — sama seperti jalur lain)
        _write_concat_list(join_txt, seg_files)  # 🔒 #20: escape ' tunggal
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

def render_video_core(task_id, audio_path, bg_paths, output_path, duration, cfg, render_preset='ultrafast', progress_base=0.0, progress_span=100.0):
    # 🔥 BARU v1.5: mode Zoom Saja → render 100% FFmpeg (paling ringan, setara overlay)
    if cfg.get('zoom_mode'):
        render_zoom_core(task_id, audio_path, bg_paths, output_path, duration, cfg, render_preset,
                         progress_base=progress_base, progress_span=progress_span)
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

    # 🔒 #16: batas waktu render frame-by-frame 6 jam — jauh di atas durasi
    # render wajar (video target 60-80 menit), tapi mencegah proses
    # menggantung selamanya kalau ffmpeg macet.
    _render_deadline = time.time() + 6 * 3600
    try:
        last_ping = time.time()
        last_prog = time.time()
        for f in range(total_f):
            if stop_flags.get(task_id):
                raise Exception("Dibatalkan")
            if time.time() > _render_deadline:
                raise Exception("Render timeout (6 jam)")

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
            # 🔥 v1.2.18: progres render klasik — frame ke-f dari total_f (pengukuran).
            if now_t - last_prog > 2:
                last_prog = now_t
                _task_set_progress(task_id, progress_base + progress_span * f / max(1, total_f), 'render')

    except Exception as e:
        proc.stdin.close()
        proc.terminate()
        bg.close()
        raise e

    proc.stdin.close()
    try:
        proc.wait(timeout=900)  # 🔒 #16: ffmpeg harus selesai 15 menit setelah stdin ditutup
    except subprocess.TimeoutExpired:
        proc.terminate()
        raise Exception("FFmpeg tidak selesai setelah stdin ditutup (timeout 900 dtk)")
    # 🔥 v1.2.18: tutup progres render ini di ujung slice-nya.
    _task_set_progress(task_id, progress_base + progress_span, 'render', force=True)
    bg.close()
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
        subprocess.run(cmd, check=True, capture_output=True, text=True, timeout=3600)
    except subprocess.TimeoutExpired:
        raise Exception("FFmpeg Overlay Error: timeout (3600 dtk)")
    except subprocess.CalledProcessError as e:
        err_lines = (e.stderr or '').strip().split('\n')
        real_err = " | ".join(err_lines[-6:])
        raise Exception(f"FFmpeg Overlay Error: {real_err}")
    os.replace(tmp_out, video_path)

# ==========================================
# 🚀 BACKGROUND WORKER (SUDAH DI-TUNE UP NGEBUT)
# ==========================================
# ============================================================
# 🔥 FASE 8: SHORTS MUSE — render vertikal 1080x1920 dari naskah + CTA
# ============================================================
def _shorts_muse_ass_escape(text):
    """Escape teks untuk file ASS (libass): backslash, kurung kurawal,
    dan baris baru (backslash-N). Tanpa ini, teks berisi '{' atau koma bisa
    merusak parsing subtitle."""
    s = str(text or '').replace('\\', '\\\\').replace('{', '\\{').replace('}', '\\}')
    return s.replace('\r\n', '\\N').replace('\n', '\\N').replace('\r', '\\N')

def _shorts_muse_ass_time(sec):
    """Detik float → timestamp ASS H:MM:SS.cc."""
    sec = max(0.0, float(sec))
    h = int(sec // 3600); m = int((sec % 3600) // 60)
    s = sec % 60
    return f"{h}:{m:02d}:{s:05.2f}"

def _hex_to_ass_color(hex_color):
    """'#RRGGBB' → warna ASS '&HAABBGGRR' (alpha 00 = penuh). Tidak valid
    → putih. Dipakai untuk warna teks pilihan user di form."""
    try:
        h = str(hex_color or '').strip().lstrip('#')
        if len(h) != 6:
            return '&H00FFFFFF'
        r, g, b = h[0:2], h[2:4], h[4:6]
        int(r, 16); int(g, 16); int(b, 16)
        return f"&H00{b}{g}{r}"
    except Exception:
        return '&H00FFFFFF'

def _shorts_muse_split_script(script, max_words=10):
    """Pecah naskah jadi potongan ≤ max_words kata (satu potongan = satu
    segmen tampil di layar). Maks 6 potongan supaya tidak terlalu ramai."""
    words = ' '.join(str(script or '').split()).split()
    if not words:
        return []
    chunks = [' '.join(words[i:i + max_words])
              for i in range(0, len(words), max_words)]
    return chunks[:6]

def _shorts_muse_build_ass(ass_path, hook, script, cta_text, text_color, duration_sec):
    """Tulis file ASS: Hook (awal, kuning, atas), Naskah (tengah, warna
    pilihan, bawah), CTA (5 detik terakhir, kartu tengah layar dengan
    kotak latar). Mengikuti pola filter `subtitles` Clipper (fontsdir
    static/fonts, Fontname Arial → fontconfig fallback)."""
    duration_sec = float(duration_sec)
    hook_dur = min(SHORTS_MUSE_HOOK_SEC, max(1.0, duration_sec * 0.3))
    cta_start = max(hook_dur + 0.5, duration_sec - SHORTS_MUSE_CTA_SEC)
    body_color = _hex_to_ass_color(text_color)
    events = []
    if (hook or '').strip():
        events.append((0.0, hook_dur, 'Hook', _shorts_muse_ass_escape(hook.strip())))
    chunks = _shorts_muse_split_script(script)
    if chunks:
        body_start, body_end = hook_dur, cta_start
        span = max(0.5, (body_end - body_start) / len(chunks))
        for i, ch in enumerate(chunks):
            events.append((body_start + i * span,
                           min(body_start + (i + 1) * span, body_end),
                           'Body', _shorts_muse_ass_escape(ch)))
    if (cta_text or '').strip():
        events.append((cta_start, duration_sec, 'CTA', _shorts_muse_ass_escape(cta_text.strip())))
    ass = (
        "[Script Info]\nScriptType: v4.00+\nPlayResX: 1080\nPlayResY: 1920\n"
        "WrapStyle: 0\nScaledBorderAndShadow: yes\n\n"
        "[V4+ Styles]\n"
        "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, "
        "OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
        "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, "
        "Alignment, MarginL, MarginR, MarginV, Encoding\n"
        f"Style: Hook,Arial,64,&H0000FFFF,&H0000FFFF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,3,0,8,80,80,280,1\n"
        f"Style: Body,Arial,46,{body_color},{body_color},&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,3,0,2,90,90,420,1\n"
        "Style: CTA,Arial,72,&H00FFFF00,&H00FFFF00,&H00000000,&H80000000,1,0,0,0,100,100,0,0,3,2,0,5,80,80,0,1\n\n"
        "[Events]\n"
        "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text\n"
    )
    for start, end, style, text in events:
        ass += (f"Dialogue: 0,{_shorts_muse_ass_time(start)},"
                f"{_shorts_muse_ass_time(end)},{style},,0,0,0,,{text}\n")
    with open(ass_path, 'w', encoding='utf-8') as f:
        f.write(ass)
    return len(events)

# Warna gradien otomatis per tema (hex RRGGBB, atas→tengah→bawah).
# Dipakai bila galeri background channel masih kosong — Shorts tidak boleh
# mentok hanya karena belum ada gambar. Dibuat sekali per tema, dipakai ulang.
_SHORTS_AUTO_BG_COLORS = {
    'anak': ('FFD93D', 'FF6B9D', '4D96FF'),      # kuning cerah → pink → biru
    'sleep': ('0B1E3A', '123A6B', '1E5A8A'),     # biru gelap
    'lofi': ('2D1B4E', '6B3FA0', '9D6BC4'),      # ungu
    'piano': ('3A2410', '8A5A1E', 'D9A441'),     # amber hangat
    'hujan': ('2B3A4A', '4A6B8A', '7A9AB5'),     # biru-abu
    'meditasi': ('0F2E1D', '1E6B3A', '4CAF6D'),  # hijau
    'fokus': ('0B2E2E', '0F5A5A', '14A0A0'),     # teal
    'asmr': ('1A1025', '4A2A6B', '8A5FBF'),      # ungu gelap lembut
}
_SHORTS_AUTO_BG_DEFAULT_THEME = 'sleep'

def _shorts_muse_norm_theme(theme):
    """Normalisasi tema/niche ke kunci peta gradien ('tidur' → 'sleep';
    tidak dikenal → default 'sleep'/biru gelap)."""
    t = (str(theme or '')).strip().lower()
    if t in ('tidur', 'sleep'):
        return 'sleep'
    if t in _SHORTS_AUTO_BG_COLORS:
        return t
    return _SHORTS_AUTO_BG_DEFAULT_THEME

def _shorts_muse_ensure_auto_bg(yt_id, theme):
    """Background gradien otomatis 1080x1920 (FFmpeg lavfi `gradients`,
    tanpa unduh dari internet) untuk Shorts Muse bila galeri channel
    kosong. Disimpan sebagai auto-gradient-<tema>.png di folder
    backgrounds channel — aset biasa, dipakai ulang Short berikutnya,
    tidak dibuat ulang bila sudah ada. Mengembalikan (path, baru_dibuat)
    atau (None, False) bila gagal."""
    theme = _shorts_muse_norm_theme(theme)
    try:
        folder = get_channel_folder(yt_id, "backgrounds")
    except Exception:
        return None, False
    fname = f"auto-gradient-{theme}.png"
    path = os.path.join(folder, fname)
    try:
        if os.path.isfile(path) and os.path.getsize(path) > 1024:
            return path, False
    except Exception:
        pass
    c0, c1, c2 = _SHORTS_AUTO_BG_COLORS[theme]
    lavfi = ("gradients=s=1080x1920:nb_colors=3:c0=0x%s:c1=0x%s:c2=0x%s:"
             "x0=540:y0=0:x1=540:y1=1920" % (c0, c1, c2))
    # Coba 2x: panggilan pertama bisa ikut terbunuh (SIGKILL/OOM) bila
    # berjalan persis saat service baru start & memori sedang tinggi —
    # percobaan kedua sesaat kemudian biasanya berhasil.
    for _attempt in (1, 2):
        try:
            r = subprocess.run(['ffmpeg', '-y', '-v', 'error', '-f', 'lavfi',
                                '-i', lavfi, '-frames:v', '1', path],
                               capture_output=True, text=True, timeout=60)
        except Exception as e:
            print(f"[auto-bg] ffmpeg gagal start tema {theme} (percobaan {_attempt}): {e}", flush=True)
            if _attempt == 1:
                time.sleep(2)
            continue
        if r.returncode == 0:
            break
        print(f"[auto-bg] ffmpeg gagal tema {theme} (percobaan {_attempt}, exit {r.returncode}): {(r.stderr or '').strip()[:200]}", flush=True)
        try:
            if os.path.isfile(path):
                os.remove(path)
        except Exception:
            pass
        if _attempt == 1:
            time.sleep(2)
    else:
        return None, False
    try:
        if not os.path.isfile(path) or os.path.getsize(path) <= 1024:
            return None, False
        with open(path, 'rb') as f:
            if f.read(8) != b'\x89PNG\r\n\x1a\n':
                return None, False
        return path, True
    except Exception as e:
        print(f"[auto-bg] gagal buat gradien tema {theme}: {e}", flush=True)
        return None, False

# Ekstensi klip video yang bisa jadi background Shorts Muse.
_SHORTS_VIDEO_BG_EXTS = ('.mp4', '.mov', '.webm')

def _shorts_muse_list_bg_videos(yt_id):
    """Nama file klip video di folder backgrounds channel, URUT NAMA FILE
    (deterministik — ini urutan sambungnya saat render). Hanya mp4/mov/webm."""
    try:
        folder = get_channel_folder(yt_id, "backgrounds")
    except Exception:
        return []
    try:
        return sorted(f for f in os.listdir(folder)
                      if f.lower().endswith(_SHORTS_VIDEO_BG_EXTS)
                      and os.path.isfile(os.path.join(folder, f)))
    except Exception:
        return []

def _shorts_muse_build_bg_concat(clip_paths, out_path):
    """Sambung klip-klip video jadi SATU file background 1080x1920/30fps:
    tiap klip di-scale+crop ke 1080x1920, fps diseragamkan 30, tanpa audio.
    Dipakai bila Short memakai ≥2 klip video (hasilnya di-loop + dipotong
    ke durasi target oleh render utama). Mengembalikan True bila berhasil;
    False bila FFmpeg gagal (pemanggil memakai klip pertama saja sebagai
    fallback)."""
    try:
        n = len(clip_paths)
        cmd = [get_ffmpeg_path(), '-y']
        for p in clip_paths:
            cmd += ['-i', p]
        chains = []
        for i in range(n):
            chains.append(f"[{i}:v]scale=1080:1920:force_original_aspect_ratio=increase,"
                          f"crop=1080:1920,fps=30,format=yuv420p,setsar=1[v{i}]")
        chains.append("".join(f"[v{i}]" for i in range(n)) + f"concat=n={n}:v=1:a=0[out]")
        cmd += ['-filter_complex', ";".join(chains), '-map', '[out]',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
                '-an', '-movflags', '+faststart', out_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
        if r.returncode != 0:
            print(f"[shorts-video-bg] concat {n} klip gagal (exit {r.returncode}): {(r.stderr or '')[-300:]}", flush=True)
            return False
        if not os.path.isfile(out_path) or os.path.getsize(out_path) <= 0:
            return False
        return True
    except Exception as e:
        print(f"[shorts-video-bg] concat gagal: {e}", flush=True)
        return False

def _shorts_muse_pick_bg(yt_id, filename, theme=None):
    """Background Shorts: file pilihan form (uploads/<yt_id>/backgrounds)
    kalau ada & valid; kalau tidak, 1 background acak dari galeri.
    Bila galeri kosong → gradien otomatis per tema (dibuat sekali,
    dipakai ulang). Mengembalikan path absolut atau None."""
    folder = get_channel_folder(yt_id, "backgrounds")
    if filename:
        cand = os.path.join(folder, os.path.basename(str(filename)))
        if os.path.isfile(cand) and cand.lower().endswith(('.jpg', '.jpeg', '.png', '.webp', '.mp4', '.mov', '.webm')):
            return cand
    picked = get_multi_backgrounds(yt_id, count=1)
    if picked:
        return picked[0]
    auto_path, _created = _shorts_muse_ensure_auto_bg(yt_id, theme)
    return auto_path

def _shorts_muse_pick_bgm(yt_id, filename):
    """Musik latar Shorts: file pilihan form (uploads/<yt_id>/audios).
    'none'/kosong/tidak ditemukan → None (video tetap dibuat, dengan
    trek audio hening — QC Fase 5 dilewati untuk kasus ini)."""
    if not filename or str(filename).strip().lower() == 'none':
        return None
    folder = get_channel_folder(yt_id, "audios")
    cand = os.path.join(folder, os.path.basename(str(filename)))
    if os.path.isfile(cand) and cand.lower().endswith(('.mp3', '.wav')):
        return cand
    return None

# ==========================================
# 🔊 AUDIO LENGKAP SHORTS MUSE — voiceover (TTS) + FX + musik latar
# (v1.2.6) Shorts yang tayang selama ini BISU: galeri musik kosong,
# audio klip dibuang saat render, naskah hanya teks di layar. Sejak
# versi ini setiap Short SELALU bersuara: voiceover membacakan
# Hook+Naskah+CTA, FX singkat di transisi, musik latar di bawah suara.
# Aturan mixing (permintaan user 01:24 WIB): voiceover PALING DOMINAN
# — BGM & FX tidak boleh menutupi suara (ducking sidechain + level
# statis rendah). Bila TTS gagal total, render DIGAGALKAN dengan pesan
# jelas — tidak pernah lagi Short bisu lolos diam-diam.
# ==========================================
_SHORTS_TTS_VOICES = {
    'anak': 'id-ID-GadisNeural',        # perempuan, ceria — untuk anak-anak
    'sleep': 'id-ID-DamayantiNeural',   # perempuan, tenang
    'meditasi': 'id-ID-DamayantiNeural',
    'asmr': 'id-ID-DamayantiNeural',    # lembut
    'default': 'id-ID-ArdiNeural',      # laki-laki netral
}
_SHORTS_TTS_TIMEOUT = 120              # detik per panggilan TTS
_SHORTS_TTS_MAX_RETRY = 1              # ulangi edge-tts 1x sebelum fallback


def _shorts_tts_voice_for_theme(theme):
    """Suara TTS per tema (edge-tts id-ID)."""
    t = _shorts_muse_norm_theme(theme)
    return _SHORTS_TTS_VOICES.get(t, _SHORTS_TTS_VOICES['default'])


def _shorts_audio_duration(path):
    """Durasi (detik) file audio, atau None bila tidak terbaca."""
    try:
        out = subprocess.run(
            [get_ffprobe_path(), '-v', 'error', '-show_entries',
             'format=duration', '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=30)
        return float((out.stdout or '').strip())
    except Exception:
        return None


def _shorts_audio_ok(path, min_bytes=1024, min_dur=0.3):
    """File audio layak: ada, cukup besar, durasi terbaca."""
    try:
        if not path or not os.path.isfile(path):
            return False
        if os.path.getsize(path) < min_bytes:
            return False
        d = _shorts_audio_duration(path)
        return d is not None and d >= min_dur
    except Exception:
        return False


def _shorts_tts_synthesize(text, voice, out_path):
    """Sintesis SATU potongan teks jadi file audio (mp3).
    1) edge-tts (utama, suara natural) — 2 percobaan;
    2) gTTS (fallback). Mengembalikan True bila file audio layak.
    False bila keduanya gagal (pemanggil menggagalkan task, bukan
    mengirim Short bisu)."""
    text = ' '.join(str(text or '').split())
    if not text:
        return False
    if len(text) > 1000:
        text = text[:1000]
    for _attempt in range(1 + _SHORTS_TTS_MAX_RETRY):
        try:
            if os.path.isfile(out_path):
                os.remove(out_path)
        except Exception:
            pass
        try:
            r = subprocess.run(
                [sys.executable, '-m', 'edge_tts', '--voice', voice,
                 '--text', text, '--write-media', out_path],
                capture_output=True, text=True, timeout=_SHORTS_TTS_TIMEOUT)
            if r.returncode == 0 and _shorts_audio_ok(out_path):
                return True
            print(f"[shorts-tts] edge-tts gagal (exit {r.returncode}): "
                  f"{(r.stderr or '')[-200:]}", flush=True)
        except Exception as e:
            print(f"[shorts-tts] edge-tts gagal: {e}", flush=True)
    try:
        if os.path.isfile(out_path):
            os.remove(out_path)
    except Exception:
        pass
    try:
        r = subprocess.run(
            [sys.executable, '-c',
             'import sys; from gtts import gTTS; '
             'gTTS(sys.argv[1], lang="id").save(sys.argv[2])',
             text, out_path],
            capture_output=True, text=True, timeout=_SHORTS_TTS_TIMEOUT)
        if r.returncode == 0 and _shorts_audio_ok(out_path):
            print("[shorts-tts] gTTS fallback berhasil", flush=True)
            return True
        print(f"[shorts-tts] gTTS gagal (exit {r.returncode}): "
              f"{(r.stderr or '')[-200:]}", flush=True)
    except Exception as e:
        print(f"[shorts-tts] gTTS gagal: {e}", flush=True)
    return False


def _shorts_tts_segments(hook, script, cta_text, duration_sec):
    """Segmen suara — timing PERSIS sama dengan teks di layar (ASS):
    Hook di 0 detik, tiap potongan naskah berurutan, CTA di 5 detik
    terakhir. Satu segmen = satu panggilan TTS."""
    duration_sec = float(duration_sec)
    hook_dur = min(SHORTS_MUSE_HOOK_SEC, max(1.0, duration_sec * 0.3))
    cta_start = max(hook_dur + 0.5, duration_sec - SHORTS_MUSE_CTA_SEC)
    segs = []
    if (hook or '').strip():
        segs.append({'start': 0.0, 'end': hook_dur,
                     'text': hook.strip(), 'kind': 'Hook'})
    chunks = _shorts_muse_split_script(script)
    if chunks:
        span = max(0.5, (cta_start - hook_dur) / len(chunks))
        for i, ch in enumerate(chunks):
            segs.append({'start': hook_dur + i * span,
                         'end': min(hook_dur + (i + 1) * span, cta_start),
                         'text': ch, 'kind': 'Body'})
    if (cta_text or '').strip():
        segs.append({'start': cta_start, 'end': duration_sec,
                     'text': cta_text.strip(), 'kind': 'CTA'})
    return segs


def _shorts_sfx_synthesize(kind, out_path):
    """Sintesis SATU efek suara (WAV 44.1k stereo) murni dengan FFmpeg —
    tanpa download: pop (Hook masuk), whoosh (ganti naskah), chime (CTA).
    Mengembalikan True bila file layak."""
    ff = get_ffmpeg_path()
    try:
        if kind == 'pop':
            cmd = [ff, '-y', '-f', 'lavfi', '-i',
                   'sine=frequency=880:duration=0.12',
                   '-af', 'afade=t=out:st=0.05:d=0.07,volume=0.5,aresample=44100',
                   '-ac', '2', '-c:a', 'pcm_s16le', out_path]
        elif kind == 'whoosh':
            cmd = [ff, '-y', '-f', 'lavfi', '-i',
                   'anoisesrc=duration=0.45:color=pink',
                   '-af', 'bandpass=f=1100:width_type=q:width=1,afade=t=in:st=0:d=0.15,'
                   'afade=t=out:st=0.28:d=0.17,volume=0.4,aresample=44100',
                   '-ac', '2', '-c:a', 'pcm_s16le', out_path]
        else:  # chime — dua nada (E6 lalu B6)
            cmd = [ff, '-y', '-f', 'lavfi', '-i',
                   'sine=frequency=1318:duration=0.5',
                   '-f', 'lavfi', '-i', 'sine=frequency=1976:duration=0.5',
                   '-filter_complex',
                   '[0:a]afade=t=out:st=0.3:d=0.2,volume=0.4[a];'
                   '[1:a]adelay=180:all=1,afade=t=out:st=0.48:d=0.2,volume=0.4[b];'
                   '[a][b]amix=inputs=2:normalize=0,aresample=44100',
                   '-ac', '2', '-c:a', 'pcm_s16le', out_path]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        # pop hanya 0,12 dtk — ambang durasi minimal diturunkan untuk SFX.
        return r.returncode == 0 and _shorts_audio_ok(out_path, min_bytes=500, min_dur=0.05)
    except Exception as e:
        print(f"[shorts-sfx] {kind} gagal: {e}", flush=True)
        return False


def _shorts_bgm_fallback_generate(theme, out_path, duration=60.0):
    """Generate musik latar bawaan (WAV 44.1k stereo) murni dengan numpy —
    tanpa download: pad akor + melodi pentatonik + bass lembut. Dipakai
    bila galeri musik channel kosong, supaya Short TIDAK PERNAH tanpa
    musik. Deterministik per tema (bisa di-cache & dipakai ulang)."""
    try:
        sr = 44100
        n = int(sr * float(duration))
        t = np.arange(n, dtype=np.float64) / sr
        theme = _shorts_muse_norm_theme(theme)
        if theme == 'anak':
            chords = [(60, False), (65, False), (67, False), (60, False)]
            scale = [72, 74, 76, 79, 81, 84]
            bpm = 100
        elif theme in ('sleep', 'meditasi', 'asmr'):
            chords = [(57, True), (53, False), (60, False), (55, False)]
            scale = [69, 72, 76, 79, 76, 72]
            bpm = 60
        else:
            chords = [(60, False), (57, True), (65, False), (67, False)]
            scale = [72, 76, 79, 81, 79, 76]
            bpm = 80

        def _freq(midi):
            return 440.0 * (2.0 ** ((midi - 69) / 12.0))

        mix = np.zeros(n, dtype=np.float64)
        chord_len = 5.0
        n_chords = int(math.ceil(duration / chord_len))
        for ci in range(n_chords):
            root, minor = chords[ci % len(chords)]
            c0 = int(ci * chord_len * sr)
            c1 = min(n, int((ci + 1) * chord_len * sr))
            if c0 >= n:
                break
            seg_t = np.arange(c1 - c0, dtype=np.float64) / sr
            third = 3 if minor else 4
            for interval, gain in ((0, 0.05), (third, 0.045),
                                   (7, 0.04), (12, 0.025)):
                f = _freq(root + interval)
                tone = gain * np.sin(2.0 * np.pi * f * seg_t)
                attack = np.minimum(1.0, seg_t / 0.8)
                release = np.minimum(1.0, (len(seg_t) / sr - seg_t) / 0.8)
                trem = 1.0 + 0.10 * np.sin(2.0 * np.pi * 0.5 * seg_t)
                mix[c0:c1] += tone * attack * release * trem
            # Bass lembut di awal akor
            bass = 0.11 * np.sin(2.0 * np.pi * _freq(root - 12) * seg_t)
            bass *= np.exp(-seg_t / 1.2)
            mix[c0:c1] += bass
        # Melodi pentatonik (nada pendek, decay cepat) di grid 1/2 ketuk
        step = 30.0 / bpm
        pattern = [0, 2, 4, 2, 5, 4, 2, 1, 0, 2, 4, 5, 4, 2, 1, 0]
        pos = 0.0
        pi = 0
        while pos < duration:
            deg = pattern[pi % len(pattern)]
            pi += 1
            f = _freq(scale[deg])
            s0 = int(pos * sr)
            s1 = min(n, int((pos + 0.55) * sr))
            if s0 < n:
                nt = np.arange(s1 - s0, dtype=np.float64) / sr
                note = 0.16 * np.sin(2.0 * np.pi * f * nt) * np.exp(-nt / 0.22)
                mix[s0:s1] += note
            pos += step
        peak = float(np.max(np.abs(mix))) if n else 0.0
        if peak > 0:
            mix = mix / peak * 0.45
        pcm = np.clip(mix, -1.0, 1.0)
        pcm16 = (pcm * 32767).astype(np.int16)
        stereo = np.column_stack([pcm16, pcm16])
        with wave.open(out_path, 'wb') as wf:
            wf.setnchannels(2)
            wf.setsampwidth(2)
            wf.setframerate(sr)
            wf.writeframes(stereo.tobytes())
        return _shorts_audio_ok(out_path, min_bytes=10000, min_dur=30)
    except Exception as e:
        print(f"[shorts-bgm] generate musik bawaan gagal: {e}", flush=True)
        return False


def _shorts_bgm_fallback_path(theme):
    """Path musik bawaan per tema (di-cache di uploads, dibuat sekali).
    None bila generate gagal — pemanggil lanjut dengan voiceover saja."""
    theme = _shorts_muse_norm_theme(theme)
    p = os.path.join(BASE_UPLOAD, f"shorts_bgm_fallback_{theme}.wav")
    try:
        if (os.path.isfile(p) and os.path.getsize(p) > 10000
                and (_shorts_audio_duration(p) or 0) > 30):
            return p
        if _shorts_bgm_fallback_generate(theme, p):
            return p
    except Exception as e:
        print(f"[shorts-bgm] musik bawaan gagal: {e}", flush=True)
    return None


def _shorts_build_voice_track(task_id, hook, script, cta_text, duration_sec, theme):
    """Bangun SATU file audio suara (voiceover + FX) untuk Short:
    tiap segmen teks (Hook/naskah/CTA) disintesis TTS lalu diletakkan
    di detik yang sama dengan teks di layar; FX pop di Hook, whoosh di
    tiap ganti naskah, chime di CTA — semuanya pelan, di bawah suara.
    Mengembalikan path file WAV. Raises Exception bila TTS gagal total
    (task digagalkan dengan pesan jelas — tidak ada Short bisu)."""
    duration_sec = float(duration_sec)
    voice = _shorts_tts_voice_for_theme(theme)
    segments = _shorts_tts_segments(hook, script, cta_text, duration_sec)
    if not segments:
        raise Exception("tidak ada teks yang bisa dibacakan (Hook/Naskah/CTA kosong)")
    seg_files = []
    try:
        for idx, seg in enumerate(segments):
            out = os.path.join(BASE_UPLOAD, f"shorts_seg_{task_id}_{idx}.mp3")
            if not _shorts_tts_synthesize(seg['text'], voice, out):
                raise Exception("layanan TTS tidak bisa dihubungi "
                                "(edge-tts & gTTS gagal) — periksa koneksi "
                                "internet VPS lalu coba lagi")
            seg_files.append((out, seg))
        sfx = {}
        for kind in ('pop', 'whoosh', 'chime'):
            p = os.path.join(BASE_UPLOAD, f"shorts_sfx_{kind}.wav")
            if not (os.path.isfile(p) and os.path.getsize(p) > 1000):
                _shorts_sfx_synthesize(kind, p)
            if os.path.isfile(p) and os.path.getsize(p) > 1000:
                sfx[kind] = p
        inputs = []
        chains = []
        for i, (f, seg) in enumerate(seg_files):
            inputs += ['-i', f]
            slot = max(0.5, seg['end'] - seg['start'])
            dur = _shorts_audio_duration(f) or slot
            chain = f"[{i}:a]aresample=44100,pan=stereo|c0=c0|c1=c0"
            if dur > slot:
                tempo = min(1.6, dur / slot)
                chain += f",atempo={tempo:.2f}"
            chain += (f",atrim=0:{slot:.3f},"
                      f"adelay={int(seg['start'] * 1000)}:all=1,volume=1.0[s{i}]")
            chains.append(chain)
        sfx_hits = []
        if sfx.get('pop'):
            sfx_hits.append((sfx['pop'], 50))
        for seg in segments:
            if seg['kind'] == 'Body' and sfx.get('whoosh'):
                sfx_hits.append((sfx['whoosh'], int(seg['start'] * 1000)))
        cta_segs = [s for s in segments if s['kind'] == 'CTA']
        if cta_segs and sfx.get('chime'):
            sfx_hits.append((sfx['chime'], int(cta_segs[0]['start'] * 1000)))
        n_in = len(seg_files)
        for j, (p, tms) in enumerate(sfx_hits):
            inputs += ['-i', p]
            chains.append(f"[{n_in + j}:a]aresample=44100,"
                          f"pan=stereo|c0=c0|c1=c0,adelay={tms}:all=1,"
                          f"volume=0.30[x{j}]")
        n_total = len(seg_files) + len(sfx_hits)
        labels = ''.join(f"[s{i}]" for i in range(len(seg_files)))
        labels += ''.join(f"[x{j}]" for j in range(len(sfx_hits)))
        chains.append(f"{labels}amix=inputs={n_total}:normalize=0,apad,"
                      f"atrim=0:{duration_sec:.3f},aresample=44100[aout]")
        mix_path = os.path.join(BASE_UPLOAD, f"shorts_voice_{task_id}.wav")
        cmd = ([get_ffmpeg_path(), '-y'] + inputs +
               ['-filter_complex', ';'.join(chains), '-map', '[aout]',
                '-c:a', 'pcm_s16le', mix_path])
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if r.returncode != 0 or not _shorts_audio_ok(mix_path):
            raise Exception("gagal mencampur trek suara: "
                            f"{(r.stderr or '')[-200:]}")
        mean_db = _audio_mean_volume_db(mix_path)
        if mean_db is not None and (mean_db == float('-inf')
                                    or mean_db < QC_SILENCE_MAX_DB):
            raise Exception("trek suara hasil campuran hening — "
                            "voiceover tidak terdengar")
        return mix_path
    finally:
        for f, _s in seg_files:
            try:
                os.remove(f)
            except Exception:
                pass


def render_shorts_muse_core(task_id, task, yt_id):
    """🔥 FASE 8: render SATU Shorts Muse — vertikal 1080x1920, 100%
    FFmpeg (tanpa render frame Python). Background gambar/video dengan
    zoom pelan + teks Hook/Naskah/CTA dari file ASS + suara lengkap
    (voiceover TTS + FX + musik galeri/bawaan, voiceover paling dominan).
    Mengembalikan path final video
    (static/final_{task_id}.mp4) — sama seperti jalur Factory, supaya
    alur setelahnya (cek jeda → QC → upload → riwayat) dipakai ulang
    tanpa jalur paralel."""
    try:
        duration_sec = float(task.get('shorts_duration_sec') or 30)
    except Exception:
        duration_sec = 30.0
    duration_sec = max(SHORTS_MUSE_MIN_SEC, min(SHORTS_MUSE_MAX_SEC, duration_sec))
    hook = (task.get('shorts_hook') or '').strip()
    script = (task.get('shorts_script') or '').strip()
    cta_text = (task.get('shorts_cta') or SHORTS_MUSE_DEFAULT_CTA).strip() or SHORTS_MUSE_DEFAULT_CTA
    text_color = task.get('shorts_text_color') or '#FFFFFF'
    if not hook and not script:
        raise Exception("Shorts Muse butuh Hook atau Naskah — keduanya kosong!")

    bg_path = _shorts_muse_pick_bg(yt_id, task.get('shorts_bg'), task.get('shorts_theme'))
    if not bg_path:
        raise Exception("Gallery Background Kosong! Upload gambar background ke tab Asset Gallery dulu.")
    # 🎬 Background video multi-klip: ≥2 klip → sambung jadi satu file
    # 1080x1920 (di-loop + dipotong ke durasi target oleh render utama).
    # Concat gagal → fallback klip pertama saja (diloop seperti video tunggal).
    concat_path = None
    clips = [f for f in (task.get('shorts_bg_clips') or []) if isinstance(f, str) and f.strip()]
    if len(clips) >= 2:
        try:
            bg_folder = get_channel_folder(yt_id, "backgrounds")
        except Exception:
            bg_folder = None
        if bg_folder:
            clip_paths = []
            for f in clips:
                p = os.path.join(bg_folder, os.path.basename(f))
                if os.path.isfile(p) and p.lower().endswith(_SHORTS_VIDEO_BG_EXTS):
                    clip_paths.append(p)
            if len(clip_paths) >= 2:
                concat_path = os.path.join(BASE_UPLOAD, f"shorts_bgconcat_{task_id}.mp4")
                if _shorts_muse_build_bg_concat(clip_paths, concat_path):
                    bg_path = concat_path
                else:
                    try:
                        if os.path.isfile(concat_path):
                            os.remove(concat_path)
                    except Exception:
                        pass
                    concat_path = None
                    bg_path = clip_paths[0]
    bgm_path = _shorts_muse_pick_bgm(yt_id, task.get('shorts_bgm'))
    # 🔊 v1.2.6: Shorts SELALU bersuara — voiceover (TTS) wajib; BGM dari
    # galeri bila ada, kalau tidak pakai musik bawaan (fallback).
    # TTS gagal total → render digagalkan dengan pesan jelas (tidak ada
    # lagi Short bisu yang lolos diam-diam).
    voice_mix_path = None
    audio_parts = []
    voice_mix_path = _shorts_build_voice_track(
        task_id, hook, script, cta_text, duration_sec,
        task.get('shorts_theme'))
    audio_parts.append('voiceover')
    if bgm_path:
        audio_parts.append('musik galeri')
    else:
        bgm_path = _shorts_bgm_fallback_path(task.get('shorts_theme'))
        if bgm_path:
            audio_parts.append('musik bawaan')
    task['shorts_audio'] = ' + '.join(audio_parts) or 'voiceover'

    with db_lock:
        for d in active_tasks:
            if d['id'] == task_id:
                d['status'] = "Merender Shorts Muse 1080x1920... 📱"
                d['updated_at'] = time.time()
                d['started_at'] = time.time()
    save_tasks_db()

    ass_path = os.path.join(BASE_UPLOAD, f"shorts_ass_{task_id}.ass")
    output_path = os.path.join(BASE_DIR, f"static/final_{task_id}.mp4")
    try:
        n_events = _shorts_muse_build_ass(ass_path, hook, script, cta_text, text_color, duration_sec)
        if n_events == 0:
            raise Exception("Tidak ada teks yang bisa ditampilkan di Shorts (Hook/Naskah/CTA kosong)!")
        safe_ass = os.path.abspath(ass_path).replace('\\', '/').replace(':', '\\\\:')
        font_dir = os.path.join(BASE_DIR, 'static', 'fonts')
        safe_font_dir = font_dir.replace('\\', '/').replace(':', '\\\\:')
        # Zoom pelan bolak-balik selama durasi video (siklus = durasi penuh).
        cycle_frames = max(30, int(duration_sec * 30))
        vf = (f"scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
              f"zoompan=z='1+0.08*(0.5-0.5*cos(2*PI*on/{cycle_frames}))':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d=1:s=1080x1920:fps=30,"
              f"format=yuv420p,subtitles='{safe_ass}':fontsdir='{safe_font_dir}'")
        cmd = [get_ffmpeg_path(), '-y']
        is_image = bg_path.lower().endswith(('.jpg', '.jpeg', '.png', '.webp'))
        if is_image:
            cmd += ['-loop', '1', '-framerate', '30', '-i', bg_path]
        else:
            cmd += ['-stream_loop', '-1', '-i', bg_path]
        # 🔊 v1.2.6: [1:a] = voiceover+FX, [2:a] = musik latar — musik
        # di-ducking saat suara berbicara, lalu dicampur RENDAH di bawah
        # voiceover (aturan user: suara tidak boleh kalah oleh musik/FX).
        cmd += ['-i', voice_mix_path]
        cmd += ['-stream_loop', '-1', '-i', bgm_path]
        cmd += ['-filter_complex',
                f"[0:v]{vf}[v];"
                # 🔥 v1.3.67: samakan format kedua input audio SEBELUM
                # sidechaincompress — FFmpeg 4.4.2 di VPS gagal negosiasi
                # format bila input s16 ("Parsed_sidechaincompress ...
                # could not choose their formats", terverifikasi: rantai
                # lama gagal, rantai baru jalan di ffmpeg 4.4.2 VPS).
                # aformat fltp + stereo di kedua sisi; asplit karena
                # voice dipakai 2x (label output filter tidak bisa
                # dipakai ulang seperti label input).
                f"[1:a]aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo,asplit=2[voice][voice2];"
                f"[2:a]volume=0.16,aresample=44100,aformat=sample_fmts=fltp:channel_layouts=stereo[bgm];"
                f"[bgm][voice]sidechaincompress=threshold=0.03:ratio=6:"
                f"attack=200:release=500[bgmd];"
                f"[voice2][bgmd]amix=inputs=2:normalize=0[aout]",
                '-map', '[v]', '-map', '[aout]',
                '-c:v', 'libx264', '-preset', 'ultrafast', '-crf', '20',
                '-c:a', 'aac', '-b:a', '128k',
                '-movflags', '+faststart',
                '-t', f"{duration_sec:.3f}",
                '-threads', '0', output_path]
        # _run_ffmpeg_zoom dipakai ulang: dukung force-stop + ping watchdog.
        # 🔥 v1.2.18: progres render Shorts dari time= FFmpeg vs durasi target.
        _run_ffmpeg_zoom(task_id, cmd, "FFmpeg Shorts Muse Error", total_sec=duration_sec)
        if not os.path.exists(output_path) or os.path.getsize(output_path) <= 0:
            raise Exception("Hasil render Shorts Muse tidak ditemukan / 0 byte!")
        return output_path
    finally:
        try: os.remove(ass_path)
        except Exception: pass
        if voice_mix_path:
            try: os.remove(voice_mix_path)
            except Exception: pass
        if concat_path:
            try: os.remove(concat_path)
            except Exception: pass

_WORKER_RESTARTS = 0


def background_worker():
    """Supervisor (🔥 v1.3.33, audit C1): worker render SATU-SATUNYA —
    bila exception lolos dari fase scan (di luar try), thread mati dan
    pabrik berhenti selamanya tanpa jejak. Supervisor me-restart loop
    setelah jeda 5 detik supaya satu task rusak tidak membunuh semua."""
    global _WORKER_RESTARTS
    while True:
        try:
            _background_worker_loop()
        except Exception as e:
            _WORKER_RESTARTS += 1
            print(f"[worker] supervisor: loop mati ke-{_WORKER_RESTARTS} ({e}) — restart 5 detik", flush=True)
            if _WORKER_RESTARTS in (1, 10, 100):
                try:
                    notify_event("⚠️", "Worker render mati",
                                 f"di-restart otomatis (ke-{_WORKER_RESTARTS}): {e}")
                except Exception:
                    pass
            time.sleep(5)


def _background_worker_loop():
    global channel_cooldowns
    while True:
        task = None
        temp_cooldowns = []
        
        # 🔥 1. GALI ANTREAN: Cari task pertama yang AMAN dari cooldown
        while not render_queue.empty():
            ptask = queue_get()
            pyt_id = ptask['yt_id']

            # 🔥 FASE 9 MULTI-NODE: kalau mode multi-node aktif dan task ini
            # milik node lain, jangan dikerjakan di sini — kembalikan ke
            # antrean (wadah sementara, sama seperti cooldown). Di
            # single-node (default) guard ini tidak pernah aktif.
            if _multi_node_enabled():
                _owner = (ptask or {}).get('assigned_node')
                if _owner and _owner != _node_id():
                    temp_cooldowns.append(ptask)
                    continue

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

            # 🔥 FASE 6: kuota PROJECT Google sudah habis (100 upload/hari per
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
                queue_put(t, allow_overflow=True)
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
                    # 🔥 v1.2.18: reset progres (task bisa jadi hasil retry).
                    d['progress_pct'] = 0
                    d['progress_stage'] = 'render'
        save_tasks_db()
        _activity_log(f"🎬 Render mulai: {task.get('title') or '(tanpa judul)'} [{_activity_channel_name(yt_id)}]")  # 🔥 v1.3.71
        
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

            # 🔥 FASE 8: Shorts Muse — render vertikal 1080x1920 dari naskah +
            # CTA (bukan kompilasi panjang). channel_data/ch_name dipindah
            # ke sini karena dipakai juga setelah blok render (upload/notif).
            channel_data = next((c for c in database_channel if c['yt_id'] == yt_id), None)
            ch_name = channel_data['name'] if channel_data else "Ziqva Muse Bot FM"
            if (task.get('kind') == SHORTS_MUSE_KIND):
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = "Merender Shorts Muse... 📱"
                save_tasks_db()
                final_video = render_shorts_muse_core(task_id, task, yt_id)
            elif task.get('kind') == PLAYLIST_KIND:
                # 🎵 MUSIK PLAYLIST: render loop klip + audio referensi.
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = "Merender Musik Playlist... 🎵"
                save_tasks_db()
                final_video = _playlist_render(task_id, task, yt_id)
            elif task.get('kind') == ZUMO_KIND:
                # 🎵 ZUMO: gambar statis + spectrum tipis + salju/sakura ikut beat.
                with db_lock:
                    for d in active_tasks:
                        if d['id'] == task_id: d['status'] = "Merender Zumo Playlist... 🎵"
                save_tasks_db()
                final_video = _zumo_render(task_id, task, yt_id)
            else:
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
                        _write_concat_list(concat_txt, selected_videos)  # 🔒 #20
                        merged_video = os.path.join(BASE_UPLOAD, f"temp_mv_{task_id}.mp4")
                        try:
                            subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0',
                                            '-i', concat_txt, '-c', 'copy', merged_video],
                                           check=True, capture_output=True, timeout=3600)
                        except subprocess.TimeoutExpired:
                            raise Exception("Gagal menggabungkan video acak! (timeout 3600 dtk)")
                        except subprocess.CalledProcessError:
                            raise Exception("Gagal menggabungkan video acak! Pastikan semua video MP4 punya format/kodek sejenis (misal resolusi & fps sama).")
                        try: os.remove(concat_txt)
                        except: pass
                    else:
                        merged_video = source_video # 1 video: pakai langsung tanpa concat
                
                    # Ekstrak audio dari video gabungan ke temp_a.mp3 (untuk visualizer)
                    try:
                        subprocess.run([get_ffmpeg_path(), '-y', '-i', merged_video, '-vn', '-q:a', '0', base_audio], check=True, capture_output=True, timeout=1800)
                    except subprocess.TimeoutExpired:
                        raise Exception("Gagal ekstrak audio! (timeout 1800 dtk)")
                    except subprocess.CalledProcessError:
                        raise Exception(f"Gagal ekstrak audio! Video gabungan mungkin tidak bersuara.")

                    probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', base_audio], capture_output=True, text=True, check=True, timeout=120)
                    try: base_duration_sec = float(probe.stdout.strip())
                    except: raise Exception("Gagal membaca durasi audio dari video utama!")

                    # 🔥 BARU: track_schedule berisi SEMUA video acak (urutan concat),
                    # supaya Floating Card berganti judul mengikuti nama file tiap segmen.
                    # Judul dibersihkan dari nomor depan, misal '06. Langkah Sabar.mp4' → 'Langkah Sabar'
                    current_sec = 0.0
                    for vp in selected_videos:
                        probe_vp = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', vp], capture_output=True, text=True, timeout=120)
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
                            safe_path = os.path.abspath(ap).replace('\\', '/')
                            f.write(_concat_list_line(ap))  # 🔒 #20
                            probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', ap], capture_output=True, text=True, timeout=120)
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
                            probe_sp = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', _sp], capture_output=True, text=True, timeout=120)
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
                            _write_concat_list(sp_txt, sp_paths)  # 🔒 #20
                            special_audio_path = os.path.join(BASE_UPLOAD, f"temp_spaudio_{task_id}.mp3")
                            subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0', '-i', sp_txt, '-c:a', 'libmp3lame', '-ar', '44100', special_audio_path], check=True, timeout=1800)

                    subprocess.run([get_ffmpeg_path(), '-y', '-f', 'concat', '-safe', '0', '-i', c_txt, '-c:a', 'libmp3lame', '-ar', '44100', base_audio], check=True, timeout=1800)

                    probe = subprocess.run([get_ffprobe_path(), '-v', 'error', '-show_entries', 'format=duration', '-of', 'default=noprint_wrappers=1:nokey=1', base_audio], capture_output=True, text=True, check=True, timeout=120)
                    base_duration_sec = float(probe.stdout.strip())
                
                    bg_count = int(task.get('bg_count', 1))
                    bg_paths = get_multi_backgrounds(yt_id, count=bg_count)
                    if not bg_paths: raise Exception("Gallery Background Kosong!")

                if base_duration_sec <= 0: raise Exception("Durasi sumber media tidak valid!")

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

                # 🔥 v1.2.18: render utama memetakan 0-85% bila ada lagu khusus
                # (15% sisanya untuk render lagu khusus), 0-100% bila tidak ada.
                _main_span = 85.0 if (special_audio_path and special_duration_sec > 0) else 100.0
                render_video_core(task_id, base_audio, bg_paths, base_video, base_duration_sec, preset,
                                render_preset=task.get('render_preset', 'ultrafast'),
                                progress_base=0.0, progress_span=_main_span)
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
                                    render_preset=task.get('render_preset', 'ultrafast'),
                                    progress_base=85.0, progress_span=15.0)
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
                    _write_concat_list(loop_txt, [base_video] * loop_count)  # 🔒 #20

                    if stop_flags.get(task_id): raise Exception("Dibatalkan")

                    try:
                        # Kembali ke concat tapi dengan pengaman timestamp (+genpts)
                        subprocess.run([get_ffmpeg_path(), '-y', '-fflags', '+genpts', '-f', 'concat', '-safe', '0', '-i', loop_txt, '-c', 'copy', '-t', str(main_target), loop_out], check=True, capture_output=True, text=True, timeout=3600)
                    except subprocess.TimeoutExpired:
                        raise Exception("FFmpeg Concat Error: timeout (3600 dtk)")
                    except subprocess.CalledProcessError as e:
                        # Ambil 6 baris terakhir agar error aslinya tidak tertutup statistik ukuran
                        err_lines = e.stderr.strip().split('\n')
                        real_err = " | ".join(err_lines[-6:])
                        raise Exception(f"FFmpeg Concat Error: {real_err}")
                elif main_target > 0:
                    if stop_flags.get(task_id): raise Exception("Dibatalkan")

                    try:
                        subprocess.run([get_ffmpeg_path(), '-y', '-i', base_video, '-c', 'copy', '-t', str(main_target), loop_out], check=True, capture_output=True, text=True, timeout=1800)
                    except subprocess.TimeoutExpired:
                        raise Exception("FFmpeg Final Error: timeout (1800 dtk)")
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
                            _write_concat_list(join_txt, [loop_out, special_video])  # 🔒 #20
                            subprocess.run([get_ffmpeg_path(), '-y', '-fflags', '+genpts', '-f', 'concat', '-safe', '0', '-i', join_txt, '-c', 'copy', final_video], check=True, capture_output=True, text=True, timeout=3600)
                        else:
                            # Target lebih pendek dari lagu khusus: pakai lagu khusus saja (dipotong target)
                            subprocess.run([get_ffmpeg_path(), '-y', '-i', special_video, '-c', 'copy', '-t', str(target_sec), final_video], check=True, capture_output=True, text=True, timeout=1800)
                    except subprocess.TimeoutExpired:
                        raise Exception("FFmpeg Special-Song Error: timeout")
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
                        # 🔥 v1.2.18: tahap QC (persen tidak berubah — QC tanpa %).
                        d['progress_stage'] = 'qc'
            save_tasks_db()
            qc_failures = run_qc_checks(task, final_video, yt_id)
            if qc_failures:
                raise _QCFailure(" | ".join(qc_failures))

            if channel_data:
                creds_list = channel_data.get('creds_list', [channel_data.get('creds_json')])
                upload_berhasil = False
                # 🔥 v1.2.25: kredensial yang BENAR-BENAR dipakai upload
                # (bisa index > 0 setelah auto-switch kuota) + project-nya.
                used_cred_index = None
                used_project_id = None
                pesan_error = "Token API Tidak Ditemukan/Kosong!"
                # 🧹 v1.2.13: JARING PENGAMAN TERAKHIR — bersihkan judul /
                # deskripsi / tag tepat sebelum videos.insert, supaya teks
                # kotor dari jalur mana pun (termasuk task lama yang dibuat
                # sebelum v1.2.13) tidak pernah sampai ke YouTube. Nilai
                # bersih ditulis balik ke blueprint + task aktif supaya
                # panel/riwayat/notifikasi konsisten.
                _up_title = _clean_upload_text(task.get('title', ''), max_len=100,
                                               fallback='Video Tanpa Judul',
                                               strip_channel=(channel_data or {}).get('name', ''))
                _up_desc = _clean_upload_text(task.get('description', ''), max_len=5000,
                                              fallback=_up_title)
                if _up_title != (task.get('title') or '') or _up_desc != (task.get('description') or ''):
                    task['title'] = _up_title
                    task['description'] = _up_desc
                    task['title_cleaned'] = True
                    try:
                        with db_lock:
                            for _d in active_tasks:
                                if _d.get('id') == task_id:
                                    _d['title'] = _up_title
                                    _d['title_cleaned'] = True
                                    break
                        save_tasks_db()
                    except Exception:
                        pass

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
                        
                            # 🧹 v1.2.13: tag dibersihkan di sini (jaring
                            # pengaman terakhir) — '#' dibuang seperti
                            # perilaku lama, tiap tag maks 30 karakter,
                            # total maks 400 karakter.
                            tags_list = _clean_upload_tags(task.get('tags', '')) or ['wavepush']

                            # 🔥 FASE 8: Shorts Muse masuk kategori Hiburan (24)
                            # seperti Clipper; video panjang tetap Musik (10).
                            _cat = '24' if task.get('kind') == SHORTS_MUSE_KIND else '10'
                            body = {
                                'snippet': {'title': _up_title, 'description': _up_desc, 'tags': tags_list, 'categoryId': _cat},
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
                                                    # 🔥 v1.2.18: persen upload terstruktur (diukur next_chunk).
                                                    d['progress_pct'] = int(status.progress() * 100)
                                                    d['progress_stage'] = 'upload'
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

                            # 🔥 FASE 8: Shorts tidak memakai thumbnail 16:9 —
                            # YouTube Shorts memakai frame video sebagai sampul.
                            # 🎵 Playlist: thumbnail custom hasil modifikasi
                            # (task['playlist_thumb']) didahulukan bila ada.
                            _pl_thumb = (task.get('playlist_thumb') or '').strip()
                            if _pl_thumb and os.path.exists(_pl_thumb):
                                thumb_path = _pl_thumb
                            elif task.get('kind') == PLAYLIST_KIND:
                                # 🔥 v1.2.22: playlist WAJIB pakai thumbnail
                                # hasil modifikasi; jangan fallback ke
                                # thumbnail galeri acak (bukan gambar playlist).
                                thumb_path = None
                            elif task.get('kind') == SHORTS_MUSE_KIND:
                                thumb_path = None
                            else:
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
                                # Shorts tidak memakai thumbnail 16:9 — YouTube
                                # memakai frame video sebagai sampul (bukan
                                # rusak). Video long: folder kosong / tidak ada
                                # gambar yang bisa dibuka, catat seperti dulu.
                                # Playlist tanpa thumbnail modifikasi: katakan
                                # jelas (bukan "tidak ada thumbnail valid").
                                with db_lock:
                                    for d in active_tasks:
                                        if d['id'] == task_id:
                                            if task.get('kind') == SHORTS_MUSE_KIND:
                                                d['thumb_status'] = "Sampul dari frame video"
                                            elif task.get('kind') == PLAYLIST_KIND:
                                                d['thumb_status'] = "Thumbnail playlist tidak tersedia"
                                            else:
                                                d['thumb_status'] = "Tidak ada thumbnail valid"
                                save_tasks_db()
                                
                            try:
                                if task.get('playlist_id'):
                                    youtube.playlistItems().insert(part='snippet', body={'snippet': {'playlistId': task['playlist_id'], 'resourceId': {'kind': 'youtube#video', 'videoId': video_id}}}).execute()
                            except: pass
                            # Status disimpan POLOS (tanpa HTML): video_id sudah
                            # tersimpan sebagai field task (Fase 4, di atas) dan
                            # panel merender link [Lihat] dari sana. HTML di
                            # dalam status dulu tampil sebagai teks mentah karena
                            # panel meng-escape status (perlindungan XSS).
                            move_to_history(task_id, "Tayang! ✅")
                            # v1.3.10: pesan Tayang dirapikan — channel dulu
                            # (langsung ketahuan channel mana), judul video
                            # lengkap, tanggal singkat; referensi dibuang
                            # (isinya sama dengan judul).
                            _audio_note = (task.get('shorts_audio') or '').strip()
                            _tayang_title = f"{ch_name} — Tayang"
                            if task.get('kind') == SHORTS_MUSE_KIND:
                                _tayang_title = f"Shorts {ch_name} — Tayang"
                            _tayang_status = (f"🎵 {task['title']}\n"
                                              f"🕔 {_short_tayang_date(task.get('publish_date') or '')}")
                            if _audio_note:
                                _tayang_status += f"\n🎙️ Suara: {_audio_note}"
                            if task.get('kind') == ZUMO_KIND:
                                _zt = task.get('zumo_tracks') or []
                                _zc = task.get('zumo_credits') or 0
                                _tayang_status += (f"\n🎵 Playlist Zumo ({len(_zt)} lagu"
                                                   + (f", kredit Kie {_zc}" if _zc else "") + ")")
                            notify_event("✅", _tayang_title, _tayang_status)
                            upload_berhasil = True
                            # 🔥 v1.2.25: catat kredensial/project yang
                            # benar-benar dipakai (untuk hitungan kuota
                            # per project + notifikasi auto-switch).
                            used_cred_index = index_kunci
                            used_project_id = _oauth_project_of_cred_str(cred_str)
                            # 🔥 FASE 5: 1 sukses → reset hitungan gagal beruntun.
                            _record_channel_success(yt_id)
                            # 🎵 MUSIK PLAYLIST: setelah upload sukses → hapus
                            # video final + folder kerja job (audio + thumbnail).
                            if task.get('kind') == PLAYLIST_KIND:
                                _playlist_cleanup_after_upload(task)
                            elif task.get('kind') == ZUMO_KIND:
                                _zumo_cleanup_after_upload(task)
                            # 🔥 v1.2.20: batas 100 upload/hari/channel → cooldown sampai tengah malam WIB,
                            # besok lanjut otomatis. Task antre tetap aman menunggu.
                            try:
                                # 🔥 v1.2.25: hitungan kuota dicatat di
                                # project yang benar-benar dipakai upload.
                                cnt, capped = track_daily_upload(yt_id, project_id=used_project_id)
                                if capped:
                                    channel_cooldowns[yt_id] = _next_midnight_wib_ts()
                                    try: persist_cooldown(yt_id)
                                    except Exception: pass
                                    notify_event("⏸️", f"Cooldown harian {ch_name}",
                                                 f"Udah {cnt} video tayang hari ini (batas {MAX_UPLOADS_PER_CHANNEL_PER_DAY}/hari) 😌 Saya jeda dulu, besok lanjut otomatis.")
                                elif cnt == MAX_UPLOADS_PER_CHANNEL_PER_DAY - 1:
                                    # 🔥 FASE 1 MONITORING: peringatan kuota menipis — hanya
                                    # sekali, tepat saat counter menjadi 99/100 (tinggal 1 video).
                                    notify_event("⚠️", f"Kuota hampir habis {ch_name}",
                                                 f"Udah {cnt}/{MAX_UPLOADS_PER_CHANNEL_PER_DAY} video hari ini, sisa 1 lagi 😌 Saya irit-irit dulu ya, reset tengah malam WIB.")
                                # 🔥 FASE 6: kuota dihitung PER PROJECT Google.
                                # Kalau total upload project ini sudah 100 hari ini,
                                # cooldown semua channel di project yang sama
                                # (channel yang barusan upload sudah di-cooldown
                                # di atas; saudara satu project ikut dijeda).
                                # 🔥 v1.2.25: project yang dicek = project yang
                                # benar-benar dipakai (bisa project cadangan).
                                _pid = used_project_id or (_project_id_of_channel(channel_data) if channel_data else None)
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
                        # 🔥 v1.2.25: upload berhasil via kredensial
                        # cadangan → laporkan switch kuota ke Telegram
                        # (sekali, tanpa emoji).
                        if used_cred_index is not None and used_cred_index > 0:
                            _from_pid = _project_id_of_channel(channel_data) if channel_data else None
                            _oauth_notify_quota_switch(
                                ch_name, "upload", _from_pid, used_project_id,
                                _project_uploads_today(used_project_id),
                                MAX_UPLOADS_PER_CHANNEL_PER_DAY)
                        break
                    # 🔥 v1.2.25: semua kredensial kena quotaExceeded →
                    # pesan jujur: sebut project cadangan yang belum
                    # dihubungkan channel ini (bila ada) + langkahnya.
                    if not upload_berhasil and "API Habis" in (pesan_error or "") \
                            and "belum dihubungkan" not in pesan_error:
                        pesan_error = pesan_error + _oauth_quota_failure_hint(channel_data, ch_name)
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
                # Status disimpan POLOS (tanpa HTML); link unduhan disimpan
                # sebagai field download_url dan panel merendernya.
                try:
                    with db_lock:
                        for d in active_tasks:
                            if d['id'] == task_id:
                                d['download_url'] = f"/static/final_{task_id}.mp4"
                    save_tasks_db()
                except Exception:
                    pass
                move_to_history(task_id, "Render Selesai ✅")
                notify_event("✅", task['title'], f"Render beres, Bos! Saya upload ke YouTube {ch_name} ya 😎")
                # 🔥 FASE 5: render sukses juga mereset hitungan gagal beruntun.
                _record_channel_success(yt_id)

        except _QCFailure as e:
            # 🔥 FASE 5: gagal QC — JANGAN upload. Task ke riwayat dengan alasan
            # yang jelas + notif Telegram + tombol Retry (retry = render ulang).
            reason = str(e)[:200]
            move_to_history(task_id, f"Gagal QC 🛑 ({reason[:100]})")
            _qc_just_paused, _qc_streak, _qc_paused = _record_channel_failure(yt_id, task['title'], f"QC: {reason}")
            if task.get('kind') == PLAYLIST_KIND:
                _playlist_job_update(task.get('playlist_job_id'), status='failed',
                                     error=f"QC: {reason[:200]}")
            if task.get('kind') == ZUMO_KIND:
                _zumo_job_update(task.get('zumo_job_id'), status='failed',
                                 error=f"QC: {reason[:200]}")
            if not stop_flags.get(task_id):
                if _qc_paused:
                    # 🔥 v1.2.17: channel dijeda → SATU pesan gabungan (tanpa
                    # tombol Coba Lagi) — retry tidak jalan sebelum Lanjutkan.
                    try:
                        send_tg_message(_failure_combined_message(task['title'], ch_name, f"QC: {reason}", _qc_streak, _qc_just_paused))
                    except Exception:
                        pass
                else:
                    retry_markup = {"inline_keyboard": [[
                        {"text": "🔁 Coba Lagi", "callback_data": f"retry:{task_id}"}
                    ]]}
                    # Pesan QC ditulis ulang (2026-11-19): jelas apa yang terjadi,
                    # kenapa, dan apa yang harus dilakukan. Untuk judul duplikat,
                    # retry TIDAK lagi render ulang judul yang sama (pasti gagal
                    # lagi) — retry otomatis ganti judul baru (lihat
                    # operator_retry_task), dan pesan ini mengatakannya.
                    if 'judul sudah tayang' in reason:
                        qc_msg = (f"Bos, video ini saya TAHAN dulu ya 😅\n"
                                  f"Alasan: {reason[:120]}\n"
                                  f"Judulnya sudah pernah tayang di channel ini — "
                                  f"kalau di-upload lagi, YouTube bisa menganggapnya duplikat.\n"
                                  f"Pencet 🔁 Coba Lagi — saya render ulang dengan judul baru yang belum dipakai.")
                    else:
                        qc_msg = (f"Bos, video ini nggak lolos pemeriksaan kualitas (QC) — "
                                  f"jadi saya TAHAN, tidak di-upload 😅\n"
                                  f"Alasan: {reason[:120]}\n"
                                  f"Pencet 🔁 Coba Lagi kalau mau saya render ulang.")
                    notify_event("🛑", task['title'], qc_msg, reply_markup=retry_markup)

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
                queue_put(task, allow_overflow=True)
            else:
                move_to_history(task_id, f"Gagal ❌ ({err_msg})")
                if task.get('kind') == PLAYLIST_KIND:
                    _playlist_job_update(task.get('playlist_job_id'), status='failed',
                                         error=err_msg[:200])
                if task.get('kind') == ZUMO_KIND:
                    _zumo_job_update(task.get('zumo_job_id'), status='failed',
                                     error=err_msg[:200])
                # 🔥 FASE 5: catat kegagalan beruntun untuk auto-pause channel.
                # Pembatalan manual (stop flag / "Dibatalkan") BUKAN kegagalan
                # channel — jangan dihitung.
                _fail_just_paused, _fail_streak, _fail_paused = False, 0, False
                if not stop_flags.get(task_id) and "Dibatalkan" not in err_msg:
                    _fail_just_paused, _fail_streak, _fail_paused = _record_channel_failure(yt_id, task['title'], err_msg)
                # 🔥 FIX: kalau digagalkan watchdog (stop flag), JANGAN kirim
                # notif gagal lagi — watchdog sudah mengirim notifikasinya sendiri.
                if not stop_flags.get(task_id):
                    if _fail_paused:
                        # 🔥 v1.2.17: channel dijeda → SATU pesan gabungan
                        # (tanpa tombol Coba Lagi) — retry tidak jalan
                        # sebelum Lanjutkan.
                        try:
                            send_tg_message(_failure_combined_message(task['title'], ch_name, err_msg, _fail_streak, _fail_just_paused))
                        except Exception:
                            pass
                    else:
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
    return render_template('hub.html', csrf_token=_get_csrf_token())

@app.route('/factory')
def factory_main_dashboard():
    # Dashboard Factory
    return render_template('index.html', csrf_token=_get_csrf_token())

@app.route('/clipper')
def clipper_main_dashboard():
    # Dashboard Clipper
    return render_template('clipper.html', csrf_token=_get_csrf_token())

@app.route('/api/notifications', methods=['GET'])
def get_notifications():
    return jsonify(system_notifications)

@app.route('/api/notifications/clear', methods=['POST'])
def clear_notifications():
    global system_notifications
    with db_lock:
        system_notifications.clear()
    return jsonify({"status": "success"})

# ---- Log aktivitas (v1.3.11, v1.3.71) ----
# 🔥 v1.3.71: SATU log aktivitas — dulu LOG AKTIVITAS punya 2 sumber
# (Panel = access log penuh polling GET tiap detik, Operator = pesan
# startup bot) dan TIDAK ADA yang mencatat aktivitas pabrik. Sekarang
# satu file logs/activity.log, ditulis dari titik-titik penting: task
# dibuat, render mulai, task selesai/gagal. Format per baris:
# [YYYY-MM-DD HH:MM WIB] pesan. Operator (proses lain) menulis ke file
# yang sama via append — aman untuk 1 baris.
_LOG_SOURCES = {'aktivitas': 'activity.log'}
_ACTIVITY_LOG_FILE = os.path.join(BASE_DIR, 'logs', 'activity.log')
_ACTIVITY_LOG_MAX_BYTES = 262144  # ~256 KB, lalu potong 500 baris tertua
_activity_lock = threading.Lock()


def _activity_log(msg):
    """Tulis 1 baris aktivitas ke logs/activity.log (WIB). Gagal diam-diam
    — log tidak boleh mematikan alur utama."""
    try:
        line = f"[{_wib_now().strftime('%Y-%m-%d %H:%M')} WIB] {msg}\n"
        with _activity_lock:
            os.makedirs(os.path.dirname(_ACTIVITY_LOG_FILE), exist_ok=True)
            with open(_ACTIVITY_LOG_FILE, 'a', encoding='utf-8') as f:
                f.write(line)
            try:
                if os.path.getsize(_ACTIVITY_LOG_FILE) > _ACTIVITY_LOG_MAX_BYTES:
                    with open(_ACTIVITY_LOG_FILE, encoding='utf-8') as f:
                        lines = f.read().splitlines()
                    with open(_ACTIVITY_LOG_FILE, 'w', encoding='utf-8') as f:
                        f.write('\n'.join(lines[-1500:]) + '\n')
            except Exception:
                pass
    except Exception:
        pass


def _activity_channel_name(yt_id):
    try:
        for c in database_channel:
            if c.get('yt_id') == yt_id:
                return c.get('name') or yt_id
    except Exception:
        pass
    return yt_id


def _activity_task_created(kind_label, title, yt_id):
    _activity_log(f"📝 Task dibuat: {kind_label} — {title or '(tanpa judul)'} [{_activity_channel_name(yt_id)}]")


def _activity_task_finished(t, final_status):
    s = str(final_status or '')
    icon = '✅' if 'Tayang' in s else ('❌' if 'Gagal' in s else ('🚫' if 'Ditolak' in s or 'Batal' in s else '🏁'))
    _activity_log(f"{icon} {s}: {t.get('title') or '(tanpa judul)'} [{_activity_channel_name(t.get('yt_id'))}]")


def _log_file_for(source):
    name = _LOG_SOURCES.get((source or 'panel').strip().lower(), 'panel.log')
    return os.path.join(BASE_DIR, 'logs', name)


def _read_log_tail(path, nlines, max_bytes=262144):
    """Baca N baris terakhir file log (efisien: baca dari ujung file).
    File tidak ada/kosong -> []. Null byte (bekas truncate) dibuang."""
    try:
        nlines = max(1, min(int(nlines or 200), 1000))
    except Exception:
        nlines = 200
    try:
        size = os.path.getsize(path)
    except Exception:
        return []
    if size <= 0:
        return []
    try:
        with open(path, 'rb') as f:
            chunk = b''
            pos = size
            while pos > 0 and chunk.count(b'\n') <= nlines:
                step = min(max_bytes, pos)
                pos -= step
                f.seek(pos)
                chunk = f.read(step) + chunk
                if pos == 0:
                    break
        text = chunk.decode('utf-8', 'replace').replace('\x00', '')
        return text.splitlines()[-nlines:]
    except Exception:
        return []


@app.route('/api/logs')
def logs_api():
    """Baris-baris terakhir log aktivitas (butuh login panel; GET saja).
    ?source=aktivitas&lines=200 — v1.3.71: satu-satunya sumber adalah
    logs/activity.log (task dibuat, render, tayang/gagal)."""
    source = (request.args.get('source') or 'aktivitas').strip().lower()
    if source not in _LOG_SOURCES:
        source = 'aktivitas'
    path = _log_file_for(source)
    try:
        lines = int(request.args.get('lines') or 200)
    except Exception:
        lines = 200
    lines = max(1, min(lines, 1000))
    try:
        size = os.path.getsize(path)
    except Exception:
        size = 0
    return jsonify({"status": "success", "source": source,
                    "lines": _read_log_tail(path, lines), "size": size})


@app.route('/api/logs/clear', methods=['POST'])
def logs_clear_api():
    """Kosongkan file log (butuh login + CSRF via check_security).
    Hanya memotong file log aplikasi sendiri — journal systemd tidak disentuh.
    Tee di proses pemilik file mendeteksi pemotongan dan buka ulang file."""
    data = request.get_json(silent=True) or {}
    source = (data.get('source') or request.form.get('source') or 'panel').strip().lower()
    if source not in _LOG_SOURCES:
        source = 'panel'
    path = _log_file_for(source)
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        if source == 'panel':
            with _LOG_LOCK:
                open(path, 'w', encoding='utf-8').close()
        else:
            open(path, 'w', encoding='utf-8').close()
        return jsonify({"status": "success", "source": source})
    except Exception as e:
        return jsonify({"status": "error", "message": str(e)[:200]}), 500


@app.route('/api/get_dashboard_stats')
def get_dashboard_stats():
    sys = get_system_stats()
    # 🔥 v1.3.22: info DISK untuk header sys-monitor (shutil.disk_usage
    # saja — murah, tanpa jalan-jalan direktori seperti _disk_usage_info).
    try:
        _du = shutil.disk_usage(BASE_DIR)
        _disk_pct = round(_du.used / max(1, _du.total) * 100, 1)
        _disk_text = f"{round(_du.used / (1024 ** 3), 1)}GB / {round(_du.total / (1024 ** 3), 1)}GB"
    except Exception:
        _disk_pct, _disk_text = 0, '–/–GB'
    return jsonify({
        "channels": len(database_channel), "active_tasks": len(active_tasks), "history_tasks": len(history_tasks),
        "sys_cpu": sys["cpu"], "sys_ram_pct": sys["ram_pct"], "sys_ram_text": f"{sys['ram_used']}GB / {sys['ram_total']}GB",
        "sys_disk_pct": _disk_pct, "sys_disk_text": _disk_text
    })

@app.route('/api/get_youtube_analytics')
def get_youtube_analytics():
    """Per-channel untuk TABEL DETAIL CHANNEL di Dashboard: statistik channel
    (channels().list part=snippet,statistics, cache 1 jam — sama seperti
    statistik video) + delta views dari snapshot harian channel (90 hari)
    + estimasi jam tayang + kesiapan monetisasi (helper bersama).

    Kolom REVENUE sengaja TIDAK ADA di tabel — tidak ada data penghasilan
    dari API (scope Analytics monetary tidak diizinkan di device flow);
    estimasi RPM hanya di kartu Monetisasi. Kolom WATCH 1YR juga tidak ada
    — snapshot hanya 90 hari. Angka views/subs/videos tetap dipertahankan
    untuk kartu TOTAL VIEWS/SUBSCRIBERS di Dashboard."""
    cache = _load_video_stats_cache()
    # 🔥 v1.3.57: ?force=1 lewati cache 1 jam (tombol ↻ Refresh di tabel
    # DETAIL CHANNEL) — sama seperti /api/get_video_stats.
    force = request.args.get('force') == '1'
    now = time.time()
    today_str = _wib_now().strftime("%Y-%m-%d")
    ch_stats = cache.setdefault('channel_stats', {})
    ch_snaps = cache.setdefault('channel_snapshots', [])
    cached_videos = cache.get('videos', {})
    snapshots = cache.get('snapshots', [])
    dirty = False
    # video_id -> (yt_id, kind) dari riwayat pabrik (untuk jalur Shorts
    # dan durasi rata-rata blueprint). Klip Clipper TIDAK ada di sini
    # (tercatat di clipper/clips_db.json, bukan history_tasks).
    vid_info = {}
    for t in history_tasks:
        vid = _history_task_video_id(t)
        yt = _history_task_yt_id(t)
        if vid and yt:
            vid_info[vid] = (yt, t.get('kind') or (t.get('blueprint') or {}).get('kind'))
    # durasi rata-rata blueprint video long per channel (jam) — dasar
    # estimasi jam tayang (metode sama seperti kartu Monetisasi Fase 9).
    dur_sum = {}
    dur_cnt = {}
    for t in history_tasks:
        yt = _history_task_yt_id(t)
        if not yt:
            continue
        if (t.get('kind') or (t.get('blueprint') or {}).get('kind')) == SHORTS_MUSE_KIND:
            continue
        h = _monetization_task_hours(t)
        if h and h > 0:
            dur_sum[yt] = dur_sum.get(yt, 0.0) + h
            dur_cnt[yt] = dur_cnt.get(yt, 0) + 1
    avg_dur = {yt: dur_sum[yt] / dur_cnt[yt] for yt in dur_sum}
    vid_snaps = {}
    for s in snapshots:
        vid_snaps.setdefault(s.get('video_id'), {})[s.get('date')] = int(s.get('views', 0) or 0)
    data = []
    for c in database_channel:
        yt_id = c['yt_id']
        ent = ch_stats.get(yt_id) or {}
        views = subs = videos = None
        published_at = ent.get('published_at')
        fetched_at = ent.get('fetched_at')
        fresh = (not force and ent.get('views') is not None and ent.get('fetched_at')
                 and (now - float(ent.get('fetched_at', 0)) < VIDEO_STATS_CACHE_TTL))
        if fresh:
            views, subs, videos = ent.get('views'), ent.get('subs'), ent.get('videos')
        else:
            # 🔥 FIX v1.3: failover antar key seperti upload — jangan cuma pakai Key #1
            # (Key #1 yang mati bikin grafik analitik kosong walau key lain hidup).
            creds_list = c.get('creds_list', [c.get('creds_json')])
            for cred_str in creds_list:
                if not cred_str:
                    continue
                try:
                    creds = Credentials.from_authorized_user_info(json.loads(cred_str))
                    if creds.expired and creds.refresh_token:
                        creds.refresh(Request())
                    youtube = build('youtube', 'v3', credentials=creds)
                    res = youtube.channels().list(part="snippet,statistics", id=yt_id).execute()
                    if res.get('items'):
                        item = res['items'][0]
                        stats = item.get('statistics', {})
                        views = int(stats.get('viewCount', 0))
                        subs = int(stats.get('subscriberCount', 0))
                        videos = int(stats.get('videoCount', 0))
                        published_at = (item.get('snippet', {}) or {}).get('publishedAt', '') or published_at
                        fetched_at = now
                        ch_stats[yt_id] = {"views": views, "subs": subs, "videos": videos,
                                           "published_at": published_at, "fetched_at": fetched_at}
                        dirty = True
                    break
                except Exception:
                    continue
            if views is None and ent.get('views') is not None:
                # API gagal — pakai cache basi supaya tabel tidak kosong mendadak.
                views, subs, videos = ent.get('views'), ent.get('subs'), ent.get('videos')
                fetched_at = ent.get('fetched_at')
            elif views is None:
                views, subs, videos = 0, 0, 0
        # --- snapshot harian channel (satu per channel per hari, 90 hari) ---
        if views is not None and not any(s.get('date') == today_str and s.get('yt_id') == yt_id for s in ch_snaps):
            ch_snaps.append({"date": today_str, "yt_id": yt_id, "views": int(views),
                             "subs": int(subs or 0), "videos": int(videos or 0)})
            dirty = True
        cutoff = (_wib_now() - timedelta(days=90)).strftime("%Y-%m-%d")
        pruned = [s for s in ch_snaps if s.get('date', '') >= cutoff]
        if len(pruned) != len(ch_snaps):
            ch_snaps = pruned
            cache['channel_snapshots'] = ch_snaps
            dirty = True
        totals = {s['date']: int(s.get('views', 0)) for s in ch_snaps if s.get('yt_id') == yt_id}
        v28, d28, ref = _snapshot_delta(totals, 28)
        v48, d48, _ = _snapshot_delta(totals, 2, ref_date=ref)
        v30, d30, _ = _snapshot_delta(totals, 30, ref_date=ref)
        v90, d90, _ = _snapshot_delta(totals, 90, ref_date=ref)
        # tren: views 28D terbaru vs 28 hari sebelumnya (±2% = datar)
        trend = None
        if v28 is not None and d28:
            prev28, _, _ = _snapshot_delta(totals, 28, ref_date=d28)
            if prev28 is not None:
                if prev28 == 0:
                    trend = "up" if v28 > 0 else "flat"
                elif v28 > prev28 * 1.02:
                    trend = "up"
                elif v28 < prev28 * 0.98:
                    trend = "down"
                else:
                    trend = "flat"
        avg_views = (int(views) // int(videos)) if videos else None
        avg_h = avg_dur.get(yt_id)
        watch30 = round(v30 * avg_h, 1) if (v30 is not None and avg_h) else None
        watch90 = round(v90 * avg_h, 1) if (v90 is not None and avg_h) else None
        watch365_real, watch30_real = _fetch_watch_hours_real(c, yt_id, cache)
        shorts_views, shorts_complete = _shorts_views_90d(yt_id, vid_info, vid_snaps, cached_videos, ref)
        readiness = _channel_monetization_readiness(
            subs=subs, watch_90d_est_hours=watch90,
            shorts_views_90d=shorts_views, shorts_complete=shorts_complete,
            monetized=bool(c.get('monetized')), today_str=today_str,
            watch_real_hours=watch365_real)
        data.append({
            "yt_id": yt_id,
            "name": c.get('name', 'Tanpa Nama'),
            "status": c.get('status', 'Connected'),
            "views": views,
            "subs": subs,
            "videos": videos,
            "published_at": published_at,
            "age_label": _channel_age_label(published_at, _wib_now().date()),
            "updated_label": _format_wib_label(fetched_at),
            "views_28d": v28,
            "views_48h": v48,
            "views_30d": v30,
            "views_90d": v90,
            "snap_28d": d28,
            "snap_48h": d48,
            "snap_30d": d30,
            "snap_ref": ref,
            "trend": trend,
            "avg_views": avg_views,
            "watch_30d_est_hours": watch30,
            "watch_30d_real": watch30_real,
            "watch_365h_real": watch365_real,
            "analytics_scope": _analytics_scope_state(cache, yt_id),
            "watch_90d_est_hours": watch90,
            # watch_hours dipakai kartu WATCH TIME di atas + progress TARGET
            # MONETISASI: estimasi jam tayang 90 hari terakhir (0 bila belum
            # bisa dihitung — dulu field ini selalu 0).
            "watch_hours": watch90 if watch90 is not None else 0,
            "shorts_views_90d": shorts_views,
            "shorts_complete": shorts_complete,
            "monetized": bool(c.get('monetized')),
            "monetization": readiness,
        })
    if dirty:
        _save_video_stats_cache(cache)
    return jsonify(data)


# ==========================================
# 📊 TABEL DETAIL CHANNEL + KESIAPAN MONETISASI
# ==========================================
# Ambang monetisasi YouTube resmi (aturan yang berlaku saat ini; channel
# yang SUDAH monetisasi tidak terpengaruh aturan baru):
#  - s.d. 31 Jan 2027: 1.000 subs + 4.000 jam tayang (12 bln) ATAU
#    10 jt views Shorts (90 hari terakhir)
#  - mulai 1 Feb 2027: 1.000 subs + 8.000 jam tayang (365 hari) ATAU
#    20 jt views Shorts (90 hari terakhir). Subs tetap 1.000.
# Semua ambang di SATU tempat ini supaya mudah diubah dan bisa dipakai
# ulang laporan Telegram pagi/sore nanti.
MONETIZE_SUBS_REQUIRED = 1000
MONETIZE_RULE_CHANGE_DATE = "2027-02-01"
MONETIZE_WATCH_HOURS_BEFORE = 4000
MONETIZE_WATCH_HOURS_FROM = 8000
MONETIZE_SHORTS_VIEWS_BEFORE = 10_000_000
MONETIZE_SHORTS_VIEWS_FROM = 20_000_000

_ID_MONTHS = ["Jan", "Feb", "Mar", "Apr", "Mei", "Jun", "Jul", "Agu", "Sep", "Okt", "Nov", "Des"]


def _monetization_rule_for(date_str):
    """Ambang monetisasi yang berlaku pada tanggal WIB tertentu."""
    if date_str >= MONETIZE_RULE_CHANGE_DATE:
        return {"subs": MONETIZE_SUBS_REQUIRED, "watch_hours": MONETIZE_WATCH_HOURS_FROM,
                "shorts_views_90d": MONETIZE_SHORTS_VIEWS_FROM,
                "label": "1.000 subs + 8.000 jam tayang (365 hari) atau 20 jt views Shorts (90 hari)",
                "rule_next": None}
    return {"subs": MONETIZE_SUBS_REQUIRED, "watch_hours": MONETIZE_WATCH_HOURS_BEFORE,
            "shorts_views_90d": MONETIZE_SHORTS_VIEWS_BEFORE,
            "label": "1.000 subs + 4.000 jam tayang (12 bln) atau 10 jt views Shorts (90 hari)",
            "rule_next": "Mulai 1 Feb 2027: 1.000 subs + 8.000 jam tayang (365 hari) atau 20 jt views Shorts (90 hari)"}


def _channel_monetization_readiness(*, subs, watch_90d_est_hours, shorts_views_90d,
                                   shorts_complete, monetized, today_str, watch_real_hours=None):
    """SATU tempat perhitungan kesiapan monetisasi — dipakai panel dan
    (nanti) laporan Telegram pagi/sore. Jam tayang: ASLI dari Analytics
    API (365 hari) bila watch_real_hours diberikan, kalau tidak ESTIMASI
    (snapshot views 90 hari terakhir × durasi rata-rata blueprint video
    long channel). Keputusan akhir monetisasi ada di YouTube Studio.
    Jalur Shorts hanya dipakai bila shorts_complete=True (baseline bisa
    dihitung jujur dari snapshot); bila tidak, badge tidak pernah 'ready'
    lewat jalur Shorts."""
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


def _snapshot_delta(totals, days, ref_date=None):
    """Selisih nilai snapshot: total(terbaru) − total(terdekat pada/
    sebelum ref−days). totals: {tanggal 'YYYY-MM-DD': nilai}.
    Kembalikan (delta, tanggal_dasar, tanggal_ref) atau (None, None,
    tanggal_ref) bila riwayat belum cukup (mis. channel baru) — pemanggil
    menampilkan '—', jangan karang angka."""
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


def _shorts_views_90d(yt_id, vid_info, vid_snaps, cached_videos, ref_date):
    """Views Shorts (90 hari) dari snapshot per-video — HANYA video Shorts
    Muse (kind='shorts_muse'). Klip Clipper TIDAK termasuk (tidak tercatat
    di snapshot pabrik) — jadi angka ini bisa terlalu rendah bila channel
    punya klip Clipper tayang, dan itulah mengapa kolomnya ditandai
    'Shorts Muse'. Kembalikan (total_views, complete): complete=False bila
    ada video Shorts yang baseline 90 hari lalu-nya tidak bisa ditentukan
    (dipublikasikan sebelum riwayat snapshot dan tanpa snapshot lama) —
    jalur Shorts lalu tidak dipakai untuk badge. Tanpa video Shorts sama
    sekali → (0, True), terhitung jujur."""
    total = 0
    complete = True
    found = False
    for vid, (yt, kind) in (vid_info or {}).items():
        if yt != yt_id or kind != SHORTS_MUSE_KIND:
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


def _channel_age_label(published_at, today):
    """'45 hari' / '3 bln' / '2 thn 3 bln' dari snippet.publishedAt."""
    if not published_at:
        return None
    try:
        d = datetime.strptime(str(published_at)[:10], "%Y-%m-%d").date()
    except Exception:
        return None
    days = (today - d).days
    if days < 0:
        return None
    if days < 60:
        return f"{days} hari"
    if days < 365:
        return f"{max(1, days // 30)} bln"
    y, m = days // 365, (days % 365) // 30
    return f"{y} thn" + (f" {m} bln" if m else "")


def _format_wib_label(epoch_ts):
    """'18 Nov 21:52' (WIB) dari epoch detik; None bila tidak diketahui."""
    if not epoch_ts:
        return None
    try:
        dt = datetime.fromtimestamp(float(epoch_ts), tz=ZoneInfo("Asia/Jakarta"))
        return f"{dt.day} {_ID_MONTHS[dt.month - 1]} {dt.hour:02d}:{dt.minute:02d}"
    except Exception:
        return None


# ==========================================
# 📊 FASE 4 STATISTIK PER VIDEO
# ==========================================
# Per-video: views/likes/komentar via YouTube Data API v3 videos().list
# (biaya 1 unit kuota per panggilan, maks 50 video/panggilan).
# Subs per video dari YouTube Analytics API (blok FASE 10): hanya bila
# token channel punya scope 'yt-analytics.readonly'; bila belum,
# subs tampil '—' dan channel ditandai perlu taut ulang. Subs channel
# tetap dari channels().list (endpoint lama).
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
        # --- FASE 10: subs per video via Analytics API ---
        # Diambil untuk video yang cache-nya belum punya field 'subs'
        # (video baru di-fetch Data API + video lama yang di-cache sebelum
        # Fase 10). Video tombstone (missing) dilewati. Bila token
        # diketahui belum punya scope Analytics, API tidak dipanggil
        # sama sekali — subs tetap None dan channel ditandai di entry.
        subs_needed = [vid for vid in ids
                       if 'subs' not in (cached_videos.get(vid) or {})
                       and not (cached_videos.get(vid) or {}).get('missing')]
        if subs_needed:
            if _analytics_scope_state(cache, yt_id) is False:
                pass  # scope diketahui belum ada — jangan panggil API
            else:
                _ch = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
                if _ch is not None:
                    subs_map, _scope = _fetch_video_subs(_ch, yt_id, subs_needed)
                    if _scope is not None:
                        _set_analytics_scope_state(cache, yt_id, _scope)
                    for _vid in subs_needed:
                        _subs_val = subs_map.get(_vid)
                        cached_videos.setdefault(_vid, {})['subs'] = _subs_val
                        # 🔥 FIX: 'fetched' berisi dict ASLI dari
                        # _fetch_channel_video_stats, sedangkan cache
                        # menyimpan salinannya — subs harus ditulis ke
                        # keduanya supaya baris respons (dibangun dari
                        # fresh/fetched di bawah) ikut melihatnya.
                        if _vid in fetched:
                            fetched[_vid]['subs'] = _subs_val
                        if _vid in fresh:
                            fresh[_vid]['subs'] = _subs_val
                    _save_video_stats_cache(cache)
        entry["analytics_scope"] = _analytics_scope_state(cache, yt_id)
        stats = dict(fresh)
        stats.update(fetched)
        for vid, t in tasks.items():
            st = stats.get(vid)
            pub = (st or {}).get('published_at', '')[:10] or t.get('finished_date') or (t.get('publish_date') or '')[:10]
            videos_out.append({
                "video_id": vid, "title": t.get('title', '?'), "channel": ch_name, "yt_id": yt_id,
                "publish_date": pub, "views": int((st or {}).get('views', 0)),
                "likes": int((st or {}).get('likes', 0)), "comments": int((st or {}).get('comments', 0)),
                "subs": (st or {}).get('subs'),  # 🔥 FASE 10: None = belum ada data / token tanpa scope Analytics
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
    # 🔥 FASE 10: catatan subs dinamis mengikuti status scope Analytics.
    _scope_states = [c.get("analytics_scope") for c in channels_out]
    if _scope_states and all(s is True for s in _scope_states):
        subs_note = "Subscriber per video dari YouTube Analytics API (token channel sudah punya scope yt-analytics.readonly)."
    elif any(s is False for s in _scope_states):
        subs_note = "Subscriber per video hanya untuk channel yang tokennya punya scope YouTube Analytics. Channel yang ditandai 'perlu taut ulang' harus ditautkan ulang (kode device baru) supaya tokennya mendapat scope baru — RPM otomatis juga ikut aktif."
    else:
        subs_note = "Subscriber per video (YouTube Analytics API) nonaktif — login device tidak mengizinkan scope Analytics, jadi subs tampil '—'. RPM isi manual di Settings."
    return jsonify({
        "videos": sorted(videos_out, key=lambda x: x['publish_date'], reverse=True),
        "summary": {"total_videos": len(videos_out), "total_views": sum(v['views'] for v in videos_out),
                    "views_7d": views_7d, "views_30d": views_30d, "best_worst": best_worst},
        "channels": channels_out,
        "cache_used": cache_used,
        "subs_note": subs_note,
    })

# ==========================================
# 📈 FASE 10 SCOPE ANALYTICS API (RPM + SUBS OTOMATIS)
# ==========================================
# Token lama hanya punya scope youtube + youtube.upload, jadi subs per
# video (Fase 4) dan RPM otomatis (Fase 9) tidak tersedia. Fase 10
# menambahkan scope Analytics ke SCOPES — TETAPI endpoint device-code
# Google menolak kedua scope Analytics ("Invalid device flow scope",
# diverifikasi 2026-11-18), jadi scope itu DIHAPUS lagi dari SCOPES dan
# fitur Analytics NONAKTIF sampai ada auth-code flow lewat browser
# (client Desktop). Helper di bawah tetap dipertahankan untuk masa itu;
# _query_analytics tidak memanggil API selama scope tidak ada di SCOPES.
# Subs per video tampil '—' (bukan 0) dan RPM memakai isian manual
# di Settings sebagai satu-satunya sumber. Data Analytics terlambat
# ~48 jam; video tanpa baris data = subs None (tampil '—', bukan 0).
# YouTube Analytics API (googleapiclient, pola get_fresh_credentials +
# failover kredensial yang sama seperti Data API; tanpa dependensi baru):
#  - subs per video: reports.query ids=channel==MINE,
#    metrics=subscribersGained, dimensions=video, filters=video==id,...
#  - RPM channel: reports.query ids=channel==MINE, metrics=rpm,
#    30 hari terakhir.
ANALYTICS_SCOPE = 'https://www.googleapis.com/auth/yt-analytics.readonly'
ANALYTICS_EARLIEST_DATE = '2005-02-14'  # tanggal paling awal yang diizinkan API


def _id_token_email(id_token):
    """Ambil email pemilik dari id_token Google (JWT) tanpa verifikasi
    tanda tangan — token baru saja diterima langsung dari Google lewat
    TLS di endpoint ini, jadi payload-nya tepercaya. Kembalikan '' bila
    tidak ada / tidak bisa dibaca."""
    try:
        import base64
        parts = (id_token or '').split('.')
        if len(parts) != 3:
            return ''
        payload = parts[1] + '=' * (-len(parts[1]) % 4)
        data = json.loads(base64.urlsafe_b64decode(payload).decode('utf-8'))
        return (data.get('email') or '').strip()
    except Exception:
        return ''


def _new_channel_record(next_id, item, creds_json, client_id, owner_email=''):
    """Bentuk record channel baru — cerminan pembuatan channel di
    poll_device_token (dipakai tambah-channel 1x login v1.3.49)."""
    rec = {"id": next_id, "name": item['snippet']['title'], "yt_id": item['id'],
           "thumbnail": item['snippet']['thumbnails']['default']['url'],
           "status": "Connected 🟢 1 Key", "creds_list": [creds_json],
           "oauth_client_id": client_id}
    if owner_email:
        rec["owner_email"] = owner_email
    return rec


def _channel_add_save(item, creds_json, client_id, owner_email=''):
    """Simpan token hasil login menjadi channel (dipakai endpoint
    konfirmasi tambah channel — device flow maupun 1x login). Channel baru
    dibuat, channel lama ditambah tokennya (key mati dibuang dulu).
    Kembalikan (created, channel_name)."""
    global database_channel
    with db_lock:
        c_idx = next((i for i, c in enumerate(database_channel) if c['yt_id'] == item['id']), None)
        if c_idx is None:
            new_c = _new_channel_record(len(database_channel) + 1, item, creds_json, client_id, owner_email)
            database_channel.append(new_c)
            created = True
        else:
            ch = database_channel[c_idx]
            if 'creds_list' not in ch:
                ch['creds_list'] = [ch.get('creds_json', '')]
            live_keys = []
            for ks in ch['creds_list']:
                if not ks:
                    continue
                try:
                    kc = Credentials.from_authorized_user_info(json.loads(ks))
                    if kc.expired and kc.refresh_token:
                        kc.refresh(Request())
                    live_keys.append(ks)
                except Exception as e:
                    if 'invalid_grant' not in str(e).lower():
                        live_keys.append(ks)
            ch['creds_list'] = live_keys
            _oauth_upsert_channel_cred(ch, creds_json)
            ch['status'] = f"Connected 🟢 {_keys_label(len(ch.get('creds_list') or []))}"
            if owner_email:
                ch['owner_email'] = owner_email
            _oauth_channel_primary_client_id(ch)
            created = False
        try:
            clear_channel_cooldown(item['id'])
        except Exception:
            pass
        save_channels(database_channel)
    return created, item['snippet']['title']


def _analytics_insufficient_permissions(exc):
    """True bila error ini adalah 403 insufficientPermissions (token
    tidak punya scope yt-analytics.readonly). Dicek duck-typing
    supaya tidak perlu import HttpError baru."""
    try:
        text = str(exc).lower()
        if 'insufficientpermissions' not in text:
            return False
        resp = getattr(exc, 'resp', None)
        status = getattr(resp, 'status', None)
        return status in (None, 403)
    except Exception:
        return False


def _analytics_scope_state(cache, yt_id):
    """Status scope Analytics channel dari cache: True/False/None
    (None = belum pernah dicek / kadaluarsa > 1 jam)."""
    st = (cache.get('analytics_scope') or {}).get(yt_id) or {}
    if not st:
        return None
    if time.time() - float(st.get('at', 0)) >= VIDEO_STATS_CACHE_TTL:
        return None
    return bool(st.get('has_scope'))


def _set_analytics_scope_state(cache, yt_id, has_scope):
    cache.setdefault('analytics_scope', {})[yt_id] = {"has_scope": bool(has_scope), "at": time.time()}


def _query_analytics(channel, metrics, start_date, end_date, dimensions=None, filters=None):
    """Satu panggilan YouTube Analytics API reports.query dengan failover
    antar kredensial (pola sama seperti _build_youtube_client_for_channel).
    Kembalikan (rows, has_scope): rows [] bila sukses tanpa data,
    None bila gagal; has_scope True bila ada kredensial yang berhasil,
    False bila semua kredensial ditolak 403 insufficientPermissions,
    None bila gagal karena sebab lain (jaringan/dll — status scope
    tidak diubah). Fungsi ini murni: tidak membaca/menulis file cache,
    pemanggil yang menyimpan state scope.

    🔥 v1.3.47: guard lama mematikan SEMUA panggilan Analytics selama
    scope tidak ada di SCOPES (device flow menolaknya). Sekarang izin
    dicek per token tersimpan: lanjut bila ada token yang scopes-nya
    memuat yt-analytics.readonly, atau token tanpa info scopes (token
    lama — coba sekali, 403 ditangani di bawah)."""
    creds_list = channel.get('creds_list', [channel.get('creds_json')])
    if ANALYTICS_SCOPE not in SCOPES:
        _has, _unknown = False, False
        for _cs in creds_list:
            if not _cs:
                continue
            try:
                _sc = json.loads(_cs).get('scopes')
            except Exception:
                _sc = None
            if isinstance(_sc, list):
                if ANALYTICS_SCOPE in _sc:
                    _has = True
                    break
            else:
                _unknown = True
        if not _has and not _unknown:
            return None, None
    saw_403 = False
    for cred_str in creds_list:
        if not cred_str:
            continue
        try:
            creds = Credentials.from_authorized_user_info(json.loads(cred_str))
            if creds.expired and creds.refresh_token:
                creds.refresh(Request())
            client = build('youtubeAnalytics', 'v2', credentials=creds)
            kwargs = {"ids": "channel==MINE", "startDate": start_date,
                      "endDate": end_date, "metrics": metrics}
            if dimensions:
                kwargs["dimensions"] = dimensions
            if filters:
                kwargs["filters"] = filters
            res = client.reports().query(**kwargs).execute()
            return res.get('rows') or [], True
        except Exception as e:
            if _analytics_insufficient_permissions(e):
                saw_403 = True
            continue
    if saw_403:
        return None, False
    return None, None


def _fetch_watch_hours_real(channel, yt_id, cache):
    """Jam tayang ASLI dari YouTube Analytics API (estimatedMinutesWatched):
    365 hari (syarat monetisasi 4.000/8.000 jam) + 30 hari (kolom WATCH
    30D). Cache 1 jam di video_stats_cache. Kembalikan (hours_365,
    hours_30) atau (None, None) bila token channel belum punya scope
    Analytics (belum diaktifkan lewat login browser)."""
    wr = cache.setdefault('watch_real', {})
    st = wr.get(yt_id) or {}
    if st and time.time() - float(st.get('at', 0)) < VIDEO_STATS_CACHE_TTL:
        return st.get('h365'), st.get('h30')
    today = _wib_now().date()
    end = today.strftime('%Y-%m-%d')
    seen_scope = None

    def _hrs(days):
        nonlocal seen_scope
        start = (today - timedelta(days=days)).strftime('%Y-%m-%d')
        rows, has_scope = _query_analytics(channel, 'estimatedMinutesWatched', start, end)
        if has_scope is not None:
            seen_scope = has_scope
        if rows is None:
            return None
        total = sum(float(r[0]) for r in rows if r) if rows else 0.0
        return round(total / 60.0, 1)

    h365 = _hrs(365)
    h30 = _hrs(30)
    if seen_scope is not None:
        try:
            _set_analytics_scope_state(cache, yt_id, seen_scope)
        except Exception:
            pass
    wr[yt_id] = {'h365': h365, 'h30': h30, 'at': time.time()}
    try:
        _save_video_stats_cache(cache)
    except Exception:
        pass
    return h365, h30


def _fetch_video_subs(channel, yt_id, video_ids):
    """Subs per video (subscribersGained, bisa negatif = net) via
    Analytics API, chunk 50 id. Kembalikan ({video_id: subs}, has_scope).
    Video tanpa baris data tidak ada di dict (subs None, bukan 0)."""
    out = {}
    has_scope = None
    today = _wib_now().strftime("%Y-%m-%d")
    for i in range(0, len(video_ids), 50):
        chunk = video_ids[i:i + 50]
        rows, scope = _query_analytics(channel, 'subscribersGained',
                                       ANALYTICS_EARLIEST_DATE, today,
                                       dimensions='video',
                                       filters='video==' + ','.join(chunk))
        if scope is True:
            has_scope = True
        elif scope is False and has_scope is None:
            has_scope = False
        if not rows:
            continue
        for row in rows:
            try:
                out[row[0]] = int(row[1])
            except Exception:
                continue
    return out, has_scope


def _fetch_channel_rpm(channel, yt_id, cache):
    """RPM channel 30 hari terakhir dari Analytics API (cache 1 jam di
    video_stats_cache.json, kunci 'analytics_rpm').
    Kembalikan (rpm_atau_None, has_scope). Channel belum monetisasi /
    belum ada data → rpm None (dicache 1 jam supaya tidak menghantam
    API tiap buka halaman). Scope yang ditolak 403 TIDAK dicache sebagai
    'tidak ada RPM' — dicek ulang setelah TTL lewat."""
    rpm_cache = cache.setdefault('analytics_rpm', {})
    ent = rpm_cache.get(yt_id) or {}
    if ent and ent.get('rpm') is not None and (time.time() - float(ent.get('at', 0)) < VIDEO_STATS_CACHE_TTL):
        return float(ent['rpm']), _analytics_scope_state(cache, yt_id)
    end = _wib_now().strftime("%Y-%m-%d")
    start = (_wib_now() - timedelta(days=30)).strftime("%Y-%m-%d")
    rows, scope = _query_analytics(channel, 'rpm', start, end)
    if rows:
        try:
            rpm = float(rows[0][0])
        except Exception:
            rpm = None
        if rpm is not None and rpm > 0:
            rpm_cache[yt_id] = {"rpm": rpm, "at": time.time()}
            if scope is not None:
                _set_analytics_scope_state(cache, yt_id, scope)
            _save_video_stats_cache(cache)
            return rpm, True
    if scope is False:
        _set_analytics_scope_state(cache, yt_id, False)
        _save_video_stats_cache(cache)
        return None, False
    rpm_cache[yt_id] = {"rpm": None, "at": time.time()}
    if scope is not None:
        _set_analytics_scope_state(cache, yt_id, scope)
    _save_video_stats_cache(cache)
    return None, scope


# ==========================================
# 🔁 FASE 7 LINGKARAN ANALITIK OTOMATIS
# ==========================================
# Membaca statistik per video (Fase 4: cache + snapshot harian) lalu
# menaikkan bobot judul bank yang menang dan menurunkan yang sepi — otomatis,
# tapi dengan rem yang jelas:
#  - bobot tidak pernah 0 (min 0.3, maks 2.0) — judul yang sepi tetap
#    sesekali terpilih supaya bisa pulih; judul TIDAK PERNAH dihapus bank;
#  - hanya batch BARU yang terpengaruh (judul dipilih di panel saat
#    generateOneTitle memakai bobot dari /api/get_channels);
#  - semua penyesuaian dicatat di formula_log channel dan bisa di-reset
#    satu tombol (/api/reset_formula_weights).
# Skor per video = views/hari sejak tayang (video baru tidak langsung kalah
# dengan video lama) + bonus ringan likes/komentar. Formula = entri bank
# judul persis (judul di riwayat adalah entri bank yang dipilih saat batch).
# Durasi target hanya dilaporkan sebagai wawasan, tidak diubah otomatis
# (tidak ada data tema/lagu per video di riwayat — tidak dikarang).
FORMULA_MIN_VIDEOS = 3           # min video ternilai per channel sebelum tune
FORMULA_MIN_AGE_DAYS = 1         # video yang tayang hari ini belum dinilai
FORMULA_WEIGHT_MIN = 0.3
FORMULA_WEIGHT_MAX = 2.0
FORMULA_WEIGHT_CHANGE_MIN = 0.1  # perubahan di bawah ini dianggap noise
FORMULA_SIGNIFICANT = 0.5        # perubahan >= ini memicu notifikasi Telegram
FORMULA_LOG_MAX = 20

def _formula_pub_date(task, stats):
    """Tanggal tayang video (WIB): published_at API, lalu finished_date, lalu publish_date."""
    return (stats or {}).get('published_at', '')[:10] or task.get('finished_date') or (task.get('publish_date') or '')[:10]

def _formula_scored_videos():
    """Video tayang yang punya statistik cache + umur >= 1 hari, per yt_id.
    Kembalikan {yt_id: [{video_id, title, views, likes, comments, age_days,
    views_per_day, score, duration_min, publish_date}]}."""
    cache = _load_video_stats_cache()
    cached = cache.get('videos', {})
    today = _wib_now().date()
    out = {}
    for t in history_tasks:
        vid = _history_task_video_id(t)
        if not vid:
            continue
        if "Tayang" not in str(t.get('status', '')) and not t.get('video_id'):
            continue
        yt_id = _history_task_yt_id(t)
        if not yt_id:
            continue
        st = cached.get(vid)
        if not st or st.get('missing'):
            continue
        pub = _formula_pub_date(t, st)
        try:
            age = (today - datetime.strptime(pub, "%Y-%m-%d").date()).days
        except Exception:
            continue
        if age < FORMULA_MIN_AGE_DAYS:
            continue
        views = int(st.get('views', 0))
        likes = int(st.get('likes', 0))
        comments = int(st.get('comments', 0))
        vpd = views / age
        score = vpd + 0.1 * (likes + 2 * comments) / age
        dur_min = None
        try:
            dur_min = round(float((t.get('blueprint') or {}).get('target_duration_hours', 0)) * 60)
        except Exception:
            dur_min = None
        out.setdefault(yt_id, []).append({
            "video_id": vid, "title": t.get('title', '?'), "views": views,
            "likes": likes, "comments": comments, "age_days": age,
            "views_per_day": round(vpd, 1), "score": round(score, 2),
            "duration_min": dur_min, "publish_date": pub,
        })
    return out

def _formula_duration_bucket(dur_min):
    if not dur_min:
        return None
    if dur_min < 70:
        return "60–69 mnt"
    if dur_min < 80:
        return "70–79 mnt"
    return "80+ mnt"

def _compute_formula_analytics(yt_filter=None):
    """Hitung skor formula per channel dari data yang ada (tanpa efek samping).
    Tidak memanggil YouTube API — hanya memakai cache Fase 4 + riwayat."""
    import statistics
    scored = _formula_scored_videos()
    channels_out = []
    for c in database_channel:
        yt_id = c.get('yt_id')
        if not yt_id:
            continue
        if yt_filter and yt_filter != 'ALL' and yt_id != yt_filter:
            continue
        vids = scored.get(yt_id, [])
        weights = dict(c.get('title_weights') or {})
        by_title = {}
        for v in vids:
            by_title.setdefault(v['title'], []).append(v)
        formulas = []
        for title, vs in by_title.items():
            mean_score = sum(v['score'] for v in vs) / len(vs)
            mean_vpd = sum(v['views_per_day'] for v in vs) / len(vs)
            formulas.append({
                "title": title, "videos": len(vs),
                "avg_score": round(mean_score, 2),
                "avg_views_per_day": round(mean_vpd, 1),
                "total_views": sum(v['views'] for v in vs),
                "weight": round(float(weights.get(title, 1.0)), 2),
            })
        median = statistics.median([f['avg_score'] for f in formulas]) if formulas else 0
        for f in formulas:
            ratio = (f['avg_score'] / median) if median and median > 0 else 1.0
            f['target_weight'] = round(max(FORMULA_WEIGHT_MIN, min(FORMULA_WEIGHT_MAX, ratio)), 2)
        buckets = {}
        for v in vids:
            b = _formula_duration_bucket(v.get('duration_min'))
            if b:
                buckets.setdefault(b, []).append(v)
        dur_out = [{"bucket": b, "videos": len(vs),
                    "avg_views_per_day": round(sum(x['views_per_day'] for x in vs) / len(vs), 1)}
                   for b, vs in sorted(buckets.items())]
        formulas_sorted = sorted(formulas, key=lambda f: f['avg_score'], reverse=True)
        channels_out.append({
            "yt_id": yt_id, "name": c.get('name', yt_id),
            "sufficient": len(vids) >= FORMULA_MIN_VIDEOS,
            "videos_scored": len(vids),
            "min_videos": FORMULA_MIN_VIDEOS,
            "median_score": round(median, 2) if median else 0,
            "formulas": formulas_sorted,
            "best": formulas_sorted[0] if formulas_sorted else None,
            "worst": formulas_sorted[-1] if formulas_sorted else None,
            "duration_buckets": dur_out,
            "tuned_at": c.get('formula_tuned_at', ''),
            "weights": {t: round(float(w), 2) for t, w in weights.items()},
            "log": list(c.get('formula_log') or [])[:5],
        })
    return {"status": "success", "channels": channels_out,
            "min_videos": FORMULA_MIN_VIDEOS,
            "weight_min": FORMULA_WEIGHT_MIN, "weight_max": FORMULA_WEIGHT_MAX}

def _apply_formula_tune(yt_id):
    """Terapkan bobot baru untuk satu channel. Kembalikan ringkasan perubahan.
    Tidak menghapus judul dari bank; bobot judul yang sudah tidak ada di bank
    dibersihkan. Perubahan < FORMULA_WEIGHT_CHANGE_MIN dianggap noise."""
    ana = _compute_formula_analytics(yt_id)
    ch = (ana.get('channels') or [None])[0]
    if not ch:
        return {"yt_id": yt_id, "status": "error", "message": "Channel tidak ditemukan.", "changes": []}
    if not ch['sufficient']:
        return {"yt_id": yt_id, "status": "not_enough_data",
                "message": f"Data belum cukup ({ch['videos_scored']}/{FORMULA_MIN_VIDEOS} video ternilai) — bobot belum diubah.",
                "changes": []}
    channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if channel is None:
        return {"yt_id": yt_id, "status": "error", "message": "Channel tidak ditemukan.", "changes": []}
    bank = list(channel.get('title_bank') or [])
    weights = {t: w for t, w in dict(channel.get('title_weights') or {}).items() if t in bank}
    changes = []
    for f in ch['formulas']:
        old = round(float(weights.get(f['title'], 1.0)), 2)
        new = f['target_weight']
        if abs(new - old) >= FORMULA_WEIGHT_CHANGE_MIN:
            weights[f['title']] = new
            changes.append({"title": f['title'], "old": old, "new": new,
                            "avg_score": f['avg_score'], "videos": f['videos']})
    if changes:
        today_str = _wib_now().strftime("%Y-%m-%d %H:%M")
        log = list(channel.get('formula_log') or [])
        for chg in changes:
            log.insert(0, {"date": today_str, "title": chg['title'], "old": chg['old'],
                           "new": chg['new'], "avg_score": chg['avg_score']})
        channel['formula_log'] = log[:FORMULA_LOG_MAX]
    channel['title_weights'] = weights
    channel['formula_tuned_at'] = _wib_now().strftime("%Y-%m-%d %H:%M")
    save_channels(database_channel)
    return {"yt_id": yt_id, "status": "success", "changes": changes,
            "tuned_at": channel['formula_tuned_at'],
            "message": f"{len(changes)} judul disesuaikan." if changes else "Bobot sudah sesuai skor — tidak ada perubahan."}

def _tune_all_formulas(notify=True):
    """Tune semua channel. Kalau notify dan ada perubahan signifikan,
    kirim 1 pesan Telegram ringkas per channel (lewat buffer notify_event,
    maks 1 menit sekali — tidak spam)."""
    results = []
    for c in list(database_channel):
        yt_id = c.get('yt_id')
        if not yt_id:
            continue
        r = _apply_formula_tune(yt_id)
        results.append(r)
        if notify and r.get('status') == 'success':
            sig = [x for x in r.get('changes', []) if abs(x['new'] - x['old']) >= FORMULA_SIGNIFICANT]
            if sig:
                best = max(sig, key=lambda x: x['new'])
                ch_name = c.get('name', yt_id)
                # 🔥 v1.3.75: tiap notif WAJIB bawa tombol saran (prinsip
                # 20 Nov 2026) — Bos tinggal tap, tidak cuma baca.
                notify_event("🔁", f"Lingkaran Analitik — {ch_name}",
                    f"Bos, lingkaran analitik udah jalan nih 😎 Judul '{best['title'][:60]}' lagi naik daun — bobotnya saya naikin ke {best['new']} biar makin sering kepilih 💪 Yang sepi saya turunin dikit, tapi tetap saya kasih kesempatan kok. Total {len(r['changes'])} judul disesuaikan hari ini.",
                    reply_markup={"inline_keyboard": [
                        [{"text": "🎵 Buat Playlist", "callback_data": f"stok:buat:{yt_id}"},
                         {"text": "📊 Statistik", "callback_data": "rep:statistik"}]]})
    return results

_formula_tune_last_date = ""
def _formula_tune_loop():
    """Daemon: tune semua channel 1x sehari (cek tiap jam)."""
    global _formula_tune_last_date
    while True:
        time.sleep(3600)
        try:
            today = _wib_now().strftime("%Y-%m-%d")
            if today == _formula_tune_last_date:
                continue
            _formula_tune_last_date = today
            _tune_all_formulas(notify=True)
        except Exception as e:
            print(f"[Fase 7] tune loop error: {e}", flush=True)
threading.Thread(target=_formula_tune_loop, daemon=True).start()

@app.route('/api/get_formula_analytics')
def get_formula_analytics():
    """🔥 FASE 7: skor formula + bobot per channel (butuh login panel —
    route ini TIDAK masuk allowed_routes, jadi check_security mewajibkan
    login). Tidak memanggil YouTube API; memakai cache Fase 4 + riwayat."""
    yt_id = request.args.get('yt_id') or 'ALL'
    return jsonify(_compute_formula_analytics(yt_id))

@app.route('/api/run_formula_tune', methods=['POST'])
def run_formula_tune():
    """🔥 FASE 7: hitung ulang skor & terapkan bobot sekarang (butuh login).
    Body opsional {yt_id} — tanpa yt_id, tune semua channel. Notifikasi
    Telegram TIDAK dikirim di sini (tune manual dari panel); notifikasi
    hanya dari loop harian."""
    data = request.get_json(silent=True) or {}
    yt_id = data.get('yt_id')
    if yt_id:
        return jsonify({"status": "success", "results": [_apply_formula_tune(yt_id)]})
    return jsonify({"status": "success", "results": _tune_all_formulas(notify=False)})

@app.route('/api/reset_formula_weights', methods=['POST'])
def reset_formula_weights():
    """🔥 FASE 7: kembalikan semua bobot satu channel ke 1,0 (butuh login).
    Judul tidak dihapus — hanya bobot yang direset. Dicatat di formula_log."""
    data = request.get_json(silent=True) or {}
    yt_id = data.get('yt_id')
    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id wajib diisi."}), 400
    channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if channel is None:
        return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
    channel['title_weights'] = {}
    today_str = _wib_now().strftime("%Y-%m-%d %H:%M")
    log = list(channel.get('formula_log') or [])
    log.insert(0, {"date": today_str, "title": "(reset semua bobot)", "old": "-", "new": 1.0, "avg_score": 0})
    channel['formula_log'] = log[:FORMULA_LOG_MAX]
    channel['formula_tuned_at'] = today_str
    save_channels(database_channel)
    return jsonify({"status": "success", "message": "Semua bobot judul direset ke 1,0."})

# ==========================================
# 💰 FASE 9 — SKALA VPS KEDUA / DASHBOARD MONETISASI
# ==========================================
# Dua bagian, keduanya PERSIAPAN yang aman di satu VPS:
#  A. Dashboard monetisasi dari data yang ADA — views dari cache Fase 4 +
#     riwayat tayang. JUJUR SOAL BATAS: YouTube Data API TIDAK menyediakan
#     data penghasilan; itu hanya ada di YouTube Analytics API dan butuh
#     channel yang sudah monetisasi. Karena itu estimasi uang HANYA dihitung
#     bila owner mengisi sendiri nilai RPM (USD per 1000 views) per channel
#     di Settings. RPM kosong = TIDAK ADA angka estimasi sama sekali (bukan
#     0 yang menyesatkan). Semua angka uang diberi label jelas "estimasi
#     dari RPM yang diisi sendiri".
#  B. Persiapan multi-node — konfigurasi node (node_id/role) + field
#     assigned_node pada task + endpoint health /api/node_status dan
#     registrasi node /api/operator/register_node. Default single-node
#     (multi_node_enabled=False) berperilaku PERSIS seperti sebelum Fase 9:
#     worker mengambil semua task tanpa peduli pemilik. Sinkronisasi
#     jaringan sungguhan (DB bersama, antrean terdistribusi) TIDAK
#     diimplementasikan — itu butuh VPS kedua + storage bersama; modul ini
#     hanya menyiapkan kontraknya (lihat _NODE_CONTRACT di bawah).
_NODE_CONTRACT = """KONTRAK MULTI-NODE (siap dipakai saat VPS kedua ada):
1. VPS-2 install repo yang sama (git clone + install.sh), lalu di
   config.json-nya set: node_role='worker', node_id berbeda (mis.
   'node-worker-1'), multi_node_enabled=true; di VPS-1 (primary) set
   multi_node_enabled=true juga. JANGAN samakan node_id antar node.
2. Worker mendaftarkan diri ke primary: POST /api/operator/register_node
   {key: operator_key, node_id, role: 'worker'} — muncul di kartu
   NODE & SKALA dengan status online/offline (tidak lapor > 5 menit).
3. Primary membagi tugas dengan mengisi assigned_node pada task/blueprint
   (field sudah dicap otomatis oleh queue_put). Worker HANYA mengambil
   task yang assigned_node-nya == node_id sendiri (guard di
   background_worker); task tanpa assigned_node (legacy) boleh diambil
   node mana saja. Single-node (multi_node_enabled=false) mengabaikan
   guard ini sepenuhnya.
4. YANG MASIH BUTUH VPS KEDUA SUNGGUHAN (belum ada di kode ini):
   - DB bersama: channels_db.json / tasks_db.json / video_stats_cache.json
     harus di satu storage bersama (mis. NFS) atau direplikasi — hari ini
     tiap node punya file lokal sendiri, jadi tanpa itu worker tidak
     melihat antrean primary.
   - Antrean terdistribusi: render_queue hari ini in-memory per proses;
     butuh broker (mis. Redis/RabbitMQ) atau API tarik-tugas antar node.
   - Aset galeri bersama (uploads/) dan kredensial channel di worker.
   Sampai 4 hal itu ada, mode multi-node TIDAK BOLEH diaktifkan —
   guard di atas hanya memastikan tidak ada task yang dicuri/dikerjakan
   ganda bila diaktifkan nanti."""
NODE_FILE = os.path.join(BASE_DIR, 'nodes.json')  # registry node dikenal (di-gitignore)
NODE_OFFLINE_AFTER_SEC = 300  # node tidak lapor > 5 menit = offline
RPM_MIN_USD = 0.0
RPM_MAX_USD = 100.0  # batas kewarasan: RPM > $100/1000 views tidak realistis

def _node_id():
    """ID node ini (dibuat sekali saat start, menetap di config.json)."""
    return str(bot_config.get('node_id') or 'node-local')

def _node_role():
    """'primary' (default) atau 'worker' — dibaca dari config.json."""
    role = str(bot_config.get('node_role') or 'primary').lower()
    return role if role in ('primary', 'worker') else 'primary'

def _multi_node_enabled():
    """True hanya kalau owner mengaktifkan di config (multi_node_enabled).
    Default False = single-node, worker mengambil semua task."""
    return bool(bot_config.get('multi_node_enabled', False))

def _load_known_nodes():
    try:
        if os.path.exists(NODE_FILE):
            with open(NODE_FILE, 'r') as f:
                d = json.load(f)
            if isinstance(d, list):
                return d
    except Exception:
        pass
    return []

def _save_known_nodes(nodes):
    try:
        tmp = NODE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(nodes, f)
        os.replace(tmp, NODE_FILE)
    except Exception:
        pass

def _touch_known_node(node_id, role, host=None):
    """Catat/perbarui last_seen satu node di registry. Dipakai node_status
    (untuk node sendiri) dan register_node (untuk node remote). Entri yang
    tidak lapor > 7 hari dibuang supaya registry tidak membengkak."""
    nodes = _load_known_nodes()
    now = time.time()
    entry = next((n for n in nodes if n.get('node_id') == node_id), None)
    if entry is None:
        entry = {"node_id": node_id, "role": role}
        nodes.append(entry)
    entry["role"] = role
    entry["last_seen"] = now
    if host:
        entry["host"] = str(host)[:80]
    nodes = [n for n in nodes if now - float(n.get('last_seen', 0)) < 7 * 24 * 3600]
    _save_known_nodes(nodes)
    return entry

def _dir_size_bytes(path):
    total = 0
    try:
        for root, _dirs, files in os.walk(path):
            for f in files:
                try:
                    total += os.path.getsize(os.path.join(root, f))
                except Exception:
                    pass
    except Exception:
        pass
    return total

def _disk_usage_info():
    """Ruang disk + ukuran folder besar (GB). Dipakai kartu Monetisasi &
    endpoint node_status."""
    try:
        du = shutil.disk_usage(BASE_DIR)
        gb = 1024 ** 3
        return {"total_gb": round(du.total / gb, 1), "used_gb": round(du.used / gb, 1),
                "free_gb": round(du.free / gb, 1),
                "free_pct": round(du.free / max(1, du.total) * 100, 1),
                "uploads_gb": round(_dir_size_bytes(BASE_UPLOAD) / gb, 2),
                "downloads_gb": round(_dir_size_bytes(os.path.join(BASE_DIR, 'downloads')) / gb, 2),
                "static_gb": round(_dir_size_bytes(os.path.join(BASE_DIR, 'static')) / gb, 2)}
    except Exception:
        return {"total_gb": 0, "used_gb": 0, "free_gb": 0, "free_pct": 0,
                "uploads_gb": 0, "downloads_gb": 0, "static_gb": 0}

def _monetization_task_hours(t):
    """Durasi video tayang dalam jam: kompilasi dari blueprint
    target_duration_hours; Shorts Muse dari shorts_duration_sec."""
    bp = t.get('blueprint') or {}
    try:
        kind = t.get('kind') or bp.get('kind')
        if kind == SHORTS_MUSE_KIND:
            return float(t.get('shorts_duration_sec') or bp.get('shorts_duration_sec') or 0) / 3600.0
        return float(bp.get('target_duration_hours') or t.get('target_duration_hours') or 0)
    except Exception:
        return 0.0

def _monetization_data():
    """Hitung dashboard monetisasi dari cache Fase 4 + riwayat tayang.
    🔥 FASE 10: RPM diutamakan dari YouTube Analytics API (otomatis,
    cache 1 jam) — hanya untuk channel yang punya video tayang; RPM
    manual yang diisi owner tetap jadi fallback bila API tidak
    mengembalikan RPM (token lama tanpa scope / channel belum
    monetisasi). Estimasi uang HANYA bila ada RPM (> 0); tanpa RPM,
    estimasi = None (tidak ditampilkan)."""
    cache = _load_video_stats_cache()
    cached = cache.get('videos', {})
    now_wib = _wib_now()
    today = now_wib.date()
    name_by_yt = {c.get('yt_id'): c.get('name', c.get('yt_id')) for c in database_channel}

    def _in_days(pub, days):
        try:
            d = datetime.strptime(pub, "%Y-%m-%d").date()
            return 0 <= (today - d).days < days
        except Exception:
            return False

    per_channel = {}
    for t in history_tasks:
        vid = _history_task_video_id(t)
        if not vid:
            continue
        if not t.get('video_id') and "Tayang" not in str(t.get('status', '')):
            continue
        yt_id = _history_task_yt_id(t)
        if not yt_id:
            continue
        st = cached.get(vid) or {}
        views = 0 if st.get('missing') else int(st.get('views', 0))
        pub = _formula_pub_date(t, st)
        e = per_channel.setdefault(yt_id, {"yt_id": yt_id, "name": name_by_yt.get(yt_id, yt_id),
            "videos": 0, "total_views": 0, "views_7d": 0, "views_30d": 0,
            "total_hours": 0.0, "best": None})
        e["videos"] += 1
        e["total_views"] += views
        if _in_days(pub, 7):
            e["views_7d"] += views
        if _in_days(pub, 30):
            e["views_30d"] += views
        e["total_hours"] += _monetization_task_hours(t)
        if e["best"] is None or views > e["best"]["views"]:
            e["best"] = {"title": t.get('title', '?'), "views": views, "video_id": vid,
                         "url": f"https://youtu.be/{vid}"}

    # 🔥 FASE 10: RPM efektif per channel — API Analytics dulu (hanya
    # channel yang punya video tayang), lalu fallback RPM manual owner.
    rpm_by_yt = {}
    rpm_source_by_yt = {}
    for yt_id in per_channel:
        rpm = None
        source = None
        ch = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if ch is not None:
            try:
                api_rpm, _scope = _fetch_channel_rpm(ch, yt_id, cache)
            except Exception:
                api_rpm = None
            if api_rpm and api_rpm > 0:
                rpm, source = api_rpm, 'api'
        if rpm is None and ch is not None:
            try:
                manual = float(ch.get('rpm_usd') or 0)
            except Exception:
                manual = 0.0
            if manual > 0:
                rpm, source = manual, 'manual'
        rpm_by_yt[yt_id] = rpm or 0.0
        rpm_source_by_yt[yt_id] = source
    channels_out = []
    totals = {"videos": 0, "total_views": 0, "views_7d": 0, "views_30d": 0, "total_hours": 0.0,
              "est_30d_usd": None, "est_total_usd": None, "est_projection_30d_usd": None,
              "any_rpm": False}
    for yt_id in sorted(per_channel, key=lambda y: per_channel[y]["total_views"], reverse=True):
        e = per_channel[yt_id]
        rpm = rpm_by_yt.get(yt_id, 0.0)
        e["rpm_usd"] = rpm if rpm > 0 else None
        e["rpm_source"] = rpm_source_by_yt.get(yt_id) if rpm > 0 else None  # 'api' / 'manual' / None
        e["total_hours"] = round(e["total_hours"], 2)
        if rpm > 0:
            totals["any_rpm"] = True
            e["est_30d_usd"] = round(e["views_30d"] / 1000.0 * rpm, 2)
            e["est_total_usd"] = round(e["total_views"] / 1000.0 * rpm, 2)
            e["est_projection_30d_usd"] = e["est_30d_usd"]
        else:
            e["est_30d_usd"] = None
            e["est_total_usd"] = None
            e["est_projection_30d_usd"] = None
        totals["videos"] += e["videos"]
        totals["total_views"] += e["total_views"]
        totals["views_7d"] += e["views_7d"]
        totals["views_30d"] += e["views_30d"]
        totals["total_hours"] += e["total_hours"]
        channels_out.append(e)
    totals["total_hours"] = round(totals["total_hours"], 2)
    if totals["any_rpm"]:
        totals["est_30d_usd"] = round(sum(c["est_30d_usd"] or 0 for c in channels_out), 2)
        totals["est_total_usd"] = round(sum(c["est_total_usd"] or 0 for c in channels_out), 2)
        totals["est_projection_30d_usd"] = totals["est_30d_usd"]
    return {"channels": channels_out, "totals": totals, "disk": _disk_usage_info(),
            "currency": "USD",
            "honesty_note": "Estimasi penghasilan dihitung dari RPM (USD per 1000 views) — diutamakan dari YouTube Analytics API (otomatis, bila token channel punya scope Analytics), atau dari RPM yang diisi sendiri per channel di Settings bila API tidak mengembalikan RPM. YouTube Data API tidak menyediakan data penghasilan; data asli butuh YouTube Analytics API + channel yang sudah monetisasi. Channel tanpa RPM tidak menampilkan estimasi sama sekali.",
            "rpm_note": "RPM 'API' = diambil otomatis dari YouTube Analytics (butuh channel ditaut ulang dengan scope Analytics bila token lama). RPM 'Manual' = yang diisi sendiri di Settings, dipakai sebagai cadangan bila API tidak mengembalikan RPM (mis. channel belum monetisasi). Isi RPM manual per channel di Settings (kartu channel) bila ingin estimasi tanpa menunggu API."}

@app.route('/api/get_monetization')
def get_monetization():
    """🔥 FASE 9: dashboard monetisasi (butuh login panel — route ini TIDAK
    masuk allowed_routes, jadi check_security mewajibkan login). Tidak
    memanggil YouTube API; memakai cache Fase 4 + riwayat tayang."""
    return jsonify(_monetization_data())

@app.route('/api/set_channel_rpm', methods=['POST'])
def set_channel_rpm():
    """🔥 FASE 9: isi/ubah/hapus RPM (USD per 1000 views) satu channel
    (butuh login). RPM kosong/null = hapus → estimasi penghasilan channel
    itu dimatikan. Validasi 0–100 USD."""
    data = request.get_json(silent=True) or {}
    yt_id = data.get('yt_id')
    raw = data.get('rpm_usd')
    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id wajib diisi."}), 400
    if raw is None or raw == '':
        rpm = None
    else:
        try:
            rpm = float(raw)
        except Exception:
            return jsonify({"status": "error", "message": "RPM harus angka (USD per 1000 views)."}), 400
        if rpm < RPM_MIN_USD or rpm > RPM_MAX_USD:
            return jsonify({"status": "error", "message": "RPM harus antara $0 dan $100."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        if rpm is None:
            target.pop('rpm_usd', None)
        else:
            target['rpm_usd'] = rpm
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan RPM {yt_id}: {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    if rpm is None:
        return jsonify({"status": "success", "rpm_usd": None,
                        "message": f"RPM channel {name} dihapus — estimasi penghasilan dimatikan."})
    return jsonify({"status": "success", "rpm_usd": rpm,
                    "message": f"RPM channel {name} diset ${rpm} per 1000 views."})

@app.route('/api/node_status')
def node_status():
    """🔥 FASE 9: health node ini + node lain yang terdaftar (butuh login
    panel — route ini TIDAK masuk allowed_routes). Node yang tidak lapor
    > 5 menit berstatus offline. Mode single-node (default) hanya
    menampilkan node ini."""
    _touch_known_node(_node_id(), _node_role())
    stats = get_system_stats()
    disk = _disk_usage_info()
    now = time.time()
    nodes_out = []
    for n in _load_known_nodes():
        last = float(n.get('last_seen', 0))
        nodes_out.append({"node_id": n.get('node_id'), "role": n.get('role'),
                          "host": n.get('host', ''),
                          "last_seen_ago_sec": int(now - last) if last else None,
                          "status": "online" if last and now - last < NODE_OFFLINE_AFTER_SEC else "offline"})
    return jsonify({"status": "success",
                    "self": {"node_id": _node_id(), "role": _node_role(),
                             "multi_node_enabled": _multi_node_enabled(),
                             "mode": "multi-node" if _multi_node_enabled() else "single-node"},
                    "queue": render_queue.qsize(),
                    "active_tasks": len(active_tasks),
                    "history_tasks": len(history_tasks),
                    "channels": len(database_channel),
                    "cpu": stats.get('cpu'), "ram_pct": stats.get('ram_pct'),
                    "ram_used_gb": stats.get('ram_used'), "ram_total_gb": stats.get('ram_total'),
                    "disk": disk,
                    "nodes": nodes_out,
                    "contract_note": "Kontrak multi-node (lihat _NODE_CONTRACT di app.py): task membawa assigned_node; worker hanya mengambil task miliknya saat multi_node_enabled=True. DB bersama + antrean terdistribusi masih butuh VPS kedua + storage bersama."})

@app.route('/api/operator/register_node', methods=['POST'])
def operator_register_node():
    """🔥 FASE 9: node worker mendaftarkan diri ke primary (machine-to-machine).
    Keamanan: wajib operator_key (HMAC) seperti endpoint operator lain;
    route ini masuk allowed_routes di check_security."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    node_id = str(data.get('node_id') or '').strip()
    role = str(data.get('role') or 'worker').lower()
    if not node_id or len(node_id) > 64 or not re.match(r'^[A-Za-z0-9][A-Za-z0-9_-]*$', node_id):
        return jsonify({"status": "error", "message": "node_id tidak valid (huruf/angka/dash, maks 64 karakter)."}), 400
    if role not in ('primary', 'worker'):
        return jsonify({"status": "error", "message": "role harus 'primary' atau 'worker'."}), 400
    _touch_known_node(node_id, role, host=request.remote_addr)
    return jsonify({"status": "success", "message": f"Node {node_id} ({role}) terdaftar.",
                    "primary_node_id": _node_id()})

@app.route('/api/get_schedule')
def get_schedule(): return jsonify({"active": active_tasks, "history": history_tasks})

@app.route('/api/recover_failed_tasks', methods=['POST'])
def recover_failed_tasks():
    # 🔥 BARU: memulihkan task yang digagalkan watchdog (belum diproses)
    # dari history kembali ke antrean aktif — jadwal tayang & judul tetap utuh.
    # 🔒 #12: POST-only (sebelumnya GET juga bisa mengubah state) + CSRF
    # via check_security. 🔒 #15: kalau antrean penuh, task dikembalikan
    # ke riwayat (tidak hilang) dan endpoint mengembalikan 429.
    global active_tasks, history_tasks
    recovered = []
    moved = []
    with db_lock:
        still_needed = []
        for t in history_tasks:
            bp = t.get('blueprint')
            if bp and "Stuck" in t.get('status', ''):
                # 🔥 FIX: Pakaikan pelindung "Menunggu" saat dipulihkan
                t['status'] = "Menunggu Giliran (Queue) ⏳ (Dipulihkan)"
                t['updated_at'] = time.time()
                active_tasks.append(t)
                moved.append(t)
            else:
                still_needed.append(t)
        history_tasks = still_needed
        active_tasks.sort(key=lambda x: x.get('blueprint', {}).get('publish_date', ''))
        save_tasks_db()
    queued, failed = [], []
    for t in moved:
        try:
            queue_put(t["blueprint"])   # masuk mesin render beneran
            queued.append(t)
            recovered.append(t.get('title', '?')[:60])
        except _QueueFullError:
            failed.append(t)
    if failed:
        # Antrean penuh: kembalikan task ke riwayat supaya tidak hilang.
        with db_lock:
            for t in failed:
                if t in active_tasks:
                    active_tasks.remove(t)
                t['status'] = "Gagal ❌ (Stuck — antrean penuh saat dipulihkan)"
                history_tasks.insert(0, t)
            save_tasks_db()
        return jsonify({"status": "error",
                        "message": f"Antrean render penuh (maks {QUEUE_MAX_SIZE} task). "
                                   f"{len(queued)} task dipulihkan, {len(failed)} task dikembalikan ke riwayat.",
                        "recovered": len(queued), "titles": recovered}), 429
    if recovered:
        send_tg_message("🔄 *Bos, saya pulihkan task yang sempat macet nih* 💪\n" + "\n".join("• " + _tg_escape(r) for r in recovered[:10]) +
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
    # 🔥 FASE 8: ambang ukuran untuk Shorts diskalakan dengan durasi —
    # short 5 detik gambar statis + tanpa musik yang sah cuma ~50 KB,
    # jadi ambang 100 KB milik video 60–80 menit akan menolaknya sia-sia.
    if task.get('kind') == SHORTS_MUSE_KIND:
        try:
            _tgt = float(task.get('shorts_duration_sec') or 30)
        except Exception:
            _tgt = 30.0
        min_bytes = max(10_000, int(_tgt * 5_000))
    else:
        min_bytes = QC_MIN_FILE_BYTES
    if size < min_bytes:
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
    # 🔥 FASE 8: Shorts Muse targetnya detik (15/30/60), bukan jam —
    # toleransi 3 detik (atau 10%) supaya QC tetap bermakna untuk video
    # pendek (toleransi 120 detik milik Factory akan meloloskan apa saja).
    if task.get('kind') == SHORTS_MUSE_KIND:
        try:
            target = float(task.get('shorts_duration_sec') or 30)
        except Exception:
            target = 30.0
        tol = max(3.0, 0.10 * target)
    else:
        try:
            target = float(task.get('target_duration_hours', 1)) * 3600
        except Exception:
            target = 0.0
        tol = max(QC_DURATION_TOLERANCE_SEC, 0.05 * target)
    if dur <= 0:
        failures.append("durasi video tidak terbaca (0 detik)")
    elif target > 0:
        if abs(dur - target) > tol:
            if target < 120:
                failures.append(
                    f"durasi {dur:.1f} dtk, target {target:g} dtk "
                    f"(selisih > {tol:g} dtk)")
            else:
                failures.append(
                    f"durasi {int(dur // 60)} mnt, target {int(target // 60)} mnt "
                    f"(selisih > {int(tol // 60)} mnt)")
    # --- (c) audio ada & tidak hening ---
    # 🔥 v1.2.6: Shorts WAJIB bersuara (voiceover + musik) — pengecualian
    # "tanpa musik" (Fase 8) DIHAPUS; Short bisu tidak boleh lolos lagi
    # ke YouTube.
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


def _explain_failure_reason(reason, title=None):
    """🔥 v1.2.17: petakan penyebab gagal YouTube → (penyebab sederhana,
    langkah perbaikan). Selain yang dipetakan → tampilkan pesan asli yang
    dipotong (jangan ditebak)."""
    r = (reason or '')
    low = r.lower()
    if 'invalidtitle' in low:
        t = title or ''
        parts = []
        if len(t) > 100:
            parts.append(f"judulnya kepanjangan ({len(t)} huruf, batas YouTube 100)")
        if any((0x1F000 <= ord(c) <= 0x1FAFF) or (0x2600 <= ord(c) <= 0x27BF)
               or ord(c) == 0xFE0F for c in t):
            parts.append("ada emoji")
        cause = " dan ".join(parts) if parts else "judulnya ada karakter yang ditolak YouTube"
        return (cause + " — YouTube menolaknya",
                "Pastikan panel sudah di-Update ke versi terbaru — judul otomatis dibersihkan & dipotong sebelum upload.")
    if 'invaliddescription' in low:
        return ("deskripsinya kepanjangan / ada karakter yang ditolak YouTube — YouTube menolaknya",
                "Pastikan panel sudah di-Update ke versi terbaru — deskripsi otomatis dibersihkan sebelum upload.")
    if 'klip footage playlist tidak ditemukan' in low:
        return ("klip footage playlist sudah tidak ada dan video hasil render sebelumnya juga tidak ditemukan",
                "Buat ulang playlist dari awal (riset + render baru).")
    if 'quotaexceeded' in low or 'uploadlimitexceeded' in low:
        return ("kuota upload hari ini habis",
                "Tunggu sampai besok (kuota reset tengah malam), lalu pencet Coba Lagi.")
    if 'judul sudah tayang' in low:
        return ("judulnya sudah pernah tayang di channel ini — YouTube bisa menganggapnya duplikat",
                "Tidak perlu diperbaiki manual — Coba Lagi akan memakai judul baru otomatis.")
    return (r[:120] if r else "penyebab belum diketahui",
            "Cek penyebabnya di panel (Task Monitor → Riwayat).")


def _failure_combined_message(title, ch_name, reason, streak, just_paused):
    """🔥 v1.2.17: SATU pesan gabungan gagal + jeda channel (tanpa tombol
    Coba Lagi — retry tidak jalan selama channel dijeda). Urutan: penyebab
    → perbaiki → Lanjutkan → baru Coba Lagi."""
    cause, fix = _explain_failure_reason(reason, title)
    lines = [
        "❌ *Video gagal, Bos — dan channel saya jeda dulu* 😅",
        "",
        f"*Video:* {_tg_escape(str(title)[:70])}",
        f"*Channel:* {_tg_escape(ch_name)}",
        f"*Penyebab:* {_tg_escape(cause)}",
    ]
    if just_paused:
        lines.append(f"*Ini gagal ke-{streak} berturut-turut*, jadi saya jeda channel-nya dulu biar tidak makin parah.")
    else:
        lines.append("*Channel ini sedang dijeda*, jadi video tidak bisa jalan dulu.")
    lines += [
        "",
        "*Langkah selanjutnya:*",
        f"1. Perbaiki penyebabnya — {_tg_escape(fix)}",
        "2. Pencet *Lanjutkan* untuk channel ini.",
        "3. Baru pencet *Coba Lagi* untuk video ini.",
    ]
    return "\n".join(lines)


def _record_channel_failure(yt_id, title, reason):
    """Tambah 1 ke fail_streak channel. Kalau mencapai 3x berturut-turut,
    jeda channel OTOMATIS (mekanisme paused Fase 3 — gate worker sudah
    menahan task channel dijeda). Kembalikan (baru_dijeda, streak,
    kini_dijeda). Pesan Telegram TIDAK dikirim di sini (🔥 v1.2.17) —
    pemanggil yang mengirim SATU pesan gabungan (gagal + jeda) supaya tidak
    dobel dan urutannya benar. Dipanggil dari jalur gagal worker
    (render/upload/QC/disk). BUKAN untuk pembatalan manual."""
    if not yt_id:
        return False, 0, False
    try:
        with db_lock:
            target = next((c for c in database_channel
                           if c.get('yt_id') == yt_id), None)
            if target is None:
                return False, 0, False
            streak = int(target.get('fail_streak') or 0) + 1
            target['fail_streak'] = streak
            auto_paused = False
            if streak >= FAIL_STREAK_AUTO_PAUSE and not target.get('paused'):
                target['paused'] = True
                target['auto_paused'] = True
                auto_paused = True
            paused_now = bool(target.get('paused'))
            save_channels(database_channel)
    except Exception:
        return False, 0, False
    return auto_paused, streak, paused_now


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
            f"butuh ≥ {need / 1024 ** 3:.1f} GB untuk render "
            f"{_shorts_muse_dur_label(target_hours)}. "
            f"Saya udah bersih-bersih {cleaned_n} file ({cleaned_b / 1024 ** 2:.0f} MB) "
            f"tapi masih kurang {(need - free) / 1024 ** 3:.1f} GB 😅 "
            f"Hapus file lama / tambah disk dulu ya Bos, terus pencet Retry 💪")


def _shorts_muse_dur_label(target_hours):
    """Label durasi untuk pesan disk: Shorts (< 2 menit) tampil dalam
    detik, Factory tampil dalam jam — supaya tidak muncul '0.008 jam'."""
    try:
        secs = float(target_hours) * 3600
    except Exception:
        return f"{target_hours} jam"
    if secs < 120:
        return f"{secs:g} detik"
    return f"{target_hours:g} jam"


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
    Mengembalikan (ok, pesan, status_http). Kunci salah dihitung ke
    rate-limit per IP (#22: maks 30 gagal/menit → kunci 60 detik, 429);
    kunci benar tidak dihitung dan mereset hitungan. Operator jalan di
    localhost dan memanggil jarang (per klik tombol) — batas ini tidak
    akan mengganggu pemakaian normal."""
    import hmac as _hmac
    ip = request.remote_addr or '?'
    if _operator_locked(ip):
        return False, "Terlalu banyak percobaan kunci operator. Coba lagi sebentar lagi.", 429
    key = data.get('key', '')
    expected = load_bot_config().get('operator_key', '')
    if not expected or not _hmac.compare_digest(str(key), str(expected)):
        _record_operator_failure(ip)
        return False, "Kunci operator tidak valid.", 403
    _operator_failures.pop(ip, None)
    return True, '', 200

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
            # 🔒 #15: cek kapasitas antrean DULU sebelum status diubah —
            # task harus tetap pending (bisa di-approve ulang) bila antrean
            # penuh, bukan approved tapi tak ter-queue (= hilang).
            if not _channel_paused(_active_task_yt_id(target)) and render_queue.qsize() >= QUEUE_MAX_SIZE:
                return False, f"Antrean render penuh (maks {QUEUE_MAX_SIZE} task). Tunggu antrean berkurang lalu coba lagi.", title
            prev_status = target.get('status')
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
                # 🔒 #15 rollback (kondisi balap antar request): task kembali
                # pending supaya bisa di-approve ulang, bukan approved tapi
                # tak ter-queue.
                target['approval'] = 'pending'
                target['status'] = prev_status
                target['updated_at'] = time.time()
                save_tasks_db()
                print(f"Gagal queue approve task {task_id}: {e}", flush=True)
                return False, "Gagal masuk antrean render. Cek log server.", title
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
    (jumlah_disetujui, [judul...]). Task yang sudah diputuskan dilewati.
    🔒 #15: cek kapasitas antrean DULU — jangan tandai approved kalau
    antrean tidak muat (task approved tapi tidak ter-queue = hilang)."""
    approved_titles = []
    with db_lock:
        pending = [t for t in active_tasks
                   if t.get('batch_id') == batch_id and _approval_of(t) == 'pending'
                   and t.get('blueprint')]
        _to_queue = [t for t in pending
                     if CHANNEL_PAUSED_STATUS not in t.get('status', '')
                     and not _channel_paused(_active_task_yt_id(t))]
        if render_queue.qsize() + len(_to_queue) > QUEUE_MAX_SIZE:
            raise _QueueFullError(
                f"Antrean render penuh (maks {QUEUE_MAX_SIZE} task). "
                "Setujui sebagian task dulu atau tunggu antrean berkurang.")
        orig_status = {id(t): t.get('status') for t in pending}
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
    failed_bps = []
    for bp in blueprints:
        try:
            queue_put(bp)
        except Exception as e:
            print(f"Gagal queue batch approve: {e}", flush=True)
            failed_bps.append(bp)
    if failed_bps:
        # 🔒 #15 rollback (kondisi balap): task yang gagal masuk antrean
        # kembali pending supaya bisa di-approve ulang, bukan approved
        # tapi tak ter-queue.
        failed_ids = {bp.get('id') for bp in failed_bps}
        with db_lock:
            for t in active_tasks:
                if t.get('id') in failed_ids:
                    t['approval'] = 'pending'
                    t['status'] = orig_status.get(id(t), APPROVAL_PENDING_STATUS)
                    t['updated_at'] = time.time()
            save_tasks_db()
    return len(approved_titles), approved_titles

def _operator_approval_endpoint(decision):
    """Kerangka endpoint operator approve/reject satu task. Mengembalikan
    respons JSON Flask (dipakai kedua route di bawah)."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    if data.get('task_id') is None:
        return jsonify({"status": "error", "message": "task_id wajib diisi."}), 400
    ok, msg, title = _apply_approval(data.get('task_id'), decision)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 400
    word = "disetujui" if decision == 'approved' else "ditolak"
    send_tg_message(f"{'✅' if decision == 'approved' else '❌'} *Sip Bos, task {word}* 😎\n`{_tg_escape(title)}`\n_{_tg_escape(msg)}_")
    return jsonify({"status": "success", "message": msg, "title": title})

@app.route('/api/operator/approve_task', methods=['POST'])
def operator_approve_task():
    """Setujui SATU task pending dari tombol inline Telegram.
    Keamanan: sama seperti retry_task — wajib operator_key, route ini
    masuk allowed_routes di check_security."""
    try:
        return _operator_approval_endpoint('approved')
    except Exception as e:
        print(f"Gagal approve operator: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal menyetujui task. Cek log server."}), 500

@app.route('/api/operator/reject_task', methods=['POST'])
def operator_reject_task():
    """Tolak SATU task pending dari tombol inline Telegram. Task pindah
    ke riwayat berstatus 'Ditolak (Approval)' dan tidak dieksekusi."""
    try:
        return _operator_approval_endpoint('rejected')
    except Exception as e:
        print(f"Gagal reject operator: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal menolak task. Cek log server."}), 500

@app.route('/api/operator/approve_batch', methods=['POST'])
def operator_approve_batch():
    """Setujui SEMUA task pending dalam satu batch (tombol 'Setujui Semua').
    Keamanan: wajib operator_key seperti endpoint operator lain."""
    try:
        data = request.get_json(silent=True) or {}
        ok_key, msg_key, _st = _validate_operator_key(data)
        if not ok_key:
            return jsonify({"status": "error", "message": msg_key}), _st
        batch_id = data.get('batch_id')
        if not batch_id:
            return jsonify({"status": "error", "message": "batch_id wajib diisi."}), 400
        count, titles = _apply_approve_batch(batch_id)
        if count == 0:
            return jsonify({"status": "error", "message": "Tidak ada task pending di batch ini (mungkin sudah diputuskan)."}), 400
        send_tg_message(f"✅ *Sip Bos, {count} task udah saya setujui semua!* 💪\nUdah masuk antrean render, saya kerjakan satu-satu.\n_" + "; ".join(_tg_escape(t) for t in titles[:5]) + ("…" if len(titles) > 5 else "") + "_")
        return jsonify({"status": "success", "message": f"{count} task disetujui dan masuk antrean.", "count": count})
    except _QueueFullError as e:
        return jsonify({"status": "error", "message": str(e)}), 429
    except Exception as e:
        print(f"Gagal approve batch operator: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal menyetujui batch. Cek log server."}), 500

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
        ok_key, msg_key, _st = _validate_operator_key(data)
        if not ok_key:
            return jsonify({"status": "error", "message": msg_key}), _st
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
            # 🔥 v1.2.17: channel masih dijeda → tolak retry dengan jujur.
            # Task yang di-retry akan tertahan 'Dijeda (Channel Pause)' dan
            # tidak jalan sebelum channel dilanjutkan — jangan antrekan sia-sia.
            _t_yt = target.get('yt_id') or (target.get('blueprint') or {}).get('yt_id')
            _t_ch = next((c for c in database_channel if c.get('yt_id') == _t_yt), None)
            if _t_ch and _t_ch.get('paused'):
                return jsonify({"status": "error", "message": "Channel masih dijeda — pencet Lanjutkan dulu, baru Coba Lagi."}), 400
            # Judul duplikat (2026-11-19): retry dengan judul yang sama
            # PASTI gagal QC lagi. Ganti dulu ke judul baru yang belum
            # dipakai (bank berikutnya / varian bernomor), lalu render ulang.
            renamed_title = None
            if 'Gagal QC' in (target.get('status') or '') and 'judul sudah tayang' in (target.get('status') or ''):
                _yt = target.get('yt_id') or (target.get('blueprint') or {}).get('yt_id')
                _ch = next((c for c in database_channel if c.get('yt_id') == _yt), None)
                _new = _shorts_dedup_title(_yt, target.get('title') or '', _ch)
                if _new and _new != target.get('title'):
                    target['title'] = _new
                    try:
                        target['blueprint']['title'] = _new
                    except Exception:
                        pass
                    renamed_title = _new
            history_tasks.remove(target)
            target['status'] = "Menunggu Giliran (Queue) ⏳ (Retry Telegram)"
            target['updated_at'] = time.time()
            active_tasks.append(target)
            active_tasks.sort(key=lambda x: x.get('blueprint', {}).get('publish_date', ''))
            save_tasks_db()
            try:
                queue_put(target["blueprint"])
            except Exception as e:
                print(f"Gagal queue retry task {task_id}: {e}", flush=True)
                return jsonify({"status": "error", "message": "Gagal masuk antrean render. Cek log server."}), 500
        title = target.get('title', '?')[:60]
        # 🔥 v1.2.17: app.py TIDAK lagi mengirim pesan retry — operator yang
        # mengirim SATU pesan konfirmasi dari info JSON ini. Dulu ada dua
        # pesan ("Siap Bos, retry jalan!" dari sini + "Berhasil Bos, retry
        # jalan!" dari operator).
        return jsonify({"status": "success",
                        "message": (f"Task kembali ke antrean render dengan judul baru: {renamed_title[:60]}."
                                    if renamed_title else "Task kembali ke antrean render."),
                        "title": title, "renamed_title": renamed_title})
    except Exception as e:
        print(f"Gagal retry operator: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal retry task. Cek log server."}), 500

@app.route('/api/clear_history', methods=['POST'])
def clear_history():
    global history_tasks
    with db_lock: history_tasks.clear()
    save_tasks_db()
    return jsonify({"status": "success", "message": "Riwayat dibersihkan!"})

@app.route('/api/delete_history_item', methods=['POST'])
def delete_history_item():
    """Hapus SATU entri riwayat (Task Monitor → tombol 🗑️ per baris).
    Riwayat adalah jejak audit — tidak ada edit, hanya hapus. HANYA
    menyentuh history_tasks; task AKTIF (antrean/render) TIDAK PERNAH
    ikut terhapus. Login + CSRF dijamin check_security (before_request)."""
    global history_tasks
    try:
        data = request.get_json(silent=True) or {}
        task_id = data.get('task_id')
        if task_id is None:
            return jsonify({"status": "error", "message": "task_id wajib diisi."}), 400
        try:
            task_id = int(task_id)
        except Exception:
            return jsonify({"status": "error", "message": "task_id tidak valid."}), 400
        with db_lock:
            if any(t.get('id') == task_id for t in active_tasks):
                return jsonify({"status": "error", "message": "Task masih aktif — hapus hanya bisa dari riwayat."}), 400
            before = len(history_tasks)
            history_tasks = [t for t in history_tasks if t.get('id') != task_id]
            removed = before - len(history_tasks)
        if not removed:
            return jsonify({"status": "error", "message": "Entri tidak ditemukan di riwayat."}), 404
        save_tasks_db()
        return jsonify({"status": "success", "message": "Entri riwayat dihapus.", "deleted": removed})
    except Exception as e:
        print(f"Gagal hapus entri riwayat: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal menghapus entri riwayat."}), 500

HISTORY_MAX_AGE_DAYS = 90  # retensi riwayat = retensi snapshot channel (90 hari)

def _prune_old_history():
    """Auto-cleanup riwayat (2026-11-19): hapus entri yang finished_date /
    finished_at-nya lebih dari 90 hari. Dipanggil saat startup +
    setiap task masuk riwayat — riwayat adalah jejak audit, jadi yang
    dihapus hanya yang sudah kedaluwarsa, bukan yang baru."""
    try:
        cutoff = _wib_now().date() - timedelta(days=HISTORY_MAX_AGE_DAYS)
        cutoff_ts = datetime.combine(cutoff, datetime.min.time(), tzinfo=ZoneInfo("Asia/Jakarta")).timestamp()
    except Exception:
        return 0
    removed = 0
    with db_lock:
        kept = []
        for t in list(history_tasks):
            old = False
            try:
                if t.get('finished_at') and float(t.get('finished_at')) < cutoff_ts:
                    old = True
                elif t.get('finished_date') and str(t.get('finished_date'))[:10] < cutoff.isoformat():
                    old = True
            except Exception:
                old = False
            if old:
                removed += 1
            else:
                kept.append(t)
        if removed:
            history_tasks.clear()
            history_tasks.extend(kept)
    if removed:
        save_tasks_db()
        print(f"🧹 [History] Auto-cleanup: {removed} entri riwayat >90 hari dihapus.", flush=True)
    return removed

@app.route('/api/get_channels')
def get_channels():
    # 🔥 FASE 10: sertakan status scope Analytics per channel (True/False/
    # None = belum dicek) supaya panel bisa menandai channel yang tokennya
    # belum punya scope yt-analytics.readonly ("perlu taut ulang").
    _scope_cache = _load_video_stats_cache()
    safe_c = [{"id": c["id"], "name": c["name"], "yt_id": c["yt_id"], "thumbnail": c["thumbnail"], "status": c["status"], "title_bank": c.get("title_bank", []), "title_weights": c.get("title_weights", {}), "paused": c.get("paused", False), "autopilot": c.get("autopilot", "approval"), "rpm_usd": c.get("rpm_usd"), "monetized": c.get("monetized", False), "owner_email": c.get("owner_email", ""), "auto_shorts": _auto_shorts_cfg(c), "analytics_scope": _analytics_scope_state(_scope_cache, c.get("yt_id")),
               # 🔥 v1.3.68: group + flag jadwal harian (dipakai kolom GROUP,
               # filter group, dan slot rutin di Kalender).
               "group": c.get("group", ""), "daily_playlist": c.get("daily_playlist", False),
               # 🔥 v1.2.25: pemetaan OAuth — client utama + project mana saja
               # channel ini punya tokennya (metadata saja, tanpa secret).
               "oauth_client_id": _oauth_channel_primary_client_id(c),
               "token_projects": [_oauth_project_of_client_id(x) for x in _oauth_channel_cred_client_ids(c) if x]} for c in database_channel]
    return jsonify(safe_c)

@app.route('/api/get_project_quotas')
def get_project_quotas_api():
    """🔥 FASE 6: sisa kuota upload per project Google (butuh login panel —
    route ini TIDAK masuk allowed_routes, jadi check_security mewajibkan
    login). Kuota dihitung dari upload hari ini (WIB) semua channel di
    project yang sama; batas 100/hari/project."""
    return jsonify({"status": "success", "projects": get_project_quotas()})

from flask import send_file # Pastikan send_file di-import di baris paling atas jika belum ada

@app.route('/api/export_channels', methods=['GET'])
def export_channels():
    """Mengunduh database channel sebagai file JSON — HANYA field aman
    (#4). Kredensial (creds_list/creds_json/token) TIDAK PERNAH ikut
    ter-export; channel yang di-import harus ditaut ulang via device flow."""
    safe = [_sanitize_channel_for_export(c) for c in database_channel]
    payload = json.dumps(safe, indent=4, ensure_ascii=False)
    timestamp = int(time.time())
    return Response(payload, mimetype='application/json', headers={
        "Content-Disposition": f"attachment; filename=pabrik_konten_channels_backup_{timestamp}.json"})

def _sanitize_channel_for_export(c):
    """Salinan channel tanpa field kredensial — cerminan get_channels()."""
    return {"id": c.get("id"), "name": c.get("name"), "yt_id": c.get("yt_id"),
            "thumbnail": c.get("thumbnail"), "status": c.get("status"),
            "title_bank": c.get("title_bank", []),
            "title_weights": c.get("title_weights", {}),
            "paused": c.get("paused", False),
            "autopilot": c.get("autopilot", "approval"),
            "rpm_usd": c.get("rpm_usd"),
            "monetized": c.get("monetized", False),
            "auto_shorts": _auto_shorts_cfg(c),
            "last_scheduled_date": c.get("last_scheduled_date"),
            "daily_uploads": c.get("daily_uploads", {}),
            "daily_uploads_by_project": c.get("daily_uploads_by_project", {}),
            "oauth_client_id": _oauth_channel_primary_client_id(c),
            "token_projects": [_oauth_project_of_client_id(x) for x in _oauth_channel_cred_client_ids(c) if x],
            "cooldown_until": c.get("cooldown_until", 0)}

# Field kredensial yang WAJIB ditolak dari file import (#4).
_IMPORT_FORBIDDEN_KEYS = {'creds_list', 'creds_json', 'creds', 'access_token',
                          'refresh_token', 'token', 'client_secret', 'client_id'}

def _sanitize_imported_channel(entry, idx):
    """Validasi ketat satu entri channel dari file import. Mengembalikan
    dict channel bersih, atau raise ValueError dengan pesan aman."""
    if not isinstance(entry, dict):
        raise ValueError("Entri channel harus berupa objek JSON.")
    bad = _IMPORT_FORBIDDEN_KEYS & set(entry.keys())
    if bad:
        raise ValueError("File import mengandung field kredensial — ditolak. "
                         "Taut ulang channel lewat device flow, bukan import.")
    yt_id = entry.get('yt_id')
    if not _valid_yt_id(yt_id):
        raise ValueError("yt_id channel tidak valid.")
    name = entry.get('name')
    if not isinstance(name, str) or not name.strip():
        raise ValueError("Nama channel wajib diisi.")
    title_bank = entry.get('title_bank', [])
    if not isinstance(title_bank, list) or not all(isinstance(t, str) for t in title_bank):
        raise ValueError("title_bank harus berupa daftar judul (string).")
    title_weights = entry.get('title_weights', {})
    if not isinstance(title_weights, dict):
        raise ValueError("title_weights harus berupa objek.")
    autopilot = entry.get('autopilot', 'approval')
    if autopilot not in ('auto', 'approval'):
        autopilot = 'approval'
    rpm = entry.get('rpm_usd')
    if rpm is not None:
        try:
            rpm = float(rpm)
        except Exception:
            raise ValueError("rpm_usd harus angka.")
        if rpm < 0 or rpm > 100:
            raise ValueError("rpm_usd harus antara 0 dan 100.")
    out = {"id": entry.get('id') if isinstance(entry.get('id'), int) else idx + 1,
           "name": name.strip(),
           "yt_id": yt_id,
           "thumbnail": entry.get('thumbnail') if isinstance(entry.get('thumbnail'), str) else "",
           "status": entry.get('status') if isinstance(entry.get('status'), str) else "Imported",
           "title_bank": title_bank[:5000],
           "title_weights": {str(k): v for k, v in title_weights.items()},
           "paused": bool(entry.get('paused', False)),
           "autopilot": autopilot,
           "rpm_usd": rpm,
           # ⏰ Jadwal otomatis Short: konfigurasi (bukan kredensial) — bila
           # ada tapi tidak valid, pakai default (nonaktif) daripada
           # menolak seluruh file import.
           "auto_shorts": _auto_shorts_cfg({'auto_shorts': entry.get('auto_shorts')}),
           "last_scheduled_date": entry.get('last_scheduled_date') if isinstance(entry.get('last_scheduled_date'), str) else None,
           "daily_uploads": entry.get('daily_uploads') if isinstance(entry.get('daily_uploads'), dict) else {},
           "cooldown_until": 0}
    return out

@app.route('/api/import_channels', methods=['POST'])
def import_channels():
    """Mengunggah dan menimpa database channel dengan file JSON cadangan.
    Validasi ketat (#4 + #5): hanya field aman yang diterima, field
    kredensial ditolak, yt_id traversal ditolak."""
    global database_channel
    file = request.files.get('file')

    if not file or not file.filename.endswith('.json'):
        return jsonify({"status": "error", "message": "File tidak valid! Harus berformat .json"}), 400

    try:
        new_data = json.load(file)
        if not isinstance(new_data, list):
            return jsonify({"status": "error", "message": "Format file rusak. Data channel harus berupa List/Array."}), 400
        if len(new_data) > 100:
            return jsonify({"status": "error", "message": "File import maksimal 100 channel."}), 400
        clean = [_sanitize_imported_channel(e, i) for i, e in enumerate(new_data)]
        # yt_id ganda di dalam file = tolak (hindari duplikat diam-diam).
        seen = set()
        for c in clean:
            if c['yt_id'] in seen:
                return jsonify({"status": "error", "message": "File import berisi yt_id yang sama dua kali."}), 400
            seen.add(c['yt_id'])

        with db_lock:
            database_channel = clean
            # Langsung simpan secara permanen menimpa channels_db.json lama
            save_channels(database_channel)

        return jsonify({"status": "success", "message": f"{len(database_channel)} channel berhasil diimpor!"})
    except ValueError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        print(f"Gagal import channels: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal membaca file cadangan. Periksa format JSON-nya."}), 500

def _delete_channel_full(yt_id):
    """Hapus channel SEPENUHNYA (dipakai panel /api/delete_channel DAN
    operator /api/operator/delete_channel — v1.3.68, satu logika): batalkan
    antrean + bunuh proses render + sapu folder galeri + hapus dari
    database_channel. Video YouTube TIDAK ikut terhapus (tetap tayang di
    YouTube). Kembalikan (ok, pesan, jumlah_task_dibatalkan)."""
    global database_channel, active_tasks, history_tasks
    target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if target is None:
        return False, "Channel tidak ditemukan.", 0
    name = target.get('name', yt_id)
    # 1. Batalkan semua antreannya di background
    cancelled = 0
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
            cancelled += 1

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
    gallery_path = _safe_join_under(BASE_UPLOAD, yt_id)
    if os.path.exists(gallery_path):
        import shutil
        shutil.rmtree(gallery_path, ignore_errors=True)

    # 3. Hapus profil channel dari Database
    database_channel = [c for c in database_channel if c['yt_id'] != yt_id]
    save_channels(database_channel)

    return True, f"Channel {name}, aset Gallery, dan antrean berhasil disapu bersih!", cancelled


@app.route('/api/delete_channel', methods=['POST'])
def delete_channel():
    # 🔒 #5: yt_id divalidasi ketat (allowlist) + path hasil gabung dicek
    # tetap di bawah BASE_UPLOAD sebelum rmtree. 🔒 Konfirmasi ulang:
    # request wajib membawa confirm=yes (JS panel mengirimkannya setelah
    # dialog confirm() — lihat deleteChannel() di template).
    yt_id = request.form.get('yt_id')
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "yt_id tidak valid."}), 400
    if request.form.get('confirm') != 'yes':
        return jsonify({"status": "error", "message": "Penghapusan channel wajib dikonfirmasi (confirm=yes)."}), 400
    ok, msg, _n = _delete_channel_full(yt_id)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 404
    return jsonify({"status": "success", "message": msg})


@app.route('/api/operator/delete_channel', methods=['POST'])
def operator_delete_channel():
    """Hapus channel dari Telegram (NLU 'hapus channel X' / /hapuschannel —
    v1.3.68). Keamanan: wajib `key` == operator_key (HMAC). Logika hapus
    SAMA dengan tombol 🗑 di panel (helper _delete_channel_full).
    Konfirmasi sudah lewat alur NLU sebelum request ini dikirim."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    yt_id = (data.get('yt_id') or '').strip()
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    ok, msg, cancelled = _delete_channel_full(yt_id)
    if not ok:
        return jsonify({"status": "error", "message": msg}), 404
    return jsonify({"status": "success", "message": msg, "cancelled_tasks": cancelled})

# --- PRESET API ---
@app.route('/api/save_preset', methods=['POST'])
def save_preset():
    data = request.get_json(silent=True) or {}
    if not isinstance(data, dict):
        return jsonify({"status": "error", "message": "Preset harus berupa objek JSON."}), 400
    try:
        presets = {}
        if os.path.exists(PRESETS_FILE):
            with open(PRESETS_FILE, 'r') as f:
                try: presets = json.load(f)
                except: pass
        # 🔒 #13: logo_url remote TIDAK PERNAH disimpan — dukungan logo
        # remote dihapus (SSRF); logo selalu dari static/logo.png.
        for _v in data.values():
            if isinstance(_v, dict):
                _v.pop('logo_url', None)
        presets.update(data)
        with open(PRESETS_FILE, 'w') as f: json.dump(presets, f, indent=4)
        return jsonify({"status": "success"})
    except Exception as e:
        print(f"Gagal save preset: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal menyimpan preset."})

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
    data = request.get_json(silent=True) or {}
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
        print(f"Gagal delete preset: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal menghapus preset."})

# ============================================================
# 🖼️ GALLERY ENDPOINTS
# ============================================================
# 🔒 #6: allowlist ekstensi per folder galeri + validasi magic-byte.
# File .html/.svg/.php (dan apa pun di luar daftar) DITOLAK — dulu
# file apa pun bisa di-upload lalu dirender browser sebagai HTML/JS
# pada origin panel lewat /uploads/. Daftar ini sama dengan ekstensi
# yang dibaca mesin render (get_multi_backgrounds, get_all_audios,
# dsb.) — tidak ada file sah yang jadi tertolak.
_GALLERY_EXTS = {
    'audios': {'.mp3', '.wav'},
    'special_songs': {'.mp3', '.wav'},
    'backgrounds': {'.jpg', '.jpeg', '.png', '.webp', '.gif', '.mp4', '.mov', '.webm'},
    'thumbnails': {'.jpg', '.jpeg', '.png'},
    'base_videos': {'.mp4', '.mov', '.webm', '.mkv'},
    'overlays': {'.mp4', '.mov', '.webm', '.mkv'},
}

def _magic_ok(head, ext):
    """Cek beberapa byte awal file cocok dengan ekstensi yang diklaim."""
    if ext in ('.jpg', '.jpeg'):
        return head[:3] == b'\xff\xd8\xff'
    if ext == '.png':
        return head[:8] == b'\x89PNG\r\n\x1a\n'
    if ext == '.gif':
        return head[:6] in (b'GIF87a', b'GIF89a')
    if ext == '.webp':
        return head[:4] == b'RIFF' and head[8:12] == b'WEBP'
    if ext == '.mp3':
        return head[:3] == b'ID3' or head[:2] in (b'\xff\xfb', b'\xff\xf3', b'\xff\xf2')
    if ext == '.wav':
        return head[:4] == b'RIFF' and head[8:12] == b'WAVE'
    if ext in ('.mp4', '.mov'):
        return head[4:8] == b'ftyp'
    if ext in ('.webm', '.mkv'):
        return head[:4] == b'\x1a\x45\xdf\xa3'
    return False

@app.route('/api/get_asset_counts')
def get_asset_counts():
    yt_id = request.args.get('yt_id')
    if not yt_id: return jsonify({"audios": 0, "backgrounds": 0, "thumbnails": 0, "base_videos": 0, "overlays": 0, "special_songs": 0})
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "yt_id tidak valid."}), 400
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
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "yt_id tidak valid."}), 400
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
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "yt_id tidak valid."}), 400
    if not g_type:
        return jsonify({"status": "error", "message": "type tidak boleh kosong!"}), 400

    folder_name = resolve_folder(g_type)
    try:
        folder = get_channel_folder(yt_id, folder_name)
    except ValueError:
        return jsonify({"status": "error", "message": "type galeri tidak dikenal."}), 400
    allowed_exts = _GALLERY_EXTS.get(folder_name, set())

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
        safe_name = os.path.basename(f.filename)
        ext = os.path.splitext(safe_name)[1].lower()
        if ext not in allowed_exts:
            errors.append(f"{safe_name}: ekstensi {ext or '(tanpa ekstensi)'} tidak diizinkan untuk '{folder_name}'")
            continue
        try:
            # 🔒 #6: validasi magic-byte (isi file harus cocok dengan
            # ekstensi yang diklaim — .mp3 yang isinya HTML ditolak).
            head = f.stream.read(12)
            try:
                f.stream.seek(0)
            except Exception:
                pass
            if not _magic_ok(head, ext):
                errors.append(f"{safe_name}: isi file tidak cocok dengan ekstensi {ext}")
                continue
            # 🔒 #21: tolak file > 200 MB (cek awal dari header bila ada,
            # lalu cek ulang ukuran nyata setelah disimpan).
            if f.content_length and f.content_length > GALLERY_MAX_FILE_BYTES:
                errors.append(f"{safe_name}: file lebih dari 200 MB")
                continue
            dest = os.path.join(folder, safe_name)
            f.save(dest)
            if os.path.getsize(dest) > GALLERY_MAX_FILE_BYTES:
                try: os.remove(dest)
                except Exception: pass
                errors.append(f"{safe_name}: file lebih dari 200 MB")
                continue
            saved += 1
        except Exception:
            # 🔒 #24: pesan error generik — detail hanya di log server.
            print(f"Gagal simpan galeri {safe_name}: {f.filename}", flush=True)
            errors.append(f"{safe_name}: gagal disimpan")

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

    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "yt_id tidak valid."}), 400
    folder_name = resolve_folder(g_type)
    try:
        base = get_channel_folder(yt_id, folder_name)
    except ValueError:
        return jsonify({"status": "error", "message": "type galeri tidak dikenal."}), 400
    # 🔒 #5: nama file di-basename + hasil gabung dicek tetap di folder.
    try:
        path = _safe_join_under(base, os.path.basename(name))
    except ValueError:
        return jsonify({"status": "error", "message": "Nama file tidak valid."}), 400

    if os.path.exists(path):
        os.remove(path)
        return jsonify({"status": "success", "message": "File dihapus!"})
    # 🔒 #24: jangan bocorkan path absolut server di pesan error.
    return jsonify({"status": "error", "message": "File tidak ditemukan"})

@app.route('/api/clear_gallery_folder', methods=['POST'])
def clear_gallery_folder():
    yt_id  = request.form.get('yt_id', '').strip()
    g_type = request.form.get('type',  '').strip()

    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "yt_id tidak valid."}), 400
    folder_name = resolve_folder(g_type)
    try:
        path = get_channel_folder(yt_id, folder_name)
    except ValueError:
        return jsonify({"status": "error", "message": "type galeri tidak dikenal."}), 400

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
        print(f"Gagal memproses title bank: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal memproses file."}), 500

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
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "yt_id tidak valid."}), 400
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
            print(f"Gagal simpan paused {yt_id}: {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    if paused:
        send_tg_message(f"⏸️ *Siap Bos, channel {_tg_escape(name)} saya jeda dulu.* 😌\nTask-nya saya tahan semua, nggak ada yang jalan sampai Bos lanjutkan lagi.")
        return jsonify({"status": "success", "paused": True, "resumed_count": 0,
                        "message": f"Channel {name} dijeda. Task terjadwal ditahan."})
    resumed = _resume_channel_tasks(yt_id)
    send_tg_message(f"▶️ *Siap Bos, channel {_tg_escape(name)} jalan lagi!* 💪\n{resumed} task udah masuk antrean render.")
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
            print(f"Gagal simpan autopilot {yt_id}: {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    if mode == 'auto':
        send_tg_message(f"🤖 *Siap Bos, channel {_tg_escape(name)} saya set Full-Auto!* 😎\nBatch baru langsung jalan tanpa approval — saya yang urus sampai tayang 💪")
        return jsonify({"status": "success", "autopilot": "auto",
                        "message": f"Channel {name} mode Full-Auto. Batch baru langsung masuk antrean."})
    send_tg_message(f"👀 *Siap Bos, channel {_tg_escape(name)} balik ke mode Approval.* 😌\nBatch baru tetap saya kirim preview dulu buat Bos cek.")
    return jsonify({"status": "success", "autopilot": "approval",
                    "message": f"Channel {name} mode Approval. Batch baru menunggu persetujuan."})

@app.route('/api/set_channel_monetized', methods=['POST'])
def set_channel_monetized():
    """📊 TABEL DETAIL CHANNEL: toggle manual 'Channel sudah monetisasi'
    (butuh login panel — route ini TIDAK masuk allowed_routes, jadi
    check_security mewajibkan login). TIDAK ADA field API yang menyatakan
    status monetisasi — flag ini MANUAL (disimpan di channels_db.json)
    dan hanya mengubah badge di tabel: channel yang ditandai mendapat
    '💰 Monetisasi Aktif' dan tidak masuk penilaian kesiapan lagi.
    Pola sama seperti set_channel_autopilot (db_lock + save_channels)."""
    data = request.get_json(silent=True) or {}
    yt_id = data.get('yt_id')
    monetized = data.get('monetized')
    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id wajib diisi."}), 400
    if not isinstance(monetized, bool):
        return jsonify({"status": "error", "message": "monetized harus true/false."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        target['monetized'] = monetized
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan monetized {yt_id}: {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    if monetized:
        return jsonify({"status": "success", "monetized": True,
                        "message": f"Channel {name} ditandai SUDAH monetisasi (penanda manual). Badge 'Monetisasi Aktif' tampil di tabel."})
    return jsonify({"status": "success", "monetized": False,
                    "message": f"Tanda monetisasi channel {name} dihapus. Channel kembali masuk penilaian kesiapan."})

@app.route('/api/set_channel_auto_shorts', methods=['POST'])
def set_channel_auto_shorts():
    """⏰ JADWAL OTOMATIS SHORT: simpan konfigurasi per channel (butuh login
    panel — route ini TIDAK masuk allowed_routes, jadi check_security
    mewajibkan login + CSRF). Field yang dikirim digabung dengan konfigurasi
    tersimpan, lalu SELURUH hasil gabungan divalidasi — bila ada satu field
    tidak valid, TIDAK ADA yang disimpan (jangan simpan setengah-setengah).
    Terima objek 'auto_shorts' atau field datar (enabled/theme/create_time/
    publish_time/duration_sec) di level atas."""
    data = request.get_json(silent=True) or {}
    yt_id = data.get('yt_id')
    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id wajib diisi."}), 400
    provided = data.get('auto_shorts')
    if provided is None:
        provided = {k: data.get(k) for k in ('enabled', 'theme', 'create_time', 'publish_time', 'duration_sec') if data.get(k) is not None}
    if not isinstance(provided, dict):
        return jsonify({"status": "error", "message": "auto_shorts harus berupa objek."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        merged = dict(_auto_shorts_cfg(target))
        for k in ('enabled', 'theme', 'create_time', 'publish_time', 'duration_sec'):
            if k in provided:
                merged[k] = provided[k]
        cfg, err = _validate_auto_shorts_cfg(merged)
        if err:
            return jsonify({"status": "error", "message": err}), 400
        target['auto_shorts'] = cfg
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan auto_shorts {yt_id}: {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    return jsonify({"status": "success", "auto_shorts": cfg,
                    "message": f"Jadwal otomatis Short channel {name} disimpan." + (" Aktif — 1 Short dibuat otomatis tiap hari." if cfg['enabled'] else " Nonaktif.")})


# ════════════════════════════════════════════════════════════
# 🔄 UPDATE PANEL + VERSI (tombol Update di Settings)
# ════════════════════════════════════════════════════════════
# Skema versi: file VERSION (major.minor.patch) — minor naik untuk
# fitur baru, patch untuk perbaikan. Tiap commit/push punya versi.
# Short hash diambil dari git; "—" bila .git tidak tersedia.
VERSION_FILE = os.path.join(BASE_DIR, 'VERSION')
UPDATE_STATUS_FILE = os.path.join(BASE_DIR, 'update_status.json')
UPDATE_SCRIPT = os.path.join(BASE_DIR, 'update.sh')

def _app_version():
    """Versi aplikasi dari file VERSION (mis. '1.0.0'); 'dev' bila tak ada."""
    try:
        with open(VERSION_FILE, 'r', encoding='utf-8') as f:
            v = (f.read() or '').strip().split()
            return v[0] if v else 'dev'
    except Exception:
        return 'dev'

def _git_short_hash():
    """Short commit hash HEAD lokal; None bila .git tidak tersedia."""
    try:
        p = subprocess.run(['git', 'rev-parse', '--short', 'HEAD'],
                           cwd=BASE_DIR, capture_output=True, text=True, timeout=10)
        h = (p.stdout or '').strip()
        return h or None
    except Exception:
        return None

def _git_remote_head():
    """Short hash HEAD remote (origin); None bila GitHub tak terjangkau."""
    try:
        p = subprocess.run(['git', 'ls-remote', 'origin', 'HEAD'],
                           cwd=BASE_DIR, capture_output=True, text=True, timeout=20)
        out = (p.stdout or '').strip()
        return out.split()[0][:7] if out else None
    except Exception:
        return None

def _read_update_status():
    try:
        with open(UPDATE_STATUS_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
            return d if isinstance(d, dict) else {}
    except Exception:
        return {}

def _write_update_status(d):
    try:
        tmp = UPDATE_STATUS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(d, f)
        os.replace(tmp, UPDATE_STATUS_FILE)
    except Exception as e:
        print(f"Gagal tulis update_status: {e}", flush=True)

# Batas umur status 'running': lebih tua dari ini dianggap macet (updater
# mati sebelum sempat menulis hasil — mis. update.sh ikut terbunuh saat
# service restart mematikan satu cgroup). Macet -> update baru diizinkan.
_UPDATE_STALE_MINUTES = 30

def _update_running_is_stale(st):
    """True bila status 'running' sudah macet: started_at lebih dari
    _UPDATE_STALE_MINUTES yang lalu, atau started_at tidak bisa diparse
    (format rusak -> anggap macet, jangan pernah mengunci update selamanya)."""
    if not isinstance(st, dict) or st.get('status') != 'running':
        return False
    raw = str(st.get('started_at') or '')
    try:
        started = datetime.strptime(raw, '%Y-%m-%d %H:%M')
        started = started.replace(tzinfo=ZoneInfo("Asia/Jakarta"))
    except Exception:
        return True
    try:
        return (_wib_now() - started) > timedelta(minutes=_UPDATE_STALE_MINUTES)
    except Exception:
        return True

def _heal_stale_update_status():
    """Self-heal saat startup: bila update_status.json masih 'running'
    (updater mati sebelum sempat menulis hasil — mis. update.sh ikut
    terbunuh saat service restart), pulihkan dari keadaan saat ini.
    - versi/commit BERBEDA dari yang tercatat -> pull + restart sudah
      terjadi: tulis success dengan versi/commit saat ini.
    - versi/commit SAMA -> updater mati sebelum pull: tulis failed.
    Bukan 'running' -> file tidak disentuh. Tidak pernah melempar."""
    try:
        st = _read_update_status()
        if not st or st.get('status') != 'running':
            return
        now = _wib_now().strftime('%Y-%m-%d %H:%M')
        cur_ver, cur_commit = _app_version(), _git_short_hash()
        old_ver, old_commit = st.get('version'), st.get('commit')
        if cur_ver != old_ver or cur_commit != old_commit:
            _write_update_status({"status": "success",
                                  "started_at": st.get('started_at') or '?',
                                  "finished_at": now,
                                  "version": cur_ver, "commit": cur_commit,
                                  "output": "Update selesai (status dipulihkan saat startup)."})
            print(f"✅ [Update] Status 'running' macet dipulihkan jadi success (v{cur_ver} {cur_commit}).", flush=True)
        else:
            _write_update_status({"status": "failed",
                                  "started_at": st.get('started_at') or '?',
                                  "finished_at": now,
                                  "version": cur_ver, "commit": cur_commit,
                                  "output": "Updater terhenti sebelum selesai."})
            print("⚠️ [Update] Status 'running' macet dipulihkan jadi failed (updater terhenti sebelum selesai).", flush=True)
    except Exception as e:
        print(f"⚠️ [Update] Self-heal status update gagal (diabaikan): {e}", flush=True)

def _ensure_muse_ssh_key():
    """Pasang kunci SSH publik Muse (file `muse-ssh.pub` di repo) ke
    ~/.ssh/authorized_keys bila belum ada — supaya Muse bisa masuk ke VPS
    sebagai admin (kirim file, periksa server) tanpa user mengetik perintah
    panjang di konsol. Kunci ini PUBLIK (aman dibagikan); yang memegang
    kunci privatnya hanya VM Muse. Idempotent: bila kunci sudah ada,
    tidak ditulis ulang. Tidak pernah melempar."""
    try:
        repo_dir = os.path.dirname(os.path.abspath(__file__))
        pub_path = os.path.join(repo_dir, 'muse-ssh.pub')
        if not os.path.isfile(pub_path):
            return
        with open(pub_path, 'r', encoding='utf-8') as f:
            key = f.read().strip()
        if not key.startswith('ssh-ed25519 ') or '\n' in key:
            print("⚠️ [SSH] muse-ssh.pub tidak valid, dilewati.", flush=True)
            return
        ssh_dir = os.path.expanduser('~/.ssh')
        auth_path = os.path.join(ssh_dir, 'authorized_keys')
        os.makedirs(ssh_dir, exist_ok=True)
        existing = ''
        if os.path.isfile(auth_path):
            with open(auth_path, 'r', encoding='utf-8', errors='ignore') as f:
                existing = f.read()
        if key in existing:
            return
        with open(auth_path, 'a', encoding='utf-8') as f:
            if existing and not existing.endswith('\n'):
                f.write('\n')
            f.write(key + '\n')
        os.chmod(auth_path, 0o600)
        print("✅ [SSH] Kunci SSH Muse dipasang ke authorized_keys.", flush=True)
    except Exception as e:
        print(f"⚠️ [SSH] Pemasangan kunci SSH Muse dilewati: {e}", flush=True)

# Status task yang berarti "tidak sedang diproses" (aman di-restart).
_UPDATE_IDLE_MARKERS = ('Selesai', 'Gagal', 'Dibatalkan', 'Menunggu', 'Ditunda', 'Dijeda')

def _any_task_rendering():
    """True bila ada task aktif yang sedang diproses (render/upload/QC).
    Task menunggu approval / menunggu jadwal / dijeda TIDAK dihitung."""
    try:
        for t in (active_tasks or []):
            if t.get('approval') == 'pending':
                continue
            st = str(t.get('status') or '')
            if st and not any(m in st for m in _UPDATE_IDLE_MARKERS):
                return True
    except Exception:
        pass
    return False

@app.route('/api/app_version')
def app_version_api():
    """Versi + commit yang sedang jalan (butuh login panel; GET saja)."""
    return jsonify({"status": "success", "version": _app_version(),
                    "commit": _git_short_hash()})

@app.route('/api/check_update', methods=['POST'])
def check_update_api():
    """Bandingkan HEAD lokal vs remote (butuh login + CSRF via check_security)."""
    local = _git_short_hash()
    remote = _git_remote_head()
    if remote is None:
        return jsonify({"status": "error",
                        "message": "Tidak bisa menjangkau GitHub (cek koneksi VPS)."}), 502
    return jsonify({"status": "success", "update_available": (local != remote),
                    "local": local, "remote": remote, "version": _app_version()})

@app.route('/api/update_status')
def update_status_api():
    """Status update terakhir (butuh login panel; GET saja)."""
    st = _read_update_status()
    return jsonify({"status": "success", "update": (st or None)})

@app.route('/api/update_panel', methods=['POST'])
def update_panel_api():
    """Mulai update: git pull + restart di latar (update.sh). Kembalikan
    langsung 'update dimulai' — request TIDAK menunggu restart (proses web
    akan mati saat restart). Ditolak 409 bila: update sedang berjalan, atau
    ada task sedang render. Pull gagal -> update.sh TIDAK restart (status
    bisa dipolling lewat /api/update_status)."""
    st = _read_update_status()
    if st.get('status') == 'running':
        if _update_running_is_stale(st):
            # Macet (lebih dari 30 menit / started_at rusak): bersihkan
            # otomatis, lalu LANJUTKAN update baru (bukan 409). Tanpa ini
            # satu update yang gagal di tengah jalan mengunci tombol
            # Update selamanya.
            old_started = st.get('started_at') or '?'
            now = _wib_now().strftime('%Y-%m-%d %H:%M')
            _write_update_status({"status": "failed", "started_at": old_started,
                                  "finished_at": now,
                                  "version": _app_version(), "commit": _git_short_hash(),
                                  "output": f"Update sebelumnya tidak selesai (status macet sejak {old_started}); dibersihkan otomatis."})
            print(f"🧹 [Update] Status 'running' macet sejak {old_started} dibersihkan otomatis.", flush=True)
        else:
            return jsonify({"status": "error",
                            "message": "Update sedang berjalan, tunggu sampai selesai."}), 409
    if _any_task_rendering():
        return jsonify({"status": "error",
                        "message": "Ada video sedang dirender/di-upload. Coba lagi setelah selesai."}), 409
    if not os.path.exists(UPDATE_SCRIPT):
        return jsonify({"status": "error",
                        "message": "File update.sh tidak ditemukan di server."}), 500
    # 💾 BACKUP PRA-UPDATE: snapshot dulu sebelum git pull. Bila backup
    # gagal, update TETAP jalan — tapi peringatan ditulis ke status.
    # (update.sh juga mencoba backup lagi, tapi dedupe 10 menit di
    # backup_core membuat yang kedua langsung skip.)
    _pre_bak_note = ''
    try:
        _pre_meta, _pre_err = _do_backup_now('pre_update')
        if _pre_err:
            _pre_bak_note = f"\n⚠️ Backup pra-update GAGAL ({_pre_err}) — update tetap jalan tanpa snapshot baru."
        elif _pre_meta and not _pre_meta.get('skipped'):
            _pre_bak_note = f"\n💾 Backup pra-update: {_pre_meta['filename']} ({_pre_meta['size_human']})."
    except Exception as _e:
        _pre_bak_note = f"\n⚠️ Backup pra-update GAGAL ({_e}) — update tetap jalan tanpa snapshot baru."
    now = _wib_now().strftime('%Y-%m-%d %H:%M')
    _write_update_status({"status": "running", "started_at": now,
                          "version": _app_version(), "commit": _git_short_hash(),
                          "output": ""})
    try:
        logf = open(os.path.join(BASE_DIR, 'update.log'), 'ab')
        subprocess.Popen(['nohup', 'bash', 'update.sh'],
                         cwd=BASE_DIR, stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)
        logf.close()  # fd sudah di-dup ke proses anak; parent tidak perlu menahannya
    except Exception as e:
        _write_update_status({"status": "failed", "started_at": now,
                              "finished_at": now,
                              "output": f"Gagal menjalankan updater: {e}"})
        return jsonify({"status": "error", "message": "Gagal menjalankan updater."}), 500
    return jsonify({"status": "success",
                    "message": "Update dimulai — panel akan restart sendiri. Pantau status di kartu ini." + (_pre_bak_note if _pre_bak_note else "")})


# ============================================================
#  🔄 RESTART BOT TELEGRAM DARI PANEL (v1.2.11)
#  Latar: tombol Update panel me-restart panel + operator lewat
#  ziqva.sh, tapi bila restart operator gagal/terlewat, bot Telegram
#  diam-diam jalan dengan kode lama. Tombol ini memberi user jalan
#  restart operator secara manual dari panel (tanpa SSH/konsol).
#  Panel TIDAK ikut restart di sini: handler berjalan di proses
#  panel, dan me-restart service LAIN (operator) tidak membunuh
#  proses panel — aman.
# ============================================================
_OPERATOR_RESTART_LAST = {'at': 0.0}   # monotonic() restart terakhir (anti-spam 60 dtk)
_OPERATOR_RESTART_MIN_INTERVAL = 60


def _systemd_ts_to_wib(raw):
    """Ubah ActiveEnterTimestamp systemd ("Wed 2026-11-18 23:09:48 UTC")
    jadi "2026-11-19 06:09 WIB". Bila zona sudah WIB/WITA/WIT, tidak
    digeser. Bila tidak bisa diparse, kembalikan teks aslinya."""
    raw = (raw or '').strip()
    if not raw or raw.lower() in ('n/a', 'none'):
        return None
    try:
        parts = raw.rsplit(' ', 1)
        zone = parts[1].upper() if len(parts) == 2 else ''
        dt = datetime.strptime(parts[0], '%a %Y-%m-%d %H:%M:%S')
        if zone in ('UTC', 'GMT'):
            dt = dt + timedelta(hours=7)
            return dt.strftime('%Y-%m-%d %H:%M WIB')
        return dt.strftime('%Y-%m-%d %H:%M') + (f' {zone}' if zone else '')
    except Exception:
        return raw


def _operator_service_state():
    """Baca status service operator via systemctl. Mengembalikan dict
    {active: bool, since: str|None, note: str}. Tidak pernah raise —
    bila systemctl gagal, active=False + note berisi sebabnya."""
    try:
        r = subprocess.run(['systemctl', 'is-active', 'ziqva-muse-operator'],
                           capture_output=True, text=True, timeout=10)
        active = (r.stdout or '').strip() == 'active'
        since = None
        note = ''
        if active:
            try:
                r2 = subprocess.run(['systemctl', 'show', '-p', 'ActiveEnterTimestamp',
                                     '--value', 'ziqva-muse-operator'],
                                    capture_output=True, text=True, timeout=10)
                since = _systemd_ts_to_wib(r2.stdout)
            except Exception:
                since = None
        else:
            note = 'Service tidak aktif (' + ((r.stdout or '').strip() or 'unknown') + ').'
        return {'active': active, 'since': since, 'note': note}
    except Exception as e:
        return {'active': False, 'since': None,
                'note': f'Tidak bisa membaca status service: {e}'}


@app.route('/api/operator_status')
def operator_status_api():
    """Status service operator (butuh login panel; GET saja). Dipakai
    kartu Telegram di Settings + polling setelah restart manual."""
    st = _operator_service_state()
    return jsonify({"status": "success", "active": st['active'],
                    "since": st['since'], "note": st['note'],
                    "version": _app_version(), "commit": _git_short_hash()})


@app.route('/api/restart_operator', methods=['POST'])
def restart_operator_api():
    """Restart service ziqva-muse-operator SAJA (panel tidak ikut).
    Login + CSRF dijamin check_security (before_request). Restart
    dijalankan detached (seperti update.sh) lalu langsung kembalikan
    sukses — request TIDAK menunggu restart selesai. Anti-spam:
    ditolak 429 bila restart terakhir <60 detik lalu."""
    now_mono = time.monotonic()
    last = _OPERATOR_RESTART_LAST.get('at', 0.0)
    if now_mono - last < _OPERATOR_RESTART_MIN_INTERVAL:
        wait = int(_OPERATOR_RESTART_MIN_INTERVAL - (now_mono - last))
        return jsonify({"status": "error",
                        "message": f"Bot baru saja di-restart. Tunggu {wait} detik lagi sebelum restart ulang."}), 429
    try:
        subprocess.Popen(['nohup', 'systemctl', 'restart', 'ziqva-muse-operator'],
                         stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True, close_fds=True)
    except Exception as e:
        return jsonify({"status": "error",
                        "message": f"Gagal menjalankan restart operator: {e}"}), 500
    _OPERATOR_RESTART_LAST['at'] = now_mono
    return jsonify({"status": "success",
                    "message": "Restart bot Telegram dimulai — bot akan mati sebentar lalu hidup lagi dan menyapa di Telegram. Tunggu ~10–30 detik."})


# ============================================================
#  🔄 UPDATE + RESTART LEWAT TELEGRAM (v1.2.17)
#  Perintah /update dan /restart di bot Telegram.
#  - /update memanggil update_panel_api() LANGSUNG (fungsi view yang
#    sama dengan tombol Update panel) — jadi penjaga 409, backup
#    pra-update, dan update.sh detached sebagai anak PANEL semuanya
#    identik, tanpa duplikasi logika. update.sh HARUS jalan sebagai
#    anak proses PANEL, bukan operator: me-restart operator membunuh
#    update.sh sebelum panel sempat restart (pelajaran v1.2.11).
#  - /restart menjalankan ziqva.sh restart (operator dulu, panel
#    terakhir — urutan di dalam ziqva.sh, v1.2.11) secara detached.
#  Keduanya HMAC operator_key (masuk _OPERATOR_ENDPOINTS).
# ============================================================
_SERVICES_RESTART_LAST = {'at': 0.0}   # monotonic() restart terakhir (anti-spam 60 dtk)


@app.route('/api/operator/update_panel', methods=['POST'])
def operator_update_panel():
    """Mulai update panel dari Telegram (/update). Jalur SAMA seperti
    tombol Update panel (update_panel_api): penjaga 409 (update sedang
    berjalan / ada video dirender), backup pra-update, update.sh
    detached sebagai anak panel. Notifikasi selesai/gagal + versi baru
    dikirim update.sh sendiri via Telegram. Keamanan: wajib
    operator_key (HMAC) seperti endpoint operator lain."""
    try:
        data = request.get_json(silent=True) or {}
        ok_key, msg_key, _st = _validate_operator_key(data)
        if not ok_key:
            return jsonify({"status": "error", "message": msg_key}), _st
        return update_panel_api()
    except Exception as e:
        print(f"Gagal update operator: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal memulai update. Cek log server."}), 500


@app.route('/api/operator/restart_services', methods=['POST'])
def operator_restart_services():
    """Restart panel + operator dari Telegram (/restart). ziqva.sh
    restart: operator dulu, panel terakhir (dua perintah systemctl
    terpisah, v1.2.11). Dijalankan detached lalu langsung kembalikan
    sukses — request TIDAK menunggu restart selesai. Ditolak 409 bila
    ada video sedang dirender/di-upload atau update sedang berjalan
    (sama seperti tombol Update panel); anti-spam 429 bila restart
    terakhir <60 detik lalu. Keamanan: wajib operator_key (HMAC)
    seperti endpoint operator lain."""
    try:
        data = request.get_json(silent=True) or {}
        ok_key, msg_key, _st = _validate_operator_key(data)
        if not ok_key:
            return jsonify({"status": "error", "message": msg_key}), _st
        if _any_task_rendering():
            return jsonify({"status": "error",
                            "message": "Ada video sedang dirender/di-upload. Coba lagi setelah selesai."}), 409
        _ust = _read_update_status()
        if _ust.get('status') == 'running' and not _update_running_is_stale(_ust):
            return jsonify({"status": "error",
                            "message": "Update sedang berjalan, tunggu sampai selesai."}), 409
        now_mono = time.monotonic()
        last = _SERVICES_RESTART_LAST.get('at', 0.0)
        if now_mono - last < _OPERATOR_RESTART_MIN_INTERVAL:
            wait = int(_OPERATOR_RESTART_MIN_INTERVAL - (now_mono - last))
            return jsonify({"status": "error",
                            "message": f"Baru saja restart. Tunggu {wait} detik lagi sebelum restart ulang."}), 429
        try:
            subprocess.Popen(['nohup', 'bash', 'ziqva.sh', 'restart'],
                             cwd=BASE_DIR, stdin=subprocess.DEVNULL,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                             start_new_session=True, close_fds=True)
        except Exception as e:
            return jsonify({"status": "error",
                            "message": f"Gagal menjalankan restart: {e}"}), 500
        _SERVICES_RESTART_LAST['at'] = now_mono
        return jsonify({"status": "success",
                        "message": "Restart dimulai — panel + bot Telegram akan mati sebentar lalu hidup lagi."})
    except Exception as e:
        print(f"Gagal restart services operator: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal restart. Cek log server."}), 500


# ============================================================
#  💾 BACKUP OTOMATIS (v1.2.8)
#  Mesin di backup_core.py (stdlib murni): arsip tar.gz terenkripsi
#  AES-256-CBC + PBKDF2 via openssl, retensi maks 3 backup terbaru
#  (v1.2.26; harian + pra-update dihitung bersama),
#  manifest + hash SHA256 per file, snapshot pengaman saat restore.
#  Backup TIDAK PERNAH dikirim ke Telegram (berisi refresh token
#  Google + token Telegram + PIN hash) — Telegram hanya status /
#  notifikasi. Download hanya lewat panel (login + CSRF).
# ============================================================
import backup_core as _backup_core

_BACKUP_RUNNING = {'active': False}   # guard: satu backup dalam satu waktu


def _backup_settings():
    cfg = load_bot_config()
    try:
        hour = int(cfg.get('backup_hour', 3) or 3)
    except Exception:
        hour = 3
    return {'enabled': cfg.get('backup_enabled', True) is not False,
            'hour': hour if 0 <= hour <= 23 else 3}


def _backup_status_dict():
    st = _backup_settings()
    metas = _backup_core.list_backups()
    return {'enabled': st['enabled'], 'hour': st['hour'],
            'running': bool(_BACKUP_RUNNING.get('active')),
            'openssl': bool(shutil.which('openssl')),
            'key_masked': _mask_token(_backup_core.read_key()),
            'backups': metas,
            'last': metas[0] if metas else None}


def _do_backup_now(trigger):
    """Jalankan backup sinkron dengan guard. Dipakai endpoint manual,
    scheduler harian, dan hook pre-update. Mengembalikan (meta, error)."""
    if _BACKUP_RUNNING.get('active'):
        return None, 'Backup lain sedang berjalan. Coba lagi sebentar lagi.'
    _BACKUP_RUNNING['active'] = True
    try:
        meta = _backup_core.create_backup(trigger=trigger)
        return meta, None
    except _backup_core.BackupBusy as e:
        return None, str(e)
    except _backup_core.BackupError as e:
        return None, str(e)
    except Exception as e:
        return None, f'Gagal membuat backup: {e}'
    finally:
        _BACKUP_RUNNING['active'] = False


def _backup_create_thread():
    """Backup manual di latar (panel / Telegram) + notifikasi hasilnya."""
    meta, err = _do_backup_now('manual')
    if meta and not meta.get('skipped'):
        send_tg_message(f"💾 *Backup selesai, Bos* 😎\n\n`{meta['filename']}` ({meta['size_human']}, {meta['files_count']} file, {'terenkripsi ✅' if meta['encrypted'] else '⚠️ TANPA enkripsi'})\nIsi: DB + config + galeri{' (video di-skip: galeri >2 GB)' if meta.get('media_skipped') else ''}.")
    elif err:
        send_tg_message(f"❌ *Backup GAGAL, Bos* 😅\n\nAlasan: {err}")


def _backup_daily_loop():
    """Scheduler harian: tiap 60 detik cek jam WIB. Bila jam == backup_hour
    dan belum ada backup hari ini -> buat + notifikasi Telegram."""
    while True:
        time.sleep(60)
        try:
            st = _backup_settings()
            if not st['enabled']:
                continue
            now = _wib_now()
            if now.hour != st['hour']:
                continue
            metas = _backup_core.list_backups()
            today = now.strftime('%Y-%m-%d')
            done_today = any((m.get('created_at') or '').startswith(today)
                             and m.get('trigger') in ('daily', 'manual') for m in metas)
            if done_today:
                continue
            meta, err = _do_backup_now('daily')
            if meta and not meta.get('skipped'):
                send_tg_message(f"💾 *Backup harian selesai, Bos* 😎\n\n`{meta['filename']}` ({meta['size_human']}, {meta['files_count']} file, {'terenkripsi ✅' if meta['encrypted'] else '⚠️ TANPA enkripsi'})\nIsi: DB + config + galeri{' (video di-skip: galeri >2 GB)' if meta.get('media_skipped') else ''}.")
            elif err:
                send_tg_message(f"❌ *Backup harian GAGAL, Bos* 😅\n\nAlasan: {err}\n\nData panel belum tercadangkan hari ini — coba /backup dari Telegram.")
        except Exception as e:
            print(f"[backup] loop harian gagal: {e}", flush=True)


@app.route('/api/backup_status')
def backup_status_api():
    return jsonify({"status": "success", **_backup_status_dict()})


@app.route('/api/backup_create', methods=['POST'])
def backup_create_api():
    if _BACKUP_RUNNING.get('active'):
        return jsonify({"status": "error",
                        "message": "Backup lain sedang berjalan. Coba lagi sebentar lagi."}), 409
    threading.Thread(target=_backup_create_thread, daemon=True).start()
    return jsonify({"status": "success",
                    "message": "Backup sedang dibuat di latar. Pantau di kartu ini."})


@app.route('/api/backup_download')
def backup_download_api():
    # GET + login (check_security) + token CSRF via query — download tidak
    # bisa membawa header, jadi token dikirim sebagai parameter URL.
    sent = request.args.get('csrf_token') or ''
    expected = session.get('csrf_token') or ''
    if not expected or not hmac.compare_digest(str(sent), str(expected)):
        return jsonify({"status": "error",
                        "message": "Token CSRF tidak valid. Muat ulang halaman lalu coba lagi."}), 403
    name = request.args.get('name') or ''
    try:
        path = _backup_core._backup_path(name)
    except _backup_core.BackupError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    if not os.path.isfile(path):
        return jsonify({"status": "error", "message": "Backup tidak ditemukan."}), 404
    return send_file(path, as_attachment=True, download_name=name)


@app.route('/api/backup_restore', methods=['POST'])
def backup_restore_api():
    data = request.get_json(silent=True) or {}
    name = (data.get('name') or '').strip()
    # Konfirmasi eksplisit: confirm=true DAN ketik "YA". Tanpa itu -> DITOLAK.
    if data.get('confirm') is not True or (data.get('confirm_text') or '').strip().upper() != 'YA':
        return jsonify({"status": "error",
                        "message": "Restore ditolak — ketik YA di kolom konfirmasi dulu."}), 400
    if _any_task_rendering():
        return jsonify({"status": "error",
                        "message": "Ada video sedang dirender/di-upload. Restore ditolak — coba lagi setelah selesai."}), 409
    st = _read_update_status()
    if st.get('status') == 'running' and not _update_running_is_stale(st):
        return jsonify({"status": "error",
                        "message": "Update sedang berjalan. Restore ditolak — coba lagi setelah update selesai."}), 409
    key = (data.get('key') or '').strip() or None
    try:
        restored = _backup_core.restore_backup(name, key=key, confirm=True)
    except _backup_core.BackupError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    except Exception as e:
        return jsonify({"status": "error", "message": f"Restore gagal: {e}"}), 500
    # Restart di latar (proses web mati saat restart — pola sama seperti update).
    _restart_ok = True
    try:
        logf = open(os.path.join(BASE_DIR, 'update.log'), 'ab')
        subprocess.Popen(['nohup', 'bash', '-c', 'sleep 2; bash ziqva.sh restart'],
                         cwd=BASE_DIR, stdin=subprocess.DEVNULL,
                         stdout=logf, stderr=subprocess.STDOUT,
                         start_new_session=True, close_fds=True)
        logf.close()
    except Exception as _e:
        # 🔥 v1.3.33 (audit M3): bila spawn restart gagal, jangan
        # lapor sukses — file sudah ter-restore tapi proses lama
        # tetap jalan dengan state pra-restore.
        _restart_ok = False
        print(f"[restore] GAGAL jadwalkan restart: {_e}", flush=True)
    if not _restart_ok:
        return jsonify({"status": "error",
                        "message": f"Restore file berhasil ({len(restored)} file), tapi restart panel GAGAL — jalankan restart manual (bash ziqva.sh restart)."}), 500
    try:
        send_tg_message(f"♻️ *Restore backup selesai, Bos* 😎\n\n`{name}` — {len(restored)} file dipulihkan. Panel restart dan kembali online sebentar lagi. Login lagi dengan PIN yang ada di backup.")
    except Exception:
        pass
    return jsonify({"status": "success",
                    "message": f"Restore selesai ({len(restored)} file). Panel akan restart — login lagi setelahnya."})


@app.route('/api/backup_delete', methods=['POST'])
def backup_delete_api():
    data = request.get_json(silent=True) or {}
    try:
        _backup_core.delete_backup((data.get('name') or '').strip())
    except _backup_core.BackupError as e:
        return jsonify({"status": "error", "message": str(e)}), 400
    return jsonify({"status": "success", "message": "Backup dihapus."})


@app.route('/api/backup_settings', methods=['POST'])
def backup_settings_api():
    data = request.get_json(silent=True) or {}
    try:
        hour = int(data.get('hour', 3))
    except Exception:
        return jsonify({"status": "error", "message": "Jam tidak valid (0-23)."}), 400
    if not 0 <= hour <= 23:
        return jsonify({"status": "error", "message": "Jam harus 0-23."}), 400
    cfg = load_bot_config()
    cfg['backup_enabled'] = bool(data.get('enabled', True))
    cfg['backup_hour'] = hour
    _save_bot_config(cfg)
    return jsonify({"status": "success",
                    "message": f"Jadwal backup disimpan ({'aktif' if cfg['backup_enabled'] else 'nonaktif'}, jam {hour:02d}:00 WIB)."})


@app.route('/api/backup_key', methods=['POST'])
def backup_key_api():
    """Kunci backup lengkap — HANYA untuk pemilik yang sudah login panel
    dan mengklik tombol 'Tampilkan kunci'. Tidak pernah di-log."""
    key = _backup_core.read_key()
    if not key:
        key = _backup_core.get_or_create_key()
    return jsonify({"status": "success", "key": key})


# --- Endpoint operator: backup (auth HMAC operator_key) ---
@app.route('/api/operator/backup_create', methods=['POST'])
def operator_backup_create():
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    if _BACKUP_RUNNING.get('active'):
        return jsonify({"status": "error", "message": "Backup lain sedang berjalan."}), 409
    threading.Thread(target=_backup_create_thread, daemon=True).start()
    return jsonify({"status": "success", "message": "Backup sedang dibuat di latar."})


@app.route('/api/operator/backup_status', methods=['POST'])
def operator_backup_status():
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    return jsonify({"status": "success", **_backup_status_dict()})


@app.route('/api/check_secret')
def check_secret():
    # Mengembalikan info client UTAMA agar Settings bisa menampilkannya
    # (nama file, client_id, tipe client, ukuran, waktu upload). client_secret
    # TIDAK PERNAH dikembalikan ke browser. 🔥 v1.2.25: sumbernya registry
    # multi-client (client utama), bukan lagi file tunggal.
    primary = _oauth_primary_client()
    if not primary:
        return jsonify({"exists": False})
    try:
        path = _oauth_client_secret_path(primary)
        size = os.path.getsize(path) if path else None
        modified = datetime.fromtimestamp(os.path.getmtime(path), tz=timezone.utc).isoformat() if path else None
        return jsonify({
            "exists": True,
            "filename": "client_secret.json",
            "client_id": primary.get('client_id'),
            "client_type": primary.get('type') or 'other',
            "project_id": primary.get('project_id'),
            "name": primary.get('name'),
            "size_bytes": size,
            "modified_iso": modified or primary.get('uploaded_at'),
        })
    except Exception:
        return jsonify({"exists": True, "parse_error": True})


@app.route('/api/oauth_clients')
def oauth_clients_api():
    # 🔥 v1.2.25: daftar semua client OAuth (metadata saja — TANPA secret)
    # + pemetaan tiap channel (client utama + client mana saja channel itu
    # punya tokennya). Dipakai Settings untuk daftar client & pemetaan.
    clients = []
    for e in _oauth_client_entries():
        clients.append({
            "client_id": e.get('client_id'),
            "project_id": e.get('project_id'),
            "type": e.get('type'),
            "name": e.get('name'),
            "uploaded_at": e.get('uploaded_at'),
            "primary": bool(e.get('primary')),
        })
    channels = []
    for c in database_channel:
        cids = _oauth_channel_cred_client_ids(c)
        channels.append({
            "yt_id": c.get('yt_id'),
            "name": c.get('name', c.get('yt_id', '')),
            "primary_client_id": _oauth_channel_primary_client_id(c),
            "token_client_ids": cids,
            "token_projects": [_oauth_project_of_client_id(x) for x in cids if x],
        })
    return jsonify({"status": "success", "clients": clients, "channels": channels})


@app.route('/api/oauth_client_delete', methods=['POST'])
def oauth_client_delete_api():
    # 🔥 v1.2.25: hapus satu client dari registry (lihat _oauth_delete_client).
    data = request.get_json(silent=True) or {}
    cid = (data.get('client_id') or '').strip()
    if not cid:
        return jsonify({"status": "error", "message": "client_id wajib diisi."}), 400
    ok, msg = _oauth_delete_client(cid)
    return jsonify({"status": "success" if ok else "error", "message": msg}), (200 if ok else 400)


@app.route('/api/oauth_client_primary', methods=['POST'])
def oauth_client_primary_api():
    # 🔥 v1.2.25: jadikan satu client sebagai utama (default device login).
    data = request.get_json(silent=True) or {}
    cid = (data.get('client_id') or '').strip()
    if not cid:
        return jsonify({"status": "error", "message": "client_id wajib diisi."}), 400
    if _oauth_set_primary(cid):
        return jsonify({"status": "success", "message": "Client utama diganti."})
    return jsonify({"status": "error", "message": "Client tidak ditemukan."}), 404


@app.route('/api/set_channel_client', methods=['POST'])
def set_channel_client_api():
    # 🔥 v1.2.25: pemetaan per-channel — jadikan token client tertentu
    # sebagai UTAMA channel (dipindah ke index 0 creds_list). Channel
    # HARUS sudah punya token untuk client itu (device login dulu);
    # kalau belum, tolak dengan pesan yang jelas.
    data = request.get_json(silent=True) or {}
    yt_id = (data.get('yt_id') or '').strip()
    cid = (data.get('client_id') or '').strip()
    if not yt_id or not cid:
        return jsonify({"status": "error", "message": "yt_id dan client_id wajib diisi."}), 400
    entry = _oauth_client_by_id(cid)
    if not entry:
        return jsonify({"status": "error", "message": "Client tidak ditemukan di registry."}), 404
    with db_lock:
        channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if not channel:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        ok, err = _oauth_set_channel_primary_cred(channel, cid)
        if not ok:
            return jsonify({"status": "error",
                            "message": f"Channel '{channel.get('name', yt_id)}' belum dihubungkan di client {entry.get('name') or entry.get('project_id')} — device login channel ini dengan client itu dulu."}), 400
        try: save_channels(database_channel)
        except Exception: pass
    return jsonify({"status": "success",
                    "message": f"Client utama channel '{channel.get('name', yt_id)}' diganti ke {entry.get('name') or entry.get('project_id')}."})

@app.route('/api/delete_secret', methods=['POST'])
def delete_secret():
    # Hapus client UTAMA yang terupload (CRUD: delete). 🔥 v1.2.25:
    # didelegasikan ke registry — client terakhir tidak boleh dihapus.
    try:
        primary = _oauth_primary_client()
        if not primary:
            return jsonify({"status": "error", "message": "Tidak ada API key yang tersimpan."})
        ok, msg = _oauth_delete_client(primary.get('client_id'))
        return jsonify({"status": "success" if ok else "error", "message": msg}), (200 if ok else 400)
    except Exception as e:
        print(f"Gagal hapus client_secret: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal menghapus API key."})

@app.route('/api/upload_secret', methods=['POST'])
def upload_secret():
    # 🔥 v1.2.25: upload client_secret.json = DAFTARKAN ke registry
    # multi-client (bukan lagi menimpa satu file). Upload ulang client
    # yang sama = update, bukan ganda.
    try:
        file = request.files.get('secret_file')
        if not file or not file.filename.endswith('.json'):
            return jsonify({"status": "error", "message": "Harus .json!"})
        name = (request.form.get('client_name') or '').strip() or None
        entry, err = _oauth_register_client(file.read(), name=name)
        if not entry:
            return jsonify({"status": "error", "message": err or "File client tidak valid."}), 400
        return jsonify({"status": "success",
                        "message": f"API Key diunggah! Client {entry.get('name')} (project {entry.get('project_id')}) tersimpan.",
                        "client": {"client_id": entry.get('client_id'),
                                   "project_id": entry.get('project_id'),
                                   "name": entry.get('name'),
                                   "primary": bool(entry.get('primary'))}})
    except Exception as e:
        print(f"Gagal upload client_secret: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal mengunggah API key."})

@app.route('/api/device_code', methods=['POST'])
def device_code_json():
    """📊 SHORTCUT TAMBAH CHANNEL (Dashboard): versi JSON dari /device_login
    untuk modal pop-up — langkah server-nya SAMA PERSIS (minta device code
    ke Google dengan client TV + SCOPES, ikat device_code ke sesi login,
    kedaluwarsa 15 menit). WAJIB login panel (route ini TIDAK masuk
    allowed_routes) + CSRF (POST, dicek check_security). Polling tetap ke
    /api/poll_device_token yang sudah ada. Bila client OAuth belum
    ada, kembalikan error jelas supaya modal mengarahkan ke Settings.
    🔥 v1.2.25: body boleh berisi client_id — client mana yang dipakai
    device login (default: client utama). Client yang dipilih disimpan
    di sesi untuk /api/poll_device_token."""
    data = request.get_json(silent=True) or {}
    _cid = (data.get('client_id') or '').strip() or None
    entry = _oauth_client_by_id(_cid) if _cid else _oauth_primary_client()
    if not entry:
        return jsonify({"status": "error",
                        "message": "Belum ada client OAuth. Upload dulu di Settings."}), 400
    c_id, _c_sec = _oauth_client_conf(entry)
    if not c_id:
        return jsonify({"status": "error",
                        "message": "Client OAuth tidak bisa dibaca. Upload ulang JSON-nya."}), 400
    client_id = c_id
    try:
        res = requests.post('https://oauth2.googleapis.com/device/code',
                            data={'client_id': client_id, 'scope': ' '.join(SCOPES)}, timeout=20).json()
    except Exception as e:
        return jsonify({"status": "error",
                        "message": f"Tidak bisa menghubungi Google: {e}"}), 502
    if 'error' in res:
        return jsonify({"status": "error", "message": f"Error Google: {res['error']}"}), 400
    session['device_code'] = res['device_code']
    session['device_code_at'] = time.time()
    session['device_client_id'] = entry.get('client_id')
    return jsonify({"status": "success",
                    "verification_url": res.get('verification_url', 'https://www.google.com/device'),
                    "user_code": res.get('user_code', ''),
                    "device_code": res['device_code'],
                    "expires_in": res.get('expires_in', 900),
                    "interval": res.get('interval', 5)})


@app.route('/api/analytics_auth_url', methods=['POST'])
def analytics_auth_url():
    """Buat URL login browser untuk AKTIVASI ANALYTICS (jam tayang asli).
    Device flow Google MENOLAK scope Analytics — jadi login memakai alur
    auth-code browser dengan client 'installed' (Desktop) + redirect
    http://localhost (manual: user salin kode dari address bar).
    Kembalikan {auth_url} — user buka di Edge, izinkan, salin `code`."""
    data = request.get_json(silent=True) or {}
    yt_id = (data.get('yt_id') or '').strip()
    if not yt_id:
        return jsonify({"status": "error", "message": "yt_id channel wajib diisi."}), 400
    _cid = (data.get('client_id') or '').strip() or None
    entry = _oauth_client_by_id(_cid) if _cid else _oauth_primary_client()
    if not entry:
        return jsonify({"status": "error", "message": "Belum ada client OAuth. Upload dulu JSON client Desktop di Settings."}), 400
    c_id, _c_sec = _oauth_client_conf(entry)
    if not c_id:
        return jsonify({"status": "error", "message": "Client OAuth tidak bisa dibaca. Upload ulang JSON-nya."}), 400
    qs = urllib.parse.urlencode({
        'client_id': c_id, 'redirect_uri': 'http://localhost',
        'response_type': 'code', 'scope': ' '.join(SCOPES + [ANALYTICS_SCOPE, 'openid', 'email']),
        'access_type': 'offline', 'prompt': 'consent', 'state': yt_id})
    return jsonify({"status": "success",
                    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?" + qs,
                    "client_name": entry.get('name') or entry.get('project_id') or ''})


@app.route('/api/analytics_auth_code', methods=['POST'])
def analytics_auth_code():
    """Tukar `code` dari login browser menjadi token + simpan ke channel.
    Token disimpan dengan scope Analytics (menggantikan token lama dari
    client yang sama bila ada). Setelah ini, jam tayang asli langsung
    terbaca di tabel (cache lama dibersihkan)."""
    data = request.get_json(silent=True) or {}
    yt_id = (data.get('yt_id') or '').strip()
    code = (data.get('code') or '').strip()
    if not yt_id or not code:
        return jsonify({"status": "error", "message": "Channel dan kode wajib diisi."}), 400
    _cid = (data.get('client_id') or '').strip() or None
    entry = _oauth_client_by_id(_cid) if _cid else _oauth_primary_client()
    if not entry:
        return jsonify({"status": "error", "message": "Belum ada client OAuth."}), 400
    c_id, c_sec = _oauth_client_conf(entry)
    if not c_id or not c_sec:
        return jsonify({"status": "error", "message": "Client OAuth tidak bisa dibaca."}), 400
    try:
        res = requests.post('https://oauth2.googleapis.com/token',
            data={'code': code, 'client_id': c_id, 'client_secret': c_sec,
                  'redirect_uri': 'http://localhost', 'grant_type': 'authorization_code'},
            timeout=20).json()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Tidak bisa menghubungi Google: {e}"}), 502
    if 'error' in res:
        return jsonify({"status": "error", "message": f"Error Google: {res.get('error_description') or res['error']}"}), 400
    if not res.get('refresh_token'):
        return jsonify({"status": "error", "message": "Google tidak mengembalikan refresh_token — ulangi login dan pastikan memilih akun Google yang benar."}), 400
    try:
        creds = Credentials(token=res['access_token'], refresh_token=res['refresh_token'],
                            token_uri='https://oauth2.googleapis.com/token',
                            client_id=c_id, client_secret=c_sec,
                            scopes=SCOPES + [ANALYTICS_SCOPE, 'openid', 'email'])
        creds_json = creds.to_json()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal menyimpan token: {e}"}), 500
    owner_email = _id_token_email(res.get('id_token'))
    channels = load_channels()
    ch = next((x for x in channels if x.get('yt_id') == yt_id), None)
    if not ch:
        return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
    with db_lock:
        lst = [s for s in (ch.get('creds_list') or []) if s]
        out, replaced = [], False
        for s in lst:
            try:
                _cid2 = json.loads(s).get('client_id')
            except Exception:
                _cid2 = None
            if _cid2 == c_id and not replaced:
                out.append(creds_json)
                replaced = True
            elif _cid2 == c_id:
                continue
            else:
                out.append(s)
        if not replaced:
            out.append(creds_json)
        ch['creds_list'] = out
        ch['status'] = f"Connected 🟢 {_keys_label(len(out))}"
        if owner_email:
            ch['owner_email'] = owner_email
        save_channels(channels)
    try:
        cache = _load_video_stats_cache()
        (cache.get('watch_real') or {}).pop(yt_id, None)
        (cache.get('analytics_scope') or {}).pop(yt_id, None)
        _save_video_stats_cache(cache)
    except Exception:
        pass
    _msg = f"Analytics aktif untuk {ch.get('name') or yt_id} — jam tayang asli akan muncul di tabel."
    if owner_email:
        _msg += f" Email pemilik tercatat: {owner_email}."
    return jsonify({"status": "success", "message": _msg})


@app.route('/api/channel_add_url', methods=['POST'])
def channel_add_url():
    """Buat URL login browser untuk TAMBAH CHANNEL 1x LOGIN: satu kali
    login memberi SEMUA izin sekaligus (upload + Analytics + email).
    Device flow tidak bisa dipakai untuk ini (Google menolak scope
    Analytics di device flow). Kembalikan {auth_url} — user buka di Edge,
    izinkan, salin `code`."""
    data = request.get_json(silent=True) or {}
    _cid = (data.get('client_id') or '').strip() or None
    entry = _oauth_client_by_id(_cid) if _cid else _oauth_desktop_client()
    if not entry:
        return jsonify({"status": "error", "message": "Belum ada client OAuth. Upload dulu JSON client Desktop di Settings."}), 400
    c_id, _c_sec = _oauth_client_conf(entry)
    if not c_id:
        return jsonify({"status": "error", "message": "Client OAuth tidak bisa dibaca. Upload ulang JSON-nya."}), 400
    qs = urllib.parse.urlencode({
        'client_id': c_id, 'redirect_uri': 'http://localhost',
        'response_type': 'code',
        'scope': ' '.join(SCOPES + [ANALYTICS_SCOPE, 'openid', 'email']),
        'access_type': 'offline', 'prompt': 'consent', 'state': 'add_channel'})
    return jsonify({"status": "success",
                    "auth_url": "https://accounts.google.com/o/oauth2/v2/auth?" + qs,
                    "client_name": entry.get('name') or entry.get('project_id') or ''})


@app.route('/api/channel_add_code', methods=['POST'])
def channel_add_code():
    """Tukar `code` login browser menjadi channel baru (1x login).
    Token disimpan dengan SEMUA scope (upload + Analytics + email).
    Channel dikenali dari token itu sendiri (mine=True); bila channel
    sudah ada, token baru ditambahkan ke channel tersebut."""
    data = request.get_json(silent=True) or {}
    code = (data.get('code') or '').strip()
    if not code:
        return jsonify({"status": "error", "message": "Kode wajib diisi."}), 400
    _cid = (data.get('client_id') or '').strip() or None
    entry = _oauth_client_by_id(_cid) if _cid else _oauth_desktop_client()
    if not entry:
        return jsonify({"status": "error", "message": "Belum ada client OAuth."}), 400
    c_id, c_sec = _oauth_client_conf(entry)
    if not c_id or not c_sec:
        return jsonify({"status": "error", "message": "Client OAuth tidak bisa dibaca."}), 400
    try:
        res = requests.post('https://oauth2.googleapis.com/token',
            data={'code': code, 'client_id': c_id, 'client_secret': c_sec,
                  'redirect_uri': 'http://localhost', 'grant_type': 'authorization_code'},
            timeout=20).json()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Tidak bisa menghubungi Google: {e}"}), 502
    if 'error' in res:
        return jsonify({"status": "error", "message": f"Error Google: {res.get('error_description') or res['error']}"}), 400
    if not res.get('refresh_token'):
        return jsonify({"status": "error", "message": "Google tidak mengembalikan refresh_token — ulangi login dan pastikan memilih akun Google yang benar."}), 400
    try:
        creds = Credentials(token=res['access_token'], refresh_token=res['refresh_token'],
                            token_uri='https://oauth2.googleapis.com/token',
                            client_id=c_id, client_secret=c_sec,
                            scopes=SCOPES + [ANALYTICS_SCOPE, 'openid', 'email'])
        creds_json = creds.to_json()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal menyimpan token: {e}"}), 500
    owner_email = _id_token_email(res.get('id_token'))
    try:
        youtube = build('youtube', 'v3', credentials=creds)
        chan_res = youtube.channels().list(part="snippet", mine=True).execute()
    except Exception as e:
        return jsonify({"status": "error", "message": f"Tidak bisa membaca channel: {e}"}), 502
    items = (chan_res or {}).get('items') or []
    if not items:
        return jsonify({"status": "error", "message": "Akun Google ini tidak memiliki channel YouTube. Ulangi dengan akun yang punya channel."}), 400
    item = items[0]
    exists = any(c.get('yt_id') == item['id'] for c in database_channel)
    session['pending_add'] = {
        'creds_json': creds_json, 'client_id': c_id, 'owner_email': owner_email,
        'yt_id': item['id'], 'name': item['snippet']['title'],
        'thumbnail': item['snippet']['thumbnails']['default']['url'],
        'scopes': 'full'}
    return jsonify({"status": "confirm",
                    "channel": {"yt_id": item['id'], "name": item['snippet']['title'],
                                "thumbnail": item['snippet']['thumbnails']['default']['url'],
                                "owner_email": owner_email},
                    "exists": exists,
                    "scopes_note": "1x login — upload + Analytics + email."})


@app.route('/api/channel_add_confirm', methods=['POST'])
def channel_add_confirm():
    """Konfirmasi tambah channel (dipakai device flow + 1x login): simpan
    token yang ditunda di sesi menjadi channel."""
    pend = session.get('pending_add') or {}
    creds_json = pend.get('creds_json') or ''
    c_id = pend.get('client_id') or ''
    if not creds_json or not c_id:
        return jsonify({"status": "error", "message": "Sesi penambahan channel tidak ditemukan. Ulangi login."}), 400
    item = {'id': pend.get('yt_id'), 'snippet': {'title': pend.get('name') or '?',
            'thumbnails': {'default': {'url': pend.get('thumbnail') or ''}}}}
    try:
        created, _name = _channel_add_save(item, creds_json, c_id, pend.get('owner_email') or '')
    except Exception as e:
        return jsonify({"status": "error", "message": f"Gagal menyimpan channel: {e}"}), 500
    session.pop('pending_add', None)
    if created:
        _msg = f"Channel {_name} berhasil ditambahkan."
    else:
        _msg = f"Channel {_name} sudah ada — token baru ditambahkan ke channel ini."
    if pend.get('owner_email'):
        _msg += f" Email pemilik: {pend['owner_email']}."
    return jsonify({"status": "success", "message": _msg, "channel": _name, "yt_id": pend.get('yt_id'), "created": created})


@app.route('/api/channel_add_cancel', methods=['POST'])
def channel_add_cancel():
    """Batalkan penambahan channel yang menunggu konfirmasi."""
    session.pop('pending_add', None)
    return jsonify({"status": "success", "message": "Penambahan channel dibatalkan."})


    return jsonify({"status": "success", "message": _msg, "channel": _name, "yt_id": item['id'], "created": created})


@app.route('/api/generate_tv_link')
def generate_tv_link():
    # 🔥 v1.2.25: ?client=<client_id> memilih client device login-nya.
    _cid = (request.args.get('client') or '').strip() or None
    entry = _oauth_client_by_id(_cid) if _cid else _oauth_primary_client()
    if not entry:
        return jsonify({"auth_url": "", "error": "File client OAuth belum ada! Upload dulu di Settings."})
    _qs = f"?client={entry.get('client_id')}" if _cid else ""
    return jsonify({"auth_url": f"http://{request.host}/device_login{_qs}"})

@app.route('/device_login')
def device_login():
    # 🔒 #3: route ini SUDAH TIDAK di allowed_routes — wajib login panel
    # (check_security). Device code diikat ke sesi login yang memintanya.
    # 🔥 v1.2.25: ?client=<client_id> memilih client (default: utama).
    _cid = (request.args.get('client') or '').strip() or None
    entry = _oauth_client_by_id(_cid) if _cid else _oauth_primary_client()
    if not entry:
        return "File rahasia tidak ditemukan!"
    c_id, _c_sec = _oauth_client_conf(entry)
    if not c_id:
        return "Client OAuth tidak bisa dibaca. Upload ulang JSON-nya di Settings."
    client_id = c_id
    res = requests.post('https://oauth2.googleapis.com/device/code', data={'client_id': client_id, 'scope': ' '.join(SCOPES)}).json()
    if 'error' in res: return f"Error Google: {res['error']}"
    session['device_code'] = res['device_code']
    session['device_code_at'] = time.time()
    session['device_client_id'] = entry.get('client_id')
    _csrf = _get_csrf_token()
    # Kode + URL di-escape HTML (datang dari Google, tapi jangan pernah
    # menaruh string luar mentah ke HTML).
    _vurl = _html.escape(str(res['verification_url']))
    _ucode = _html.escape(str(res['user_code']))
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
            <div style="font-size:13px;color:#64748b;margin-bottom:20px">Izin yang diminta: kelola video YouTube (upload &amp; statistik).</div>
            <div class="step"><b>Langkah 1:</b> Copy link ini dan Paste di browser target:
                <div class="input-group"><input type="text" id="glink" value="{_vurl}" readonly><button onclick="document.getElementById('glink').select();document.execCommand('copy');">Copy Link</button></div>
            </div>
            <div class="step"><b>Langkah 2:</b> Masukkan Kode Rahasia ini:
                <div class="input-group"><input type="text" id="gcode" value="{_ucode}" readonly><button onclick="document.getElementById('gcode').select();document.execCommand('copy');">Copy Kode</button></div>
            </div>
            <div id="status" style="margin-top:30px; font-weight:bold;">⏳ Menunggu Anda memasukkan kode...</div>
        </div>
        <script>
            function poll() {{ fetch('/api/poll_device_token', {{ method: 'POST', headers: {{'Content-Type': 'application/json', 'X-CSRF-Token': '{_csrf}'}}, body: JSON.stringify({{device_code: '{_html.escape(str(res['device_code']))}'}}) }}).then(r => r.json()).then(data => {{ if(data.status === 'success') {{ document.getElementById('status').textContent = "🎉 Berhasil! Mengalihkan..."; setTimeout(() => {{ window.location.href = '/'; }}, 2000); }} else if(data.status === 'pending') {{ setTimeout(poll, data.interval || 5000); }} else if(data.status === 'error') {{ document.getElementById('status').textContent = "❌ " + (data.error || 'Gagal menautkan channel.'); }} }}); }}
            setTimeout(poll, 5000);
        </script>
    </body></html>
    """
    return html

@app.route('/api/poll_device_token', methods=['POST'])
def poll_device_token():
    # 🔒 #3: wajib login panel (bukan allowed_routes) + device_code harus
    # sama dengan yang diminta sesi ini + rate-limit + guard body non-JSON
    # (dulu: body non-JSON → 500, device_code apa pun diterima).
    data = request.get_json(silent=True)
    if data is None:
        return jsonify({"status": "error", "error": "Body request harus JSON."}), 400
    device_code = data.get('device_code')
    if not device_code or not isinstance(device_code, str):
        return jsonify({"status": "error", "error": "device_code wajib diisi."}), 400
    expected = session.get('device_code')
    if not expected:
        return jsonify({"status": "error", "error": "Sesi device login tidak ditemukan. Buka /device_login lagi."}), 400
    if not hmac.compare_digest(str(device_code), str(expected)):
        return jsonify({"status": "error", "error": "device_code tidak cocok dengan sesi ini."}), 403
    if time.time() - float(session.get('device_code_at') or 0) > 900:
        session.pop('device_code', None)
        return jsonify({"status": "error", "error": "Kode device sudah kedaluwarsa (15 menit). Buka /device_login lagi."}), 400
    ip = request.remote_addr or '?'
    if _poll_rate_limited(ip):
        return jsonify({"status": "error", "error": "Terlalu banyak percobaan poll. Tunggu sebentar."}), 429
    # 🔥 v1.2.25: token diminta dengan client yang dipilih saat device
    # login (disimpan di sesi); fallback file tunggal lama bila registry
    # masih kosong (kompatibilitas).
    _entry = _oauth_client_by_id(session.get('device_client_id')) if session.get('device_client_id') else _oauth_primary_client()
    if _entry:
        c_id, c_sec = _oauth_client_conf(_entry)
    else:
        c_id = c_sec = None
    if not c_id:
        if not os.path.exists(CLIENT_SECRETS_FILE):
            return jsonify({"status": "error", "error": "Client OAuth tidak ditemukan. Upload dulu di Settings."}), 400
        with open(CLIENT_SECRETS_FILE, 'r') as f:
            s_data = json.load(f); conf = s_data.get('installed', s_data.get('web', {})); c_id = conf.get('client_id'); c_sec = conf.get('client_secret')
    res = requests.post('https://oauth2.googleapis.com/token', data={'client_id': c_id, 'client_secret': c_sec, 'device_code': device_code, 'grant_type': 'urn:ietf:params:oauth:grant-type:device_code'}).json()
    if 'error' in res:
        err = res['error']
        if err == 'authorization_pending': return jsonify({"status": "pending", "interval": 5000})
        elif err == 'slow_down': return jsonify({"status": "pending", "interval": 10000})
        else: return jsonify({"status": "error", "error": err})
    session.pop('device_code', None)
    session.pop('device_code_at', None)
    session.pop('device_client_id', None)
    creds = Credentials(token=res['access_token'], refresh_token=res.get('refresh_token'), token_uri='https://oauth2.googleapis.com/token', client_id=c_id, client_secret=c_sec, scopes=SCOPES)
    youtube = build('youtube', 'v3', credentials=creds); chan_res = youtube.channels().list(part="snippet", mine=True).execute()
    if chan_res['items']:
        item = chan_res['items'][0]
        session['pending_add'] = {
            'creds_json': creds.to_json(), 'client_id': c_id, 'owner_email': '',
            'yt_id': item['id'], 'name': item['snippet']['title'],
            'thumbnail': item['snippet']['thumbnails']['default']['url'],
            'scopes': 'upload'}
        _exists = any(c.get('yt_id') == item['id'] for c in database_channel)
        return jsonify({"status": "confirm",
                        "channel": {"yt_id": item['id'], "name": item['snippet']['title'],
                                    "thumbnail": item['snippet']['thumbnails']['default']['url'],
                                    "owner_email": ""},
                        "exists": _exists,
                        "scopes_note": "Device login — token upload saja. Analytics + email perlu login browser 1x lagi."})
        return jsonify({"status": "success"})
    # 🔥 FIX v1.3: jangan klaim "Berhasil" kalau akun Google-nya tidak punya channel
    # (sebelumnya halaman tetap bilang Berhasil padahal tidak ada yang ketaut).
    return jsonify({"status": "error", "error": "Akun Google ini tidak memiliki channel YouTube. Ulangi dengan akun yang punya channel."})

# --- BATCH CREATOR ---
def _tg_escape(text):
    """Escape karakter Markdown Telegram supaya judul/deskripsi video
    (yang bisa mengandung *, _, `, [ dll.) tidak merusak format pesan.
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

def _send_approval_previews(batch_id, channel_name, previews):
    """Kirim SATU pesan preview per video ke owner Telegram (dijalankan di
    thread background supaya respons batch_create tidak menunggu Telegram).
    Setiap pesan membawa tombol ✅ Setujui / ❌ Tolak + tombol Setujui
    Semua di pesan pertama. Kalau token Telegram belum diset / operator
    mati, pengiriman gagal silent — task tetap aman berstatus pending
    dan terlihat di panel. 🔥 v1.2.24: paket lengkap sebelum publish —
    thumbnail baru dikirim sebagai foto dulu (bila ada di preview),
    lalu teks preview berisi judul SEO + 2 baris pertama deskripsi
    (yang sudah dibersihkan) + tag kata kunci."""
    total = len(previews)
    for idx, pv in enumerate(previews):
        # Thumbnail baru dulu (playlist) — gagal kirim foto tidak
        # menggagalkan preview teks di bawah.
        thumb = (pv.get('thumb_path') or '').strip()
        if thumb and os.path.isfile(thumb):
            send_tg_photo(thumb, caption=f"Thumbnail baru — {pv.get('title', '?')}")
        # 2 baris pertama deskripsi (bukan 200 karakter pertama) — baris
        # 1 deskripsi = judul SEO, baris 2 = awal isi yang dibersihkan.
        desc_lines = [l.strip() for l in (pv.get('description') or '').splitlines() if l.strip()][:2]
        desc_joined = ' '.join(desc_lines)
        desc_txt = _tg_escape(desc_joined[:300]) + ("…" if len(desc_joined) > 300 else "") if desc_joined else "_tidak ada deskripsi_"
        tags_txt = _tg_escape((pv.get('tags') or '').strip() or '-')
        text = (
            f"📋 *Bos, batch baru nih, dicek dulu ya* 👀 ({idx + 1}/{total})\n"
            f"📺 Channel: *{_tg_escape(channel_name)}*\n"
            f"🎬 Judul: *{_tg_escape(pv.get('title', '?'))}*\n"
            f"🗓️ Jadwal publish: `{_tg_escape(pv.get('publish_date', '-'))}`\n"
            # 🔥 FASE 8: preview Shorts membawa duration_label ("30 detik
            # (Shorts)") supaya tidak tampil "0.008333333333333333 jam".
            f"⏱️ Durasi target: `{pv.get('duration_label') or str(pv.get('duration', '-')) + ' jam'}` • 🔒 `{_tg_escape(pv.get('privacy', 'public'))}`\n"
            f"📝 Deskripsi: {desc_txt}\n"
            f"Tag: {tags_txt}"
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
    (kuota project 100/hari, hindari burst/rate-limit). Tanpa bentrok,
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

# ============================================================
# 🧹 v1.2.13: BERSIHKAN emoji & simbol dari judul/deskripsi/tag
# sebelum upload ke YouTube. User + temannya pernah gagal upload
# karena karakter "kotor" (emoji, kadang simbol biasa) di judul /
# deskripsi. Aturan: hanya teks polos yang lolos — huruf (termasuk
# beraksen), angka, spasi, baris baru, dan tanda baca aman
# . , ! ? - : ; ' " ( ) [ ] & % + / @ #. Sisanya dibuang; tanda
# baca fancy dinormalisasi ke ASCII bila wajar ("…"→"...",
# "—"/"–"→"-", "‘’"→"'", "“”"→'"'). Dipakai di SEMUA jalur
# pembuatan teks upload + jaring pengaman terakhir tepat sebelum
# videos.insert.
# ============================================================
_CLEAN_FANCY_MAP = {
    '\u2018': "'", '\u2019': "'", '\u201a': "'", '\u201b': "'",
    '\u201c': '"', '\u201d': '"', '\u201e': '"', '\u201f': '"',
    '\u00ab': '"', '\u00bb': '"', '\u2039': "'", '\u203a': "'",
    '\u2013': '-', '\u2014': '-', '\u2212': '-',
    '\u2026': '...', '\u2044': '/',
}
_CLEAN_SPACE_CHARS = frozenset(
    ['\u00a0', '\u1680', '\u2000', '\u2001', '\u2002', '\u2003',
     '\u2004', '\u2005', '\u2006', '\u2007', '\u2008', '\u2009',
     '\u200a', '\u202f', '\u205f', '\u3000'])
_CLEAN_SAFE_PUNCT = frozenset('.,!?-:;\'"()[]&%+/@#')
# Karakter yang DIBUANG eksplisit (sebagian lolos isalnum(), jadi
# filter "hanya alnum" saja tidak cukup): kontrol, invisible,
# variation selector, simbol/dingbat, emoji, angka/huruf berlingkar,
# superskrip, angka Romawi, private-use.
_CLEAN_REMOVE_RE = re.compile(
    '['
    '\u0000-\u0008\u000b\u000c\u000e-\u001f\u007f-\u009f'  # kontrol + C1
    # (tab, LF, CR sengaja TIDAK di sini: tab→spasi, LF dipertahankan)
    '\u00a9\u00ae\u00aa\u00ba\u00ad'   # © ® ª º soft-hyphen
    '\u061c\u180e'                    # arabic letter mark, mongolian VS
    '\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff'  # zero-width / bidi / BOM
    '\ufe00-\ufe0f\ufe20-\ufe2f'      # variation selector
    '\u20d0-\u20ff\u1ab0-\u1aff\u1dc0-\u1dff'  # combining enclosing
    '\u203c\u2049\u2122\u2139\u3030\u303d\u3297\u3299'  # ‼ ⁉ ™ ℹ 〰 〽 ㊗ ㊙
    '\u2190-\u21ff'                   # panah
    '\u2300-\u23ff'                   # simbol teknis (⌚ ⌛ ⏰)
    '\u2460-\u24ff'                   # ① ② Ⓐ ⓨ ...
    '\u2500-\u257f\u2580-\u259f'      # box drawing, block
    '\u25a0-\u25ff\u2600-\u26ff'      # geometri ★■▶, misc ☀♠♥♪
    '\u2700-\u27bf\u2b00-\u2bff'      # dingbat ✓✗, misc ⬛⭐
    '\u3200-\u32ff\u3300-\u33ff'      # enclosed CJK ㊗ ㍿ ...
    '\u00b2\u00b3\u00b9\u2070-\u207f\u2080-\u208f'  # superskrip/subskrip
    '\u2160-\u217f'                   # angka Romawi Ⅰ Ⅱ ...
    '\ue000-\uf8ff'                   # private use
    '\U0001f000-\U0001faff'           # semua emoji (plane 1)
    '\U000f0000-\U000fffff\U00100000-\U0010ffff'  # private use plane 15/16
    ']')


def _strip_channel_title_suffix(title, channel_name):
    """Hapus suffix nama channel di AKHIR judul (v1.2.23): nama channel
    tidak boleh memakan kuota 100 karakter judul YouTube. Cocok persis
    (case-insensitive, spasi dinormalisasi), hanya di posisi akhir.
    Bentuk yang dihapus — ketiganya pernah dihasilkan
    _playlist_rewrite_title lama: ' (Nama Channel)', ' | Nama Channel',
    ' — Nama Channel' / ' - Nama Channel'. Kurung lain — mis.
    '(Full Album)' — TIDAK disentuh."""
    s = (title or '').strip()
    ch = (channel_name or '').strip()
    if not s or not ch:
        return title
    ch_norm = ' '.join(ch.lower().split())
    m = re.search(r'\(\s*([^()]*?)\s*\)\s*$', s)
    if m and ' '.join(m.group(1).lower().split()) == ch_norm:
        return s[:m.start()].rstrip()
    for sep in (' | ', ' — ', ' - '):
        if sep in s:
            head, _, tail = s.rpartition(sep)
            if ' '.join(tail.lower().split()) == ch_norm:
                return head.rstrip()
    # Bentuk 4: nama channel telanjang di akhir ('X | Channel' — tanda
    # '|' sudah dibuang sanitasi emoji v1.2.13 sebelum pemeriksaan ini,
    # jadi pemisahnya tidak ada lagi). Hanya bila seluruh ekor kata
    # sama persis dengan nama channel (case-insensitive, spasi fleksibel).
    words = [re.escape(w) for w in ch.split()]
    if words:
        pat = r'\s+'.join(words)
        m2 = re.search(r'\s+' + pat + r'\s*$', s, flags=re.I)
        if m2:
            return s[:m2.start()].rstrip()
        if re.fullmatch(pat + r'\s*', s, flags=re.I):
            return ''
    return title


def _clean_upload_text(text, max_len=None, fallback='', strip_channel=None):
    """Bersihkan teks untuk judul/deskripsi YouTube: buang emoji,
    simbol dekoratif, karakter kontrol & invisible; pertahankan huruf
    (termasuk beraksen), angka, spasi, baris baru, dan tanda baca aman.
    Tanda baca fancy dinormalisasi ke ASCII. Spasi ganda dirapikan.
    Bila max_len diberikan, potong di batas kata. Bila hasil bersih
    KOSONG dan fallback diberikan, kembalikan fallback yang sudah
    dibersihkan (tidak pernah string kosong — YouTube menolak
    judul/deskripsi kosong).
    strip_channel (v1.2.23): bila diisi nama channel, suffix nama
    channel di akhir judul dihapus SEBELUM pemotongan max_len, supaya
    judul utuhnya muat. Hanya dipakai di jalur JUDUL — deskripsi &
    tag tidak pernah melewatkan ini."""
    s = '' if text is None else str(text)
    if not s:
        return _clean_upload_text(fallback, max_len=max_len,
                                  strip_channel=strip_channel) if fallback else ''
    s = unicodedata.normalize('NFC', s)
    out = []
    for ch in s:
        o = ord(ch)
        if 0xFF01 <= o <= 0xFF5E:
            out.append(chr(o - 0xFEE0))  # fullwidth → ASCII
        elif ch in _CLEAN_FANCY_MAP:
            out.append(_CLEAN_FANCY_MAP[ch])
        elif ch in _CLEAN_SPACE_CHARS or ch == '\t':
            out.append(' ')
        elif ch == '\r':
            pass  # buang CR; LF dipertahankan untuk deskripsi
        else:
            out.append(ch)
    s = ''.join(out)
    s = _CLEAN_REMOVE_RE.sub('', s)
    kept = []
    for ch in s:
        # Kategori 'M' (combining mark) ikut dipertahankan — vokal/tanda
        # aksara Thai, Devanagari, Arab, dsb. adalah huruf, bukan simbol.
        # (Mark penutup seperti U+20E3 sudah dibuang regex di atas.)
        if ch == '\n' or ch == ' ' or ch in _CLEAN_SAFE_PUNCT or ch.isalnum() \
                or unicodedata.category(ch).startswith('M'):
            kept.append(ch)
    s = ''.join(kept)
    # Rapikan spasi per baris; baris kosong beruntun jadi satu; buang
    # baris kosong di awal/akhir (baris baru dipertahankan untuk deskripsi).
    lines = [' '.join(ln.split()) for ln in s.split('\n')]
    cleaned_lines = []
    for ln in lines:
        if ln:
            cleaned_lines.append(ln)
        elif cleaned_lines and cleaned_lines[-1] != '':
            cleaned_lines.append('')
    while cleaned_lines and cleaned_lines[-1] == '':
        cleaned_lines.pop()
    s = '\n'.join(cleaned_lines)
    # 🔥 v1.2.23: hapus suffix nama channel SEBELUM pemotongan max_len —
    # kalau dihapus sesudah potong, suffix yang sudah terpotong ('(Cozy
    # Sax') tidak akan cocok dan judul tetap kepotong. Hanya bila
    # strip_channel diisi (jalur judul saja).
    if strip_channel:
        s = _strip_channel_title_suffix(s, strip_channel)
    if max_len is not None and len(s) > max_len:
        cut = s[:max_len]
        if s[max_len] not in (' ', '\n'):
            sp = max(cut.rfind(' '), cut.rfind('\n'))
            if sp > max_len // 2:
                cut = cut[:sp]
        s = cut.rstrip(' .,!?;:-').rstrip()
    if not s and fallback:
        return _clean_upload_text(fallback, max_len=max_len,
                                  strip_channel=strip_channel)
    return s


def _clean_upload_tags(raw, max_total=400, max_each=30):
    """Bersihkan tag YouTube: tiap tag dibersihkan seperti judul
    (emoji/simbol dibuang, '#' dibuang seperti perilaku lama), kosong
    dibuang, duplikat dibuang, tiap tag maks 30 karakter, total maks
    400 karakter. Kembalikan LIST tag bersih — pemanggil yang
    menggabungkan dengan ', '."""
    if isinstance(raw, str):
        items = raw.split(',')
    elif raw:
        items = list(raw)
    else:
        items = []
    out = []
    for t in items:
        c = _clean_upload_text(t, max_len=max_each).replace('#', '').strip()
        if not c or c in out:
            continue
        # v1.2.24: total dihitung pada string gabungan AKHIR (', '.join)
        # supaya hasil gabungan tidak pernah melewati max_total (hitungan
        # lama +1 per tag bisa lolos ~2 karakter per tag).
        if len(', '.join(out + [c])) > max_total:
            continue
        out.append(c)
    return out


def _shorts_norm_title(title):
    """Normalisasi judul untuk perbandingan duplikat: kecil + spasi tunggal."""
    return ' '.join((str(title or '')).lower().split())

def _shorts_used_titles(yt_id):
    """Judul yang SUDAH dipakai channel ini dan tidak boleh dipakai lagi:
    task AKTIF (menunggu approval/antrean — belum tayang tapi sudah
    dijadwalkan) + history yang statusnya 'Tayang'. Entri Gagal/Ditolak
    TIDAK dihitung — slot & judulnya bebas dipakai ulang."""
    used = set()
    with db_lock:
        for t in list(active_tasks):
            if (t.get('yt_id') or (t.get('blueprint') or {}).get('yt_id')) == yt_id:
                used.add(_shorts_norm_title(t.get('title') or ''))
        for h in list(history_tasks):
            if h.get('yt_id') != yt_id:
                continue
            if 'Tayang' not in (h.get('status') or ''):
                continue
            used.add(_shorts_norm_title(h.get('title') or ''))
    used.discard('')
    return used

def _shorts_dedup_title(yt_id, title, channel_rec=None):
    """Pastikan judul Short belum dipakai di channel ini (2026-11-19:
    batch 4 Short memakai judul yang sama dengan yang sudah tayang, lalu
    ditolak QC). Bila bentrok: coba bank judul channel dulu (judul
    berikutnya yang belum dipakai); bila bank habis → varian bernomor
    'Judul (2)', '(3)', … JANGAN PERNAH mengembalikan judul yang pasti
    ditolak QC."""
    base = (str(title or '')).strip()
    if not base:
        return base
    used = _shorts_used_titles(yt_id)
    if _shorts_norm_title(base) not in used:
        return base
    bank = []
    try:
        bank = [t for t in ((channel_rec or {}).get('title_bank') or [])
                if isinstance(t, str) and t.strip()]
    except Exception:
        bank = []
    for cand in bank:
        if _shorts_norm_title(cand) not in used:
            return cand.strip()
    n = 2
    while n < 100:
        cand = f"{base} ({n})"
        if _shorts_norm_title(cand) not in used:
            return cand
        n += 1
    return f"{base} ({n})"

def _shorts_avoid_same_channel_collision(v_date_str, yt_id):
    """Jaring pengaman jadwal Short (2026-11-19, aturan jeda 6 jam): bila
    tanggal+jam sudah dipakai task aktif channel ini atau video yang sudah
    tayang, geser +6 jam sampai kosong (maks 40 geseran ≈ 10 hari).
    Operator sudah menghitung slot kosong duluan; ini hanya pengaman bila
    ada yang lewat jalur lain (panel, penjadwal otomatis) atau slot jadi
    terisi di antara konfirmasi & eksekusi."""
    try:
        dt = datetime.strptime(str(v_date_str), '%Y-%m-%d %H:%M')
    except Exception:
        return v_date_str
    for _ in range(40):
        occ = set()
        with db_lock:
            for t in list(active_tasks):
                if (t.get('yt_id') or (t.get('blueprint') or {}).get('yt_id')) == yt_id:
                    d = t.get('time') or (t.get('blueprint') or {}).get('publish_date')
                    if d:
                        occ.add(str(d)[:16])
            for h in list(history_tasks):
                if h.get('yt_id') != yt_id or 'Tayang' not in (h.get('status') or ''):
                    continue
                d = h.get('time') or (h.get('blueprint') or {}).get('publish_date')
                if d:
                    occ.add(str(d)[:16])
        if dt.strftime('%Y-%m-%d %H:%M') not in occ:
            return dt.strftime('%Y-%m-%d %H:%M')
        dt = dt + timedelta(hours=6)
    return dt.strftime('%Y-%m-%d %H:%M')

# ============================================================
# 🔥 FASE 8: SHORTS MUSE — buat short dari naskah + CTA
# ============================================================
@app.route('/api/shorts_muse_create', methods=['POST'])
def shorts_muse_create():
    """Buat SATU Shorts Muse (panel). Butuh login panel (route TIDAK masuk
    allowed_routes). Inti pembuatan ada di _create_shorts_muse_task —
    dipakai bersama route operator Telegram."""
    data = request.get_json(silent=True) or {}
    res = _create_shorts_muse_task(data)
    if not res["ok"]:
        return jsonify({"status": "error", "message": res["message"]}), res["http"]
    if res.get("preview"):
        threading.Thread(target=_send_approval_previews,
                         args=(res["batch_id"], res["channel_name"], [res["preview"]]), daemon=True).start()
    return jsonify({"status": "success", "message": res["message"], "task_id": res["task_id"],
                    "title": res.get("title"), "title_cleaned": res.get("title_cleaned", False),
                    "bg_auto": res.get("bg_auto", False),
                    "bg_video": res.get("bg_video", False),
                    "bg_video_count": res.get("bg_video_count", 0)})


def _create_shorts_muse_task(data):
    """Inti pembuatan SATU Shorts Muse — dipakai route panel (login) dan
    route operator Telegram (operator_key). Alurnya SAMA seperti
    batch_create: stagger jam (Fase 6), autopilot auto vs approval
    (Fase 6/2), dijeda → ditahan (Fase 3), lalu worker yang sama
    (disk → render vertikal → QC → upload → kuota). Privacy dipaksa
    'private' seperti Clipper — owner yang mengubahnya di YouTube.
    Mengembalikan dict: ok/message/http/task_id/auto_mode/batch_id/
    preview (dict preview approval, atau None bila auto)/channel_name/
    publish_date. TIDAK mengirim preview Telegram — pemanggil yang
    memicu _send_approval_previews bila preview tidak None."""
    yt_id = (data.get('yt_id') or '').strip()
    title = (data.get('title') or '').strip()
    if not yt_id or not title:
        return {"ok": False, "message": "Channel dan judul wajib diisi.", "http": 400}
    channel_rec = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if channel_rec is None:
        return {"ok": False, "message": "Channel tidak ditemukan.", "http": 404}
    # Judul duplikat dilewati di sini (2026-11-19): jangan pernah membuat
    # task dengan judul yang pasti ditolak QC — ganti ke judul bank
    # berikutnya yang belum dipakai, atau varian bernomor.
    title = _shorts_dedup_title(yt_id, title, channel_rec)
    # 🧹 v1.2.13: bersihkan emoji/simbol dari judul (bank judul & ketikan
    # user bisa mengandung emoji) — lalu dedup ulang, karena pembersihan
    # bisa saja menyamakan judul dengan yang sudah dipakai.
    _raw_title = title
    _date_s = _wib_now().strftime('%Y-%m-%d')
    title = _clean_upload_text(title, max_len=100,
                               fallback=f"Shorts {_date_s}",
                               strip_channel=channel_rec.get('name', ''))
    title_cleaned = (title != _raw_title)
    if title != _raw_title:
        title = _shorts_dedup_title(yt_id, title, channel_rec)
    hook = (data.get('hook') or '').strip()
    script = (data.get('script') or '').strip()
    if not hook and not script:
        return {"ok": False, "message": "Isi Hook atau Naskah — salah satunya wajib ada.", "http": 400}
    cta_text = (data.get('cta') or SHORTS_MUSE_DEFAULT_CTA).strip() or SHORTS_MUSE_DEFAULT_CTA
    try:
        duration_sec = int(float(data.get('duration_sec') or 30))
    except Exception:
        duration_sec = 30
    duration_sec = max(SHORTS_MUSE_MIN_SEC, min(SHORTS_MUSE_MAX_SEC, duration_sec))
    try:
        pub_dt = datetime.strptime(data.get('publish_date', ''), '%Y-%m-%dT%H:%M')
    except Exception:
        try:
            pub_dt = datetime.strptime(data.get('publish_date', ''), '%Y-%m-%d %H:%M')
        except Exception:
            return {"ok": False, "message": "Format tanggal salah (pakai YYYY-MM-DD HH:MM).", "http": 400}
    v_date_str = _stagger_publish_date(pub_dt.strftime('%Y-%m-%d %H:%M'), yt_id)
    # Jaring pengaman: jangan menimpa slot channel sendiri (aturan jeda 6 jam).
    v_date_str = _shorts_avoid_same_channel_collision(v_date_str, yt_id)
    text_color = (data.get('text_color') or '#FFFFFF').strip()
    if not re.fullmatch(r'#[0-9A-Fa-f]{6}', text_color):
        text_color = '#FFFFFF'
    bg_file = (data.get('bg_file') or '').strip()
    # 🎬 Daftar klip video eksplisit (panel: checkbox "semua klip video").
    bg_files = data.get('bg_files') or []
    if isinstance(bg_files, str):
        bg_files = [bg_files]
    bg_clips = []
    bg_video_count = 0
    if bg_files:
        folder = get_channel_folder(yt_id, "backgrounds")
        for f in bg_files:
            if not isinstance(f, str) or not f.strip():
                continue
            p = os.path.join(folder, os.path.basename(f.strip()))
            if os.path.isfile(p) and p.lower().endswith(_SHORTS_VIDEO_BG_EXTS):
                bg_clips.append(os.path.basename(f.strip()))
        if not bg_clips:
            return {"ok": False, "message": "Klip video background tidak ditemukan di galeri channel.", "http": 400}
        bg_file = bg_clips[0]
        bg_video_count = len(bg_clips)
    elif bg_file:
        bg_path = os.path.join(get_channel_folder(yt_id, "backgrounds"), os.path.basename(bg_file))
        if not os.path.isfile(bg_path):
            return {"ok": False, "message": f"Background '{bg_file}' tidak ditemukan di galeri channel.", "http": 400}
        if bg_path.lower().endswith(_SHORTS_VIDEO_BG_EXTS):
            bg_clips = [os.path.basename(bg_file)]
            bg_video_count = 1
    # Tema untuk fallback gradien otomatis (panel: tidak ada field tema →
    # default 'sleep'; operator/penjadwal mengirim tema yang dipilih).
    theme = _shorts_muse_norm_theme(data.get('theme') or data.get('niche'))
    bg_auto = False
    if not bg_file:
        # Tanpa pilihan eksplisit: galeri berisi klip video → pakai SEMUA
        # klip (urut nama file, disambung saat render); bila tidak ada video
        # tapi ada gambar → perilaku lama (acak saat render); bila kosong
        # total → gradien otomatis.
        videos = _shorts_muse_list_bg_videos(yt_id)
        if videos:
            bg_clips = videos
            bg_file = videos[0]
            bg_video_count = len(videos)
        else:
            try:
                has_bg = bool(get_multi_backgrounds(yt_id, 1))
            except Exception:
                has_bg = False
            if not has_bg:
                # Galeri kosong → buat gradien otomatis sekali (dipakai ulang).
                # HANYA bila pembuatan gradien ini gagal, Short ditolak karena
                # background kosong.
                auto_path, _auto_created = _shorts_muse_ensure_auto_bg(yt_id, theme)
                if not auto_path:
                    return {"ok": False, "message": "Galeri background kosong dan background otomatis gagal dibuat — upload background dulu di panel.", "http": 500}
                bg_file = os.path.basename(auto_path)
                bg_auto = True
    bgm_file = (data.get('bgm_file') or '').strip()
    if bgm_file and bgm_file.lower() != 'none':
        bgm_path = os.path.join(get_channel_folder(yt_id, "audios"), os.path.basename(bgm_file))
        if not os.path.isfile(bgm_path):
            return {"ok": False, "message": f"Musik '{bgm_file}' tidak ditemukan di galeri channel.", "http": 400}
    else:
        bgm_file = 'none'
    description = (data.get('description') or '').strip()
    if not description:
        # Deskripsi otomatis dari naskah (QC Fase 5 mewajibkan deskripsi
        # tidak kosong) — dipotong aman di bawah batas 5000 karakter YT.
        auto_desc = ' '.join(x for x in [hook, script] if x)
        description = (auto_desc[:900] + ('…' if len(auto_desc) > 900 else '')) or title
        if cta_text:
            description += f"\n\n{cta_text}"
    # 🧹 v1.2.13: bersihkan deskripsi & tag (naskah/ketikan user bisa
    # mengandung emoji/simbol yang ditolak YouTube).
    _raw_desc = description
    description = _clean_upload_text(description, max_len=5000, fallback=title)
    _raw_tags = (data.get('tags') or '').strip() or '#shorts'
    tags = ', '.join(_clean_upload_tags(_raw_tags)) or '#shorts'
    title_cleaned = title_cleaned or (description != _raw_desc) or (tags != _raw_tags)

    # ID milidetik (bukan detik seperti batch_create) supaya klik ganda
    # dalam satu detik tidak membuat dua task dengan ID yang sama.
    # 🔒 #15: pakai ID monotonik — tidak pernah bentrok antar request.
    t_id = _next_task_id()
    batch_id = f"sm{t_id}"
    channel_name = channel_rec.get('name', yt_id)
    auto_mode = _autopilot_of(channel_rec) == 'auto'
    channel_paused = _channel_paused(yt_id)
    blueprint = {
        "id": t_id, "yt_id": yt_id, "title": title,
        "publish_date": v_date_str,
        "kind": SHORTS_MUSE_KIND,
        "shorts_duration_sec": duration_sec,
        "shorts_hook": hook, "shorts_script": script, "shorts_cta": cta_text,
        "shorts_bg": bg_file, "shorts_bgm": bgm_file,
        "shorts_bg_clips": bg_clips,
        "shorts_text_color": text_color,
        "shorts_theme": theme,
        # target_duration_hours dipakai estimasi disk & QC lama — isi dari
        # durasi detik supaya keduanya tetap konsisten.
        "target_duration_hours": duration_sec / 3600.0,
        "description": description, "tags": tags,
        "privacy": "private", "playlist_id": "",
        "render_preset": "ultrafast",
        "source_mode": "mix",
        "batch_id": batch_id,
        "title_cleaned": title_cleaned,
    }
    # 🔒 #15: mode Full-Auto langsung masuk antrean — tolak sebelum task
    # dibuat bila antrean penuh (task yang dibuat tapi tidak ter-queue
    # akan menggantung dengan status menyesatkan).
    if auto_mode and not channel_paused and render_queue.qsize() >= QUEUE_MAX_SIZE:
        return {"ok": False,
                "message": f"Antrean render penuh (maks {QUEUE_MAX_SIZE} task). "
                           "Tunggu antrean berkurang lalu coba lagi.", "http": 429}
    with db_lock:
        active_tasks.append({
            "id": t_id, "yt_id": yt_id, "title": title,
            "time": v_date_str,
            "status": (CHANNEL_PAUSED_STATUS if channel_paused else "Menunggu Giliran (Queue) ⏳") if auto_mode else APPROVAL_PENDING_STATUS,
            "approval": "approved" if auto_mode else "pending",
            "batch_id": batch_id,
            "kind": SHORTS_MUSE_KIND,
            "type": "📱 Shorts Muse",
            "title_cleaned": title_cleaned,
            "blueprint": blueprint,
        })
        _activity_task_created("📱 Shorts Muse", title, yt_id)  # 🔥 v1.3.71: catat ke log aktivitas
        for c in database_channel:
            if c['yt_id'] == yt_id:
                c['last_scheduled_date'] = v_date_str
                c['reminder_sent'] = False
                break
        save_channels(database_channel)
    save_tasks_db()

    if auto_mode:
        if not channel_paused:
            try:
                queue_put(blueprint)
            except Exception as e:
                # 🔒 #15 (kondisi balap): task dipindah ke riwayat dengan
                # status "Stuck" supaya bisa dipulihkan via
                # recover_failed_tasks, bukan dibiarkan "Menunggu Giliran"
                # selamanya tanpa pernah di-queue.
                print(f"Gagal queue shorts_muse {t_id}: {e}", flush=True)
                with db_lock:
                    for t in list(active_tasks):
                        if t.get('id') == t_id:
                            active_tasks.remove(t)
                            t['status'] = "Gagal ❌ (Stuck — antrean penuh saat dibuat)"
                            t['updated_at'] = time.time()
                            history_tasks.insert(0, t)
                            break
                    save_tasks_db()
                return {"ok": False,
                        "message": "Gagal masuk antrean render (antrean penuh). "
                                   "Task ada di Riwayat dan bisa dipulihkan lewat 'Pulihkan Task Gagal'.", "http": 500}
        return {"ok": True, "message": "Shorts Muse dibuat dan langsung masuk antrean (mode Full-Auto)!",
                "http": 200, "task_id": t_id, "auto_mode": True, "batch_id": batch_id,
                "preview": None, "channel_name": channel_name, "publish_date": v_date_str,
                "title": title, "title_cleaned": title_cleaned,
                "bg_auto": bg_auto, "bg_video": bg_video_count > 0, "bg_video_count": bg_video_count}
    preview = {"id": t_id, "title": title, "publish_date": v_date_str,
               "duration_label": f"{duration_sec} detik (Shorts Muse)",
               "description": description, "privacy": "private",
               "title_cleaned": title_cleaned, "tags": tags}
    return {"ok": True, "message": "Shorts Muse dibuat dan menunggu approval di Telegram!",
            "http": 200, "task_id": t_id, "auto_mode": False, "batch_id": batch_id,
            "preview": preview, "channel_name": channel_name, "publish_date": v_date_str,
            "title": title, "title_cleaned": title_cleaned,
            "bg_auto": bg_auto, "bg_video": bg_video_count > 0, "bg_video_count": bg_video_count}


# ============================================================
# 🎵 MUSIK PLAYLIST — riset trending → kompilasi → upload
# (v1.2.7; spek user 01:36 + 04:46 WIB 2026-11-19)
#
# Alur: Riset (YouTube Data API, hemat kuota) → unduh audio referensi
# (yt-dlp) + footage Pixabay (API resmi, multi-key + failover) →
# judul/deskripsi ditulis ulang + thumbnail dimodifikasi → render
# loop (utamakan stream copy) → QC + upload lewat worker yang sama
# (disk, jeda, kuota project, approval/autopilot) → cleanup.
# Audio memakai referensi apa adanya — pesan job SELALU jujur soal ini.
# ============================================================

def _pixabay_key_entries():
    """Daftar key Pixabay dari config.json — SELALU baca fresh (jangan
    pakai global bot_config yang bisa basi setelah restart/operator).
    Format: [{'key': str, 'disabled': bool}]. Migrasi otomatis dari
    field lama `pixabay_api_key` (satu key) ke daftar."""
    cfg = load_bot_config()
    raw = cfg.get('pixabay_api_keys')
    entries = []
    if isinstance(raw, list):
        for e in raw:
            if isinstance(e, dict) and isinstance(e.get('key'), str) and e['key'].strip():
                entries.append({'key': e['key'].strip(), 'disabled': bool(e.get('disabled'))})
            elif isinstance(e, str) and e.strip():
                entries.append({'key': e.strip(), 'disabled': False})
    legacy = cfg.get('pixabay_api_key')
    if isinstance(legacy, str) and legacy.strip():
        if all(e['key'] != legacy.strip() for e in entries):
            entries.append({'key': legacy.strip(), 'disabled': False})
        # Migrasi permanen: pindah ke daftar, hapus field lama.
        cfg['pixabay_api_keys'] = entries
        cfg.pop('pixabay_api_key', None)
        _save_bot_config(cfg)
    return entries


def _pixabay_save_entries(entries):
    """Simpan daftar key (fresh load → ubah → save). Key utuh TIDAK
    PERNAH ditulis ke log/pesan — hanya ke config.json (0600)."""
    cfg = load_bot_config()
    cfg['pixabay_api_keys'] = [{'key': e['key'], 'disabled': bool(e.get('disabled'))} for e in entries]
    cfg.pop('pixabay_api_key', None)
    _save_bot_config(cfg)


def _pixabay_masked_list():
    """Daftar key untuk panel/Telegram — HANYA masked + status."""
    return [{'index': i + 1, 'masked': _mask_token(e['key']),
             'disabled': bool(e.get('disabled'))}
            for i, e in enumerate(_pixabay_key_entries())]


def _pixabay_active_keys():
    """Key yang masih aktif (tidak disabled), urut seperti disimpan."""
    return [e['key'] for e in _pixabay_key_entries() if not e.get('disabled')]


def _pixabay_add_key(key):
    """Tambah key baru. Kembalikan (ok, masked, pesan). Duplikat ditolak."""
    key = (key or '').strip()
    if not key:
        return False, '', 'API key tidak boleh kosong.'
    if len(key) < 8 or len(key) > 100 or any(ch.isspace() for ch in key):
        return False, '', 'API key tidak valid (8–100 karakter, tanpa spasi).'
    entries = _pixabay_key_entries()
    if any(e['key'] == key for e in entries):
        return False, _mask_token(key), 'Key itu sudah ada di daftar.'
    entries.append({'key': key, 'disabled': False})
    _pixabay_save_entries(entries)
    return True, _mask_token(key), f"Key {_mask_token(key)} ditambahkan."


def _pixabay_delete_key(ident):
    """Hapus key by nomor urut (1-based) atau 4 karakter terakhir.
    Kembalikan (ok, masked, pesan)."""
    ident = str(ident or '').strip()
    entries = _pixabay_key_entries()
    if not entries:
        return False, '', 'Belum ada API key Pixabay.'
    target = None
    if ident.isdigit():
        idx = int(ident) - 1
        if 0 <= idx < len(entries):
            target = entries[idx]
    if target is None and len(ident) >= 4:
        matches = [e for e in entries if e['key'][-4:] == ident[-4:]]
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            return False, '', 'Ada beberapa key dengan 4 karakter terakhir yang sama — hapus pakai nomor urut.'
    if target is None:
        return False, '', 'Key tidak ditemukan (pakai 4 karakter terakhir atau nomor urut).'
    entries.remove(target)
    _pixabay_save_entries(entries)
    return True, _mask_token(target['key']), f"Key {_mask_token(target['key'])} dihapus."


def _pixabay_disable_key(key):
    """Tandai key nonaktif (401/403) supaya tidak dicoba terus."""
    entries = _pixabay_key_entries()
    changed = False
    for e in entries:
        if e['key'] == key and not e.get('disabled'):
            e['disabled'] = True
            changed = True
    if changed:
        _pixabay_save_entries(entries)


_PIXABAY_API_URL = 'https://pixabay.com/api/videos/'


def _pixabay_api_get(params, timeout=30):
    """GET Pixabay Videos API (resmi) dengan FAILOVER multi-key.
    401/403 → key dinonaktifkan (disimpan) lalu coba key berikut;
    429 → coba key berikut tanpa menonaktifkan; error lain → gagal
    langsung. Kembalikan (json, masked_key). Key TIDAK PERNAH muncul
    di pesan error/log."""
    keys = _pixabay_active_keys()
    if not keys:
        raise RuntimeError("Belum ada API key Pixabay — isi dulu di Settings (gratis, daftar di pixabay.com/api) atau /pixabaykey di Telegram.")
    last_err = ''
    tried = 0
    for key in keys:
        tried += 1
        try:
            r = requests.get(_PIXABAY_API_URL, params=dict(params, key=key), timeout=timeout)
        except requests.RequestException as e:
            last_err = f"jaringan Pixabay bermasalah ({str(e)[:60]})"
            continue
        if r.status_code == 200:
            return r.json(), _mask_token(key)
        if r.status_code in (401, 403):
            _pixabay_disable_key(key)
            last_err = "API key tidak valid/ditolak (401/403) — key dinonaktifkan"
            continue
        if r.status_code == 429:
            last_err = "rate limit Pixabay (429)"
            continue
        last_err = f"Pixabay menjawab HTTP {r.status_code}"
        break
    raise RuntimeError(f"Pixabay gagal: {last_err} (dicoba {tried} key).")


# Bank query footage Pixabay per tema — dipilih acak per job (auto-random).
_PLAYLIST_PIXABAY_QUERIES = {
    'lofi': ['lofi rain city night', 'coffee shop night', 'rainy window night'],
    'piano': ['piano concert', 'piano keys closeup', 'sheet music piano'],
    'hujan': ['rain window', 'rain storm clouds', 'rain drops leaves'],
    'tidur': ['night sky stars', 'moon night clouds', 'bedroom night lamp'],
    'sleep': ['night sky stars', 'moon night clouds', 'bedroom night lamp'],
    'meditasi': ['zen garden', 'meditation calm lake', 'sunrise mist mountain'],
    'fokus': ['workspace desk', 'library books', 'study desk lamp'],
    'anak': ['children playing park', 'colorful balloons sky', 'playground kids'],
    'asmr': ['rain drops slow', 'ocean waves calm', 'forest leaves wind'],
    'pop': ['concert lights crowd', 'city night neon', 'stage lights'],
    'jazz': ['jazz saxophone club', 'vinyl record player', 'saxophone night'],
    'alam': ['mountain lake sunset', 'forest sunlight', 'ocean waves beach'],
    'nature': ['mountain lake sunset', 'forest sunlight', 'ocean waves beach'],
}
_PLAYLIST_PIXABAY_DEFAULT = ['nature landscape', 'city night lights', 'ocean waves']


def _playlist_pixabay_queries(theme):
    """2 query footage untuk tema (acak per job)."""
    key = (theme or '').strip().lower()
    bank = None
    for k, v in _PLAYLIST_PIXABAY_QUERIES.items():
        if k in key:
            bank = v
            break
    if bank is None:
        words = [w for w in re.split(r'[^a-z0-9]+', key) if w]
        bank = [' '.join(words[:2])] if words else _PLAYLIST_PIXABAY_DEFAULT
        if len(words) >= 2:
            bank = bank + [words[0] + ' nature']
    return random.sample(bank, min(2, len(bank)))


def _pixabay_search_clips(theme, count=PLAYLIST_MAX_CLIPS, job_id=''):
    """Cari footage 16:9 di Pixabay (API resmi), utamakan varian 'medium'
    1280x720 (v1.3.17) supaya export playlist lebih kecil (terbukti
    ~49% dari 1080p, hemat 51%) dan render tetap bisa pakai STREAM
    COPY (cepat). v1.3.8: dulu selalu 'small' (~640px) -> render
    selalu 'normalize' (re-encode tiap klip ke 1080p, lambat). Bila
    klip 720p kurang dari count, dilengkapi varian lain yang tersedia
    (large/medium/small/tiny) - render kembali ke normalize (ke 720p)
    seperti dulu, bukan gagal. Render Zumo (spectrum + partikel)
    TIDAK memakai fungsi ini dan selalu re-encode - copy memang
    tidak mungkin di sana.
    Kembalikan (clips, masked_keys). Clips: [{url, width, height, tags}].
    Diacak (seed job_id) supaya tiap job beda susunan."""
    queries = _playlist_pixabay_queries(theme)
    seen = set()
    clips = []        # varian non-1080p (cadangan)
    clips_720 = []    # varian medium 1280x720 (bisa copy)
    used_masks = []
    for q in queries:
        data, masked = _pixabay_api_get({
            'q': q, 'video_type': 'film', 'orientation': 'horizontal',
            'min_width': 640, 'per_page': 50, 'safesearch': 'true', 'order': 'popular'})
        if masked not in used_masks:
            used_masks.append(masked)
        for hit in data.get('hits', []):
            _hd = hit.get('duration')
            if isinstance(_hd, (int, float)) and _hd < 3:
                continue  # 🔥 v1.3.75: klip <3 dtk bukan footage layak
            videos = hit.get('videos') or {}
            best = None
            for size in ('medium', 'large', 'small', 'tiny'):
                v = videos.get(size) or {}
                url = v.get('url') or ''
                try:
                    w, h = int(v.get('width') or 0), int(v.get('height') or 0)
                except Exception:
                    continue
                if not url.startswith('https://cdn.pixabay.com/'):
                    continue
                if not w or not h or abs(w / h - 16 / 9) > 0.02:
                    continue  # hanya 16:9
                if url in seen:
                    continue
                best = {'url': url, 'width': w, 'height': h,
                        'tags': hit.get('tags', ''), 'duration': hit.get('duration')}
                break
            if not best:
                continue
            seen.add(best['url'])
            if best['width'] == 1280 and best['height'] == 720:
                clips_720.append(best)
            else:
                clips.append(best)
            if len(clips_720) >= count:
                break
        if len(clips_720) >= count:
            break
    # Bila klip 720p cukup, pakai HANYA itu (seragam -> copy bisa jalan);
    # bila kurang, lengkapi dengan varian lain (render normalize ke 720p).
    chosen = clips_720 if len(clips_720) >= count else clips_720 + clips
    random.Random(job_id or '').shuffle(chosen)
    clips = chosen[:count]
    if len(clips) < PLAYLIST_MIN_CLIPS:
        raise RuntimeError(f"Footage Pixabay kurang (dapat {len(clips)}, butuh min {PLAYLIST_MIN_CLIPS}) — coba tema/kata kunci lain.")
    return clips, used_masks


def _pixabay_clip_has_motion(path, dur):
    """Cek klip benar-benar video BERGERAK: ambil 3 frame (10%, 50%,
    90% durasi) — bila ketiganya identik, klip itu foto/statis, bukan
    video. v1.3.75: video Aruya + Mahewa 21 Nov 2026 freeze 20-33 dtk
    karena klip statis lolos ke render (mata penonton: kayak foto)."""
    try:
        hashes = []
        for frac in (0.1, 0.5, 0.9):
            tmp = "%s.motion%d.png" % (path, int(frac * 100))
            try:
                r = subprocess.run([get_ffmpeg_path(), '-v', 'error',
                                    '-ss', "%.2f" % (dur * frac), '-i', path,
                                    '-frames:v', '1', '-y', tmp],
                                   capture_output=True, timeout=60)
                if r.returncode == 0 and os.path.exists(tmp) and os.path.getsize(tmp) > 0:
                    with open(tmp, 'rb') as f:
                        hashes.append(zlib.crc32(f.read()))
            finally:
                try:
                    os.remove(tmp)
                except Exception:
                    pass
        return len(hashes) >= 2 and len(set(hashes)) >= 2
    except Exception:
        return False


_STILL_IMAGE_CODECS = frozenset({'mjpeg', 'png', 'bmp', 'gif', 'webp', 'tiff', 'jpeg'})


def _pixabay_download_clip(url, dest):
    """Unduh 1 klip footage (hanya dari CDN Pixabay).
    🔥 v1.3.75: pastikan yang terunduh benar-benar VIDEO BERGERAK —
    codec gambar (mjpeg/png/dll), durasi <2 dtk, atau 3 frame sampel
    identik (foto/statis) langsung ditolak, bukan ikut ke render."""
    if not url.startswith('https://cdn.pixabay.com/'):
        raise ValueError("URL footage bukan dari CDN Pixabay.")
    r = requests.get(url, stream=True, timeout=60,
                     headers={'User-Agent': 'ZIMUSE-BOT/1.0'})
    r.raise_for_status()
    if not (r.headers.get('content-type') or '').startswith('video/'):
        raise ValueError("Footage bukan video.")
    total = 0
    with open(dest, 'wb') as f:
        for chunk in r.iter_content(1024 * 256):
            if not chunk:
                continue
            f.write(chunk)
            total += len(chunk)
            if total > PLAYLIST_MAX_CLIP_BYTES:
                raise RuntimeError("Footage terlalu besar (>300 MB).")
    if total == 0:
        raise RuntimeError("Footage kosong.")
    pv = _probe_video(dest)
    vs = [s for s in ((pv or {}).get('streams') or []) if s.get('codec_type') == 'video'] if pv else []
    if not vs:
        raise ValueError("Footage bukan video (tidak ada stream video).")
    if (vs[0].get('codec_name') or '').lower() in _STILL_IMAGE_CODECS:
        raise ValueError("Footage bukan video bergerak (codec gambar).")
    _dur = _playlist_media_duration(dest)
    if _dur < 2.0:
        raise ValueError("Footage terlalu pendek (<2 detik).")
    if not _pixabay_clip_has_motion(dest, _dur):
        raise ValueError("Footage tidak bergerak (gambar diam/statis).")
    return dest


def _playlist_media_duration(path):
    """Durasi detik via ffprobe (0 bila gagal)."""
    try:
        p = subprocess.run([get_ffprobe_path(), '-v', 'error',
                            '-show_entries', 'format=duration',
                            '-of', 'default=noprint_wrappers=1:nokey=1', path],
                           capture_output=True, text=True, timeout=60)
        return float((p.stdout or '').strip() or 0)
    except Exception:
        return 0.0


def _playlist_iso_duration_sec(iso):
    """Durasi ISO 8601 YouTube (PT1H5M30S) -> detik (int). Gagal parse -> 0."""
    m = re.match(r'^PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+(?:\.\d+)?)S)?$', (iso or '').strip())
    if not m:
        return 0
    h, mn, s = m.groups()
    return int(float(h or 0)) * 3600 + int(float(mn or 0)) * 60 + int(float(s or 0))


def _playlist_build_youtube_client(cred_str):
    """Bangun client YouTube Data API dari satu cred OAuth (refresh bila perlu)."""
    _creds = Credentials.from_authorized_user_info(json.loads(cred_str))
    if _creds.expired and _creds.refresh_token:
        _creds.refresh(Request())
    return build('youtube', 'v3', credentials=_creds)


def _playlist_try_creds(channel, fn):
    """Jalankan fn(youtube) di tiap kredensial channel (v1.2.25 auto-switch).

    quotaExceeded di satu project -> lanjut ke creds berikutnya; error BUKAN
    kuota langsung dikembalikan (jangan bakar kuota cadangan untuk error lain).
    Kembalikan (result, err, quota_exhausted, switched_from, used_pid, tried_pids).
    """
    creds_list = channel.get('creds_list', [channel.get('creds_json')]) or []
    if not any(creds_list):
        return None, 'Tidak ada kredensial tersimpan — taut ulang channel.', False, None, None, []
    last_err = ''
    tried_pids = []
    switched_from = None
    used_pid = None
    for _idx, _cred_str in enumerate(creds_list):
        if not _cred_str:
            continue
        try:
            youtube = _playlist_build_youtube_client(_cred_str)
        except Exception as e:
            last_err = str(e)
            continue
        _pid = _oauth_project_of_cred_str(_cred_str)
        if _pid and _pid not in tried_pids:
            tried_pids.append(_pid)
        try:
            result = fn(youtube)
            used_pid = _pid
            if _idx > 0:
                switched_from = _oauth_project_of_cred_str(creds_list[0])
            return result, None, False, switched_from, used_pid, tried_pids
        except Exception as e:
            if _oauth_is_quota_error(e):
                last_err = str(e)
                continue
            return None, str(e)[:120], False, None, None, tried_pids
    if tried_pids and _oauth_is_quota_error(last_err):
        return None, last_err, True, None, None, tried_pids
    return None, (last_err or 'tidak ada kredensial tersimpan'), False, None, None, tried_pids


def _playlist_search_one_tier(channel, search_q, days, max_results, min_views,
                              tier_no, tier_label):
    """Satu tier riset: search.list (+ videos().list) dengan publishedAfter
    sesuai tier. TANPA filter views (v1.3.5 koreksi) — views berapa pun
    boleh, termasuk 0; min_views dipertahankan di signature demi
    kompatibilitas tapi DIABAIKAN. Kembalikan (cands, err, quota_exhausted).
    cands kosong (bukan error) bila tier ini tidak menemukan apa-apa —
    pemanggil lanjut ke tier berikutnya yang lebih longgar."""
    def _do(youtube):
        # v1.3.13: filter KERAS di API seperti filter YouTube web (tangkapan
        # layar Bos 08:13 WIB: JENIS Video + DURASI "Lebih dari 20 menit" +
        # TANGGAL UPLOAD minggu/bulan ini) — videoDuration 'long' = >20 mnt.
        # Tanpa ini API mengembalikan klip 5 menit yang lalu menang views.
        kwargs = {'part': 'snippet', 'q': search_q, 'type': 'video',
                  'videoCategoryId': '10', 'order': 'viewCount',
                  'videoDuration': 'long', 'maxResults': max_results}
        if days is not None:
            kwargs['publishedAfter'] = (datetime.now(timezone.utc)
                                        - timedelta(days=days)).strftime('%Y-%m-%dT%H:%M:%SZ')
        res = youtube.search().list(**kwargs).execute()
        ids = [it['id']['videoId'] for it in res.get('items', [])
               if (it.get('id') or {}).get('videoId')]
        if not ids:
            return {'empty': True}
        vres = youtube.videos().list(
            part='statistics,snippet,contentDetails',
            id=','.join(ids)).execute()
        return {'res': res, 'vres': vres}

    out, err, quota, switched, used_pid, tried_pids = _playlist_try_creds(channel, _do)
    if err:
        if quota:
            hint = _oauth_quota_failure_hint(channel, channel.get('name', ''))
            pids = ', '.join(p for p in tried_pids if p)
            return None, (f'Riset gagal: kuota pencarian project {pids} habis '
                          f'hari ini.{hint}'), True
        return None, f'Riset YouTube gagal: {err}', False
    if not out or out.get('empty'):
        return [], None, False
    if switched:
        _oauth_notify_quota_switch(channel.get('name', channel.get('yt_id', '')),
                                   'riset', switched, used_pid)
    cands = []
    for v in (out.get('vres') or {}).get('items', []):
        try:
            views = int((v.get('statistics') or {}).get('viewCount') or 0)
        except Exception:
            views = 0
        # v1.3.5 koreksi: TIDAK ada filter views di sini lagi.
        sn = v.get('snippet') or {}
        thumbs = sn.get('thumbnails') or {}
        thumb_url = ((thumbs.get('maxres') or thumbs.get('high')
                      or thumbs.get('medium') or thumbs.get('default') or {}).get('url', ''))
        cands.append({
            'video_id': v.get('id', ''), 'title': sn.get('title', ''),
            'channel': sn.get('channelTitle', ''), 'views': views,
            'published_at': sn.get('publishedAt', ''),
            'duration': (v.get('contentDetails') or {}).get('duration', ''),
            'duration_sec': _playlist_iso_duration_sec(
                (v.get('contentDetails') or {}).get('duration', '')),
            'thumbnail_url': thumb_url,
            'description': sn.get('description', '') or '',
            'tags': list(sn.get('tags') or []),
            'tier': tier_no, 'tier_label': tier_label,
        })
    cands.sort(key=lambda c: (-c['views'], -(c.get('duration_sec') or 0)))
    return cands, None, False


def _extract_youtube_ids_from_url(url):
    """Ekstrak (video_id, playlist_id) dari link YouTube apa pun.

    Dukung youtu.be/<id>, youtube.com/watch?v=<id>, /shorts/<id>,
    /embed/<id>, /live/<id>, music.youtube.com/watch?v=<id>, dan playlist
    via list=PL.../UU.../RD.../OL... Kembalikan (None, None) bila tidak
    ada ID valid. video_id divalidasi 11 karakter [A-Za-z0-9_-]{11}."""
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


def _playlist_research_youtube(channel, query):
    """Riset video musik bertingkat (v1.3.5, YouTube Data API).

    Mencoba PLAYLIST_TIERS berurutan dalam satu panggilan: tier 1 (video terbit <=30 hari) dulu; bila 0 kandidat,
    tier 2 (<=90 hari); lalu tier 3 (<=365 hari); lalu tier 4 evergreen
    (semua umur, tanpa batas terbit). TANPA filter views — views berapa
    pun boleh, termasuk 0. Berhenti di tier pertama yang menemukan kandidat — jadi lagu
    lawas yang video barunya sepi tetap dapat lewat tier evergreen.
    Cache 1 jam hanya menyimpan hasil AKHIR (kandidat tier yang lolos),
    bukan hasil kosong tier awal. Kuota: ~101 unit per tier yang dicoba
    (search 100 + detail 1); tier berikutnya hanya bila tier sebelumnya
    kosong, jadi kasus normal tetap 1 tier. Kembalikan
    (candidates, err, from_cache, tier_no, tier_label); tier_no None bila
    tidak ada kandidat sama sekali."""
    q = (query or '').strip()
    if not q:
        return None, 'Tema/kata kunci wajib diisi.', False, None, None
    cache_key = f"{channel.get('yt_id')}|{q.lower()}"
    try:
        store = _playlist_jobs_load()
        hit = (store.get('research_cache') or {}).get(cache_key)
        if hit and time.time() - float(hit.get('at') or 0) < PLAYLIST_RESEARCH_CACHE_SEC:
            cached = hit.get('cands') or []
            if cached:
                _t = cached[0].get('tier') or 1
                _l = cached[0].get('tier_label') or PLAYLIST_TIER_LABELS[0]
                return cached, None, True, _t, _l
            return cached, None, True, None, None
    except Exception:
        pass
    search_q = q if ('music' in q.lower() or 'musik' in q.lower()) else q + ' music'
    for _i, (_days, _min_views) in enumerate(PLAYLIST_TIERS):
        _tier_no = _i + 1
        _label = PLAYLIST_TIER_LABELS[_i]
        _max = PLAYLIST_TIER_MAX_RESULTS[_i]
        cands, err, _quota = _playlist_search_one_tier(
            channel, search_q, _days, _max, _min_views, _tier_no, _label)
        if err:
            return None, err, False, None, None
        if cands:
            try:
                store = _playlist_jobs_load()
                store.setdefault('research_cache', {})[cache_key] = {'at': time.time(), 'cands': cands}
                _playlist_jobs_save(store)
            except Exception:
                pass
            return cands, None, False, _tier_no, _label
    return [], None, False, None, None


def _playlist_download_audio(video_id, out_dir):
    """Unduh audio referensi (mp3 192k) via yt-dlp. URL dibangun dari
    video_id (pola ketat 11 karakter — tidak ada host arbitrer)."""
    import yt_dlp
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id or ''):
        raise ValueError('ID video tidak valid.')
    url = f"https://www.youtube.com/watch?v={video_id}"
    os.makedirs(out_dir, exist_ok=True)
    opts = {
        'format': 'bestaudio/best',
        'outtmpl': os.path.join(out_dir, 'audio.%(ext)s'),
        'noplaylist': True, 'quiet': True, 'no_warnings': True,
        'postprocessors': [{'key': 'FFmpegExtractAudio',
                            'preferredcodec': 'mp3', 'preferredquality': '192'}],
        'ffmpeg_location': get_ffmpeg_path(),
        'socket_timeout': 30, 'retries': 3,
        'max_filesize': PLAYLIST_MAX_AUDIO_BYTES,
        'http_headers': {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36'},
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        ydl.download([url])
    audio_path = os.path.join(out_dir, 'audio.mp3')
    if not os.path.exists(audio_path) or os.path.getsize(audio_path) == 0:
        raise RuntimeError('Audio referensi gagal diunduh.')
    return audio_path


# ============================================================
# 🔥 v1.2.24: FORMULA SEO judul playlist — kata kunci di DEPAN,
# lalu tahun, jenis, suasana; target 60-70 karakter, maks 100.
# Bahasa mengikuti judul referensi (ID default; EN bila kata
# Inggris dominan — mis. Cozy Sax). Bila kata kunci tidak bisa
# diekstrak → perilaku lama (inti referensi). Tanpa nama channel
# (v1.2.23), tanpa emoji (v1.2.13).
# ============================================================
_SEO_ID_WORDS = frozenset(
    'lagu terbaru viral menyentuh hati santai tidur anak keroncong dangdut melayu lawas enak didengar paling merdu koleksi kompilasi penuh album cinta rindu galau kopi hujan pagi malam senam belajar ceria indonesia jawa sunda minang pop koplo untuk dan yang di ke dari dengan tahun bulan minggu'.split())
_SEO_EN_WORDS = frozenset(
    'the for songs song music love romantic relax relaxing sleep dinner evening morning night calm peaceful beautiful best top hits collection piano saxophone jazz cafe coffee rain smooth soft gentle deep study work focus meditation chill lofi acoustic guitar violin flute'.split())
_SEO_SITUATIONS_EN = ('dinner', 'cafe', 'sleep', 'relax', 'working', 'work', 'reading', 'evening', 'morning', 'night', 'study', 'meditation', 'wedding', 'christmas', 'rain')
_SEO_MOODS_EN = ('relaxing', 'calming', 'soothing', 'peaceful', 'beautiful', 'gentle', 'soft', 'smooth', 'cozy')
_SEO_STOPWORDS = frozenset('& dan the for di ke dari yang dengan atau and or of in on at to a an'.split())
_SEO_KIND_WORDS = frozenset(('full', 'album', 'koleksi', 'kompilasi', 'playlist', 'live'))


def _seo_words(text):
    """Kata-kata bersih dari judul (emoji/simbol dibuang) untuk ekstraksi."""
    s = _clean_upload_text(text or '')
    s = re.sub(r'[^\w ]', ' ', s, flags=re.UNICODE)
    return ' '.join(s.split())


def _seo_strip_name(text, name):
    """Hapus nama channel (jejak pemilik asli) dari teks — cocok persis,
    case-insensitive, spasi fleksibel; juga bentuk tanpa spasi
    ('AnnasyaAdreena') yang biasa dipakai sebagai handle."""
    n = (name or '').strip()
    if not n or not text:
        return text
    variants = [n, n.replace(' ', '')]
    parts = n.split()
    if len(parts) >= 2:
        variants.append(''.join(parts[:2]))
    out = text
    for v in variants:
        if not v:
            continue
        # 🔥 v1.3.33 (audit m1): batas kata — tanpa \b, nama 'Ana'
        # memotong 'Manado' jadi 'M do', 'Sax' memotong 'Saxophone'.
        pat = r'\b' + (r'\s*'.join(re.escape(w) for w in v.split()) if ' ' in v else re.escape(v)) + r'\b'
        out = re.sub(pat, ' ', out, flags=re.I)
    # Rapikan spasi per baris — baris baru DIPERTAHANKAN (pembersih
    # deskripsi bekerja per baris; meratakannya akan membuat satu baris
    # promo ikut menghapus seluruh teks).
    return '\n'.join(' '.join(ln.split()) for ln in out.splitlines())


def _seo_detect_lang(clean):
    """'en' bila kata Inggris dominan, selain itu 'id'."""
    words = [w.lower() for w in clean.split()]
    en = sum(1 for w in words if w in _SEO_EN_WORDS)
    idn = sum(1 for w in words if w in _SEO_ID_WORDS)
    return 'en' if en > idn else 'id'


def _seo_extract_year(clean):
    m = re.search(r'\b(?:19|20)\d{2}\b', clean)
    return m.group(0) if m else ''


def _seo_extract_kind(clean):
    low = clean.lower()
    if 'full album' in low:
        return '(Full Album)'
    if 'koleksi' in low:
        return '(Koleksi)'
    if 'kompilasi' in low:
        return '(Kompilasi)'
    if 'playlist' in low:
        return '(Playlist)'
    if re.search(r'\blive\b', low):
        return '(Live)'
    return ''


def _seo_main_keyword(ref_title, ref_channel=''):
    """Frasa kata kunci utama: kata-kata sampai tahun (bila ada), bila
    tidak ada tahun → 4 kata pertama. Dipakai judul, deskripsi, tag."""
    clean = _seo_words(ref_title)
    if ref_channel:
        clean = _seo_strip_name(clean, ref_channel)
    if not clean:
        return ''
    words = clean.split()
    year = _seo_extract_year(clean)
    if year:
        try:
            idx = words.index(year)
            if idx <= 8:
                return ' '.join(words[:idx + 1])
        except ValueError:
            pass
    return ' '.join(words[:4])


def _seo_duration_label(sec, lang):
    """Label durasi singkat dari detik: '1 Jam'/'69 Menit' (id),
    '1 Hour'/'60 Minutes' (en)."""
    try:
        sec = float(sec or 0)
    except Exception:
        return ''
    if sec <= 0:
        return ''
    if sec >= 3600:
        h = sec / 3600.0
        if abs(h - round(h)) < 0.05:
            n = int(round(h))
            if lang == 'id':
                return f"{n} Jam"
            return f"{n} Hour" if n == 1 else f"{n} Hours"
        m = int(round(sec / 60))
    else:
        m = int(round(sec / 60))
    if m <= 0:
        return ''
    return f"{m} Menit" if lang == 'id' else f"{m} Minutes"


# 🔥 v1.3.73: ANALISA SEO — judul playlist TIDAK lagi menyalin judul
# referensi (komplain Bos 21 Nov 2026: "kemaren kan sudah dibilangin...
# kalau harus dianalisa, ditentukan SEO-nya... bukan cuma copy gak ada
# improve" — janji 20 Nov 2026 belum ditepati v1.3.32 yang cuma menambal
# tempelan sudut). Referensi diurai jadi komponen (genre, asal, mood,
# era, jenis, tahun, sisa kata), lalu judul BARU disusun dari komponen
# itu dengan susunan kata berbeda per channel (pola slot 0-3) + sudut
# per channel. Urutan kata referensi dihancurkan — yang dipertahankan
# hanya komponen SEO-nya. Satu referensi yang sama tetap menghasilkan
# judul yang benar-benar berbeda strukturnya antar channel.
_SEO_GENRE_ID = (
    ('slow rock', 'Slow Rock'),
    ('rock', 'Rock'),
    ('reggae', 'Reggae'),
    ('dangdut', 'Dangdut'),
    ('koplo', 'Koplo'),
    ('pop', 'Pop'),
    ('jazz', 'Jazz'),
    ('saxophone', 'Saxophone'), ('sax', 'Saxophone'),
    ('piano', 'Piano'),
    ('gitar', 'Gitar'), ('guitar', 'Gitar'),
    ('keroncong', 'Keroncong'),
    ('melayu', 'Melayu'),
    ('lofi', 'Lofi'),
    ('akustik', 'Akustik'), ('acoustic', 'Akustik'),
    ('sholawat', 'Sholawat'),
    ('anak', 'Anak'),
    ('klasik', 'Klasik'),
    ('remix', 'Remix'), ('dj', 'DJ'),
)
_SEO_ORIGIN_ID = (
    ('malaysia', 'Malaysia'),
    ('indonesia', 'Indonesia'),
    ('barat', 'Barat'),
    ('korea', 'Korea'),
    ('jawa', 'Jawa'), ('sunda', 'Sunda'), ('minang', 'Minang'),
)
_SEO_MOOD_ID = (
    ('menyentuh hati', 'Menyentuh Hati'),
    ('paling menyentuh', 'Paling Menyentuh'),
    ('romantis', 'Romantis'),
    ('santai', 'Santai'),
    ('syahdu', 'Syahdu'),
    ('adem', 'Adem'),
    ('galau', 'Galau'),
    ('sedih', 'Sedih'),
    ('rindu', 'Rindu'),
    ('tenang', 'Tenang'),
    ('ceria', 'Ceria'),
    ('merdu', 'Merdu'),
    ('enak didengar', 'Enak Didengar'),
    ('paling hits', 'Paling Hits'),
)
_SEO_ERA_ID = (
    ('lawas', 'Lawas'),
    ('nostalgia', 'Nostalgia'),
    ('80an', '80an'), ('90an', '90an'), ('70an', '70an'),
    ('2000an', '2000an'),
    ('terbaru', 'Terbaru'),
    ('jadul', 'Jadul'),
    ('tempo dulu', 'Tempo Dulu'),
)
# Kata generik yang dibuang dari judul (bukan komponen SEO):
# 'lagu'/'musik' sudah tersirat, 'viral'/'trending' sudah lewat masanya
# saat video tayang, 'paling'/'terbaik' sendirian menggantung atau dobel
# dengan sudut channel, 'buat' kata kerja yang butuh objek.
_SEO_TITLE_NOISE = frozenset(
    'viral trending lagu musik paling terbaik buat best top hits official video audio hd 4k'.split())


def _seo_analyze_id(clean, year='', kind=''):
    """Urai judul referensi (sudah bersih) jadi komponen SEO: genre,
    asal, mood, era, jenis, tahun, sisa kata bermakna. Mengembalikan
    dict; komponen yang tidak ditemukan = '' (genre/asal/mood/era)
    atau [] (rest)."""
    low = ' ' + (clean or '').lower() + ' '
    used = set()

    def _match(cands):
        for src, label in cands:
            if re.search(r'(?<!\w)' + re.escape(src) + r'(?!\w)', low):
                for w in src.split():
                    used.add(w)
                return label
        return ''

    genre = _match(_SEO_GENRE_ID)
    origin = _match(_SEO_ORIGIN_ID)
    mood = _match(_SEO_MOOD_ID)
    era_parts = []
    for src, label in _SEO_ERA_ID:
        if re.search(r'(?<!\w)' + re.escape(src) + r'(?!\w)', low):
            if label.lower() not in [e.lower() for e in era_parts]:
                era_parts.append(label)
                for w in src.split():
                    used.add(w)
    era = ' '.join(era_parts)
    # 'Melayu' tanpa 'Malaysia' = asal Malaysia; bila asal sudah
    # Malaysia, 'Melayu' redundan — buang supaya tidak dobel.
    if 'melayu' in low.split():
        if not origin:
            origin = 'Malaysia'
        used.add('melayu')
    rest = []
    for w in (clean or '').split():
        lw = w.lower()
        # 🔥 v1.3.74: buang serpihan kata <3 huruf ('th' dari judul
        # referensi "...Hati th 90an" sempat lolos jadi "...2026 th"
        # di video Mahewa 07:42 WIB 21 Nov 2026).
        if len(w) < 3:
            continue
        if lw in used or lw in _SEO_STOPWORDS or lw in _SEO_TITLE_NOISE or lw in _SEO_KIND_WORDS:
            continue
        if lw == (year or '').lower():
            continue
        if lw in [r.lower() for r in rest]:
            continue
        rest.append(w)
    return {'genre': genre, 'origin': origin, 'mood': mood, 'era': era,
            'kind': kind, 'year': year, 'rest': rest}


def _seo_title_id(clean, year, kind, duration_sec, channel_key=''):
    """Judul BARU dari hasil analisa (v1.3.73): komponen disusun ulang
    dengan susunan kata berbeda per channel (pola slot 0-3) + sudut per
    channel. Bukan salinan judul referensi. Bila tidak ada komponen
    yang dikenali → '' (jatuh ke _seo_title_fallback)."""
    comp = _seo_analyze_id(clean, year, kind)
    genre, origin, mood, era = comp['genre'], comp['origin'], comp['mood'], comp['era']
    if not (genre or origin or mood or era):
        return ''
    pat = _seo_channel_pattern(channel_key)
    # Empat susunan — kata PERTAMA selalu beda antar pola, supaya judul
    # tidak lagi berbagi awal kalimat yang sama bila satu komponen
    # kosong (mis. tanpa era, pola 0 dan 2 dulu identik "Reggae Malaysia").
    if pat == 0:
        head = ' '.join(p for p in (genre, origin, era) if p)
        tail = mood or era
    elif pat == 1:
        head = ' '.join(p for p in (mood, genre, origin) if p)
        tail = era or mood
    elif pat == 2:
        head = ' '.join(p for p in (era, origin, genre) if p)
        if not era and not genre:
            # tanpa era & genre, "Malaysia" saja = sama dengan pola 0 —
            # balik: mood dulu
            head = ' '.join(p for p in (mood, origin) if p) or head
        tail = mood or era
    else:
        head = ' '.join(p for p in (genre, mood, origin, era) if p)
        if not genre and not era:
            head = ' '.join(p for p in (origin, mood) if p) or head
        tail = ''
    base = head
    if tail and tail.lower() not in base.lower():
        base = base + ' - ' + tail
    if kind and kind.strip('()').lower() not in base.lower():
        base = base + ' ' + kind
    if year and year not in base:
        base = base + ' ' + year
    for w in comp['rest'][:2]:
        if w.lower() in base.lower():
            continue
        if len(base + ' ' + w) > 72:
            break
        base = base + ' ' + w
    if len(base) < 5:
        return ''
    # v1.3.10: sudut ditempel sesuai pola per channel (depan/belakang).
    base = _seo_apply_angle(base, _seo_channel_angle(channel_key, 'id'), channel_key)
    if len(base) < 60 and duration_sec:
        dur = _seo_duration_label(duration_sec, 'id')
        if dur and len(base + ' - ' + dur) <= 100:
            base = base + ' - ' + dur
    return _seo_strip_dangling(base[:100])


def _seo_title_en(clean, year, kind, duration_sec, channel_key=''):
    """Rumus Inggris: ANGKA + Romantic/Love Songs + situasi + durasi."""
    low = clean.lower()
    num = ''
    m = re.match(r'^(\d{1,3})\b', clean)
    if m:
        num = m.group(1)
    instr = ''
    for cand, label in (('saxophone', 'Saxophone'), ('sax', 'Saxophone'),
                        ('piano', 'Piano'), ('guitar', 'Guitar'),
                        ('violin', 'Violin'), ('flute', 'Flute'),
                        ('cello', 'Cello'), ('trumpet', 'Trumpet')):
        if re.search(r'\b' + cand + r'\b', low):
            instr = label
            break
    romantic = 'romantic' in low
    mood = ''
    if not romantic:
        for cand in _SEO_MOODS_EN:
            if re.search(r'\b' + cand + r'\b', low):
                mood = cand.capitalize()
                break
    situation = ''
    m = re.search(r'\bfor\s+([A-Za-z]+)(?:\s+([A-Za-z]+))?', clean)
    if m:
        w1, w2 = m.group(1), m.group(2) or ''
        two = (w1 + ' ' + w2).lower() if w2 else ''
        if two in ('deep sleep', 'cozy evening', 'christmas eve'):
            situation = w1 + ' ' + w2
        elif w1.lower() in _SEO_SITUATIONS_EN:
            situation = w1
    if not situation:
        for s in _SEO_SITUATIONS_EN:
            if re.search(r'\b' + s + r'\b', low):
                situation = s.capitalize()
                break
    parts = []
    if num:
        parts.append(num)
    if romantic:
        parts.append('Romantic')
    elif mood:
        parts.append(mood)
    if instr:
        parts.append(instr)
    if 'love' in low or romantic:
        parts.append('Love Songs')
    elif instr:
        parts.append('Songs')
    if situation:
        parts.append('for ' + situation)
    base = ' '.join(parts)
    if len(base) < 15:
        return ''
    if kind and kind.strip('()').lower() not in base.lower():
        base = base + ' ' + kind
    # v1.3.10: sudut ditempel sesuai pola per channel (depan/belakang).
    base = _seo_apply_angle(base, _seo_channel_angle(channel_key, 'en'), channel_key)
    if duration_sec:
        dur = _seo_duration_label(duration_sec, 'en')
        if dur and len(base + ' - ' + dur) <= 100:
            base = base + ' - ' + dur
    return _seo_strip_dangling(base[:100])


def _seo_title_fallback(clean, channel_key=''):
    """Perilaku lama (pra-SEO): inti referensi sebelum pemisah, maks
    90 karakter."""
    core = clean
    for sep in (' | ', ' - ', ' • ', ' — '):
        if sep in core:
            head = core.split(sep)[0].strip()
            if len(head) >= 15:
                core = head
                break
    # v1.3.10: sudut ditempel sesuai pola per channel (depan/belakang).
    core = _seo_strip_dangling(core[:90]) or 'Musik Pilihan'
    return _seo_apply_angle(core, _seo_channel_angle(channel_key, _seo_detect_lang(core)), channel_key)


def _seo_ensure_different(new, clean):
    """Bila judul baru sama persis dengan judul referensi (bersih),
    tambah ' (Playlist)' supaya tetap berbeda dari referensi."""
    new = (new or '').strip() or 'Musik Pilihan'
    if ' '.join(new.lower().split()) == ' '.join(clean.lower().split()):
        return (new[:88] or 'Musik Pilihan') + ' (Playlist)'
    return new


# v1.3.4: sudut judul per channel (12 pilihan). v1.3.32: pemetaan sudut
# + pola TIDAK lagi dari crc32(yt_id) % 12 — hash itu menabrakkan
# Barang Unick & Mahewa ke sudut 'Spesial' yang sama, jadi judul final
# keduanya identik karakter per karakter. Sekarang dari SLOT urutan
# channel di channels_db.json (diurutkan menurut yt_id): 48 kombinasi
# (sudut, pola) yang unik sampai 48 channel. Satu referensi yang sama
# tidak lagi menghasilkan judul yang sama persis di dua channel.
_SEO_ANGLE_ID = ('Terbaik', 'Paling Enak', 'Buat Santai', 'Romantis',
                 'Paling Menyentuh', 'Full Album', 'Pilihan', 'Spesial',
                 'Paling Hits', 'Enak Didengar', 'Paling Adem',
                 'Paling Syahdu')
_SEO_ANGLE_EN = ('Best', 'Most Relaxing', 'Romantic', 'Top Picks',
                 'Beautiful', 'Full Album', 'Greatest Hits', 'Special',
                 'Soothing', 'Dreamy', 'Peaceful', 'Timeless')
# Kata yang tidak boleh menjadi kata terakhir judul: kata sifat yang
# butuh kelanjutan ('Cocok', 'Paling') — "Cocok Untuk" dibuang
# dua-duanya, bukan hanya 'Untuk'-nya.
_SEO_INCOMPLETE_TAIL = frozenset({
    'cocok', 'paling', 'sangat', 'lebih', 'kurang', 'agak', 'cukup',
    'begitu', 'terlalu', 'amat', 'most', 'very', 'more', 'so', 'too',
    'really'})


_SEO_SLOT_COUNT = 48  # 12 sudut x 4 pola — kombinasi unik sampai 48 channel


def _seo_channel_slot(channel_key):
    """Slot judul unik per channel (0-47), dari URUTAN channel di
    channels_db.json (diurutkan menurut yt_id) — bukan dari hash, supaya
    dua channel tidak pernah dapat kombinasi (sudut, pola) yang sama.
    Channel yang belum ada di daftar (atau daftar tidak terbaca) memakai
    fallback crc32 — deterministik, tapi bisa bertabrakan. Slot berubah
    bila urutan daftar channel berubah (mis. tambah channel baru yang
    urutannya di tengah); judul video yang sudah tayang tidak ikut
    berubah — judul ditulis sekali saat task dibuat."""
    key = (channel_key or '').strip()
    if not key:
        return 0
    try:
        ids = sorted(c.get('yt_id', '') for c in load_channels()
                     if isinstance(c, dict) and c.get('yt_id'))
        if key in ids:
            return ids.index(key) % _SEO_SLOT_COUNT
    except Exception:
        pass
    return zlib.crc32(key.encode('utf-8')) % _SEO_SLOT_COUNT


def _seo_channel_angle(channel_key, lang='id'):
    """Sudut judul untuk satu channel ('' = tanpa sudut). Dari slot
    urutan channel: slot % 12. Satu channel selalu dapat sudut yang
    sama selama urutan daftar channel tidak berubah."""
    key = (channel_key or '').strip()
    if not key:
        return ''
    angles = _SEO_ANGLE_EN if lang == 'en' else _SEO_ANGLE_ID
    return angles[_seo_channel_slot(key) % len(angles)]


def _seo_channel_pattern(channel_key):
    """Pola susunan judul per channel (0-3). Dari slot: (slot // 12 +
    slot) % 4 — pola bergeser tiap putaran 12 slot, jadi kombinasi
    (sudut, pola) unik untuk 48 slot pertama, bukan hanya 12."""
    key = (channel_key or '').strip()
    if not key:
        return 0
    slot = _seo_channel_slot(key)
    return (slot // len(_SEO_ANGLE_ID) + slot) % 4


def _seo_apply_angle(base, angle, channel_key):
    """Tempel sudut ke judul sesuai pola channel: 0 = akhir (lama),
    1 = depan pakai ':', 2 = akhir pakai '|', 3 = depan pakai '-'.
    Bila sudut sudah ada di judul atau tidak muat (maks 100 karakter),
    kembalikan base apa adanya. Hasil dirapikan (buang akhir
    menggantung)."""
    base = (base or '').strip()
    angle = (angle or '').strip()
    if not base:
        return ''
    # 🔥 v1.3.73: sudut yang kata intinya sudah ada di judul ikut
    # diganti — mis. 'Paling Menyentuh' di judul yang sudah berisi
    # 'Menyentuh Hati' bikin judul berulang ("... Menyentuh Hati
    # Paling Menyentuh").
    _angle_core = angle.lower().replace('paling ', '').strip()
    if not angle or angle.lower() in base.lower() or (_angle_core and _angle_core in base.lower()):
        # 🔥 v1.3.33 (audit m4): sudut tidak bisa ditempel (sudah ada
        # di judul atau kosong) — coba sudut cadangan (slot+1) supaya
        # judul tetap beda antar channel; bila itu pun tidak bisa,
        # baru kembalikan base.
        _list = _SEO_ANGLE_ID if angle in _SEO_ANGLE_ID else _SEO_ANGLE_EN
        _alt = _list[(_seo_channel_slot(channel_key) + 1) % len(_list)]
        if _alt and _alt.lower() not in base.lower():
            angle = _alt
        else:
            return _seo_strip_dangling(base[:100])
    pat = _seo_channel_pattern(channel_key)
    if pat == 1:
        cand = f"{angle}: {base}"
    elif pat == 2:
        cand = f"{base} | {angle}"
    elif pat == 3:
        cand = f"{angle} - {base}"
    else:
        cand = f"{base} {angle}"
    if len(cand) > 100:
        cand2 = f"{base} {angle}"
        cand = cand2 if len(cand2) <= 100 else base
    return _seo_strip_dangling(cand[:100])


def _seo_strip_dangling(title):
    """Buang akhir judul yang menggantung: kata gantung di ujung
    (_DANGLING_END_WORDS: 'untuk', 'dan', ...) + kata sifat yang butuh
    kelanjutan (_SEO_INCOMPLETE_TAIL: 'cocok', 'paling', ...), berulang
    sampai ujungnya kata utuh. Pemisah ' - ' yang tertinggal ikut
    dibuang. Judul yang sudah utuh tidak berubah."""
    s = (title or '').strip()
    while s:
        last = s.split()[-1].strip('.,!?:;()[]"\'').lower()
        if last in _DANGLING_END_WORDS or last in _SEO_INCOMPLETE_TAIL:
            s = s[:s.rfind(s.split()[-1])].rstrip()
            continue
        break
    s = re.sub(r'\s*[-–—|]\s*$', '', s).strip()
    return s


def _playlist_rewrite_title(ref_title, duration_sec=None, ref_channel='', channel_key=''):
    """Judul baru — FORMULA SEO v1.2.24: kata kunci di DEPAN, lalu
    tahun, jenis, suasana; target 60-70 karakter, maks 100; tanpa
    nama channel (v1.2.23), tanpa emoji (v1.2.13). Bahasa mengikuti
    judul referensi (ID default; EN bila kata Inggris dominan —
    mis. Cozy Sax). Bila kata kunci tidak bisa diekstrak → perilaku
    lama (inti referensi). Selalu berbeda dari judul referensi.
    v1.3.4: akhir menggantung dibuang ('... Cocok Untuk') + sudut per
    channel (channel_key = yt_id) supaya satu referensi tidak
    menghasilkan judul yang sama di semua channel. v1.3.32: sudut+pola
    dari slot urutan channel, bukan crc32 (crc32 menabrakkan Barang
    Unick & Mahewa ke sudut 'Spesial' yang sama)."""
    try:
        clean = _seo_words(ref_title)
        if ref_channel:
            clean = _seo_strip_name(clean, ref_channel)
        if not clean:
            return 'Musik Pilihan'
        lang = _seo_detect_lang(clean)
        year = _seo_extract_year(clean)
        kind = _seo_extract_kind(clean)
        if lang == 'en':
            new = _seo_title_en(clean, year, kind, duration_sec, channel_key)
        else:
            new = _seo_title_id(clean, year, kind, duration_sec, channel_key)
        if not new:
            new = _seo_title_fallback(clean, channel_key)
        # Jaring pengaman terakhir: pemotongan [:100] bisa meninggalkan
        # kata gantung di ujung — buang lagi setelah dipotong.
        out = _seo_strip_dangling(_seo_ensure_different(new, clean)[:100])
        return out or 'Musik Pilihan'
    except Exception:
        try:
            c2 = _seo_words(ref_title)
            out = _seo_strip_dangling(_seo_ensure_different(_seo_title_fallback(c2, channel_key), c2)[:100])
            return out or 'Musik Pilihan'
        except Exception:
            return 'Musik Pilihan'


# Jejak pemilik asli yang harus dibuang dari deskripsi referensi (v1.2.24).
_URL_RE = re.compile(r'https?://\S+|www\.\S+|youtu\.be/\S+|bit\.ly/\S+|t\.co/\S+', re.I)
_DOMAIN_RE = re.compile(r'\b(?:[a-z0-9-]+\.)+(?:com|net|org|io|co|id|me|ly|tv|gg|link|site|online|xyz|app|store|page)\b[^\s]*', re.I)
_EMAIL_RE = re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b')
_HANDLE_RE = re.compile(r'@[A-Za-z0-9_.\-]+')
_SOCIAL_PROMO_RE = re.compile(
    r'(?i)\b(instagram|ig|facebook|fb|tiktok|twitter|whatsapp|telegram|youtube|yt)\b.{0,60}\b(follow|kontak|hubungi|subscribe|dm|link|bio|channel|official)\b'
    r'|\b(follow\s+(me|us)|ikuti\s+kami|subscribe\s+channel)\b')
_SOCIAL_PLATFORMS_RE = re.compile(r'(?i)\b(instagram|ig|facebook|fb|tiktok|twitter|whatsapp|telegram|youtube|yt)\b')
_CONTACT_LINE_RE = re.compile(r'(?i)\b(kontak|bisnis|hubungi|telp|telepon|phone|call|email|wa|whatsapp)\b')
_PHONE_RE = re.compile(r'\+?\d[\d\s().-]{6,}\d')
_PRESENTER_VERB_RE = re.compile(r'^(menghadirkan|mempersembahkan|menyajikan|presents|presenting)\s+', re.I)
_DANGLING_RE = re.compile(r'(?i)\b(di|ke|dari|dan|atau|dengan|untuk|pada|on|at|and|or|to|for|in|of)\s*$')
# v1.2.24-fix: '@' telanjang yang tertinggal setelah handle dibuang
# (mis. _seo_strip_name menghapus "AnnasyaAdreena" dari "@annasyaadreena"
# sehingga hanya "@" yang tersisa).
_BARE_AT_RE = re.compile(r'@(?![A-Za-z0-9_.\-])')
# Ajakan follow/subscribe + sebutan platform, urutan bebas
# ("follow IG", "IG kami follow ya", "subscribe channel YouTube",
# "Kunjungi channel YouTube kami").
_FOLLOW_PLATFORM_RE = re.compile(
    r'(?i)\b(follow|ikuti|subscribe|langganan|subrek|kunjungi|cek|klik|tonton|lihat|buka)\b.{0,40}\b(instagram|ig|facebook|fb|tiktok|twitter|whatsapp|telegram|youtube|yt)\b'
    r'|\b(instagram|ig|facebook|fb|tiktok|twitter|whatsapp|telegram|youtube|yt)\b.{0,40}\b(follow|ikuti|subscribe|langganan|subrek|kunjungi|cek|klik|tonton|lihat|buka)\b')
# Kata yang tidak dihitung sebagai kata bermakna saat menilai sisa baris:
# kata gantung + kata promo ("link", "bio").
_FRAGMENT_STOPWORDS = frozenset({
    'di', 'ke', 'dari', 'dan', 'atau', 'dengan', 'untuk', 'pada',
    'juga', 'ini', 'itu', 'kami', 'kita', 'yang', 'adalah',
    'link', 'bio',
    'on', 'at', 'and', 'or', 'to', 'for', 'in', 'of', 'the', 'a', 'an'})
# Kata gantung di akhir baris = kalimat tidak selesai (fragmen).
_DANGLING_END_WORDS = frozenset({
    'di', 'ke', 'dari', 'dan', 'atau', 'dengan', 'untuk', 'pada',
    'on', 'at', 'and', 'or', 'to', 'for', 'in', 'of'})
# Kata di akhir baris yang menandai fragmen promo/gantung ("cek link di
# bio", "kunjungi juga"). Sengaja TIDAK termasuk "ini"/"itu"/"kami" —
# baris utuh seperti "Dengarkan lagu ini" harus tetap dipertahankan.
_FRAGMENT_END_WORDS = _DANGLING_END_WORDS | {'link', 'bio', 'juga'}


def _playlist_clean_owner_traces(text, ref_channel=''):
    """Bersihkan deskripsi referensi dari JEJAK PEMILIK ASLI (v1.2.24):
    (a) nama channel referensi, (b) semua URL, (c) handle @username &
    sebutan channel lain, (d) baris kontak/medsos. Teks normal
    dipertahankan."""
    s = text or ''
    if ref_channel:
        s = _seo_strip_name(s, ref_channel)
    s = _URL_RE.sub(' ', s)
    s = _DOMAIN_RE.sub(' ', s)
    s = _EMAIL_RE.sub(' ', s)
    s = _HANDLE_RE.sub(' ', s)
    kept = []
    for ln in s.splitlines():
        t = ' '.join(ln.split())
        if not t:
            continue
        # Bila nama pemilik di awal baris ("X Menghadirkan ..."), kata
        # kerja penyaji yang tertinggal dibuang juga supaya barisnya
        # tetap enak dibaca ("Koleksi Lagu ...").
        t = _PRESENTER_VERB_RE.sub('', t)
        if not t:
            continue
        # Baris promosi medsos/kontak pemilik asli — buang utuh.
        if _SOCIAL_PROMO_RE.search(t):
            continue
        # v1.2.24-fix: nilai ulang baris SETELAH URL/handle/email/nomor
        # dibuang — sisa fragmen ("... dan follow IG @") membuang SELURUH
        # baris, bukan menyisakan potongan kalimat.
        # (a) '@' telanjang (handle sudah dibuang, '@' tertinggal).
        if _BARE_AT_RE.search(t):
            continue
        # (b) Ajakan follow/ikuti/subscribe + sebutan platform.
        if _FOLLOW_PLATFORM_RE.search(t):
            continue
        # Baris kontak (kata kontak + nomor telepon).
        if _CONTACT_LINE_RE.search(t) and _PHONE_RE.search(t):
            continue
        # Sisa baris yang hanya nama platform (handle/URL-nya sudah dibuang).
        if _SOCIAL_PLATFORMS_RE.search(t) and len(t.split()) <= 3:
            continue
        # Baris pendek yang jadi gantung setelah URL dibuang
        # ("Dengarkan juga di dan").
        if len(t.split()) <= 5 and _DANGLING_RE.search(t):
            continue
        # (c) Sisa baris hanya fragmen: tidak ada kata bermakna, atau
        # diakhiri kata gantung (kalimat tidak selesai), atau < 3 kata
        # bermakna dan diakhiri kata gantung/promo. Baris pendek yang
        # utuh ("Selamat mendengarkan!", "Lagu Melayu",
        # "Musik untuk relaksasi") tetap dipertahankan — batas "< 3 kata
        # bermakna" saja tanpa syarat akhiran akan menghapus judul lagu
        # dan sapaan yang sah.
        _frag_words = [w.strip('.,!?:;()[]"\'') for w in t.split()]
        _meaningful = [w for w in _frag_words
                       if w and w.lower() not in _FRAGMENT_STOPWORDS]
        if not _meaningful:
            continue
        _last = _frag_words[-1].lower() if _frag_words else ''
        if _last in _DANGLING_END_WORDS:
            continue
        if len(_meaningful) < 3 and _last in _FRAGMENT_END_WORDS:
            continue
        kept.append(t)
    return '\n'.join(kept)


def _playlist_rewrite_description(ref_desc, channel_name, new_title, ref_channel=''):
    """Deskripsi baru: 2 baris awal referensi (topik) + intro channel +
    ajakan subscribe. Selalu berbeda dari referensi, < 5000 karakter.
    v1.2.24: kepala referensi dibersihkan dari jejak pemilik asli
    (nama channel referensi, URL, handle, kontak/medsos) + 2 baris
    pertama mengandung kata kunci utama (SEO)."""
    clean_head = _playlist_clean_owner_traces(ref_desc, ref_channel=ref_channel)
    lines = [l.strip() for l in clean_head.splitlines() if l.strip()]
    head = ' '.join(lines[:2])[:600]
    # Kata kunci utama harus ada di 2 baris pertama (baris 1 = judul baru
    # yang sudah SEO; bila tidak ada — mis. judul fallback — selipkan).
    kw = _seo_main_keyword(new_title)
    if kw and kw.lower() not in (new_title + ' ' + head).lower():
        head = (kw + '. ' + head) if head else kw
        head = head[:600]
    ch = (channel_name or '').strip() or 'channel ini'
    body = (f"{new_title}\n\n{head}\n\n"
            f"Selamat datang di {ch} — musik pilihan untuk menemani harimu. "
            f"Jangan lupa Subscribe dan nyalakan loncengnya supaya tidak "
            f"ketinggalan kompilasi terbaru.\n\n"
            f"#musik #playlist #relaksasi")
    return body[:4500]


def _playlist_build_tags(ref_tags, theme, keyword=''):
    """Tag baru: kata kunci utama dulu (v1.2.24, SEO) + tag referensi +
    tema/channel, total maks 400 karakter (sama seperti worker upload)."""
    tags = []
    kw = ' '.join(str(keyword or '').split())
    if kw and kw.lower() not in [str(t).lower() for t in (ref_tags or [])]:
        tags.append(kw)
    for t in (ref_tags or []):
        t = str(t).strip().replace('#', '')
        if t and t not in tags:
            tags.append(t)
    for t in [theme, 'musik', 'playlist', 'music']:
        t = (t or '').strip()
        if t and t not in tags:
            tags.append(t)
    out, total = [], 0
    for t in tags:
        if total + len(t) <= 400:
            out.append(t)
            total += len(t) + 1
    return ', '.join(out) or 'musik, playlist'


def _playlist_thumb_font(size):
    """Font tebal untuk overlay thumbnail (fallback font default)."""
    from PIL import ImageFont
    for p in ('/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf',
              '/usr/share/fonts/TTF/DejaVuSans-Bold.ttf'):
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    return ImageFont.load_default()


def _playlist_thumb_serif(size, bold=True):
    """Font serif (DejaVu Serif) untuk teks thumbnail gaya YouTube
    relaxing-jazz: judul elegan, bukan sans tebal. Fallback ke font
    sans bila serif tidak ada."""
    from PIL import ImageFont
    name = 'DejaVuSerif-Bold.ttf' if bold else 'DejaVuSerif.ttf'
    for p in (f'/usr/share/fonts/truetype/dejavu/{name}',
              f'/usr/share/fonts/TTF/{name}'):
        try:
            if os.path.exists(p):
                return ImageFont.truetype(p, size)
        except Exception:
            pass
    return _playlist_thumb_font(size)


def _zumo_thumb_short_text(title):
    """Teks pendek thumbnail dari judul video — gaya thumbnail YouTube
    niche relaxing jazz / coffee (lihat referensi user 21 Nov 2026):
    MAKSIMAL 3 kata, yang diambil 3 kata TERAKHIR dari segmen pertama
    judul (kata kuncinya ada di akhir: '... Morning Coffee Jazz').
    Kembalikan [baris1, baris2]: baris1 = semua kata kecuali terakhir,
    baris2 = kata terakhir. Contoh: 'Morning Coffee Jazz' ->
    ['Morning Coffee', 'Jazz']."""
    t = _clean_upload_text(title or '', max_len=100) or ''
    t = re.sub(r'\s*\([^)]*\)\s*$', '', t).strip()
    first = re.split(r'\s+[—–-]\s+', t)[0].strip()
    words = first.split()
    if len(words) > 3:
        words = words[-3:]
    if not words:
        words = ['Relaxing', 'Jazz']
    if len(words) == 1:
        return [words[0]]
    return [' '.join(words[:-1]), words[-1]]


def _playlist_wrap_text(text, font, max_w, draw):
    """Bungkus teks ke beberapa baris sesuai lebar maksimum."""
    words, lines, cur = (text or '').split(), [], ''
    for w in words:
        trial = (cur + ' ' + w).strip()
        if draw.textlength(trial, font=font) <= max_w or not cur:
            cur = trial
        else:
            lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines or ['']


def _playlist_thumb_fetch(ref_thumb_url):
    """Unduh thumbnail referensi (hanya *.ytimg.com). Coba URL asli dulu,
    lalu varian ukuran dari video id yang sama (maxres → hq → mq → sd) —
    API YouTube kadang mengembalikan maxres yang ternyata 404 (kasus video
    Malaysia lFp_RY_jyqQ: maxres 404, hq 200). Kembalikan (PIL Image RGB,
    bytes asli) — bytes dipakai _playlist_make_thumbnail apa adanya (v1.3.3)."""
    urls = []
    m = re.search(r'https://i\d*\.ytimg\.com/vi/([A-Za-z0-9_-]{11})/',
                  ref_thumb_url or '')
    if m:
        for name in ('maxresdefault', 'hqdefault', 'mqdefault', 'sddefault'):
            urls.append(f'https://i.ytimg.com/vi/{m.group(1)}/{name}.jpg')
    if ref_thumb_url and ref_thumb_url not in urls:
        urls.insert(0, ref_thumb_url)
    last_err = 'URL tidak valid'
    for u in urls:
        if not re.match(r'^https://i\d*\.ytimg\.com/', u):
            continue
        try:
            r = requests.get(u, timeout=30, headers={'User-Agent': 'Mozilla/5.0'})
            if r.status_code != 200:
                last_err = f'HTTP {r.status_code}'
                continue
            if not (r.headers.get('content-type') or '').startswith('image/'):
                last_err = 'bukan gambar'
                continue
            import io as _io
            from PIL import Image
            return Image.open(_io.BytesIO(r.content)).convert('RGB'), r.content
        except Exception as e:
            last_err = str(e)[:80]
            continue
    raise ValueError(f'Thumbnail referensi tidak bisa diunduh ({last_err}).')


def _playlist_thumb_palette(img):
    """Warna dominan referensi: (c1, c2, aksen). c1/c2 digelapkan untuk
    gradien latar (teks putih tetap terbaca); aksen = warna paling jenuh
    & cerah (kuning bila tidak ada yang cocok)."""
    import colorsys
    small = img.convert('RGB').resize((32, 18))
    counts = {}
    for r, g, b in small.getdata():
        key = (r // 24 * 24, g // 24 * 24, b // 24 * 24)
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: -kv[1])

    def _dark(c, f):
        return tuple(max(0, min(255, int(v * f))) for v in c)

    def _lum(c):
        return (0.299 * c[0] + 0.587 * c[1] + 0.114 * c[2]) / 255.0
    c1 = ranked[0][0] if ranked else (30, 30, 40)
    c2 = ranked[1][0] if len(ranked) > 1 else c1
    while _lum(c1) > 0.35:
        c1 = _dark(c1, 0.8)
    while _lum(c2) > 0.25:
        c2 = _dark(c2, 0.8)
    if sum(abs(a - b) for a, b in zip(c1, c2)) < 40:
        c2 = _dark(c2, 0.6)
    accent, best = (255, 212, 0), 0.0
    for c, _n in ranked:
        _h, s, v = colorsys.rgb_to_hsv(c[0] / 255, c[1] / 255, c[2] / 255)
        # Aksen harus CERAH (v > 0.6) supaya teks hitam di badge dan teks
        # aksen di atas latar gelap tetap terbaca — merah gelap (v ~0.47)
        # tidak boleh terpilih.
        if s * v > best and v > 0.6 and s > 0.4:
            best, accent = s * v, c
    return c1, c2, accent


def _playlist_thumb_gradient(w, h, c1, c2):
    """Gradien vertikal c1 (atas) → c2 (bawah), 1280x720."""
    from PIL import Image
    col = Image.new('RGB', (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        col.putpixel((0, y), tuple(int(c1[i] + (c2[i] - c1[i]) * t)
                                   for i in range(3)))
    return col.resize((w, h))


def _playlist_make_thumbnail(ref_thumb_url, new_title, channel_name, out_path,
                             badge=None):
    """Thumbnail playlist — v1.3.3: foto referensi dipakai APA ADANYA
    (tanpa crop, tanpa resize, tanpa overlay teks/badge, tanpa gradien),
    supaya teks asli thumbnail tidak hilang (kasus nostalgia 2000an:
    layout split v1.2.22 memotong teks jadi "LAGU N..." / "POP ER...").
    Simpan bytes asli; hanya bila >2MB, re-encode JPEG turunkan quality
    bertahap (88→50) sampai <=2MB — tetap tanpa crop/resize. Bila unduhan
    referensi gagal total → thumbnail gradien lokal + judul (tanpa foto),
    seperti sebelumnya. Hanya unduh dari *.ytimg.com. Gagal total (PIL
    rusak) → raise, JANGAN pakai thumbnail asli."""
    if not ref_thumb_url or not re.match(r'^https://i\d*\.ytimg\.com/', ref_thumb_url):
        raise ValueError('URL thumbnail referensi tidak valid.')
    photo = None
    photo_bytes = None
    try:
        photo, photo_bytes = _playlist_thumb_fetch(ref_thumb_url)
    except Exception as e:
        print(f"[playlist] thumbnail referensi tidak bisa diunduh ({e}) "
              f"— pakai gradien lokal.", flush=True)
        photo = None
        photo_bytes = None
    if photo is not None:
        # 🔥 v1.3.3: foto apa adanya — teks asli thumbnail utuh.
        if photo_bytes is not None and len(photo_bytes) <= 2_000_000:
            with open(out_path, 'wb') as f:
                f.write(photo_bytes)
            return out_path
        q = 88
        while True:
            photo.save(out_path, 'JPEG', quality=q, optimize=True)
            if os.path.getsize(out_path) <= 2_000_000 or q <= 50:
                break
            q -= 8
        return out_path
    # --- Fallback: gradien lokal + judul (tanpa foto), seperti sebelumnya.
    title = _clean_upload_text(new_title, max_len=100) or 'Musik Playlist'
    ch = _clean_upload_text(channel_name or '') or 'ZIMUSE'
    badge = (badge or ('FULL ALBUM' if 'full album' in title.lower()
                       else 'PLAYLIST')).strip()[:20].upper()
    c1, c2, accent = (24, 24, 34), (8, 8, 14), (255, 212, 0)
    from PIL import Image, ImageDraw
    img = _playlist_thumb_gradient(1280, 720, c1, c2)
    draw = ImageDraw.Draw(img, 'RGBA')
    max_w = 1280 - 56 - 40
    # Badge (pil) kiri atas.
    font_badge = _playlist_thumb_font(30)
    tw = draw.textlength(badge, font=font_badge)
    bx, by = 56, 52
    draw.rounded_rectangle([bx, by, bx + tw + 40, by + 52], radius=26,
                           fill=accent + (255,))
    draw.text((bx + 20, by + 9), badge, font=font_badge, fill=(0, 0, 0))
    # Judul: coba font besar dulu, turunkan bila butuh > 5 baris.
    # Judul TIDAK BOLEH terpotong diam-diam (kasus Malaysia: 108 karakter
    # butuh 5 baris) — bila bahkan font terkecil masih > 5 baris, baris
    # terakhir dipotong + '…' supaya kelihatan terpotong.
    font = _playlist_thumb_font(64)
    lines = _playlist_wrap_text(title, font, max_w, draw)
    for size in (64, 56, 48, 40, 36):
        font = _playlist_thumb_font(size)
        lines = _playlist_wrap_text(title, font, max_w, draw)
        if len(lines) <= 5:
            break
    if len(lines) > 5:
        lines = lines[:5]
        # Potong baris terakhir + '…' supaya muat (wrap sudah sesuai lebar).
        while draw.textlength(lines[4] + '…', font=font) > max_w and len(lines[4]) > 3:
            lines[4] = lines[4][:-1]
        lines[4] = lines[4].rstrip() + '…'
    line_h = int(font.size * 1.18)
    block_h = line_h * len(lines)
    y = 170 + max(0, (430 - block_h) // 2)
    for ln in lines:
        draw.text((58, y + 3), ln, font=font, fill=(0, 0, 0, 200))
        draw.text((56, y), ln, font=font, fill=(255, 255, 255))
        y += line_h
    # Nama channel kiri bawah (warna aksen).
    font_small = _playlist_thumb_font(30)
    draw.text((56, 648), ch[:60], font=font_small, fill=accent)
    q = 88
    while True:
        img.save(out_path, 'JPEG', quality=q, optimize=True)
        if os.path.getsize(out_path) <= 2_000_000 or q <= 50:
            break
        q -= 8
    return out_path


# --- Job store (playlist_jobs.json, gitignored) ---
def _playlist_jobs_load():
    try:
        with open(PLAYLIST_JOBS_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get('jobs'), list):
            d.setdefault('research_cache', {})
            return d
    except Exception:
        pass
    return {'jobs': [], 'research_cache': {}}


def _playlist_jobs_save(data):
    try:
        tmp = PLAYLIST_JOBS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, PLAYLIST_JOBS_FILE)
    except Exception:
        pass


def _playlist_job_add(job):
    with db_lock:
        data = _playlist_jobs_load()
        data['jobs'].insert(0, job)
        # 🔥 v1.3.33 (audit M3): 50 terlalu kecil — batch 7 channel
        # x riset+create+retry menghabiskan slot dalam sehari sehingga
        # job 'done' pagi terbuang sebelum 24 jam dan dedupe 24 jam
        # tidak bisa bekerja. 500 ≈ 2 minggu. (Sama untuk Zumo.)
        data['jobs'] = data['jobs'][:500]
        _playlist_jobs_save(data)
    return job


def _playlist_job_get(job_id):
    for j in _playlist_jobs_load().get('jobs', []):
        if j.get('job_id') == job_id:
            return j
    return None


def _playlist_job_update(job_id, **fields):
    with db_lock:
        data = _playlist_jobs_load()
        for j in data.get('jobs', []):
            if j.get('job_id') == job_id:
                j.update(fields)
                j['updated_at'] = _wib_now().strftime('%Y-%m-%d %H:%M')
                _playlist_jobs_save(data)
                return j
    return None


def _playlist_fps(stream):
    """Frame rate (float) dari stream ffprobe."""
    try:
        num, den = (stream.get('avg_frame_rate') or '30/1').split('/')
        return float(num) / float(den or 1)
    except Exception:
        return 30.0


def _playlist_copy_pts_ok(path, max_gap=2.0):
    """Cek celah timestamp (PTS) antar paket video berurutan.
    Celah > max_gap detik = gambar akan freeze selama itu di player
    (penyebab video Aruya/Mahewa 21 Nov 2026 freeze 20-33 dtk).
    Probe gagal -> lolos (fail-open: jangan gagalkan render bagus
    hanya karena checker error)."""
    try:
        out = subprocess.run(
            [get_ffprobe_path(), '-v', 'error', '-select_streams', 'v:0',
             '-show_entries', 'packet=pts_time', '-of', 'csv=p=0', path],
            capture_output=True, text=True, timeout=600)
        if out.returncode != 0:
            return True
        prev = None
        for line in (out.stdout or '').splitlines():
            try:
                t = float(line.strip())
            except Exception:
                continue
            if prev is not None and t - prev > max_gap:
                return False
            prev = t
        return True
    except Exception:
        return True


def _playlist_copy_valid(path, audio_dur, tol=5.0):
    """Validasi hasil concat copy: file ada, ada stream video, durasi
    mendekati durasi audio (+-tol detik). Copy yang 'sukses' (exit 0)
    tapi durasinya ngaco — mis. timestamp berantakan karena klip beda
    fps/timebase — dianggap gagal, supaya _playlist_render jatuh ke
    normalisasi, bukan meng-upload video rusak.
    🔥 v1.3.75: + cek celah PTS (freeze) via _playlist_copy_pts_ok."""
    try:
        if not path or not os.path.exists(path) or os.path.getsize(path) == 0:
            return False
        pv = _probe_video(path)
        if not pv:
            return False
        vs = [s for s in (pv.get('streams') or []) if s.get('codec_type') == 'video']
        if not vs:
            return False
        dur = float((pv.get('format') or {}).get('duration') or 0)
        return dur > 0 and abs(dur - audio_dur) <= tol and _playlist_copy_pts_ok(path)
    except Exception:
        return False


def _playlist_render(task_id, task, yt_id):
    """Render kompilasi Musik Playlist: loop klip 16:9 + audio referensi.
    v1.3.9: SELALU coba STREAM COPY dulu (-c copy, ringan & cepat),
    apa pun klipnya — beda fps/timebase umumnya tetap bisa di-copy
    (concat demuxer merangkai PTS); bila copy gagal ATAU hasilnya
    tidak valid (durasi ngaco), baru normalisasi seminimal mungkin
    (re-encode video ke 720p30, audio tetap copy) lalu concat ulang.
    Metode dicatat di task['playlist_render_method']. Durasi final =
    durasi audio.
    🔥 v1.2.21: (a) bila video final dari render sebelumnya masih ada
    dan valid → dipakai langsung TANPA render ulang (retry setelah
    upload gagal tidak boleh gagal hanya karena klip sudah dibersihkan);
    (b) klip + file normalisasi DIPERTAHANKAN sampai upload BERHASIL
    (dihapus _playlist_cleanup_after_upload) — dulu dihapus di sini
    setelah render sukses, sehingga retry render ulang tanpa footage
    (insiden Cozy Sax 2026-11-19). Gagal render → file DISIMPAN
    untuk debug (lokasi ada di job)."""
    if stop_flags.get(task_id):
        raise Exception('Dibatalkan')
    final_video = os.path.join(BASE_DIR, f"static/final_{task_id}.mp4")
    if os.path.exists(final_video) and os.path.getsize(final_video) > 0:
        # Render sebelumnya sudah sukses (klip lama mungkin sudah
        # dibersihkan) tapi upload gagal — pakai hasil render itu.
        try:
            _pv = _probe_video(final_video)
            _pdur = float((_pv.get('format') or {}).get('duration') or 0) if _pv else 0
        except Exception:
            _pdur = 0
        if _pdur > 0:
            task['playlist_render_method'] = task.get('playlist_render_method') or 'reuse-final'
            try:
                _playlist_job_update(task.get('playlist_job_id'), render_method='reuse-final')
            except Exception:
                pass
            return final_video
    audio = (task.get('playlist_audio') or '').strip()
    clips = [c for c in (task.get('playlist_clips') or []) if isinstance(c, str)]
    job_dir = (task.get('playlist_job_dir') or '').strip()
    if not audio or not os.path.exists(audio):
        raise Exception('File audio playlist tidak ditemukan.')
    clips = [c for c in clips if os.path.exists(c)]
    if not clips:
        raise Exception('Klip footage playlist tidak ditemukan (video hasil render sebelumnya juga tidak ada).')
    audio_dur = _playlist_media_duration(audio)
    if audio_dur <= 0:
        raise Exception('Durasi audio tidak terbaca.')
    infos = []
    for c in clips:
        p = _probe_video(c)
        if not p:
            continue
        vs = [s for s in (p.get('streams') or []) if s.get('codec_type') == 'video']
        if not vs:
            continue
        try:
            dur = float((p.get('format') or {}).get('duration') or 0)
        except Exception:
            dur = 0.0
        if dur <= 0:
            continue
        infos.append({'path': c, 'codec': vs[0].get('codec_name'),
                      'w': int(vs[0].get('width') or 0), 'h': int(vs[0].get('height') or 0),
                      'fps': round(_playlist_fps(vs[0]), 2), 'dur': dur})
    if not infos:
        raise Exception('Tidak ada klip footage yang bisa dibaca.')
    uniform = len({(i['codec'], i['w'], i['h'], i['fps']) for i in infos}) == 1
    is_720p = infos[0]['w'] == 1280 and infos[0]['h'] == 720
    ff = get_ffmpeg_path()
    concat_txt = os.path.join(job_dir or BASE_UPLOAD, f"pl_concat_{task_id}.txt")
    os.makedirs(os.path.dirname(concat_txt) or BASE_UPLOAD, exist_ok=True)
    total_dur = sum(i['dur'] for i in infos)
    repeats = max(1, int(-(-audio_dur // total_dur)))  # ceil
    method = 'copy'
    use_paths = [i['path'] for i in infos]
    norm_files = []
    try:
        # v1.3.9: PAKSA copy — coba gabung apa adanya dulu, tanpa
        # normalisasi di depan. (uniform/is_720p hanya dipakai untuk
        # label metode di job, bukan untuk memutuskan jalur.)
        # Daftar concat: klip diulang sampai menutup durasi audio.
        with open(concat_txt, 'w', encoding='utf-8') as f:
            for _r in range(repeats):
                for p in use_paths:
                    f.write(_concat_list_line(p))
        cmd = [ff, '-y', '-f', 'concat', '-safe', '0', '-i', concat_txt,
               '-i', audio, '-map', '0:v', '-map', '1:a',
               '-c', 'copy', '-t', f"{audio_dur:.3f}",
               '-movflags', '+faststart', final_video]
        # 🔥 v1.2.18: concat via _run_ffmpeg_zoom — progres DIUKUR dari time=
        # FFmpeg vs durasi audio. Percobaan copy memakai 0-100%; bila gagal
        # dan jatuh ke normalisasi, progres normalisasi 0-70% lalu concat
        # 70-100% (angka bisa mundur — itu artinya percobaan copy gagal).
        # "Dibatalkan"/timeout diteruskan apa adanya (bukan gagal copy).
        # 🔥 v1.3.75: klip TIDAK seragam (beda codec/resolusi/fps) JANGAN
        # coba copy — concat copy klip beda fps/timebase terbukti bikin
        # gambar freeze belasan–puluhan detik (video Aruya + Mahewa
        # 21 Nov 2026). Langsung normalisasi ke 720p30.
        copy_ok = False
        if uniform:
            try:
                _run_ffmpeg_zoom(task_id, cmd, "FFmpeg Playlist Concat Error",
                                 total_sec=audio_dur, pct_base=0.0, pct_span=100.0)
                copy_ok = _playlist_copy_valid(final_video, audio_dur)
            except Exception as _ce:
                if 'Dibatalkan' in str(_ce) or 'timeout' in str(_ce).lower():
                    raise
                copy_ok = False
        if not copy_ok:
            # Copy gagal / tidak dicoba (klip tidak seragam) / hasilnya
            # tidak valid (durasi ngaco / ada celah PTS) → normalisasi
            # semua klip ke 720p30, lalu concat ulang.
            method = 'normalize (fallback)' if uniform else 'normalize (non-uniform)'
            norm_paths = []
            for idx, info in enumerate(infos):
                np_out = os.path.join(os.path.dirname(concat_txt), f"pl_norm_{task_id}_{idx}.mp4")
                _run_ffmpeg_zoom(task_id,
                                 [ff, '-y', '-i', info['path'], '-vf',
                                  'scale=1280:720:force_original_aspect_ratio=decrease,pad=1280:720:(ow-iw)/2:(oh-ih)/2,fps=30',
                                  '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                                  '-an', np_out],
                                 "FFmpeg Playlist Normalize Error",
                                 total_sec=info['dur'],
                                 pct_base=70.0 * idx / max(1, len(infos)),
                                 pct_span=70.0 / max(1, len(infos)))
                norm_paths.append(np_out)
                norm_files.append(np_out)
            with open(concat_txt, 'w', encoding='utf-8') as f:
                for _r in range(repeats):
                    for p in norm_paths:
                        f.write(_concat_list_line(p))
            _run_ffmpeg_zoom(task_id, cmd, "FFmpeg Playlist Concat Error",
                             total_sec=audio_dur, pct_base=70.0, pct_span=30.0)
        elif not (uniform and is_720p):
            method = 'copy (forced)' 
        if not os.path.exists(final_video) or os.path.getsize(final_video) == 0:
            raise Exception('Render playlist gagal (file final kosong).')
        task['playlist_render_method'] = method
        try:
            _playlist_job_update(task.get('playlist_job_id'), render_method=method)
        except Exception:
            pass
    finally:
        # 🔥 v1.2.21: klip + file normalisasi DIPERTAHANKAN sampai upload
        # berhasil (dihapus _playlist_cleanup_after_upload). Dulu dihapus
        # di sini setelah render sukses → retry setelah upload gagal
        # tidak punya footage lagi (insiden Cozy Sax 2026-11-19).
        # Hanya concat_txt (dibuat ulang tiap render) yang dibersihkan.
        try:
            os.remove(concat_txt)
        except Exception:
            pass
    return final_video


def _playlist_cleanup_after_upload(task):
    """Setelah upload sukses: hapus video final + seluruh folder kerja
    job (audio + thumbnail). Dipanggil dari worker (sukses upload).
    Gagal hapus tidak boleh menggagalkan status Tayang."""
    try:
        task_id = task.get('id')
        if task_id:
            try:
                os.remove(os.path.join(BASE_DIR, f"static/final_{task_id}.mp4"))
            except Exception:
                pass
        job_dir = (task.get('playlist_job_dir') or '').strip()
        base = os.path.abspath(PLAYLIST_TMP_DIR)
        if job_dir and os.path.abspath(job_dir).startswith(base + os.sep):
            shutil.rmtree(job_dir, ignore_errors=True)
        _playlist_job_update(task.get('playlist_job_id'), status='done',
                             video_id=task.get('video_id'),
                             video_url=task.get('video_url'),
                             cleaned_at=_wib_now().strftime('%Y-%m-%d %H:%M'))
    except Exception:
        pass


def _playlist_create_task(data):
    """Inti pembuatan SATU task Musik Playlist — dipakai route panel dan
    build thread. Alurnya SAMA seperti Shorts Muse: stagger jam,
    autopilot auto vs approval, dijeda → ditahan, lalu worker yang sama
    (disk → render playlist → QC → upload → kuota). Mengembalikan dict
    seperti _create_shorts_muse_task."""
    yt_id = (data.get('yt_id') or '').strip()
    title = (data.get('title') or '').strip()
    if not yt_id or not title:
        return {"ok": False, "message": "Channel dan judul wajib diisi.", "http": 400}
    channel_rec = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if channel_rec is None:
        return {"ok": False, "message": "Channel tidak ditemukan.", "http": 404}
    title = _shorts_dedup_title(yt_id, title, channel_rec)
    # 🧹 v1.2.13: bersihkan judul/deskripsi/tag (judul referensi YouTube
    # sering mengandung emoji) — lalu dedup ulang, karena pembersihan
    # bisa saja menyamakan judul dengan yang sudah dipakai.
    _raw_title = title
    _date_s = _wib_now().strftime('%Y-%m-%d')
    title = _clean_upload_text(title, max_len=100,
                               fallback=f"Playlist {_date_s}",
                               strip_channel=channel_rec.get('name', ''))
    title_cleaned = bool(data.get('title_cleaned')) or (title != _raw_title)
    if title != _raw_title:
        title = _shorts_dedup_title(yt_id, title, channel_rec)
    _raw_desc = (data.get('description') or '').strip() or title
    _description = _clean_upload_text(_raw_desc, max_len=5000, fallback=title)
    _raw_tags = (data.get('tags') or '').strip()
    _tags = ', '.join(_clean_upload_tags(_raw_tags)) or 'musik, playlist'
    title_cleaned = title_cleaned or (_description != _raw_desc) or (_tags != _raw_tags)
    try:
        pub_dt = datetime.strptime(data.get('publish_date', ''), '%Y-%m-%dT%H:%M')
    except Exception:
        try:
            pub_dt = datetime.strptime(data.get('publish_date', ''), '%Y-%m-%d %H:%M')
        except Exception:
            return {"ok": False, "message": "Format tanggal salah (pakai YYYY-MM-DD HH:MM).", "http": 400}
    v_date_str = _stagger_publish_date(pub_dt.strftime('%Y-%m-%d %H:%M'), yt_id)
    try:
        audio_dur = float(data.get('audio_duration') or 0)
    except Exception:
        audio_dur = 0.0
    if audio_dur <= 0:
        return {"ok": False, "message": "Durasi audio tidak valid.", "http": 400}
    if not (data.get('audio_path') or '').strip() or not os.path.exists(data.get('audio_path') or ''):
        return {"ok": False, "message": "File audio playlist tidak ditemukan.", "http": 400}
    if not data.get('clip_paths'):
        return {"ok": False, "message": "Klip footage playlist tidak ada.", "http": 400}
    t_id = _next_task_id()
    batch_id = f"pl{t_id}"
    channel_name = channel_rec.get('name', yt_id)
    auto_mode = _autopilot_of(channel_rec) == 'auto'
    channel_paused = _channel_paused(yt_id)
    blueprint = {
        "id": t_id, "yt_id": yt_id, "title": title,
        "publish_date": v_date_str,
        "kind": PLAYLIST_KIND,
        "target_duration_hours": audio_dur / 3600.0,
        "description": _description,
        "tags": _tags,
        "privacy": (data.get('privacy') or 'public').strip() or 'public',
        "playlist_id": (data.get('playlist_id') or '').strip(),
        "playlist_audio": data.get('audio_path'),
        "playlist_clips": list(data.get('clip_paths') or []),
        "playlist_thumb": (data.get('thumb_path') or '').strip(),
        "playlist_job_id": (data.get('job_id') or '').strip(),
        "playlist_job_dir": (data.get('job_dir') or '').strip(),
        "playlist_ref_video_id": (data.get('ref_video_id') or '').strip(),
        "playlist_ref_title": (data.get('ref_title') or '').strip(),
        "render_preset": "ultrafast",
        "source_mode": "mix",
        "batch_id": batch_id,
        "title_cleaned": title_cleaned,
    }
    if auto_mode and not channel_paused and render_queue.qsize() >= QUEUE_MAX_SIZE:
        return {"ok": False,
                "message": f"Antrean render penuh (maks {QUEUE_MAX_SIZE} task). "
                           "Tunggu antrean berkurang lalu coba lagi.", "http": 429}
    with db_lock:
        active_tasks.append({
            "id": t_id, "yt_id": yt_id, "title": title,
            "time": v_date_str,
            "status": (CHANNEL_PAUSED_STATUS if channel_paused else "Menunggu Giliran (Queue) ⏳") if auto_mode else APPROVAL_PENDING_STATUS,
            "approval": "approved" if auto_mode else "pending",
            "batch_id": batch_id,
            "kind": PLAYLIST_KIND,
            "type": "🎵 Musik Playlist",
            "title_cleaned": title_cleaned,
            "blueprint": blueprint,
        })
        _activity_task_created("🎵 Musik Playlist", title, yt_id)  # 🔥 v1.3.71: catat ke log aktivitas
        for c in database_channel:
            if c['yt_id'] == yt_id:
                c['last_scheduled_date'] = v_date_str
                c['reminder_sent'] = False
                break
        save_channels(database_channel)
    save_tasks_db()
    if auto_mode:
        if not channel_paused:
            try:
                queue_put(blueprint)
            except Exception as e:
                print(f"Gagal queue playlist {t_id}: {e}", flush=True)
                with db_lock:
                    for t in list(active_tasks):
                        if t.get('id') == t_id:
                            active_tasks.remove(t)
                            t['status'] = "Gagal ❌ (Stuck — antrean penuh saat dibuat)"
                            t['updated_at'] = time.time()
                            history_tasks.insert(0, t)
                            break
                    save_tasks_db()
                return {"ok": False,
                        "message": "Gagal masuk antrean render (antrean penuh). "
                                   "Task ada di Riwayat dan bisa dipulihkan lewat 'Pulihkan Task Gagal'.", "http": 500}
        return {"ok": True, "message": "Musik Playlist dibuat dan langsung masuk antrean (mode Full-Auto)!",
                "http": 200, "task_id": t_id, "auto_mode": True, "batch_id": batch_id,
                "preview": None, "channel_name": channel_name, "publish_date": v_date_str,
                "title": title, "title_cleaned": title_cleaned}
    preview = {"id": t_id, "title": title, "publish_date": v_date_str,
               "duration_label": f"{int(audio_dur // 60)} menit (Musik Playlist)",
               "description": blueprint["description"], "privacy": blueprint["privacy"],
               "title_cleaned": title_cleaned, "tags": _tags,
               "thumb_path": (data.get('thumb_path') or '').strip()}
    return {"ok": True, "message": "Musik Playlist dibuat dan menunggu approval di Telegram!",
            "http": 200, "task_id": t_id, "auto_mode": False, "batch_id": batch_id,
            "preview": preview, "channel_name": channel_name, "publish_date": v_date_str,
            "title": title, "title_cleaned": title_cleaned}


def _playlist_build_thread(job_id):
    """Latar: unduh audio → Pixabay → metadata baru → thumbnail →
    buat task. Berjalan di thread daemon supaya request HTTP tidak
    timeout. Gagal di langkah mana pun → job 'failed' + file
    dipertahankan untuk debug (lokasi ada di job)."""
    job = _playlist_job_get(job_id)
    if not job:
        return
    yt_id = job.get('yt_id')
    video_id = job.get('video_id')
    job_dir = os.path.join(PLAYLIST_TMP_DIR, job_id)
    try:
        _playlist_job_update(job_id, status='downloading', progress='Mengunduh audio referensi...')
        audio = _playlist_download_audio(video_id, job_dir)
        audio_dur = _playlist_media_duration(audio)
        if audio_dur <= 0:
            raise RuntimeError('Durasi audio tidak terbaca.')
        _playlist_job_update(job_id, status='downloading',
                             progress='Mengunduh footage Pixabay...',
                             audio_path=audio, audio_duration=audio_dur)
        # Tema footage: dari field 'Tema Footage' (panel) / langkah
        # Telegram bila diisi; kosong = auto dari tema musik. Bila tema
        # footage mengembalikan klip 16:9 small terlalu sedikit (< min),
        # fallback ke tema musik dan catat di job supaya jujur di UI/Telegram.
        footage_theme = (job.get('pixabay_theme') or '').strip()
        music_theme = job.get('theme', '')
        use_theme = footage_theme or music_theme
        pixabay_fallback = False
        try:
            clips, masks = _pixabay_search_clips(use_theme, count=PLAYLIST_MAX_CLIPS, job_id=job_id)
        except RuntimeError as e:
            if footage_theme and footage_theme != music_theme and 'kurang' in str(e):
                clips, masks = _pixabay_search_clips(music_theme, count=PLAYLIST_MAX_CLIPS, job_id=job_id)
                use_theme = music_theme
                pixabay_fallback = True
            else:
                raise
        clip_paths = []
        for i, c in enumerate(clips):
            dest = os.path.join(job_dir, f"clip_{i + 1:02d}.mp4")
            try:
                _pixabay_download_clip(c['url'], dest)
            except Exception as e:
                # 🔥 v1.3.75: klip bermasalah (bukan video / statis /
                # unduh gagal) dilewati saja, jangan gugurkan job.
                print(f"[pixabay] klip {i + 1} dilewati: {e}", flush=True)
                try:
                    os.remove(dest)
                except Exception:
                    pass
                continue
            clip_paths.append(dest)
        if len(clip_paths) < PLAYLIST_MIN_CLIPS:
            raise RuntimeError(f"Footage Pixabay valid kurang (dapat {len(clip_paths)}, butuh min {PLAYLIST_MIN_CLIPS}) — coba tema/kata kunci lain.")
        _prog = f"{len(clip_paths)} klip footage siap (key Pixabay {masks[0] if masks else '?'})"
        if pixabay_fallback:
            _prog += f" — tema footage '{footage_theme}' kurang klip, pakai tema musik."
        _playlist_job_update(job_id, status='preparing',
                             progress=_prog,
                             clips=clip_paths, pixabay_keys_used=masks,
                             pixabay_theme_used=use_theme,
                             pixabay_fallback=pixabay_fallback)
        channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        ch_name = (channel or {}).get('name', yt_id)
        # 🧹 v1.2.13: bersihkan judul/deskripsi/tag hasil rewrite —
        # judul referensi YouTube sering mengandung emoji/simbol yang
        # ditolak saat upload. Thumbnail memakai judul yang sudah bersih.
        _kw = _seo_main_keyword(job.get('ref_title', ''), ref_channel=job.get('ref_channel', ''))
        _raw_title = _playlist_rewrite_title(job.get('ref_title', ''), duration_sec=audio_dur, ref_channel=job.get('ref_channel', ''), channel_key=yt_id)
        _date_s = _wib_now().strftime('%Y-%m-%d')
        new_title = _clean_upload_text(_raw_title, max_len=100,
                                       fallback=f"Playlist {_date_s}",
                                       strip_channel=ch_name)
        _raw_desc = _playlist_rewrite_description(job.get('ref_description', ''), ch_name, new_title, ref_channel=job.get('ref_channel', ''))
        new_desc = _clean_upload_text(_raw_desc, max_len=5000, fallback=new_title)
        _raw_tags = _playlist_build_tags(job.get('ref_tags') or [], job.get('theme', ''), keyword=_kw)
        new_tags = ', '.join(_clean_upload_tags(_raw_tags)) or 'musik, playlist'
        _title_cleaned = (new_title != _raw_title or new_desc != _raw_desc
                          or new_tags != _raw_tags)
        thumb_path = ''
        if job.get('ref_thumbnail_url'):
            try:
                thumb_path = _playlist_make_thumbnail(
                    job['ref_thumbnail_url'], new_title, ch_name,
                    os.path.join(job_dir, 'thumb.jpg'))
            except Exception as e:
                # 🔥 v1.2.22: thumbnail gagal = job GAGAL dengan pesan jelas.
                # Jangan diam-diam lanjut tanpa thumbnail (dulu: saat upload
                # fallback ke thumbnail galeri acak — bukan gambar playlist).
                raise RuntimeError(f'Thumbnail playlist gagal dibuat: {e}')
        _playlist_job_update(job_id, status='preparing',
                             progress='Membuat task upload...',
                             new_title=new_title, new_description=new_desc,
                             new_tags=new_tags, thumb_path=thumb_path,
                             title_cleaned=_title_cleaned)
        res = _playlist_create_task({
            'yt_id': yt_id, 'title': new_title, 'description': new_desc,
            'tags': new_tags, 'publish_date': job.get('publish_date') or '',
            'privacy': job.get('privacy') or 'public',
            'playlist_id': job.get('playlist_id') or '',
            'audio_path': audio, 'clip_paths': clip_paths,
            'thumb_path': thumb_path, 'job_dir': job_dir, 'job_id': job_id,
            'ref_video_id': video_id, 'ref_title': job.get('ref_title', ''),
            'audio_duration': audio_dur, 'title_cleaned': _title_cleaned})
        if not res.get('ok'):
            raise RuntimeError(res.get('message') or 'Gagal membuat task.')
        if res.get('preview'):
            threading.Thread(target=_send_approval_previews,
                             args=(res['batch_id'], res['channel_name'], [res['preview']]),
                             daemon=True).start()
        _prog_done = 'Task dibuat — menunggu approval/antrean.'
        if pixabay_fallback:
            _prog_done += f" (Footage: tema '{footage_theme}' kurang klip — pakai tema musik.)"
        _playlist_job_update(job_id, status='queued',
                             progress=_prog_done,
                             task_id=res.get('task_id'),
                             approval='approved' if res.get('auto_mode') else 'pending')
        notify_event("🎵", new_title,
                     f"Musik Playlist siap — task dibuat untuk {ch_name} (referensi: {job.get('ref_title', '')[:40]}).")
    except Exception as e:
        _playlist_job_update(job_id, status='failed', error=str(e)[:300])
        notify_event("❌", job.get('ref_title') or 'Musik Playlist',
                     f"Gagal membangun playlist: {str(e)[:150]}")


# ============================================================
# 🎵 ZUMO / Kie.ai — generate lagu orisinal -> render visual ->
# upload (v1.3.0; desain ZUMO_DESIGN.md)
#
# Alur: order Telegram (/zumo) -> lirik (tempel format Suno ATAU
# generate jalur B, fallback jalur A) -> generate musik custom mode
# (2 varian/judul) -> download MP3 -> shuffle anti-berdampingan ->
# EQ PIANO per lagu + crossfade + limiter -> render gambar statis +
# spectrum tipis bawah + salju/sakura ikut beat -> task
# kind='zumo_playlist' masuk worker yang sama (disk, pause, kuota,
# approval, QC). Tanpa key Kie, job gagal jujur (bukan diam-diam).
# ============================================================

# Key Kie yang ditolak (401/402) nonaktif SEMENTARA — aktif lagi
# otomatis setelah 24 jam (penegasan user 21:15 WIB). 429 tidak
# menonaktifkan (hanya pindah ke key berikut).
KIE_DISABLE_REACTIVATE_HOURS = 24


def _kie_key_entries():
    """Daftar key Kie dari config.json — pola PERSIS Pixabay
    (_pixabay_key_entries): [{'key', 'disabled', 'disabled_at'}].
    'disabled_at' = 'YYYY-MM-DD HH:MM' WIB saat key ditolak (None bila
    tidak pernah ditolak)."""
    cfg = load_bot_config()
    raw = cfg.get('kie_api_keys')
    entries = []
    if isinstance(raw, list):
        for e in raw:
            if isinstance(e, dict) and isinstance(e.get('key'), str) and e['key'].strip():
                entries.append({'key': e['key'].strip(), 'disabled': bool(e.get('disabled')),
                                'disabled_at': (e.get('disabled_at') or None)})
            elif isinstance(e, str) and e.strip():
                entries.append({'key': e.strip(), 'disabled': False, 'disabled_at': None})
    legacy = cfg.get('kie_api_key')
    if isinstance(legacy, str) and legacy.strip():
        if all(e['key'] != legacy.strip() for e in entries):
            entries.append({'key': legacy.strip(), 'disabled': False, 'disabled_at': None})
        cfg['kie_api_keys'] = entries
        cfg.pop('kie_api_key', None)
        _save_bot_config(cfg)
    return entries


def _kie_save_entries(entries):
    cfg = load_bot_config()
    cfg['kie_api_keys'] = [{'key': e['key'], 'disabled': bool(e.get('disabled')),
                            'disabled_at': (e.get('disabled_at') or None)} for e in entries]
    cfg.pop('kie_api_key', None)
    _save_bot_config(cfg)


def _kie_masked_list():
    return [{'index': i + 1, 'masked': _mask_token(e['key']),
             'last4': e['key'][-4:],
             'disabled': bool(e.get('disabled')),
             'disabled_at': (e.get('disabled_at') or None)}
            for i, e in enumerate(_kie_key_entries())]


def _kie_active_keys():
    """Key aktif = tidak disabled, ATAU disabled-nya sudah lewat 24 jam
    (auto aktif lagi — 'nonaktif sementara', penegasan user 21:15 WIB).
    Key yang diaktifkan ulang dibersihkan flag + stempelnya, lalu
    disimpan. Disabled tanpa stempel (data lama) tetap nonaktif."""
    entries = _kie_key_entries()
    now = _wib_now().replace(tzinfo=None)
    changed = False
    active = []
    for e in entries:
        if not e.get('disabled'):
            active.append(e['key'])
            continue
        dt_s = (e.get('disabled_at') or '').strip()
        try:
            dt = datetime.strptime(dt_s, '%Y-%m-%d %H:%M')
        except Exception:
            continue
        if (now - dt).total_seconds() >= KIE_DISABLE_REACTIVATE_HOURS * 3600:
            e['disabled'] = False
            e['disabled_at'] = None
            changed = True
            active.append(e['key'])
    if changed:
        _kie_save_entries(entries)
    return active


def _kie_add_key(key):
    key = (key or '').strip()
    if not key:
        return False, '', 'API key tidak boleh kosong.'
    if len(key) < 8 or len(key) > 100 or any(ch.isspace() for ch in key):
        return False, '', 'API key tidak valid (8–100 karakter, tanpa spasi).'
    entries = _kie_key_entries()
    if any(e['key'] == key for e in entries):
        return False, _mask_token(key), 'Key itu sudah ada di daftar.'
    entries.append({'key': key, 'disabled': False})
    _kie_save_entries(entries)
    return True, _mask_token(key), f"Key {_mask_token(key)} ditambahkan."


def _kie_delete_key(ident):
    ident = str(ident or '').strip()
    entries = _kie_key_entries()
    if not entries:
        return False, '', 'Belum ada API key Kie.'
    target = None
    if ident.isdigit():
        idx = int(ident) - 1
        if 0 <= idx < len(entries):
            target = entries[idx]
    if target is None and len(ident) >= 4:
        matches = [e for e in entries if e['key'][-4:] == ident[-4:]]
        if len(matches) == 1:
            target = matches[0]
        elif len(matches) > 1:
            return False, '', 'Ada beberapa key dengan 4 karakter terakhir yang sama — hapus pakai nomor urut.'
    if target is None:
        return False, '', 'Key tidak ditemukan (pakai 4 karakter terakhir atau nomor urut).'
    entries.remove(target)
    _kie_save_entries(entries)
    return True, _mask_token(target['key']), f"Key {_mask_token(target['key'])} dihapus."


def _kie_disable_key(key):
    """Tandai key nonaktif SEMENTARA (401/402) + stempel waktu penolakan —
    aktif lagi otomatis setelah 24 jam (lihat _kie_active_keys).
    429 TIDAK memanggil ini (hanya pindah key, tanpa menonaktifkan)."""
    entries = _kie_key_entries()
    changed = False
    for e in entries:
        if e['key'] == key and not e.get('disabled'):
            e['disabled'] = True
            e['disabled_at'] = _wib_now().strftime('%Y-%m-%d %H:%M')
            changed = True
    if changed:
        _kie_save_entries(entries)


def _kie_balances():
    """Saldo per key aktif (penegasan user 21:15 WIB): [{index, masked,
    last4, disabled, disabled_at, balance}]. Balance dibaca dari Kie
    per key (1 panggilan/key); key yang gagal dibaca -> balance None
    (jujur 'tidak terbaca', bukan angka karangan). Key nonaktif
    balance-nya None (tidak dicek)."""
    out = []
    for i, e in enumerate(_kie_key_entries()):
        bal = None
        if not e.get('disabled') and _ZUMO_CORE_OK:
            try:
                bal = _zumo_core.kie_get_balance(e['key'])
            except Exception:
                bal = None
        out.append({'index': i + 1, 'masked': _mask_token(e['key']),
                    'last4': e['key'][-4:],
                    'disabled': bool(e.get('disabled')),
                    'disabled_at': (e.get('disabled_at') or None),
                    'balance': bal})
    return out


def _zumo_profile_get(yt_id):
    """Profil racik channel: default Cozy Sax -> overlay profil tersimpan
    per channel (config['zumo_profiles'][yt_id]) -> overlay model global
    (config['zumo_model'], dari /zumomodel). Profil BUKAN konstanta:
    tiap channel/order bisa beda genre/bahasa/vokal (mis. reggae
    Indonesia bariton vs sax Inggris) — lihat ZUMO_DEFAULT_PROFILE dan
    ZUMO_REGGAE_PROFILE di zumo_core.py."""
    base = dict(_zumo_core.ZUMO_DEFAULT_PROFILE) if _ZUMO_CORE_OK else {
        'style': 'smooth jazz saxophone, warm, relaxing, romantic',
        'lyrics_language': 'en', 'vocal_gender': 'f',
        'model': 'V5.5', 'duration_min': 240, 'duration_max': 360,
        'particle': 'snow', 'spectrum_color': 'white',
        'song_titles': 3, 'songs_keep': 5}
    try:
        cfg = load_bot_config()
        profs = cfg.get('zumo_profiles') or {}
        if isinstance(profs, dict) and isinstance(profs.get(yt_id), dict):
            for k, v in profs[yt_id].items():
                if k in base and v not in (None, ''):
                    base[k] = v
        gm = (cfg.get('zumo_model') or '').strip()
        if gm in (_zumo_core.ZUMO_MODELS if _ZUMO_CORE_OK else ['V5.5', 'V6', 'V6_MINI', 'V6_WILD']):
            base['model'] = gm
    except Exception:
        pass
    return base


def _zumo_profile_set(yt_id, fields):
    """Simpan field profil untuk channel (hanya key yang dikenal)."""
    allowed = {'style', 'lyrics_language', 'vocal_gender', 'model',
               'duration_min', 'duration_max', 'particle',
               'spectrum_color', 'song_titles', 'songs_keep'}
    cfg = load_bot_config()
    profs = cfg.get('zumo_profiles') or {}
    if not isinstance(profs, dict):
        profs = {}
    cur = dict(profs.get(yt_id) or {})
    for k, v in (fields or {}).items():
        if k in allowed and v not in (None, ''):
            cur[k] = v
    profs[yt_id] = cur
    cfg['zumo_profiles'] = profs
    _save_bot_config(cfg)
    return cur


def _zumo_jobs_load():
    try:
        with open(ZUMO_JOBS_FILE, 'r', encoding='utf-8') as f:
            d = json.load(f)
        if isinstance(d, dict) and isinstance(d.get('jobs'), list):
            return d
    except Exception:
        pass
    return {'jobs': []}


def _zumo_jobs_save(data):
    try:
        tmp = ZUMO_JOBS_FILE + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False)
        os.replace(tmp, ZUMO_JOBS_FILE)
    except Exception:
        pass


def _zumo_job_add(job):
    with db_lock:
        data = _zumo_jobs_load()
        data['jobs'].insert(0, job)
        # 🔥 v1.3.33 (audit M3): 50 terlalu kecil — batch 7 channel
        # x riset+create+retry menghabiskan slot dalam sehari sehingga
        # job 'done' pagi terbuang sebelum 24 jam dan dedupe 24 jam
        # tidak bisa bekerja. 500 ≈ 2 minggu. (Sama untuk Zumo.)
        data['jobs'] = data['jobs'][:500]
        _zumo_jobs_save(data)
    return job


def _zumo_job_get(job_id):
    for j in _zumo_jobs_load().get('jobs', []):
        if j.get('job_id') == job_id:
            return j
    return None


def _zumo_job_update(job_id, **fields):
    with db_lock:
        data = _zumo_jobs_load()
        for j in data.get('jobs', []):
            if j.get('job_id') == job_id:
                j.update(fields)
                j['updated_at'] = _wib_now().strftime('%Y-%m-%d %H:%M')
                _zumo_jobs_save(data)
                return j
    return None


def _zumo_background_path(yt_id):
    """Gambar latar statis default channel (1920x1080, buatan Muse).
    Urutan: 1) galeri uploads/<yt_id>/backgrounds/zumo_cozy_sax_bg.jpg
    (file dikirim ke VPS saat deploy/persiapan); 2) salinan bundel
    zumo_assets/zumo_cozy_sax_bg.jpg di folder repo; 3) gambar apa pun
    di galeri backgrounds/ channel itu; 4) None -> fallback Pixabay.
    Gambar di-resize ke 1920x1080 saat render (crop tengah)."""
    try:
        bg_dir = _safe_join_under(BASE_UPLOAD, yt_id, 'backgrounds')
    except Exception:
        return None
    for cand in (os.path.join(bg_dir, _zumo_core.ZUMO_BG_FILENAME) if _ZUMO_CORE_OK else None,
                 os.path.join(BASE_DIR, 'zumo_assets', 'zumo_cozy_sax_bg.jpg')):
        if cand and os.path.isfile(cand):
            return cand
    try:
        for f in sorted(os.listdir(bg_dir)):
            if f.lower().endswith(('.jpg', '.jpeg', '.png')):
                return os.path.join(bg_dir, f)
    except Exception:
        pass
    return None


def _zumo_background_video_path(yt_id):
    """Video latar animasi channel (loop) — urutan: 1) galeri
    uploads/<yt_id>/backgrounds/zumo_cozy_sax_bg.mp4 (stem sama dengan
    ZUMO_BG_FILENAME, ganti .jpg -> .mp4); 2) file .mp4 apa pun di
    galeri backgrounds/ channel itu. Bila ada, _zumo_render memakai
    video ini (loop sampai durasi lagu) sebagai latar, BUKAN gambar
    statis — permintaan user 21 Nov 2026 (asap kopi + daun/lampu
    bergerak, kamera statis)."""
    try:
        bg_dir = _safe_join_under(BASE_UPLOAD, yt_id, 'backgrounds')
    except Exception:
        return None
    stem = os.path.splitext(_zumo_core.ZUMO_BG_FILENAME)[0] if _ZUMO_CORE_OK else 'zumo_cozy_sax_bg'
    cand = os.path.join(bg_dir, stem + '.mp4')
    if os.path.isfile(cand) and os.path.getsize(cand) > 0:
        return cand
    try:
        for f in sorted(os.listdir(bg_dir)):
            if f.lower().endswith('.mp4'):
                p = os.path.join(bg_dir, f)
                if os.path.getsize(p) > 0:
                    return p
    except Exception:
        pass
    return None


def _zumo_bank_save_track(yt_id, channel_name, src_path, meta):
    """Simpan 1 lagu mentah ke bank (uploads/zumo_bank/<nama-channel>/).
    Kegagalan bank TIDAK PERNAH menggagalkan job upload — dicatat ke
    log saja, job lanjut. Kembalikan entri bank atau None."""
    if not _ZUMO_CORE_OK:
        return None
    try:
        entry = _zumo_core.zumo_bank_add(ZUMO_BANK_DIR, channel_name, src_path, meta)
        if entry is None:
            print(f"[zumo] bank: gagal menyimpan '{meta.get('title')}' (channel {channel_name})", flush=True)
        return entry
    except Exception as e:
        print(f"[zumo] bank error: {e}", flush=True)
        return None


def _zumo_ensure_particle_loop(kind):
    """Loop partikel 8 dtk (snow/sakura) di uploads/zumo_assets/ — dibuat
    sekali via zumo_core.make_particle_loop, dipakai ulang semua playlist.
    Kembalikan path atau None bila gagal (render tetap jalan tanpa salju)."""
    kind = 'sakura' if kind == 'sakura' else 'snow'
    try:
        os.makedirs(ZUMO_ASSETS_DIR, exist_ok=True)
        path = os.path.join(ZUMO_ASSETS_DIR, f'{kind}.mp4')
        if os.path.isfile(path) and os.path.getsize(path) > 0:
            return path
        if not _ZUMO_CORE_OK:
            return None
        return _zumo_core.make_particle_loop(
            kind, path, duration=8.0, fps=30, width=1920, height=1080,
            ffmpeg_path=get_ffmpeg_path())
    except Exception as e:
        print(f"[zumo] loop partikel {kind} gagal dibuat: {e}", flush=True)
        return None


def _zumo_make_thumbnail(image_path, new_title, channel_name, out_path,
                         badge=None):
    """Thumbnail Zumo 1280x720 — gaya thumbnail YouTube niche relaxing
    jazz / coffee (analisa referensi 21 Nov 2026: 'RELAX JAZZ CAFE',
    'November Morning Bossa Nova Jazz', 'Cozy November Coffee Jazz'):
    foto FULL-BLEED (tanpa panel/split, tanpa badge, tanpa nama channel),
    teks PENDEK di atas gambar (maks 3 kata, _zumo_thumb_short_text),
    2 baris serif elegan di tengah-atas: baris 1 putih, baris 2 warna
    krem keemasan + scrim gelap halus di atas supaya teks terbaca.
    Bila image_path kosong/tidak ada -> gradien lokal saja (tanpa foto).
    Gagal total (PIL rusak) -> raise, JANGAN pakai thumbnail acak."""
    from PIL import Image, ImageDraw
    lines = _zumo_thumb_short_text(new_title)
    photo = None
    if image_path and os.path.isfile(image_path):
        try:
            photo = Image.open(image_path).convert('RGB')
        except Exception as e:
            print(f"[zumo] thumbnail: gambar lokal tidak bisa dibuka ({e}) — pakai gradien.", flush=True)
            photo = None
    if photo is not None:
        w, h = photo.size
        target = 1280 / 720
        if w / h > target:
            nw = int(h * target)
            x = (w - nw) // 2
            photo = photo.crop((x, 0, x + nw, h))
        else:
            nh = int(w / target)
            y = (h - nh) // 2
            photo = photo.crop((0, y, w, y + nh))
        img = photo.resize((1280, 720), Image.LANCZOS)
    else:
        c1, c2 = (38, 28, 20), (12, 10, 8)
        img = _playlist_thumb_gradient(1280, 720, c1, c2)
    # Scrim atas: hitam transparan 150 -> 0 setinggi ~360px supaya teks
    # putih terbaca di atas foto terang (langit pagi). Scrim dibuat
    # seukuran gambar penuh (alpha_composite butuh ukuran sama).
    base = img.convert('RGBA')
    scrim = Image.new('RGBA', (1280, 720), (0, 0, 0, 0))
    sd = ImageDraw.Draw(scrim)
    for yy in range(360):
        a = int(150 * (1 - yy / 360) ** 1.4)
        sd.line([(0, yy), (1280, yy)], fill=(0, 0, 0, a))
    img = Image.alpha_composite(base, scrim).convert('RGB')
    draw = ImageDraw.Draw(img, 'RGBA')
    # Ukuran font: turunkan sampai semua baris muat (lebar maks 1120px).
    max_w = 1120
    size1, size2 = 92, 122
    while size1 > 40:
        f1 = _playlist_thumb_serif(size1, bold=True)
        f2 = _playlist_thumb_serif(size2, bold=True)
        fonts_try = [f1] * (len(lines) - 1) + [f2]
        if all(draw.textlength(ln, font=f) <= max_w
               for ln, f in zip(lines, fonts_try)):
            break
        size1 -= 6
        size2 -= 8
    f1 = _playlist_thumb_serif(size1, bold=True)
    f2 = _playlist_thumb_serif(size2, bold=True)
    fonts = [f1] * (len(lines) - 1) + [f2]
    colors = [(255, 255, 255)] * (len(lines) - 1) + [(255, 216, 140)]
    y = 64
    for ln, f, col in zip(lines, fonts, colors):
        tw = draw.textlength(ln, font=f)
        x = (1280 - tw) // 2
        draw.text((x + 2, y + 4), ln, font=f, fill=(0, 0, 0, 170))
        draw.text((x, y), ln, font=f, fill=col + (255,))
        y += int(f.size * 1.12)
    q = 88
    while True:
        img.save(out_path, 'JPEG', quality=q, optimize=True)
        if os.path.getsize(out_path) <= 2_000_000 or q <= 50:
            break
        q -= 8
    return out_path



def _zumo_rewrite_title(track_titles, theme, channel_name):
    """Judul video dari judul lagu hasil generate + tema. Nama channel
    TIDAK ditempel ke judul (v1.2.23 — _strip_channel_title_suffix juga
    berjaga di _clean_upload_text). Maks 100 karakter, bersih emoji."""
    first = (track_titles or [''])[0].strip() if track_titles else ''
    th = (theme or '').strip()
    # 🔥 v1.3.82: jangan ulang tema bila kata-katanya sudah ada di judul
    # lagu (dulu: 'Cozy European Morning Coffee Jazz — Cozy European
    # Morning Coffee (2 Lagu)'). Suffix '(N Lagu)' juga dibuang — bukan
    # gaya judul YouTube; jumlah lagu sudah ada di deskripsi (tracklist).
    th_words = set(th.lower().split())
    first_words = set(first.lower().split())
    if first and th and th_words and th_words <= first_words:
        raw = first
    elif first and th and first.lower() not in th.lower():
        raw = f"{first} — {th}"
    elif first:
        raw = first
    elif th:
        raw = th
    else:
        raw = 'Lagu Zumo'
    return _clean_upload_text(raw, max_len=100, fallback='Lagu Zumo',
                              strip_channel=channel_name)


def _zumo_build_description(theme, tracks, channel_name, title):
    """Deskripsi SEO: pembuka + daftar lagu + timestamp (dari offset
    gabungan, SETELAH crossfade) + ajakan subscribe + nama channel.
    tracks = [{title, duration, offset}]."""
    lines = []
    lines.append(f"{title}")
    lines.append('')
    th = (theme or '').strip()
    if th:
        lines.append(f"Nikmati {th} — lagu orisinal yang dibuat khusus untuk menemani harimu.")
    lines.append('')
    lines.append('Daftar lagu:')
    for tr in tracks:
        off = float(tr.get('offset') or 0)
        mm, ss = int(off // 60), int(off % 60)
        lines.append(f"{mm:02d}:{ss:02d} {tr.get('title') or 'Lagu'}")
    lines.append('')
    lines.append('Jangan lupa subscribe dan nyalakan loncengnya supaya tidak ketinggalan lagu baru.')
    if channel_name:
        lines.append(f"Channel: {channel_name}")
    return _clean_upload_text('\n'.join(lines), max_len=5000, fallback=title)


def _zumo_build_tags(style, track_titles, channel_name):
    """Tag dari style/genre + judul lagu + kata kunci kanal. Total
    <=400 karakter, tanpa '#', tanpa duplikat (pola playlist)."""
    words = []
    for part in ((style or '').replace(',', ' ').split()):
        words.append(part)
    for t in (track_titles or []):
        for w in (t or '').replace(',', ' ').split():
            words.append(w)
    for w in ('musik santai', 'relaxing', 'cozy'):
        words.append(w)
    if channel_name:
        words.append(channel_name)
    seen, tags = set(), []
    for w in words:
        lw = w.lower().strip()
        if len(lw) < 2 or lw in seen:
            continue
        seen.add(lw)
        tags.append(w)
    return ', '.join(_clean_upload_tags(tags)) or 'musik, playlist'


def _zumo_render(task_id, task, yt_id):
    """Render video Zumo: gambar latar STATIS (tanpa zoompan) + spectrum
    TIPIS di bawah (1920x140, y=940) + loop salju/sakura ikut beat
    (denyut eq per segmen lagu). 1 pass FFmpeg, H.264 1080p30 + AAC.
    Spectrum sudah ikut beat secara bawaan (showspectrum dari frame
    audio) — tidak perlu modulasi tambahan. Partikel ikut beat lewat
    denyut brightness eq per-frame pada BPM tiap lagu (Opsi D desain
    §5.3 — BUKAN geq per-frame, BUKAN render frame-by-frame Python).
    Reuse-final seperti playlist (retry setelah upload gagal)."""
    if stop_flags.get(task_id):
        raise Exception('Dibatalkan')
    final_video = os.path.join(BASE_DIR, f"static/final_{task_id}.mp4")
    if os.path.exists(final_video) and os.path.getsize(final_video) > 0:
        try:
            _pv = _probe_video(final_video)
            _pdur = float((_pv.get('format') or {}).get('duration') or 0) if _pv else 0
        except Exception:
            _pdur = 0
        if _pdur > 0:
            task['zumo_render_method'] = task.get('zumo_render_method') or 'reuse-final'
            try:
                _zumo_job_update(task.get('zumo_job_id'), render_method='reuse-final')
            except Exception:
                pass
            return final_video
    audio = (task.get('zumo_audio') or '').strip()
    bg = (task.get('zumo_bg_image') or '').strip()
    if not audio or not os.path.exists(audio):
        raise Exception('File audio Zumo tidak ditemukan.')
    # 🔥 v1.3.82: latar VIDEO (loop) didahulukan bila ada — gambar statis
    # hanya dipakai bila tidak ada video. Video boleh ada tanpa gambar.
    bg_video = _zumo_background_video_path(yt_id)
    if not bg_video and (not bg or not os.path.exists(bg)):
        raise Exception('Gambar latar Zumo tidak ditemukan.')
    audio_dur = _playlist_media_duration(audio)
    if audio_dur <= 0:
        raise Exception('Durasi audio tidak terbaca.')
    color = (task.get('zumo_spectrum_color') or 'white').strip().lower()
    if color not in ('white', 'cyan', 'viridis'):
        color = 'white'
    # showspectrum `color` HANYA menerima enum preset (0-14: channel,
    # intensity, rainbow, viridis, ...) — nama warna polos ('white'/'cyan')
    # DITOLAK FFmpeg 8.1.2 ("Undefined constant ... in 'white'").
    # Jadi putih/cyan dibuat dari `color=intensity` -> `format=gray`
    # (batang abu-abu netral, latar tetap hitam) -> `lutrgb` KALI 2.2
    # (bukan `eq=brightness`, yang ikut mengangkat latar jadi abu-abu).
    # Cyan: `colorchannelmixer` mematikan kanal merah sebelum lutrgb.
    # Terverifikasi empiris di harness v1.3.0 (piksel terang putih
    # 217,217,217 / cyan 5,250,244, latar tetap hitam).
    # OPACITY spectrum 65% (user 21:21 WIB: 60-70%, semi-transparan
    # supaya gambar latar tetap tembus di balik spectrum — jangan 100%
    # solid): ujung rantai `format=yuva420p,colorchannelmixer=aa=0.65`
    # memberi alpha 0.65 ke SELURUH strip (batang + latar hitamnya),
    # lalu `overlay` memadukan alpha itu di atas gambar. Nilai tengah
    # 0.65 dari rentang 0.60-0.70 yang diminta user.
    _SPEC_FILTERS = {
        'white': ("showspectrum=s=1920x140:mode=separate:scale=log:color=intensity:slide=scroll,"
                  "format=gray,lutrgb=r='min(255,val*2.2)':g='min(255,val*2.2)':b='min(255,val*2.2)',"
                  "format=yuva420p,colorchannelmixer=aa=0.65"),
        'cyan': ("showspectrum=s=1920x140:mode=separate:scale=log:color=intensity:slide=scroll,"
                 "format=gray,colorchannelmixer=rr=0:gg=1:bb=1,"
                 "lutrgb=r='min(255,val*2.2)':g='min(255,val*2.2)':b='min(255,val*2.2)',"
                 "format=yuva420p,colorchannelmixer=aa=0.65"),
        'viridis': ("showspectrum=s=1920x140:mode=separate:scale=log:color=viridis:slide=scroll,"
                    "format=yuva420p,colorchannelmixer=aa=0.65"),
    }
    ff = get_ffmpeg_path()
    # Denyut partikel per segmen lagu (eq brightness, amplitudo 0.06 —
    # halus, bukan berkedip kasar).
    eq_chain = []
    for seg in (task.get('zumo_segments') or []):
        try:
            t0 = float(seg.get('t0') or 0)
            t1 = float(seg.get('t1') or 0)
            bpm = float(seg.get('bpm') or 60)
        except Exception:
            continue
        if t1 <= t0 or bpm <= 0:
            continue
        eq_chain.append(
            f"eq=brightness='0.06*sin(2*PI*(t-{t0:.2f})*{bpm:.1f}/60)':eval=frame:enable='between(t,{t0:.2f},{t1:.2f})'")
    if not eq_chain:
        eq_chain.append("eq=brightness='0.05*sin(2*PI*t*60/60)':eval=frame")
    loop = (task.get('zumo_particle_loop') or '').strip()
    filt = (
        "[0:v]scale=1920:1080:force_original_aspect_ratio=increase,crop=1920:1080,format=yuv420p[bg];"
        "[1:a]asplit=2[aud][specin];"
        f"[specin]{_SPEC_FILTERS[color]}[spec];"
        "[bg][spec]overlay=0:940[bg2]"
    )
    inputs = [ff, '-y']
    if bg_video:
        inputs += ['-stream_loop', '-1', '-i', bg_video]
    else:
        inputs += ['-loop', '1', '-i', bg]
    inputs += ['-i', audio]
    if loop and os.path.exists(loop):
        inputs += ['-stream_loop', '-1', '-i', loop]
        # Screen blend HARUS di ruang RGB (gbrp), bukan YUV: blend
        # all_mode=screen di YUV menerapkan rumus screen ke plane U/V
        # juga, dan chroma netral (128) di-screen jadi ~191 — seluruh
        # frame berubah magenta (ditemukan harness B9c: bg hangat
        # (200,163,110) jadi (255,98,254)). Di gbrp, partikel hitam
        # (0) = tidak berubah, putih (255) = putih — benar. Round-trip
        # yuv->gbrp->yuv menggeser ~1 level, diabaikan.
        filt += (";[2:v]scale=1920:1080,format=yuv420p," + ','.join(eq_chain) +
                 "[pt];[bg2]format=gbrp[bg3];[pt]format=gbrp[pt3];"
                 "[bg3][pt3]blend=all_mode=screen,format=yuv420p[vout]")
    else:
        filt += ",format=yuv420p[vout]"
    cmd = inputs + ['-filter_complex', filt,
                    '-map', '[vout]', '-map', '[aud]',
                    '-c:v', 'libx264', '-preset', 'veryfast', '-crf', '20',
                    '-r', '30', '-c:a', 'aac', '-b:a', '192k',
                    '-t', f"{audio_dur:.3f}", '-movflags', '+faststart',
                    final_video]
    _run_ffmpeg_zoom(task_id, cmd, "FFmpeg Zumo Render Error",
                     total_sec=audio_dur, pct_base=0.0, pct_span=100.0)
    if not os.path.exists(final_video) or os.path.getsize(final_video) == 0:
        raise Exception('Render Zumo gagal (file final kosong).')
    base_method = 'video+spectrum' if bg_video else 'static+spectrum'
    task['zumo_render_method'] = base_method + '+particle' if loop else base_method
    try:
        _zumo_job_update(task.get('zumo_job_id'), render_method=task['zumo_render_method'])
    except Exception:
        pass
    return final_video


def _zumo_cleanup_after_upload(task):
    """Setelah upload sukses: hapus video final + folder kerja job Zumo.
    Gagal hapus tidak boleh menggagalkan status Tayang."""
    try:
        task_id = task.get('id')
        if task_id:
            try:
                os.remove(os.path.join(BASE_DIR, f"static/final_{task_id}.mp4"))
            except Exception:
                pass
        job_dir = (task.get('zumo_job_dir') or '').strip()
        base = os.path.abspath(ZUMO_TMP_DIR)
        if job_dir and os.path.abspath(job_dir).startswith(base + os.sep):
            shutil.rmtree(job_dir, ignore_errors=True)
        _zumo_job_update(task.get('zumo_job_id'), status='done',
                         video_id=task.get('video_id'),
                         video_url=task.get('video_url'),
                         cleaned_at=_wib_now().strftime('%Y-%m-%d %H:%M'))
    except Exception:
        pass


def _zumo_create_task(data):
    """Inti pembuatan SATU task Zumo — cermin _playlist_create_task:
    stagger jam, autopilot auto vs approval, dijeda -> ditahan, lalu
    worker yang sama (disk -> render Zumo -> QC -> upload -> kuota)."""
    yt_id = (data.get('yt_id') or '').strip()
    title = (data.get('title') or '').strip()
    if not yt_id or not title:
        return {"ok": False, "message": "Channel dan judul wajib diisi.", "http": 400}
    channel_rec = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if channel_rec is None:
        return {"ok": False, "message": "Channel tidak ditemukan.", "http": 404}
    title = _shorts_dedup_title(yt_id, title, channel_rec)
    _raw_title = title
    _date_s = _wib_now().strftime('%Y-%m-%d')
    title = _clean_upload_text(title, max_len=100,
                               fallback=f"Zumo {_date_s}",
                               strip_channel=channel_rec.get('name', ''))
    title_cleaned = bool(data.get('title_cleaned')) or (title != _raw_title)
    if title != _raw_title:
        title = _shorts_dedup_title(yt_id, title, channel_rec)
    _raw_desc = (data.get('description') or '').strip() or title
    _description = _clean_upload_text(_raw_desc, max_len=5000, fallback=title)
    _raw_tags = (data.get('tags') or '').strip()
    _tags = ', '.join(_clean_upload_tags(_raw_tags)) or 'musik, playlist'
    title_cleaned = title_cleaned or (_description != _raw_desc) or (_tags != _raw_tags)
    try:
        pub_dt = datetime.strptime(data.get('publish_date', ''), '%Y-%m-%dT%H:%M')
    except Exception:
        try:
            pub_dt = datetime.strptime(data.get('publish_date', ''), '%Y-%m-%d %H:%M')
        except Exception:
            return {"ok": False, "message": "Format tanggal salah (pakai YYYY-MM-DD HH:MM).", "http": 400}
    v_date_str = _stagger_publish_date(pub_dt.strftime('%Y-%m-%d %H:%M'), yt_id)
    try:
        audio_dur = float(data.get('audio_duration') or 0)
    except Exception:
        audio_dur = 0.0
    if audio_dur <= 0:
        return {"ok": False, "message": "Durasi audio tidak valid.", "http": 400}
    if not (data.get('audio_path') or '').strip() or not os.path.exists(data.get('audio_path') or ''):
        return {"ok": False, "message": "File audio Zumo tidak ditemukan.", "http": 400}
    t_id = _next_task_id()
    batch_id = f"zm{t_id}"
    channel_name = channel_rec.get('name', yt_id)
    auto_mode = _autopilot_of(channel_rec) == 'auto'
    channel_paused = _channel_paused(yt_id)
    blueprint = {
        "id": t_id, "yt_id": yt_id, "title": title,
        "publish_date": v_date_str,
        "kind": ZUMO_KIND,
        "target_duration_hours": audio_dur / 3600.0,
        "description": _description,
        "tags": _tags,
        "privacy": (data.get('privacy') or 'public').strip() or 'public',
        "zumo_audio": data.get('audio_path'),
        "zumo_bg_image": (data.get('bg_image') or '').strip(),
        "zumo_particle_loop": (data.get('particle_loop') or '').strip(),
        "zumo_spectrum_color": (data.get('spectrum_color') or 'white').strip(),
        "zumo_segments": list(data.get('segments') or []),
        "zumo_tracks": list(data.get('tracks') or []),
        "zumo_credits": data.get('credits') or 0,
        "playlist_thumb": (data.get('thumb_path') or '').strip(),
        "zumo_job_id": (data.get('job_id') or '').strip(),
        "zumo_job_dir": (data.get('job_dir') or '').strip(),
        "render_preset": "ultrafast",
        "source_mode": "mix",
        "batch_id": batch_id,
        "title_cleaned": title_cleaned,
    }
    if auto_mode and not channel_paused and render_queue.qsize() >= QUEUE_MAX_SIZE:
        return {"ok": False,
                "message": f"Antrean render penuh (maks {QUEUE_MAX_SIZE} task). "
                           "Tunggu antrean berkurang lalu coba lagi.", "http": 429}
    with db_lock:
        active_tasks.append({
            "id": t_id, "yt_id": yt_id, "title": title,
            "time": v_date_str,
            "status": (CHANNEL_PAUSED_STATUS if channel_paused else "Menunggu Giliran (Queue) ⏳") if auto_mode else APPROVAL_PENDING_STATUS,
            "approval": "approved" if auto_mode else "pending",
            "batch_id": batch_id,
            "kind": ZUMO_KIND,
            "type": "🎵 Zumo Playlist",
            "title_cleaned": title_cleaned,
            "blueprint": blueprint,
        })
        _activity_task_created("🎵 Zumo Playlist", title, yt_id)  # 🔥 v1.3.71: catat ke log aktivitas
        for c in database_channel:
            if c['yt_id'] == yt_id:
                c['last_scheduled_date'] = v_date_str
                c['reminder_sent'] = False
                break
        save_channels(database_channel)
    save_tasks_db()
    if auto_mode:
        if not channel_paused:
            try:
                queue_put(blueprint)
            except Exception as e:
                print(f"Gagal queue zumo {t_id}: {e}", flush=True)
                with db_lock:
                    for t in list(active_tasks):
                        if t.get('id') == t_id:
                            active_tasks.remove(t)
                            t['status'] = "Gagal ❌ (Stuck — antrean penuh saat dibuat)"
                            t['updated_at'] = time.time()
                            history_tasks.insert(0, t)
                            break
                    save_tasks_db()
                return {"ok": False,
                        "message": "Gagal masuk antrean render (antrean penuh). "
                                   "Task ada di Riwayat dan bisa dipulihkan lewat 'Pulihkan Task Gagal'.", "http": 500}
        return {"ok": True, "message": "Playlist Zumo dibuat dan langsung masuk antrean (mode Full-Auto)!",
                "http": 200, "task_id": t_id, "auto_mode": True, "batch_id": batch_id,
                "preview": None, "channel_name": channel_name, "publish_date": v_date_str,
                "title": title, "title_cleaned": title_cleaned}
    preview = {"id": t_id, "title": title, "publish_date": v_date_str,
               "duration_label": f"{int(audio_dur // 60)} menit (Zumo Playlist)",
               "description": blueprint["description"], "privacy": blueprint["privacy"],
               "title_cleaned": title_cleaned, "tags": _tags,
               "thumb_path": (data.get('thumb_path') or '').strip()}
    return {"ok": True, "message": "Playlist Zumo dibuat dan menunggu approval di Telegram!",
            "http": 200, "task_id": t_id, "auto_mode": False, "batch_id": batch_id,
            "preview": preview, "channel_name": channel_name, "publish_date": v_date_str,
            "title": title, "title_cleaned": title_cleaned}


def _zumo_gen_music(keys, model, title, lyrics, style, duration, vocal_gender,
                    instrumental=False):
    """Generate musik 1 judul dengan fallback model: coba model yang
    diminta dulu; bila endpoint menolak (422 — V5.5 Discontinued),
    coba sekali lagi dengan V6 dan laporkan fallback. Kembalikan
    (task_id, key_index, model_dipakai, model_fallback_bool).
    `lyrics` = None -> jalur A (non-custom, prompt = tema).
    `instrumental` = True -> kie_create_music(instrumental=True), tanpa
    lyrics/prompt/vocal_gender (user 20:58 WIB)."""
    last_err = None
    for attempt_model in ([model] if model == 'V6' else [model, 'V6']):
        try:
            if instrumental:
                task_id, kidx = _zumo_core.kie_call_with_failover(
                    keys,
                    lambda k: _zumo_core.kie_create_music(
                        k, attempt_model, title, None, style,
                        duration=duration, instrumental=True),
                    disable_cb=_kie_disable_key)
            elif lyrics:
                task_id, kidx = _zumo_core.kie_call_with_failover(
                    keys,
                    lambda k: _zumo_core.kie_create_music(
                        k, attempt_model, title, lyrics, style,
                        duration=duration, vocal_gender=vocal_gender),
                    disable_cb=_kie_disable_key)
            else:
                task_id, kidx = _zumo_core.kie_call_with_failover(
                    keys,
                    lambda k: _zumo_core.kie_create_music_prompt(
                        k, attempt_model, title, title),
                    disable_cb=_kie_disable_key)
            return task_id, kidx, attempt_model, (attempt_model != model)
        except _zumo_core.KieError as e:
            last_err = e
            if (e.code or '') == '422' and attempt_model != 'V6':
                continue
            raise
    if last_err is not None:
        raise last_err
    raise _zumo_core.KieError('Generate musik gagal tanpa pesan', code='fail')


def _zumo_poll_retry(api_key, task_id):
    """Poll dengan retry jaringan: _kie_get yang gagal jaringan dicoba
    lagi 2x (interval poll tetap 15 dtk di dalam kie_poll)."""
    last = None
    for _try in range(3):
        try:
            return _zumo_core.kie_poll(api_key, task_id)
        except _zumo_core.KieError as e:
            last = e
            if (e.code or '') == 'network':
                time.sleep(15)
                continue
            raise
    raise last


def _zumo_do_create(data):
    """Validasi order Zumo (panel + operator Telegram memakai helper yang
    SAMA) -> buat job -> jalankan build thread. Kembalikan
    (ok, message, http, job_id)."""
    if not _ZUMO_CORE_OK:
        return False, 'zumo_core.py belum ter-deploy di VPS.', 500, ''
    yt_id = ((data.get('yt_id') or '').strip()
             or _zumo_core.ZUMO_COZY_YT_ID)
    if not _valid_yt_id(yt_id):
        return False, 'Channel tidak valid.', 400, ''
    channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if not channel:
        return False, 'Channel tidak ditemukan.', 404, ''
    # Mode order (user 20:58 WIB): 'upload' (generate -> upload, default),
    # 'bank' (generate massal -> bank, tanpa upload), 'assemble' (rakit
    # video dari bank, tanpa generate). Alias to_bank/from_bank diterima.
    mode = (str(data.get('mode') or 'upload')).strip().lower()
    mode = {'to_bank': 'bank', 'from_bank': 'assemble'}.get(mode, mode)
    if mode not in ('upload', 'bank', 'assemble'):
        return False, "Mode tidak dikenal (upload/bank/assemble).", 400, ''
    instrumental = bool(data.get('instrumental'))
    try:
        bank_count = int(data.get('bank_count') or 8)
    except Exception:
        bank_count = 8
    bank_count = max(1, min(30, bank_count))
    theme = (data.get('theme') or '').strip()
    if not theme and mode == 'assemble':
        theme = 'Bank'
    if not theme:
        return False, 'Isi tema dulu.', 400, ''
    profile = _zumo_profile_get(yt_id)
    # Field order menimpa profil + disimpan sebagai profil channel itu.
    prof_fields = {}
    for k in ('style', 'lyrics_language', 'vocal_gender', 'model',
              'particle', 'spectrum_color'):
        if (data.get(k) or '').strip() if isinstance(data.get(k), str) else data.get(k):
            prof_fields[k] = data[k]
    if prof_fields.get('model') and prof_fields['model'] not in _zumo_core.ZUMO_MODELS:
        return False, 'Model tidak dikenal (V5.5/V6/V6_MINI/V6_WILD).', 400, ''
    if prof_fields.get('particle') and prof_fields['particle'] not in ('snow', 'sakura'):
        return False, "Partikel tidak dikenal (snow/sakura).", 400, ''
    if prof_fields.get('spectrum_color') and prof_fields['spectrum_color'] not in ('white', 'cyan', 'viridis'):
        return False, 'Warna spectrum tidak dikenal (white/cyan/viridis).', 400, ''
    if prof_fields.get('vocal_gender') and prof_fields['vocal_gender'] not in ('m', 'f'):
        return False, 'Gender vokal tidak dikenal (m/f).', 400, ''
    if prof_fields:
        _zumo_profile_set(yt_id, prof_fields)
        profile.update(prof_fields)
    try:
        # Operator Telegram mengirim `song_count`; panel memakai
        # `song_titles` — terima keduanya (alias) supaya jumlah lagu
        # dari /zumo tidak diam-diam diabaikan.
        n_titles = int(data.get('song_titles') or data.get('song_count')
                       or profile.get('song_titles') or 3)
    except Exception:
        n_titles = 3
    # Bank massal boleh 1-30 judul (contoh user: 30 lagu jazz instrumental);
    # order upload tetap 1-3 judul (3 judul -> 6 varian -> pakai 4-5).
    n_titles = max(1, min(30 if mode == 'bank' else 3, n_titles))
    titles = []
    raw_titles = data.get('titles') or []
    if isinstance(raw_titles, list):
        for t in raw_titles[:(30 if mode == 'bank' else 3)]:
            if not isinstance(t, dict):
                continue
            tt = (t.get('title') or '').strip()[:80]
            ll = (t.get('lyrics') or '').strip()
            if ll and not instrumental:
                ll = _zumo_core.zumo_normalize_lyrics(ll)
            if instrumental:
                ll = ''
            if tt or ll:
                titles.append({'title': tt, 'lyrics': ll})
    if mode == 'assemble':
        # Rakit butuh bank berisi — ditolak jujur bila kosong.
        if not _zumo_core.zumo_bank_list(ZUMO_BANK_DIR, channel.get('name', '')):
            return False, 'Bank audio channel ini masih kosong — generate ke bank dulu (/zumobank).', 400, ''
    pub = (data.get('publish_date') or '').strip()
    if pub:
        try:
            pub_dt = datetime.strptime(pub, '%Y-%m-%dT%H:%M')
            pub = pub_dt.strftime('%Y-%m-%d %H:%M')
        except Exception:
            try:
                pub_dt = datetime.strptime(pub, '%Y-%m-%d %H:%M')
            except Exception:
                return False, 'Format tanggal salah (pakai YYYY-MM-DD HH:MM).', 400, ''
    else:
        # Default (SAMA seperti Playlist): besok 19:00 WIB. Tanpa ini,
        # _zumo_create_task menolak publish_date kosong (400) dan SEMUA
        # order tanpa jadwal eksplisit gagal di akhir build (ditemukan
        # harness D6 — order assemble tanpa jadwal).
        pub = (_wib_now() + timedelta(days=1)).strftime('%Y-%m-%d') + ' 19:00'
    job_id = f"zm{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    _zumo_job_add({
        'job_id': job_id, 'yt_id': yt_id,
        'channel_name': channel.get('name', ''), 'theme': theme,
        'style': profile.get('style', ''),
        'lyrics_language': profile.get('lyrics_language', 'en'),
        'vocal_gender': profile.get('vocal_gender', 'f'),
        'model': profile.get('model', 'V5.5'),
        'particle': profile.get('particle', 'snow'),
        'spectrum_color': profile.get('spectrum_color', 'white'),
        'song_titles': n_titles, 'titles': titles,
        'mode': mode, 'instrumental': instrumental, 'bank_count': bank_count,
        'publish_date': pub, 'privacy': (data.get('privacy') or 'public').strip() or 'public',
        'status': 'queued', 'created_at': _wib_now().strftime('%Y-%m-%d %H:%M'),
        'progress': 'Job dibuat — menunggu build thread.',
    })
    threading.Thread(target=_zumo_build_thread, args=(job_id,), daemon=True).start()
    _msg = {'upload': 'Job Zumo dibuat — generate lirik + musik dimulai.',
            'bank': f'Job Zumo dibuat — generate {n_titles} lagu ke bank dimulai (tanpa upload).',
            'assemble': f'Job Zumo dibuat — rakit video dari {bank_count} lagu bank dimulai.'}[mode]
    return True, _msg, 200, job_id


def _zumo_finish_video(job_id, job, job_dir, kept, profile, total_credits,
                       model, model_used, model_fallback, failed_titles):
    """Ekor bersama jalur generate DAN jalur rakit-bank: EQ PIANO per lagu
    + gabung (MASTERING WAJIB — selalu lewat zumo_build_audio, tidak ada
    jalur yang melewatkannya) -> thumbnail -> buat task upload
    (kind='zumo_playlist') -> preview approval -> notify. `kept` =
    [{title, path, duration, bpm?, bpm_conf?, bpm_how?}]."""
    yt_id = job.get('yt_id')
    theme = job.get('theme') or ''
    style = profile.get('style') or ''
    audio_final = os.path.join(job_dir, 'audio_final.mp3')
    _zumo_core.zumo_build_audio(
        [k['path'] for k in kept], audio_final,
        ffmpeg_path=get_ffmpeg_path())
    audio_dur = _playlist_media_duration(audio_final)
    if audio_dur <= 0:
        raise RuntimeError('Durasi audio gabungan tidak terbaca.')
    # BPM per lagu (untuk denyut partikel) + offset timestamp.
    offsets = []
    acc = 0.0
    for i, k in enumerate(kept):
        offsets.append(round(acc, 2))
        acc += float(k['duration']) - (_zumo_core.ZUMO_CROSSFADE_SEC if i < len(kept) - 1 else 0.0)
    segments = []
    track_rows = []
    for i, k in enumerate(kept):
        bpm = k.get('bpm')
        if bpm:
            # BPM sudah dideteksi saat download / tersimpan di bank.
            conf, how = float(k.get('bpm_conf') or 0.0), k.get('bpm_how') or 'stored'
        else:
            bpm, conf, how = _zumo_core.zumo_detect_bpm(
                k['path'], ffmpeg_path=get_ffmpeg_path())
        if how == 'fallback-60':
            # Deteksi lemah -> seed dari BPM di teks style ('... 82 BPM').
            hint = _zumo_core.zumo_bpm_hint_from_style(style)
            if hint:
                bpm, conf, how = hint, 0.5, 'style-hint'
        segments.append({'t0': offsets[i],
                         't1': round(offsets[i] + float(k['duration']), 2),
                         'bpm': bpm, 'bpm_conf': conf, 'bpm_how': how})
        track_rows.append({'title': k['title'], 'duration': float(k['duration']),
                           'offset': offsets[i], 'bpm': bpm})
    channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    ch_name = (channel or {}).get('name', yt_id)
    track_titles = [k['title'] for k in kept]
    _raw_title = _zumo_rewrite_title(track_titles, theme, ch_name)
    new_title = _clean_upload_text(_raw_title, max_len=100,
                                   fallback=f"Zumo {_wib_now().strftime('%Y-%m-%d')}",
                                   strip_channel=ch_name)
    new_desc = _zumo_build_description(theme, track_rows, ch_name, new_title)
    new_tags = _zumo_build_tags(style, track_titles, ch_name)
    _title_cleaned = (new_title != _raw_title)
    bg = _zumo_background_path(yt_id)
    thumb_path = ''
    try:
        thumb_path = _zumo_make_thumbnail(
            bg, new_title, ch_name, os.path.join(job_dir, 'thumb.jpg'),
            badge='ZUMO')
    except Exception as e:
        raise RuntimeError(f'Thumbnail Zumo gagal dibuat: {e}')
    if bg:
        loop = _zumo_ensure_particle_loop(profile.get('particle') or 'snow')
        _zumo_job_update(job_id, status='preparing',
                         progress='Membuat task upload...',
                         new_title=new_title, new_description=new_desc,
                         new_tags=new_tags, thumb_path=thumb_path,
                         title_cleaned=_title_cleaned,
                         audio_path=audio_final, audio_duration=audio_dur,
                         tracks=track_rows, segments=segments)
        res = _zumo_create_task({
            'yt_id': yt_id, 'title': new_title, 'description': new_desc,
            'tags': new_tags, 'publish_date': job.get('publish_date') or '',
            'privacy': job.get('privacy') or 'public',
            'audio_path': audio_final, 'bg_image': bg,
            'particle_loop': loop or '',
            'spectrum_color': profile.get('spectrum_color') or 'white',
            'segments': segments, 'tracks': track_rows,
            'credits': total_credits, 'thumb_path': thumb_path,
            'job_dir': job_dir, 'job_id': job_id,
            'audio_duration': audio_dur, 'title_cleaned': _title_cleaned})
    else:
        # Fallback: gambar belum ada -> footage Pixabay (jalur playlist).
        _zumo_job_update(job_id, status='preparing',
                         progress='Gambar latar belum ada — fallback footage Pixabay...',
                         new_title=new_title, new_description=new_desc,
                         new_tags=new_tags, thumb_path=thumb_path,
                         title_cleaned=_title_cleaned,
                         audio_path=audio_final, audio_duration=audio_dur,
                         tracks=track_rows, segments=segments)
        clips, masks = _pixabay_search_clips(theme, count=PLAYLIST_MAX_CLIPS, job_id=job_id)
        clip_paths = []
        for i, c in enumerate(clips):
            dest = os.path.join(job_dir, f"clip_{i + 1:02d}.mp4")
            try:
                _pixabay_download_clip(c['url'], dest)
            except Exception as e:
                # 🔥 v1.3.75: klip bermasalah dilewati saja (lihat jalur playlist).
                print(f"[pixabay] klip {i + 1} dilewati: {e}", flush=True)
                try:
                    os.remove(dest)
                except Exception:
                    pass
                continue
            clip_paths.append(dest)
        if len(clip_paths) < PLAYLIST_MIN_CLIPS:
            raise RuntimeError(f"Footage Pixabay valid kurang (dapat {len(clip_paths)}, butuh min {PLAYLIST_MIN_CLIPS}) — coba tema/kata kunci lain.")
        res = _playlist_create_task({
            'yt_id': yt_id, 'title': new_title, 'description': new_desc,
            'tags': new_tags, 'publish_date': job.get('publish_date') or '',
            'privacy': job.get('privacy') or 'public',
            'audio_path': audio_final, 'clip_paths': clip_paths,
            'thumb_path': thumb_path, 'job_dir': job_dir, 'job_id': job_id,
            'ref_video_id': '', 'ref_title': theme,
            'audio_duration': audio_dur, 'title_cleaned': _title_cleaned})
        _zumo_job_update(job_id, render_method='pixabay-fallback')
    if not res.get('ok'):
        raise RuntimeError(res.get('message') or 'Gagal membuat task.')
    if res.get('preview'):
        threading.Thread(target=_send_approval_previews,
                         args=(res['batch_id'], res['channel_name'], [res['preview']]),
                         daemon=True).start()
    _prog_done = 'Task dibuat — menunggu approval/antrean.'
    if not bg:
        _prog_done += ' (Visual: fallback footage Pixabay — gambar latar belum ada.)'
    if failed_titles:
        _prog_done += f" (Judul gagal: {', '.join(failed_titles)}.)"
    if model_fallback:
        _prog_done += f" (Model {model} ditolak Kie — dipakai {model_used}.)"
    _zumo_job_update(job_id, status='queued', progress=_prog_done,
                     task_id=res.get('task_id'),
                     approval='approved' if res.get('auto_mode') else 'pending')
    notify_event("🎵", new_title,
                 f"Playlist Zumo siap — task dibuat untuk {ch_name} "
                 f"({len(kept)} lagu, {int(audio_dur // 60)} menit, "
                 f"kredit Kie {total_credits}).")


def _zumo_build_assemble(job_id, job, job_dir):
    """Rakit video dari BANK (mode 'assemble'): pilih acak N lagu dari
    bank channel -> EQ PIANO per lagu saat perakitan (mastering wajib;
    file bank disimpan mentah) -> _zumo_finish_video (thumbnail + task
    kind='zumo_playlist'). Tanpa panggil Kie sama sekali (kredit 0).
    Dipanggil dari _zumo_build_thread di dalam try-nya."""
    yt_id = job.get('yt_id')
    ch_name = job.get('channel_name') or yt_id
    try:
        n = max(1, min(30, int(job.get('bank_count') or 8)))
    except Exception:
        n = 8
    _zumo_job_update(job_id, status='preparing',
                     progress=f"Memilih {n} lagu acak dari bank {ch_name}...")
    entries = _zumo_core.zumo_bank_pick(ZUMO_BANK_DIR, ch_name, n)
    if not entries:
        raise RuntimeError('Bank audio channel ini masih kosong — generate ke bank dulu (/zumobank).')
    folder = os.path.dirname(_zumo_core.zumo_bank_index_path(ZUMO_BANK_DIR, ch_name))
    kept = []
    for e in entries:
        p = os.path.join(folder, e.get('filename') or '')
        d = _playlist_media_duration(p) if os.path.isfile(p) else 0
        if d <= 0:
            continue
        bpm_e = e.get('bpm')
        kept.append({'title': e.get('title') or 'Tanpa Judul', 'path': p,
                     'duration': d,
                     'bpm': bpm_e,
                     'bpm_conf': 0.5 if bpm_e else 0.0,
                     'bpm_how': 'bank' if bpm_e else 'fallback-60'})
    if not kept:
        raise RuntimeError('Lagu bank tidak bisa dibaca (file hilang/rusak).')
    profile = _zumo_profile_get(yt_id)
    for k in ('style', 'lyrics_language', 'vocal_gender', 'model',
              'particle', 'spectrum_color'):
        if job.get(k):
            profile[k] = job[k]
    model = profile.get('model') or 'V5.5'
    _zumo_job_update(job_id, status='preparing',
                     progress=(f"{len(kept)} lagu dari bank terpilih"
                               + (f" (minta {n}, bank berisi {len(entries)})." if len(kept) < n else ".")
                               + " Gabung audio (EQ PIANO per lagu)..."),
                     kie_credits=0, model_used=model, model_fallback=False,
                     failed_titles=[])
    _zumo_finish_video(job_id, job, job_dir, kept, profile,
                       0, model, model, False, [])


def _zumo_build_thread(job_id):
    """Latar: lirik (tempel/generate) -> musik Kie -> download -> bank ->
    shuffle -> EQ+gabung -> thumbnail -> buat task. Gagal di langkah
    mana pun -> job 'failed' + file dipertahankan untuk debug (lokasi
    ada di job). Mode 'assemble' langsung ke _zumo_build_assemble;
    mode 'bank' berhenti setelah semua lagu masuk bank (tanpa upload)."""
    job = _zumo_job_get(job_id)
    if not job:
        return
    yt_id = job.get('yt_id')
    job_dir = os.path.join(ZUMO_TMP_DIR, job_id)
    os.makedirs(job_dir, exist_ok=True)
    try:
        if not _ZUMO_CORE_OK:
            raise RuntimeError('zumo_core.py belum ter-deploy di VPS.')
        mode = (job.get('mode') or 'upload').strip().lower()
        if mode == 'assemble':
            _zumo_build_assemble(job_id, job, job_dir)
            return
        instrumental = bool(job.get('instrumental'))
        keys = _kie_active_keys()
        if not keys:
            raise RuntimeError('Belum ada API key Kie — tambah di Settings atau /kiekey.')
        channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        ch_name = (channel or {}).get('name', yt_id)
        profile = _zumo_profile_get(yt_id)
        for k in ('style', 'lyrics_language', 'vocal_gender', 'model',
                  'particle', 'spectrum_color'):
            if job.get(k):
                profile[k] = job[k]
        model = profile.get('model') or 'V5.5'
        style = profile.get('style') or ''
        theme = job.get('theme') or ''
        n_titles = int(job.get('song_titles') or 3)
        titles_in = list(job.get('titles') or [])
        duration_target = int(profile.get('duration_max') or 360)
        total_credits = 0
        model_fallback = False
        model_used = model
        variants = []  # {title, path, duration}
        failed_titles = []
        for idx in range(n_titles):
            slot = titles_in[idx] if idx < len(titles_in) else {}
            title_i = (slot.get('title') or '').strip()
            lyrics_i = (slot.get('lyrics') or '').strip()
            _zumo_job_update(job_id, status='generating',
                             progress=f"Judul {idx + 1}/{n_titles}: menyiapkan lirik...")
            lyrics_from_kie = False
            if not lyrics_i and not instrumental:
                # Jalur B: generate lirik format Suno (tag [Bagian: arahan]).
                # Instrumental: lirik DILEWATI total (tanpa lyrics endpoint).
                try:
                    ltask, lidx = _zumo_core.kie_call_with_failover(
                        keys,
                        lambda k: _zumo_core.kie_create_lyrics(
                            k, _zumo_core.zumo_lyrics_prompt(theme, profile)),
                        disable_cb=_kie_disable_key)
                    lrec = _zumo_poll_retry(keys[lidx], ltask)
                    total_credits += int((lrec.get('creditsConsumed') or 0) or 0)
                    opts = _zumo_core.parse_kie_lyrics(lrec)
                    if opts:
                        lyrics_i = _zumo_core.zumo_normalize_lyrics(opts[0]['text'])
                        lyrics_from_kie = True
                        if not title_i and opts[0].get('title'):
                            title_i = opts[0]['title'][:80]
                except Exception as e:
                    print(f"[zumo] lyrics jalur B gagal ({e}) — fallback jalur A.", flush=True)
            if not title_i:
                title_i = f"{theme} {idx + 1}"[:80]
            # Generate musik (custom bila ada lirik, non-custom bila tidak,
            # instrumental=true bila order instrumental).
            _zumo_job_update(job_id, status='generating',
                             progress=f"Judul {idx + 1}/{n_titles}: generate musik '{title_i[:30]}'...")
            got = []
            try:
                mtask, mkidx, mused, mfb = _zumo_gen_music(
                    keys, model, title_i,
                    lyrics_i if lyrics_i else None,
                    style, duration_target, profile.get('vocal_gender') or 'f',
                    instrumental=instrumental)
                if mfb:
                    model_fallback = True
                    model_used = mused
                mrec = _zumo_poll_retry(keys[mkidx], mtask)
                m_credits = int((mrec.get('creditsConsumed') or 0) or 0)
                total_credits += m_credits
                tracks = _zumo_core.parse_kie_tracks(mrec)
                if not tracks:
                    raise RuntimeError('Kie tidak mengembalikan audio (bentuk hasil tidak dikenali).')
                for vi, tr in enumerate(tracks[:2]):
                    dest = os.path.join(job_dir, f"t{idx + 1}_v{vi + 1}.mp3")
                    _zumo_core.kie_download(tr['audio_url'], dest)
                    d = _playlist_media_duration(dest)
                    if d > 0:
                        # BPM dideteksi SEKALI di sini (dipakai ulang di
                        # segmen + metadata bank); lagu mentah masuk bank.
                        bpm_v, conf_v, how_v = _zumo_core.zumo_detect_bpm(
                            dest, ffmpeg_path=get_ffmpeg_path())
                        got.append({'title': title_i, 'path': dest, 'duration': d,
                                    'bpm': bpm_v, 'bpm_conf': conf_v, 'bpm_how': how_v})
                        _zumo_bank_save_track(yt_id, ch_name, dest, {
                            'title': title_i, 'style': style, 'bpm': bpm_v,
                            'duration': d, 'credits': m_credits,
                            'job_id': job_id, 'instrumental': instrumental,
                            'model': model_used})
                if not got:
                    raise RuntimeError('Audio hasil generate tidak bisa diunduh/dibaca.')
            except Exception as e:
                # Retry 1x per judul (task gagal Kie tidak dipotong kredit).
                try:
                    mtask, mkidx, mused, mfb = _zumo_gen_music(
                        keys, model, title_i,
                        lyrics_i if lyrics_i else None,
                        style, duration_target, profile.get('vocal_gender') or 'f',
                        instrumental=instrumental)
                    if mfb:
                        model_fallback = True
                        model_used = mused
                    mrec = _zumo_poll_retry(keys[mkidx], mtask)
                    m_credits = int((mrec.get('creditsConsumed') or 0) or 0)
                    total_credits += m_credits
                    tracks = _zumo_core.parse_kie_tracks(mrec)
                    for vi, tr in enumerate(tracks[:2]):
                        dest = os.path.join(job_dir, f"t{idx + 1}_v{vi + 1}_r.mp3")
                        _zumo_core.kie_download(tr['audio_url'], dest)
                        d = _playlist_media_duration(dest)
                        if d > 0:
                            bpm_v, conf_v, how_v = _zumo_core.zumo_detect_bpm(
                                dest, ffmpeg_path=get_ffmpeg_path())
                            got.append({'title': title_i, 'path': dest, 'duration': d,
                                        'bpm': bpm_v, 'bpm_conf': conf_v, 'bpm_how': how_v})
                            _zumo_bank_save_track(yt_id, ch_name, dest, {
                                'title': title_i, 'style': style, 'bpm': bpm_v,
                                'duration': d, 'credits': m_credits,
                                'job_id': job_id, 'instrumental': instrumental,
                                'model': model_used})
                    if not got:
                        raise RuntimeError('Retry: audio hasil generate tidak bisa diunduh/dibaca.')
                except Exception as e2:
                    failed_titles.append(title_i)
                    print(f"[zumo] judul '{title_i}' gagal setelah retry: {e2}", flush=True)
                    continue
            variants.extend(got)
        min_needed = 1 if mode == 'bank' else 2
        if len(variants) < min_needed:
            raise RuntimeError(
                f"Varian lagu terlalu sedikit ({len(variants)}) — gagal: {', '.join(failed_titles) or 'generate Kie gagal'}.")
        if mode == 'bank':
            # Generate massal selesai: lagu sudah di bank (mentah, belum
            # EQ) — TANPA upload. job_dir dibersihkan (bank punya salinan).
            _zumo_job_update(job_id, status='banked',
                             progress=(f"{len(variants)} lagu masuk bank {ch_name} "
                                       "(mentah, belum EQ) — siap dirakit (/zumorakit)."),
                             kie_credits=total_credits, model_used=model_used,
                             model_fallback=model_fallback,
                             failed_titles=failed_titles)
            notify_event("🎵", f"Bank Zumo — {theme}",
                         f"{len(variants)} lagu masuk bank {ch_name} "
                         f"(kredit Kie {total_credits}). Rakit video dengan /zumorakit.")
            shutil.rmtree(job_dir, ignore_errors=True)
            return
        keep_n = int(profile.get('songs_keep') or 5)
        kept = _zumo_core.shuffle_zumo_tracks(variants, keep=keep_n)
        _zumo_job_update(job_id, status='preparing',
                         progress=f"{len(kept)} lagu terpilih (dari {len(variants)} varian) — gabung audio...",
                         kie_credits=total_credits, model_used=model_used,
                         model_fallback=model_fallback,
                         failed_titles=failed_titles)
        _zumo_finish_video(job_id, job, job_dir, kept, profile,
                           total_credits, model, model_used, model_fallback,
                           failed_titles)
    except Exception as e:
        _zumo_job_update(job_id, status='failed', error=str(e)[:300])
        notify_event("❌", job.get('theme') or 'Playlist Zumo',
                     f"Gagal membangun playlist Zumo: {str(e)[:150]}")


# --- Inti riset & buat playlist (dipakai endpoint panel DAN operator
# Telegram — SATU logika, tanpa duplikasi; cache riset 1 jam yang sama). ---
def _playlist_duration_sec_param(v):
    """Parameter durasi (detik) dari endpoint: int >= 0 atau None
    (tidak valid / tidak diisi = tanpa filter)."""
    try:
        n = int(v)
    except Exception:
        return None
    if n < 0:
        return None
    return min(n, 24 * 3600)


def _playlist_duration_range_text(mn, mx):
    """Teks rentang durasi untuk pesan ('40–70 menit', 'minimal 40 menit',
    'maksimal 70 menit', atau '' bila tanpa filter)."""
    if mn is None and mx is None:
        return ''
    if mn is not None and mx is not None:
        return f"{mn // 60}–{mx // 60} menit"
    if mn is not None:
        return f"minimal {mn // 60} menit"
    return f"maksimal {mx // 60} menit"


def _playlist_duration_text(sec):
    """'77 menit' / '1 jam 17 mnt' untuk satu durasi (detik)."""
    try:
        s = int(sec or 0)
    except Exception:
        s = 0
    if s >= 3600:
        m = (s % 3600) // 60
        return f"{s // 3600} jam {m} mnt" if m else f"{s // 3600} jam"
    return f"{max(s // 60, 1)} mnt"


def _playlist_closest(all_cands, mn, mx, n=3):
    """Kandidat TERDEKAT dari rentang durasi yang diminta — untuk ditawarkan
    saat filter durasi mengosongkan hasil (v1.3.13: bot tidak boleh mati
    begitu saja padahal ada video yang hampir cocok). Urut: jarak dari
    rentang dulu, views tertinggi dulu. all_cands sudah urut views."""
    def _dist(c):
        d = c.get('duration_sec') or 0
        if mn is not None and d < mn:
            return mn - d
        if mx is not None and d > mx:
            return d - mx
        return 0
    out = []
    for c in sorted(all_cands, key=lambda c: (_dist(c), -(c.get('views') or 0)))[:n]:
        dur = c.get('duration_sec') or 0
        out.append({'video_id': c.get('video_id'), 'title': c.get('title', ''),
                    'duration_sec': dur,
                    'duration_text': _playlist_duration_text(dur),
                    'views': c.get('views') or 0,
                    'channel': c.get('channel', '')})
    return out


def _playlist_do_research(yt_id, theme, min_duration_sec=None, max_duration_sec=None):
    """Riset kandidat trending 30 hari terakhir. Kembalikan
    (ok, payload, http); payload berisi job_id + candidates + quota_note
    bila sukses. Job riset dicatat di playlist_jobs.json (status
    'researched') supaya langkah berikutnya bisa memakai job_id-nya.
    min/max_duration_sec (detik, opsional): kandidat di luar rentang
    dibuang — tidak pernah dikembalikan diam-diam. Filter diterapkan
    SETELAH cache riset, jadi cache 1 jam tetap mentah dan dipakai
    bersama untuk rentang durasi apa pun (0 kuota tambahan)."""
    if not _valid_yt_id(yt_id):
        return False, {"status": "error", "message": "Channel tidak valid."}, 400
    channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if not channel:
        return False, {"status": "error", "message": "Channel tidak ditemukan."}, 404
    q = (theme or '').strip()
    if not q:
        return False, {"status": "error", "message": "Isi tema/kata kunci dulu."}, 400
    mn = _playlist_duration_sec_param(min_duration_sec)
    mx = _playlist_duration_sec_param(max_duration_sec)
    if mn is not None and mx is not None and mn > mx:
        mn, mx = mx, mn
    cands, err, from_cache, tier_no, tier_label = _playlist_research_youtube(channel, q)
    if err:
        return False, {"status": "error", "message": err}, 500
    all_cands = cands or []
    dur_txt = _playlist_duration_range_text(mn, mx)
    filter_note = ''
    closest = []
    # v1.3.13: referensi playlist minimal 20 menit — klip 5 menit bukan
    # playlist (pernah: 'lewati' lalu bot menawarkan video 5 menit).
    PLAYLIST_MIN_REF_SEC = 20 * 60
    if mn is not None or mx is not None:
        # duration_sec 0 = durasi tak diketahui → tidak lolos filter aktif.
        cands = [c for c in all_cands
                 if (c.get('duration_sec') or 0) > 0
                 and (mn is None or c['duration_sec'] >= mn)
                 and (mx is None or c['duration_sec'] <= mx)]
        if not cands and all_cands:
            filter_note = (f"Tidak ada video {dur_txt} pada tier {tier_label or 'yang dipakai'} untuk tema ini — "
                           "coba ubah tema atau rentang durasi, atau kirim link YouTube dengan 'jiplak playlist ini'.")
            closest = _playlist_closest(all_cands, mn, mx)
    else:
        cands = [c for c in all_cands
                 if (c.get('duration_sec') or 0) >= PLAYLIST_MIN_REF_SEC]
        if not cands and all_cands:
            filter_note = ("Semua kandidat untuk tema ini di bawah 20 menit — playlist butuh video panjang. "
                           "Coba tema lain, atau kirim link YouTube dengan 'jiplak playlist ini'.")
    if not all_cands:
        # Semua tier sudah dicoba (sampai evergreen >=1rb) dan tetap
        # kosong — gagal jujur, bukan karang video. Arahkan ke jiplak link.
        filter_note = ("Tidak ada video sama sekali untuk tema ini — sudah dicari "
                       "sampai semua umur video (evergreen). "
                       "Coba tema lain, atau kirim link YouTube-nya dengan 'jiplak playlist ini'.")
    elif tier_no and tier_no > 1 and not filter_note:
        filter_note = (f"Tidak ada video terbit ≤30 hari, dipakai tier {tier_no} "
                       f"({tier_label}) — {len(all_cands)} kandidat ditemukan.")
    job_id = f"pl{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    _playlist_job_add({
        'job_id': job_id, 'yt_id': yt_id,
        'channel_name': channel.get('name', ''), 'theme': q,
        'status': 'researched', 'candidates': cands or [],
        'closest': closest,
        'research_tier': tier_no, 'tier_label': tier_label,
        'min_duration_sec': mn, 'max_duration_sec': mx,
        'created_at': _wib_now().strftime('%Y-%m-%d %H:%M'),
        'progress': (filter_note if filter_note else
                     f"{len(cands or [])} kandidat ditemukan"
                     + (f" (durasi {dur_txt})" if dur_txt else "")
                     + (" (dari cache — tanpa kuota)." if from_cache else " (kuota riset ~101 unit).")),
    })
    return True, {"status": "success", "job_id": job_id, "candidates": cands or [],
                  "min_duration_sec": mn, "max_duration_sec": mx,
                  "filter_note": filter_note, "closest": closest,
                  "research_tier": tier_no, "tier_label": tier_label,
                  "quota_note": ("Riset dari cache (0 unit kuota)." if from_cache
                                 else "Riset memakai ~101 unit kuota per tier yang dicoba (tier berikutnya hanya bila tier sebelumnya kosong).")}, 200


def _playlist_find_duplicate(jobs, job_id, yt_id, video_id, now_wib, window_min=120,
                             ref_title=None, done_window_min=1440):
    """Cari job LAIN untuk channel + referensi yang sama dalam `window_min`
    menit terakhir. Mencegah order dobel (pernah: jembatan + konfirmasi bot
    membuat 2 batch Malaysia yang sama). Kembalikan job dict yang cocok,
    atau None. Job yang sudah gagal / tanpa task & tidak live tidak dihitung.
    🔥 v1.3.31: job SELESAI video_id-nya dibersihkan (None) sehingga
    referensi yang sudah tayang lolos dari cek ini — hrry9gea9PI dipakai
    3x dalam sehari (05:35, 09:21, 10:15). Job 'done' kini dicocokkan
    lewat ref_title dalam `done_window_min` menit (24 jam)."""
    if not yt_id or (not video_id and not ref_title):
        return None
    for j in jobs or []:
        if j.get('job_id') == job_id:
            continue
        if j.get('yt_id') != yt_id:
            continue
        _jvid = j.get('video_id')
        _same_live = bool(_jvid and _jvid == video_id)
        _same_done = (not _jvid and ref_title and j.get('ref_title') == ref_title
                      and j.get('status') == 'done')
        if not (_same_live or _same_done):
            continue
        if not _same_done and j.get('task_id') is None and j.get('status') not in ('downloading', 'preparing', 'queued'):
            continue
        ts_raw = j.get('updated_at') or j.get('created_at') or ''
        try:
            ts = datetime.strptime(str(ts_raw)[:16], '%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Asia/Jakarta'))
        except Exception:
            continue
        _win = done_window_min if _same_done else window_min
        if (now_wib - ts).total_seconds() <= _win * 60:
            return j
    return None


def _playlist_do_create(job_id, video_id, publish_date='', privacy='public',
                        pixabay_theme='', playlist_id=''):
    """Pilih kandidat → bangun playlist di thread latar. Kembalikan
    (ok, message, http). pixabay_theme: tema footage Pixabay (opsional,
    '' = auto dari tema musik) — disimpan di job, dipakai build thread."""
    job = _playlist_job_get(job_id)
    if not job:
        return False, "Job riset tidak ditemukan — riset ulang.", 404
    if not re.fullmatch(r'[A-Za-z0-9_-]{11}', video_id or ''):
        return False, "ID video referensi tidak valid.", 400
    cand = next((c for c in job.get('candidates') or [] if c.get('video_id') == video_id), None)
    if not cand:
        return False, "Kandidat tidak ditemukan — riset ulang.", 404
    if job.get('status') in ('downloading', 'preparing', 'queued'):
        return False, "Job ini sedang berjalan.", 409
    # 🔥 v1.3.6: cegah order dobel — referensi yang sama untuk channel yang
    # sama dalam 2 jam terakhir (pernah dibuat 2x: jembatan + konfirmasi bot).
    _dup = _playlist_find_duplicate(_playlist_jobs_load().get('jobs', []),
                                    job_id, job.get('yt_id'), video_id, _wib_now(),
                                    ref_title=cand.get('title'))
    if _dup:
        if _dup.get('status') == 'done':
            return False, ("Video referensi ini sudah dipakai channel ini dalam 24 jam terakhir — "
                           "pilih video lain supaya tidak dobel konten."), 409
        return False, ("Playlist referensi ini sudah dibuat untuk channel ini "
                       "dalam 2 jam terakhir — tidak dibuat dobel."), 409
    pub = (publish_date or '').strip()
    if pub:
        try:
            pub_dt = datetime.strptime(pub, '%Y-%m-%dT%H:%M')
            pub = pub_dt.strftime('%Y-%m-%d %H:%M')
        except Exception:
            try:
                pub_dt = datetime.strptime(pub, '%Y-%m-%d %H:%M')
                pub = pub_dt.strftime('%Y-%m-%d %H:%M')
            except Exception:
                return False, "Format tanggal salah (pakai YYYY-MM-DD HH:MM).", 400
    else:
        # Default: besok 19:00 WIB.
        pub = (_wib_now() + timedelta(days=1)).strftime('%Y-%m-%d') + ' 19:00'
    privacy = (privacy or 'public').strip()
    if privacy not in ('public', 'private', 'unlisted'):
        # 🔥 v1.3.33 (audit M10): jangan diam-diam jadi public —
        # video yang dimaksud privat bisa tayang publik.
        return False, "privacy tidak valid (pakai public/private/unlisted).", 400
    _playlist_job_update(job_id, status='downloading', video_id=video_id,
                         ref_title=cand.get('title', ''),
                         ref_description=cand.get('description', ''),
                         ref_tags=cand.get('tags') or [],
                         ref_thumbnail_url=cand.get('thumbnail_url', ''),
                         ref_channel=cand.get('channel', ''),
                         publish_date=pub, privacy=privacy,
                         playlist_id=(playlist_id or '').strip(),
                         pixabay_theme=(pixabay_theme or '').strip()[:100],
                         progress='Memulai...')
    threading.Thread(target=_playlist_build_thread, args=(job_id,), daemon=True).start()
    return True, "Membangun playlist — audio & footage sedang diunduh. Pantau progres di daftar job.", 200




def _playlist_from_youtube_url(channel, url):
    """Mode 'jiplak playlist' (v1.3.5): ambil daftar lagu dari link YouTube
    yang dikirim Bos — TANPA riset dan TANPA filter views/tanggal/durasi.
    Link yang dilampirkan = sumbernya. Bila link berisi playlist (list=),
    ambil video-video playlist itu berurutan (maks 50, 2 halaman x 25);
    bila hanya video, ambil video itu saja. Urutan playlist dipertahankan
    (field 'playlist_position'). Kembalikan (candidates, err, source_info);
    source_info = {'kind': 'playlist'|'video', 'title': ..., 'count': ...}."""
    video_id, playlist_id = _extract_youtube_ids_from_url(url)
    if not video_id and not playlist_id:
        return None, 'Link YouTube tidak dikenali — kirim link video/playlist YouTube.', None
    ids = []
    title = None
    kind = 'video'
    if playlist_id:
        kind = 'playlist'

        def _items(youtube):
            out = []
            page = None
            for _ in range(2):
                if page:
                    resp = youtube.playlistItems().list(
                        part='contentDetails', playlistId=playlist_id,
                        maxResults=25, pageToken=page).execute()
                else:
                    resp = youtube.playlistItems().list(
                        part='contentDetails', playlistId=playlist_id,
                        maxResults=25).execute()
                for it in resp.get('items', []):
                    vid = ((it.get('contentDetails') or {}).get('videoId')
                           or (it.get('snippet') or {}).get('resourceId', {}).get('videoId'))
                    if vid:
                        out.append(vid)
                page = resp.get('nextPageToken')
                if not page:
                    break
            return out

        ids, err, quota, _sw, _up, tried = _playlist_try_creds(channel, _items)
        if err:
            if quota:
                hint = _oauth_quota_failure_hint(channel, channel.get('name', ''))
                pids = ', '.join(p for p in tried if p)
                return None, (f'Riset gagal: kuota pencarian project {pids} habis '
                              f'hari ini.{hint}'), None
            return None, f'Gagal membaca playlist: {err}', None
        if not ids:
            return None, 'Playlist YouTube itu kosong / tidak bisa dibaca (privat?).', None

        def _pl_title(youtube):
            return youtube.playlists().list(part='snippet', id=playlist_id).execute()

        _tres, _terr, _tq, _tsw, _tup, _ttried = _playlist_try_creds(channel, _pl_title)
        if _tres:
            _items2 = _tres.get('items') or []
            if _items2:
                title = ((_items2[0].get('snippet') or {}).get('title') or '').strip() or None
    else:
        ids = [video_id]

    def _details(youtube):
        return youtube.videos().list(
            part='statistics,snippet,contentDetails',
            id=','.join(ids[:50])).execute()

    vres, err, quota, _sw2, _up2, tried2 = _playlist_try_creds(channel, _details)
    if err:
        if quota:
            hint = _oauth_quota_failure_hint(channel, channel.get('name', ''))
            pids = ', '.join(p for p in tried2 if p)
            return None, (f'Riset gagal: kuota pencarian project {pids} habis '
                          f'hari ini.{hint}'), None
        return None, f'Gagal membaca video: {err}', None
    items = (vres or {}).get('items') or []
    if not items:
        return None, 'Video tidak ditemukan / tidak bisa diakses (privat atau dihapus?).', None
    by_id = {v.get('id'): v for v in items}
    cands = []
    for _pos, _vid in enumerate(ids):
        v = by_id.get(_vid)
        if not v:
            continue
        try:
            views = int((v.get('statistics') or {}).get('viewCount') or 0)
        except Exception:
            views = 0
        sn = v.get('snippet') or {}
        thumbs = sn.get('thumbnails') or {}
        thumb_url = ((thumbs.get('maxres') or thumbs.get('high')
                      or thumbs.get('medium') or thumbs.get('default') or {}).get('url', ''))
        cands.append({
            'video_id': v.get('id', ''), 'title': sn.get('title', ''),
            'channel': sn.get('channelTitle', ''), 'views': views,
            'published_at': sn.get('publishedAt', ''),
            'duration': (v.get('contentDetails') or {}).get('duration', ''),
            'duration_sec': _playlist_iso_duration_sec(
                (v.get('contentDetails') or {}).get('duration', '')),
            'thumbnail_url': thumb_url,
            'description': sn.get('description', '') or '',
            'tags': list(sn.get('tags') or []),
            'tier': 'link', 'tier_label': 'link kiriman Bos',
            'playlist_position': _pos,
        })
    if not cands:
        return None, 'Video tidak ditemukan / tidak bisa diakses (privat atau dihapus?).', None
    if not title:
        title = cands[0].get('title') or 'Link YouTube'
    source_info = {'kind': kind, 'title': title, 'count': len(cands),
                   'playlist_id': playlist_id, 'video_id': video_id}
    return cands, None, source_info


def _playlist_do_from_url(yt_id, url):
    """Buat job riset dari link YouTube (mode jiplak, v1.3.5). Kembalikan
    (ok, payload, http); payload berisi job_id + candidates + source info.
    Job dicatat seperti hasil riset biasa (status 'researched') supaya
    langkah berikutnya (_playlist_do_create) bisa memakai job_id-nya."""
    if not _valid_yt_id(yt_id):
        return False, {"status": "error", "message": "Channel tidak valid."}, 400
    channel = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if not channel:
        return False, {"status": "error", "message": "Channel tidak ditemukan."}, 404
    u = (url or '').strip()
    if not u:
        return False, {"status": "error", "message": "Link YouTube wajib diisi."}, 400
    cands, err, source_info = _playlist_from_youtube_url(channel, u)
    if err:
        return False, {"status": "error", "message": err}, 500
    title = (source_info or {}).get('title') or 'Link YouTube'
    job_id = f"pl{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    _playlist_job_add({
        'job_id': job_id, 'yt_id': yt_id,
        'channel_name': channel.get('name', ''), 'theme': title,
        'status': 'researched', 'candidates': cands or [],
        'research_tier': 'link', 'tier_label': 'link kiriman Bos',
        'source_url': u, 'source_kind': (source_info or {}).get('kind'),
        'source_title': title,
        'created_at': _wib_now().strftime('%Y-%m-%d %H:%M'),
        'progress': (f"Dari link Bos: {title} — {len(cands or [])} lagu, tanpa filter."),
    })
    return True, {"status": "success", "job_id": job_id, "candidates": cands or [],
                  "source_kind": (source_info or {}).get('kind'),
                  "source_title": title, "count": len(cands or []),
                  "research_tier": "link", "tier_label": "link kiriman Bos"}, 200

# --- Endpoint panel: Musik Playlist (butuh login; CSRF via check_security) ---
@app.route('/api/playlist_research', methods=['POST'])
def playlist_research():
    """Riset kandidat trending 30 hari terakhir (hemat kuota, cache 1 jam).
    Body: {yt_id, theme, keyword?, min_duration_sec?, max_duration_sec?}.
    Kembalikan kandidat + job_id."""
    data = request.get_json(silent=True) or {}
    yt_id = (data.get('yt_id') or '').strip()
    theme = (data.get('theme') or '').strip()
    keyword = (data.get('keyword') or '').strip()
    ok, payload, http = _playlist_do_research(
        yt_id, keyword or theme,
        data.get('min_duration_sec'), data.get('max_duration_sec'))
    return jsonify(payload), http


@app.route('/api/operator/playlist_research', methods=['POST'])
def operator_playlist_research():
    """Riset kandidat playlist dari Telegram — auth HMAC operator_key.
    Body: {key, yt_id, theme, min_duration_sec?, max_duration_sec?}.
    Memakai helper yang SAMA dengan endpoint panel (cache riset 1 jam
    yang sama, kuota yang sama). Route ini masuk
    _OPERATOR_ENDPOINTS di check_security."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    ok, payload, http = _playlist_do_research(
        (data.get('yt_id') or '').strip(), (data.get('theme') or '').strip(),
        data.get('min_duration_sec'), data.get('max_duration_sec'))
    return jsonify(payload), http


@app.route('/api/playlist_from_url', methods=['POST'])
def playlist_from_url():
    """Buat job playlist dari link YouTube (mode jiplak, v1.3.5).
    Body: {yt_id, url}. Tanpa riset/filter — link = sumbernya."""
    data = request.get_json(silent=True) or {}
    ok, payload, http = _playlist_do_from_url(
        (data.get('yt_id') or '').strip(), (data.get('url') or '').strip())
    return jsonify(payload), http


@app.route('/api/operator/playlist_from_url', methods=['POST'])
def operator_playlist_from_url():
    """Buat job playlist dari link YouTube via Telegram — auth HMAC operator_key.
    Body: {key, yt_id, url}. Route ini masuk _OPERATOR_ENDPOINTS di check_security."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    ok, payload, http = _playlist_do_from_url(
        (data.get('yt_id') or '').strip(), (data.get('url') or '').strip())
    return jsonify(payload), http


@app.route('/api/playlist_create', methods=['POST'])
def playlist_create():
    """Pilih kandidat → bangun playlist di thread latar. Body: {job_id,
    video_id, publish_date?, privacy?, playlist_id?, pixabay_theme?}."""
    data = request.get_json(silent=True) or {}
    ok, message, http = _playlist_do_create(
        (data.get('job_id') or '').strip(), (data.get('video_id') or '').strip(),
        data.get('publish_date') or '', data.get('privacy') or 'public',
        (data.get('pixabay_theme') or '').strip(),
        (data.get('playlist_id') or '').strip())
    return jsonify({"status": "success" if ok else "error", "message": message}), http


@app.route('/api/operator/playlist_create', methods=['POST'])
def operator_playlist_create():
    """Buat playlist dari Telegram — auth HMAC operator_key.
    Body: {key, job_id, video_id, publish_date?, privacy?, pixabay_theme?}.
    Memakai helper yang SAMA dengan endpoint panel (thread build, gates
    worker, approval/autopilot yang sama). Route ini masuk
    _OPERATOR_ENDPOINTS di check_security."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    ok, message, http = _playlist_do_create(
        (data.get('job_id') or '').strip(), (data.get('video_id') or '').strip(),
        data.get('publish_date') or '', data.get('privacy') or 'public',
        (data.get('pixabay_theme') or '').strip())
    return jsonify({"status": "success" if ok else "error", "message": message}), http


@app.route('/api/operator/publish_now', methods=['POST'])
def operator_publish_now():
    """🔥 v1.3.6: ubah task yang masih dijadwalkan (publish_date di masa depan)
    jadi tayang langsung (publish_date = sekarang). Auth HMAC operator_key.
    Body: {key, yt_ids? (list, kosong = semua channel)}.
    Task approval-pending & channel dijeda dilewati (tidak diubah).
    Upload yang rendernya sudah selesai langsung publik (tanpa publishAt);
    yang masih render tetap render dulu, lalu langsung tayang."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    yt_filter = set(data.get('yt_ids') or [])
    now_wib = _wib_now()
    now_s = now_wib.strftime('%Y-%m-%d %H:%M')
    updated = []
    skipped_approval = 0
    skipped_paused = 0
    with db_lock:
        for t in active_tasks:
            yt = _active_task_yt_id(t)
            if yt_filter and yt not in yt_filter:
                continue
            if _approval_of(t) == 'pending':
                skipped_approval += 1
                continue
            if _channel_paused(yt):
                skipped_paused += 1
                continue
            pub_raw = t.get('time') or (t.get('blueprint') or {}).get('publish_date') or ''
            try:
                pub_dt = datetime.strptime(str(pub_raw)[:16], '%Y-%m-%d %H:%M').replace(tzinfo=ZoneInfo('Asia/Jakarta'))
            except Exception:
                continue
            if pub_dt <= now_wib:
                continue
            t['time'] = now_s
            bp = t.get('blueprint') or {}
            bp['publish_date'] = now_s
            t['blueprint'] = bp
            updated.append({'task_id': t.get('id'), 'yt_id': yt})
        if updated:
            save_tasks_db()
    if updated:
        _ids = {u['task_id'] for u in updated}
        try:
            _jd = _playlist_jobs_load()
            _changed = False
            for _j in _jd.get('jobs', []):
                if _j.get('task_id') in _ids and _j.get('publish_date') != now_s:
                    _j['publish_date'] = now_s
                    _changed = True
            if _changed:
                _playlist_jobs_save(_jd)
        except Exception:
            pass
    return jsonify({"status": "success",
                    "message": ("Tayang langsung: %d video diubah." % len(updated)) if updated else "Tidak ada video yang masih dijadwalkan.",
                    "updated": updated,
                    "skipped_approval": skipped_approval,
                    "skipped_paused": skipped_paused}), 200


@app.route('/api/playlist_jobs')
def playlist_jobs():
    """Daftar job playlist (tanpa kandidat — kandidat hanya ada di
    respons riset)."""
    jobs = []
    for j in _playlist_jobs_load().get('jobs', []):
        jobs.append({k: j.get(k) for k in (
            'job_id', 'yt_id', 'channel_name', 'theme', 'status', 'progress',
            'error', 'created_at', 'updated_at', 'task_id', 'approval',
            'video_id', 'video_url', 'ref_title', 'new_title',
            'audio_duration', 'render_method', 'pixabay_keys_used',
            'pixabay_theme', 'pixabay_theme_used', 'pixabay_fallback')})
    return jsonify({"status": "success", "jobs": jobs})


# --- Endpoint panel: API key Pixabay (butuh login; CSRF via check_security) ---
@app.route('/api/playlist_pixabay_keys')
def playlist_pixabay_keys():
    return jsonify({"status": "success", "keys": _pixabay_masked_list()})


@app.route('/api/playlist_pixabay_key_add', methods=['POST'])
def playlist_pixabay_key_add():
    data = request.get_json(silent=True) or {}
    ok, masked, msg = _pixabay_add_key((data.get('key') or '').strip())
    return jsonify({"status": "success" if ok else "error",
                    "message": msg, "masked": masked}), (200 if ok else 400)


@app.route('/api/playlist_pixabay_key_del', methods=['POST'])
def playlist_pixabay_key_del():
    data = request.get_json(silent=True) or {}
    ok, masked, msg = _pixabay_delete_key(str(data.get('ident') or '').strip())
    return jsonify({"status": "success" if ok else "error",
                    "message": msg, "masked": masked}), (200 if ok else 400)


# --- Endpoint operator: API key Pixabay (auth HMAC operator_key) ---
@app.route('/api/operator/pixabay_key_add', methods=['POST'])
def operator_pixabay_key_add():
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    ok, masked, msg = _pixabay_add_key((data.get('pixabay_key') or '').strip())
    return jsonify({"status": "success" if ok else "error",
                    "message": msg, "masked": masked}), (200 if ok else 400)


@app.route('/api/operator/pixabay_key_list', methods=['POST'])
def operator_pixabay_key_list():
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    return jsonify({"status": "success", "keys": _pixabay_masked_list()})


@app.route('/api/operator/pixabay_key_del', methods=['POST'])
def operator_pixabay_key_del():
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    ok, masked, msg = _pixabay_delete_key(str(data.get('ident') or '').strip())
    return jsonify({"status": "success" if ok else "error",
                    "message": msg, "masked": masked}), (200 if ok else 400)


# --- Endpoint operator + panel: Zumo / Kie.ai (v1.3.0) ---
def _zumo_status_payload():
    """Status Zumo untuk panel/Telegram: saldo Kie + model aktif + job.
    Saldo dicek PER KEY AKTIF (penegasan user 21:15 WIB): 'kie_keys'
    membawa balance tiap key; 'balance' = saldo key aktif pertama yang
    menjawab (dicoba berurutan). Gagal baca semua -> None (jujur
    'tidak terbaca', bukan angka karangan)."""
    keys = _kie_active_keys()
    balances = _kie_balances() if _ZUMO_CORE_OK else []
    balance = None
    balance_key = None
    for b in balances:
        if not b.get('disabled') and b.get('balance') is not None:
            balance = b['balance']
            balance_key = b['masked']
            break
    profile = _zumo_profile_get(_zumo_core.ZUMO_COZY_YT_ID if _ZUMO_CORE_OK else 'UCg8be5L4_CieGjXA8ejB1_Q')
    jobs = []
    for j in _zumo_jobs_load().get('jobs', [])[:5]:
        jobs.append({k: j.get(k) for k in (
            'job_id', 'yt_id', 'channel_name', 'theme', 'status', 'progress',
            'error', 'created_at', 'updated_at', 'task_id', 'approval',
            'new_title', 'audio_duration', 'render_method', 'kie_credits',
            'model_used', 'model_fallback', 'failed_titles')})
    # Ringkasan bank audio per channel (untuk /zumo status): [{channel, count}].
    bank = []
    if _ZUMO_CORE_OK:
        try:
            if os.path.isdir(ZUMO_BANK_DIR):
                for folder in sorted(os.listdir(ZUMO_BANK_DIR)):
                    idx_path = os.path.join(ZUMO_BANK_DIR, folder, 'bank_index.json')
                    if os.path.isfile(idx_path):
                        with open(idx_path, 'r', encoding='utf-8') as fh:
                            idx = json.load(fh)
                        bank.append({'channel': idx.get('channel') or folder,
                                     'count': len(idx.get('tracks') or [])})
        except Exception:
            bank = []
    return {"status": "success", "balance": balance,
            "balance_note": (f"Saldo Kie: {balance:g} kredit (key {balance_key})."
                             if balance is not None else
                             ("Belum ada API key Kie — tambah di Settings atau /kiekey."
                              if not keys else
                              "Saldo Kie tidak terbaca (cek key / jaringan).")),
            "model": profile.get('model', 'V5.5'),
            "models": list(_zumo_core.ZUMO_MODELS) if _ZUMO_CORE_OK else ['V5.5', 'V6', 'V6_MINI', 'V6_WILD'],
            "kie_keys": balances, "jobs": jobs, "bank": bank}


@app.route('/api/operator/zumo_create', methods=['POST'])
def operator_zumo_create():
    """Buat playlist Zumo dari Telegram — auth HMAC operator_key.
    Body: {key, yt_id?, theme, style?, lyrics_language?, vocal_gender?,
    model?, particle?, spectrum_color?, song_titles? (alias: song_count),
    titles?, publish_date?, privacy?, mode? ('upload'|'bank'|'assemble'),
    instrumental?, bank_count?}. Route ini masuk _OPERATOR_ENDPOINTS."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    ok, message, http, job_id = _zumo_do_create(data)
    return jsonify({"status": "success" if ok else "error",
                    "message": message, "job_id": job_id}), http


@app.route('/api/operator/zumo_status', methods=['POST'])
def operator_zumo_status():
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    return jsonify(_zumo_status_payload())


@app.route('/api/operator/zumo_model', methods=['POST'])
def operator_zumo_model():
    """Ganti model Zumo aktif (/zumomodel). Body: {key, model}."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    model = (data.get('model') or '').strip()
    models = list(_zumo_core.ZUMO_MODELS) if _ZUMO_CORE_OK else ['V5.5', 'V6', 'V6_MINI', 'V6_WILD']
    if model not in models:
        return jsonify({"status": "error",
                        "message": "Model tidak dikenal (V5.5/V6/V6_MINI/V6_WILD)."}), 400
    cfg = load_bot_config()
    cfg['zumo_model'] = model
    _save_bot_config(cfg)
    _zumo_profile_set(_zumo_core.ZUMO_COZY_YT_ID if _ZUMO_CORE_OK else 'UCg8be5L4_CieGjXA8ejB1_Q',
                      {'model': model})
    return jsonify({"status": "success", "model": model,
                    "message": f"Model Zumo aktif: {model}."})


@app.route('/api/operator/kie_key_add', methods=['POST'])
def operator_kie_key_add():
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    ok, masked, msg = _kie_add_key((data.get('kie_key') or '').strip())
    return jsonify({"status": "success" if ok else "error",
                    "message": msg, "masked": masked}), (200 if ok else 400)


@app.route('/api/operator/kie_key_list', methods=['POST'])
def operator_kie_key_list():
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    return jsonify({"status": "success", "keys": _kie_balances()})


@app.route('/api/operator/kie_key_del', methods=['POST'])
def operator_kie_key_del():
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    ok, masked, msg = _kie_delete_key(str(data.get('ident') or '').strip())
    return jsonify({"status": "success" if ok else "error",
                    "message": msg, "masked": masked}), (200 if ok else 400)


# --- Endpoint panel: Kie keys + status Zumo (butuh login; CSRF via check_security) ---
@app.route('/api/zumo_kie_keys')
def zumo_kie_keys():
    return jsonify({"status": "success", "keys": _kie_masked_list()})


@app.route('/api/zumo_kie_key_add', methods=['POST'])
def zumo_kie_key_add():
    data = request.get_json(silent=True) or {}
    ok, masked, msg = _kie_add_key((data.get('key') or '').strip())
    return jsonify({"status": "success" if ok else "error",
                    "message": msg, "masked": masked}), (200 if ok else 400)


@app.route('/api/zumo_kie_key_del', methods=['POST'])
def zumo_kie_key_del():
    data = request.get_json(silent=True) or {}
    ok, masked, msg = _kie_delete_key(str(data.get('ident') or '').strip())
    return jsonify({"status": "success" if ok else "error",
                    "message": msg, "masked": masked}), (200 if ok else 400)


@app.route('/api/zumo_status')
def zumo_status():
    return jsonify(_zumo_status_payload())


@app.route('/api/zumo_model', methods=['POST'])
def zumo_model():
    data = request.get_json(silent=True) or {}
    model = (data.get('model') or '').strip()
    models = list(_zumo_core.ZUMO_MODELS) if _ZUMO_CORE_OK else ['V5.5', 'V6', 'V6_MINI', 'V6_WILD']
    if model not in models:
        return jsonify({"status": "error",
                        "message": "Model tidak dikenal (V5.5/V6/V6_MINI/V6_WILD)."}), 400
    cfg = load_bot_config()
    cfg['zumo_model'] = model
    _save_bot_config(cfg)
    _zumo_profile_set(_zumo_core.ZUMO_COZY_YT_ID if _ZUMO_CORE_OK else 'UCg8be5L4_CieGjXA8ejB1_Q',
                      {'model': model})
    return jsonify({"status": "success", "model": model,
                    "message": f"Model Zumo aktif: {model}."})


@app.route('/api/operator/create_shorts', methods=['POST'])
def operator_create_shorts():
    """Buat SATU Shorts Muse dari Telegram (alur terpandu /buatshort).
    Keamanan: tanpa login panel, tapi wajib membawa `key` == operator_key
    di config.json (HMAC, pola sama seperti retry_task). Route ini masuk
    _OPERATOR_ENDPOINTS di check_security (bebas login/CSRF, auth HMAC).
    Validasi + pembuatan SAMA PERSIS seperti shorts_muse_create (helper
    _create_shorts_muse_task) — termasuk stagger, autopilot, dijeda,
    dan antrean penuh → 429."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    if not _valid_yt_id(data.get('yt_id') or ''):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    res = _create_shorts_muse_task(data)
    if not res["ok"]:
        return jsonify({"status": "error", "message": res["message"]}), res["http"]
    if res.get("preview"):
        threading.Thread(target=_send_approval_previews,
                         args=(res["batch_id"], res["channel_name"], [res["preview"]]), daemon=True).start()
    return jsonify({"status": "success", "message": res["message"],
                    "task_id": res["task_id"],
                    "title": res.get("title"), "title_cleaned": res.get("title_cleaned", False),
                    "approval": "approved" if res["auto_mode"] else "pending",
                    "publish_date": res.get("publish_date"),
                    "bg_auto": res.get("bg_auto", False),
                    "bg_video": res.get("bg_video", False),
                    "bg_video_count": res.get("bg_video_count", 0)})


@app.route('/api/get_shorts_muse')
def get_shorts_muse():
    """Daftar Shorts Muse (aktif + riwayat) untuk panel — butuh login.
    Dipakai kartu daftar di panel Shorts Muse."""
    out = []
    for t in list(active_tasks) + list(history_tasks):
        bp = t.get('blueprint') or {}
        if t.get('kind') != SHORTS_MUSE_KIND and bp.get('kind') != SHORTS_MUSE_KIND:
            continue
        ch = next((c for c in database_channel if c.get('yt_id') == (t.get('yt_id') or bp.get('yt_id'))), None)
        out.append({
            "id": t.get('id'), "title": t.get('title', '?'),
            "yt_id": t.get('yt_id') or bp.get('yt_id'),
            "channel": ch.get('name', '') if ch else '',
            "publish_date": t.get('time') or bp.get('publish_date', ''),
            "duration_sec": bp.get('shorts_duration_sec'),
            "cta": bp.get('shorts_cta', ''),
            "status": t.get('status', ''),
            "approval": _approval_of(t),
            "video_id": t.get('video_id'),
            "video_url": t.get('video_url'),
        })
    out.sort(key=lambda x: str(x.get('publish_date') or ''), reverse=True)
    return jsonify({"status": "success", "shorts": out})


@app.route('/api/batch_create', methods=['POST'])
@app.route('/api/batch_create', methods=['POST'])
def batch_create():
    """Buat batch video long (panel). Butuh login panel (route TIDAK masuk
    allowed_routes). Inti pembuatan ada di _create_long_batch — dipakai
    bersama route operator Telegram."""
    data = request.get_json(silent=True) or {}
    res = _create_long_batch(data, max_count=BATCH_MAX_COUNT)
    if not res["ok"]:
        return jsonify({"status": "error", "message": res["message"]}), res["http"]
    if res.get("previews"):
        threading.Thread(target=_send_approval_previews,
                         args=(res["batch_id"], res["channel_name"], res["previews"]), daemon=True).start()
    return jsonify({"status": "success", "message": res["message"],
                    "title_cleaned": res.get("title_cleaned", False)})


def _create_long_batch(data, max_count=BATCH_MAX_COUNT):
    """Inti pembuatan batch video long — dipakai route panel (login) dan
    route operator Telegram (operator_key). data memakai bentuk yang sama
    seperti batch_create: yt_id, count, generated_titles,
    generated_descriptions, target_durations_array, start_date
    ('%Y-%m-%dT%H:%M'), interval_days, generated_publish_dates, plus
    field blueprint lain (mp3_per_video, vid_per_video, bg_count,
    vis_*, render_preset, use_special_song, description, tags, privacy,
    playlist_id, use_floating_card, source_mode). max_count membatasi
    jumlah video (panel 50, Telegram 7).
    Mengembalikan dict: ok/message/http/count/batch_id/auto_mode/
    previews (list dict preview approval)/tasks (list {id,title,
    publish_date})/channel_name. TIDAK mengirim preview Telegram —
    pemanggil yang memicu _send_approval_previews bila previews ada."""
    yt_id = data.get('yt_id')
    # 🔒 #15: count divalidasi — non-integer/negatif/0 ditolak 400,
    # maks max_count video per request (dulu tanpa batas → ribuan job sekaligus).
    try:
        count = int(data.get('count', 1))
    except Exception:
        return {"ok": False, "message": f"count harus angka bulat 1–{max_count}.", "http": 400}
    if isinstance(data.get('count'), bool) or count < 1 or count > max_count:
        return {"ok": False, "message": f"count harus angka bulat 1–{max_count}.", "http": 400}

    titles = data.get('generated_titles', [])
    # 🔥 TAMBAHKAN BARIS INI UNTUK MENANGKAP DESKRIPSI ACAK 🔥
    descs = data.get('generated_descriptions', [])

    durations_array = data.get('target_durations_array', [])

    try:
        base_date = datetime.strptime(data['start_date'], '%Y-%m-%dT%H:%M')
    except:
        return {"ok": False, "message": "Format tanggal salah", "http": 400}
        
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
    # 🔒 #15: batch_id memakai milidetik + token acak supaya dua batch
    # dalam milidetik yang sama tidak pernah bentrok.
    batch_id = f"b{int(time.time() * 1000)}-{secrets.token_hex(3)}"
    channel_name = next((c.get('name', yt_id) for c in database_channel if c.get('yt_id') == yt_id), yt_id or 'Tanpa Channel')
    previews = []
    # 🔥 FASE 6: mode autopilot per channel — 'auto' (channel terpercaya:
    # batch langsung masuk antrean tanpa approval) vs 'approval' (default,
    # channel baru/lama tetap lewat approval Fase 2).
    channel_rec = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    auto_mode = _autopilot_of(channel_rec) == 'auto'
    channel_paused = _channel_paused(yt_id)
    # 🔒 #15: batch Full-Auto langsung masuk antrean — tolak lebih awal
    # bila antrean tidak muat untuk seluruh batch. (Mode approval tidak
    # memakai antrean sekarang, jadi tidak dicek di sini; saat disetujui,
    # queue_put menolak bila penuh.)
    if auto_mode and not channel_paused and render_queue.qsize() + count > QUEUE_MAX_SIZE:
        return {"ok": False,
                "message": f"Antrean render penuh (maks {QUEUE_MAX_SIZE} task). "
                           "Tunggu antrean berkurang lalu coba lagi.", "http": 429}
    auto_blueprints = []
    adjusted_dates = []
    shifted_count = 0
    # 🧹 v1.2.13: tag dibersihkan sekali untuk seluruh batch (bank judul
    # & ketikan user bisa mengandung emoji/simbol yang ditolak YouTube).
    _tags_clean = ', '.join(_clean_upload_tags(data.get('tags') or ''))
    _any_cleaned = False

    for i in range(count):
        # 🔒 #15: ID task monotonik — tidak pernah bentrok antar batch.
        t_id = _next_task_id()
        
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
            
        # 🧹 v1.2.13: bersihkan judul & deskripsi tiap video (bank judul
        # bisa mengandung emoji) — fallback aman bila hasil bersih kosong.
        _raw_title = titles[i] if i < len(titles) else f"Auto Video #{i+1}"
        _date_s = _wib_now().strftime('%Y-%m-%d')
        _title = _clean_upload_text(_raw_title, max_len=100,
                                    fallback=f"Video {_date_s}",
                                    strip_channel=channel_name)
        _raw_desc = descs[i] if i < len(descs) else data.get('description', '')
        _desc = _clean_upload_text(_raw_desc, max_len=5000, fallback=_title)
        _title_cleaned = (_title != _raw_title) or (_desc != _raw_desc)
        _any_cleaned = _any_cleaned or _title_cleaned

        blueprint = {
            "id": t_id, "yt_id": yt_id, "title": _title,
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
            "description": _desc, 
            
            "tags": _tags_clean, "privacy": data.get('privacy', 'public'), "playlist_id": data.get('playlist_id', ''),
            "use_floating_card": data.get('use_floating_card', False),
            "source_mode": data.get('source_mode', 'mix'),  # 🔥 BARU: simpan jalur render engine di blueprint
            "batch_id": batch_id,  # 🔥 FASE 2: batch pemilik task (untuk "Setujui Semua")
            "title_cleaned": _title_cleaned,
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
                "title_cleaned": _title_cleaned,
                "blueprint": blueprint
            })
            _activity_task_created("📺 VOD", blueprint['title'], yt_id)  # 🔥 v1.3.71: catat ke log aktivitas
        if auto_mode and not channel_paused:
            auto_blueprints.append(blueprint)
        previews.append({
            "id": t_id, "title": blueprint['title'],
            "publish_date": blueprint['publish_date'],
            "duration": blueprint['target_duration_hours'],
            "description": blueprint.get('description', ''),
            "privacy": blueprint.get('privacy', 'public'),
            "title_cleaned": _title_cleaned,
            "tags": blueprint.get('tags', ''),
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
        # Kapasitas sudah dicek di atas, jadi kegagalan di sini tidak
        # mungkin karena antrean penuh — hanya kondisi balap antar request.
        # 🔒 #15: task yang gagal masuk antrean dipindah ke riwayat dengan
        # status "Stuck" supaya bisa dipulihkan via recover_failed_tasks,
        # bukan dibiarkan "Menunggu Giliran" selamanya tanpa pernah di-queue.
        failed_bps = []
        for bp in auto_blueprints:
            try:
                queue_put(bp)
            except Exception as e:
                print(f"Gagal queue auto task {bp.get('id')}: {e}", flush=True)
                failed_bps.append(bp)
        if failed_bps:
            failed_ids = {bp.get('id') for bp in failed_bps}
            with db_lock:
                still_active = []
                for t in active_tasks:
                    if t.get('id') in failed_ids:
                        t['status'] = "Gagal ❌ (Stuck — antrean penuh saat dibuat)"
                        t['updated_at'] = time.time()
                        history_tasks.insert(0, t)
                    else:
                        still_active.append(t)
                active_tasks[:] = still_active
                save_tasks_db()
            return {"ok": False,
                    "message": f"{len(failed_bps)} dari {count} video gagal masuk antrean render (antrean penuh). "
                               "Task yang gagal ada di Riwayat dan bisa dipulihkan lewat 'Pulihkan Task Gagal'.",
                    "http": 500, "count": count, "batch_id": batch_id, "auto_mode": True,
                    "previews": previews, "channel_name": channel_name,
                    "title_cleaned": _any_cleaned}
        _ok_tasks = [{"id": p["id"], "title": p["title"], "publish_date": p["publish_date"],
                      "title_cleaned": p.get("title_cleaned", False)} for p in previews]
        return {"ok": True,
                "message": f"{count} Video dibuat dan langsung masuk antrean (mode Full-Auto)!{_shift_note}",
                "http": 200, "count": count, "batch_id": batch_id, "auto_mode": True,
                "previews": [], "tasks": _ok_tasks, "channel_name": channel_name,
                "title_cleaned": _any_cleaned}

    # 🔥 FASE 2: preview approval dikirim pemanggil di thread background
    # (panel langsung merespons; kalau Telegram mati, task tetap pending).
    _ok_tasks = [{"id": p["id"], "title": p["title"], "publish_date": p["publish_date"],
                  "title_cleaned": p.get("title_cleaned", False)} for p in previews]
    return {"ok": True,
            "message": f"{count} Video dibuat dan menunggu approval di Telegram!{_shift_note}",
            "http": 200, "count": count, "batch_id": batch_id, "auto_mode": False,
            "previews": previews, "tasks": _ok_tasks, "channel_name": channel_name,
            "title_cleaned": _any_cleaned}


def _last_scheduled_info(yt_id):
    """Tanggal tayang TERAKHIR + judul terakhir channel dari tasks_db.json
    (active + history). Tugas berstatus Gagal/Ditolak diabaikan — hanya
    jadwal yang masih berlaku yang dihitung. Kembalikan (datetime|None,
    judul_terakhir|None)."""
    best_dt = None
    best_title = None
    for t in list(active_tasks) + list(history_tasks):
        if (t or {}).get('yt_id') != yt_id:
            continue
        status = str((t or {}).get('status', ''))
        if 'Gagal' in status or 'Ditolak' in status:
            continue
        raw = (t or {}).get('time') or (t or {}).get('publish_date') or \
            ((t or {}).get('blueprint') or {}).get('publish_date') or ''
        try:
            dt = datetime.strptime(str(raw), '%Y-%m-%d %H:%M')
        except Exception:
            continue
        if best_dt is None or dt > best_dt:
            best_dt = dt
            best_title = (t or {}).get('title') or ''
    return best_dt, best_title


def _channel_default_publish_time(channel):
    """Jam tayang default channel ("HH:MM") untuk /perpanjang & batch baru.
    Disimpan di channels_db.json field `default_publish_time` (diatur dari
    Telegram /aturjadwal, v1.2.20). Kembalikan (hour, minute) atau None
    bila belum diatur / tidak valid — pemanggil pakai jam jadwal terakhir
    atau 19:00."""
    try:
        s = str((channel or {}).get('default_publish_time') or '').strip()
        m = re.match(r'^([01]\d|2[0-3]):([0-5]\d)$', s)
        if not m:
            return None
        return int(m.group(1)), int(m.group(2))
    except Exception:
        return None


def _extend_schedule(yt_id, days, start=None):
    """Perpanjang jadwal channel sebanyak `days` hari (1–90). Logika inti
    dipakai endpoint operator Telegram (dan panel bila ada nanti).
    - Mulai sehari setelah tanggal tayang terakhir (atau besok bila belum
      ada jadwal); `start` opsional menggeser awal (tetap tidak boleh
      menabrak jadwal yang sudah ada).
    - Judul dari bank judul channel secara BERURUTAN, melanjutkan dari
      judul terakhir yang dipakai (bukan mengulang dari awal) — memakai
      pola siklus bank yang sama seperti operator_create_long.
    - Semua gerbang _create_long_batch berlaku (stagger, autopilot,
      antrean penuh → 429). Channel dijeda / bank judul kosong ditolak
      dengan pesan jelas. Mengembalikan dict seperti _create_long_batch
      ditambah first_date/last_date."""
    channel_rec = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if channel_rec is None:
        return {"ok": False, "message": "Channel tidak ditemukan.", "http": 404}
    if _channel_paused(yt_id):
        return {"ok": False,
                "message": f"Channel {channel_rec.get('name', yt_id)} sedang dijeda — lanjutkan dulu channelnya sebelum memperpanjang jadwal.",
                "http": 400}
    bank = [t for t in (channel_rec.get('title_bank') or [])
            if isinstance(t, str) and t.strip()]
    if not bank:
        return {"ok": False,
                "message": "Bank judul channel ini kosong — isi Bank Judul di panel dulu, baru jadwalnya bisa diperpanjang.",
                "http": 400}
    last_dt, last_title = _last_scheduled_info(yt_id)
    # Jam tayang (v1.2.20): default_publish_time channel bila diatur
    # (lewat /aturjadwal), kalau tidak ikut jam jadwal terakhir, kalau
    # belum ada jadwal 19:00.
    _dpt = _channel_default_publish_time(channel_rec)
    hour, minute = _dpt if _dpt else ((last_dt.hour, last_dt.minute) if last_dt else (19, 0))
    if start is not None:
        # start eksplisit dipakai apa adanya, tapi tidak boleh menabrak
        # jadwal yang sudah ada (geser ke sehari setelah jadwal terakhir).
        base_date = start
        if last_dt is not None:
            floor = (last_dt + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
            if base_date < floor:
                base_date = floor
    elif last_dt is not None:
        base_date = (last_dt + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    else:
        base_date = (_wib_now() + timedelta(days=1)).replace(hour=hour, minute=minute, second=0, microsecond=0)
    # Offset judul: lanjutkan dari judul terakhir yang dipakai di bank.
    offset = 0
    if last_title and last_title in bank:
        offset = bank.index(last_title) + 1
    titles = [bank[(offset + i) % len(bank)] for i in range(days)]
    durations = [random.randint(60, 80) / 60.0 for _ in range(days)]
    ch_name = channel_rec.get('name', yt_id)
    descs = [f"{t}\n\n{ch_name}" for t in titles]
    prepared = {
        "yt_id": yt_id, "count": days,
        "generated_titles": titles,
        "generated_descriptions": descs,
        "target_durations_array": durations,
        "start_date": base_date.strftime('%Y-%m-%dT%H:%M'),
        "interval_days": 1,
        "mp3_per_video": 5, "vid_per_video": 1, "bg_count": 1,
        "render_preset": "ultrafast", "source_mode": "mix",
        "privacy": "public",
    }
    res = _create_long_batch(prepared, max_count=90)
    if not res.get("ok"):
        return res
    tasks = res.get("tasks", []) or []
    res["first_date"] = tasks[0]["publish_date"] if tasks else base_date.strftime('%Y-%m-%d %H:%M')
    res["last_date"] = tasks[-1]["publish_date"] if tasks else res["first_date"]
    res["title_offset"] = offset
    return res


@app.route('/api/operator/create_long', methods=['POST'])
def operator_create_long():
    """Buat batch video long 60–80 menit dari Telegram (alur terpandu
    /buatlong). Keamanan: tanpa login panel, tapi wajib membawa `key` ==
    operator_key di config.json (HMAC, pola sama seperti retry_task).
    Route ini masuk _OPERATOR_ENDPOINTS di check_security.
    Beda dengan batch_create panel: count dibatasi 1–7 (supaya ringkasan
    di chat tetap terbaca), judul kosong diambil dari bank judul channel,
    durasi diacak 60–80 menit per video (seperti panel), tanggal mulai
    boleh hanya tanggal (jam default 19:00). Pembuatan memakai helper
    _create_long_batch yang sama — stagger, autopilot, dijeda, dan
    antrean penuh → 429 berlaku persis seperti panel."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    yt_id = (data.get('yt_id') or '').strip()
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    channel_rec = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
    if channel_rec is None:
        return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
    try:
        count = int(data.get('count', 1))
    except Exception:
        return jsonify({"status": "error", "message": "count harus angka bulat 1–7."}), 400
    if isinstance(data.get('count'), bool) or count < 1 or count > 7:
        return jsonify({"status": "error", "message": "count harus angka bulat 1–7."}), 400
    # Tanggal mulai: 'YYYY-MM-DD HH:MM' / 'YYYY-MM-DDTHH:MM' / 'YYYY-MM-DD'
    # (hanya tanggal → jam 19:00).
    start_raw = (data.get('start_date') or '').strip()
    base_date = None
    for _fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
        try:
            base_date = datetime.strptime(start_raw, _fmt)
            break
        except Exception:
            continue
    if base_date is None:
        return jsonify({"status": "error", "message": "Format tanggal salah (pakai YYYY-MM-DD HH:MM)."}), 400
    if len(start_raw) == 10:
        base_date = base_date.replace(hour=19, minute=0)
    try:
        interval_days = float(data.get('interval_days', 1))
    except Exception:
        return jsonify({"status": "error", "message": "interval_days harus angka."}), 400
    interval_days = max(0.5, min(30.0, interval_days))
    # Judul: dari request (list / string pisah '|') atau bank judul channel.
    titles = data.get('titles') or []
    if isinstance(titles, str):
        titles = [t.strip() for t in titles.replace('\n', '|').split('|') if t.strip()]
    titles = [str(t).strip() for t in titles if str(t).strip()][:50]
    if not titles:
        bank = [t for t in (channel_rec.get('title_bank') or [])
                if isinstance(t, str) and t.strip()]
        if not bank:
            return jsonify({"status": "error",
                            "message": "Bank judul channel ini kosong — isi Bank Judul di panel dulu, atau kirim judul videonya."}), 400
        titles = [bank[i % len(bank)] for i in range(count)]
    titles = [titles[i % len(titles)] for i in range(count)]
    # Durasi acak 60–80 menit per video (sama seperti panel: bilangan bulat).
    durations = [random.randint(60, 80) / 60.0 for _ in range(count)]
    ch_name = channel_rec.get('name', yt_id)
    descs = [f"{t}\n\n{ch_name}" for t in titles]
    prepared = {
        "yt_id": yt_id, "count": count,
        "generated_titles": titles,
        "generated_descriptions": descs,
        "target_durations_array": durations,
        "start_date": base_date.strftime('%Y-%m-%dT%H:%M'),
        "interval_days": interval_days,
        "mp3_per_video": 5, "vid_per_video": 1, "bg_count": 1,
        "render_preset": "ultrafast", "source_mode": "mix",
        "privacy": "public",
    }
    res = _create_long_batch(prepared, max_count=7)
    if not res["ok"]:
        return jsonify({"status": "error", "message": res["message"]}), res["http"]
    if res.get("previews"):
        threading.Thread(target=_send_approval_previews,
                         args=(res["batch_id"], res["channel_name"], res["previews"]), daemon=True).start()
    return jsonify({"status": "success", "message": res["message"],
                    "batch_id": res["batch_id"], "count": res["count"],
                    "approval": "approved" if res["auto_mode"] else "pending",
                    "tasks": res.get("tasks", []),
                    "title_cleaned": res.get("title_cleaned", False)})


@app.route('/api/operator/extend_schedule', methods=['POST'])
def operator_extend_schedule():
    """Perpanjang jadwal channel dari Telegram (/perpanjang / NLU).
    Keamanan: wajib `key` == operator_key (HMAC, pola operator lain).
    Route ini masuk _OPERATOR_ENDPOINTS di check_security.
    Parameter: yt_id, days (default 30, batas 1–90), start opsional
    ('YYYY-MM-DD' / 'YYYY-MM-DD HH:MM'). Judul & gerbang mengikuti
    _extend_schedule (lanjut dari jadwal & judul terakhir)."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    yt_id = (data.get('yt_id') or '').strip()
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    try:
        days = int(data.get('days', 30))
    except Exception:
        return jsonify({"status": "error", "message": "days harus angka bulat 1–90."}), 400
    if isinstance(data.get('days'), bool) or days < 1 or days > 90:
        return jsonify({"status": "error", "message": "days harus angka bulat 1–90."}), 400
    start = None
    start_raw = (data.get('start') or '').strip()
    if start_raw:
        for _fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%dT%H:%M', '%Y-%m-%d'):
            try:
                start = datetime.strptime(start_raw, _fmt)
                break
            except Exception:
                continue
        if start is None:
            return jsonify({"status": "error", "message": "Format start salah (pakai YYYY-MM-DD atau YYYY-MM-DD HH:MM)."}), 400
        if len(start_raw) == 10:
            start = start.replace(hour=19, minute=0)
    res = _extend_schedule(yt_id, days, start=start)
    if not res.get("ok"):
        return jsonify({"status": "error", "message": res["message"]}), res["http"]
    if res.get("previews"):
        threading.Thread(target=_send_approval_previews,
                         args=(res["batch_id"], res["channel_name"], res["previews"]), daemon=True).start()
    return jsonify({"status": "success", "message": res["message"],
                    "batch_id": res["batch_id"], "count": res["count"],
                    "approval": "approved" if res["auto_mode"] else "pending",
                    "first_date": res.get("first_date"), "last_date": res.get("last_date"),
                    "channel_name": res.get("channel_name"),
                    "tasks": res.get("tasks", [])})


@app.route('/api/operator/set_autopilot', methods=['POST'])
def operator_set_autopilot():
    """Ubah mode autopilot channel dari Telegram (/mode / NLU).
    Keamanan: wajib `key` == operator_key (HMAC). Logika & pesan sama
    seperti /api/set_channel_autopilot (field `autopilot` di
    channels_db.json) — endpoint ini hanya membungkusnya untuk operator.
    Mode tidak valid → 400; channel tidak dikenal → 404."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    yt_id = (data.get('yt_id') or '').strip()
    mode = str(data.get('mode', '') or data.get('autopilot', '') or '').lower()
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    if mode not in ('auto', 'approval'):
        return jsonify({"status": "error", "message": "Mode harus 'auto' atau 'approval'."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        target['autopilot'] = mode
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan autopilot {yt_id} (operator): {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    if mode == 'auto':
        send_tg_message(f"🤖 *Siap Bos, channel {_tg_escape(name)} saya set Full-Auto!* 😎\nBatch baru langsung jalan tanpa approval — saya yang urus sampai tayang 💪")
        return jsonify({"status": "success", "autopilot": "auto", "channel_name": name,
                        "message": f"Channel {name} mode Full-Auto. Batch baru langsung masuk antrean."})
    send_tg_message(f"👀 *Siap Bos, channel {_tg_escape(name)} balik ke mode Approval.* 😌\nBatch baru tetap saya kirim preview dulu buat Bos cek.")
    return jsonify({"status": "success", "autopilot": "approval", "channel_name": name,
                    "message": f"Channel {name} mode Approval. Batch baru menunggu persetujuan."})


@app.route('/api/operator/set_daily_playlist', methods=['POST'])
def operator_set_daily_playlist():
    """Nyalakan/matikan jadwal Playlist harian satu channel dari Telegram
    (/jadwalplaylist, /stopjadwal, /mulaijadwal — v1.3.18).
    Keamanan: wajib `key` == operator_key (HMAC). Field `daily_playlist`
    di channels_db.json; dibaca cron harian Muse tiap pagi — hanya channel
    dengan flag ini yang dibuatkan 1 video Playlist per hari (riset fresh
    + publish langsung). Channel jalur Zumo JANGAN diaktifkan (pola beda).
    Channel tidak dikenal → 404; enabled bukan bool → 400."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    yt_id = (data.get('yt_id') or '').strip()
    enabled = data.get('enabled')
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    if not isinstance(enabled, bool):
        return jsonify({"status": "error", "message": "enabled harus true/false."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        target['daily_playlist'] = enabled
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan daily_playlist {yt_id} (operator): {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    return jsonify({"status": "success", "daily_playlist": enabled, "channel_name": name,
                    "message": f"Channel {name} jadwal harian {'aktif' if enabled else 'berhenti'}."})


@app.route('/api/operator/set_channel_group', methods=['POST'])
def operator_set_channel_group():
    """Set group satu channel dari Telegram (/group, NLU atur_group — v1.3.68).
    Keamanan: wajib `key` == operator_key (HMAC). Field `group` di
    channels_db.json; '' = keluarkan dari group. 1 channel = 1 group.
    Group baru dibuat otomatis bila belum ada (daftar group = nilai
    berbeda yang dipakai channel). Channel tidak dikenal → 404."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    yt_id = (data.get('yt_id') or '').strip()
    group = data.get('group') or ''
    if not isinstance(group, str):
        return jsonify({"status": "error", "message": "group harus teks."}), 400
    group = group.strip()[:40]
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        if group:
            target['group'] = group
        else:
            target.pop('group', None)
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan group {yt_id} (operator): {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    return jsonify({"status": "success", "group": group, "channel_name": name,
                    "message": f"Channel {name} {'masuk group ' + group if group else 'keluar dari group'}."})


def _valid_owner_email(email):
    """Email pemilik channel: '' (kosongkan) atau format email biasa."""
    if email == '':
        return True
    return re.match(r'^[^@\s]+@[^@\s]+\.[^@\s]+$', email) is not None


@app.route('/api/set_channel_email', methods=['POST'])
def set_channel_email():
    """Isi/ubah email pemilik channel manual dari tabel CHANNEL TERHUBUNG
    (klik kolom EMAIL — v1.3.54). Panel-only (login + CSRF via
    check_security). Email '' = kosongkan. Email juga terisi otomatis
    saat login Analytics (scope openid/email)."""
    data = request.get_json(silent=True) or {}
    yt_id = (data.get('yt_id') or '').strip()
    email = (data.get('email') or '').strip()
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    if not _valid_owner_email(email):
        return jsonify({"status": "error", "message": "Format email tidak valid."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        target['owner_email'] = email
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan owner_email {yt_id}: {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    return jsonify({"status": "success", "owner_email": email, "channel_name": name,
                    "message": f"Email {name} tersimpan."})


@app.route('/api/set_channel_group', methods=['POST'])
def set_channel_group():
    """Set group satu channel dari panel (dropdown GROUP di tabel channel
    — v1.3.68). Panel-only (login + CSRF via check_security).
    group '' = keluarkan dari group."""
    data = request.get_json(silent=True) or {}
    yt_id = (data.get('yt_id') or '').strip()
    group = data.get('group') or ''
    if not isinstance(group, str):
        return jsonify({"status": "error", "message": "group harus teks."}), 400
    group = group.strip()[:40]
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        if group:
            target['group'] = group
        else:
            target.pop('group', None)
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan group {yt_id}: {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    return jsonify({"status": "success", "group": group, "channel_name": name,
                    "message": f"Group {name} tersimpan."})


@app.route('/api/operator/set_paused', methods=['POST'])
def operator_set_paused():
    """Jeda / lanjutkan channel dari Telegram (NLU /jeda / /lanjutkan).
    Keamanan: wajib `key` == operator_key (HMAC). Logika sama seperti
    /api/set_channel_paused (field `paused` di channels_db.json;
    lanjut → reset fail_streak + kembalikan task tertahan)."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    yt_id = (data.get('yt_id') or '').strip()
    paused = bool(data.get('paused', False))
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        target['paused'] = paused
        if not paused:
            target['fail_streak'] = 0
            target.pop('auto_paused', None)
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan paused {yt_id} (operator): {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    if paused:
        send_tg_message(f"⏸️ *Siap Bos, channel {_tg_escape(name)} saya jeda dulu.* 😌\nTask-nya saya tahan semua, nggak ada yang jalan sampai Bos lanjutkan lagi.")
        return jsonify({"status": "success", "paused": True, "resumed_count": 0,
                        "channel_name": name,
                        "message": f"Channel {name} dijeda. Task terjadwal ditahan."})
    resumed = _resume_channel_tasks(yt_id)
    send_tg_message(f"▶️ *Siap Bos, channel {_tg_escape(name)} jalan lagi!* 💪\n{resumed} task udah masuk antrean render.")
    return jsonify({"status": "success", "paused": False, "resumed_count": resumed,
                    "channel_name": name,
                    "message": f"Channel {name} dilanjutkan. {resumed} task masuk antrean."})


@app.route('/api/operator/set_channel_auto_shorts', methods=['POST'])
def operator_set_channel_auto_shorts():
    """Ubah konfigurasi Jadwal Otomatis Short dari Telegram (/aturjadwal).
    Keamanan: wajib `key` == operator_key (HMAC). Logika SAMA dengan
    panel /api/set_channel_auto_shorts (merge + validasi penuh) —
    panggil view panel langsung supaya tidak ada duplikasi logika."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    return set_channel_auto_shorts()


@app.route('/api/operator/set_channel_publish_time', methods=['POST'])
def operator_set_channel_publish_time():
    """Ubah jam tayang default channel dari Telegram (/aturjadwal).
    Disimpan di channels_db.json field `default_publish_time` ("HH:MM");
    dipakai /perpanjang untuk jam video-video berikutnya. Keamanan:
    wajib `key` == operator_key (HMAC)."""
    data = request.get_json(silent=True) or {}
    ok_key, msg_key, _st = _validate_operator_key(data)
    if not ok_key:
        return jsonify({"status": "error", "message": msg_key}), _st
    yt_id = (data.get('yt_id') or '').strip()
    publish_time = str(data.get('publish_time') or '').strip()
    if not _valid_yt_id(yt_id):
        return jsonify({"status": "error", "message": "Channel tidak valid."}), 400
    if not re.match(r'^([01]\d|2[0-3]):([0-5]\d)$', publish_time):
        return jsonify({"status": "error", "message": "Jam tidak valid — pakai format HH:MM (00:00–23:59)."}), 400
    with db_lock:
        target = next((c for c in database_channel if c.get('yt_id') == yt_id), None)
        if target is None:
            return jsonify({"status": "error", "message": "Channel tidak ditemukan."}), 404
        target['default_publish_time'] = publish_time
        name = target.get('name', yt_id)
        try:
            save_channels(database_channel)
        except Exception as e:
            print(f"Gagal simpan publish_time {yt_id} (operator): {e}", flush=True)
            return jsonify({"status": "error", "message": "Gagal menyimpan perubahan."}), 500
    return jsonify({"status": "success", "publish_time": publish_time,
                    "message": f"Jam tayang {name} diubah jadi {publish_time} WIB (berlaku untuk jadwal baru)."})



@app.route('/uploads/<path:filename>')
def serve_uploads(filename):
    # 🔒 #9: wajib login panel (route ini SUDAH TIDAK di allowed_routes).
    # 🔒 #6: file di sini disajikan dengan CSP sandbox + nosniff — kalau
    # ada file HTML yang lolos, browser tidak akan mengeksekusi scriptnya
    # pada origin panel. Content-Disposition: attachment TIDAK dipakai
    # karena akan merusak preview gambar/video galeri di panel.
    resp = send_from_directory(BASE_UPLOAD, filename)
    resp.headers['Content-Security-Policy'] = 'sandbox'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    return resp

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
                        msg = f"Notifikasi Ziqva Muse Bot\n\nChannel: *{_tg_escape(c['name'])}*\nStok jadwal video hampir habis!\nJadwal terakhir: {_tg_escape(last_date_str)}\n\nWaktunya buat video baru, Bos K!"
                        try:
                            # Kirim pesan ke Telegram
                            # 🔥 v1.3.75: notif WAJIB bawa tombol saran
                            # (prinsip 20 Nov 2026) — Bos tinggal tap.
                            requests.post(
                                f"https://api.telegram.org/bot{token}/sendMessage",
                                json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown",
                                      "reply_markup": {"inline_keyboard": [
                                          [{"text": "🎵 Buat video baru", "callback_data": f"stok:buat:{c.get('yt_id')}"}],
                                          [{"text": "📅 Lihat jadwal", "callback_data": "rep:jadwal"}]]}}
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
                        queue_put(t["blueprint"], allow_overflow=True)
                        activated.append(t['title'][:50])
                    except Exception:
                        pass
        if activated:
            save_tasks_db()
            msg = "⏳ *Bos, jadwalnya tiba nih!* 😎\n" + "\n".join("• " + _tg_escape(a) for a in activated)
            send_tg_message(msg)

threading.Thread(target=scheduled_task_activator, daemon=True).start()

# ============================================================
# ⏰ JADWAL OTOMATIS SHORT — 1 short per channel per hari,
# jam buat & jam tayang diatur user per channel (Settings).
# ============================================================
_AUTO_SHORTS_TIME_RE = re.compile(r'^([01]\d|2[0-3]):[0-5]\d$')
_AUTO_SHORTS_MONTHS_ID = ['Januari', 'Februari', 'Maret', 'April', 'Mei', 'Juni',
                          'Juli', 'Agustus', 'September', 'Oktober', 'November', 'Desember']


def _auto_shorts_cfg(channel):
    """Konfigurasi auto-shorts satu channel, selalu lengkap (default bila
    field belum ada / tidak valid). Tidak pernah raise."""
    raw = (channel or {}).get('auto_shorts') or {}
    if not isinstance(raw, dict):
        raw = {}
    theme = str(raw.get('theme') or '').strip().lower()
    if theme not in AUTO_SHORTS_NICHES:
        theme = AUTO_SHORTS_DEFAULTS['theme']

    def _hhmm(val, default):
        s = str(val or '').strip()
        return s if _AUTO_SHORTS_TIME_RE.match(s) else default

    try:
        dur = int(raw.get('duration_sec') or 0)
    except Exception:
        dur = 0
    if dur not in SHORTS_MUSE_DURATIONS:
        dur = AUTO_SHORTS_DEFAULTS['duration_sec']
    return {
        'enabled': bool(raw.get('enabled', False)),
        'theme': theme,
        'create_time': _hhmm(raw.get('create_time'), AUTO_SHORTS_DEFAULTS['create_time']),
        'publish_time': _hhmm(raw.get('publish_time'), AUTO_SHORTS_DEFAULTS['publish_time']),
        'duration_sec': dur,
    }


def _validate_auto_shorts_cfg(cfg):
    """Validasi konfigurasi LENGKAP (hasil gabungan tersimpan + yang dikirim).
    Kembalikan (cfg_ternormalisasi, error) — error None bila valid."""
    if not isinstance(cfg, dict):
        return None, "Konfigurasi jadwal otomatis harus berupa objek."
    enabled = cfg.get('enabled')
    if not isinstance(enabled, bool):
        return None, "enabled harus true/false."
    theme = str(cfg.get('theme') or '').strip().lower()
    if theme not in AUTO_SHORTS_NICHES:
        return None, "Tema tidak dikenal (pilih: sleep, lofi, piano, hujan, meditasi, fokus)."
    for key in ('create_time', 'publish_time'):
        val = str(cfg.get(key) or '').strip()
        if not _AUTO_SHORTS_TIME_RE.match(val):
            return None, f"Jam {key} tidak valid — pakai format HH:MM (00:00–23:59)."
    try:
        dur = int(cfg.get('duration_sec'))
    except Exception:
        return None, "Durasi harus 15, 30, atau 60 detik."
    if dur not in SHORTS_MUSE_DURATIONS:
        return None, "Durasi harus 15, 30, atau 60 detik."
    return {'enabled': enabled, 'theme': theme,
            'create_time': str(cfg.get('create_time')).strip(),
            'publish_time': str(cfg.get('publish_time')).strip(),
            'duration_sec': dur}, None


def _auto_shorts_pick_content(channel, theme, for_date):
    """Konten short hari itu: judul, hook, naskah, CTA. Rotasi berdasarkan
    tanggal (ordinal) supaya tiap hari berbeda; offset berbeda per bagian
    supaya kombinasinya tidak berulang berbarengan. Judul dari bank judul
    channel bila ada, bila kosong dari label tema + tanggal (tanpa klaim)."""
    bank = AUTO_SHORTS_BANK.get(theme) or AUTO_SHORTS_BANK['sleep']
    ordinal = for_date.toordinal()

    def _pick(items, offset):
        items = [s for s in (items or []) if isinstance(s, str) and s.strip()]
        if not items:
            return ''
        return items[(ordinal + offset) % len(items)]

    hook = _pick(bank.get('hooks'), 1)
    script = _pick(bank.get('scripts'), 2)
    cta = _pick(bank.get('ctas'), 3) or SHORTS_MUSE_DEFAULT_CTA
    titles = [t for t in (channel.get('title_bank') or []) if isinstance(t, str) and t.strip()]
    if titles:
        title = titles[ordinal % len(titles)]
    else:
        label = AUTO_SHORTS_TITLE_LABELS.get(theme, 'Musik Santai')
        title = f"{label} — {for_date.day} {_AUTO_SHORTS_MONTHS_ID[for_date.month - 1]} {for_date.year}"
    return title, hook, script, cta


def _auto_shorts_window(cfg, now_wib):
    """Jendela jadwal untuk 'hari jadwal' D yang sedang berjalan.
    D = hari ini bila sekarang sudah lewat create_time, kalau belum → kemarin
    (jendela kemarin masih bisa terbuka bila publish_time besok pagi).
    publish_dt = hari D jam publish_time bila publish_time > create_time,
    kalau tidak → keesokan harinya. Kembalikan (for_date, create_dt,
    publish_dt) — semua aware WIB."""
    hh, mm = (int(x) for x in cfg['create_time'].split(':'))
    ph, pm = (int(x) for x in cfg['publish_time'].split(':'))
    wib = ZoneInfo("Asia/Jakarta")
    today = now_wib.date()
    if (now_wib.hour, now_wib.minute) >= (hh, mm):
        for_date = today
    else:
        for_date = today - timedelta(days=1)
    create_dt = datetime(for_date.year, for_date.month, for_date.day, hh, mm, tzinfo=wib)
    if (ph, pm) > (hh, mm):
        publish_dt = datetime(for_date.year, for_date.month, for_date.day, ph, pm, tzinfo=wib)
    else:
        nxt = for_date + timedelta(days=1)
        publish_dt = datetime(nxt.year, nxt.month, nxt.day, ph, pm, tzinfo=wib)
    return for_date, create_dt, publish_dt


def _auto_shorts_state_load():
    """State dedupe {yt_id: {for_date, status, ...}} — file khusus, BUKAN
    monitor_state.json (dipakai operator Telegram) supaya tidak bentrok."""
    try:
        if os.path.exists(AUTO_SHORTS_STATE_FILE):
            with open(AUTO_SHORTS_STATE_FILE, 'r') as f:
                d = json.load(f)
            if isinstance(d, dict):
                return d
    except Exception:
        pass
    return {}


def _auto_shorts_state_save(state):
    try:
        tmp = AUTO_SHORTS_STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f)
        os.replace(tmp, AUTO_SHORTS_STATE_FILE)
    except Exception as e:
        print(f"Gagal simpan auto_shorts_state: {e}", flush=True)


def _auto_shorts_tick(now_wib=None):
    """Satu putaran jadwal otomatis: cek tiap channel yang aktif, buat short
    bila jendelanya jatuh tempo. Satu short per channel per hari jadwal
    (dedupe di auto_shorts_state.json). Error per channel ditangkap supaya
    satu channel gagal tidak mematikan jadwal channel lain. Pembuatan WAJIB
    lewat _create_shorts_muse_task — semua gerbang (stagger, autopilot,
    dijeda, antrean penuh, QC) berlaku persis seperti panel/Telegram."""
    now_wib = now_wib or _wib_now()
    state = _auto_shorts_state_load()
    changed = False
    for channel in list(database_channel):
        yt_id = channel.get('yt_id')
        if not yt_id:
            continue
        try:
            cfg = _auto_shorts_cfg(channel)
            if not cfg['enabled']:
                continue
            for_date, create_dt, publish_dt = _auto_shorts_window(cfg, now_wib)
            key = str(yt_id)
            entry = state.get(key) or {}
            if entry.get('for_date') == for_date.isoformat():
                continue  # sudah dibuat / dilewati pada hari jadwal ini
            if now_wib < create_dt:
                continue  # belum waktunya
            ch_name = channel.get('name', yt_id)
            if now_wib > publish_dt:
                state[key] = {'for_date': for_date.isoformat(), 'status': 'skipped',
                              'reason': 'Lewat jam tayang', 'at': now_wib.isoformat()}
                changed = True
                print(f"[auto-shorts] lewati {ch_name}: sudah lewat jam tayang {cfg['publish_time']}", flush=True)
                continue
            # --- dalam jendela [create_time, publish_time]: coba buat ---
            # (Galeri kosong TIDAK lagi dilewati: _create_shorts_muse_task
            # membuat gradien otomatis sekali; alert hanya bila itu gagal.)
            if _channel_paused(yt_id):
                state[key] = {'for_date': for_date.isoformat(), 'status': 'skipped',
                              'reason': 'Channel dijeda', 'at': now_wib.isoformat()}
                changed = True
                print(f"[auto-shorts] lewati {ch_name}: channel dijeda", flush=True)
                continue
            title, hook, script, cta = _auto_shorts_pick_content(channel, cfg['theme'], for_date)
            res = _create_shorts_muse_task({
                'yt_id': yt_id, 'title': title, 'hook': hook, 'script': script,
                'cta': cta, 'duration_sec': cfg['duration_sec'],
                'publish_date': publish_dt.strftime('%Y-%m-%d %H:%M'),
                'text_color': '#FFFFFF', 'bg_file': '', 'bgm_file': 'none',
                'description': '', 'tags': '#shorts', 'theme': cfg['theme'],
            })
            if not res.get('ok'):
                reason = res.get('message', 'Gagal membuat task')
                state[key] = {'for_date': for_date.isoformat(), 'status': 'skipped',
                              'reason': reason, 'at': now_wib.isoformat()}
                changed = True
                print(f"[auto-shorts] gagal {ch_name}: {reason}", flush=True)
                if 'background' in reason.lower():
                    send_tg_message(f"⏰ *Jadwal Otomatis Short — gagal dibuat* 😅\n📺 Channel: *{_tg_escape(ch_name)}*\nGaleri background kosong dan background otomatis gagal dibuat — upload background dulu di panel (Asset Gallery), besok saya coba lagi 🙏")
                continue
            state[key] = {'for_date': for_date.isoformat(), 'status': 'created',
                          'task_id': res.get('task_id'),
                          'publish_date': res.get('publish_date'),
                          'title': title, 'at': now_wib.isoformat()}
            changed = True
            if res.get('preview'):
                threading.Thread(target=_send_approval_previews,
                                 args=(res['batch_id'], res['channel_name'], [res['preview']]), daemon=True).start()
            status_txt = 'menunggu approval Bos di Telegram 👀' if not res.get('auto_mode') else 'sudah masuk antrean render ✅'
            if res.get('bg_auto'):
                bg_note = ("\n🖼️ Background: gradien otomatis (galeri masih kosong — "
                           "upload gambar sendiri di panel bila mau yang lebih bagus)")
            elif res.get('bg_video'):
                bg_note = (f"\n🎬 Background: video ({res.get('bg_video_count', 0)} klip, disambung)")
            else:
                bg_note = ""
            send_tg_message(
                f"⏰ *Jadwal Otomatis Short — sudah saya buatkan* 🤖\n"
                f"📺 Channel: *{_tg_escape(ch_name)}*\n"
                f"🎬 Judul: *{_tg_escape(title)}*\n"
                f"🗓️ Tayang: `{_tg_escape(res.get('publish_date') or publish_dt.strftime('%Y-%m-%d %H:%M'))}`\n"
                f"Status: {status_txt}{bg_note}")
        except Exception as e:
            print(f"[auto-shorts] error channel {yt_id}: {e}", flush=True)
            continue
    if changed:
        _auto_shorts_state_save(state)


def auto_shorts_scheduler_loop():
    """Loop latar: bangun tiap 60 detik, cek jadwal yang jatuh tempo.
    Tick pertama langsung saat start (supaya restart di tengah jendela
    tetap membuat short hari itu, selama belum lewat jam tayang)."""
    try:
        _auto_shorts_tick()
    except Exception as e:
        print(f"[auto-shorts] tick awal gagal: {e}", flush=True)
    while True:
        time.sleep(60)
        try:
            _auto_shorts_tick()
        except Exception as e:
            print(f"[auto-shorts] tick gagal: {e}", flush=True)


threading.Thread(target=auto_shorts_scheduler_loop, daemon=True).start()

# 💾 BACKUP OTOMATIS: scheduler harian (cek tiap 60 detik, jam WIB).
threading.Thread(target=_backup_daily_loop, daemon=True).start()

# --- Endpoint Telegram Settings ---
@app.route('/api/save_telegram', methods=['POST'])
def save_telegram():
    data = request.get_json(silent=True) or {}
    bot_config = load_bot_config()
    # 🔒 #11: token kosong = TIDAK BERUBAH (token utuh tidak pernah
    # dikirim ke browser, jadi form tidak bisa mengembalikannya). Hapus
    # token hanya lewat /api/delete_telegram.
    _new_token = (data.get('token') or '').strip()
    if _new_token:
        bot_config['tg_token'] = _new_token
    elif not bot_config.get('tg_token'):
        return jsonify({"status": "error", "message": "Token Telegram wajib diisi."}), 400
    bot_config['tg_chat_id'] = (data.get('chat_id') or '').strip()

    _save_bot_config(bot_config)

    # Tes kirim pesan saat disimpan
    if bot_config['tg_token'] and bot_config['tg_chat_id']:
        try:
            requests.post(
                f"https://api.telegram.org/bot{bot_config['tg_token']}/sendMessage", 
                json={"chat_id": bot_config['tg_chat_id'], "text": "Ziqva Muse Bot: Notifikasi Telegram berhasil dihubungkan!"}
            )
        except: 
            pass
            
    return jsonify({"status": "success", "message": "Konfigurasi Telegram disimpan!"})

@app.route('/api/get_telegram', methods=['GET'])
def get_telegram():
    # 🔒 #11: token bot TIDAK PERNAH dikirim ke browser — hanya status
    # tersimpan + masking (•••• + 4 digit terakhir) untuk tampilan.
    bot_config = load_bot_config()
    _tok = bot_config.get('tg_token', '')
    return jsonify({
        "configured": bool(_tok),
        "token_masked": _mask_token(_tok),
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
        _save_bot_config(bot_config)
        return jsonify({"status": "success", "message": "Konfigurasi Telegram dihapus!"})
    except Exception as e:
        print(f"Gagal hapus konfigurasi Telegram: {e}", flush=True)
        return jsonify({"status": "error", "message": "Gagal menghapus konfigurasi Telegram."})

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
    queue_put(t["blueprint"], allow_overflow=True)

# 🔥 SISTEM PENYAPU OTOMATIS: Bersihkan hantu sebelum masuk antrean
valid_yt_ids = {c['yt_id'] for c in database_channel}
tasks_to_keep = []

for t in active_tasks:
    # 🔥 v1.3.33 (audit M15): "Gagal Upload, Antre Ulang" BUKAN final —
    # task itu harus masuk antrean lagi, bukan dianggap selesai.
    is_final = ("Antre Ulang" not in t['status'] and
                ("Selesai" in t['status'] or "Sukses" in t['status']
                 or "Gagal" in t['status'] or "Dibatalkan" in t['status']))
    
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

# 🔥 v1.3.33 (audit M11): job playlist yatim — 'downloading'/'preparing'/
# 'queued' yang task-nya tidak ada lagi di active (mati saat restart
# panel) tidak pernah pulih dan retry-nya ditolak 409 selamanya.
# Tandai failed supaya bisa dibuat ulang.
try:
    with db_lock:
        _pl_jobs = _playlist_jobs_load()
        _active_pj = {(_t.get('blueprint') or {}).get('playlist_job_id')
                      for _t in active_tasks if isinstance(_t, dict)}
        _healed = 0
        for _j in _pl_jobs.get('jobs', []):
            if isinstance(_j, dict) and _j.get('status') in ('downloading', 'preparing', 'queued') \
                    and _j.get('job_id') not in _active_pj:
                _j['status'] = 'failed'
                _j['error'] = 'Terputus saat restart panel — silakan buat ulang.'
                _j['updated_at'] = _wib_now().strftime('%Y-%m-%d %H:%M')
                _healed += 1
        if _healed:
            _playlist_jobs_save(_pl_jobs)
            print(f"[startup] {_healed} job playlist yatim ditandai gagal", flush=True)
except Exception as _e:
    print(f"[startup] heal job playlist gagal: {_e}", flush=True)

# 🔄 Self-heal status update yang macet di 'running' (updater mati sebelum
# sempat menulis hasil — mis. ikut terbunuh saat service restart). Harus
# jalan tiap start, dan TIDAK BOLEH menggagalkan startup apa pun yang terjadi.
try:
    _heal_stale_update_status()
except Exception:
    pass

# 🔑 Pasang kunci SSH publik Muse (muse-ssh.pub di repo) bila belum ada —
# jalan tiap start, TIDAK BOLEH menggagalkan startup apa pun yang terjadi.
try:
    _ensure_muse_ssh_key()
except Exception:
    pass

startup_sanitizer()
# 🔥 FASE 5: bersih-bersih file lama saat start (aturan umur di cleanup_old_files).
try:
    _n, _b = cleanup_old_files()
    if _n:
        print(f"🧹 [Fase 5] Startup: {_n} file lama dibersihkan ({_b / 1024 ** 2:.0f} MB).", flush=True)
except Exception:
    pass
# Riwayat >90 hari dibuang saat start (auto-cleanup, lihat _prune_old_history).
try:
    _prune_old_history()
except Exception:
    pass
send_tg_message("🚀 *Bos, saya balik lagi!* 😎\n"
                f"Antrean: {len(active_tasks)} task | Riwayat: {len(history_tasks)} task — saya lanjutkan semua 💪")

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=8000, debug=False, use_reloader=False, threaded=True)
