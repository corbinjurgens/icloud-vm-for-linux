"""Host GUI and tray icon for the iCloud-on-Linux bridge (v2 plan section 6).

``health``, ``bridge``, ``power`` and ``autostart`` are deliberately free of any
Qt import so they can be unit tested and called from worker threads; ``tray``,
``window`` and ``__main__`` are the PySide6 layer.
"""

# Pre-1.0 on purpose: nothing here has ever shipped, so the leading zero is the
# honest statement of that (the pre-release policy in CONTRIBUTING.md). The
# minor digit tracks the design line the code implements -- 0.2.x is the v2
# plan. This string is the one version source in the repository; the Makefile
# and packaging/build-deb.sh read it, so there is nothing else to keep in step.
__version__ = "0.2.0"
