import os
import time
import json
import psutil
import requests
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
CHANNELS_FILE = os.path.join(BASE_DIR, 'channels_db.json')
TASKS_FILE = os.path.join(BASE_DIR, 'tasks_db.json')

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

def send_message(token, chat_id, text, parse_mode="Markdown"):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        return res.json()
    except Exception as e:
        print(f"Error kirim pesan TG: {e}")
        return None

def get_system_telemetry():
    cpu = psutil.cpu_percent(interval=0.5)
    mem = psutil.virtual_memory()
    disk = psutil.disk_usage(BASE_DIR)
    return {
        "cpu": cpu,
        "ram_pct": mem.percent,
        "ram_used_gb": round(mem.used / (1024**3), 1),
        "ram_total_gb": round(mem.total / (1024**3), 1),
        "disk_free_gb": round(disk.free / (1024**3), 1)
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
    cmd = text.strip()
    parts = cmd.split()
    first_word = parts[0].lower() if parts else ""

    # MENU / START
    if first_word in ["/start", "/help", "halo", "menu", "hai"]:
        msg = (
            "🤖 *MUSE AI — OPERATOR PABRIK KONTEN AKTIF*\n\n"
            "Halo Bos! Saya adalah operator AI pabrik konten Anda. Saya siap menerima perintah kerja dari Anda kapan saja.\n\n"
            "📌 *Daftar Perintah Cepat:*\n"
            "• `/status` — Cek kesehatan CPU, RAM, & antrean pabrik\n"
            "• `/channel` — Cek daftar channel YouTube & status koneksi\n"
            "• `/antrean` — Cek daftar video yang sedang/akan dirender\n"
            "• `/judul <niche> [jumlah]` — Buatkan bank judul SEO viral\n"
            "• `/deskripsi <niche>` — Buatkan template deskripsi SEO YouTube\n"
            "• `/blueprint` — Cek standar resep video (durasi & preset)\n\n"
            "💡 *Tips:* Anda juga bisa chat bebas, misalnya:\n"
            "_\"Muse, buatkan 15 judul sleep music\"_ atau _\"Cek server\"_."
        )
        send_message(token, chat_id, msg)
        return

    # STATUS
    if first_word in ["/status", "status", "server", "cek server"]:
        stats = get_system_telemetry()
        tasks_data = load_tasks()
        active = tasks_data.get('active', [])
        in_progress = len([t for t in active if 'Selesai' not in t.get('status', '') and 'Gagal' not in t.get('status', '')])
        
        msg = (
            "📊 *LAPORAN STATUS PABRIK KONTEN*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"⚡ *Beban CPU:* `{stats['cpu']}%`\n"
            f"🧠 *RAM Terpakai:* `{stats['ram_used_gb']} GB / {stats['ram_total_gb']} GB ({stats['ram_pct']}%)`\n"
            f"💾 *Sisa Disk:* `{stats['disk_free_gb']} GB`\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"🏭 *Antrean Render Aktif:* `{in_progress} video`\n"
            f"📁 *Riwayat Tugas:* `{len(tasks_data.get('history', []))} selesai`\n"
            "🟢 *Status Node:* Normal 24/7 (Port 8000)"
        )
        send_message(token, chat_id, msg)
        return

    # CHANNELS
    if first_word in ["/channel", "/channels", "channel"]:
        channels = load_channels()
        if not channels:
            msg = (
                "📺 *DAFTAR CHANNEL YOUTUBE*\n"
                "Belum ada channel terdaftar di panel.\n\n"
                "👉 Silakan buka panel di browser Anda: `http://localhost:8000/factory` "
                "lalu klik *Connect* untuk menautkan channel pertama."
            )
        else:
            msg = f"📺 *DAFTAR CHANNEL YOUTUBE ({len(channels)})*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            for idx, ch in enumerate(channels, 1):
                name = ch.get('channel_name', 'Tanpa Nama')
                connected = "🟢 Terhubung" if ch.get('access_token') or ch.get('refresh_token') else "🔴 Putus"
                msg += f"*{idx}. {name}*\nStatus: {connected}\nID: `{ch.get('yt_id', '-')}`\n\n"
        send_message(token, chat_id, msg)
        return

    # ANTREAN / QUEUE
    if first_word in ["/antrean", "/queue", "antrean"]:
        tasks_data = load_tasks()
        active = tasks_data.get('active', [])
        if not active:
            msg = "📂 *ANTREAN RENDER KOSONG*\nSaat ini tidak ada video dalam antrean render."
        else:
            msg = f"📂 *ANTREAN TUGAS RENDER ({len(active)})*\n━━━━━━━━━━━━━━━━━━━━━━\n"
            for idx, t in enumerate(active[:8], 1):
                title = t.get('title', 'Video Kompilasi')
                status = t.get('status', 'Menunggu')
                sched = t.get('schedule_date', '-')
                msg += f"*{idx}. {title[:35]}...*\nStatus: `{status}` | Tayang: `{sched}`\n\n"
            if len(active) > 8:
                msg += f"_...dan {len(active)-8} video lainnya dalam antrean._"
        send_message(token, chat_id, msg)
        return

    # GENERATOR JUDUL
    if first_word in ["/judul", "/ide", "/titles"] or "buatkan" in cmd.lower() or "judul" in cmd.lower():
        niche = "Deep Sleep Music"
        count = 10
        
        # parse args
        if len(parts) > 1 and first_word in ["/judul", "/ide", "/titles"]:
            # check last param for count
            if parts[-1].isdigit():
                count = int(parts[-1])
                niche = " ".join(parts[1:-1]) or "Deep Sleep Music"
            else:
                niche = " ".join(parts[1:])
        elif "sleep" in cmd.lower() or "tidur" in cmd.lower():
            niche = "Sleep Music"
        elif "lofi" in cmd.lower() or "chill" in cmd.lower():
            niche = "Lofi Study"

        titles = generate_seo_titles(niche, count)
        title_list = "\n".join([f"{i+1}. `{t}`" for i, t in enumerate(titles)])
        
        msg = (
            f"💡 *BANK JUDUL VIRAL & SEO ({count} Judul)*\n"
            f"🎯 *Niche:* `{niche}`\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"{title_list}\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "📋 *Cara Pakai:* Salin judul di atas langsung ke kolom *Bank Judul* channel Anda di web panel!"
        )
        send_message(token, chat_id, msg)
        return

    # DESKRIPSI TEMPLATE
    if first_word in ["/deskripsi", "deskripsi"]:
        niche = "Deep Sleep Music"
        if len(parts) > 1:
            niche = " ".join(parts[1:])
        msg = (
            f"📝 *TEMPLATE DESKRIPSI SEO YOUTUBE ({niche.title()})*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            f"Welcome to your ultimate sanctuary for relaxation and peace. 🌙✨\n\n"
            f"This {niche.lower()} compilation is carefully crafted to help ease insomnia, relieve mental fatigue, and release stress after a demanding day.\n\n"
            "🎧 Listening Advice:\n"
            "• Use low-medium volume (30-40%)\n"
            "• Dim the lights in your bedroom\n"
            "• Close your eyes and breathe gently\n\n"
            "Tracklist & Composition by Pabrik Konten Audio Hub.\n\n"
            "#deepsleep #relaxingmusic #sleepaid #ambientmusic #insomniarelief #calmmind"
        )
        send_message(token, chat_id, msg)
        return

    # BLUEPRINT INFO
    if first_word in ["/blueprint", "blueprint"]:
        msg = (
            "📐 *STANDAR BLUEPRINT PABRIK KONTEN*\n"
            "━━━━━━━━━━━━━━━━━━━━━━\n"
            "• *Durasi:* Acak 60 – 80 menit (optimal untuk retensi dan algoritma)\n"
            "• *Preset Render:* `ultrafast` (hemat CPU server)\n"
            "• *Visualizer:* Waveform di bawah (pos_y 85%, opacity 100%)\n"
            "• *Ending Special Song:* 1 lagu penutup di 2 menit terakhir\n"
            "• *Batas Deskripsi YouTube:* Maksimal 5.000 karakter\n"
            "• *Kapasitas Upload:* ~6 video per hari per project Google Cloud"
        )
        send_message(token, chat_id, msg)
        return

    # DEFAULT FALLBACK (Conversational)
    msg = (
        f"🤖 *Muse AI Operator*\n\n"
        f"Saya menerima pesan Anda: \"{text}\"\n\n"
        "Saya siap membantu mengelola pabrik konten Anda. "
        "Gunakan perintah `/status`, `/channel`, `/antrean`, atau `/judul <niche>` untuk menginstruksikan saya!"
    )
    send_message(token, chat_id, msg)

def watchdog_background_loop(token, chat_id):
    """Lapis 1 Watchdog: silent when healthy, alert only if failure or low schedule"""
    print("🛡️ [Muse Watchdog] Lapis 1 pengawas aktif...")
    last_alert_time = 0
    while True:
        try:
            time.sleep(300) # cek setiap 5 menit
            tasks_data = load_tasks()
            active = tasks_data.get('active', [])
            
            # 1. Cek apakah ada render yang gagal
            failed = [t for t in active if 'Gagal' in t.get('status', '') or 'Error' in t.get('status', '')]
            now = time.time()
            if failed and (now - last_alert_time > 3600):
                msg = (
                    "🚨 *PERINGATAN OPERATOR PABRIK KONTEN*\n\n"
                    f"Ditemukan `{len(failed)}` tugas render yang mengalami kendala!\n"
                    f"Contoh error: `{failed[0].get('title', 'Unknown')}` — {failed[0].get('status', '')}\n\n"
                    "Silakan periksa panel: `http://localhost:8000/factory`"
                )
                send_message(token, chat_id, msg)
                last_alert_time = now
        except Exception as e:
            print(f"Watchdog exception: {e}")
            time.sleep(60)

def main():
    config = load_config()
    token = config.get('tg_token')
    owner_chat_id = str(config.get('tg_chat_id', ''))

    if not token or not owner_chat_id:
        print("❌ Token Telegram atau Chat ID belum ditemukan di config.json!")
        return

    print("==================================================")
    print("🚀 MUSE AI OPERATOR: Menghubungkan ke Telegram...")
    print(f"🤖 Bot Token: {token[:12]}...")
    print(f"👤 Owner Chat ID: {owner_chat_id}")
    print("==================================================")

    # Kirim salam pembuka bahwa operator aktif
    startup_msg = (
        "🟢 *MUSE AI OPERATOR AKTIF*\n\n"
        "Halo Bos! Saya sekarang sudah resmi terhubung 2 arah dengan sistem Pabrik Konten Anda.\n\n"
        "Ketik `/status` atau `/start` di sini untuk mulai memberi instruksi kerja! 🚀"
    )
    send_message(token, owner_chat_id, startup_msg)

    # Jalankan background watchdog thread
    w_thread = threading.Thread(target=watchdog_background_loop, args=(token, owner_chat_id), daemon=True)
    w_thread.start()

    # Polling updates loop
    offset = 0
    while True:
        try:
            url = f"https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=20"
            res = requests.get(url, timeout=25).json()
            if res.get('ok') and res.get('result'):
                for update in res['result']:
                    offset = update['update_id'] + 1
                    msg = update.get('message')
                    if not msg:
                        continue
                    sender_id = str(msg.get('from', {}).get('id', ''))
                    text = msg.get('text', '')

                    # Verifikasi keamanan pemilik (hanya tanggapi owner)
                    if sender_id == owner_chat_id and text:
                        print(f"📩 Perintah diterima dari Bos: {text}")
                        handle_user_command(token, owner_chat_id, text)
                    elif text:
                        print(f"⚠️ Akses ditolak dari ID asing: {sender_id}")
            time.sleep(1)
        except Exception as e:
            print(f"Loop error: {e}")
            time.sleep(3)

if __name__ == '__main__':
    main()
