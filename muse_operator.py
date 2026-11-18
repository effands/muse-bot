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
        return data
    except Exception:
        return {"daily_report_date": "", "alerts": {}}

def save_monitor_state(state):
    """Simpan state secara atomik (temp + replace) agar tidak rusak saat restart."""
    try:
        tmp = MONITOR_STATE_FILE + '.tmp'
        with open(tmp, 'w') as f:
            json.dump(state, f, indent=2)
        os.replace(tmp, MONITOR_STATE_FILE)
    except Exception as e:
        print(f"Gagal simpan monitor_state: {e}", flush=True)

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
            [{"text": "📊 Status Server"}, {"text": "💡 10 Judul Sleep"}],
            [{"text": "📺 Cek Channel"}, {"text": "📂 Antrean Render"}],
            [{"text": "📝 Template Deskripsi"}, {"text": "📐 Blueprint Resep"}]
        ],
        "resize_keyboard": True,
        "is_persistent": True
    }

def send_message(token, chat_id, text, parse_mode="Markdown", reply_markup=None):
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
    elif "lofi" in niche_lower or "study" in niche_lower or "fokus" in niche_lower or "chill" in niche_lower:
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

def handle_user_command(token, chat_id, text):
    try:
        raw_cmd = text.strip()
        # Bersihkan emoji agar teks tombol tombol seperti "📊 Status Server" cocok ke "status server"
        clean_cmd = re.sub(r'[^\w\s/]', '', raw_cmd).strip().lower()
        parts = clean_cmd.split()
        first_word = parts[0] if parts else ""

        print(f"Perintah diproses: '{raw_cmd}' -> clean: '{clean_cmd}'", flush=True)

        # 1. MENU / START
        if first_word in ["/start", "/help", "halo", "menu", "hai"]:
            msg = (
                "🤖 *ZIQVA MUSE BOT — CLOUD OPERATOR* 😎\n\n"
                "Halo Bos! Saya siap kerja, tinggal perintah aja. Sentuh tombol di bawah atau klik perintah berikut:\n\n"
                "👉 /status — Cek channel, antrean, kuota upload & kesehatan server\n"
                "👉 /channel — Cek channel YouTube terdaftar\n"
                "👉 /antrean — Cek daftar video yang sedang diproses\n"
                "👉 /jadwal — Lihat jadwal publish 7 hari ke depan & channel yang dijeda\n"
                "👉 /judul — Buatkan 10 judul video tidur viral\n"
                "👉 /deskripsi — Buatkan template deskripsi SEO\n"
                "👉 /blueprint — Cek resep settingan video"
            )
            send_message(token, chat_id, msg)
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
                    name = ch.get('channel_name', 'Tanpa Nama')
                    connected = "🟢 Terhubung" if ch.get('access_token') or ch.get('refresh_token') else "🔴 Putus"
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

        # 5. GENERATOR JUDUL
        if "judul" in clean_cmd or "ide" in clean_cmd or "sleep" in clean_cmd or "titles" in clean_cmd:
            niche = "Deep Sleep Music"
            count = 10
            if parts and parts[-1].isdigit():
                count = int(parts[-1])
            if "lofi" in clean_cmd or "chill" in clean_cmd:
                niche = "Lofi Study"

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

        # 6. TEMPLATE DESKRIPSI
        if "deskripsi" in clean_cmd or "description" in clean_cmd:
            niche = "Deep Sleep Music"
            msg = (
                f"📝 *Template Deskripsi SEO buat Bos:*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Welcome to your ultimate sanctuary for relaxation and deep sleep. 🌙✨\n\n"
                f"This {niche.lower()} compilation is carefully crafted to help ease insomnia, relieve mental fatigue, and release stress after a demanding day.\n\n"
                "🎧 Listening Advice:\n"
                "• Use low-medium volume (30-40%)\n"
                "• Dim the lights in your bedroom\n"
                "• Close your eyes and breathe gently\n\n"
                "Tracklist & Audio Engineering by Ziqva Muse Bot Cloud System.\n\n"
                "#deepsleep #relaxingmusic #sleepaid #ambientmusic #insomniarelief #calmmind"
            )
            send_message(token, chat_id, msg)
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
            "Coba sentuh tombol di bawah atau ketik:\n"
            "👉 /status | /channel | /antrean | /jadwal | /judul"
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
