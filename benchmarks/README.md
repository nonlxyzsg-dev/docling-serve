# benchmarks — воспроизводимые замеры docling-serve

> Инструментарий для TASK_02 и далее. Цель — измерить baseline текущего поведения (барьеры параллелизма к VLM) и сравнивать с результатами после fork-патчей.

---

## Что здесь

```
benchmarks/
├── README.md              # этот файл
├── run.py                 # прогнать весь fixtures/ через docling-serve → results/<label>.json
├── compare.py             # сравнить два results/*.json
├── plot_timeline.py       # нарисовать timeline.png из mock_vlm_timeline.jsonl
├── docker-compose.yml     # docling-serve + mock-vlm для локального прогона
├── mock_vlm_server/       # лёгкий FastAPI-мок OpenAI-совместимого VLM API
│   ├── main.py
│   ├── Dockerfile
│   └── requirements.txt
├── configs/
│   ├── prod_like.json     # UVICORN_WORKERS=2, LOC_NUM_WORKERS=2 (текущий prod)
│   └── experimental.json  # для TASK_06 (4×1 vs 2×2)
├── fixtures/              # эталонные документы — см. fixtures/README.md
│   ├── pdf_text_large/    # Porazhay.pdf (207 стр / 84 картинки) — главный кейс
│   ├── pdf_formulas/      # trigonometria-47-52.pdf (6 стр, формулы)
│   ├── pdf_text_small/    # schet-10.pdf (1 стр, sanity)
│   ├── pdf_scan_cyrillic/ # 432674638.pdf (3 стр, скан)
│   └── docx_with_ole/     # DOCX с OLE-формулами
├── results/
│   └── baseline.json      # текущая конфигурация без fork-патчей
└── profiles/
    └── baseline_porazhay/ # py-spy профиль + mock_vlm_timeline
```

## Короткий рецепт запуска

Требуется: Docker, Python 3.11+, доступ к `ghcr.io/docling-project/docling-serve:main`.

```bash
# 1. Поднять mock-VLM + docling-serve
cd benchmarks
docker compose up -d

# 2. Дождаться готовности
curl -s http://localhost:5001/health
curl -s http://localhost:4000/health

# 3. Прогнать baseline
python run.py \
    --endpoint http://localhost:5001/v1/convert/file \
    --mock-vlm-timeline http://localhost:4000/timeline \
    --config configs/prod_like.json \
    --fixtures fixtures/ \
    --output results/baseline.json \
    --label baseline

# 4. Посмотреть сводку
python compare.py results/baseline.json results/baseline.json  # против самого себя → "all unchanged"
```

## Как работает mock-VLM

`mock_vlm_server/main.py` — FastAPI-сервис, имитирующий OpenAI-совместимый `POST /v1/chat/completions`. Он:

- Принимает запрос, логирует `ts_start`
- Спит `random.gauss(mean, stddev)` секунд (настраивается через env `MOCK_VLM_LATENCY_MEAN_S`, `MOCK_VLM_LATENCY_STDDEV_S`)
- Возвращает валидный JSON c `choices[0].message.content = "<MOCK IMAGE DESCRIPTION>"`
- Пишет в `/data/mock_vlm_timeline.jsonl` одну строку на запрос
- Endpoint `GET /timeline?since=<ts>&until=<ts>` — выгрузка таймлайна

**Зачем мок, а не реальный SGLang:** для подтверждения барьеров нужны воспроизводимые тайминги и таймлайн запросов. Реальный SGLang добавляет переменную latency (prefix cache, очередь GPU, другие клиенты) — в нём невозможно сказать, «волны» из-за docling или из-за backend'а. Мок убирает эту переменную.

## Метрики, которые `run.py` собирает

На каждый документ:

| Поле                 | Что                                                      |
| -------------------- | -------------------------------------------------------- |
| `wall_time_s`        | wall clock от первой строчки запроса до получения ответа |
| `md_size_bytes`      | размер получившегося markdown                            |
| `md_sha256`          | хэш — для regression-теста в TASK_03                     |
| `vlm_requests`       | сколько запросов пришло на mock-VLM за этот интервал     |
| `vlm_peak_inflight`  | пиковое количество одновременных запросов                |
| `vlm_mean_inflight`  | среднее количество одновременных запросов                |
| `vlm_waves`          | число «волн» (интервалы, где inflight падал до 0)        |

«Волна» определяется так: сегмент активности, начинающийся с первого не-нулевого inflight и заканчивающийся, когда inflight падает до 0 на ≥ 1 секунду. Пустые интервалы между ними — простои.

## Как читать результаты

- **Baseline должен показать волны** (`vlm_waves >= 3` на `Porazhay.pdf`). Если нет — baseline не валиден, нужно увеличить `MOCK_VLM_LATENCY_MEAN_S` и перепрогнать.
- **Peak inflight на baseline ожидаемо <= 4** — это потолок `page_batch_size`. После TASK_03 должно вырасти до сотен/десятков (в зависимости от `max_concurrent_in_flight`).
- **Markdown SHA256 между baseline и post-TASK_03** — должен быть **одинаковым** для документов, где логика recognition не менялась. Если отличается — `compare.py` выведет fuzzy match score.

## Чего здесь нет (намеренно)

- Не используется pytest / unittest — это не unit-тесты, это измерения. Воспроизводимость — через явные шаги в `docker-compose.yml`/`run.py`, не через CI.
- Нет сравнения с реальным SGLang. Это отдельная активность (optional cross-check).
- Нет нагрузочного теста с параллельными документами — это TASK_06.

## Открытые вопросы

См. `TASKS/TASK_02_infrastructure.md` раздел «Открытые вопросы».
