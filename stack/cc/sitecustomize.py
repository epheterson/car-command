# Loaded by every Python process in this image (PYTHONPATH=/app): see logscrub.py and privacy.py.
# The log scrub first, on its own: it needs nothing but the standard library, so it holds
# even in an interpreter where privacy.py cannot load.
import logscrub

logscrub.install()

import privacy  # noqa: E402

privacy.install()
privacy.install_redirect()
