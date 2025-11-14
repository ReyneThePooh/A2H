import os


def get_project_structure(project_path, max_depth=None, ignore_dirs=None):
    """
    获取项目的目录结构字符串

    Args:
        project_path: 项目根路径
        max_depth: 最大扫描深度，None表示不限制
        ignore_dirs: 要忽略的目录列表

    Returns:
        str: 目录树的字符串表示
    """
    if ignore_dirs is None:
        ignore_dirs = {
            'build', '.gradle', '.idea', '.git',
            'gradle', '.externalNativeBuild',
            'captures', 'local.properties'
        }

    project_path = os.path.abspath(project_path)

    def build_tree(current_path, current_depth=0):
        """递归构建目录树"""
        if max_depth is not None and current_depth > max_depth:
            return None

        tree = {
            'name': os.path.basename(current_path) if current_depth > 0 else current_path,
            'path': current_path,
            'children': []
        }

        try:
            items = sorted(os.listdir(current_path))
        except PermissionError:
            return tree

        for item in items:
            item_path = os.path.join(current_path, item)

            if not os.path.isdir(item_path):
                continue

            if item in ignore_dirs:
                continue

            subtree = build_tree(item_path, current_depth + 1)
            if subtree:
                tree['children'].append(subtree)

        return tree

    def tree_to_string(tree, indent=0, prefix=""):
        """将目录树转换为字符串"""
        if tree is None:
            return ""

        lines = []

        if indent == 0:
            lines.append(f"{tree['name']}/")
        else:
            lines.append(f"{prefix}{tree['name']}/")

        children_count = len(tree['children'])
        for i, child in enumerate(tree['children']):
            is_last = (i == children_count - 1)

            if indent == 0:
                new_prefix = "├── " if not is_last else "└── "
                next_prefix = "│   " if not is_last else "    "
            else:
                new_prefix = prefix + ("├── " if not is_last else "└── ")
                next_prefix = prefix + ("│   " if not is_last else "    ")

            child_str = tree_to_string(child, indent + 1, new_prefix if indent == 0 else next_prefix)
            if child_str:
                lines.append(child_str)

        return "\n".join(lines)

    # 构建树并转换为字符串
    tree = build_tree(project_path)
    return tree_to_string(tree)


 

# 使用示例
if __name__ == "__main__":
    project_dir = r'C:\Users\dpq\Desktop\diary-1.0.1'
    structure_str = get_project_structure(project_dir)
    print(structure_str)


