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
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
CHANNELS_FILE = os.path.join(BASE_DIR, 'channels_db.json')
TASKS_FILE = os.path.join(BASE_DIR, 'tasks_db.json')
# 🔥 FASE 1 MONITORING: state dedupe (laporan harian & alert) — file runtime,
# JANGAN di-commit (sudah masuk .gitignore). Isinya hanya tanggal + kunci alert.
MONITOR_STATE_FILE = os.path.join(BASE_DIR, 'monitor_state.json')
# Batas upload harian per channel — cerminan MAX_UPLOADS_PER_CHANNEL_PER_DAY di app.py.
MAX_UPLOADS_PER_DAY = 6
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
        data.setdefault("evening_report_date", "")
        data.setdefault("alerts", {})
        data.setdefault("make_drafts", {})
        data.setdefault("channel_report_snapshot", {})
        data.setdefault("channel_stats_cache", {})
        return data
    except Exception:
        return {"daily_report_date": "", "evening_report_date": "", "alerts": {},
                "make_drafts": {}, "channel_report_snapshot": {}, "channel_stats_cache": {}}

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


def _autopilot_of(channel_data):
    """Mode autopilot channel: 'auto' atau 'approval' (default — sama
    seperti _autopilot_of di app.py; operator tidak mengimpor app)."""
    if not channel_data:
        return 'approval'
    mode = str(channel_data.get('autopilot') or 'approval').lower()
    return mode if mode in ('auto', 'approval') else 'approval'


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
                                   shorts_complete, monetized, today_str):
    """SATU tempat perhitungan kesiapan monetisasi di operator (kembaran
    helper panel). PENTING: jam tayang di sini HANYA ESTIMASI (snapshot
    views 90 hari terakhir × durasi rata-rata blueprint video long channel).
    Jam tayang asli butuh YouTube Analytics API yang tidak diizinkan di
    device flow; keputusan akhir monetisasi ada di YouTube Studio. Jalur
    Shorts hanya dipakai bila shorts_complete=True; bila tidak, badge tidak
    pernah 'ready' lewat jalur Shorts."""
    rule = _monetization_rule_for(today_str)
    base = {"subs": subs or 0, "subs_required": rule["subs"],
            "watch_est": watch_90d_est_hours, "watch_required": rule["watch_hours"],
            "shorts_views": shorts_views_90d, "shorts_required": rule["shorts_views_90d"],
            "shorts_complete": bool(shorts_complete),
            "rule_label": rule["label"], "rule_next": rule["rule_next"]}
    if monetized:
        return dict(base, status="monetized", badge="💰 Monetisasi Aktif", via=None)
    subs_ok = (subs or 0) >= rule["subs"]
    hours_ok = watch_90d_est_hours is not None and watch_90d_est_hours >= rule["watch_hours"]
    shorts_ok = bool(shorts_complete) and (shorts_views_90d or 0) >= rule["shorts_views_90d"]
    ready = subs_ok and (hours_ok or shorts_ok)
    via = None
    if ready:
        via = "shorts" if (shorts_ok and not hours_ok) else "hours"
    return dict(base, status="ready" if ready else "progress",
                badge="✅ Siap Monet (est.)" if ready else "⏳ Belum Siap", via=via)


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


def _fmt_eta_days(missing, per_day):
    """Perkiraan hari menuju target: '~N hari lagi'. None bila tidak bisa
    dihitung (kecepatan nol/negatif atau target sudah tercapai). Angka
    SELALU perkiraan — jangan pernah disajikan sebagai janji."""
    if missing is None or missing <= 0:
        return None
    if per_day is None or per_day <= 0:
        return None
    days = math.ceil(missing / per_day)
    if days <= 0:
        return None
    if days > 3650:
        return f"~{_fmt_num(days)}+ hari lagi"
    return f"~{days} hari lagi"


def _monetization_analysis_line(ready, subs_per_day, hours_per_day, eta_hint, short=False):
    """SATU baris analisis kekurangan + saran tindakan per channel (bahasa
    sederhana, nada operator). subs_per_day / hours_per_day = kecepatan
    dari 2 laporan terakhir (None bila belum bisa dihitung); eta_hint =
    alasan kenapa kecepatan tidak bisa dihitung (atau None). short=True =
    versi ringkas tanpa perkiraan waktu (dipakai bila channel banyak supaya
    laporan tetap ≤4096 karakter).
    TIDAK PERNAH menjanjikan hasil — semua waktu adalah 'Perkiraan ... (bila
    kecepatan sekarang bertahan)'."""
    if ready.get("status") == "monetized":
        return "🔍 Sudah monetisasi — pertahankan konsistensi upload ya Bos."
    if ready.get("status") == "ready":
        return "🔍 Siap diajukan — cek YouTube Studio ya Bos (keputusan akhir ada di YouTube)."
    subs = ready.get("subs") or 0
    subs_req = ready.get("subs_required") or 1000
    subs_ok = subs >= subs_req
    watch = ready.get("watch_est")
    watch_req = ready.get("watch_required") or 4000
    hours_ok = watch is not None and watch >= watch_req
    shorts_ok = bool(ready.get("shorts_complete")) and (ready.get("shorts_views") or 0) >= ready.get("shorts_required", 10_000_000)
    subs_missing = max(0, subs_req - subs)
    hours_missing = max(0, watch_req - watch) if watch is not None else None

    def _eta_note(missing, per_day):
        """' Perkiraan ~N hari lagi (bila kecepatan sekarang bertahan).' atau
        penjelasan jujur kenapa tidak bisa dihitung."""
        eta = _fmt_eta_days(missing, per_day)
        if eta:
            return f" Perkiraan {eta} (bila kecepatan sekarang bertahan)."
        if per_day is not None and per_day <= 0:
            return " Belum naik sejak laporan terakhir."
        if eta_hint:
            return f" {eta_hint}"
        return " Belum bisa dihitung — butuh 2 laporan."

    if short:
        if hours_ok and not subs_ok:
            return f"🔍 Subs kurang {_fmt_num(subs_missing)} — Shorts lebih sering, Bos."
        if subs_ok and not (hours_ok or shorts_ok):
            if watch is None:
                return "🔍 Jam tayang belum bisa dihitung — buka Dashboard sekali ya Bos."
            return (f"🔍 Jam kurang ~{_fmt_num(round(hours_missing))} jam (est.) — "
                    f"perbanyak video long, Bos.")
        if watch is None:
            return (f"🔍 Subs kurang {_fmt_num(subs_missing)} • jam belum bisa dihitung — "
                    f"fokus Shorts rutin dulu.")
        subs_prog = subs / subs_req if subs_req else 0
        perf_prog = max(watch / watch_req if watch_req else 0,
                        (ready.get("shorts_views") or 0) / ready["shorts_required"] if ready.get("shorts_complete") else 0)
        paling_kurang = "subs" if subs_prog <= perf_prog else "jam tayang"
        return (f"🔍 Subs kurang {_fmt_num(subs_missing)} • jam kurang ~{_fmt_num(round(hours_missing))} jam (est.) — "
                f"Shorts rutin + video long. Paling kurang: {paling_kurang}.")

    if hours_ok and not subs_ok:
        # Jam tayang cukup, subscriber kurang → Shorts harus lebih sering.
        return (f"🔍 Jam tayang sudah cukup, subs kurang {_fmt_num(subs_missing)} lagi — "
                f"Shorts harus lebih sering, Bos, Shorts paling cepat nambah subscriber."
                + _eta_note(subs_missing, subs_per_day))
    if subs_ok and not (hours_ok or shorts_ok):
        if watch is None:
            return ("🔍 Subs sudah cukup, jam tayang belum bisa dihitung — "
                    "buka Dashboard sekali ya Bos biar snapshot keisi.")
        return (f"🔍 Subs sudah cukup, jam tayang kurang ~{_fmt_num(round(hours_missing))} jam (est.) — "
                f"perbanyak video long, Bos, biar jam tayangnya naik."
                + _eta_note(hours_missing, hours_per_day))
    # Keduanya kurang.
    if watch is None:
        return (f"🔍 Subs kurang {_fmt_num(subs_missing)} • jam tayang belum bisa dihitung — "
                f"fokus Shorts rutin dulu buat nambah subs, sambil buka Dashboard sekali biar jam tayang kehitung."
                + _eta_note(subs_missing, subs_per_day))
    subs_prog = subs / subs_req if subs_req else 0
    perf_prog = max(watch / watch_req if watch_req else 0,
                    (ready.get("shorts_views") or 0) / ready["shorts_required"] if ready.get("shorts_complete") else 0)
    paling_kurang = "subs" if subs_prog <= perf_prog else "jam tayang"
    eta_txt = (_eta_note(subs_missing, subs_per_day) if paling_kurang == "subs"
               else _eta_note(hours_missing, hours_per_day))
    return (f"🔍 Subs kurang {_fmt_num(subs_missing)} • jam tayang kurang ~{_fmt_num(round(hours_missing))} jam (est.) — "
            f"kombinasi: Shorts rutin buat nambah subs + video long buat jam tayang. "
            f"Yang paling kurang: {paling_kurang}." + eta_txt)


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
            blocks.append([f"• *{_tg_escape(name)}* — data belum bisa diambil 😌 (coba lagi di laporan berikutnya)"])
            continue
        views, subs, videos = int(st["views"]), int(st["subs"] or 0), int(st["videos"] or 0)
        new_snapshot[st["yt_id"]] = {"views": views, "subs": subs, "videos": videos, "at": now_label}
        block = [f"• *{_tg_escape(name)}* — 👁️ {_fmt_num(views)} views • 👥 {_fmt_num(subs)} subs (📹 {_fmt_num(videos)} video)"]
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
            watch_txt = f"~{_fmt_num(round(watch90))} jam" if watch90 is not None else "jam tayang: — (belum bisa dihitung)"
            block.append(f"  💰 ⏳ Belum Siap — subs {_fmt_num(subs)}/{_fmt_num(rule['subs'])} • {watch_txt}/{_fmt_num(rule['watch_hours'])} jam (est.)")
        # --- analisis kekurangan + saran tindakan ---
        # Bila channel banyak (>4), pakai versi ringkas tanpa perkiraan waktu
        # supaya laporan tetap ≤4096 karakter (jangan potong di tengah angka).
        compact = len(stats) > 4
        block.append("  " + _monetization_analysis_line(ready, subs_per_day, hours_per_day, eta_hint, short=compact))
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
        lines.append("_Segera siap = syarat yang paling kurang sudah ≥50%._")
    for block in blocks:
        lines.extend(block)
    if len(stats) > 8:
        lines.append(f"_...dan {len(stats) - 8} channel lainnya (cek di panel)._")
    if any_missing_baseline:
        lines.append("_Delta mulai terlihat dari laporan berikutnya._")
    lines.append(f"_Syarat monet saat ini: {rule['label']}._")
    if rule.get("rule_next"):
        lines.append(f"_{rule['rule_next']}._")
    lines.append("_Jam tayang adalah estimasi (views × durasi rata-rata); angka resmi hanya di YouTube Studio._")
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
    elif "anak" in niche_lower or "kids" in niche_lower or "balita" in niche_lower:
        templates = [
            "Lagu Anak Ceria 🎵 Belajar Berhitung 1-10 Sambil Bernyanyi (Edukatif & Menyenangkan)",
            "Ayo Bernyanyi Bersama! Lagu Anak-Anak Ceria untuk Balita & PAUD",
            "Belajar Huruf A-Z Sambil Bernyanyi 🎵 Lagu Anak Edukatif untuk Usia Dini",
            "Lagu Anak: Warna-Warni Pelangi 🌈 Belajar Warna Sambil Bernyanyi",
            "Gerak dan Lagu Anak Ceria 🕺 Senam Pagi Seru untuk Anak-Anak",
            "Lagu Tidur Anak 🌙 Lullaby Lembut untuk Anak Tidur Nyenyak",
            "Belajar Nama Hewan Sambil Bernyanyi 🐶 Lagu Anak Edukatif yang Seru",
            "Lagu Anak Indonesia 🎵 Kompilasi Lagu Ceria untuk Anak Usia Dini",
            "Ayo Belajar Berhitung! Lagu Angka 1-20 untuk Anak PAUD & TK",
            "Lagu Anak: Hari-Hari Ceria ☀️ Lagu Semangat Pagi untuk Anak-Anak",
            "Belajar Bentuk & Warna 🎨 Lagu Anak Edukatif yang Mudah Diingat",
            "Lagu Anak Ceria: Sahabatku 🧒 Lagu Persahabatan untuk Anak-Anak",
        ]
    elif "asmr" in niche_lower:
        templates = [
            "ASMR for Deep Sleep 😴 Gentle Whispers & Soft Tapping to Fall Asleep Fast",
            "Relaxing ASMR Whispers • Soft Sounds for Sleep, Stress Relief & Calm Nights",
            "ASMR Tapping & Brushing Sounds for Instant Relaxation (No Talking)",
            "Fall Asleep in Minutes • Soothing ASMR Whispers for Insomnia Relief",
            "Cozy ASMR Night • Gentle Tapping, Soft Whispers & Rain for Deep Sleep",
            "ASMR for Anxiety Relief • Calming Whispers to Quiet Your Mind",
            "Soft Spoken ASMR • Gentle Whispers to Help You Sleep Peacefully",
            "ASMR Ear-to-Ear Whispers • Deep Relaxation & Sleep Sounds (Headphones On)",
            "Tingly ASMR Sounds • Brushing, Tapping & Whispering for Deep Sleep",
            "ASMR Sleep Aid • Whispered Affirmations & Calm Sounds for Insomnia",
            "Relaxing ASMR for Study & Focus • Soft Tapping Without Distraction",
            "ASMR Rain & Whispers • The Perfect Soundscape for a Good Night's Sleep",
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
    "buat short", "buat video long",
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
    return (
        "📋 *Cek dulu ya Bos, ini Shorts yang mau dibuat:* 🎬\n"
        f"📺 Channel: *{_tg_escape(d.get('channel_name', ''))}*\n"
        f"🎬 Judul: *{_tg_escape(d.get('title', ''))}*\n"
        f"🪝 Hook (4 detik): {_tg_escape(hook[:150])}{'…' if len(hook) > 150 else ''}\n"
        f"📝 Naskah: {_tg_escape(script[:200])}{'…' if len(script) > 200 else ''}\n"
        f"📣 CTA: {_tg_escape(d.get('cta', 'Subscribe'))}\n"
        f"⏱️ Durasi: `{d.get('duration_sec', 30)} detik`\n"
        f"🗓️ Jadwal: `{_tg_escape(d.get('publish_date', ''))}`\n"
        f"🖼️ Background: `{_tg_escape(bg_label)}`\n"
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
            if not opts:
                lines = ["🖼️ *Langkah 7 dari 7: Background*\n\nGaleri channel ini masih kosong — nanti saya buatkan background gradien otomatis, Bos. 🎨\n\nBalas `lewati` untuk lanjut (atau upload gambar sendiri di panel bila mau yang lebih bagus)."]
                send_message(token, chat_id, "\n".join(lines))
                return
            lines = ["🖼️ *Langkah 7 dari 7: Background*\n\nPilih gambar/video background dari galeri:\n"]
            vids = d.get('bg_video_count') or 0
            if vids:
                lines.append(f"🎬 Galeri ini punya {vids} klip video — balas `lewati` untuk pakai SEMUA klip disambung jadi background bergerak, atau pilih 1 file di bawah untuk pakai file itu saja.\n")
            for i, f in enumerate(opts[:10], 1):
                lines.append(f"{i}. `{_tg_escape(f)}`")
            if len(opts) > 10:
                lines.append(f"_…dan {len(opts) - 10} file lainnya_")
            lines.append("\nBalas angkanya untuk pakai 1 file itu saja. Ketik `lewati` untuk pakai semua klip video disambung (atau gambar acak bila tidak ada video).")
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
                # Tanpa pilihan eksplisit → panel yang memutuskan: semua klip
                # video disambung bila ada, gambar acak bila tidak ada video,
                # gradien otomatis bila galeri kosong.
                d['bg_file'] = ''
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


def _nlu_compute_shorts_schedule(ch, count, anchor_time, publish_time):
    """Hitung jadwal Shorts aturan jeda 6 jam (2026-11-19): video pertama
    di slot kosong berikutnya (jam eksplisit/jangkar hari/jam tetap
    channel), video berikutnya = sebelumnya + 6 jam — boleh lewat tengah
    malam ke hari berikutnya. Bila slot hasil hitungan sudah terisi,
    geser +6 jam sampai kosong. Kembalikan list 'YYYY-MM-DD HH:MM'."""
    first = anchor_time or publish_time
    dt = None
    if first:
        try:
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
                ch, count, slots.get('anchor_time'), slots.get('publish_time'))
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


def parse_natural_command(text, channels):
    """Terjemahkan pesan bebas → {intent, confidence 0-100, slots}.
    Intent: buat_short, buat_long, status, laporan, kuota, stok, statistik,
    monetisasi, jadwal, gagal, channel_list, bantuan, jeda_channel,
    lanjutkan_channel, perpanjang_jadwal, ubah_mode, hapus,
    hentikan_antrean, tidak_dikenali."""
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
    # --- Buat Short / Long. ---
    elif create_verb and ('short' in clean or 'video pendek' in clean):
        intent = 'buat_short'
        conf = 50
    elif create_verb and ('video long' in clean or 'video panjang' in clean
                          or 'kompilasi' in clean or re.search(r'\blong\b', clean)):
        intent = 'buat_long'
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
        tm = _nlu_extract_time(clean)
        if tm:
            slots['publish_time'] = tm
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
                    'monetisasi', 'jadwal', 'gagal', 'channel_list', 'bantuan'):
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
    names = _nlu_channel_names(slots.get('channels') or [])
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
        # Bos bisa mengoreksi sebelum dieksekusi.
        sched = slots.get('schedule') or {}
        if sched:
            if len(sched) == 1:
                return base + f" — jadwal: {_nlu_format_schedule(next(iter(sched.values())))}"
            lines = []
            for yt in (slots.get('channels') or []):
                if yt in sched:
                    nm = _nlu_channel_names([yt])
                    lines.append(f"• {nm[0] if nm else yt}: {_nlu_format_schedule(sched[yt])}")
            if lines:
                return base + " — jadwal:\n" + "\n".join(lines)
            return base
        if slots.get('publish_time'):
            return base + f", jam tayang {slots['publish_time']}"
        return base
    if intent == 'buat_long':
        niche = _NICHES.get(slots.get('niche') or 'sleep', 'Deep Sleep Music')
        return (f"buatkan {slots.get('count') or 1} video long tema {niche} untuk {ch}")
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
    return intent or 'sesuatu'


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
                 f"Maksud Bos: {_nlu_describe(pending)}?{extra}",
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
    if intent in ('buat_short', 'buat_long', 'perpanjang_jadwal', 'ubah_mode',
                  'jeda_channel', 'lanjutkan_channel', 'hapus', 'hentikan_antrean'):
        if slots.get('channels') == 'all':
            slots['channels'] = [c.get('yt_id') for c in chans]
        if not slots.get('channels') and slots.get('channel_unknown'):
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
                pass
    # --- Konfirmasi bila aksi membuat konten / berbahaya / matikan pengaman. ---
    needs_confirm = (
        intent in ('buat_short', 'buat_long', 'hapus', 'hentikan_antrean')
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
        base_slot = _nlu_publish_slot(slots.get('publish_time'), ch)
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
    start_date = _nlu_publish_slot(slots.get('publish_time'), ch)
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


def _nlu_execute(token, chat_id, pending):
    kind = pending.get('intent')
    if kind == 'buat_short':
        _execute_nlu_create_shorts(token, chat_id, pending)
    elif kind == 'buat_long':
        _execute_nlu_create_long(token, chat_id, pending)
    elif kind == 'perpanjang_jadwal':
        _execute_nlu_extend(token, chat_id, pending)
    elif kind == 'ubah_mode':
        _execute_nlu_set_mode(token, chat_id, pending)
    elif kind == 'jeda_channel' or kind == 'lanjutkan_channel':
        _execute_nlu_set_paused(token, chat_id, pending)
    elif kind in ('hapus', 'hentikan_antrean'):
        _execute_nlu_destructive(token, chat_id, pending)


def _nlu_run_readonly(token, chat_id, intent):
    """Perintah baca saja dari NLU — memakai builder yang sama seperti
    perintah baku."""
    if intent == 'status':
        send_message(token, chat_id, build_status_report())
    elif intent == 'laporan':
        kind = 'morning' if _wib_now().hour < 12 else 'evening'
        report = build_daily_report(kind)
        send_message(token, chat_id, report if report else "Belum ada data laporan hari ini, Bos.")
    elif intent == 'kuota':
        send_message(token, chat_id, build_quota_report())
    elif intent == 'stok':
        send_message(token, chat_id, build_stock_report())
    elif intent == 'statistik':
        send_message(token, chat_id, build_stats_report())
    elif intent == 'monetisasi':
        send_message(token, chat_id, build_monetization_report())
    elif intent == 'jadwal':
        send_message(token, chat_id, build_schedule_report())
    elif intent == 'gagal':
        send_message(token, chat_id, build_failed_report()[0])
    elif intent == 'channel_list':
        chans = load_channels()
        if not chans:
            send_message(token, chat_id, "Belum ada channel yang tertaut, Bos.")
            return
        lines = ["📺 *Channel yang tertaut:*"]
        for c in chans:
            name = _channel_display_name(c)
            mode = 'Full-Auto' if _autopilot_of(c) == 'auto' else 'Approval'
            paused = ' ⏸️ Dijeda' if c.get('paused') else ''
            lines.append(f"• *{name}* — Mode: {mode}{paused}")
        send_message(token, chat_id, "\n".join(lines))
    elif intent == 'bantuan':
        send_message(token, chat_id, build_help_message())
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
        '/laporan': ('laporan', 'report', 'pagi', 'sore'),
        '/kuota': ('kuota', 'limit', 'upload'),
        '/stok': ('stok', 'sisa', 'jadwal'),
        '/statistik': ('statistik', 'stats', 'views'),
        '/monetisasi': ('monetisasi', 'penghasilan', 'uang', 'rpm'),
        '/jadwal': ('jadwal', 'schedule', 'kalender'),
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
    if awaiting == 'mode':
        mode = _nlu_extract_mode(clean_cmd)
        if mode:
            slots['mode'] = mode
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
    if intent in ('status', 'laporan', 'kuota', 'stok', 'statistik',
                  'monetisasi', 'jadwal', 'gagal', 'channel_list', 'bantuan',
                  'tambah_channel'):
        _nlu_audit_log(raw_cmd, parsed, 'executed')
        _nlu_run_readonly(token, chat_id, intent)
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
    ('riwayat', '8 upload terakhir', 'pantau', ''),
    ('gagal', 'Task gagal + tombol Retry', 'pantau', ''),
    ('laporan', 'Laporan harian (otomatis tiap pagi 07:00 & sore 17:00 WIB), kapan saja', 'pantau', ''),
    ('statistik', 'Views/likes/komentar + video terbaik & terburuk', 'hasil', ''),
    ('monetisasi', 'Estimasi penghasilan dari RPM manual', 'hasil', ''),
    ('judul', 'Judul SEO, mis. /judul lofi 15', 'konten', '[niche] [jumlah]'),
    ('deskripsi', 'Template deskripsi SEO', 'konten', '[niche]'),
    ('tag', 'Bank hashtag, mis. /tag piano', 'konten', '[niche]'),
    ('cta', 'Ide teks CTA buat Shorts', 'konten', ''),
    ('ideashorts', 'Hook + naskah singkat Shorts', 'konten', '[niche]'),
    ('blueprint', 'Resep settingan video', 'konten', ''),
    ('buatshort', 'Buat Shorts Muse terpandu (judul, hook, naskah, CTA)', 'buat', ''),
    ('buatlong', 'Buat batch video long 60–80 menit terpandu', 'buat', ''),
    ('batal', 'Batalkan draft yang sedang dibuat', 'buat', ''),
    ('perpanjang', 'Tambah jadwal, mis. /perpanjang Aruya 0089 30 atau /perpanjang semua 30', 'atur', '[channel] [hari]'),
    ('mode', 'Ganti mode, mis. /mode Aruya 0089 auto', 'atur', '[channel] [auto|approval]'),
    ('jeda', 'Jeda channel (jadwal & antrean berhenti)', 'atur', '[channel]'),
    ('lanjutkan', 'Lanjutkan channel yang dijeda', 'atur', '[channel]'),
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

        # 1. MENU / START — teks dibangun dari _BOT_COMMANDS (satu sumber
        # dengan setMyCommands, supaya daftarnya tidak pernah beda).
        if first_word in ["/start", "/help", "halo", "menu", "hai"]:
            send_message(token, chat_id, _build_start_text())
            return

        # --- 🎬 BUAT VIDEO (alur terpandu) — dicek sebelum cabang lama
        # supaya "buat short" tidak ketangkap cabang /ideashorts ("shorts").
        if first_word in ("/buatshort",) or clean_cmd in ("buat short",):
            _start_make_short(token, chat_id, raw_cmd)
            return
        if first_word in ("/buatlong",) or clean_cmd in ("buat video long",):
            _start_make_long(token, chat_id, raw_cmd)
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

        # DEFAULT FALLBACK — did-you-mean (NLU sudah mencoba menerjemahkan
        # dan tidak mengenali maksudnya; sarankan perintah terdekat).
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
