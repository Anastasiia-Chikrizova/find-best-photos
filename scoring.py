"""Анализ изображений и подсчёт метрик качества.

Каждая метрика, кроме «сырой» резкости, нормирована в диапазон [0, 1].
Резкость возвращается «сырой» (дисперсия Лапласиана) — её нормировка
зависит от батча и делается на стороне эндпоинта, см. normalize_sharpness().
"""

import os
import io
import numpy as np
import cv2
from PIL import Image, ImageOps

# HEIC/HEIF (формат iPhone по умолчанию). Колёса pillow-heif везут libheif
# с собой, так что в Docker доп. системных пакетов не нужно.
try:
    import pillow_heif
    pillow_heif.register_heif_opener()
    _HEIF_OK = True
except Exception:
    _HEIF_OK = False

# RAW (Apple ProRAW = .dng, плюс форматы других камер). rawpy везёт libraw.
try:
    import rawpy
    _RAW_OK = True
except Exception:
    _RAW_OK = False

# Снимаем лимит Pillow на «бомбу декомпрессии» — размер мы и так режем по байтам.
Image.MAX_IMAGE_PIXELS = None

# RAW-расширения: их Pillow не возьмёт, нужен libraw.
RAW_EXTS = {"dng", "arw", "cr2", "cr3", "nef", "nrw", "raf",
            "orf", "rw2", "pef", "srw", "raw", "3fr", "dcr"}

# --- Конфиг через окружение ---------------------------------------------------
# Опорное значение резкости (дисперсия Лапласиана) для одиночных фото и
# тонких батчей, где нечего калибровать. Под разные камеры значение разное,
# поэтому при батче калибруемся относительно самого батча (см. normalize_sharpness).
SHARPNESS_REF = float(os.getenv("SHARPNESS_REF", "500"))
# Опорный уровень шума (sigma по методу Immerkær). Выше — фото зашумлённее.
NOISE_REF = float(os.getenv("NOISE_REF", "12"))
# Перед анализом ужимаем фото до этого размера по длинной стороне. Главный
# рычаг скорости при сотнях снимков: метрики попиксельные, на 12 Мп считать
# их бессмысленно. Заодно приводит все фото к одному масштабу — резкость, шум
# и смаз становятся сопоставимыми между разными камерами/разрешениями.
ANALYZE_MAX_DIM = int(os.getenv("ANALYZE_MAX_DIM", "1280"))

# Веса итоговой оценки. Сумма = 1.0.
WEIGHTS = {
    "sharpness": 0.30,
    "exposure": 0.20,
    "faces": 0.15,
    "composition": 0.10,
    "noise": 0.15,       # применяется как (1 - noise_penalty)
    "motion": 0.10,      # применяется как (1 - motion_penalty)
}

_HAAR = cv2.data.haarcascades
face_cascade = cv2.CascadeClassifier(_HAAR + "haarcascade_frontalface_default.xml")
eye_cascade = cv2.CascadeClassifier(_HAAR + "haarcascade_eye.xml")


def _pil_to_bgr(im: "Image.Image") -> np.ndarray:
    """PIL → BGR-массив с применённой EXIF-ориентацией.

    iPhone хранит ориентацию в EXIF, а не в пикселях. Без exif_transpose
    портретные кадры приходят «лёжа» — и Haar-каскад их лиц не находит.
    """
    im = ImageOps.exif_transpose(im)
    rgb = np.asarray(im.convert("RGB"))
    return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)


def _decode_raw(data: bytes):
    """RAW/DNG → BGR через libraw. half_size — быстрее и нам всё равно ужимать."""
    if not _RAW_OK:
        return None
    try:
        with rawpy.imread(io.BytesIO(data)) as raw:
            rgb = raw.postprocess(use_camera_wb=True, half_size=True)
        return cv2.cvtColor(rgb, cv2.COLOR_RGB2BGR)
    except Exception:
        return None


def decode_image(data: bytes, filename: str = ""):
    """Декодирует байты в BGR-картинку. None — если формат не распознан.

    Порядок: RAW по расширению → Pillow (JPEG/PNG/HEIC/HEIF/TIFF/WebP, с
    EXIF-ориентацией) → OpenCV → RAW как последняя попытка. Для JPEG Pillow
    декодирует сразу уменьшенным (draft) — тот же выигрыш, что у IMREAD_REDUCED.
    """
    ext = filename.rsplit(".", 1)[-1].lower() if "." in filename else ""

    if ext in RAW_EXTS:
        img = _decode_raw(data)
        if img is not None:
            return img

    try:
        im = Image.open(io.BytesIO(data))
        im.draft("RGB", (ANALYZE_MAX_DIM, ANALYZE_MAX_DIM))  # быстрый уменьшенный декод JPEG; для прочих — no-op
        return _pil_to_bgr(im)
    except Exception:
        pass

    arr = np.frombuffer(data, np.uint8)
    img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
    if img is not None:
        return img

    return _decode_raw(data)  # вдруг RAW без внятного расширения


def _fit(img: np.ndarray) -> np.ndarray:
    """Ужимает картинку до ANALYZE_MAX_DIM по длинной стороне (увеличение не делаем)."""
    h, w = img.shape[:2]
    longest = max(h, w)
    if longest <= ANALYZE_MAX_DIM:
        return img
    scale = ANALYZE_MAX_DIM / longest
    return cv2.resize(img, (round(w * scale), round(h * scale)), interpolation=cv2.INTER_AREA)


def sharpness(gray: np.ndarray) -> float:
    """Резкость как дисперсия Лапласиана. Чем выше — тем резче (меньше расфокус)."""
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def exposure(gray: np.ndarray) -> float:
    """Доля пикселей вне зон пересвета/провала. 1.0 — идеально, 0 — всё выбито."""
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
    total = hist.sum()
    if total == 0:
        return 0.0
    hist = hist / total
    underexposed = float(hist[:50].sum())
    overexposed = float(hist[220:].sum())
    return max(0.0, 1.0 - underexposed - overexposed)


def face_score(gray: np.ndarray) -> float:
    """Оценка лиц: есть лицо — хорошо, видны оба глаза (в фокусе) — ещё лучше."""
    faces = face_cascade.detectMultiScale(gray, 1.1, 4, minSize=(30, 30))
    if len(faces) == 0:
        return 0.0
    score = 0.0
    for (x, y, w, h) in faces:
        roi = gray[y:y + h, x:x + w]
        eyes = eye_cascade.detectMultiScale(roi, 1.1, 3)
        score += 1.0 + (0.5 if len(eyes) >= 2 else 0.0)
    return min(score / len(faces), 2.0) / 2.0


def composition(gray: np.ndarray) -> float:
    """Правило третей: какая доля контуров приходится на линии третей."""
    edges = cv2.Canny(gray, 50, 150)
    h, w = edges.shape
    third_h, third_w = h // 3, w // 3
    thirds_mask = np.zeros_like(edges)
    for i in (1, 2):
        thirds_mask[i * third_h - 10:i * third_h + 10, :] = 255
        thirds_mask[:, i * third_w - 10:i * third_w + 10] = 255
    intersection = cv2.bitwise_and(edges, thirds_mask)
    total_edges = float(edges.sum())
    if total_edges == 0:
        return 0.0
    return float(intersection.sum() / total_edges)


def noise_penalty(gray: np.ndarray) -> float:
    """Оценка шума (ISO-артефакты) методом Immerkær.

    Сворачиваем картинку с ядром Лапласиана 2-го порядка и оцениваем sigma
    гауссова шума. Возвращаем штраф в [0, 1]: 0 — чисто, 1 — очень шумно.
    """
    h, w = gray.shape
    if h < 3 or w < 3:
        return 0.0
    kernel = np.array([[1, -2, 1], [-2, 4, -2], [1, -2, 1]], dtype=np.float64)
    conv = cv2.filter2D(gray.astype(np.float64), -1, kernel)
    sigma = np.abs(conv).sum() * np.sqrt(0.5 * np.pi) / (6.0 * (w - 2) * (h - 2))
    return float(min(sigma / NOISE_REF, 1.0))


def motion_penalty(gray: np.ndarray) -> float:
    """Детект смаза от движения (отдельно от расфокуса).

    Смаз направлен: вдоль направления движения высокие частоты «съедаются»,
    поперёк — сохраняются. Считаем энергию градиента по 4 направлениям и меряем
    анизотропию. Расфокус, наоборот, изотропен (давит все направления ровно),
    поэтому даёт низкую анизотропию. Штраф в [0, 1].
    """
    g = gray.astype(np.float64)
    kernels = {
        "h": np.array([[-1, 0, 1]], dtype=np.float64),
        "v": np.array([[-1], [0], [1]], dtype=np.float64),
        "d1": np.array([[-1, 0, 0], [0, 0, 0], [0, 0, 1]], dtype=np.float64),
        "d2": np.array([[0, 0, -1], [0, 0, 0], [1, 0, 0]], dtype=np.float64),
    }
    energies = [float(np.abs(cv2.filter2D(g, -1, k)).mean()) for k in kernels.values()]
    emax, emin = max(energies), min(energies)
    if emax < 1e-6:
        return 0.0
    return float((emax - emin) / emax)


def analyze_image(data: bytes, filename: str = ""):
    """Считает все метрики для одного изображения.

    Возвращает словарь с «сырой» резкостью (sharpness_raw) и частичной оценкой
    `partial` — взвешенной суммой всех метрик, КРОМЕ резкости. Итоговый score
    собирается в normalize_sharpness() после калибровки резкости по батчу.
    """
    img = decode_image(data, filename)
    if img is None:
        return None
    img = _fit(img)
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)

    s = sharpness(gray)
    e = exposure(gray)
    f = face_score(gray)
    c = composition(gray)
    n = noise_penalty(gray)
    m = motion_penalty(gray)

    partial = (
        WEIGHTS["exposure"] * e
        + WEIGHTS["faces"] * f
        + WEIGHTS["composition"] * c
        + WEIGHTS["noise"] * (1.0 - n)
        + WEIGHTS["motion"] * (1.0 - m)
    )

    return {
        "sharpness_raw": round(s, 2),
        "exposure": round(e, 4),
        "faces": round(f, 4),
        "composition": round(c, 4),
        "noise": round(n, 4),
        "motion": round(m, 4),
        "partial": round(partial, 6),
    }


def normalize_sharpness(results: list) -> None:
    """Калибрует резкость относительно батча и проставляет итоговый score + rank.

    Магическое число 500 плохо переносится между камерами, поэтому опорой
    берём 90-й перцентиль резкости внутри батча (но не ниже SHARPNESS_REF * 0.2,
    чтобы серия одинаково мыльных фото не «растягивалась» искусственно).
    Мутирует элементы списка на месте.
    """
    if not results:
        return

    raws = np.array([r["sharpness_raw"] for r in results], dtype=np.float64)
    if len(raws) >= 5:
        ref = float(np.percentile(raws, 90))
        ref = max(ref, SHARPNESS_REF * 0.2)
    else:
        ref = SHARPNESS_REF
    ref = max(ref, 1.0)

    for r in results:
        sharp_norm = min(r["sharpness_raw"] / ref, 1.0)
        total = WEIGHTS["sharpness"] * sharp_norm + r["partial"]
        r["sharpness"] = round(sharp_norm, 4)
        r["score"] = round(total, 4)
        r.pop("partial", None)

    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
