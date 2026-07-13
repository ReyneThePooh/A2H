import importlib.util
import json
import re
import shutil
import subprocess
import time
from pathlib import Path


def load_path_config():
    config_path = Path(__file__).resolve().parent / "hello_agents" / "core" / "path_config.py"
    spec = importlib.util.spec_from_file_location("harmony_path_config", config_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Cannot load path config: {config_path}")

    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


path_config = load_path_config()
HARMONY_SOURCE_PROJECT_DIR = path_config.HARMONY_SOURCE_PROJECT_DIR
HARMONY_TEMPLATE_DIR = path_config.HARMONY_TEMPLATE_DIR
HARMONY_WORK_BASE_DIR = path_config.HARMONY_WORK_BASE_DIR

# =========================================================
# 需要你根据实际情况修改的 3 个路径
# =========================================================

# 1) DevEco Studio 创建的“壳工程”根目录（完整鸿蒙工程）
TEMPLATE_DIR = HARMONY_TEMPLATE_DIR

# 2) 你用大模型/脚本生成的工程根目录
SOURCE_PROJECT_DIR = HARMONY_SOURCE_PROJECT_DIR

# 3) 临时检查工程目录（脚本自动创建 & 删除）
WORK_DIR = HARMONY_WORK_BASE_DIR.parent / f"{HARMONY_WORK_BASE_DIR.name}_{time.strftime('%Y%m%d_%H%M%S')}"

# hvigor 任务名（如果模板在命令行里用的是 assembleEntry，就改成 assembleEntry）
DEFAULT_HVIGOR_TASK = "assembleHap"


# =========================================================
# 工具函数
# =========================================================

def safe_rmtree(path: Path, retries: int = 5, delay: float = 0.5) -> bool:
    """在 Windows 下安全删除目录，对 WinError 32 做重试；失败返回 False。"""
    path = Path(path)
    if not path.exists():
        return True

    for i in range(retries):
        try:
            shutil.rmtree(path)
            return True
        except PermissionError as e:
            print(f"[WARN] PermissionError when deleting {path}, retry {i + 1}/{retries}: {e}")
            time.sleep(delay)
        except Exception as e:
            print(f"[WARN] Failed to delete {path}, retry {i + 1}/{retries}: {e}")
            time.sleep(delay)

    try:
        shutil.rmtree(path)
        return True
    except Exception as e:
        print(f"[ERROR] Final attempt failed to delete {path}: {e}")
        return False


def merge_tree(src: Path, dst: Path):
    """递归合并目录：同名文件覆盖，同名目录保留并继续合并。"""
    dst.mkdir(parents=True, exist_ok=True)

    for item in src.iterdir():
        target = dst / item.name
        if item.is_dir():
            if target.exists() and not target.is_dir():
                target.unlink()
            merge_tree(item, target)
        else:
            if target.exists() and target.is_dir():
                safe_rmtree(target)
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, target)


def copy_generated_ets(src_ets: Path, target_ets: Path):
    """
    保留模板里的 entryability/entrybackupability 等入口目录。
    生成侧的 pages 目录整目录替换；其它 ets 子目录/文件做递归合并。
    """
    target_ets.mkdir(parents=True, exist_ok=True)

    src_pages = src_ets / "pages"
    target_pages = target_ets / "pages"
    if src_pages.exists():
        if target_pages.exists():
            print(f"[INFO] Replacing pages dir: {target_pages}")
            safe_rmtree(target_pages)
        print("[INFO] Copying generated pages to work dir ...")
        shutil.copytree(src_pages, target_pages)
    else:
        print("[WARN] Source has no ets/pages dir, keep template pages (if any).")

    for item in src_ets.iterdir():
        if item.name == "pages":
            continue

        target = target_ets / item.name
        if item.is_dir():
            print(f"[INFO] Merging generated ets dir: {item.name}")
            merge_tree(item, target)
        else:
            print(f"[INFO] Copying generated ets file: {item.name}")
            shutil.copy2(item, target)


def normalize_page_path(page: str) -> str:
    page = str(page).strip().replace("\\", "/")
    page = page.removeprefix("./")
    if page.endswith(".ets"):
        page = page[:-4]
    return page


def page_exists(ets_dir: Path, page: str) -> bool:
    page = normalize_page_path(page)
    return (ets_dir / f"{page}.ets").exists()


def read_first_configured_page(profile_path: Path) -> str | None:
    if not profile_path.exists():
        return None

    try:
        with profile_path.open("r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        print(f"[WARN] Failed to read {profile_path}: {e}")
        return None

    pages = data.get("src", [])
    if not isinstance(pages, list) or not pages:
        return None

    first_page = pages[0]
    if not isinstance(first_page, str) or not first_page.strip():
        return None

    return normalize_page_path(first_page)


def choose_entry_page(ets_dir: Path, src_profile_path: Path) -> str:
    configured_page = read_first_configured_page(src_profile_path)
    if configured_page and page_exists(ets_dir, configured_page):
        return configured_page

    if configured_page:
        print(f"[WARN] Configured page '{configured_page}' not found, choosing an existing page.")

    pages_dir = ets_dir / "pages"
    if (pages_dir / "Index.ets").exists():
        return "pages/Index"

    page_files = list(pages_dir.glob("*.ets")) if pages_dir.exists() else []
    if not page_files:
        print("[WARN] No page file found under ets/pages; fallback to pages/Index.")
        return "pages/Index"

    newest_page = max(page_files, key=lambda p: (p.stat().st_mtime, p.name.lower()))
    return f"pages/{newest_page.stem}"


def sync_entry_page(target_main: Path, entry_page: str):
    profile_dir = target_main / "resources" / "base" / "profile"
    profile_dir.mkdir(parents=True, exist_ok=True)
    main_pages_json = profile_dir / "main_pages.json"
    with main_pages_json.open("w", encoding="utf-8") as f:
        json.dump({"src": [entry_page]}, f, ensure_ascii=False, indent=2)
        f.write("\n")
    print(f"[INFO] main_pages.json entry page: {entry_page}")

    entry_ability = target_main / "ets" / "entryability" / "EntryAbility.ets"
    if not entry_ability.exists():
        print(f"[WARN] EntryAbility.ets not found: {entry_ability}")
        return

    content = entry_ability.read_text(encoding="utf-8")
    updated, count = re.subn(
        r"windowStage\.loadContent\(\s*(['\"])([^'\"]+)\1",
        f"windowStage.loadContent('{entry_page}'",
        content,
        count=1,
    )
    if count == 0:
        print("[WARN] loadContent(...) not found in EntryAbility.ets; entry page not updated.")
        return

    entry_ability.write_text(updated, encoding="utf-8")
    print(f"[INFO] EntryAbility loadContent page: {entry_page}")


# =========================================================
# 准备工作目录：复制模板工程，仅替换 entry/src/main/ets(+resources)
# =========================================================

def prepare_work_dir():
    global WORK_DIR
    print(f"[INFO] Using template:       {TEMPLATE_DIR}")
    print(f"[INFO] Using source project:  {SOURCE_PROJECT_DIR}")
    print(f"[INFO] Work dir (base):       {WORK_DIR}")

    if not TEMPLATE_DIR.exists():
        raise FileNotFoundError(f"Template dir not found: {TEMPLATE_DIR}")
    if not SOURCE_PROJECT_DIR.exists():
        raise FileNotFoundError(
            f"Source project dir not found: {SOURCE_PROJECT_DIR}"
        )

    # 1) 删除旧的工作目录；若失败则切换到新的唯一目录
    if WORK_DIR.exists():
        WORK_DIR = WORK_DIR.parent / f"{WORK_DIR.name}_{time.strftime('%Y%m%d_%H%M%S')}"

    # 2) 整个复制模板工程到工作目录
    print("[INFO] Copying template project to work dir ...")
    shutil.copytree(
        TEMPLATE_DIR,
        WORK_DIR,
        ignore=shutil.ignore_patterns("build", ".hvigor"),
    )

    # 3) 只替换 entry/src/main/ets 和（可选）resources
    # -------------------------------------------------
    #   模板:  WORK_DIR/entry/src/main/module.json5   ✅ 保留
    #          WORK_DIR/entry/build-profile.json5     ✅ 保留
    #          WORK_DIR/entry/hvigorfile.ts           ✅ 保留
    #
    #   生成:  SOURCE_PROJECT_DIR/entry/src/main/ets/...
    #                  (可选) /resources/...
    #
    #   目标:  仅替换 ets 和 resources，不动 module.json5 等配置。
    # -------------------------------------------------

    # 生成侧路径
    src_main = SOURCE_PROJECT_DIR / "entry" / "src" / "main"
    src_ets = src_main / "ets"
    src_res = src_main / "resources"

    if not src_ets.exists():
        raise FileNotFoundError(
            f"Source ets dir not found: {src_ets}"
        )

    # 模板侧路径（在工作目录中）
    target_main = WORK_DIR / "entry" / "src" / "main"
    target_ets = target_main / "ets"
    target_res = target_main / "resources"

    # 合并 ets：保留模板 Ability 入口，只替换/合并生成代码
    print("[INFO] Merging ets from source to work dir ...")
    copy_generated_ets(src_ets, target_ets)

    # 如果生成侧有 resources，则合并覆盖同名文件，保留模板缺省资源
    if src_res.exists():
        print(f"[INFO] Merging resources from source to work dir ...")
        merge_tree(src_res, target_res)
    else:
        print("[INFO] Source has no resources dir, keep template resources (if any).")

    entry_page = choose_entry_page(target_ets, src_res / "base" / "profile" / "main_pages.json")
    sync_entry_page(target_main, entry_page)

    # 检查 module.json5 是否仍然存在
    module_json = target_main / "module.json5"
    if not module_json.exists():
        print("[WARN] module.json5 is missing in work dir. "
              "Make sure your TEMPLATE_DIR's entry/src/main/module.json5 存在。")
    else:
        print(f"[INFO] module.json5 kept from template: {module_json}")

    print(f"[INFO] Prepared work dir at: {WORK_DIR}")


# =========================================================
# 调用 hvigor 构建
# =========================================================

def run_hvigor(task: str = DEFAULT_HVIGOR_TASK):
    """
    在 WORK_DIR 中运行 hvigorw.bat 进行构建。
    """
    print(f"[INFO] Running hvigor task '{task}' in {WORK_DIR}")

    cmd = f"hvigorw.bat {task}"

    result = subprocess.run(
        cmd,
        cwd=WORK_DIR,
        capture_output=True,
        text=True,
        shell=True,
        encoding='utf-8',
    )

    print("=== hvigor stdout ===")
    print(result.stdout)
    print("=== hvigor stderr ===")
    print(result.stderr)

    if result.returncode != 0:
        print(f"[RESULT] BUILD FAILED (task: {task})")
    else:
        print(f"[RESULT] BUILD SUCCESS (task: {task})")

    return result


# =========================================================
# 主入口
# =========================================================

if __name__ == "__main__":
    prepare_work_dir()
    run_hvigor()
