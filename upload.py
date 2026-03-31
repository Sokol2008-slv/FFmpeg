"""
Upload & Process — принимает видео файл напрямую, обрабатывает в фоне,
пишет статус в Supabase video_jobs.
"""

import os
import uuid
import json
import asyncio
from pathlib import Path
from datetime import datetime, timezone
from fastapi import APIRouter, UploadFile, File, Request, HTTPException
from typing import Optional

router = APIRouter()

WORK_DIR = Path("/tmp/kaizen-ffmpeg")

# Supabase клиент (lazy init)
_supabase = None


def get_supabase():
    global _supabase
    if _supabase is None:
        from supabase import create_client
        url = os.environ.get("SUPABASE_URL")
        key = os.environ.get("SUPABASE_SERVICE_KEY")
        if not url or not key:
            raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_KEY not set")
        _supabase = create_client(url, key)
    return _supabase


def parse_job_options(request: Request) -> dict:
    """Парсим опции из x-job-options header."""
    raw = request.headers.get("x-job-options", "{}")
    try:
        opts = json.loads(raw)
    except json.JSONDecodeError:
        opts = {}
    return {
        "opt_fillers": bool(opts.get("optFillers", True)),
        "opt_subtitles": bool(opts.get("optSubtitles", True)),
        "opt_subtitles_lang": opts.get("optSubtitlesLang", "auto"),
        "opt_color": bool(opts.get("optColor", False)),
    }


async def process_job(job_id: str, video_path: Path, options: dict):
    """Фоновая обработка видео: филлеры → субтитры → цветокоррекция."""
    sb = get_supabase()

    try:
        # Обновляем статус на processing
        sb.table("video_jobs").update({
            "status": "processing",
        }).eq("id", job_id).execute()

        from subtitles import (
            get_video_info, extract_audio, transcribe_whisper,
            identify_cuts, remap_timestamps, trim_video,
            generate_ass, burn_subtitles
        )

        info = await get_video_info(video_path)
        current_video = video_path
        job_dir = video_path.parent

        # --- 1. Удаление филлеров и тишины ---
        if options["opt_fillers"]:
            audio_path = job_dir / "audio.mp3"
            await extract_audio(video_path, audio_path)

            lang = options["opt_subtitles_lang"]
            if lang == "auto":
                lang = None  # Whisper auto-detect

            transcript = await transcribe_whisper(audio_path, lang)
            words = transcript.get("words", [])

            if words:
                from subtitles import FILLERS_RU, FILLERS_EN
                fillers = FILLERS_RU if (lang or "ru") == "ru" else FILLERS_EN

                skip_indices = set()
                for i, w in enumerate(words):
                    clean = w["word"].strip().lower().rstrip(".,!?;:")
                    if clean in fillers:
                        skip_indices.add(i)

                segments = identify_cuts(
                    words, lang or "ru", True, True, 0.7, info["duration"]
                )

                trimmed_path = job_dir / "trimmed.mp4"
                await trim_video(video_path, segments, trimmed_path, info)
                current_video = trimmed_path

                remapped_words = remap_timestamps(words, segments, skip_indices)
                info = await get_video_info(trimmed_path)
            else:
                remapped_words = []
        else:
            # Если филлеры не нужны, но субтитры нужны — всё равно транскрибируем
            remapped_words = []
            if options["opt_subtitles"]:
                audio_path = job_dir / "audio.mp3"
                await extract_audio(video_path, audio_path)

                lang = options["opt_subtitles_lang"]
                if lang == "auto":
                    lang = None

                transcript = await transcribe_whisper(audio_path, lang)
                remapped_words = transcript.get("words", [])

        # --- 2. Субтитры ---
        if options["opt_subtitles"] and remapped_words:
            ass_path = job_dir / "subtitles.ass"
            ass_content = generate_ass(
                remapped_words, "word",  # word-style по умолчанию (рилсы)
                info["width"], info["height"],
            )
            ass_path.write_text(ass_content, encoding="utf-8")

            subtitled_path = job_dir / "subtitled.mp4"
            await burn_subtitles(current_video, ass_path, subtitled_path, info)
            current_video = subtitled_path
            info = await get_video_info(subtitled_path)

        # --- 3. Цветокоррекция ---
        if options["opt_color"]:
            color_path = job_dir / "color.mp4"
            await apply_color_correction(current_video, color_path, info)
            current_video = color_path

        # Финальный файл
        final_path = job_dir / "final.mp4"
        if current_video != final_path:
            if final_path.exists():
                final_path.unlink()
            current_video.rename(final_path)

        # Формируем URL для скачивания
        result_url = f"/download/{job_id}/result_{job_id}.mp4"

        # Обновляем статус в Supabase
        sb.table("video_jobs").update({
            "status": "done",
            "result_url": result_url,
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()

    except Exception as e:
        # Ошибка — пишем в Supabase
        sb.table("video_jobs").update({
            "status": "error",
            "error_message": str(e)[:500],
            "finished_at": datetime.now(timezone.utc).isoformat(),
        }).eq("id", job_id).execute()
        raise


async def apply_color_correction(video_path: Path, output_path: Path, info: dict):
    """Цветокоррекция: насыщенность, контраст, тёплый тон."""
    from subtitles import run_ffmpeg

    # eq: лёгкий подъём контраста и насыщенности + тёплый тон
    vf = "eq=contrast=1.05:saturation=1.2:brightness=0.02,colorbalance=rs=0.03:gs=0.01:bs=-0.02"

    cmd = [
        "-i", str(video_path),
        "-vf", vf,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-r", str(info["fps"]),
        "-pix_fmt", "yuv420p",
    ]
    if info["has_audio"]:
        cmd.extend(["-c:a", "aac", "-b:a", "192k"])
    else:
        cmd.append("-an")
    cmd.extend(["-y", str(output_path)])

    await run_ffmpeg(cmd)


@router.post("/upload")
async def upload_video(request: Request, file: UploadFile = File(...)):
    """
    Принимает видео файл, сохраняет, создаёт job в Supabase,
    запускает фоновую обработку.
    """
    # Валидация типа файла
    content_type = file.content_type or ""
    if not content_type.startswith("video/") and not file.filename.lower().endswith((".mp4", ".mov", ".avi", ".webm")):
        raise HTTPException(status_code=400, detail="Only video files accepted")

    # Парсим опции обработки
    options = parse_job_options(request)

    # Создаём job
    job_id = str(uuid.uuid4())
    job_dir = WORK_DIR / job_id[:8]
    job_dir.mkdir(parents=True, exist_ok=True)

    # Сохраняем файл на диск (стриминг для больших файлов)
    video_path = job_dir / "input.mp4"
    file_size = 0
    with open(video_path, "wb") as f:
        while chunk := await file.read(1024 * 1024):  # 1MB chunks
            f.write(chunk)
            file_size += len(chunk)

    # Создаём запись в Supabase
    try:
        sb = get_supabase()

        # Получаем user_id из Authorization header (Supabase JWT)
        auth_header = request.headers.get("authorization", "")
        user_id = None
        if auth_header.startswith("Bearer "):
            token = auth_header[7:]
            try:
                # Декодируем JWT чтобы получить user_id
                import base64
                payload = token.split(".")[1]
                # Добавляем padding
                payload += "=" * (4 - len(payload) % 4)
                decoded = json.loads(base64.urlsafe_b64decode(payload))
                user_id = decoded.get("sub")
            except Exception:
                pass

        if not user_id:
            raise HTTPException(status_code=401, detail="Unauthorized")

        sb.table("video_jobs").insert({
            "id": job_id,
            "user_id": user_id,
            "file_name": file.filename or "video.mp4",
            "file_size": file_size,
            "storage_path": str(video_path),
            "status": "queued",
            "opt_fillers": options["opt_fillers"],
            "opt_subtitles": options["opt_subtitles"],
            "opt_subtitles_lang": options["opt_subtitles_lang"],
            "opt_color": options["opt_color"],
        }).execute()

    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to create job: {e}")

    # Запускаем обработку в фоне
    asyncio.create_task(process_job(job_id[:8], video_path, options))

    return {"jobId": job_id}
