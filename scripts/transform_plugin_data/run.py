#!/usr/bin/env python3

import base64
import glob
import json
import os
import random
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple


REPO_URL_RE = re.compile(r"^https://github\.com/([^/]+)/([^/]+?)(?:\.git)?/?$")
VERSION_RE = re.compile(r"^\s*version\s*:\s*['\"]?([^'\"\r\n#]+)", re.MULTILINE)

MAX_RETRIES = int(os.getenv("MAX_RETRIES", "5"))
BASE_DELAY = float(os.getenv("BASE_DELAY", "2"))
MAX_DELAY = float(os.getenv("MAX_DELAY", "30"))
MAX_WORKERS = int(os.getenv("MAX_WORKERS", "12"))

PAT_TOKEN = os.getenv("PAT_TOKEN", "").strip()
GITHUB_URL = "https://raw.githubusercontent.com/AstrBotDevs/AstrBot_Plugins_Collection/main/plugins.json"


def run_cmd(args: list[str], check: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(args, check=check, text=True, capture_output=True)


def load_json(path: str, fallback: Any) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return fallback


def save_json(path: str, data: Any, pretty: bool = False) -> None:
    with open(path, "w", encoding="utf-8") as f:
        if pretty:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.write("\n")
        else:
            json.dump(data, f, ensure_ascii=False, separators=(",", ":"))


def get_headers(accept: str = "application/vnd.github+json") -> Dict[str, str]:
    headers = {
        "Accept": accept,
        "User-Agent": "GitHub-Action-Plugin-Transformer",
    }
    if PAT_TOKEN:
        headers["Authorization"] = f"token {PAT_TOKEN}"
    return headers


def http_get_json(url: str, timeout: int = 20) -> Tuple[Optional[Dict[str, Any]], int]:
    req = urllib.request.Request(url, headers=get_headers())
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
            if not body:
                return {}, status
            return json.loads(body), status
    except urllib.error.HTTPError as e:
        status = e.code
        try:
            body = e.read().decode("utf-8", errors="replace")
            return (json.loads(body) if body else {}), status
        except Exception:
            return {}, status
    except Exception:
        return None, -1


def configure_git() -> None:
    run_cmd(["git", "config", "--local", "user.email", "action@github.com"])
    run_cmd(["git", "config", "--local", "user.name", "GitHub Action"])
    print("✅ Git 配置完成", flush=True)


def fetch_original_plugin_data() -> Tuple[bool, Dict[str, Any]]:
    print("开始获取原始插件数据...", flush=True)
    req = urllib.request.Request(
        GITHUB_URL,
        headers=get_headers(accept="application/json"),
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = resp.getcode()
            body = resp.read().decode("utf-8", errors="replace")
    except Exception as e:
        print(f"❌ 网络请求失败: {e}", flush=True)
        return False, {}

    print(f"HTTP状态码: {status}", flush=True)
    if status != 200:
        print(f"❌ 最终返回非200状态码: {status}", flush=True)
        return False, {}
    if not body.strip():
        print("❌ 获取到的响应为空，跳过更新", flush=True)
        return False, {}
    if len(body.encode("utf-8")) < 50:
        print(f"❌ 响应内容过小 ({len(body.encode('utf-8'))} 字节)，可能是错误响应", flush=True)
        return False, {}
    try:
        data = json.loads(body)
    except json.JSONDecodeError:
        print("❌ 响应不是有效的JSON格式，跳过更新", flush=True)
        return False, {}
    if data in ({}, [], None):
        print("❌ 获取到空的JSON数据，跳过更新", flush=True)
        return False, {}
    if not isinstance(data, dict):
        print("❌ JSON结构不是对象，跳过更新", flush=True)
        return False, {}

    save_json("original_plugins.json", data, pretty=False)
    print(f"✅ 成功获取原始插件数据 ({len(body.encode('utf-8'))} 字节)", flush=True)
    return True, data


def load_existing_cache() -> Tuple[bool, Dict[str, Any]]:
    print("检查现有缓存文件...", flush=True)
    if os.path.exists("plugin_cache_original.json"):
        cache = load_json("plugin_cache_original.json", {})
        save_json("existing_cache.json", cache, pretty=False)
        print("发现现有缓存文件，将用作回退数据", flush=True)
        return True, cache
    print("没有现有缓存文件", flush=True)
    return False, {}


def normalize_cache(cache_raw: Dict[str, Any]) -> Dict[str, Any]:
    if not isinstance(cache_raw, dict):
        return {}
    if isinstance(cache_raw.get("data"), dict):
        return cache_raw["data"]
    return cache_raw


def build_cache_by_repo(cache_data: Dict[str, Any]) -> Dict[str, Dict[str, Any]]:
    result: Dict[str, Dict[str, Any]] = {}
    for value in cache_data.values():
        if not isinstance(value, dict):
            continue
        repo_url = value.get("repo")
        if not isinstance(repo_url, str) or not repo_url:
            continue
        result[repo_url] = {
            "stars": int(value.get("stars") or 0),
            "updated_at": value.get("updated_at") or "",
            "version": value.get("version") or "",
            "logo": value.get("logo") or "",
        }
    return result


def fetch_repo(owner: str, repo: str) -> Tuple[Optional[Dict[str, Any]], str]:
    url = f"https://api.github.com/repos/{owner}/{repo}"
    for attempt in range(1, MAX_RETRIES + 1):
        if attempt > 1:
            delay = min(BASE_DELAY * (2 ** (attempt - 2)), MAX_DELAY)
            delay += random.uniform(0, delay * 0.5)
            print(f"    第 {attempt} 次尝试 (延迟 {delay:.1f}s)...", flush=True)
            time.sleep(delay)

        payload, status = http_get_json(url, timeout=20 if attempt > 1 else 15)
        if payload is None and status == -1:
            pass
        elif status == 200 and isinstance(payload, dict) and "stargazers_count" in payload:
            return payload, "success"
        elif status in (301, 302):
            return payload if isinstance(payload, dict) else {}, "redirected"
        elif status == 404:
            return payload if isinstance(payload, dict) else {}, "deleted"
        elif status == 403:
            return payload if isinstance(payload, dict) else {}, "api_limit"
        elif status in (429, 502, 503, 504):
            print(f"    临时错误 HTTP {status}，准备重试", flush=True)
        else:
            if status > 0:
                print(f"    未知HTTP状态码: {status}", flush=True)

        if attempt < MAX_RETRIES:
            print(f"  尝试 {attempt}/{MAX_RETRIES} 失败，准备重试...", flush=True)

    return None, "network_error"


def extract_version(owner: str, repo: str) -> str:
    for metadata_file in ("metadata.yml", "metadata.yaml"):
        url = f"https://api.github.com/repos/{owner}/{repo}/contents/{metadata_file}"
        payload, status = http_get_json(url, timeout=10)
        if status != 200 or not isinstance(payload, dict):
            continue
        content = payload.get("content")
        if not isinstance(content, str) or not content:
            continue
        try:
            metadata_text = base64.b64decode(content).decode("utf-8", errors="replace")
        except Exception:
            continue
        match = VERSION_RE.search(metadata_text)
        if match:
            return match.group(1).strip()
    return ""


def extract_logo(owner: str, repo: str, default_branch: str) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/contents/logo.png"
    payload, status = http_get_json(url, timeout=10)
    if status == 200 and isinstance(payload, dict) and payload.get("name") == "logo.png":
        return f"https://raw.githubusercontent.com/{owner}/{repo}/{default_branch}/logo.png"
    return ""


def process_repo(repo_url: str, cache_by_repo: Dict[str, Dict[str, Any]], has_existing_cache: bool) -> Tuple[str, Dict[str, Any]]:
    stars = 0
    updated_at = ""
    version = ""
    logo = ""
    status = "unknown"

    match = REPO_URL_RE.match(repo_url)
    if not match:
        return repo_url, {
            "stars": stars,
            "updated_at": updated_at,
            "version": version,
            "logo": logo,
            "status": "invalid_repo_url",
        }

    owner, repo = match.group(1), match.group(2)
    print(f"获取仓库信息: {owner}/{repo}", flush=True)
    print("  初次尝试...", flush=True)
    repo_payload, status = fetch_repo(owner, repo)

    if status == "success" and isinstance(repo_payload, dict):
        stars = int(repo_payload.get("stargazers_count") or 0)
        updated_at = repo_payload.get("updated_at") or ""
        default_branch = repo_payload.get("default_branch") or "main"
        version = extract_version(owner, repo)
        logo = extract_logo(owner, repo, default_branch)
        print(f"  ✅ 成功 - Stars: {stars}, 更新时间: {updated_at}", flush=True)
        if logo:
            print(f"  🖼️  找到logo: {logo}", flush=True)
    else:
        if has_existing_cache:
            cached = cache_by_repo.get(repo_url)
            if cached and (cached.get("stars", 0) != 0 or cached.get("updated_at", "") != ""):
                stars = int(cached.get("stars", 0))
                updated_at = str(cached.get("updated_at", ""))
                version = str(cached.get("version", ""))
                logo = str(cached.get("logo", ""))
                status = "cached"
                print(f"  🔄 使用缓存数据: Stars: {stars}", flush=True)

    if status == "redirected":
        print("  🔄 仓库重定向", flush=True)
    elif status == "deleted":
        print("  🗑️  仓库已删除或不可访问 (404)", flush=True)
    elif status == "api_limit":
        print("  ⚠️  API限制或访问被拒绝 (403)", flush=True)
    elif status == "network_error":
        print("  ❌ 所有重试均失败", flush=True)

    return repo_url, {
        "stars": stars,
        "updated_at": updated_at,
        "version": version,
        "logo": logo,
        "status": status,
    }


def get_repo_info(original_plugins: Dict[str, Any], existing_cache: Dict[str, Any], has_existing_cache: bool) -> Dict[str, Dict[str, Any]]:
    print("开始获取仓库信息...", flush=True)
    repo_urls: list[str] = []
    seen = set()
    for plugin in original_plugins.values():
        if not isinstance(plugin, dict):
            continue
        repo_url = plugin.get("repo")
        if isinstance(repo_url, str) and repo_url and repo_url not in seen:
            seen.add(repo_url)
            repo_urls.append(repo_url)

    cache_by_repo = build_cache_by_repo(normalize_cache(existing_cache))
    repo_info: Dict[str, Dict[str, Any]] = {}
    counters: Dict[str, int] = {
        "success": 0,
        "cached": 0,
        "redirected": 0,
        "deleted": 0,
        "api_limit": 0,
        "network_error": 0,
        "invalid_repo_url": 0,
        "unknown": 0,
    }

    worker_count = min(MAX_WORKERS, max(1, len(repo_urls)))
    with ThreadPoolExecutor(max_workers=worker_count) as executor:
        futures = [executor.submit(process_repo, repo_url, cache_by_repo, has_existing_cache) for repo_url in repo_urls]
        for future in as_completed(futures):
            repo_url, info = future.result()
            repo_info[repo_url] = info
            status = str(info.get("status", "unknown"))
            counters[status] = counters.get(status, 0) + 1

    if repo_urls:
        success_rate = int((counters.get("success", 0) * 100) / len(repo_urls))
        print(f"📈 成功率: {success_rate}%", flush=True)
        if success_rate < 50:
            print("⚠️  警告: 成功率过低，可能存在网络问题或GitHub服务异常", flush=True)
            if has_existing_cache:
                print("已启用缓存回退机制", flush=True)

    save_json("repo_info.json", repo_info, pretty=False)
    print("✅ 仓库信息获取完成", flush=True)
    return repo_info


def transform_plugin_data(original_plugins: Dict[str, Any], repo_info: Dict[str, Any], existing_cache_raw: Dict[str, Any]) -> Dict[str, Any]:
    print("开始转换插件数据格式...", flush=True)
    cache = normalize_cache(existing_cache_raw)
    result: Dict[str, Any] = {}

    removed_deleted: list[str] = []
    failed_or_other: list[str] = []
    redirected: list[str] = []

    for key, plugin in original_plugins.items():
        if not isinstance(plugin, dict):
            continue
        repo_url = plugin.get("repo", "")
        repo_entry = repo_info.get(repo_url)
        cache_entry = cache.get(key, {}) if isinstance(cache.get(key, {}), dict) else {}
        repo_status = repo_entry.get("status", "") if isinstance(repo_entry, dict) else ""

        if repo_entry and (
            repo_status == "deleted" or (repo_status != "success" and not cache_entry)
        ):
            if repo_status == "deleted":
                removed_deleted.append(key)
            else:
                failed_or_other.append(f"{key} ({repo_status})")
            continue

        repo_version = (repo_entry.get("version") if isinstance(repo_entry, dict) else "") or ""
        cache_version = cache_entry.get("version", "") or ""
        repo_stars = repo_entry.get("stars") if isinstance(repo_entry, dict) else None
        cache_stars = cache_entry.get("stars", 0) or 0
        repo_updated = (repo_entry.get("updated_at") if isinstance(repo_entry, dict) else "") or ""
        cache_updated = cache_entry.get("updated_at", "") or ""
        repo_logo = (repo_entry.get("logo") if isinstance(repo_entry, dict) else "") or ""
        cache_logo = cache_entry.get("logo", "") or ""

        final_version = repo_version or cache_version or "1.0.0"
        final_stars = int(repo_stars) if repo_status == "success" and repo_stars is not None else int(cache_stars)
        final_updated = repo_updated or cache_updated or ""
        final_logo = repo_logo or cache_logo or ""

        new_plugin = dict(plugin)
        new_plugin["desc"] = plugin.get("desc")
        new_plugin["author"] = plugin.get("author")
        new_plugin["repo"] = plugin.get("repo")
        new_plugin["tags"] = plugin.get("tags", [])
        new_plugin["stars"] = final_stars
        new_plugin["version"] = final_version
        if "social_link" in plugin:
            new_plugin["social_link"] = plugin.get("social_link")
        if final_updated:
            new_plugin["updated_at"] = final_updated
        else:
            new_plugin.pop("updated_at", None)
        if final_logo:
            new_plugin["logo"] = final_logo
        else:
            new_plugin.pop("logo", None)

        result[key] = new_plugin

        if repo_status == "redirected":
            redirected.append(key)
        elif repo_status and repo_status not in ("success", "cached", "deleted"):
            failed_or_other.append(f"{key} ({repo_status})")

    save_json("temp_plugin_cache_original.json", result, pretty=False)
    save_json("plugin_cache_original.json", result, pretty=True)

    original_count = len(original_plugins)
    new_count = len(result)
    removed_count = original_count - new_count

    success_repos = sum(1 for v in repo_info.values() if v.get("status") == "success")
    cached_repos = sum(1 for v in repo_info.values() if v.get("status") == "cached")
    redirected_repos = sum(1 for v in repo_info.values() if v.get("status") == "redirected")
    deleted_repos = sum(1 for v in repo_info.values() if v.get("status") == "deleted")
    failed_repos = sum(
        1
        for v in repo_info.values()
        if v.get("status") not in ("success", "cached", "redirected", "deleted")
    )

    print("✅ 数据转换完成", flush=True)
    print("", flush=True)
    print("📊 转换统计:", flush=True)
    print(f"  插件数量变化: {original_count} -> {new_count}", flush=True)
    if removed_count > 0:
        print(f"  🗑️  已移除: {removed_count} 个失效插件", flush=True)
    print(f"  ✅ 实时数据: {success_repos} 个仓库", flush=True)
    print(f"  🔄 缓存数据: {cached_repos} 个仓库", flush=True)
    print(f"  🔄 重定向: {redirected_repos} 个仓库", flush=True)
    print(f"  🗑️  已删除(已移除): {deleted_repos} 个仓库", flush=True)
    print(f"  ❌ 网络错误(已保留): {failed_repos} 个仓库", flush=True)

    if removed_deleted:
        print("", flush=True)
        print("🗑️  以下仓库已从缓存中移除:", flush=True)
        for item in removed_deleted:
            print(f"  - {item} (404 Not Found)", flush=True)

    if failed_or_other:
        print("", flush=True)
        print("❌ 网络错误的仓库（已保留，使用缓存数据）:", flush=True)
        for item in failed_or_other:
            print(f"  - {item}", flush=True)

    if redirected:
        print("", flush=True)
        print("🔄 发生重定向的仓库列表（已保留）:", flush=True)
        for item in redirected:
            print(f"  - {item}", flush=True)

    return result


def pull_latest_changes_before_checking() -> None:
    run_cmd(["git", "fetch", "origin", "main", "--depth=1"])
    current_branch = run_cmd(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()

    if current_branch == "HEAD":
        run_cmd(["git", "checkout", "-B", "main", "origin/main"])
    else:
        checkout_main = subprocess.run(["git", "checkout", "main"], text=True, capture_output=True)
        if checkout_main.returncode != 0:
            run_cmd(["git", "checkout", "-b", "main", "origin/main"])

    pull = subprocess.run(["git", "pull", "--rebase", "--autostash", "origin", "main"], text=True, capture_output=True)
    if pull.returncode == 0:
        print("✅ pull --rebase --autostash 成功", flush=True)
    else:
        subprocess.run(["git", "rebase", "--abort"], text=True, capture_output=True)
        raise RuntimeError("❌ pull --rebase --autostash 失败，工作流将退出以便人工检查")


def check_for_changes() -> bool:
    print("检查文件状态...", flush=True)

    remote_ls = run_cmd(["git", "ls-tree", "--name-only", "-r", "origin/main", "--", "plugin_cache_original.json"], check=False)
    remote_exists = bool(remote_ls.stdout.strip())
    if remote_exists:
        print("文件在远程仓库中已存在", flush=True)
    else:
        print("文件在远程仓库中不存在", flush=True)

    if not os.path.exists("plugin_cache_original.json"):
        raise RuntimeError("❌ 本地文件不存在")

    plugin_cache = load_json("plugin_cache_original.json", None)
    if plugin_cache is None:
        raise RuntimeError("❌ JSON格式无效")
    print("✅ JSON格式有效", flush=True)

    run_cmd(["git", "add", "plugin_cache_original.json"])

    if remote_exists:
        diff = subprocess.run(["git", "diff", "--cached", "--exit-code", "--", "plugin_cache_original.json"], text=True, capture_output=True)
        has_changes = diff.returncode != 0
        if has_changes:
            print("✅ 检测到文件内容变更", flush=True)
        else:
            print("ℹ️ 文件内容没有变化", flush=True)
    else:
        has_changes = True
        print("✅ 这是新文件，需要提交", flush=True)

    status = run_cmd(["git", "status"], check=False)
    print("Git 状态:", flush=True)
    if status.stdout:
        print(status.stdout, flush=True)
    return has_changes


def commit_and_push_changes(repo_info: Dict[str, Any]) -> None:
    print("验证Git认证状态...", flush=True)
    auth = subprocess.run(["git", "ls-remote", "origin", "HEAD"], text=True, capture_output=True)
    if auth.returncode != 0:
        raise RuntimeError("❌ Git认证失败，检查PAT_TOKEN权限")
    print("✅ Git认证成功", flush=True)

    run_cmd(["git", "add", "plugin_cache_original.json"])
    plugin_cache = load_json("plugin_cache_original.json", {})
    total_plugins = len(plugin_cache) if isinstance(plugin_cache, dict) else 0
    success_repos = sum(1 for v in repo_info.values() if v.get("status") == "success")
    timestamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
    commit_message = f"🔄 Update plugin cache: {total_plugins} plugins, {success_repos} fresh updates - {timestamp}"

    commit = subprocess.run(["git", "commit", "-m", commit_message], text=True, capture_output=True)
    if commit.returncode != 0:
        raise RuntimeError(f"❌ 提交失败: {commit.stderr.strip() or commit.stdout.strip()}")

    push = subprocess.run(["git", "push", "origin", "HEAD"], text=True, capture_output=True)
    if push.returncode != 0:
        raise RuntimeError(f"❌ 推送失败: {push.stderr.strip() or push.stdout.strip()}")
    print("✅ 成功推送到远程仓库", flush=True)


def clean_up() -> None:
    files = [
        "temp_plugin_cache_original.json",
        "temp_response.txt",
        "temp_headers.txt",
        "original_plugins.json",
        "repo_info.json",
        "temp_repo_info.json",
        "existing_cache.json",
    ]
    for path in files:
        if os.path.exists(path):
            os.remove(path)
    for path in glob.glob("temp_api_headers_*.txt"):
        if os.path.exists(path):
            os.remove(path)
    print("🧹 临时文件清理完成", flush=True)


def print_summary(should_update: bool, has_changes: bool) -> None:
    if should_update:
        if has_changes:
            print("✅ 插件数据已成功转换并提交", flush=True)
            if os.path.exists("plugin_cache_original.json"):
                plugin_cache = load_json("plugin_cache_original.json", {})
                total_plugins = len(plugin_cache) if isinstance(plugin_cache, dict) else 0
                print(f"📊 最终结果: {total_plugins} 个插件已更新", flush=True)
        else:
            print("ℹ️ 数据获取和转换成功，但内容未发生变化", flush=True)
    else:
        print("❌ 由于网络问题、GitHub服务错误或数据异常，跳过了数据转换", flush=True)
        print("请检查GitHub服务状态或查看上面的错误详情", flush=True)


def main() -> int:
    should_update = False
    has_changes = False
    repo_info: Dict[str, Any] = {}

    try:
        configure_git()
        should_update, original_plugins = fetch_original_plugin_data()
        existing_cache: Dict[str, Any] = {}
        has_existing_cache = False

        if should_update:
            has_existing_cache, existing_cache = load_existing_cache()
            repo_info = get_repo_info(original_plugins, existing_cache, has_existing_cache)
            transform_plugin_data(original_plugins, repo_info, existing_cache)

        pull_latest_changes_before_checking()

        if should_update:
            has_changes = check_for_changes()
            if has_changes:
                commit_and_push_changes(repo_info)
    except Exception as e:
        print(str(e), flush=True)
        print_summary(should_update, has_changes)
        clean_up()
        return 1

    print_summary(should_update, has_changes)
    clean_up()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
