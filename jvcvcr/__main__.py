"""Allow `python -m jvcvcr` to launch the GUI."""

from .gui import main

if __name__ == "__main__":
    raise SystemExit(main())
