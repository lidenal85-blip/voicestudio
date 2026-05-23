# Деплой на Railway

## 1. Подготовка репозитория

```bash
cd /storage/emulated/0/Workstation/KWORK/voice_studio_mvp_full
git init
git add .
git commit -m "Voice Studio MVP"
```

Создай репозиторий на GitHub и запушь:
```bash
git remote add origin https://github.com/ВАШ_ЛОГИН/voice-studio.git
git push -u origin main
```

## 2. Деплой на Railway

1. Открой **railway.app** → **New Project** → **Deploy from GitHub repo**
2. Выбери свой репозиторий
3. Railway автоматически определит Python и запустит через nixpacks

## 3. Переменные окружения

В Railway → твой проект → **Variables** добавь:

| Переменная | Значение | Обязательно |
|-----------|---------|-------------|
| `COLAB_SECRET` | твой секрет (≥8 символов) | ✅ |
| `MAX_FILE_SIZE_MB` | `200` | нет |
| `DEBUG` | `false` | нет |

`PORT` Railway проставляет сам.
`RAILWAY_PUBLIC_DOMAIN` Railway проставляет сам — сервер читает его автоматически.

## 4. Получить URL

После деплоя Railway → **Settings** → **Networking** → **Generate Domain**.
Скопируй URL вида `https://voice-studio-production.up.railway.app`.

## 5. Colab

В ноутбуке:
```python
STUDIO_URL   = "https://voice-studio-production.up.railway.app"
COLAB_SECRET = "твой секрет"
```

## Что изменилось vs Termux

| | Termux | Railway |
|--|--------|---------|
| Туннель | cloudflared (нестабильно) | Не нужен |
| 524 timeout | Есть | Нет |
| URL | Меняется при каждом запуске | Постоянный |
| Файлы | Сохраняются всегда | Сбрасываются при редеплое |
| БД | Сохраняется всегда | Сбрасывается при редеплое |
| Цена | Бесплатно | $5/мес кредит |

## Важно: файлы сбрасываются при редеплое

Railway даёт ephemeral disk. Это значит:
- При каждом `git push` и редеплое — `storage/` и `voice_studio.db` удалятся
- Для постоянного хранения нужен **Railway Volume** (~$0.25/GB/мес)
- Для MVP это нормально: загрузил трек → обработал → скачал → проект не нужен

Если хочешь постоянное хранение:
Railway → твой сервис → **Volumes** → Mount Path: `/data`
Затем в Variables: `STORAGE_PATH=/data/storage` и `DB_PATH=/data/voice_studio.db`
