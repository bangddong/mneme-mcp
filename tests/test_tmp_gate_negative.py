"""임시 — ci-ok 게이트 음성 검증용. 확인 후 삭제한다.

매트릭스 1개 leg(ubuntu/3.11)만 실패시켜, 나머지 3개가 통과해도
집계 게이트(ci-ok)가 빨간불이 되는지 본다.
"""
import platform
import sys


def test_temporarily_fail_on_one_matrix_leg():
    if platform.system() == "Linux" and sys.version_info[:2] == (3, 11):
        raise AssertionError("일부러 실패 — ci-ok 게이트 검증용 (되돌릴 예정)")
