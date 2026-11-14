# local.llm

Универсальный модуль и lookup-плагин: неструктурированный текст + промпт + схема → JSON,
с помощью локальной GGUF-модели. Без внешних сервисов, без выхода данных наружу.

```yaml
- name: facts
  local.llm.llm_extract:
    text: "{{ payload }}"
    prompt_file: prompts/redfish_extract.txt
    fields: "{{ fields }}"
    model_path: /opt/llm/models/LFM2-350M-Extract-Q4_K_M.gguf

- name: значение в шаблоне, без фактов
  set_fact:
    parsed: "{{ lookup('local.llm.llm_extract', payload, prompt_file=..., fields=...) }}"
```

Ядро лежит в `plugins/module_utils/llm_core.py` и общее для модуля, lookup и тестов.
Параметры и коды ошибок — `../../../docs/contract.md`.
