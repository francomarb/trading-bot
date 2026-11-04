"""Small callable composition helpers for engine integration points."""

from __future__ import annotations

from collections.abc import Callable

from loguru import logger


class CompositePostCycleHook:
    """Run independent post-cycle hooks without cross-suppressing failures."""

    def __init__(self, *hooks: Callable[[], None]) -> None:
        self._hooks = hooks

    def __call__(self) -> None:
        for hook in self._hooks:
            try:
                hook()
            except Exception as exc:  # noqa: BLE001 - isolation is the contract
                name = getattr(hook, "__name__", type(hook).__name__)
                logger.warning(
                    f"post-cycle component {name} failed "
                    f"(other hooks and trading unaffected): {exc}"
                )
