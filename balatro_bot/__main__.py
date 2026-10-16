"""支持 `python -m balatro_bot`。"""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
