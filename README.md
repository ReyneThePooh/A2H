# Android -> HarmonyOS 翻译与差分测试

本项目用于把 Android 项目翻译为 HarmonyOS/ArkTS 工程，并执行资源迁移、工程打包、鸿蒙构建修复和可选的差分回放。

## 环境要求

- Windows、Python 3.10+
- Android SDK Platform-Tools（`adb`）
- DevEco Studio，包含 Node.js、Hvigor、HarmonyOS SDK 和 `hdc`
- 可访问的 OpenAI 兼容模型服务
- Android 原项目和 HarmonyOS 模板工程

需要真实设备回放时，确认 `adb devices` 和 `hdc list targets` 都能识别设备。

## 安装

在仓库根目录执行：

```powershell
py -3.10 -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
```

如果 PowerShell 不允许激活脚本，可以直接使用：

```powershell
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## 配置

复制环境变量模板：

```powershell
Copy-Item .env.example .env
```

至少填写以下字段：

```dotenv
LLM_MODEL_ID=模型名称
LLM_API_KEY=模型服务密钥
LLM_BASE_URL=https://模型服务地址/v1
ANDROID_PROJECT_DIR=C:\path\to\android-project
HARMONY_TEMPLATE_DIR=E:\path\to\HarmonyTemplate\template
HARMONY_SOURCE_PROJECT_DIR=E:\path\to\HarmonyProject
HARMONY_WORK_BASE_DIR=E:\path\to\HarmonyWorkDir
NODE_HOME=C:\Program Files\Huawei\DevEco Studio\tools\node
```

不要把真实密钥提交到 Git。

差分测试配置：

```powershell
Copy-Item config.example.yaml config.yaml
```

按本机情况修改 `config.yaml` 中的 `adb_path`、`hdc_path`、设备序列号和 `harmony_ability`。

## 翻译、打包和构建

在仓库根目录运行：

```powershell
python main.py
```

主流程会依次执行依赖分析、资源迁移、ArkTS 翻译、HarmonyOS 工程打包和 `assembleHap` 构建修复。输出工程位于 `HARMONY_WORK_BASE_DIR` 指定的目录。

常用命令：

```powershell
# 限制构建修复轮数
python main.py --max-fix-rounds 4 --max-builds 12

# 从已有 HarmonyOS 工程继续修复
python main.py --resume E:\path\to\HarmonyProject

# 查看运行状态
python main.py --status E:\path\to\run-dir
```

构建成功后，`.hap` 通常位于输出工程的 `entry/build/default/outputs/default/`。配置签名后，可以用 DevEco Studio 打开输出工程并运行。

## 部署到鸿蒙设备

```powershell
hdc list targets
hdc install -r E:\path\to\app.hap
```

也可以用 DevEco Studio 打开输出工程，选择设备后点击运行。`bundleName` 和 `EntryAbility` 必须与工程配置一致。

## 差分测试

差分测试包括录制、鸿蒙回放和评估：

```powershell
# Android 端录制轨迹
python -m diff_tester record `
  --pkg com.example.android `
  --device <adb_serial> `
  --traces 20 `
  --out record_out `
  --config config.yaml

# HarmonyOS 端回放
python -m diff_tester replay `
  --bundle com.example.harmony `
  --device <hdc_serial> `
  --hap E:\path\to\app.hap `
  --traces record_out\traces `
  --page-pairs page_pairs.json `
  --out results `
  --config config.yaml

# 生成指标和归因报告
python -m diff_tester evaluate `
  --results results `
  --traces record_out\traces `
  --bundle com.example.harmony `
  --page-pairs page_pairs.json `
  --out report `
  --config config.yaml
```

构建完成后，也可以启用差分门禁：

```powershell
python main.py --enable-diff-gate --seeds-dir .diff_gate\seeds
```

## 测试

离线单元测试不需要设备：

```powershell
python -m pytest -q
```

## 目录说明

- `main.py`：翻译、打包和构建修复入口
- `pipeline/`：翻译、资源迁移、工程打包和修复流程
- `diff_tester/`：录制、回放、归一化、预言和评估
- `HarmonyTemplate/`：HarmonyOS 工程模板
- `tests/`：离线测试
- `.env.example`、`config.example.yaml`：配置模板

离线测试通过不代表某个具体应用已经通过真实设备差分回放；设备、模型服务和签名配置依赖本机环境。
