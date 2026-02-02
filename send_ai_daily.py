#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AI 日报自动化系统
- 从 RSS 源抓取最近 48 小时内容
- 使用大模型 API 评分并生成日报（支持 OpenAI / 通义千问 / ARK）
- 发送到飞书群（自定义机器人 + 签名校验）
- 基于 sha256(link) 去重
"""

import os
import sys
import json
import hashlib
import hmac
import base64
import time
import logging
import concurrent.futures
from abc import ABC, abstractmethod
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Dict, Optional, Set

import requests
import feedparser
from dateutil import parser as date_parser
from dotenv import load_dotenv

# 设置 UTF-8 输出，避免 Windows 下 GBK 编码问题
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 加载 .env 文件（如果存在）
load_dotenv()

# ==================== 配置常量 ====================
# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 基础配置
MAX_CANDIDATES = 60
TOP_N = 3
HOURS_WINDOW = 48
RSS_TIMEOUT = 10
MAX_WORKERS = 5  # 并行抓取线程数

# 路径配置
SENT_HASHES_FILE = Path("data/sent_hashes.txt")

# API 配置
LLM_PROVIDER = os.getenv("LLM_PROVIDER", "openai").lower()
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
DASHSCOPE_API_KEY = os.getenv("DASHSCOPE_API_KEY", "")
QWEN_MODEL = os.getenv("QWEN_MODEL", "qwen-plus")
ARK_API_KEY = os.getenv("ARK_API_KEY", "")
ARK_MODEL = os.getenv("ARK_MODEL", "")

# 飞书配置
FEISHU_WEBHOOK_URL = os.getenv("FEISHU_WEBHOOK_URL", "")
FEISHU_SECRET = os.getenv("FEISHU_SECRET", "")

# RSS 配置
RSS_URLS_RAW = os.getenv("RSS_URLS", "")
RSS_URLS = [line.strip() for line in RSS_URLS_RAW.strip().split("\n") if line.strip()]

# ==================== Prompts ====================
SYSTEM_PROMPT_SCORE = """你是一名资深AI工程师和技术编辑，核心任务是从指定RSS条目中筛选出最值得企业内部AI团队关注的内容，**特别聚焦ERP系统重构与企业级应用落地**。

### 核心目标
精准筛选符合企业AI团队技术落地需求、业务优先级及技术栈适配性的内容，优先推荐能直接支撑企业级AI应用落地、ERP系统重构实践的优质信息。

### 执行规则
1. **筛选前提**：仅处理满足以下条件的RSS条目，不满足则直接排除：
   - 发布时间：近30天内（含当天）；
   - 来源类型：正规技术媒体（如TechCrunch、InfoQ）、权威厂商官网（如SAP、用友、金蝶、OpenAI、Google官方站点）、行业核心期刊（如《企业信息化》《中国金融科技》）；
   - 行业聚焦：优先覆盖制造、金融、零售三大主流企业服务行业，其他行业仅保留与ERP/企业级AI强相关的内容。
2. **评分标准（0~10分，优先级从高到低）**：
   - 9~10分：内容聚焦ERP系统重构（如SAP S/4HANA AI化改造、用友NC Cloud智能模块升级）、企业级财务软件AI实践（如金蝶云星空智能记账/预算预测），需包含具体技术细节（如采用的大模型微调方法、RAG架构设计）或落地案例（如某制造企业ERP重构后的效率提升数据）；
   - 7~9分：大模型/AI平台能力更新（如OpenAI GPT-4 Turbo企业级API新增功能、Google Gemini Enterprise适配ERP系统的接口优化），需明确对企业级应用的支撑价值（如降低ERP数据处理延迟30%）；
   - 5~7分：Agent/Tool/RAG/系统设计实践（如企业级AI Agent与ERP系统的集成方案、基于RAG的ERP知识问答工具开发），需包含可复用的技术框架或流程（如Agent调用ERP接口的授权机制）；
   - 3~5分：产品应用案例、评测（如某零售企业使用AI+ERP的案例报告、第三方机构对SAP AI模块的性能评测），需有真实数据支撑（如案例中库存周转率提升25%）；
   - 0~2分：泛泛而谈（无具体技术细节、无落地案例，仅空谈“AI赋能ERP”等概念）或营销软文（以产品推广为核心，无实质技术价值，如“某厂商新ERP系统全球首发，AI能力业界领先”但未说明具体功能）。
3. **冲突处理**：若条目同时符合多个评分标准，按最高分值对应的标准评分；若泛泛而谈与营销软文特征叠加，按0分处理。

### 输出要求
返回按评分从高到低排序的JSON数组，每个元素必须包含以下字段：
- "link"：RSS条目的原始链接（字符串，非空）；
- "score"：评分结果（数值，保留整数）；
- "reason"：评分理由（字符串，需明确标注内容核心价值点及匹配的评分标准项，示例：“内容介绍了某制造企业SAP S/4HANA重构中采用的RAG架构设计与库存预测落地案例，匹配9~10分评分标准”）。

### 注意事项
- 禁止遗漏符合筛选前提的有效条目；
- 禁止对不符合前提的条目进行评分；
- 评分理由需基于条目实际内容，不得虚构或夸大。
"""

SYSTEM_PROMPT_REPORT = """你是《AI前沿信息速递》内部战略洞察简报的智能编辑，负责为技术团队（关注技术细节、落地可行性）与产品团队（关注业务价值、痛点解决）提供高价值、可行动的创新探索思路，重点支撑我方处于需求调研阶段、存在数据孤岛与流程僵化核心痛点、基于Java+MySQL技术栈的ERP系统重构工作。

以下是你需要遵循的核心要求：
1. **输出形式**：以【模块化结构化报告】呈现，单条信息必须采用"核心结论+分维度分析"的固定格式。
2. **篇幅控制**：简报整体字数需严格控制在800-1200字之间。
3. **分析维度**：必须覆盖以下四个关键维度：
   - **信源类型**：识别原文属于官方博客、学术论文、技术社区还是营销通稿。
   - **ERP相关性**：按标准标记为🔴高（强相关erp、sap等财务系统的）、🟡中（直接涉及企业级复杂业务/数据处理/流程自动化或解决ERP痛点）、🔵低（通用技术可适配ERP场景）、⚪不相关（纯C端或娱乐向）
   - **实施方法**：明确原文指出的技术栈（如LangChain、OCI、SAP BTP）、架构模式或工程实践。
   - **探索方向**：针对我方ERP重构的具体建议；若存在以下情况需标记【需交叉验证】：①原文技术细节不足支撑ERP适配；②方案落地性存疑；③与现有Java+MySQL技术栈适配性不明确。交叉验证由技术团队牵头，联合产品团队通过文献调研、原型测试执行。
4. **内容规范**：语言必须精炼、专业，严格基于原文内容，不得虚构信息。

请严格按照上述要求生成模块化结构化报告，并将最终内容放置在<简报>标签内。"""


# ==================== LLM Client 抽象 ====================
class LLMClient(ABC):
    @abstractmethod
    def call_json(self, system_prompt: str, user_prompt: str) -> Dict:
        pass

class OpenAIClient(LLMClient):
    def __init__(self, api_key: str, model: str = "gpt-4o-2024-08-06"):
        self.api_key = api_key
        self.model = model
        self.url = "https://api.openai.com/v1/chat/completions"

    def call_json(self, system_prompt: str, user_prompt: str) -> Dict:
        if not self.api_key:
            logger.error("OpenAI API Key not configured")
            sys.exit(1)
            
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt}
            ],
            "response_format": {"type": "json_object"}
        }
        try:
            resp = requests.post(self.url, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception as e:
            logger.error(f"OpenAI API Call Failed: {e}")
            sys.exit(1)

class QwenClient(LLMClient):
    def __init__(self, api_key: str, model: str = "qwen-plus"):
        self.api_key = api_key
        self.model = model
        self.url = "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"

    def call_json(self, system_prompt: str, user_prompt: str) -> Dict:
        if not self.api_key:
            logger.error("DashScope API Key not configured")
            sys.exit(1)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        combined_prompt = f"{system_prompt}\n\n{user_prompt}"
        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": combined_prompt}],
            "response_format": {"type": "json_object"}
        }

        try:
            resp = requests.post(self.url, headers=headers, json=payload, timeout=60)
            resp.raise_for_status()
            data = resp.json()
            content = data["choices"][0]["message"]["content"]
            return json.loads(content)
        except Exception as e:
            logger.error(f"Qwen API Call Failed: {e}")
            if 'resp' in locals():
                logger.debug(f"Response content: {resp.text}")
            sys.exit(1)

class ArkClient(LLMClient):
    def __init__(self, api_key: str, model: str):
        self.api_key = api_key
        self.model = model
        self.url = "https://ark.cn-beijing.volces.com/api/v3/responses"

    def call_json(self, system_prompt: str, user_prompt: str) -> Dict:
        if not self.api_key or not self.model:
            logger.error("ARK API Key or Model not configured")
            sys.exit(1)

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        payload = {
            "model": self.model,
            "input": [
                {
                    "role": "system", 
                    "content": [{"type": "input_text", "text": system_prompt}]
                },
                {
                    "role": "user", 
                    "content": [{"type": "input_text", "text": user_prompt}]
                }
            ]
        }

        for attempt in range(3):
            try:
                resp = requests.post(self.url, headers=headers, json=payload, timeout=120)
                resp.raise_for_status()
                data = resp.json()
                
                content = ""
                for output_item in data.get("output", []):
                    if output_item.get("type") == "message" and output_item.get("role") == "assistant":
                        for content_item in output_item.get("content", []):
                            if content_item.get("type") == "output_text":
                                content = content_item.get("text", "")
                                break
                        if content:
                            break
                
                if not content:
                    logger.error("ARK API response contained no content field inside 'output'. Checking raw response...")
                    logger.debug(f"Raw Response: {resp.text}")
                    sys.exit(1)
                
                try:
                    # 清理可能存在的 XML 标签包裹
                    if "<简报>" in content:
                        content = content.replace("<简报>", "").replace("</简报>", "").strip()

                    return json.loads(content)
                except json.JSONDecodeError as e:
                    # 尝试修复常见的 markdown 代码块包裹问题
                    if "```json" in content:
                        content = content.split("```json")[1].split("```")[0].strip()
                        return json.loads(content)
                    elif "```" in content:
                        content = content.split("```")[1].split("```")[0].strip()
                        return json.loads(content)
                    else:
                        logger.error(f"JSON Decode Error. Content was: {content}")
                        raise e

            except (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.ChunkedEncodingError) as e:
                logger.warning(f"ARK API Call Failed (Attempt {attempt+1}/3): {e}")
                if attempt < 2:
                    time.sleep(2)
            except Exception as e:
                logger.error(f"ARK API Call Failed: {e}")
                if 'resp' in locals():
                    logger.debug(f"Response status: {resp.status_code}")
                    logger.debug(f"Response content: {resp.text}")
                sys.exit(1)
        
        logger.error("ARK API Call Failed after 3 attempts")
        sys.exit(1)

def get_llm_client() -> LLMClient:
    logger.info(f"Using LLM Provider: {LLM_PROVIDER}")
    if LLM_PROVIDER == "qwen":
        return QwenClient(DASHSCOPE_API_KEY, QWEN_MODEL)
    elif LLM_PROVIDER == "ark":
        return ArkClient(ARK_API_KEY, ARK_MODEL)
    else:
        return OpenAIClient(OPENAI_API_KEY)


# ==================== 工具函数 ====================
def load_sent_hashes() -> Set[str]:
    """加载已发送的 hash 集合"""
    if not SENT_HASHES_FILE.exists():
        SENT_HASHES_FILE.parent.mkdir(parents=True, exist_ok=True)
        SENT_HASHES_FILE.touch()
        return set()
    with open(SENT_HASHES_FILE, "r", encoding="utf-8") as f:
        return set(line.strip() for line in f if line.strip())

def save_sent_hashes(hashes: Set[str]):
    """保存已发送的 hash 集合"""
    SENT_HASHES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(SENT_HASHES_FILE, "w", encoding="utf-8") as f:
        for h in sorted(hashes):
            f.write(h + "\n")

def hash_link(link: str) -> str:
    return hashlib.sha256(link.encode("utf-8")).hexdigest()

def is_recent(published_str: str, hours: int = HOURS_WINDOW) -> bool:
    try:
        pub_time = date_parser.parse(published_str)
        if pub_time.tzinfo is None:
            pub_time = pub_time.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        return (now - pub_time) <= timedelta(hours=hours)
    except Exception:
        return False

# ==================== RSS 抓取 ====================
def fetch_single_feed(url: str, sent_hashes: Set[str]) -> List[Dict]:
    """抓取单个 RSS 源并过滤"""
    candidates = []
    try:
        logger.info(f"抓取 RSS: {url}")
        response = requests.get(url, timeout=RSS_TIMEOUT)
        response.raise_for_status()
        feed = feedparser.parse(response.content)
        
        for entry in feed.entries:
            link = entry.get("link", "")
            if not link: 
                continue
                
            link_hash = hash_link(link)
            if link_hash in sent_hashes:
                continue
                
            published = entry.get("published", entry.get("updated", ""))
            if not is_recent(published, HOURS_WINDOW):
                continue
                
            title = entry.get("title", "")
            summary = entry.get("summary", entry.get("description", ""))
            if len(summary) > 500:
                summary = summary[:500] + "..."
            
            candidates.append({
                "title": title,
                "link": link,
                "summary": summary,
                "published": published,
                "hash": link_hash
            })
    except Exception as e:
        logger.warning(f"抓取 {url} 失败: {e}")
    
    return candidates

def fetch_rss_entries() -> List[Dict]:
    """并发抓取所有 RSS 源"""
    if not RSS_URLS:
        logger.warning("RSS_URLS 为空")
        return []

    sent_hashes = load_sent_hashes()
    logger.info(f"配置的 RSS 源数量: {len(RSS_URLS)}")
    
    all_candidates = []
    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = {executor.submit(fetch_single_feed, url, sent_hashes): url for url in RSS_URLS}
        for future in concurrent.futures.as_completed(futures):
            try:
                candidates = future.result()
                all_candidates.extend(candidates)
                if len(all_candidates) >= MAX_CANDIDATES * 2: # 稍微多抓一点也无妨，最后再截断
                     # 这里其实无法立刻停止其他线程，但可以提前break loop如果需要
                     pass
            except Exception as e:
                logger.error(f"线程执行异常: {e}")

    # 全局截断
    if len(all_candidates) > MAX_CANDIDATES:
        logger.info(f"截断候选集: {len(all_candidates)} -> {MAX_CANDIDATES}")
        all_candidates = all_candidates[:MAX_CANDIDATES]
        
    logger.info(f"共收集 {len(all_candidates)} 条候选")
    return all_candidates

# ==================== 评分阶段 ====================
def compact_for_scoring(entries: List[Dict]) -> List[Dict]:
    compact = []
    for e in entries:
        snippet = (e.get("summary") or "").strip()
        if len(snippet) > 160:
            snippet = snippet[:160] + "..."
        compact.append({
            "title": (e.get("title") or "")[:120],
            "link": e.get("link"),
            "published": e.get("published", ""),
            "snippet": snippet
        })
    return compact

def score_entries(llm_client: LLMClient, entries: List[Dict]) -> List[Dict]:
    if not entries:
        return []

    user_prompt = f"""请对以下 {len(entries)} 条 RSS 条目打分：

{json.dumps(compact_for_scoring(entries), ensure_ascii=False, indent=2)}

返回格式：
{{
  "scores": [
    {{"link": "...", "score": 8.5, "reason": "..."}},
    ...
  ]
}}"""

    result = llm_client.call_json(SYSTEM_PROMPT_SCORE, user_prompt)
    scores = result.get("scores", [])
    
    # 排序并取 Top N
    scores.sort(key=lambda x: x.get("score", 0), reverse=True)
    top_scores = scores[:TOP_N]

    # 补充完整信息
    link_map = {e["link"]: e for e in entries}
    top_entries = []
    for s in top_scores:
        link = s["link"]
        if link in link_map:
            entry = link_map[link].copy()
            entry["score"] = s["score"]
            entry["score_reason"] = s["reason"]
            top_entries.append(entry)

    logger.info(f"评分完成，Top {TOP_N}: {len(top_entries)} 条")
    return top_entries

# ==================== 日报生成阶段 ====================
def generate_daily_report(llm_client: LLMClient, top_entries: List[Dict]) -> Dict:
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    
    user_prompt = f"""基于以下 Top 3 RSS 条目，生成一张"终版 AI 日报卡片"的 JSON（中文），结构必须完全符合下面的 JSON 契约。

【Top 3 条目】
{json.dumps(top_entries, ensure_ascii=False, indent=2)}

【JSON 契约】
{{
  "date": "{today}",
  "theme": "今日主题（15字以内）",
  "items": [
    {{
      "title": "条目标题",
      "publish_date": "YYYY-MM-DD",
      "source_type": "技术博客/论文/...",
      "source_name": "OpenAI/Google/...",
      "erp_relevance": "🔴 高 / 🟡 中 / 🔵 低",
      "summary": "核心摘要（明确点出所属方向，如“属原生AI实践”等）",
      "key_facts": "关键事实（数据或结论）",
      "implementation_method": "实施方法（注明技术栈）",
      "exploration_direction": "面向我方ERP重构的具体建议",
      "link": "原文链接"
    }}
  ]
}}

【硬约束】
- items 数组必须包含所有 3 条输入内容（如果不足3条则全部包含）。
- summary 需简练，highlight ERP relevance.
- implementation_method 必须识别出具体的 tool/library/framework，如果没有则写“通用大模型能力”。
- exploration_direction 必须具体。"""

    report = llm_client.call_json(SYSTEM_PROMPT_REPORT, user_prompt)
    return validate_and_fix_report(report)

def validate_and_fix_report(report: Dict) -> Dict:
    """校验并修复日报 JSON 结构"""
    today = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d")
    
    if "date" not in report: report["date"] = today
    if "theme" not in report: report["theme"] = "AI 技术动态"
    if "items" not in report or not isinstance(report["items"], list): report["items"] = []
    
    # 简单的字段补全
    for item in report["items"]:
        if "title" not in item: item["title"] = "未知标题"
        if "source_type" not in item: item["source_type"] = "未知"
        if "erp_relevance" not in item: item["erp_relevance"] = "🔵 低"
        if "summary" not in item: item["summary"] = "暂无摘要"
    
    logger.info("日报结构校验完成")
    return report

# ==================== 飞书推送 ====================
def send_to_feishu(report: Dict):
    if not FEISHU_WEBHOOK_URL:
        logger.warning("未配置 FEISHU_WEBHOOK_URL，跳过发送")
        return

    timestamp = str(int(time.time()))
    sign = ""
    if FEISHU_SECRET:
        string_to_sign = f"{timestamp}\n{FEISHU_SECRET}"
        hmac_code = hmac.new(string_to_sign.encode("utf-8"), digestmod=hashlib.sha256).digest()
        sign = base64.b64encode(hmac_code).decode("utf-8")

    # 构造卡片元素
    elements = [
        {"tag": "div", "text": {"tag": "lark_md", "content": f"**📌 今日主题：{report.get('theme')}**"}}
    ]

    items = report.get("items", [])
    for idx, item in enumerate(items, 1):
        elements.append({"tag": "hr"})
        
        # 构造单条内容的 markdown
        content_md = f"**标题：{idx}. [{item.get('title')}]({item.get('link')})**\n"
        content_md += f"**发布日期：** {item.get('publish_date')} | **信源类型：** {item.get('source_type')}（{item.get('source_name')}） | **ERP相关性：** {item.get('erp_relevance')}\n"
        content_md += f"**核心摘要：** {item.get('summary')}\n\n"
        content_md += "**核心洞察：**\n"
        content_md += f"🔹 **关键事实：** {item.get('key_facts')}\n"
        content_md += f"🔹 **实施方法：** {item.get('implementation_method')}\n"
        content_md += f"🔹 **探索方向：** {item.get('exploration_direction')}\n\n"
        content_md += f"**原文链接：** {item.get('link')}"

        elements.append({
            "tag": "div",
            "text": {
                "tag": "lark_md",
                "content": content_md
            }
        })

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "title": {"tag": "plain_text", "content": f"📰 AI 日报 | {report.get('date')}"},
            "template": "blue"
        },
        "elements": elements
    }

    payload = {
        "timestamp": timestamp,
        "sign": sign,
        "msg_type": "interactive",
        "card": card
    }

    for attempt in range(3):
        try:
            resp = requests.post(FEISHU_WEBHOOK_URL, json=payload, timeout=10)
            resp.raise_for_status()
            res_json = resp.json()
            if res_json.get("code") == 0:
                logger.info("飞书推送成功")
                return
            logger.warning(f"飞书推送失败: {res_json}")
        except Exception as e:
            logger.warning(f"飞书推送尝试 {attempt+1} 失败: {e}")
            if attempt < 2:
                time.sleep(2 ** attempt)
    logger.error("飞书推送最终失败")

# ==================== 主流程 ====================
def main():
    logger.info("开始执行 AI 日报任务")
    
    # 0. 初始化 LLM 客户端
    llm_client = get_llm_client()

    # 1. 抓取 RSS
    candidates = fetch_rss_entries()
    if not candidates:
        logger.info("无新内容，退出")
        return

    # 2. 评分
    top_entries = score_entries(llm_client, candidates)
    if not top_entries:
        logger.info("无高分内容，退出")
        return

    # 3. 生成日报
    report = generate_daily_report(llm_client, top_entries)
    logger.info("日报生成完成")
    print(json.dumps(report, ensure_ascii=False, indent=2))

    # 4. 发送飞书
    send_to_feishu(report)

    # 5. 更新去重文件
    sent_hashes = load_sent_hashes()
    new_hashes = {e["hash"] for e in top_entries}
    sent_hashes.update(new_hashes)
    save_sent_hashes(sent_hashes)
    logger.info(f"已更新去重文件，新增 {len(new_hashes)} 条")

if __name__ == "__main__":
    main()
