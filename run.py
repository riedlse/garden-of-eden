#!/usr/bin/env python3
from app.lib.logging_config import configure_logging

# Configure logging before the app (and therefore the hardware drivers) is
# built, or their import-time INFO lines are dropped.
configure_logging(log_file="garden-api.log")

from app import create_app  # noqa: E402

app = create_app("default")

if __name__ == "__main__":
    app.run(debug=True, host="0.0.0.0", port=5000)
