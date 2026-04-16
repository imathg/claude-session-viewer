# Claude Session Viewer

一个轻量的本地 Web 应用，用来浏览、搜索、导出 Claude Code / Claude Desktop 的历史对话。

读取 `~/.claude/projects/*.jsonl`，纯 Python + 单文件 HTML，不依赖任何外部包。

## 运行

```bash
python3 server.py
```

浏览器会自动打开 `http://localhost:8080`。未检测到 `~/.claude` 时，可在页面上手动输入路径。

## 功能

- **按项目分组**：侧边栏列出所有 project，末级文件夹加粗、父路径暗显
- **Session 展示**：优先级 `custom-title` > Claude 自动生成的 `slug` > 首条用户消息
- **预览最后一句**：侧边栏每个 session 下方显示最新的用户 query
- **类型筛选**：`全部` / `我的对话` / `定时任务` 三个过滤器；定时任务用 ⏱ 标记
- **搜索**
  - 输入即时筛选标题、预览、slug
  - 回车触发当前 project 下的全文搜索（覆盖 user / assistant / thinking 内容）
- **会话视图**：打开会话默认滚到底部，右下角浮动按钮可以一键跳顶/跳底
- **导出**：勾选任意消息，导出为独立 HTML 或复制为纯文本

## 文件结构

```
server.py    # HTTP 服务 + JSONL 解析
index.html   # 单文件前端（HTML + CSS + JS）
config.json  # 自定义路径（首次运行后生成）
```
