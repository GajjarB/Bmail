"""Ollama client and hardware tiering.

Deliberately stdlib-only: this is a handful of JSON POSTs to 127.0.0.1, and
adding an HTTP dependency to the core for it would be silly.

Two things this module guarantees:

* the host is local by default and a non-local host must be opted into
  explicitly, because "local-first" has to be enforced, not just documented;
* generation is schema-constrained via Ollama's `format` parameter, so the
  output is always valid JSON — which is a guarantee about *shape*, never about
  *truth*. Value checking happens in `sift.llm.stage3`.
"""

from __future__ import annotations

import json
import os
import platform
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from sift.errors import LLMError

DEFAULT_HOST = "http://127.0.0.1:11434"
_LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1", "0:0:0:0:0:0:0:1"}


@dataclass(slots=True)
class HardwareTier:
    name: str
    ram_gb: float
    model: str
    note: str


# Ordered from most to least capable. First entry whose RAM requirement is met wins.
TIERS: tuple[tuple[float, HardwareTier], ...] = (
    (30.0, HardwareTier("heavy", 32, "mistral-small:24b",
                        "strongest schema adherence; ~20 GB at Q4")),
    (15.0, HardwareTier("standard", 16, "qwen3:8b",
                        "the default: best quality per watt for this task")),
    (7.0, HardwareTier("light", 8, "qwen3:4b",
                       "~3 GB, workable for classify and extract on CPU")),
    (0.0, HardwareTier("minimal", 4, "qwen3:1.7b",
                       "last resort; expect noticeably weaker German")),
)


def total_ram_gb() -> float:
    """Best-effort physical memory, without adding a dependency for it."""
    try:
        if hasattr(os, "sysconf") and "SC_PAGE_SIZE" in os.sysconf_names:
            pages = os.sysconf("SC_PHYS_PAGES")
            page_size = os.sysconf("SC_PAGE_SIZE")
            if pages > 0 and page_size > 0:
                return round(pages * page_size / 1024**3, 1)
    except (ValueError, OSError):
        pass
    if platform.system() == "Darwin":  # pragma: no cover - platform specific
        try:
            output = subprocess.run(
                ["sysctl", "-n", "hw.memsize"], capture_output=True, text=True, timeout=5
            )
            return round(int(output.stdout.strip()) / 1024**3, 1)
        except (OSError, ValueError, subprocess.SubprocessError):
            pass
    if platform.system() == "Windows":  # pragma: no cover - platform specific
        try:
            import ctypes

            class _Status(ctypes.Structure):
                _fields_ = [
                    ("dwLength", ctypes.c_ulong), ("dwMemoryLoad", ctypes.c_ulong),
                    ("ullTotalPhys", ctypes.c_ulonglong), ("ullAvailPhys", ctypes.c_ulonglong),
                    ("ullTotalPageFile", ctypes.c_ulonglong),
                    ("ullAvailPageFile", ctypes.c_ulonglong),
                    ("ullTotalVirtual", ctypes.c_ulonglong),
                    ("ullAvailVirtual", ctypes.c_ulonglong),
                    ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
                ]

            status = _Status()
            status.dwLength = ctypes.sizeof(_Status)
            ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status))
            return round(status.ullTotalPhys / 1024**3, 1)
        except Exception:
            pass
    return 8.0


def detect_tier(ram_gb: float | None = None) -> HardwareTier:
    """Pick a model tier from available memory. Overridable in config."""
    ram = ram_gb if ram_gb is not None else total_ram_gb()
    for minimum, tier in TIERS:
        if ram >= minimum:
            return HardwareTier(tier.name, ram, tier.model, tier.note)
    return TIERS[-1][1]


def has_gpu() -> bool:
    """Rough check, used only for the progress estimate shown to the user."""
    if shutil.which("nvidia-smi"):
        return True
    if platform.system() == "Darwin" and platform.machine() in ("arm64", "aarch64"):
        return True
    return bool(os.environ.get("HIP_VISIBLE_DEVICES") or os.environ.get("CUDA_VISIBLE_DEVICES"))


class OllamaClient:
    """Minimal Ollama client: chat with a JSON schema, embeddings, health check."""

    def __init__(
        self,
        host: str = DEFAULT_HOST,
        *,
        timeout: float = 180.0,
        allow_remote: bool = False,
    ) -> None:
        self.host = host.rstrip("/")
        self.timeout = timeout
        hostname = urlparse(self.host).hostname or ""
        if hostname not in _LOCAL_HOSTS and not allow_remote:
            raise LLMError(
                f"refusing to send mail content to non-local host '{hostname}'. "
                "Set llm.allow_remote = true in the config if you really mean it."
            )

    # -- transport ------------------------------------------------------------- #

    def _post(self, path: str, payload: dict[str, Any]) -> dict[str, Any]:
        request = urllib.request.Request(
            f"{self.host}{path}",
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:400]
            raise LLMError(f"Ollama returned HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise LLMError(
                f"cannot reach Ollama at {self.host} ({exc.reason}). "
                "Start it with `ollama serve`, or run with --no-llm."
            ) from exc
        except TimeoutError as exc:
            raise LLMError(f"Ollama timed out after {self.timeout:.0f}s") from exc

    # -- capabilities ----------------------------------------------------------- #

    def available(self) -> bool:
        try:
            self.list_models()
            return True
        except LLMError:
            return False

    def list_models(self) -> list[str]:
        request = urllib.request.Request(f"{self.host}/api/tags", method="GET")
        try:
            with urllib.request.urlopen(request, timeout=min(self.timeout, 10)) as response:
                data = json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            raise LLMError(f"cannot reach Ollama at {self.host}: {exc}") from exc
        return [entry.get("name", "") for entry in data.get("models", [])]

    def has_model(self, model: str) -> bool:
        names = self.list_models()
        base = model.split(":")[0]
        return any(name == model or name.split(":")[0] == base for name in names)

    # -- generation -------------------------------------------------------------- #

    def chat_json(
        self,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        *,
        temperature: float = 0.0,
        num_ctx: int = 4096,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        """One schema-constrained turn. Returns (parsed object, timing metadata).

        `format=<JSON Schema>` makes Ollama constrain decoding to the grammar, so
        the reply parses every time. That is a guarantee about validity only:
        a well-formed `"deadline": "not mentioned"` is still wrong, which is why
        the caller validates values afterwards.
        """
        started = time.perf_counter()
        payload = {
            "model": model,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            "format": schema,
            "stream": False,
            "options": {"temperature": temperature, "num_ctx": num_ctx},
        }
        response = self._post("/api/chat", payload)
        content = (response.get("message") or {}).get("content", "")
        meta = {
            "latency_ms": int((time.perf_counter() - started) * 1000),
            "eval_count": response.get("eval_count", 0),
            "prompt_eval_count": response.get("prompt_eval_count", 0),
            "model": model,
        }
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError:
            # Constrained decoding should make this unreachable; a stray code
            # fence from an older server is the one case seen in practice.
            match = re.search(r"\{.*\}", content, re.DOTALL)
            if not match:
                raise LLMError(f"model returned non-JSON: {content[:200]!r}") from None
            try:
                parsed = json.loads(match.group(0))
            except json.JSONDecodeError as exc:
                raise LLMError(f"model returned non-JSON: {content[:200]!r}") from exc
        if not isinstance(parsed, dict):
            raise LLMError(f"model returned {type(parsed).__name__}, expected an object")
        return parsed, meta

    def embed(self, model: str, text: str) -> list[float]:
        response = self._post("/api/embeddings", {"model": model, "prompt": text})
        vector = response.get("embedding")
        if not isinstance(vector, list):
            raise LLMError(f"model '{model}' did not return an embedding")
        return [float(value) for value in vector]

    def pull_hint(self, model: str) -> str:
        return f"ollama pull {model}"
