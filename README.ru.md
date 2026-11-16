# `andrewich.ai` — текст → JSON локальной LLM

Ansible-коллекция: модуль и lookup-плагин, которые превращают неструктурированный текст в JSON
по заданной схеме с помощью маленькой GGUF-модели на контроллере. Инференс локальный, данные не
покидают хост. Единственный сетевой контакт — однократное скачивание GGUF-модели по HTTPS, и
только если явно задан `model_url`.

```
текст + промпт + схема ──► GBNF-грамматика ──► llama.cpp (350M) ──► JSON ──► проверки ──► факты
```

Модель по умолчанию — `LFM2-350M-Extract` (220 МБ). Ничего про Redfish, логи nginx или тикеты
в коде не зашито: домен — это промпт-файл плюс схема.

## Установка

```bash
# из git (ветка по умолчанию)
ansible-galaxy collection install git+https://github.com/Andrewich/ansible-collection-ai.git

# закреплённый релиз, рекомендуется для продакшена
ansible-galaxy collection install git+https://github.com/Andrewich/ansible-collection-ai.git,0.4.0
```

`requirements.yml`:

```yaml
collections:
  - name: https://github.com/Andrewich/ansible-collection-ai.git
    type: git
    version: 0.4.0
```

Затем `ansible-galaxy collection install -r requirements.yml`.

У самой коллекции нет зависимостей, кроме `ansible-core >= 2.15`. Для бэкенда `python` нужен
`llama-cpp-python` в том интерпретаторе, который запускает модуль (обычно контроллер):

```bash
python3 -m venv .venv
.venv/bin/pip install "llama-cpp-python==0.3.37" --only-binary=:all: \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
.venv/bin/pip install ansible-core
```

Колёса важны на старых дистрибутивах: предсобранные бинарники `llama.cpp` требуют
`GLIBCXX_3.4.30` и на CentOS Stream 9 не запускаются, а колесо собрано под manylinux.

## Быстрый старт

```yaml
- name: Redfish payload -> факты
  andrewich.ai.llm_extract:
    text: "{{ payload.content }}"
    prompt_file: prompts/redfish_extract.txt
    fields: "{{ redfish_fields }}"   # список элементов name/type/description
    model_url: https://huggingface.co/LiquidAI/LFM2-350M-Extract-GGUF/resolve/main/LFM2-350M-Extract-Q4_K_M.gguf
    model_sha256: 687a31c3e7864647aa181e1feb156e4e5da33978c174d7dbf0d289f6014a5621
    strict_download: true
  delegate_to: localhost
  register: extracted

- name: Действовать по фактам
  debug:
    msg: "{{ extracted.ansible_facts.llm_result.serial }}"
```

То же самое в шаблоне, без facts и без `delegate_to`:

```yaml
- name: Разобрать ошибку nginx
  set_fact:
    parsed: >-
      {{ lookup('andrewich.ai.llm_extract', payload,
                prompt_file='prompts/nginx_error_classify.txt',
                json_schema=nginx_schema) }}
```

Примеры в [`playbooks/`](playbooks/) покрывают три домена одним модулем (Redfish, логи nginx,
тикет поддержки) плюс нативный фильтр компакции Redfish. Разница — только промпт-файл и схема.

## Что гарантирует модуль

| Свойство | Как обеспечено |
|---|---|
| JSON заданной формы | GBNF-грамматика из схемы: лишний ключ или строка вместо числа невозможны |
| «Не знаю» выразимо | у каждого nullable-листа грамматики есть альтернатива `null`; отсутствующие значения попадают в `llm.missing` |
| Выдумки не проходят молча | grounding по границам токенов в том чанке, который выдал значение; `on_ungrounded: drop` (по умолчанию) удаляет ненайденное |
| Данные не покидают хост | инференс локальный; `server`-бэкенд принимает только loopback, редиректы и прокси запрещены |
| Модель можно не качать вручную | `model_url` скачивает GGUF один раз, атомарно, под файловой блокировкой |
| Mock не просочится в прод | `backend: mock` требует `allow_mock: true` |
| Enum нормализуется | `Dell Inc.` → `Dell`, `Hewlett Packard Enterprise` → `HPE`, по вхождению, без доменных таблиц |
| Повторный прогон дешёвый | кэш по модели, промпту, схеме, входу и политикам; политики применяются и к кэшу |
| Секреты не утекают | всё локально, кэш 0700/0600, `cache: false` для чувствительного входа |

Метаданные `llm` — часть контракта: `missing`, `unverified`, `dropped`, `checked`, `grounded`,
`tokens`, `cache_hit`, `normalized`, `rejected`, хэши промпта/схемы/входа и
`model.download` (`fresh` / `cached` / `skipped` / `false`).

## Скачивание модели

Если задан `model_url`, `prepare()` скачивает модель до инференса:

- только HTTPS и только `backend: python` (серверный бэкенд загружает модель сам);
- локальное имя `<sha256(url)[:16]>-<basename>.gguf` в `model_dir`
  (по умолчанию `<cache_dir>/models`), поэтому одинаковые basename не конфликтуют;
- файл пишется потоково чанками ~1 МБ в `mkstemp` → `fsync` → `os.replace`; первые четыре байта
  обязаны быть `GGUF`, SHA-256 считается на лету;
- параллельные задачи сериализуются lock-файлом и `fcntl.flock`: при `forks > 1` качает один
  процесс, остальные ждут;
- `model_sha256` настоятельно рекомендуется; без него защищают только магия GGUF и
  `Content-Length` (модуль пишет предупреждение), а `strict_download: true` без хеша — ошибка
  `model_checksum_required`;
- `check_mode` ничего не скачивает и возвращает план.

> **Первый запуск на большом парке.** Модель ~220 МБ. Если её нет на контроллере, первый
> плейбук скачает её один раз, но при `forks: 50` десятки задач встанут в очередь на блокировке,
> и общее время вырастет. Сделайте отдельный прогревающий play с `throttle: 1` (или один
> `andrewich.ai.llm_extract` на `localhost`), а рабочие прогоны запускайте после него — все
> задачи увидят готовый файл.

## Бэкенды

| Бэкенд | Как | Когда |
|---|---|---|
| `python` (по умолчанию) | `llama-cpp-python` в процессе модуля | до нескольких десятков хостов; зависимостей нет, кроме колеса |
| `server` | OpenAI-совместимый API, грамматика в теле запроса | много хостов, модель горячая, общий процесс |
| `mock` | канированный ответ | CI и тесты, только с `allow_mock: true` |

Сервер поднимается **без бинарников llama.cpp**:

```bash
./scripts/run-llama-server.sh --port 8080
# или systemd: scripts/llama-server.service
```

## Структура репозитория

```
galaxy.yml                 метаданные коллекции (namespace andrewich, name ai)
meta/runtime.yml           requires_ansible
README.md / README.ru.md   этот файл, английский и русский
docs/contract.md           полный контракт (параметры, ошибки, политики, замеры)
docs/contract.ru.md        то же по-русски
plugins/module_utils/      llm_core.py — грамматика, проверки, кэш, бэкенды
plugins/modules/           llm_extract.py — тонкая обёртка AnsibleModule
plugins/lookup/            llm_extract.py — то же ядро для шаблонов
prompts/                   инструкции для модели, версионируются
schemas/                   JSON Schema для вложенных структур
fixtures/                  реальные payload-ы (Dell, HPE, Inspur, Huawei, Fujitsu)
playbooks/                 сквозные примеры
tests/                     unit-тесты, без сети и без модели
scripts/                   скачивание модели, llama-server, сборка и установка
```

## Разработка

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*.py'   # 190 тестов
./scripts/build-and-install.sh                                   # сборка + установка в ./.collections
./scripts/run-playbook.sh playbooks/introspect_llm_extract.yml   # сквозная проверка, реальная модель + mock
```

`ansible.cfg` указывает `collections_paths` на `./.collections`, поэтому плейбуки всегда
используют установленную коллекцию — ровно так же, как у пользователя.

## Замеры

2 vCPU, без GPU, LFM2-350M-Extract Q4_K_M:

| Сценарий | Результат |
|---|---|
| 294 символа сжатого текста, 10 полей | 10/10 фактов, 492 in / 131 out токена, ~6 с |
| тот же текст повторно | `cache_hit: true`, `attempts: 0`, ~0.01 с |
| документ без 8 из 10 полей | 2 факта; 8 выдуманных значений поймано и удалено, `missing` заполнен |
| `backend: server` (`python -m llama_cpp.server`) | те же факты, ~7.4 с |
| `on_too_long: split`, 1.2 КБ, 2 чанка | те же 10 фактов, ~14 с |
| скачивание модели, 219 МБ | 2.7 с, `download: fresh`, затем `cached` |

Важное из исследования: **350M на этой задаче слаба без посторонней помощи** (~47 % полей),
поэтому и появились grounding, `on_ungrounded` и строгий бюджет ввода. Компакция входа — снаружи
модуля и в виде «ключ = значение»: длинные JSON-пути сбивают модель (`Oem.Dell...` → `vendor:
"Oem.Dell"`), короткие ключи дают 10/10.

## Границы доверия и оговорки

- Выход — только JSON. YAML/XML/свободный текст не поддерживаются: грамматика их не гарантирует.
- Модуль скачивает только модель и ничего другого; он ничего не исполняет. Результат — данные:
  не подставляйте его в `shell`/`command` без `assert` по схеме и значениям.
- Промпт и схема — доверенные артефакты; документ недоверенный и никогда не исполняется.
- Детерминированного слоя (алиасы/JSON-пути) нет: `aliases` в `fields` отвергается с
  `schema_invalid`. Этот шаг — до вызова модуля.
- Проекция фактов отличается от envelope: `null` и ungrounded-значения удалены, поэтому факты
  валидируются по `projection_schema`, а не по исходной схеме (см. [контракт](docs/contract.ru.md), §5).
- 350M годится для 0.5–1.5 КБ чистого текста; больше — `on_too_long: split` или модель 1.2B
  (93 % полей без посторонней помощи, но в 3 раза медленнее).
- `ansible-test sanity` не прогонялся (нужны `pylint`/`voluptuous`); структура коллекции готова.

## Лицензия

GPL-3.0-or-later. См. [LICENSE](LICENSE).
