# Контракт `local.llm.llm_extract`

Статус: версия ядра **2.1**, коллекция **0.3.0**. Закрыты P0/P1 ревью и находки,
которые ревью не выявило (§15). Часть замечаний сознательно вне объёма (§16).

Задача: неструктурированный текст + промпт + схема → JSON. Всё доменное (Redfish, логи,
тикеты) живёт в промптах, схемах и плейбуках, а не в коде.

## 1. Форма поставки

```
ansible_collections/local/llm/
  galaxy.yml, meta/runtime.yml
  plugins/module_utils/llm_core.py    # всё ядро: грамматика, проверки, кэш, бэкенды
  plugins/modules/llm_extract.py      # тонкая обёртка: DOCUMENTATION + AnsibleModule
  plugins/lookup/llm_extract.py       # то же ядро для шаблонов
```

```yaml
- local.llm.llm_extract:            # факты хоста
- set_fact:
    parsed: "{{ lookup('local.llm.llm_extract', payload, prompt_file=..., fields=...) }}"
```

## 2. Конвейер

```
text|src ─► prepare ─► [для каждого чанка: GBNF ─► backend ─► envelope]
                                   │
                    finalize_chunks: grounding по своему чанку
                                   ▼
                          merge ─► on_ungrounded ─► projection ─► facts
```

- **envelope** — валидированные данные модели (с `null`), ещё без политик; именно он
  кэшируется.
- **projection** — то, что уходит в факты: без `null`, без ungrounded-значений.

## 3. Параметры

| Параметр | Тип | Дефолт | Примечание |
|---|---|---|---|
| `text` | raw | — | str или dict/list (Ansible парсит JSON) → сериализуется обратно |
| `src` | path | — | читается **байтами**, UTF-8 strict, без перевода строк |
| `prompt_file` / `prompt` | path / str | — | файл предпочтительнее |
| `fields` | list[dict] | — | только `name/type/description/enum`; `aliases` → `schema_invalid` |
| `json_schema` | dict | — | вложенные структуры, см. §4 |
| `result_key` | str | `llm_result` | |
| `model_path` / `model_id` | path / str | — / basename | |
| `backend` | str | `python` | `python` / `server` / `mock` |
| `server_url` | str | `http://127.0.0.1:8080` | **только loopback**, без редиректов и прокси |
| `max_input_chars` | int ≥ 1 | `1200` | |
| `on_too_long` | str | `fail` | `fail` / `truncate` / `split` |
| `merge` | str | `first` | см. таблицу §7 |
| `on_missing` | str | `allow` | `allow` / `fail` |
| `on_invalid` | str | `retry` | `retry` / `fail` / `return_raw` |
| `grounding` | str | `substring` | `substring` / `none` |
| `on_ungrounded` | str | **`drop`** | `drop` / `keep` / `fail` |
| `min_grounded` | float 0..1 | `0.0` | при `checked == 0` и пороге > 0 — отказ |
| `strict_enum` | bool | `false` | |
| `cache` / `cache_dir` | bool / path | `true` / `~/.cache/ansible-llm` | |
| `temperature` ≥ 0 / `seed` | float / int | `0.0` / `0` | |
| `max_tokens` ≥ 1 | int | `320` | повтор удваивает |
| `retries` ≥ 0 | int | `1` | попытка N: `temperature+0.1`, `max_tokens×2`, `seed+1` |
| `n_ctx` ≥ 1 / `n_threads` ≥ 0 / `timeout` ≥ 1 | int | `4096` / `0` / `120` | |
| `mock_response` | str | — | только вместе с `allow_mock` |
| `allow_mock` | bool | `false` | без него `backend: mock` → `mock_not_allowed` |
| `debug` | bool | `false` | `raw_output`, `projection_schema` |

Некорректные значения (типы, диапазоны, `choices`, взаимоисключения, неизвестные ключи в
lookup) дают `invalid_params` до любого инференса.

## 4. Схема: что разрешено

| Правило | Поведение |
|---|---|
| `enum` | только на `string` и на `items` строкового массива; иначе `schema_unsupported` |
| `enum: [null]` | означает «значение обязано быть null» |
| `maxLength` / `maxItems` | целые ≥ 0; `0` — валидный лимит; > 120 / > 8 — `schema_unsupported` |
| корень схемы | обязан быть объектом, иначе `schema_unsupported` в preflight |
| `required` | отсутствующие ключи недопустимы; не-`required` свойства nullable |
| пустой массив | сохраняется как `[]`; в `missing` попадает только `null` |

GBNF строится из схемы: лимиты строки и массива берутся из `maxLength`/`maxItems`
(с потолком 120/8), ключи и enum-литералы экранируются как JSON-строки.
`strict_enum` включает жёсткие литералы, по умолчанию выключен.

## 5. Три контракта результата

| Объект | Гарантия |
|---|---|
| envelope | проходит `validate_instance(envelope, schema)`, включая `required` и `null` |
| projection (`ansible_facts.llm_result`) | без `null` и без ungrounded-значений; `required` ослаблены, схема — `projection_schema()` |
| метаданные `llm` | `missing`, `unverified`, `dropped`, `checked`, `grounded`, `rejected`, `normalized`, `coerced` |

То есть «факты валидны по исходной схеме» больше не утверждается: исходная схема
валидирует envelope, а проекция описывается `projection_schema` (доступна при `debug`).

## 6. Grounding: честная семантика

Проверка отвечает на вопрос «встречается ли значение в **том чанке, который его выдал**»,
токен-ограниченно (границы `[0-9A-Za-zА-Яа-яЁё_]`), с учётом значения до нормализации.

| Было | Стало |
|---|---|
| `On` подтверждалось словом `Only` | `On` ≠ `Only` |
| `536` подтверждалось `1536` | `536` ≠ `1536` |
| enum не проверялся | проверяется; `HPE` в документе про Dell — ungrounded |
| `Dell Inc.` в массиве не нормализовался | нормализуется по item-правилам |
| хвост при `truncate` «подтверждал» значения | хвост в модель не попал — доказательством не является |
| чанк 2 не мог спасти значение из чанка 1 | «заземлённый» кандидат выигрывает при merge |

Grounding **не** привязывает значение к конкретному полю записи (это вне объёма, §16):
он проверяет присутствие в отправленном тексте. `checked`/`grounded` — честный знаменатель,
boolean и `null` в него не входят.

## 7. Слияние чанков

| Режим | Скаляры | Списки |
|---|---|---|
| `first` | первый grounded non-null | первый список |
| `concat` | первый grounded non-null | обычная конкатенация, дубликаты сохраняются |
| `deep_merge` | первый grounded non-null | конкатенация с дедупликацией, словари рекурсивно |

## 8. Политика `on_ungrounded`

| Режим | Поведение |
|---|---|
| `drop` (по умолчанию) | ненайденные значения удаляются, попадают в `missing` и `llm.dropped` |
| `keep` | возвращаются, помечаются в `llm.unverified` |
| `fail` | ошибка `ungrounded_values` |

## 9. Кэш

Envelope-формат v2: `{v: 2, chunks: [envelope, ...]}` — по одному на чанк, с `data`
(валидированные данные с `null`), `missing`, `normalized`, `coerced`, `rejected`.
На попадании выполняется **тот же** `finalize_chunks`, что и для свежего ответа, поэтому
`min_grounded`, `on_missing`, `on_ungrounded` применяются заново.

- Ключ: версия ядра, модель (id + размер + mtime), backend, `server_url`, хэши промпта/схемы/
  входа, `temperature`, `seed`, `max_tokens`, `strict_enum`, `on_too_long`, `merge`,
  `on_missing`, `grounding`, `on_ungrounded`, `max_input_chars`, `min_grounded`, формат кэша.
- Запись: `mkstemp` в каталоге кэша (0600), `fsync`, атомарный `os.replace`; каталог должен
  принадлежать euid и быть без group/world-write, иначе кэш отключается.
- Чтение: неверная версия, форма или данные, не проходящие схему, — промах.
- `check_mode`: инференс выполняется, кэш не пишется.

## 10. Бэкенды

| Бэкенд | Как | Ограничение |
|---|---|---|
| `python` | `llama-cpp-python` в процессе | грузит модель на задачу |
| `server` | OpenAI-совместимый API, грамматика в теле | **только loopback**, без прокси и редиректов |
| `mock` | канированный ответ | только с `allow_mock: true` |

## 11. Результат

```yaml
ansible_facts:
  llm_result: { ... projection ... }
llm:
  ok: true
  model: {id, path, size}
  prompt_sha256 / schema_sha256 / input_sha256
  input_chars: 294
  backend: python
  cache_hit: false
  check_mode: false
  attempts: 1
  chunks: 1
  tokens: {in: 492, out: 131}
  elapsed_ms: 5600
  missing: []
  unverified: []
  dropped: 0
  checked: 9
  grounded: 9
```

При `ok: false` модуль **не возвращает** `ansible_facts` (не затирает прежнее значение),
а lookup всегда бросает ошибку.

## 12. Ошибки

`invalid_params`, `input_empty`, `input_not_found`, `input_unreadable`, `input_encoding`,
`input_too_long`, `prompt_not_found`, `schema_invalid`, `schema_unsupported`,
`model_not_found`, `backend_unavailable`, `invalid_server_url`, `mock_not_allowed`,
`invalid_json`, `schema_violation`, `missing_fields`, `grounding_failed`,
`ungrounded_values`, `timeout`.

## 13. Проверка

```
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
→ Ran 159 tests ... OK

./scripts/run-playbook.sh playbooks/introspect_llm_extract.yml
→ PLAY RECAP: ok=20 failed=0 ignored=2
```

Замеры (2 vCPU, LFM2-350M-Extract Q4_K_M):

| Сценарий | Результат |
|---|---|
| сжатый текст 294 символа, 10 полей | 10/10, 492 in / 131 out, ~5.6 с |
| документ без 8 полей | 2 факта, 8 выдуманных поймано и удалено |
| повторный прогон | `cache_hit: true`, `attempts: 0` |
| `on_too_long: split`, 2 чанка | значения заземляются по своим чанкам |

## 14. Границы доверия

- Промпт и схема — **доверенные артефакты**: они попадают в системное сообщение.
- Документ — **недоверенный**: он в пользовательском сообщении, результат никогда не
  исполняется. Грамматика ограничивает форму, но не смысл: результат нельзя подставлять в
  `shell`/`command`/`uri` без `assert` по схеме и значениям.
- Дефолт `on_ungrounded: drop` и `allow_mock: false` выбраны так, чтобы случайная
  конфигурация не производила выдуманные факты.

## 15. Что закрыто

| # | Замечание | Исправление |
|---|---|---|
| P0-1 | enum-отмывание (`Only → On`) | нормализация по границам токенов, только единственное совпадение |
| P0-2 | кэш обходил `min_grounded` | envelope v2 + общий `finalize_chunks`; ключ дополнен |
| P0-4 | enum у не-строк, нулевые лимиты | таблица «ключевое слово × тип», `is not None`, лимиты из схемы |
| P0-5 | небезопасная запись кэша | mkstemp/0600, проверка каталога, валидация записи |
| P0-6 | egress через `server_url` | только loopback, без прокреди и редиректов |
| P1-1 | терялись null-пути в `missing` | объединение множеств |
| P1-2 | пустые массивы исчезали; корень | `[]` сохраняется, корень обязан быть объектом |
| P1-5 | merge до grounding | grounding по чанку, grounded-кандидат выигрывает |
| P1-6 | слабая валидация, зависание | единый `validate_params`, `max_input_chars ≥ 1` |
| P1-8 | `src` не байт-в-байт | чтение байтами, новые коды ошибок |
| P1-9 | `return_raw`/факты | `raw_output` всегда, факты не пишутся при `ok=false` |
| P1-10 | проекция vs схема | два контракта, `projection_schema()` |
| P0-3 | «field-attributable» | **вне объёма**: формулировка означает присутствие в чанке |
| — | `backend: mock` в проде | `allow_mock` по умолчанию `false` |
| — | enum в массивах не проверялся | проверяется и нормализуется по item-правилам |
| — | нулевой знаменатель = 1.0 | при `min_grounded > 0` и `checked == 0` — отказ |

## 16. Вне объёма (осознанно)

P0-3 (привязка значения к полю записи), P1-3 (хеш содержимого GGUF), P1-4 (коллизии путей
`a.b`), P1-7 (жёсткий timeout в процессе), P2-1 (best-of-N), P2-4 (полный Unicode),
P2-5 (инъекция через схему — схема считается доверенной), P3 (упаковка в Galaxy).
