# TASK_02 — Отчёт

> Фаза: **отчёт**. План в `TASK_02_infrastructure.md`.
> Статус: **частично завершено.** Инфраструктура готова; профилирование и baseline-прогон заблокированы отсутствием Docker-демона и реальных эталонных документов в песочнице Claude. См. раздел 4.

---

## 1. Что сделано

**Инфраструктура форка:**

- Создана ветка `upstream-tracking` в `docling` и `docling-serve`, привязанная к upstream/main, запушена в origin
- Добавлен remote `upstream = https://github.com/docling-project/<repo>.git` в обоих репо
- Создан `CHANGELOG_FORK.md` в обоих репо с базовой версией и начальной записью
- `TASK_02_infrastructure.md` (план задачи) — закоммичен, обсуждён в нём каждый шаг

**Версии зафиксированы:**

- `docling`: upstream/main = `f5fa294` = `v2.88.0` + 1 косметический upstream-коммит (README badge). Ни одного кодового изменения в нашем форке поверх этого. **Чистая база.**
- `docling-serve`: upstream/main = `683eeca feat: Move client SDK to docling (#575)` = `v1.16.1` + **1 значимый upstream-коммит** (вынос SDK, `policy.py`, правки `app.py`, `datamodel/*`). Не тегированная точка.

**Benchmark-пакет (полностью готов, синтаксически проверен, прогон заблокирован):**

- `benchmarks/README.md` — инструкция + описание метрик
- `benchmarks/mock_vlm_server/` — FastAPI-мок OpenAI-совместимого VLM (`main.py`, `Dockerfile`, `requirements.txt`). Сервис пишет `/data/mock_vlm_timeline.jsonl` с полями `{request_id, ts_start, ts_end, duration_s}` и экспортирует `GET /timeline?since=&until=` для выборки.
- `benchmarks/run.py` — CLI-прогон (`--endpoint`, `--config`, `--fixtures`, `--output`, `--label`, `--only`, `--mock-vlm-timeline`, `--mock-vlm-reset`). По каждому документу собирает `wall_time_s`, `md_size_bytes`, `md_sha256`, `vlm_requests`, `vlm_peak_inflight`, `vlm_mean_inflight`, `vlm_waves`, `vlm_total_active_s`, `vlm_total_idle_s`. Запускает документы последовательно. Ошибки каждого документа фиксируются в поле `errors[]`, не прерывая прогон.
- `benchmarks/compare.py` — diff двух result-JSON. Печатает таблицу wall time + таблицу VLM-параллелизма в markdown. Детектирует регрессии > 5 % wall time и дрейф размера markdown > 5 %. Exit-коды: 0 / 1 / 2.
- `benchmarks/plot_timeline.py` — визуализация `mock_vlm_timeline.jsonl`. По умолчанию — ASCII histogram в stdout; опционально PNG через matplotlib.
- `benchmarks/docker-compose.yml` — `docling-serve` (из `ghcr.io/docling-project/docling-serve:main`, `UVICORN_WORKERS=2`, `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=2`, `cap_add: SYS_PTRACE` для py-spy) + `mock-vlm` (build'ится из `mock_vlm_server/`). Образ docling-serve параметризуется через `${DOCLING_SERVE_IMAGE}`, латентность мока — через `${MOCK_VLM_LATENCY_*}`.
- `benchmarks/configs/prod_like.json` — параметры конверсии, **идентичные тем, что инжектит `docling-proxy` для text-PDF**: `pipeline=standard`, `do_ocr=false`, `image_export_mode=placeholder`, `do_picture_description=true`, `do_picture_description_custom=false`, `do_picture_classification=false` + полный JSON `picture_description_api` с `concurrency=8`.
- `benchmarks/configs/experimental.json` — для TASK_06, `UVICORN_WORKERS=4`, `LOC_NUM_WORKERS=1`, остальное идентично.
- `benchmarks/fixtures/` — пять подкаталогов по типам документов с `README.md` и `.gitkeep`: `pdf_text_large/`, `pdf_formulas/`, `pdf_text_small/`, `pdf_scan_cyrillic/`, `docx_with_ole/`.
- `benchmarks/results/.gitignore` — по умолчанию все JSON вне git; whitelisted: `baseline.json`, `after_task_03.json`, `after_task_06.json`.
- `benchmarks/profiles/.gitignore` — игнорируем «сырой» `mock_vlm_timeline.jsonl` в корне, но оставляем именованные подкаталоги типа `baseline_porazhay/`.

**Синтаксический smoke-test:**
- `python3 -m py_compile` прошёл на всех скриптах без предупреждений
- `run.py --help`, `compare.py --help`, `plot_timeline.py --help` печатают корректный usage
- Юнит-проверка `compute_vlm_metrics()` на синтетических данных с двумя волнами: детекция `vlm_waves=2` корректна, активное/idle время совпадает с ожиданием
- `compare.py` прогнан на двух dummy-JSON: выдаёт корректную markdown-таблицу и exit code 0, когда прирост wall time отрицательный

---

## 2. Версионная база форка — выводы

Ответ на Q6 из TASK_01_DECISIONS.md: **tagged releases или head main?** — ответ получен, картинка разная для двух репозиториев.

### `docling` — чистая база

| Ссылка                          | Коммит      | Комментарий                                                       |
| ------------------------------- | ----------- | ----------------------------------------------------------------- |
| upstream/main                   | `f5fa294`   | сверху v2.88.0, правка badge в README                             |
| наш `main` (текущая точка)      | `33b42dd`   | `f5fa294` + `Add files via upload` + `Rename docling__CLAUDE.md`  |
| `v2.88.0` tag                   | `e04e602`   | за 1 коммит до upstream/main (только readme фикс)                 |

**Вывод:** эффективная база = `v2.88.0` + README-косметика upstream. **Ни одного кодового изменения** в нашем форке поверх этого. Rebase тривиален.

**Решение (принял сам):** базу оставляем на текущей точке (upstream/main на 2026-04-14). Откатываться до `v2.88.0` строго не требуется, потому что «дельта» — не код. Зафиксировано в `docling/CHANGELOG_FORK.md`.

### `docling-serve` — неочевидно, нужно решение

| Ссылка                          | Коммит      | Комментарий                                                                                     |
| ------------------------------- | ----------- | ----------------------------------------------------------------------------------------------- |
| upstream/main                   | `683eeca`   | `v1.16.1` + 1 коммит `feat: Move client SDK to docling (#575)`                                  |
| наш `main` (текущая точка)      | `1649966`   | `683eeca` + наши TASK_01/TASK_02 docs + benchmarks (**без изменений рабочего кода**)            |
| `v1.16.1` tag                   | `12a7943`   | за 1 коммит до upstream/main                                                                    |

Дельта upstream/main − v1.16.1 — **значимая**: `683eeca` трогает `docling_serve/app.py` (−89 строк), добавляет `docling_serve/policy.py` (+169 строк), переписывает `docling_serve/datamodel/{convert,requests,responses}.py`, удаляет весь `docling-serve-client/` (−10 000 строк). Это не bump — это вынос SDK и добавление policy-слоя. **По сути pre-v1.17.0-dev.**

**Открытый вопрос для инженера (выбор между A и B):**

- **Вариант A (принят по умолчанию в TASK_02):** база = `683eeca` (upstream/main на 2026-04-14). Плюс: меньше будет дельта при следующем rebase upstream → наш main. Минус: база не воспроизводится через `pip install docling-serve==1.16.1` — это «snapshot in time».
- **Вариант B:** rollback до `v1.16.1` (=`12a7943`). Откатываем `683eeca`, теряем `policy.py` и рефактор datamodel/app. Плюс: чистый тег, воспроизводимо. Минус: при следующем rebase придётся повторно принимать `683eeca` как upstream-изменение, адаптировать наши fork-патчи под уже изменённый `app.py`/`datamodel/`, плюс проверять, что `policy.py` не меняет семантику нужного нам пайплайна.

**Моя рекомендация:** **Вариант A**. Причины:
1. Rollback `683eeca` — это 10 000+ строк удаления клиента и переписывание 4 файлов политики. Это не «откат», это мини-форк.
2. Наши предстоящие fork-патчи (TASK_03) трогают модели `docling`, а не `docling-serve`. Коммит `683eeca` их не затрагивает.
3. Следующий rebase всё равно будет с upstream/main, не с релизом — `683eeca` уже учтён.
4. `v1.16.1` всё равно ожидается в ближайшем теге (`v1.17.0` предположительно), а стоять на нём ≤ недели — приемлемо.

**Зафиксировано:** в `docling-serve/CHANGELOG_FORK.md` под «Базовая версия» открытый вопрос описан явно; до решения инженера TASK_02/03 идут на варианте A.

### Pin для сборки

В `benchmarks/docker-compose.yml` образ docling-serve прибит через переменную окружения:
```
image: ${DOCLING_SERVE_IMAGE:-ghcr.io/docling-project/docling-serve:main}
```
По умолчанию — `:main` (как в prod). Для воспроизводимости baseline рекомендую прогонять с явным digest: `DOCLING_SERVE_IMAGE=ghcr.io/...@sha256:... docker compose up`. Инженер сам определит digest на машине прогона.

---

## 3. Что лежит в `benchmarks/` (карта артефактов)

```
benchmarks/
├── README.md                     # Инструкция и описание метрик
├── docker-compose.yml            # docling-serve + mock-vlm, pin через DOCLING_SERVE_IMAGE
├── run.py                        # CLI-прогон → results/*.json
├── compare.py                    # diff двух прогонов + детект регрессий
├── plot_timeline.py              # ASCII/PNG визуализация mock_vlm_timeline.jsonl
├── mock_vlm_server/
│   ├── main.py                   # FastAPI-мок OpenAI chat/completions
│   ├── Dockerfile                # python:3.11-slim + fastapi + uvicorn
│   └── requirements.txt
├── configs/
│   ├── prod_like.json            # идентично тому, что инжектит docling-proxy
│   └── experimental.json         # UVICORN_WORKERS=4, LOC_NUM_WORKERS=1 (для TASK_06)
├── fixtures/
│   ├── README.md                 # Карта: какой файл куда класть
│   ├── pdf_text_large/.gitkeep   # Porazhay.pdf — главный кейс
│   ├── pdf_formulas/.gitkeep     # trigonometria-47-52.pdf
│   ├── pdf_text_small/.gitkeep   # schet-10.pdf
│   ├── pdf_scan_cyrillic/.gitkeep # 432674638.pdf
│   └── docx_with_ole/.gitkeep    # TBD
├── results/
│   └── .gitignore                # *.json игнор, whitelisted: baseline, after_task_03, after_task_06
└── profiles/
    └── .gitignore                # игнорируем сырой mock_vlm_timeline.jsonl в корне
```

**Поток данных (как ожидается на прогоне):**

```
docker compose up
  ├─ docling-serve на :5001 (prod-like)
  └─ mock-vlm на :4000, пишет ./profiles/mock_vlm_timeline.jsonl

python run.py --endpoint http://localhost:5001/v1/convert/file \
              --mock-vlm-timeline http://localhost:4000/timeline \
              --mock-vlm-reset http://localhost:4000/timeline/reset \
              --config configs/prod_like.json \
              --fixtures fixtures/ \
              --output results/baseline.json --label baseline

  1. POST /timeline/reset → пустой mock_vlm_timeline.jsonl
  2. Для каждого файла в fixtures/:
     - build form с picture_description_api cfg из configs/prod_like.json
     - POST /v1/convert/file → docling-serve
     - докинг конвертирует, дёргает http://mock-vlm:4000/v1/chat/completions
       для каждой картинки
     - mock-vlm пишет запись в timeline
     - после ответа docling-serve: GET /timeline?since=&until= → список записей
     - compute_vlm_metrics(entries) → {peak, mean, waves, active, idle}
  3. results/baseline.json с полным summary
```

**Как проверить, что baseline валиден (на прогоне):**
1. На `Porazhay.pdf`: `vlm_waves >= 3`, `vlm_peak_inflight <= 4`, `vlm_mean_inflight <= 2.5`. Если не так — поднять `MOCK_VLM_LATENCY_MEAN_S` до 10 и перепрогнать.
2. На `schet-10.pdf`: `wall_time_s <= 30`. Если больше — что-то с мокингом или конфигом.
3. На всех документах: `errors == []`.

---

## 4. Заблокированные шаги и почему

**Проверено в песочнице Claude:**

1. **Docker-клиент установлен** (`docker version` → 29.3.1), но **docker daemon недоступен**: `docker ps` → `failed to connect to the docker API at unix:///var/run/docker.sock`. Песочница не предоставляет docker daemon.
2. **Python 3.11** доступен, `requests` не установлен по умолчанию (не проблема — скрипты проходят синтаксический контроль и `--help`).
3. **Прямой git-доступ к upstream GitHub работает** — `git fetch upstream --tags` прошёл для обоих репо, теги и история получены. MCP-инструменты GitHub тоже доступны (restricted scope).
4. **Эталонных реальных документов нет** — `Porazhay.pdf`, `trigonometria-47-52.pdf`, `schet-10.pdf`, `432674638.pdf`, DOCX с OLE — это продовые файлы на `tvr-srv-ai`, в песочнице их нет.
5. **Прямого сетевого доступа к prod SGLang (`10.121.3.190:4000`)** не имею — это внутренняя сеть корпоративного кластера.

**Из-за этого НЕ выполнены шаги 4, 5, 9 из плана TASK_02_infrastructure.md:**

| Шаг                                                 | Статус        | Причина                                           |
| --------------------------------------------------- | ------------- | ------------------------------------------------- |
| 4. Локальный запуск docling-serve в Docker          | **blocked**   | нет docker daemon                                 |
| 5. Профилирование `Porazhay.pdf` через py-spy       | **blocked**   | нет docker + нет документа                        |
| 9. Baseline-прогон в `results/baseline.json`        | **blocked**   | нет docker + нет документов                       |

Всё остальное (шаги 1, 2, 3, 6, 7, 8 — инфраструктура и код инструментов) **выполнено** и проверено статически.

**Что это значит для TASK_03:**
- Tooling для бенчмарков готов **полностью**. Инженер может запустить `docker compose up && python run.py ...` на своей машине и получить `baseline.json` без моей дальнейшей помощи.
- **Эмпирическое подтверждение диагноза барьеров (Q1 из TASK_01) пока не получено.** По коду — подтверждено в TASK_01_REPORT.md с точностью до строки. По профилю — будет после прогона на машине инженера.
- **Моя рекомендация:** можно начинать TASK_03 **параллельно** с прогоном baseline. Если baseline пойдёт не так, как ожидается — притормозим и переосмыслим, но шансов на сюрприз мало: код-анализ в TASK_01 был разобран по косточкам.

---

## 5. Что нужно от инженера для завершения TASK_02

Чёткий короткий список действий, которые блокируют TASK_02 до конца и могут быть сделаны только на твоей стороне:

1. **Залей реальные документы в `benchmarks/fixtures/`** (см. раздел 6 ниже).
2. **На машине с Docker** (твой dev или CI runner):
   ```bash
   cd benchmarks
   # опционально: запинить конкретный digest docling-serve
   # export DOCLING_SERVE_IMAGE=ghcr.io/docling-project/docling-serve@sha256:...
   docker compose up -d
   # подождать пока healthcheck позеленеет
   python run.py \
       --endpoint http://localhost:5001/v1/convert/file \
       --mock-vlm-timeline http://localhost:4000/timeline \
       --mock-vlm-reset http://localhost:4000/timeline/reset \
       --config configs/prod_like.json \
       --fixtures fixtures/ \
       --output results/baseline.json \
       --label baseline
   ```
3. **Снять py-spy профиль** во время прогона `Porazhay.pdf`:
   ```bash
   # в соседнем терминале, пока идёт run.py --only pdf_text_large
   WORKER_PID=$(docker exec bench-docling-serve pgrep -f 'uvicorn' | head -1)
   docker exec bench-docling-serve py-spy record -o /tmp/profile.svg \
       --pid $WORKER_PID --duration 180 --subprocesses
   docker cp bench-docling-serve:/tmp/profile.svg \
       benchmarks/profiles/baseline_porazhay/profile.svg
   ```
   (py-spy ставится в образе одной командой: `docker exec bench-docling-serve pip install py-spy`.)
4. **Скопировать `mock_vlm_timeline.jsonl`** в `benchmarks/profiles/baseline_porazhay/` (файл пишется на volume `./profiles/mock_vlm_timeline.jsonl`).
5. **Запушить результаты** в `main`: `baseline.json` + `profiles/baseline_porazhay/*` + коротая заметка в `CHANGELOG_FORK.md`.
6. **Решить вопрос варианта A/B** по версионной базе `docling-serve` (раздел 2).
7. **Решить вопросы 1–8** из `TASK_02_infrastructure.md` раздел 5, которые я принял сам (особенно 6 — pin образа, и 7 — `git lfs` / внешнее хранилище для fixtures).

Когда пункты 1–6 выполнены — TASK_02 полностью завершён. Я дополню `TASK_02_REPORT.md` разделом «эмпирическая проверка барьеров» и дадим TASK_03 зелёный свет.

Если **любой** из шагов невыполним (например, Docker-сборка не пройдёт из-за сетевых ограничений в корпоративном окружении) — скажи, в чём проблема, и я адаптирую инструмент под доступную среду (например, запустить docling-serve без Docker через `pip install docling-serve==1.16.1` в venv + отдельный uvicorn для mock-vlm).

---

## 6. Ответ на вопрос «как залить примеры документов»

Твой вопрос: **«в проект залить примеры документов чтобы ты на них потестировал что получается и какая скорость?»**

### Куда

Все документы — в `benchmarks/fixtures/<подкаталог>/`. Структура уже есть, с `.gitkeep` заглушками. Карта:

| Файл                        | Подкаталог                        |
| --------------------------- | --------------------------------- |
| `Porazhay.pdf`              | `benchmarks/fixtures/pdf_text_large/`    |
| `trigonometria-47-52.pdf`   | `benchmarks/fixtures/pdf_formulas/`      |
| `schet-10.pdf`              | `benchmarks/fixtures/pdf_text_small/`    |
| `432674638.pdf`             | `benchmarks/fixtures/pdf_scan_cyrillic/` |
| DOCX с OLE (когда подберёшь)| `benchmarks/fixtures/docx_with_ole/`     |

Имена файлов сохраняем оригинальные — они упомянуты в `CLAUDE.md` и `TASK_01_DECISIONS.md`, так будет проще связать с baseline.

После заливки — обнови `benchmarks/fixtures/README.md`: для каждого файла допиши «источник, N страниц, N картинок, что проверяем». Если лень — просто коммить с пометкой «TODO: описать в README», я допишу при ревью.

### Про размер / git lfs

`Porazhay.pdf` — 207 страниц с 84 картинками, почти наверняка **десятки МБ**. Прямой коммит в git — не лучший выбор, история будет тяжелеть. Варианты:

- **Вариант A (проще):** `git lfs track "benchmarks/fixtures/**/*.pdf"`, инициализировать LFS в репо, закоммитить `.gitattributes`, дальше обычный `git add`. GitHub LFS бесплатный лимит 1 ГБ — нам хватает с запасом.
- **Вариант B (без LFS):** документы живут на `tvr-srv-ai` в `/srv/benchmarks/fixtures/`, а в репо — **только** placeholder-файлы `<имя>.external` с путями и sha256. `run.py` добавлю поддержку `.external` разрезолва в локальный путь через env (`BENCHMARKS_FIXTURES_ROOT=/srv/benchmarks/fixtures`).
- **Вариант C (компромисс):** маленькие (schet-10.pdf, trigonometria, 432674638) — прямо в git; большие (Porazhay.pdf) — через LFS или external.

**Моя рекомендация:** **вариант A (git lfs)**. Мы контролируем fork полностью, файлы не секретные, GitHub LFS простой и хорошо интегрируется с git. Вариант B добавляет слой абстракции в `run.py`, без которого можно обойтись.

### Про анонимизацию (важно ещё раз)

Если в каком-то файле есть PII / коммерческая тайна:
- замажь конкретные куски **до** коммита
- или выбери другой документ
- или оставь вне git + положи `.external` placeholder (вариант B выше)

Я не вижу содержимое документов до их коммита, так что ответственность за анонимизацию — на тебе. В `fixtures/README.md` это правило уже прописано.

### **Что я НЕ могу сделать прямо сейчас — скажу честно**

Тестировать **реальную скорость прямо сейчас я не могу**:

1. **В песочнице Claude нет docker daemon.** Tooling для прогона полностью готов и синтаксически проверен, но запустить `docker compose up` в этой среде невозможно.
2. **У меня нет сетевого доступа к prod SGLang (`10.121.3.190:4000`).** Это внутренняя сеть кластера — песочница не может туда дотянуться.
3. **Продовые файлы в песочницу не попадают.** Даже если ты их куда-то положишь в репо сейчас — я смогу их **увидеть** (прочитать структуру), но **не смогу прогнать** без рабочего Docker.

**Что я могу сделать, когда ты зальёшь документы:**
- Проверить, что файлы в правильных подкаталогах, имена соответствуют ожидаемым
- Прочитать (через `pdfinfo`/`pdftotext`, если они есть в песочнице, или через python-библиотеки) метаданные — количество страниц, приблизительное количество картинок, чтобы обновить `fixtures/README.md`
- Статически проанализировать структуру PDF (какие типы объектов, есть ли embedded images, есть ли OCR-слой) и соотнести с ожидаемым поведением docling — это может дать дополнительные данные для TASK_03, даже без прогона
- Подготовить ожидаемые ответы на разные сценарии, чтобы ускорить анализ когда ты пришлёшь `baseline.json`

**Предлагаемый порядок дальнейшей работы:**
1. Ты заливаешь 4-5 документов в `benchmarks/fixtures/*/` (вариант git lfs или просто commit в git, если суммарно ≤ 20 МБ)
2. Я (статически) анализирую структуру PDF каждого файла, обновляю `fixtures/README.md`, фиксирую предсказания
3. Ты на своей машине запускаешь `docker compose up && python run.py ...` (инструкция в `benchmarks/README.md` готова)
4. Ты коммитишь `results/baseline.json` + `profiles/baseline_porazhay/*` в `main`
5. Я анализирую, фиксирую в финальной версии `TASK_02_REPORT.md`, даю зелёный свет TASK_03

Если у тебя есть **другой способ** дать мне среду с docker + сеть до SGLang + реальные файлы (ssh на prod/dev, другой sandbox, pre-populated docker image) — скажи, я адаптируюсь.

---

## 7. Открытые вопросы для согласования до TASK_03

Вопросы, которые я не могу решить в одиночку и которые блокируют переход к TASK_03:

1. **Вариант A vs B для версионной базы `docling-serve`.** Я рекомендую A (текущий upstream/main = 683eeca). Нужен твой явный ответ, чтобы зафиксировать в `CHANGELOG_FORK.md`.

2. **Хранение fixtures.** Git LFS, внешнее хранилище, или прямой commit? Рекомендую **git lfs**.

3. **Анонимизация документов.** Для каких файлов нужна и в каком объёме. Я не вижу содержимое — решение на тебе.

4. **Кто запускает baseline-прогон и когда.** Сам или я создаю отдельный subtask с инструкцией для CI runner'а? Если CI — нужен pipeline и секреты.

5. **Pin образа docling-serve для baseline.** Оставляем `:main` (плавает) или прибиваем digest? Рекомендую **digest** для baseline, чтобы сравнение после TASK_03 не было загрязнено upstream-дрейфом.

6. **`picture_description_api.concurrency=8`** в `configs/prod_like.json` — я взял из `DEFAULT_VLM_CONCURRENCY` в `docling-proxy/main.py`. Подтверди, что в prod именно это значение. Если нет — правим конфиг, тогда baseline пойдёт с другим верхним потолком inflight.

7. **Нужен ли дополнительно «боевой» baseline с реальным SGLang** (помимо mock). Моё мнение: **нет**, mock-baseline достаточен для подтверждения барьеров. Но если хочешь «абсолютные» числа — скажи, добавлю конфиг `configs/prod_sglang.json` и прогонять будем отдельно.

8. **Параллельно запускать TASK_03 с baseline-прогоном?** Моё мнение: **да** — код-анализ в TASK_01 был глубоким, шансов на сюрприз от профиля мало. Если не хочешь рисковать — подожди baseline перед TASK_03.

---

## Статус завершения

**TASK_02 — частично завершён.**

- ✓ Ветки `upstream-tracking` в обоих репо
- ✓ `CHANGELOG_FORK.md` в обоих репо
- ✓ Версии уточнены
- ✓ Каркас `benchmarks/` полностью готов (19 файлов, синтаксис проверен)
- ✓ `TASK_02_infrastructure.md` (план) и `TASK_02_REPORT.md` (этот файл) в `TASKS/`
- ✗ Docker-поднятие — **blocked** (нет docker daemon в песочнице)
- ✗ `py-spy` профиль — **blocked** (зависит от Docker + документа)
- ✗ `baseline.json` — **blocked** (зависит от Docker + документов)

**TASK_02 завершён, ожидаю обратной связи.** Нужны ответы на 8 открытых вопросов выше и, если хочешь полного завершения TASK_02, — прогон baseline на твоей стороне.

Параллельно, если ты дашь отмашку, могу стартовать **TASK_03** (архитектурный сдвиг: `RecognitionBackend`, submit-all+fan-in, overlap фаз, отключить `picture_classification`). Первый шаг TASK_03 — опять **анализ** (без кода), результат — `TASK_03_analysis.md` с детальным планом вмешательства. Потом пауза для согласования, потом реализация.
