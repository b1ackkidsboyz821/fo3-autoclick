import sys
from pathlib import Path

if __name__ == "__main__":
    base = Path(__file__).resolve().parent
    try:
        if "--tk" in sys.argv:
            raise ImportError
        from fo3auto.webapp import main
    except ImportError:   # pywebview missing (old .venv) or asked for the classic window
        from fo3auto.gui import main
    main(base)
