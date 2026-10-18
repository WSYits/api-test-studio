"""
测试用例生成模块
从混合输入空间生成具体的测试用例

工作流程：
1. 遍历混合空间中的每个 space（参数空间）
2. 对于每个 space 下的每个 subspace（子空间，代表一个测试场景）
3. 从 subspace 的 single_parameter_spaces 映射中获取参数分类
4. 使用分类名称从单参数空间中查找对应的 sample_value
5. 构造完整的测试用例
"""

import json
from typing import Dict, List, Any, Optional, Tuple
from datetime import datetime


class TestCaseGenerator:
    """从混合空间和单参数空间生成测试用例"""
    
    def __init__(self):
        pass
    
    def generate_test_cases(
        self,
        api_id: int,
        api_info: Dict[str, Any],
        mixed_spaces: List[Dict[str, Any]],
        single_spaces: Dict[str, List[Dict[str, Any]]] | List[Dict[str, Any]],
        desired_count: int = 5,
        case_types: Optional[List[str]] = None,
    ) -> List[Dict[str, Any]]:
        """
        生成测试用例 - 按选定类型分配，不重复候选组合
        
        通过笛卡尔积方式：从每个混合空间中选一个子空间，组合起来生成一个测试用例
        特点：
        1. 将组合分为 valid 和 invalid 两类
        2. 单选仅使用选定类型；双选均分，奇数时异常多一条
        3. 正向用例需要验证所有参数都来自 valid 类型的参数空间
        
        Args:
            api_id: API ID
            api_info: API 信息 {method, url, query_params: [], path_params: [], ...}
            mixed_spaces: 混合空间数组
            single_spaces: 单参数空间，可以是：
                         - 字典格式: {param_name: [{category_name, sample_value, description}, ...]}
                         - 数组格式: [{param_name: "...", valid: [...], invalid: [...]}, ...]
            desired_count: 期望生成的用例数量，默认5个
            case_types: positive/negative 数组，省略时兼容双选
        
        Returns:
            测试用例列表
        """
        import random
        
        test_cases = []
        
        # 转换 single_spaces 格式（从数组 -> 字典）
        single_spaces = self._normalize_single_spaces(single_spaces)
        
        # 为每个混合空间收集其子空间列表
        spaces_subspaces = []
        for space in mixed_spaces:
            subspaces = space.get("subspaces", [])
            spaces_subspaces.append((space, subspaces))
        
        if not spaces_subspaces:
            return test_cases
        
        # 笛卡尔积：生成所有子空间组合
        subspace_combinations = self._cartesian_product_subspaces(spaces_subspaces)
        
        # 分离 valid 和 invalid 组合
        valid_combinations = []
        invalid_combinations = []
        
        for combo in subspace_combinations:
            # 检查子空间类型：如果所有都是 valid，则归类为 valid 组合
            subspace_types = [subspace.get("type", "valid") for _, subspace in combo]
            if all(st == "valid" for st in subspace_types):
                valid_combinations.append(combo)
            else:
                invalid_combinations.append(combo)
        
        print(f"\n📊 【用例分类统计】valid 组合: {len(valid_combinations)}, invalid 组合: {len(invalid_combinations)}")
        
        selected_types = set(case_types if case_types is not None else ["positive", "negative"])
        if not selected_types or not selected_types <= {"positive", "negative"}:
            raise ValueError("case_types 必须包含 positive 或 negative")
        if selected_types == {"positive", "negative"}:
            valid_target = desired_count // 2
            invalid_target = desired_count - valid_target
        else:
            valid_target = desired_count if "positive" in selected_types else 0
            invalid_target = desired_count if "negative" in selected_types else 0

        selected_valid = random.sample(valid_combinations, min(len(valid_combinations), valid_target))
        selected_invalid = random.sample(invalid_combinations, min(len(invalid_combinations), invalid_target))
        # 仅从已选类型的剩余候选中补足，保持两个分类列表准确。
        for candidates, selected, case_type in (
            (valid_combinations, selected_valid, "positive"),
            (invalid_combinations, selected_invalid, "negative"),
        ):
            remaining_count = desired_count - len(selected_valid) - len(selected_invalid)
            if remaining_count > 0 and case_type in selected_types:
                remaining = [c for c in candidates if c not in selected]
                selected.extend(random.sample(remaining, min(len(remaining), remaining_count)))

        selected_combinations = selected_valid + selected_invalid

        # 每个组合生成一个测试用例
        for combo_idx, subspace_combo in enumerate(selected_combinations):
            # subspace_combo 是一个列表：[(space, subspace), (space, subspace), ...]
            test_case = self._generate_test_case_from_combination(
                api_id=api_id,
                api_info=api_info,
                combo_idx=combo_idx,
                subspace_combo=subspace_combo,
                single_spaces=single_spaces,
            )
            test_cases.append(test_case)
        
        return test_cases
    
    def _cartesian_product_subspaces(
        self,
        spaces_subspaces: List[Tuple[Dict, List[Dict]]],
    ) -> List[List[Tuple[Dict, Dict]]]:
        """
        生成笛卡尔积：从每个空间中各选一个子空间
        
        Returns:
            [
                [(space0, subspace0), (space1, subspace0), ...],
                [(space0, subspace0), (space1, subspace1), ...],
                ...
            ]
        """
        if not spaces_subspaces:
            return []
        
        # 递归生成笛卡尔积
        def cartesian_helper(index):
            if index == len(spaces_subspaces):
                return [[]]
            
            space, subspaces = spaces_subspaces[index]
            rest_combos = cartesian_helper(index + 1)
            
            result = []
            for subspace in subspaces:
                for rest_combo in rest_combos:
                    result.append([(space, subspace)] + rest_combo)
            
            return result
        
        return cartesian_helper(0)
    
    def _generate_test_case_from_combination(
        self,
        api_id: int,
        api_info: Dict[str, Any],
        combo_idx: int,
        subspace_combo: List[Tuple[Dict, Dict]],
        single_spaces: Dict[str, List[Dict[str, Any]]],
    ) -> Dict[str, Any]:
        """
        从子空间组合生成一个测试用例
        
        Args:
            subspace_combo: [(space, subspace), (space, subspace), ...]
        """
        
        # 合并所有子空间的参数
        merged_single_param_spaces_mapping = {}
        space_names = []
        subspace_descriptions = []
        subspace_types = []
        
        for space, subspace in subspace_combo:
            space_name = space.get("space_name", "")
            space_names.append(space_name)
            
            subspace_description = subspace.get("description", "")
            subspace_descriptions.append(subspace_description)
            
            subspace_type = subspace.get("type", "valid")
            subspace_types.append(subspace_type)
            
            # 合并该子空间的参数映射
            single_param_spaces = subspace.get("single_parameter_spaces", {})
            for param_name, categories in single_param_spaces.items():
                if param_name not in merged_single_param_spaces_mapping:
                    merged_single_param_spaces_mapping[param_name] = []
                merged_single_param_spaces_mapping[param_name].extend(categories)
        
        # 收集所有参数
        all_parameter_list = list(merged_single_param_spaces_mapping.keys())
        
        # 与候选筛选一致：所有子空间均为 valid 才是正向用例
        case_type = "正向用例" if all(st == "valid" for st in subspace_types) else "异常输入"
        must_be_valid = case_type == "正向用例"  # 正向用例需要验证参数都是valid
        
        print(f"\n🔍 【生成测试用例调试信息】API ID: {api_id}, Combo #{combo_idx}, 类型: {case_type}")
        print(f"  空间名称: {space_names}")
        print(f"  合并后的参数列表: {all_parameter_list}")
        print(f"  API 查询参数: {[p.get('name') for p in api_info.get('query_params', [])]}")
        print(f"  API 路径参数: {[p.get('name') for p in api_info.get('path_params', [])]}")
        print(f"  API 请求头参数: {[p.get('name') for p in api_info.get('headers_params', [])]}")
        print(f"  API 请求体参数: {[p.get('name') for p in api_info.get('body_params', [])]}")
        
        # 生成参数值
        query_params, path_params, headers_params, body_params = self._generate_parameters(
            api_info=api_info,
            parameter_list=all_parameter_list,
            single_param_spaces_mapping=merged_single_param_spaces_mapping,
            single_spaces=single_spaces,
            must_be_valid=must_be_valid,
        )
        
        print(f"  生成的查询参数: {query_params}")
        print(f"  生成的路径参数: {path_params}")
        print(f"  生成的请求头: {headers_params}")
        print(f"  生成的请求体: {body_params}")
        
        # 构造测试用例名称
        test_case_name = f"Case#{combo_idx} - {' + '.join(space_names)}"
        description = " | ".join(subspace_descriptions)
        
        test_case = {
            "api_id": api_id,
            "name": test_case_name,
            "description": description,
            "query_params": query_params,
            "path_params": path_params,
            "headers_params": headers_params,
            "body_params": body_params,
            "space_names": space_names,
            "subspace_types": subspace_types,
            "case_type": case_type,
            "created_at": datetime.now().isoformat(),
        }
        
        return test_case
    
    def _generate_parameters(
        self,
        api_info: Dict[str, Any],
        parameter_list: List[str],
        single_param_spaces_mapping: Dict[str, List[Dict[str, Any]]],
        single_spaces: Dict[str, List[Dict[str, Any]]],
        must_be_valid: bool = False,
    ) -> Tuple[Dict, Dict, Dict, Dict]:
        """
        生成参数值
        
        核心算法：
        1. 对于每个参数名 param_name in parameter_list
        2. 从 single_param_spaces_mapping[param_name] 获取分类列表
        3. 从分类列表中采样一个值
        4. 使用分类名称从 single_spaces[param_name] 中查找对应的 sample_value
        5. 根据参数位置（query/path/headers/body）放入对应字典
        
        Args:
            must_be_valid: 若为True，则只使用valid类型的参数(主要用于正向用例)
        
        Returns:
            (query_params, path_params, headers_params, body_params)
        """
        query_params = {}
        path_params = {}
        headers_params = {}
        body_params = {}
        
        # 从 API info 获取参数定义
        api_query_params = api_info.get("query_params", [])
        api_path_params = api_info.get("path_params", [])
        api_headers_params = api_info.get("headers_params", [])
        api_body_params = api_info.get("body_params", [])
        
        print(f"\n  📝 【参数生成过程】共 {len(parameter_list)} 个参数要处理，must_be_valid={must_be_valid}")
        
        # 遍历参数列表
        for param_name in parameter_list:
            # Step 1: 从映射中获取该参数在此子空间中的分类列表
            categories = single_param_spaces_mapping.get(param_name, [])
            
            print(f"    [{param_name}] 从映射中获取分类: {len(categories)} 个")
            
            # Step 2-3: 从分类中采样值
            sample_value = self._sample_from_categories(
                param_name=param_name,
                categories=categories,
                single_spaces=single_spaces,
                must_be_valid=must_be_valid,
            )
            
            print(f"      采样结果: sample_value={sample_value}")
            
            # 如果采样得到的值是 None，尝试从 single_spaces 中直接获取第一个值
            if sample_value is None and param_name in single_spaces:
                param_categories = single_spaces[param_name]
                if isinstance(param_categories, list) and param_categories:
                    # 如果 must_be_valid，只考虑 valid 分类
                    if must_be_valid:
                        valid_cats = [c for c in param_categories if c.get("type") == "valid"]
                        if valid_cats:
                            first_cat = valid_cats[0]
                        else:
                            first_cat = param_categories[0]
                    else:
                        first_cat = param_categories[0]
                    
                    if isinstance(first_cat, dict):
                        sample_value = first_cat.get("sample_value")
                        if sample_value is None:
                            sample_value = first_cat.get("description", "")
                        print(f"      从 single_spaces 中获取备选值: {sample_value}")
            
            # 只有当 sample_value 不是 None 时才添加到字典
            if sample_value is not None:
                # 调试：检查参数是否在各个列表中
                in_query = self._is_param_in_list(param_name, api_query_params)
                in_path = self._is_param_in_list(param_name, api_path_params)
                in_headers = self._is_param_in_list(param_name, api_headers_params)
                in_body = self._is_param_in_list(param_name, api_body_params)
                
                print(f"      位置检查: query={in_query}, path={in_path}, headers={in_headers}, body={in_body}")
                
                # 根据参数位置放入对应的字典
                if in_query:
                    query_params[param_name] = sample_value
                    print(f"      ✅ 添加到查询参数")
                elif in_path:
                    path_params[param_name] = sample_value
                    print(f"      ✅ 添加到路径参数")
                elif in_headers:
                    headers_params[param_name] = sample_value
                    print(f"      ✅ 添加到请求头")
                elif in_body:
                    body_params[param_name] = sample_value
                    print(f"      ✅ 添加到请求体")
                else:
                    print(f"      ⚠️  参数没有被路由到任何位置！")
            else:
                print(f"      ❌ sample_value 为 None，参数被跳过")
        
        return query_params, path_params, headers_params, body_params
    
    def _sample_from_categories(
        self,
        param_name: str,
        categories: List[Dict[str, Any]],
        single_spaces: Dict[str, List[Dict[str, Any]]],
        must_be_valid: bool = False,
    ) -> Any:
        """
        从分类列表中采样值
        
        核心逻辑：
        1. 如果 must_be_valid，先过滤出仅 valid 类型的分类
        2. 从分类中随机选一个 category_name
        3. 然后用 category_name 去 single_spaces 中查找对应的分类
        4. 从该分类的 sample_values 中随机选一个值
        
        Args:
            param_name: 参数名，如 "language"
            categories: 子空间中的分类列表（包含 category_name, type, description）
            single_spaces: 单参数空间（包含 category_name 对应的 sample_values 数组）
            must_be_valid: 若为True，只从valid分类中采样
        
        Returns:
            采样得到的值，或 None
        """
        import random
        
        print(f"        【_sample_from_categories】参数: {param_name}, 分类数: {len(categories)}, must_be_valid: {must_be_valid}")
        if categories:
            print(f"          第1个分类数据: {categories[0]}")
        
        if not categories:
            print(f"          ❌ 没有可用的分类")
            return None
        
        # 如果 must_be_valid，过滤出仅 valid 分类
        if must_be_valid:
            valid_categories = [c for c in categories if c.get("type") == "valid"]
            if valid_categories:
                categories = valid_categories
                print(f"          🔒 must_be_valid=True，已过滤为 valid 分类: {len(categories)} 个")
            else:
                print(f"          ⚠️  must_be_valid=True，但找不到 valid 分类，使用全部分类")
        
        # Step 1: 从 categories 中随机选择一个
        selected_category = random.choice(categories)
        selected_category_name = selected_category.get("category_name")
        print(f"          🎲 随机选择分类: {selected_category_name}")
        
        # Step 2: 在 single_spaces 中查找该参数
        if param_name not in single_spaces:
            print(f"          ❌ 参数 {param_name} 不在 single_spaces 中")
            return None
        
        param_categories = single_spaces[param_name]
        if not isinstance(param_categories, list):
            print(f"          ❌ single_spaces[{param_name}] 不是列表")
            return None
        
        print(f"          📍 在 single_spaces[{param_name}] 中查找分类: {selected_category_name}")
        
        # Step 3: 找到对应的分类定义，从其 sample_values 中随机选择
        for cat in param_categories:
            if not isinstance(cat, dict):
                continue
            
            if cat.get("category_name") == selected_category_name:
                print(f"             ✅ 找到匹配的分类信息: {cat}")
                sample_values = cat.get("sample_values", [])
                
                print(f"             sample_values 数量: {len(sample_values) if isinstance(sample_values, list) else 'N/A'}")
                
                if isinstance(sample_values, list) and sample_values:
                    # 从 sample_values 中随机选一个
                    sample_value = random.choice(sample_values)
                    print(f"          ✅ 从 sample_values 中随机选择: {repr(sample_value)[:80]}")
                    return sample_value
                else:
                    # 备选：使用 description
                    desc = cat.get("description", "")
                    if desc:
                        print(f"          ⚠️  sample_values 为空，使用 description: {repr(desc)[:80]}")
                        return desc
                    print(f"          ⚠️  sample_values 和 description 都为空，返回空字符串")
                    return ""
        
        # 找不到该分类定义
        print(f"          ❌ 在 single_spaces[{param_name}] 中找不到分类 {selected_category_name}")
        return None
    
    def _is_param_in_list(
        self,
        param_name: str,
        param_list: List[Dict[str, Any]],
    ) -> bool:
        """检查参数是否在参数列表中"""
        for param in param_list:
            if param.get("name") == param_name:
                return True
        return False
    
    def _normalize_single_spaces(
        self,
        single_spaces: Dict[str, List[Dict[str, Any]]] | List[Dict[str, Any]],
    ) -> Dict[str, List[Dict[str, Any]]]:
        """
        统一 single_spaces 格式
        
        输入可能是两种格式：
        1. 字典格式（本地测试），已经是目标格式
        2. 数组格式（数据库），需要转换
        """
        import random
        
        # 如果已经是字典格式，直接返回
        if isinstance(single_spaces, dict):
            print(f"  ℹ️  single_spaces 已是字典格式，直接使用")
            return single_spaces
        
        # 如果是列表格式，转换为字典
        if isinstance(single_spaces, list):
            print(f"  🔄 single_spaces 是数组格式，需要转换为字典")
            result = {}
            
            for param_info in single_spaces:
                if not isinstance(param_info, dict):
                    continue
                
                param_name = param_info.get("param_name")
                if not param_name:
                    continue
                
                print(f"    【参数 {param_name}】")
                categories = []
                
                # 处理 valid 分类
                for cat in param_info.get("valid", []):
                    if isinstance(cat, dict):
                        sample_values = cat.get("sample_values", [])
                        
                        # 保留 sample_values 数组，不在这里提前选择
                        categories.append({
                            "category_name": cat.get("category_name", ""),
                            "sample_values": sample_values if isinstance(sample_values, list) else [],
                            "type": "valid",
                            "description": cat.get("description", ""),
                        })
                        print(f"      ✅ valid - {cat.get('category_name')}: {len(sample_values)} 个 sample_values")
                
                # 处理 invalid 分类
                for cat in param_info.get("invalid", []):
                    if isinstance(cat, dict):
                        sample_values = cat.get("sample_values", [])
                        
                        # 保留 sample_values 数组，不在这里提前选择
                        categories.append({
                            "category_name": cat.get("category_name", ""),
                            "sample_values": sample_values if isinstance(sample_values, list) else [],
                            "type": "invalid",
                            "description": cat.get("description", ""),
                        })
                        print(f"      ❌ invalid - {cat.get('category_name')}: {len(sample_values)} 个 sample_values")
                
                if categories:
                    result[param_name] = categories
            
            print(f"  ✅ 转换完成，参数数: {len(result)}")
            return result
        
        # 都不是，返回空字典
        print(f"  ❌ single_spaces 格式未知，返回空字典")
        return {}


def generate_test_cases_from_mixed_space(
    api_id: int,
    api_info: Dict[str, Any],
    mixed_spaces_data: Dict[str, Any],
    single_spaces_data: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    从混合空间生成测试用例的便利函数
    
    Args:
        api_id: API ID
        api_info: API 信息
        mixed_spaces_data: 混合空间数据 {multi_parameter_spaces: [...]}
        single_spaces_data: 单参数空间数据 {spaces: {...}}
    
    Returns:
        测试用例列表
    """
    generator = TestCaseGenerator()
    
    # 提取数据
    mixed_spaces = mixed_spaces_data.get("multi_parameter_spaces", [])
    single_spaces = single_spaces_data.get("spaces", {})
    
    return generator.generate_test_cases(
        api_id=api_id,
        api_info=api_info,
        mixed_spaces=mixed_spaces,
        single_spaces=single_spaces,
    )
