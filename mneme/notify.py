"""Discord 웹훅 알림 — 폰으로 가는 유일한 아웃바운드 접점.

봇 계정·상시 프로세스 없이 웹훅 URL 하나로 동작한다(DECISIONS.md 2026-07-02 #3:
Discord 봇 동결, 알림 니치는 웹훅으로 회수). `DISCORD_WEBHOOK_URL`이 비어 있으면
자동 비활성화 — 알림 실패가 본 루프를 절대 막지 않는다.

웹훅 URL 발급: Discord 채널 설정 → 연동 → 웹후크 → URL 복사 → .env에 등록.
"""
import os

import httpx


def is_enabled() -> bool:
    return bool(os.getenv("DISCORD_WEBHOOK_URL", "").strip())


def send(text: str) -> bool:
    """웹훅으로 메시지 전송. 비활성/실패 시 False (예외를 밖으로 던지지 않는다)."""
    url = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not url:
        return False
    try:
        # Discord 메시지 본문 상한 2000자
        r = httpx.post(url, json={"content": text[:2000]}, timeout=5.0)
        return r.status_code in (200, 204)
    except Exception:
        return False


def notify_degrading(transitions: list[dict]) -> bool:
    """Outer Loop에서 degrading으로 전이된 스킬들을 복습 알림으로 보낸다.

    study- 접두 스킬(학습 토픽)이 주 대상이지만, 다른 스킬의 퇴화도 알 가치가 있어
    전이 전부를 보낸다. degrading 전이가 없으면 no-op.
    """
    items = [t for t in transitions if t.get("to") == "degrading"]
    if not items:
        return False
    lines = ["📉 **복습 필요** — 성향이 식어가는 스킬이 감지됐습니다."]
    for t in items:
        lines.append(f"- `{t['name']}` ({t['reason']})")
    lines.append("확인: `growth_log()` 또는 `python -m mneme.growth`")
    return send("\n".join(lines))
