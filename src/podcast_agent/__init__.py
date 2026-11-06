"""Agentic podcast summariser, show-notes writer and fact-checker."""

import os

__version__ = "0.3.0"

# The release that produced a result: semantic version plus the git commit when the image was built
# with one (docker build --build-arg BUILD_SHA=$(git rev-parse --short HEAD)).
CODE_VERSION = __version__ + (f"+{os.environ['PODCAST_AGENT_BUILD']}" if os.getenv("PODCAST_AGENT_BUILD") else "")
