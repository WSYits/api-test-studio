from fastapi import APIRouter, HTTPException, UploadFile, Form, Depends, Body, BackgroundTasks
from sqlalchemy import select, union_all, literal
from sqlalchemy.orm import Session
from types import SimpleNamespace
from copy import deepcopy
from database import SessionLocal, Project, Api, ApiRequestBody, SingleInputSpace, MultiInputSpace, MixedInputSpace, AdoptedTestCase, TestCase, TestRunTask, TestRunResult
from services.openapi_parser import parse_openapi, extract_apis_from_openapi, extract_project_info
from services.ai_constraint_extractor import get_extractor
from services.partition import generate_single_parameter_partition, generate_multi_parameter_partition, generate_mixed_parameter_partition
import json
import hashlib
import requests
import time
from datetime import datetime

router = APIRouter()

def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

@router.get("/projects")
async def list_projects(db: Session = Depends(get_db)):
    projects = db.query(Project).all()
    return [{"id": p.id, "name": p.name, "base_url": p.base_url, "openapi_content": p.openapi_content} for p in projects]

@router.post("/projects")
async def create_project(
    name: str = Form(...),
    base_url: str = Form(default=""),
    openapi_file: UploadFile = None,
    db: Session = Depends(get_db),
):
    if not name.strip():
        raise HTTPException(status_code=400, detail="Project name is required")

    existing_project = db.query(Project).filter(Project.name == name).first()
    if existing_project:
        raise HTTPException(status_code=400, detail="Project name already exists")

    openapi_content = None
    apis_to_create = []
    
    # 如果上传了OpenAPI文件，则解析并导入API
    if openapi_file:
        try:
            # 确定文件格式
            filename = openapi_file.filename.lower()
            if filename.endswith('.yaml') or filename.endswith('.yml'):
                file_format = 'yaml'
            elif filename.endswith('.json'):
                file_format = 'json'
            else:
                raise ValueError("Only .json, .yaml, and .yml formats are supported")
            
            # 读取文件内容
            content = await openapi_file.read()
            content_str = content.decode('utf-8')
            
            # 解析OpenAPI文档
            spec = parse_openapi(content_str, file_format)
            openapi_content = spec
            
            # 从OpenAPI提取项目信息，如果没有提供base_url则使用OpenAPI中的
            project_info = extract_project_info(spec)
            if not base_url.strip():
                base_url = project_info['base_url']
            if not name.strip():
                name = project_info['name']
            
            # 提取API列表
            apis_to_create = extract_apis_from_openapi(spec)
            
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Failed to parse OpenAPI file: {str(e)}")

    # 创建项目
    new_project = Project(name=name, base_url=base_url.strip() if base_url else "", openapi_content=openapi_content)
    db.add(new_project)
    db.flush()  # 获取project_id
    
    # 如果有解析出的API，就自动创建它们
    if apis_to_create:
        for api_info in apis_to_create:
            # 获取参数信息
            parameters = api_info.get('parameters', {})
            headers_list = parameters.get('headers', [])
            query_list = parameters.get('query', [])
            path_list = parameters.get('path', [])
            request_bodies_list = api_info.get('request_bodies', [])
            
            new_api = Api(
                project_id=new_project.id,
                name=api_info['name'],
                method=api_info['method'],
                url=api_info['url'],
                headers_params=headers_list,
                query_params=query_list,
                path_params=path_list,
                # 新增：保存 swagger_doc 和服务信息
                swagger_doc=api_info.get('swagger_doc'),
                service_name=api_info.get('service_name'),
                service_description=api_info.get('service_description'),
            )
            db.add(new_api)
            db.flush()  # 获取 api_id
            
            # 提取参数依赖关系
            try:
                # 构建用于提取依赖的对象
                swagger_operation = {
                    'operationId': api_info.get('name', ''),
                    'parameters': [],
                    'x-dependencies': api_info.get('x-dependencies', []),
                }
                
                # 添加参数信息
                for header in headers_list:
                    swagger_operation['parameters'].append({
                        'name': header.get('name', ''),
                        'in': 'header',
                        'description': header.get('description', ''),
                        'required': header.get('required', False),
                        'schema': {'type': header.get('type', 'string')}
                    })
                
                for query in query_list:
                    swagger_operation['parameters'].append({
                        'name': query.get('name', ''),
                        'in': 'query',
                        'description': query.get('description', ''),
                        'required': query.get('required', False),
                        'schema': {'type': query.get('type', 'string')}
                    })
                
                for path in path_list:
                    swagger_operation['parameters'].append({
                        'name': path.get('name', ''),
                        'in': 'path',
                        'description': path.get('description', ''),
                        'required': path.get('required', False),
                        'schema': {'type': path.get('type', 'string')}
                    })
                
                # 调用 AI 提取依赖关系
                extractor = get_extractor()
                dependencies = extractor.extract_parameter_dependencies(swagger_operation)
                
                # 将依赖关系存储到 API
                new_api.dependencies = dependencies
            except Exception as e:
                print(f"提取依赖关系出错: {str(e)}")
                new_api.dependencies = []
            
            # 创建它们的 requestBody
            for rb_info in request_bodies_list:
                request_body = ApiRequestBody(
                    api_id=new_api.id,
                    media_type=rb_info['media_type'],
                    body_params=rb_info['body_params'],
                    is_default=rb_info['is_default'],
                )
                db.add(request_body)
    
    db.commit()
    db.refresh(new_project)

    return {
        "id": new_project.id,
        "name": new_project.name,
        "base_url": new_project.base_url,
        "apis_imported": len(apis_to_create)
    }

@router.put("/projects/{project_id}")
async def update_project(
    project_id: int,
    base_url: str = Form(...),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    project.base_url = base_url.strip() if base_url else ""
    db.commit()
    db.refresh(project)
    
    return {
        "id": project.id,
        "name": project.name,
        "base_url": project.base_url,
    }

@router.get("/projects/{project_id}/apis")
async def list_apis(project_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    apis = db.query(Api).filter(Api.project_id == project_id).all()
    result = []
    for a in apis:
        # 获取该 API 的所有请求体
        request_bodies = db.query(ApiRequestBody).filter(ApiRequestBody.api_id == a.id).all()
        request_bodies_data = [{
            "id": rb.id,
            "media_type": rb.media_type,
            "body_params": rb.body_params or [],
            "is_default": rb.is_default,
        } for rb in request_bodies]
        
        # 如果没有请求体定义，默认创建一个 json 类型的
        if not request_bodies_data:
            request_bodies_data = [{
                "id": None,
                "media_type": "application/json",
                "body_params": [],
                "is_default": 1,
            }]
        
        result.append({
            "id": a.id,
            "project_id": a.project_id,
            "name": a.name,
            "method": a.method,
            "url": a.url,
            "headers_params": a.headers_params or [],
            "query_params": a.query_params or [],
            "path_params": a.path_params or [],
            "single_constraints": a.single_constraints or {},
            "dependencies": a.dependencies if isinstance(a.dependencies, list) else [],
            "request_bodies": request_bodies_data,
        })
    
    return result

@router.post("/projects/{project_id}/apis")
async def create_api(
    project_id: int,
    name: str = Form(...),
    method: str = Form(...),
    url: str = Form(...),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    if not name.strip():
        raise HTTPException(status_code=400, detail="API name is required")
    
    if not method.strip():
        raise HTTPException(status_code=400, detail="HTTP method is required")
    
    if not url.strip():
        raise HTTPException(status_code=400, detail="API URL is required")
    
    new_api = Api(project_id=project_id, name=name, method=method, url=url)
    db.add(new_api)
    db.commit()
    db.refresh(new_api)
    
    return {
        "id": new_api.id,
        "name": new_api.name,
        "method": new_api.method,
        "url": new_api.url,
        "headers_params": [],
        "query_params": [],
        "path_params": [],
        "single_constraints": {},
        "dependencies": [],
    }

@router.put("/projects/{project_id}/apis/{api_id}")
async def update_api(
    project_id: int,
    api_id: int,
    headers_params: str = Form(default="[]"),
    query_params: str = Form(default="[]"),
    path_params: str = Form(default="[]"),
    body_type: str = Form(default="application/json"),
    body_params: str = Form(default="[]"),
    single_constraints: str = Form(default="{}"),
    dependencies: str = Form(default="{}"),
    db: Session = Depends(get_db),
):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    try:
        api.headers_params = json.loads(headers_params)
        api.query_params = json.loads(query_params)
        api.path_params = json.loads(path_params)
        api.single_constraints = json.loads(single_constraints)
        api.dependencies = json.loads(dependencies)
        body_params_list = json.loads(body_params)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format for parameters")
    
    # 更新或创建 ApiRequestBody
    # 首先尝试查找是否已存在该 media_type 的请求体
    request_body = db.query(ApiRequestBody).filter(
        ApiRequestBody.api_id == api_id,
        ApiRequestBody.media_type == body_type
    ).first()
    
    if request_body:
        # 更新现有的请求体
        request_body.body_params = body_params_list
    else:
        # 创建新的请求体
        request_body = ApiRequestBody(
            api_id=api_id,
            media_type=body_type,
            body_params=body_params_list,
            is_default=1,  # 新创建的设为默认
        )
        db.add(request_body)
    
    db.commit()
    db.refresh(api)
    
    # 获取更新后的请求体列表
    request_bodies = db.query(ApiRequestBody).filter(ApiRequestBody.api_id == api_id).all()
    request_bodies_data = [{
        "id": rb.id,
        "media_type": rb.media_type,
        "body_params": rb.body_params or [],
        "is_default": rb.is_default,
    } for rb in request_bodies]
    
    return {
        "id": api.id,
        "name": api.name,
        "method": api.method,
        "url": api.url,
        "headers_params": api.headers_params,
        "query_params": api.query_params,
        "path_params": api.path_params,
        "single_constraints": api.single_constraints or {},
        "dependencies": api.dependencies if isinstance(api.dependencies, list) else [],
        "request_bodies": request_bodies_data,
    }

@router.delete("/projects/{project_id}/apis/{api_id}")
async def delete_api(project_id: int, api_id: int, db: Session = Depends(get_db)):
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    db.delete(api)
    db.commit()
    
    return {"message": "API deleted successfully"}


def _build_request_url(project: Project, api: Api, path_params: dict) -> str:
    """Build request URL from project base_url + api path and substitute path params."""
    if api.url.startswith("http://") or api.url.startswith("https://"):
        url = api.url
    else:
        base_url = project.base_url.rstrip("/") if project.base_url else ""
        url = base_url + api.url

    for key, value in (path_params or {}).items():
        placeholder = "{" + str(key) + "}"
        url = url.replace(placeholder, str(value))

    return url


def _execute_http_request(method: str, url: str, headers: dict, query: dict, body: dict, body_type: str = 'application/json'):
    """Execute HTTP request and return status/headers/body."""
    method_upper = (method or "GET").upper()
    body = body or {}
    headers = headers or {}
    query = query or {}

    # 根据 body_type 设置 Content-Type 请求头
    if body and method_upper in ['POST', 'PUT', 'PATCH']:
        if body_type not in headers.values() and 'Content-Type' not in headers:
            headers['Content-Type'] = body_type

    start_time = time.time()

    # 判断是否为 form-urlencoded 格式
    content_type = headers.get('Content-Type', '').lower()
    is_form_urlencoded = 'application/x-www-form-urlencoded' in content_type

    if method_upper == "GET":
        response = requests.get(url, headers=headers, params=query, timeout=30)
    elif method_upper == "POST":
        response = requests.post(
            url,
            headers=headers,
            params=query,
            json=body if not is_form_urlencoded and isinstance(body, dict) else None,
            data=body if is_form_urlencoded else None,
            timeout=30,
        )
    elif method_upper == "PUT":
        response = requests.put(
            url,
            headers=headers,
            params=query,
            json=body if not is_form_urlencoded and isinstance(body, dict) else None,
            data=body if is_form_urlencoded else None,
            timeout=30,
        )
    elif method_upper == "DELETE":
        response = requests.delete(url, headers=headers, params=query, timeout=30)
    elif method_upper == "PATCH":
        response = requests.patch(
            url,
            headers=headers,
            params=query,
            json=body if not is_form_urlencoded and isinstance(body, dict) else None,
            data=body if is_form_urlencoded else None,
            timeout=30,
        )
    else:
        raise ValueError(f"Unsupported method: {method_upper}")

    elapsed_ms = int((time.time() - start_time) * 1000)
    try:
        response_body = response.json()
    except Exception:
        response_body = response.text

    return {
        "status_code": response.status_code,
        "request_headers": headers,  # ← 返回实际发送的request headers（包括自动添加的Content-Type）
        "response_headers": dict(response.headers),
        "body": response_body,
        "elapsed_ms": elapsed_ms,
    }


def _run_test_task(task_id: int):
    """Background worker: execute all adopted cases in one task."""
    db = SessionLocal()
    try:
        task = db.query(TestRunTask).filter(TestRunTask.id == task_id).first()
        if not task:
            return

        project = db.query(Project).filter(Project.id == task.project_id).first()
        api = db.query(Api).filter(Api.id == task.api_id).first()
        if not project or not api:
            task.status = "failed"
            task.error_message = "Project or API not found"
            task.finished_at = datetime.now().isoformat()
            db.commit()
            return

        adopted_cases = db.query(AdoptedTestCase).filter(AdoptedTestCase.api_id == task.api_id).all()
        task.status = "running"
        task.started_at = datetime.now().isoformat()
        task.total_cases = len(adopted_cases)
        db.commit()

        for adopted in adopted_cases:
            status = "failed"
            actual_status = None
            passed = 0
            response_headers = {}
            response_body = {}
            duration_ms = None
            error_message = None
            url = None
            request_headers = {}
            request_query = {}
            request_body = {}

            try:
                print(f"\n=== 执行测试用例 (adopted_id={adopted.id}) ===")
                print(f"adopted.headers_params: {adopted.headers_params}")
                print(f"adopted.query_params: {adopted.query_params}")
                print(f"adopted.body_params: {adopted.body_params}")
                
                url = _build_request_url(project, api, adopted.path_params or {})
                
                # 获取该 API 的默认 body_type
                default_request_body = db.query(ApiRequestBody).filter(
                    ApiRequestBody.api_id == api.id,
                    ApiRequestBody.is_default == 1
                ).first()
                body_type = default_request_body.media_type if default_request_body else 'application/json'
                
                # 准备请求信息用于记录（处理headers_params可能是列表或字典的情况）
                # 如果是列表格式（参数定义），转换为字典；如果是字典格式，直接使用
                headers_raw = adopted.headers_params
                if isinstance(headers_raw, list):
                    # 列表格式：[{name: "X-Custom", type: "string", value: "test"}, ...]
                    request_headers = {item.get("name"): item.get("value", "") for item in headers_raw if item.get("name")}
                    print(f"DEBUG: Converted list headers to dict: {request_headers}")
                else:
                    # 字典格式：{name: value}
                    request_headers = dict(headers_raw) if headers_raw else {}
                
                request_query = dict(adopted.query_params) if adopted.query_params else {}
                request_body = dict(adopted.body_params) if adopted.body_params else {}
                
                # 调试日志
                print(f"DEBUG: url={url}, headers={request_headers}, query={request_query}, body={request_body}")
                print(f"DEBUG: adopted.headers_params raw value: {headers_raw}, type: {type(headers_raw)}")
                
                result = _execute_http_request(
                    method=api.method,
                    url=url,
                    headers=request_headers,
                    query=request_query,
                    body=request_body,
                    body_type=body_type,
                )

                actual_status = str(result["status_code"])
                expected_status = str(adopted.expected_status or "200")
                passed = 1 if actual_status == expected_status else 0
                status = "passed" if passed else "failed"
                # 使用实际发送的request headers（包括自动添加的Content-Type）
                request_headers = result.get("request_headers", request_headers)
                response_headers = result["response_headers"]
                response_body = result["body"]
                duration_ms = result["elapsed_ms"]
            except Exception as e:
                error_message = str(e)

            task_result = TestRunResult(
                task_id=task.id,
                test_case_id=adopted.source_test_case_id,
                status=status,
                expected_status=str(adopted.expected_status or "200"),
                actual_status=actual_status,
                passed=passed,
                duration_ms=duration_ms,
                error_message=error_message,
                request_url=url,
                request_headers=json.dumps(request_headers) if request_headers else "{}",
                request_body=json.dumps(request_body) if request_body else "{}",
                request_query=json.dumps(request_query) if request_query else "{}",
                response_headers=response_headers,
                response_body=response_body,
                executed_at=datetime.now().isoformat(),
            )
            db.add(task_result)

            task.executed_cases += 1
            if passed:
                task.passed_cases += 1
            else:
                task.failed_cases += 1
            db.commit()

        task.status = "completed"
        task.finished_at = datetime.now().isoformat()
        db.commit()

    except Exception as e:
        task = db.query(TestRunTask).filter(TestRunTask.id == task_id).first()
        if task:
            task.status = "failed"
            task.error_message = str(e)
            task.finished_at = datetime.now().isoformat()
            db.commit()
    finally:
        db.close()

@router.post("/projects/{project_id}/apis/{api_id}/debug")
async def debug_api(
    project_id: int,
    api_id: int,
    headers_params: str = Form(default="[]"),
    query_params: str = Form(default="[]"),
    path_params: str = Form(default="[]"),
    body_type: str = Form(default="application/json"),
    body_data: str = Form(default=""),
    db: Session = Depends(get_db),
):
    """
    调试接口：作为代理发送真实HTTP请求
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    try:
        headers_list = json.loads(headers_params)
        query_list = json.loads(query_params)
        path_list = json.loads(path_params)
    except json.JSONDecodeError:
        raise HTTPException(status_code=400, detail="Invalid JSON format for parameters")
    
    # 构建请求 URL：组合 base_url 和 api.url
    if api.url.startswith('http://') or api.url.startswith('https://'):
        # api.url 已经是完整的 URL
        url = api.url
    else:
        # api.url 是相对路径，需要组合 base_url
        base_url = project.base_url.rstrip('/') if project.base_url else ''
        url = base_url + api.url
    
    # 替换路径参数
    for path_param in path_list:
        if path_param.get('value'):
            placeholder = '{' + path_param.get('name', '') + '}'
            url = url.replace(placeholder, str(path_param.get('value', '')))
    
    # 构建请求头
    request_headers = {}
    for header in headers_list:
        if header.get('value'):
            request_headers[header.get('name', '')] = header.get('value', '')
    
    # 构建查询参数
    request_params = {}
    for query in query_list:
        if query.get('value'):
            request_params[query.get('name', '')] = query.get('value', '')
    
    # 构建请求体
    request_body = None
    if api.method in ['POST', 'PUT', 'PATCH'] and body_data:
        if body_type == 'application/json':
            try:
                request_body = json.loads(body_data)
                request_headers['Content-Type'] = 'application/json'
            except json.JSONDecodeError:
                raise HTTPException(status_code=400, detail="Invalid JSON body")
        elif body_type == 'application/xml':
            request_body = body_data
            request_headers['Content-Type'] = 'application/xml'
        elif body_type == 'application/x-www-form-urlencoded':
            # body_data 应该是 URL 编码的形式
            request_body = body_data
            request_headers['Content-Type'] = 'application/x-www-form-urlencoded'
    
    # 发送实际请求
    try:
        if api.method == 'GET':
            response = requests.get(url, headers=request_headers, params=request_params, timeout=30)
        elif api.method == 'POST':
            response = requests.post(url, headers=request_headers, params=request_params, data=request_body if isinstance(request_body, str) else None, json=request_body if isinstance(request_body, dict) else None, timeout=30)
        elif api.method == 'PUT':
            response = requests.put(url, headers=request_headers, params=request_params, data=request_body if isinstance(request_body, str) else None, json=request_body if isinstance(request_body, dict) else None, timeout=30)
        elif api.method == 'DELETE':
            response = requests.delete(url, headers=request_headers, params=request_params, timeout=30)
        elif api.method == 'PATCH':
            response = requests.patch(url, headers=request_headers, params=request_params, data=request_body if isinstance(request_body, str) else None, json=request_body if isinstance(request_body, dict) else None, timeout=30)
        else:
            raise HTTPException(status_code=400, detail=f"Unsupported method: {api.method}")
        
        # 获取响应内容
        try:
            response_body = response.json()
        except:
            response_body = response.text
        
        return {
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "body": response_body,
        }
    except requests.exceptions.Timeout:
        raise HTTPException(status_code=504, detail="Request timeout")
    except requests.exceptions.RequestException as e:
        raise HTTPException(status_code=502, detail=f"Request failed: {str(e)}")


@router.post("/projects/{project_id}/apis/{api_id}/extract-constraints")
async def extract_constraints_with_ai(
    project_id: int,
    api_id: int,
    user_prompt: str = Form(default=""),
    constraint_type: str = Form(default="single"),
    db: Session = Depends(get_db),
):
    """
    同步提取 API 的参数约束或依赖关系，在后台执行但等待完成后返回
    
    Args:
        project_id: 项目ID
        api_id: API ID
        user_prompt: 用户提供的额外约束需求描述（可选）
        constraint_type: 约束类型 - 'single' (单参数约束) 或 'dependency' (多参数依赖)
    
    Returns:
        提取的约束或依赖关系列表
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    try:
        print(f"开始提取约束，API ID: {api_id}, 类型: {constraint_type}")
        
        # 从项目的 OpenAPI 文档中获取该 API 的完整定义
        swagger_doc = None
        
        if project.openapi_content:
            try:
                api_spec = project.openapi_content if isinstance(project.openapi_content, dict) else json.loads(project.openapi_content)
                paths = api_spec.get("paths", {})
                api_url_normalized = api.url.rstrip('/').lower() if api.url else ""
                
                for path, path_item in paths.items():
                    path_normalized = path.rstrip('/').lower()
                    if path_normalized == api_url_normalized:
                        method_lower = api.method.lower() if api.method else ""
                        if method_lower in path_item:
                            swagger_doc = path_item[method_lower]
                            break
            except Exception as e:
                print(f"⚠️ 解析 OpenAPI 失败: {str(e)}")
        
        if not swagger_doc:
            swagger_doc = {}
        
        if "parameters" not in swagger_doc:
            swagger_doc["parameters"] = []
        
        # 添加查询参数
        query_params = api.query_params or []
        if query_params and isinstance(query_params, list):
            existing_param_names = {p.get("name") for p in swagger_doc["parameters"] if p.get("in") == "query"}
            for param in query_params:
                param_name = param.get("name", "") if isinstance(param, dict) else ""
                if param_name and param_name not in existing_param_names:
                    swagger_doc["parameters"].append({
                        "name": param_name,
                        "in": "query",
                        "required": param.get("required", False) if isinstance(param, dict) else False,
                        "description": param.get("description", "") if isinstance(param, dict) else "",
                        "schema": {"type": param.get("type", "string") if isinstance(param, dict) else "string"}
                    })
        
        # 添加路径参数
        path_params = api.path_params or []
        if path_params and isinstance(path_params, list):
            existing_param_names = {p.get("name") for p in swagger_doc["parameters"] if p.get("in") == "path"}
            for param in path_params:
                param_name = param.get("name", "") if isinstance(param, dict) else ""
                if param_name and param_name not in existing_param_names:
                    swagger_doc["parameters"].append({
                        "name": param_name,
                        "in": "path",
                        "required": True,
                        "description": param.get("description", "") if isinstance(param, dict) else "",
                        "schema": {"type": param.get("type", "string") if isinstance(param, dict) else "string"}
                    })
        
        # 添加请求头参数
        headers_params = api.headers_params or []
        if headers_params and isinstance(headers_params, list):
            existing_param_names = {p.get("name") for p in swagger_doc["parameters"] if p.get("in") == "header"}
            for param in headers_params:
                param_name = param.get("name", "") if isinstance(param, dict) else ""
                if param_name and param_name not in existing_param_names:
                    swagger_doc["parameters"].append({
                        "name": param_name,
                        "in": "header",
                        "required": param.get("required", False) if isinstance(param, dict) else False,
                        "description": param.get("description", "") if isinstance(param, dict) else "",
                        "schema": {"type": param.get("type", "string") if isinstance(param, dict) else "string"}
                    })
        
        # 添加请求体参数 - 优先从数据库读取
        request_bodies = db.query(ApiRequestBody).filter(ApiRequestBody.api_id == api_id).all()
        if request_bodies:
            for rb in request_bodies:
                if rb.is_default:
                    swagger_doc["requestBody"] = {
                        "required": True,
                        "content": {
                            rb.media_type: {"schema": rb.body_params or {}}
                        }
                    }
                    break
        # 如果数据库没有，则保持 OpenAPI 文档中的 requestBody（如果有的话）
        
        # 使用 AI 提取约束或依赖关系
        print(f"🔄 准备调用 AI 提取器...")
        extractor = get_extractor()
        result_data = None
        
        if constraint_type == "dependency":
            print(f"📍 提取参数依赖关系")
            result_data = extractor.extract_parameter_dependencies(swagger_doc, project_id=project_id, api_id=api_id, db=db)
            api.dependencies = result_data
        else:
            print(f"📍 提取单参数约束")
            result_data = extractor.extract_single_constraints_from_swagger(
                swagger_doc, 
                user_prompt=user_prompt if user_prompt and user_prompt.strip() else None
            )
            api.single_constraints = result_data
        
        print(f"✅ 约束提取成功，数据类型: {type(result_data)}, 数据: {result_data}")
        
        db.commit()
        
        # 确保返回正确的格式
        result_count = 0
        if isinstance(result_data, dict):
            result_count = len(result_data)
        elif isinstance(result_data, list):
            result_count = len(result_data)
        
        return {
            "status": "success",
            "type": constraint_type,
            "data": result_data if result_data else {},
            "count": result_count
        }
        
    except Exception as e:
        import traceback
        error_detail = traceback.format_exc()
        print(f"❌ 约束提取失败: {str(e)}\n{error_detail}")
        raise HTTPException(status_code=500, detail=f"Failed to extract constraints: {str(e)}")


@router.post("/projects/{project_id}/apis/{api_id}/save-dependencies")
async def save_dependencies(
    project_id: int,
    api_id: int,
    dependencies: str = Form(...),  # JSON string
    db: Session = Depends(get_db),
):
    """
    保存提取的参数依赖关系到数据库
    
    Args:
        project_id: 项目ID
        api_id: API ID
        dependencies: 依赖关系的 JSON 字符串数组
    """
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    try:
        # 解析 JSON
        deps_list = json.loads(dependencies) if isinstance(dependencies, str) else dependencies
        
        # 验证格式
        if not isinstance(deps_list, list):
            raise ValueError("Dependencies must be a list")
        
        # 验证每个依赖的结构
        validated_deps = []
        for dep in deps_list:
            if not isinstance(dep, dict):
                continue
            
            # 支持新格式：name, parameters (with location), constraint
            validated_dep = {
                "name": str(dep.get("name", "")).strip(),
                "parameters": dep.get("parameters", []) if isinstance(dep.get("parameters"), list) else [],
                "constraint": str(dep.get("constraint", "")).strip()
            }
            
            # 验证 parameters 格式，确保每个参数都有 name 和 location
            if validated_dep["parameters"]:
                valid_params = []
                for param in validated_dep["parameters"]:
                    if isinstance(param, dict) and "name" in param and "location" in param:
                        valid_params.append({
                            "name": str(param["name"]).strip(),
                            "location": param["location"]
                        })
                validated_dep["parameters"] = valid_params
            
            # 只添加有参数的依赖
            if validated_dep["parameters"]:
                validated_deps.append(validated_dep)
        
        # 保存到数据库
        api.dependencies = validated_deps
        db.commit()
        
        return {
            "status": "success",
            "message": f"Saved {len(validated_deps)} dependencies",
            "count": len(validated_deps)
        }
    
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save dependencies: {str(e)}")


@router.post("/projects/{project_id}/apis/{api_id}/generate-partition")
async def generate_parameter_partition(
    project_id: int,
    api_id: int,
    db: Session = Depends(get_db),
):
    """
    为指定 API 生成单参数输入空间划分
    """
    # 获取 API
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    try:
        # 解析 Swagger 文档
        swagger_doc = json.loads(api.swagger_doc) if isinstance(api.swagger_doc, str) else api.swagger_doc
        
        # 获取已保存的 swagger_operation（从 Swagger 文档中提取）
        if not swagger_doc:
            raise HTTPException(status_code=400, detail="API Swagger document is empty")
        
        # 构建 swagger_operation（简化版，包含必要的信息）
        swagger_operation = {
            "summary": swagger_doc.get("summary", ""),
            "description": swagger_doc.get("description", ""),
            "parameters": swagger_doc.get("parameters", []),
            "requestBody": swagger_doc.get("requestBody", {}),
            "responses": swagger_doc.get("responses", {}),
        }
        
        # 收集参数：query, path, headers, body
        parameters = {
            "query": api.query_params or [],
            "path": api.path_params or [],
            "headers": api.headers_params or [],
            "body": []
        }
        
        # 从 ApiRequestBody 获取 body 参数
        request_bodies = db.query(ApiRequestBody).filter(ApiRequestBody.api_id == api_id).all()
        if request_bodies:
            for rb in request_bodies:
                body_params = rb.body_params if isinstance(rb.body_params, list) else []
                parameters["body"].extend(body_params if isinstance(body_params, list) else [])
        
        # 获取单参数约束
        single_constraints = {}
        if api.single_constraints:
            single_constraints = api.single_constraints if isinstance(api.single_constraints, dict) else json.loads(api.single_constraints)
        
        # 生成输入空间划分
        partitions = generate_single_parameter_partition(
            api_id=api_id,
            swagger_operation=swagger_operation,
            parameters=parameters,
            single_constraints=single_constraints,
        )
        
        return {
            "status": "success",
            "message": f"Generated partitions for {len(partitions)} parameters",
            "partitions": partitions,
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid data: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate partition: {str(e)}")


@router.post("/projects/{project_id}/apis/{api_id}/generate-combi-partition")
async def generate_combi_partition_endpoint(
    project_id: int,
    api_id: int,
    db: Session = Depends(get_db),
):
    """
    为指定 API 生成多参数输入空间划分（基于参数间依赖关系）
    """
    # 获取 API
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    try:
        # 收集参数
        parameters = {
            "query": api.query_params or [],
            "path": api.path_params or [],
            "headers": api.headers_params or [],
            "body": []
        }
        
        # 从 ApiRequestBody 获取 body 参数
        request_bodies = db.query(ApiRequestBody).filter(ApiRequestBody.api_id == api_id).all()
        if request_bodies:
            for rb in request_bodies:
                body_params = rb.body_params if isinstance(rb.body_params, list) else []
                parameters["body"].extend(body_params if isinstance(body_params, list) else [])
        
        # 获取参数间依赖关系
        dependencies = []
        if api.dependencies:
            dependencies = api.dependencies if isinstance(api.dependencies, list) else json.loads(api.dependencies)
        
        # 获取单参数划分结果（如果有的话）
        # 这可以从缓存或数据库中获取，这里假设为空列表
        single_partitions = []
        
        # 生成多参数输入空间划分
        result = generate_multi_parameter_partition(
            api_id=api_id,
            parameters=parameters,
            dependencies=dependencies,
            single_partitions=single_partitions,
        )
        
        return {
            "status": "success",
            "message": f"Generated {len(result.get('spaces', []))} parameter spaces",
            "result": result,
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid data: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate multi-parameter partition: {str(e)}")


@router.post("/projects/{project_id}/apis/{api_id}/generate-mixed-partition")
async def generate_mixed_partition_endpoint(
    project_id: int,
    api_id: int,
    db: Session = Depends(get_db),
):
    """
    为指定 API 生成混合输入空间划分（包含单参数和多参数空间）
    """
    # 获取 API
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    try:
        # 收集参数
        parameters = {
            "query": api.query_params or [],
            "path": api.path_params or [],
            "headers": api.headers_params or [],
            "body": []
        }
        
        # 从 ApiRequestBody 获取 body 参数
        request_bodies = db.query(ApiRequestBody).filter(ApiRequestBody.api_id == api_id).all()
        if request_bodies:
            for rb in request_bodies:
                body_params = rb.body_params if isinstance(rb.body_params, list) else []
                parameters["body"].extend(body_params if isinstance(body_params, list) else [])
        
        # 获取参数间依赖关系
        dependencies = []
        if api.dependencies:
            dependencies = api.dependencies if isinstance(api.dependencies, list) else json.loads(api.dependencies)
        
        # 生成混合输入空间划分
        result = generate_mixed_parameter_partition(
            api_id=api_id,
            parameters=parameters,
            dependencies=dependencies,
        )
        
        return {
            "status": "success",
            "message": "Generated mixed parameter spaces",
            "result": result,
        }
    
    except ValueError as e:
        raise HTTPException(status_code=400, detail=f"Invalid data: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to generate mixed partition: {str(e)}")


def _partition_inputs(db, api, request_body):
    # 读取 API 已保存的单参数约束
    all_single_constraints = api.single_constraints or {}

    if isinstance(all_single_constraints, str):
        all_single_constraints = json.loads(all_single_constraints)

    if not isinstance(all_single_constraints, dict):
        raise HTTPException(
            status_code=400,
            detail="单参数约束数据格式错误"
        )

    # 读取 API 已保存的多参数依赖
    all_dependencies = api.dependencies or []

    if isinstance(all_dependencies, str):
        all_dependencies = json.loads(all_dependencies)

    if all_dependencies == {}:
        all_dependencies = []

    if not isinstance(all_dependencies, list):
        raise HTTPException(
            status_code=400,
            detail="多参数依赖数据格式错误"
        )

    # 构建当前 API 所有合法的约束 ID
    available_ids = {
        f"single_{name}"
        for name in all_single_constraints
    }

    available_ids.update({
        f"dep_{index}"
        for index in range(len(all_dependencies))
    })

    # 读取本次用户选择
    payload = request_body or {}

    if "selected_constraints" not in payload:
        # 兼容旧请求：默认使用全部约束
        selected_ids = available_ids
    else:
        requested_ids = payload["selected_constraints"]

        if (
            not isinstance(requested_ids, list)
            or any(not isinstance(item, str) for item in requested_ids)
        ):
            raise HTTPException(
                status_code=400,
                detail="selected_constraints 必须是字符串数组"
            )

        selected_ids = set(requested_ids)

        # 拒绝不存在的约束 ID
        unknown_ids = selected_ids - available_ids

        if unknown_ids:
            raise HTTPException(
                status_code=400,
                detail=f"未知的约束 ID: {sorted(unknown_ids)}"
            )

    # 筛选单参数约束
    selected_single_constraints = {
        name: constraint
        for name, constraint in all_single_constraints.items()
        if f"single_{name}" in selected_ids
    }

    # 筛选多参数依赖
    selected_dependencies = [
        dependency
        for index, dependency in enumerate(all_dependencies)
        if f"dep_{index}" in selected_ids
    ]

    swagger_doc = json.loads(api.swagger_doc) if isinstance(api.swagger_doc, str) else api.swagger_doc
    if not isinstance(swagger_doc, dict) or not swagger_doc:
        raise HTTPException(status_code=400, detail="API Swagger document is empty or invalid")
    bodies = db.query(ApiRequestBody).filter(ApiRequestBody.api_id == api.id).order_by(ApiRequestBody.id).all()
    parameters = {"query": api.query_params or [], "path": api.path_params or [],
                  "headers": api.headers_params or [], "body": []}
    body_metadata = []
    for body in bodies:
        parameters["body"].extend(body.body_params if isinstance(body.body_params, list) else [])
        body_metadata.append({"media_type": body.media_type, "is_default": body.is_default,
                              "body_params": body.body_params})
    snapshot = {"version": 1, "api_id": api.id, "method": api.method, "url": api.url,
                "swagger_doc": swagger_doc, "parameters": parameters, "request_bodies": body_metadata,
                "selected_ids": sorted(selected_ids), "single_constraints": selected_single_constraints,
                "dependencies": selected_dependencies}
    fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True, ensure_ascii=False,
                                            separators=(",", ":"), allow_nan=False).encode()).hexdigest()
    return fingerprint, swagger_doc, parameters, selected_single_constraints, selected_dependencies


def _partition_records(db, api_id):
    return [db.query(model).filter(model.api_id == api_id).order_by(model.id).all()
            for model in (SingleInputSpace, MultiInputSpace, MixedInputSpace)]


def _partition_snapshot(db, api_id):
    # One SQLite statement has one consistent read view, including in legacy
    # sqlite3 transaction mode. Column reads also bypass the ORM identity map.
    models = (SingleInputSpace, MultiInputSpace, MixedInputSpace)
    statement = union_all(*[
        select(literal(index).label("kind"), model.spaces, model.input_fingerprint)
        .where(model.api_id == api_id) for index, model in enumerate(models)
    ])
    records = [[], [], []]
    for kind, spaces, fingerprint in db.execute(statement):
        records[kind].append(SimpleNamespace(spaces=deepcopy(spaces), input_fingerprint=fingerprint))
    return records


def _valid_partition_spaces(spaces, single=False, mixed=False):
    def name(value):
        return isinstance(value, str) and bool(value.strip())

    def category(value, typed=False):
        return (isinstance(value, dict) and name(value.get("category_name"))
                and (not typed or value.get("type") in ("valid", "invalid"))
                and ("type" not in value or value["type"] in ("valid", "invalid"))
                and (typed or bool(value.get("sample_values")) or "sample_value" in value
                     or bool(value.get("description")))
                and ("description" not in value or isinstance(value["description"], str))
                and ("sample_values" not in value or isinstance(value["sample_values"], list)))

    def categories(values, typed=False):
        return isinstance(values, list) and bool(values) and all(category(v, typed) for v in values)

    if single:
        if isinstance(spaces, dict):
            return bool(spaces) and all(name(k) and categories(v) for k, v in spaces.items())
        if not isinstance(spaces, list) or not spaces:
            return False
        names = []
        for group in spaces:
            if not isinstance(group, dict) or not name(group.get("param_name")):
                return False
            names.append(group["param_name"])
            if any(not isinstance(group.get(kind), list) for kind in ("valid", "invalid")):
                return False
            values = group["valid"] + group["invalid"]
            if not categories(values):
                return False
        return len(names) == len(set(names))
    if not isinstance(spaces, list) or not spaces:
        return False
    for space in spaces:
        if not isinstance(space, dict) or not isinstance(space.get("subspaces"), list) or not space["subspaces"]:
            return False
        for item in [space] + space["subspaces"]:
            if not isinstance(item, dict):
                return False
            if "parameter_list" in item and (not isinstance(item["parameter_list"], list)
                    or not all(name(v) for v in item["parameter_list"])):
                return False
        for sub in space["subspaces"]:
            if sub.get("type") not in ("valid", "invalid") or not isinstance(sub.get("description"), str):
                return False
            mapping = sub.get("single_parameter_spaces", {})
            if (mixed and "single_parameter_spaces" not in sub) or not isinstance(mapping, dict):
                return False
            if not all(name(k) and categories(v, typed=True) for k, v in mapping.items()):
                return False
            if mixed and not set(sub.get("parameter_list", [])).issubset(mapping):
                return False
    return True


def _partition_cache_valid(records, fingerprint):
    return all(len(rows) == 1 and rows[0].input_fingerprint == fingerprint
               and _valid_partition_spaces(rows[0].spaces, single=index == 0, mixed=index == 2)
               for index, rows in enumerate(records))


@router.post("/projects/{project_id}/apis/{api_id}/generate-full-partition")
async def generate_full_partition_pipeline(
    project_id: int,
    api_id: int,
    request_body: dict | None = Body(default=None),
    db: Session = Depends(get_db),
):
    """Reuse a complete matching snapshot, or atomically replace all three spaces."""
    try:
        api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
        if not api:
            raise HTTPException(status_code=404, detail="API not found")
        fingerprint, swagger_doc, parameters, constraints, dependencies = _partition_inputs(db, api, request_body)
        records = _partition_snapshot(db, api_id)
        from_cache = _partition_cache_valid(records, fingerprint)
        if from_cache:
            single_spaces, multi_spaces_before, multi_spaces_after = [rows[0].spaces for rows in records]
        else:
            # Keep generated values in memory; no cache writes until every stage succeeds.
            single_spaces = generate_single_parameter_partition(
                api_id=api_id, swagger_operation={key: swagger_doc.get(key, default) for key, default in
                    (("summary", ""), ("description", ""), ("parameters", []), ("requestBody", {}), ("responses", {}))},
                parameters=parameters, single_constraints=constraints)
            if not _valid_partition_spaces(single_spaces, single=True):
                raise ValueError("单参数空间生成失败或为空")
            multi_result = generate_multi_parameter_partition(
                api_id=api_id, parameters=parameters, dependencies=dependencies, single_partitions=single_spaces)
            multi_spaces_before = multi_result.get("spaces") if isinstance(multi_result, dict) else None
            if not _valid_partition_spaces(multi_spaces_before):
                raise ValueError("多参数空间生成失败或为空")
            single_mapping = single_spaces
            if isinstance(single_spaces, list):
                single_mapping = {}
                for group in single_spaces:
                    if group.get("param_name"):
                        single_mapping[group["param_name"]] = [
                            {"category_name": cat.get("category_name", "有效分类" if kind == "valid" else "无效分类"),
                             "description": cat.get("description", ""), "type": kind}
                            for kind in ("valid", "invalid") for cat in group.get(kind, [])]
            mixed_result = generate_mixed_parameter_partition(
                api_id=api_id, multi_parameter_spaces=multi_spaces_before, single_parameter_spaces=single_mapping)
            multi_spaces_after = mixed_result.get("multi_parameter_spaces") if isinstance(mixed_result, dict) else None
            if not _valid_partition_spaces(multi_spaces_after, mixed=True):
                raise ValueError("混合空间生成失败或为空")
            # End the read transaction and recheck persisted inputs after slow generation.
            db.rollback()
            db.expire_all()
            current_api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
            if current_api is None or _partition_inputs(db, current_api, request_body)[0] != fingerprint:
                raise HTTPException(status_code=409, detail="生成期间 API 输入已变化，请重试；原缓存已保留")
            records = _partition_records(db, api_id)
            timestamp = datetime.now().isoformat()
            # Update the first row and remove only redundant cache rows in one transaction.
            for model, rows, spaces in zip((SingleInputSpace, MultiInputSpace, MixedInputSpace), records,
                                           (single_spaces, multi_spaces_before, multi_spaces_after)):
                record = rows[0] if rows else model(api_id=api_id, created_at=timestamp)
                for duplicate in rows[1:]:
                    db.delete(duplicate)
                record.spaces = spaces
                record.input_fingerprint = fingerprint
                record.updated_at = timestamp
                db.add(record)
            db.commit()
        return {
            "status": "success", "message": "Reused matching cache" if from_cache else "Generated successfully",
            "api_id": api_id, "input_fingerprint": fingerprint,
            "from_cache": {"sisp": from_cache, "misp": from_cache, "mixed": from_cache},
            "single_spaces": {"count": len(single_spaces), "parameters": list(single_spaces) if isinstance(single_spaces, dict)
                              else [s.get("param_name") for s in single_spaces]},
            "multi_spaces_before": {"count": len(multi_spaces_before), "spaces": multi_spaces_before[:2]},
            "multi_spaces_after": {"count": len(multi_spaces_after), "spaces": multi_spaces_after[:2]}}
    except HTTPException:
        db.rollback()
        raise
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Invalid data: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to generate full partition pipeline: {str(e)}")


@router.post("/projects/{project_id}/apis/{api_id}/generate-test-cases")
async def generate_test_cases_endpoint(
    project_id: int,
    api_id: int,
    request_body: dict = Body(...),
    db: Session = Depends(get_db),
):
    """
    为指定 API 生成测试用例
    
    流程：
    1. 读取缓存的混合空间（MixedInputSpace）
    2. 读取缓存的单参数空间（SingleInputSpace）
    3. 使用测试用例生成器生成具体的测试用例 (可指定数量)
    4. 返回生成的测试用例列表
    """
    try:
        # 获取请求体中的参数
        desired_count = request_body.get("count")
        if type(desired_count) is not int or not 1 <= desired_count <= 50:
            raise HTTPException(status_code=400, detail="count 必须为 1-50 的整数，不接受布尔值、小数、字符串或空值")
        case_types = request_body.get("case_types", ["positive", "negative"])
        if (not isinstance(case_types, list) or not case_types
                or any(not isinstance(t, str) or t not in ("positive", "negative") for t in case_types)):
            raise HTTPException(status_code=400, detail="case_types 必须为非空数组，且只能包含 positive、negative")
        case_types = list(dict.fromkeys(case_types))
        
        # 获取 API
        api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
        if not api:
            raise HTTPException(status_code=404, detail="API not found")
        
        # Legacy count-only callers mean "all constraints" and must also use valid caches.
        current, *_ = _partition_inputs(db, api, request_body)
        explicit_snapshot = "input_fingerprint" in request_body or "selected_constraints" in request_body
        if explicit_snapshot:
            expected = request_body.get("input_fingerprint")
            if not isinstance(expected, str) or not expected:
                raise HTTPException(status_code=400, detail="缺少输入指纹，请重新生成输入空间")
            if expected != current:
                raise HTTPException(status_code=409, detail="输入空间与本次约束选择不一致，请重新生成输入空间")
        records = _partition_snapshot(db, api_id)
        if not _partition_cache_valid(records, current):
            raise HTTPException(status_code=409 if explicit_snapshot else 400,
                                detail="输入空间不完整、已过期或与本次约束选择不一致，请重新生成输入空间")

        # Use precisely the detached values whose fingerprints were validated.
        single_spaces = records[0][0].spaces
        mixed_spaces = records[2][0].spaces

        # 【调试】打印 single_spaces 的详细结构
        print(f"\n📊 【single_spaces 详细结构调试】")
        print(f"  Type: {type(single_spaces)}")
        if isinstance(single_spaces, list) and single_spaces:
            first_param = single_spaces[0]
            print(f"  第1个参数: {first_param.get('param_name', 'N/A')}")
            if first_param.get('valid'):
                first_valid = first_param['valid'][0]
                print(f"    - 第1个valid分类: {first_valid.get('category_name', 'N/A')}")
                print(f"    - sample_values: {first_valid.get('sample_values', [])}")
                print(f"    - sample_values 类型: {type(first_valid.get('sample_values', []))}")
                print(f"    - sample_values 长度: {len(first_valid.get('sample_values', []))}")
        
        # 构建 API 信息
        api_info = {
            "method": api.method,
            "url": api.url,
            "query_params": api.query_params or [],
            "path_params": api.path_params or [],
            "headers_params": api.headers_params or [],
            "body_params": [],
        }
        
        # 从 ApiRequestBody 获取 body 参数
        request_bodies = db.query(ApiRequestBody).filter(ApiRequestBody.api_id == api_id).all()
        if request_bodies:
            for rb in request_bodies:
                body_params = rb.body_params if isinstance(rb.body_params, list) else []
                api_info["body_params"].extend(body_params if isinstance(body_params, list) else [])
        
        # 使用测试用例生成器
        from services.testcase.testcase_generator import TestCaseGenerator
        
        generator = TestCaseGenerator()
        test_cases = generator.generate_test_cases(
            api_id=api_id,
            api_info=api_info,
            mixed_spaces=mixed_spaces,
            single_spaces=single_spaces,
            desired_count=desired_count,
            case_types=case_types,
        )

        if not test_cases:
            raise HTTPException(status_code=400, detail="所选类型没有可生成的候选组合，原用例和采纳记录已保留")

        # 生成后直接落库，保证前端刷新后可见
        db.query(TestCase).filter(TestCase.api_id == api_id).delete()
        db.query(AdoptedTestCase).filter(AdoptedTestCase.api_id == api_id).delete()

        timestamp = datetime.now().isoformat()
        for tc_data in test_cases:
            test_case = TestCase(
                api_id=api_id,
                name=tc_data.get("name", ""),
                description=tc_data.get("description", ""),
                headers_params=tc_data.get("headers_params", {}),
                query_params=tc_data.get("query_params", {}),
                path_params=tc_data.get("path_params", {}),
                body_params=tc_data.get("body_params", {}),
                expected_status=str(tc_data.get("expected_status", "200")),
                case_type=tc_data.get("case_type", "正向用例"),
                adopted=0,
                created_at=timestamp,
            )
            db.add(test_case)

        db.commit()
        
        print(f"✅ 成功生成测试用例，API ID: {api_id}，数量: {len(test_cases)} (请求: {desired_count})")
        
        # 调试：打印第一个用例
        if test_cases:
            print(f"\n📋 【第1个测试用例数据】")
            print(json.dumps(test_cases[0], indent=2, ensure_ascii=False, default=str))
        
        return {
            "status": "success",
            "api_id": api_id,
            "message": (f"所选类型候选组合不足：请求 {desired_count} 条，实际生成 {len(test_cases)} 条"
                        if len(test_cases) < desired_count else f"成功生成 {len(test_cases)} 条用例"),
            "requested_count": desired_count,
            "actual_count": len(test_cases),
            "case_types": case_types,
            "count": len(test_cases),
            "test_cases": test_cases,
        }
    
    except HTTPException:
        db.rollback()
        raise
    except ValueError as e:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"Invalid data: {str(e)}")
    except Exception as e:
        db.rollback()
        import traceback
        error_detail = traceback.format_exc()
        print(f"❌ 测试用例生成失败: {str(e)}\n{error_detail}")
        raise HTTPException(status_code=500, detail=f"Failed to generate test cases: {str(e)}")


@router.get("/projects/{project_id}/apis/{api_id}/test-cases")
async def list_test_cases(project_id: int, api_id: int, db: Session = Depends(get_db)):
    """获取 API 的所有测试用例"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    test_cases = db.query(TestCase).filter(TestCase.api_id == api_id).all()
    
    return {
        "api_id": api_id,
        "count": len(test_cases),
        "test_cases": [
            {
                "id": tc.id,
                "name": tc.name,
                "description": tc.description,
                "headers_params": tc.headers_params or {},
                "query_params": tc.query_params or {},
                "path_params": tc.path_params or {},
                "body_params": tc.body_params or {},
                "expected_status": tc.expected_status or "200",
                "case_type": tc.case_type or "正向用例",
                "status": "已采纳" if tc.adopted else "未采纳",
                "adopted": bool(tc.adopted),
                "created_at": tc.created_at,
            }
            for tc in test_cases
        ]
    }


@router.post("/projects/{project_id}/apis/{api_id}/save-test-cases")
async def save_test_cases(
    project_id: int,
    api_id: int,
    test_cases_data: str = Form(...),  # JSON string
    db: Session = Depends(get_db),
):
    """保存生成的测试用例到数据库"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    try:
        # 解析 JSON
        test_cases_list = json.loads(test_cases_data) if isinstance(test_cases_data, str) else test_cases_data
        
        if not isinstance(test_cases_list, list):
            raise ValueError("test_cases must be a list")
        
        # 删除旧的测试用例
        db.query(TestCase).filter(TestCase.api_id == api_id).delete()
        db.query(AdoptedTestCase).filter(AdoptedTestCase.api_id == api_id).delete()
        
        timestamp = datetime.now().isoformat()
        # 保存新的测试用例
        saved_count = 0
        for tc_data in test_cases_list:
            if not isinstance(tc_data, dict):
                continue
            
            test_case = TestCase(
                api_id=api_id,
                name=tc_data.get("name", ""),
                description=tc_data.get("description", ""),
                headers_params=tc_data.get("headers_params", {}),
                query_params=tc_data.get("query_params", {}),
                path_params=tc_data.get("path_params", {}),
                body_params=tc_data.get("body_params", {}),
                expected_status=str(tc_data.get("expected_status", tc_data.get("expectedStatus", "200"))),
                case_type=tc_data.get("case_type", "正向用例"),
                adopted=1 if tc_data.get("status") == "已采纳" or tc_data.get("adopted") else 0,
                created_at=timestamp,
            )
            db.add(test_case)
            saved_count += 1
        
        db.commit()
        print(f"✅ 保存测试用例，API ID: {api_id}，数量: {saved_count}")
        
        return {
            "status": "success",
            "message": f"Saved {saved_count} test cases",
            "count": saved_count
        }
    
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to save test cases: {str(e)}")


@router.get("/projects/{project_id}/apis/{api_id}/adopted-test-cases")
async def list_adopted_test_cases(project_id: int, api_id: int, db: Session = Depends(get_db)):
    """获取 API 的所有已采纳测试用例"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")
    
    adopted_cases = db.query(AdoptedTestCase).filter(AdoptedTestCase.api_id == api_id).all()
    
    return {
        "api_id": api_id,
        "count": len(adopted_cases),
        "adopted_test_cases": [
            {
                "id": tc.id,
                "name": tc.name,
                "description": tc.description,
                "headers_params": tc.headers_params or {},
                "query_params": tc.query_params or {},
                "path_params": tc.path_params or {},
                "body_params": tc.body_params or {},
                "expected_status": tc.expected_status,
                "source_test_case_id": tc.source_test_case_id,
                "created_at": tc.created_at,
            }
            for tc in adopted_cases
        ]
    }


@router.post("/projects/{project_id}/apis/{api_id}/adopt-test-cases")
async def adopt_test_cases(
    project_id: int,
    api_id: int,
    test_cases_data: str = Form(...),  # JSON string
    db: Session = Depends(get_db),
):
    """采纳指定的测试用例到已采纳列表（可以全部采纳或部分采纳）"""
    try:
        project = db.query(Project).filter(Project.id == project_id).first()
        if not project:
            raise HTTPException(status_code=404, detail="Project not found")
        
        api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
        if not api:
            raise HTTPException(status_code=404, detail="API not found")
        
        # 解析测试用例数据
        test_cases_list = json.loads(test_cases_data) if isinstance(test_cases_data, str) else test_cases_data
        
        if not isinstance(test_cases_list, list):
            raise ValueError("test_cases must be a list")
        
        # 保存采纳的测试用例
        adopted_count = 0
        timestamp = datetime.now().isoformat()
        
        for tc_data in test_cases_list:
            if not isinstance(tc_data, dict):
                continue

            source_id = tc_data.get("id")

            if source_id:
                source_case = db.query(TestCase).filter(
                    TestCase.id == source_id,
                    TestCase.api_id == api_id
                ).first()
                if source_case:
                    source_case.adopted = 1

            if source_id:
                db.query(AdoptedTestCase).filter(
                    AdoptedTestCase.api_id == api_id,
                    AdoptedTestCase.source_test_case_id == source_id,
                ).delete()
            
            adopted_case = AdoptedTestCase(
                api_id=api_id,
                source_test_case_id=source_id,
                name=tc_data.get("name", ""),
                description=tc_data.get("description", ""),
                headers_params=tc_data.get("headers_params", {}),
                query_params=tc_data.get("query_params", {}),
                path_params=tc_data.get("path_params", {}),
                body_params=tc_data.get("body_params", {}),
                expected_status=str(tc_data.get("expected_status", tc_data.get("expectedStatus", "200"))),
                created_at=timestamp,
            )
            db.add(adopted_case)
            adopted_count += 1
        
        db.commit()
        print(f"✅ 采纳测试用例，API ID: {api_id}，数量: {adopted_count}")
        
        return {
            "status": "success",
            "message": f"Adopted {adopted_count} test cases",
            "count": adopted_count
        }
    
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=400, detail=f"Invalid JSON: {str(e)}")
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to adopt test cases: {str(e)}")


@router.delete("/projects/{project_id}/apis/{api_id}/test-cases/{test_case_id}")
async def delete_test_case(
    project_id: int,
    api_id: int,
    test_case_id: int,
    db: Session = Depends(get_db),
):
    """删除测试用例，并清理对应的已采纳记录。"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")

    test_case = db.query(TestCase).filter(TestCase.id == test_case_id, TestCase.api_id == api_id).first()
    if not test_case:
        raise HTTPException(status_code=404, detail="Test case not found")

    try:
        db.query(AdoptedTestCase).filter(
            AdoptedTestCase.api_id == api_id,
            AdoptedTestCase.source_test_case_id == test_case_id,
        ).delete()
        db.delete(test_case)
        db.commit()
        return {"status": "success", "message": "Test case deleted"}
    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to delete test case: {str(e)}")


def _calc_elapsed_seconds(task: TestRunTask):
    if not task.started_at:
        return 0
    try:
        start_dt = datetime.fromisoformat(task.started_at)
        end_dt = datetime.fromisoformat(task.finished_at) if task.finished_at else datetime.now()
        return max(0, int((end_dt - start_dt).total_seconds()))
    except Exception:
        return 0


@router.post("/projects/{project_id}/apis/{api_id}/run-adopted-test-cases")
async def run_adopted_test_cases(
    project_id: int,
    api_id: int,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    """创建异步测试任务，执行指定 API 的全部已采纳用例。"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    api = db.query(Api).filter(Api.id == api_id, Api.project_id == project_id).first()
    if not api:
        raise HTTPException(status_code=404, detail="API not found")

    adopted_cases_count = db.query(AdoptedTestCase).filter(AdoptedTestCase.api_id == api_id).count()
    if adopted_cases_count == 0:
        raise HTTPException(status_code=400, detail="No adopted test cases found")

    now = datetime.now().isoformat()
    task = TestRunTask(
        project_id=project_id,
        api_id=api_id,
        status="pending",
        total_cases=adopted_cases_count,
        executed_cases=0,
        passed_cases=0,
        failed_cases=0,
        created_at=now,
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    background_tasks.add_task(_run_test_task, task.id)

    return {
        "status": "success",
        "task_id": task.id,
        "message": f"Task created with {adopted_cases_count} adopted cases",
    }


@router.get("/test-run-tasks")
async def list_test_run_tasks(
    project_id: int = None,
    db: Session = Depends(get_db),
):
    """任务列表：用于测试报告页轮询展示。"""
    query = db.query(TestRunTask)
    if project_id is not None:
        query = query.filter(TestRunTask.project_id == project_id)

    tasks = query.order_by(TestRunTask.id.desc()).all()
    return {
        "count": len(tasks),
        "tasks": [
            {
                "id": t.id,
                "project_id": t.project_id,
                "api_id": t.api_id,
                "status": t.status,
                "total_cases": t.total_cases,
                "executed_cases": t.executed_cases,
                "passed_cases": t.passed_cases,
                "failed_cases": t.failed_cases,
                "pass_rate": (t.passed_cases / t.executed_cases) if t.executed_cases else 0,
                "elapsed_seconds": _calc_elapsed_seconds(t),
                "created_at": t.created_at,
                "started_at": t.started_at,
                "finished_at": t.finished_at,
                "error_message": t.error_message,
            }
            for t in tasks
        ],
    }


@router.get("/test-run-tasks/{task_id}")
async def get_test_run_task_detail(task_id: int, db: Session = Depends(get_db)):
    """任务详情：包含核心指标和每条用例执行明细。"""
    task = db.query(TestRunTask).filter(TestRunTask.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    results = db.query(TestRunResult).filter(TestRunResult.task_id == task_id).order_by(TestRunResult.id.asc()).all()

    details = []
    for r in results:
        tc = db.query(TestCase).filter(TestCase.id == r.test_case_id).first() if r.test_case_id else None
        
        # 安全地解析请求信息（可能是JSON字符串）
        request_headers = {}
        request_body = {}
        request_query = {}
        
        if r.request_headers:
            try:
                request_headers = json.loads(r.request_headers) if isinstance(r.request_headers, str) else r.request_headers
            except:
                request_headers = {}
        
        if r.request_body:
            try:
                request_body = json.loads(r.request_body) if isinstance(r.request_body, str) else r.request_body
            except:
                request_body = {}
        
        if r.request_query:
            try:
                request_query = json.loads(r.request_query) if isinstance(r.request_query, str) else r.request_query
            except:
                request_query = {}
        
        details.append(
            {
                "id": r.id,
                "test_case_id": r.test_case_id,
                "test_case_name": tc.name if tc else "(deleted)",
                "test_case_description": tc.description if tc else "",
                "case_type": tc.case_type if tc else "",
                "status": r.status,
                "passed": bool(r.passed),
                "expected_status": r.expected_status,
                "actual_status": r.actual_status,
                "duration_ms": r.duration_ms,
                "error_message": r.error_message,
                "request_url": r.request_url,
                "request_headers": request_headers,
                "request_body": request_body,
                "request_query": request_query,
                "response_headers": r.response_headers or {},
                "response_body": r.response_body,
                "executed_at": r.executed_at,
            }
        )

    return {
        "task": {
            "id": task.id,
            "project_id": task.project_id,
            "api_id": task.api_id,
            "status": task.status,
            "total_cases": task.total_cases,
            "executed_cases": task.executed_cases,
            "passed_cases": task.passed_cases,
            "failed_cases": task.failed_cases,
            "pass_rate": (task.passed_cases / task.executed_cases) if task.executed_cases else 0,
            "elapsed_seconds": _calc_elapsed_seconds(task),
            "created_at": task.created_at,
            "started_at": task.started_at,
            "finished_at": task.finished_at,
            "error_message": task.error_message,
        },
        "details": details,
    }