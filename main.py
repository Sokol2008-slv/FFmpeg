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
import base64
import asyncio
import httpx
from pathlib import Path
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field
from typing import Optional
from openai import AsyncOpenAI

openai_client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY", ""))

app = FastAPI(title="Kaizen FFmpeg Service", version="2.0.0")

# CORS — разрешаем фронтенд Axiomativ
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

from pad_to_square import router as pad_router
from subtitles import router as subtitle_router
from upload import router as upload_router
app.include_router(pad_router)
app.include_router(subtitle_router)
app.include_router(upload_router)

WORK_DIR = Path("/tmp/kaizen-ffmpeg")
WORK_DIR.mkdir(exist_ok=True)

FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


# --- Models ---

class ProcessVideoRequest(BaseModel):
    video_url: str = Field(..., description="URL видео от Kling 2.6")
    logo_url: str = Field(..., description="URL логотипа Kaizen (PNG с прозрачностью)")
    slogan: Optional[str] = Field(None, description="Слоган для аутро (если есть)")
    target_aspect: Optional[str] = Field(None, description="Целевой формат: '9:16', '1:1' или None (оставить как есть)")
    pad_style: str = Field("black", description="Стиль паддинга: 'black' (быстро) или 'blur' (размытый фон)")
    outro_duration: float = Field(3.0, description="Длительность аутро в секундах")
    fade_duration: float = Field(1.0, description="Длительность crossfade перехода в секундах")
    watermark_opacity: float = Field(0.8, description="Прозрачность watermark (0.0-1.0)")
    watermark_scale: float = Field(0.15, description="Размер логотипа относительно ширины видео")
    watermark_margin: int = Field(50, description="Отступ от края в пикселях (50+ для Instagram safe zone)")
    skip_outro: bool = Field(False, description="Если True — только watermark в углу, без аутро (для Stories и Posts)")
    audio_url: Optional[str] = Field(None, description="URL аудио для замены/добавления к видео (ASMR, музыка)")


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
    text = text.replace("%", "%%")
    return text


async def download_file(url: str, dest: Path) -> Path:
    """Скачивает файл по URL."""
    async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
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

    # 1.5. Pad видео до целевого формата (если указан)
    if req.target_aspect:
        padded_path = job_dir / "padded.mp4"
        if req.target_aspect == "9:16":
            target_w = int(h * 9 / 16)
            target_h = h
            if target_w < w:
                target_h = int(w * 16 / 9)
                target_w = w
        elif req.target_aspect == "1:1":
            target_w = target_h = max(w, h)
        else:
            target_w, target_h = w, h

        if target_w != w or target_h != h:
            pad_x = (target_w - w) // 2
            pad_y = (target_h - h) // 2

            if req.pad_style == "blur":
                # Размытый фон из самого видео + оригинал по центру (медленно)
                vf = (
                    f"[0:v]scale={target_w}:{target_h}:force_original_aspect_ratio=increase,"
                    f"crop={target_w}:{target_h},gblur=sigma=40[bg];"
                    f"[0:v]scale={w}:{h}[fg];"
                    f"[bg][fg]overlay=(W-w)/2:(H-h)/2,format=yuv420p"
                )
                pad_cmd = [
                    "-i", str(video_path),
                    "-filter_complex", vf,
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-r", str(fps),
                    "-pix_fmt", "yuv420p",
                ]
            else:
                # Чёрные полосы (быстро)
                pad_cmd = [
                    "-i", str(video_path),
                    "-vf", f"pad={target_w}:{target_h}:{pad_x}:{pad_y}:color=black,format=yuv420p",
                    "-c:v", "libx264", "-preset", "fast", "-crf", "23",
                    "-r", str(fps),
                    "-pix_fmt", "yuv420p",
                ]

            if has_audio:
                pad_cmd.extend(["-c:a", "aac", "-b:a", "192k"])
            pad_cmd.extend(["-y", str(padded_path)])
            await run_ffmpeg(pad_cmd)
            video_path = padded_path
            w, h = target_w, target_h

    # 2. Наложить watermark на основное видео
    watermarked_path = job_dir / "watermarked.mp4"

    logo_w = int(w * req.watermark_scale)
    margin_x = req.watermark_margin + 40   # Instagram safe zone — компенсируем zoom+сдвиг вправо
    margin_y = req.watermark_margin + 120  # Instagram safe zone — компенсируем zoom+сдвиг вверх

    watermark_filter = (
        f"[1:v]scale={logo_w}:-1,format=rgba,"
        f"colorchannelmixer=aa={req.watermark_opacity}[wm];"
        f"[0:v][wm]overlay=W-w-{margin_x}:{margin_y},format=yuv420p"
    )

    watermark_cmd = [
        "-i", str(video_path),
        "-i", str(logo_path),
        "-filter_complex", watermark_filter,
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
        "-r", str(fps),
        "-pix_fmt", "yuv420p",
    ]

    if has_audio:
        watermark_cmd.extend(["-c:a", "aac", "-b:a", "192k"])

    watermark_cmd.extend(["-y", str(watermarked_path)])
    await run_ffmpeg(watermark_cmd)

    # 2.5. Добавить аудио если указан audio_url
    if req.audio_url:
        audio_path = job_dir / "audio.mp3"
        await download_file(req.audio_url, audio_path)
        with_audio_path = job_dir / "with_audio.mp4"
        audio_cmd = [
            "-i", str(watermarked_path),
            "-i", str(audio_path),
            "-map", "0:v",
            "-map", "1:a",
            "-c:v", "copy",
            "-c:a", "aac", "-b:a", "192k",
            "-shortest",
            "-y", str(with_audio_path)
        ]
        await run_ffmpeg(audio_cmd)
        watermarked_path = with_audio_path
        has_audio = True

    # Если skip_outro=True — возвращаем видео только с watermark (для Stories и Posts)
    if req.skip_outro:
        return watermarked_path

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
        "-c:v", "libx264", "-preset", "fast", "-crf", "23",
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
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
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
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
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


class AnalyzeRequest(BaseModel):
    video_url: str
    frame_count: int = Field(6, ge=1, le=10)


@app.post("/analyze")
async def analyze_video(req: AnalyzeRequest):
    """
    Извлекает N кадров из видео и аудио-дорожку.
    Кадры возвращаются как base64 JPEG.
    Аудио возвращается как base64 MP3 (если есть).
    Используется для анализа содержимого видео через Claude Vision + Whisper.
    """
    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    video_path = job_dir / "input.mp4"
    await download_file(req.video_url, video_path)

    info = await get_video_info(video_path)
    duration = info["duration"]
    has_audio = info["has_audio"]

    # Извлекаем кадры равномерно по всему видео
    frames_b64 = []
    for i in range(req.frame_count):
        t = duration * (i + 0.5) / req.frame_count
        frame_path = job_dir / f"frame_{i}.jpg"
        try:
            await run_ffmpeg([
                "-ss", str(round(t, 2)),
                "-i", str(video_path),
                "-frames:v", "1",
                "-q:v", "4",
                "-y", str(frame_path),
            ])
            if frame_path.exists():
                frames_b64.append(base64.b64encode(frame_path.read_bytes()).decode())
        except Exception:
            pass

    # Извлекаем аудио и транскрибируем через Whisper (если есть)
    transcript = None
    if has_audio and os.environ.get("OPENAI_API_KEY"):
        audio_path = job_dir / "audio.mp3"
        try:
            await run_ffmpeg([
                "-i", str(video_path),
                "-vn",
                "-ar", "16000",
                "-ac", "1",
                "-b:a", "64k",
                "-t", "60",  # максимум 60 секунд
                "-y", str(audio_path),
            ])
            if audio_path.exists() and audio_path.stat().st_size > 1000:
                with open(audio_path, "rb") as f:
                    result = await openai_client.audio.transcriptions.create(
                        model="whisper-1",
                        file=f,
                        response_format="text",
                    )
                transcript = result.strip() if result else None
        except Exception as e:
            transcript = None  # тихо пропускаем ошибку Whisper

    return {
        "frames": frames_b64,
        "transcript": transcript,
        "has_audio": has_audio,
        "duration": round(duration, 1),
        "frame_count": len(frames_b64),
    }


@app.get("/preview-logo")
async def preview_logo(image_url: str, logo_url: str):
    """
    Быстрый превью позиции логотипа на изображении — без видео, без Kling.
    Возвращает JPG с наложенным логотипом.
    Используй для проверки позиции логотипа перед запуском полного пайплайна.

    Пример:
    GET /preview-logo?image_url=https://...&logo_url=https://...
    """
    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    image_path = job_dir / "image.jpg"
    logo_path = job_dir / "logo.png"
    output_path = job_dir / "preview.jpg"

    await asyncio.gather(
        download_file(image_url, image_path),
        download_file(logo_url, logo_path),
    )

    # Получаем размеры изображения
    probe_cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "json", str(image_path)]
    proc = await asyncio.create_subprocess_exec(
        *probe_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    info = json.loads(stdout)
    w = info["streams"][0]["width"]

    logo_w = int(w * 0.15)
    margin_x = 50 + 40
    margin_y = 50 + 120

    cmd = [
        "ffmpeg", "-y",
        "-i", str(image_path),
        "-i", str(logo_path),
        "-filter_complex",
        f"[1:v]scale={logo_w}:-1,format=rgba,colorchannelmixer=aa=0.8[wm];"
        f"[0:v][wm]overlay=W-w-{margin_x}:{margin_y}",
        "-frames:v", "1",
        "-q:v", "2",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()

    if not output_path.exists():
        raise HTTPException(status_code=500, detail="Failed to generate preview")

    return FileResponse(output_path, media_type="image/jpeg", filename="logo_preview.jpg")


@app.get("/preview-instagram")
async def preview_instagram(image_url: str, logo_url: str):
    """
    Симулирует как видео будет выглядеть в Instagram Reels на iPhone.

    1. Накладывает логотип с текущими margin настройками
    2. Обрезает боковые края (Instagram кропит ~76px с каждой стороны для 1056px видео на iPhone 14)
    3. Рисует полупрозрачные зоны Instagram UI (кнопки справа, подпись снизу)

    Результат = точная симуляция без публикации.
    """
    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    image_path = job_dir / "image.jpg"
    logo_path = job_dir / "logo.png"
    with_logo_path = job_dir / "with_logo.jpg"
    output_path = job_dir / "instagram_preview.jpg"

    await asyncio.gather(
        download_file(image_url, image_path),
        download_file(logo_url, logo_path),
    )

    # Получаем размеры
    probe_cmd = ["ffprobe", "-v", "error", "-select_streams", "v:0",
                 "-show_entries", "stream=width,height", "-of", "json", str(image_path)]
    proc = await asyncio.create_subprocess_exec(
        *probe_cmd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await proc.communicate()
    info = json.loads(stdout)
    w = info["streams"][0]["width"]
    h = info["streams"][0]["height"]

    # Шаг 1: наложить логотип (те же margin что в /process)
    logo_w = int(w * 0.15)
    margin_x = 50 + 40
    margin_y = 50 + 120

    cmd1 = [
        "ffmpeg", "-y",
        "-i", str(image_path),
        "-i", str(logo_path),
        "-filter_complex",
        f"[1:v]scale={logo_w}:-1,format=rgba,colorchannelmixer=aa=0.8[wm];"
        f"[0:v][wm]overlay=W-w-{margin_x}:{margin_y}",
        "-frames:v", "1", "-q:v", "2",
        str(with_logo_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd1, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    await proc.communicate()

    # Шаг 2: симулировать Instagram кроп + UI оверлей
    # iPhone 14: 390pt wide. Видео 9:16 отображается по высоте экрана.
    # Видимая ширина видео = 390/844 * h_display * aspect — реальный кроп с каждой стороны:
    # scale_factor = screen_h / h = 844/1920 ≈ 0.4396 (для стандартного 1920px)
    # Для нашего h: scaled_w = w * (844/h) * (1920/844) — упрощается до w * 1920/h
    # Crop: сколько px обрезается с каждой стороны по горизонтали
    iphone_screen_w = 390  # logical points iPhone 14
    iphone_screen_h = 844  # logical points iPhone 14
    # При отображении full-height: масштаб = iphone_screen_h / h
    scale = iphone_screen_h / h
    scaled_w_pts = w * scale
    side_crop_pts = (scaled_w_pts - iphone_screen_w) / 2
    side_crop_px = int(side_crop_pts / scale)  # переводим обратно в пиксели видео
    side_crop_px = max(0, side_crop_px)

    # Видимая ширина после кропа
    visible_w = w - 2 * side_crop_px

    # Зоны Instagram UI (в координатах обрезанного изображения):
    # Правая колонка кнопок (лайк/коммент/поделиться): последние ~13% ширины
    ui_right_w = int(visible_w * 0.13)
    # Нижняя зона (username, подпись, музыка): последние ~22% высоты
    ui_bottom_h = int(h * 0.22)
    # Верхняя зона (UI chrome Instagram + Dynamic Island): ~8% высоты
    ui_top_h = int(h * 0.08)

    # FFmpeg: кроп боковых краёв + нарисовать полупрозрачные UI зоны
    font = FONT_PATH
    filter_complex = (
        f"[0:v]crop={visible_w}:{h}:{side_crop_px}:0[cropped];"
        # Правая зона кнопок Instagram (лайк/коммент/поделиться)
        f"[cropped]drawbox=x={visible_w - ui_right_w}:y=0:w={ui_right_w}:h={h}:"
        f"color=black@0.45:t=fill[r1];"
        # Нижняя зона (username, подпись, музыка)
        f"[r1]drawbox=x=0:y={h - ui_bottom_h}:w={visible_w}:h={ui_bottom_h}:"
        f"color=black@0.50:t=fill[r2];"
        # Верхняя зона (Dynamic Island + Instagram header)
        f"[r2]drawbox=x=0:y=0:w={visible_w}:h={ui_top_h}:"
        f"color=black@0.35:t=fill[r3];"
        # Подпись снизу
        f"[r3]drawtext=text='caption / music / username':fontfile={font}:"
        f"x=10:y={h - ui_bottom_h + 12}:fontsize=24:fontcolor=white@0.7[out]"
    )

    cmd2 = [
        "ffmpeg", "-y",
        "-i", str(with_logo_path),
        "-vf", filter_complex,
        "-frames:v", "1", "-q:v", "2",
        str(output_path),
    ]
    proc = await asyncio.create_subprocess_exec(
        *cmd2, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    _, stderr = await proc.communicate()

    if not output_path.exists():
        raise HTTPException(status_code=500, detail=f"Failed: {stderr.decode()[-500:]}")

    return FileResponse(output_path, media_type="image/jpeg", filename="instagram_preview.jpg")


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
    """Скачать готовый файл (видео или изображение)."""
    media_types = {
        ".mp4": "video/mp4",
        ".jpg": "image/jpeg",
    }

    ext = Path(filename).suffix.lower()
    if ext not in media_types:
        raise HTTPException(status_code=400, detail="Unsupported file type")

    # Ищем выходной файл в папке задания
    job_dir = WORK_DIR / job_id

    # Сначала пробуем точное имя файла
    file_path = job_dir / filename
    if not file_path.exists():
        # Fallback: ищем по известным именам
        candidates = ["final.mp4", "watermarked.mp4", "square.jpg", "vertical.jpg"]
        file_path = None
        for name in candidates:
            p = job_dir / name
            if p.exists():
                file_path = p
                break

    if not file_path or not file_path.exists():
        raise HTTPException(status_code=404, detail="File not found")
    return FileResponse(
        file_path,
        media_type=media_types[ext],
        filename=filename,
    )


@app.on_event("startup")
async def startup():
    WORK_DIR.mkdir(exist_ok=True)


if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(
        app,
        host="0.0.0.0",
        port=port,
        timeout_keep_alive=300,
    )
