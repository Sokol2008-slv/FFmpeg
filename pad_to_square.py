"""
Pad-to-Square / Pad-to-Vertical — эндпоинты для добавления полос к изображению.
"""

import uuid
import json
import asyncio
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter()

WORK_DIR = Path("/tmp/kaizen-ffmpeg")


async def download_file(url: str, dest: Path) -> Path:
    """Скачивает файл по URL."""
    import httpx
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


class PadToSquareRequest(BaseModel):
    image_url: str = Field(..., description="URL изображения")
    bg_color: str = Field("black", description="Цвет полос: black, white")


class PadToSquareResponse(BaseModel):
    status: str
    output_url: str
    filename: str


@router.post("/pad-to-square", response_model=PadToSquareResponse)
async def pad_to_square(req: PadToSquareRequest):
    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    input_path = job_dir / "input.jpg"
    output_path = job_dir / "square.jpg"

    await download_file(req.image_url, input_path)

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
        raise HTTPException(status_code=400, detail="No image stream found")

    w = int(video_stream["width"])
    h = int(video_stream["height"])
    size = max(w, h)
    pad_x = (size - w) // 2
    pad_y = (size - h) // 2

    cmd = [
        "-i", str(input_path),
        "-vf", f"pad={size}:{size}:{pad_x}:{pad_y}:color={req.bg_color}",
        "-q:v", "2", "-y", str(output_path)
    ]
    await run_ffmpeg(cmd)

    filename = f"square_{job_id}.jpg"
    return PadToSquareResponse(
        status="done",
        output_url=f"/download/{job_id}/{filename}",
        filename=filename,
    )


class PadToVerticalRequest(BaseModel):
    image_url: str = Field(..., description="URL изображения")
    bg_color: str = Field("black", description="Цвет полос: black, white")


class PadToVerticalResponse(BaseModel):
    status: str
    output_url: str
    filename: str


@router.post("/pad-to-vertical", response_model=PadToVerticalResponse)
async def pad_to_vertical(req: PadToVerticalRequest):
    """Добавляет полосы слева/справа (или сверху/снизу) чтобы получился формат 9:16."""
    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    input_path = job_dir / "input.jpg"
    output_path = job_dir / "vertical.jpg"

    await download_file(req.image_url, input_path)

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
        filename=filename,
    )


# --- Overlay Price on Image ---

class OverlayPriceRequest(BaseModel):
    image_url: str = Field(..., description="URL фото блюда")
    dish_name: str = Field(..., description="Название блюда")
    price: str = Field(..., description="Цена, например '57 AED'")
    logo_url: Optional[str] = Field(None, description="URL логотипа (PNG)")
    format: str = Field("square", description="Формат: 'square' (1:1) или 'vertical' (9:16)")
    bg_color: str = Field("black", description="Цвет полос: black, white")
    is_new: bool = Field(False, description="Пометка NEW на фото")


class OverlayPriceResponse(BaseModel):
    status: str
    output_url: str
    filename: str


@router.post("/overlay-price", response_model=OverlayPriceResponse)
async def overlay_price(req: OverlayPriceRequest):
    """Создаёт пост-картинку: фото блюда + название + цена + логотип."""
    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    input_path = job_dir / "input.jpg"
    logo_path = job_dir / "logo.png"
    filename = f"post_{job_id}.jpg"
    output_path = job_dir / filename

    await download_file(req.image_url, input_path)

    # Get image dimensions
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
        raise HTTPException(status_code=400, detail="Cannot read image")

    w = int(video_stream["width"])
    h = int(video_stream["height"])

    # Target dimensions
    if req.format == "vertical":
        target_w = int(h * 9 / 16) if int(h * 9 / 16) >= w else w
        target_h = int(w * 16 / 9) if int(h * 9 / 16) < w else h
    else:  # square
        target_w = target_h = max(w, h)

    pad_x = (target_w - w) // 2
    pad_y = (target_h - h) // 2

    # Font size relative to image
    font_size_name = max(target_w // 18, 24)
    font_size_price = max(target_w // 12, 32)
    font_size_new = max(target_w // 20, 20)
    margin = max(target_w // 25, 20)

    # Build filter chain
    filters = []
    # Only pad if dimensions actually change
    if target_w != w or target_h != h:
        filters.append(
            f"pad={target_w}:{target_h}:{pad_x}:{pad_y}:color={req.bg_color}"
        )

    # Price text (bottom left, bold)
    price_text = req.price.replace("'", "\\'")
    filters.append(
        f"drawtext=text='{price_text}'"
        f":fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
        f":fontsize={font_size_price}"
        f":fontcolor=white"
        f":borderw=3:bordercolor=black@0.6"
        f":x={margin}:y=h-{margin}-th"
    )

    # Dish name (above price)
    dish_text = req.dish_name.replace("'", "\\'").replace(":", "\\:")
    filters.append(
        f"drawtext=text='{dish_text}'"
        f":fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
        f":fontsize={font_size_name}"
        f":fontcolor=white"
        f":borderw=2:bordercolor=black@0.5"
        f":x={margin}:y=h-{margin}-{font_size_price}-{margin // 2}-th"
    )

    # NEW badge
    if req.is_new:
        filters.append(
            f"drawtext=text='NEW'"
            f":fontfile=/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"
            f":fontsize={font_size_new}"
            f":fontcolor=white"
            f":box=1:boxcolor=red@0.85:boxborderw=8"
            f":x={margin}:y={margin}"
        )

    vf = ",".join(filters)

    # If logo provided, overlay it
    if req.logo_url:
        await download_file(req.logo_url, logo_path)
        logo_scale = target_w // 7
        logo_margin = margin
        cmd = [
            "-i", str(input_path),
            "-i", str(logo_path),
            "-filter_complex",
            f"[0:v]{vf}[bg];"
            f"[1:v]scale={logo_scale}:-1[wm];"
            f"[bg][wm]overlay=W-w-{logo_margin}:{logo_margin}",
            "-q:v", "2", "-y", str(output_path)
        ]
    else:
        cmd = [
            "-i", str(input_path),
            "-vf", vf,
            "-q:v", "2", "-y", str(output_path)
        ]

    await run_ffmpeg(cmd)

    return OverlayPriceResponse(
        status="done",
        output_url=f"/download/{job_id}/{filename}",
        filename=filename,
    )

