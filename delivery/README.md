# AI Voice Studio MVP

Бэкенд-сервис для разделения аудио на **вокал** и **инструментал**.  
Работает на телефоне (Termux). Принимает файлы, ставит задачи Colab-воркеру, принимает результаты обратно.

---

## Стек

| Слой | Технология |
|------|-----------|
| Framework | FastAPI + Uvicorn |
| БД | SQLite + aiosqlite + SQLAlchemy 2.x |
| Хранилище | Локальная FS (замена S3) |
| Аудио | FFmpeg (конвертация + длительность) |
| UI | Jinja2-шаблоны (тёмная тема) |
| Конфиг | pydantic-settings + .env |

---

## Структура проекта

```
voice_studio_mvp/
├── main.py                  # точка входа
├── requirements.txt
├── .env                     # из .env.example
├── config/
│   └── settings.py          # все настройки
├── db/
│   ├── models.py            # Project, AudioAsset, Job
│   └── init_db.py           # init + get_db dependency
├── services/
│   ├── storage.py           # LocalStorageAdapter
│   └── converter.py         # FFmpeg-обёртка
├── api/
│   └── routers.py           # 5 API-эндпоинтов
├── web/
│   ├── router.py            # HTML-роутер (Jinja2)
│   └── templates/
│       ├── base.html
│       ├── index.html
│       ├── project.html
│       └── upload.html
├── storage/                 # создаётся автоматически
└── logs/                    # создаётся автоматически
```

---

## Установка (Termux)

```bash
pkg install python ffmpeg
pip install -r requirements.txt
cp .env.example .env
# Заполнить .env (минимум — COLAB_SECRET)
python main.py
```

---

## .env (минимальный)

```env
COLAB_SECRET=your_secret_here
MAX_FILE_SIZE_MB=100
PORT=8000
```

---

## API-эндпоинты

### Загрузка трека
```
POST /api/upload
Content-Type: multipart/form-data
Body: file=<audio file>

Response: { project_id, job_id, status }
```

### Получить pending-задания (воркер)
```
GET /api/jobs/pending?secret=COLAB_SECRET

Response: [{ job_id, project_id, type, input_params }]
```

### Взять задание в работу (воркер)
```
POST /api/jobs/{job_id}/claim?secret=COLAB_SECRET

Response: { job_id, status: "processing", download_url }
```

### Сдать результат (воркер)
```
POST /api/jobs/{job_id}/complete?secret=COLAB_SECRET
Content-Type: multipart/form-data
Body: vocal_file=<wav>, instrumental_file=<wav>

Response: { status: "ok" }
```

### Скачать ассет
```
GET /api/projects/{project_id}/assets/{type}
type: original | vocal | instrumental

Response: FileResponse (audio/wav)
```

### Удалить проект
```
DELETE /api/projects/{project_id}

Response: { status: "deleted" }
```

---

## Web UI

| URL | Страница |
|-----|---------|
| `/` | Список всех проектов |
| `/upload` | Форма загрузки трека |
| `/projects/{id}` | Плеер + скачивание файлов |

---

## Colab-воркер — схема работы

```
1. GET /api/jobs/pending?secret=...     → получить задания
2. POST /api/jobs/{id}/claim?secret=... → взять в работу + получить download_url
3. Скачать original.wav по download_url
4. Запустить Demucs/Spleeter
5. POST /api/jobs/{id}/complete?secret=... → отдать vocal + instrumental
```

---

## Статусы

**Project.status:** `created` → `processing` → `ready` / `failed`  
**Project.pipeline_state:** `uploaded` → `separating` → `ready`  
**Job.status:** `pending` → `processing` → `done` / `failed`

---

## Чеклист

- [x] `uvicorn main:app` стартует на `:8000`
- [x] `POST /api/upload` — файл сохранён, Job создан
- [x] `GET /api/jobs/pending` — возвращает pending jobs
- [x] `POST /api/jobs/{id}/complete` — файлы сохранены, `Project.status=ready`
- [x] `GET /api/projects/{id}/assets/vocal` — FileResponse
- [x] `.env.example` создан
- [x] `README.md` в delivery/
