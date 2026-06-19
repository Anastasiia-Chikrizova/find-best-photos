"""Photo Ranker — FastAPI-приложение для ранжирования фото по качеству.

Анализ вынесен в scoring.py, фронтенд — в index.html.
Лимиты и калибровка настраиваются через переменные окружения.
"""

import os
import json
import base64
import asyncio
import logging
from pathlib import Path
from typing import List
from concurrent.futures import ThreadPoolExecutor

from fastapi import FastAPI, UploadFile, File, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse, Response

import scoring
import r2

app = FastAPI(title="Photo Ranker")
log = logging.getLogger("photo-ranker")

# --- Лимиты (через окружение) -------------------------------------------------
MAX_FILE_MB = float(os.getenv("MAX_FILE_MB", "80"))          # макс. размер одного файла (ProRAW DNG бывает 25–75 МБ)
MAX_FILES_PER_REQUEST = int(os.getenv("MAX_FILES_PER_REQUEST", "50"))  # макс. файлов за запрос
MAX_FILE_BYTES = int(MAX_FILE_MB * 1024 * 1024)

# Анализ — CPU-bound и синхронный, поэтому крутим его в пуле потоков: event loop
# не блокируется, а OpenCV на тяжёлых операциях отпускает GIL, так что батч
# реально раскладывается по ядрам. Размер пула — по числу ядер (env переопределяет).
WORKERS = int(os.getenv("ANALYZE_WORKERS", str(min(8, (os.cpu_count() or 2)))))
_EXECUTOR = ThreadPoolExecutor(max_workers=WORKERS)


async def _analyze_batch(items: List[tuple]) -> list:
    """items: список (filename, bytes). Параллельно считает метрики, бьётые/битые пропускает."""
    loop = asyncio.get_running_loop()
    tasks = [loop.run_in_executor(_EXECUTOR, scoring.analyze_image, data, name) for name, data in items]
    metrics_list = await asyncio.gather(*tasks)
    out = []
    for (name, _), metrics in zip(items, metrics_list):
        if metrics is not None:
            out.append({"filename": name, **metrics})
    return out

BASE_DIR = Path(__file__).resolve().parent

# Конфиг для фронтенда: веса и калибровка резкости отдаются в страницу,
# чтобы клиент мог собрать итоговый рейтинг из накопленных батчей сам.
_FRONT_CONFIG = {
    "sharpnessWeight": scoring.WEIGHTS["sharpness"],
    "sharpnessRef": scoring.SHARPNESS_REF,
    "maxFileMb": MAX_FILE_MB,
    "maxFilesPerRequest": MAX_FILES_PER_REQUEST,
    # Сколько батчей фронт держит «в полёте» одновременно. Несколько параллельных
    # запросов прячут сетевую задержку и кормят серверный пул потоков без простоя.
    "uploadConcurrency": int(os.getenv("UPLOAD_CONCURRENCY", "4")),
    # Если R2 настроен — фронт грузит оригиналы прямо в R2, иначе шлёт multipart
    # на /analyze. Так приложение работает и без облака (локально / без креденшелов).
    "r2Enabled": r2.enabled,
}


def _load_index() -> str:
    html = (BASE_DIR / "index.html").read_text(encoding="utf-8")
    return html.replace("__CONFIG__", json.dumps(_FRONT_CONFIG))


def _check_file(f: UploadFile, data: bytes) -> None:
    if len(data) > MAX_FILE_BYTES:
        raise HTTPException(
            status_code=413,
            detail=f"Файл {f.filename} больше {MAX_FILE_MB:.0f} МБ",
        )


@app.get("/", response_class=HTMLResponse)
def root():
    return _load_index()


@app.get("/health")
def health():
    return {"status": "ok"}


@app.post("/analyze")
async def analyze_photos(files: List[UploadFile] = File(...)):
    """Анализирует один батч и возвращает СЫРЫЕ метрики без финального ранга.

    Резкость нормируется на стороне клиента по всему набору (см. index.html),
    поэтому здесь ранжирование не делается — это даёт корректный рейтинг при
    разбиении большого набора на несколько запросов.
    """
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=413,
            detail=f"Не больше {MAX_FILES_PER_REQUEST} файлов за запрос",
        )

    items = []
    for f in files:
        data = await f.read()
        _check_file(f, data)
        items.append((f.filename, data))

    results = await _analyze_batch(items)
    return JSONResponse(content={"count": len(results), "photos": results})


@app.get("/r2/health")
def r2_health():
    """Диагностика R2: коннект + доступность бакета. Дёрни в браузере/curl."""
    ok, detail = r2.check()
    return JSONResponse(status_code=200 if ok else 503, content={"ok": ok, "detail": detail})


@app.post("/r2/sign")
def r2_sign(payload: dict):
    """Выдаёт presigned-PUT URL-ы, чтобы браузер залил оригиналы прямо в R2.

    Тело: {"files": [{"name": ..., "size": ...}, ...]}
    Ответ: {"uploads": [{"name", "key", "url"}, ...]} в том же порядке.
    """
    if not r2.enabled:
        raise HTTPException(status_code=503, detail="R2 не настроен")
    files = payload.get("files", [])
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(status_code=413, detail=f"Не больше {MAX_FILES_PER_REQUEST} файлов за запрос")

    uploads = []
    for f in files:
        name = f.get("name", "file")
        if int(f.get("size", 0)) > MAX_FILE_BYTES:
            raise HTTPException(status_code=413, detail=f"Файл {name} больше {MAX_FILE_MB:.0f} МБ")
        try:
            key, url = r2.presign_put(name)
        except Exception as e:
            # Чаще всего — кривой конфиг R2 (например, R2_BUCKET = URL, а не имя бакета).
            log.exception("presign_put failed")
            raise HTTPException(status_code=500, detail=f"Ошибка подписи R2: {e}")
        uploads.append({"name": name, "key": key, "url": url})
    return JSONResponse(content={"uploads": uploads})


def _fetch_and_score(key: str, name: str):
    """Скачивает объект из R2 и считает метрики. Выполняется в пуле потоков."""
    data = r2.get_bytes(key, max_bytes=MAX_FILE_BYTES)
    if data is None:
        return None
    metrics = scoring.analyze_image(data, name)
    if metrics is None:
        return None
    # key возвращаем клиенту — по нему он потом просит серверное превью /r2/thumb
    return {"filename": name or key, "key": key, **metrics}


@app.post("/r2/analyze")
async def r2_analyze(payload: dict):
    """Анализирует уже загруженные в R2 объекты по ключам.

    Тело: {"items": [{"key": ..., "name": ...}, ...]}. Возвращает сырые метрики
    без ранга — финальную калибровку резкости делает клиент (как в /analyze).
    """
    if not r2.enabled:
        raise HTTPException(status_code=503, detail="R2 не настроен")
    items = payload.get("items", [])
    if len(items) > MAX_FILES_PER_REQUEST:
        raise HTTPException(status_code=413, detail=f"Не больше {MAX_FILES_PER_REQUEST} файлов за запрос")

    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(_EXECUTOR, _fetch_and_score, it.get("key", ""), it.get("name", ""))
        for it in items
    ]
    results = [r for r in await asyncio.gather(*tasks) if r is not None]
    return JSONResponse(content={"count": len(results), "photos": results})


def _thumb_from_r2(key: str, max_dim: int):
    """Скачивает объект из R2 и делает JPEG-превью. В пуле потоков."""
    data = r2.get_bytes(key, max_bytes=MAX_FILE_BYTES)
    if data is None:
        return None
    return scoring.thumbnail_jpeg(data, key, max_dim=max_dim)


@app.get("/r2/thumb")
async def r2_thumb(key: str, size: int = 400):
    """Серверное JPEG-превью объекта из R2 — для браузеров, что не рисуют HEIC/RAW."""
    if not r2.enabled:
        raise HTTPException(status_code=503, detail="R2 не настроен")
    max_dim = min(2000, max(64, size))
    loop = asyncio.get_running_loop()
    jpeg = await loop.run_in_executor(_EXECUTOR, _thumb_from_r2, key, max_dim)
    if jpeg is None:
        raise HTTPException(status_code=404, detail="превью недоступно")
    return Response(content=jpeg, media_type="image/jpeg",
                    headers={"Cache-Control": "private, max-age=3600"})


@app.post("/rank")
async def rank_photos(files: List[UploadFile] = File(...)):
    """Полный цикл в одном запросе: анализ + калибровка + ранжирование.

    Удобно для API/curl. UI же шлёт батчи в /analyze и ранжирует сам.
    """
    if len(files) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=413,
            detail=f"Не больше {MAX_FILES_PER_REQUEST} файлов за запрос",
        )

    items = []
    for f in files:
        data = await f.read()
        _check_file(f, data)
        items.append((f.filename, data))

    results = await _analyze_batch(items)
    scoring.normalize_sharpness(results)
    return JSONResponse(content={"count": len(results), "photos": results})


@app.post("/rank-base64")
async def rank_base64(payload: dict):
    photos = payload.get("photos", [])
    if len(photos) > MAX_FILES_PER_REQUEST:
        raise HTTPException(
            status_code=413,
            detail=f"Не больше {MAX_FILES_PER_REQUEST} файлов за запрос",
        )

    items = []
    for item in photos:
        name = item.get("filename", "unknown")
        try:
            data = base64.b64decode(item.get("data", ""))
        except Exception:
            continue
        if len(data) > MAX_FILE_BYTES:
            continue
        items.append((name, data))

    results = await _analyze_batch(items)
    scoring.normalize_sharpness(results)
    return JSONResponse(content={"count": len(results), "photos": results})
