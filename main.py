"""MamboTTS v2 root entrypoint script (spec item 3).

Installs a minimal pure-stdlib crash hook before importing Qt packages so
early DLL / import failures under ``pythonw.exe`` still log to ``crash.log``
and notify the user, then delegates to ``mambotts.__main__.main``.
"""

import datetime
import os
import sys
import threading
import traceback


def _install_early_crash_hook() -> None:
    root_dir = os.path.dirname(os.path.abspath(__file__))

    def _hook(etype, value, tb):
        if etype is KeyboardInterrupt or issubclass(etype, KeyboardInterrupt):
            return
        log_path = os.path.join(root_dir, "crash.log")
        try:
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(f"--- {datetime.datetime.now().isoformat()} ---\n")
                traceback.print_exception(etype, value, tb, file=fh)
            detail = "".join(traceback.format_exception_only(etype, value))[-800:]
        except Exception:
            detail = str(value)
        if sys.platform == "win32":
            try:
                import ctypes

                ctypes.windll.user32.MessageBoxW(
                    0,
                    f"MamboTTS 启动或运行时遇到错误，详情已写入 crash.log：\n\n{detail}",
                    "MamboTTS 错误",
                    0x10,
                )
            except Exception:
                pass

    sys.excepthook = _hook
    threading.excepthook = lambda args: _hook(args.exc_type, args.exc_value, args.exc_traceback)


_install_early_crash_hook()

from mambotts.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
