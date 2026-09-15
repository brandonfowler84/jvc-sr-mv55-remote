"""Computer control for the JVC SR-MV55U DVD/VCR deck over RS-232C.

Layers, lowest first:

* :mod:`jvcvcr.protocol`  -- pure command/response definitions and codecs
* :mod:`jvcvcr.transport` -- serial port, or the in-process simulator
* :mod:`jvcvcr.device`    -- threaded controller, framing, polling, state
* :mod:`jvcvcr.macros`    -- shareable named command sequences
* :mod:`jvcvcr.gui`       -- the PySide6 application

The lower three layers have no GUI dependency, so they can be used from
scripts or other applications.
"""

__version__ = "1.1.0"

from . import protocol  # noqa: F401
from .protocol import Deck  # noqa: F401

__all__ = ["protocol", "Deck", "__version__"]
