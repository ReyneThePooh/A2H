"""静态资源迁移 — Android res/ → HarmonyOS resources/（工程配置由 project_packager 套模板提供）"""

import os
import json
import shutil
import re
from pathlib import Path
from dataclasses import dataclass, field, asdict
from xml.etree import ElementTree as ET


# ============================================================
# 数据模型
# ============================================================

@dataclass
class ResourceEntry:
    android_path: str      # 原始 Android 路径，如 res/values/strings.xml
    android_ref: str       # Android 引用方式，如 R.string.app_name / @string/app_name
    harmony_path: str       # 迁移后的 HarmonyOS 路径
    harmony_key: str        # 在 HarmonyOS 文件中的 key
    value: str = ""         # 资源值
    resource_type: str = "" # string | color | dimen | drawable | mipmap | layout


@dataclass
class ResourceMapping:
    """记录 Android → HarmonyOS 资源迁移映射"""
    entries: list[ResourceEntry] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "entries": [asdict(e) for e in self.entries],
            # 快速查找索引
            "by_android_ref": {e.android_ref: asdict(e) for e in self.entries},
            "by_type": {},
        }

    def build_index(self):
        """构建按类型索引"""
        index = {}
        for e in self.entries:
            t = e.resource_type
            if t not in index:
                index[t] = []
            index[t].append(asdict(e))
        return index

    def summary(self) -> str:
        """可读摘要"""
        by_type = self.build_index()
        lines = [f"资源迁移完成，共 {len(self.entries)} 项："]
        for t, items in sorted(by_type.items()):
            lines.append(f"\n  [{t}] ({len(items)} 项)")
            for item in items[:5]:
                lines.append(f"    {item['android_ref']} → {item['harmony_key']} = '{item['value'][:40]}'")
            if len(items) > 5:
                lines.append(f"    ... 还有 {len(items) - 5} 项")
        return "\n".join(lines)


# ============================================================
# XML 解析工具
# ============================================================

def parse_strings_xml(path: str) -> list[dict]:
    """解析 strings.xml → [{name, value}]"""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        return [
            {"name": e.attrib.get("name", ""), "value": e.text or ""}
            for e in root.findall("string")
        ]
    except Exception:
        return []


def parse_colors_xml(path: str) -> list[dict]:
    """解析 colors.xml → [{name, value}]"""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        result = []
        for e in root:
            name = e.attrib.get("name", "")
            val = e.text or ""
            if not val:
                continue
            # 简化 #AARRGGBB → #RRGGBB（HarmonyOS 不需要 alpha 通道）
            if val.startswith("#") and len(val) == 9:
                val = "#" + val[3:]  # 去掉 alpha 通道的前两位
            result.append({"name": name, "value": val})
        return result
    except Exception:
        return []


def parse_dimens_xml(path: str) -> list[dict]:
    """解析 dimens.xml → [{name, value}]"""
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        result = []
        for e in root:
            name = e.attrib.get("name", "")
            val = e.text or ""
            if val:
                result.append({"name": name, "value": val})
        return result
    except Exception:
        return []


def parse_manifest(path: str) -> dict:
    """解析 AndroidManifest.xml，提取关键信息"""
    info = {
        "package": "",
        "activities": [],
        "permissions": [],
        "application_label": "",
        "application_icon": "",
    }
    try:
        tree = ET.parse(path)
        root = tree.getroot()
        info["package"] = root.attrib.get("package", "")

        for elem in root.iter():
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "activity":
                info["activities"].append({
                    "name": elem.attrib.get("{http://schemas.android.com/apk/res/android}name", ""),
                    "exported": elem.attrib.get("{http://schemas.android.com/apk/res/android}exported", ""),
                })
            elif tag == "uses-permission":
                info["permissions"].append(elem.attrib.get("{http://schemas.android.com/apk/res/android}name", ""))
            elif tag == "application":
                info["application_label"] = elem.attrib.get("{http://schemas.android.com/apk/res/android}label", "@string/app_name")
                info["application_icon"] = elem.attrib.get("{http://schemas.android.com/apk/res/android}icon", "@mipmap/ic_launcher")
    except Exception:
        pass
    return info


# ============================================================
# 资源迁移器
# ============================================================

class ResourceMigrator:
    """Android 资源 → HarmonyOS 资源迁移"""

    def __init__(self, android_project_path: str, harmony_output_path: str):
        self.android_root = Path(android_project_path)
        self.harmony_root = Path(harmony_output_path)
        self.mapping = ResourceMapping()

    def run(self) -> ResourceMapping:
        """执行完整迁移"""
        print("=" * 50)
        print("静态资源迁移")
        print("=" * 50)

        # 创建 HarmonyOS 工程目录
        self._create_scaffold()

        # 迁移各类资源
        self._migrate_strings()
        self._migrate_colors()
        self._migrate_dimens()
        self._migrate_drawables()
        self._migrate_mipmaps()

        # 保存映射
        self._save_mapping()

        print(self.mapping.summary())
        return self.mapping

    def _create_scaffold(self):
        """创建 HarmonyOS 工程骨架"""
        dirs = [
            "entry/src/main/ets/pages",
            "entry/src/main/ets/utils",
            "entry/src/main/resources/base/element",
            "entry/src/main/resources/base/media",
        ]
        for d in dirs:
            (self.harmony_root / d).mkdir(parents=True, exist_ok=True)
        print(f"  工程骨架已创建: {self.harmony_root}")

    # ---- 字符串迁移 ----

    def _migrate_strings(self):
        """strings.xml → element/string.json"""
        res_dir = self._find_res_dir()
        if not res_dir:
            return

        strings_path = res_dir / "values" / "strings.xml"
        if not strings_path.exists():
            print("  ⚠️ 未找到 strings.xml")
            return

        entries = parse_strings_xml(str(strings_path))
        if not entries:
            return

        # 生成 HarmonyOS string.json 格式
        string_data = {
            "string": [
                {"name": e["name"], "value": e["value"]}
                for e in entries
            ]
        }

        output_path = self.harmony_root / "entry/src/main/resources/base/element/string.json"
        self._write_json(output_path, string_data)

        # 记录映射
        for e in entries:
            self.mapping.entries.append(ResourceEntry(
                android_path=str(strings_path),
                android_ref=f"R.string.{e['name']}",
                harmony_path=str(output_path.relative_to(self.harmony_root)),
                harmony_key=e["name"],
                value=e["value"],
                resource_type="string",
            ))

        print(f"  strings.xml → string.json ({len(entries)} 项)")

    # ---- 颜色迁移 ----

    def _migrate_colors(self):
        """colors.xml → element/color.json"""
        res_dir = self._find_res_dir()
        if not res_dir:
            return

        colors_path = res_dir / "values" / "colors.xml"
        if not colors_path.exists():
            return

        entries = parse_colors_xml(str(colors_path))
        if not entries:
            return

        color_data = {
            "color": [
                {"name": e["name"], "value": e["value"]}
                for e in entries
            ]
        }

        output_path = self.harmony_root / "entry/src/main/resources/base/element/color.json"
        self._write_json(output_path, color_data)

        for e in entries:
            self.mapping.entries.append(ResourceEntry(
                android_path=str(colors_path),
                android_ref=f"R.color.{e['name']}",
                harmony_path=str(output_path.relative_to(self.harmony_root)),
                harmony_key=e["name"],
                value=e["value"],
                resource_type="color",
            ))

        print(f"  colors.xml → color.json ({len(entries)} 项)")

    # ---- 尺寸迁移 ----

    def _migrate_dimens(self):
        """dimens.xml → element/float.json"""
        res_dir = self._find_res_dir()
        if not res_dir:
            return

        dimens_path = res_dir / "values" / "dimens.xml"
        if not dimens_path.exists():
            return

        entries = parse_dimens_xml(str(dimens_path))
        if not entries:
            return

        float_data = {
            "float": [
                {"name": e["name"], "value": e["value"]}
                for e in entries
            ]
        }

        output_path = self.harmony_root / "entry/src/main/resources/base/element/float.json"
        self._write_json(output_path, float_data)

        for e in entries:
            self.mapping.entries.append(ResourceEntry(
                android_path=str(dimens_path),
                android_ref=f"R.dimen.{e['name']}",
                harmony_path=str(output_path.relative_to(self.harmony_root)),
                harmony_key=e["name"],
                value=e["value"],
                resource_type="dimen",
            ))

        print(f"  dimens.xml → float.json ({len(entries)} 项)")

    # ---- 图片资源 ----

    def _migrate_drawables(self):
        """res/drawable*/ 下的图片 → resources/base/media/"""
        res_dir = self._find_res_dir()
        if not res_dir:
            return

        media_dir = self.harmony_root / "entry/src/main/resources/base/media"
        supported = {'.png', '.jpg', '.jpeg', '.gif', '.svg', '.webp'}

        for drawable_dir in sorted(res_dir.glob("drawable*")):
            if not drawable_dir.is_dir():
                continue

            for img_file in sorted(drawable_dir.iterdir()):
                if img_file.suffix.lower() not in supported:
                    continue

                # 直接用文件名（去密度后缀），例如 ic_add.png
                dest = media_dir / img_file.name
                shutil.copy2(img_file, dest)

                name = img_file.stem  # 不带扩展名的文件名
                self.mapping.entries.append(ResourceEntry(
                    android_path=str(img_file),
                    android_ref=f"R.drawable.{name}",
                    harmony_path=str(dest.relative_to(self.harmony_root)),
                    harmony_key=name,
                    value=str(dest),
                    resource_type="drawable",
                ))

        drawable_count = sum(1 for e in self.mapping.entries if e.resource_type == "drawable")
        if drawable_count:
            print(f"  drawable → media/ ({drawable_count} 项)")

    def _migrate_mipmaps(self):
        """res/mipmap*/ 下的应用图标 → resources/base/media/"""
        res_dir = self._find_res_dir()
        if not res_dir:
            return

        media_dir = self.harmony_root / "entry/src/main/resources/base/media"

        for mipmap_dir in sorted(res_dir.glob("mipmap*")):
            if not mipmap_dir.is_dir():
                continue

            for img_file in sorted(mipmap_dir.iterdir()):
                if img_file.suffix.lower() not in {'.png', '.jpg', '.webp'}:
                    continue

                # 同名的只保留第一个（最高分辨率不要覆盖最新的）
                dest = media_dir / img_file.name
                if dest.exists():
                    # 选择更大的文件
                    if img_file.stat().st_size > dest.stat().st_size:
                        shutil.copy2(img_file, dest)
                else:
                    shutil.copy2(img_file, dest)

                name = img_file.stem
                # 避免重复添加同一个 ref
                ref = f"R.mipmap.{name}"
                if ref not in {e.android_ref for e in self.mapping.entries}:
                    self.mapping.entries.append(ResourceEntry(
                        android_path=str(img_file),
                        android_ref=ref,
                        harmony_path=str(dest.relative_to(self.harmony_root)),
                        harmony_key=name,
                        value=str(dest),
                        resource_type="mipmap",
                    ))

        mipmap_count = sum(1 for e in self.mapping.entries if e.resource_type == "mipmap")
        if mipmap_count:
            print(f"  mipmap → media/ ({mipmap_count} 项)")

    # ---- 辅助方法 ----

    def _find_res_dir(self) -> Path | None:
        """查找 Android res 目录"""
        candidates = [
            self.android_root / "app/src/main/res",
            self.android_root / "src/main/res",
        ]
        for c in candidates:
            if c.exists():
                return c
        return None

    def _save_mapping(self):
        """保存资源映射到 JSON"""
        output_path = self.harmony_root / ".resource_mapping.json"
        self._write_json(output_path, self.mapping.to_dict())
        print(f"  资源映射已保存: {output_path}")

    @staticmethod
    def _write_json(path: Path, data):
        with open(path, 'w', encoding='utf-8') as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ============================================================
# 测试入口
# ============================================================

if __name__ == "__main__":
    android_project = "/Users/kxh/Desktop/横向/sample-android-project"
    harmony_output = "/Users/kxh/Desktop/横向/sample-android-project/HarmonyProject"
    migrator = ResourceMigrator(android_project, harmony_output)
    mapping = migrator.run()
