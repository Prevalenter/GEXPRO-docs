# 项目目标
本目录是 GEXPRO 项目的文档站发布目录：一套手工维护的静态 HTML 站点（Sphinx + Furo 风格），
由 GitHub 仓库 `Prevalenter/GEXPRO-docs` 通过 GitHub Pages 发布。
线上地址：https://prevalenter.github.io/GEXPRO-docs/


# 发布规则（最高优先级）
- **绝对不要主动推送 GitHub。** 不要执行 `git push`，也不要执行其他对远端的写操作
  （`push --force`、推送 tag、删除/新建远端分支、创建 PR、改动仓库或 Pages 设置等）。
- 只允许**人工推送**。所有内容都先在本地改好（必要时做本地 `git commit`），
  然后停下来，明确告诉用户「已准备好，请人工推送」。
- 即使用户的需求听起来像是“把文件放到网站上去”“上线”“发布”“给个下载链接”，
  也只在本地完成修改，并给出推送后将生效的链接，不要代为推送。
- 只有用户明确说出“推送 / push / 可以推了”这类指令时，才允许执行推送。
- 如果发现本地有未提交或未推送的改动，只需说明状态，不要擅自推送。


# 网站结构
- `index.html`：首页（含 Release status 列表）
- `hardware/bom.html`：硬件物料清单（BOM）
- `hardware/mechanical-design.html`：机械设计文件页，含 GX16 / EX16 压缩包下载链接
- `hardware/GX16 STEP file.zip`、`hardware/EX16 STEP file.zip`：GX16 灵巧手与 EX16 外骨骼手套的
  STEP 模型压缩包，直接以普通文件提交（每个几 MB），不要引入 Git LFS
- `_static/`：Furo 主题样式、脚本与图片资源
- `.nojekyll`：关闭 GitHub Pages 的 Jekyll 处理，不要删除


# 编辑约定
- 本站是生成后的静态 HTML，直接编辑 HTML，并保持既有结构：新页面复制已有页面的
  `head`、侧边栏（`toctree`）与 footer，只替换 `<article>` 内的正文。
- 新增页面时，要在 `index.html` 与 `hardware/*.html` 的侧边栏中补上导航项
  （当前页加 `current-page`），并在 `index.html` 的 Release status 列表中登记，
  状态统一使用 `Draft` / `Planned` 这类标签。
- 站内相对链接要与文件所在层级一致：根目录页面用 `hardware/xxx.html`，
  `hardware/` 下的页面用 `xxx.html`、`../index.html`。
- 文件名中的空格在链接里要写成 `%20`，例如
  `hardware/GX16%20STEP%20file.zip`。
- 下载链接使用 `<a href="..." download>`，方便直接保存。
- `.gitignore` 已忽略 `.DS_Store` 以及解压目录 `hardware/GX16 STEP file`、
  `hardware/EX16 STEP file`；只提交 zip 压缩包，不要提交解压后的模型文件。


# 内容要求
- 只用文件列表、目录结构和体积等可核实的事实描述压缩包内容，不要编造材料、公差、
  打印参数或装配细节。
- 压缩包内容更新后，同步更新 `hardware/mechanical-design.html` 中的文件数量、
  体积和更新日期。
- 页面文案与既有站点保持一致的英文风格。


# 交付方式
- 每次改动结束后，向用户说明：改了哪些文件、本地是否已提交、**需要人工 `git push`**，
  以及推送后生效的完整链接。
