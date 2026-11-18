import threading
import time

# Lock global untuk memastikan hanya ada 1 task berat di seluruh sistem
_heavy_task_lock = threading.Lock()

# Batas tunggu default acquire_resource(): 10 menit. Cukup longgar supaya
# render normal tidak gagal, tapi worker tidak pernah menggenggam antrean
# selamanya bila RAM tak kunjung lega atau lock tak kunjung lepas.
DEFAULT_ACQUIRE_TIMEOUT = 600.0
_RAM_WAIT_POLL_SECONDS = 10.0


def check_ram_ok(max_ram_pct=85.0):
    """Mengecek apakah RAM masih di bawah batas aman."""
    try:
        import psutil
        mem = psutil.virtual_memory()
        return mem.percent < max_ram_pct
    except ImportError:
        return True # Abaikan jika psutil tidak ada

def acquire_resource(worker_name="Worker", timeout=DEFAULT_ACQUIRE_TIMEOUT):
    """
    Mengantre untuk mendapatkan akses CPU/RAM.

    Urutan diperbaiki (dulu: lock dipegang dulu, baru cek RAM sambil tidur
    tanpa batas — worker pertama bisa menggenggam lock selamanya saat RAM
    >85% dan memblokir semua worker lain):
      1. Tunggu RAM lega TANPA memegang lock (poll tiap 10 dtk).
      2. Setelah RAM OK, baru acquire lock (dengan timeout).
    Bila batas waktu habis di langkah mana pun, angkat TimeoutError supaya
    pemanggil bisa gagal dengan jelas / menjadwalkan ulang — jangan pernah
    menggantung tanpa batas. Pemanggil lama yang memanggil tanpa argumen
    timeout tetap jalan (default 600 dtk).
    """
    print(f"⏳ [{worker_name}] Meminta akses resource...")
    deadline = time.monotonic() + float(timeout)

    # 1. Tunggu RAM lega TANPA memegang lock.
    while not check_ram_ok():
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"[{worker_name}] Timeout {timeout:.0f} dtk menunggu RAM lega (<85%)"
            )
        print(f"⚠️ [{worker_name}] RAM penuh (>85%). Menunggu lega...")
        time.sleep(min(_RAM_WAIT_POLL_SECONDS, remaining))

    # 2. RAM OK — baru ambil lock, dengan sisa waktu sebagai batas.
    remaining = deadline - time.monotonic()
    if remaining <= 0:
        raise TimeoutError(
            f"[{worker_name}] Timeout {timeout:.0f} dtk menunggu resource"
        )
    acquired = _heavy_task_lock.acquire(timeout=remaining)
    if not acquired:
        raise TimeoutError(
            f"[{worker_name}] Timeout {timeout:.0f} dtk menunggu lock resource "
            "(masih dipakai worker lain)"
        )

    print(f"🟢 [{worker_name}] Resource diamankan. Mulai eksekusi!")

def release_resource(worker_name="Worker"):
    """Melepas akses agar antrean modul lain bisa berjalan."""
    try:
        _heavy_task_lock.release()
        print(f"🔓 [{worker_name}] Resource dilepas untuk antrean berikutnya.")
    except RuntimeError:
        pass # Abaikan jika lock sudah terlepas
