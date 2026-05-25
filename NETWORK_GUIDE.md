# 网络配置指南

## 问题描述

如果遇到以下错误：
```
❌ 错误: API错误: Request timed out
```

这是网络超时错误，通常由以下原因导致：
1. 网络连接不稳定
2. API 服务器响应慢
3. 代理未配置（特别是在中国大陆）
4. 防火墙阻断

## 解决方案

### 方案1: 设置代理（推荐中国大陆用户）

**Windows PowerShell:**
```powershell
$env:HTTP_PROXY = "http://127.0.0.1:7890"
$env:HTTPS_PROXY = "http://127.0.0.1:7890"
```

**Linux/Mac:**
```bash
export HTTP_PROXY="http://127.0.0.1:7890"
export HTTPS_PROXY="http://127.0.0.1:7890"
```

### 方案2: 使用系统代理

如果你的系统已经配置了代理，确保环境变量正确：
```bash
# 检查当前代理设置
echo $HTTP_PROXY
echo $HTTPS_PROXY
```

### 方案3: 修改超时设置

如果网络较慢，可以在代码中增加超时时间：

```python
# 在 agent_loop.py 中找到以下行（约第 2275 行）
http_client = httpx.Client(
    timeout=httpx.Timeout(30.0, read=180.0, write=60.0, connect=30.0),
    ...
)

# 修改为更大的值
timeout=httpx.Timeout(60.0, read=300.0, write=120.0, connect=60.0)
```

## 已实施的改进

在最新版本中，我们已实施以下改进：

| 改进项 | 原值 | 新值 |
|--------|------|------|
| 读取超时 | 60s | 180s |
| 连接超时 | 未设置 | 30s |
| 写入超时 | 未设置 | 60s |
| 重试次数 | 2 | 3 |
| 重试策略 | 无 | 指数退避 |
| 代理支持 | 无 | 自动读取环境变量 |

## 测试网络连接

运行以下命令测试网络是否正常：

```bash
# 测试 API 连接
curl -v https://api.openai.com/v1/models

# 或使用 Python
python -c "import httpx; r = httpx.get('https://api.openai.com'); print(r.status_code)"
```

## 常见错误及解决方案

### 错误1: Connection refused
- 检查 API endpoint 是否正确
- 检查防火墙设置

### 错误2: Name resolution failed
- 检查 DNS 设置
- 尝试使用代理

### 错误3: SSL certificate verify failed
- 更新 CA 证书
- 或在 httpx 中设置 `verify=False`（不推荐）

### 错误4: Proxy connection failed
- 检查代理地址是否正确
- 确认代理服务正在运行

## 代理推荐

如果你在中国大陆，推荐使用以下代理方案：

1. **Clash** - 支持 HTTP/SOCKS5 代理
2. **V2Ray** - 支持 VMess 协议
3. **Shadowsocks** - 轻量级代理

配置示例（Clash）:
```yaml
# clash 配置文件
port: 7890        # HTTP 代理端口
socks-port: 7891  # SOCKS5 代理端口
```

然后设置环境变量：
```bash
export HTTP_PROXY="http://127.0.0.1:7890"
export HTTPS_PROXY="http://127.0.0.1:7890"
```

## 日志调试

如果问题持续，可以启用调试日志：

```python
# 在 agent_loop.py 开头添加
import logging
logging.basicConfig(level=logging.DEBUG)
```

这将显示详细的网络请求日志，帮助定位问题。
