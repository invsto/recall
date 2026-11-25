"""python -m recall_v2 <command>: the Recall v2 command line."""
import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
