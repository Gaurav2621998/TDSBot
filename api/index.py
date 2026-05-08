import sys
from pathlib import Path

# Add the backend directory to the sys.path so that 'app' can be imported
backend_path = str(Path(__file__).parent.parent / "backend")
if backend_path not in sys.path:
    sys.path.append(backend_path)

from app.main import app
