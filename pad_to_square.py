"""
Pad-to-Square — эндпоинт для добавления полос к изображению до квадрата.
"""

import uuid
import json
import asyncio
from pathlib import Path
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


