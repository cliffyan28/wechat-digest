#!/usr/bin/env python3
"""
抓取微信公众号文章正文内容

用法：
  # 单篇文章
  python3 fetch-article.py "https://mp.weixin.qq.com/s?__biz=..."

  # 从 biz-articles.py 的 JSON 输出批量抓取
  python3 biz-articles.py "华尔街见闻" --since 2026-04-10 --format json | python3 fetch-article.py --stdin

  # 批量抓取，输出到目录
  python3 biz-articles.py "华尔街见闻" --limit 5 --format json | python3 fetch-article.py --stdin --outdir ./articles

  # 输出纯文本（默认 markdown）
  python3 fetch-article.py --format text "https://mp.weixin.qq.com/s?..."

原理：
  使用 Playwright (headless Chromium) 加载微信文章页面，绕过 JS 环境检测，
  提取 #js_content 中的文章正文、标题、作者、发布时间。
"""

import argparse
import json
import os
import re
import sys
import time

from playwright.sync_api import sync_playwright


def sanitize_filename(name):
    """清理文件名中的非法字符"""
    return re.sub(r'[\\/:*?"<>|]', '_', name).strip()[:80]


def fetch_article(page, url, retries=2):
    """抓取单篇文章，返回 dict 或 None"""
    for attempt in range(retries + 1):
        try:
            page.goto(url, wait_until='networkidle', timeout=30000)

            content_html = page.content()
            if '环境异常' in content_html:
                if attempt < retries:
                    time.sleep(2)
                    continue
                return {'error': '环境异常（验证拦截）', 'url': url}

            article_el = page.query_selector('#js_content')
            if not article_el:
                return {'error': '未找到文章内容（#js_content）', 'url': url}

            # 提取 metadata
            title_el = page.query_selector('#activity-name')
            title = title_el.inner_text().strip() if title_el else page.title()

            author_el = page.query_selector('#js_name')
            author = author_el.inner_text().strip() if author_el else ''

            pub_el = page.query_selector('#publish_time')
            pub_time = pub_el.inner_text().strip() if pub_el else ''

            text = article_el.inner_text().strip()

            return {
                'title': title,
                'author': author,
                'pub_time': pub_time,
                'url': url,
                'content': text,
            }

        except Exception as e:
            if attempt < retries:
                time.sleep(2)
                continue
            return {'error': str(e), 'url': url}

    return {'error': '重试耗尽', 'url': url}


def format_markdown(article):
    """将文章格式化为 markdown"""
    lines = []
    lines.append(f"# {article['title']}\n")
    meta = []
    if article.get('author'):
        meta.append(f"**作者**: {article['author']}")
    if article.get('pub_time'):
        meta.append(f"**发布时间**: {article['pub_time']}")
    if article.get('url'):
        meta.append(f"**原文链接**: {article['url']}")
    if meta:
        lines.append(' | '.join(meta))
        lines.append('')
    lines.append('---\n')
    lines.append(article['content'])
    lines.append('')
    return '\n'.join(lines)


def format_text(article):
    """将文章格式化为纯文本"""
    lines = []
    lines.append(article['title'])
    if article.get('author'):
        lines.append(f"作者: {article['author']}")
    if article.get('pub_time'):
        lines.append(f"发布时间: {article['pub_time']}")
    lines.append('')
    lines.append(article['content'])
    lines.append('')
    return '\n'.join(lines)


def main():
    parser = argparse.ArgumentParser(description='抓取微信公众号文章正文')
    parser.add_argument('urls', nargs='*', help='文章 URL（可多个）')
    parser.add_argument('--stdin', action='store_true',
                        help='从 stdin 读取 biz-articles.py 的 JSON 输出')
    parser.add_argument('--outdir', help='输出目录（每篇文章一个文件）')
    parser.add_argument('--format', choices=['md', 'text', 'json'],
                        default='md', help='输出格式（默认 md）')
    parser.add_argument('--delay', type=float, default=1.0,
                        help='批量抓取时每篇间隔秒数（默认 1.0）')
    args = parser.parse_args()

    # 收集所有要抓取的 URL
    urls = []
    if args.stdin:
        data = json.load(sys.stdin)
        for item in data:
            u = item.get('url', '')
            if u and u.startswith('http'):
                urls.append(u)
    urls.extend(args.urls)

    if not urls:
        parser.print_help()
        return

    if args.outdir:
        os.makedirs(args.outdir, exist_ok=True)

    results = []
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(
            user_agent='Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
                       'AppleWebKit/537.36 (KHTML, like Gecko) '
                       'Chrome/120.0.0.0 Safari/537.36'
        )

        for i, url in enumerate(urls):
            if i > 0:
                time.sleep(args.delay)

            print(f'[{i+1}/{len(urls)}] 正在抓取...', file=sys.stderr, end=' ')
            article = fetch_article(page, url)

            if 'error' in article:
                print(f'失败: {article["error"]}', file=sys.stderr)
                results.append(article)
                continue

            print(f'{article["title"]}（{len(article["content"])} 字）',
                  file=sys.stderr)
            results.append(article)

            # 输出到文件
            if args.outdir:
                date_prefix = ''
                if article.get('pub_time'):
                    m = re.search(r'(\d{4})年(\d+)月(\d+)日', article['pub_time'])
                    if m:
                        date_prefix = f'{m.group(1)}-{int(m.group(2)):02d}-{int(m.group(3)):02d}-'
                fname = sanitize_filename(date_prefix + article['title'])
                ext = '.md' if args.format == 'md' else '.txt'
                if args.format == 'json':
                    ext = '.json'
                fpath = os.path.join(args.outdir, fname + ext)
                with open(fpath, 'w') as f:
                    if args.format == 'json':
                        json.dump(article, f, ensure_ascii=False, indent=2)
                    elif args.format == 'text':
                        f.write(format_text(article))
                    else:
                        f.write(format_markdown(article))

        browser.close()

    # 非文件输出模式：输出到 stdout
    if not args.outdir:
        ok = [r for r in results if 'error' not in r]
        if args.format == 'json':
            print(json.dumps(ok, ensure_ascii=False, indent=2))
        else:
            for article in ok:
                if args.format == 'text':
                    print(format_text(article))
                else:
                    print(format_markdown(article))

    # 统计
    ok_count = sum(1 for r in results if 'error' not in r)
    fail_count = len(results) - ok_count
    print(f'\n完成: {ok_count} 篇成功, {fail_count} 篇失败', file=sys.stderr)


if __name__ == '__main__':
    main()
