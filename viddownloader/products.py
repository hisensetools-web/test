"""The unit everything works on: one product, its video links, its folder name."""
from __future__ import annotations

from dataclasses import dataclass, field

from . import config


@dataclass
class Product:
    name: str
    links: list[str] = field(default_factory=list)

    @property
    def slug(self) -> str:
        return config.slugify(self.name)


def select(products: list[Product], only: list[str] | None) -> list[Product]:
    """Products whose name or slug contains any of the `only` terms (case-insensitive); all when `only` is empty."""
    if not only:
        return products
    terms = [t.strip().lower() for t in only if t.strip()]
    return [p for p in products if any(t in p.name.lower() or t in p.slug for t in terms)]
