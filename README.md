# find-best-photos

Ранжирует фото по качеству: резкость, экспозиция, лица, композиция, шум, смаз.
FastAPI + OpenCV, встроенный веб-интерфейс. Поддерживает JPEG/PNG, HEIC/HEIF
(iPhone) и RAW/DNG.

## Запуск локально

```bash
pip install -r requirements.txt
uvicorn main:app --reload
```

Открыть http://localhost:8000

## Загрузка через Cloudflare R2 (опционально)

По умолчанию браузер шлёт фото multipart прямо на бекенд. Если настроить R2,
браузер грузит **оригиналы напрямую в R2** по presigned-URL, а бекенд скачивает
их по ключу для анализа (оригиналы не удаляются — хранятся в бакете).

Включается переменными окружения:

| Переменная | Назначение |
|------------|-----------|
| `R2_ACCOUNT_ID` | ID аккаунта Cloudflare |
| `R2_ACCESS_KEY_ID` | Access Key R2 API-токена |
| `R2_SECRET_ACCESS_KEY` | Secret Key R2 API-токена |
| `R2_BUCKET` | Имя бакета |
| `R2_PREFIX` | Префикс ключей (по умолчанию `uploads`) |
| `R2_PUT_EXPIRES` | Срок жизни presigned-URL, сек (по умолчанию 3600) |

Если переменных нет — приложение работает по обычному пути (multipart на бекенд),
ничего настраивать не нужно.

### CORS на бакете (обязательно для R2)

Браузерный `PUT` упрётся в CORS, если не разрешить его на бакете. В дашборде
R2 → бакет → Settings → CORS Policy:

```json
[
  {
    "AllowedOrigins": ["https://ваш-домен"],
    "AllowedMethods": ["PUT"],
    "AllowedHeaders": ["*"],
    "MaxAgeSeconds": 3600
  }
]
```

(для локальной разработки добавьте `http://localhost:8000`)

### Хранение

Оригиналы остаются в R2 после анализа. Если со временем захотите автоматически
подчищать старые загрузки — настройте Object Lifecycle rule на префикс `uploads/`
в настройках бакета.

## Прочие переменные окружения

| Переменная | По умолчанию | Назначение |
|------------|--------------|-----------|
| `MAX_FILE_MB` | 80 | Лимит размера одного файла |
| `MAX_FILES_PER_REQUEST` | 50 | Лимит файлов на запрос |
| `ANALYZE_MAX_DIM` | 1280 | Ужимание по длинной стороне перед анализом |
| `ANALYZE_WORKERS` | по числу ядер | Размер пула потоков анализа |
| `UPLOAD_CONCURRENCY` | 4 | Параллельных батчей на клиенте |
| `SHARPNESS_REF` / `NOISE_REF` | 500 / 12 | Калибровка резкости/шума |

## Docker

Ключи **не запекаются в образ** — передаются в рантайме. Образ остаётся без
секретов (их не видно в `docker history` и не утечёт в реестр).

Самый простой способ — `docker compose` (подхватывает `.env` сам):

```bash
cp .env.example .env      # впиши R2-ключи
docker compose up --build
```

Либо вручную через `docker run`, передавая файл с переменными:

```bash
docker build -t photo-ranker .
docker run -p 8000:8000 --env-file .env photo-ranker
```

Или отдельными флагами `-e` (например, из секрет-менеджера CI):

```bash
docker run -p 8000:8000 \
  -e R2_ACCOUNT_ID=... -e R2_ACCESS_KEY_ID=... \
  -e R2_SECRET_ACCESS_KEY=... -e R2_BUCKET=... \
  photo-ranker
```

> Не добавляй ключи через `ENV` в `Dockerfile` и не копируй `.env` внутрь образа —
> они попадут в слои и утекут вместе с образом. `.env` уже в `.gitignore` и
> `.dockerignore`.

## Деплой на Render

Render собирает из `Dockerfile`. Ключи задаются в **Environment** сервиса
(Dashboard → сервис → Environment), а не в образе. Файл `.env` для Render не нужен.
