"""Cloudflare R2 (S3-совместимое хранилище): presigned-загрузка и чтение.

Браузер грузит оригиналы прямо в R2 по presigned-PUT URL, а бекенд потом
скачивает их по ключу для анализа. Оригиналы НЕ удаляем (храним).

Включается переменными окружения; если их нет — enabled=False и приложение
работает по обычному пути (multipart прямо на бекенд).
"""

import os
import re
import uuid

ACCOUNT_ID = os.getenv("R2_ACCOUNT_ID", "")
ACCESS_KEY = os.getenv("R2_ACCESS_KEY_ID", "")
SECRET_KEY = os.getenv("R2_SECRET_ACCESS_KEY", "")
BUCKET = os.getenv("R2_BUCKET", "")
PREFIX = os.getenv("R2_PREFIX", "uploads")           # папка-префикс для ключей
PUT_EXPIRES = int(os.getenv("R2_PUT_EXPIRES", "3600"))  # срок жизни presigned-URL, сек

enabled = bool(ACCOUNT_ID and ACCESS_KEY and SECRET_KEY and BUCKET)

_client = None
if enabled:
    try:
        import boto3
        from botocore.config import Config
        _client = boto3.client(
            "s3",
            endpoint_url=f"https://{ACCOUNT_ID}.r2.cloudflarestorage.com",
            aws_access_key_id=ACCESS_KEY,
            aws_secret_access_key=SECRET_KEY,
            config=Config(signature_version="s3v4", region_name="auto"),
        )
    except Exception:
        enabled = False
        _client = None


def _safe_name(name: str) -> str:
    """Берём только basename и чистим от опасных символов, расширение сохраняем."""
    name = os.path.basename(name or "file")
    name = re.sub(r"[^\w.\-]+", "_", name).strip("._") or "file"
    return name[:128]


def presign_put(name: str):
    """Генерит уникальный ключ и presigned-PUT URL. Возвращает (key, url)."""
    key = f"{PREFIX}/{uuid.uuid4().hex}/{_safe_name(name)}"
    url = _client.generate_presigned_url(
        "put_object",
        Params={"Bucket": BUCKET, "Key": key},  # content-type не подписываем — браузер шлёт свой
        ExpiresIn=PUT_EXPIRES,
    )
    return key, url


def get_bytes(key: str, max_bytes: int = None):
    """Скачивает объект по ключу. None — если нет/ошибка/больше лимита."""
    if not key.startswith(f"{PREFIX}/"):
        return None  # не выпускаем за пределы своего префикса
    try:
        obj = _client.get_object(Bucket=BUCKET, Key=key)
        if max_bytes is not None and obj.get("ContentLength", 0) > max_bytes:
            return None
        return obj["Body"].read()
    except Exception:
        return None
