#!/usr/bin/env python3
# ============================================================
#  ZIMUSE BOT — Mesin Backup Otomatis (backup_core.py)
#  Modul murni stdlib: TIDAK mengimpor app.py / Flask, supaya bisa
#  dipakai dari tiga tempat tanpa efek samping:
#    1) app.py        — endpoint panel + scheduler harian + hook pre-update
#    2) update.sh     — backup pra-update saat update dijalankan dari konsol
#    3) restore.sh    — restore dari konsol
#
#  Format arsip:  backup-YYYY-MM-DD-HHMM.tar.gz[.enc]
#    - .tar.gz  : tar gzip berisi file state + galeri + manifest.json
#    - .enc     : dienkripsi AES-256-CBC + PBKDF2 via openssl (tanpa
#                 dependensi Python baru — cryptography TIDAK ada di
#                 requirements.txt dan update.sh tidak pip install).
#                 Kunci 32 byte (hex) digenerate sekali, disimpan di
#                 config.json['backup_key'] + cermin backup_key.txt
#                 (chmod 600, keduanya gitignored). Tanpa kunci ini,
#                 backup terenkripsi TIDAK bisa dibuka — kunci bisa
#                 dilihat/dikopi pemilik lewat panel (login + CSRF).
#    - Bila openssl tidak ditemukan: arsip polos .tar.gz + peringatan
#      jelas di metadata (backup tetap jalan, tanpa enkripsi).
#
#  Keamanan: isi backup TIDAK PERNAH di-log / dikirim ke Telegram /
#  dimasukkan ke git. Download hanya lewat panel (login + CSRF).
#  Kunci TIDAK PERNAH muncul di log — subprocess openssl menerima
#  kunci lewat env var (bukan argv, supaya tidak terlihat di `ps`).
# ============================================================
import argparse
import datetime
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tarfile
import time

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
META_FILE = os.path.join(BACKUP_DIR, 'backups.json')
KEY_FILE = os.path.join(BASE_DIR, 'backup_key.txt')
CONFIG_FILE = os.path.join(BASE_DIR, 'config.json')
LOCK_FILE = os.path.join(BACKUP_DIR, '.backup.lock')

# File state yang WAJIB masuk backup (bila ada; yang belum ada di-skip
# dan dicatat di manifest — mis. playlist_jobs.json belum ada di VPS
# lama sebelum v1.2.7).
STATE_FILES = [
    'config.json', 'channels_db.json', 'tasks_db.json',
    'client_secret.json', 'oauth_clients.json', 'monitor_state.json',
    'video_stats_cache.json', 'auto_shorts_state.json',
    'playlist_jobs.json', 'nlu_audit.jsonl',
    'presets.json', 'VERSION',
]
# 🔥 v1.2.25: file secret per client (oauth_clients/*.json) ikut backup
# seperti client_secret.json — tanpa ini, restore kehilangan client
# cadangan. Restore hanya boleh mengeluarkan awalan yang diizinkan.
_OAUTH_CLIENTS_PREFIX = 'oauth_clients/'
# Galeri: semua file di uploads/ KECUALI file kerja sementara yang bisa
# di-generate ulang (temp render, segmen TTS, concat, playlist_tmp).
_GALLERY_SKIP_PREFIXES = ('temp_', 'loop_', 'shorts_voice_', 'shorts_seg_',
                         'shorts_ass_', 'shorts_bgconcat_', 'zoomlog_')
_GALLERY_SKIP_DIRS = {'playlist_tmp'}
_GALLERY_SKIP_SUFFIXES = ('.tmp',)
_VIDEO_EXTS = {'.mp4', '.mov', '.webm', '.avi', '.mkv', '.m4v'}
MEDIA_MAX_BYTES = 2 * 1024 * 1024 * 1024   # 2 GiB: galeri di atas ini -> video di-skip
LOCK_STALE_SECONDS = 30 * 60               # lock lebih tua dari ini dianggap macet
PRE_UPDATE_DEDUPE_SECONDS = 10 * 60        # backup pre-update <10 mnt lalu -> pakai yang lama
_FILENAME_RE = re.compile(r'^backup-\d{4}-\d{2}-\d{2}-\d{4}\.tar\.gz(\.enc)?$')
# Awalan path yang BOLEH keluar dari arsip saat restore (anti path-traversal).
_ALLOWED_RESTORE_PREFIXES = tuple(STATE_FILES) + ('uploads/', 'manifest.json', 'oauth_clients/')


class BackupError(Exception):
    pass


class BackupBusy(BackupError):
    pass


# ---------------- util dasar ----------------

def _wib_now():
    try:
        from zoneinfo import ZoneInfo
        return datetime.datetime.now(ZoneInfo('Asia/Jakarta'))
    except Exception:
        return datetime.datetime.now() + datetime.timedelta(hours=7)


def _sha256(path):
    h = hashlib.sha256()
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b''):
            h.update(chunk)
    return h.hexdigest()


def _human(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.1f} {unit}' if unit != 'B' else f'{int(n)} B'
        n /= 1024.0
    return f'{n:.1f} GB'


def _load_config_raw():
    try:
        with open(CONFIG_FILE, encoding='utf-8') as f:
            return json.load(f)
    except Exception:
        return {}


def _save_config_raw(cfg):
    tmp = CONFIG_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(cfg, f, indent=4)
    try:
        os.chmod(tmp, 0o600)
    except Exception:
        pass
    os.replace(tmp, CONFIG_FILE)


def _load_meta():
    try:
        with open(META_FILE, encoding='utf-8') as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except Exception:
        return []


def _save_meta(items):
    os.makedirs(BACKUP_DIR, exist_ok=True)
    tmp = META_FILE + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump(items, f, indent=2, ensure_ascii=False)
    os.replace(tmp, META_FILE)


def _valid_backup_name(name):
    return bool(name) and bool(_FILENAME_RE.match(name)) and '/' not in name and '\\' not in name


def _backup_path(name):
    if not _valid_backup_name(name):
        raise BackupError('Nama backup tidak valid.')
    p = os.path.join(BACKUP_DIR, name)
    if os.path.commonpath([os.path.abspath(p), os.path.abspath(BACKUP_DIR)]) != os.path.abspath(BACKUP_DIR):
        raise BackupError('Nama backup tidak valid.')
    return p


# ---------------- kunci enkripsi ----------------

def get_or_create_key():
    """Kunci backup (hex 64 karakter). Diambil dari config.json bila ada,
    kalau belum ada digenerate sekali lalu disimpan di config.json +
    cermin backup_key.txt. Kunci TIDAK PERNAH di-log."""
    cfg = _load_config_raw()
    key = (cfg.get('backup_key') or '').strip()
    if key and len(key) >= 32:
        _mirror_key(key)
        return key
    key = secrets.token_hex(32)
    cfg['backup_key'] = key
    try:
        _save_config_raw(cfg)
    except Exception:
        pass  # config gagal ditulis (mis. read-only) — cermin file tetap dibuat
    _mirror_key(key)
    return key


def _mirror_key(key):
    try:
        with open(KEY_FILE, 'w', encoding='utf-8') as f:
            f.write(key.strip() + '\n')
        os.chmod(KEY_FILE, 0o600)
    except Exception:
        pass


def read_key():
    """Baca kunci dari config.json, lalu cermin backup_key.txt.
    Mengembalikan '' bila tidak ada di mana-mana."""
    cfg = _load_config_raw()
    key = (cfg.get('backup_key') or '').strip()
    if key:
        return key
    try:
        with open(KEY_FILE, encoding='utf-8') as f:
            return f.read().strip()
    except Exception:
        return ''


# ---------------- daftar file ----------------

def _gather_files(media_max_bytes=MEDIA_MAX_BYTES):
    """Kumpulkan file untuk backup.
    Mengembalikan (files, media_total, media_skipped_files, media_included)
    files = daftar (path_absolut, arcname_rel_ke_BASE_DIR)."""
    files = []
    state_included = []
    for name in STATE_FILES:
        p = os.path.join(BASE_DIR, name)
        if os.path.isfile(p):
            files.append((p, name))
            state_included.append(name)
    # 🔥 v1.2.25: file secret per client (oauth_clients/*.json).
    _oc_dir = os.path.join(BASE_DIR, 'oauth_clients')
    if os.path.isdir(_oc_dir):
        for nm in sorted(os.listdir(_oc_dir)):
            if not nm.endswith('.json'):
                continue
            p = os.path.join(_oc_dir, nm)
            if os.path.isfile(p):
                files.append((p, f'oauth_clients/{nm}'))
                state_included.append(f'oauth_clients/{nm}')
    # Galeri uploads/
    uploads_dir = os.path.join(BASE_DIR, 'uploads')
    gallery = []
    media_total = 0
    if os.path.isdir(uploads_dir):
        for root, dirs, names in os.walk(uploads_dir):
            dirs[:] = [d for d in dirs if d not in _GALLERY_SKIP_DIRS]
            for nm in names:
                if nm.startswith(_GALLERY_SKIP_PREFIXES) or nm.endswith(_GALLERY_SKIP_SUFFIXES):
                    continue
                p = os.path.join(root, nm)
                try:
                    sz = os.path.getsize(p)
                except Exception:
                    continue
                gallery.append((p, os.path.relpath(p, BASE_DIR), sz))
                media_total += sz
    media_included = True
    media_skipped = []
    if media_total > media_max_bytes:
        # Galeri terlalu besar: video di-skip, gambar + audio + manifest tetap ikut.
        media_included = False
        kept = []
        for p, arc, sz in gallery:
            if os.path.splitext(p)[1].lower() in _VIDEO_EXTS:
                media_skipped.append(arc)
            else:
                kept.append((p, arc))
        gallery = kept
    else:
        gallery = [(p, arc) for p, arc, _sz in gallery]
    files.extend(gallery)
    return files, state_included, media_total, media_skipped, media_included


# ---------------- lock ----------------

class _BackupLock:
    """Lockfile supaya dua backup tidak jalan bersamaan (panel + cron +
    update.sh). Lock macet >30 menit dianggap basi dan dibersihkan."""

    def __enter__(self):
        os.makedirs(BACKUP_DIR, exist_ok=True)
        try:
            fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(fd, str(time.time()).encode())
            os.close(fd)
            return self
        except FileExistsError:
            try:
                age = time.time() - os.path.getmtime(LOCK_FILE)
            except Exception:
                age = 0
            if age > LOCK_STALE_SECONDS:
                try:
                    os.remove(LOCK_FILE)
                except Exception:
                    pass
                fd = os.open(LOCK_FILE, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(time.time()).encode())
                os.close(fd)
                return self
            raise BackupBusy('Backup lain sedang berjalan. Coba lagi sebentar lagi.')

    def __exit__(self, *exc):
        try:
            os.remove(LOCK_FILE)
        except Exception:
            pass
        return False


# ---------------- buat backup ----------------

def create_backup(trigger='manual', media_max_bytes=MEDIA_MAX_BYTES):
    """Buat satu backup. Mengembalikan dict metadata (atau dict dengan
    'skipped': True bila dedupe pre-update). Melempar BackupError bila gagal."""
    os.makedirs(BACKUP_DIR, exist_ok=True)
    try:
        os.chmod(BACKUP_DIR, 0o700)
    except Exception:
        pass
    now = _wib_now()
    ts = now.timestamp()
    # Dedupe pre-update: backup <10 menit lalu sudah cukup.
    if trigger == 'pre_update':
        metas = _load_meta()
        if metas:
            newest = max(metas, key=lambda m: m.get('created_ts', 0))
            if ts - newest.get('created_ts', 0) < PRE_UPDATE_DEDUPE_SECONDS:
                return dict(newest, skipped=True,
                            note='Backup pra-update sudah ada <10 menit lalu — dipakai yang itu.')
    with _BackupLock():
        files, state_included, media_total, media_skipped, media_included = _gather_files(media_max_bytes)
        est = sum(os.path.getsize(p) for p, _a in files) + 10 * 1024 * 1024
        try:
            free = shutil.disk_usage(BASE_DIR).free
        except Exception:
            free = None
        if free is not None and free < 3 * est:
            raise BackupError(f'Disk tidak cukup untuk backup (butuh ~{_human(3 * est)}, sisa {_human(free)}).')
        stamp = now.strftime('%Y-%m-%d-%H%M')
        plain_name = f'backup-{stamp}.tar.gz'
        tmp_tar = os.path.join(BACKUP_DIR, f'.tmp-{stamp}.tar.gz')
        manifest = {
            'version': _read_version(),
            'created_at_wib': now.strftime('%Y-%m-%d %H:%M'),
            'trigger': trigger,
            'files': [],
            'state_files': state_included,
            'media_included': media_included,
            'media_skipped_files': media_skipped,
            'media_total_bytes': media_total,
        }
        for p, arc in files:
            manifest['files'].append({'path': arc, 'size': os.path.getsize(p),
                                      'sha256': _sha256(p)})
        # Tulis tar.gz sementara.
        with tarfile.open(tmp_tar, 'w:gz', compresslevel=6) as tf:
            for p, arc in files:
                tf.add(p, arcname=arc)
            data = json.dumps(manifest, indent=2, ensure_ascii=False).encode('utf-8')
            info = tarfile.TarInfo('manifest.json')
            info.size = len(data)
            info.mtime = int(ts)
            tf.addfile(info, io.BytesIO(data))
        # Enkripsi (openssl) bila tersedia; kalau tidak, arsip polos + peringatan.
        key = get_or_create_key()
        encrypted = False
        warn = ''
        final_name = plain_name
        final_path = os.path.join(BACKUP_DIR, final_name)
        if shutil.which('openssl'):
            enc_name = plain_name + '.enc'
            enc_path = os.path.join(BACKUP_DIR, enc_name)
            env = dict(os.environ)
            env['BACKUP_KEY'] = key
            r = subprocess.run(['openssl', 'enc', '-aes-256-cbc', '-salt', '-pbkdf2',
                                '-in', tmp_tar, '-out', enc_path, '-pass', 'env:BACKUP_KEY'],
                               env=env, capture_output=True, timeout=900)
            try:
                os.remove(tmp_tar)
            except Exception:
                pass
            if r.returncode != 0:
                try:
                    os.remove(enc_path)
                except Exception:
                    pass
                raise BackupError('Enkripsi backup gagal (openssl). Backup dibatalkan — tidak ada file setengah jadi.')
            final_name, final_path, encrypted = enc_name, enc_path, True
        else:
            os.replace(tmp_tar, final_path)
            warn = 'TANPA enkripsi — openssl tidak ditemukan di VPS.'
        try:
            os.chmod(final_path, 0o600)
        except Exception:
            pass
        size = os.path.getsize(final_path)
        meta = {
            'filename': final_name,
            'created_at': now.strftime('%Y-%m-%d %H:%M'),
            'created_ts': ts,
            'size_bytes': size,
            'size_human': _human(size),
            'encrypted': encrypted,
            'trigger': trigger,
            'includes_media': media_included,
            'media_skipped': bool(media_skipped),
            'media_skipped_files': media_skipped,
            'media_total_bytes': media_total,
            'files_count': len(manifest['files']),
            'state_files': state_included,
            'version': manifest['version'],
            'note': warn,
        }
        metas = _load_meta()
        metas.append(meta)
        metas.sort(key=lambda m: m.get('created_ts', 0))
        _save_meta(metas)
        prune_backups()
        return meta


# v1.2.26: retensi MAKSIMAL 3 backup terbaru (minta user 16:48 WIB —
# hemat disk). Harian + pra-update + manual dihitung BERSAMA, hanya
# 3 terbaru yang disimpan; sisanya dihapus otomatis saat backup baru
# dibuat (dan sekali saat startup panel — lihat app.py).
MAX_BACKUPS = 3


def _read_version():
    try:
        with open(os.path.join(BASE_DIR, 'VERSION'), encoding='utf-8') as f:
            return f.read().strip().split()[0]
    except Exception:
        return ''


# ---------------- daftar & retensi ----------------

def list_backups():
    metas = _load_meta()
    # Sinkronkan dengan file yang benar-benar ada (hapus entri yatim).
    metas = [m for m in metas if os.path.isfile(os.path.join(BACKUP_DIR, m.get('filename', '')))]
    metas.sort(key=lambda m: m.get('created_ts', 0), reverse=True)
    return metas


def prune_backups():
    """Retensi v1.2.26: MAKSIMAL 3 backup terbaru secara total — harian,
    pra-update, dan manual dihitung bersama (3 terbaru disimpan,
    sisanya dihapus). Mengembalikan daftar nama file yang dihapus."""
    metas = _load_meta()
    metas.sort(key=lambda m: m.get('created_ts', 0), reverse=True)
    keep = metas[:MAX_BACKUPS]
    keep_names = {m.get('filename') for m in keep}
    deleted = []
    for m in metas:
        if m.get('filename') not in keep_names:
            try:
                os.remove(os.path.join(BACKUP_DIR, m['filename']))
                deleted.append(m['filename'])
            except Exception:
                pass
    _save_meta(keep)
    return deleted


def delete_backup(name):
    p = _backup_path(name)
    if not os.path.isfile(p):
        raise BackupError('Backup tidak ditemukan.')
    os.remove(p)
    metas = [m for m in _load_meta() if m.get('filename') != name]
    _save_meta(metas)
    return True


# ---------------- restore ----------------

def _safe_members(tf):
    out = []
    for m in tf.getmembers():
        name = m.name
        if os.path.isabs(name) or '..' in name.split('/'):
            raise BackupError(f'Arsip berisi path tidak aman: {name}')
        if not (name == 'manifest.json' or name.startswith('uploads/') or name.startswith('oauth_clients/') or name in STATE_FILES):
            raise BackupError(f'Arsip berisi file di luar cakupan backup: {name}')
        out.append(m)
    return out


def _verify_manifest(tmpdir, manifest):
    for entry in manifest.get('files', []):
        p = os.path.join(tmpdir, entry['path'])
        if not os.path.isfile(p):
            raise BackupError(f"File hilang di arsip: {entry['path']}")
        if _sha256(p) != entry.get('sha256'):
            raise BackupError(f"Hash tidak cocok (arsip rusak?): {entry['path']}")


def restore_backup(name, key=None, confirm=False):
    """Restore satu backup. WAJIB confirm=True (tanpa konfirmasi -> ditolak).
    Langkah: validasi nama -> dekripsi ke temp -> verifikasi hash manifest
    -> snapshot pengaman file yang akan ditimpa -> timpa file -> selesai.
    Restart service DILAKUKAN pemanggil (panel / restore.sh), bukan di sini.
    Mengembalikan daftar file yang dipulihkan."""
    if not confirm:
        raise BackupError('Restore butuh konfirmasi eksplisit (confirm=True).')
    path = _backup_path(name)
    if not os.path.isfile(path):
        raise BackupError('Backup tidak ditemukan.')
    encrypted = name.endswith('.enc')
    use_key = (key or '').strip() or read_key()
    if encrypted and not use_key:
        raise BackupError('Backup ini terenkripsi tapi kunci backup tidak ditemukan. Tempel kunci backup di form restore (lihat kartu Backup di Settings).')
    work = os.path.join(BACKUP_DIR, '.restore-tmp')
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work, exist_ok=True)
    try:
        tar_path = path
        if encrypted:
            tar_path = os.path.join(work, 'backup.tar.gz')
            env = dict(os.environ)
            env['BACKUP_KEY'] = use_key
            r = subprocess.run(['openssl', 'enc', '-d', '-aes-256-cbc', '-pbkdf2',
                                '-in', path, '-out', tar_path, '-pass', 'env:BACKUP_KEY'],
                               env=env, capture_output=True, timeout=600)
            if r.returncode != 0:
                raise BackupError('Dekripsi gagal — kunci backup salah atau arsip rusak.')
        with tarfile.open(tar_path, 'r:gz') as tf:
            members = _safe_members(tf)
            tf.extractall(work, members=members)
        manifest_path = os.path.join(work, 'manifest.json')
        if not os.path.isfile(manifest_path):
            raise BackupError('Manifest tidak ditemukan di arsip — arsip bukan backup ZIMUSE yang valid.')
        with open(manifest_path, encoding='utf-8') as f:
            manifest = json.load(f)
        _verify_manifest(work, manifest)
        # Snapshot pengaman: salin dulu file yang akan ditimpa (bisa rollback).
        stamp = _wib_now().strftime('%Y-%m-%d-%H%M%S')
        rollback = os.path.join(BACKUP_DIR, f'rollback-{stamp}.tar.gz')
        with tarfile.open(rollback, 'w:gz', compresslevel=6) as tf:
            for entry in manifest.get('files', []):
                cur = os.path.join(BASE_DIR, entry['path'])
                if os.path.isfile(cur):
                    tf.add(cur, arcname=entry['path'])
        try:
            os.chmod(rollback, 0o600)
        except Exception:
            pass
        # Timpa file (manifest.json sendiri TIDAK ikut dipulihkan ke BASE_DIR).
        restored = []
        for entry in manifest.get('files', []):
            src = os.path.join(work, entry['path'])
            dst = os.path.join(BASE_DIR, entry['path'])
            if os.path.commonpath([os.path.abspath(dst), os.path.abspath(BASE_DIR)]) != os.path.abspath(BASE_DIR):
                raise BackupError(f"Path tujuan tidak aman: {entry['path']}")
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
            restored.append(entry['path'])
        # Bersihkan rollback lama (simpan 3 terbaru).
        try:
            rbs = sorted([f for f in os.listdir(BACKUP_DIR) if f.startswith('rollback-') and f.endswith('.tar.gz')])
            for old in rbs[:-3]:
                os.remove(os.path.join(BACKUP_DIR, old))
        except Exception:
            pass
        return restored
    finally:
        shutil.rmtree(work, ignore_errors=True)


# ---------------- CLI (dipakai update.sh & restore.sh) ----------------

def main(argv=None):
    ap = argparse.ArgumentParser(description='Backup otomatis ZIMUSE BOT')
    sub = ap.add_subparsers(dest='cmd', required=True)
    p_b = sub.add_parser('backup', help='Buat backup sekarang')
    p_b.add_argument('--trigger', default='manual')
    p_b.add_argument('--media-max-mb', type=float, default=None,
                     help='Batas ukuran galeri (MB) — untuk tes saja')
    p_r = sub.add_parser('restore', help='Restore satu backup (butuh --confirm)')
    p_r.add_argument('--file', required=True)
    p_r.add_argument('--key', default=None)
    p_r.add_argument('--confirm', action='store_true')
    sub.add_parser('list', help='Daftar backup')
    sub.add_parser('prune', help='Jalankan retensi sekarang')
    args = ap.parse_args(argv)
    try:
        if args.cmd == 'backup':
            mm = MEDIA_MAX_BYTES if args.media_max_mb is None else int(args.media_max_mb * 1024 * 1024)
            meta = create_backup(trigger=args.trigger, media_max_bytes=mm)
            if meta.get('skipped'):
                print(f"SKIP: {meta.get('note','')} (pakai {meta['filename']})")
            else:
                print(f"OK: {meta['filename']} ({meta['size_human']}, terenkripsi={meta['encrypted']}, {meta['files_count']} file)")
            return 0
        if args.cmd == 'restore':
            restored = restore_backup(args.file, key=args.key, confirm=args.confirm)
            print(f"OK: {len(restored)} file dipulihkan dari {args.file}")
            return 0
        if args.cmd == 'list':
            for m in list_backups():
                print(f"{m['filename']}  {m.get('created_at','?')}  {m.get('size_human','?')}  trigger={m.get('trigger','?')}")
            return 0
        if args.cmd == 'prune':
            print('Dihapus:', prune_backups() or 'tidak ada')
            return 0
    except BackupBusy as e:
        print(f'BUSY: {e}', file=sys.stderr)
        return 2
    except BackupError as e:
        print(f'GAGAL: {e}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
