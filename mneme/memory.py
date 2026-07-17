import sqlite3
import os
from pathlib import Path


def get_db_path() -> str:
    return os.getenv("DB_PATH", "./memory/state.db")


def get_connection() -> sqlite3.Connection:
    db_path = get_db_path()
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db():
    conn = get_connection()
    with conn:
        conn.executescript("""
            CREATE VIRTUAL TABLE IF NOT EXISTS wiki_fts USING fts5(
                path,
                content,
                tokenize = 'unicode61'
            );

            CREATE TABLE IF NOT EXISTS wiki_index (
                path TEXT PRIMARY KEY,
                summary TEXT,
                tags TEXT,
                content_hash TEXT,   -- 원문 sha256 (미변경 페이지 재요약 스킵용)
                updated_at TEXT,
                updated_by TEXT
            );

            CREATE TABLE IF NOT EXISTS facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                source_agent TEXT,
                trust_score REAL DEFAULT 0.5,
                wiki_path TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id TEXT,
                agent TEXT,
                tool TEXT,
                query TEXT,
                result_summary TEXT,
                success INTEGER,
                score REAL,
                what_worked TEXT,
                what_failed TEXT,
                next_hint TEXT,
                skills_used TEXT,
                created_at TEXT DEFAULT (datetime('now'))
            );

            -- L3 Procedural Memory: 스킬 성향 θ_eff
            CREATE TABLE IF NOT EXISTS skills (
                name TEXT PRIMARY KEY,
                description TEXT,
                wiki_path TEXT,
                base REAL DEFAULT 0.5,        -- θ_base (공유 기반)
                delta REAL DEFAULT 0.0,        -- δ (개인화 적응, |δ| <= 0.2)
                propensity REAL DEFAULT 0.5,   -- θ_eff = clip(base + delta)
                state TEXT DEFAULT 'seeding',  -- seeding/developing/active/degrading/archived
                success_count INTEGER DEFAULT 0,
                use_count INTEGER DEFAULT 0,
                seed_protected INTEGER DEFAULT 0,
                created_at TEXT DEFAULT (datetime('now')),
                updated_at TEXT DEFAULT (datetime('now'))
            );

            CREATE TABLE IF NOT EXISTS working (
                session_id TEXT,
                key TEXT,
                value TEXT,
                expires_at TEXT,
                PRIMARY KEY (session_id, key)
            );

            -- Outer Loop: 주기 정산 스냅샷 (생명주기 전이 + CI/BC 추적)
            CREATE TABLE IF NOT EXISTS loop_cycles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ran_at TEXT DEFAULT (datetime('now')),
                episodes_processed INTEGER,
                ci REAL,                 -- nullable (로컬 LLM 미가용 시 NULL)
                bc REAL,
                transitions TEXT,        -- JSON: [{name, from, to, reason}]
                propensities TEXT        -- JSON: {skill: propensity} (다음 사이클 BC 기준점)
            );

            -- L5 Identity: 자기 인식 4장치 (M14 self_model)
            CREATE TABLE IF NOT EXISTS self_model (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                assessed_at TEXT DEFAULT (datetime('now')),
                episodes_seen INTEGER,        -- 평가 시점 누적 에피소드 수 (게이트 기준)
                success_rate REAL,            -- 최근 윈도우 성공률
                growth_rate REAL,             -- success_rate - 직전 행 success_rate
                calibration_error REAL,       -- M15: mean|self_score - assessor_score|, nullable
                difficulty REAL,              -- M17 난이도 스칼라 [0,1]
                regulation TEXT,              -- M16: healthy/crash/stagnant/overspeed
                curriculum TEXT,              -- M17: 성장 타깃 JSON
                notes TEXT
            );

            -- 성장 조치 큐: 자기평가가 감지한 "사람이 해소해야 할" 문제 백로그.
            -- dedup_key로 같은 문제 중복 누적 방지. 정상화되면 자동 resolved.
            CREATE TABLE IF NOT EXISTS growth_actions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                created_at TEXT DEFAULT (datetime('now')),
                last_seen_at TEXT DEFAULT (datetime('now')),
                seen_count INTEGER DEFAULT 1,
                kind TEXT,            -- regulation/calibration/skill-at-risk
                severity TEXT,        -- warn/critical
                subject TEXT,         -- 대상(스킬명·growth-rate 등)
                detail TEXT,          -- 사람용 설명 + 권장 조치
                dedup_key TEXT,       -- open 항목 식별 키
                status TEXT DEFAULT 'open',   -- open/resolved
                resolved_at TEXT,
                resolution_note TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_growth_open
                ON growth_actions(status, dedup_key);
        """)
        # 마이그레이션: 기존 DB의 wiki_index에 content_hash 컬럼 보강 (없으면 추가)
        cols = [r["name"] for r in conn.execute("PRAGMA table_info(wiki_index)")]
        if "content_hash" not in cols:
            conn.execute("ALTER TABLE wiki_index ADD COLUMN content_hash TEXT")
    conn.close()
