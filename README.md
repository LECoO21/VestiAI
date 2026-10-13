# FitMix

[English](README_EN.md)

FitMix 是一个可本地运行、自带网页界面的数字衣橱。它能整理衣物、保存搭配、管理形象照，并生成 AI 虚拟试穿效果。

> 本项目采用 BYOK（Bring Your Own Key）：仓库不包含任何默认 API Key，使用者自行配置 AI 服务凭证并承担相应费用。

## 功能

- 在“我的衣橱”上传单品照或人物穿搭照：只提取衣服入库，不保存整套造型
- 在“穿搭库”导入完整穿搭：保存造型，并把新识别出的单品同步到衣橱
- 按分类、季节、收藏、关键词和最近使用时间整理衣橱
- 选择上装与下装，或选择连衣裙，生成基础/高质量试穿图
- 上装与下装的组合，或单件连衣裙，都可直接保存到穿搭库
- 保存最多 6 张形象照，查看最近试穿并把结果存入穿搭库
- 编辑、收藏、合并和删除单品；删除穿搭不会连带删除独立单品与试穿历史
- 数据与图片默认保存在本机 `data/`，无需外部数据库

## 界面预览

| 我的衣橱 | 搭配选择 |
| --- | --- |
| ![我的衣橱](docs/screenshots/wardrobe.png) | ![搭配选择](docs/screenshots/try-on.png) |

| AI 试穿结果 | 穿搭库 |
| --- | --- |
| ![AI 试穿结果](docs/screenshots/try-on-result.png) | ![穿搭库](docs/screenshots/looks.png) |

截图使用专门生成的演示素材，不含作者的真实衣物、人物或历史记录。

## 系统要求

- Python 3.11 或更高版本
- 可访问阿里云百炼的网络和一个已开通百炼的阿里云账号
- Chrome、Safari、Edge 或 Firefox 的较新版本

FitMix 当前固定使用百炼 **华北 2（北京）** 地域。API Key 与 Workspace ID 必须属于该地域和同一业务空间。

## 获取百炼凭证

1. 登录[阿里云百炼控制台](https://bailian.console.aliyun.com/)，选择华北 2（北京）并开通模型服务。
2. 按[官方说明创建 API Key](https://help.aliyun.com/zh/model-studio/get-api-key)。
3. 按[官方说明获取 Workspace ID](https://help.aliyun.com/zh/model-studio/obtain-the-app-id-and-workspace-id)。
4. 准备下面两个值：

```dotenv
DASHSCOPE_API_KEY=你的_API_Key
DASHSCOPE_WORKSPACE_ID=你的_业务空间_ID
```

不要把 `.env`、API Key 或含凭证的截图提交到 GitHub。macOS App 只把凭证保存在当前 Mac；源码版不提供网页凭证入口。

## macOS App（推荐）

当前安装包适用于 **Apple Silicon Mac（M1 及更新芯片）、macOS 11 或更高版本**，无需安装 Python：

1. 从 [GitHub Releases](https://github.com/betterjally-oss/FitMix/releases/latest) 下载 `FitMix-macOS-arm64.dmg`。
2. 打开 DMG，把 FitMix 拖进“应用程序”。
3. 第一次启动时，按住 Control 点击 FitMix，选择“打开”并确认；之后可正常双击。
4. 在自动弹出的“API 设置”中填写自己的百炼 Workspace ID 和 API Key。

衣橱数据和凭证保存在 `~/Library/Application Support/FitMix`。API Key 不会进入 GitHub，也不会显示在设置页中。

## 源码安装与启动

先克隆仓库：

```bash
git clone https://github.com/betterjally-oss/FitMix.git
cd FitMix
```

### macOS（源码版）

向导会打开百炼官方页面，把凭证写入仅当前用户可读的 `.env`，并做一次不创建付费任务的验证：

```bash
chmod +x scripts/macos/setup.command scripts/macos/start.command
./scripts/macos/setup.command
./scripts/macos/start.command
```

### Windows（PowerShell）

```powershell
py -3.11 -m venv .venv
.venv\Scripts\python -m pip install -r requirements.txt
Copy-Item .env.example .env
notepad .env
.venv\Scripts\python -m fitmix.app
```

在记事本中填写两个凭证并保存，然后打开 <http://127.0.0.1:5050>。

### Linux

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
cp .env.example .env
${EDITOR:-nano} .env
.venv/bin/python -m fitmix.app
```

打开 <http://127.0.0.1:5050>。按 `Control-C` 停止服务。

## 使用流程

1. 在“我的衣橱”点击“添加衣服”，上传单件衣服或人物穿搭照。人物穿搭照只会拆分出衣服并加入衣橱，不保存整套造型。
2. 想保留完整造型时，在“穿搭库”点击“导入完整穿搭”；新识别出的衣服会同步到衣橱，明显重复的单品会自动复用。
3. 检查识别结果，按需修改名称、分类、季节与收藏状态。
4. 进入“搭配试穿”，选择“上装 + 下装”或一件“套装/连衣裙”。只保存衣服组合不产生试穿费用。
5. 添加一张单人、全身、正面、背景简单的形象照，确认预计费用后生成试穿图。
6. 在结果页比较前后效果、下载图片、继续搭配，或保存到“穿搭库”。

## 费用说明

以下操作会调用百炼并按你的阿里云账号计费：

- 导入穿搭照：视觉理解与衣物解析
- 添加单品照：视觉理解与衣物解析
- 生成试穿图：基础或高质量 OutfitAnyone 任务

浏览、搜索、筛选、编辑、收藏、保存衣服组合和删除本地记录不会调用模型。界面中的约 ¥0.20 / ¥0.50 只是演示估算，实际价格、免费额度与账单以[阿里云百炼官方计费](https://help.aliyun.com/zh/model-studio/)为准。建议在阿里云费用中心设置预算提醒或限额。

## 数据与隐私

- `.env`、`data/`、虚拟环境、缓存和系统文件已被 Git 忽略。
- SQLite 数据库、原图、衣物图和生成结果默认只保存在本机 `data/`。
- 使用 AI 功能时，相关图片会上传到阿里云百炼处理；这不是离线推理。
- 本地模式默认不启用登录，原有数据归管理员所有。配置托管演示密码后，每个浏览器会获得独立的匿名衣橱，不能查看或删除其他访客的数据。
- 匿名身份保存在浏览器的签名 Cookie 中；清除浏览器数据后会获得新的空衣橱。请仍只使用适合演示的素材。

## 托管演示

服务器通过环境变量配置部署者自己的凭证，访客无需填写 API Key：

```bash
export DASHSCOPE_API_KEY="..."
export DASHSCOPE_WORKSPACE_ID="..."
export FITMIX_DEPLOYMENT_PROFILE="demo"
export FITMIX_ACCESS_PASSWORD="访客访问密码"
export FITMIX_ADMIN_PASSWORD="管理员密码"
export FITMIX_SECRET_KEY="一段足够长的随机字符串"
export FITMIX_VISITOR_TRYON_LIMIT=3
export FITMIX_VISITOR_BUDGET_CENTS=200
export FITMIX_GLOBAL_BUDGET_CENTS=6000
gunicorn --bind 0.0.0.0:${PORT:-5050} --workers 1 --threads 4 fitmix.app:app
```

这与本地 BYOK 相互独立。访客只能生成基础试穿，每个匿名衣橱默认最多 3 次、最多 ¥2；Plus 只对管理员开放。应用还会执行默认 ¥60 的全局软件限额。模型平台侧仍应使用临时 Key 并设置消费上限，演示结束后撤销 Key。

veFaaS 演示模式默认把 SQLite 和图片写入 `/tmp/fitmix-data`，并拒绝在密码或签名密钥缺失时启动。部署包通过 `.vefaasignore` 排除本地 `.env` 与 `data/`。`/tmp` 会随实例销毁而清空，因此短期演示应固定为一个预留实例；需要长期可靠保存时，应改用 NAS 或对象存储与持久化数据库。

## 备份与恢复

**先退出 FitMix 或停止源码服务**，再备份。数据库使用 SQLite 备份接口，已提交但尚未写回主文件的数据也会保留；图片同时复制，并生成完整性校验清单。备份不包含 API Key、日志和可重建的缩略图。

源码版在项目目录运行：

```bash
.venv/bin/python -m fitmix.backup create --data-dir data
```

终端会显示新备份的目录，默认位于项目的 `backups/`。Mac App 的数据备份使用同一个工具，指定实际数据位置：

```bash
.venv/bin/python -m fitmix.backup create --data-dir "$HOME/Library/Application Support/FitMix/data"
```

检查和恢复时，将示例中的 `fitmix-实际备份名称` 换成刚才显示的目录名：

```bash
.venv/bin/python -m fitmix.backup verify backups/fitmix-实际备份名称
.venv/bin/python -m fitmix.backup restore backups/fitmix-实际备份名称 --destination restored-data
```

恢复只写入尚不存在的新目录，不覆盖原衣橱。源码版可以先用恢复目录启动检查：

```bash
WARDROBE_DATA_DIR="$PWD/restored-data" .venv/bin/python -m fitmix.app
```

Mac App 恢复时，把恢复目标设为 `~/Library/Application Support/FitMix/restored-data`。保持 App 关闭，用 Finder 将原 `data` 重命名保留，再将核验好的 `restored-data` 改名为 `data`。

数据库升级前会自动建立一份同样格式的备份；迁移失败时回滚数据库修改并停止启动。较新版本的数据不会被旧代码强行打开。备份可能包含人物照片，请保存在自己管理的位置。

## 常见问题

**页面提示“百炼凭证尚未配置”**

确认项目根目录存在 `.env`，两个变量均非空，然后完全停止并重新启动服务。

**提示凭证、地域或业务空间不匹配**

确认 API Key 与 Workspace ID 都来自华北 2（北京）的同一业务空间。macOS 可重新运行 `./scripts/macos/setup.command` 验证。

**上传失败**

仅支持 JPG/PNG。请使用服装清晰、人物单一、光线充足的图片，并检查网络与百炼控制台权限。

**端口 5050 已被占用**

先关闭旧的 FitMix 终端；或找到占用该端口的进程并停止后重试。

**模型任务失败或超时**

不会自动重复创建付费试穿。稍后在保留当前选择的情况下手动重试，并到百炼控制台检查额度、权限与任务状态。

## 卸载

App 版删除 `/Applications/FitMix.app` 即可卸载；如需同时清除衣橱数据和本地凭证，再删除 `~/Library/Application Support/FitMix`。源码版停止服务后删除 FitMix 文件夹即可。需要撤销调用权限时，请同时在百炼控制台删除对应 API Key。

## 构建 macOS 安装包

在 Apple Silicon Mac 上运行：

```bash
chmod +x scripts/macos/build.command
./scripts/macos/build.command
```

生成文件位于 `dist/FitMix.app` 和 `dist/FitMix-macOS-arm64.dmg`。

## 开发检查

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
.venv/bin/python -B scripts/check.py
```

检查脚本在临时数据目录运行，清除真实凭证配置，并禁止测试访问真实模型服务；包含既有 unittest、前端语法、Mac 脚本语法和 Gunicorn 配置检查。前端检查需要 Node.js。开发进度和当前验收范围见 [开发实施记录](开发手册/FitMix开发实施记录.md)。

## License

[MIT](LICENSE)
