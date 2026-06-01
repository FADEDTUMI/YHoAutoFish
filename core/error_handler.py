"""Global error handler — captures unhandled exceptions and reports via EventTracker.

Installs custom sys.excepthook and threading.excepthook.
Never interferes with original exception handling (still calls original hooks).
"""
import logging
import sys
import threading
import traceback

log = logging.getLogger(__name__)


class GlobalErrorHandler:
    _original_excepthook = None
    _original_thread_excepthook = None
    _tracker = None
    _installed = False

    @classmethod
    def install(cls, tracker):
        if cls._installed:
            return
        cls._tracker = tracker
        cls._original_excepthook = sys.excepthook
        cls._original_thread_excepthook = threading.excepthook
        sys.excepthook = cls._handle_exception
        threading.excepthook = cls._handle_thread_exception
        cls._installed = True

    @classmethod
    def uninstall(cls):
        if not cls._installed:
            return
        if cls._original_excepthook:
            sys.excepthook = cls._original_excepthook
        if cls._original_thread_excepthook:
            threading.excepthook = cls._original_thread_excepthook
        cls._installed = False
        cls._tracker = None

    @classmethod
    def _handle_exception(cls, exc_type, exc_value, exc_tb):
        if exc_type not in (KeyboardInterrupt, SystemExit):
            try:
                if cls._tracker:
                    stacktrace = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
                    cls._tracker.error_report(
                        error_type="unhandled_exception",
                        error_message=str(exc_value)[:500],
                        stacktrace=stacktrace[:8192],
                        context={"thread": "main", "type": exc_type.__name__},
                    )
            except Exception:
                pass
        if cls._original_excepthook:
            cls._original_excepthook(exc_type, exc_value, exc_tb)

    @classmethod
    def _handle_thread_exception(cls, args):
        try:
            if cls._tracker:
                stacktrace = "".join(traceback.format_exception(
                    args.exc_type, args.exc_value, args.exc_traceback,
                ))
                thread_name = getattr(args.thread, "name", "unknown") if args.thread else "unknown"
                cls._tracker.error_report(
                    error_type="unhandled_thread_exception",
                    error_message=str(args.exc_value)[:500],
                    stacktrace=stacktrace[:8192],
                    context={"thread": thread_name, "type": args.exc_type.__name__},
                )
        except Exception:
            pass
        if cls._original_thread_excepthook:
            cls._original_thread_excepthook(args)
