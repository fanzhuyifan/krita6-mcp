"""Krita plugin entry point; pure bridge modules also work outside Krita."""

import sys

# Krita initializes its Python API before importing enabled plugins. Avoid an
# eager import in external Python, where Qt and LibKis must not be loaded.
if "krita" in sys.modules:
    from krita import Krita

    from .extension import KritaBridgeExtension

    Krita.instance().addExtension(KritaBridgeExtension(Krita.instance()))
