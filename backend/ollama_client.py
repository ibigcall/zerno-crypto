"""Тонкий клиент локальной Ollama.

Используем /api/chat со structured outputs (`format` = JSON Schema): модель
обязана вернуть валидный JSON нужной формы, поэтому разбор ответа не превращается
в угадывание. Reasoning-модели (deepseek-r1 и родственные) кладут размышления в
поле `thinking` или в теги <think>…</think> — их вырезаем.
"""
import json
import logging
import re

import requests

from . import config

log = logging.getLogger("zerno.ollama")

_THINK_RE = re.compile(r"<think>.*?</think>", re.S | re.I)
_FENCE_RE = re.compile(r"^```(?:json)?\s*|\s*```$", re.S)


class OllamaError(RuntimeError):
    pass


def available():
    """Модель поднята и нужный тег скачан?"""
    try:
        resp = requests.get(f"{config.OLLAMA_URL}/api/tags", timeout=5)
        resp.raise_for_status()
        tags = [m.get("name", "") for m in resp.json().get("models", [])]
    except (requests.RequestException, ValueError) as exc:
        log.warning("Ollama недоступна: %s", exc)
        return False, []
    return True, tags


def model_ready():
    ok, tags = available()
    if not ok:
        return False
    if not tags:
        return True
    wanted = config.OLLAMA_MODEL
    return any(t == wanted or t.split(":")[0] == wanted.split(":")[0] for t in tags)


def chat_json(system, user, schema, model=None, temperature=0.25):
    """Один запрос к модели, ответ — словарь по схеме."""
    model = model or config.OLLAMA_MODEL
    body = {
        "model": model,
        "stream": False,
        "format": schema,
        "options": {
            "temperature": temperature,
            "num_ctx": config.OLLAMA_NUM_CTX,
        },
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    try:
        resp = requests.post(
            f"{config.OLLAMA_URL}/api/chat", json=body, timeout=config.OLLAMA_TIMEOUT
        )
        resp.raise_for_status()
        payload = resp.json()
    except requests.RequestException as exc:
        raise OllamaError(f"Ollama {model}: {exc}") from exc
    except ValueError as exc:
        raise OllamaError(f"Ollama {model}: некорректный ответ ({exc})") from exc

    content = ((payload.get("message") or {}).get("content") or "").strip()
    if not content:
        raise OllamaError(f"Ollama {model}: пустой ответ")
    content = _FENCE_RE.sub("", _THINK_RE.sub("", content)).strip()
    # если модель всё же дописала текст вокруг JSON — берём первый объект
    if not content.startswith("{"):
        start, end = content.find("{"), content.rfind("}")
        if start == -1 or end <= start:
            raise OllamaError(f"Ollama {model}: в ответе нет JSON")
        content = content[start:end + 1]
    try:
        data = json.loads(content)
    except json.JSONDecodeError as exc:
        raise OllamaError(f"Ollama {model}: JSON не разобрался ({exc})") from exc
    if not isinstance(data, dict):
        raise OllamaError(f"Ollama {model}: ожидался объект")
    return data
