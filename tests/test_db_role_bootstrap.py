from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DB = ROOT / "lib" / "db.py"


def read_db() -> str:
    return DB.read_text(encoding="utf-8")


def test_runtime_bootstrap_refreshes_app_role_credentials() -> None:
    source = read_db()

    assert "async def _ensure_app_role_on_conn" in source
    assert "ALTER ROLE {role_ident} WITH LOGIN PASSWORD" in source
    assert "CREATE ROLE {role_ident} LOGIN PASSWORD" in source
    assert "GRANT CONNECT ON DATABASE" in source
    assert "password=app_password" in source
    assert "await verify_conn.fetchval(\"SELECT 1\")" in source


def test_runtime_bootstrap_runs_before_app_pool_creation() -> None:
    source = read_db()

    ensure_index = source.index("await _ensure_app_role_on_conn(")
    pool_index = source.index("_pool = await asyncpg.create_pool(")

    assert ensure_index < pool_index
    assert "if dual_role:\n                await _ensure_app_role_on_conn(" in source


if __name__ == "__main__":
    test_runtime_bootstrap_refreshes_app_role_credentials()
    test_runtime_bootstrap_runs_before_app_pool_creation()
    print("runtime app-role bootstrap regression checks passed")
