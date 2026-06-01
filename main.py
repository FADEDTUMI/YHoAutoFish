#! python3
import sys
import os

# Ensure modules can be found
sys.path.append(os.path.dirname(os.path.abspath(__file__)))

from core.admin import ensure_admin_or_relaunch

if not ensure_admin_or_relaunch():
    sys.exit(0)

from core.dpi import set_process_dpi_awareness

set_process_dpi_awareness()

from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QIcon
from core.paths import resource_path
from core.version import APP_DISPLAY_NAME, APP_NAME, APP_VERSION
from gui.app import AppWindow
from core.tracker import EventTracker
from core.error_handler import GlobalErrorHandler

if __name__ == '__main__':
    print("Starting app...", flush=True)
    app = QApplication(sys.argv)
    
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_DISPLAY_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setWindowIcon(QIcon(resource_path("logo.jpg")))
    
    print("Creating AppWindow...", flush=True)
    window = AppWindow()
    print("Showing AppWindow...", flush=True)
    window.show()

    # Initialize analytics tracker
    tracker = EventTracker.get()
    GlobalErrorHandler.install(tracker)

    exit_code = app.exec()
    EventTracker.get().shutdown()
    sys.exit(exit_code)
