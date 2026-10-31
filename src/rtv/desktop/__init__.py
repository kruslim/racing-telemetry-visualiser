"""RTV Desktop: a native PySide6 client for the pitwall and the post-race review.

The window talks to the same FastAPI app as the web frontend (embedded in-process
by ``rtv.desktop.server``, or an existing server given with ``--server``). Qt is an
optional extra (``pip install -e ".[desktop]"``) and is imported only inside this
package; ``rtv.desktop.model`` is pure Python and needs no display.
"""

from __future__ import annotations
