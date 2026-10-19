# Deploy Console 并行任务中心版开发交接文档

## 1. 文档目的

本文档用于让未参与历史开发的新成员快速理解项目背景、核心流程、架构边界、风险点和正确的修改方式。目标不是描述每一行代码，而是防止后续开发偏离现有业务语义，尤其避免以下事故：

- Jenkins 页面显示已取消，但线上构建仍在运行。
- Jenkins 构建成功后提取错包号或部署错环境。
- Ops 页面定位失败时误点其他服务、区域或包号。
- SSH 部署更新文件失败后未恢复原文件。
- UI 改造破坏暗色主题对比度或业务状态反馈。
- 凭据被写入源码、日志、文档或 Git。

## 2. 项目背景

原始工作流由人工在多个系统之间切换完成：

1. 登录 Jenkins，逐层进入文件夹任务并填写参数。
2. 等待构建完成，从控制台日志复制镜像包号。
3. 通过 SSH 修改服务器镜像清单及 Kubernetes YAML。
4. 执行镜像拉取和 `kubectl apply`。
5. 打开 Kubernetes Dashboard 并粘贴 Token。
6. 部分项目不走 SSH，而是在鼎捷 Ops 平台选择专案、服务类型、云区和版本发布。

Deploy Console 将上述步骤集中到一个跨平台桌面程序中，并提供两条一键发布路径：

```text
Jenkins 构建成功
├─ SSH 环境部署 -> 更新镜像清单 -> 拉取镜像 -> 更新 YAML -> apply -> Dashboard
└─ Ops 云端部署 -> 选择专案/服务/区域 -> 匹配包号 -> 提交部署请求
```

并行任务中心版只使用一个系统主窗口。每个任务仍有独立的 Jenkins、Ops、线程、日志、取消事件和部署状态，但通过主工作区切换；底部任务中心负责展示全部任务的阶段、进度、耗时和结果。原版程序及其配置目录不在本项目的写入范围内。

## 3. 技术栈与运行环境

| 领域 | 技术 |
| --- | --- |
| 桌面 UI | PySide6 / Qt |
| Jenkins HTTP | requests |
| SSH/SFTP | paramiko |
| 浏览器自动化 | Selenium + Edge（Windows）/Chrome 或 Chromium（macOS、Deepin） |
| 凭据加密 | Windows DPAPI；macOS、Deepin 本地私钥/Fernet |
| 打包 | PyInstaller onefile/windowed |
| 测试 | unittest + unittest.mock |

当前默认开发环境是 Windows PowerShell，Python 3.11 已完成验证；Linux/WSL 使用 Python 3.12 完成构建和单元测试。

## 4. 代码结构与职责

### `main.py`

目前是单文件桌面应用，包含以下模块级职责：

- `ConfigStore`：读取、迁移和原子保存用户配置。
- `JenkinsClient`：任务浏览、参数读取、构建触发、日志轮询和远程取消。
- `Worker`：通过 `QThread` 执行阻塞任务，避免冻结 UI。
- UI 辅助控件：卡片、状态徽章、可搜索下拉框、任务树样式和标题栏。
- `DeployTool`：单个任务工作区，持有三个主页面、信号槽、环境管理、取消事件及流程编排；在任务中心版中以普通子控件嵌入主窗口。
- `TaskCenterPanel`：底部可折叠任务列表，显示包号或 Job、目标、阶段、总体进度、耗时和操作按钮。
- `ApplicationWindowManager`：唯一系统主窗口，使用 `QStackedWidget` 切换任务，并持有进程级部署目标锁和 SSH 合批协调器。
- DPAPI：`protect_secret` / `unprotect_secret`。
- Selenium：Ops 云端发布和 Dashboard 登录。

### 单窗口任务生命周期

1. `ApplicationWindowManager.create_window()` 为兼容旧调用名而保留，但只创建嵌入式任务，不再创建系统窗口。
2. `Ctrl+N` 和标题栏“新建任务”入口最终都创建新的 `DeployTool` 工作区；SSH 模式继承当前目标环境。底部任务中心只负责状态展示、任务切换和展开收起。
3. `DeployTool._begin_tracked_task()` 冻结本次任务标题、包号和目标；构建、SSH 与 Ops 信号持续更新任务中心摘要。
4. 一个工作区同一时间只允许一个打包或部署操作。并行操作必须新建任务，避免日志、取消事件和状态被覆盖。
5. 运行中的任务不能移除；主窗口右上角“×”隐藏到 Windows 系统托盘，任务继续在后台运行，单击托盘图标恢复。通过托盘菜单或“配置迁移 → 退出应用”请求真正退出时，仍会拒绝关闭并提示先结束运行中的任务。
6. 任务切换只改变可见工作区，不影响后台 `Worker`、合批等待或远程命令。

不要把 `DeployTool` 再改回 `Qt.Window`，也不要在任务切换时销毁或重建运行中的工作区。`windows` 属性目前表示任务集合，名称仅为旧调用兼容。

不要在没有测试保护的情况下把 `main.py` 大规模拆分或重写。后续若要模块化，建议按以下顺序渐进拆分：

1. `jenkins_client.py`
2. `config_store.py`
3. `deployment/ssh.py`
4. `deployment/ops.py`
5. `ui/` 页面和控件

每次只拆一个职责，并保持公共调用行为不变。

### `core.py`

只放无 UI、无网络、无磁盘副作用的纯业务函数：

- Dashboard 稳定 ID 绑定和重命名。
- 二开构建路径识别及参数排序。
- Jenkins Job API URL 拼接。
- 包号提取和拆分。
- 镜像清单与 YAML 镜像更新。

新增可纯函数化的业务规则时优先放这里，并补单元测试。

### `test_core.py`

覆盖核心业务、Jenkins 重试/取消、一键云端流程分支和错误处理。所有网络、浏览器和远程操作必须 Mock，测试不能操作真实环境。

### `design-system/deploy-console/MASTER.md`

UI 设计系统的唯一依据。当前采用深色、高密度运维控制台风格：

- Slate 深色表面。
- 蓝色用于登录、刷新、配置等控制操作。
- 绿色用于构建、部署和自动登录等正向主操作。
- 红色用于取消、删除等危险操作。
- 所有暗色弹出列表必须显式设置白色文字，不能依赖 Windows 原生主题继承。
- 正文、表单、任务树和下拉列表必须保持足够对比度。

## 5. 配置与凭据

运行时配置文件：

```text
Windows: %APPDATA%\BuildDeployToolParallel\config.json
macOS:   ~/Library/Application Support/BuildDeployToolParallel/config.json
Deepin:  ${XDG_CONFIG_HOME:-~/.config}/BuildDeployToolParallel/config.json
```

主要结构：

```json
{
  "jenkins": {
    "url": "",
    "username": "",
    "password": "平台加密密文",
    "values": {
      "任务/路径": {
        "参数名": "参数值"
      }
    }
  },
  "cloud_deploy": {
    "url": "",
    "username": "",
    "password": "平台加密密文",
    "project": "",
    "deploy_type": "",
    "cloud": "",
    "region": ""
  },
  "cloud_status": {
    "url": "",
    "username": "",
    "password": "平台加密密文"
  },
  "environments": [],
  "dashboards": [],
  "last_package": ""
}
```

约束：

- Windows 使用 DPAPI；macOS 和 Deepin 使用本地私钥/Fernet 加密配置值。
- macOS、Deepin 直接使用权限为 `0600` 的 `.credential.key`，避免 Keychain 或 Secret Service/DBus 阻塞；不得放宽该文件权限。
- 各平台密文不可互相迁移，首次切换系统时必须重新录入密码和 Token。
- 配置保存采用临时文件替换，避免写到一半损坏原配置。
- 不要将真实配置或解密后的凭据加入项目。
- 日志中不得输出 SSH 密码、Jenkins Token、Ops 密码、Rancher 密码或 Dashboard Token。

## 6. Jenkins 核心流程

### 登录与任务浏览

`login_jenkins` 创建 `JenkinsClient`，后台通过 `/whoAmI/api/json` 验证非匿名登录身份。Windows 首次登录递归读取完整任务树，已有有效任务树缓存则在身份验证通过后复用；验证和加载全部成功才显示“登录成功”并收起连接栏，失败保持展开并显示具体错误。参数和分支按任务点击读取并缓存，不等待 Ops 目标加载。任务树刷新验证身份后只更新目录，保留已有参数和云端目标缓存，不受搜索过滤限制。macOS 和 Deepin 同样先验证身份，仍按任务树展开按需读取子任务。登录行为回归见 `test_jenkins_login.py`。

需要保留的行为：

- 使用 Jenkins 返回的真实 Job URL，不要仅依赖显示名称重建 URL。
- Windows 持久缓存必须校验版本、Jenkins 地址、账号和节点结构；手动刷新成功后才覆盖旧缓存，刷新失败时保留已有缓存。
- `selection_cache` 保存参数定义及云端界面选项，按平台地址、账号及 Job 或云厂商/专案隔离，跨任务和重启复用；缓存命中直接渲染，不创建 Worker。构建参数及分支仅在点击未缓存任务时读取，启动和任务树刷新均不遍历预加载；旧版本完整或部分缓存直接兼容，失败任务再次点击可重试。参数缓存不保存密码参数默认值，配置导出不携带此缓存。异步结果必须校验连接身份和当前请求，旧连接结果不得覆盖新连接。分支刷新只更新对应参数，云端选项加载和刷新仍遍历全部专案，真实部署仍由 API 校验。
- Git 参数通过 Git Parameter 插件接口读取候选值。
- `GIT_BRANCH`/`GIT_BRANCH2` 参数旁提供“刷新分支”按钮，通过 Jenkins Git Parameter 接口异步更新当前列表；刷新失败保留原列表，且不重置同一任务的其他参数。
- 二开任务的 `GIT_BRANCH2` 为空时回退到 `GIT_BRANCH`。
- 每个 Job 的参数值按任务路径保存。
- 状态集仅查询当前任务工作区发起的构建，按 Jenkins 连接、Job 路径及 URL 绑定本次构建编号；切换模块使旧请求失效，手动刷新不回退到 `lastBuild`。未打包或排队取消且无编号时隐藏详情；继续打包替换该 Job 上次尝试的绑定。
- 任务路径和打包操作栏在配置滚动区外。页签可拖动，`tab_order` 用页面 objectName 保存顺序，各任务同步；程序跳转通过页面标识定位，禁止依赖固定索引。
- APP 任务允许对应系统的 mobile 专案（ETM → etmmobile），自动匹配前端；WEB/SERVICE 仍按原系统精确校验，不使用任意前缀匹配。

### 构建与包号

`start_build` 校验配置并冻结本次目标，`_run_pipeline` 负责串联完整流程。

`_launch_build` 统一启动首次构建与“继续”：启动时保存仅驻留内存的配置快照，每次尝试使用独立副本，并绑定原 Jenkins 客户端。仅已取消且全部 Worker 收尾后允许继续；重新提交完整打包流程、清除旧进度/包号并重新登记 SSH 批次。后续表单修改不得改变快照，新建其他操作时清除旧快照，失败或结果未知不得当作已停止提供继续。

Jenkins 构建完成后必须满足：

- `result == SUCCESS` 才能继续部署。
- `ABORTED` 必须归类为取消，不得弹“一键流程失败”。
- 必须从控制台日志识别到完整包号，否则停止部署。
- 包号匹配规则修改时必须更新 `core.extract_package_number` 的测试样例。

### 取消语义

取消不是只设置本地标志。正确语义是：

- 排队阶段调用 `cancelQueue`，轮询确认队列取消或条目消失；若条目已进入构建，则继续终止对应构建。
- 构建阶段按 `/stop -> /term -> /kill` 分级请求，并轮询 `building` 直到为 `false`。
- 使用锁避免 UI 取消线程与构建轮询线程重复执行整套取消流程。
- 只有确认 Jenkins 不再构建后，UI 才能显示“已确认终止”。
- 如果 Jenkins 仍运行，必须明确报错并提示人工检查，不能假装取消成功。

## 7. SSH 环境部署流程

入口为 `start_deploy` 或一键流程中的 `_execute_deploy`。

固定顺序：

1. 校验包号。
2. 建立 SSH/SFTP 连接。
3. 更新镜像清单。
4. 执行镜像拉取命令。
5. 更新 YAML 的目标镜像。
6. 执行应用命令，例如 `kubectl apply`。

文件安全规则：

- 更新远程文件前，在同目录创建 `.bak.YYYYMMDDHHMMSS` 备份。
- 上传或校验失败时尝试恢复备份。
- Windows SSH 部署调用 `core.update_image_list(..., clear_existing=True)`，备份后清空镜像清单并仅写入本次包号，避免拉取脚本重复处理历史镜像。
- macOS 和 Deepin 暂时仍使用 `core.update_image_list` 的默认行为，只按镜像名称替换目标行，不得误改其他镜像。
- `core.update_yaml_image` 只修改匹配镜像的 `image:` 行。
- 命令非零退出必须停止后续步骤。
- 部署阶段取消只能在当前远程步骤结束后阻止后续步骤；不要在线程中强杀 SSH 命令而留下未知状态。

## 8. Ops 云端部署流程

TAB3 通过 IAM/BFF API 加载目标并部署；显式点击“登录并加载部署数据”时，在数据加载后调用 Selenium 打开可见浏览器并自动登录 Ops 网页。刷新和后台部署不启动浏览器，网页登录失败不清除 API 会话。旧 Selenium 部署方法仍仅供排障，不会自动回退，以免接口响应不明时再次点击造成重复部署。

流程：

1. `OpsApiClient.login` 完成 IAM 公钥、AES 密钥和登录握手，Token 只保存在内存中。
2. `_load_cloud_options` 通过 API 读取专案、服务类型和云区节点。
3. `_run_cloud_deploy` 使用冻结的专案、类型、云厂商、区域和包号解析唯一部署目标。
4. 提交前调用镜像仓库接口精确校验仓库名和版本号。
5. 调用部署接口提交一次请求；部署 POST 禁止自动重试。
6. 提交后轮询部署记录，只有发现相对提交前新增或变化的同包记录才报告成功。
7. 确认提交成功后，使用已保存的 Rancher 账号密码打开并登录 Pod 状态页；自动登录失败时回退为默认浏览器并复制密码。

必须遵守的防误发规则：

- 专案、服务类型、云厂商、区域、包号任一项缺失都不提交。
- 一键流程启动前复制并冻结 `cloud_settings`，避免构建期间用户修改目标。
- 包号必须在目标镜像仓库中精确匹配，找不到时停止，绝不能选择第一项或最新项兜底。
- 不得为部署 POST 配置 requests/urllib3 自动重试。
- 历史中已有同包记录不能作为本次提交成功的依据。
- 提交响应中断、提交后查询记录失败或轮询不到新记录时必须显示“部署结果未知”，先人工核对 Ops 记录，禁止直接重试。
- API 字段或路径变化时，应先通过浏览器开发者工具重新确认协议，再更新 `ops_api.py`。
- Ops 已接受的部署请求无法通过当前工具撤回，取消只对提交前有效。

## 9. Dashboard 自动登录

环境页面与 Dashboard 使用不同的打开策略：

- 双击环境名称列，使用系统默认浏览器打开环境 URL，并将已保存的环境密码复制到剪贴板、显示账号；默认浏览器不执行自动填充。
- 双击 Dashboard 地址列，自动查找 Token 输入框、填写 Token 并登录；Windows 登录成功后将当前 Dashboard 域名导航到 `/#/pod?namespace=etm`，直接显示 ETM Pods。
- 成功后保留浏览器供用户使用。
- Dashboard 页面结构不兼容时，回退为复制 Token 并使用系统默认浏览器打开 Dashboard。
- 不要移除回退路径；自动化失败不能阻止用户手工登录。

## 10. UI 与线程模型

所有网络、SSH、Selenium 和长时间操作必须通过 `Worker(QThread)` 执行。后台线程不能直接修改 Qt 控件，应通过 `DeployTool.emit` 映射到信号，再由主线程槽函数更新 UI。

任务中心折叠高度固定为 46px，展开后由底部 `QDockWidget` 占用约 270px。任务失败时自动展开；任务行点击切换 `QStackedWidget` 当前页。点击包号列或选中任务后按 Ctrl+C 可复制完整包号，无包号时不修改剪贴板。环境部署左栏使用滚动容器，避免展开任务中心时压缩表单行。不要把完整任务表单复制进底栏，也不要为任务恢复默认新窗口行为。

状态规则：

- 操作超过约 300ms 时显示进度或状态文字。
- 操作中禁用重复提交按钮。
- 成功、进行中、失败/取消分别使用绿色、蓝色、红色徽章，并保留文本说明，不能只靠颜色。
- 暗色输入框和弹出列表内部文字必须为白色或高对比浅色。
- 所有按钮、输入框和下拉框必须保留可见焦点状态。
- 不要恢复旧版大面积白色卡片，也不要混用多套圆角、阴影或图标风格。

## 11. 测试策略

每次修改至少运行：

```powershell
python -m py_compile main.py core.py ops_api.py test_core.py test_package_inference.py test_parallel_windows.py test_ui_regressions.py
python -m unittest test_core.py test_package_inference.py test_parallel_windows.py test_ui_regressions.py -v
```

修改以下区域时应补对应测试：

| 修改区域 | 必须覆盖 |
| --- | --- |
| 包号正则 | 正常日志、多个镜像、无匹配 |
| 镜像/YAML 更新 | 精确替换、不相关行不变 |
| Jenkins 重试 | 502/503/504、最终成功、最终失败 |
| Jenkins 取消 | queue、stop、term、kill、ABORTED |
| 一键分支 | SSH 与 Ops 不互相串流程 |
| 服务类型匹配 | 大小写、空格、前端/后端区分 |
| 配置迁移 | Dashboard ID 绑定与重命名 |
| Jenkins 参数 | 分支刷新成功/失败、其他参数值保留 |
| 任务中心 | 子任务不是系统窗口、折叠/失败展开、切换/移除、运行中禁止重入 |
| 主窗口生命周期 | 右上角关闭最小化、显式退出受运行状态保护 |

真实环境验证要谨慎：

- Jenkins 构建会产生线上任务。
- SSH 部署会修改远程文件并执行集群命令。
- Ops 最终提交会改变云端环境。
- 未获得明确授权时，只允许做登录、读取、截图等只读验证，不得提交部署。

## 12. 打包与发版

项目只保留一个打包配置：`DeployConsole.spec`。

```text
Windows: build.bat
macOS:   build-macos.sh
Deepin:  build-deepin.sh
```

产物：

```text
Windows: release/DeployConsoleBatchSafe.exe
macOS:   dist/DeployConsoleParallel.app、dist/DeployConsoleParallel-macos.dmg
Deepin:  dist/DeployConsoleParallel、dist/DeployConsoleParallel-deepin25-x86_64.tar.gz
```

发版前检查：

1. 单元测试全部通过。
2. `python main.py` 能正常启动。
3. Jenkins、环境部署、云端部署三个 TAB 无文字溢出和暗色文字问题。
4. 底部任务中心可收起、展开、切换任务，失败状态不会只靠颜色表达。
5. 下拉弹窗普通项和选中项均为高对比文字。
6. 打包完成后检查对应平台产物的更新时间与 SHA256。
7. macOS 正式分发前完成代码签名和 Apple 公证。
8. Deepin 在真实 DDE 桌面验证 XCB、剪贴板和 Chrome/Chromium 自动登录。

不要继续创建 `stable`、`new`、`v2`、`v3` 等临时 spec 或历史 EXE。需要版本号时使用 Git tag 或发行记录，不要依赖文件名管理历史。

Windows 打包配置会在依赖分析前自动从当前进程 PATH 排除 `codex-runtimes` 下的工具目录，避免误收 Poppler 的 `icuuc.dll` 而导致 QtCore 加载失败。切换打包环境后应使用 `--clean`（`build.bat` 已包含）或独立 workpath。可运行 `python -m unittest test_build_spec -v` 回归此隔离规则；交付前必须执行 EXE 的两项自检。

## 13. 已知风险与后续建议

### 跨平台发布

- PyInstaller 不支持跨操作系统生成原生程序，三个平台必须分别原生构建。
- 当前 Windows 与 Linux/WSL AMD64 已完成构建；Deepin 目标版本为 25 AMD64。
- macOS 脚本已生成 `.app/.dmg` 流程，但必须在 Intel 或 Apple Silicon Mac 上分别构建、签名和验证。
- Ubuntu 24.04 构建物依赖较新的 glibc，不承诺兼容 Deepin 20/23；需要兼容旧 Deepin 时应在对应系统或兼容构建容器中重新打包。

### 高优先级风险

- Ops 和 Dashboard 依赖外部页面 DOM，平台升级后可能失效。
- `main.py` 体积较大，继续堆叠功能会增加回归风险。
- 当前 SSH 使用 `AutoAddPolicy` 接受未知主机密钥，内部工具使用方便但安全性有限。
- HTTPS Jenkins 当前关闭证书校验，若用于更严格环境应增加可配置证书验证。

### 推荐演进顺序

1. 为 Ops 页面定位器增加诊断快照或脱敏 DOM 日志。
2. 将 Jenkins、SSH、Ops 分模块拆出，保持现有接口。
3. 为配置增加显式版本号和迁移函数。
4. 增加应用关闭时释放后台 Ops Edge Driver 的逻辑。
5. 若 Ops 提供稳定 API，优先替换 Selenium 提交流程。

## 14. 新成员开始开发前检查表

- [ ] 阅读本文档和 `README.md`。
- [ ] 阅读 `design-system/deploy-console/MASTER.md`。
- [ ] 本地运行 20 项以上现有测试并确认全绿。
- [ ] 确认没有真实密码、Token 或配置进入工作区。
- [ ] 明确本次修改属于 Jenkins、SSH、Ops、Dashboard、UI 中哪一层。
- [ ] 先补或定位测试，再做小范围修改。
- [ ] 涉及真实部署时取得明确授权。
- [ ] 最终只使用 `DeployConsole.spec` 打包。

## 15. 2026-08-17 任务中心版交付记录

- `python -m unittest test_core.py test_package_inference.py test_parallel_windows.py -v`：100 项测试全部通过。
- Qt 离屏集成检查：两个任务的 `isWindow()` 均为 `False`，任务工作区只存在于主窗口 `QStackedWidget` 中；第二任务创建、失败展开、切换和移除均通过。
- Windows 构建及凭据/Selenium 自检通过：`dist/DeployConsoleBatchSafe.exe`，SHA256 `2B10BEDC1548850D9E362F66D8357D2332D1A5C72C4216714799A74D2D1F4571`。
- WSL Ubuntu 24.04 x86_64 构建及凭据/Chrome 后端自检通过：`dist/DeployConsoleParallel`，SHA256 `FE43EA2D2EBA07AE254211AB28A230EE5007551468FE436EF0F54A977CF20F6F`。
- Deepin 压缩包：`dist/DeployConsoleParallel-deepin25-x86_64.tar.gz`，SHA256 `11E98645815F649E233A6D76C742DB0C1C57E535EC4A833C20C790270276FB28`。
- WSL 构建环境缺少部分 XCB/TIFF 动态库，PyInstaller 未将其打入单文件；真实 Deepin 25 DDE 应安装对应桌面运行库，并完成 XCB 启动、剪贴板和 Chrome/Chromium 验收。
- `build-macos.sh` 已通过 shell 语法检查，并会临时生成原生 `.icns` 后构建 `.app/.dmg`；本机不是 macOS，缺少 `hdiutil`、`codesign` 和 Xcode 工具链，因此本次未生成或伪造 macOS 产物。必须在真实 Mac 上重新执行脚本、签名和验证。
