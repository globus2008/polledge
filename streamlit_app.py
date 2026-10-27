"""Streamlit Cloud entry point: `streamlit run streamlit_app.py`."""

import sys
from pathlib import Path

# Make the package importable without installation (Streamlit Cloud only installs requirements.txt).
sys.path.insert(0, str(Path(__file__).resolve().parent / "src"))

from polledge.web.app import main  # noqa: E402

main()
