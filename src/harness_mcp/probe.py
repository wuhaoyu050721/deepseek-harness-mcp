"""阶段 0 的确定性探针，仅验证 MCP stdio，不访问项目或模型。"""

import importlib.metadata
import platform

from mcp.server import MCPServer
from pydantic import BaseModel

server = MCPServer("deepseek-harness-phase0")


class ProbeResult(BaseModel):
    nonce: str
    stage: str
    mcp_version: str
    python_version: str
    model_called: bool
    harness_executed: bool
    business_tools_implemented: bool


@server.tool(structured_output=True)
def phase0_probe(nonce: str) -> ProbeResult:
    """原样返回短随机标记，供客户端核对真实工具调用；不提交 Harness 任务。"""
    if not 1 <= len(nonce) <= 128:
        raise ValueError("nonce 长度必须为 1 到 128")
    return ProbeResult(
        nonce=nonce,
        stage="phase0_protocol_probe",
        mcp_version=importlib.metadata.version("mcp"),
        python_version=platform.python_version(),
        model_called=False,
        harness_executed=False,
        business_tools_implemented=False,
    )


def main() -> None:
    server.run()


if __name__ == "__main__":
    main()

