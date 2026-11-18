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

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
CHANNELS_FILE = os.path.join(BASE_DIR, 'channels_db.json')
TASKS_FILE = os.path.join(BASE_DIR, 'tasks_db.json')

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
                "🤖 *MUSE AI — OPERATOR PABRIK KONTEN*\n\n"
                "Halo Bos! Saya siap menerima instruksi kerja. Sentuh salah satu tombol di bawah atau klik perintah berikut:\n\n"
                "👉 /status — Cek kesehatan CPU, RAM, & antrean\n"
                "👉 /channel — Cek channel YouTube terdaftar\n"
                "👉 /antrean — Cek daftar video yang sedang diproses\n"
                "👉 /judul — Buatkan 10 judul video tidur viral\n"
                "👉 /deskripsi — Buatkan template deskripsi SEO\n"
                "👉 /blueprint — Cek resep settingan video"
            )
            send_message(token, chat_id, msg)
            return

        # 2. STATUS SERVER
        if "status" in clean_cmd or "server" in clean_cmd:
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
                f"🏭 *Antrean Render:* `{in_progress} video`\n"
                f"📁 *Riwayat Selesai:* `{len(tasks_data.get('history', []))} video`\n"
                "🟢 *Node Engine:* Online (Port 8000)"
            )
            send_message(token, chat_id, msg)
            return

        # 3. CHANNEL LIST
        if "channel" in clean_cmd:
            channels = load_channels()
            if not channels:
                msg = (
                    "📺 *DAFTAR CHANNEL YOUTUBE*\n"
                    "Belum ada channel terhubung di panel.\n\n"
                    "👉 Buka browser: http://localhost:8000/factory\n"
                    "Lalu klik tombol *Connect* untuk menautkan channel YouTube pertama."
                )
            else:
                msg = f"📺 *DAFTAR CHANNEL YOUTUBE ({len(channels)})*\n━━━━━━━━━━━━━━━━━━━━━━\n"
                for idx, ch in enumerate(channels, 1):
                    name = ch.get('channel_name', 'Tanpa Nama')
                    connected = "🟢 Terhubung" if ch.get('access_token') or ch.get('refresh_token') else "🔴 Putus"
                    msg += f"*{idx}. {name}*\nStatus: {connected}\nID: `{ch.get('yt_id', '-')}`\n\n"
            send_message(token, chat_id, msg)
            return

        # 4. ANTREAN RENDER
        if "antrean" in clean_cmd or "queue" in clean_cmd:
            tasks_data = load_tasks()
            active = tasks_data.get('active', [])
            if not active:
                msg = "📂 *ANTREAN RENDER KOSONG*\nSaat ini belum ada tugas render yang dijadwalkan."
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
                f"💡 *BANK JUDUL VIRAL & SEO ({count} Judul)*\n"
                f"🎯 *Target Global:* `{niche}`\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"{title_list}\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "📋 Salin judul di atas ke kolom *Bank Judul* channel Anda di browser!"
            )
            send_message(token, chat_id, msg)
            return

        # 6. TEMPLATE DESKRIPSI
        if "deskripsi" in clean_cmd or "description" in clean_cmd:
            niche = "Deep Sleep Music"
            msg = (
                f"📝 *TEMPLATE DESKRIPSI SEO YOUTUBE*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                f"Welcome to your ultimate sanctuary for relaxation and deep sleep. 🌙✨\n\n"
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

        # 7. BLUEPRINT RESEP
        if "blueprint" in clean_cmd or "resep" in clean_cmd:
            msg = (
                "📐 *STANDAR BLUEPRINT PABRIK KONTEN*\n"
                "━━━━━━━━━━━━━━━━━━━━━━\n"
                "• *Durasi:* Acak 60 – 80 menit (optimal untuk retensi dan algoritma)\n"
                "• *Preset Render:* ultrafast (hemat CPU server)\n"
                "• *Visualizer:* Waveform di bawah (pos_y 85%, opacity 100%)\n"
                "• *Ending Special Song:* 1 lagu penutup di 2 menit terakhir\n"
                "• *Batas Deskripsi YouTube:* Maksimal 5.000 karakter\n"
                "• *Kapasitas Upload:* ~6 video per hari per project Google Cloud"
            )
            send_message(token, chat_id, msg)
            return

        # DEFAULT FALLBACK
        msg = (
            f"🤖 *Muse AI Operator*\n\n"
            f"Saya menerima pesan: \"{raw_cmd}\"\n\n"
            "Silakan sentuh tombol di bawah atau ketik:\n"
            "👉 /status | /channel | /antrean | /judul"
        )
        send_message(token, chat_id, msg)

    except Exception as e:
        print(f"Error di handle_user_command: {e}", flush=True)
        send_message(token, chat_id, f"⚠️ Maaf bos, ada kendala: `{e}`")

def watchdog_background_loop(token, chat_id):
    print("🛡️ [Muse Watchdog] Lapis 1 pengawas aktif...", flush=True)
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
                    "🚨 *PERINGATAN OPERATOR PABRIK KONTEN*\n\n"
                    f"Ditemukan `{len(failed)}` tugas render yang mengalami kendala!\n"
                    f"Contoh error: `{failed[0].get('title', 'Unknown')}` — {failed[0].get('status', '')}\n\n"
                    "Silakan periksa panel: http://localhost:8000/factory"
                )
                send_message(token, chat_id, msg)
                last_alert_time = now
        except Exception as e:
            print(f"Watchdog exception: {e}", flush=True)
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
    print(f"🤖 Bot Token: {token[:12]}...", flush=True)
    print(f"👤 Owner Chat ID: {owner_chat_id}", flush=True)
    print("==================================================", flush=True)

    w_thread = threading.Thread(target=watchdog_background_loop, args=(token, owner_chat_id), daemon=True)
    w_thread.start()

    # Kirim salam & pasang keyboard menu interaktif
    welcome_msg = (
        "🟢 *MUSE AI OPERATOR SIAP!*\n\n"
        "Sistem sudah diperbarui. Sekarang semua tombol di bawah sudah aktif merespons seketika saat disentuh! 🚀"
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
