"""
Comparator registry. A new comparator is added by decorating a class with
@register - the engine looks classes up by their Reltio short name and never
needs to change.
"""

from typing import Dict, Optional, Type

from .comparators.base import UNSUPPORTED, CompareResult, Comparator, EngineContext
from .exceptions_log import LOG
from .rule_config_parser import ClassMapping, short_class, short_attr

REGISTRY: Dict[str, Type[Comparator]] = {}
DEFAULT_COMPARATOR = "BasicStringComparator"


def register(cls: Type[Comparator]) -> Type[Comparator]:
    REGISTRY[cls.RELTIO_NAME] = cls
    return cls


def _load_builtin() -> None:
    import importlib
    from .comparators import BUILTIN_MODULES
    for mod in BUILTIN_MODULES:                      # modules register themselves on import
        importlib.import_module(f"{__package__}.comparators.{mod}")


class UnsupportedComparator(Comparator):
    """Placeholder for a class with no Python implementation: never claims equality."""
    STATUS = UNSUPPORTED

    def __init__(self, name: str, *a, **kw):
        super().__init__(*a, **kw)
        self.RELTIO_NAME = name

    def compare(self, a: str, b: str) -> CompareResult:
        return CompareResult(False, 0.0, f"{self.RELTIO_NAME} not implemented")


def create(class_name: str, params: Optional[dict] = None, ctx: Optional[EngineContext] = None,
           group: str = "", attribute: str = "") -> Comparator:
    _load_builtin()
    name = short_class(class_name) or DEFAULT_COMPARATOR
    cls = REGISTRY.get(name)
    if cls is None:
        LOG.add("UNSUPPORTED_COMPARATOR", f"Comparator class {name} is not implemented",
                group=group, attribute=short_attr(attribute), severity="ERROR")
        return UnsupportedComparator(name, params, ctx, group, attribute)
    return cls(params, ctx, group, attribute)


def for_mapping(mapping: Optional[ClassMapping], ctx: EngineContext, group: str, attribute: str) -> Comparator:
    if mapping is None:
        return create(DEFAULT_COMPARATOR, {}, ctx, group, attribute)
    return create(mapping.class_name, mapping.params, ctx, group, attribute)


def registered() -> Dict[str, Type[Comparator]]:
    _load_builtin()
    return dict(REGISTRY)
