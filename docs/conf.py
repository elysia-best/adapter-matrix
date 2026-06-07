"""Sphinx 配置 —— NoneBot Adapter Matrix 文档。

基于 GHA page 自动部署，使用 Furo 主题与 MyST 解析器。
"""

from __future__ import annotations

import sys
from pathlib import Path

# 将项目根加入搜索路径，确保 autodoc 可导入
_docs_dir = Path(__file__).resolve().parent
_project_root = _docs_dir.parent
sys.path.insert(0, str(_project_root))

# nonebot.adapters 在 site-packages 中是常规包，无法通过命名空间包机制
# 发现项目根下的 nonebot.adapters.matrix。这里手动扩展 __path__ 来解决。
import nonebot.adapters

_project_adapters = str(_project_root / "nonebot" / "adapters")
if _project_adapters not in nonebot.adapters.__path__:
    nonebot.adapters.__path__.append(_project_adapters)
del nonebot

# -- 项目信息 ----------------------------------------------------------------

project = "NoneBot Adapter Matrix"
copyright = "2025, Elysia"
author = "Elysia"
release = "0.3.0"

# -- 通用配置 ----------------------------------------------------------------

extensions = [
    "sphinx.ext.autodoc",
    "sphinx.ext.autosummary",
    "sphinx.ext.viewcode",
    "sphinx.ext.intersphinx",
    "sphinx_autodoc_typehints",
    "myst_parser",
]

templates_path = ["_templates"]
exclude_patterns = ["_build", "Thumbs.db", ".DS_Store"]

language = "zh_CN"

# -- autodoc 配置 ------------------------------------------------------------

autodoc_typehints = "description"
autodoc_member_order = "bysource"
autodoc_default_options = {
    "undoc-members": False,
    "show-inheritance": True,
}

# 默认不自动包含成员，仅在需要时显式使用 :members:
autodoc_include_members = False

# -- intersphinx 配置 --------------------------------------------------------

intersphinx_mapping = {
    "python": ("https://docs.python.org/3", None),
}

# -- MyST 配置 ---------------------------------------------------------------

myst_enable_extensions = [
    "colon_fence",
    "deflist",
    "html_admonition",
    "fieldlist",
]

# -- HTML 输出 ---------------------------------------------------------------

html_theme = "furo"
html_title = "NoneBot Adapter Matrix"
html_static_path = ["_static"]
html_theme_options = {
    "source_repository": "https://github.com/nonebot/adapter-matrix",
    "source_branch": "master",
    "source_directory": "docs/",
}
