#!/usr/bin/env bash

echo "开始获取仓库信息..."

# 创建一个临时文件存储仓库信息
echo "{}" > repo_info.json

# 初始化统计计数器
total_repos=0
success_count=0
failed_count=0
deleted_count=0
network_error_count=0
redirect_count=0

# 重试配置
MAX_RETRIES=5
BASE_DELAY=2
MAX_DELAY=30

# 重试函数
retry_api_call() {
  local owner="$1"
  local repo="$2"
  local attempt="$3"

  # 计算退避延迟 (指数退避 + 随机抖动)
  local delay=$((BASE_DELAY * (2 ** (attempt - 1))))
  if [ $delay -gt $MAX_DELAY ]; then
    delay=$MAX_DELAY
  fi
  # 添加随机抖动 (0-50% 的延迟时间)
  local jitter=$((RANDOM % (delay / 2 + 1)))
  delay=$((delay + jitter))

  echo "    第 $attempt 次尝试 (延迟 ${delay}s)..."
  sleep $delay

  # 使用临时文件捕获响应头
  local temp_headers="temp_api_headers_${total_repos}_${attempt}.txt"

  # 执行API调用，增强网络配置
  local response=$(curl -L -s \
    --max-time 20 \
    --connect-timeout 10 \
    --retry 0 \
    --max-redirs 5 \
    --keepalive-time 60 \
    --tcp-nodelay \
    -H "Authorization: token $PAT_TOKEN" \
    -H "Accept: application/vnd.github.v3+json" \
    -H "User-Agent: GitHub-Action-Plugin-Transformer" \
    -H "Connection: keep-alive" \
    -D "$temp_headers" \
    -w "HTTPSTATUS:%{http_code}:CURL_EXIT:%{exitcode}" \
    "https://api.github.com/repos/$owner/$repo" 2>/dev/null || echo "CURL_ERROR:-1")

  # 解析响应
  if [[ "$response" == "CURL_ERROR"* ]]; then
    rm -f "$temp_headers"
    return 1
  fi

  # 提取状态码和curl退出码
  local http_code=$(echo "$response" | grep -o "HTTPSTATUS:[0-9]*" | cut -d: -f2)
  local curl_exit=$(echo "$response" | grep -o "CURL_EXIT:[0-9]*" | cut -d: -f2)
  local body=$(echo "$response" | sed 's/HTTPSTATUS:[0-9]*:CURL_EXIT:[0-9]*$//')

  # 检查curl退出码
  if [ "$curl_exit" != "0" ]; then
    echo "    CURL错误码: $curl_exit"
    rm -f "$temp_headers"
    return 1
  fi

  # 检查HTTP状态码是否需要重试
  case "$http_code" in
    200)
      # 验证响应是否为有效JSON
      if echo "$body" | jq -e '.stargazers_count' > /dev/null 2>&1; then
        echo "$body"
        rm -f "$temp_headers"
        return 0
      else
        echo "    响应不是有效JSON"
        rm -f "$temp_headers"
        return 1
      fi
      ;;
    429|502|503|504)
      # 这些状态码应该重试
      echo "    临时错误 HTTP $http_code，将重试"
      rm -f "$temp_headers"
      return 1
      ;;
    301|302|404|403)
      # 这些状态码不应该重试，直接返回
      echo "$body:HTTP:$http_code"
      rm -f "$temp_headers"
      return 0
      ;;
    *)
      echo "    未知HTTP状态码: $http_code"
      rm -f "$temp_headers"
      return 1
      ;;
  esac
}

# 从原始数据中提取所有仓库URL
jq -r 'to_entries[] | .value.repo // empty' original_plugins.json | while read -r repo_url; do
  # 提取GitHub用户名和仓库名
  if [[ "$repo_url" =~ https://github\.com/([^/]+)/([^/]+) ]]; then
    owner="${BASH_REMATCH[1]}"
    repo="${BASH_REMATCH[2]}"
    total_repos=$((total_repos + 1))

    echo "[$total_repos] 获取仓库信息: $owner/$repo"

    # 执行重试逻辑
    api_response=""
    success=false

    for attempt in $(seq 1 $MAX_RETRIES); do
      if [ $attempt -eq 1 ]; then
        echo "  初次尝试..."
        # 第一次尝试，无延迟
        temp_headers="temp_api_headers_${total_repos}_1.txt"
        api_response=$(curl -L -s \
          --max-time 15 \
          --connect-timeout 8 \
          --retry 0 \
          --max-redirs 5 \
          -H "Authorization: token $PAT_TOKEN" \
          -H "Accept: application/vnd.github.v3+json" \
          -H "User-Agent: GitHub-Action-Plugin-Transformer" \
          -D "$temp_headers" \
          -w "HTTPSTATUS:%{http_code}" \
          "https://api.github.com/repos/$owner/$repo" 2>/dev/null || echo "CURL_ERROR")

        if [[ "$api_response" != "CURL_ERROR" ]]; then
          http_code=$(echo "$api_response" | grep -o "HTTPSTATUS:[0-9]*" | cut -d: -f2)
          api_response=$(echo "$api_response" | sed 's/HTTPSTATUS:[0-9]*$//')

          # 检查是否成功或不需要重试的错误
          case "$http_code" in
            200)
              if echo "$api_response" | jq -e '.stargazers_count' > /dev/null 2>&1; then
                success=true
                break
              fi
              ;;
            301|302|404|403)
              # 不需要重试的状态码
              success=true
              break
              ;;
            429|502|503|504)
              # 需要重试的状态码
              echo "  临时错误 HTTP $http_code，准备重试"
              ;;
          esac
        fi
        rm -f "$temp_headers"
      else
        # 重试调用
        retry_response=$(retry_api_call "$owner" "$repo" "$attempt")
        if [ $? -eq 0 ]; then
          api_response="$retry_response"
          success=true
          break
        fi
      fi

      # 如果不是最后一次尝试，显示重试信息
      if [ $attempt -lt $MAX_RETRIES ]; then
        echo "  尝试 $attempt/$MAX_RETRIES 失败，准备重试..."
      fi
    done

    # 处理最终结果
    stars=0
    updated_at=""
    version=""
    astrbot_version=""
    support_platforms=""
    logo=""
    status="unknown"

    if [ "$success" = true ]; then
      # 检查是否包含HTTP状态码信息
      if [[ "$api_response" == *":HTTP:"* ]]; then
        http_code=$(echo "$api_response" | grep -o ":HTTP:[0-9]*" | cut -d: -f3)
        api_response=$(echo "$api_response" | sed 's/:HTTP:[0-9]*$//')
      fi

      case "$http_code" in
        200)
          if echo "$api_response" | jq -e '.stargazers_count' > /dev/null 2>&1; then
            stars=$(echo "$api_response" | jq -r '.stargazers_count // 0')
            updated_at=$(echo "$api_response" | jq -r '.updated_at // ""')
            success_count=$((success_count + 1))
            status="success"

            echo "  ✅ 成功 - Stars: $stars, 更新时间: $updated_at"

            # 获取metadata版本
            for metadata_file in "metadata.yml" "metadata.yaml"; do
              metadata_response=$(curl -L -s --max-time 10 --max-redirs 3 \
                -H "Authorization: token $PAT_TOKEN" \
                -H "Accept: application/vnd.github.v3.raw" \
                -H "User-Agent: GitHub-Action-Plugin-Transformer" \
                "https://api.github.com/repos/$owner/$repo/contents/$metadata_file" 2>/dev/null || echo "{}")

              if [[ ! "$metadata_response" =~ "Not Found" ]] && [[ ! "$metadata_response" =~ "Bad Gateway" ]]; then
                # 检查是否是base64编码的内容
                if echo "$metadata_response" | jq -e '.content' > /dev/null 2>&1; then
                  metadata_content=$(echo "$metadata_response" | jq -r '.content' | base64 -d 2>/dev/null || echo "")
                else
                  metadata_content="$metadata_response"
                fi

                # 尝试解析YAML并提取版本
                if [ ! -z "$metadata_content" ]; then
                  parsed_version=$(echo "$metadata_content" | grep -E "^version:\s*['\"]?([^'\"]+)['\"]?" | sed -E "s/version:\s*['\"]?([^'\"]+)['\"]?/\1/" || echo "")
                  # 去除注释和多余的空白字符
                  cleaned_version=$(echo "$parsed_version" | sed -E 's/[#].*$//' | sed -E 's/\r$//' | xargs)
                  if [ ! -z "$cleaned_version" ]; then
                    version="$cleaned_version"
                  fi

                  parsed_astrbot_version=$(echo "$metadata_content" | grep -E "^astrbot_version:\s*['\"]?([^'\"]+)['\"]?" | sed -E "s/astrbot_version:\s*['\"]?([^'\"]+)['\"]?/\1/" || echo "")
                  cleaned_astrbot_version=$(echo "$parsed_astrbot_version" | sed -E 's/[#].*$//' | sed -E 's/\r$//' | xargs)
                  if [ ! -z "$cleaned_astrbot_version" ]; then
                    astrbot_version="$cleaned_astrbot_version"
                  fi

                  parsed_support_platforms=$(echo "$metadata_content" | grep -E "^support_platforms:\s*['\"]?([^'\"]+)['\"]?" | sed -E "s/support_platforms:\s*['\"]?([^'\"]+)['\"]?/\1/" || echo "")
                  cleaned_support_platforms=$(echo "$parsed_support_platforms" | sed -E 's/[#].*$//' | sed -E 's/\r$//' | xargs)
                  if [ ! -z "$cleaned_support_platforms" ]; then
                    support_platforms="$cleaned_support_platforms"
                  fi

                  if [ ! -z "$version" ] || [ ! -z "$astrbot_version" ] || [ ! -z "$support_platforms" ]; then
                    break
                  fi
                fi
              fi
            done

            # 检查logo.png是否存在
            logo_response=$(curl -L -s --max-time 10 --max-redirs 3 \
              -H "Authorization: token $PAT_TOKEN" \
              -H "Accept: application/vnd.github.v3+json" \
              -H "User-Agent: GitHub-Action-Plugin-Transformer" \
              "https://api.github.com/repos/$owner/$repo/contents/logo.png" 2>/dev/null || echo "{}")

            # 检查logo.png是否存在（验证响应包含name字段且不是错误消息）
            # 使用单个jq调用进行验证以提高性能
            logo_valid=$(echo "$logo_response" | jq -r 'if .name and (.message | not) then "true" else "false" end' 2>/dev/null || echo "false")
            if [ "$logo_valid" = "true" ]; then
              # 获取默认分支
              default_branch=$(echo "$api_response" | jq -r '.default_branch // "main"')
              logo="https://raw.githubusercontent.com/$owner/$repo/$default_branch/logo.png"
              echo "  🖼️  找到logo: $logo"
            fi
          fi
          ;;
        301|302)
          echo "  🔄 仓库重定向 ($http_code)"
          redirect_count=$((redirect_count + 1))
          status="redirected"
          ;;
        404)
          echo "  🗑️  仓库已删除或不可访问 (404)"
          deleted_count=$((deleted_count + 1))
          status="deleted"
          ;;
        403)
          echo "  ⚠️  API限制或访问被拒绝 (403)"
          failed_count=$((failed_count + 1))
          status="api_limit"
          ;;
      esac
    else
      echo "  ❌ 所有重试均失败"
      network_error_count=$((network_error_count + 1))
      status="network_error"
    fi

    # 如果失败，尝试使用缓存数据
    if [ "$status" != "success" ] && [ "$HAS_EXISTING_CACHE" = "true" ]; then
      cached_data=$(jq -r --arg url "$repo_url" '.data // {} | to_entries[] | select(.value.repo == $url) | .value | {stars: .stars, updated_at: .updated_at, version: .version, astrbot_version: .astrbot_version, support_platforms: .support_platforms, logo: .logo}' existing_cache.json 2>/dev/null || echo "{}")

      if [ "$cached_data" != "{}" ] && [ "$cached_data" != "" ]; then
        cached_stars=$(echo "$cached_data" | jq -r '.stars // 0')
        cached_updated=$(echo "$cached_data" | jq -r '.updated_at // ""')
        cached_version=$(echo "$cached_data" | jq -r '.version // ""')
        cached_astrbot_version=$(echo "$cached_data" | jq -r '.astrbot_version // ""')
        cached_support_platforms=$(echo "$cached_data" | jq -r '.support_platforms // ""')
        cached_logo=$(echo "$cached_data" | jq -r '.logo // ""')

        if [ "$cached_stars" != "0" ] || [ "$cached_updated" != "" ]; then
          echo "  🔄 使用缓存数据: Stars: $cached_stars"
          stars="$cached_stars"
          updated_at="$cached_updated"
          version="$cached_version"
          astrbot_version="$cached_astrbot_version"
          support_platforms="$cached_support_platforms"
          logo="$cached_logo"
          status="cached"
        fi
      fi
    fi

    # 将信息添加到repo_info.json
    jq --arg url "$repo_url" \
       --arg stars "$stars" \
       --arg updated "$updated_at" \
       --arg version "$version" \
       --arg astrbot_version "$astrbot_version" \
       --arg support_platforms "$support_platforms" \
       --arg logo "$logo" \
       --arg status "$status" \
       '. + {($url): {stars: ($stars | tonumber), updated_at: $updated, version: $version, astrbot_version: $astrbot_version, support_platforms: $support_platforms, logo: $logo, status: $status}}' \
       repo_info.json > temp_repo_info.json && mv temp_repo_info.json repo_info.json

    # 添加基础延迟避免API限制
    sleep 0.5
  fi
done

# 成功率检查
if [ $total_repos -gt 0 ]; then
  success_rate=$((success_count * 100 / total_repos))
  echo "📈 成功率: $success_rate%"

  if [ $success_rate -lt 50 ]; then
    echo "⚠️  警告: 成功率过低，可能存在网络问题或GitHub服务异常"
    if [ "$HAS_EXISTING_CACHE" = "true" ]; then
      echo "已启用缓存回退机制"
    fi
  fi
fi

echo "✅ 仓库信息获取完成"

