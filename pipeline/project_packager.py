"""工程打包器 — 把翻译产物套进 DevEco 壳工程模板，生成可直接构建/运行的完整鸿蒙工程

流程：
1. 复制模板工程（跳过 build/.hvigor 等缓存目录）
2. 合并生成侧 ets：pages/ 整目录替换，其余目录递归合并（保留模板的 EntryAbility 入口）
3. 合并生成侧 resources：element/*.json 做键级合并（模板的 module_desc 等 key 不丢失），
   其余文件同名覆盖
4. 注册 pages/ 下含 @Entry 的页面到 main_pages.json，并同步 EntryAbility 的 loadContent 入口页
5. 把 AndroidManifest 的 uses-permission 映射为 ohos 权限注入 module.json5
6. 提供 hvigorw assembleHap 构建能力，供主流程的构建修复闭环调用
"""

import json
import os
import re
import shutil
import subprocess
from pathlib import Path

# 模板中不需要复制的缓存/产物目录
TEMPLATE_IGNORES = {"build", ".hvigor", ".idea", "oh_modules", ".preview"}


class ResourceValidationError(ValueError):
    """打包后的 HarmonyOS 资源未通过确定性校验。"""


class ResourceConflictError(ResourceValidationError):
    """打包后的 HarmonyOS 资源存在逻辑名称冲突。"""

    def __init__(self, conflicts: list[dict[str, object]]):
        self.conflicts = conflicts
        details = []
        for conflict in conflicts:
            files = ", ".join(str(path) for path in conflict["files"])
            details.append(
                f"{conflict['directory']}/{conflict['name']}: {files}"
            )
        super().__init__("资源逻辑名称冲突：" + "; ".join(details))


class InvalidResourceNameError(ResourceValidationError):
    """资源文件名含 HarmonyOS 不支持的字符。"""

    def __init__(self, invalid_names: list[dict[str, object]]):
        self.invalid_names = invalid_names
        details = ", ".join(str(item["file"]) for item in invalid_names)
        super().__init__(f"资源文件名只能包含字母、数字和下划线：{details}")

# Android 权限 → HarmonyOS 权限（常见项；无对应或需特殊申请的跳过并告警）
PERMISSION_MAP = {
    "android.permission.INTERNET": "ohos.permission.INTERNET",
    "android.permission.ACCESS_NETWORK_STATE": "ohos.permission.GET_NETWORK_INFO",
    "android.permission.ACCESS_WIFI_STATE": "ohos.permission.GET_WIFI_INFO",
    "android.permission.VIBRATE": "ohos.permission.VIBRATE",
    "android.permission.ACCESS_FINE_LOCATION": "ohos.permission.LOCATION",
    "android.permission.ACCESS_COARSE_LOCATION": "ohos.permission.APPROXIMATELY_LOCATION",
    "android.permission.CAMERA": "ohos.permission.CAMERA",
    "android.permission.RECORD_AUDIO": "ohos.permission.MICROPHONE",
    "android.permission.WAKE_LOCK": "ohos.permission.RUNNING_LOCK",
    "android.permission.RECEIVE_BOOT_COMPLETED": "ohos.permission.RECEIVE_STARTUP_COMPLETED",
}

# 用户授权权限除 name 外，还必须声明用途和使用场景。这里的 resource 名会写入
# string.json，避免在 module.json5 中硬编码文案。
USER_GRANT_PERMISSION_DETAILS = {
    "ohos.permission.LOCATION": (
        "location_permission_reason",
        "用于提供基于位置的服务",
    ),
    "ohos.permission.APPROXIMATELY_LOCATION": (
        "approximately_location_permission_reason",
        "用于提供基于大致位置的服务",
    ),
    "ohos.permission.CAMERA": (
        "camera_permission_reason",
        "用于拍摄照片或视频",
    ),
    "ohos.permission.MICROPHONE": (
        "microphone_permission_reason",
        "用于录制或处理语音内容",
    ),
}


# ============================================================
# 目录合并
# ============================================================

def _merge_tree(src: Path, dst: Path, ignore_names: set[str] | None = None):
    """递归合并目录：同名文件覆盖，同名目录继续合并；ignore_names 在任意层级跳过"""
    dst.mkdir(parents=True, exist_ok=True)
    for item in src.iterdir():
        if ignore_names and item.name in ignore_names:
            continue
        target = dst / item.name
        if item.is_dir():
            if target.exists() and not target.is_dir():
                target.unlink()
            _merge_tree(item, target, ignore_names)
        else:
            if target.exists() and target.is_dir():
                shutil.rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def _copy_template(template_dir: Path, output_dir: Path):
    """复制模板工程到输出目录（合并式，允许输出目录已存在；任意层级跳过缓存目录）"""
    _merge_tree(template_dir, output_dir, ignore_names=TEMPLATE_IGNORES)


def _merge_generated_ets(src_ets: Path, target_ets: Path):
    """合并生成侧 ets：pages/ 整目录替换，其余递归合并（保留模板 Ability 入口）"""
    target_ets.mkdir(parents=True, exist_ok=True)

    src_pages = src_ets / "pages"
    target_pages = target_ets / "pages"
    if src_pages.exists():
        if target_pages.exists():
            shutil.rmtree(target_pages)
        shutil.copytree(src_pages, target_pages)
        print(f"  pages/ 已替换为翻译产物 ({len(list(src_pages.glob('*.ets')))} 个页面)")

    for item in src_ets.iterdir():
        if item.name == "pages":
            continue
        target = target_ets / item.name
        if item.is_dir():
            _merge_tree(item, target)
        else:
            shutil.copy2(item, target)


# ============================================================
# element JSON 键级合并
# ============================================================

def _merge_element_json(src_file: Path, dst_file: Path):
    """合并 element 资源 JSON（如 string.json）：按 name 去重取并集，生成侧覆盖同名 key。

    模板中的 module_desc / EntryAbility_label 等被 module.json5 引用的 key 必须保留，
    因此不能整文件覆盖。
    """
    src_data = json.loads(src_file.read_text(encoding="utf-8"))
    if not dst_file.exists():
        dst_file.parent.mkdir(parents=True, exist_ok=True)
        dst_file.write_text(json.dumps(src_data, ensure_ascii=False, indent=2), encoding="utf-8")
        return

    dst_data = json.loads(dst_file.read_text(encoding="utf-8"))

    for res_type, src_items in src_data.items():
        if not isinstance(src_items, list):
            dst_data[res_type] = src_items
            continue
        dst_items = dst_data.get(res_type, [])
        by_name = {item.get("name"): item for item in dst_items}
        for item in src_items:
            by_name[item.get("name")] = item
        dst_data[res_type] = list(by_name.values())

    dst_file.write_text(json.dumps(dst_data, ensure_ascii=False, indent=2), encoding="utf-8")


def _merge_generated_resources(src_res: Path, target_res: Path):
    """合并生成侧 resources：element 下的 JSON 键级合并，其余文件同名覆盖"""
    for src_file in src_res.rglob("*"):
        if not src_file.is_file():
            continue
        rel = src_file.relative_to(src_res)
        target = target_res / rel

        if src_file.suffix == ".json" and rel.parent.name == "element":
            _merge_element_json(src_file, target)
        elif rel.as_posix() == "base/profile/main_pages.json":
            continue  # 由 _register_pages 统一生成
        else:
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src_file, target)
    print("  resources/ 已合并（element JSON 键级合并）")


def find_resource_name_conflicts(resources_root: str | Path) -> list[dict[str, object]]:
    """查找同一资源目录中同名但扩展名不同的文件。"""
    root = Path(resources_root)
    if not root.exists():
        return []

    grouped: dict[tuple[str, str], list[Path]] = {}
    for resource in root.rglob("*"):
        if not resource.is_file():
            continue
        relative = resource.relative_to(root)
        key = (relative.parent.as_posix().casefold(), resource.stem.casefold())
        grouped.setdefault(key, []).append(relative)

    conflicts = []
    for (directory, name), files in sorted(grouped.items()):
        if len(files) > 1:
            conflicts.append({
                "directory": directory,
                "name": name,
                "files": sorted(files, key=lambda path: path.as_posix().casefold()),
            })
    return conflicts


def find_invalid_resource_names(resources_root: str | Path) -> list[dict[str, object]]:
    """查找不符合 HarmonyOS ``[a-zA-Z0-9_]`` 规则的逻辑资源名。"""
    root = Path(resources_root)
    if not root.exists():
        return []
    invalid = []
    for resource in root.rglob("*"):
        if resource.is_file() and not re.fullmatch(r"[a-zA-Z0-9_]+", resource.stem):
            invalid.append({
                "name": resource.stem,
                "file": resource.relative_to(root),
            })
    return sorted(invalid, key=lambda item: str(item["file"]).casefold())


def validate_resource_names(resources_root: str | Path) -> None:
    conflicts = find_resource_name_conflicts(resources_root)
    if conflicts:
        raise ResourceConflictError(conflicts)
    invalid_names = find_invalid_resource_names(resources_root)
    if invalid_names:
        raise InvalidResourceNameError(invalid_names)


def _normalize_app_label(target_res: Path):
    """让入口 Ability 的 label 使用实际 app_name，避免模板默认显示为 "label"。"""
    string_json = target_res / "base" / "element" / "string.json"
    if not string_json.exists():
        return

    data = json.loads(string_json.read_text(encoding="utf-8"))
    strings = data.get("string", [])
    if not isinstance(strings, list):
        return

    by_name = {
        item.get("name"): item
        for item in strings
        if isinstance(item, dict)
    }
    app_name = by_name.get("app_name", {}).get("value")
    entry_label = by_name.get("EntryAbility_label")
    if not app_name or not isinstance(entry_label, dict):
        return

    if entry_label.get("value") != app_name:
        entry_label["value"] = app_name
        string_json.write_text(
            json.dumps(data, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        print(f"  EntryAbility_label 已同步为 app_name: {app_name}")


# ============================================================
# 页面注册与入口同步
# ============================================================

def _choose_entry_page(pages: list[str], generated_main_pages: Path) -> str:
    """选择入口页：生成侧 main_pages.json 首项 > MainPage > Index > 字典序第一个"""
    if generated_main_pages.exists():
        try:
            configured = json.loads(generated_main_pages.read_text(encoding="utf-8")).get("src", [])
            if configured:
                first = str(configured[0]).replace("\\", "/").removeprefix("./")
                first = first[:-4] if first.endswith(".ets") else first
                if first in pages:
                    return first
        except Exception:
            pass
    for candidate in ("pages/MainPage", "pages/Index"):
        if candidate in pages:
            return candidate
    return pages[0]


def _register_pages(target_main: Path, generated_main_pages: Path) -> str:
    """把 pages/ 下真正的页面（含 @Entry 装饰器）注册进 main_pages.json，返回入口页。

    模型/组件等无 @Entry 的 .ets 若被注册，编译器会报 10905402。
    """
    pages_dir = target_main / "ets" / "pages"
    pages = sorted(
        f"pages/{p.stem}"
        for p in pages_dir.glob("*.ets")
        if "@Entry" in p.read_text(encoding="utf-8", errors="replace")
    )
    if not pages:
        raise FileNotFoundError(f"未找到任何含 @Entry 的页面: {pages_dir}")

    entry_page = _choose_entry_page(pages, generated_main_pages)
    ordered = [entry_page] + [p for p in pages if p != entry_page]

    profile_dir = target_main / "resources" / "base" / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    (profile_dir / "main_pages.json").write_text(
        json.dumps({"src": ordered}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"  main_pages.json 已注册 {len(ordered)} 个页面，入口: {entry_page}")

    # 同步 EntryAbility 的 loadContent
    entry_ability = target_main / "ets" / "entryability" / "EntryAbility.ets"
    if entry_ability.exists():
        content = entry_ability.read_text(encoding="utf-8")
        updated, count = re.subn(
            r"windowStage\.loadContent\(\s*(['\"])([^'\"]+)\1",
            f"windowStage.loadContent('{entry_page}'",
            content,
            count=1,
        )
        if count:
            entry_ability.write_text(updated, encoding="utf-8")
            print(f"  EntryAbility.loadContent → {entry_page}")
        else:
            print("  ⚠️ EntryAbility.ets 中未找到 loadContent，入口页未同步")
    else:
        print("  ⚠️ 模板缺少 EntryAbility.ets")

    return entry_page


# ============================================================
# 权限注入
# ============================================================

def _add_permission_reason_resources(
    string_json: Path, permission_details: list[tuple[str, str]],
):
    """为用户授权权限补充 reason 所引用的字符串资源，保留已有的同名资源。"""
    if not permission_details:
        return

    data = json.loads(string_json.read_text(encoding="utf-8")) if string_json.exists() else {}
    strings = data.setdefault("string", [])
    existing_names = {
        item.get("name") for item in strings if isinstance(item, dict)
    }
    for resource_name, reason in permission_details:
        if resource_name not in existing_names:
            strings.append({"name": resource_name, "value": reason})

    string_json.parent.mkdir(parents=True, exist_ok=True)
    string_json.write_text(
        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def _inject_permissions(
    module_json5: Path,
    android_permissions: list[str],
    string_json: Path,
):
    """把 Android 权限映射为 ohos 权限，注入模板 module.json5。

    module.json5 是 JSON5（含尾逗号），无法用 json 解析，采用文本插入。
    """
    if not android_permissions or not module_json5.exists():
        return

    ohos_perms = []
    for p in android_permissions:
        mapped = PERMISSION_MAP.get(p)
        if mapped:
            ohos_perms.append(mapped)
        else:
            print(f"  ⚠️ 权限无直接映射，已跳过（如需请手动处理）: {p}")

    if not ohos_perms:
        return

    content = module_json5.read_text(encoding="utf-8")
    if '"requestPermissions"' in content:
        print("  module.json5 已含 requestPermissions，跳过注入")
        return

    permission_details = []
    permission_blocks = []
    for permission in sorted(set(ohos_perms)):
        details = USER_GRANT_PERMISSION_DETAILS.get(permission)
        if details:
            resource_name, reason = details
            permission_details.append(details)
            permission_blocks.append(
                f'      {{\n'
                f'        "name": "{permission}",\n'
                f'        "reason": "$string:{resource_name}",\n'
                f'        "usedScene": {{\n'
                f'          "abilities": ["EntryAbility"],\n'
                f'          "when": "inuse"\n'
                f'        }}\n'
                f'      }}'
            )
        else:
            permission_blocks.append(f'      {{\n        "name": "{permission}"\n      }}')

    perms_json = ",\n".join(permission_blocks)
    block = f'    "requestPermissions": [\n{perms_json}\n    ],\n'

    # 插在 "mainElement" 行之后（模板固定含该行）
    updated, count = re.subn(
        r'(^\s*"mainElement"\s*:\s*"[^"]*",\s*\n)',
        r"\1" + block,
        content,
        count=1,
        flags=re.M,
    )
    if count:
        module_json5.write_text(updated, encoding="utf-8")
        _add_permission_reason_resources(string_json, permission_details)
        print(f"  已注入 {len(set(ohos_perms))} 个权限: {sorted(set(ohos_perms))}")
    else:
        print("  ⚠️ module.json5 中未找到 mainElement 行，权限未注入")


# ============================================================
# import 路径重写
# ============================================================

# 匹配 import ... from '路径' 与 import x = require('路径') 中的相对路径
_IMPORT_PATH_RE = re.compile(r"""(from\s+|import\s*\(\s*|require\s*\(\s*)(['"])([^'"]+)\2""")


def rewrite_flat_imports(pages_dir: str | Path) -> int:
    """把 pages/ 下指向不存在路径的相对 import 重写为同目录兄弟文件。

    翻译各 unit 时 LLM 可能虚构 data/models、utils 等目录结构，而产物实际
    平铺在 pages/ 下。按“路径末段文件名 → 同目录存在同名 .ets”规则确定性改写。
    返回被修改的文件数。
    """
    pages_dir = Path(pages_dir)
    siblings = {p.stem for p in pages_dir.glob("*.ets")}
    changed_files = 0

    for ets in sorted(pages_dir.glob("*.ets")):
        content = ets.read_text(encoding="utf-8")

        def _fix(m: re.Match) -> str:
            prefix, quote, spec = m.groups()
            if not spec.startswith("."):
                return m.group(0)  # 系统/三方模块不动
            target = (ets.parent / spec).resolve()
            if target.with_suffix(".ets").exists() or target.exists():
                return m.group(0)  # 路径本来就有效
            stem = spec.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
            stem = stem[:-4] if stem.endswith(".ets") else stem
            if stem in siblings and stem != ets.stem:
                return f"{prefix}{quote}./{stem}{quote}"
            return m.group(0)

        updated = _IMPORT_PATH_RE.sub(_fix, content)
        if updated != content:
            ets.write_text(updated, encoding="utf-8")
            changed_files += 1

    if changed_files:
        print(f"  import 重写: {changed_files} 个文件的失效相对路径已指向同目录文件")
    return changed_files


# ============================================================
# 打包主流程
# ============================================================

def package_project(
    template_dir: str | Path,
    generated_dir: str | Path,
    output_dir: str | Path,
    android_permissions: list[str] | None = None,
) -> Path:
    """把翻译产物套进模板，输出完整可构建的 DevEco 工程，返回工程路径"""
    template_dir = Path(template_dir)
    generated_dir = Path(generated_dir)
    output_dir = Path(output_dir)

    if not template_dir.exists():
        raise FileNotFoundError(f"模板工程不存在: {template_dir}")
    src_main = generated_dir / "entry" / "src" / "main"
    src_ets = src_main / "ets"
    if not src_ets.exists():
        raise FileNotFoundError(f"生成侧无 ets 目录: {src_ets}")

    print("=" * 50)
    print("工程打包（套 DevEco 模板）")
    print("=" * 50)
    print(f"  模板: {template_dir}")
    print(f"  产物: {generated_dir}")
    print(f"  输出: {output_dir}")

    _copy_template(template_dir, output_dir)
    print("  模板工程已复制")

    target_main = output_dir / "entry" / "src" / "main"
    _merge_generated_ets(src_ets, target_main / "ets")

    src_res = src_main / "resources"
    if src_res.exists():
        _merge_generated_resources(src_res, target_main / "resources")
    _normalize_app_label(target_main / "resources")

    _register_pages(target_main, src_res / "base" / "profile" / "main_pages.json")
    rewrite_flat_imports(target_main / "ets" / "pages")
    _inject_permissions(
        target_main / "module.json5",
        android_permissions or [],
        target_main / "resources" / "base" / "element" / "string.json",
    )
    validate_resource_names(target_main / "resources")

    print(f"  打包完成: {output_dir}")
    return output_dir


# ============================================================
# hvigor 构建自检
# ============================================================

def run_hvigor(project_dir: str | Path, task: str = "assembleHap") -> subprocess.CompletedProcess:
    """在工程目录运行 hvigorw 构建，返回完整的 CompletedProcess（含 stdout/stderr）。

    需要 NODE_HOME 指向 DevEco 自带 node。找不到 hvigorw.bat 时抛 FileNotFoundError。
    """
    project_dir = Path(project_dir)
    hvigorw = project_dir / "hvigorw.bat"
    if not hvigorw.exists():
        raise FileNotFoundError(f"未找到 hvigorw.bat: {hvigorw}")

    env = os.environ.copy()
    node_home = env.get("NODE_HOME", "")
    if node_home:
        env["PATH"] = node_home + os.pathsep + env.get("PATH", "")

    print(f"\n构建: hvigorw {task} @ {project_dir}")
    return subprocess.run(
        f'"{hvigorw}" {task}',
        cwd=project_dir,
        capture_output=True,
        text=True,
        shell=True,
        encoding="utf-8",
        errors="replace",
        env=env,
    )


def run_hvigor_build(project_dir: str | Path, task: str = "assembleHap") -> bool:
    """构建自检：运行 hvigorw 并打印日志尾部，返回是否成功"""
    try:
        result = run_hvigor(project_dir, task)
    except FileNotFoundError as e:
        print(f"  ⚠️ {e}")
        return False

    tail = "\n".join((result.stdout or "").splitlines()[-15:])
    print(tail)
    if result.returncode != 0:
        print("=== stderr ===")
        print("\n".join((result.stderr or "").splitlines()[-30:]))
        print("构建失败")
        return False
    print("构建成功")
    return True
