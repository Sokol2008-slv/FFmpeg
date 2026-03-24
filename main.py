"""
Kaizen Detailers — FFmpeg Video Post-Processing Service
========================================================
Принимает video_url + logo_url, накладывает:
1. Watermark (логотип) справа сверху на всё видео
2. Аутро: тёмный экран + логотип по центру (+ слоган, если указан)
3. Плавный crossfade переход между видео и аутро

Deploy: Railway (Dockerfile)
"""

import os
import uuid
import json
import asyncio
import httpx
from pathlib import Path
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field
from typing import Optional

app = FastAPI(title="Kaizen FFmpeg Service", version="2.0.0")

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


class PadToVerticalRequest(BaseModel):
    image_url: str = Field(..., description="URL изображения")
    bg_color: str = Field("black", description="Цвет полос: black, white")


class PadToVerticalResponse(BaseModel):
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
    text = text.replace("%", "%%")
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

    # Получаем fps из видеопотока
    fps = 30
    if video_stream:
        r_frame_rate = video_stream.get("r_frame_rate", "30/1")
        parts = r_frame_rate.split("/")
        if len(parts) == 2 and int(parts[1]) > 0:
            fps = round(int(parts[0]) / int(parts[1]))

    return {
        "duration": duration,
        "width": width,
        "height": height,
        "fps": fps,
        "has_audio": audio_stream is not None,
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
    fps = info["fps"]
    has_audio = info["has_audio"]
    duration = info["duration"]
    fade_dur = min(req.fade_duration, duration * 0.5, req.outro_duration * 0.5)

    # 2. Наложить watermark на основное видео
    watermarked_path = job_dir / "watermarked.mp4"

    logo_w = int(w * req.watermark_scale)
    margin = req.watermark_margin

    watermark_filter = (
        f"[1:v]scale={logo_w}:-1,format=rgba,"
        f"colorchannelmixer=aa={req.watermark_opacity}[wm];"
        f"[0:v][wm]overlay=W-w-{margin}:{margin},format=yuv420p"
    )

    watermark_cmd = [
        "-i", str(video_path),
        "-i", str(logo_path),
        "-filter_complex", watermark_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-r", str(fps),
        "-pix_fmt", "yuv420p",
    ]

    if has_audio:
        watermark_cmd.extend(["-c:a", "aac", "-b:a", "192k"])

    watermark_cmd.extend(["-y", str(watermarked_path)])
    await run_ffmpeg(watermark_cmd)

    # 3. Создать аутро с fade-in
    outro_path = job_dir / "outro.mp4"
    outro_logo_w = int(w * 0.30)

    if req.slogan and req.slogan.strip():
        safe_slogan = escape_drawtext(req.slogan.strip())
        logo_y = "(H-h)/2-80"
        outro_filter = (
            f"[1:v]scale={outro_logo_w}:-1,format=rgba[logo];"
            f"[0:v][logo]overlay=(W-w)/2:{logo_y}:format=auto[with_logo];"
            f"[with_logo]drawtext="
            f"text='{safe_slogan}':"
            f"fontfile={FONT_PATH}:"
            f"fontcolor=white:fontsize={int(w * 0.035)}:"
            f"x=(w-text_w)/2:y=(h/2)+60"
        )
    else:
        outro_filter = (
            f"[1:v]scale={outro_logo_w}:-1,format=rgba[logo];"
            f"[0:v][logo]overlay=(W-w)/2:(H-h)/2:format=auto"
        )

    outro_cmd = [
        "-f", "lavfi",
        "-i", f"color=c=black:s={w}x{h}:d={req.outro_duration}:r={fps}",
        "-i", str(logo_path),
        "-filter_complex", outro_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-r", str(fps),
        "-t", str(req.outro_duration),
        "-pix_fmt", "yuv420p",
        "-y", str(outro_path),
    ]
    await run_ffmpeg(outro_cmd)

    # 4. Crossfade: плавный переход из видео в аутро
    output_path = job_dir / "final.mp4"

    # Получаем точную длительность watermarked видео для offset
    watermarked_info = await get_video_info(watermarked_path)
    xfade_offset = watermarked_info["duration"] - fade_dur

    if has_audio:
        # Добавляем тишину к аутро для совместимости аудио потоков
        outro_with_audio = job_dir / "outro_audio.mp4"
        await run_ffmpeg([
            "-i", str(outro_path),
            "-f", "lavfi", "-i", "anullsrc=r=44100:cl=stereo",
            "-c:v", "copy",
            "-c:a", "aac",
            "-shortest",
            "-y", str(outro_with_audio),
        ])

        # Видео: settb + xfade, Аудио: acrossfade
        xfade_cmd = [
            "-i", str(watermarked_path),
            "-i", str(outro_with_audio),
            "-filter_complex",
            f"[0:v]settb=AVTB,fps={fps},format=yuv420p[v0];"
            f"[1:v]settb=AVTB,fps={fps},format=yuv420p[v1];"
            f"[v0][v1]xfade=transition=fade:duration={fade_dur}:offset={xfade_offset}[vout];"
            f"[0:a]aformat=sample_rates=44100:channel_layouts=stereo[a0];"
            f"[1:a]aformat=sample_rates=44100:channel_layouts=stereo[a1];"
            f"[a0][a1]acrossfade=d={fade_dur}:c1=tri:c2=tri[aout]",
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-y", str(output_path),
        ]
    else:
        # Только видео, без аудио
        xfade_cmd = [
            "-i", str(watermarked_path),
            "-i", str(outro_path),
            "-filter_complex",
            f"[0:v]settb=AVTB,fps={fps},format=yuv420p[v0];"
            f"[1:v]settb=AVTB,fps={fps},format=yuv420p[v1];"
            f"[v0][v1]xfade=transition=fade:duration={fade_dur}:offset={xfade_offset}[vout]",
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "18",
            "-pix_fmt", "yuv420p",
            "-an",
            "-y", str(output_path),
        ]

    await run_ffmpeg(xfade_cmd)

    return output_path


# --- Endpoints ---

@app.get("/health")
async def health():
    return {"status": "ok", "service": "kaizen-ffmpeg", "version": "2.0.0"}


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


@app.post("/pad-to-vertical", response_model=PadToVerticalResponse)
async def pad_to_vertical(req: PadToVerticalRequest):
    """Добавляет полосы слева/справа (или сверху/снизу) чтобы получился формат 9:16."""
    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    input_path = job_dir / "input.jpg"
    output_path = job_dir / "vertical.jpg"

    await download_file(req.image_url, input_path)

    # Получаем размеры изображения
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", str(input_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    info = json.loads(stdout.decode())
    video_stream = next(
        (s for s in info.get("streams", []) if s["codec_type"] == "video"), None
    )
    if not video_stream:
        raise HTTPException(status_code=400, detail="Cannot read image dimensions")

    w = int(video_stream["width"])
    h = int(video_stream["height"])

    # Целевой формат 9:16 — высота остаётся, ширина подгоняется
    target_w = int(h * 9 / 16)
    if target_w < w:
        # Фото слишком широкое — берём ширину как базу
        target_h = int(w * 16 / 9)
        target_w = w
    else:
        target_h = h

    pad_x = (target_w - w) // 2
    pad_y = (target_h - h) // 2

    cmd = [
        "-i", str(input_path),
        "-vf", f"pad={target_w}:{target_h}:{pad_x}:{pad_y}:color={req.bg_color}",
        "-q:v", "2", "-y", str(output_path)
    ]
    await run_ffmpeg(cmd)

    filename = f"vertical_{job_id}.jpg"

    return PadToVerticalResponse(
        status="done",
        output_url=f"/download/{job_id}/{filename}",
        filename=filename
    )


@app.get("/download/{job_id}/{filename}")
async def download(job_id: str, filename: str):
    """Скачать готовый файл (видео или изображение)."""
    # Определяем файл по расширению из filename
    output_files = {
        ".mp4": ("final.mp4", "video/mp4"),
        ".jpg": ("vertical.jpg", "image/jpeg"),
    }

    ext = Path(filename).suffix.lower()
    if ext not in output_files:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    actual_name, media_type = output_files[ext]
    file_path = WORK_DIR / job_id / actual_name

    if not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        file_path,
        media_type=media_type,
        filename=filename,
    )


@app.on_event("startup")
async def startup():
    WORK_DIR.mkdir(exist_ok=True)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
