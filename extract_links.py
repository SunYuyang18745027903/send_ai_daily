#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
提取指定网站中的原文链接中的超链接
"""

import os
import sys
import logging
import requests
from bs4 import BeautifulSoup

# 设置 UTF-8 输出，避免 Windows 下 GBK 编码问题
if sys.platform == 'win32':
    import io
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding='utf-8')

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

# 固定基础 URL
BASE_URL = "http://100.68.66.102:18001/views/article/"

def extract_links_from_url(url):
    """
    从指定 URL 提取超链接
    """
    try:
        logger.info(f"访问 URL: {url}")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        # 解析 HTML
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 提取所有超链接
        links = []
        for a_tag in soup.find_all('a', href=True):
            href = a_tag.get('href')
            text = a_tag.get_text(strip=True)
            links.append({
                'href': href,
                'text': text
            })
        
        logger.info(f"从 {url} 提取到 {len(links)} 个超链接")
        return links
    except Exception as e:
        logger.error(f"访问 {url} 失败: {e}")
        return []


def find_original_link(url):
    """
    从指定 URL 中查找原文链接
    """
    try:
        logger.info(f"查找原文链接: {url}")
        response = requests.get(url, timeout=10)
        response.raise_for_status()
        
        # 解析 HTML
        soup = BeautifulSoup(response.content, 'html.parser')
        
        # 精确定位包含"🔗 原文链接"文本的a标签
        original_link = None
        for a_tag in soup.find_all('a', href=True):
            text = a_tag.get_text(strip=True)
            if '🔗 原文链接' == text:
                href = a_tag.get('href')
                # 处理href中可能的反引号包围
                if href.startswith('`') and href.endswith('`'):
                    href = href.strip('`')
                original_link = href
                break
        
        if original_link:
            logger.info(f"找到原文链接: {original_link}")
            return original_link
        else:
            logger.warning("未找到原文链接")
            return None
    except Exception as e:
        logger.error(f"查找原文链接失败: {e}")
        return None


def get_original_link(input_str):
    """
    根据输入字符串抓取原文链接
    
    Args:
        input_str (str): 输入字符串，例如 "3271041950-2652670441_2" 或 "rss/feed/3271041950-2652670441_2"
    
    Returns:
        str: 抓取到的原文链接，例如 "https://mp.weixin.qq.com/s/N1PQuc2P1ycI575EiNmuyg"，
             如果未找到则返回空字符串
    """
    # 截取字符串，获取文章 ID
    # 例如从 "rss/feed/3271041950-2652670441_2" 中提取 "3271041950-2652670441_2"
    article_id = input_str.split('/')[-1]
    logger.info(f"开始处理输入字符串: {input_str}")
    logger.info(f"提取到的文章 ID: {article_id}")
    
    # 拼接完整 URL
    url = BASE_URL + article_id
    
    # 查找原文链接
    original_link = find_original_link(url)
    
    if original_link:
        logger.info(f"成功抓取到原文链接: {original_link}")
        return original_link
    else:
        logger.warning(f"未找到原文链接: {url}")
        return ""


if __name__ == "__main__":
    # 当直接运行此脚本时，执行简单的导入检查
    logger.info("extract_links.py 模块加载成功，可通过 import extract_links 使用其中的函数")
    logger.info("主要函数: get_original_link(input_str)")
    logger.info("示例用法: extract_links.get_original_link('rss/feed/3271041950-2652670441_2')")
