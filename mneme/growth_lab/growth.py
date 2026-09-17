"""성장 조치 큐 (Growth Backlog).

자기평가(self_model.assess)가 감지한 "사람이 해소해야 할" 문제를 박제하는 백로그.
매 사이클 로그에 흘려보내는 대신, 조치 항목으로 남겨 사용자가 주기적으로 해소한다.

흐름:
  self_model.assess() ─emit()→ growth_actions(open)
  사용자 ─growth_log()→ 미해소 목록 확인 ─growth_resolve(id)→ resolved
  문제가 스스로 정상화 ─emit()이 자동 resolved 처리 (자기치유, 백로그=현재 상태)

dedup_key로 같은 문제 재감지 시 새 행을 만들지 않고 last_seen_at·seen_count만 갱신한다.
"""
import sys
import io

from mneme.memory import get_connection

# emit()이 관리하는 dedup_key 네임스페이스 — 자동 해소 대상 판별에 사용.
MANAGED_PREFIXES = ("regulation:", "calibration", "skill:")


def record(kind: str, severity: str, subject: str, detail: str,
           dedup_key: str) -> int:
    """조치 항목 1건 기록. 같은 dedup_key의 open 항목이 있으면 갱신(중복 방지)."""
    conn = get_connection()
    try:
        with conn:
            existing = conn.execute(
                "SELECT id, seen_count FROM growth_actions "
                "WHERE dedup_key=? AND status='open' ORDER BY id DESC LIMIT 1",
                (dedup_key,),
            ).fetchone()
            if existing:
                conn.execute(
                    "UPDATE growth_actions SET last_seen_at=datetime('now'), "
                    "seen_count=?, detail=?, severity=? WHERE id=?",
                    (existing["seen_count"] + 1, detail, severity, existing["id"]),
                )
                return existing["id"]
            cur = conn.execute(
                "INSERT INTO growth_actions(kind, severity, subject, detail, dedup_key) "
                "VALUES(?,?,?,?,?)",
                (kind, severity, subject, detail, dedup_key),
            )
            return cur.lastrowid
    finally:
        conn.close()


def resolve(action_id: int, note: str = "") -> dict:
    """조치 항목을 해소(resolved) 처리."""
    conn = get_connection()
    try:
        with conn:
            cur = conn.execute(
                "UPDATE growth_actions SET status='resolved', "
                "resolved_at=datetime('now'), resolution_note=? "
                "WHERE id=? AND status='open'",
                (note, action_id),
            )
        if cur.rowcount == 0:
            return {"resolved": False, "reason": f"open action #{action_id} 없음"}
        return {"resolved": True, "id": action_id}
    finally:
        conn.close()


def open_actions() -> list[dict]:
    """미해소(open) 조치 항목 — critical 우선, 오래된 순."""
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM growth_actions WHERE status='open' "
            "ORDER BY CASE severity WHEN 'critical' THEN 0 ELSE 1 END, created_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def all_actions(limit: int = 50) -> list[dict]:
    conn = get_connection()
    try:
        rows = conn.execute(
            "SELECT * FROM growth_actions ORDER BY id DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def emit(desired: list[dict]):
    """자기평가가 도출한 현재 문제 목록(desired)을 백로그에 동기화한다.

    desired: [{kind, severity, subject, detail, dedup_key}, ...]
      - 목록의 각 항목을 record() (신규 생성 또는 재감지 갱신).
      - 관리 네임스페이스(MANAGED_PREFIXES)의 open 항목 중 desired에 없는 것은
        문제가 사라진 것으로 보고 자동 resolved (자기치유).
    """
    desired_keys = {d["dedup_key"] for d in desired}
    for d in desired:
        record(d["kind"], d["severity"], d["subject"], d["detail"], d["dedup_key"])

    # 자동 해소: 관리 대상인데 더 이상 감지되지 않는 open 항목
    for a in open_actions():
        key = a["dedup_key"] or ""
        if key in desired_keys:
            continue
        if any(key.startswith(p) for p in MANAGED_PREFIXES):
            resolve(a["id"], note="자동 해소: 자기평가에서 더 이상 감지되지 않음")


# ── CLI ─────────────────────────────────────────────────
def main():
    import argparse

    try:
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    except Exception:
        pass

    p = argparse.ArgumentParser(description="mneme 성장 조치 큐")
    p.add_argument("--all", action="store_true", help="해소된 항목까지 표시")
    p.add_argument("--resolve", type=int, metavar="ID", help="해당 ID 조치 해소")
    p.add_argument("--note", default="", help="--resolve 시 해소 메모")
    args = p.parse_args()

    if args.resolve is not None:
        res = resolve(args.resolve, args.note)
        print(res)
        return

    actions = all_actions() if args.all else open_actions()
    if not actions:
        print("✅ 미해소 조치 없음 — 자기평가 기준 건강한 상태")
        return

    icon = {"critical": "🔴", "warn": "🟡"}
    for a in actions:
        st = "" if a["status"] == "open" else f" [{a['status']}]"
        mark = icon.get(a["severity"], "•")
        print(f"#{a['id']} {mark} [{a['kind']}] {a['subject']}{st}")
        print(f"    {a['detail']}")
        print(f"    최초 {a['created_at']} · 최근감지 {a['last_seen_at']} (×{a['seen_count']})")
        if a["status"] == "resolved":
            print(f"    해소 {a['resolved_at']}: {a['resolution_note']}")
        print()

    open_n = sum(1 for a in actions if a["status"] == "open")
    print(f"— open {open_n}건. 해소: python -m mneme.growth --resolve <ID> --note \"...\"")


if __name__ == "__main__":
    main()
