# Electron 桌面壳

正式使用和源码启动步骤见项目根目录 [README](../README.md)。桌面壳通过本地 Product Shell 展示会议状态；V1.0.3 的默认主脑交接方式为手动 GPT Handoff。GPT 网页自动化、Browser Extension 与 API Brain 属于非默认实验路径。

开发时在仓库根目录先安装 Python 依赖，再在本目录运行：

    npm ci
    npm start

本地 macOS 应用包可在依赖就绪后使用 `python3 scripts/build_v1_app.py` 构建。构建脚本不会打包数据库、日志、Cookie 或浏览器配置文件；生成的未签名应用不等同于经过公证的正式安装包。
