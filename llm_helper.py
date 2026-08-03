#!/usr/bin/env python3
"""调用 Ollama 兼容接口，为任务生成多条不重复的每日工作描述。

接口地址和密钥不写死在代码里，通过环境变量自行配置：
  OLLAMA_URL       生成接口地址，例如 https://your-ollama-host:port/api/generate
  OLLAMA_API_KEY   接口的 Bearer token
  OLLAMA_MODEL     模型名（可选，默认 qwen3.6:35b-a3b）

不配置也能正常跑：会跳过大模型调用，直接用模板生成的兜底描述，不会报错
卡住整个流程。
"""

import os
import re

import requests

API_KEY = os.getenv("OLLAMA_API_KEY", "")
URL = os.getenv("OLLAMA_URL", "")
MODEL = os.getenv("OLLAMA_MODEL", "qwen3.6:35b-a3b")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Content-Type": "application/json",
}


def _llm_configured() -> bool:
    return bool(URL and API_KEY)


def _call_llm(prompt: str) -> str:
    resp = requests.post(
        URL,
        headers=HEADERS,
        json={"model": MODEL, "prompt": prompt, "stream": False, "think": False},
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json().get("response", "").strip()


def _clean_lines(text: str) -> list[str]:
    lines = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        # 去掉常见的编号前缀，如 "1. " "1、" "- " "* "
        line = re.sub(r"^[\d一二三四五六七八九十]+[.、\)\s]*", "", line)
        line = line.lstrip("-*• ").strip()
        if line:
            lines.append(line)
    return lines


def generate_daily_descriptions(task_name: str, n: int) -> list[str]:
    """为任务生成 n 条互不相同的一句话工作描述，按开发流程不同阶段递进。
    没配置 OLLAMA_URL/OLLAMA_API_KEY，或者请求失败，都会直接用模板兜底，
    不会抛异常打断整个填报流程。"""
    if n <= 0:
        return []

    results: list[str] = []

    if not _llm_configured():
        print("    [大模型] 未配置 OLLAMA_URL / OLLAMA_API_KEY，跳过大模型调用，使用模板描述", flush=True)
    else:
        attempts = 0
        while len(results) < n and attempts < 4:
            attempts += 1
            remaining = n - len(results)
            avoid = ""
            if results:
                avoid_list = "\n".join(f"- {d}" for d in results)
                avoid = f"\n以下描述已经用过，不要重复或过于相似：\n{avoid_list}\n"

            prompt = (
                f"你是一名研发工程师，正在为任务《{task_name}》填写连续多天的每日工作进展描述。\n"
                f"请生成 {remaining} 条不同的中文工作描述，每条一句话（20~40字），语气自然、具体，"
                f"像真实的研发日报，内容围绕需求分析、方案设计、编码实现、联调、测试、修复问题、"
                f"文档整理等不同阶段展开，条目之间不要重复或雷同。{avoid}\n"
                f"严格按下面格式输出，每条一行，不要加编号、不要加多余说明：\n"
            )

            print(f"    [大模型] 第 {attempts} 次请求，还差 {remaining} 条...", flush=True)
            try:
                text = _call_llm(prompt)
            except Exception as e:
                print(f"    [大模型] 请求失败（{e}），改用模板兜底", flush=True)
                break
            for line in _clean_lines(text):
                if line not in results:
                    results.append(line)
                if len(results) >= n:
                    break
            print(f"    [大模型] 已生成 {len(results)}/{n} 条", flush=True)

    if len(results) < n:
        # 兜底：不足的部分用简单模板补齐，保证流程不中断
        for i in range(len(results), n):
            results.append(f"继续推进《{task_name}》相关工作，处理第 {i + 1} 阶段的开发与验证。")

    return results[:n]


if __name__ == "__main__":
    for d in generate_daily_descriptions("统一检索功能优化", 3):
        print("-", d)
