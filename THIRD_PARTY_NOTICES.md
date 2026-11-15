# 第三方组件与许可声明

Bonfire 本体以 MIT 许可发布（见 `LICENSE`）。下面是它运行、构建时用到的第三方组件，
以及各自的许可与分发方式。**本项目不分发任何第三方安装包或音源内容**，只声明依赖关系。

## 运行时依赖（随程序一起打包分发）

| 组件 | 版本 | 许可 | 用途 | 分发方式 |
| --- | --- | --- | --- | --- |
| [pywebview](https://pywebview.flowrl.com/) | 6.2.1 | BSD-3-Clause | 无边框窗口 + WebView2 容器 | 打包进 exe（源码形式，许可允许再分发，保留版权声明） |
| [pythonnet](https://pythonnet.github.io/) | 3.2.1 | MIT | pywebview 调用 WinForms/.NET | 同上 |
| [clr-loader](https://github.com/pythonnet/clr-loader) | 0.3.1 | MIT（pythonnet 项目） | .NET 运行时加载 | 同上 |
| [bottle](https://bottlepy.org/) | 0.13.4 | MIT | pywebview 内置 HTTP 服务 | 同上 |
| [proxy-tools](https://pypi.org/project/proxy-tools/) | 0.1.0 | MIT | 代理环境适配 | 同上 |
| [Pillow](https://python-pillow.org/) | 13.0.0 | MIT-CMU（HPND 衍生） | 图标提取 / 托盘图 | 同上 |
| [typing-extensions](https://pypi.org/project/typing-extensions/) | 4.16.0 | PSF-2.0 | 类型标注兼容 | 同上 |
| Microsoft Edge WebView2 Runtime | 系统提供 | Microsoft 专有（可自由再分发运行时） | 渲染页面 | **不打包**，依赖系统已装 |

## 构建工具（不随产物分发，只用于生成 exe）

| 组件 | 版本 | 许可 | 说明 |
| --- | --- | --- | --- |
| [PyInstaller](https://pyinstaller.org/) | 6.22.3 | GPL-2.0-or-later **+ bootloader exception** | 其 bootloader 例外明确允许用它打包并分发**闭源/其他许可**的程序，因此不影响 Bonfire 采用 MIT |
| [websocket-client](https://pypi.org/project/websocket-client/) | 1.9.2 | Apache-2.0 | **仅本地调试用**（CDP 诊断），不进 `requirements.txt`、不进产物 |

## 外部服务与第三方软件（不由本项目分发）

- **音源检索**：程序只是在你自己的电脑上向各平台公开接口发起请求取流，不内置、不缓存、不再分发任何音频内容。各平台的服务条款与版权归各自所有，使用前请自行确认合规。
- **RivaTuner Statistics Server (RTSS)**：限帧功能依赖本机已安装的 RTSS 写入其 Global profile。RTSS 是 Unwinder 的免费软件，**本项目不打包、不代装、不再分发**，仅在界面里提供跳转官网的入口。
- **字体 / 图标**：界面使用系统字体与内联 SVG，未嵌入第三方字体文件。

## 如果你要二次分发

1. 保留 `LICENSE` 与这份 `THIRD_PARTY_NOTICES.md`；
2. 若替换或新增依赖，请按上表格式补充组件、版本、许可；
3. 不要把你本地的第三方安装包（如 RTSS 安装程序）打进发布产物。
