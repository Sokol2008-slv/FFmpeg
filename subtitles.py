"""
Subtitles & Silence Removal — эндпоинт для обработки разговорных видео.
Транскрипция через Whisper API, вырезание тишины/филлеров, наложение субтитров.
"""

import os
import uuid
import json
import asyncio
import tempfile
from pathlib import Path
from typing import Optional
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field
from openai import AsyncOpenAI

router = APIRouter()

WORK_DIR = Path("/tmp/kaizen-ffmpeg")
FONT_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"
FONT_BOLD_PATH = "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf"

# Филлеры — слова-паразиты которые вырезаем
FILLERS_RU = {"эээ", "ээ", "э", "ммм", "мм", "ааа", "аа", "ну", "вот", "типа", "как бы", "короче", "эм", "ам"}
FILLERS_EN = {"uh", "um", "uhm", "hmm", "like", "you know", "so", "ah", "er"}


# --- Models ---

class SubtitleVideoRequest(BaseModel):
    video_url: str = Field(..., description="URL видео для обработки")
    language: str = Field("ru", description="Язык речи: 'ru', 'en' и т.д.")
    remove_silence: bool = Field(True, description="Вырезать длинные паузы")
    remove_fillers: bool = Field(True, description="Вырезать слова-паразиты (ээээ, ммм)")
    add_subtitles: bool = Field(True, description="Наложить субтитры")
    subtitle_style: str = Field("word", description="Стиль: 'word' (по 1-3 слова, как в рилсах) или 'phrase' (по фразе)")
    silence_threshold: float = Field(0.7, description="Минимум тишины для вырезания (секунды)")


class SubtitleVideoResponse(BaseModel):
    status: str
    output_url: str
    filename: str
    transcript: Optional[str] = None


# --- Helpers ---

async def download_file(url: str, dest: Path) -> Path:
    import httpx
    async with httpx.AsyncClient(follow_redirects=True, timeout=300.0) as client:
        resp = await client.get(url)
        resp.raise_for_status()
        dest.write_bytes(resp.content)
    return dest


async def run_ffmpeg(cmd: list[str]) -> str:
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
    process = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "quiet", "-print_format", "json",
        "-show_streams", "-show_format", str(video_path),
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
    )
    stdout, _ = await process.communicate()
    info = json.loads(stdout.decode())
    video_stream = next(
        (s for s in info.get("streams", []) if s["codec_type"] == "video"), None
    )
    audio_stream = next(
        (s for s in info.get("streams", []) if s["codec_type"] == "audio"), None
    )
    duration = float(info.get("format", {}).get("duration", 0))
    width = int(video_stream["width"]) if video_stream else 1080
    height = int(video_stream["height"]) if video_stream else 1920
    fps = 30
    if video_stream:
        r_frame_rate = video_stream.get("r_frame_rate", "30/1")
        parts = r_frame_rate.split("/")
        if len(parts) == 2 and int(parts[1]) > 0:
            fps = round(int(parts[0]) / int(parts[1]))
    return {
        "duration": duration, "width": width, "height": height,
        "fps": fps, "has_audio": audio_stream is not None,
    }


async def extract_audio(video_path: Path, audio_path: Path):
    """Извлекаем аудио в WAV для Whisper."""
    await run_ffmpeg([
        "-i", str(video_path),
        "-vn", "-acodec", "pcm_s16le", "-ar", "16000", "-ac", "1",
        "-y", str(audio_path),
    ])


async def transcribe_whisper(audio_path: Path, language: str) -> dict:
    """Транскрибируем через OpenAI Whisper API с таймкодами слов."""
    client = AsyncOpenAI(api_key=os.environ.get("OPENAI_API_KEY"))

    with open(audio_path, "rb") as f:
        response = await client.audio.transcriptions.create(
            model="whisper-1",
            file=f,
            language=language,
            response_format="verbose_json",
            timestamp_granularities=["word", "segment"],
        )

    return {
        "text": response.text,
        "words": [{"word": w.word, "start": w.start, "end": w.end} for w in (response.words or [])],
        "segments": [{"text": s.text, "start": s.start, "end": s.end} for s in (response.segments or [])],
    }


def identify_cuts(words: list[dict], language: str, remove_silence: bool,
                   remove_fillers: bool, silence_threshold: float,
                   video_duration: float) -> list[dict]:
    """
    Определяем какие сегменты видео оставить.
    Возвращаем список keep-сегментов: [{start, end}, ...]
    """
    if not words:
        return [{"start": 0, "end": video_duration}]

    fillers = FILLERS_RU if language == "ru" else FILLERS_EN

    # Помечаем слова для удаления
    skip_indices = set()
    if remove_fillers:
        for i, w in enumerate(words):
            clean = w["word"].strip().lower().rstrip(".,!?;:")
            if clean in fillers:
                skip_indices.add(i)

    # Собираем keep-сегменты из оставшихся слов
    keep_words = [(i, w) for i, w in enumerate(words) if i not in skip_indices]
    if not keep_words:
        return [{"start": 0, "end": video_duration}]

    # Группируем в непрерывные сегменты
    # Добавляем маленький буфер вокруг каждого слова для плавности
    BUFFER = 0.05  # 50мс буфер
    MAX_GAP = 0.3  # Оставляем паузу максимум 0.3с при вырезании тишины

    segments = []
    current_start = max(0, keep_words[0][1]["start"] - BUFFER)
    current_end = keep_words[0][1]["end"] + BUFFER

    for idx in range(1, len(keep_words)):
        prev_word = keep_words[idx - 1][1]
        curr_word = keep_words[idx][1]
        gap = curr_word["start"] - prev_word["end"]

        if remove_silence and gap > silence_threshold:
            # Большая пауза — закрываем текущий сегмент, начинаем новый
            segments.append({
                "start": current_start,
                "end": min(current_end, video_duration),
            })
            current_start = max(0, curr_word["start"] - BUFFER)
            current_end = curr_word["end"] + BUFFER
        else:
            # Продолжаем текущий сегмент
            current_end = curr_word["end"] + BUFFER

    # Последний сегмент
    segments.append({
        "start": current_start,
        "end": min(current_end, video_duration),
    })

    # Добавляем начало видео если речь не с самого начала (до 0.5с)
    if segments and segments[0]["start"] > 0.5 and not remove_silence:
        segments[0]["start"] = 0

    return segments


def remap_timestamps(words: list[dict], segments: list[dict], skip_indices: set) -> list[dict]:
    """
    Пересчитываем таймкоды слов после вырезания сегментов.
    Каждое оставшееся слово получает новый start/end в обрезанном видео.
    """
    remapped = []
    cumulative_offset = 0  # Сколько времени вырезано до текущего момента

    for seg_idx, seg in enumerate(segments):
        # Считаем offset — сколько вырезано между началом видео и началом этого сегмента
        if seg_idx == 0:
            cumulative_offset = seg["start"]
        else:
            prev_seg = segments[seg_idx - 1]
            cumulative_offset += seg["start"] - prev_seg["end"]

        # Ищем слова попадающие в этот сегмент
        for i, w in enumerate(words):
            if i in skip_indices:
                continue
            if w["start"] >= seg["start"] - 0.1 and w["end"] <= seg["end"] + 0.1:
                remapped.append({
                    "word": w["word"],
                    "start": w["start"] - cumulative_offset,
                    "end": w["end"] - cumulative_offset,
                })

    return remapped


async def trim_video(video_path: Path, segments: list[dict], output_path: Path, info: dict):
    """Вырезаем нужные сегменты и склеиваем через concat фильтр."""
    if len(segments) == 1 and segments[0]["start"] < 0.1:
        # Нечего вырезать — просто копируем
        await run_ffmpeg([
            "-i", str(video_path),
            "-t", str(segments[0]["end"]),
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-c:a", "aac", "-b:a", "192k",
            "-y", str(output_path),
        ])
        return

    fps = info["fps"]
    n = len(segments)

    # Строим filter_complex: trim каждый сегмент, concat
    parts_v = []
    parts_a = []
    for i, seg in enumerate(segments):
        parts_v.append(
            f"[0:v]trim=start={seg['start']:.3f}:end={seg['end']:.3f},setpts=PTS-STARTPTS,format=yuv420p[v{i}]"
        )
        if info["has_audio"]:
            parts_a.append(
                f"[0:a]atrim=start={seg['start']:.3f}:end={seg['end']:.3f},asetpts=PTS-STARTPTS[a{i}]"
            )

    # Concat
    v_inputs = "".join(f"[v{i}]" for i in range(n))
    concat_v = f"{v_inputs}concat=n={n}:v=1:a=0[vout]"

    if info["has_audio"]:
        a_inputs = "".join(f"[a{i}]" for i in range(n))
        concat_a = f"{a_inputs}concat=n={n}:v=0:a=1[aout]"
        filter_complex = ";".join(parts_v + parts_a + [concat_v, concat_a])
        cmd = [
            "-i", str(video_path),
            "-filter_complex", filter_complex,
            "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-r", str(fps),
            "-c:a", "aac", "-b:a", "192k",
            "-pix_fmt", "yuv420p",
            "-y", str(output_path),
        ]
    else:
        filter_complex = ";".join(parts_v + [concat_v])
        cmd = [
            "-i", str(video_path),
            "-filter_complex", filter_complex,
            "-map", "[vout]",
            "-c:v", "libx264", "-preset", "fast", "-crf", "23",
            "-r", str(fps),
            "-pix_fmt", "yuv420p",
            "-an",
            "-y", str(output_path),
        ]

    await run_ffmpeg(cmd)


def generate_ass(words: list[dict], style: str, video_width: int, video_height: int) -> str:
    """
    Генерируем ASS файл субтитров.
    style='word' — по 1-3 слова (как в рилсах)
    style='phrase' — по фразе/сегменту
    """
    # Масштабируем размер шрифта под разрешение
    base_size = max(video_height // 25, 36)

    # ASS header
    ass = f"""[Script Info]
Title: Subtitles
ScriptType: v4.00+
PlayResX: {video_width}
PlayResY: {video_height}
WrapStyle: 0

[V4+ Styles]
Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding
Style: Default,DejaVu Sans,{base_size},&H00FFFFFF,&H000000FF,&H00000000,&H96000000,-1,0,0,0,100,100,1,0,3,0,0,2,20,20,{video_height // 12},1

[Events]
Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text
"""

    def format_time(seconds: float) -> str:
        """Формат ASS: H:MM:SS.CC"""
        h = int(seconds // 3600)
        m = int((seconds % 3600) // 60)
        s = int(seconds % 60)
        cs = int((seconds % 1) * 100)
        return f"{h}:{m:02d}:{s:02d}.{cs:02d}"

    if not words:
        return ass

    if style == "word":
        # По 1-3 слова — группируем
        groups = []
        i = 0
        while i < len(words):
            # Берём 1-3 слова, не разбивая если слова идут подряд быстро
            group = [words[i]]
            j = i + 1
            max_words = 3
            while j < len(words) and len(group) < max_words:
                gap = words[j]["start"] - words[j - 1]["end"]
                if gap > 0.4:  # Пауза — разрываем группу
                    break
                group.append(words[j])
                j += 1
            groups.append(group)
            i = j

        for group in groups:
            start = group[0]["start"]
            end = group[-1]["end"]
            # Минимальная длительность показа — 0.3с
            if end - start < 0.3:
                end = start + 0.3
            text = " ".join(w["word"].strip().lower() for w in group)
            ass += f"Dialogue: 0,{format_time(start)},{format_time(end)},Default,,0,0,0,,{text}\n"
    else:
        # По фразе — группируем слова в предложения (до 8 слов или пауза > 0.8с)
        groups = []
        current_group = [words[0]]
        for i in range(1, len(words)):
            gap = words[i]["start"] - words[i - 1]["end"]
            if gap > 0.8 or len(current_group) >= 8:
                groups.append(current_group)
                current_group = [words[i]]
            else:
                current_group.append(words[i])
        if current_group:
            groups.append(current_group)

        for group in groups:
            start = group[0]["start"]
            end = group[-1]["end"]
            if end - start < 0.5:
                end = start + 0.5
            text = " ".join(w["word"].strip().lower() for w in group)
            ass += f"Dialogue: 0,{format_time(start)},{format_time(end)},Default,,0,0,0,,{text}\n"

    return ass


async def burn_subtitles(video_path: Path, ass_path: Path, output_path: Path, info: dict):
    """Накладываем ASS субтитры на видео."""
    # Экранируем путь для фильтра (Windows-пути и спецсимволы)
    ass_str = str(ass_path).replace("\\", "/").replace(":", "\\:")

    cmd = [
        "-i", str(video_path),
        "-vf", f"ass={ass_str}",
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


# --- Endpoint ---

@router.post("/subtitle-video", response_model=SubtitleVideoResponse)
async def subtitle_video(req: SubtitleVideoRequest):
    """
    Полный пайплайн: транскрипция → вырезание тишины/филлеров → субтитры.
    """
    if not os.environ.get("OPENAI_API_KEY"):
        raise HTTPException(status_code=500, detail="OPENAI_API_KEY not configured")

    job_id = str(uuid.uuid4())[:8]
    job_dir = WORK_DIR / job_id
    job_dir.mkdir(exist_ok=True)

    # 1. Скачиваем видео
    video_path = job_dir / "input.mp4"
    await download_file(req.video_url, video_path)

    info = await get_video_info(video_path)

    # 2. Извлекаем аудио
    audio_path = job_dir / "audio.wav"
    await extract_audio(video_path, audio_path)

    # 3. Транскрибируем через Whisper
    transcript = await transcribe_whisper(audio_path, req.language)
    words = transcript["words"]

    if not words:
        raise HTTPException(status_code=400, detail="Whisper не распознал речь в видео")

    # 4. Определяем что вырезать
    fillers = FILLERS_RU if req.language == "ru" else FILLERS_EN
    skip_indices = set()
    if req.remove_fillers:
        for i, w in enumerate(words):
            clean = w["word"].strip().lower().rstrip(".,!?;:")
            if clean in fillers:
                skip_indices.add(i)

    segments = identify_cuts(
        words, req.language, req.remove_silence,
        req.remove_fillers, req.silence_threshold, info["duration"]
    )

    # 5. Вырезаем сегменты
    need_trim = req.remove_silence or req.remove_fillers
    if need_trim and len(segments) > 0:
        trimmed_path = job_dir / "trimmed.mp4"
        await trim_video(video_path, segments, trimmed_path, info)
        current_video = trimmed_path

        # Пересчитываем таймкоды слов после обрезки
        remapped_words = remap_timestamps(words, segments, skip_indices)
        # Обновляем инфо о видео после обрезки
        info = await get_video_info(trimmed_path)
    else:
        current_video = video_path
        remapped_words = [w for i, w in enumerate(words) if i not in skip_indices]

    # 6-7. Генерируем и накладываем субтитры
    if req.add_subtitles and remapped_words:
        ass_path = job_dir / "subtitles.ass"
        ass_content = generate_ass(
            remapped_words, req.subtitle_style,
            info["width"], info["height"],
        )
        ass_path.write_text(ass_content, encoding="utf-8")

        output_path = job_dir / "final.mp4"
        await burn_subtitles(current_video, ass_path, output_path, info)
    else:
        output_path = current_video

    filename = f"subtitled_{job_id}.mp4"
    # Переименовываем финальный файл
    final_path = job_dir / filename
    if output_path != final_path:
        output_path.rename(final_path)

    return SubtitleVideoResponse(
        status="done",
        output_url=f"/download/{job_id}/{filename}",
        filename=filename,
        transcript=transcript["text"],
    )
