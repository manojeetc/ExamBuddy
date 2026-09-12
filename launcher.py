import socket
import sys
import webbrowser

from app import app


def get_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


if __name__ == "__main__":
    # Older flaskwebgui releases expect every macOS browser adapter to expose
    # a name, but Apple's default adapter does not provide that attribute.
    default_browser = webbrowser.get()
    if not hasattr(default_browser, "name"):
        default_browser.name = "Safari"
    from flaskwebgui import FlaskUI

    port = get_free_port()
    print("\nAMC Exam Practice is starting...")
    print("Opening the Flask GUI window...\n")
    gui_options = {
        "server": "flask",
        "app": app,
        "port": port,
        "width": 1280,
        "height": 850,
        "fullscreen": False,
        "app_mode": True,
        "auto_close": True,
        "server_kwargs": {
            "app": app,
            "host": "127.0.0.1",
            "port": port,
            "debug": False,
            "use_reloader": False,
        },
    }
    if sys.platform == "darwin":
        gui_options["browser_path"] = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
    FlaskUI(**gui_options).run()
