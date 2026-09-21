"""测试辅助：统一打印输入与判定依据，便于单测日志追溯。"""

from __future__ import annotations

import sys
import unittest


class LoggingTestCase(unittest.TestCase):
    """每个断言打印 GIVEN/EXPECT/ACTUAL，满足日志可追溯要求。"""

    def log_judgement(self, given: str, expect: str, actual: str) -> None:
        msg = f"\n  [用例] {self._testMethodName}\n  [输入] {given}\n  [判定依据] {expect}\n  [实际] {actual}"
        sys.stdout.write(msg + "\n")
        sys.stdout.flush()

    def assertErrorCode(self, exc_cm, code: str, given: str = "") -> None:
        actual = getattr(exc_cm.exception, "code", None)
        self.log_judgement(given or "见测试构造", f"错误码 == {code!r}", f"{actual!r} ({exc_cm.exception})")
        self.assertEqual(actual, code)
