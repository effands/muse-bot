import audioop
import base64
import os
import re
import subprocess
import wave
from datetime import timedelta

import requests


HF_API_URL = "https://api-inference.huggingface.co/models/openai/whisper-large-v3"
GROQ_API_URL = "https://api.groq.com/openai/v1/audio/transcriptions"
GEMINI_API_URL = (
    "https://generativelanguage.googleapis.com/v1beta/models/"
    "gemini-1.5-flash:generateContent"
)


# Urutan default hanya digunakan jika token tidak memiliki urutan eksplisit.
# Token tetap dicoba satu per satu; jika gagal, provider berikutnya otomatis dipakai.
DEFAULT_PROVIDER_ORDER = ["huggingface", "groq", "gemini"]


def get_ffmpeg_path():
    return "/usr/bin/ffmpeg"


def format_srt_time(seconds):
    """Konversi detik ke format SRT: 00:00:00,000."""
    total_ms = max(0, round(float(seconds) * 1000))
    hours, remainder = divmod(total_ms, 3_600_000)
    minutes, remainder = divmod(remainder, 60_000)
    seconds_int, milliseconds = divmod(remainder, 1_000)
    return (
        f"{hours:02d}:{minutes:02d}:{seconds_int:02d},"
        f"{milliseconds:03d}"
    )


def get_viral_moments(source_video, count=1, duration_sec=40):
    """Deteksi momen dengan volume suara tertinggi."""
    temp_wav = source_video + "_temp.wav"
    try:
        subprocess.run(
            [
                get_ffmpeg_path(), "-y", "-i", source_video,
                "-vn", "-ac", "1", "-ar", "8000", "-f", "wav", temp_wav,
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )

        with wave.open(temp_wav, "rb") as wav_file:
            frames = wav_file.getnframes()
            rate = wav_file.getframerate()
            duration = frames / float(rate)

            if duration <= duration_sec:
                return [{"start": 0, "duration": int(duration)}]

            rms_values = []
            window_frames = rate
            for _ in range(int(duration)):
                data = wav_file.readframes(window_frames)
                rms_values.append(audioop.rms(data, 2) if data else 0)

        if os.path.exists(temp_wav):
            os.remove(temp_wav)

        candidates = []
        safe_limit = int(duration) - duration_sec
        for index in range(max(0, safe_limit)):
            window = rms_values[index:index + duration_sec]
            base = sum(window)
            # 🔥 FASE 6 (audit C1): skor bukan cuma volume total — jendela
            # yang energinya "hidup" (naik-turun seperti ucapan/momen,
            # bukan bunyi datar yang keras terus) diberi bonus. RMS tetap
            # dominan (bonus maks +30%).
            mean = base / max(1, len(window))
            if mean > 0:
                var = sum((v - mean) ** 2 for v in window) / len(window)
                variation = min(1.0, (var ** 0.5) / mean)
            else:
                variation = 0.0
            score = base * (1.0 + 0.3 * variation)
            candidates.append({
                "start": index,
                "end": index + duration_sec,
                "score": score,
            })

        candidates.sort(key=lambda item: item["score"], reverse=True)
        selected = []
        for candidate in candidates:
            overlaps = any(
                not (
                    candidate["end"] <= item["start"]
                    or candidate["start"] >= item["end"]
                )
                for item in selected
            )
            if not overlaps:
                selected.append(candidate)
            if len(selected) >= count:
                break

        selected.sort(key=lambda item: item["start"])
        # 🔥 FASE 6 (audit C1): potong di batas hening — klip jangan mulai
        # dengan detik-detik hening. Geser start maju ke detik pertama yang
        # bersuara (maks 5 detik, durasi tetap 40 detik).
        max_rms = max(rms_values) if rms_values else 0
        silence_threshold = max_rms * 0.08
        trimmed = []
        for item in selected:
            start = item["start"]
            shift = 0
            while (shift < 5 and (start + shift) < len(rms_values)
                   and rms_values[start + shift] <= silence_threshold):
                shift += 1
            trimmed.append({"start": str(start + shift), "duration": duration_sec})
        return trimmed

    except Exception:
        if os.path.exists(temp_wav):
            try:
                os.remove(temp_wav)
            except OSError:
                pass
        return [
            {"start": str(index * duration_sec), "duration": duration_sec}
            for index in range(count)
        ]


MOMENTS_CACHE_VERSION = 1


def get_viral_moments_cached(source_video, count=1, duration_sec=40):
    """🔥 FASE 6 (audit C2): cache hasil deteksi momen per video sumber.
    Batch dengan sumber yang sama tidak memindai ulang video penuh tiap klip
    — hasil disimpan di sidecar JSON di samping file sumber dan dipakai ulang
    selama ukuran + mtime file sumber tidak berubah. Fallback (exception di
    dalam get_viral_moments) TIDAK di-cache."""
    import json as _json

    cache_path = str(source_video) + ".moments.json"
    try:
        st = os.stat(source_video)
        key_size, key_mtime = st.st_size, st.st_mtime
    except OSError:
        return get_viral_moments(source_video, count=count, duration_sec=duration_sec)
    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            cached = _json.load(f)
        if (cached.get("version") == MOMENTS_CACHE_VERSION
                and cached.get("size") == key_size
                and cached.get("mtime") == key_mtime
                and cached.get("duration_sec") == duration_sec
                and isinstance(cached.get("moments"), list)
                and len(cached["moments"]) >= count):
            return [dict(m) for m in cached["moments"][:count]]
    except Exception:
        pass
    moments = get_viral_moments(source_video, count=count, duration_sec=duration_sec)
    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            _json.dump({"version": MOMENTS_CACHE_VERSION, "size": key_size,
                        "mtime": key_mtime, "duration_sec": duration_sec,
                        "moments": moments}, f)
    except Exception:
        pass
    return moments


def infer_provider(token):
    """Mengenali provider dari prefix token bila provider tidak disimpan."""
    value = str(token or "").strip()
    if value.startswith("hf_"):
        return "huggingface"
    if value.startswith("gsk_"):
        return "groq"
    if value.startswith("AIza"):
        return "gemini"
    return None


def normalize_api_keys(api_keys, legacy_provider=None):
    """
    Mengubah format token lama maupun format dashboard baru menjadi daftar:

    {
        "provider": "groq",
        "key": "..."
    }

    Format yang didukung:
    - ["hf_x", "gsk_x", "AIza..."]
    - [{"provider": "groq", "key": "gsk_x"}]
    - {"huggingface": ["hf_x"], "groq": ["gsk_x"]}
    """
    if not api_keys:
        return []

    if isinstance(api_keys, dict):
        items = []
        for provider, values in api_keys.items():
            if isinstance(values, str):
                values = [values]
            for value in values or []:
                items.append({"provider": provider, "key": value})
        api_keys = items

    normalized = []
    for item in api_keys:
        if isinstance(item, str):
            key = item.strip()
            provider = infer_provider(key) or legacy_provider
        elif isinstance(item, dict):
            key = str(item.get("key") or item.get("token") or "").strip()
            provider = item.get("provider") or item.get("service")
            provider = str(provider).strip().lower() if provider else None
        else:
            continue

        if not key:
            continue
        if provider:
            provider = provider.lower().replace(" ", "_")
        if provider in {"hf", "hugging_face"}:
            provider = "huggingface"
        if provider in {"google", "google_ai", "ai_studio"}:
            provider = "gemini"
        if provider in {"groq_whisper"}:
            provider = "groq"

        if provider in DEFAULT_PROVIDER_ORDER:
            normalized.append({"provider": provider, "key": key})

    return normalized


def _short_key(key):
    key = str(key)
    return f"...{key[-6:]}" if len(key) > 6 else "***"


def _write_hf_srt(result, srt_path):
    chunks = result.get("chunks") or []
    with open(srt_path, "w", encoding="utf-8") as srt_file:
        if chunks:
            for index, chunk in enumerate(chunks, start=1):
                timestamp = chunk.get("timestamp") or [0, None]
                start = float(timestamp[0] or 0)
                end = timestamp[1]
                end = float(end) if end is not None else start + 3
                text = str(chunk.get("text") or "").strip()
                if not text:
                    continue
                srt_file.write(
                    f"{index}\n{format_srt_time(start)} --> "
                    f"{format_srt_time(end)}\n{text}\n\n"
                )
        else:
            text = str(result.get("text") or "").strip()
            if not text:
                raise ValueError("Hugging Face tidak mengembalikan transkrip")
            srt_file.write(
                f"1\n00:00:00,000 --> 00:15:00,000\n{text}\n\n"
            )


def _write_groq_srt(result, srt_path):
    segments = result.get("segments") or []
    with open(srt_path, "w", encoding="utf-8") as srt_file:
        if not segments:
            text = str(result.get("text") or "").strip()
            if not text:
                raise ValueError("Groq tidak mengembalikan transkrip")
            segments = [{"start": 0, "end": 900, "text": text}]
        for index, segment in enumerate(segments, start=1):
            text = str(segment.get("text") or "").strip()
            if not text:
                continue
            start = float(segment.get("start") or 0)
            end = float(segment.get("end") or start + 3)
            srt_file.write(
                f"{index}\n{format_srt_time(start)} --> "
                f"{format_srt_time(end)}\n{text}\n\n"
            )


def _clean_srt_text(text):
    text = str(text or "").strip()
    text = re.sub(r"^```(?:srt|text)?\s*", "", text, flags=re.I)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _transcribe_one(audio_path, credential, srt_path):
    provider = credential["provider"]
    token = credential["key"]
    print(
        f"[AI Rotation] Provider={provider} token={_short_key(token)}"
    )

    if provider == "groq":
        with open(audio_path, "rb") as audio_file:
            response = requests.post(
                GROQ_API_URL,
                headers={"Authorization": f"Bearer {token}"},
                files={"file": ("audio.mp3", audio_file, "audio/mpeg")},
                data={
                    "model": "whisper-large-v3",
                    "response_format": "verbose_json",
                },
                timeout=180,
            )
        if response.status_code != 200:
            raise RuntimeError(f"Groq HTTP {response.status_code}: {response.text[:200]}")
        _write_groq_srt(response.json(), srt_path)
        return srt_path

    if provider == "gemini":
        with open(audio_path, "rb") as audio_file:
            audio_b64 = base64.b64encode(audio_file.read()).decode("utf-8")
        payload = {
            "contents": [{
                "parts": [
                    {
                        "text": (
                            "Transcribe this audio exactly. Return only valid raw "
                            "SubRip SRT text, with accurate timestamps. No markdown."
                        )
                    },
                    {
                        "inlineData": {
                            "mimeType": "audio/mpeg",
                            "data": audio_b64,
                        }
                    },
                ]
            }]
        }
        response = requests.post(
            f"{GEMINI_API_URL}?key={token}",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=240,
        )
        if response.status_code != 200:
            raise RuntimeError(
                f"Gemini HTTP {response.status_code}: {response.text[:200]}"
            )
        result = response.json()
        text = result["candidates"][0]["content"]["parts"][0]["text"]
        text = _clean_srt_text(text)
        if not text:
            raise ValueError("Gemini tidak mengembalikan transkrip")
        with open(srt_path, "w", encoding="utf-8") as srt_file:
            srt_file.write(text + "\n")
        return srt_path

    if provider == "huggingface":
        with open(audio_path, "rb") as audio_file:
            response = requests.post(
                HF_API_URL,
                headers={"Authorization": f"Bearer {token}"},
                data=audio_file.read(),
                timeout=240,
            )
        if response.status_code != 200:
            raise RuntimeError(
                f"Hugging Face HTTP {response.status_code}: "
                f"{response.text[:200]}"
            )
        _write_hf_srt(response.json(), srt_path)
        return srt_path

    raise ValueError(f"Provider tidak didukung: {provider}")


def generate_lyrics_srt(audio_path, api_keys, provider="auto"):
    """
    Transkripsi dengan rotasi global lintas semua provider.

    `provider` dipertahankan hanya untuk kompatibilitas kode lama. Jika
    bernilai auto, seluruh token dengan provider yang tercatat akan dicoba.
    Dashboard sebaiknya mengirim token sebagai object {provider, key}.
    """
    legacy_provider = None if provider in (None, "", "auto") else provider
    credentials = normalize_api_keys(api_keys, legacy_provider=legacy_provider)
    if not credentials:
        raise Exception(
            "Tidak ada API key yang valid. Tambahkan token beserta provider "
            "(huggingface, groq, atau gemini) dari dashboard."
        )

    srt_path = audio_path + ".srt"
    failures = []

    # Urutan list dashboard dipertahankan, sehingga rotasi benar-benar global.
    for credential in credentials:
        try:
            return _transcribe_one(audio_path, credential, srt_path)
        except Exception as exc:
            provider_name = credential["provider"]
            print(
                f"[AI Rotation] Gagal {provider_name} "
                f"({_short_key(credential['key'])}): {exc}"
            )
            failures.append(f"{provider_name}: {exc}")
            if os.path.exists(srt_path):
                try:
                    os.remove(srt_path)
                except OSError:
                    pass
            continue

    summary = " | ".join(failures[-8:])
    raise Exception(
        "SEMUA API KEY DARI SEMUA PROVIDER GAGAL. "
        f"Detail rotasi: {summary}"
    )


__all__ = [
    "get_viral_moments",
    "get_viral_moments_cached",
    "generate_lyrics_srt",
    "normalize_api_keys",
]
