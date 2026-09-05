"""Discoverable aggregate for the split protocol adapter regression suite."""

import sys
import unittest
from pathlib import Path


TESTS_ROOT = Path(__file__).resolve().parent
if str(TESTS_ROOT) not in sys.path:
    sys.path.insert(0, str(TESTS_ROOT))

from protocol_cases.history_and_cleanup_cases import HistoryAndCleanupCases  # noqa: E402
from protocol_cases.server_runtime_cases import ServerRuntimeCases  # noqa: E402
from protocol_cases.stream_and_captcha_cases import StreamAndCaptchaCases  # noqa: E402
from protocol_cases.support import ProtocolAdaptersTestSupport  # noqa: E402
from protocol_cases.tool_and_context_cases import ToolAndContextCases  # noqa: E402
from protocol_cases.web_and_compatibility_cases import WebAndCompatibilityCases  # noqa: E402


CASE_MIXINS = (
    ToolAndContextCases,
    StreamAndCaptchaCases,
    HistoryAndCleanupCases,
    ServerRuntimeCases,
    WebAndCompatibilityCases,
)
_seen_tests: set[str] = set()
for _mixin in CASE_MIXINS:
    _names = {name for name in vars(_mixin) if name.startswith("test_")}
    overlap = _seen_tests & _names
    if overlap:
        raise RuntimeError(f"duplicate split test names: {sorted(overlap)}")
    _seen_tests.update(_names)


class ProtocolAdaptersTest(
    ToolAndContextCases,
    StreamAndCaptchaCases,
    HistoryAndCleanupCases,
    ServerRuntimeCases,
    WebAndCompatibilityCases,
    ProtocolAdaptersTestSupport,
    unittest.TestCase,
):
    """Single fixture-backed test class assembled from focused case groups."""


if __name__ == "__main__":
    unittest.main()
