# TASK_02 — Инфраструктура форка, профилирование и benchmark-пакет

> Фаза: **план выполнения** (не отчёт). Отчёт будет в `TASK_02_REPORT.md` по завершении.
> Источник требований: `TASK_01_DECISIONS.md` раздел «TASK_02».
> Сложность: **S–M**.

---

## 0. Цель задачи

TASK_02 подготавливает почву для TASK_03 (архитектурный сдвиг). Конкретно:

1. **Зафиксировать версионную базу форка.** Нам нужен чистый ответ: на каком именно коммите upstream стоит наш `docling 2.88.0` и `docling-serve 1.16.1`. Это будет точка, к которой привязан первый `[fork]`-коммит.
2. **Создать инфраструктуру синхронизации с upstream** — ветки `upstream-tracking`, журналы изменений `CHANGELOG_FORK.md` — чтобы каждый последующий патч легко ревьюился и rebase не превращался в археологию.
3. **Подтвердить диагноз из TASK_01 эмпирически.** TASK_01_REPORT основан на чтении кода; TASK_02 — на профиле живого процесса. Если профиль покажет, что время тратится не там, где мы думаем, — TASK_03 переформулируется до реализации, а не в середине.
4. **Создать воспроизводимый benchmark-пакет.** Без него сравнение «до/после» в TASK_03 будет на глаз. Нам нужна утилита, которая запускает фиксированный набор документов, собирает метрики и пишет JSON для diff.
5. **Снять baseline.** Числа текущей конфигурации (время, пики параллелизма к VLM, попадание в prefix cache SGLang, размер markdown). В TASK_03 будем сравнивать с ними.

**Никакого кода ядра (`docling`, `docling-serve`) в TASK_02 не пишем.** Вся работа — инфраструктура, инструменты, документация.

---

## 1. Что входит в задачу и что не входит

**Входит:**
- Ветка `upstream-tracking` в `docling` и `docling-serve` (мгновенная команда, без merge — просто точка привязки)
- `CHANGELOG_FORK.md` в обоих репо
- Уточнение версионной базы (git tag vs HEAD на момент форка)
- Docker-сборка `docling-serve` локально, идентичная prod (образ + env)
- Профиль `py-spy record` на эталонном `Поражай.pdf`
- Каталог `benchmarks/` в `docling-serve`: `fixtures/`, `results/`, `profiles/`, `README.md`, `run.py`, `compare.py`
- Baseline-прогон с записью результатов в `benchmarks/results/baseline.json`

**НЕ входит:**
- Никаких изменений кода в `docling` или `docling-serve` (только доки и инфраструктура)
- Никаких изменений на production-сервере tvr-srv-ai
- Форк `docling-jobkit` (решение Q4 — остаёмся на sed-патче пока)
- Реализация OCR SDK backend (это TASK_08)
- Изменения прокси (это TASK_04b)
- Любая оптимизация parallel-то-pipeline (это TASK_03)

---

## 2. Шаги выполнения

### Шаг 1. Ветки `upstream-tracking` в обоих репо

**Назначение:** фиксация «точки входа» upstream, от которой будут отсчитываться fork-коммиты. Эта ветка не для постоянного слежения за прогрессом upstream — для этого есть remote-tracking ветки. Она для ручных еженедельных merge upstream→наш main, чтобы было однозначное место для конфликтов и ревизии [fork]-патчей.

**Действия:**

В обоих репозиториях (`docling`, `docling-serve`):

```bash
# добавить upstream remote, если его нет
git remote add upstream https://github.com/docling-project/<repo>.git  # <repo> = docling или docling-serve
git fetch upstream main

# создать локальную ветку upstream-tracking на upstream/main
git checkout -b upstream-tracking upstream/main
git push -u origin upstream-tracking

# вернуться на main
git checkout main
```

**Проверка:**
- `git branch -a` показывает `upstream-tracking` и `remotes/origin/upstream-tracking`
- `git log upstream-tracking..main` показывает все наши fork-коммиты (сейчас — ни одного кода, только docs, что ожидаемо)

**Блокер:** в текущем sandbox нет прямого доступа к `github.com/docling-project/*`. Если `git remote add upstream <url>` не работает — попробуем через `gh api` или через HTTPS-клон upstream вручную. Проверим и отметим в отчёте. Если не получится — оставим TODO «добавить upstream remote» в `CHANGELOG_FORK.md` и создадим ветку `upstream-tracking` = текущий `main` (как locked-in snapshot).

**Замечание:** ветка `upstream-tracking` — это работа **только с upstream**. Merge upstream → main делается отдельной операцией и не является частью TASK_02.

### Шаг 2. Уточнение версий (tag vs head main)

**Назначение:** ответить на Q6 из TASK_01 — это tag или head main?

**Предварительные наблюдения (из сессии):**

- В нашем форке `docling` (локально): последний коммит `33b42dd Rename docling__CLAUDE.md to CLAUDE.md`. Выше:
  - `d9bd8df Add files via upload`
  - `f5fa294 chore(readme): fix broken Apify badge (typo) (#3296)` — upstream-коммит
  - `e04e602 chore: bump version to 2.88.0 [skip ci]` — здесь зафиксирована версия
- Локальных git-tag'ов **нет** (`git tag` пуст). У upstream теги вроде `v2.88.0` существуют, но мы их не забрали при клонировании.
- Аналогично нужно проверить `docling-serve`: ожидаем `v1.16.1`.

**Действия:**

1. Зафетчить теги upstream:
   ```bash
   cd /home/user/docling
   git fetch upstream --tags
   git tag --list 'v2.88.*'
   git log v2.88.0 --oneline -1  # должен показать коммит c bump до 2.88.0
   ```
2. Сравнить коммит тега `v2.88.0` с нашим `e04e602`:
   ```bash
   git log --oneline e04e602..v2.88.0  # пусто → наш forkpoint совпадает с тегом
   ```
   Если пусто — мы стоим **на** теге `v2.88.0`. Если нет — разница покажет дрейф.
3. То же самое для `docling-serve` и тега `v1.16.1`.
4. Убедиться, что между `e04e602` (v2.88.0) и нашим текущим `HEAD` (`33b42dd`) **нет** кодовых изменений в `docling/` или `docling-serve/` — только README и CLAUDE.md. Если есть — их нужно либо обосновать, либо удалить.

**Предварительный вывод (до фетча upstream тегов):** у нас, похоже, эффективная база = `e04e602` = upstream v2.88.0 + 1 коммит фикса README + загруженный CLAUDE.md. Это чистая картинка: `HEAD = v2.88.0 + nothing meaningful`. Подтвердится или опровергнется фетчем тегов.

**Ожидаемый результат:** в `CHANGELOG_FORK.md` будет строка вида `Base: docling v2.88.0 (upstream commit <sha>)`.

**Блокер:** если `git fetch upstream` невозможен в песочнице — отметить и попросить инженера выполнить фетч на его стороне либо дать возможность сходить в GitHub через `mcp__github__` tools.

### Шаг 3. `CHANGELOG_FORK.md` в обоих репо

**Назначение:** лог наших патчей относительно upstream. Живёт в корне репо, обновляется **в том же коммите**, что и патч. Должен позволять за 30 секунд ответить на вопрос «что мы вообще поменяли относительно upstream v2.88.0».

**Формат** (общий для `docling` и `docling-serve`):

```markdown
# CHANGELOG_FORK

> Журнал изменений относительно upstream. Читай перед rebase.

## Базовая версия

- upstream: `docling v2.88.0` (коммит `e04e602`)
- дата форка: 2026-XX-XX
- причина: TASK_01, Issue #419 (parallelism barriers)

## [unreleased]

_(здесь fork-коммиты, которые ещё не вошли в синхронизацию с upstream)_

## История

_(пусто — fork только начинает жить)_
```

**Правило обновления:** каждый `[fork]`-коммит добавляет строку в секцию `[unreleased]` в том же commit, где меняется код. Формат строки: `- [fork] <тип>: <описание> (файл:строка, issue #NNN если есть)`.

**Действия для TASK_02:**
- Создать `CHANGELOG_FORK.md` в корне `docling` и `docling-serve`
- Вписать базовую версию (после шага 2)
- Закоммитить с сообщением `[fork] docs: инициализация CHANGELOG_FORK`
- В секции `[unreleased]` зафиксировать сам TASK_01 и TASK_02 (без кода) как исторические вехи:
  - `- TASK_01: анализ параллелизма (docs)`
  - `- TASK_02: инфраструктура форка + benchmark-пакет (docs/tooling)`

### Шаг 4. Локальный запуск docling-serve в Docker

**Назначение:** поднять копию prod-окружения локально, чтобы на ней можно было запустить профайлер и benchmark. Prod = `ghcr.io/docling-project/docling-serve:main`, CPU only, `UVICORN_WORKERS=2`, `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=2`.

**Важное уточнение для локального прогона:** если мы хотим воспроизвести **проблему** (волны параллелизма к VLM), нужен **реальный VLM backend**. В sandbox его нет. Поэтому вариантов два:

- **Вариант A: с реальным SGLang prod.** Локальный docling-serve в Docker на машине инженера, VLM URL = `http://10.121.3.190:4000/v1` (боевой SGLang). Это не нагружает сам docling-serve prod (он изолирован от нашего локального инстанса), но **создаёт запросы к SGLang**. Для 1 документа / 84 картинок — ~28 слотов × несколько секунд = приемлемая нагрузка. Требует согласования с инженером.
- **Вариант B: с mock-VLM.** Локальный fake-server, который принимает `/v1/chat/completions`, sleep'ит N секунд, возвращает фиксированный JSON. Тогда в профиле мы ясно увидим, где именно docling ждёт вместо того, чтобы слать следующий запрос. **Этот вариант достаточно для подтверждения гипотезы о барьерах и не требует SGLang.** Время sleep'а подбирается так, чтобы имитировать реальный latency SGLang (p50 ~5s, p95 ~15s).

**Предложение:** **использовать вариант B (mock-VLM)** для самого профиля барьеров. Вариант A — только если после патча TASK_03 нужно будет замерить реальный wall time.

**Действия (вариант B):**

1. Написать `benchmarks/mock_vlm_server.py` — FastAPI-сервис на одном файле, который:
   - Слушает `POST /v1/chat/completions`
   - `sleep(random.gauss(5, 2))` (настраиваемый), минимум 0.5s
   - Возвращает валидный OpenAI-compatible JSON с `choices[0].message.content = "<MOCK IMAGE DESCRIPTION>"`
   - Логирует timestamp каждого запроса в `mock_vlm_timeline.jsonl` — это наш фактический источник истины про «волны»
2. Поднять docling-serve через docker-compose:
   ```yaml
   services:
     docling-serve:
       image: ghcr.io/docling-project/docling-serve:main  # pin до v1.16.1 после шага 2
       environment:
         UVICORN_WORKERS: 2
         DOCLING_SERVE_ENG_LOC_NUM_WORKERS: 2
         # VLM config через прокси-style запрос (см. benchmarks/run.py)
       ports: ["5001:5001"]
     mock-vlm:
       build: benchmarks/mock_vlm_server
       ports: ["4000:4000"]
   ```
3. Убедиться, что картинки идут на `http://mock-vlm:4000/v1` — конфигурация в запросе к `/v1/convert/file`, как делает реальный `docling-proxy`.

**Блокеры окружения:**
- В текущем sandbox Docker **скорее всего недоступен** (песочница), нужно проверить (`docker version`). Если нет — этот шаг блокируется до прогона на машине инженера.
- Если Docker доступен — pulling `ghcr.io/docling-project/docling-serve:main` занимает время и трафик; попросить подтверждение перед запуском.

**Ожидаемый результат:** рабочий локальный стек `docling-serve + mock-vlm`, готовый к отправке запросов. Конфигурация зафиксирована в `benchmarks/docker-compose.yml`.

### Шаг 5. Профилирование `Поражай.pdf` через `py-spy`

**Назначение:** эмпирически подтвердить диагноз TASK_01. Ожидание: видим, что `_enrich_document` / `_annotate_images` доминирует; видим барьеры как «плато» в profile; видим корреляцию с таймлайном `mock_vlm_timeline.jsonl` («волны»).

**Инструменты:**
- `py-spy record -o profile.svg --pid <PID docling-serve worker> --duration 120` — даёт flame graph с sampled stack traces. Работает без инструментации. Требует `--cap-add=SYS_PTRACE` в docker-compose.
- Параллельно: собрать таймлайн запросов из `mock_vlm_timeline.jsonl` → нарисовать гистограмму «запросов в секунду» через `benchmarks/plot_timeline.py`.
- Дополнительно: `strace -f -e network` — не нужен, mock-vlm timeline лучше.

**Действия:**

1. Стартовать docker-compose (шаг 4)
2. В отдельном терминале: `docker exec docling-serve bash -lc 'pip install py-spy'` (если не в образе)
3. Получить PID uvicorn worker:
   ```bash
   docker exec docling-serve ps -ef | grep uvicorn
   ```
4. Отправить документ:
   ```bash
   curl -X POST http://localhost:5001/v1/convert/file \
     -F 'files=@benchmarks/fixtures/pdf_text_large/Porazhay.pdf' \
     -F 'parameters={"pipeline": "vlm", ...}' \
     > /tmp/result.json &
   REQUEST_PID=$!
   ```
5. Одновременно: `docker exec docling-serve py-spy record -o /profiles/porazhay.svg --pid <WORKER_PID> --duration 180 --subprocesses`
6. Дождаться обоих; сохранить `profile.svg` + `mock_vlm_timeline.jsonl` в `benchmarks/profiles/baseline_porazhay/`.
7. Построить `timeline.png` через `benchmarks/plot_timeline.py` (простой matplotlib).

**Что конкретно ищем в flame graph (критерии подтверждения гипотезы):**
- `_enrich_document` доминирует по времени (>60% wall clock)
- Внутри него — `PictureDescriptionApiModel._annotate_images` → `ThreadPoolExecutor.map` → `future.result`
- Между вызовами `_annotate_images` есть «дыры» — моменты, когда ни одна картинка не в полёте
- На `mock_vlm_timeline.jsonl`: «волны» — интервалы, где `requests_in_flight` колеблется между высоким значением и нулём

**Что делает результат «неожиданным» (и меняет TASK_03):**
- Если flame graph показывает доминирование `layout_model` или `table_structure` — барьеры не в picture description, а в CPU-фазе → TASK_03 меняется.
- Если нет «дыр» между `_annotate_images` — тогда барьер не про executor lifecycle, а про что-то другое (например, generator-lazy evaluation по pages).

**Артефакты** (в репо):
- `benchmarks/profiles/baseline_porazhay/profile.svg`
- `benchmarks/profiles/baseline_porazhay/mock_vlm_timeline.jsonl` (тримм до 1000 строк если большой)
- `benchmarks/profiles/baseline_porazhay/timeline.png`
- Короткая note `benchmarks/profiles/baseline_porazhay/NOTES.md` — что увидели, на сколько сходится с TASK_01

**Блокер:** вся эта секция требует работающего Docker на машине с достаточным CPU. Из текущего sandbox не выполнима.

### Шаг 6. Каталог `benchmarks/` и его структура

**Назначение:** физическое место для benchmark-пакета. Структура нарочно простая — без пакета, без тестов, без CLI-фреймворков. Один `run.py`, один `compare.py`, документы в `fixtures/`, результаты в `results/`, профили в `profiles/`.

**Структура:**

```
benchmarks/
├── README.md                   # Инструкция: как запустить, что ожидать
├── run.py                      # Прогнать весь fixtures/ через docling-serve, записать results/<timestamp>.json
├── compare.py                  # diff двух результатов (wall time, markdown size, etc.)
├── plot_timeline.py            # Визуализация mock_vlm_timeline.jsonl → timeline.png
├── docker-compose.yml          # docling-serve + mock-vlm
├── mock_vlm_server/
│   ├── main.py                 # FastAPI mock VLM
│   └── Dockerfile
├── fixtures/                   # Эталонные документы — коммитятся отдельно
│   ├── pdf_text_large/         # Поражай.pdf (207/84)
│   ├── pdf_formulas/           # trigonometria-47-52.pdf (6 стр., формулы)
│   ├── pdf_text_small/         # schet-10.pdf (1 стр., sanity)
│   ├── pdf_scan_cyrillic/      # 432674638.pdf (3 стр., скан)
│   ├── docx_with_ole/          # <TBD> DOCX с OLE formulas
│   └── README.md               # Описание каждого файла, источник, характеристики
├── results/
│   ├── baseline.json           # Текущая конфигурация (TASK_02, шаг 9)
│   └── .gitignore              # *.json кроме baseline.json и явно помеченных
├── profiles/
│   └── baseline_porazhay/      # см. шаг 5
└── configs/
    ├── prod_like.json          # UVICORN_WORKERS=2, LOC_NUM_WORKERS=2
    └── experimental.json       # для TASK_06 (4x1 vs 2x2)
```

**Основание структуры:** раздел 9.2 из `TASK_01_REPORT.md` (эталонный набор).

**Ключевые решения:**
- `fixtures/` — по **типу документа**, не по размеру. Это упрощает добавление новых кейсов.
- `results/` — JSON, `.gitignore` всё кроме `baseline.json`. Исторические прогоны не коммитим (будут тяжелеть).
- `profiles/` — SVG + timeline.jsonl + notes. Коммитим только baseline и ключевые сравнения; остальное gitignore.
- Мокового VLM и docker-compose живут **внутри** `benchmarks/`, а не в корне репо — чтобы не засорять и чтобы было понятно, что это не production-артефакты.

**Метрики, которые собираем** (в `results/<timestamp>.json`):

```json
{
  "timestamp": "2026-04-14T10:00:00Z",
  "config": {
    "image": "ghcr.io/.../docling-serve:main",
    "uvicorn_workers": 2,
    "loc_num_workers": 2,
    "vlm_backend": "mock",
    "vlm_latency_mean_s": 5.0,
    "vlm_latency_stddev_s": 2.0,
    "docling_version": "2.88.0",
    "docling_serve_version": "1.16.1"
  },
  "documents": [
    {
      "name": "Porazhay.pdf",
      "pages": 207,
      "pictures": 84,
      "wall_time_s": 780.0,
      "md_size_bytes": 123456,
      "md_sha256": "...",
      "vlm_requests": 84,
      "vlm_peak_inflight": 4,
      "vlm_mean_inflight": 1.5,
      "vlm_waves": 6,
      "errors": []
    }
  ],
  "notes": "..."
}
```

Поля `vlm_*` рассчитываются из `mock_vlm_timeline.jsonl`. `md_sha256` нужен для regression-теста в TASK_03 (проверка, что markdown существенно не изменился).

### Шаг 7. `benchmarks/run.py` — прогон набора

**Назначение:** CLI-скрипт, который запускает весь fixture-набор против поднятого docling-serve и собирает JSON метрик. Никакой магии — прямо `requests.post`, `time.perf_counter`, запись в JSON.

**Сигнатура:**

```bash
python benchmarks/run.py \
    --endpoint http://localhost:5001/v1/convert/file \
    --mock-vlm-timeline http://localhost:4000/timeline \
    --config benchmarks/configs/prod_like.json \
    --fixtures benchmarks/fixtures/ \
    --only pdf_text_large \                  # optional, прогнать только подкаталог
    --output benchmarks/results/$(date -Iseconds).json \
    --label baseline
```

**Основной алгоритм:**

1. Прочитать конфиг (endpoint, versions, параметры VLM pipeline)
2. Для каждого подкаталога `fixtures/`:
   - Для каждого файла:
     - Запомнить `t_start = perf_counter()`
     - `POST /v1/convert/file` с нужными параметрами (VLM pipeline, custom vlm endpoint, do_picture_description=true, ...)
     - Засечь `t_end = perf_counter()`
     - Сохранить `md_content`, посчитать `sha256`, размер
     - Сфетчить `mock_vlm_timeline` (HTTP GET у mock-сервера) **за этот интервал времени**
     - Посчитать `vlm_*` метрики из таймлайна
3. Записать результаты в `--output` JSON
4. Напечатать короткий summary в stdout (wall time на документ, total)

**Особенности:**
- Запуск документов — **последовательно**, не параллельно. Параллельная нагрузка — отдельное измерение для TASK_06, не входит в baseline.
- Если какой-то документ упал — записать `errors`, продолжить остальные.
- `mock-vlm` сервер должен поддерживать endpoint `GET /timeline?since=<ts>&until=<ts>` для выборки запросов в интервале — это несложно, просто фильтр по ts.

**Что НЕ делает `run.py`:**
- Не поднимает docker-compose (инженер/CI запускает отдельно)
- Не рисует графики (это `plot_timeline.py`)
- Не сравнивает с baseline (это `compare.py`)

**Параметры VLM pipeline в запросе** — нужно посмотреть, как `docling-proxy` формирует запрос в `main.py` (пока держим в голове из прошлого чтения: `pipeline=vlm`, `vlm_pipeline_options.url=<mock-vlm>/v1/chat/completions`, `model=mock`, `do_picture_description=false` потому что do_picture_description в VLM pipeline ломается, `image_export_mode=placeholder`, остальное дефолтное). **В TASK_02 скопировать в `configs/prod_like.json` тот же набор параметров, что реально использует прокси.** Это нужно для корректного baseline.

### Шаг 8. `benchmarks/compare.py` — diff двух прогонов

**Назначение:** удобный diff двух результатов — `baseline.json` vs `after_task_03.json`.

**Сигнатура:**

```bash
python benchmarks/compare.py benchmarks/results/baseline.json benchmarks/results/after_task_03.json
```

**Вывод (таблица в stdout, markdown):**

```
| Document           | wall_time baseline | wall_time after | Δ        | md_sha256 match |
| ------------------ | ------------------ | --------------- | -------- | --------------- |
| Porazhay.pdf       | 780.0s             | 180.0s          | -76.9%   | ✓               |
| trigonometria.pdf  | 18.0s              | 17.5s           | -2.8%    | ✓               |
...
```

**Дополнительно:**
- Регрессионный чек: если `md_sha256` не совпадает — **warning** (не error), посчитать `levenshtein_ratio` (через `rapidfuzz`, уже используется в docling для fuzzy match) между текстами. Если < 0.95 — error.
- Метрики `vlm_*` — отдельная таблица: `peak_inflight`, `mean_inflight`, `waves`.
- Exit code: 0 если все документы улучшились или остались в пределах ±5%; 1 если есть регрессии; 2 если произошли ошибки выполнения.

**Что НЕ делает `compare.py`:**
- Не тянет `git log` или мету — только два JSON-файла
- Не строит графики — только текстовая таблица

**Реализация:** ~100-150 строк на одном файле. Зависимости: `rapidfuzz` (опционально, для диффа markdown). Если не хотим новую зависимость — использовать `difflib.SequenceMatcher.ratio()` из stdlib.

### Шаг 9. Baseline — `benchmarks/results/baseline.json`

**Назначение:** зафиксировать числа текущей (нефорк-патченной) версии. Это **точка отсчёта для TASK_03**. Если TASK_03 не улучшит эти числа — патч отвергаем.

**Действия:**

1. Прогнать `run.py --label baseline --output benchmarks/results/baseline.json` против свежеподнятого docling-serve + mock-vlm (шаг 4).
2. Убедиться, что все 5 документов прошли без ошибок.
3. Проверить, что для `Porazhay.pdf`:
   - `wall_time_s` ≥ 600 (подтверждаем «13 минут» в mock-среде — может быть меньше, если mock-VLM быстрее реального SGLang; это нормально, главное — увидеть барьеры, не абсолютное время)
   - `vlm_peak_inflight` ≤ 4 (подтверждаем `page_batch_size=4` как потолок)
   - `vlm_waves` ≥ 3 (должны быть видны «волны»)
4. Закоммитить `baseline.json` и профиль из шага 5 в репо.
5. Записать вывод про barrier reality в `TASK_02_REPORT.md`.

**Важно:** baseline — это **snapshot для сравнения**, не прогноз prod'а. В prod с реальным SGLang времена будут другие (реальная latency, реальные тайминги SGLang scheduling, prefix cache). Но **структура волн** — барьеры между батчами — должна сохраниться, потому что это логика docling, а не VLM.

**Критерий «baseline валидный»:** если на mock-VLM с latency 5s/картинка и 4 слотами барьер виден (`vlm_waves >= 3`, `mean_inflight <= 2.5`), baseline считается пригодным для сравнения. Если волны не воспроизводятся на mock — значит mock слишком быстрый, поднимаем `vlm_latency_mean_s` до 10 и перепрогоняем.

**Артефакты в коммите:**
- `benchmarks/results/baseline.json` — числа
- `benchmarks/profiles/baseline_porazhay/*` — профиль
- `benchmarks/configs/prod_like.json` — конфиг прогона
- Заметка в `CHANGELOG_FORK.md` под `[unreleased]`: `- TASK_02: baseline зафиксирован (Porazhay: <N>s)`

---

## 3. Что нужно от инженера (блокеры окружения)

Честный список того, что я **не могу сделать из текущего sandbox** без твоей помощи или без выхода за пределы песочницы:

1. **Docker / docker-compose.** Скорее всего недоступно в песочнице. Шаги 4, 5, 9 (профилирование и baseline-прогон) блокируются до ручного выполнения в твоей локальной среде. Я могу полностью подготовить все скрипты и compose-файл, чтобы ты выполнил один `docker compose up && python benchmarks/run.py`.
2. **Доступ к upstream GitHub для fetch tags/remote.** Нужны `git fetch upstream --tags` для `docling-project/docling` и `docling-project/docling-serve`. Если прямой `git remote add upstream` не работает, попробую через MCP github tools (`mcp__github__get_tag`), либо попрошу тебя выполнить локально.
3. **Реальные эталонные документы.** `Поражай.pdf`, `trigonometria-47-52.pdf`, `schet-10.pdf`, `432674638.pdf`, DOCX с OLE — это реальные прод-файлы с твоей стороны. Их нет в sandbox. Без них я могу подготовить только структуру `fixtures/` и `README.md` с описанием того, что туда класть. См. раздел 4 ниже.
4. **Реальный SGLang.** Даже если получу Docker — реальный VLM не нужен для baseline (мокаем), но нужен для cross-проверки (A из шага 4). Это опционально и только если захотим «боевой» baseline (не нужен для TASK_03).
5. **`py-spy`.** Обычно ставится через `pip install py-spy`. На машине, где будет профиль, pip доступен — не проблема.

**Что могу сделать прямо сейчас, **без** блокеров:**
- Все файлы инфраструктуры (`CHANGELOG_FORK.md`, `benchmarks/` каркас, `run.py`, `compare.py`, `mock_vlm_server/`, `docker-compose.yml`, `configs/prod_like.json`)
- Создать ветки `upstream-tracking` (если получится fetch upstream; иначе как snapshot текущего main)
- Подтверждение версии (через `mcp__github__get_tag` в обоих репо)
- Записать всё в `TASK_02_REPORT.md` **как план с пометками «выполнить на машине инженера»** для заблокированных шагов.

---

## 4. Предложение по загрузке эталонных документов

Твой вопрос: **«в проект залить примеры документов чтобы ты на них потестировал что получается и какая скорость?»** — отвечаю.

**Да, заливай. Вот куда и как:**

### Куда класть

После того, как я закоммичу каркас `benchmarks/` (шаг 6), в репо появятся пустые подкаталоги. Твои документы кладёшь по соответствию:

| Подкаталог                          | Что туда                                           |
| ----------------------------------- | -------------------------------------------------- |
| `benchmarks/fixtures/pdf_text_large/` | `Поражай.pdf` (207/84) — **главный кейс**          |
| `benchmarks/fixtures/pdf_formulas/`   | `trigonometria-47-52.pdf` (6 стр, формулы)         |
| `benchmarks/fixtures/pdf_text_small/` | `schet-10.pdf` (1 стр, sanity check)               |
| `benchmarks/fixtures/pdf_scan_cyrillic/` | `432674638.pdf` (3 стр, скан, кириллица)        |
| `benchmarks/fixtures/docx_with_ole/`  | DOCX с OLE-формулами (когда подберёшь)             |

После заливки — обнови `benchmarks/fixtures/README.md` (или скажи мне, я обновлю), в котором для каждого файла записано: источник, количество страниц, количество картинок, что именно этот файл тестирует.

### Про анонимизацию

Если какие-то документы содержат PII / коммерческую тайну — перед коммитом:
- Либо замазать конкретные куски (имена/номера/суммы)
- Либо выбрать документ, где такой информации нет
- Либо держать их **вне git** (отдельный том, `.gitignore`), а в репо класть только `fixtures/pdf_text_large/.external` с описанием «файл живёт в /mnt/.../Porazhay.pdf на tvr-srv-ai»

Я могу работать и с первым, и с третьим вариантом — для анализа важно только, что файл где-то физически есть и его можно стабильно прогонять через `run.py`.

### **Чего я тебе честно сказать обязан — я НЕ могу измерить реальную скорость прямо сейчас**

Чтобы не было иллюзии:

1. **В текущем sandbox нет Docker.** Я не могу поднять docling-serve и вообще ничего прогнать.
2. **У меня нет доступа к SGLang на tvr-srv-aiprod.** Даже если бы был Docker — реальный VLM за пределами песочницы.
3. **Прямой доступ к файловой системе tvr-srv-ai (10.121.3.201) у меня тоже нет.**

Поэтому реальные числа (wall time, волны, параллелизм) появятся в `baseline.json` **только после того, как ты (или кто-то) выполнит `benchmarks/run.py` на машине, где это возможно**. Я могу:

- Полностью подготовить весь tooling (скрипты, compose, mock-VLM, configs)
- Проверить статически, что всё собирается и синтаксически корректно
- Дать пошаговую инструкцию «как запустить» в `benchmarks/README.md`
- Проанализировать результаты, когда ты пришлёшь `baseline.json` обратно в репо

**Предлагаемый порядок:**
1. Я сейчас коммичу каркас TASK_02 (скрипты + структура, без реальных файлов)
2. Ты заливаешь документы в `fixtures/`
3. Ты (на своей машине с Docker) запускаешь `benchmarks/run.py`, получаешь `baseline.json` + профиль `baseline_porazhay.svg`
4. Коммитишь их в репо
5. Я анализирую, фиксирую в `TASK_02_REPORT.md`, готовлю TASK_03

Если у тебя есть **другой способ** дать мне среду, в которой docker+pip+SGLang доступны — скажи, и я заменю «блокер» на прямое выполнение.

---

## 5. Открытые вопросы для согласования до начала работ

Список коротких точек, где мне нужно твоё «да/нет», прежде чем я начну или там, где я принимаю решение в одиночку:

1. **Вариант mock-VLM для baseline (не реальный SGLang).** Моё решение: **да, mock** — для подтверждения барьеров реальный VLM не нужен, а mock быстрее, повторимее и не трогает прод. Скажи, если хочешь реальный SGLang в baseline.
2. **Анонимизация документов.** Моё решение: решаешь ты. Я не вижу содержимого, буду работать с тем, что дашь.
3. **`docker-compose.yml` внутри `benchmarks/`, а не в корне.** Моё решение: **внутри `benchmarks/`**, чтобы не путать с каким-либо боевым compose'ом. OK?
4. **Формат `mock_vlm_timeline.jsonl`.** Моё решение: одна строка JSON на запрос, поля: `ts_start`, `ts_end`, `duration_s`, `request_id`. Этого достаточно для волн и peak_inflight. Нужны дополнительные поля?
5. **Уровень анонимизации метрик в `baseline.json`.** Моё решение: имена документов (`Porazhay.pdf`) — оставляем, это уже упомянуто в твоих CLAUDE.md. Если нужна анонимизация — подменяем на `doc_pdf_text_large_1`.
6. **`pin` версии в `Dockerfile` / `docker-compose.yml`.** Сейчас prod использует `:main`, что плавает. Моё предложение для benchmarks: **прибить тег** — `ghcr.io/docling-project/docling-serve:v1.16.1` (если такой тег опубликован) или конкретный sha digest. Иначе baseline поплывёт между прогонами. OK?
7. **Коммит `benchmarks/fixtures/*.pdf` в git.** Моё решение: если файлы небольшие (до ~5 МБ суммарно) — коммитим напрямую. Если больше — `git lfs` или внешнее хранилище + `.external` placeholders. Ориентировочно для `Поражай.pdf` (207 стр) — это ~20-50 МБ, уже **большой для обычного git**. Нужно твоё решение: lfs/внешнее/anyway commit.
8. **Новые зависимости в benchmarks.** `fastapi`, `uvicorn` для mock-VLM — ок (уже есть в prod-образе); `matplotlib` для `plot_timeline.py` — опционально, можно заменить на текстовый histogram. Моё предложение: использовать только `requests` + stdlib для `run.py`/`compare.py`, `matplotlib` — только в `plot_timeline.py` и помечено как optional. OK?

Если по какому-то пункту ответа не дашь — действую по моему предложенному варианту и фиксирую это в `TASK_02_REPORT.md`.

---

## 6. Критерии завершения

TASK_02 считается завершённым, когда выполнены **все** пункты ниже:

- [ ] `upstream-tracking` создан в `docling` и `docling-serve`, запушен в origin
- [ ] Версионная база зафиксирована: явное указание в `CHANGELOG_FORK.md` вида «upstream v2.88.0, commit `<sha>`»
- [ ] `CHANGELOG_FORK.md` создан в обоих репо, с секциями `## Базовая версия`, `## [unreleased]`, `## История`
- [ ] Каркас `benchmarks/` создан в `docling-serve` со всеми подкаталогами и `README.md`
- [ ] `benchmarks/run.py` работает (хотя бы с mock-VLM на `localhost`): синтаксис корректный, импорты работают, `--help` печатает usage
- [ ] `benchmarks/compare.py` работает: на двух одинаковых JSON выдаёт «all docs unchanged»
- [ ] `benchmarks/mock_vlm_server/main.py` запускается локально (если Docker доступен) и отвечает на `POST /v1/chat/completions` валидным JSON
- [ ] `benchmarks/configs/prod_like.json` содержит параметры, идентичные тем, что реально использует `docling-proxy` для VLM pipeline (скопировать из `main.py` прокси)
- [ ] Профиль `benchmarks/profiles/baseline_porazhay/` снят — **или** явно помечено «заблокировано отсутствием Docker/документа» в отчёте
- [ ] `baseline.json` снят — **или** явно помечено «заблокировано» как выше
- [ ] `TASK_02_REPORT.md` написан: выводы по профилю (или почему не снят), baseline (или почему не снят), подтверждение или опровержение гипотезы из TASK_01
- [ ] Все коммиты на main имеют префикс `[fork]` и связаны с TASK_02
- [ ] Пушнуты в origin/main оба репо
- [ ] Сообщение инженеру: «TASK_02 завершён, ожидаю обратной связи» — с перечнем того, что сделано и что заблокировано

**Если какие-то пункты не сделаны из-за блокеров окружения** — они не отменяются, а фиксируются в отчёте с точной формулировкой «требует выполнения на машине с Docker». Считать TASK_02 завершённым без прогона baseline **можно**, но явно помеченным как «инфраструктура готова, прогон на стороне инженера».
