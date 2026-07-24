"""Host GUI and tray icon for the iCloud-on-Linux bridge (v2 plan section 6).

``health`` and ``bridge`` are deliberately free of any Qt import so they can be
unit tested and called from worker threads; ``tray``, ``window`` and
``__main__`` are the PySide6 layer.
"""

__version__ = "2.0.0"
