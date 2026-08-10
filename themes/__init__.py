"""Theme registry.

Every module in this package that defines a module-level `VIEWS` dict
(name -> view class) is auto-registered — dropping a new theme file in this
directory is all it takes to make it selectable with --view-*.

A view class has the signature:

    View(w, h, palette: fx.Palette | None, opts: dict) -> .render() -> PIL.Image

`palette=None` means "use the theme's own signature palette"; a Palette is
passed when the user overrides with --palette. `opts` carries owner_* strings.
"""

import importlib
import pkgutil

VIEWS: dict[str, type] = {}

for _m in pkgutil.iter_modules(__path__):
    _mod = importlib.import_module(f"{__name__}.{_m.name}")
    VIEWS.update(getattr(_mod, "VIEWS", {}))
