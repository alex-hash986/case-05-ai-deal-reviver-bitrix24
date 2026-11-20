import subprocess
import sys

from src.core.config import ROOT, Settings

if __name__ == "__main__":
    raise SystemExit(subprocess.call([
        sys.executable, "-m", "streamlit", "run", str(ROOT / "src/ui/app.py"),
        "--server.address", "127.0.0.1", "--server.port", str(Settings().ui_port),
    ], cwd=ROOT))
