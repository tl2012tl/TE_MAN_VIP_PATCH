from __future__ import annotations

import importlib
import importlib.abc
import importlib.machinery
import importlib.util
import logging
import os
import sys
from pathlib import Path

from comfy_execution.utils import get_executing_context


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def _worker_count() -> int:
    raw = str(os.getenv("TE_MAN_CONCURRENT_WORKERS", "0")).strip()
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def _feature_enabled() -> bool:
    return _worker_count() > 0 or _env_bool("TE_MAN_CONCURRENT_ENABLED", False)


_BOOT_MARK = "TE_MAN_VIP_PATCH_BOOT_V3"
_PATCH_APPLIED = False
_PATCH_HOOK = None


def _patch_prompt_server_legacy_accessors() -> None:
    try:
        import server
    except Exception:
        return

    prompt_server_cls = getattr(server, "PromptServer", None)
    if prompt_server_cls is None:
        return

    def _get_last_node_id(self):
        executing_context = get_executing_context()
        if executing_context is not None and getattr(executing_context, "node_id", None):
            return executing_context.node_id
        value = getattr(self, "_legacy_last_node_id", None)
        return value or ""

    def _set_last_node_id(self, value):
        self._legacy_last_node_id = value

    prompt_server_cls.last_node_id = property(_get_last_node_id, _set_last_node_id)


def _find_runtime_module_path() -> Path:
    base_dir = Path(__file__).resolve().parent
    runtime_name = "te_man_concurrent_patch"

    for suffix in (".pyd", ".so", ".py"):
        direct_path = base_dir / f"{runtime_name}{suffix}"
        if direct_path.exists():
            return direct_path

    raise FileNotFoundError(runtime_name)


def _load_module_from_path(module_path: Path):
    module_name = "te_man_concurrent_patch"
    module_dir = str(module_path.parent)
    if module_dir not in sys.path:
        sys.path.insert(0, module_dir)

    if module_path.suffix.lower() in {".pyd", ".so"}:
        sys.modules.pop(module_name, None)
        return importlib.import_module(module_name)

    spec = importlib.util.spec_from_file_location(module_name, module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"unable to load patch module: {module_path}")

    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


def _load_patch_module():
    module_path = _find_runtime_module_path()
    return _load_module_from_path(module_path)


def _apply_patch_module_once():
    global _PATCH_APPLIED
    if _PATCH_APPLIED:
        return
    patch_module = _load_patch_module()
    patch_module.apply_patch()
    _patch_prompt_server_legacy_accessors()
    _PATCH_APPLIED = True


class _PostExecutionImportLoader(importlib.abc.Loader):
    def __init__(self, wrapped_loader):
        self._wrapped_loader = wrapped_loader

    def create_module(self, spec):
        create_module = getattr(self._wrapped_loader, "create_module", None)
        if create_module is not None:
            return create_module(spec)
        return None

    def exec_module(self, module):
        self._wrapped_loader.exec_module(module)
        try:
            _apply_patch_module_once()
        except Exception as exc:
            logging.exception("[TE MAN VIP PATCH] failed to apply delayed patch: %s", exc)
        finally:
            try:
                if _PATCH_HOOK in sys.meta_path:
                    sys.meta_path.remove(_PATCH_HOOK)
            except Exception:
                pass


class _PostExecutionImportFinder(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname != "execution":
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path)
        if spec is None or spec.loader is None:
            return None
        spec.loader = _PostExecutionImportLoader(spec.loader)
        return spec


def _install_delayed_patch_hook():
    global _PATCH_HOOK
    execution_module = sys.modules.get("execution")
    if getattr(execution_module, "PromptQueue", None) is not None:
        _apply_patch_module_once()
        return
    if _PATCH_HOOK is None:
        _PATCH_HOOK = _PostExecutionImportFinder()
        sys.meta_path.insert(0, _PATCH_HOOK)


try:
    if _feature_enabled():
        logging.warning("[%s] TE MAN 并发核心激活", _BOOT_MARK)
        logging.warning("[%s] TE MAN 并发核心修改中...", _BOOT_MARK)
        _install_delayed_patch_hook()
except Exception as exc:
    logging.exception("[TE MAN VIP PATCH] failed to apply prestartup patch: %s", exc)
