# Контракт `local.llm.llm_extract`

Статус: **P0–P2 ревью исправлены**, форма поставки — коллекция.

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
    ...
- set_fact:                        # значение в шаблоне, без delegate_to и без facts
    parsed: "{{ lookup('local.llm.llm_extract', payload, prompt_file=..., fields=...) }}"
```

`collections_paths = ./` в `ansible.cfg`. Ядро не импортирует Ansible, поэтому модуль, lookup
и тесты используют один и тот же код.

## 2. Конвейер

```
text|src ─► prepare ─► GBNF ─► backend ─► coerce ─► enum ─► validate ─► strip null
              │                                                            │
              └─ cache (ключ + гейты) ◄── facts ◄── on_ungrounded ◄── ground ┘
```

Сообщения: **system** = промпт из файла + компактный список полей (не JSON Schema);
**user** = сам документ. Инструкция и данные не смешиваются.

## 3. Параметры

| Параметр | Тип | Дефолт | Примечание |
|---|---|---|---|
| `text` | raw | — | str или dict/list (Ansible парсит JSON) → сериализуется обратно |
| `src` | path | — | файл, байт-в-байт |
| `prompt_file` | path | — | основной способ |
| `prompt` | str | — | инлайн, для тестов |
| `fields` | list[dict] | — | только `name/type/description/enum`; `aliases` → ошибка |
| `json_schema` | dict | — | вложенные структуры |
| `result_key` | str | `llm_result` | |
| `model_path` | path | — | для `backend: python` |
| `model_id` | str | basename | в кэш-ключе и метаданных |
| `backend` | str | `python` | `python` / `server` / `mock` |
| `server_url` | str | `http://127.0.0.1:8080` | `llama-server` или `python -m llama_cpp.server` |
| `max_input_chars` | int | `1200` | бюджет под 350M |
| `on_too_long` | str | `fail` | `fail` / `truncate` / `split` |
| `merge` | str | `first` | `first` / `concat` / `deep_merge` |
| `on_missing` | str | `allow` | `allow` / `fail` |
| `on_invalid` | str | `retry` | `retry` / `fail` / `return_raw` |
| `grounding` | str | `substring` | `substring` / `none` |
| `on_ungrounded` | str | **`drop`** | `drop` / `keep` / `fail` |
| `min_grounded` | float | `0.0` | |
| `strict_enum` | bool | `false` | |
| `cache` / `cache_dir` | bool / path | `true` / `~/.cache/ansible-llm` | 0700, файлы 0600 |
| `temperature` / `seed` | float / int | `0.0` / `0` | |
| `max_tokens` | int | `320` | повтор удваивает |
| `retries` | int | `1` | доп. попытки, только при `on_invalid: retry` |
| `n_ctx` / `n_threads` / `timeout` | int | `4096` / `0` / `120` | |
| `mock_response` | str | — | для `backend: mock` |
| `debug` | bool | `false` | `raw_output` в метаданных |

## 4. Null и необязательные поля

- `fields` → все ключи `required`, но каждый лист `["<тип>", "null"]`.
- Грамматика генерирует `n1 ::= null | string` для каждого nullable-листа. **Модель может
  сказать «не знаю»**, иначе она обязана выдумать значение.
- Свойства вне `required` необязательны: в грамматике они получают `null`-альтернативу, а
  `validate_instance` разрешает их отсутствие. Грамматика и валидатор согласованы.
- `strip_nulls` убирает `null` и складывает пути в `llm.missing`.

## 5. Грамматика GBNF

- Ключи и enum-значения — JSON-строки вместе с кавычками (`"\"vendor\""`); иначе модель
  выдаёт `{vendor: ...}` и это не JSON.
- Строка ≤120 символов, массив ≤8 элементов — иначе 350M уходит в повтор.
- `strict_enum: false` по умолчанию: жёсткий enum заставляет модель молчать там, где значение
  не совпадает буквально.

## 6. Grounding: «в этом поле», а не «где-то в тексте»

| Было | Стало |
|---|---|
| `"On"` подтверждался словом `Only` | поиск по границам токенов: `On` ≠ `Only` |
| `536` подтверждался `1536` | границы цифр: `536` ≠ `1536` |
| enum-поля не проверялись вообще | проверяются, включая значение до нормализации (`Dell Inc.`) |
| boolean попадал в знаменатель | boolean не проверяется и в знаменатель не входит |

`llm.checked` и `llm.grounded` показывают честный знаменатель. Производные метки
(`upstream_timeout` при тексте `upstream timed out`) grounding честно помечает как
ненайденные — для классификации используют `grounding: none` или `on_ungrounded: keep`.

## 7. Политика `on_ungrounded`

350M выдумывает правдоподобные значения, когда документ молчит: на тексте из двух строк
(`SerialNumber`, `Memory`) она вернула `vendor: Dell`, `bios_version: "1.2"`,
`cpu_model: "N/A"`, `cpu_cores_total: 16`, `power_state: On`, `health: OK`.

| Режим | Поведение |
|---|---|
| `drop` (по умолчанию) | значения, не найденные в тексте, удаляются, попадают в `missing` и `llm.dropped` |
| `keep` | возвращаются, помечаются в `llm.unverified` |
| `fail` | ошибка `ungrounded_values` |

Измерено на том же тексте: `dropped: 8`, в фактах остаются только `serial` и `memory_gib`.

## 8. Кэш

Ключ: `version ‖ model_id ‖ backend ‖ server_url ‖ prompt_sha ‖ schema_sha ‖ input_sha ‖
temperature ‖ seed ‖ max_tokens ‖ strict_enum ‖ on_too_long ‖ merge ‖ on_missing ‖ grounding ‖
on_ungrounded ‖ max_input_chars ‖ размер и mtime модели`.

- Два разных сервера с разными моделями больше не делят ключ.
- На попадании в кэш заново применяются `on_missing`, `on_ungrounded`, `min_grounded` —
  жёсткий порог нельзя обойти закэшированным результатом.
- Каталог 0700, файлы 0600; для секретов — `cache: false`.

## 9. Повторы

`retries` — дополнительные попытки при `on_invalid: retry`. Каждая следующая: `temperature
+ 0.1`, `max_tokens × 2`, `seed + 1`. Иначе при `temperature: 0` и фиксированном seed повторы
байт-в-байт идентичны, и «best of N» — фикция (`test_retry_changes_temperature_and_budget`).

## 10. Промпт

Вместо дословной JSON Schema — компактный список:

```
- vendor (string; one of: Dell, HPE, Lenovo, Supermicro, Inspur, Huawei)  // Server manufacturer
- memory_gib (integer)  // Total installed RAM in GiB
```

Замер на эталонной задаче: **658 → 492 входных токена (−25 %)**, качество не изменилось.
Заметки после `//` помечены в инструкции как описание поля, «не значение» — иначе 350M
копирует описание в ответ (и такой ответ отсекается как ungrounded).

## 11. Результат

```yaml
ansible_facts:
  llm_result: { ... }
llm:
  ok: true
  model: {id, path, size}
  prompt_sha256 / schema_sha256 / input_sha256
  input_chars: 294
  backend: python
  cache_hit: false
  attempts: 1
  chunks: 1
  tokens: {in: 492, out: 131}
  elapsed_ms: 6134
  missing: []
  unverified: []
  coerced: []
  normalized: [{path: vendor, from: "Dell Inc.", to: Dell}]
  rejected: []
  checked: 9
  grounded: 9
  dropped: 0
```

## 12. Ошибки

`input_empty`, `input_too_long`, `prompt_not_found`, `schema_invalid`, `schema_unsupported`,
`model_not_found`, `backend_unavailable`, `invalid_json`, `schema_violation`, `missing_fields`,
`grounding_failed`, `ungrounded_values`, `timeout`.

## 13. Проверка

```
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
→ Ran 80 tests ... OK

./scripts/run-playbook.sh playbooks/introspect_llm_extract.yml
→ PLAY RECAP: ok=20 failed=0 ignored=2
```

Замеры (2 vCPU, LFM2-350M-Extract Q4_K_M):

| Сценарий | Результат |
|---|---|
| сжатый текст, 294 символа | 10/10 фактов, 492 in / 131 out токена, ~6.1 с |
| текст без 8 полей | 2 факта, 8 выдуманных поймано и удалено, `missing` из 8 путей |
| повторный прогон | `cache_hit: true`, `attempts: 0` |
| `backend: server` (`llama_cpp.server`) | те же факты, ~7.4 с |

Плейбук проверяет: отказ по бюджету, реальное извлечение, нормализацию enum, кэш, вложенную
схему, классификацию с `grounding: none`, `schema_violation`, документ с отсутствующими
полями и lookup-плагин.

## 14. Границы

- Не-JSON выходы, сбор данных по сети, исполнение действий — не дело модуля.
- Детерминированного слоя (алиасы/JSON-пути) **нет**: `aliases` в `fields` отвергается с
  `schema_invalid`, в `vars/redfish_fields.yml` их нет. Такую логику можно добавить как
  отдельный шаг до вызова модуля.
- `ansible-test sanity` требует установленных `pylint`/`voluptuous`; структура коллекции и
  `meta/runtime.yml` для него готовы, сам прогон — отдельная задача CI.

## 15. Что закрыто из ревью

| # | Замечание | Исправление |
|---|---|---|
| 1 | `null` недостижим в грамматике | `n1 ::= null \| string`, опциональные поля тоже nullable |
| 2 | grounding пропускает enum и подстроки | границы токенов, enum + значение до нормализации, честный знаменатель |
| 3 | кэш обходит `min_grounded`, ключ неполон | ключ дополнен, гейты применяются на попадании |
| 4 | `retries` не меняет результат | температура +0.1, бюджет ×2, seed +1 |
| 5 | `aliases` игнорируются молча | удалены из `vars/`, в `fields` — ошибка `schema_invalid` |
| 6 | `required` игнорируется генератором | опциональные свойства nullable в грамматике и в валидации |
| 7 | 68 % промпта — схема | компактный список полей, −25 % токенов |
| 8 | нет коллекции и lookup | `ansible_collections/local/llm`, ядро в `module_utils`, добавлен lookup |
