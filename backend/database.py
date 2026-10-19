from sqlalchemy import JSON, ForeignKey, create_engine, Column, Integer, String, text
from sqlalchemy.orm import sessionmaker, declarative_base

SQLALCHEMY_DATABASE_URL = "sqlite:///./api_test_studio.db"

engine = create_engine(
    SQLALCHEMY_DATABASE_URL, connect_args={"check_same_thread": False}
)

SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)

Base = declarative_base()


    
class Project(Base):
    __tablename__ = "projects"

    id = Column(Integer, primary_key=True, index=True)
    name = Column(String, index=True, nullable=True, comment="Project name")
    base_url = Column(String, nullable=True, comment="Base URL for the project")
    openapi_content = Column(JSON, nullable=True, comment="OpenAPI file content")

class Api(Base):
    __tablename__ = "apis"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False)
    
    name = Column(String, index=True, nullable=True, comment="API name")
    method = Column(String, index=True, nullable=True, comment="HTTP method")   
    url = Column(String, index=True, nullable=True, comment="API URL")      

    headers_params = Column(JSON, default={})
    query_params = Column(JSON, default={})
    path_params = Column(JSON, default={})
    
    single_constraints = Column(JSON, default={}, comment="Single parameter constraints")
    dependencies = Column(JSON, default={}, comment="Multi-parameter dependencies and constraints")
    
    swagger_doc = Column(JSON, nullable=True, comment="Swagger operation definition for this API")
    service_name = Column(String, nullable=True, comment="Service name from OpenAPI info title")
    service_description = Column(String, nullable=True, comment="Service description from OpenAPI info description")


class ApiRequestBody(Base):
    __tablename__ = "api_request_bodies"

    id = Column(Integer, primary_key=True, index=True)
    api_id = Column(Integer, ForeignKey("apis.id", ondelete="CASCADE"), nullable=False)
    
    media_type = Column(String, nullable=False, comment="Content-Type (e.g., application/json, application/x-www-form-urlencoded)")
    body_params = Column(JSON, default={}, comment="Parameters for this media type")
    is_default = Column(Integer, default=0, comment="Whether this is the default body type for the API")

class TestCase(Base):
    __tablename__ = "test_cases"

    id = Column(Integer, primary_key=True, index=True)
    api_id = Column(Integer, ForeignKey("apis.id", ondelete="CASCADE"), nullable=False)

    name = Column(String, nullable=True, comment="Test case name")
    description = Column(String, nullable=True, comment="Test case description")

    headers_params = Column(JSON, default={})                                                              
    query_params = Column(JSON, default={})
    path_params = Column(JSON, default={})

    body_params = Column(JSON, default={})
    expected_status = Column(String, nullable=True, comment="Expected HTTP status code")
    case_type = Column(String, nullable=True, comment="Test case type")
    adopted = Column(Integer, default=0, comment="Whether this case has been adopted")
    created_at = Column(String, nullable=True, comment="Creation timestamp")


class SingleInputSpace(Base):
    """单参数输入空间表 (SISP - Single Input Space Parameter)"""
    __tablename__ = "single_input_spaces"

    id = Column(Integer, primary_key=True, index=True)
    api_id = Column(Integer, ForeignKey("apis.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 单参数空间的完整结构：{param_name: [categories...]}
    spaces = Column(JSON, nullable=False, comment="Single parameter input spaces")
    
    input_fingerprint = Column(String, nullable=True)

    created_at = Column(String, nullable=True, comment="Creation timestamp")
    updated_at = Column(String, nullable=True, comment="Last update timestamp")


class MultiInputSpace(Base):
    """多参数输入空间表 (MISP - Multi Input Space Parameter)"""
    __tablename__ = "multi_input_spaces"

    id = Column(Integer, primary_key=True, index=True)
    api_id = Column(Integer, ForeignKey("apis.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 多参数空间 - 混合前
    spaces = Column(JSON, nullable=False, comment="Multi-parameter spaces before mixing")
    
    input_fingerprint = Column(String, nullable=True)

    created_at = Column(String, nullable=True, comment="Creation timestamp")
    updated_at = Column(String, nullable=True, comment="Last update timestamp")


class MixedInputSpace(Base):
    """混合输入空间表 (Mixed multi-parameter spaces with single parameter space mappings)"""
    __tablename__ = "mixed_input_spaces"

    id = Column(Integer, primary_key=True, index=True)
    api_id = Column(Integer, ForeignKey("apis.id", ondelete="CASCADE"), nullable=False, index=True)
    
    # 混合空间 - 混合后（关联单参数空间分类）
    spaces = Column(JSON, nullable=False, comment="Mixed multi-parameter spaces with single parameter space mappings")
    
    input_fingerprint = Column(String, nullable=True)

    created_at = Column(String, nullable=True, comment="Creation timestamp")
    updated_at = Column(String, nullable=True, comment="Last update timestamp")


class AdoptedTestCase(Base):
    """已采纳测试用例表"""
    __tablename__ = "adopted_test_cases"

    id = Column(Integer, primary_key=True, index=True)
    api_id = Column(Integer, ForeignKey("apis.id", ondelete="CASCADE"), nullable=False, index=True)
    source_test_case_id = Column(Integer, nullable=True, comment="Source TestCase ID")

    name = Column(String, nullable=True, comment="Test case name")
    description = Column(String, nullable=True, comment="Test case description")

    headers_params = Column(JSON, default={})
    query_params = Column(JSON, default={})
    path_params = Column(JSON, default={})
    body_params = Column(JSON, default={})
    
    expected_status = Column(String, nullable=True, comment="Expected HTTP status code")
    created_at = Column(String, nullable=True, comment="Creation timestamp")


class TestRunTask(Base):
    """测试执行任务表"""
    __tablename__ = "test_run_tasks"

    id = Column(Integer, primary_key=True, index=True)
    project_id = Column(Integer, ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True)
    api_id = Column(Integer, ForeignKey("apis.id", ondelete="CASCADE"), nullable=False, index=True)

    status = Column(String, nullable=False, default="pending", comment="pending/running/completed/failed")
    total_cases = Column(Integer, nullable=False, default=0)
    executed_cases = Column(Integer, nullable=False, default=0)
    passed_cases = Column(Integer, nullable=False, default=0)
    failed_cases = Column(Integer, nullable=False, default=0)

    started_at = Column(String, nullable=True)
    finished_at = Column(String, nullable=True)
    created_at = Column(String, nullable=True)
    error_message = Column(String, nullable=True)


class TestRunResult(Base):
    """测试任务执行明细表"""
    __tablename__ = "test_run_results"

    id = Column(Integer, primary_key=True, index=True)
    task_id = Column(Integer, ForeignKey("test_run_tasks.id", ondelete="CASCADE"), nullable=False, index=True)
    test_case_id = Column(Integer, ForeignKey("test_cases.id", ondelete="SET NULL"), nullable=True, index=True)

    status = Column(String, nullable=False, default="failed", comment="passed/failed/running")
    expected_status = Column(String, nullable=True)
    actual_status = Column(String, nullable=True)
    passed = Column(Integer, nullable=False, default=0)

    duration_ms = Column(Integer, nullable=True)
    error_message = Column(String, nullable=True)

    request_url = Column(String, nullable=True, comment="The constructed request URL")
    request_headers = Column(String, nullable=True, comment="The request headers (JSON string)")
    request_body = Column(String, nullable=True, comment="The request body (JSON string)")
    request_query = Column(String, nullable=True, comment="The request query parameters (JSON string)")

    response_headers = Column(JSON, default={})
    response_body = Column(JSON, default={})
    executed_at = Column(String, nullable=True)


def _ensure_sqlite_column(table_name: str, column_name: str, definition_sql: str):
    """Add missing SQLite columns for backward compatibility with existing DB files."""
    with engine.begin() as conn:
        cols = conn.execute(text(f"PRAGMA table_info({table_name})")).fetchall()
        existing_columns = {c[1] for c in cols}
        if column_name not in existing_columns:
            conn.execute(text(f"ALTER TABLE {table_name} ADD COLUMN {column_name} {definition_sql}"))


def run_lightweight_migrations():
    """Run additive, backward-compatible schema updates for SQLite."""
    _ensure_sqlite_column("single_input_spaces", "input_fingerprint", "VARCHAR")
    _ensure_sqlite_column("multi_input_spaces", "input_fingerprint", "VARCHAR")
    _ensure_sqlite_column("mixed_input_spaces", "input_fingerprint", "VARCHAR")
    _ensure_sqlite_column("test_cases", "expected_status", "VARCHAR")
    _ensure_sqlite_column("test_cases", "case_type", "VARCHAR")
    _ensure_sqlite_column("test_cases", "adopted", "INTEGER DEFAULT 0")
    _ensure_sqlite_column("test_cases", "created_at", "VARCHAR")
    _ensure_sqlite_column("adopted_test_cases", "source_test_case_id", "INTEGER")
    _ensure_sqlite_column("test_run_results", "request_url", "TEXT")
    _ensure_sqlite_column("test_run_results", "request_headers", "TEXT")
    _ensure_sqlite_column("test_run_results", "request_body", "TEXT")
    _ensure_sqlite_column("test_run_results", "request_query", "TEXT")
