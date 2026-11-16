# `local.llm` — текст → JSON локальной LLM

Ansible-коллекция: модуль и lookup-плагин, которые превращают неструктурированный текст
в JSON по заданной схеме с помощью маленькой GGUF-модели, запущенной локально.
Инференс выполняется локально, данные не покидают контроллер. Единственный сетевой контакт —
однократное скачивание GGUF-модели по HTTPS, и только если явно задан `model_url`.

```
текст + промпт + схема ──► GBNF-грамматика ──► llama.cpp (350M) ──► JSON ──► проверки ──► факты
```

Модель по умолчанию — `LFM2-350M-Extract` (220 МБ), запускается на контроллере.

## Быстрый старт

```bash
# 1. модель
./scripts/fetch_model.sh

# 2. окружение
python3 -m venv .venv
.venv/bin/pip install -r scripts/requirements.txt --only-binary=:all: \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
.venv/bin/pip install ansible-core

# 3. проверка: 8 задач, реальная модель + mock, без BMC
./scripts/run-playbook.sh playbooks/introspect_llm_extract.yml

# 4. тесты
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'
```

`inventory.ini` содержит `ansible_python_interpreter` и `llm_model` — поправьте под себя.

## Использование

```yaml
- name: redfish -> facts
  local.llm.llm_extract:
    text: "{{ payload.content }}"
    prompt_file: prompts/redfish_extract.txt
    fields: "{{ lookup('file', 'vars/redfish_fields.yml') | from_yaml }}"
    model_path: /opt/llm/models/LFM2-350M-Extract-Q4_K_M.gguf
  delegate_to: localhost
  register: extracted

- name: действовать по фактам
  debug:
    msg: "{{ extracted.ansible_facts.llm_result.serial }}"
```

```yaml
- name: то же самое в шаблоне, без facts и delegate_to
  set_fact:
    parsed: >-
      {{ lookup('local.llm.llm_extract', payload,
                prompt_file='prompts/nginx_error_classify.txt',
                json_schema=nginx_schema) }}
```

Три домена в [playbooks/introspect_llm_extract.yml](playbooks/introspect_llm_extract.yml)
используют один и тот же модуль: Redfish, лог nginx, тикет поддержки. Разница — только
промпт-файл и схема.

## Что гарантирует модуль

| Свойство | Как обеспечено |
|---|---|
| Валидный JSON заданной формы | GBNF-грамматика из схемы: лишний ключ или строка вместо числа невозможны |
| «Не знаю» выразимо | каждый nullable-лист грамматики имеет альтернативу `null`; отсутствующие значения попадают в `llm.missing` |
| Выдумки не проходят молча | grounding по границам токенов в том чанке, который выдал значение; `on_ungrounded: drop` удаляет ненайденные |
| Данные не уходят с хоста | `server`-бэкенд принимает только loopback; инференс локальный |
| Модель можно не качать вручную | `model_url` скачивает GGUF один раз, атомарно и под lock |
| Mock не просочится в прод | `backend: mock` работает только с `allow_mock: true` |
| Enum нормализуется | `Dell Inc.` → `Dell`, `Hewlett Packard Enterprise` → `HPE` (по вхождению, без доменных таблиц) |
| Повторный прогон дешёвый | кэш по хэшу модели, промпта, схемы, входа и политик; проверки применяются и к кэшу |
| Секреты не утекают | всё локально, кэш 0700/0600, `cache: false` для чувствительного |

Метаданные ответа (`llm`) — часть контракта: `missing`, `unverified`, `dropped`, `checked`,
`grounded`, `tokens`, `cache_hit`, `normalized`, `rejected`, хэши промпта/схемы/входа.
По ним видно, откуда взялось каждое значение.

## Замеры

2 vCPU, без GPU, LFM2-350M-Extract Q4_K_M:

| Сценарий | Результат |
|---|---|
| 294 символа сжатого текста, 10 полей | 10/10 фактов, 492 in / 131 out токена, ~6.1 с |
| тот же текст повторно | `cache_hit: true`, `attempts: 0`, ~0.01 с |
| документ без 8 из 10 полей | 2 факта; 8 выдуманных значений поймано и удалено, `missing` заполнен |
| `backend: server` (`python -m llama_cpp.server`) | те же факты, ~7.4 с |
| `on_too_long: split`, 1.2 КБ, 2 чанка | те же 10 фактов, ~14.4 с |

Важное из исследования: **350M без посторонней помощи на этой задаче слаба** (~47 % полей),
поэтому и появились grounding, `on_ungrounded` и строгий бюджет ввода. Компакция входа — снаружи
модуля и в виде «ключ = значение»: длинные JSON-пути сбивают модель (`Oem.Dell...` → `vendor:
"Oem.Dell"`), короткие ключи дают 10/10.

## Скачивание модели

Модель не обязательно класть на диск руками:

```yaml
- local.llm.llm_extract:
    text: "{{ payload }}"
    prompt_file: prompts/redfish_extract.txt
    fields: "{{ fields }}"
    model_url: https://huggingface.co/LiquidAI/LFM2-350M-Extract-GGUF/resolve/main/LFM2-350M-Extract-Q4_K_M.gguf
    model_sha256: 687a31c3e7864647aa181e1feb156e4e5da33978c174d7dbf0d289f6014a5621
    strict_download: true
```

- только `https` и только `backend: python` (серверный бэкенд загружает модель сам);
- файл кладётся в `model_dir` (по умолчанию `<cache_dir>/models`) под именем
  `<sha256(url)[:16]>-<basename>.gguf`, поэтому одинаковые basename не конфликтуют;
- запись потоковая (чанки ~1 МБ) в `mkstemp` → `fsync` → `os.replace`, первые 4 байта
  обязаны быть `GGUF`, hash считается на лету;
- параллельные задачи сериализуются lock-файлом и `flock`, так что при `forks > 1`
  качает один процесс, остальные ждут;
- `model_sha256` настоятельно рекомендуется: без него остаются только магия GGUF и
  `Content-Length` (модуль пишет предупреждение), а `strict_download: true` без хеша — отказ.

> **Первый запуск на большом парке.** Модель 219 МБ. Если её нет на контроллере, первый
> плейбук скачает её один раз, но при `forks: 50` десятки задач одновременно встанут в
> ожидание блокировки, а суммарное время вырастет. Сделайте отдельный прогревающий play с
> `throttle: 1` (или одним `local.llm.llm_extract` на `localhost`), а рабочие прогоны
> запускайте после него — тогда все задачи увидят готовый файл.

## Бэкенды

| Бэкенд | Как | Когда |
|---|---|---|
| `python` (по умолчанию) | `llama-cpp-python` в процессе модуля | до нескольких десятков хостов; ноль зависимостей кроме колеса |
| `server` | OpenAI-совместимый API, грамматика в теле запроса | много хостов, модель горячая, общий процесс |
| `mock` | канированный ответ | CI и тесты |

Сервер поднимается **без бинарников llama.cpp**:

```bash
./scripts/run-llama-server.sh --port 8080
# или systemd: scripts/llama-server.service
```

Это важно для старых дистрибутивов: предсобранный `llama.cpp` требует `GLIBCXX_3.4.30`
(на CentOS Stream 9 не запускается), а колесо `llama-cpp-python` собрано под manylinux и работает.

## Структура

```
ansible_collections/local/llm/          коллекция (galaxy.yml, meta/runtime.yml)
  plugins/module_utils/llm_core.py      ядро: грамматика, проверки, кэш, бэкенды
  plugins/modules/llm_extract.py        модуль (тонкая обёртка)
  plugins/lookup/llm_extract.py         lookup поверх того же ядра
  plugins/filter/redfish_identity.py    компакция Redfish в "путь = значение" (без awk/jq)
docs/contract.md                        контракт: параметры, ошибки, политики, замеры
prompts/*.txt                           инструкции для модели (версионируются)
schemas/*.json                          JSON Schema для вложенных структур
vars/redfish_fields.yml                 схема фактов Redfish (домен, не модуль)
fixtures/                               «грязные» payload-ы Dell / HPE / Inspur
playbooks/introspect_llm_extract.yml    сквозная проверка: 8 задач, реальная модель + mock
playbooks/redfish_identity.yml          пример: vendor/model/CPU из реальных Huawei и Fujitsu
tests/test_*.py                         159 тестов: контракт, конвейер, регрессии
scripts/                                модель, сервер, запуск плейбука, requirements
```

## Границы и оговорки

- Выход — только JSON. YAML/XML/свободный текст не поддерживаются: грамматика их не гарантирует.
- Модуль ничего не качает и ничего не исполняет. Вход — `uri`, `command`, `slurp`, lookup.
  Результат — данные: не подставляйте его в `shell`/`command` без проверки по схеме.
- Детерминированного слоя (алиасы/JSON-пути) нет: `aliases` в `fields` отвергается с
  `schema_invalid`. Такой слой — отдельный шаг до вызова модуля.
- Проекция фактов отличается от envelope: `null` и ungrounded-значения удалены, поэтому факты
  валидируются по `projection_schema`, а не по исходной схеме (см. `docs/contract.md` §5).
- 350M годится для 0.5–1.5 КБ чистого текста; больше — `on_too_long: split` или модель 1.2B
  (проверено: 93 % полей без посторонней помощи, но в 3 раза медленнее).
- `ansible-test sanity` не прогонялся: нужны `pylint`/`voluptuous`. Структура коллекции готова.
