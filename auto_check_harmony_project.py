import shutil
import subprocess
import time
from pathlib import Path

# =========================================================
# 需要你根据实际情况修改的 3 个路径
# =========================================================

# 1) DevEco Studio 创建的“壳工程”根目录（完整鸿蒙工程）
TEMPLATE_DIR = Path(r"C:\Users\dpq\Desktop\test")

# 2) 你用大模型/脚本生成的工程根目录
SOURCE_PROJECT_DIR = Path(
    r"D:\projects\HelloAgents-main\HarmonyProject"
)

# 3) 临时检查工程目录（脚本自动创建 & 删除）
WORK_DIR = Path(r"D:\projects\agent-development\HarmonyCheckWorkDir_" + time.strftime("%Y%m%d_%H%M%S"))

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
    shutil.copytree(TEMPLATE_DIR, WORK_DIR)

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

    # 替换 ets
    if target_ets.exists():
        print(f"[INFO] Removing template ets dir: {target_ets}")
        safe_rmtree(target_ets)

    print(f"[INFO] Copying ets from source to work dir ...")
    shutil.copytree(src_ets, target_ets)

    # 如果你生成侧有 resources，也一并替换
    if src_res.exists():
        if target_res.exists():
            print(f"[INFO] Removing template resources dir: {target_res}")
            safe_rmtree(target_res)
        print(f"[INFO] Copying resources from source to work dir ...")
        shutil.copytree(src_res, target_res)
    else:
        print("[INFO] Source has no resources dir, keep template resources (if any).")

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
