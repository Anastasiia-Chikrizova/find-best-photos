import cv2
import numpy as np
from fastapi import FastAPI, UploadFile, File
from fastapi.responses import JSONResponse, HTMLResponse
from typing import List
import base64

app = FastAPI(title="Photo Ranker")

HTML = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Photo Ranker</title>
<style>
  * { box-sizing: border-box; margin: 0; padding: 0; }
  body { font-family: system-ui, sans-serif; background: #0f0f0f; color: #e0e0e0; min-height: 100vh; }
  header { padding: 20px 24px; border-bottom: 1px solid #222; }
  header h1 { font-size: 1.3rem; font-weight: 600; letter-spacing: -0.02em; }
  header p { font-size: 0.82rem; color: #666; margin-top: 4px; }
  .drop-zone {
    display: block; margin: 24px auto; max-width: 640px;
    border: 2px dashed #333; border-radius: 12px;
    padding: 36px 24px; text-align: center; cursor: pointer;
    transition: border-color 0.2s, background 0.2s;
  }
  .drop-zone.drag-over { border-color: #555; background: #1a1a1a; }
  .drop-zone svg { opacity: 0.3; margin-bottom: 12px; }
  .drop-zone p { color: #555; font-size: 0.9rem; }
  .drop-zone span { color: #888; font-size: 0.8rem; margin-top: 8px; display: block; }
  #file-input { display: none; }
  #status { text-align: center; padding: 12px 16px; color: #666; font-size: 0.85rem; min-height: 36px; }
  #status.error { color: #e05555; }
  .grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(160px, 1fr)); gap: 12px; padding: 0 16px 48px; }
  @media (max-width: 480px) {
    header { padding: 14px 16px; }
    header h1 { font-size: 1.1rem; }
    .drop-zone { margin: 16px 16px; padding: 28px 16px; }
    .drop-zone svg { width: 36px; height: 36px; }
    .grid { grid-template-columns: repeat(2, 1fr); gap: 10px; padding: 0 12px 32px; }
    .card-body { padding: 8px; }
  }
  .card {
    background: #1a1a1a; border-radius: 10px; overflow: hidden;
    border: 1px solid #2a2a2a; transition: transform 0.15s;
  }
  .card:hover { transform: translateY(-2px); }
  .card img { width: 100%; aspect-ratio: 1; object-fit: cover; display: block; }
  .card-body { padding: 12px; }
  .rank { font-size: 0.7rem; color: #555; text-transform: uppercase; letter-spacing: 0.08em; margin-bottom: 4px; }
  .rank b { color: #aaa; font-size: 1rem; }
  .filename { font-size: 0.78rem; color: #777; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; margin-bottom: 10px; }
  .score-row { display: flex; justify-content: space-between; font-size: 0.72rem; color: #555; margin-top: 4px; }
  .score-row span:last-child { color: #888; }
  .bar-wrap { height: 3px; background: #2a2a2a; border-radius: 2px; margin-top: 10px; }
  .bar { height: 100%; border-radius: 2px; background: linear-gradient(90deg, #4a9eff, #a78bfa); }
  .medal-1 { border-color: #b8860b; }
  .medal-2 { border-color: #606060; }
  .medal-3 { border-color: #7a4a2a; }
</style>
</head>
<body>
<header>
  <h1>Photo Ranker</h1>
  <p>Сортирует фото по резкости, экспозиции, лицам и композиции</p>
</header>

<label class="drop-zone" id="drop-zone" for="file-input">
  <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5">
    <path d="M21 15v4a2 2 0 01-2 2H5a2 2 0 01-2-2v-4M17 8l-5-5-5 5M12 3v12"/>
  </svg>
  <p>Перетащи фото сюда или нажми для выбора</p>
  <span>Можно сразу много — JPG, PNG, HEIC</span>
</label>
<input type="file" id="file-input" multiple accept="image/*" style="display:none">

<div id="status"></div>
<div class="grid" id="grid"></div>

<script>
const drop = document.getElementById('drop-zone');
const input = document.getElementById('file-input');
const status = document.getElementById('status');
const grid = document.getElementById('grid');

drop.addEventListener('dragover', e => { e.preventDefault(); drop.classList.add('drag-over'); });
drop.addEventListener('dragleave', () => drop.classList.remove('drag-over'));
drop.addEventListener('drop', e => { e.preventDefault(); drop.classList.remove('drag-over'); handle(e.dataTransfer.files); });
input.addEventListener('change', () => handle(input.files));

function handle(files) {
  if (!files.length) return;
  status.className = '';
  status.textContent = `Обрабатываю ${files.length} фото...`;
  grid.innerHTML = '';

  const previews = {};
  Array.from(files).forEach(f => {
    const url = URL.createObjectURL(f);
    previews[f.name] = url;
  });

  const fd = new FormData();
  Array.from(files).forEach(f => fd.append('files', f, f.name));

  fetch('/rank', { method: 'POST', body: fd })
    .then(r => r.json())
    .then(data => {
      status.textContent = `Готово: ${data.count} фото отсортировано`;
      data.photos.forEach(p => {
        const pct = Math.round(p.score * 100);
        const medals = { 1: 'medal-1', 2: 'medal-2', 3: 'medal-3' };
        const medal = medals[p.rank] || '';
        const card = document.createElement('div');
        card.className = 'card ' + medal;
        card.innerHTML = \`
          <img src="\${previews[p.filename] || ''}" alt="\${p.filename}" loading="lazy">
          <div class="card-body">
            <div class="rank">#<b>\${p.rank}</b></div>
            <div class="filename">\${p.filename}</div>
            <div class="bar-wrap"><div class="bar" style="width:\${pct}%"></div></div>
            <div class="score-row"><span>Итог</span><span>\${pct}%</span></div>
            <div class="score-row"><span>Резкость</span><span>\${p.sharpness}</span></div>
            <div class="score-row"><span>Экспозиция</span><span>\${Math.round(p.exposure*100)}%</span></div>
            <div class="score-row"><span>Лица</span><span>\${Math.round(p.faces*100)}%</span></div>
            <div class="score-row"><span>Композиция</span><span>\${Math.round(p.composition*100)}%</span></div>
          </div>
        \`;
        grid.appendChild(card);
      });
    })
    .catch(err => {
      status.className = 'error';
      status.textContent = 'Ошибка: ' + err.message;
    });
}
</script>
</body>
</html>"""

face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")


def decode_image(data: bytes) -> np.ndarray:
    arr = np.frombuffer(data, np.uint8)
    return cv2.imdecode(arr, cv2.IMREAD_COLOR)


def sharpness(img: np.ndarray) -> float:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(gray, cv2.CV_64F).var())


def exposure(img: np.ndarray) -> float:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    hist = cv2.calcHist([gray], [0], None, [256], [0, 256]).flatten()
    hist = hist / hist.sum()
    underexposed = hist[:50].sum()
    overexposed = hist[220:].sum()
    return float(1.0 - underexposed - overexposed)


def face_score(img: np.ndarray) -> float:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    faces = face_cascade.detectMultiScale(gray, 1.1, 4, minSize=(30, 30))
    if len(faces) == 0:
        return 0.0
    score = 0.0
    for (x, y, w, h) in faces:
        roi = gray[y:y+h, x:x+w]
        eyes = eye_cascade.detectMultiScale(roi, 1.1, 3)
        score += 1.0 + (0.5 if len(eyes) >= 2 else 0.0)
    return min(score / len(faces), 2.0) / 2.0


def composition(img: np.ndarray) -> float:
    gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    edges = cv2.Canny(gray, 50, 150)
    h, w = edges.shape
    third_h, third_w = h // 3, w // 3
    thirds_mask = np.zeros_like(edges)
    for i in [1, 2]:
        thirds_mask[i * third_h - 10:i * third_h + 10, :] = 255
        thirds_mask[:, i * third_w - 10:i * third_w + 10] = 255
    intersection = cv2.bitwise_and(edges, thirds_mask)
    total_edges = edges.sum()
    if total_edges == 0:
        return 0.0
    return float(intersection.sum() / total_edges)


def score_image(data: bytes) -> dict:
    img = decode_image(data)
    if img is None:
        return None
    s = sharpness(img)
    e = exposure(img)
    f = face_score(img)
    c = composition(img)
    sharp_norm = min(s / 500.0, 1.0)
    total = 0.4 * sharp_norm + 0.3 * e + 0.2 * f + 0.1 * c
    return {
        "sharpness": round(s, 2),
        "exposure": round(e, 4),
        "faces": round(f, 4),
        "composition": round(c, 4),
        "score": round(total, 4),
    }


@app.get("/", response_class=HTMLResponse)
def root():
    return HTML


@app.post("/rank")
async def rank_photos(files: List[UploadFile] = File(...)):
    results = []
    for f in files:
        data = await f.read()
        metrics = score_image(data)
        if metrics is None:
            continue
        results.append({"filename": f.filename, **metrics})
    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
    return JSONResponse(content={"count": len(results), "photos": results})


@app.post("/rank-base64")
async def rank_base64(payload: dict):
    photos = payload.get("photos", [])
    results = []
    for item in photos:
        name = item.get("filename", "unknown")
        b64 = item.get("data", "")
        try:
            data = base64.b64decode(b64)
        except Exception:
            continue
        metrics = score_image(data)
        if metrics is None:
            continue
        results.append({"filename": name, **metrics})
    results.sort(key=lambda x: x["score"], reverse=True)
    for i, r in enumerate(results):
        r["rank"] = i + 1
    return JSONResponse(content={"count": len(results), "photos": results})
