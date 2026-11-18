import os
import re
import sys
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
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
CHANNELS_FILE = os.path.join(BASE_DIR, 'channels_db.json')
TASKS_FILE = os.path.join(BASE_DIR, 'tasks_db.json')
# 🔥 FASE 1 MONITORING: state dedupe (laporan harian & alert) — file runtime,
# JANGAN di-commit (sudah masuk .gitignore). Isinya hanya tanggal + kunci alert.
MONITOR_STATE_FILE = os.path.join(BASE_DIR, 'monitor_state.json')
# Batas upload harian per channel — cerminan MAX_UPLOADS_PER_CHANNEL_PER_DAY di app.py.
MAX_UPLOADS_PER_DAY = 6
DAILY_REPORT_HOUR_WIB = 7  # laporan harian otomatis tiap pagi 07:00 WIB
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
    try:
        with open(TASKS_FILE, 'r') as f:
            data = json.load(f)
            if isinstance(data, list):
                return {"active": data, "history": []}
            return data
    except Exception:
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
        data.setdefault("alerts", {})
        data.setdefault("make_drafts", {})
        return data
    except Exception:
        return {"daily_report_date": "", "alerts": {}, "make_drafts": {}}

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
# 🎬 BUAT VIDEO LEWAT TELEGRAM — DRAFT PERCAKAPAN
# ==========================================
# Draft alur /buatshort & /buatlong disimpan di monitor_state.json
# (kunci "make_drafts", per chat_id) supaya tahan restart. Draft yang
# tidak ada kabar > 30 menit dianggap hangus.
_MAKE_DRAFT_TTL = 1800  # 30 menit (detik)
_BG_EXTS = ('.mp4', '.jpg', '.jpeg', '.png', '.gif', '.webp', '.mov')
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

def check_panel_online():
    """Cek proses panel web (port 8000) dari dalam VPS. Redirect ke /login
    (302) tetap dihitung online — yang penting prosesnya menjawab."""
    try:
        res = requests.get(PANEL_URL + '/', timeout=5)
        return res.status_code in (200, 302)
    except Exception:
        return False

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
        "🤖 *Operator Telegram:* 🟢 Online",
        f"⚡ *CPU:* `{stats['cpu']}%`  🧠 *RAM:* `{stats['ram_used_gb']} GB / {stats['ram_total_gb']} GB ({stats['ram_pct']}%)`",
        f"💾 *Sisa Disk:* `{stats['disk_free_gb']} GB`",
        "━━━━━━━━━━━━━━━━━━━━━━",
    ]
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
            lines.append(f"• *{_tg_escape(name)}* — {conn}{cd_txt}{pause_txt}")
            lines.append(f"  Upload hari ini: `{used}/{MAX_UPLOADS_PER_DAY}` (sisa `{left}`) • Stok: {stock_txt}")
        lines.append(f"📈 *Total sisa kuota hari ini:* `{total_left} video`")
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏭 *Antrean aktif:* `{len(active)} task` (`{in_progress}` sedang berjalan/menunggu)",
        f"⏳ *Menunggu approval:* `{len(pending)} task`" + (" — cek pesan preview di atas, atau setujui dari panel." if pending else ""),
        f"✅ *Upload sukses hari ini:* `{ok_today}`",
        f"❌ *Gagal hari ini:* `{fail_today}`",
        f"📁 *Riwayat:* `{len(history)} task`",
    ]
    # Penutup berbasis data: jangan klaim aman kalau ada yang gagal/mati/pending.
    if fail_today > 0 or not panel_ok or pending:
        lines.append("Bos, ada yang perlu dicek/diputuskan, lihat di atas ya 👀")
    else:
        lines.append("Aman Bos, saya yang urus 💪")
    return "\n".join(lines)

def build_daily_report():
    """Rekap pagi: hasil KEMARIN (WIB), antrean tersisa, stok tiap channel."""
    tasks_data = load_tasks()
    active = tasks_data.get('active', [])
    history = tasks_data.get('history', [])
    channels = load_channels()
    yday = _wib_yesterday_str()
    ok, fail, ok_titles, fail_titles = _count_history_for_date(history, yday)

    # Sapaan pagi 1x sehari (persona asisten pribadi) — di depan laporan,
    # info penting tetap di bawahnya, tidak tertunda.
    lines = [
        "Pagi Bos! ☀️ Gimana kabarnya hari ini, sehat kan? 😊",
        "",
        "🌅 *LAPORAN HARIAN BUAT BOS* ☀️",
        f"Rekap {_wib_now().strftime('%d %b %Y')} (hasil kemarin, {yday})",
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"✅ *Upload sukses kemarin:* `{ok}`",
        f"❌ *Gagal kemarin:* `{fail}`",
    ]
    if fail_titles:
        lines.append("Gagal: " + "; ".join("• " + _tg_escape(t) for t in fail_titles[:5]))
    if ok_titles:
        lines.append("Sukses: " + "; ".join("• " + _tg_escape(t) for t in ok_titles[:5]))
    lines += [
        "━━━━━━━━━━━━━━━━━━━━━━",
        f"🏭 *Antrean tersisa:* `{len(active)} task`",
    ]
    for t in active[:5]:
        lines.append(f"• {_tg_escape(t.get('title', '?')[:45])} — {_tg_escape(t.get('status', '-')[:40])}")
    if len(active) > 5:
        lines.append(f"_...dan {len(active) - 5} task lainnya._")
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
    else:
        lines.append("\nKemarin lagi sepi Bos, nggak ada yang tayang 😌")
    lines.append("Bos, ketik /status kapan aja kalau mau cek kondisi terkini 😎")
    return "\n".join(lines)

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
                            "video_id": vid})
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
    """Kuota upload: per project Google (batas 6/hari/project) + per channel."""
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
        du = ch.get('daily_uploads') or {}
        if du.get('date') == today:
            e["used"] += int(du.get('count', 0))
    lines.append("☁️ *Per Project Google* (batas 6 upload/hari/project):")
    for pid in sorted(projects):
        e = projects[pid]
        used = e["used"]
        left = max(0, MAX_UPLOADS_PER_DAY - used)
        names = ", ".join(_channel_display_name(c) for c in e["channels"][:4])
        cd = any((c.get('cooldown_until') or 0) > time.time() for c in e["channels"])
        lines.append(f"• Project `{_tg_escape(pid)}`: `{used}/{MAX_UPLOADS_PER_DAY}` (sisa `{left}`)"
                     + (" ⏸️ cooldown sampai tengah malam" if cd else ""))
        lines.append(f"  Channel: {_tg_escape(names)}")
        if len(e["channels"]) > 4:
            lines.append(f"  _...dan {len(e['channels']) - 4} channel lainnya._")
    lines.append("━━━━━━━━━━━━━━━━━━━━━━")
    lines.append("📺 *Per channel hari ini:*")
    for ch in channels[:8]:
        name = _channel_display_name(ch)
        used, left = get_channel_quota(ch)
        pause = " ⏸️ Dijeda" if ch.get('paused') else ""
        lines.append(f"• *{_tg_escape(name)}*: `{used}/{MAX_UPLOADS_PER_DAY}` (sisa `{left}`){pause}")
    if len(channels) > 8:
        lines.append(f"_...dan {len(channels) - 8} channel lainnya._")
    lines.append("Bos, kalau kuota project udah 6/6, upload baru nunggu reset tengah malam WIB ya 😌")
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
    for t in history[:8]:
        date = (t.get('finished_date') or (t.get('publish_date') or '')[:10] or '-')[:10]
        title = (t.get('title') or '?')[:45]
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
        lines.append(f"{icon} `{_tg_escape(date)}` — {_tg_escape(title)}{ch_txt}")
        lines.append(f"   Status: {_tg_escape(st[:60])}")
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
            lines.append(f"  🏆 Terbaik: {_tg_escape(best['title'][:45])} ({_fmt_num(best['views'])} views)")
            if len(vids) > 1:
                worst = vids[-1]
                lines.append(f"  📉 Terendah: {_tg_escape(worst['title'][:45])} ({_fmt_num(worst['views'])} views)")
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
    if failed_hist:
        lines.append(f"Di riwayat: `{len(failed_hist)} task` gagal.")
        for t in failed_hist[:5]:
            title = (t.get('title') or '?')[:45]
            st = str(t.get('status', ''))[:60]
            date = (t.get('finished_date') or '')[:10] or '-'
            lines.append(f"• `{_tg_escape(date)}` {_tg_escape(title)}")
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
                lines.append(f"  • {dt.strftime('%d %b %H:%M')} — {_tg_escape((t.get('title') or '?')[:40])}")
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
                lines.append(f"  • `{dt.strftime('%H:%M')}` {_tg_escape((t.get('title') or '?')[:40])} — {label}")
        lines.append("")
    # Task yang channel-nya tidak dikenal (mis. channel baru dihapus).
    unknown = [(dt, t) for dt, t in upcoming + overdue if _task_yt(t) not in known_ids]
    if unknown:
        total += len(unknown)
        lines.append("📺 *Channel Tidak Dikenal*")
        for dt, t in sorted(unknown, key=lambda x: x[0])[:5]:
            label = _task_schedule_label(t)
            lines.append(f"  • `{dt.strftime('%d %b %H:%M')}` {_tg_escape((t.get('title') or '?')[:40])} — {label}")
        lines.append("")
    lines.append(f"Total: `{total} task` dalam 7 hari ke depan (termasuk yang terlambat).")
    lines.append("Bos, ketik /status buat cek kuota & antrean terkini 😎")
    return "\n".join(lines)

def _tg_escape(text):
    """Escape karakter Markdown Telegram supaya nama channel / judul video
    (yang bisa mengandung *, _, [, ] dll.) tidak merusak format pesan.
    Hanya bagian DATA yang di-escape — template pesan yang memang memakai
    *tebal* / `kode` tetap ditulis langsung. (#23)"""
    text = str(text or '')
    for ch in ['\\', '_', '*', '[', ']', '(', ')', '~', '`', '>', '#', '+', '-', '=', '|', '{', '}', '.', '!']:
        text = text.replace(ch, '\\' + ch)
    return text

def get_main_keyboard():
    return {
        "keyboard": [
            [{"text": "📊 Status Server"}, {"text": "🎯 Kuota Upload"}],
            [{"text": "📺 Cek Channel"}, {"text": "📂 Antrean Render"}],
            [{"text": "🗓️ Jadwal 7 Hari"}, {"text": "📈 Statistik Video"}],
            [{"text": "💰 Monetisasi"}, {"text": "❌ Task Gagal"}],
            [{"text": "💡 10 Judul Sleep"}, {"text": "🎬 Ide Shorts"}],
            [{"text": "📝 Template Deskripsi"}, {"text": "📐 Blueprint Resep"}],
            [{"text": "🎬 Buat Short"}, {"text": "🎥 Buat Video Long"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }

def send_message(token, chat_id, text, parse_mode="Markdown", reply_markup=None):
    # Pengaman batas 4096 karakter Telegram — pesan kepanjangan dipotong rapi.
    text = _clip(str(text or ''))
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup is not None:
        payload["reply_markup"] = reply_markup
    else:
        payload["reply_markup"] = get_main_keyboard()
        
    try:
        res = requests.post(url, json=payload, timeout=15)
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
    dibaca fresh dari config.json — TIDAK pernah dikirim ke chat."""
    config = load_config()
    key = config.get('operator_key', '')
    if not key:
        return False, "Kunci operator belum siap (panel mungkin baru restart). Coba lagi sebentar lagi."
    try:
        res = requests.post(
            PANEL_URL + '/api/operator/retry_task',
            json={"task_id": task_id, "key": key},
            timeout=20
        )
        data = res.json()
        if res.status_code == 200 and data.get('status') == 'success':
            return True, data.get('message', 'Task kembali ke antrean render.')
        return False, data.get('message', f"Panel menjawab HTTP {res.status_code}.")
    except Exception as e:
        return False, f"Tidak bisa menghubungi panel: {e}"

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
            ok, info = request_retry(task_id)
            if ok:
                send_message(token, chat_id,
                             f"✅ *Berhasil Bos, retry jalan!* 💪\nTask #{task_id} udah masuk antrean render lagi.\n_{info}_")
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
    # --- BUAT VIDEO: konfirmasi ✅ / batal ❌ dari ringkasan /buatshort & /buatlong ---
    if data in ('mkshort:yes', 'mkshort:no', 'mklong:yes', 'mklong:no'):
        kind = 'short' if data.startswith('mkshort') else 'long'
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
                             "Ketik /buatshort atau /buatlong untuk mulai lagi ya Bos!")
                return
            if not confirm:
                _clear_make_draft(chat_id)
                answer_callback(token, cq_id, "Dibatalkan Bos 😌")
                send_message(token, chat_id,
                             "😌 *Sip Bos, dibatalkan.*\nTidak ada video yang dibuat. "
                             "Ketik /buatshort atau /buatlong kalau mau mulai lagi ya Bos!")
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
                             "Coba lagi dengan /buatshort atau /buatlong ya Bos!")
                return
            _send_make_created_message(token, chat_id, kind, d, resp)
        finally:
            _cb_in_flight.discard(data)
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
    niche_lower = niche.lower()
    templates = []
    
    if "tidur" in niche_lower or "sleep" in niche_lower or "insomnia" in niche_lower or "night" in niche_lower:
        templates = [
            "Fall Asleep in 3 Minutes 😴 Deep Sleep Music, Relaxing Piano & Soft Rain for Insomnia",
            "Delta Waves Sleep Music: Stop Overthinking, Calming Sounds for Deep Relaxation",
            "Bedtime Piano Melodies to Relieve Anxiety, Stress & Mental Exhaustion (Deep Sleep)",
            "Deep Sleep Music 24/7 • Cozy Bedroom Ambience with Gentle Rain Sounds",
            "Instant Relief from Insomnia: 60 Minutes Relaxing Piano for Peaceful Night",
            "Deepest Sleep Ever • 432Hz Miracle Tone & Healing Night Ambience",
            "Calm Your Mind and Fall Asleep Fast • Peaceful Piano & Cozy Rain Sounds",
            "No More Sleepless Nights: Calming Piano Music for Instant Sleep & Recovery",
            "Deep Relaxation & Stress Relief: Soothing Night Lullaby for Restless Minds",
            "Fall Asleep Fast with Soothing Piano & Distant Thunder Ambience",
            "Peaceful Sleep in Minutes • Deep Delta Wave Healing Frequencies 528Hz",
            "Cozy Night Rain in Cabin • Relaxing Instrumental Melodies for Sweet Dreams",
            "Release Stress & Anxiety Instantly • Gentle Piano Melodies for Overthinkers",
            "Sleep Like a Baby • Soothing Piano & Soft White Noise for Deep Rest",
            "Midnight Rest: 80 Minutes of Pure Calming Soundscapes for Insomnia Relief"
        ]
    elif "piano" in niche_lower:
        templates = [
            "Relaxing Piano Music for Deep Sleep 😴 Beautiful Melodies to Calm Your Mind Instantly",
            "Peaceful Piano & Soft Rain: Fall Asleep Fast with Gentle Night Melodies",
            "Calming Piano for Anxiety Relief • Soothing Instrumental for Stress-Free Nights",
            "Beautiful Piano Melodies for Sleep, Meditation & Deep Relaxation (60 Mins)",
            "Midnight Piano: Gentle Keys to Release Stress and Drift into Sweet Dreams",
            "Piano Lullaby for Insomnia • Soft, Slow Melodies for Peaceful Rest",
            "Healing Piano Tones 432Hz • Deep Relaxation Music for Mind and Body",
            "Cozy Evening Piano • Warm Melodies for Reading, Resting & Unwinding",
            "Emotional Piano Collection: Tender Melodies for Quiet Nights Alone",
            "Sleep Piano Radio 🎹 Continuous Calming Melodies All Night Long"
        ]
    elif "hujan" in niche_lower or "rain" in niche_lower:
        templates = [
            "Rain Sounds for Sleeping 😴 Gentle Rain on Roof & Distant Thunder for Deep Sleep",
            "Cozy Rainy Night Ambience • Rain on Window for Instant Relaxation & Sleep",
            "Heavy Rain & Thunderstorm Sounds for Deep Sleep (No Ads, All Night)",
            "Rain in a Cabin in the Woods • Peaceful Night Ambience for Insomnia Relief",
            "Soft Rain & Piano: The Perfect Combination to Fall Asleep in Minutes",
            "Rain Sounds for Focus & Study • Calm Rainy Day Ambience for Deep Work",
            "Midnight Rainstorm • Soothing Rain Sounds to Block Noise & Sleep Better",
            "Gentle Rain on Leaves • Nature Sounds for Meditation & Stress Relief",
            "Rainy Day in Tokyo • Cozy Rain Ambience with Soft City Sounds",
            "Fall Asleep to Rain • 8 Hours of Pure Relaxing Rain Sounds"
        ]
    elif "meditasi" in niche_lower or "meditation" in niche_lower:
        templates = [
            "Guided Meditation Music 🧘 Deep Relaxation & Inner Peace (60 Mins)",
            "Morning Meditation: Calm Your Mind, Breathe Slowly & Start Fresh",
            "Healing Meditation Music 432Hz • Release Anxiety & Find Stillness",
            "Zen Garden Ambience • Peaceful Meditation Sounds for Mindfulness",
            "Deep Breathing Meditation Music • Slow Tones for Stress Relief",
            "Chakra Healing Meditation • Soothing Sounds for Balance & Energy",
            "Sleep Meditation: Drift into Deep Rest with Gentle Ambient Tones",
            "Mindfulness Meditation Music for Focus, Calm & Emotional Healing",
            "Sunrise Meditation • Soft Ambient Music for a Peaceful Morning",
            "Let Go of Stress: Guided Relaxation Music for Body & Mind"
        ]
    elif "fokus" in niche_lower or "focus" in niche_lower:
        templates = [
            "Deep Focus Music 🎯 Concentration Sounds for Studying, Working & Reading",
            "Focus Flow State • Steady Rhythms to Keep Your Brain in the Zone (60 Mins)",
            "Deep Work Music for Maximum Productivity • No Distractions, Pure Focus",
            "Study Music for Concentration • Calm Instrumental for Exams & Deadlines",
            "Pomodoro Focus Session • 25 Minutes of Pure Concentration Music",
            "Brain Food: Focus Music Backed by Science for Deep Thinking",
            "Concentration Boost • Ambient Focus Sounds for Coding & Writing",
            "Flow State Music: Enter Deep Focus in 5 Minutes (Study & Work)",
            "Morning Focus Ritual • Energizing Yet Calm Music for a Productive Day",
            "Silent Mind, Sharp Focus • Minimal Ambient Music for Deep Work"
        ]
    elif "lofi" in niche_lower or "study" in niche_lower or "chill" in niche_lower:
        templates = [
            "Lofi Hip Hop Radio ☕ Beats to Relax/Study to [Cozy Rain Ambience]",
            "Late Night Study Session • Chill Lofi Beats for Focus & Concentration",
            "Cozy Bedroom Lofi Beats • Smooth Instrumental for Reading & Working",
            "Peaceful Morning Lofi ☀️ Beats to Start Your Productive Day with Joy",
            "Alone in Tokyo Rain • Nostalgic Lofi Chill Beats for Overthinkers",
            "Deep Work Flow • Instrumental Lofi Beats to Maximize Productivity",
            "Coffee Shop Ambience & Chill Lofi Beats for Rainy Afternoons",
            "Sunday Evening Relax • Soft Lofi Beats for Stress Relief & Unwinding",
            "Midnight Coding & Deep Focus • Minimal Chillhop Beats 2026",
            "Warm Autumn Lofi • Relaxing Beats to Study, Chill, and Sleep to"
        ]
    else:
        templates = [
            f"Relaxing {niche.title()} Music • Soothing Melodies for Stress Relief & Peace",
            f"Deep Focus & Calm • Beautiful {niche.title()} Instrumental Collection",
            f"Peaceful Mind: Calming {niche.title()} Sounds for Instant Relaxation",
            f"Healing & Recharging: {niche.title()} Ambience for Restful Days",
            f"Serenity Now • Gentle {niche.title()} Melodies to Refresh Your Soul",
            f"Morning Harmony • Uplifting {niche.title()} Music for Positive Energy",
            f"End of Day Reflection • Soft {niche.title()} Instrumental for Relaxing",
            f"Quiet Moments: {niche.title()} Soundscapes to Melt Stress Away",
            f"Deep Relaxation 2026 • Peaceful {niche.title()} Compilation",
            f"Comfort & Calm • Pure Acoustic {niche.title()} for Study and Work"
        ]
        
    while len(templates) < count:
        templates.append(f"Ultimate {niche.title()} Relaxation Vol. {len(templates)+1} (60 Mins)")
    return templates[:count]

# ==========================================
# 🔥 PERLUASAN KONTEN — NICHE, HASHTAG, CTA, IDE SHORTS
# Bank konten statis (bukan data live): judul/deskripsi/tag/CTA/ide Shorts
# per niche. Niche yang dikenal: sleep, lofi, piano, hujan, meditasi, fokus.
# ==========================================
_NICHES = {
    'sleep': 'Deep Sleep Music',
    'lofi': 'Lofi Study',
    'piano': 'Piano',
    'hujan': 'Hujan',
    'meditasi': 'Meditasi',
    'fokus': 'Fokus',
}


def _parse_niche_arg(clean_cmd, parts, default='sleep'):
    """(kunci_niche, label) dari kata kunci di pesan. Urutan penting: yang
    spesifik dulu supaya 'fokus' tidak ketangkap cabang 'lofi'."""
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
    "buat short", "buat video long",
}

def _make_short_summary(d):
    hook = (d.get('hook') or '—')
    script = (d.get('script') or '—')
    return (
        "📋 *Cek dulu ya Bos, ini Shorts yang mau dibuat:* 🎬\n"
        f"📺 Channel: *{_tg_escape(d.get('channel_name', ''))}*\n"
        f"🎬 Judul: *{_tg_escape(d.get('title', ''))}*\n"
        f"🪝 Hook (4 detik): {_tg_escape(hook[:150])}{'…' if len(hook) > 150 else ''}\n"
        f"📝 Naskah: {_tg_escape(script[:200])}{'…' if len(script) > 200 else ''}\n"
        f"📣 CTA: {_tg_escape(d.get('cta', 'Subscribe'))}\n"
        f"⏱️ Durasi: `{d.get('duration_sec', 30)} detik`\n"
        f"🗓️ Jadwal: `{_tg_escape(d.get('publish_date', ''))}`\n"
        f"🖼️ Background: `{_tg_escape(d.get('bg_file', ''))}`\n"
        f"🎵 Musik: `{_tg_escape(d.get('bgm_file', 'none'))}`\n\n"
        "Kalau sudah oke, pencet ✅ Buat. Kalau mau ubah, pencet ❌ Batal lalu mulai lagi dari /buatshort ya Bos!"
    )

def _make_long_summary(d):
    lines = [
        "📋 *Cek dulu ya Bos, ini batch video long yang mau dibuat:* 🎥",
        f"📺 Channel: *{_tg_escape(d.get('channel_name', ''))}*",
        f"🎬 Jumlah: `{d.get('count', 1)} video` (durasi tiap video diacak 60–80 menit)",
        f"🗓️ Mulai: `{_tg_escape(d.get('start_date', ''))}` • Jeda antar video: `{d.get('interval_days', 1)} hari`",
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

def _make_pick_channel(channels):
    """Kembalikan (channel_terpilih_atau_None, pesan_pilih_atau_None).
    1 channel → langsung pakai; >1 → minta pilih nomor."""
    if not channels:
        return None, None
    if len(channels) == 1:
        return channels[0], None
    lines = ["📺 *Bos punya lebih dari 1 channel nih, pilih dulu ya:*\n"]
    for i, c in enumerate(channels, 1):
        mark = " ⏸️ Dijeda" if c.get('paused') else ""
        lines.append(f"{i}. {_tg_escape(_channel_display_name(c))}{mark}")
    lines.append("\nBalas dengan angkanya saja (mis. `1`).")
    return None, "\n".join(lines)

def _make_check_bg_or_cancel(token, chat_id, draft):
    """Cek galeri background channel untuk Shorts. Bila kosong → batalkan
    draft dengan pesan jelas. Kembalikan True bila boleh lanjut."""
    d = draft.get('data') or {}
    bgs = _gallery_files(d.get('yt_id', ''), 'backgrounds', _BG_EXTS)
    if not bgs:
        _clear_make_draft(chat_id)
        send_message(token, chat_id,
                     "😅 *Yah Bos, galerinya masih kosong* — Shorts butuh 1 gambar background dulu.\n\n"
                     "Upload background dulu di panel (Asset Gallery), lalu ketik /buatshort lagi ya Bos! 🙏")
        return False
    d['bg_options'] = bgs
    draft['data'] = d
    return True

def _make_ask_step(token, chat_id, draft):
    """Kirim pertanyaan untuk langkah draft saat ini."""
    d = draft.get('data') or {}
    step = draft.get('step')
    kind = draft.get('kind')
    if step == 'channel':
        _ch, msg = _make_pick_channel(load_channels())
        if msg:
            send_message(token, chat_id, msg)
        return
    if kind == 'short':
        if step == 'title':
            send_message(token, chat_id,
                         "🎬 *Buat Short — langkah 1 dari 7: Judul*\n\nKirim judul Shorts-nya, Bos. (mis. `Musik Tidur Nyenyak 30 Detik`)")
        elif step == 'hook':
            send_message(token, chat_id,
                         "🪝 *Langkah 2 dari 7: Hook (4 detik pertama)*\n\nKirim kalimat pembuka yang bikin orang berhenti scroll.\nKetik `lewati` kalau mau kosongkan (nanti pakai naskah saja).")
        elif step == 'script':
            send_message(token, chat_id,
                         "📝 *Langkah 3 dari 7: Naskah*\n\nKirim naskah Shorts-nya (ditampilkan berurutan di video).\nKetik `lewati` kalau sudah cukup dengan Hook saja.")
        elif step == 'cta':
            send_message(token, chat_id,
                         "📣 *Langkah 4 dari 7: CTA (kartu akhir 5 detik)*\n\nKirim teks ajakannya (mis. `Subscribe`).\nKetik `lewati` untuk pakai CTA standar (`Subscribe`).")
        elif step == 'duration':
            send_message(token, chat_id,
                         "⏱️ *Langkah 5 dari 7: Durasi*\n\nBalas `15`, `30`, atau `60` (detik).\nKetik `lewati` untuk 30 detik.")
        elif step == 'publish':
            nxt = _next_slot_for_channel(next((c for c in load_channels()
                                              if c.get('yt_id') == d.get('yt_id')), None))
            send_message(token, chat_id,
                         "🗓️ *Langkah 6 dari 7: Jadwal tayang*\n\nKirim tanggal & jam (format `YYYY-MM-DD HH:MM`, mis. `2026-11-20 19:00`).\n"
                         f"Ketik `lewati` untuk jadwal berikutnya (`{nxt}`).")
        elif step == 'bg':
            opts = d.get('bg_options') or []
            lines = ["🖼️ *Langkah 7 dari 7: Background*\n\nPilih gambar/video background dari galeri:\n"]
            for i, f in enumerate(opts[:10], 1):
                lines.append(f"{i}. `{_tg_escape(f)}`")
            if len(opts) > 10:
                lines.append(f"_…dan {len(opts) - 10} file lainnya_")
            lines.append("\nBalas angkanya. Ketik `lewati` untuk pakai yang pertama.")
            send_message(token, chat_id, "\n".join(lines))
        elif step == 'bgm':
            opts = _gallery_files(d.get('yt_id', ''), 'audios', _AUDIO_EXTS)
            d['bgm_options'] = opts
            _save_make_draft(chat_id, draft)
            if not opts:
                d['bgm_file'] = 'none'
                draft['step'] = 'confirm'
                _save_make_draft(chat_id, draft)
                send_message(token, chat_id, _make_short_summary(d),
                             reply_markup={"inline_keyboard": [
                                 [{"text": "✅ Buat", "callback_data": "mkshort:yes"},
                                  {"text": "❌ Batal", "callback_data": "mkshort:no"}]]})
                return
            lines = ["🎵 *Terakhir: Musik latar*\n\nPilih musik dari galeri (atau `0` untuk tanpa musik):\n"]
            for i, f in enumerate(opts[:10], 1):
                lines.append(f"{i}. `{_tg_escape(f)}`")
            if len(opts) > 10:
                lines.append(f"_…dan {len(opts) - 10} file lainnya_")
            lines.append("\nBalas angkanya, `0` tanpa musik, atau `lewati` untuk tanpa musik.")
            send_message(token, chat_id, "\n".join(lines))
        elif step == 'confirm':
            send_message(token, chat_id, _make_short_summary(d),
                         reply_markup={"inline_keyboard": [
                             [{"text": "✅ Buat", "callback_data": "mkshort:yes"},
                              {"text": "❌ Batal", "callback_data": "mkshort:no"}]]})
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
                         "🗓️ *Langkah 3 dari 4: Tanggal mulai*\n\nKirim tanggal & jam video pertama (format `YYYY-MM-DD HH:MM`, mis. `2026-11-20 19:00`).\n"
                         "Tanggal saja juga boleh (jam jadi 19:00). Ketik `lewati` untuk besok jam 19:00.")
        elif step == 'interval':
            send_message(token, chat_id,
                         "⏳ *Langkah 4 dari 4: Jeda antar video*\n\nBerapa hari jeda tiap video? Balas angkanya (mis. `1`).\nKetik `lewati` untuk 1 hari.")
        elif step == 'confirm':
            send_message(token, chat_id, _make_long_summary(d),
                         reply_markup={"inline_keyboard": [
                             [{"text": "✅ Buat", "callback_data": "mklong:yes"},
                              {"text": "❌ Batal", "callback_data": "mklong:no"}]]})

def _short_next_step(d):
    """Langkah pertama yang masih kosong untuk Short (dipakai lompatan
    sekali kirim — bagian yang sudah diisi tidak ditanya lagi)."""
    if not d.get('title'):
        return 'title'
    if not d.get('hook') and not d.get('script'):
        return 'hook'
    if not d.get('cta'):
        return 'cta'
    if not d.get('duration_sec'):
        return 'duration'
    if not d.get('publish_date'):
        return 'publish'
    if not d.get('bg_file'):
        return 'bg'
    if not d.get('bgm_file'):
        return 'bgm'
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
    ch, ask_msg = _make_pick_channel(channels)
    draft = {"kind": "short", "step": "title", "data": dict(pre)}
    if ch is None:
        draft['step'] = 'channel'
        _save_make_draft(chat_id, draft)
        send_message(token, chat_id,
                     "🎬 *Siap Bos, kita buat Short bareng-bareng ya!* 😎\n"
                     "Jawab tiap langkah satu per satu — ketik /batal kapan saja untuk berhenti.\n\n" + ask_msg)
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
                 "Jawab tiap langkah satu per satu — ketik /batal kapan saja untuk berhenti.")
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
            parsed = _parse_make_date(parts[1])
            if parsed:
                pre['start_date'] = parsed
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

def _handle_make_answer(token, chat_id, draft, raw_cmd):
    """Proses SATU jawaban dalam alur terpandu. Kembalikan True bila
    pesan ditangani sebagai jawaban (bukan perintah lain)."""
    d = draft.get('data') or {}
    step = draft.get('step')
    kind = draft.get('kind')
    ans = (raw_cmd or '').strip()
    low = ans.lower()
    if step == 'channel':
        channels = load_channels()
        try:
            idx = int(ans) - 1
            ch = channels[idx] if 0 <= idx < len(channels) else None
        except Exception:
            ch = None
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
        else:
            draft['step'] = _long_next_step(d)
        _save_make_draft(chat_id, draft)
        _make_ask_step(token, chat_id, draft)
        return True
    if kind == 'short':
        if step == 'title':
            if not ans:
                send_message(token, chat_id, "😅 *Judulnya masih kosong Bos* — kirim judul Shorts-nya ya.")
                return True
            d['title'] = ans[:200]
            draft['step'] = 'hook'
        elif step == 'hook':
            d['hook'] = '' if low in ('lewati', '-', 'skip') else ans[:500]
            draft['step'] = 'script'
        elif step == 'script':
            d['script'] = '' if low in ('lewati', '-', 'skip') else ans[:2000]
            if not (d.get('hook') or d.get('script')):
                send_message(token, chat_id,
                             "😅 *Hook dan Naskah tidak boleh dua-duanya kosong Bos* — "
                             "isi salah satunya ya. Kirim Hook-nya dulu:")
                draft['step'] = 'hook'
                _save_make_draft(chat_id, draft)
                return True
            draft['step'] = 'cta'
        elif step == 'cta':
            d['cta'] = 'Subscribe' if low in ('lewati', '-', 'skip') else ans[:100]
            draft['step'] = 'duration'
        elif step == 'duration':
            if low in ('lewati', '-', 'skip'):
                d['duration_sec'] = 30
            elif ans in ('15', '30', '60'):
                d['duration_sec'] = int(ans)
            else:
                send_message(token, chat_id,
                             "😅 *Durasi harus 15, 30, atau 60 detik Bos* — balas salah satunya ya.")
                return True
            draft['step'] = 'publish'
        elif step == 'publish':
            if low in ('lewati', '-', 'skip'):
                ch = next((c for c in load_channels() if c.get('yt_id') == d.get('yt_id')), None)
                d['publish_date'] = _next_slot_for_channel(ch)
            else:
                parsed = _parse_make_date(ans)
                if not parsed:
                    send_message(token, chat_id,
                                 "😅 *Format tanggalnya salah Bos* — pakai `YYYY-MM-DD HH:MM` (mis. `2026-11-20 19:00`).")
                    return True
                d['publish_date'] = parsed
            draft['step'] = 'bg'
        elif step == 'bg':
            opts = d.get('bg_options') or []
            if low in ('lewati', '-', 'skip'):
                d['bg_file'] = opts[0] if opts else ''
            else:
                try:
                    idx = int(ans) - 1
                    picked = opts[idx] if 0 <= idx < len(opts) else None
                except Exception:
                    picked = None
                if not picked:
                    send_message(token, chat_id,
                                 f"😅 *Pilihannya nggak ada Bos* — balas angkanya 1–{len(opts)} ya.")
                    return True
                d['bg_file'] = picked
            draft['step'] = 'bgm'
        elif step == 'bgm':
            opts = d.get('bgm_options') or []
            if low in ('lewati', '-', 'skip', '0', 'tanpa musik', 'none'):
                d['bgm_file'] = 'none'
            else:
                try:
                    idx = int(ans) - 1
                    picked = opts[idx] if 0 <= idx < len(opts) else None
                except Exception:
                    picked = None
                if not picked:
                    send_message(token, chat_id,
                                 f"😅 *Pilihannya nggak ada Bos* — balas angkanya 1–{len(opts)}, atau `0` tanpa musik.")
                    return True
                d['bgm_file'] = picked
            draft['step'] = 'confirm'
        elif step == 'confirm':
            if low in ('ya', 'buat', '✅', 'ok', 'oke', 'lanjut'):
                _confirm_make_create(token, chat_id, draft)
                return True
            if low in ('batal', 'tidak', '❌', 'cancel'):
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
            parsed = _parse_make_date(ans)
            if not parsed:
                send_message(token, chat_id,
                             "😅 *Format tanggalnya salah Bos* — pakai `YYYY-MM-DD HH:MM` (mis. `2026-11-20 19:00`).")
                return True
            d['start_date'] = parsed
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
        if low in ('ya', 'buat', '✅', 'ok', 'oke', 'lanjut'):
            _confirm_make_create(token, chat_id, draft)
            return True
        if low in ('batal', 'tidak', '❌', 'cancel'):
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
                     "Coba lagi dengan /buatshort atau /buatlong ya Bos!")
        return
    _send_make_created_message(token, chat_id, kind, d, resp)

def handle_user_command(token, chat_id, text):
    try:
        raw_cmd = text.strip()
        # Bersihkan emoji agar teks tombol tombol seperti "📊 Status Server" cocok ke "status server"
        clean_cmd = re.sub(r'[^\w\s/]', '', raw_cmd).strip().lower()
        parts = clean_cmd.split()
        first_word = parts[0] if parts else ""

        print(f"Perintah diproses: '{raw_cmd}' -> clean: '{clean_cmd}'", flush=True)

        # --- 🎬 ALUR BUAT VIDEO (/buatshort & /buatlong): dicek PALING DULU
        # supaya jawaban langkah tidak ketangkap cabang lain (mis. kata
        # "sleep" di jawaban akan ketangkap /judul). /batal membatalkan
        # kapan saja; /start membuka menu (draft ikut dibatalkan); tombol
        # keyboard & perintah "/" lain tetap jalan, draft tersimpan.
        if first_word in ("/batal",) or clean_cmd in ("batal", "cancel"):
            if _load_make_draft(chat_id):
                _clear_make_draft(chat_id)
                send_message(token, chat_id,
                             "😌 *Sip Bos, dibatalkan.* Tidak ada video yang dibuat.")
            else:
                send_message(token, chat_id,
                             "😅 *Tidak ada yang sedang dibuat Bos* — ketik /buatshort atau /buatlong untuk mulai.")
            return
        _draft = _load_make_draft(chat_id)
        if isinstance(_draft, dict) and _draft.get("expired"):
            send_message(token, chat_id,
                         "⌛ *Eh Bos, draft buat videonya sudah hangus (>30 menit tidak ada kabar)* 😅\n"
                         "Ketik /buatshort atau /buatlong untuk mulai lagi ya Bos!")
            return
        if _draft:
            if first_word in ("/start", "/help"):
                _clear_make_draft(chat_id)
            elif first_word in ("/buatshort", "/buatlong") or clean_cmd in ("buat short", "buat video long"):
                _clear_make_draft(chat_id)
                # jatuh ke cabang /buatshort|/buatlong di bawah (mulai baru)
            elif first_word.startswith("/"):
                pass  # perintah lain tetap jalan, draft tersimpan
            elif clean_cmd in _MAIN_BUTTON_TEXTS:
                pass  # tombol keyboard tetap jalan, draft tersimpan
            else:
                _handle_make_answer(token, chat_id, _draft, raw_cmd)
                return

        # 1. MENU / START
        if first_word in ["/start", "/help", "halo", "menu", "hai"]:
            msg = (
                "🤖 *ZIQVA MUSE BOT — CLOUD OPERATOR* 😎\n\n"
                "Halo Bos! Saya siap kerja, tinggal perintah aja. Sentuh tombol di bawah atau klik perintah berikut:\n\n"
                "*📊 Pantau:*\n"
                "👉 /status — Channel, antrean, kuota & kesehatan server\n"
                "👉 /server — Detail mesin: CPU, RAM, disk, uptime, panel\n"
                "👉 /kuota — Kuota upload per project Google & per channel\n"
                "👉 /stok — Sisa stok jadwal & isi galeri per channel\n"
                "👉 /channel — Daftar channel YouTube terhubung\n"
                "👉 /antrean — Video yang sedang diproses\n"
                "👉 /jadwal — Jadwal publish 7 hari ke depan\n"
                "👉 /riwayat — 8 upload terakhir\n"
                "👉 /gagal — Task gagal + tombol Retry\n"
                "👉 /laporan — Laporan harian, kapan saja\n\n"
                "*📈 Hasil:*\n"
                "👉 /statistik — Views/likes/komentar + video terbaik & terburuk\n"
                "👉 /monetisasi — Estimasi penghasilan dari RPM manual\n\n"
                "*💡 Konten:*\n"
                "👉 /judul [niche] [jumlah] — Judul SEO, mis. /judul lofi 15\n"
                "👉 /deskripsi [niche] — Template deskripsi SEO\n"
                "👉 /tag [niche] — Bank hashtag, mis. /tag piano\n"
                "👉 /cta — Ide teks CTA buat Shorts\n"
                "👉 /ideashorts [niche] — Hook + naskah singkat Shorts\n"
                "👉 /blueprint — Resep settingan video\n\n"
                "*🎬 Buat Video:*\n"
                "👉 /buatshort — Buat Shorts Muse terpandu (judul, hook, naskah, CTA)\n"
                "👉 /buatshort Judul | Hook | Naskah | CTA — Sekali kirim\n"
                "👉 /buatlong — Buat batch video long 60–80 menit terpandu\n"
                "👉 /buatlong 3 | 2026-11-20 19:00 | 1 — Sekali kirim (jumlah | tanggal | jeda hari)\n"
                "👉 /batal — Batalkan draft yang sedang dibuat\n\n"
                "Niche: sleep, lofi, piano, hujan, meditasi, fokus."
            )
            send_message(token, chat_id, msg)
            return

        # --- 🎬 BUAT VIDEO (alur terpandu) — dicek sebelum cabang lama
        # supaya "buat short" tidak ketangkap cabang /ideashorts ("shorts").
        if first_word in ("/buatshort",) or clean_cmd in ("buat short",):
            _start_make_short(token, chat_id, raw_cmd)
            return
        if first_word in ("/buatlong",) or clean_cmd in ("buat video long",):
            _start_make_long(token, chat_id, raw_cmd)
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

        # 14. LAPORAN HARIAN (on-demand, pakai builder yang sudah ada)
        if "laporan" in clean_cmd or "report" in clean_cmd:
            send_message(token, chat_id, build_daily_report())
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

        # 3. CHANNEL LIST
        if "channel" in clean_cmd:
            channels = load_channels()
            if not channels:
                msg = (
                    "📺 *DAFTAR CHANNEL YOUTUBE*\n"
                    "Bos, belum ada channel yang terhubung nih 😅\n\n"
                    "👉 Buka browser panel VPS di port 8000 (/factory)\n"
                    "Lalu klik tombol *Connect* buat menautkan channel YouTube pertama."
                )
            else:
                msg = f"📺 *Channel-nya Bos ({len(channels)})*\n━━━━━━━━━━━━━━━━━━━━━━\n"
                for idx, ch in enumerate(channels, 1):
                    name = _channel_display_name(ch)
                    connected = "🟢 Terhubung" if _channel_connected(ch) else "🔴 Putus"
                    msg += f"*{idx}. {_tg_escape(name)}*\nStatus: {connected}\nID: `{_tg_escape(ch.get('yt_id', '-'))}`\n\n"
            send_message(token, chat_id, msg)
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
                    sched = t.get('schedule_date', '-')
                    msg += f"*{idx}. {_tg_escape(title[:35])}...*\nStatus: `{_tg_escape(status)}` | Tayang: `{_tg_escape(sched)}`\n\n"
                if len(active) > 8:
                    msg += f"_...dan {len(active)-8} video lainnya dalam antrean._"
            send_message(token, chat_id, msg)
            return

        # 4b. JADWAL PUBLISH (FASE 3)
        if "jadwal" in clean_cmd or "schedule" in clean_cmd:
            send_message(token, chat_id, build_schedule_report())
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
                "• *Kapasitas Upload:* ~6 video per hari per project Google Cloud"
            )
            send_message(token, chat_id, msg)
            return

        # DEFAULT FALLBACK
        msg = (
            f"🤖 *Eh Bos, pesan \"{_tg_escape(raw_cmd)}\" saya terima tapi saya nggak ngerti maksudnya* 😅\n\n"
            "Coba sentuh tombol di bawah atau ketik /start buat lihat semua perintah ya Bos!\n"
            "Perintah cepat: /status /server /kuota /stok /statistik /monetisasi /judul /ideashorts /buatshort /buatlong"
        )
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
        except Exception as e:
            print(f"Watchdog exception: {e}", flush=True)
            time.sleep(60)

def daily_report_loop(token, chat_id):
    """🔥 FASE 1 MONITORING: laporan harian otomatis 07:00 WIB.
    Pola sama seperti watchdog (thread + sleep), tanpa cron sistem atau
    dependensi baru. Cek tiap 60 detik; kirim sekali sehari (dedupe tanggal
    di monitor_state.json — tahan restart, tidak dobel kirim)."""
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
            send_message(token, chat_id, build_daily_report())
            state["daily_report_date"] = today
            save_monitor_state(state)
            print(f"🌅 Laporan harian {today} terkirim.", flush=True)
        except Exception as e:
            print(f"Daily report exception: {e}", flush=True)
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

    # Kirim salam & pasang keyboard menu interaktif
    welcome_msg = (
        "🟢 *Bos, saya siap!* 😎\n\n"
        "Sistem udah diperbarui, semua tombol di bawah udah bisa dipencet dan langsung saya respon! 💪"
    )
    send_message(token, owner_chat_id, welcome_msg, reply_markup=get_main_keyboard())

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
                        print(f"📩 Perintah diterima dari Bos ({chat_id}): {text}", flush=True)
                        handle_user_command(token, chat_id, text)
                    elif text:
                        print(f"⚠️ Akses ditolak dari ID asing: {sender_id} / chat: {chat_id}", flush=True)
            time.sleep(0.3)
        except Exception as e:
            print(f"Polling loop exception: {e}", flush=True)
            time.sleep(2)

if __name__ == '__main__':
    main()
