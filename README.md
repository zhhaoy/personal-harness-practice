# Personal Harness

**Personal Agent Infrastructure Framework / 个人智能体基础设施框架**

**The only "hands and feet" of a Large Language Model are tools.**
**大模型唯一的「手脚」，就是工具。**

---

## Documentation / 文档

- **[English README](README_EN.md)**
- **[中文说明](README_CN.md)**

---

## Quick Links / 快速链接

| Item | Link |
|------|------|
| Architecture / 架构说明 | [AGENTS.md](AGENTS.md) |
| Network Guide / 网络指南 | [NETWORK_GUIDE.md](NETWORK_GUIDE.md) |
| Design Docs / 设计文档 | [design/](design/) |

---

## Quick Start / 快速开始

```bash
# Install dependencies / 安装依赖
pip install nicegui openai prompt-toolkit pyyaml requests httpx python-dotenv

# Configure environment / 配置环境
export LLM_API_BASE="your_endpoint"
export LLM_API_KEY="your_key"
export LLM_MODEL="your_model"

# Run Web UI / 运行 Web 界面
python src/web_ui.py
```

---

## License / 许可证

MIT License
