# Kaizen FFmpeg Service

Микросервис для пост-обработки видео Kaizen Detailers.

## Что делает
- Накладывает логотип (watermark) справа сверху на всё видео
- Добавляет аутро: тёмный экран + логотип по центру (+ слоган, когда появится)
- Возвращает готовое видео для публикации

## Деплой на Railway

1. Создай новый проект на [railway.app](https://railway.app)
2. Подключи GitHub репо (или залей через CLI)
3. Railway автоматически найдёт Dockerfile и задеплоит
4. Получишь URL вида `https://kaizen-ffmpeg-xxx.up.railway.app`

## API

### POST /process

```json
{
    "video_url": "https://...",
    "logo_url": "https://...",
    "slogan": null,
    "outro_duration": 3.0,
    "watermark_opacity": 0.8,
    "watermark_scale": 0.15,
    "watermark_margin": 20
}
```

Ответ:
```json
{
    "status": "done",
    "output_url": "/download/abc123/kaizen_abc123.mp4",
    "filename": "kaizen_abc123.mp4"
}
```

### GET /download/{job_id}/{filename}

Скачать готовое видео.

## Использование в n8n

HTTP Request нода:
- **Method:** POST
- **URL:** `https://<railway-url>/process`
- **Body:**
  - `video_url`: `{{ $json.video_url }}` (из Airtable)
  - `logo_url`: `{{ $json.logo_url }}` (из Airtable)

Затем второй HTTP Request для скачивания:
- **Method:** GET
- **URL:** `https://<railway-url>{{ $json.output_url }}`

## Параметры

| Параметр | По умолчанию | Описание |
|----------|-------------|----------|
| watermark_opacity | 0.8 | Прозрачность логотипа (0.0-1.0) |
| watermark_scale | 0.15 | Размер логотипа (% от ширины видео) |
| watermark_margin | 20 | Отступ от края (px) |
| outro_duration | 3.0 | Длительность аутро (сек) |
| slogan | null | Текст слогана (пока не указан) |
