"""
Kaizen Detailers — FFmpeg Video Post-Processing Service
========================================================
Принимает video_url + logo_url, накладывает:
1. Watermark (логотип) справа сверху на всё видео
2. Аутро с плавным crossfade: тёмный экран + логотип по центру (+ слоган)

Deploy: Railway (Dockerfile)
"""

import os
import uuid
import asyncio
import httpx
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional

app = FastAPI(title="Kaizen FFmpeg Service", version="1.0.0")

WORK_DIR = Path("/tmp/kaizen-ffmpeg")
WORK_DIR.mkdir(exist_ok=True)

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

# --- Models ---

class ProcessVideoRequest(BaseModel):
    video_url: str = Field(..., description="URL видео от Kling 2.6")
    logo_url: str = Field(..., description="URL логотипа Kaizen (PNG с прозрачностью)")
    slogan: Optional[str] = Field(None, description="Слоган для аутро (если есть)")
    outro_duration: float = Field(3.0, description="Длительность аутро в секундах")
    fade_duration: float = Field(1.0, description="Длительность crossfade перехода в секундах")
    watermark_opacity: float = Field(0.8, description="Прозрачность watermark (0.0-1.0)")
    watermark_scale: float = Field(0.15, description="Размер логотипа относительно ширины видео")
    watermark_margin: int = Field(20, description="Отступ от края в пикселях")


class ProcessVideoResponse(BaseModel):
    status: str
    output_url: str
    filename: str


# --- Helpers ---

def escape_drawtext(text: str) -> str:
    """Экранирует спецсимволы для FFmpeg drawtext."""
    text = text.replace("\\", "\\\\")
    text = text.replace("'", "'\\\\\\''")
    text = text.replace(":", "\\:")
    text = text.replace(";", "\\;")
    text = text.replace("[", "\\[")
    text = text.replace("]", "\\]")
    return text


async def download_file(url: str, dest: Path) -> Path:
    """Скачивает файл по URL."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=120.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest


async def run_ffmpeg(cmd: list[str]) -> str:
    """Запускает FFmpeg команду."""
    process = await asyncio.create_subprocess_exec(
        "ffmpeg", *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, stderr = await process.communicate()
    if process.returncode != 0:
        raise RuntimeError(f"FFmpeg error: {stderr.decode()}")
    return stderr.decode()


async def get_video_info(video_path: Path) -> dict:
    """Получает информацию о видео через ffprobe."""
    import json
    process = await asyncio.create_subprocess_exec(
        "ffprobe",
        "-v", "quiet",
        "-print_format", "json",
        "-show_streams",
        "-show_format",
        str(video_path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    info = json.loads(stdout.decode())

    video_stream = next(
        (s for s in info.get("streams", []) if s["codec_type"] == "video"),
        None
    )
    audio_stream = next(
        (s for s in info.get("streams", []) if s["codec_type"] == "audio"),
        None
    )

    duration = float(info.get("format", {}).get("duration", 0))
    width = int(video_stream["width"]) if video_stream else 1080
    height = int(video_stream["height"]) if video_stream else 1920

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "has_audio": audio_stream is not None
    }


# --- Main Processing ---

async def process_video(req: ProcessVideoRequest, job_id: str) -> Path:
    """
    Полный пайплайн обработки видео:
    1. Скачать видео и логотип
    2. Наложить watermark справа сверху
    3. Создать аутро (тёмный экран + логотип + слоган) с fade-in
    4. Crossfade: основное видео плавно переходит в аутро
    """

    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    # 1. Скачиваем файлы
    video_path = job_dir / "input.mp4"
    logo_path = job_dir / "logo.png"

    await asyncio.gather(
        download_file(req.video_url, video_path),
        download_file(req.logo_url, logo_path),
    )

    # Получаем инфо о видео
    info = await get_video_info(video_path)
    w, h = info["width"], info["height"]
    has_audio = info["has_audio"]
    duration = info["duration"]
    fade_dur = req.fade_duration

    # 2. Наложить watermark на основное видео
    watermarked_path = job_dir / "watermarked.mp4"

    logo_w = int(w * req.watermark_scale)
    margin = req.watermark_margin

    watermark_filter = (
        f"[1:v]scale={logo_w}:-1,format=rgba,"
        f"colorchannelmixer=aa={req.watermark_opacity}[wm];"
        f"[0:v][wm]overlay=W-w-{margin}:{margin}"
    )

    watermark_cmd = [
        "-i", str(video_path),
        "-i", str(logo_path),
        "-filter_complex", watermark_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-crf", "18",
    ]

    if has_audio:
        watermark_cmd.extend(["-c:a", "aac", "-b:a", "192k"])

    watermark_cmd.extend(["-y", str(watermarked_path)])

    await run_ffmpeg(watermark_cmd)

    # 3. Создать аутро с fade-in
    outro_path = job_dir / "outro.mp4"

    outro_logo_w = int(w * 0.30)

    # Аутро: тёмный фон + логотип по центру + опционально слоган
    # Логотип и текст появляются с fade-in
    if req.slogan:
        safe_slogan = escape_drawtext(req.slogan)
        logo_y = "(H-h)/2-80"
        outro_filter = (
            f"[1:v]scale={outro_logo_w}:-1,format=rgba[logo];"
            f"[0:v][logo]overlay=(W-w)/2:{logo_y}:format=auto[with_logo];"
            f"[with_logo]drawtext="
            f"text='{safe_slogan}':"
            f"fontfile={FONT_PATH}:"
            f"fontcolor=white:fontsize={int(w * 0.035)}:"
            f"x=(w-text_w)/2:y=(h/2)+60[composed];"
            f"[composed]fade=t=in:st=0:d={fade_dur}:alpha=0"
        )
    else:
        outro_filter = (
            f"[1:v]scale={outro_logo_w}:-1,format=rgba[logo];"
            f"[0:v][logo]overlay=(W-w)/2:(H-h)/2:format=auto[composed];"
            f"[composed]fade=t=in:st=0:d={fade_dur}:alpha=0"
        )

    outro_cmd = [
        "-f", "lavfi",
        "-i", f"color=c=0x111111:s={w}x{h}:d={req.outro_duration}:r=30",
        "-i", str(logo_path),
        "-filter_complex", outro_filter,
        "-c:v", "libx264",
        "-preset", "fast",
        "-t", str(req.outro_duration),
        "-pix_fmt", "yuv420p",
        "-y", str(outro_path),
    ]

    await run_ffmpeg(outro_cmd)

    # 4. Crossfade: плавный переход из видео в аутро
    output_path = job_dir / "final.mp4"

    # xfade offset = длительность первого видео минус длительность перехода
    watermarked_info = await get_video_info(watermarked_path)
    xfade_offset = watermarked_info["duration"] - fade_dur

    if has_audio:
        # Добавляем тишину к аутро, чтобы аудио потоки совпадали
        outro_with_audio = job_dir / "outro_audio.mp4"
        await run_ffmpeg([
            "-i", str(outro_path),
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            "-y", str(outro_with_audio),
        ])

        # Видео: xfade, Аудио: acrossfade
        xfade_cmd = [
            "-i", str(watermarked_path),
            "-i", str(outro_with_audio),
            "-filter_complex",
            f"[0:v][1:v]xfade=transition=fade:duration={fade_dur}:offset={xfade_offset},format=yuv420p[v];"
            f"[0:a][1:a]acrossfade=d={fade_dur}:c1=tri:c2=tri[a]",
            "-map", "[v]",
            "-map", "[a]",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-c:a", "aac",
            "-b:a", "192k",
            "-y", str(output_path),
        ]
    else:
        # Только видео, без аудио
        xfade_cmd = [
            "-i", str(watermarked_path),
            "-i", str(outro_path),
            "-filter_complex",
            f"[0:v][1:v]xfade=transition=fade:duration={fade_dur}:offset={xfade_offset},format=yuv420p[v]",
            "-map", "[v]",
            "-c:v", "libx264",
            "-preset", "fast",
            "-crf", "18",
            "-y", str(output_path),
        ]

    await run_ffmpeg(xfade_cmd)

    return output_path


# --- Endpoints ---

@app.get("/health")
async def health():
    return {"status": "ok", "service": "kaizen-ffmpeg"}


@app.post("/process", response_model=ProcessVideoResponse)
async def process_endpoint(req: ProcessVideoRequest):
    """
    Обработка видео: watermark + аутро с crossfade.

    Пример запроса из n8n:
    POST /process
    {
        "video_url": "https://...",
        "logo_url": "https://...",
        "slogan": null
    }
    """
    job_id = str(uuid.uuid4())[:8]

    try:
        output_path = await process_video(req, job_id)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    filename = f"kaizen_{job_id}.mp4"

    return ProcessVideoResponse(
        status="done",
        output_url=f"/download/{job_id}/{filename}",
        filename=filename,
    )


@app.get("/download/{job_id}/{filename}")
async def download(job_id: str, filename: str):
    """Скачать готовое видео."""
    file_path = WORK_DIR / job_id / "final.mp4"
    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        file_path,
        media_type="video/mp4",
        filename=filename,
    )


@app.on_event("startup")
async def startup():
    WORK_DIR.mkdir(exist_ok=True)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
