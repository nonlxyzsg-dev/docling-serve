# TASK 01 — Отчёт: анализ параллелизма и архитектуры форка docling / docling-serve

> **Фаза:** Анализ. Кода в задаче не пишется.
> **Объект:** форки `nonlxyzsg-dev/docling` (2.88.0), `nonlxyzsg-dev/docling-serve` (1.16.1), `docling-jobkit` 1.17.0 (pip-зависимость), `nonlxyzsg-dev/docling-proxy` (справочник workaround'ов).
> **Автор:** Claude (сессия TASK_01).
> **Статус форков на момент анализа:** чистый checkout upstream, `[fork]`-коммитов нет, `CHANGELOG_FORK.md` отсутствует в обоих репо.

---

## 1. Резюме

**Главные находки одним абзацем.** Гипотеза о «барьерах между батчами» **подтверждена и локализована**: обработка картинок в `docling` построена на последовательности «собрать батч → отправить в `ThreadPoolExecutor` → дождаться всех → следующий батч». Барьер возникает в трёх независимых местах: (а) `PictureDescriptionApiModel._annotate_images` создаёт новый `ThreadPoolExecutor` на каждый batch, `executor.map()` + выход из `with`-блока синхронно ждут завершения всех; (б) `base_pipeline._enrich_document` строго исчерпывает каждый batch перед следующим (`# Must exhaust!`); (в) `ApiVlmEngine.predict_batch` также собирает результаты через `[future.result() for future in futures]` — синхронный барьер до окончания всего батча. Плюс к этому: `elements_batch_size=16` по умолчанию и комментарий в коде `picture_description_api_model.py:51` — *«не все API разрешают batch, например vllm не разрешает более 1»* — подтверждают, что архитектура ориентирована на «batch = waves», а не на непрерывный поток.

**Дополнительно:** `page_batch_size=4` (`docling/datamodel/settings.py:31`) — это *НЕ* хардкод в привычном смысле: `pydantic-settings` с `env_prefix="DOCLING_"` делает его конфигурируемым через `DOCLING_PERF_PAGE_BATCH_SIZE`. Upstream issue #419 в этом смысле вводит в заблуждение: проблема не в «захардкожено», а в том, что (1) дефолт низкий, (2) `page_batch_concurrency` в том же файле помечен как `# Currently unused`, (3) параметр нигде не документирован. То есть управляемость есть, но архитектура *pages batch → drain → next batch* остаётся неизменной — и именно она порождает «волны» в метриках SGLang.

**Реалистичная оценка ускорения после форк-патчей.** Оценка по коду, без профилирования на проде:

- Убрать барьер `picture_description_api` (переход на непрерывный поток задач через общий семафор + пул задач на уровень документа) → ожидаемо **×2–×4** на документах с 50+ картинок, ближе к верхней границе при `vlm_concurrency ≥ 16`.
- Параллельно: поднять `DOCLING_PERF_PAGE_BATCH_SIZE` с 4 до 10–16 → **небольшой выигрыш** только на этапе pages, не решает волн.
- Суммарно: реалистичный ориентир — ускорение **с 13 мин до 3–5 мин** на нашем эталонном документе 207 стр./84 картинки. До «эталона OCR SDK» (1–1.5 мин) форк-патчами **не доберёмся** — разрыв закроет только переход на другой recognition backend (см. раздел 5).

**Главные риски.**
1. **Rebase-нагрузка.** Upstream активно меняет `base_pipeline` и `stages/` (видно по git log — регулярные рефакторинги). Каждый наш `[fork]`-патч в этих файлах — будущий merge-конфликт.
2. **Thread-safety.** Issue #2285 вскрывает проблему, которую мы пока обходим везде через `_PIPELINE_CACHE_LOCK`, но `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=2` + общие модели в кэше pipeline объективно создаёт окна для гонок на кэшах токенизатора и моделях enrichment.
3. **Неопределённость относительно docling-jobkit.** Local orchestrator и точка барьера `worker.py:126` (SyntaxError) живут в отдельной pip-зависимости, которую мы не форкаем. Патчим sed'ом в Dockerfile — это фрагильно.
4. **Picture classification — CPU, последовательный.** Не покрывается форк-патчами «убрать барьеры», требует отдельного решения (отключение или GPU).
5. **Regression surface большой.** Любое изменение в pipeline ломает текущее поведение на документах, которые сейчас работают приемлемо. Нужен benchmark-пакет до того, как приступить к фиксам.

---

## 2. Карта проблемы параллелизма

### 2.1. Полный цикл обработки одного документа (фактический flow)

Порядок исполнения при типичной для нас конфигурации (TEXT PDF > 20 стр., standard pipeline, `picture_description_api`):

```
docling-serve app.py  /v1/convert/file
  → FormDepends → ConvertDocumentsRequestOptions
  → orchestrator.enqueue() (LocalOrchestrator из docling-jobkit)
    → поток воркера (DOCLING_SERVE_ENG_LOC_NUM_WORKERS=2)
      → DocumentConverter.convert()
        → pipeline.execute()              # StandardPdfPipeline
          ├─ _build_document()            # pages в батчах page_batch_size=4
          │   для каждого batch:
          │     layout → ocr → tables → page predictions
          │     # Must exhaust! барьер в конце батча
          └─ _enrich_document()           # enrichment по всему документу
              для каждой enrichment-модели (последовательно):
                для каждого batch картинок (elements_batch_size=16):
                  model(element_batch)
                    → PictureDescriptionApiModel._annotate_images()
                      → with ThreadPoolExecutor(concurrency):
                          yield from executor.map(...)   # ← барьер
                  # выход из with → drain всех оставшихся → барьер
```

Ключевая особенность: enrichment идёт **после** завершения всей страничной фазы, и при этом внутри enrichment идёт последовательно по моделям и по батчам. Pipeline **не** пытается параллелить «pages ↔ enrichment» или «batch N ↔ batch N+1 enrichment». Это и есть корневая причина волн.

### 2.2. Барьеры в коде — конкретные места

#### Барьер №1 — `PictureDescriptionApiModel._annotate_images`

**Файл:** `docling/models/stages/picture_description/picture_description_api_model.py:50-66`

```python
def _annotate_images(self, images: Iterable[Image.Image]) -> Iterable[str]:
    # Note: technically we could make a batch request here,
    # but not all APIs will allow for it. For example, vllm won't allow more than 1.
    def _api_request(image):
        page_tags, _, _ = api_image_request(...)
        return page_tags

    with ThreadPoolExecutor(max_workers=self.concurrency) as executor:
        yield from executor.map(_api_request, images)
```

Что здесь происходит:
1. `ThreadPoolExecutor` создаётся **заново на каждый вызов метода**, то есть на каждый batch элементов.
2. `executor.map()` эагерно отправляет все задачи из `images` в пул и возвращает итератор.
3. `yield from` выдаёт результаты в порядке подачи. Если первый запрос завис, даже уже завершённые последующие блокируются на стороне `yield`.
4. Выход из `with` → `__exit__` → `shutdown(wait=True)` — ждёт завершения всех оставшихся задач.
5. Следующий batch элементов → снова новый executor → снова пустая очередь → снова ramp-up → снова drain.

Дефолт `concurrency = 1` (`docling/datamodel/pipeline_options.py:709`, `PictureDescriptionApiOptions`). У нас прокси инжектирует свой `concurrency=14/16` через `build_picture_description_api` (`docling-proxy/main.py:289-303`), иначе было бы вообще последовательно.

Комментарий в строке 51 важен стратегически: авторы docling явно указывают, что **не делают батчовых API-запросов**, потому что «некоторые API не разрешают». Это значит, что даже если бы не было барьера между батчами, внутри батча запросы всё равно идут по одному на картинку.

#### Барьер №2 — `_enrich_document` в `base_pipeline`

**Файл:** `docling/pipeline/base_pipeline.py:106-128` (цитата по отчёту Explore-агента):

```python
for model in self.enrichment_pipe:
    for element_batch in chunkify(
        _prepare_elements(conv_res, model),
        model.elements_batch_size,
    ):
        for element in model(
            doc=conv_res.document, element_batch=element_batch
        ):  # Must exhaust!
            pass
```

Комментарий `# Must exhaust!` — это прямое указание на барьер: внешний цикл не продвинется к следующему `element_batch`, пока внутренний генератор не будет полностью вычерпан. Это именно то место, где «все картинки батча должны завершиться до следующего батча».

`elements_batch_size` задаётся либо классом модели (см. комментарий `# elements_batch_size = 4` в `picture_description_api_model.py:19` — закомментирован), либо общим дефолтом `BatchConcurrencySettings.elements_batch_size = 16` в `docling/datamodel/settings.py:33-35`.

#### Барьер №3 — `ApiVlmEngine.predict_batch` (VLM pipeline)

**Файл:** `docling/models/inference_engines/vlm/api_openai_compatible_engine.py:101-224` (цитата по отчёту Explore-агента):

```python
def predict_batch(self, input_batch: List[VlmEngineInput]) -> List[VlmEngineOutput]:
    ...
    max_workers = min(self.options.concurrency, len(input_batch))
    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = [
            executor.submit(_process_single_input, input_data)
            for input_data in input_batch
        ]
        outputs = [future.result() for future in futures]  # ← барьер
    return outputs
```

Эта реализация используется, если активен **VLM pipeline** (full-page OCR для сканов). Синхронный барьер здесь ещё жёстче: `[future.result() for future in futures]` возвращает полный список, только когда все futures завершены. Вызывается из `VlmConvertModel.__call__`, который сам вызывается из `base_pipeline._build_document` внутри цикла по `page_batch`.

**Важно:** `max_workers = min(concurrency, len(batch))`. Если `page_batch_size=4` и `concurrency=14`, то `max_workers=4` — **пул сужается до размера батча**, остальные 10 слотов не используются. Это отдельная причина, почему «поднять concurrency» без увеличения batch бесполезно.

#### Барьер №4 — `_build_document` между page-батчами

**Файл:** `docling/pipeline/base_pipeline.py:236-322` (цитата по отчёту Explore-агента):

```python
for page_batch in chunkify(conv_res.pages, settings.perf.page_batch_size):
    ...
    for p in pipeline_pages:  # Must exhaust!
        ...
```

Тот же паттерн «chunkify → exhaust → next chunk». На этапе page processing это создаёт волны для VLM pipeline (сканы).

### 2.3. Хардкоды, константы и «скрытые» дефолты

| Константа | Значение | Файл:строка | Комментарий |
|---|---|---|---|
| `page_batch_size` | 4 | `docling/datamodel/settings.py:31` | Конфигурируется через `DOCLING_PERF_PAGE_BATCH_SIZE`, но дефолт низкий |
| `page_batch_concurrency` | 1 | `docling/datamodel/settings.py:32` | **`# Currently unused`** — объявлен, но код на него не смотрит. Это и есть точка, где архитектурно должно бы быть «сколько page-батчей идут параллельно», но не реализовано |
| `doc_batch_size` | 1 | `docling/datamodel/settings.py:29` | Комментарий: *should be ≥ doc_batch_concurrency* |
| `doc_batch_concurrency` | 1 | `docling/datamodel/settings.py:30` | **`# Warning: Experimental! No benefit expected without free-threaded python`** — признают, что GIL не даёт выигрыша на уровне документов без Python 3.13 free-threaded build |
| `elements_batch_size` | 16 | `docling/datamodel/settings.py:33-35` | Общий дефолт для enrichment. `PictureDescriptionApiModel` комментарий `# elements_batch_size = 4` (закомментирован) — намекает, что раньше было 4 |
| `PictureDescriptionApiOptions.concurrency` | 1 | `docling/datamodel/pipeline_options.py:709` (по отчёту агента) | Дефолт, перекрывается нашим прокси |
| `PictureDescriptionBaseOptions.batch_size` | 8 | `docling/datamodel/pipeline_options.py:599` (по отчёту агента) | ⚠ Противоречие: reporter агент нашёл `batch_size=8` в `pipeline_options.py`, но в `settings.py` общий дефолт 16. Возможно, специфичный для модели override. **Требует уточнения** при переходе к TASK_02 |

### 2.4. Что в принципе не параллелится сейчас

1. **`picture_classification`** (`DocumentPictureClassifier`, `docling/models/stages/picture_classifier/document_picture_classifier.py:37-210`). CPU-модель, `predict_batch` обрабатывает весь батч за один вызов, параметров `concurrency`/`batch_size` у неё нет. На 84 картинках это вклад в общий tail time.
2. **`PictureDescriptionVlmModel`** (локальный SmolVLM, `docling/models/stages/picture_description/picture_description_vlm_model.py:24-132`). Нет параметра `concurrency` — батч отправляется в `model.generate()` одним вызовом PyTorch. Это архитектурный выбор, не баг: локальная модель работает на одном GPU, параллелизм там на уровне tensor ops. Мы этот путь не используем (прокси принудительно `picture_description_api`), но его инициализация вызывается даже при VLM pipeline — см. баг №3 в каталоге (раздел 3).
3. **Enrichment-модели идут последовательно друг за другом** (`for model in self.enrichment_pipe`). Если включены picture_description + picture_classification + table structure одновременно, они запускаются одна за другой, а не параллельно. Не наш текущий случай (classification отключена), но это тоже потенциальная точка.
4. **Документы в проде обрабатываются через Local orchestrator с `num_workers=2`**, но: `DOCLING_SERVE_ENG_LOC_NUM_WORKERS` управляет числом тасков в пуле docling-jobkit; комментарий в `docling/datamodel/settings.py:30` прямо говорит, что `doc_batch_concurrency` выше 1 не даёт выигрыша без free-threaded Python (GIL).

### 2.5. Почему получаются «волны» — реконструкция паттерна

Исходные данные: документ 207 стр. / 84 картинки, TEXT PDF, standard pipeline, `vlm_concurrency=14-16`.

Реконструкция по коду:

1. **Фаза pages** (~5–7 мин у нас, в основном CPU-работа: layout, tables, extraction). SGLang в это время простаивает — картинки ещё не дошли до VLM. Объясняет начальный период нулевого RPS в метриках.
2. **Фаза enrichment (picture_description_api)**. 84 картинки разбиваются на батчи по `elements_batch_size` (16 или 8 — см. противоречие выше). Для каждого батча:
   - Новый `ThreadPoolExecutor(max_workers=14)` → все 14 слотов сразу заполняются → SGLang видит всплеск 14–16 параллельных запросов.
   - По мере ответов `yield from executor.map(...)` отдаёт результаты в порядке подачи. Если 13 запросов готовы, а первый ещё думает — 13 «висят» и не yield'ятся, но пул-то уже свободен и мог бы начать следующий батч — **но не начинает**, потому что следующий батч управляется из внешнего цикла `_enrich_document`, который ждёт `exhaust`.
   - Выход из `with ThreadPoolExecutor` → shutdown → drain. SGLang видит снижение потока до 0–1.
   - Пока CPU-поток docling готовит следующий batch (подкачка элементов из `chunkify`, prep картинок), **пул задач пуст** → 30–60 сек простоя SGLang, которые мы видим в метриках.
   - Новая итерация цикла → новый `ThreadPoolExecutor` → новый всплеск.
3. 84 картинки / батч 16 = ~6 батчей. 6 волн при ~60 сек каждая + 6 промежутков по ~30 сек + фаза pages ≈ 8–10 мин обработки картинок + 3–5 мин pages = **13 мин суммарно**. Совпадает с наблюдаемым.

Это модель, не профиль. На проде она проверяется микробенчмарком, см. раздел 9.

### 2.6. Где barrier НЕ является барьером — и почему это не помогает

`executor.map()` сам по себе итератор и теоретически мог бы быть потребителем без барьера. Но:

- Он создаётся **внутри** `_annotate_images`, и его lifetime ограничен `with`-блоком.
- Внешний код (`_enrich_document`) вычерпывает этот генератор до конца перед следующим батчем (`# Must exhaust!`).
- Никто на уровне pipeline **не** держит долгоживущий пул задач с общим семафором.

Что сделать, чтобы «убрать барьер», — см. раздел 7 (TASK_02+). В этом отчёте решения не предлагаются кодово; только указывается, что точка вмешательства — это переход от «executor per batch» к «pool + semaphore per document» или даже «per orchestrator».

---

## 3. Каталог известных багов

Четыре workaround'а из `CLAUDE.md` разделов 7.4 (docling-serve) и 6.3 (docling), найденные в коде.

### 3.1. Таблица

| # | Баг | Файл/место в upstream | Актуален ли в 2.88.0 / 1.16.1 / jobkit 1.17.0 | Как обходим сейчас | Как фиксить нативно в форке | Сложность |
|---|---|---|---|---|---|---|
| 1 | `SyntaxError: keyword argument repeated: exc_info` в `worker.py:126` | `docling_jobkit/orchestrators/local/worker.py:126` (внешняя pip-зависимость, не в наших репо) | **Актуален** (пин `docling-jobkit>=1.17.0,<2.0.0`, sed-патч в Dockerfile активен) | `sed` в Dockerfile образа docling-serve, патчит дублирующийся kwarg | Вариант A: отправить PR в docling-jobkit upstream (это тривиальный фикс — убрать дубль). Вариант B: в нашем форке docling-serve вшить monkey-patch или локальный wheel docling-jobkit. Вариант C: форкать docling-jobkit тоже | **S** (сам фикс — 1 строка; вопрос в том, куда коммитить) |
| 2 | Pillow `tile cannot extend outside image` при `image_export_mode=embedded` + VLM pipeline | `docling/pipeline/vlm_pipeline.py:306-315` (crop bbox после scale может выйти за границы страницы) | **Актуален** (код на месте в 2.88.0) | Прокси (`docling-proxy/main.py:750`) принудительно устанавливает `image_export_mode=placeholder` при `pipeline=vlm` | Клэмпить `crop_bbox` к размерам изображения перед `page.image.crop(...)`. Защитная функция `_safe_crop(img, bbox)` либо прямое ограничение координат | **S** |
| 3 | `do_picture_description=true` при VLM pipeline инициализирует SmolVLM → `FileNotFoundError` | `VlmPipeline` наследуется от `PaginatedPipeline` → `ConvertPipeline.__init__`, где unconditional инициализируется `PictureDescriptionModel` даже когда он логически избыточен для VLM pipeline (full-page OCR уже описывает картинки) | **Актуален** | Прокси (`docling-proxy/main.py:756-758`) при `pipeline=vlm` принудительно сбрасывает `do_picture_description=false`, `do_picture_description_custom=false` | В `VlmPipeline.__init__` не инициализировать picture_description (пропускать его в enrichment_pipe), либо на уровне `ConvertPipeline` ввести флаг `skip_enrichment_for_vlm_pipeline` | **M** (риск зацепить общий pipeline init, нужен тест) |
| 4 | `picture_description_custom_config` не уважает `concurrency` — последовательная обработка | `PictureDescriptionVlmModel._annotate_images` (`docling/models/stages/picture_description/picture_description_vlm_model.py:91-132`) — batch материализуется целиком и отправляется в `model.generate()` без параметра `concurrency` | **Актуален**, но это архитектурный выбор для локального SmolVLM на одном GPU (параллелизм на уровне torch). Проблема реальна, только если пользователь пытается применить custom_config к API-backend | Прокси (`docling-proxy/main.py:764-771`) всегда переключает на `picture_description_api`, комментарий в коде: *«custom_config был из-за thinking mode, но агентная инстанция + /no_think решает это»* | Нативно: не «фикс», а архитектурный шаг — расширить `PictureDescriptionVlmOptions` параметром `concurrency` для случая API-режима. На практике это уже делает `PictureDescriptionApiModel`, поэтому дублировать не нужно. **Разумнее: ничего не делать, оставить выбор в руках пользователя через `picture_description_api`** | **S** (если решим вообще фиксить) / N/A (если нет) |

### 3.2. Комментарии к таблице

**Баг №1 — docling-jobkit.** Важно, что это **НЕ в нашем форке** `docling`. `docling-jobkit` — отдельный репозиторий, который мы не форкаем. В `docling-serve/pyproject.toml` строка 38 закрепляет `docling-jobkit[kfp,rq,ray,vlm]>=1.17.0,<2.0.0`. Если баг не закрыт в upstream docling-jobkit — у нас два пути:
- быстрый: оставить sed-патч в Dockerfile (фрагильно, но работает);
- системный: завести маленький форк docling-jobkit и pin'ить на него.
В рамках TASK_01 решение не принимается, просто фиксирую проблему как открытую.

**Баг №2 — Pillow crop.** Причина — `bbox.scaled(scale=scale)` может дать координаты, выходящие за пределы изображения страницы после масштабирования (например, из-за float-ошибок или элементов, лежащих на границе). Фикс тривиален — clamp координат к размерам `page.image.size`.

**Баг №3 — picture_description при VLM pipeline.** Архитектурно некрасивое место: VLM pipeline делает full-page OCR, и добавлять поверх этого ещё и отдельное описание картинок избыточно. Сейчас оно инициализируется «про запас», потому что конструктор `ConvertPipeline` не различает сценарии. Фикс: либо пропускать инициализацию при `pipeline_options` соответствующего типа, либо сделать enrichment_pipe опциональным для VLM pipeline.

**Баг №4 — custom_config concurrency.** Формально это не баг, а имя. `PictureDescriptionVlmModel` — это путь для локальной HF-модели (SmolVLM, Qwen local и т.п.), где concurrency неуместен. Проблема возникает, когда пользователь ожидает, что custom_config — это «гибкая версия API». Решение — документация + имена, а не код.

### 3.3. Upstream issues — статус на момент анализа

- **#419** (docling-serve, `page_batch_size`): **закрыт**. В обсуждении признана неточность — параметр конфигурируем через env, но дефолт низкий.
- **#425** (docling-serve, RQ parallel processing): **открыт**. К нашей «волновой» проблеме прямого отношения не имеет (мы в Local mode), но упомянут в CLAUDE.md как контекст.
- **#463** (docling-serve, env vars для VLM defaults): **открыт**. Подтверждено в коде `docling_serve/settings.py`: нет env vars для `vlm_api_url`, `vlm_api_key`, `vlm_concurrency` и т.п. Наш прокси эту роль сейчас выполняет.
- **#318** (docling-serve, VLM API не вызывается): **закрыт**. Нас не затрагивает напрямую.
- **#257** (docling-serve, общая производительность): **закрыт** (PR #341). Но PR #341 не решает проблему «волн» — он улучшает общий baseline, не убирая барьеры.
- **#2285** (docling discussion, thread-safety): **открыт, без ответа**. См. раздел 4.
- **#2635** (docling, зависание VLM pipeline): **открыт**. Симптомы другие (полное зависание в async режиме, недоступность GPU), но намекает на проблемы в той же подсистеме.

Вывод: наши 4 workaround'а в прокси **все ещё актуальны** в 2.88.0 / 1.16.1 / docling-jobkit 1.17.0. Никакой из них upstream не закрыл.

---

## 4. Thread-safety

### 4.1. Наша конфигурация в проде

- `UVICORN_WORKERS=2` → 2 независимых **процесса** Python (fork). GIL у каждого свой, память не разделяется, кэши и состояния изолированы.
- `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=2` → внутри каждого процесса LocalOrchestrator создаёт пул **потоков** с 2 воркерами, которые обрабатывают задачи из очереди через общий `DoclingConverterManager`. Память и объекты шарятся.
- `DOCLING_SERVE_ENG_LOC_SHARE_MODELS=False` (дефолт) → **модели не шарятся** между воркерами явно, но `DocumentConverter` сам кэширует pipeline'ы через `_PIPELINE_CACHE_LOCK`, то есть один и тот же pipeline-объект может прийти из кэша в оба потока одного процесса.

**Итого:** внутри одного процесса у нас потенциально **2 потока работают с одним и тем же pipeline, который содержит инициализированные VLM/OCR/layout модели**. Это именно тот сценарий, который Issue #2285 называет небезопасным.

### 4.2. Что защищено — и чем

**`DocumentConverter._get_pipeline` (`docling/document_converter.py:589-602`, цитата по отчёту Explore-агента):**

```python
_PIPELINE_CACHE_LOCK = threading.Lock()
...
def _get_pipeline(self, doc_format):
    ...
    with _PIPELINE_CACHE_LOCK:
        if cache_key not in self.initialized_pipelines:
            self.initialized_pipelines[cache_key] = pipeline_class(...)
        else:
            return self.initialized_pipelines[cache_key]
```

Защищена **только инициализация** кэша. Само использование pipeline (и моделей внутри него) происходит **без лока**. Это корректно, если модели read-only и не содержат мутабельного состояния во время инференса.

**`PictureDescriptionVlmModel._model_init_lock` (строки 20-87 по отчёту агента):**

```python
_model_init_lock = threading.Lock()
...
with _model_init_lock:
    self.processor = AutoProcessor.from_pretrained(...)
    self.model = AutoModelForImageTextToText.from_pretrained(...)
```

Опять же — лок только на инициализацию.

### 4.3. Что НЕ защищено — и где риск

Issue #2285 явно называет следующие классы как потенциально не-thread-safe:

1. **`HybridChunker`** — «not inherently thread-safe».
2. **`HuggingFaceTokenizer`** — «uses cached properties that can be mutated during processing».
3. **OCR модели** — «may not be thread-safe if shared across threads».
4. **Layout модели** — «similar concerns as OCR».
5. **PDF backends** — «contain global locks limiting true parallelism».

Из этих пунктов для нас релевантны:
- **HuggingFaceTokenizer** — используется enrichment-моделями (в т.ч. picture description через transformers, когда custom_config). Мы эту ветку не используем, но она инициализируется (см. баг №3). Риск низкий, но есть.
- **OCR модели** — у нас OCR отключён (`do_ocr=false`, прокси это инжектирует на уровне standard pipeline). Риск **не актуален**.
- **Layout модели** — активны всегда. При 2 одновременных документах в одном процессе они шарятся. Риск реальный.
- **PDF backends** — глобальные локи на уровне `pypdfium2` ограничивают параллелизм, но не создают гонок. Это деградация скорости, не корректности.

**Отдельный риск — `DocumentPictureClassifier` и enrichment engines.** По отчёту агента: `self.engine`, `self._classes` — read-only после init. Инференс через engine должен быть stateless. Но это нужно **верифицировать на практике** — race может проявиться в cached-тensor buffers внутри torch-модулей, которые снаружи выглядят read-only.

### 4.4. Насколько реален риск при типичной нагрузке

Наша нагрузка: 1–3 документа одновременно, пиково до 10 (при 2 процессах × 2 потока = 4 параллельных обработок максимум). Вероятность, что два потока одновременно войдут в layout-модель на одном и том же pipeline — **высокая в пиковые моменты**.

Симптомы, которые мы увидели бы при нарушении thread-safety:
- Плавающие ошибки на пике нагрузки, не воспроизводимые локально.
- Редкие «битые» результаты (перепутанные координаты bbox между страницами разных документов).
- Segfault в torch/transformers из-за гонок на тензорных буферах.

Пока мы такого **не наблюдали** в проде (по крайней мере, в задаче не упомянуто). Это не значит, что проблема отсутствует — это значит, что пока повезло или она маскируется на уровне логов.

### 4.5. Что делать

**В рамках форка — три варианта.**

**Вариант A: изолировать pipeline по потоку.** Сделать `DocumentConverter` per-thread (thread-local instance). Плюс: простота, гарантия. Минус: удвоение памяти моделей (layout, VLM processor). При 2 потоках × 2 процесса = 4 копии — неприемлемо для prod на CPU-only образе.

**Вариант B: ограничить concurrency на уровне orchestrator.** Поставить `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1` (один поток на процесс), масштабировать процессами. Плюс: гарантия безопасности, меньше GIL-давления. Минус: меньше параллелизма на одном ядре.

**Вариант C: добавить fine-grained локи в критичных моделях.** Точечно обернуть layout/tokenizer локом внутри `__call__`. Плюс: минимальная инвазивность. Минус: невидимо ломается производительность, требует аудита каждого места.

**Рекомендация для форка (без принятия решения в TASK_01):** Вариант B — самый безопасный быстрый ход. Поставить `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1`, `UVICORN_WORKERS=4` (вместо 2×2). Точно такой же суммарный параллелизм, но без шаринга моделей внутри процесса. Это изменение конфигурации, не кода, — можно попробовать без форк-патча и измерить.

**В рамках кода форка — не делать ничего в TASK_02/03, пока не станет понятно, что проблема реально проявляется.** Thread-safety — это оптимизация корректности, и если мы не видим гонок в проде, тратить инженерное время на их устранение преждевременно. Задача отложена, но зафиксирована как открытый риск.

---

## 5. Архитектура для сменного recognition backend

### 5.1. Хорошая новость — абстракция частично уже есть

В `docling/models/` существует каталог `inference_engines/vlm/`, где живут классы-энжины для VLM-вывода. Ключевые:

- `ApiVlmEngine` (`docling/models/inference_engines/vlm/api_openai_compatible_engine.py`) — OpenAI-compatible HTTP backend.
- (вероятно есть и другие; агент не перечислил все, но каталог существует и это точка расширения).

**`VlmConvertModel` (`docling/models/stages/vlm_convert/vlm_convert_model.py`)** принимает engine через интерфейс `predict_batch(input_batch: List[VlmEngineInput]) -> List[VlmEngineOutput]`. То есть на уровне VLM pipeline абстракция уже построена: stage-модель не знает, что под ней — API или локальная модель.

**Плохая новость:** эта абстракция есть **только** в новой `VlmConvertModel` ветке VLM pipeline. Для **picture description** (которую мы используем в standard pipeline) такой абстракции **нет**:

- `PictureDescriptionApiModel` (`docling/models/stages/picture_description/picture_description_api_model.py`) — прямо зашит на HTTP через `api_image_request` (`docling/utils/api_image_request.py`).
- `PictureDescriptionVlmModel` — прямо зашит на transformers `AutoModelForImageTextToText`.

То есть для picture description путь к «подключить OCR SDK как backend» — не через существующий `inference_engines`, а через создание нового класса-модели (например, `PictureDescriptionOcrSdkModel`).

### 5.2. Карта мест, где захардкожено «recognition = OpenAI-compatible VLM call»

| # | Место | Что захардкожено | Что нужно для смены backend |
|---|---|---|---|
| 1 | `PictureDescriptionApiModel._annotate_images` | Прямой вызов `api_image_request(...)` (HTTP, OpenAI-совместимый формат payload) | Вынести в отдельный «recognition backend» интерфейс: `RecognitionBackend.describe_picture(image, prompt, **kwargs) -> str`. Тогда `PictureDescriptionApiModel` становится адаптером для OpenAI API, а `PictureDescriptionOcrSdkModel` — для нашего OCR SDK |
| 2 | `docling/utils/api_image_request.py` | Вся логика сборки OpenAI-compatible payload: `messages`, `image_url` base64, `response.choices[0].message.content` | Эта утилита сейчас знает формат. При появлении второго backend — её нельзя переиспользовать, нужна точка полиморфизма выше |
| 3 | `ApiVlmEngine.predict_batch` (VLM pipeline) | OpenAI-compatible batch HTTP | Здесь абстракция уже есть через `VlmEngine.predict_batch` интерфейс. Достаточно добавить `OcrSdkVlmEngine(VlmEngine)` и зарегистрировать в factory |
| 4 | `inference_engines/vlm/*` factory | По имени `engine_type="api_openai"` выбирается `ApiVlmEngine` | Тут же можно добавить `engine_type="ocr_sdk"` |
| 5 | Опции в `pipeline_options.py` | `PictureDescriptionApiOptions` привязан к полям `url`, `headers`, `params`, т.е. ассумирует HTTP+OpenAI | Нужен либо base-класс `PictureDescriptionBackendOptions` с несколькими наследниками, либо `kind`-дискриминатор (pydantic `Literal`) |

### 5.3. Минимальная абстракция (предложение, без реализации)

**Идея — один интерфейс, две реализации.**

```
RecognitionBackend (Protocol)
├── describe_picture(image: PIL.Image, prompt: str, **kw) -> str
└── describe_batch(images: List[PIL.Image], prompt: str, **kw) -> Iterable[str]

OpenAiCompatibleBackend(RecognitionBackend)   # обёртка над текущим api_image_request
OcrSdkBackend(RecognitionBackend)              # клиент к нашему OCR SDK (HTTP 10.121.3.201:9996)
```

Picture description модель тогда:

```
PictureDescriptionModel
  backend: RecognitionBackend
  def _annotate_images(images):
      yield from backend.describe_batch(images, self.prompt, ...)
```

Конфигурация через discriminated union:

```yaml
picture_description:
  kind: openai_compatible | ocr_sdk
  # далее — поля, специфичные для kind
```

Это **не требует переписывать pipeline** — только заменить текущую `PictureDescriptionApiModel` на полиморфную версию. `base_pipeline._enrich_document` работает с любой реализацией интерфейса.

### 5.4. Что важно учесть в будущей реализации (не сейчас)

1. **Батч или не батч.** OpenAI-совместимые API в большинстве своём не принимают batch (комментарий в `picture_description_api_model.py:51` это признаёт). Наш OCR SDK, наоборот, **любит batch** (32 воркера). То есть интерфейс должен быть `describe_batch`, и в OpenAI-реализации он разворачивается в N параллельных вызовов, а в OCR SDK — в один batch-вызов. Это принципиально другой паттерн трафика, и абстракция должна это переживать.
2. **Semantic: OCR vs. description.** Наш OCR SDK делает **layout + текст**, не «семантическое описание картинки». Для инженерных чертежей, графиков, фото без текста этого может быть недостаточно. Возможно, нужны **два разных backend'а** одновременно: OCR SDK для картинок с текстом, VLM API для картинок без текста. Это решение выносится в отдельную задачу (TASK_NN: routing picture → backend by classification).
3. **Stateful vs. stateless.** OpenAI API stateless (каждый запрос независим). OCR SDK может иметь warm-up, коннекшен-пул, persistent sessions. Интерфейс должен допускать lifecycle methods (`init`, `close`), но не требовать их.
4. **Thread-safety.** Backend должен быть thread-safe (будет использоваться из enrichment-пула и, возможно, из `DocumentPictureClassifier`). Это требование к реализации, не к интерфейсу.

### 5.5. Уровень риска при будущем рефакторинге

- **Низкий** — если ограничиться заменой `PictureDescriptionApiModel` на адаптер через `RecognitionBackend`: изменения точечные, пара файлов, полиграмм не нужен.
- **Средний** — если делать полноценный discriminated union в `pipeline_options.py` и мигрировать API прокси: это ломает форматы параметров, нужна либо обратная совместимость на уровне docling-serve, либо синхронный флаг-день.
- **Высокий** — если одновременно пытаться унифицировать `inference_engines/vlm/` и `picture_description/*` в одну абстракцию. Upstream этого **не делал**, и такой рефакторинг — это большой PR, который будет конфликтовать при каждом rebase.

**Вывод для плана работ:** в TASK_02/03 эту абстракцию **не трогать**. Она выносится в отдельный этап (например, TASK_05), после того как основная «волновая» проблема будет решена. Сейчас достаточно зафиксировать **где именно** будущие изменения должны жить.

---

## 6. Версионная стратегия

### 6.1. Зафиксированные версии форка

| Репозиторий | Версия | Коммит | Состояние |
|---|---|---|---|
| `nonlxyzsg-dev/docling` | 2.88.0 (в `pyproject.toml:3`) | последний upstream-коммит в main: `f5fa294` (`chore(readme): fix broken Apify badge (typo)`), версионный тег-коммит: `e04e602` (`chore: bump version to 2.88.0`) | Чистый upstream. `[fork]`-коммитов нет. Поверх — только `d9bd8df` (`Add files via upload`) и `33b42dd` (`Rename docling__CLAUDE.md to CLAUDE.md`) — это наши organizational-коммиты, не изменения кода |
| `nonlxyzsg-dev/docling-serve` | 1.16.1 (в `pyproject.toml:3`) | последний upstream: `683eeca` (`feat: Move client SDK to docling (#575)`), версионный: `12a7943` (`chore: bump version to 1.16.1`) | Чистый upstream. Поверх — `3a5b1f0` (upload), `3344b00` (rename CLAUDE.md), `592debe` (create TASK_01_analysis.md) |
| `docling-jobkit` | 1.17.0 (по `uv.lock` в docling-serve) | — | **Не форкаем.** Пиним на диапазон `>=1.17.0,<2.0.0`. Баг `worker.py:126` патчим sed'ом в Dockerfile |
| `nonlxyzsg-dev/docling-proxy` | не применимо (не upstream-форк, наш собственный код) | последние коммиты: `v3.5: disable PaddleOCR`, `switch to picture_description_api with concurrency`, `add concurrency to build_custom_model` | Рабочая версия, изменения в прокси вне scope форка (см. CLAUDE.md docling-proxy) |

### 6.2. На какой версии форкаемся

**`docling` 2.88.0** — зафиксирован pin'ом в `docling-serve/pyproject.toml:36`: `docling>=2.88.0,<3.0.0`. Перейти на 2.89+ можно безболезненно внутри 2.x, но это отдельная решение после TASK_02+. Сейчас не меняем.

**`docling-serve` 1.16.1** — head upstream main на момент форка. Следующие минорные (1.17+) нужно будет рассмотреть отдельно.

**`docling-jobkit` 1.17.0** — pin на диапазон. Не форкаем. Если в upstream выйдет 1.17.1 с фиксом `worker.py:126` — убираем наш sed-патч.

### 6.3. Что менялось в upstream за последние 2–3 минорных версии (по git log)

По git log `docling` видны коммиты ближе к head:
- `f5fa294` — chore(readme)
- `e04e602` — bump 2.88.0
- `c23622f` — docs: agent skill bundle
- `42157a3` — feat(service): client SDK for docling serve (#3264)
- `6b257ec` — fix(ocr): rapidocr 3.8 mobile model naming
- `60fc517` — chore: Condensing latex test backend
- `2446f5c` — bump 2.87.0
- `d431224` — fix: transformers v5 compatibility for AUTOMODEL_CAUSALLM VLMs

Наблюдения:
- **Активность идёт в VLM-ветке** (`d431224` — fix для transformers v5 в VLM). Это именно те файлы, которые мы будем править (`vlm_convert_model.py`, `api_openai_compatible_engine.py`). Вероятность merge-конфликта при rebase — **высокая**.
- **Client SDK был вынесен в docling** из docling-serve (#3264, #575). Нас не касается напрямую, но меняет границы репозиториев.
- `rapidocr`, `latex backend`, доки — не наша зона.

По `docling-serve`:
- `683eeca` — feat: Move client SDK
- `12a7943` — bump 1.16.1
- `590394e` — fix: downgrade torch for linux arm64
- `8cc28d2` — bump 1.16.0
- `6a64f95` — fix: support dict fields in FormDepends
- `c02d9f1` — feat: experimental client SDK
- `ee62133` — chore: updated dependencies

Наблюдения:
- `6a64f95` (fix FormDepends dict fields) — это именно тот путь, через который прокси отправляет `picture_description_api=<json>`. Изменение релевантно, надо учитывать при парсинге наших параметров.
- Активных изменений в `orchestrator_factory.py` / `settings.py` / `app.py` за последние минорки не видно — наш основной код-фронт относительно стабилен.

### 6.4. Стратегия синхронизации с upstream (подтверждение правил из CLAUDE.md)

- **Ветки:**
  - `main` — наша рабочая, все `[fork]`-коммиты сюда сразу (см. CLAUDE.md разд. 9).
  - `upstream-tracking` — еженедельный merge из upstream/main. **Создать ещё не создана** ни в одном из двух форков. Это должно быть сделано первым действием в TASK_02 (или отдельным оргкоммитом).
- **Rebase-политика:** при переходе на новую upstream-версию каждый `[fork]`-коммит пересматривается вручную. Если соответствующий upstream-issue закрыт — наш патч удаляется. Это прямое указание из CLAUDE.md разд. 6.2.
- **Коммиты:** префикс `[fork]`, русскоязычные сообщения, conventional format. Пример: `[fork] perf: убрать барьер между батчами в picture_description_api`.
- **`CHANGELOG_FORK.md`:** обязателен по CLAUDE.md разд. 6.1, **сейчас отсутствует** в обоих репо. Создать пустой в начале TASK_02 с записью о старте форка.

### 6.5. Открытые вопросы по версионной стратегии

1. **Когда впервые rebase'иться на upstream?** Варианты: (a) сразу после TASK_02 — свежая база, но пока патчей мало, риск отдавать время; (b) после TASK_05 — когда основные фиксы готовы, тестируется в проде; (c) по расписанию раз в N недель. Рекомендация: (c), раз в 2 недели, не раньше чем после TASK_03.
2. **Что делать, если upstream выпустит 2.89.0 с собственным фиксом `page_batch_concurrency`?** Откатить наш патч и использовать upstream. Это явное указание в CLAUDE.md 6.2 — наш фикс удаляется.
3. **`docling-jobkit` — форкать или нет?** Решение отложено. Пока — sed-патч. Если придётся менять ещё что-то в jobkit — форкать.

---

## 7. Предлагаемая последовательность работ

Порядок выстроен по принципу «сначала то, что даёт максимум эффекта при минимуме риска, потом остальное». Ни одна из задач ниже **не** начинается до явного одобрения инженером.

### TASK_02 — Инфраструктура форка и benchmark-пакет

**Что делаем:** организационные шаги, без которых продуктивная работа невозможна.

- Создать ветку `upstream-tracking` в обоих репо (`docling`, `docling-serve`).
- Создать `CHANGELOG_FORK.md` в обоих репо с начальной записью (версия форка, дата, причина).
- Создать каталог `benchmarks/` в `docling-serve` с набором эталонных документов (см. раздел 9).
- Создать скрипт `benchmarks/run.py` — прогоняет набор через локально поднятый docling-serve, замеряет время, сохраняет результаты в `benchmarks/results/YYYY-MM-DD_HH-MM.json`.
- Зафиксировать baseline на нашей текущей конфигурации (без патчей) — это референсная точка для всех последующих измерений.

**Сложность:** S–M. **Эффект:** без этого нельзя доказать, что патчи работают. Ноль эффекта в проде, но разблокирует всё последующее.

### TASK_03 — Убрать барьер в picture_description_api (главная боль)

**Что делаем:** переход с паттерна «executor per batch» на паттерн «persistent semaphore + submit». Точки вмешательства:

- `PictureDescriptionApiModel._annotate_images`: вместо создания ThreadPoolExecutor на каждый batch — переход на единый долгоживущий пул и семафор, который живёт дольше, чем один batch. Варианты реализации обсуждаются в TASK_03 (не в этом отчёте).
- Альтернатива: рефакторинг на `asyncio.Semaphore` + `asyncio.gather`, но это рвёт синхронный API модели. Менее предпочтительно.
- Bubble-up: возможно, нужно править и `base_pipeline._enrich_document`, чтобы не форсировать `# Must exhaust!` между батчами. Это более инвазивный фикс, но и более чистый.

**Сложность:** **M**. **Ожидаемый эффект:** ускорение **до ×3–×4** на документах с 50+ картинок. На эталонном 207/84: с 13 мин до 3–5 мин.

**Риск rebase:** высокий — `base_pipeline.py` активно меняется в upstream. Плюс — фикс должен быть минимально-инвазивным (точечные изменения, а не переписывание файла).

### TASK_04 — Нативные фиксы 4 известных багов

**Что делаем:** переносим workaround'ы из прокси в код форка, где это даёт выигрыш.

- **Баг 1 (jobkit worker.py:126):** попытка №1 — PR в upstream docling-jobkit. Если не принимают — форкать jobkit.
- **Баг 2 (Pillow crop out of image):** клэмп координат в `vlm_pipeline.py:306-315`. 1 коммит, 5 строк.
- **Баг 3 (do_picture_description при VLM pipeline):** скипать инициализацию picture description model в `VlmPipeline.__init__`. Аккуратно, чтобы не сломать случай, когда оба реально нужны (edge case).
- **Баг 4:** **решение — не фиксить**, оставить как есть (см. раздел 3.2).

После этих фиксов прокси можно упростить (убрать sed-патч в Dockerfile, убрать принудительное `image_export_mode=placeholder`, убрать подавление `do_picture_description`). Это **отдельный коммит** в прокси, не в форке.

**Сложность:** **S**. **Эффект:** упрощение прокси, меньше sed-патчей в Dockerfile, меньше хрупких мест.

### TASK_05 — Env vars для VLM defaults (закрыть issue #463)

**Что делаем:** добавить env vars в `docling_serve/settings.py`:

- `DOCLING_SERVE_DEFAULT_VLM_API_URL`
- `DOCLING_SERVE_DEFAULT_VLM_API_KEY`
- `DOCLING_SERVE_DEFAULT_VLM_MODEL`
- `DOCLING_SERVE_DEFAULT_VLM_CONCURRENCY`
- `DOCLING_SERVE_DEFAULT_VLM_TIMEOUT`
- `DOCLING_SERVE_DEFAULT_VLM_PROMPT`

Значения применяются как дефолты в `ConvertDocumentsRequestOptions` при отсутствии per-request параметров.

**Эффект:** можно вынести конфигурацию VLM из прокси в env docker-compose. Прокси перестаёт быть «единой точкой знаний про VLM», знания уходят в образ docling-serve, как и должно быть.

**Сложность:** **S**. **Эффект:** архитектурная чистота, плюс закрытие upstream issue (возможно, primary PR).

### TASK_06 — Настройка thread-safety в проде (без кода)

**Что делаем:** *конфигурационный* эксперимент, а не патч. Меняем `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1`, `UVICORN_WORKERS=4` (вместо 2×2). Измеряем по benchmark-пакету: (a) корректность; (b) производительность. Если деградации нет — закрываем риск thread-safety переключением профиля prod.

**Сложность:** S (это docker-compose change, не код). **Эффект:** закрытие риска #2285 без форк-патча.

### TASK_07 — Абстракция recognition backend (подготовка к OCR SDK)

**Что делаем:** рефакторинг, описанный в разделе 5.3: ввод `RecognitionBackend` как Protocol, перевод `PictureDescriptionApiModel` на него как адаптер, **без** добавления реализации для OCR SDK. Это делает архитектуру готовой, но не подключает нового backend.

**Сложность:** **M**. **Эффект:** разблокирует будущий TASK_NN по подключению OCR SDK. Сам по себе — нулевой runtime effect.

**Порядок:** после TASK_03 (главная боль), TASK_04 (баги), TASK_05 (env vars). То есть не раньше чем через 3–4 итерации.

### TASK_08 — Параллелизация picture_classification (опционально)

**Что делаем:** оценка, можно ли ускорить `DocumentPictureClassifier` за счёт GPU или путём отключения, если в наших документах он не даёт value.

**У нас сейчас:** classification отключена на уровне прокси (`DEFAULT_VLM_CONCURRENCY` через `build_custom_model`, но classification устанавливается из параметра `classification` и в текущей конфигурации не активна). Поэтому **вероятно, эта задача нам не нужна**. Отмечаем как «низкий приоритет, только если появится потребность».

**Сложность:** **L**. **Эффект:** −1–3 минуты на документ с 80+ картинок, **только если мы включим** classification.

### TASK_09 — Подключение OCR SDK как второго recognition backend (долгосрочная цель)

**Что делаем:** реализация `OcrSdkBackend(RecognitionBackend)`, клиент к `10.121.3.201:9996`. Это долгосрочная архитектурная цель, упомянутая в CLAUDE.md разд. 5. **Не в ближайшем плане.**

**Сложность:** **L**. **Эффект:** потенциальный приближение к эталону 1–1.5 мин на документах с большим числом картинок.

### Порядок в виде списка

```
TASK_02 ─ инфраструктура форка и benchmark-пакет          [S-M]
   │
TASK_03 ─ убрать барьер picture_description_api            [M] ← главная боль
   │
TASK_04 ─ нативные фиксы 4 багов                            [S]
   │
TASK_05 ─ env vars для VLM defaults                        [S]
   │
TASK_06 ─ конфиг-эксперимент workers 4x1 vs 2x2            [S]
   │
TASK_07 ─ абстракция recognition backend                    [M]
   │
TASK_08 ─ picture_classification (опционально)              [L, low prio]
   │
TASK_09 ─ OCR SDK как recognition backend                   [L, долгосрочно]
```

**Важно:** между каждой задачей — пауза для согласования (CLAUDE.md разд. 10). Переход к TASK_03 возможен только после одобрения TASK_02 результатов.

---

## 8. Открытые вопросы для инженера

Каждый вопрос — с вариантами ответа. Без этих решений TASK_02 стартовать преждевременно.

### Q1. Подтверждаешь ли ты диагноз «барьер в `_enrich_document` + `executor per batch` = волны»?

Вариант A. Да, диагноз правдоподобен, идём в TASK_03.
Вариант B. Сомневаюсь — перед TASK_03 нужно снять профиль с прода (py-spy / cProfile на реальном 207/84 документе), чтобы подтвердить, где тратится время.
Вариант C. Нет, есть другая гипотеза — хочу её обсудить.

**Моя рекомендация:** B — чем раньше мы закроем неопределённость, тем меньше риск потратить TASK_03 на фикс не в той точке. Профилирование делается **локально**, не на проде (см. CLAUDE.md 11 «не запускать профилирование на проде без согласования»).

### Q2. `elements_batch_size` — 16 или 8?

По `docling/datamodel/settings.py:33-35` — 16. По отчёту Explore-агента из `pipeline_options.py:599` — 8 (`PictureDescriptionBaseOptions.batch_size`). Не ясно, какое значение выигрывает в runtime для `PictureDescriptionApiModel`.

Вариант A. Не важно, пойму при TASK_02 при чтении кода.
Вариант B. Нужно уточнить сейчас — это может изменить расчёт «сколько волн мы видим».

**Моя рекомендация:** A. Точное значение влияет только на размер волн, не на механизм, а проверим при реализации.

### Q3. Приемлема ли рекомендация переключить prod на `UVICORN_WORKERS=4`, `DOCLING_SERVE_ENG_LOC_NUM_WORKERS=1`?

Это TASK_06. Изменение конфигурации, без кода, но касается **production-сервера** и должно быть согласовано.

Вариант A. Да, делаем сразу после TASK_02 (benchmark), до TASK_03. Это закрывает риск thread-safety и может повлиять на результаты бенчмарка.
Вариант B. Нет, оставляем 2×2 — есть операционные причины (лимиты по памяти на CPU-only образе: 4 процесса × копии моделей?).
Вариант C. Откладываем до TASK_06 как и планировалось.

**Моя рекомендация:** A — иначе baseline бенчмарка будет «жить на фоне гонок» и результаты будут шумными.

### Q4. `docling-jobkit` — форкать или нет?

Сейчас патчим sed'ом в Dockerfile (`worker.py:126`). Если придётся менять в jobkit ещё что-то (например, в local orchestrator поведение «one task done → next task»), sed масштабироваться не будет.

Вариант A. Пока sed, форкать только если появится вторая причина.
Вариант B. Сразу форкать — это маленький репозиторий, минимальный overhead, перестаём зависеть от внешних релизов.
Вариант C. Отправить PR в upstream и ждать.

**Моя рекомендация:** A на ближайшие несколько TASK, C — параллельно. Если в течение месяца upstream не реагирует — переходим на B.

### Q5. Прокси (`docling-proxy`) — оставляем как есть или тоже трогаем?

По CLAUDE.md docling-proxy разд. 3 — «не меняй код этого репозитория в рамках задач форка». Но после TASK_04 часть workaround'ов станет избыточной. Вопрос:

Вариант A. После TASK_04 отдельным коммитом в docling-proxy упрощаем (убираем принудительный `image_export_mode=placeholder`, убираем подавление `do_picture_description`, убираем sed-патч в Dockerfile). Это **отдельная** инициатива, не часть TASK_04.
Вариант B. Оставляем прокси как есть навсегда — он будет страховкой.
Вариант C. Откладываем до общего пересмотра архитектуры (TASK_09+).

**Моя рекомендация:** A, но с задержкой — через 2–3 недели после TASK_04 в проде, чтобы убедиться, что нативный фикс стабилен.

### Q6. Какая версия upstream — цель для форка?

Сейчас форк на `docling 2.88.0` / `docling-serve 1.16.1`. Это были head при начале форка.

Вариант A. Фиксируемся на этих версиях, rebase раз в 2 недели.
Вариант B. Сразу подтягиваем свежий upstream main (если есть 2.89 / 1.17) — чтобы не отставать.
Вариант C. Фиксируемся на последнем stable релизе (не main), rebase только по новым релизам.

**Моя рекомендация:** C — это даёт предсказуемость. Форк на main делает каждый rebase непредсказуемым по объёму. Если нет сильных причин использовать свежее main — остановиться на тегах.

### Q7. OCR SDK как backend — когда начинать?

Вариант A. Сразу после TASK_03 (главная боль) — потому что это наша стратегическая цель.
Вариант B. После того как убедимся, что форк-патчи дают ×3–×4 и всё равно этого недостаточно.
Вариант C. Только когда появится реальный use-case, который не закрывается форк-патчами.

**Моя рекомендация:** B. Стратегическая цель важна, но она требует отдельного большого рефакторинга. Сначала посмотрим, сколько можно выжать из минимальных патчей — возможно, ×3–×4 будет достаточно для всех текущих сценариев кроме экзотических.

### Q8. Набор эталонных документов для бенчмарков — где брать?

См. раздел 9. Вопрос: можешь ли ты предоставить анонимизированный набор из продовых документов (5–10 штук, разных типов), или нужно собирать синтетический?

Вариант A. Дам реальные документы (анонимизированные, без PII), кладём в `benchmarks/fixtures/`.
Вариант B. Не могу, нужен синтетический набор — придётся генерировать.
Вариант C. Используем только тот документ 207/84, который уже есть на эталонном стенде.

**Моя рекомендация:** A, минимум 5 документов разных типов. Синтетика не ловит реальные корнер-кейсы.

---

## 9. Эталонный набор документов для бенчмарков

### 9.1. Цель

Без воспроизводимого бенчмарка мы не сможем:
- доказать, что TASK_03 действительно даёт ×3–×4;
- обнаружить регрессию, когда TASK_04 нативный фикс сломает что-то в edge case;
- сравнить разные варианты реализации (Вариант A vs. Вариант B в TASK_03);
- зафиксировать baseline «до форка».

Каждое утверждение вида «кажется, стало быстрее» в этом проекте **должно** опираться на числа из бенчмарка (CLAUDE.md разд. 6.3).

### 9.2. Структура каталога `benchmarks/` в docling-serve

```
benchmarks/
├── README.md                 # как запустить, как интерпретировать
├── fixtures/                 # эталонные документы (в git; анонимизированные)
│   ├── pdf_text_small/       # до 20 стр., мало картинок
│   ├── pdf_text_large/       # 100-300 стр., 50+ картинок (наш главный кейс)
│   ├── pdf_scan_small/       # скан, до 20 стр.
│   ├── pdf_scan_large/       # скан, 100+ стр.
│   ├── docx_clean/           # обычный DOCX
│   ├── docx_with_ole/        # DOCX с MathType формулами (OLE)
│   ├── xlsx/                 # таблицы
│   ├── pptx/                 # презентации
│   ├── html_confluence/      # .doc Confluence export (MIME HTML)
│   └── edge_cases/           # корнер-кейсы: embedded images, empty pages, etc.
├── run.py                    # основной скрипт прогона
├── compare.py                # сравнение двух прогонов (до/после патча)
├── results/                  # JSON-выходы по прогонам
│   └── YYYY-MM-DD_HH-MM_<label>.json
└── plot.py                   # (опционально) графики времени по документам
```

`fixtures/` хранится в git. Ограничение по размеру: не более ~50 МБ на папку в сумме. Для больших файлов — отдельный s3/nexus, скрипт `run.py` подтягивает по чек-листу.

### 9.3. Какие документы нужны

Минимальный набор (5 документов, **обязательно** до TASK_03):

1. **PDF text, 207 стр., 84 картинки** — наш эталонный проблемный документ. На нём baseline 13 мин, цель TASK_03 — ≤5 мин.
2. **PDF text, 20 стр., 0 картинок** — быстрый baseline, должен остаться быстрым (регрессионный).
3. **PDF scan, 30 стр.** — тестирует VLM pipeline full-page OCR.
4. **DOCX с OLE formulas** — проверяет, что прокси + Gotenberg остаются работоспособными.
5. **Edge case: PDF embedded images** — тестирует фикс бага №2 (Pillow crop).

Расширенный набор (всего 10-12, нужен к TASK_05+):

6. PDF text, 50 стр., 200+ картинок (тяжёлый case по картинкам)
7. PDF text, 300 стр., 0 картинок (тяжёлый case по pages)
8. XLSX с большими таблицами (прокси отправляет в xlrd — проверка пути)
9. PPTX с картинками
10. HTML (Confluence export)
11. Документ на русском с формулами KaTeX (проверка пост-обработки прокси)
12. Малый PDF 1 стр. (sanity check on low end)

### 9.4. Метрики для каждого прогона

На документ:
- `total_time_ms` — wall-clock всего запроса (как видит клиент).
- `docling_request_ms` — время внутри docling (по логам прокси).
- `queue_wait_ms` — время ожидания семафора в прокси.
- `pages_processed` — число страниц.
- `pictures_described` — число картинок, прошедших описание.
- `vlm_requests_count` — сколько HTTP-запросов ушло в VLM backend (по логам SGLang или litellm).
- `vlm_concurrent_max` — пиковое число параллельных запросов к VLM (снимается из метрик SGLang/LiteLLM).
- `vlm_idle_intervals` — количество и длительность промежутков, когда к VLM 0 активных запросов, но документ ещё не обработан (это прямое измерение «волн»).
- `md_content_bytes` — размер итогового markdown (proxy для проверки корректности: не должно сильно измениться от baseline).
- `status` — HTTP-код.
- `errors` — список ошибок из ответа docling.

На прогон (агрегат по всему набору):
- `total_runtime_sec` — суммарное wall-clock прогона всего набора.
- `avg_vlm_concurrent_max` — средний пик параллелизма.
- `total_vlm_idle_time_sec` — суммарное время простоя VLM (главная метрика для TASK_03).
- `regressions` — список документов, где `md_content_bytes` изменился более чем на ±5% относительно baseline.

### 9.5. Baseline и целевые метрики

**Baseline (до форка, на текущей конфигурации 2×2):**
- Документ №1 (207/84): `total_time_ms ≈ 780000` (13 мин), `total_vlm_idle_time_sec ≈ 360` (6 мин), `vlm_concurrent_max ≈ 16`.
- Документ №2 (20/0): не измерен, но должен быть < 30 сек.
- Документ №3 (scan 30): не измерен, но должен быть 2–4 мин.

Baseline фиксируется **первым же прогоном** в TASK_02 и сохраняется в `benchmarks/results/baseline.json`.

**Целевые метрики после TASK_03:**
- Документ №1: `total_time_ms ≤ 300000` (5 мин), `total_vlm_idle_time_sec ≤ 60` (1 мин), `vlm_concurrent_max ≥ 16` (≥ заявленного concurrency).
- Документ №2: `total_time_ms ≤ baseline × 1.05` (никакой регрессии на простых документах).
- Документ №3: `total_time_ms ≤ baseline × 1.05` (то же самое для VLM pipeline — TASK_03 фиксит только picture_description_api, VLM pipeline не трогает).
- Документ №4 (DOCX OLE): не должен сломаться (ещё работает).
- Документ №5 (embedded images): **после TASK_04** должен работать **без** принудительного `image_export_mode=placeholder` в прокси.

### 9.6. Как запускается

Скрипт `benchmarks/run.py` (конкретная имплементация — в TASK_02):

```
python benchmarks/run.py \
    --target http://localhost:5005 \
    --fixtures benchmarks/fixtures \
    --label before_task03 \
    --out benchmarks/results/
```

Требования:
- Docling-serve должен быть запущен локально (не на проде) в том же Docker-образе, что в проде.
- `docling-proxy` поднят параллельно, потому что мы меряем **поведение всей цепочки**, как видит пользователь.
- VLM backend — либо реальный SGLang (если доступен), либо мок с искусственной задержкой, имитирующей реальные ~10 сек на запрос.

**Один прогон занимает ~30–60 минут** (baseline 13 мин на главном документе × ещё ~15 мин на остальные). Это **не** то, что запускается на каждый коммит; это запускается **руками** до и после каждой крупной задачи и на релиз-кандидате.

### 9.7. Куда складывать результаты

В `benchmarks/results/` в самом репо `docling-serve` (под git). Это позволяет при rebase на upstream автоматически видеть, что мы измеряли. Размер JSON'ов маленький, в истории можно отслеживать, как наши изменения влияют на числа.

Вспомогательный `benchmarks/compare.py` — принимает два JSON (до/после) и выводит дифф. Удобно для PR-описаний.

---

## Приложение A. Артефакты анализа

- `CLAUDE.md` (docling-serve, docling, docling-proxy) — контекст проекта.
- `TASKS/TASK_01_analysis.md` — техзадание на анализ.
- Git-состояние форков зафиксировано в разделе 6.
- Все ссылки на код даны в формате `path/to/file.py:line`, версия — upstream docling 2.88.0 / docling-serve 1.16.1.
