#!/usr/bin/env python3
"""
Agent Loop - 支持多模型的工具调用循环（bash only）
兼容模型：GLM、DeepSeek、Qwen（通过OpenAI兼容API）
环境变量：
    LLM_API_BASE   - API基础URL（必需）
    LLM_API_KEY    - API密钥（必需）
    LLM_MODEL      - 模型名称（必需）
"""

import os
import subprocess
import sys
from typing import Dict, List, Any, Optional, Tuple
from dataclasses import dataclass, field
import json

# 尝试导入OpenAI库
try:
    from openai import OpenAI, APIError
except ImportError:
    print("错误: 请安装 openai 库: pip install openai")
    sys.exit(1)

# 加载环境变量（如果存在.env文件）
try:
    from dotenv import load_dotenv

    load_dotenv(override=True)
except ImportError:
    pass

# 配置常量
DEFAULT_TIMEOUT = 120  # 单次命令超时（秒）
MAX_OUTPUT_SIZE = 50000  # 工具输出最大字符数
BLOCKED_COMMANDS = [
    "rm -rf /",
    "sudo",
    "shutdown",
    "reboot",
    "mkfs",
    "dd if=",
    "> /dev/",
    "chmod 777",
]

# 工具定义（OpenAI格式）
BASH_TOOL = {
    "type": "function",
    "function": {
        "name": "bash",
        "description": "执行一条Shell命令。当前工作目录: " + os.getcwd(),
        "parameters": {
            "type": "object",
            "properties": {
                "command": {"type": "string", "description": "要执行的Shell命令"}
            },
            "required": ["command"],
            "additionalProperties": False,
        },
    },
}

# 系统提示
SYSTEM_PROMPT = f"""你是一个智能编码助手，当前工作目录: {os.getcwd()}。
你可以使用bash工具运行Shell命令来解决用户的问题。
请遵循以下原则：
- 优先使用bash工具，每一步操作后分析输出，决定下一步。
- 确保命令安全，不要执行具有破坏性的操作。
- 如果命令输出过长，工具会截断，必要时请使用| head -n 50等限制输出。
- 完成任务后，给出清晰的最终答案。"""


@dataclass
class ToolCallRecord:
    """记录单次工具调用"""

    command: str
    output: str
    error: Optional[str] = None


@dataclass
class AgentResult:
    """Agent执行结果"""

    final_answer: str  # 最终回答文本
    tool_calls: List[ToolCallRecord] = field(default_factory=list)
    error: Optional[str] = None  # 整体错误信息


def run_bash(command: str, timeout: int = DEFAULT_TIMEOUT) -> Tuple[str, Optional[str]]:
    """
    执行bash命令
    返回: (输出内容, 错误信息) 错误信息为None表示成功
    """
    # 安全过滤
    cmd_lower = command.lower().strip()
    for blocked in BLOCKED_COMMANDS:
        if blocked in cmd_lower:
            return "", f"危险命令被阻止: {command}"

    try:
        result = subprocess.run(
            command,
            shell=True,
            cwd=os.getcwd(),
            capture_output=True,
            text=True,
            timeout=timeout,
        )
        # 合并stdout和stderr
        output = (result.stdout + result.stderr).strip()
        if not output:
            output = "(无输出)"
        # 截断过长输出
        if len(output) > MAX_OUTPUT_SIZE:
            output = (
                output[:MAX_OUTPUT_SIZE]
                + f"\n...[输出已截断，超出{MAX_OUTPUT_SIZE}字符]"
            )
        return output, None
    except subprocess.TimeoutExpired:
        return "", f"命令执行超时 ({timeout}秒)"
    except FileNotFoundError:
        return "", f"命令不存在: {command}"
    except Exception as e:
        return "", f"执行异常: {str(e)}"


class MultiModelClient:
    """统一的多模型客户端（基于OpenAI兼容API）"""

    def __init__(self):
        api_base = os.getenv("LLM_API_BASE")
        api_key = os.getenv("LLM_API_KEY")
        self.model = os.getenv("LLM_MODEL")

        if not all([api_base, api_key, self.model]):
            raise ValueError("请设置环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")

        self.client = OpenAI(
            base_url=api_base, api_key=api_key, timeout=60.0, max_retries=2
        )

    def chat(self, messages: List[Dict], tools: List[Dict]) -> Dict:
        """
        调用模型，返回响应字典
        包含: content, tool_calls列表, finish_reason
        """
        try:
            response = self.client.chat.completions.create(
                model=self.model,
                messages=messages,
                tools=tools,
                tool_choice="auto",
                temperature=0.1,
                max_tokens=4096,
            )
            choice = response.choices[0]
            message = choice.message

            # 提取tool_calls
            tool_calls = []
            if message.tool_calls:
                for tc in message.tool_calls:
                    tool_calls.append(
                        {
                            "id": tc.id,
                            "type": tc.type,
                            "function": {
                                "name": tc.function.name,
                                "arguments": tc.function.arguments,
                            },
                        }
                    )

            return {
                "content": message.content or "",
                "tool_calls": tool_calls,
                "finish_reason": choice.finish_reason,
            }
        except APIError as e:
            # API特定错误（包括500、404等）
            return {
                "content": "",
                "tool_calls": [],
                "finish_reason": "error",
                "error": f"API错误: {e}",
            }
        except Exception as e:
            # 其他所有异常（网络、超时等）
            return {
                "content": "",
                "tool_calls": [],
                "finish_reason": "error",
                "error": f"调用失败: {str(e)}",
            }


def agent_loop(initial_prompt: str, max_iterations: int = 20) -> AgentResult:
    """
    主控循环：执行工具调用直到模型不再请求工具
    参数:
        initial_prompt: 用户初始输入
        max_iterations: 最大循环次数（防止无限循环）
    返回:
        AgentResult 包含最终答案、工具调用记录、错误信息
    """
    # 初始化客户端
    try:
        llm = MultiModelClient()
    except ValueError as e:
        return AgentResult(final_answer="", error=str(e))

    # 构建初始messages
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": initial_prompt},
    ]

    tool_records: List[ToolCallRecord] = []
    iteration = 0

    while iteration < max_iterations:
        iteration += 1

        # 调用大模型
        resp = llm.chat(messages, [BASH_TOOL])

        # 处理API错误
        if resp.get("error"):
            error_msg = f"模型调用失败: {resp['error']}"
            return AgentResult(
                final_answer="", tool_calls=tool_records, error=error_msg
            )

        # 记录assistant的回复
        assistant_msg = {"role": "assistant", "content": resp["content"]}
        if resp["tool_calls"]:
            # 转换tool_calls格式为OpenAI API标准
            assistant_msg["tool_calls"] = [
                {"id": tc["id"], "type": tc["type"], "function": tc["function"]}
                for tc in resp["tool_calls"]
            ]
        messages.append(assistant_msg)

        # 如果没有工具调用，循环结束
        if not resp["tool_calls"]:
            return AgentResult(
                final_answer=resp["content"], tool_calls=tool_records, error=None
            )

        # 执行所有工具调用（当前只有bash）
        for tc in resp["tool_calls"]:
            # 解析参数
            try:
                args = json.loads(tc["function"]["arguments"])
                command = args.get("command", "")
            except json.JSONDecodeError:
                command = ""
                output = ""
                error = f"工具参数解析失败: {tc['function']['arguments']}"
            else:
                # 执行bash
                output, error = run_bash(command)
                # 记录调用
                tool_records.append(
                    ToolCallRecord(command=command, output=output, error=error)
                )

            # 构建工具结果消息
            tool_result_content = output if not error else f"错误: {error}"
            tool_result_msg = {
                "role": "tool",
                "tool_call_id": tc["id"],
                "content": tool_result_content,
            }
            messages.append(tool_result_msg)

    # 达到最大迭代次数
    return AgentResult(
        final_answer="达到最大循环次数，可能未完成任务。",
        tool_calls=tool_records,
        error="循环超限",
    )


def main():
    """交互式命令行入口"""
    print("\n=== 多模型Agent循环 (仅bash工具) ===")
    print("环境变量: LLM_API_BASE, LLM_API_KEY, LLM_MODEL")
    print("输入任务后，Agent将循环调用工具直至完成。输入 'exit' 退出。\n")

    while True:
        try:
            user_input = input("\033[36m用户 >> \033[0m")
        except EOFError, KeyboardInterrupt:
            break

        if user_input.strip().lower() in ("exit", "q", ""):
            break

        print("\n--- 执行中 ---")
        result = agent_loop(user_input)

        # 输出中间工具调用信息
        if result.tool_calls:
            print("\n\033[33m【工具调用记录】\033[0m")
            for idx, tc in enumerate(result.tool_calls, 1):
                print(f"{idx}. $ {tc.command}")
                if tc.error:
                    print(f"   错误: {tc.error}")
                else:
                    # 只显示输出前200字符
                    output_preview = tc.output[:200] + (
                        "..." if len(tc.output) > 200 else ""
                    )
                    print(f"   输出: {output_preview}")

        # 输出最终答案
        if result.error:
            print(f"\033[31m【错误】\033[0m {result.error}")
        else:
            print("\n\033[32m【最终答案】\033[0m")
            print(result.final_answer)

        print("\n" + "-" * 50)

    print("退出。")


if __name__ == "__main__":
    main()
