"""Single keys for the dashboard (u = SigUSD view, e = ERG view), read without Enter.

Linux/macOS: the terminal is put in cbreak mode (no echo, Ctrl+C still stops the bot) and restored on
exit. Windows: a small thread polls the console. Without a terminal (systemd, pipes) nothing is read.
"""
import os
import sys
import threading
from typing import Callable


def start(on_key: Callable[[str], None], loop) -> Callable[[], None]:
    """Begin delivering key presses to on_key on the event loop; returns the function that stops it."""
    stream = sys.stdin
    try:
        if stream is None or not stream.isatty():
            return lambda: None
    except (ValueError, OSError):        # closed or replaced stdin
        return lambda: None
    if sys.platform == "win32":
        import msvcrt
        stop = threading.Event()

        def poll():
            while not stop.is_set():
                if msvcrt.kbhit():
                    loop.call_soon_threadsafe(on_key, msvcrt.getwch())
                else:
                    stop.wait(0.1)

        threading.Thread(target=poll, daemon=True, name="dashboard-keys").start()
        return stop.set

    import termios
    import tty
    fd = stream.fileno()
    try:
        saved = termios.tcgetattr(fd)
        tty.setcbreak(fd)
    except termios.error:
        return lambda: None

    def read():
        try:
            data = os.read(fd, 32)
        except OSError:
            return
        for ch in data.decode(errors="ignore"):
            on_key(ch)

    loop.add_reader(fd, read)

    def stop():
        loop.remove_reader(fd)
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)
    return stop
